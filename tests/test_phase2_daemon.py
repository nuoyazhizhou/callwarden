"""Phase 2 daemon skeleton 单元测试。

测试范围：
- workspace registry schema 初始化
- workspace 注册/查询/状态管理
- container mount mapping 路径解析
- daemon mode 检测
"""

import os
import tempfile
import sqlite3
import pytest

from callwarden.db.db_daemon import (
    init_daemon_schema,
    register_workspace,
    list_workspaces,
    get_workspace_status,
    update_workspace_status,
    register_mount_mapping,
    list_mount_mappings,
    delete_mount_mapping,
)
from callwarden.config import (
    resolve_container_path,
    get_daemon_mode,
    is_daemon_available,
    is_daemon_required,
)


@pytest.fixture
def tmp_db():
    """临时 registry DB。"""
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = f.name
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    init_daemon_schema(conn)
    yield conn
    conn.close()
    os.unlink(db_path)


# ── workspace registry schema 测试 ──

class TestWorkspaceRegistry:

    def test_init_schema_creates_tables(self, tmp_db):
        """schema 初始化创建所有表。"""
        tables = tmp_db.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()
        table_names = [t["name"] for t in tables]
        assert "daemon_workspaces" in table_names
        assert "container_mount_mappings" in table_names
        assert "daemon_state" in table_names

    def test_register_workspace_returns_info(self, tmp_db):
        """注册 workspace 返回完整信息。"""
        ws = register_workspace(
            tmp_db, owner_uid=1000, client_view_root="/home/user1/work",
            host_real_root="/data/work", git_remote_url="https://github.com/test/repo.git",
            git_head_commit_sha="abc123"
        )
        assert ws["owner_uid"] == 1000
        assert ws["client_view_root"] == "/home/user1/work"
        assert ws["host_real_root"] == "/data/work"
        assert ws["status"] == "active"
        assert ws["workspace_instance_id"]  # 非空
        assert ws["snapshot_id"]  # 有 git info 时非空

    def test_register_workspace_without_git(self, tmp_db):
        """无 git 信息的 workspace 也能注册。"""
        ws = register_workspace(
            tmp_db, owner_uid=1000, client_view_root="/home/user1/work",
            host_real_root="/data/work"
        )
        assert ws["snapshot_id"] is None or ws["snapshot_id"] == ""

    def test_list_workspaces_all(self, tmp_db):
        """列出所有 workspace。"""
        register_workspace(tmp_db, 1000, "/home/u1/work", "/data/u1/work")
        register_workspace(tmp_db, 2000, "/home/u2/work", "/data/u2/work")
        result = list_workspaces(tmp_db)
        assert len(result) == 2

    def test_list_workspaces_by_uid(self, tmp_db):
        """按 UID 过滤 workspace。"""
        register_workspace(tmp_db, 1000, "/home/u1/work", "/data/u1/work")
        register_workspace(tmp_db, 2000, "/home/u2/work", "/data/u2/work")
        result = list_workspaces(tmp_db, owner_uid=1000)
        assert len(result) == 1
        assert result[0]["owner_uid"] == 1000

    def test_get_workspace_status(self, tmp_db):
        """获取 workspace 状态。"""
        ws = register_workspace(tmp_db, 1000, "/home/u1/work", "/data/u1/work")
        result = get_workspace_status(tmp_db, ws["workspace_instance_id"])
        assert result is not None
        assert result["status"] == "active"

    def test_get_workspace_status_not_found(self, tmp_db):
        """不存在的 workspace 返回 None。"""
        result = get_workspace_status(tmp_db, "nonexistent")
        assert result is None

    def test_update_workspace_status(self, tmp_db):
        """更新 workspace 状态。"""
        ws = register_workspace(tmp_db, 1000, "/home/u1/work", "/data/u1/work")
        update_workspace_status(tmp_db, ws["workspace_instance_id"], "archived")
        result = get_workspace_status(tmp_db, ws["workspace_instance_id"])
        assert result["status"] == "archived"


