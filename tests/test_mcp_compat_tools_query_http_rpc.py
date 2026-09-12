"""P0-COMPAT-v3（T-1788963103216-cb818938）tools_query 组 → Rust daemon native。

覆盖方法（8）：get_symbol_history / get_recent_changes / get_impact /
get_comment_from_version / get_issue_summary / find_issues / get_test_coverage /
export_module_graph。

覆盖 task 要求：
  success / invalid 参数 / daemon unavailable（fail-closed）矩阵；
  Python compat 退役断言（_SYMBOL_READ_ONLY_METHODS 已清空 +
  RUST_COMPAT_ROUTE 已摘除 8 条目）。

语义对照（Python 真相源）：
- get_symbol_history：db_query.get_symbol_history —— 符号历史版本按 parsed_at DESC。
- get_recent_changes：db_query.get_recent_changes —— since 解析（d/h/m/s）→
  changed_files/changed_functions/since_seconds。
- get_impact：analyzers/call_chain.get_call_chain_up —— 向上 BFS。
- get_comment_from_version：db_comment.get_comment_from_version —— fn@vN/hash。
- get_issue_summary：analyzers/issues.get_issue_summary —— 语言规则汇总。
- find_issues：analyzers/issues.get_function_issues（issue_type→issue_filter）。
- get_test_coverage：analyzers/coverage.get_test_coverage。
- export_module_graph：db_query.export_module_graph —— mermaid/dot；
  非法 format → invalid_params（Python ValueError 等价）。
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
    "get_symbol_history",
    "get_recent_changes",
    "get_impact",
    "get_comment_from_version",
    "get_issue_summary",
    "find_issues",
    "get_test_coverage",
    "export_module_graph",
]


@pytest.fixture()
def live_daemon(w3_live):
    """W3 隔离 harness：注入隔离 daemon 的 client / inst / endpoint。"""
    global CANONICAL_INSTANCE, _CANONICAL_ENDPOINT
    CANONICAL_INSTANCE = w3_live["inst"]
    _CANONICAL_ENDPOINT = w3_live["endpoint"]
    return w3_live["client"]


# ---------------------------------------------------------------------------
# success 矩阵（真实 workspace 快照数据，只读语义 fail-soft）
# ---------------------------------------------------------------------------
def test_symbol_history_unknown_symbol(live_daemon):
    """未知符号 → 空数组（不报错）。"""
    c = live_daemon
    r = c.call("get_symbol_history", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "qualified_name": "no::such::symbol::zzz",
    })
    assert isinstance(r, list)
    assert r == []


def test_recent_changes_shape(live_daemon):
    """since=1d → dict 三键：changed_files/changed_functions/since_seconds=86400。"""
    c = live_daemon
    r = c.call("get_recent_changes", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "since": "1d",
    })
    assert isinstance(r, dict)
    assert set(r.keys()) >= {"changed_files", "changed_functions", "since_seconds"}
    assert r["since_seconds"] == 86400
    assert isinstance(r["changed_files"], list)
    assert isinstance(r["changed_functions"], list)


def test_impact_unknown_start(live_daemon):
    """未知起始符号 → 零上游（max_depth_reached=0, total_upstream=0）。"""
    c = live_daemon
    r = c.call("get_impact", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "qualified_name": "no::such::symbol::zzz",
    })
    assert isinstance(r, dict)
    assert r["start"] == "no::such::symbol::zzz"
    assert r["max_depth_reached"] == 0
    assert r["total_upstream"] == 0
    assert r["levels"] == []
    assert r["all_upstream"] == []


def test_comment_from_version_bad_spec(live_daemon):
    """无 @ 的 spec → null（与 Python 返回 None 一致）。"""
    c = live_daemon
    r = c.call("get_comment_from_version", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "spec": "no-at-sign",
    })
    assert r is None


def test_comment_from_version_unknown_symbol(live_daemon):
    """fn@vN 无历史 → null。"""
    c = live_daemon
    r = c.call("get_comment_from_version", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "spec": "no::such::symbol::zzz@v1",
    })
    assert r is None


def test_issue_summary_shape(live_daemon):
    """汇总 dict：total_functions/issues 列表（含 ratio）。"""
    c = live_daemon
    r = c.call("get_issue_summary", {"workspace_instance_id": CANONICAL_INSTANCE})
    assert isinstance(r, dict)
    assert set(r.keys()) >= {
        "total_functions", "functions_with_issues",
        "issue_free_functions", "issue_free_ratio", "issues",
    }
    assert isinstance(r["issues"], list)
    if r["issues"]:
        assert set(r["issues"][0].keys()) >= {
            "type", "label", "severity", "function_count",
            "total_occurrences", "ratio",
        }


def test_find_issues_returns_list(live_daemon):
    """limit=5 → list 且 ≤5。"""
    c = live_daemon
    r = c.call("find_issues", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "limit": 5,
    })
    assert isinstance(r, list)
    assert len(r) <= 5


def test_test_coverage_shape(live_daemon):
    """统计 dict：total_functions/test_functions/test_ratio 等。"""
    c = live_daemon
    r = c.call("get_test_coverage", {"workspace_instance_id": CANONICAL_INSTANCE})
    assert isinstance(r, dict)
    assert set(r.keys()) >= {
        "total_functions", "test_functions", "test_ratio",
        "total_modules", "modules_with_tests", "module_coverage", "test_by_module",
    }
    assert r["total_functions"] >= r["test_functions"] >= 0


def test_export_module_graph_mermaid(live_daemon):
    """mermaid 格式 → 以 flowchart TD 开头的字符串。"""
    c = live_daemon
    r = c.call("export_module_graph", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "format": "mermaid",
    })
    assert isinstance(r, str)
    assert r.startswith("flowchart TD")


def test_export_module_graph_dot(live_daemon):
    """dot 格式 → digraph 头尾完整。"""
    c = live_daemon
    r = c.call("export_module_graph", {
        "workspace_instance_id": CANONICAL_INSTANCE,
        "format": "dot",
    })
    assert isinstance(r, str)
    assert r.startswith("digraph module_dependencies {")
    assert r.rstrip().endswith("}")


# ---------------------------------------------------------------------------
# invalid 参数矩阵
# ---------------------------------------------------------------------------
def test_export_module_graph_invalid_format(live_daemon):
    """非法 format → 业务错误（Python ValueError 等价 fail-closed）。"""
    c = live_daemon
    with pytest.raises(Exception) as ei:
        c.call("export_module_graph", {
            "workspace_instance_id": CANONICAL_INSTANCE,
            "format": "yaml",
        })
    # 不应静默返回 mermaid/dot 之外的内容
    assert not isinstance(ei.value, DaemonUnavailableError)


def test_query_methods_missing_workspace_instance(live_daemon):
    """缺 workspace_instance_id → invalid_params（快照路由门禁）。"""
    c = live_daemon
    with pytest.raises(Exception):
        c.call("get_test_coverage", {})


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method", ALL_METHODS)
def test_query_methods_daemon_unavailable_fail_closed(method):
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    # DaemonUnavailableError 异常类型本身即 fail-closed 证据
    # （错误消息随 client health 探测路径变化：E_HTTP_DAEMON_UNAVAILABLE / 502 等）。
    with pytest.raises(DaemonUnavailableError):
        c.call(method, {"workspace_instance_id": CANONICAL_INSTANCE})


# ---------------------------------------------------------------------------
# Python compat 退役断言：条目已从只读白名单摘除
# ---------------------------------------------------------------------------
def test_python_compat_entries_retired():
    from callwarden.server.tools import tools_query
    assert tools_query._SYMBOL_READ_ONLY_METHODS == {}
    from callwarden.server.compat_registry import RUST_COMPAT_ROUTE
    for m in ALL_METHODS:
        assert m not in RUST_COMPAT_ROUTE
