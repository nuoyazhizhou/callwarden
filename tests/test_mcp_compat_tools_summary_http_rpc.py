# -*- coding: utf-8 -*-
"""P0-COMPAT-v3 tools_summary 19 方法 rust_native live HTTP RPC 套件。

对应 T-1788963106520-907544c8：get_summary / project_brief / repo_map /
test_impact_selection / who_to_ask / get_ownership_map / guardrail_scan /
guardrail_check_edit / guardrail_list_rules / blast_radius / ask_codebase /
get_token_savings_report / get_vulnerability_blast_radius /
get_clone_aware_impact / review_readiness / cross_layer_impact /
evolution_frequency / hotspot_evolution / defect_learn
从 python_compat 迁移 rust_native 后的契约验证（live daemon 直连）。

写面语义（probeproven 2026-09-10）：guardrail_scan / guardrail_list_rules /
defect_learn（有 qualifying 变更时）走只读快照连接必拒 → Rust fail-closed
internal_error，消息对齐 Python worker OperationalError parity。
"""
import pytest

from callwarden.server.daemon_client import DaemonRemoteError, HttpDaemonRpcClient
from callwarden.config import get_http_authority_id

CANONICAL_INSTANCE = None  # 隔离 daemon workspace_instance_id，由 w3_live fixture 注入
_CANONICAL_ENDPOINT = None  # 隔离 daemon endpoint，由 w3_live fixture 注入
UNKNOWN_INSTANCE = "00000000deadbeef"


@pytest.fixture(scope="module")
def client(w3_live):
    """W3 隔离 harness：注入隔离 daemon 的 client / inst / endpoint。"""
    global CANONICAL_INSTANCE, _CANONICAL_ENDPOINT
    CANONICAL_INSTANCE = w3_live["inst"]
    _CANONICAL_ENDPOINT = w3_live["endpoint"]
    return w3_live["client"]


def _call(c, method, params):
    params = dict(params)
    params.setdefault("workspace_instance_id", CANONICAL_INSTANCE)
    return c.call(method, params)


# ---------------------------------------------------------------
# 只读成功路径（确定性形状）
# ---------------------------------------------------------------

def test_get_summary_no_match_null(client):
    r = _call(client, "get_summary", {"qualified_name": "NO_SUCH_SYM_XYZ"})
    assert r is None


def test_project_brief_shape(client):
    r = _call(client, "project_brief", {})
    assert isinstance(r, dict)
    assert {"project_type", "file_count", "function_count", "total_lines",
            "modules", "hot_functions", "health_score", "health_level",
            "avg_complexity", "comment_coverage"} <= set(r)
    assert r["file_count"] > 0


def test_repo_map_text_and_mermaid(client):
    r = _call(client, "repo_map", {})
    assert isinstance(r, str) and "仓库模块依赖图" in r
    m = _call(client, "repo_map", {"format": "mermaid"})
    assert isinstance(m, str) and m.startswith("graph TD")


def test_test_impact_selection_no_target_empty(client):
    r = _call(client, "test_impact_selection", {"qualified_name": "NO_SUCH_FN_XYZ"})
    assert r == []


def test_who_to_ask_empty_path_null(client):
    r = _call(client, "who_to_ask", {"file_path": ""})
    assert r is None or r == {}


def test_get_ownership_map_list(client):
    r = _call(client, "get_ownership_map", {})
    assert isinstance(r, list)


def test_blast_radius_no_such_hash_empty(client):
    r = _call(client, "blast_radius", {"symbol_hash": "NO_SUCH_HASH_XYZ", "depth": 3})
    assert isinstance(r, dict)
    assert r["total_impacted"] == 0
    assert r["by_layer"] == {"code": 0, "db": 0, "api": 0, "config": 0}


def test_ask_codebase_keyword_fallback(client):
    # Python parity：query 归一化仅替换 ','/'.'，不拆下划线 → 必须空格分词
    r = _call(client, "ask_codebase", {"question": "dispatch rpc route", "top_k": 3})
    assert isinstance(r, dict)
    assert r["metadata"]["fallback_used"] == "keyword_fallback"
    assert r["seed_functions"], "keyword_fallback 应命中种子函数"


def test_get_token_savings_report_shape(client):
    r = _call(client, "get_token_savings_report", {"time_window": "30d"})
    assert isinstance(r, dict)
    assert {"time_window", "total_saved", "total_operations", "avg_savings_pct",
            "by_operation", "daily_trend", "headline"} <= set(r)


def test_get_vulnerability_blast_radius_shape(client):
    r = _call(client, "get_vulnerability_blast_radius",
              {"finding_id": 0, "severity_filter": "", "depth": 3})
    assert isinstance(r, dict)
    assert {"total_findings", "total_impacted_symbols", "risk_level",
            "findings", "impacted_symbols_summary"} <= set(r)
    assert r["risk_level"] == "none"