# ── container mount mapping 测试 ──

class TestContainerMountMapping:

    def test_resolve_container_path_no_mapping(self):
        """无映射时返回原路径。"""
        result = resolve_container_path("/home/user1/work/firmware")
        assert result == "/home/user1/work/firmware"

    def test_resolve_container_path_with_mapping(self):
        """有映射时返回宿主机路径。"""
        mappings = {
            "/home/user1/": "/data/docker_volumes/user1/",
        }
        result = resolve_container_path("/home/user1/work/firmware", mappings)
        assert result == "/data/docker_volumes/user1/work/firmware"

    def test_resolve_container_path_partial_match(self):
        """部分匹配不会误解析。"""
        mappings = {
            "/home/user1/": "/data/u1/",
        }
        result = resolve_container_path("/home/user10/work", mappings)
        # /home/user10/ 不以 /home/user1/ 开头（但实际 startswith 会返回 True...）
        # 修正：应该用更精确的匹配
        # 实际上 startswith("/home/user1/") 对 "/home/user10/work" 返回 False
        assert result == "/home/user10/work"


# ── G4: Container Mount Mapping CRUD 测试 ──

class TestMountMappingCRUD:
    """G4: 测试 register/list/delete mount mapping 的 DB 层正确性。"""

    def test_register_returns_full_row(self, tmp_db):
        """注册一条映射，返回完整记录（含 id）。"""
        result = register_mount_mapping(
            tmp_db,
            container_id="ubuntu_2204",
            container_path="/home/user1",
            host_path="/data/volumes/user1",
            mapping_type="bind",
        )
        assert result["container_id"] == "ubuntu_2204"
        assert result["container_path"] == "/home/user1"
        assert result["host_path"] == "/data/volumes/user1"
        assert result["mapping_type"] == "bind"
        assert result["id"] > 0

    def test_register_default_mapping_type_is_bind(self, tmp_db):
        """mapping_type 缺省时默认 bind。"""
        result = register_mount_mapping(
            tmp_db,
            container_id="c1",
            container_path="/app",
            host_path="/h1",
        )
        assert result["mapping_type"] == "bind"

    def test_register_rejects_invalid_mapping_type(self, tmp_db):
        """非法 mapping_type 抛 ValueError。"""
        with pytest.raises(ValueError, match="mapping_type"):
            register_mount_mapping(
                tmp_db,
                container_id="c1",
                container_path="/app",
                host_path="/h1",
                mapping_type="invalid",
            )

    def test_register_upsert_replaces_existing(self, tmp_db):
        """同 (container_id, container_path) 二次注册应替换而非新增。"""
        register_mount_mapping(tmp_db, "c1", "/app", "/h_v1", "bind")
        updated = register_mount_mapping(tmp_db, "c1", "/app", "/h_v2", "volume")
        assert updated["host_path"] == "/h_v2"
        assert updated["mapping_type"] == "volume"
        all_mappings = list_mount_mappings(tmp_db)
        assert len(all_mappings) == 1, "重复注册应替换而非插入新行"

    def test_list_returns_empty_initially(self, tmp_db):
        """无映射时返回空列表。"""
        assert list_mount_mappings(tmp_db) == []

    def test_list_returns_all_without_filter(self, tmp_db):
        """不传 container_id 时返回全部。"""
        register_mount_mapping(tmp_db, "c1", "/app", "/h1", "bind")
        register_mount_mapping(tmp_db, "c2", "/app", "/h2", "bind")
        register_mount_mapping(tmp_db, "c1", "/data", "/h3", "volume")
        all_mappings = list_mount_mappings(tmp_db)
        assert len(all_mappings) == 3

    def test_list_filters_by_container_id(self, tmp_db):
        """按 container_id 过滤。"""
        register_mount_mapping(tmp_db, "c1", "/app", "/h1", "bind")
        register_mount_mapping(tmp_db, "c2", "/app", "/h2", "bind")
        register_mount_mapping(tmp_db, "c1", "/data", "/h3", "volume")
        c1_only = list_mount_mappings(tmp_db, container_id="c1")
        assert len(c1_only) == 2
        for m in c1_only:
            assert m["container_id"] == "c1"

    def test_delete_removes_row(self, tmp_db):
        """删除存在的映射返回 1。"""
        register_mount_mapping(tmp_db, "c1", "/app", "/h1", "bind")
        deleted = delete_mount_mapping(tmp_db, "c1", "/app")
        assert deleted == 1
        assert list_mount_mappings(tmp_db) == []

    def test_delete_returns_zero_for_missing(self, tmp_db):
        """删除不存在的映射返回 0（不报错）。"""
        deleted = delete_mount_mapping(tmp_db, "c1", "/app")
        assert deleted == 0

    def test_delete_only_affects_target(self, tmp_db):
        """删除单条不影响其他映射。"""
        register_mount_mapping(tmp_db, "c1", "/app", "/h1", "bind")
        register_mount_mapping(tmp_db, "c1", "/data", "/h2", "bind")
        register_mount_mapping(tmp_db, "c2", "/app", "/h3", "bind")
        deleted = delete_mount_mapping(tmp_db, "c1", "/app")
        assert deleted == 1
        remaining = list_mount_mappings(tmp_db)
        assert len(remaining) == 2
        for m in remaining:
            assert not (m["container_id"] == "c1" and m["container_path"] == "/app")


