"""SRV-007 迁移验收：daemon protocol rollback Python authority → Rust daemon。

覆盖 task `T-1787323461012-d8597160` step[2] fixture_negative_matrix：
["success", "invalid", "authority", "unavailable", "restart"]。

设计要点（SRV-007 = narrow rollback 探测端口，route B，单符号）：
- Python `server/daemon_protocol.py::_is_rust_protocol_rolled_back` 原本短连接
  open 本地 SQLite 查 rollback_config（feature=rust_daemon_protocol）。
- SRV-007 后退化为纯 daemon RPC 薄客户端，经
  `mcp.daemon_protocol.is_rust_protocol_rolled_back` 查询，不再 `import sqlite3`、
  不再导入 callwarden.config.DB_PATH。
- 只读 authority 读：daemon 不可用时 fail-soft 视为未回滚（返回 False），
  与 SRV-003 is_rust_backup_rolled_back 的 rollback 探测语义一致，绝不回退本地 SQLite。
- 协议自依赖环：daemon_protocol 是 RPC 帧传输层本身（UnixDaemonRpcClient 经
  send_message/recv_message/parse_response 传输），缓存过期瞬间存在
  _is_rust_protocol_rolled_back → RPC → send_message → _is_rust_protocol_rolled_back
  递归；SRV-007 引入 _ROLLBACK_QUERY_STATE in-flight 重入守卫（in-flight 时
  视为未回滚且不写缓存），本测试单独验证。
- 本测试用内存态 FakeProtocolDaemon 模拟 daemon 的 daemon_protocol_handlers 行为，
  不依赖真实 daemon 进程，也不触碰本地 SQLite 文件。
"""

import ast

import pytest

from callwarden.server.daemon_client import DaemonUnavailableError
from callwarden.server.daemon_protocol import _is_rust_protocol_rolled_back


