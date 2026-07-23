"""Phase 8.5 批次11（P0 运维 RPC 授权）：daemon admin RPC 授权测试。

任务：T-1784597116187-1c028c20
规范：docs/design/feature-matrix-code-audit-2026-07-20.md §P0（运维 RPC 缺少管理员授权）

测试覆盖：
1. ADMIN_ONLY_METHODS 常量与 Rust 端 dispatch.rs 完全对齐（14 个方法，P0-2 整改后加入 mount.list）
2. _is_admin_peer 行为矩阵（uid=0 / uid=os.getuid() / uid in admin_uids / 其他 uid）
3. dispatch fail-closed admin 校验（admin 通过 / 非 admin 拒绝 / 只读方法不被拦截）
4. 14 个 admin 方法的授权矩阵（admin 通过 + 非 admin 抛 permission_denied）

与 Rust 端对齐：
- rust_ext/src/daemon/dispatch.rs L545-564 ADMIN_ONLY_METHODS
- rust_ext/src/daemon/dispatch.rs L580-583 is_admin(peer) = peer.uid == 0 || peer.uid == current_daemon_uid()
- rust_ext/src/daemon/dispatch.rs L593-599 dispatch_inner fail-closed 校验
"""

import os
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# Fixture：构造最小 EnterpriseDaemonService 实例
# ============================================================


@pytest.fixture
def daemon_service(tmp_path):
    """构造最小 EnterpriseDaemonService 实例（无后台任务、无 schema 迁移）。

    用于测试 dispatch() 中的 admin 校验逻辑。admin 校验在方法分支之前，
    非 admin 调用 admin 方法会在进入具体 handler 前抛 permission_denied，
    所以不需要完整实现 handler 逻辑。
    """
    from callwarden.server.daemon_server import EnterpriseDaemonService
    from callwarden.server.daemon_config import DaemonConfig

    registry_db = str(tmp_path / "registry.db")
    data_root = str(tmp_path / "data")
    os.makedirs(data_root, exist_ok=True)
    os.makedirs(os.path.dirname(registry_db), exist_ok=True)

    # 用默认 config（admin_uids=[0]）
    cfg = DaemonConfig.load_from_dict({
        "data_root": data_root,
        "socket_path": str(tmp_path / "daemon.sock"),
    })

    # 初始化 registry schema（init_daemon_schema）
    conn = sqlite3.connect(registry_db)
    conn.row_factory = sqlite3.Row
    from callwarden.db.db_daemon import init_daemon_schema
    init_daemon_schema(conn)
    conn.close()

    # MagicMock snapshot_service 避免触发实际服务初始化
    snapshot_service = MagicMock()

    svc = EnterpriseDaemonService(
        registry_db=registry_db,
        snapshot_service=snapshot_service,
        data_root=data_root,
        config=cfg,
        run_startup_migrations=False,
        start_background_tasks=False,
    )
    yield svc


def _make_peer(uid: int) -> dict:
    """构造 peer credential 字典。"""
    return {"pid": os.getpid(), "uid": uid, "gid": 0}


# ============================================================
# 1. ADMIN_ONLY_METHODS 常量与 Rust 端对齐
# ============================================================