def test_get_clone_aware_impact_unknown_error(client):
    r = _call(client, "get_clone_aware_impact",
              {"qualified_name": "NO_SUCH_FN_XYZ", "depth": 3})
    assert isinstance(r, dict)
    assert "error" in r and "符号不存在" in r["error"]


def test_review_readiness_unknown_low(client):
    r = _call(client, "review_readiness", {"symbol_hash": "NO_SUCH_HASH_XYZ"})
    assert isinstance(r, dict)
    assert r["impact_scope"] == "low" and r["risk_level"] == "low"
    assert r["by_layer"] == {"code": 0, "db": 0, "api": 0, "config": 0}


def test_cross_layer_impact_unknown_all_empty(client):
    r = _call(client, "cross_layer_impact", {"symbol_hash": "NO_SUCH_HASH_XYZ"})
    assert r == {"code": [], "db": [], "api": [], "config": []}


def test_evolution_frequency_unknown_zero(client):
    r = _call(client, "evolution_frequency",
              {"qualified_name": "NO_SUCH_FN_XYZ", "time_window": "30d"})
    assert isinstance(r, dict)
    assert r["change_count"] == 0
    assert r["distribution"] == {"daily": {}, "weekly": {}, "monthly": {}}


def test_hotspot_evolution_desc_sorted(client):
    r = _call(client, "hotspot_evolution", {"module_filter": ""})
    assert isinstance(r, list)
    scores = [x.get("hotspot_score", 0) for x in r]
    assert scores == sorted(scores, reverse=True)


# ---------------------------------------------------------------
# 写面 fail-closed（parity：Python ro worker OperationalError）
# ---------------------------------------------------------------

@pytest.mark.parametrize("method,params", [
    ("guardrail_scan", {}),
    ("guardrail_scan", {"file_filter": "server"}),
    ("guardrail_list_rules", {}),
    ("guardrail_list_rules", {"category_filter": "db_safety"}),
])
def test_guardrail_write_face_fail_closed(client, method, params):
    with pytest.raises(DaemonRemoteError) as ei:
        _call(client, method, params)
    assert "write-face" in str(ei.value)


def test_defect_learn_no_qualifying_change_zero(client):
    # 空hash/未知 commit → 先判定后写，无 qualifying 变更 → 零结果（不触写面）
    for h in ("", "deadbeef" * 5):
        r = _call(client, "defect_learn", {"fix_commit_hash": h})
        assert isinstance(r, dict)
        assert r["learned_patterns"] == 0 and r["learned_fixes"] == 0


# ---------------------------------------------------------------
# 未知 workspace fail-closed
# ---------------------------------------------------------------

def test_unknown_workspace_fail_closed(client):
    for method, params in [
        ("project_brief", {}),
        ("repo_map", {}),
        ("blast_radius", {"symbol_hash": "H-X"}),
        ("hotspot_evolution", {}),
    ]:
        with pytest.raises(Exception) as ei:
            client.call(method, {"workspace_instance_id": UNKNOWN_INSTANCE, **params})
        assert "workspace_not_found" in str(ei.value), method


# ---------------------------------------------------------------
# Python 退役断言
# ---------------------------------------------------------------

def test_python_side_retired():
    ts = pytest.importorskip("callwarden.server.tools.tools_summary")
    cr = pytest.importorskip("callwarden.server.compat_registry")
    assert ts._SUMMARY_READ_ONLY_METHODS == {}
    handlers = ["_h_get_summary", "_h_project_brief", "_h_repo_map",
                "_h_test_impact_selection", "_h_who_to_ask", "_h_get_ownership_map",
                "_h_guardrail_scan", "_h_guardrail_check_edit", "_h_guardrail_list_rules",
                "_h_blast_radius", "_h_ask_codebase", "_h_get_token_savings_report",
                "_h_get_vulnerability_blast_radius", "_h_get_clone_aware_impact",
                "_h_review_readiness", "_h_cross_layer_impact", "_h_evolution_frequency",
                "_h_hotspot_evolution", "_h_defect_learn"]
    assert all(hasattr(ts, h) for h in handlers)
    gone = ["get_summary", "project_brief", "repo_map", "test_impact_selection",
            "who_to_ask", "get_ownership_map", "guardrail_scan", "guardrail_check_edit",
            "guardrail_list_rules", "blast_radius", "ask_codebase",
            "get_token_savings_report", "get_vulnerability_blast_radius",
            "get_clone_aware_impact", "review_readiness", "cross_layer_impact",
            "evolution_frequency", "hotspot_evolution", "defect_learn"]
    assert all(m not in cr.RUST_COMPAT_ROUTE for m in gone)
