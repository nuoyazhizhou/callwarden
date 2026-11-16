# -*- coding: utf-8 -*-
"""MCP-047: defect_learn → Rust daemon native 的 HTTP RPC 往返测试。

live-daemon HTTP 往返模式（同 MCP-033~049）：
1. 复用 dev cw-daemon（HTTP manifest 已在 ~/.callwarden）
2. HttpDaemonRpcClient 走 /v1/rpc 调用 defect_learn
   （P0-H：显式 workspace_instance_id）
3. 校验返回结构（learned_patterns / learned_fixes / details）

写面语义（主库写连接迁移后，决策翻转）：
- 无 qualifying 变更（空 hash / 未知 commit）→ 零结果 {learned_patterns:0,
  learned_fixes:0, details:[]}（与 Python learn_defect_from_fix 一致）。
- 存在 qualifying 变更 → 真写入库：INSERT defect_fixes / defect_patterns
  （见正向用例 test_defect_learn_positive_write）。

Rust 权威：compat_native_handlers.rs::handle_defect_learn
（经 snapshot_state.rs 主库写组 open_codegraph_db_write 路由）。
"""

import hashlib
import os
import sqlite3
import sys
import time

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)

from callwarden.server.daemon_client import HttpDaemonRpcClient  # noqa: E402
from callwarden.config import get_http_authority_id  # noqa: E402 —— sys.path/环境注入须先于 import(脚本直跑未走 site-packages,顺序为硬约束)


@pytest.fixture()
def rpc(w3_live):
    """W3 隔离 harness：注入隔离 daemon 的 client / inst / endpoint。"""
    global CANONICAL_INSTANCE, _CANONICAL_ENDPOINT
    CANONICAL_INSTANCE = w3_live["inst"]
    _CANONICAL_ENDPOINT = w3_live["endpoint"]
    return w3_live["client"]


CANONICAL_INSTANCE = None  # 隔离 daemon workspace_instance_id，由 w3_live fixture 注入
_CANONICAL_ENDPOINT = None  # 隔离 daemon endpoint，由 w3_live fixture 注入


def _call(rpc, method, params):
    params = dict(params)
    params.setdefault("workspace_instance_id", CANONICAL_INSTANCE)
    return rpc.call(method, params)


def test_defect_learn_unknown_commit(rpc):
    """未知 commit → 零结果结构完整。"""
    out = _call(rpc, "defect_learn", {"fix_commit_hash": "no_such_commit_xyz"})
    assert isinstance(out, dict)
    assert out.get("learned_patterns") == 0
    assert out.get("learned_fixes") == 0
    assert out.get("details") == []


def test_defect_learn_empty_hash(rpc):
    """空 hash → 零结果（不抛错）。"""
    out = _call(rpc, "defect_learn", {"fix_commit_hash": ""})
    assert isinstance(out, dict)
    assert out.get("learned_patterns") == 0
    assert out.get("learned_fixes") == 0
    assert isinstance(out.get("details"), list)


def test_defect_learn_daemon_unavailable_fail_closed():
    """daemon 不可用 → fail-closed（本地连接异常即证据）。"""
    c = HttpDaemonRpcClient(endpoint="https://127.0.0.1:9")
    with pytest.raises(Exception):
        c.call("defect_learn", {"workspace_instance_id": CANONICAL_INSTANCE,
                                "fix_commit_hash": "x"})


def test_new_client_instance_stable(rpc):
    """restart：新 client 实例重查仍稳定。"""
    c2 = HttpDaemonRpcClient(endpoint=_CANONICAL_ENDPOINT, authority_id=get_http_authority_id())
    out = _call(c2, "defect_learn", {"fix_commit_hash": ""})
    assert isinstance(out, dict)
    assert "learned_fixes" in out


def _cg_db(w3_live):
    """隔离 daemon 的 codegraph 主库路径（与 daemon 写连接同一物理文件，
    见 _w3_harness.setup_w3_client 注释）。"""
    return os.path.join(w3_live["data_root"], "userhome", ".callwarden", "callwarden.db")