class TestBatch11AdminOnlyMethodsConstant:
    """验证 ADMIN_ONLY_METHODS 常量与 Rust 端 dispatch.rs L545-564 完全对齐。"""

    def test_constant_is_frozenset(self):
        """ADMIN_ONLY_METHODS 必须是 frozenset（不可变，避免运行时被篡改）。"""
        from callwarden.server.daemon_server import ADMIN_ONLY_METHODS
        assert isinstance(ADMIN_ONLY_METHODS, frozenset)

    def test_constant_contains_exactly_14_methods(self):
        """与 Rust 端对齐：恰好 14 个 admin-only 方法（P0-2 整改后加入 mount.list）。"""
        from callwarden.server.daemon_server import ADMIN_ONLY_METHODS
        assert len(ADMIN_ONLY_METHODS) == 14

    def test_constant_contains_all_expected_methods(self):
        """14 个方法必须与 Rust 端 dispatch.rs ADMIN_ONLY_METHODS 完全一致。"""
        from callwarden.server.daemon_server import ADMIN_ONLY_METHODS
        expected = {
            # 数据库备份 / 还原
            "backup", "restore",
            # 资源回收
            "gc.cas", "gc.snapshots", "snapshot.evict",
            # Mount Mapping 写操作
            "mount.register", "mount.delete",
            # Mount Mapping 读操作（P0-2 整改：暴露全局 host_path，改 admin-only）
            "mount.list",
            # Toolchain 配置变更
            "toolchain.register", "toolchain.delete", "toolchain.bind",
            # Build Context 变更
            "build_context.register", "build_context.set_active", "build_context.delete",
        }
        assert ADMIN_ONLY_METHODS == expected

    def test_readonly_methods_not_in_admin_only(self):
        """只读方法不在 ADMIN_ONLY_METHODS 中（任意已连接 peer 可调用）。"""
        from callwarden.server.daemon_server import ADMIN_ONLY_METHODS
        readonly_methods = [
            "ping", "health", "schema.version",
            "workspace.list", "workspace.status", "workspace.connect",
            "workspace.file.refresh", "workspace.recover",
            "metrics.snapshot", "metrics.prometheus",
            "toolchain.list", "toolchain.get", "toolchain.resolve",
            "build_context.list",
            "query.stats", "query.symbol", "query.search",
            "query.callers", "query.callees",
            "resolved_edges.get", "resolved_edges.count",
        ]
        for m in readonly_methods:
            assert m not in ADMIN_ONLY_METHODS, f"只读方法 {m} 不应在 ADMIN_ONLY_METHODS 中"


# ============================================================
# 2. _is_admin_peer 行为矩阵
# ============================================================


class TestBatch11IsAdminPeer:
    """测试 _is_admin_peer 方法的行为矩阵。"""

    def test_uid_zero_is_admin(self, daemon_service):
        """uid=0（root）始终是 admin（与 Rust 端 peer.uid == 0 对齐）。"""
        assert daemon_service._is_admin_peer(0) is True

    def test_daemon_process_uid_is_admin(self, daemon_service):
        """daemon 进程自己的 uid 是 admin（与 Rust 端 current_daemon_uid() 对齐）。"""
        daemon_uid = os.getuid() if hasattr(os, "getuid") else 0
        assert daemon_service._is_admin_peer(daemon_uid) is True

    def test_configured_admin_uid_is_admin(self, tmp_path):
        """admin_uids 配置中的 uid 是 admin（Python 端配置扩展）。"""
        from callwarden.server.daemon_server import EnterpriseDaemonService
        from callwarden.server.daemon_config import DaemonConfig
        from callwarden.db.db_daemon import init_daemon_schema

        registry_db = str(tmp_path / "sub" / "registry.db")
        data_root = str(tmp_path / "sub")  # 与 registry_db 同目录避免触发 config 重建
        os.makedirs(data_root, exist_ok=True)

        # 配置 admin_uids=[1000, 2000]（不包含 0 之外的真实 uid）
        cfg = DaemonConfig.load_from_dict({
            "data_root": data_root,
            "socket_path": str(tmp_path / "daemon.sock"),
            "security": {"admin_uids": [1000, 2000]},
        })
        conn = sqlite3.connect(registry_db)
        conn.row_factory = sqlite3.Row
        init_daemon_schema(conn)
        conn.close()

        svc = EnterpriseDaemonService(
            registry_db=registry_db,
            snapshot_service=MagicMock(),
            data_root=data_root,
            config=cfg,
            run_startup_migrations=False,
            start_background_tasks=False,
        )
        # 配置中的 uid 是 admin
        assert svc._is_admin_peer(1000) is True
        assert svc._is_admin_peer(2000) is True
        # 未配置的非 root uid 不是 admin（除非等于 daemon 进程 uid）
        daemon_uid = os.getuid() if hasattr(os, "getuid") else 0
        if daemon_uid not in (0, 1000, 2000):
            assert svc._is_admin_peer(9999) is False

    def test_random_uid_not_admin(self, daemon_service):
        """随机非 root uid 不是 admin（默认 admin_uids=[0]）。"""
        daemon_uid = os.getuid() if hasattr(os, "getuid") else 0
        if daemon_uid == 0:
            # daemon 以 root 启动时，选一个非 root uid 测试
            assert daemon_service._is_admin_peer(9999) is False
        else:
            # daemon 非 root 启动时，选一个既不是 root 也不是 daemon uid 的值
            candidate = daemon_uid + 1000
            assert daemon_service._is_admin_peer(candidate) is False


