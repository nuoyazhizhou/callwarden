# -*- coding: utf-8 -*-
"""P0-COMPAT-v3 tools_security 15 方法 rust_native live HTTP RPC 套件。

对应 T-1788963105720-60bfc80c：list_branches / merge_preview / get_edit_history /
find_shared_symbols / cross_repo_impact / cross_repo_summary / lsp_hover /
lsp_definition / lsp_references / lsp_diagnostics / lsp_completion /
lsp_check_available / rule_candidate_list / rule_list / get_applicable_rules
从 python_compat 迁移 rust_native 后的契约验证（live daemon 直连）。
"""
import pytest

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
# rust_native 成功路径
# ---------------------------------------------------------------

def test_list_branches_returns_workspace_rows(client):
    r = _call(client, "list_branches", {})
    assert isinstance(r, list)
    assert len(r) >= 1
    row = r[0]
    assert {"id", "name", "root_path", "created_at", "is_active", "symbol_count"} <= set(row)


def test_merge_preview_no_such_branch_fail_soft(client):
    r = _call(client, "merge_preview",
              {"source_branch": "NO_SUCH_BRANCH_XYZ", "target_branch": "also-missing"})
    assert isinstance(r, dict)
    assert "error" in r


def test_merge_preview_success_shape(client):
    r = _call(client, "merge_preview",
              {"source_branch": "callwarden", "target_branch": "test-h9-ws"})
    assert isinstance(r, dict)
    assert {"affected_symbols", "impact_layers", "risk_level"} <= set(r)
    assert r["risk_level"] in {"low", "medium", "high"}


def test_get_edit_history_empty_table(client):
    r = _call(client, "get_edit_history", {"file_path": "", "limit": 3})
    assert isinstance(r, list)


def test_find_shared_symbols_total_matches_list(client):
    r = _call(client, "find_shared_symbols", {"workspace_a": "", "workspace_b": ""})
    assert isinstance(r, dict)
    assert r["total_shared"] == len(r["shared_symbols"])
    assert r["total_shared"] >= 0


def test_find_shared_symbols_missing_workspace_empty(client):
    r = _call(client, "find_shared_symbols",
              {"workspace_a": "NO_SUCH_WS_XYZ", "workspace_b": ""})
    assert r == {"total_shared": 0, "shared_symbols": []}


def test_cross_repo_impact_no_such_hash_empty(client):
    r = _call(client, "cross_repo_impact",
              {"symbol_hash": "NO_SUCH_HASH_XYZ", "depth": 2})
    assert r == {
        "source_symbol": "",
        "source_workspace": "",
        "impacted_repos": [],
        "total_impacted_repos": 0,
        "risk_level": "none",
    }


def test_cross_repo_impact_real_hash_shape(client):
    r = _call(client, "cross_repo_impact",
              {"symbol_hash": "0002e9fedd5b2ea5e5aa22f7a8d55f0f6fef178189799eff27fc98fb8e980d95",
               "depth": 2})
    assert isinstance(r, dict)
    assert {"source_symbol", "source_workspace", "local_impacted_count",
            "impacted_repos", "total_impacted_repos", "risk_level"} <= set(r)
    assert r["source_workspace"] == "TokenSlim"
    assert r["risk_level"] in {"low", "medium", "high"}


def test_cross_repo_summary_totals_consistent(client):
    r = _call(client, "cross_repo_summary", {})
    assert isinstance(r, dict)
    assert r["total_repos"] == len(r["repos"])
    assert r["total_cross_deps"] == 0
    assert r["deps_by_type"] == {}


# ---------------------------------------------------------------
# LSP 组：fail-soft 空结构契约（LSP 服务器未安装环境）
# ---------------------------------------------------------------

def test_lsp_hover_unavailable(client):
    r = _call(client, "lsp_hover", {"file_path": "cw.py", "line": 0, "character": 0})
    assert r == {"file_path": "cw.py", "line": 0, "character": 0,
                 "contents": "", "available": False}


