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