# ============================================================
# 3. dispatch fail-closed admin 校验
# ============================================================


class TestBatch11DispatchAdminEnforcement:
    """测试 dispatch() 中的 fail-closed admin 校验。"""

    def test_admin_uid_can_call_admin_method(self, daemon_service, tmp_path):
        """admin uid 调用 admin 方法不会抛 permission_denied（进入具体 handler）。"""
        peer = _make_peer(uid=0)
        # backup 需要 output_path 参数；admin uid 应该能进入 handler
        # （handler 内可能因其他原因失败，但不应该是 permission_denied）
        try:
            daemon_service.dispatch(
                peer=peer,
                method="backup",
                params={"output_path": str(tmp_path / "backup.db")},
            )
        except Exception as e:
            # admin 通过校验后，handler 内可能因 VACUUM INTO 等失败
            # 关键是不能是 permission_denied
            assert not (isinstance(e, __import__("callwarden.server.daemon_server", fromlist=["DaemonRpcError"]).DaemonRpcError) and e.code == "permission_denied"), \
                f"admin uid 不应被 permission_denied 拒绝，但收到: {e}"

    def test_non_admin_uid_rejected_for_admin_method(self, daemon_service):
        """非 admin uid 调用 admin 方法抛 permission_denied（fail-closed）。"""
        daemon_uid = os.getuid() if hasattr(os, "getuid") else 0
        if daemon_uid == 0:
            non_admin_uid = 9999
        else:
            non_admin_uid = daemon_uid + 1000
        peer = _make_peer(uid=non_admin_uid)

        from callwarden.server.daemon_server import DaemonRpcError
        with pytest.raises(DaemonRpcError) as exc_info:
            daemon_service.dispatch(
                peer=peer,
                method="backup",
                params={"output_path": "/tmp/test.db"},
            )
        assert exc_info.value.code == "permission_denied"
        assert "backup" in exc_info.value.message
        assert str(non_admin_uid) in exc_info.value.message

    def test_readonly_method_not_blocked_for_non_admin(self, daemon_service):
        """只读方法不被 admin 校验拦截（非 admin uid 也能调用）。"""
        daemon_uid = os.getuid() if hasattr(os, "getuid") else 0
        if daemon_uid == 0:
            non_admin_uid = 9999
        else:
            non_admin_uid = daemon_uid + 1000
        peer = _make_peer(uid=non_admin_uid)

        # ping 是只读方法，不应被拦截
        result = daemon_service.dispatch(
            peer=peer,
            method="ping",
            params={},
        )
        assert result["status"] == "ok"
        assert result["peer_uid"] == non_admin_uid

    def test_health_not_blocked_for_non_admin(self, daemon_service):
        """health 是只读方法，非 admin 也能调用。"""
        daemon_uid = os.getuid() if hasattr(os, "getuid") else 0
        non_admin_uid = daemon_uid + 1000 if daemon_uid == 0 else daemon_uid + 1000
        peer = _make_peer(uid=non_admin_uid)
        result = daemon_service.dispatch(
            peer=peer,
            method="health",
            params={},
        )
        # health 返回 dict（可能包含 status 字段）
        assert isinstance(result, dict)

    def test_schema_version_not_blocked_for_non_admin(self, daemon_service):
        """schema.version 是只读方法，非 admin 也能调用（不应被 admin 校验拦截）。

        注：handler 内部可能因 schema_meta 表未初始化而失败（fixture 未建该表），
        但关键是错误不是 permission_denied（说明 admin 校验放行了）。
        """
        from callwarden.server.daemon_server import DaemonRpcError
        daemon_uid = os.getuid() if hasattr(os, "getuid") else 0
        non_admin_uid = daemon_uid + 1000 if daemon_uid == 0 else daemon_uid + 1000
        peer = _make_peer(uid=non_admin_uid)
        try:
            daemon_service.dispatch(
                peer=peer,
                method="schema.version",
                params={},
            )
        except DaemonRpcError as e:
            # DaemonRpcError 中关键是不是 permission_denied
            assert e.code != "permission_denied", \
                f"schema.version 不应被 admin 校验拦截，但收到: {e.code}"
        except Exception:
            # handler 内部失败（如 sqlite3.OperationalError: no such table）可接受，
            # 关键是没被 admin 校验的 permission_denied 拦截
            pass


