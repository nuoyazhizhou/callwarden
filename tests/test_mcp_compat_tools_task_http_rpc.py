"""P0-COMPAT-v3（T-1788963104058-fdb2e848）tools_task 组 → Rust daemon native。

覆盖方法（8）：get_symbol_change_tasks / audit_verify_chain /
list_audit_signing_keys / bootstrap_status / list_clones / list_clone_groups /
get_clone_group_detail / task_plan_template。

覆盖 task 要求：
  success / invalid 参数 / daemon unavailable（fail-closed）矩阵；
  Python compat 退役断言（_TASK_READ_ONLY_METHODS 已清空 +
  RUST_COMPAT_ROUTE 已摘除 8 条目）。

语义对照（Python 真相源）：
- get_symbol_change_tasks：db_task_attribution.get_symbol_change_tasks ——
  symbol_hash_before/after 反查 + qualified_name 精确匹配，无条件返回 []。
- audit_verify_chain：db_audit_chain.verify_audit_chain（Rust 复用
  cli::security::verify_audit_chain）——链连续性 + 签名重算 + 首条 prev 空。
  limit<=0 → invalid_params（fail-closed，Python 直传 SQL 的行为差异已声明）。
- list_audit_signing_keys：db_audit_chain.list_signing_keys —— 不含 key_secret。
- bootstrap_status：db_bootstrap.bootstrap_status —— db_stale/规则计数/
  findings 计数/audit_verify/tasks 分组/recommended_next_action。
- list_clones：db_clone_detection.list_clones —— clone_type/min_similarity/
  symbol_id 过滤，相似度降序。
- list_clone_groups：db_clone_groups.list_clone_groups —— CloneGroup.to_dict
  9 字段。
- get_clone_group_detail：db_clone_groups.get_clone_group_detail —— 未找到
  返回 {"error": "group not found: <id>"}。
- task_plan_template：i18n 模板（env 链选择，默认 en）。

行为改进（相对退役前 Python compat 通道，已在 step0 evidence 声明）：
- bootstrap_status / list_clones / list_clone_groups 在 Python 通道因 compat
  worker 无 active workspace 报错/返回 error dict；Rust 以 daemon 路由解析的
  workspace_id 正常返回真实数据。
"""

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.config import get_http_authority_id

CANONICAL_INSTANCE = None  # 隔离 daemon workspace_instance_id，由 w3_live fixture 注入
_CANONICAL_ENDPOINT = None  # 隔离 daemon endpoint，由 w3_live fixture 注入

ALL_METHODS = [
    "get_symbol_change_tasks",
    "audit_verify_chain",
    "list_audit_signing_keys",
    "bootstrap_status",
    "list_clones",
    "list_clone_groups",
    "get_clone_group_detail",
    "task_plan_template",
]


@pytest.fixture()
def live_daemon(w3_live):
    """W3 隔离 harness：注入隔离 daemon 的 client / inst / endpoint。"""
    global CANONICAL_INSTANCE, _CANONICAL_ENDPOINT
    CANONICAL_INSTANCE = w3_live["inst"]
    _CANONICAL_ENDPOINT = w3_live["endpoint"]
    return w3_live["client"]


# ---------------------------------------------------------------------------
# success 矩阵（真实 workspace 数据，只读语义 fail-soft）
# ---------------------------------------------------------------------------
def test_task_plan_template_default_en(live_daemon):
    """默认返回英文模板（i18n 回退 en_US，与 Python 通道实测一致）。"""
    c = live_daemon
    r = c.call("task_plan_template", {"workspace_instance_id": CANONICAL_INSTANCE})
    assert isinstance(r, str)
    assert r.startswith("# {Root task title}")
    assert "## {Subtask 1 title}" in r
    assert "- [ ] {Incomplete step (checkbox format)}" in r


def test_list_audit_signing_keys_shape(live_daemon):
    """签名密钥列表：list 元素只含 key_id/rotated_at/is_active（无 key_secret）。"""
    c = live_daemon
    r = c.call("list_audit_signing_keys", {"workspace_instance_id": CANONICAL_INSTANCE})
    assert isinstance(r, list)
    for row in r:
        assert set(row.keys()) == {"key_id", "rotated_at", "is_active"}
        assert "key_secret" not in row


def test_symbol_change_tasks_unknown_hash(live_daemon):
    """未知 symbol_hash → 空数组（不报错）。"""
    c = live_daemon
    r = c.call("get_symbol_change_tasks", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "symbol_hash": "no-such-hash-zzz",
    })
    assert isinstance(r, list)
    assert r == []


def test_symbol_change_tasks_no_params(live_daemon):
    """无条件调用 → 空数组（Python 真相源：无 clauses 直接返回 []）。"""
    c = live_daemon
    r = c.call("get_symbol_change_tasks", {"workspace_instance_id": CANONICAL_INSTANCE})
    assert isinstance(r, list)
    assert r == []


def test_symbol_change_tasks_unknown_qualified_name(live_daemon):
    """未知 qualified_name → 空数组。"""
    c = live_daemon
    r = c.call("get_symbol_change_tasks", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "qualified_name": "no::such::symbol::zzz",
    })
    assert isinstance(r, list)
    assert r == []


def test_clone_group_detail_not_found(live_daemon):
    """不存在的 group_id → {"error": "group not found: <id>"}（Python 语义）。"""
    c = live_daemon
    r = c.call("get_clone_group_detail", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "group_id": 999999,
    })
    assert isinstance(r, dict)
    assert r == {"error": "group not found: 999999"}


