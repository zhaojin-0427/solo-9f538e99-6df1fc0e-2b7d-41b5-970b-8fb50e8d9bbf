"""SQLite 存储层：连接、建表与事务。

设计要点
--------
* 每条 (站点, 日期, 小时, 修订号) 只保留一条不可变记录，历史修订不会被覆盖。
* 读取日报时对每个小时取最大修订号，因此先到的旧修订不可能覆盖后到的新修订；
  即使修订号乱序到达，结果也只由最大修订号决定。
* 写入使用 ``BEGIN IMMEDIATE``：写事务一开始就取得 SQLite 的写锁，
  并发写入被串行化，校验与插入之间不存在竞态窗口。
* 事务失败（冲突、约束错误、磁盘错误等）一律回滚，不会留下部分批量数据。
"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS pm25_readings (
    station_id TEXT NOT NULL,
    obs_date   TEXT NOT NULL,            -- 北京时间观测日期 YYYY-MM-DD
    hour       INTEGER NOT NULL,         -- 0..23
    revision   INTEGER NOT NULL,         -- >= 1，按小时递增
    value      REAL,                     -- 浓度 µg/m³；撤回时为 NULL
    retracted  INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    PRIMARY KEY (station_id, obs_date, hour, revision),
    CHECK (length(station_id) > 0),
    CHECK (hour >= 0 AND hour <= 23),
    CHECK (revision >= 1),
    CHECK (retracted IN (0, 1)),
    CHECK (
        (retracted = 1 AND value IS NULL)
        OR (retracted = 0 AND value IS NOT NULL)
    )
);

-- 事件确认：一条记录绑定一次查询的站点/日期区间/阈值，以及当时所见事件的
-- 内容指纹（event_id）。event_id 由站点、阈值、事件构成小时的时刻和浓度决定，
-- 因此事件被补报、修订或撤回改写后，旧 event_id 不会再与任何新事件匹配。
-- 同一 (区间, 阈值, 事件) 只保留一条确认；重复确认是幂等的。
CREATE TABLE IF NOT EXISTS pm25_event_confirmations (
    confirmation_id TEXT NOT NULL PRIMARY KEY,
    station_id      TEXT NOT NULL,
    start_date      TEXT NOT NULL,       -- 查询区间起（含）
    end_date        TEXT NOT NULL,       -- 查询区间止（含）
    threshold       REAL NOT NULL,       -- 超过该值（严格大于）才计入
    event_id        TEXT NOT NULL,       -- 事件内容指纹
    event_start     TEXT NOT NULL,       -- 事件首个超阈值小时的北京时间
    event_end       TEXT NOT NULL,       -- 事件末个超阈值小时的北京时间（含）
    peak_value      REAL NOT NULL,
    duration_hours  INTEGER NOT NULL,
    source_hours    TEXT NOT NULL,       -- 构成小时的快照（JSON）
    confirmed_at    TEXT NOT NULL,
    CHECK (length(station_id) > 0),
    CHECK (start_date <= end_date),
    CHECK (duration_hours >= 3),
    UNIQUE (station_id, start_date, end_date, threshold, event_id)
);
"""

# 主键 (station_id, obs_date, hour, revision) 已覆盖日报查询的等值前缀，
# 无需额外索引。


class Database:
    """按请求创建短连接；SQLite WAL 允许读并发，写事务串行。"""

    def __init__(self, path: str) -> None:
        self.path = path

    # -- 初始化 ---------------------------------------------------------
    def initialize(self) -> None:
        parent = Path(self.path).expanduser().parent
        if str(parent) not in ("", "."):
            parent.mkdir(parents=True, exist_ok=True)

        conn = self.connect()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            # SCHEMA_SQL 含多条语句，executescript 允许一次建表。
            conn.executescript(SCHEMA_SQL)
        finally:
            conn.close()

    def connect(self) -> sqlite3.Connection:
        # isolation_level=None：关闭 sqlite3 模块的隐式事务，由我们显式 BEGIN。
        conn = sqlite3.connect(self.path, timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA busy_timeout=30000")
        conn.execute("PRAGMA synchronous=NORMAL")
        return conn

    # -- 事务 -----------------------------------------------------------
    @staticmethod
    @contextmanager
    def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
        """写事务：BEGIN IMMEDIATE 立即获取写锁，异常时整体回滚。

        COMMIT 本身失败（如磁盘写满）也走回滚分支，避免事务悬挂。
        """
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            try:
                conn.execute("ROLLBACK")
            except sqlite3.Error:
                # SQLite 可能已自行回滚（如磁盘错误）；保留原始异常。
                pass
            raise


def utc_timestamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
