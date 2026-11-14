"""事件查询 / 确认的请求与响应模型。"""

from __future__ import annotations

import datetime as dt
import math
import re
from typing import Literal

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from .config import BEIJING_TIMEZONE, MAX_EVENT_QUERY_DAYS, MIN_EVENT_HOURS

_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")

# 参数名以 README 中的 start_date / end_date / threshold 为准，
# 同时接受几种常见写法，方便调用方。
_START_ALIASES = AliasChoices(
    "start_date", "start", "from", "from_date", "begin", "date_from"
)
_END_ALIASES = AliasChoices("end_date", "end", "to", "to_date", "finish", "date_to")
_THRESHOLD_ALIASES = AliasChoices(
    "threshold", "limit", "pm25_threshold", "value_threshold"
)


def parse_beijing_date(value: object) -> dt.date:
    """只接受 ``YYYY-MM-DD``，拒绝带时间部分的字符串。

    日期始终表示北京时间（Asia/Shanghai）的观测日期，服务不做时区换算。
    """
    if isinstance(value, dt.datetime):
        raise ValueError("date must be YYYY-MM-DD (Beijing date, no time component)")
    if isinstance(value, dt.date):
        return value
    if isinstance(value, str):
        if not _DATE_RE.fullmatch(value):
            raise ValueError("date must be YYYY-MM-DD (Beijing date, no time component)")
        try:
            return dt.date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("invalid calendar date") from exc
    raise ValueError("date must be a YYYY-MM-DD string")


def _normalize_station_id(value: str | None) -> str | None:
    if value is None:
        return None
    value = value.strip()
    if not value:
        raise ValueError("station_id must not be blank")
    return value


class _DateRangeMixin(BaseModel):
    """北京时间日期区间（含两端，最多 3 天）+ 阈值。"""

    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    start_date: dt.date = Field(validation_alias=_START_ALIASES)
    end_date: dt.date = Field(validation_alias=_END_ALIASES)
    threshold: float = Field(validation_alias=_THRESHOLD_ALIASES, allow_inf_nan=False)

    @field_validator("start_date", "end_date", mode="before")
    @classmethod
    def _validate_date(cls, v: object) -> dt.date:
        return parse_beijing_date(v)

    @field_validator("threshold", mode="before")
    @classmethod
    def _reject_bad_threshold(cls, v: object) -> object:
        if isinstance(v, bool):
            raise ValueError("threshold must be a number, not a boolean")
        if isinstance(v, str):
            try:
                parsed = float(v)
            except ValueError as exc:
                raise ValueError("threshold must be a number") from exc
            if not math.isfinite(parsed):
                raise ValueError("threshold must be a finite number")
            return parsed
        if isinstance(v, float) and not math.isfinite(v):
            raise ValueError("threshold must be a finite number")
        return v

    @model_validator(mode="after")
    def _check_range(self) -> "_DateRangeMixin":
        if self.end_date < self.start_date:
            raise ValueError("end_date must not be earlier than start_date")
        span = (self.end_date - self.start_date).days + 1
        if span > MAX_EVENT_QUERY_DAYS:
            raise ValueError(
                f"date range must cover at most {MAX_EVENT_QUERY_DAYS} days (inclusive)"
            )
        return self


class EventQuery(_DateRangeMixin):
    """事件查询条件（GET 查询参数，站点通常在路径中）。"""


class EventQueryRequest(_DateRangeMixin):
    """事件查询条件（POST 请求体；可携带 ``station_id``）。"""

    station_id: str | None = None

    _validate_station = field_validator("station_id")(_normalize_station_id)