class FakeDaemonRpcError(Exception):
    """模拟 daemon 端 DaemonRpcError（带稳定 error code）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class FakeProtocolDaemon:
    """内存态 daemon rollback 权威（对齐 Rust `daemon_protocol_handlers` 语义）。"""

    def __init__(self, rolled_back: bool = False):
        self.available: bool = True
        self.rolled_back = rolled_back
        self.response_override = None  # 非 dict 响应注入（invalid 段）
        self.calls: list = []
        self.on_call = None  # 可选回调：模拟 RPC 传输层重入

    def __call__(self, method: str, params: dict):
        self.calls.append((method, params))
        if not self.available:
            raise DaemonUnavailableError("daemon 不可用（测试模拟）")
        if method == "mcp.daemon_protocol.is_rust_protocol_rolled_back":
            if self.on_call is not None:
                self.on_call()
            if self.response_override is not None:
                return self.response_override
            return {"rolled_back": self.rolled_back}
        raise FakeDaemonRpcError("method_not_found", f"未知方法 {method}")


@pytest.fixture
def fake_daemon(monkeypatch):
    """每个测试安装一个干净的内存态 daemon 薄客户端 + 复位 60s 缓存与重入状态。"""
    import callwarden.server.daemon_protocol as dp

    daemon = FakeProtocolDaemon()
    monkeypatch.setattr("callwarden.server.daemon_protocol._call_daemon_rpc", daemon)
    # 复位模块级 60s 缓存与重入守卫，避免跨测试污染
    monkeypatch.setattr(dp, "_ROLLBACK_CACHE", {"ts": 0.0, "value": False})
    monkeypatch.setattr(dp, "_ROLLBACK_QUERY_STATE", {"in_flight": False})
    return daemon


# ============================================================
# 1) success
# ============================================================


def test_success_not_rolled_back(fake_daemon):
    # 默认 daemon 报告未回滚 → Rust 短路路径保持启用
    assert _is_rust_protocol_rolled_back() is False


def test_success_rolled_back_true(fake_daemon):
    fake_daemon.rolled_back = True
    assert _is_rust_protocol_rolled_back() is True


def test_success_cache_suppresses_second_rpc(fake_daemon):
    # 60s 缓存保留：热路径（帧收发）第二次调用不再发起 RPC
    assert _is_rust_protocol_rolled_back() is False
    assert _is_rust_protocol_rolled_back() is False
    assert len(fake_daemon.calls) == 1


# ============================================================
# 2) invalid（daemon 返回畸形响应 → isinstance 校验 → 视为未回滚）
# ============================================================


def test_invalid_response_non_dict(fake_daemon):
    fake_daemon.response_override = "rolled_back: true"  # 非 dict
    assert _is_rust_protocol_rolled_back() is False


def test_invalid_response_missing_key(fake_daemon):
    fake_daemon.response_override = {"flag": 1}  # 缺 rolled_back 键
    assert _is_rust_protocol_rolled_back() is False


def test_invalid_response_truthy_int(fake_daemon):
    # Rust handler 语义 rolled_back 为 bool；Python 侧 bool() 兼容 1/0
    fake_daemon.response_override = {"rolled_back": 1}
    assert _is_rust_protocol_rolled_back() is True


# ============================================================
# 3) authority（rollback_config 权威在 daemon，Python 不再本地持有）
# ============================================================


def test_authority_rollback_config_owned_by_daemon(fake_daemon):
    fake_daemon.rolled_back = True
    assert _is_rust_protocol_rolled_back() is True
    # 权威经且仅经 daemon RPC 读取，方法名逐字对齐 step0 dispatch 分支
    assert fake_daemon.calls == [
        ("mcp.daemon_protocol.is_rust_protocol_rolled_back", {})
    ]


def test_authority_no_db_path_param(fake_daemon):
    # 薄客户端不传 db_path：权威库定位是 daemon 内部职责（handler 缺省用户级单库）
    _is_rust_protocol_rolled_back()
    assert fake_daemon.calls[0][1] == {}


# ============================================================
# 4) unavailable（fail-soft 视为未回滚，绝不回退本地 SQLite）
# ============================================================


def test_unavailable_fail_soft_false(fake_daemon):
    fake_daemon.available = False
    # 只读 authority 读：daemon 不可用时 fail-soft 视为未回滚（不抛异常）
    assert _is_rust_protocol_rolled_back() is False


# ============================================================
# 5) restart（首次不可用 → 恢复后读到正确回滚位）
# ============================================================


def test_restart_rolled_back_reads_after_recover(fake_daemon):
    import callwarden.server.daemon_protocol as dp

    fake_daemon.available = False
    assert _is_rust_protocol_rolled_back() is False  # fail-soft，并进入 60s 缓存
    fake_daemon.available = True
    fake_daemon.rolled_back = True
    # 失效 60s 缓存以模拟"恢复后重新探测 daemon 权威"
    dp._ROLLBACK_CACHE["ts"] = 0.0
    assert _is_rust_protocol_rolled_back() is True


# ============================================================
# 6) 重入守卫（SRV-007 特有：协议传输层自依赖环）
# ============================================================


def test_reentry_guard_returns_false_without_recursion(fake_daemon):
    """模拟真实自依赖环：RPC 查询经本模块帧收发传输，send_message 在帧编码前
    重入 _is_rust_protocol_rolled_back → in-flight 守卫返回 False，无递归。"""
    import callwarden.server.daemon_protocol as dp

    reentrant_result = []

    def simulate_frame_transport_reentry():
        # 传输层重入：此时外层查询 in-flight，守卫必须短路返回 False
        reentrant_result.append(_is_rust_protocol_rolled_back())

    fake_daemon.on_call = simulate_frame_transport_reentry
    assert _is_rust_protocol_rolled_back() is False
    assert reentrant_result == [False]  # 重入被守卫短路，未无限递归
    assert len(fake_daemon.calls) == 1  # 重入未触发第二次 RPC
    assert dp._ROLLBACK_QUERY_STATE["in_flight"] is False  # finally 复位


def test_reentry_guard_preset_in_flight_skips_rpc(fake_daemon):
    """预设 in-flight（缓存已过期）→ 直接视为未回滚且不发起 RPC。"""
    import callwarden.server.daemon_protocol as dp

    dp._ROLLBACK_QUERY_STATE["in_flight"] = True
    try:
        assert _is_rust_protocol_rolled_back() is False
    finally:
        dp._ROLLBACK_QUERY_STATE["in_flight"] = False
    assert fake_daemon.calls == []
    # 守卫短路结果不写缓存（保持 ts=0，下次仍会真实探测）
    assert dp._ROLLBACK_CACHE["ts"] == 0.0


# ============================================================
# 零权威证据：AST 扫描（已迁移 helper 不再含 SQLite 权威残留）
# ============================================================


def test_no_sqlite_authority_in_source():
    # 直接取已加载模块的 __file__，确保扫描的是当前生效（worktree）的迁移后源码
    import callwarden.server.daemon_protocol as dp

    src = dp.__file__
    with open(src, "r", encoding="utf-8") as f:
        full_src = f.read()
    tree = ast.parse(full_src)

    banned_imports = {"sqlite3"}
    banned_tokens = {"sqlite3", "DB_PATH", "SELECT", "PRAGMA", "rollback_config"}
    target_funcs = {"_is_rust_protocol_rolled_back"}

    violations = []
    # 模块级不得再引入 sqlite3
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                if n.name in banned_imports:
                    violations.append("import sqlite3")
        elif isinstance(node, ast.ImportFrom):
            if node.module in banned_imports:
                violations.append("from sqlite3 import")

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

    assert not violations, f"server/daemon_protocol.py 仍含 SQLite 权威残留: {violations}"