# ── G4: daemon_server mount.* RPC dispatch 测试 ──

class TestMountMappingRpcDispatch:
    """G4: 测试 EnterpriseDaemonService.dispatch 中 mount.* RPC 的端到端行为。"""

    @pytest.fixture
    def daemon_service(self, tmp_path):
        """构造一个 EnterpriseDaemonService 实例（不启动 UDS server）。"""
        from callwarden.server.daemon_server import EnterpriseDaemonService
        from callwarden.server.snapshot_manager import SnapshotManagerService
        snapshot_service = SnapshotManagerService(max_workspaces=8)
        return EnterpriseDaemonService(
            registry_db=str(tmp_path / "registry.db"),
            snapshot_service=snapshot_service,
        )

    def _peer(self):
        """构造一个 peer credential 字典。"""
        uid = os.getuid() if hasattr(os, "getuid") else 0
        return {"pid": os.getpid(), "uid": uid, "gid": uid}

    def test_mount_register_rpc_succeeds(self, daemon_service):
        """mount.register RPC 成功注册映射。"""
        result = daemon_service.dispatch(
            self._peer(),
            "mount.register",
            {
                "container_id": "ubuntu_2204",
                "container_path": "/home/user1",
                "host_path": "/data/volumes/user1",
                "mapping_type": "bind",
            },
        )
        assert result["container_id"] == "ubuntu_2204"
        assert result["host_path"] == "/data/volumes/user1"
        assert result["mapping_type"] == "bind"

    def test_mount_register_default_mapping_type(self, daemon_service):
        """缺省 mapping_type 时默认 bind。"""
        result = daemon_service.dispatch(
            self._peer(),
            "mount.register",
            {
                "container_id": "c1",
                "container_path": "/app",
                "host_path": "/h1",
            },
        )
        assert result["mapping_type"] == "bind"

    def test_mount_register_rejects_missing_container_id(self, daemon_service):
        """缺少 container_id 时返回 invalid_params。"""
        from callwarden.server.daemon_server import DaemonRpcError
        with pytest.raises(DaemonRpcError, match="container_id"):
            daemon_service.dispatch(
                self._peer(),
                "mount.register",
                {"container_path": "/app", "host_path": "/h1"},
            )

    def test_mount_register_rejects_invalid_mapping_type(self, daemon_service):
        """非法 mapping_type 返回 invalid_params。"""
        from callwarden.server.daemon_server import DaemonRpcError
        with pytest.raises(DaemonRpcError) as exc_info:
            daemon_service.dispatch(
                self._peer(),
                "mount.register",
                {
                    "container_id": "c1",
                    "container_path": "/app",
                    "host_path": "/h1",
                    "mapping_type": "invalid",
                },
            )
        assert exc_info.value.code == "invalid_params"

    def test_mount_list_returns_empty_initially(self, daemon_service):
        """初始状态下 mount.list 返回空数组。"""
        result = daemon_service.dispatch(self._peer(), "mount.list", {})
        assert result == []

    def test_mount_list_returns_all_without_filter(self, daemon_service):
        """注册 3 条后，无 filter 返回全部。"""
        for cid, cpath, hpath in [
            ("c1", "/app", "/h1"),
            ("c2", "/app", "/h2"),
            ("c1", "/data", "/h3"),
        ]:
            daemon_service.dispatch(
                self._peer(),
                "mount.register",
                {"container_id": cid, "container_path": cpath, "host_path": hpath},
            )
        result = daemon_service.dispatch(self._peer(), "mount.list", {})
        assert len(result) == 3

    def test_mount_list_filters_by_container_id(self, daemon_service):
        """按 container_id 过滤。"""
        for cid, cpath, hpath in [
            ("c1", "/app", "/h1"),
            ("c2", "/app", "/h2"),
            ("c1", "/data", "/h3"),
        ]:
            daemon_service.dispatch(
                self._peer(),
                "mount.register",
                {"container_id": cid, "container_path": cpath, "host_path": hpath},
            )
        result = daemon_service.dispatch(
            self._peer(), "mount.list", {"container_id": "c1"}
        )
        assert len(result) == 2
        for m in result:
            assert m["container_id"] == "c1"

    def test_mount_delete_removes_mapping(self, daemon_service):
        """mount.delete 删除存在的映射，返回 deleted=1。"""
        daemon_service.dispatch(
            self._peer(),
            "mount.register",
            {"container_id": "c1", "container_path": "/app", "host_path": "/h1"},
        )
        result = daemon_service.dispatch(
            self._peer(),
            "mount.delete",
            {"container_id": "c1", "container_path": "/app"},
        )
        assert result["deleted"] == 1
        # 再 list 应为空
        assert daemon_service.dispatch(self._peer(), "mount.list", {}) == []

    def test_mount_delete_returns_zero_for_missing(self, daemon_service):
        """删除不存在的映射返回 deleted=0（不报错）。"""
        result = daemon_service.dispatch(
            self._peer(),
            "mount.delete",
            {"container_id": "c1", "container_path": "/app"},
        )
        assert result["deleted"] == 0

    def test_mount_delete_rejects_missing_container_id(self, daemon_service):
        """缺少 container_id 时返回 invalid_params。"""
        from callwarden.server.daemon_server import DaemonRpcError
        with pytest.raises(DaemonRpcError, match="container_id"):
            daemon_service.dispatch(
                self._peer(),
                "mount.delete",
                {"container_path": "/app"},
            )

    def test_mount_register_upsert_via_rpc(self, daemon_service):
        """同 (container_id, container_path) 二次注册应替换而非新增。"""
        daemon_service.dispatch(
            self._peer(),
            "mount.register",
            {
                "container_id": "c1",
                "container_path": "/app",
                "host_path": "/h_v1",
                "mapping_type": "bind",
            },
        )
        updated = daemon_service.dispatch(
            self._peer(),
            "mount.register",
            {
                "container_id": "c1",
                "container_path": "/app",
                "host_path": "/h_v2",
                "mapping_type": "volume",
            },
        )
        assert updated["host_path"] == "/h_v2"
        assert updated["mapping_type"] == "volume"
        # list 应只有 1 条
        all_mappings = daemon_service.dispatch(self._peer(), "mount.list", {})
        assert len(all_mappings) == 1


