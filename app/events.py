"""连续超阈值事件的查询与确认。

规则
----
* 只使用每小时**最高修订号**且**有效**（非撤回）的浓度；
* 连续超过阈值（严格大于）的小时组成一个事件，至少 ``MIN_EVENT_HOURS`` 小时；
* 缺测（从未上报）或最新修订为撤回的小时会截断事件；
* 事件按绝对时间连续，因此 23 点之后接次日的 0 点仍然连续；
* 查询区间按北京时间日期、含两端，最多 ``MAX_EVENT_QUERY_DAYS`` 天。

一致性
------
* 事件及构成小时的修订号来自同一条 ``SELECT``（WAL 下单条语句看到一致快照），
  因此事件的边界、峰值与修订号必然属于同一份数据库状态。
* 确认在 ``BEGIN IMMEDIATE`` 写事务内**重新计算**事件并校验，通过后才插入确认
  记录；与写入并发时由 SQLite 写锁串行化，因此按提交顺序判定：
  先提交的写入会让随后的确认看到新事件而拒绝过期确认，
  先提交的确认则在写入生效前成立。过期确认抛错并整体回滚，不改动任何状态。
* ``event_id`` 是事件**内容指纹**（站点、阈值、每个构成小时的时刻与浓度）。
  补报 / 修订 / 撤回只要改变了事件的构成小时或浓度，指纹就会变化，
  旧确认不会再与任何新事件匹配；未受影响的事件指纹不变，旧确认继续有效。
  纯修订号变化（同一小时重新报同一浓度）不改变内容，视为未受影响。
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass

from .config import BEIJING_TIMEZONE, MIN_EVENT_HOURS
from .db import Database, utc_timestamp

BEIJING_TZ = dt.timezone(dt.timedelta(hours=8), BEIJING_TIMEZONE)


class StaleConfirmationError(Exception):
    """提交的事件在本次数据库状态中不存在（内容或边界已改变）。"""

    def __init__(
        self,
        requested_event_ids: list[str],
        available_event_ids: list[str],
    ) -> None:
        self.requested_event_ids = requested_event_ids
        self.available_event_ids = available_event_ids
        super().__init__("confirmation does not match any current event")


class InvalidEventReferenceError(Exception):
    """事件定位参数无法解析（例如起止时刻格式错误）。"""


@dataclass(frozen=True)
class EventHour:
    date: str
    hour: int
    value: float
    revision: int

    @property
    def time(self) -> str:
        return hour_time(self.date, self.hour)

    def as_dict(self) -> dict:
        return {
            "time": self.time,
            "date": self.date,
            "hour": self.hour,
            "value": self.value,
            "revision": self.revision,
        }


@dataclass(frozen=True)
class Event:
    station_id: str
    threshold: float
    event_id: str
    start_time: str
    end_time: str
    duration_hours: int
    peak_value: float
    peak_time: str
    hours: tuple[EventHour, ...]

    @property
    def start_date(self) -> str:
        return self.hours[0].date

    @property
    def end_date(self) -> str:
        return self.hours[-1].date

    @property
    def min_value(self) -> float:
        return min(h.value for h in self.hours)

    def as_dict(self, confirmation_id: str | None = None) -> dict:
        return {
            "event_id": self.event_id,
            "start_time": self.start_time,
            "end_time": self.end_time,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "duration_hours": self.duration_hours,
            "peak_value": self.peak_value,
            "peak_time": self.peak_time,
            "peak_revision": next(
                h.revision for h in self.hours if h.time == self.peak_time
            ),
            "min_value": self.min_value,
            "revisions": [h.revision for h in self.hours],
            "hours": [h.as_dict() for h in self.hours],
            "confirmed": confirmation_id is not None,
            "confirmation_id": confirmation_id,
        }


@dataclass(frozen=True)
class Confirmation:
    confirmation_id: str
    station_id: str
    start_date: str
    end_date: str
    threshold: float
    event_id: str
    event_start: str
    event_end: str
    peak_value: float
    duration_hours: int
    confirmed_at: str
    event: Event | None = None
    idempotent: bool = False
    current: bool | None = None

    def as_dict(self) -> dict:
        body = {
            "confirmation_id": self.confirmation_id,
            "station_id": self.station_id,
            "start_date": self.start_date,
            "end_date": self.end_date,
            "threshold": self.threshold,
            "event_id": self.event_id,
            "event_start": self.event_start,
            "event_end": self.event_end,
            "duration_hours": self.duration_hours,
            "peak_value": self.peak_value,
            "confirmed_at": self.confirmed_at,
            "idempotent": self.idempotent,
        }
        if self.event is not None:
            body["event"] = self.event.as_dict(self.confirmation_id)
        if self.current is not None:
            body["current"] = self.current
        return body


# ------------------------------------------------------------------ 时间工具
def hour_time(obs_date: str, hour: int) -> str:
    """北京时间整点小时的 ISO 8601 表示，例如 ``2025-03-01T23:00:00+08:00``。"""
    day = dt.date.fromisoformat(obs_date)
    return dt.datetime.combine(day, dt.time(hour), tzinfo=BEIJING_TZ).isoformat()


def parse_beijing_time(value: str) -> dt.datetime:
    """解析事件起止时刻；无时区信息时按北京时间理解。"""
    text = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(text)
    except ValueError as exc:
        raise InvalidEventReferenceError(f"invalid timestamp: {value!r}") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=BEIJING_TZ)
    return parsed


def iter_range(start_date: dt.date, end_date: dt.date):
    day = start_date
    while day <= end_date:
        yield day
        day += dt.timedelta(days=1)


def _hours_match(event: Event, expected: list[dict]) -> bool:
    """校验调用方回传的构成小时快照是否与当前事件内容一致。

    比较的是事件内容（每个小时的时刻与浓度），与 ``event_id`` 的指纹口径相同：
    只要内容与边界完全一致就算匹配，未知字段忽略。修订号参与「所见事件」的
    展示，但同一小时以更大修订号重报相同浓度时内容没有改变，
    因此不因修订号变化而判定过期。
    """
    if len(expected) != len(event.hours):
        return False
    pairs: list[tuple[dt.datetime, float]] = []
    for want in expected:
        if not isinstance(want, dict):
            return False
        time_value = want.get("time")
        value = want.get("value")
        if time_value is None or value is None:
            return False
        try:
            moment = parse_beijing_time(str(time_value))
            concentration = float(value)
        except (InvalidEventReferenceError, TypeError, ValueError):
            return False
        pairs.append((moment, concentration))
    actual = [
        (parse_beijing_time(item.time), item.value) for item in event.hours
    ]
    return pairs == actual


# ------------------------------------------------------------------ 事件计算
def compute_events(
    station_id: str,
    threshold: float,
    start_date: dt.date,
    end_date: dt.date,
    rows: list[sqlite3.Row],
) -> list[Event]:
    """从「区间内每小时最新修订」的行计算事件。

    ``rows`` 来自 :meth:`EventService._latest_rows`，按 (obs_date, hour) 排序。
    """
    latest: dict[tuple[str, int], sqlite3.Row] = {
        (row["obs_date"], row["hour"]): row for row in rows
    }

    events: list[Event] = []
    run: list[EventHour] = []

    def close_run() -> None:
        if len(run) >= MIN_EVENT_HOURS:
            events.append(_build_event(station_id, threshold, tuple(run)))
        run.clear()

    for day in iter_range(start_date, end_date):
        obs_date = day.isoformat()
        for hour in range(24):
            row = latest.get((obs_date, hour))
            if row is None or row["retracted"]:
                # 未报或撤回：没有有效浓度，事件在此截断。
                close_run()
                continue
            value = float(row["value"])
            if value > threshold:
                run.append(
                    EventHour(
                        date=obs_date,
                        hour=hour,
                        value=value,
                        revision=int(row["revision"]),
                    )
                )
            else:
                close_run()

    close_run()
    return events


def _build_event(station_id: str, threshold: float, hours: tuple[EventHour, ...]) -> Event:
    peak = hours[0]
    for item in hours:
        if item.value > peak.value:
            peak = item
    return Event(
        station_id=station_id,
        threshold=threshold,
        event_id=event_fingerprint(station_id, threshold, hours),
        start_time=hours[0].time,
        end_time=hours[-1].time,
        duration_hours=len(hours),
        peak_value=peak.value,
        peak_time=peak.time,
        hours=hours,
    )


def event_fingerprint(
    station_id: str, threshold: float, hours: tuple[EventHour, ...]
) -> str:
    """事件内容指纹：站点 + 阈值 + 每个构成小时的时刻与浓度。

    边界（起止时刻、时长、峰值）都由构成小时隐含，因此边界变化必然改变指纹。
    指纹只覆盖「内容」：修订号本身不是内容，同一小时用更大修订号重报相同浓度
    时事件内容与边界都没有改变，旧确认仍然有效；而任何改变浓度、增加 / 删除
    构成小时（补报延伸、撤回截断、修订改写）的操作都会改变指纹，
    使旧确认不可能再匹配到新事件。
    """
    payload = {
        "station_id": station_id,
        "threshold": threshold,
        "hours": [[h.time, h.value] for h in hours],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ 服务
_LATEST_SQL = """
SELECT r.obs_date, r.hour, r.value, r.retracted, r.revision
FROM pm25_readings AS r
WHERE r.station_id = ?
  AND r.obs_date BETWEEN ? AND ?
  AND r.revision = (
      SELECT MAX(revision)
      FROM pm25_readings AS m
      WHERE m.station_id = r.station_id
        AND m.obs_date = r.obs_date
        AND m.hour = r.hour
  )
