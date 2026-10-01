"""SQLite 存储层：连接管理、建表、写事务。

所有时间统一存为 UTC ISO8601 字符串（如 2026-09-30T08:00:00+00:00），
格式一致时可直接按字符串比较大小。
"""
import os
import sqlite3
from contextlib import contextmanager

SCHEMA = """
CREATE TABLE IF NOT EXISTS material_batches (
    batch_id    TEXT PRIMARY KEY,
    created_at  TEXT NOT NULL          -- 首次登记时间（UTC），用于“投入前可用”判定
);

-- 原料过敏原声明：每次登记/更正追加一个版本，当前有效版本 = 最大 version
CREATE TABLE IF NOT EXISTS material_declarations (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id    TEXT NOT NULL REFERENCES material_batches(batch_id),
    version     INTEGER NOT NULL,
    allergens   TEXT NOT NULL,         -- JSON 数组，如 ["milk","peanut"]
    pending     INTEGER NOT NULL,      -- 1 = 待确认
    declared_at TEXT NOT NULL,
    UNIQUE (batch_id, version)
);

CREATE TABLE IF NOT EXISTS processings (
    processing_id TEXT PRIMARY KEY,
    equipment_id  TEXT NOT NULL,
    start_utc     TEXT NOT NULL,
    end_utc       TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processing_inputs (
    processing_id TEXT NOT NULL REFERENCES processings(processing_id),
    batch_id      TEXT NOT NULL,
    PRIMARY KEY (processing_id, batch_id)
);

-- 一个批次最多只能被加工产出一次（batch_id 唯一）
CREATE TABLE IF NOT EXISTS processing_outputs (
    processing_id TEXT NOT NULL REFERENCES processings(processing_id),
    batch_id      TEXT NOT NULL UNIQUE,
    PRIMARY KEY (processing_id, batch_id)
);

CREATE TABLE IF NOT EXISTS cleanings (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    equipment_id TEXT NOT NULL,
    cleaned_at   TEXT NOT NULL,
    verified     INTEGER NOT NULL,     -- 是否已验证
    revoked      INTEGER NOT NULL DEFAULT 0  -- 1 = 已撤销，撤销后不再清除残留
);

-- 已签发标签：snapshot 为签发时刻冻结的结论与依据（JSON），不可更改
CREATE TABLE IF NOT EXISTS labels (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id      TEXT NOT NULL,
    issued_at     TEXT NOT NULL,
    supersedes_id INTEGER REFERENCES labels(id),
    snapshot      TEXT NOT NULL
);

-- 变更事件流：与业务写入同一事务落库，下游按游标（seq）增量消费。
-- seq 全局递增；同一次操作产生的多条事件按批次号排序（插入序即批次号序）。
CREATE TABLE IF NOT EXISTS trace_events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,  -- 全局递增序号
    op          TEXT NOT NULL,       -- 触发操作类型（登记/更正/撤销/预演提交等）
    op_ref      TEXT NOT NULL,       -- JSON：操作定位信息（加工号/清洗 id/声明版本/凭据等）
    batch_id    TEXT NOT NULL,       -- 批次号
    kind        TEXT NOT NULL,       -- material / product
    before_view TEXT,                -- JSON：变更前完整结论与依据；新批次为 NULL
    after_view  TEXT NOT NULL,       -- JSON：变更后完整结论与依据
    created_at  TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_trace_events_batch ON trace_events(batch_id);
"""


def db_path() -> str:
    return os.environ.get("DB_PATH", "app.db")


def connect() -> sqlite3.Connection:
    conn = sqlite3.connect(db_path(), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db() -> None:
    conn = connect()
    try:
        conn.executescript(SCHEMA)
    finally:
        conn.close()


@contextmanager
def write_tx(conn: sqlite3.Connection):
    """写事务：BEGIN IMMEDIATE 保证“校验+写入”原子，失败回滚保持原状态。"""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except Exception:
        conn.rollback()
        raise
    conn.commit()


@contextmanager
def read_tx(conn: sqlite3.Connection):
    """读事务：WAL 下为多个查询提供一致快照（初始化读取/分页读取看到同一状态），
    不阻塞并发写入。"""
    conn.execute("BEGIN")
    try:
        yield conn
    finally:
        conn.rollback()  # 只读事务，结束即释放（等效提交）