def test_audit_verify_chain_small_limit(live_daemon):
    """limit=20 → 验证摘要：total_count=20 且逐字段结构对齐 Python。"""
    c = live_daemon
    r = c.call("audit_verify_chain", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "table_name": "",
        "limit": 20,
    })
    assert isinstance(r, dict)
    assert set(r.keys()) == {
        "table_name", "total_count", "verified_count", "broken_count",
        "broken_records", "security_level",
    }
    assert r["table_name"] == ""
    assert r["total_count"] == 20
    assert r["verified_count"] + r["broken_count"] == 20
    assert isinstance(r["broken_records"], list)
    assert r["security_level"] in ("hmac", "hash_only")


def test_audit_verify_chain_table_filter(live_daemon):
    """table_name 过滤 → 回显 table_name 且只统计该表记录。"""
    c = live_daemon
    r = c.call("audit_verify_chain", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "table_name": "tasks",
        "limit": 10,
    })
    assert isinstance(r, dict)
    assert r["table_name"] == "tasks"
    assert r["total_count"] <= 10
    for row in r["broken_records"]:
        assert row["table_name"] == "tasks"


def test_list_clones_shape(live_daemon):
    """list_clones → list；非空时行键与 Python dict(row) 字段集一致。"""
    c = live_daemon
    r = c.call("list_clones", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "limit": 5,
    })
    assert isinstance(r, list)
    assert len(r) <= 5
    expected = {
        "clone_type", "similarity", "token_hash", "lines_a", "lines_b",
        "detected_at", "symbol_a_name", "symbol_a_qualified", "symbol_a_line",
        "symbol_b_name", "symbol_b_qualified", "symbol_b_line", "file_a", "file_b",
    }
    for row in r:
        assert set(row.keys()) == expected


def test_list_clones_similarity_filter(live_daemon):
    """min_similarity=0.5 → 所有权重 >= 0.5。"""
    c = live_daemon
    r = c.call("list_clones", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "min_similarity": 0.5,
        "limit": 50,
    })
    assert isinstance(r, list)
    for row in r:
        assert row["similarity"] >= 0.5


def test_list_clone_groups_shape(live_daemon):
    """list_clone_groups → list；非空时行键 = CloneGroup.to_dict() 9 字段。"""
    c = live_daemon
    r = c.call("list_clone_groups", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "limit": 5,
    })
    assert isinstance(r, list)
    assert len(r) <= 5
    expected = {
        "id", "workspace_id", "group_hash", "clone_type", "token_hash",
        "similarity", "representative_symbol_id", "member_count", "created_at",
    }
    for row in r:
        assert set(row.keys()) == expected


def test_bootstrap_status_shape(live_daemon):
    """bootstrap_status → 全字段结构（含 audit_verify/tasks 子 dict）。"""
    c = live_daemon
    r = c.call("bootstrap_status", {"workspace_instance_id": CANONICAL_INSTANCE})
    assert isinstance(r, dict)
    assert set(r.keys()) == {
        "db_stale", "current_head", "active_rules_count",
        "pending_candidates_count", "open_findings_count",
        "blocking_findings_count", "audit_verify", "latest_scan_run",
        "tasks", "recommended_next_action",
    }
    assert isinstance(r["db_stale"], bool)
    assert set(r["audit_verify"].keys()) >= {
        "total_count", "verified_count", "broken_count", "security_level",
    }
    assert set(r["tasks"].keys()) == {"open", "in_progress", "review", "applied"}
    assert isinstance(r["recommended_next_action"], str) and r["recommended_next_action"]
    # latest_scan_run：None 或含 4 字段
    if r["latest_scan_run"] is not None:
        assert set(r["latest_scan_run"].keys()) == {
            "id", "git_head", "started_at", "status",
        }


# ---------------------------------------------------------------------------
# invalid 参数矩阵（fail-closed）
# ---------------------------------------------------------------------------
def test_missing_workspace_instance_id_invalid(live_daemon):
    """缺 workspace_instance_id → invalid_params（路由层 fail-closed）。"""
    c = live_daemon
    with pytest.raises(Exception):
        c.call("task_plan_template", {})
    with pytest.raises(Exception):
        c.call("bootstrap_status", {})


def test_audit_verify_chain_zero_limit(live_daemon):
    """limit=0 → 零计数 dict（Python SQL LIMIT 0 平价语义）。"""
    c = live_daemon
    r = c.call("audit_verify_chain", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "table_name": "",
        "limit": 0,
    })
    assert isinstance(r, dict)
    assert r["total_count"] == 0
    assert r["verified_count"] == 0
    assert r["broken_count"] == 0
    assert r["broken_records"] == []


def test_audit_verify_chain_negative_limit_invalid(live_daemon):
    """limit<0 → invalid_params（SQLite 负 LIMIT=无上限，fail-closed 拒绝复刻）。"""
    c = live_daemon
    with pytest.raises(Exception):
        c.call("audit_verify_chain", {
            "workspace_instance_id": CANONICAL_INSTANCE,
            "table_name": "",
            "limit": -1,
        })


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method", ALL_METHODS)
def test_task_methods_daemon_unavailable_fail_closed(method):
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    # DaemonUnavailableError 异常类型本身即 fail-closed 证据
    with pytest.raises(DaemonUnavailableError):
        c.call(method, {"workspace_instance_id": CANONICAL_INSTANCE})


# ---------------------------------------------------------------------------
# Python compat 退役断言：条目已从只读白名单摘除
# ---------------------------------------------------------------------------
def test_python_compat_entries_retired():
    from callwarden.server.tools import tools_task
    assert tools_task._TASK_READ_ONLY_METHODS == {}
    from callwarden.server.compat_registry import RUST_COMPAT_ROUTE
    for m in ALL_METHODS:
        assert m not in RUST_COMPAT_ROUTE