# ============================================================
# 4. 14 个 admin 方法授权矩阵
# ============================================================


class TestBatch11AdminMethodMatrix:
    """14 个 admin 方法的授权矩阵：admin 通过 / 非 admin 抛 permission_denied。

    fail-closed 校验在 dispatch 顶部，非 admin 调用会在进入具体 handler 前
    抛 permission_denied，所以不需要完整 handler 实现。
    P0-2 整改（2026-07-21）后由 13 个增至 14 个（新增 mount.list）。
    """

    ADMIN_METHODS = [
        "backup",
        "restore",
        "gc.cas",
        "gc.snapshots",
        "snapshot.evict",
        "mount.register",
        "mount.delete",
        "mount.list",
        "toolchain.register",
        "toolchain.delete",
        "toolchain.bind",
        "build_context.register",
        "build_context.set_active",
        "build_context.delete",
    ]

    def test_all_14_methods_are_in_admin_only_constant(self):
        """矩阵中的 14 个方法都在 ADMIN_ONLY_METHODS 中。"""
        from callwarden.server.daemon_server import ADMIN_ONLY_METHODS
        for method in self.ADMIN_METHODS:
            assert method in ADMIN_ONLY_METHODS, f"{method} 不在 ADMIN_ONLY_METHODS 中"

    def test_admin_uid_passes_all_14_methods(self, daemon_service):
        """admin uid 调用所有 14 个 admin 方法都不应被 permission_denied 拒绝。"""
        from callwarden.server.daemon_server import DaemonRpcError
        peer = _make_peer(uid=0)
        for method in self.ADMIN_METHODS:
            try:
                daemon_service.dispatch(
                    peer=peer,
                    method=method,
                    params={},
                    received_fds=None,
                )
            except DaemonRpcError as e:
                # 不应该收到 permission_denied（可能是 invalid_params / method_not_found 等）
                assert e.code != "permission_denied", \
                    f"admin uid 调用 {method} 不应被 permission_denied 拒绝"
            except Exception:
                # 其他异常（handler 内部错误）可接受，关键是没被 permission_denied 拦截
                pass

    def test_non_admin_uid_rejected_for_all_14_methods(self, daemon_service):
        """非 admin uid 调用所有 14 个 admin 方法都抛 permission_denied。"""
        from callwarden.server.daemon_server import DaemonRpcError
        daemon_uid = os.getuid() if hasattr(os, "getuid") else 0
        non_admin_uid = daemon_uid + 1000 if daemon_uid == 0 else daemon_uid + 1000
        peer = _make_peer(uid=non_admin_uid)

        for method in self.ADMIN_METHODS:
            with pytest.raises(DaemonRpcError) as exc_info:
                daemon_service.dispatch(
                    peer=peer,
                    method=method,
                    params={},
                    received_fds=None,
                )
            assert exc_info.value.code == "permission_denied", \
                f"{method} 应该抛 permission_denied，但收到: {exc_info.value.code}"
            assert method in exc_info.value.message
            assert str(non_admin_uid) in exc_info.value.message


