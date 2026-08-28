"""SRV-008 迁移验收：daemon_server.py 六符号 Python authority → Rust daemon。

覆盖 task `T-1787323461079-dc5ac87c` step[2] fixture_negative_matrix：
["success", "invalid", "authority", "unavailable", "restart"]。

设计要点（SRV-008 = daemon_server 服务端符号权威下沉，route B）：
- Python `server/daemon_server.py::_is_rust_acl_rolled_back` /
  `_is_rust_health_rolled_back` 原本短连接 open 本地 SQLite 查 rollback_config
  （feature=rust_daemon_acl_path_budget / rust_daemon_health_check），
  SRV-008 后退化为纯 daemon RPC 薄客户端（60s 缓存保留），经
  `mcp.daemon_server.is_rust_*_rolled_back` 查询，daemon 不可用/响应畸形时
  fail-soft 视为未回滚（对齐原 except→False），绝不回退本地 SQLite。
- `get_registry_conn` 原返回 sqlite3.Connection；RPC 无法传递连接对象，
  下沉为权威元信息探测（对齐 SRV-006 handle_get_db 先例），返回归一化
  `{"registry_db", "exists", "schema_ready", "source"}`，daemon 不可用时
  fail-soft 返回带 reason=daemon_unavailable 的元信息 dict。
- 连带收紧：api_register_workspace / api_list_workspaces /
  api_get_workspace_status / api_update_workspace_status 零外部调用方，
  原依赖 get_registry_conn 可写连接，改 fail-closed 抛
  DaemonRpcError("method_migrated")，权威归 Rust dispatch 的
  workspace.register/list/status。
- 服务端核心符号保留（finding）：dispatch / _registry_conn /
  _get_workspace_resources 是 Python daemon 服务端组件（RPC 自调用 /
  返回进程内 Connection 与资源对象），权威由 Rust handler 元信息探测/
  权威声明承接，函数体保留；test_phase5_cas_replicator_wiring.py 对
  _registry_conn / _get_workspace_resources 有源码级断言
  （必须含 apply_daemon_rw_pragmas），本测试同步锁定该保留契约。
- daemon_server 无协议自依赖环（RPC 经 _mcp_common，不经本模块帧收发），
  故无需 SRV-007 式重入守卫。
- 本测试用内存态 FakeDaemonServerDaemon 模拟 daemon 的
  daemon_server_handlers 行为，不依赖真实 daemon 进程，也不触碰本地
  SQLite 文件。
"""

import ast

import pytest

from callwarden.server.daemon_client import DaemonUnavailableError
from callwarden.server.daemon_server import (
    DaemonRpcError,
    _is_rust_acl_rolled_back,
    _is_rust_health_rolled_back,
    get_registry_conn,
)

_ACL_METHOD = "mcp.daemon_server.is_rust_acl_rolled_back"
_HEALTH_METHOD = "mcp.daemon_server.is_rust_health_rolled_back"
_REGISTRY_METHOD = "mcp.daemon_server.get_registry_conn"


