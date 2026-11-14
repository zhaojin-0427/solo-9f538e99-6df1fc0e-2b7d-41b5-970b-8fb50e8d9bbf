"""应用配置与业务常量。"""

from __future__ import annotations

import os
from dataclasses import dataclass

DEFAULT_DATABASE_PATH = "data/pm25.db"

# 日报要求的最少有效小时数。不足时只返回缺失小时，不给出日均值。
REQUIRED_VALID_HOURS = 20
HOURS_PER_DAY = 24

# 单批最多接收的记录数，避免一次事务过大。
MAX_BATCH_RECORDS = 1000

# 输入中的 date/hour 表示北京时间（Asia/Shanghai）下的观测日期与小时。
BEIJING_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class Settings:
    database_path: str

    @classmethod
    def from_env(cls, database_path: str | None = None) -> "Settings":
        path = database_path or os.environ.get("DATABASE_PATH") or DEFAULT_DATABASE_PATH
        path = path.strip()
        if not path:
            path = DEFAULT_DATABASE_PATH
        return cls(database_path=path)