ORDER BY r.obs_date, r.hour
"""


class EventService:
    def __init__(self, database: Database) -> None:
        self.db = database

    @staticmethod
    def _latest_rows(
        conn: sqlite3.Connection, station_id: str, start_date: dt.date, end_date: dt.date
    ) -> list[sqlite3.Row]:
        return conn.execute(
            _LATEST_SQL, (station_id, start_date.isoformat(), end_date.isoformat())
        ).fetchall()

    def _current_events(
        self, conn: sqlite3.Connection, station_id: str, start_date: dt.date, end_date: dt.date,
        threshold: float,
    ) -> list[Event]:
        rows = self._latest_rows(conn, station_id, start_date, end_date)
        return compute_events(station_id, threshold, start_date, end_date, rows)

    # -- 查询 -----------------------------------------------------------
    def list_events(
        self,
        station_id: str,
        start_date: dt.date,
        end_date: dt.date,
        threshold: float,
    ) -> tuple[list[Event], dict[str, str]]:
        """返回 (事件列表, event_id → confirmation_id)。

        事件与确认状态在同一个读事务内读取，二者来自同一份数据库状态。
        """
        conn = self.db.connect()
        try:
            conn.execute("BEGIN")
            events = self._current_events(conn, station_id, start_date, end_date, threshold)
            links = self._confirmation_links(
                conn, station_id, start_date, end_date, threshold, events
            )
            conn.execute("COMMIT")
            return events, links
        finally:
            conn.close()

    def _confirmation_links(
        self,
        conn: sqlite3.Connection,
        station_id: str,
        start_date: dt.date,
        end_date: dt.date,
        threshold: float,
        events: list[Event],
    ) -> dict[str, str]:
        """找出与当前事件严格匹配（区间、阈值、event_id）的确认。"""
        if not events:
            return {}
        event_ids = sorted(event.event_id for event in events)
        links: dict[str, str] = {}
        chunk_size = 200
        for start in range(0, len(event_ids), chunk_size):
            chunk = event_ids[start : start + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            rows = conn.execute(
                f"""
                SELECT event_id, confirmation_id
                FROM pm25_event_confirmations
                WHERE station_id = ?
                  AND start_date = ?
                  AND end_date = ?
                  AND threshold = ?
                  AND event_id IN ({placeholders})
                ORDER BY confirmed_at, confirmation_id
                """,
                (
                    station_id,
                    start_date.isoformat(),
                    end_date.isoformat(),
                    threshold,
                    *chunk,
                ),
            ).fetchall()
            for row in rows:
                # 同一事件重复确认是幂等的，展示最早的一条。
                links.setdefault(row["event_id"], row["confirmation_id"])
        return links

    # -- 确认 -----------------------------------------------------------
    def confirm(
        self,
        station_id: str,
        start_date: dt.date,
        end_date: dt.date,
        threshold: float,
        *,
        event_ids: list[str] | None = None,
        event_start: str | None = None,
        event_end: str | None = None,
        expected_hours: list[dict] | None = None,
    ) -> list[Confirmation]:
        """校验并记录确认，返回本次确认（或已存在的确认）列表。

        事件标识可以是 ``event_ids``（一个或多个事件指纹），也可以用事件的
        起止时刻定位；都不给时确认该查询区间内当前的全部事件。
        同时给出时以 ``event_ids`` 为准（每个指纹已唯一确定事件）。

        ``expected_hours`` 是调用方所见的构成小时快照；给出时会校验每个小时的
        时刻与浓度是否与提交时刻的数据库状态完全一致（与 ``event_id`` 的指纹
        口径相同，因此单纯修订号变化不会被误判为过期）。

        整个过程在单个 ``BEGIN IMMEDIATE`` 事务内完成：先按提交时刻的数据库
        状态重算事件，任何一个引用的事件不存在（内容或边界已改变）就整体回滚
        并抛 :class:`StaleConfirmationError`，不会留下部分确认，也不会删除旧确认。
        """
        if (event_start is None) != (event_end is None):
            raise InvalidEventReferenceError(
                "event_start and event_end must be provided together"
            )

        if event_ids is not None and not event_ids:
            raise InvalidEventReferenceError("at least one event_id is required")
        if event_ids is not None:
            # event_id 是内容指纹，已唯一确定事件；忽略同时给出的时刻。
            event_start = event_end = None

        wanted_start = parse_beijing_time(event_start) if event_start else None
        wanted_end = parse_beijing_time(event_end) if event_end else None

        conn = self.db.connect()
        try:
            with Database.write_transaction(conn):
                events = self._current_events(
                    conn, station_id, start_date, end_date, threshold
                )

                targets = self._select_targets(
                    events,
                    requested_ids=event_ids,
                    wanted_start=wanted_start,
                    wanted_end=wanted_end,
                )

                if expected_hours is not None:
                    for event in targets:
                        if not _hours_match(event, expected_hours):
                            raise StaleConfirmationError(
                                [event.event_id], sorted(e.event_id for e in events)
                            )

                confirmations: list[Confirmation] = []
                for event in targets:
                    existing = self._find_confirmation(
                        conn, station_id, start_date, end_date, threshold, event.event_id
                    )
                    if existing is not None:
                        confirmations.append(existing)
                        continue
                    try:
                        confirmations.append(
                            self._insert_confirmation(
                                conn, station_id, start_date, end_date, threshold, event
                            )
                        )
                    except sqlite3.IntegrityError:
                        # 极少数情况下（例如跨进程并发）唯一约束先触发：
                        # 说明同一条确认已存在，转为幂等返回。
                        existing = self._find_confirmation(
                            conn, station_id, start_date, end_date, threshold, event.event_id
                        )
                        if existing is None:
                            raise
                        confirmations.append(existing)
                return confirmations
        finally:
            conn.close()

    @staticmethod
    def _select_targets(
        events: list[Event],
        *,
        requested_ids: list[str] | None,
        wanted_start: dt.datetime | None,
        wanted_end: dt.datetime | None,
    ) -> list[Event]:
        if wanted_start is not None and wanted_end is not None:
            matched = [
                event
                for event in events
                if parse_beijing_time(event.start_time) == wanted_start
                and parse_beijing_time(event.end_time) == wanted_end
            ]
            if not matched:
                raise StaleConfirmationError(
                    [f"{wanted_start.isoformat()}/{wanted_end.isoformat()}"],
                    sorted(event.event_id for event in events),
                )
            return matched

        if requested_ids is None:
            # 未指定具体事件：确认本次查询所见全部事件。
            if not events:
                raise StaleConfirmationError([], [])
            return list(events)

        by_id = {event.event_id: event for event in events}
        missing = [event_id for event_id in requested_ids if event_id not in by_id]
        if missing:
            raise StaleConfirmationError(missing, sorted(by_id))
        # 去重并保持提交顺序。
        seen: set[str] = set()
        targets: list[Event] = []
        for event_id in requested_ids:
            if event_id in seen:
                continue
            seen.add(event_id)
            targets.append(by_id[event_id])
        return targets

    def _insert_confirmation(
        self,
        conn: sqlite3.Connection,
        station_id: str,
        start_date: dt.date,
        end_date: dt.date,
        threshold: float,
        event: Event,
    ) -> Confirmation:
        confirmation_id = uuid.uuid4().hex
        confirmed_at = utc_timestamp()
        conn.execute(
            """
            INSERT INTO pm25_event_confirmations
                (confirmation_id, station_id, start_date, end_date, threshold,
                 event_id, event_start, event_end, peak_value, duration_hours,
                 source_hours, confirmed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                confirmation_id,
                station_id,
                start_date.isoformat(),
                end_date.isoformat(),
                threshold,
                event.event_id,
                event.start_time,
                event.end_time,
                event.peak_value,
                event.duration_hours,
                json.dumps(
                    [h.as_dict() for h in event.hours],
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                confirmed_at,
            ),
        )
        return Confirmation(
            confirmation_id=confirmation_id,
            station_id=station_id,
            start_date=start_date.isoformat(),
            end_date=end_date.isoformat(),
            threshold=threshold,
            event_id=event.event_id,
            event_start=event.start_time,
            event_end=event.end_time,
            peak_value=event.peak_value,
            duration_hours=event.duration_hours,
            confirmed_at=confirmed_at,
            event=event,
        )

    @staticmethod
    def _find_confirmation(
        conn: sqlite3.Connection,
        station_id: str,
        start_date: dt.date,
        end_date: dt.date,
        threshold: float,
        event_id: str,
    ) -> Confirmation | None:
        row = conn.execute(
            """
            SELECT confirmation_id, station_id, start_date, end_date, threshold,
                   event_id, event_start, event_end, peak_value, duration_hours,
                   confirmed_at
            FROM pm25_event_confirmations
            WHERE station_id = ?
              AND start_date = ?
              AND end_date = ?
              AND threshold = ?
              AND event_id = ?
            ORDER BY confirmed_at, confirmation_id
            LIMIT 1
            """,
            (station_id, start_date.isoformat(), end_date.isoformat(), threshold, event_id),
        ).fetchone()
        if row is None:
            return None
        return Confirmation(
            confirmation_id=row["confirmation_id"],
            station_id=row["station_id"],
            start_date=row["start_date"],
            end_date=row["end_date"],
            threshold=row["threshold"],
            event_id=row["event_id"],
            event_start=row["event_start"],
            event_end=row["event_end"],
            peak_value=row["peak_value"],
            duration_hours=row["duration_hours"],
            confirmed_at=row["confirmed_at"],
            idempotent=True,
        )

    # -- 确认历史 -------------------------------------------------------
    def list_confirmations(self, station_id: str) -> list[dict]:
        """列出站点的全部确认，并标记其事件在当前数据下是否仍然成立。"""
        conn = self.db.connect()
        try:
            conn.execute("BEGIN")
            stored = conn.execute(
                """
                SELECT confirmation_id, station_id, start_date, end_date, threshold,
                       event_id, event_start, event_end, peak_value, duration_hours,
                       confirmed_at
                FROM pm25_event_confirmations
                WHERE station_id = ?
                ORDER BY confirmed_at, confirmation_id
                """,
                (station_id,),
            ).fetchall()

            current_ids: dict[tuple[str, str, float], set[str]] = {}
            result: list[dict] = []
            for row in stored:
                key = (row["start_date"], row["end_date"], row["threshold"])
                if key not in current_ids:
                    events = self._current_events(
                        conn,
                        station_id,
                        dt.date.fromisoformat(row["start_date"]),
                        dt.date.fromisoformat(row["end_date"]),
                        row["threshold"],
                    )
                    current_ids[key] = {event.event_id for event in events}
                confirmation = Confirmation(
                    confirmation_id=row["confirmation_id"],
                    station_id=row["station_id"],
                    start_date=row["start_date"],
                    end_date=row["end_date"],
                    threshold=row["threshold"],
                    event_id=row["event_id"],
                    event_start=row["event_start"],
                    event_end=row["event_end"],
                    peak_value=row["peak_value"],
                    duration_hours=row["duration_hours"],
                    confirmed_at=row["confirmed_at"],
                    current=row["event_id"] in current_ids[key],
                )
                result.append(confirmation.as_dict())
            conn.execute("COMMIT")
            return result
        finally:
            conn.close()
