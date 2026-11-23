"""cw journal：SQLite 可查询运行日志（cron worker 运行事件记录）。

替代 runs/journal.jsonl 纯文本追加方案：
- 结构化字段（ts/run_id/event/task_id/detail），支持按 run/event/task 过滤、
  --since（ISO8601）与 --limit；
- WAL + busy_timeout=5000ms 支持多进程并发追加（见 tests/test_cli_journal.py
  并发用例：8 进程 x 50 次追加无丢失、无损坏行）；
- 数据库默认 ``~/.callwarden/run-journal.db``，可用环境变量
  ``CW_RUN_JOURNAL_DB`` 覆盖（测试隔离与多工作区场景）。

边界：权威任务状态以 daemon assignment 为准，本表只是尽力而为的可见性
记录，不参与任何任务状态机判定；不删除、不改写现有 journal.jsonl。
"""

from __future__ import annotations

import os
import sqlite3
import time
from datetime import datetime
from typing import Dict, List

# WAL 与 busy_timeout：多进程并发追加的关键（每次调用独立短连接）
_BUSY_TIMEOUT_MS = 5000
# 建库初始化（journal_mode 切换 + 建表建索引）的并发重试上限：
# delete→WAL 首次切换需要独占锁，多进程同时首连时可能 SQLITE_BUSY/LOCKED
# （busy handler 对该 PRAGMA 不完全生效），用退避重试兜底。
_INIT_MAX_ATTEMPTS = 5

_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS run_journal ("
    " id INTEGER PRIMARY KEY,"
    " ts TEXT NOT NULL,"
    " run_id TEXT,"
    " event TEXT NOT NULL,"
    " task_id TEXT,"
    " detail TEXT,"
    " created_at TEXT NOT NULL)"
)

_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_run_journal_run_ts ON run_journal(run_id, ts)",
    "CREATE INDEX IF NOT EXISTS idx_run_journal_task ON run_journal(task_id)",
    "CREATE INDEX IF NOT EXISTS idx_run_journal_event ON run_journal(event)",
)


def default_db_path() -> str:
    """运行日志数据库路径：CW_RUN_JOURNAL_DB 覆盖，否则 ~/.callwarden/run-journal.db。"""
    override = os.environ.get("CW_RUN_JOURNAL_DB", "").strip()
    if override:
        return override
    home = os.path.expanduser("~")
    return os.path.join(home, ".callwarden", "run-journal.db")


def _now_iso() -> str:
    """本地时间 ISO8601（与 journal.jsonl 追加格式保持一致，便于人工对照）。"""
    return datetime.now().isoformat(timespec="seconds")


def _connect_writable(db_path: str) -> sqlite3.Connection:
    """可写短连接：WAL + busy_timeout + 建表建索引（IF NOT EXISTS 幂等）。

    初始化段（journal_mode 切换/建表/建索引）带退避重试：多进程并发首连时
    WAL 模式切换需要独占锁，可能 database is locked；已是 WAL 则跳过切换。
    """
    parent = os.path.dirname(db_path)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_MS / 1000.0)
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    conn.execute("PRAGMA synchronous=NORMAL")
    for attempt in range(_INIT_MAX_ATTEMPTS):
        try:
            mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
            if str(mode).lower() != "wal":
                conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(_SCHEMA)
            for stmt in _INDEXES:
                conn.execute(stmt)
            break
        except sqlite3.OperationalError as exc:
            msg = str(exc).lower()
            if ("locked" in msg or "busy" in msg) and attempt < _INIT_MAX_ATTEMPTS - 1:
                time.sleep(0.05 * (attempt + 1))
                continue
            raise
    return conn


def _normalize_since(since: str) -> str:
    """把 --since 输入归一化为与存储格式一致的本地 ISO8601 字符串。

    支持 ``2026-11-23``、``2026-11-23T12:00:00``、带时区偏移或结尾 ``Z``
    的 ISO8601（统一转本地时间后比较）；非法输入抛 ValueError。
    """
    text = (since or "").strip()
    if not text:
        return ""
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        dt = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"--since 不是合法 ISO8601 时间: {since!r}") from exc
    if dt.tzinfo is not None:
        dt = dt.astimezone().replace(tzinfo=None)
    return dt.isoformat(timespec="seconds")


def append_event(event: str, run_id: str = "", task_id: str = "",
                 detail: str = "", ts: str = "",
                 db_path: str = "") -> int:
    """追加一条运行事件，返回新行 id。一次 INSERT，多进程安全。

    ``ts`` 支持任意 ISO8601（含时区/Z 后缀，统一归一化为本地时间存储），
    空串取当前本地时间；非法格式抛 ValueError。
    """
    path = db_path or default_db_path()
    ts_norm = _normalize_since(ts) or _now_iso()
    conn = _connect_writable(path)
    try:
        with conn:  # 事务提交 / 回滚
            cur = conn.execute(
                "INSERT INTO run_journal"
                " (ts, run_id, event, task_id, detail, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (ts_norm, run_id, event, task_id, detail, _now_iso()),
            )
        return int(cur.lastrowid)
    finally:
        conn.close()


def list_events(run_id: str = "", event: str = "", task_id: str = "",
                since: str = "", limit: int = 0,
                db_path: str = "") -> List[Dict]:
    """查询运行事件：按 run_id/event/task_id 过滤，--since 下界，ts 倒序。

    只读路径：不建表、不触发写锁；数据库不存在或表未建好时返回空列表。
    ``limit`` <= 0 表示不限制。
    """
    path = db_path or default_db_path()
    # 校验前置：非法 --since 无论库是否存在都报错（输入校验属边界职责，
    # 不应被"库不存在返回空"的快路径吞掉）。
    since_norm = _normalize_since(since)
    if not os.path.exists(path):
        return []
    clauses: List[str] = []
    params: List[object] = []
    if run_id:
        clauses.append("run_id = ?")
        params.append(run_id)
    if event:
        clauses.append("event = ?")
        params.append(event)
    if task_id:
        clauses.append("task_id = ?")
        params.append(task_id)
    if since_norm:
        clauses.append("ts >= ?")
        params.append(since_norm)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    sql = ("SELECT id, ts, run_id, event, task_id, detail FROM run_journal"
           + where + " ORDER BY ts DESC, id DESC")
    if limit and limit > 0:
        sql += " LIMIT ?"
        params.append(limit)
    conn = sqlite3.connect(path, timeout=_BUSY_TIMEOUT_MS / 1000.0)
    conn.execute(f"PRAGMA busy_timeout={_BUSY_TIMEOUT_MS}")
    try:
        try:
            rows = conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                return []
            raise
    finally:
        conn.close()
    return [
        {"id": r[0], "ts": r[1], "run_id": r[2], "event": r[3],
         "task_id": r[4], "detail": r[5]}
        for r in rows
    ]
