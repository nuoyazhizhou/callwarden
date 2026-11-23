"""cw journal（SQLite 运行日志）功能与并发测试。

覆盖任务验收：
- 多进程并发 append（8 进程 x 50 次）无丢失、无损坏行；
- list 按 run_id/event/task_id 过滤、--since 下界与 --limit；
- WAL 生效（journal_mode=wal）；
- 非法 --since / --ts 报错路径与 CW_RUN_JOURNAL_DB 环境变量覆盖。
"""

import os
import sqlite3
import sys
from multiprocessing import Process

import pytest

# 确保项目根目录在 path 中
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

# editable finder 的 MAPPING 重定向由根 conftest.py 处理；但 multiprocessing
# spawn 子进程不加载 conftest，只重新导入本模块，故模块级再执行一次 rebind，
# 保证并发用例的 worker 子进程也能正确解析 callwarden 包（见 _editable_rebind.py）。
import _editable_rebind

_editable_rebind.rebind_callwarden_mapping()

from callwarden.cli import journal as journal_mod  # noqa: E402 —— sys.path/环境注入须先于 import(脚本直跑未走 site-packages,顺序为硬约束)


@pytest.fixture()
def db(tmp_path):
    """每个用例独立的数据库路径（用例内直接以 db_path 传参，天然隔离）。"""
    return str(tmp_path / "run-journal.db")


def _worker(db_path: str, worker_id: int, count: int) -> None:
    """并发写进程入口：每个进程独立短连接追加 count 条事件。"""
    from callwarden.cli.journal import append_event

    for i in range(count):
        append_event(
            event="claim_ok",
            run_id=f"run-w{worker_id:02d}",
            task_id=f"T-{worker_id:02d}-{i:03d}",
            detail=f"worker {worker_id} item {i}",
            db_path=db_path,
        )


# ============================================
# 1. 并发追加（验收：8 进程 x 50 次）
# ============================================

def test_concurrent_append_no_loss_no_corruption(db):
    n_procs, n_per = 8, 50
    procs = [Process(target=_worker, args=(db, w, n_per)) for w in range(n_procs)]
    for p in procs:
        p.start()
    for p in procs:
        p.join(120)
    assert all(p.exitcode == 0 for p in procs), \
        [p.exitcode for p in procs]

    rows = journal_mod.list_events(db_path=db)
    assert len(rows) == n_procs * n_per, f"丢失行：{len(rows)} != {n_procs * n_per}"
    # 无损坏行：字段类型与取值域正确
    for r in rows:
        assert isinstance(r["id"], int)
        assert r["event"] == "claim_ok"
        assert r["run_id"].startswith("run-w")
        assert r["task_id"].startswith("T-")
    # 每进程恰好 n_per 条
    by_run = {}
    for r in rows:
        by_run[r["run_id"]] = by_run.get(r["run_id"], 0) + 1
    assert sorted(by_run.values()) == [n_per] * n_procs


# ============================================
# 2. WAL 生效
# ============================================

def test_wal_mode_enabled(db):
    journal_mod.append_event(event="run_start", db_path=db)
    conn = sqlite3.connect(db)
    try:
        mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    finally:
        conn.close()
    assert mode == "wal"


# ============================================
# 3. list 过滤 / 排序 / limit
# ============================================

def test_list_filters_by_run_event_task(db):
    journal_mod.append_event(event="run_start", run_id="run-a", db_path=db)
    journal_mod.append_event(event="claim_ok", run_id="run-a", task_id="T-1", db_path=db)
    journal_mod.append_event(event="run_start", run_id="run-b", task_id="T-2", db_path=db)

    assert len(journal_mod.list_events(db_path=db)) == 3
    assert len(journal_mod.list_events(run_id="run-a", db_path=db)) == 2
    assert len(journal_mod.list_events(event="run_start", db_path=db)) == 2
    assert len(journal_mod.list_events(task_id="T-1", db_path=db)) == 1
    # 组合过滤
    rows = journal_mod.list_events(run_id="run-a", event="claim_ok", db_path=db)
    assert len(rows) == 1 and rows[0]["task_id"] == "T-1"
    # 无匹配返回空列表
    assert journal_mod.list_events(task_id="T-none", db_path=db) == []


def test_list_orders_ts_desc(db):
    journal_mod.append_event(event="run_start", ts="2026-11-23T10:00:00", db_path=db)
    journal_mod.append_event(event="run_end", ts="2026-11-23T11:00:00", db_path=db)
    rows = journal_mod.list_events(db_path=db)
    assert [r["event"] for r in rows] == ["run_end", "run_start"]


def test_list_limit(db):
    for i, ev in enumerate(["e1", "e2", "e3"]):
        journal_mod.append_event(event=ev, ts=f"2026-11-23T1{i}:00:00", db_path=db)
    rows = journal_mod.list_events(limit=2, db_path=db)
    assert [r["event"] for r in rows] == ["e3", "e2"]
    # limit<=0 不限制
    assert len(journal_mod.list_events(limit=0, db_path=db)) == 3


def test_list_since_bounds(db):
    journal_mod.append_event(event="e1", ts="2026-11-23T10:00:00", db_path=db)
    journal_mod.append_event(event="e2", ts="2026-11-23T11:00:00", db_path=db)
    journal_mod.append_event(event="e3", ts="2026-11-23T12:00:00", db_path=db)

    rows = journal_mod.list_events(since="2026-11-23T10:30:00", db_path=db)
    assert [r["event"] for r in rows] == ["e3", "e2"]
    # 日期-only（视为当日 00:00:00）
    assert len(journal_mod.list_events(since="2026-11-23", db_path=db)) == 3
    # Z 后缀（UTC）输入可解析不崩溃（本地时区相关，不硬断言条数）
    assert isinstance(journal_mod.list_events(since="2026-11-23T00:00:00Z", db_path=db), list)


# ============================================
# 4. 报错路径与默认路径覆盖
# ============================================

def test_invalid_timestamps_raise(db):
    with pytest.raises(ValueError):
        journal_mod.append_event(event="e", ts="not-a-time", db_path=db)
    with pytest.raises(ValueError):
        journal_mod.list_events(since="abc", db_path=db)


def test_list_missing_db_returns_empty(tmp_path):
    assert journal_mod.list_events(db_path=str(tmp_path / "none.db")) == []


def test_env_override_default_path(tmp_path, monkeypatch):
    override = str(tmp_path / "override.db")
    monkeypatch.setenv("CW_RUN_JOURNAL_DB", override)
    assert journal_mod.default_db_path() == override
    journal_mod.append_event(event="run_start")  # 走默认路径（被环境变量重定向）
    assert os.path.exists(override)

    monkeypatch.delenv("CW_RUN_JOURNAL_DB")
    assert journal_mod.default_db_path().endswith(os.path.join(".callwarden", "run-journal.db"))


def test_append_normalizes_ts_to_naive_local(db):
    journal_mod.append_event(event="e", ts="2026-11-23T02:00:00Z", db_path=db)
    rows = journal_mod.list_events(db_path=db)
    assert len(rows) == 1
    assert not rows[0]["ts"].endswith("Z")
    assert "+" not in rows[0]["ts"]
