"""cw journal 补充单测：模块 API 边界 + CLI handler（_handle_journal）。

与 tests/test_cli_journal.py 分工（避免重复覆盖）：
- test_cli_journal.py：验收并发（8 进程 x 50 次）、WAL、基础过滤/排序/
  limit/since、报错路径、环境变量覆盖、ts 归一化；
- 本文件：append/list 往返字段、同秒 id 倒序 tie-break、表结构+三索引、
  --since 时区偏移归一化等价性与边界含等、CLI handler 行为
  （--json / 文本输出 / 非法 --ts / --since 退出码 2 / 无子命令打印 help）。

数据库隔离：CW_RUN_JOURNAL_DB 指向 tmp_path，不触碰 ~/.callwarden。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime

import pytest

from callwarden.cli import journal as journal_mod
from callwarden.cli import main as cli_main


@pytest.fixture()
def db(tmp_path, monkeypatch):
    """把运行日志库隔离到 tmp，避免污染 ~/.callwarden/run-journal.db。"""
    path = str(tmp_path / "run-journal.db")
    monkeypatch.setenv("CW_RUN_JOURNAL_DB", path)
    return path


# ----------------------------------------------------------------------
# append / list 往返
# ----------------------------------------------------------------------

def test_append_returns_id_and_list_roundtrip(db):
    row_id = journal_mod.append_event(
        "run_start", run_id="R1", task_id="T1", detail="启动", db_path=db)
    assert isinstance(row_id, int) and row_id >= 1
    rows = journal_mod.list_events(db_path=db)
    assert len(rows) == 1
    r = rows[0]
    assert r["id"] == row_id
    assert r["event"] == "run_start"
    assert r["run_id"] == "R1"
    assert r["task_id"] == "T1"
    assert r["detail"] == "启动"
    assert r["ts"], "ts 为空时应自动填充当前时间"


# ----------------------------------------------------------------------
# 排序 tie-break：ts 倒序，同秒内 id 倒序
# ----------------------------------------------------------------------

def test_order_ts_desc_then_id_desc(db):
    i1 = journal_mod.append_event("e1", ts="2026-11-20T10:00:00", db_path=db)
    i2 = journal_mod.append_event("e2", ts="2026-11-20T10:00:00", db_path=db)
    i3 = journal_mod.append_event("e3", ts="2026-11-21T09:00:00", db_path=db)
    rows = journal_mod.list_events(db_path=db)
    assert [r["id"] for r in rows] == [i3, i2, i1]


# ----------------------------------------------------------------------
# --since 时区偏移归一化等价性与边界含等
# ----------------------------------------------------------------------

def test_since_timezone_offset_equivalent_and_boundary_inclusive(db):
    journal_mod.append_event("old", ts="2026-11-20T10:00:00", db_path=db)
    boundary_local = datetime.fromisoformat(
        "2026-11-21T12:00:00+00:00").astimezone().replace(
        tzinfo=None).isoformat(timespec="seconds")
    journal_mod.append_event("boundary", ts=boundary_local, db_path=db)
    journal_mod.append_event("new", ts="2026-11-22T10:00:00", db_path=db)

    # Z 后缀与 +08:00 偏移归一化到本地时间后等价；恰好等于下界的记录包含（>=）
    rows_z = journal_mod.list_events(since="2026-11-21T12:00:00Z", db_path=db)
    rows_off = journal_mod.list_events(
        since="2026-11-21T20:00:00+08:00", db_path=db)
    assert [r["event"] for r in rows_z] == ["new", "boundary"]
    assert [r["event"] for r in rows_off] == ["new", "boundary"]

    # 下界之后无记录
    assert journal_mod.list_events(since="2026-11-25", db_path=db) == []


# ----------------------------------------------------------------------
# WAL / 表结构 / 三索引
# ----------------------------------------------------------------------

def test_wal_schema_and_indexes(db):
    journal_mod.append_event("init", db_path=db)
    conn = sqlite3.connect(db)
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        tables = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        assert "run_journal" in tables
        indexes = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'"
            " AND name LIKE 'idx_run_journal%'")}
        assert indexes == {
            "idx_run_journal_run_ts", "idx_run_journal_task",
            "idx_run_journal_event"}
    finally:
        conn.close()


# ----------------------------------------------------------------------
# CLI handler（直接调 _handle_journal，db 参数固定 None，走环境变量隔离）
# ----------------------------------------------------------------------

def test_cli_append_json(db, capsys):
    assert cli_main._handle_journal(
        ["append", "push_ok", "--run-id", "R", "--task-id", "T",
         "--detail", "推送成功", "--json"], None) is True
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["event"] == "push_ok"
    assert payload["id"] >= 1
    rows = journal_mod.list_events(db_path=db)
    assert len(rows) == 1 and rows[0]["detail"] == "推送成功"


def test_cli_append_text(db, capsys):
    assert cli_main._handle_journal(
        ["append", "run_end", "--run-id", "R", "--detail", "结束"], None) is True
    out = capsys.readouterr().out
    assert "run_journal.id=" in out
    assert "run_end" in out


def test_cli_list_json_and_text(db, capsys):
    journal_mod.append_event("push_ok", run_id="R1", detail="d1",
                             ts="2026-11-23T10:00:00", db_path=db)
    journal_mod.append_event("run_start", run_id="R2", detail="d2",
                             ts="2026-11-23T11:00:00", db_path=db)

    assert cli_main._handle_journal(["list", "--json"], None) is True
    rows = json.loads(capsys.readouterr().out)
    assert [r["event"] for r in rows] == ["run_start", "push_ok"]

    assert cli_main._handle_journal(["list"], None) is True
    out = capsys.readouterr().out
    assert "[run_start] run=R2" in out
    assert "[push_ok] run=R1" in out


def test_cli_list_empty_json(db, capsys):
    assert cli_main._handle_journal(["list", "--json"], None) is True
    assert json.loads(capsys.readouterr().out) == []


def test_cli_append_invalid_ts_exit2(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli_main._handle_journal(["append", "x", "--ts", "garbage"], None)
    assert excinfo.value.code == 2
    assert "E_JOURNAL_TS_INVALID" in capsys.readouterr().err


def test_cli_list_invalid_since_exit2(capsys):
    with pytest.raises(SystemExit) as excinfo:
        cli_main._handle_journal(["list", "--since", "garbage"], None)
    assert excinfo.value.code == 2
    assert "E_JOURNAL_SINCE_INVALID" in capsys.readouterr().err


def test_cli_no_action_prints_help(capsys):
    assert cli_main._handle_journal([], None) is True
    assert "usage:" in capsys.readouterr().out