class ConfirmationRequest(_DateRangeMixin):
    """确认请求：绑定本次查询区间、阈值与所见事件。

    事件标识按下列优先级取用：

    * ``event_id``：查询响应中某个事件的指纹（推荐）；
    * ``event_ids``：一次确认多个事件；
    * ``event_start`` + ``event_end``（别名 ``start_time`` / ``end_time``）：
      用事件起止时刻定位事件；
    * ``event``：把查询响应中的整个事件对象原样回传；
    * 都不给：确认本次区间与阈值下看到的全部事件。

    若回传了构成小时（``hours`` 或 ``event.hours``），还会按与 ``event_id``
    相同的口径（每个小时的时刻与浓度）复核；内容或边界不符即视为过期确认。
    """

    event_id: str | None = Field(default=None, max_length=256)
    event_ids: list[str] | None = None
    event_start: str | None = Field(
        default=None, validation_alias=AliasChoices("event_start", "start_time")
    )
    event_end: str | None = Field(
        default=None, validation_alias=AliasChoices("event_end", "end_time")
    )
    event: dict | None = None
    hours: list[dict] | None = None
    station_id: str | None = None

    _validate_station = field_validator("station_id")(_normalize_station_id)

    @field_validator("event_id")
    @classmethod
    def _strip_event_id(cls, v: str | None) -> str | None:
        if v is None:
            return None
        v = v.strip()
        if not v:
            raise ValueError("event_id must not be blank")
        return v

    @field_validator("event_ids")
    @classmethod
    def _strip_event_ids(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        cleaned = [item.strip() for item in v]
        if not cleaned or any(not item for item in cleaned):
            raise ValueError("event_ids must be a non-empty list of non-blank ids")
        return cleaned

    @model_validator(mode="after")
    def _check_times(self) -> "ConfirmationRequest":
        if (self.event_start is None) != (self.event_end is None):
            raise ValueError("event_start and event_end must be provided together")
        return self

    # -- 归一化后的身份信息 ---------------------------------------------
    def requested_event_ids(self) -> list[str] | None:
        ids: list[str] = []
        if self.event_id is not None:
            ids.append(self.event_id)
        if isinstance(self.event, dict) and isinstance(self.event.get("event_id"), str):
            ids.append(self.event["event_id"])
        if self.event_ids is not None:
            ids.extend(self.event_ids)
        return ids or None

    def time_identity(self) -> tuple[str | None, str | None]:
        start, end = self.event_start, self.event_end
        if (start is None or end is None) and isinstance(self.event, dict):
            start = start or self.event.get("start_time") or self.event.get("start")
            end = end or self.event.get("end_time") or self.event.get("end")
        return start, end

    def expected_hours(self) -> list[dict] | None:
        if self.hours is not None:
            return self.hours
        if isinstance(self.event, dict) and isinstance(self.event.get("hours"), list):
            return self.event["hours"]
        return None


class EventHourModel(BaseModel):
    time: str
    date: str
    hour: int = Field(ge=0, le=23)
    value: float
    revision: int = Field(ge=1)


class EventModel(BaseModel):
    event_id: str
    start_time: str
    end_time: str
    start_date: str
    end_date: str
    duration_hours: int = Field(ge=MIN_EVENT_HOURS)
    peak_value: float
    peak_time: str
    peak_revision: int = Field(ge=1)
    min_value: float
    revisions: list[int]
    hours: list[EventHourModel]
    confirmed: bool = False
    confirmation_id: str | None = None


class EventQueryResponse(BaseModel):
    station_id: str
    start_date: str
    end_date: str
    threshold: float
    timezone: Literal["Asia/Shanghai"] = BEIJING_TIMEZONE
    min_duration_hours: int = MIN_EVENT_HOURS
    event_count: int = 0
    events: list[EventModel] = Field(default_factory=list)


class ConfirmationModel(BaseModel):
    confirmation_id: str
    station_id: str
    start_date: str
    end_date: str
    threshold: float
    event_id: str
    event_start: str
    event_end: str
    duration_hours: int
    peak_value: float
    confirmed_at: str
    event: EventModel | None = None
    idempotent: bool = False


class ConfirmationListResponse(BaseModel):
    station_id: str
    confirmations: list[dict]