# ── daemon mode 测试 ──

class TestDaemonMode:

    def test_get_daemon_mode_returns_string(self):
        """get_daemon_mode 返回字符串。"""
        mode = get_daemon_mode()
        assert mode in ("auto", "enterprise", "local")

    def test_is_daemon_available_windows_returns_false(self):
        """Windows 上 is_daemon_available 返回 False。"""
        if os.name == "nt":
            assert is_daemon_available() is False

    def test_is_daemon_required(self):
        """is_daemon_required 返回 bool。"""
        result = is_daemon_required()
        assert isinstance(result, bool)


# ── G1 三层存储 E2E 测试 ──


class TestG1ThreeLayerStorageE2E:
    """G1: 三层存储端到端测试。

    验证 Layer 1 (CAS) + Layer 2 (ToolchainStore / 独立 toolchain.db) +
    Layer 3 (WorkspaceRegistry / daemon registry DB) 的联动。

    场景：daemon 收到 toolchain.register → build_context.register →
    toolchain.bind → toolchain.resolve 全链路请求；同时 ATTACH DATABASE
    让 workspace 连接能跨库查询 toolchain 表。
    """

    @pytest.fixture
    def daemon_service(self, tmp_path):
        """构造 EnterpriseDaemonService（使用 tmp_path 隔离 data_root）。"""
        from callwarden.server.daemon_server import EnterpriseDaemonService
        from callwarden.server.snapshot_manager import SnapshotManagerService
        snapshot_service = SnapshotManagerService(max_workspaces=4)
        return EnterpriseDaemonService(
            registry_db=str(tmp_path / "registry.db"),
            snapshot_service=snapshot_service,
            data_root=str(tmp_path / "data"),
        )

    def _peer(self):
        uid = os.getuid() if hasattr(os, "getuid") else 0
        return {"pid": os.getpid(), "uid": uid, "gid": uid}

    def test_three_layer_e2e_register_resolve(self, daemon_service, tmp_path):
        """G1 E2E：toolchain.register → bind → resolve 闭环。

        步骤：
        1. 注册 toolchain（Layer 2，独立 toolchain.db）
        2. 注册 build_context（Layer 2）
        3. 绑定 toolchain ↔ workspace（Layer 2 ↔ Layer 3）
        4. resolve_toolchain 通过 active build_context 找到 toolchain
        5. toolchain.db 文件确实存在于 data_root（独立于 registry.db）
        """
        peer = self._peer()

        # 1. 注册 toolchain（通过 daemon RPC dispatch）
        tc = daemon_service.dispatch(peer, "toolchain.register", {
            "name": "arm-gcc-9.3",
            "compiler_path": "/opt/arm-toolchain/bin/arm-none-eabi-gcc",
            "compiler_type": "gcc",
            "version": "9.3.1",
            "target_triple": "arm-none-eabi",
            "sysroot": "/opt/arm-toolchain/arm-none-eabi",
            "fingerprint": "fp_arm_9.3.1_v1",
            "description": "ARM embedded toolchain",
        })
        assert tc["name"] == "arm-gcc-9.3"
        assert tc["fingerprint"] == "fp_arm_9.3.1_v1"
        assert tc["id"] > 0

        # P0-2 整改（2026-07-21）：toolchain.resolve 等 RPC 现在校验 workspace owner
        # 先注册 workspace 拿到 workspace_id（owner_uid = peer.uid）
        workspace = daemon_service.dispatch(peer, "workspace.register", {
            "client_view_root": str(tmp_path),
            "host_real_root": str(tmp_path),
        })
        ws_id = int(workspace["workspace_id"])

        # 2. 注册 build_context（设为 active）
        bch = daemon_service.dispatch(peer, "build_context.register", {
            "workspace_id": ws_id,
            "name": "arm-debug",
            "compile_flags": ["-mcpu=cortex-m4", "-g"],
            "defines": {"DEBUG": "1"},
            "include_paths": ["/opt/arm-headers"],
            "set_active": True,
        })
        assert bch["is_active"] is True
        assert bch["name"] == "arm-debug"
        assert len(bch["build_context_hash"]) == 64  # SHA-256 hex

        # 3. 绑定 toolchain ↔ workspace（用 build_context_hash）
        bind_result = daemon_service.dispatch(peer, "toolchain.bind", {
            "workspace_id": ws_id,
            "toolchain_id": tc["id"],
            "build_context_hash": bch["build_context_hash"],
        })
        assert bind_result["bound"] is True

        # 4. resolve_toolchain 通过 active build_context 找到 toolchain
        resolved = daemon_service.dispatch(peer, "toolchain.resolve", {
            "workspace_id": ws_id,
        })
        assert resolved is not None
        assert resolved["fingerprint"] == "fp_arm_9.3.1_v1"
        assert resolved["name"] == "arm-gcc-9.3"

        # 5. toolchain.db 文件存在于 data_root（独立于 registry.db）
        toolchain_db = os.path.join(
            daemon_service._data_root, "toolchain.db"
        )
        assert os.path.exists(toolchain_db), \
            f"toolchain.db 未在 data_root 创建: {toolchain_db}"
        # registry.db 与 toolchain.db 是两个不同的文件
        registry_db = daemon_service.registry_db
        assert os.path.abspath(toolchain_db) != os.path.abspath(registry_db)

    def test_three_layer_attach_database_to_workspace(
        self, daemon_service, tmp_path
    ):
        """G1 ATTACH DATABASE：toolchain.db 可被挂载到外部连接查询。

        场景：
        1. daemon RPC 注册 toolchain（写入 toolchain.db）
        2. 用 Python 直接 attach toolchain.db 到一个 workspace 连接
        3. 通过 `<schema>.toolchains` 跨连接查询到已注册的 toolchain
        """
        peer = self._peer()
        daemon_service.dispatch(peer, "toolchain.register", {
            "name": "clang-15",
            "compiler_path": "/usr/bin/clang",
            "compiler_type": "clang",
            "fingerprint": "fp_clang_15_v1",
        })

        # 触发 toolchain.db 创建
        tc_conn = daemon_service._get_toolchain_conn()
        assert tc_conn is not None

        # 构造一个"workspace 连接"（内存 SQLite）
        ws_conn = sqlite3.connect(":memory:")

        # ATTACH toolchain.db
        from callwarden.db.db_toolchain import (
            attach_toolchain_db,
            detach_toolchain_db,
            is_toolchain_attached,
            TOOLCHAIN_ATTACH_SCHEMA,
        )
        toolchain_db_path = daemon_service._toolchain_db_path
        schema = attach_toolchain_db(ws_conn, toolchain_db_path)
        assert schema == TOOLCHAIN_ATTACH_SCHEMA
        assert is_toolchain_attached(ws_conn)

        # 跨连接查询 toolchain 表
        rows = ws_conn.execute(
            f"SELECT name, compiler_type, fingerprint FROM {schema}.toolchains"
        ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "clang-15"
        assert rows[0][1] == "clang"
        assert rows[0][2] == "fp_clang_15_v1"

        # DETACH 后再查询应报错（表不存在）
        detach_toolchain_db(ws_conn)
        assert not is_toolchain_attached(ws_conn)
        with pytest.raises(sqlite3.OperationalError):
            ws_conn.execute(f"SELECT * FROM {schema}.toolchains").fetchall()
        ws_conn.close()

    def test_three_layer_resolved_edges_isolation(
        self, daemon_service, tmp_path
    ):
        """G1 resolved_edges 按 (workspace_id, build_context_hash) 隔离。

        场景：
        - workspace 1 在 build_context_a 下有 3 条 edges
        - workspace 1 在 build_context_b 下有 1 条 edges
        - workspace 2 在 build_context_a 下有 2 条 edges
        - 互不干扰：count 只统计本 (ws, bch) 的 edges
        """
        peer = self._peer()
        bch_a = "ctx_a" * 16  # 64-char mock hash
        bch_b = "ctx_b" * 16

        # P0-2 整改（2026-07-21）：resolved_edges.* 现在校验 workspace owner
        # 先注册两个 workspace（owner_uid = peer.uid）
        ws1_root = tmp_path / "ws1"
        ws1_root.mkdir()
        ws1 = daemon_service.dispatch(peer, "workspace.register", {
            "client_view_root": str(ws1_root),
            "host_real_root": str(ws1_root),
        })
        ws1_id = int(ws1["workspace_id"])
        ws2_root = tmp_path / "ws2"
        ws2_root.mkdir()
        ws2 = daemon_service.dispatch(peer, "workspace.register", {
            "client_view_root": str(ws2_root),
            "host_real_root": str(ws2_root),
            "git_remote_url": "different",  # 避免 instance_id 冲突
        })
        ws2_id = int(ws2["workspace_id"])

        # workspace 1 / context A: 3 edges
        daemon_service.dispatch(peer, "resolved_edges.store", {
            "workspace_id": ws1_id,
            "build_context_hash": bch_a,
            "edges": [
                {"caller_symbol_id": 1, "callee_symbol_id": 10,
                 "callee_name": "foo", "call_line": 5},
                {"caller_symbol_id": 1, "callee_symbol_id": 11,
                 "callee_name": "bar", "call_line": 10},
                {"caller_symbol_id": 2, "callee_symbol_id": 10,
                 "callee_name": "foo", "call_line": 15},
            ],
        })
        # workspace 1 / context B: 1 edge
        daemon_service.dispatch(peer, "resolved_edges.store", {
            "workspace_id": ws1_id,
            "build_context_hash": bch_b,
            "edges": [
                {"caller_symbol_id": 1, "callee_symbol_id": 20,
                 "callee_name": "baz", "call_line": 20},
            ],
        })
        # workspace 2 / context A: 2 edges
        daemon_service.dispatch(peer, "resolved_edges.store", {
            "workspace_id": ws2_id,
            "build_context_hash": bch_a,
            "edges": [
                {"caller_symbol_id": 100, "callee_symbol_id": 200,
                 "callee_name": "remote", "call_line": 1},
                {"caller_symbol_id": 101, "callee_symbol_id": 200,
                 "callee_name": "remote", "call_line": 2},
            ],
        })

        # 验证 isolation
        c_1_a = daemon_service.dispatch(peer, "resolved_edges.count", {
            "workspace_id": ws1_id, "build_context_hash": bch_a,
        })
        c_1_b = daemon_service.dispatch(peer, "resolved_edges.count", {
            "workspace_id": ws1_id, "build_context_hash": bch_b,
        })
        c_2_a = daemon_service.dispatch(peer, "resolved_edges.count", {
            "workspace_id": ws2_id, "build_context_hash": bch_a,
        })

        assert c_1_a["count"] == 3
        assert c_1_b["count"] == 1
        assert c_2_a["count"] == 2

        # 验证 caller 过滤
        edges_caller1 = daemon_service.dispatch(peer, "resolved_edges.get", {
            "workspace_id": ws1_id, "build_context_hash": bch_a,
            "caller_symbol_id": 1,
        })
        assert len(edges_caller1) == 2  # (1→10) + (1→11)
        edges_caller2 = daemon_service.dispatch(peer, "resolved_edges.get", {
            "workspace_id": ws1_id, "build_context_hash": bch_a,
            "caller_symbol_id": 2,
        })
        assert len(edges_caller2) == 1  # (2→10)

    def test_three_layer_toolchain_fingerprint_dedup(
        self, daemon_service, tmp_path
    ):
        """G1 toolchain fingerprint 去重：相同 fingerprint 不重复注册。"""
        peer = self._peer()
        # 第一次注册
        tc1 = daemon_service.dispatch(peer, "toolchain.register", {
            "name": "gcc-9.3-a",
            "compiler_path": "/usr/bin/gcc",
            "compiler_type": "gcc",
            "fingerprint": "fp_dedup_test",
        })
        # 第二次注册（同 fingerprint，不同 name）
        tc2 = daemon_service.dispatch(peer, "toolchain.register", {
            "name": "gcc-9.3-b",
            "compiler_path": "/usr/bin/gcc-9.3",
            "compiler_type": "gcc",
            "fingerprint": "fp_dedup_test",
        })

        # 应返回同一个 toolchain（按 fingerprint 去重）
        assert tc1["id"] == tc2["id"]
        assert tc1["fingerprint"] == tc2["fingerprint"]

        # list 应只有 1 个
        lst = daemon_service.dispatch(peer, "toolchain.list", {})
        assert len(lst) == 1

    def test_three_layer_resolve_fallback_chain(
        self, daemon_service, tmp_path
    ):
        """G1 resolve_toolchain 4 步降级链。

        步骤：
        1. 无任何绑定时返回 None
        2. 注册 build_context_a (active) + 绑定 toolchain_a → resolve 返回 toolchain_a
        3. 改 active 到 build_context_b（无绑定）→ resolve 返回 None
           （active 已切走，但 bch 无 toolchain 绑定）
        4. 绑定 toolchain_b 到 build_context_b（explicit hash）→ 用 bch 显式
           resolve 返回 toolchain_b
        """
        peer = self._peer()

        # P0-2 整改（2026-07-21）：toolchain.resolve 现在校验 workspace owner
        # 先注册 workspace（owner_uid = peer.uid）
        workspace = daemon_service.dispatch(peer, "workspace.register", {
            "client_view_root": str(tmp_path),
            "host_real_root": str(tmp_path),
        })
        ws_id = int(workspace["workspace_id"])

        # 1. 无绑定
        r1 = daemon_service.dispatch(peer, "toolchain.resolve", {
            "workspace_id": ws_id,
        })
        assert r1 is None

        # 注册两个 toolchain
        tc_a = daemon_service.dispatch(peer, "toolchain.register", {
            "name": "tc-a", "compiler_path": "/x/a",
            "compiler_type": "gcc", "fingerprint": "fp_a",
        })
        tc_b = daemon_service.dispatch(peer, "toolchain.register", {
            "name": "tc-b", "compiler_path": "/x/b",
            "compiler_type": "clang", "fingerprint": "fp_b",
        })

        # 2. context_a (active) + 绑定 tc_a
        bch_a = daemon_service.dispatch(peer, "build_context.register", {
            "workspace_id": ws_id, "name": "a",
            "compile_flags": ["-g"], "set_active": True,
        })
        daemon_service.dispatch(peer, "toolchain.bind", {
            "workspace_id": ws_id, "toolchain_id": tc_a["id"],
            "build_context_hash": bch_a["build_context_hash"],
        })
        r2 = daemon_service.dispatch(peer, "toolchain.resolve", {
            "workspace_id": ws_id,
        })
        assert r2["fingerprint"] == "fp_a"

        # 3. 切换 active 到 context_b（无绑定）
        bch_b = daemon_service.dispatch(peer, "build_context.register", {
            "workspace_id": ws_id, "name": "b",
            "compile_flags": ["-O2"], "set_active": True,
        })
        r3 = daemon_service.dispatch(peer, "toolchain.resolve", {
            "workspace_id": ws_id,
        })
        # active 已切到 bch_b，但 bch_b 无 toolchain 绑定 → fallback 到 default（空 hash）
        # → 仍无 → 返回 None
        assert r3 is None

        # 4. 绑定 tc_b 到 bch_b（explicit hash）
        daemon_service.dispatch(peer, "toolchain.bind", {
            "workspace_id": ws_id, "toolchain_id": tc_b["id"],
            "build_context_hash": bch_b["build_context_hash"],
        })
        # 4a. 用 active（隐式）→ 应返回 tc_b
        r4a = daemon_service.dispatch(peer, "toolchain.resolve", {
            "workspace_id": ws_id,
        })
        assert r4a["fingerprint"] == "fp_b"
        # 4b. 用 explicit bch_a → 应返回 tc_a（精确匹配优先于 active）
        r4b = daemon_service.dispatch(peer, "toolchain.resolve", {
            "workspace_id": ws_id,
            "build_context_hash": bch_a["build_context_hash"],
        })
        assert r4b["fingerprint"] == "fp_a"