class FakeDaemonRpcError(Exception):
    """模拟 daemon 端 DaemonRpcError（带稳定 error code）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class FakeDaemonServerDaemon:
    """内存态 daemon 权威（对齐 Rust `daemon_server_handlers` 语义）。"""

    def __init__(self):
        self.available: bool = True
        self.acl_rolled_back: bool = False
        self.health_rolled_back: bool = False
        self.registry_meta = {
            "registry_db": "/fake/.callwarden/daemon/registry.db",
            "exists": True,
            "schema_ready": True,
            "source": "module",
        }
        self.response_override = {}  # method -> 畸形响应注入（invalid 段）
        self.calls: list = []

    def __call__(self, method: str, params: dict):
        self.calls.append((method, params))
        if not self.available:
            raise DaemonUnavailableError("daemon 不可用（测试模拟）")
        if method in self.response_override:
            return self.response_override[method]
        if method == _ACL_METHOD:
            return {"rolled_back": self.acl_rolled_back}
        if method == _HEALTH_METHOD:
            return {"rolled_back": self.health_rolled_back}
        if method == _REGISTRY_METHOD:
            return dict(self.registry_meta)
        raise FakeDaemonRpcError("method_not_found", f"未知方法 {method}")


@pytest.fixture
def fake_daemon(monkeypatch):
    """每个测试安装一个干净的内存态 daemon 薄客户端 + 复位 60s 缓存。"""
    import callwarden.server.daemon_server as ds

    daemon = FakeDaemonServerDaemon()
    monkeypatch.setattr("callwarden.server.daemon_server._call_daemon_rpc", daemon)
    # 复位模块级 60s 缓存，避免跨测试污染
    monkeypatch.setattr(ds, "_ACL_ROLLBACK_CACHE", {"ts": 0.0, "value": False})
    monkeypatch.setattr(ds, "_HEALTH_ROLLBACK_CACHE", {"ts": 0.0, "value": False})
    return daemon


# ============================================================
# 1) success
# ============================================================


def test_success_not_rolled_back(fake_daemon):
    # 默认 daemon 报告未回滚 → Rust 短路路径保持启用
    assert _is_rust_acl_rolled_back() is False
    assert _is_rust_health_rolled_back() is False


def test_success_rolled_back_true(fake_daemon):
    fake_daemon.acl_rolled_back = True
    fake_daemon.health_rolled_back = True
    assert _is_rust_acl_rolled_back() is True
    assert _is_rust_health_rolled_back() is True


def test_success_cache_suppresses_second_rpc(fake_daemon):
    # 60s 缓存保留：第二次调用不再发起 RPC（acl 与 health 各自独立缓存）
    assert _is_rust_acl_rolled_back() is False
    assert _is_rust_acl_rolled_back() is False
    assert _is_rust_health_rolled_back() is False
    assert _is_rust_health_rolled_back() is False
    assert len(fake_daemon.calls) == 2  # 1 次 acl + 1 次 health


def test_success_registry_conn_meta(fake_daemon):
    meta = get_registry_conn()
    assert meta["exists"] is True
    assert meta["schema_ready"] is True
    assert meta["source"] == "module"
    # RPC 探测返回元信息 dict，绝不返回可写连接对象
    assert not hasattr(meta, "execute")


# ============================================================
# 2) invalid（daemon 返回畸形响应 → isinstance 校验 → fail-soft）
# ============================================================


def test_invalid_response_non_dict(fake_daemon):
    fake_daemon.response_override[_ACL_METHOD] = "rolled_back: true"  # 非 dict
    fake_daemon.response_override[_HEALTH_METHOD] = 42
    assert _is_rust_acl_rolled_back() is False
    assert _is_rust_health_rolled_back() is False


def test_invalid_response_missing_key(fake_daemon):
    fake_daemon.response_override[_ACL_METHOD] = {"flag": 1}  # 缺 rolled_back 键
    assert _is_rust_acl_rolled_back() is False


def test_invalid_response_truthy_int(fake_daemon):
    # Rust handler 语义 rolled_back 为 bool；Python 侧 bool() 兼容 1/0
    fake_daemon.response_override[_HEALTH_METHOD] = {"rolled_back": 1}
    assert _is_rust_health_rolled_back() is True


def test_invalid_registry_meta_non_dict(fake_daemon):
    fake_daemon.response_override[_REGISTRY_METHOD] = "not-a-dict"
    meta = get_registry_conn()
    # 畸形响应 → fail-soft 归一化元信息（不抛异常、不回退本地连接）
    assert meta["exists"] is False
    assert meta["schema_ready"] is False
    assert meta["reason"] == "daemon_unavailable"


# ============================================================
# 3) authority（rollback_config / registry schema 权威在 daemon）
# ============================================================


def test_authority_rollback_methods_verbatim(fake_daemon):
    fake_daemon.acl_rolled_back = True
    fake_daemon.health_rolled_back = True
    assert _is_rust_acl_rolled_back() is True
    assert _is_rust_health_rolled_back() is True
    # 权威经且仅经 daemon RPC 读取，方法名逐字对齐 step0 dispatch 分支
    assert fake_daemon.calls == [(_ACL_METHOD, {}), (_HEALTH_METHOD, {})]


def test_authority_registry_conn_via_daemon_only(fake_daemon):
    get_registry_conn()
    # 薄客户端不传 db_path：权威库定位是 daemon 内部职责
    assert fake_daemon.calls == [(_REGISTRY_METHOD, {})]


def test_authority_api_workspace_funcs_fail_closed(fake_daemon):
    # api_* 4 函数已退役：即使 daemon 可用也 fail-closed，
    # 权威归 daemon RPC workspace.register/list/status（Rust dispatch）
    import callwarden.server.daemon_server as ds

    cases = [
        (ds.api_register_workspace, (1000, "/client", "/host")),
        (ds.api_list_workspaces, ()),
        (ds.api_get_workspace_status, ("ws-x",)),
        (ds.api_update_workspace_status, ("ws-x", "archived")),
    ]
    for fn, args in cases:
        with pytest.raises(DaemonRpcError) as exc:
            fn(*args)
        assert exc.value.code == "method_migrated"
    # fail-closed 为静态拒止，不得触发任何 RPC
    assert fake_daemon.calls == []


def test_retained_server_core_symbols_not_retired():
    # 服务端核心符号保留契约：dispatch 为 RPC 路由器、_registry_conn /
    # _get_workspace_resources 返回进程内对象（test_phase5_cas_replicator_wiring
    # 源码级断言要求含 apply_daemon_rw_pragmas），不得被误退役为薄客户端。
    import callwarden.server.daemon_server as ds

    src_file = ds.__file__
    with open(src_file, "r", encoding="utf-8") as f:
        full_src = f.read()
    tree = ast.parse(full_src)
    bodies = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in {
            "dispatch", "_registry_conn", "_get_workspace_resources",
        }:
            bodies[node.name] = "\n".join(
                ast.get_source_segment(full_src, s) or "" for s in node.body
            )
    assert set(bodies) == {"dispatch", "_registry_conn", "_get_workspace_resources"}
    assert "apply_daemon_rw_pragmas" in bodies["_registry_conn"]
    assert "apply_daemon_rw_pragmas" in bodies["_get_workspace_resources"]
    # dispatch 保留完整路由形态（非薄客户端桩：多条分支语句）
    assert bodies["dispatch"].count("elif") >= 5 or bodies["dispatch"].count("if ") >= 5


# ============================================================
# 4) unavailable（fail-soft，绝不回退本地 SQLite）
# ============================================================


def test_unavailable_fail_soft_false(fake_daemon):
    fake_daemon.available = False
    # 只读 authority 读：daemon 不可用时 fail-soft 视为未回滚（不抛异常）
    assert _is_rust_acl_rolled_back() is False
    assert _is_rust_health_rolled_back() is False


def test_unavailable_registry_conn_fail_soft(fake_daemon):
    fake_daemon.available = False
    meta = get_registry_conn()
    assert meta["exists"] is False
    assert meta["schema_ready"] is False
    assert meta["source"] == "module"
    assert meta["reason"] == "daemon_unavailable"


# ============================================================
# 5) restart（首次不可用 → 恢复后读到正确权威位）
# ============================================================


def test_restart_rolled_back_reads_after_recover(fake_daemon):
    import callwarden.server.daemon_server as ds

    fake_daemon.available = False
    assert _is_rust_acl_rolled_back() is False  # fail-soft，并进入 60s 缓存
    fake_daemon.available = True
    fake_daemon.acl_rolled_back = True
    # 失效 60s 缓存以模拟"恢复后重新探测 daemon 权威"
    ds._ACL_ROLLBACK_CACHE["ts"] = 0.0
    assert _is_rust_acl_rolled_back() is True


def test_restart_registry_meta_after_recover(fake_daemon):
    fake_daemon.available = False
    assert get_registry_conn()["reason"] == "daemon_unavailable"
    fake_daemon.available = True
    # get_registry_conn 无缓存：恢复后立即可读到权威元信息
    assert get_registry_conn()["schema_ready"] is True


# ============================================================
# 零权威证据：AST 扫描（已退役符号不再含 SQLite 权威残留）
# ============================================================


def test_no_sqlite_authority_in_retired_symbols():
    # 直接取已加载模块的 __file__，确保扫描的是当前生效（worktree）的迁移后源码
    import callwarden.server.daemon_server as ds

    with open(ds.__file__, "r", encoding="utf-8") as f:
        full_src = f.read()
    tree = ast.parse(full_src)

    banned_tokens = {"sqlite3", "DB_PATH", "SELECT", "PRAGMA",
                     "rollback_config", ".connect("}
    target_funcs = {"_is_rust_acl_rolled_back", "_is_rust_health_rolled_back",
                    "get_registry_conn"}

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in target_funcs:
            # 仅扫描函数体实际代码（排除 docstring，docstring 可合法描述 daemon 行为）
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0].value, "value", None), str)
            ):
                body = body[1:]
            code_seg = "\n".join(ast.get_source_segment(full_src, s) or "" for s in body)
            for tok in banned_tokens:
                if tok in code_seg:
                    violations.append(f"{node.name}: contains {tok}")

    assert not violations, f"server/daemon_server.py 退役符号仍含 SQLite 权威残留: {violations}"