# ============================================================
# 5. Rust/Python 对齐验证（静态检查源文件）
# ============================================================


class TestBatch11RustPythonAlignment:
    """验证 Python 端 ADMIN_ONLY_METHODS 与 Rust 端 dispatch.rs 对齐。"""

    def test_python_constant_matches_rust_source(self):
        """从 rust_ext/src/daemon/dispatch.rs 读取 ADMIN_ONLY_METHODS 列表，
        验证与 Python 端常量完全一致。
        """
        from callwarden.server.daemon_server import ADMIN_ONLY_METHODS

        rust_dispatch = ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs"
        if not rust_dispatch.is_file():
            pytest.skip("rust_ext/src/daemon/dispatch.rs 不存在（Rust 端未实现）")

        content = rust_dispatch.read_text(encoding="utf-8")
        # 提取 ADMIN_ONLY_METHODS 块中的字符串字面量
        # 块格式：const ADMIN_ONLY_METHODS: &[&str] = &[ ... "xxx", ... ];
        import re
        m = re.search(
            r'ADMIN_ONLY_METHODS[^=]*=\s*&\[([^\]]+)\]',
            content,
            re.DOTALL,
        )
        assert m is not None, "Rust 端未找到 ADMIN_ONLY_METHODS 定义"
        block = m.group(1)
        # 提取所有 "method.name" 字符串
        rust_methods = set(re.findall(r'"([a-z_.]+)"', block))

        assert rust_methods == set(ADMIN_ONLY_METHODS), \
            f"Python/Rust ADMIN_ONLY_METHODS 不一致:\n" \
            f"  Rust only: {rust_methods - set(ADMIN_ONLY_METHODS)}\n" \
            f"  Python only: {set(ADMIN_ONLY_METHODS) - rust_methods}"

    def test_rust_dispatch_has_fail_closed_check(self):
        """验证 Rust 端 dispatch_inner 顶部有 fail-closed admin 校验。"""
        rust_dispatch = ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs"
        if not rust_dispatch.is_file():
            pytest.skip("rust_ext/src/daemon/dispatch.rs 不存在")

        content = rust_dispatch.read_text(encoding="utf-8")
        # Rust 端应该有 ADMIN_ONLY_METHODS.contains(&method) && !is_admin(peer) 模式
        assert "ADMIN_ONLY_METHODS.contains" in content, \
            "Rust 端 dispatch.rs 缺少 ADMIN_ONLY_METHODS.contains 校验"
        assert "permission_denied" in content, \
            "Rust 端 dispatch.rs 缺少 permission_denied 错误码"

    def test_python_dispatch_has_fail_closed_check(self):
        """验证 Python 端 dispatch() 顶部有 fail-closed admin 校验。"""
        python_dispatch = ROOT / "server" / "daemon_server.py"
        content = python_dispatch.read_text(encoding="utf-8")
        assert "ADMIN_ONLY_METHODS" in content
        assert "_is_admin_peer" in content
        assert "permission_denied" in content
        # fail-closed：未授权直接 raise，不进入 handler
        assert "if method in ADMIN_ONLY_METHODS and not self._is_admin_peer" in content


# ============================================================
# 5. P0-2 整改（2026-07-21）：workspace_id 级 ACL 测试
# ============================================================


