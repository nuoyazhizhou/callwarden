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