def test_lsp_definition_unavailable(client):
    r = _call(client, "lsp_definition", {"file_path": "cw.py", "line": 0, "character": 0})
    assert r == {"definitions": [], "available": False}


def test_lsp_references_unavailable(client):
    r = _call(client, "lsp_references",
              {"file_path": "cw.py", "line": 0, "character": 0, "include_declaration": True})
    assert r == {"references": [], "total": 0, "available": False}


def test_lsp_diagnostics_unavailable(client):
    r = _call(client, "lsp_diagnostics", {"file_path": "cw.py"})
    assert r == {"file_path": "cw.py", "diagnostics": [], "total": 0, "available": False}


def test_lsp_completion_unavailable(client):
    r = _call(client, "lsp_completion", {"file_path": "cw.py", "line": 0, "character": 0})
    assert r == {"completions": [], "total": 0, "available": False}


def test_lsp_check_available_all_servers(client):
    r = _call(client, "lsp_check_available", {"language": ""})
    assert isinstance(r, dict)
    assert set(r["available_servers"]) == {"python", "typescript", "go", "rust"}
    assert all(isinstance(v, bool) for v in r["available_servers"].values())
    assert r["total_available"] == sum(1 for v in r["available_servers"].values() if v)


def test_lsp_check_available_single_language(client):
    r = _call(client, "lsp_check_available", {"language": "python"})
    assert set(r["available_servers"]) == {"python"}


# ---------------------------------------------------------------
# agent rules 组（空表 → 空结构契约）
# ---------------------------------------------------------------

def test_rule_candidate_list_empty(client):
    r = _call(client, "rule_candidate_list", {"status": "pending", "limit": 5})
    assert r == {"candidates": [], "count": 0}


def test_rule_list_empty(client):
    r = _call(client, "rule_list", {"status": "active", "limit": 5})
    assert r == {"rules": [], "count": 0}


def test_get_applicable_rules_empty(client):
    r = _call(client, "get_applicable_rules",
              {"context": {"language": "python"}, "limit": 5})
    assert r == {"rules": [], "count": 0}


# ---------------------------------------------------------------
# 未知 workspace fail-closed
# ---------------------------------------------------------------

def test_unknown_workspace_fail_closed(client):
    for method, params in [
        ("list_branches", {}),
        ("merge_preview", {"source_branch": "a", "target_branch": "b"}),
        ("cross_repo_summary", {}),
        ("rule_list", {}),
    ]:
        with pytest.raises(Exception) as ei:
            client.call(method, {"workspace_instance_id": UNKNOWN_INSTANCE, **params})
        assert "workspace_not_found" in str(ei.value), method


# ---------------------------------------------------------------
# Python 退役断言
# ---------------------------------------------------------------

def test_python_side_retired():
    ts = pytest.importorskip("callwarden.server.tools.tools_security")
    cr = pytest.importorskip("callwarden.server.compat_registry")
    assert ts._SECURITY_READ_ONLY_METHODS == {}
    handlers = ["_h_list_branches", "_h_merge_preview", "_h_get_edit_history",
                "_h_find_shared_symbols", "_h_cross_repo_impact", "_h_cross_repo_summary",
                "_h_lsp_hover", "_h_lsp_definition", "_h_lsp_references",
                "_h_lsp_diagnostics", "_h_lsp_completion", "_h_lsp_check_available",
                "_h_rule_candidate_list", "_h_rule_list", "_h_get_applicable_rules"]
    assert all(hasattr(ts, h) for h in handlers)
    gone = ["list_branches", "merge_preview", "get_edit_history", "find_shared_symbols",
            "cross_repo_impact", "cross_repo_summary", "lsp_hover", "lsp_definition",
            "lsp_references", "lsp_diagnostics", "lsp_completion", "lsp_check_available",
            "rule_candidate_list", "rule_list", "get_applicable_rules"]
    assert all(m not in cr.RUST_COMPAT_ROUTE for m in gone)
