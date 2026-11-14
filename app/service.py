"""业务逻辑：批量写入与日报查询。"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass

from .config import HOURS_PER_DAY, REQUIRED_VALID_HOURS
from .db import Database, utc_timestamp
from .models import (
    ConflictDetail,
    DailyReport,
    HourState,
    ReadingRecord,
)


class RevisionConflictError(Exception):
    """同号不同内容：整批拒绝。"""

    def __init__(self, conflicts: list[ConflictDetail]) -> None:
        self.conflicts = conflicts
        super().__init__(f"{len(conflicts)} revision conflict(s)")


@dataclass(frozen=True)
class BatchOutcome:
    received: int
    created: int
    unchanged: int
    stations: list[str]
    dates: list[str]


class ReadingService:
    def __init__(self, database: Database) -> None:
        self.db = database

    # -- 批量写入 -------------------------------------------------------
    def apply_batch(self, records: list[ReadingRecord]) -> BatchOutcome:
        """原子地应用一批修订。

        校验规则（全部在同一写事务内、持有写锁时完成）：
        1. 同号同内容 → 幂等跳过，计为 unchanged；
        2. 同号不同内容 → 抛 RevisionConflictError，整批回滚；
        3. 新修订号 → 插入新行（不覆盖旧修订）。
        """
        if not records:
            raise ValueError("batch must contain at least one record")

        unique_keys = {(r.station_id, r.date.isoformat(), r.hour, r.revision) for r in records}
        stations = sorted({r.station_id for r in records})
        dates = sorted({r.date.isoformat() for r in records})

        conn = self.db.connect()
        try:
            with Database.write_transaction(conn):
                # 先在同一事务内查已有修订，避免校验与写入之间的竞态。
                existing = self._load_existing(conn, unique_keys)

                # 批内重复检查：同一 batch 中同一 key 出现两次且内容不同 → 冲突。
                seen: dict[tuple[str, str, int, int], tuple[float | None, bool]] = {}
                conflicts: list[ConflictDetail] = []
                created = 0
                unchanged = 0
                now = utc_timestamp()

                for record in records:
                    key = (record.station_id, record.date.isoformat(), record.hour, record.revision)
                    submitted = record.content()

                    if key in seen and seen[key] != submitted:
                        conflicts.append(
                            self._conflict(
                                record,
                                existing_payload=seen[key],
                                submitted_payload=submitted,
                            )
                        )
                        continue
                    if key in seen:
                        unchanged += 1
                        continue
                    seen[key] = submitted

                    stored = existing.get(key)
                    if stored is not None:
                        if stored != submitted:
                            conflicts.append(
                                self._conflict(
                                    record,
                                    existing_payload=stored,
                                    submitted_payload=submitted,
                                )
                            )
                        else:
                            unchanged += 1
                        continue

                    conn.execute(
                        """
                        INSERT INTO pm25_readings
                            (station_id, obs_date, hour, revision, value, retracted, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            record.station_id,
                            record.date.isoformat(),
                            record.hour,
                            record.revision,
                            record.value,
                            1 if record.retracted else 0,
                            now,
                        ),
                    )
                    created += 1

                if conflicts:
                    # 让 with 块内抛异常 → ROLLBACK，本批任何插入都不生效。
                    raise RevisionConflictError(conflicts)
        finally:
            conn.close()

        return BatchOutcome(
            received=len(records),
            created=created,
            unchanged=unchanged,
            stations=stations,
            dates=dates,
        )

    @staticmethod
    def _load_existing(
        conn: sqlite3.Connection,
        keys: set[tuple[str, str, int, int]],
    ) -> dict[tuple[str, str, int, int], tuple[float | None, bool]]:
        if not keys:
            return {}
        existing: dict[tuple[str, str, int, int], tuple[float | None, bool]] = {}

        ordered = sorted(keys)
        # 用 row-value IN 一次查询多组 (station_id, obs_date, hour, revision)，
        # 只取批次涉及的键；分块以避开 SQLite 的绑定变量上限（每键 4 个参数）。
        chunk_size = 200
        for start in range(0, len(ordered), chunk_size):
            chunk = ordered[start : start + chunk_size]
            placeholders = ",".join("(?,?,?,?)" for _ in chunk)
            params = [value for key in chunk for value in key]
            rows = conn.execute(
                f"""
                SELECT station_id, obs_date, hour, revision, value, retracted
                FROM pm25_readings
                WHERE (station_id, obs_date, hour, revision) IN (VALUES {placeholders})
                """,
                params,
            ).fetchall()
            for row in rows:
                existing[
                    (row["station_id"], row["obs_date"], row["hour"], row["revision"])
                ] = (row["value"], bool(row["retracted"]))
        return existing

    @staticmethod
    def _conflict(
        record: ReadingRecord,
        *,
        existing_payload: tuple[float | None, bool],
        submitted_payload: tuple[float | None, bool],
    ) -> ConflictDetail:
        return ConflictDetail(
            station_id=record.station_id,
            date=record.date.isoformat(),
            hour=record.hour,
            revision=record.revision,
            existing=_payload_dict(existing_payload),
            submitted=_payload_dict(submitted_payload),
        )

    # -- 日报查询 -------------------------------------------------------
    def daily_report(self, station_id: str, day: dt.date) -> DailyReport:
        """按每小时最大修订号计算日报。"""
        obs_date = day.isoformat()
        conn = self.db.connect()
        try:
            # 单条 SELECT 在 WAL 模式下读到的是一致快照，不会看到写入中途的状态。
            # 子查询取每小时的最大修订号，再与该修订行关联，保证「旧修订不覆盖新值」。
            rows = conn.execute(
                """
                SELECT r.hour, r.value, r.retracted, r.revision
                FROM pm25_readings AS r
                WHERE r.station_id = ?
                  AND r.obs_date = ?
                  AND r.revision = (
                      SELECT MAX(revision)
                      FROM pm25_readings AS m
                      WHERE m.station_id = r.station_id
                        AND m.obs_date = r.obs_date
                        AND m.hour = r.hour
                  )
                ORDER BY r.hour
                """,
                (station_id, obs_date),
            ).fetchall()
        finally:
            conn.close()

        latest = {row["hour"]: row for row in rows}
        hours: list[HourState] = []
        missing_hours: list[int] = []
        retracted_hours: list[int] = []
        valid_values: list[float] = []

        for hour in range(HOURS_PER_DAY):
            row = latest.get(hour)
            if row is None:
                # 从未收到过数据。
                missing_hours.append(hour)
                hours.append(HourState(hour=hour, state="missing"))
            elif row["retracted"]:
                # 最新修订是撤回：该小时同样没有有效值，计入缺失小时，
                # 同时单独列在 retracted_hours 中以便区分「撤回」与「未报」。
                missing_hours.append(hour)
                retracted_hours.append(hour)
                hours.append(HourState(hour=hour, state="retracted", revision=row["revision"]))
            else:
                valid_values.append(row["value"])
                hours.append(
                    HourState(
                        hour=hour,
                        state="valid",
                        value=row["value"],
                        revision=row["revision"],
                    )
                )

        valid_hours = len(valid_values)
        if valid_hours >= REQUIRED_VALID_HOURS:
            daily_mean = round(sum(valid_values) / valid_hours, 2)
            status = "ok"
        else:
            daily_mean = None
            status = "insufficient_valid_hours"

        return DailyReport(
            station_id=station_id,
            date=obs_date,
            required_valid_hours=REQUIRED_VALID_HOURS,
            valid_hours=valid_hours,
            daily_mean=daily_mean,
            status=status,
            missing_hours=missing_hours,
            retracted_hours=retracted_hours,
            hours=hours,
        )


def _payload_dict(payload: tuple[float | None, bool]) -> dict:
    value, retracted = payload
    return {"value": value, "retracted": retracted}
