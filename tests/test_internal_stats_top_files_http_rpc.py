"""INT-001 (A′ graph_snapshot) internal `stats_top_files` Rust native 迁移验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_internal_stats_top_files_http_rpc.py）：
  - 结构不变量：stats_top_files 已从 python_compat 白名单退休（compat_registry
    默认 registry 为空、RUST_COMPAT_ROUTE 不含该项），Rust 侧 query_compat_handlers
    handle_stats_top_files 拥有 SQL（由 Rust agent 编译核验）
  - 迁移后路由：route_worker_call 对未注册 compat_route 的方法 fail-closed 返回
    E_HTTP_COMPAT_UNSUPPORTED（不回退本地 SQLite）
  - limit clamp：Rust handler 复刻 Python _coerce_limit（1..500）

Rust 侧 handle_stats_top_files / dispatch / http_server capability 行由 Rust
专项 agent 编译与测试核验。
"""

import pytest

import callwarden.cli.main as main_mod
from callwarden.server import compat_registry


def test_int001_stats_top_files_retired_from_python_compat():
    """结构不变量：stats_top_files 不再注册 python_compat 路由。"""
    reg = compat_registry.get_compat_registry()
    route = compat_registry.compat_route("stats_top_files")
    assert route is None, (
        "stats_top_files 已迁移 rust_native，Python 默认 registry 不得再注册"
    )
    assert "stats_top_files" not in compat_registry.RUST_COMPAT_ROUTE, (
        "RUST_COMPAT_ROUTE 不得再含 stats_top_files（python_compat 行已退休）"
    )


def test_int001_route_worker_call_fail_closed_after_retirement(monkeypatch):
    """迁移后 route_worker_call 对未注册方法 fail-closed（不落本地 SQLite）。"""
    # 强制非 local 模式（HTTP），确保不执行 fallback
    monkeypatch.setattr(main_mod, "get_daemon_mode", lambda: "enterprise")

    hit = {"fallback": False}

    def _fallback(*a, **k):
        hit["fallback"] = True
        return {"count": 0, "files": []}

    from callwarden.server.daemon_client import route_worker_call
    out = route_worker_call("stats_top_files", {"limit": 10}, _fallback)
    assert hit["fallback"] is False, "迁移后不得再走本地 SQLite fallback"
    assert isinstance(out, dict)
    assert out.get("error") == "E_HTTP_COMPAT_UNSUPPORTED"


def test_int001_limit_clamp_semantics():
    """结构不变量：Python 侧 _coerce_limit 仍保持 1..500 clamp（与 Rust 对齐）。"""
    assert compat_registry._coerce_limit(1) == 1
    assert compat_registry._coerce_limit(500) == 500
    with pytest.raises(ValueError):
        compat_registry._coerce_limit(0)
    with pytest.raises(ValueError):
        compat_registry._coerce_limit(501)