class TestP02WorkspaceIdAcl:
    """P0-2 整改验证：toolchain.resolve / build_context.list / resolved_edges.*
    必须按 workspace_id 校验 owner_uid，跨 UID 调用应抛 workspace_forbidden。

    复审报告 §8.2 要求：所有含 workspace ID 的 RPC 统一调用
    `owned_workspace(peer_uid, workspace_instance_id)`；list API 按 ACL 过滤。
    """

    def _register_workspace(self, daemon_service, owner_uid: int, tmp_path):
        """注册一个 workspace，返回 (workspace_id 数字, workspace_instance_id 字符串)。"""
        owner_peer = _make_peer(uid=owner_uid)  # 用实际 owner 注册，保证 ACL 一致
        workspace = daemon_service.dispatch(owner_peer, "workspace.register", {
            "client_view_root": str(tmp_path),
            "host_real_root": str(tmp_path),
        })
        return int(workspace["workspace_id"]), workspace["workspace_instance_id"]

    def test_toolchain_resolve_rejects_cross_uid(self, daemon_service, tmp_path):
        """toolchain.resolve 跨 UID 调用应抛 workspace_forbidden。"""
        owner_uid = os.getuid() if hasattr(os, "getuid") else 0
        ws_id, _ = self._register_workspace(daemon_service, owner_uid, tmp_path)

        other_peer = _make_peer(uid=owner_uid + 1000)
        from callwarden.server.daemon_server import DaemonRpcError
        with pytest.raises(DaemonRpcError, match="workspace 不属于当前 UID") as exc:
            daemon_service.dispatch(other_peer, "toolchain.resolve", {
                "workspace_id": ws_id,
            })
        assert exc.value.code == "workspace_forbidden"

    def test_build_context_list_rejects_cross_uid(self, daemon_service, tmp_path):
        """build_context.list 跨 UID 调用应抛 workspace_forbidden。"""
        owner_uid = os.getuid() if hasattr(os, "getuid") else 0
        ws_id, _ = self._register_workspace(daemon_service, owner_uid, tmp_path)

        other_peer = _make_peer(uid=owner_uid + 1000)
        from callwarden.server.daemon_server import DaemonRpcError
        with pytest.raises(DaemonRpcError, match="workspace 不属于当前 UID") as exc:
            daemon_service.dispatch(other_peer, "build_context.list", {
                "workspace_id": ws_id,
            })
        assert exc.value.code == "workspace_forbidden"

    def test_resolved_edges_store_rejects_cross_uid(self, daemon_service, tmp_path):
        """resolved_edges.store 跨 UID 写应抛 workspace_forbidden（最严重缺口）。"""
        owner_uid = os.getuid() if hasattr(os, "getuid") else 0
        ws_id, _ = self._register_workspace(daemon_service, owner_uid, tmp_path)

        other_peer = _make_peer(uid=owner_uid + 1000)
        from callwarden.server.daemon_server import DaemonRpcError
        with pytest.raises(DaemonRpcError, match="workspace 不属于当前 UID") as exc:
            daemon_service.dispatch(other_peer, "resolved_edges.store", {
                "workspace_id": ws_id,
                "build_context_hash": "fake_hash",
                "edges": [
                    {"caller_symbol_id": 1, "callee_symbol_id": 2,
                     "callee_name": "foo", "callee_file": "bar.py",
                     "call_line": 10, "resolution_method": "exact"},
                ],
            })
        assert exc.value.code == "workspace_forbidden"

    def test_resolved_edges_get_rejects_cross_uid(self, daemon_service, tmp_path):
        """resolved_edges.get 跨 UID 读应抛 workspace_forbidden。"""
        owner_uid = os.getuid() if hasattr(os, "getuid") else 0
        ws_id, _ = self._register_workspace(daemon_service, owner_uid, tmp_path)

        other_peer = _make_peer(uid=owner_uid + 1000)
        from callwarden.server.daemon_server import DaemonRpcError
        with pytest.raises(DaemonRpcError, match="workspace 不属于当前 UID") as exc:
            daemon_service.dispatch(other_peer, "resolved_edges.get", {
                "workspace_id": ws_id,
                "build_context_hash": "fake_hash",
            })
        assert exc.value.code == "workspace_forbidden"

    def test_resolved_edges_count_rejects_cross_uid(self, daemon_service, tmp_path):
        """resolved_edges.count 跨 UID 读应抛 workspace_forbidden。"""
        owner_uid = os.getuid() if hasattr(os, "getuid") else 0
        ws_id, _ = self._register_workspace(daemon_service, owner_uid, tmp_path)

        other_peer = _make_peer(uid=owner_uid + 1000)
        from callwarden.server.daemon_server import DaemonRpcError
        with pytest.raises(DaemonRpcError, match="workspace 不属于当前 UID") as exc:
            daemon_service.dispatch(other_peer, "resolved_edges.count", {
                "workspace_id": ws_id,
                "build_context_hash": "fake_hash",
            })
        assert exc.value.code == "workspace_forbidden"

    def test_resolved_edges_store_rejects_invalid_symbol_id(self, daemon_service, tmp_path):
        """resolved_edges.store 校验 edge 字段合法性（symbol_id 必须 > 0）。"""
        owner_uid = os.getuid() if hasattr(os, "getuid") else 0
        ws_id, _ = self._register_workspace(daemon_service, owner_uid, tmp_path)

        owner_peer = _make_peer(uid=owner_uid)
        from callwarden.server.daemon_server import DaemonRpcError
        # caller_symbol_id = 0 应被拒绝
        with pytest.raises(DaemonRpcError, match="caller_symbol_id 必须 > 0"):
            daemon_service.dispatch(owner_peer, "resolved_edges.store", {
                "workspace_id": ws_id,
                "build_context_hash": "fake_hash",
                "edges": [
                    {"caller_symbol_id": 0, "callee_symbol_id": 2,
                     "callee_name": "foo", "callee_file": "bar.py",
                     "call_line": 10, "resolution_method": "exact"},
                ],
            })

    def test_resolved_edges_store_rejects_missing_symbol_id(self, daemon_service, tmp_path):
        """resolved_edges.store 校验 edge 字段缺失。"""
        owner_uid = os.getuid() if hasattr(os, "getuid") else 0
        ws_id, _ = self._register_workspace(daemon_service, owner_uid, tmp_path)

        owner_peer = _make_peer(uid=owner_uid)
        from callwarden.server.daemon_server import DaemonRpcError
        # caller_symbol_id 缺失
        with pytest.raises(DaemonRpcError, match="caller_symbol_id 缺失"):
            daemon_service.dispatch(owner_peer, "resolved_edges.store", {
                "workspace_id": ws_id,
                "build_context_hash": "fake_hash",
                "edges": [
                    {"callee_symbol_id": 2,
                     "callee_name": "foo", "callee_file": "bar.py",
                     "call_line": 10, "resolution_method": "exact"},
                ],
            })

    def test_mount_list_rejects_non_admin(self, daemon_service, tmp_path):
        """mount.list 改为 admin-only 后，非 admin 调用应抛 permission_denied。"""
        daemon_uid = os.getuid() if hasattr(os, "getuid") else 0
        non_admin_uid = daemon_uid + 1000 if daemon_uid == 0 else daemon_uid + 1000
        other_peer = _make_peer(uid=non_admin_uid)

        from callwarden.server.daemon_server import DaemonRpcError
        with pytest.raises(DaemonRpcError, match="管理员权限") as exc:
            daemon_service.dispatch(other_peer, "mount.list", {})
        assert exc.value.code == "permission_denied"

    def test_mount_list_passes_admin(self, daemon_service, tmp_path):
        """mount.list 对 admin（root）应放行（不抛 permission_denied）。"""
        admin_peer = _make_peer(uid=0)
        from callwarden.server.daemon_server import DaemonRpcError
        try:
            daemon_service.dispatch(admin_peer, "mount.list", {})
        except DaemonRpcError as e:
            # 不应该收到 permission_denied（可能是空列表返回）
            assert e.code != "permission_denied", \
                f"admin 调用 mount.list 不应被 permission_denied 拒绝（实际 {e.code}）"