def test_defect_learn_positive_write(rpc, w3_live):
    """正向写面用例：qualifying 变更 + finding → 真写 defect_fixes/defect_patterns。

    复刻 Python learn_defect_from_fix 关联链：
    git_symbol_changes(modified, old≠new) → symbol_contents.content_hash→
    qualified_name → semgrep_findings.symbol_qualified（snippet 出现在 old 内容
    才接受 rule）→ INSERT DP-{rule_id}（learned_from='git_fix'）+ defect_fixes
    （effectiveness=1.0）。重跑同 commit → duplicate 去重、不重复写。
    """
    db = _cg_db(w3_live)
    commit = "cafe0011deadbeef0011cafe0011deadbeef0011"
    old = "def run():\n    crash()\n"
    new = "def run():\n    guard()\n"
    before_hash = hashlib.sha256(old.encode("utf-8")).hexdigest()
    after_hash = hashlib.sha256(new.encode("utf-8")).hexdigest()
    symbol_hash = hashlib.sha256(b"w3-defect-learn-symbol").hexdigest()
    qn = "crate::w3_defect_learn_run"
    rule_id = "w3-test-rule-injection"
    pid = f"DP-{rule_id}"

    conn = sqlite3.connect(db, timeout=15)
    try:
        t = time.time()
        conn.execute(
            "INSERT INTO git_commits (commit_hash, message, author, email, timestamp, workspace_id)"
            " VALUES (?, 'fix: guard crash', 'cw-test', 't@example.com', ?, 1)",
            (commit, t),
        )
        conn.execute(
            "INSERT OR IGNORE INTO symbol_contents (content_hash, name, kind, content, qualified_name)"
            " VALUES (?, 'w3_defect_learn_run', 'fn', ?, ?)",
            (symbol_hash, new, qn),
        )
        conn.execute(
            "INSERT INTO git_symbol_changes (commit_hash, symbol_hash, change_type, old_content, new_content)"
            " VALUES (?, ?, 'modified', ?, ?)",
            (commit, symbol_hash, old, new),
        )
        conn.execute(
            "INSERT INTO semgrep_findings (file_instance_id, content_hash, rule_id,"
            " symbol_qualified, snippet, scanned_at) VALUES (1, ?, ?, ?, 'crash()', ?)",
            (before_hash, rule_id, qn, t),
        )
        conn.commit()

        out = _call(rpc, "defect_learn", {"fix_commit_hash": commit})
        assert out["learned_patterns"] == 1
        assert out["learned_fixes"] == 1
        d0 = out["details"][0]
        assert d0["status"] == "learned"
        assert d0["rule_id"] == rule_id
        assert d0["pattern_id"] == pid
        assert d0["before_hash"] == before_hash
        assert d0["after_hash"] == after_hash

        # 落库断言（daemon 写连接与测试连接同一物理文件）
        conn = sqlite3.connect(db, timeout=15)
        row = conn.execute(
            "SELECT pattern_id, effectiveness FROM defect_fixes"
            " WHERE before_hash = ? AND after_hash = ?",
            (before_hash, after_hash),
        ).fetchone()
        assert row is not None and row[0] == pid and row[1] == 1.0
        pat = conn.execute(
            "SELECT learned_from, case_count FROM defect_patterns WHERE pattern_id = ?",
            (pid,),
        ).fetchone()
        assert pat is not None and pat[0] == "git_fix" and pat[1] == 1

        # 幂等去重：同 commit 重跑 → duplicate，不再写库
        out2 = _call(rpc, "defect_learn", {"fix_commit_hash": commit})
        assert out2["learned_patterns"] == 0
        assert out2["learned_fixes"] == 0
        assert out2["details"][0]["status"] == "duplicate"
        assert out2["details"][0]["pattern_id"] == pid
        cnt = conn.execute(
            "SELECT COUNT(*) FROM defect_fixes WHERE before_hash = ? AND after_hash = ?",
            (before_hash, after_hash),
        ).fetchone()[0]
        assert cnt == 1
    finally:
        conn.close()
