"""请求 / 响应模型。"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .config import HOURS_PER_DAY, MAX_BATCH_RECORDS, REQUIRED_VALID_HOURS


class ReadingRecord(BaseModel):
    """一条 PM2.5 小时值修订记录。

    ``date`` 与 ``hour`` 均为北京时间（Asia/Shanghai）下的观测日期与整点小时。
    撤回记录必须不含浓度；非撤回记录必须包含浓度。
    """

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    station_id: str = Field(min_length=1, max_length=128)
    date: dt.date
    hour: int = Field(ge=0, le=HOURS_PER_DAY - 1)
    revision: int = Field(ge=1)
    value: float | None = Field(default=None, ge=0)
    retracted: bool = False

    @field_validator("hour", "revision", "value", "retracted", mode="before")
    @classmethod
    def _reject_cross_type_coercion(cls, v: object, info) -> object:
        """拒绝让 bool/字符串被悄悄当成数字（如 ``"hour": true`` → 1）。

        JSON 里的数字类型必须写对；布尔字段只接受 JSON 布尔值。
        """
        if info.field_name == "retracted":
            if not isinstance(v, bool):
                raise ValueError("retracted must be a JSON boolean")
            return v
        if isinstance(v, bool):
            raise ValueError(f"{info.field_name} must be a number, not a boolean")
        if isinstance(v, str):
            raise ValueError(f"{info.field_name} must be a number, not a string")
        return v

    @field_validator("station_id")
    @classmethod
    def _normalize_station_id(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("station_id must not be blank")
        return v

    @field_validator("date", mode="before")
    @classmethod
    def _reject_datetime_for_date(cls, v: object) -> object:
        """只接受 YYYY-MM-DD；拒绝带时间部分的字符串，避免隐式截断。"""
        if isinstance(v, str) and ("T" in v or ":" in v):
            raise ValueError("date must be YYYY-MM-DD (Beijing date, no time component)")
        return v

    @model_validator(mode="after")
    def _check_value_matches_retraction(self) -> "ReadingRecord":
        # 撤回时 value 必须为空，非撤回时必须有值，二者不能矛盾。
        if self.retracted and self.value is not None:
            raise ValueError("retracted record must not include a value")
        if not self.retracted and self.value is None:
            raise ValueError("non-retracted record must include a value")
        return self

    def content(self) -> tuple[float | None, bool]:
        """用于判断同一修订号内容是否一致的载荷。"""
        return (self.value, self.retracted)


class BatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    records: list[ReadingRecord] = Field(min_length=1, max_length=MAX_BATCH_RECORDS)


class BatchResult(BaseModel):
    received: int
    created: int
    unchanged: int
    stations: list[str]
    dates: list[str]


class ConflictDetail(BaseModel):
    station_id: str
    date: str
    hour: int
    revision: int
    existing: dict
    submitted: dict


class ConflictResponse(BaseModel):
    code: Literal["revision_conflict"] = "revision_conflict"
    message: str
    conflicts: list[ConflictDetail]
    rolled_back: Literal[True] = True


class HourState(BaseModel):
    hour: int
    state: Literal["valid", "missing", "retracted"]
    value: float | None = None
    revision: int | None = None


class DailyReport(BaseModel):
    """站点某北京日期的日报。

    ``missing_hours`` 是「没有有效值」的全部小时（未报 + 撤回），
    ``retracted_hours`` 是其中因最新修订为撤回而无效的小时，二者为子集关系。
    因此 ``valid_hours + len(missing_hours) == 24``。
    """

    station_id: str
    date: str
    timezone: Literal["Asia/Shanghai"] = "Asia/Shanghai"
    required_valid_hours: int = REQUIRED_VALID_HOURS
    valid_hours: int
    daily_mean: float | None
    status: Literal["ok", "insufficient_valid_hours"]
    missing_hours: list[int]
    retracted_hours: list[int]
    hours: list[HourState]
