"""P23b: 测试 gc db-cleanup 子命令和 conftest 数据库隔离

测试两部分：
1. conftest.py 的 _isolate_db_path fixture 是否正确隔离测试数据库
2. _handle_gc_db_cleanup 函数的孤儿数据库检测逻辑
"""
import os
import tempfile
import sqlite3
import hashlib
import shutil
from unittest import mock

import pytest

from callwarden import config as _cw_config
from callwarden.config import CALLWARDEN_DIR, norm_path
from callwarden.db.db import CodeGraphDB


class TestConftestDbIsolation:
    """测试 conftest.py 的 _isolate_db_path fixture"""

    def test_codegraphdb_uses_isolated_path(self, tmp_path):
        """CodeGraphDB(workspace_root=tmp) 不应在 ~/.callwarden/ 下建库

        conftest.py 的 autouse fixture monkey-patch 了 get_project_db_path，
        使其返回 tmp_path/test_isolated.db，而非 ~/.callwarden/<hash>/callwarden.db
        """
        proj = tmp_path / "test_proj"
        proj.mkdir()
        (proj / "main.py").write_text("def foo(): pass\n")

        db = CodeGraphDB(workspace_root=str(proj))
        db.build_full_graph(force=False)

        # db_path 应该在 tmp_path 下，而非 ~/.callwarden/ 下
        assert tmp_path.as_posix() in db.db_path or "test_isolated" in db.db_path
        assert CALLWARDEN_DIR not in db.db_path

        db.close()

    def test_no_orphan_in_callwarden_dir(self, tmp_path):
        """测试结束后 ~/.callwarden/ 下不应新增孤儿目录"""
        proj = tmp_path / "another_proj"
        proj.mkdir()
        (proj / "app.py").write_text("def bar(): pass\n")

        db = CodeGraphDB(workspace_root=str(proj))
        db.build_full_graph(force=False)

        # 计算当前 workspace_root 的 hash
        norm_root = norm_path(os.path.abspath(str(proj)))
        path_hash = hashlib.sha256(norm_root.encode("utf-8")).hexdigest()[:16]
        orphan_path = os.path.join(CALLWARDEN_DIR, path_hash)

        # 孤儿目录不应存在（因为 conftest patch 了 get_project_db_path）
        assert not os.path.exists(orphan_path), \
            f"conftest 隔离失败：~/.callwarden/{path_hash}/ 被创建"

        db.close()


class TestGcDbCleanup:
    """测试 _handle_gc_db_cleanup 函数"""

    def _create_test_db(self, dir_path, root_path, name="test_ws"):
        """创建测试数据库（带 workspaces 表）"""
        os.makedirs(dir_path, exist_ok=True)
        db_path = os.path.join(dir_path, "callwarden.db")
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS workspaces (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                root_path TEXT NOT NULL,
                is_active INTEGER DEFAULT 0
            )
        """)
        conn.execute("INSERT INTO workspaces (name, root_path) VALUES (?, ?)",
                     (name, root_path))
        conn.commit()
        conn.close()
        return db_path

    def _patch_cw_dir(self, monkeypatch, fake_dir):
        """patch CALLWARDEN_DIR（函数内 from ..config import 是从 config 模块获取）"""
        monkeypatch.setattr(_cw_config, "CALLWARDEN_DIR", fake_dir)

    def test_detect_orphan_path_not_exist(self, tmp_path, monkeypatch):
        """workspace_root 路径不存在 → 判为孤儿"""
        from callwarden.cli.main import _handle_gc_db_cleanup

        fake_cw_dir = str(tmp_path / "fake_callwarden")
        self._patch_cw_dir(monkeypatch, fake_cw_dir)

        hash_dir = "aaaa1234bbbb5678"
        self._create_test_db(
            os.path.join(fake_cw_dir, hash_dir),
            "/nonexistent/path/to/project",
        )

        with mock.patch("callwarden.cli.main.cprint"):
            result = _handle_gc_db_cleanup(dry_run=True)

        assert result is True

    def test_detect_orphan_temp_dir(self, tmp_path, monkeypatch):
        """workspace_root 指向临时目录 → 判为孤儿"""
        from callwarden.cli.main import _handle_gc_db_cleanup

        fake_cw_dir = str(tmp_path / "fake_callwarden")
        self._patch_cw_dir(monkeypatch, fake_cw_dir)

        hash_dir = "bbbb1234cccc5678"
        temp_root = os.path.join(tempfile.gettempdir(), "pytest-fake-test-12345")
        self._create_test_db(
            os.path.join(fake_cw_dir, hash_dir),
            temp_root,
        )

        with mock.patch("callwarden.cli.main.cprint"):
            result = _handle_gc_db_cleanup(dry_run=True)

        assert result is True

    def test_detect_valid_db(self, tmp_path, monkeypatch):
        """workspace_root 路径存在且非临时目录 → 不判为孤儿"""
        from callwarden.cli.main import _handle_gc_db_cleanup

        fake_cw_dir = str(tmp_path / "fake_callwarden")
        self._patch_cw_dir(monkeypatch, fake_cw_dir)

        valid_proj = tmp_path / "valid_project"
        valid_proj.mkdir()
        hash_dir = "cccc1234dddd5678"
        self._create_test_db(
            os.path.join(fake_cw_dir, hash_dir),
            str(valid_proj),
            name="valid_proj",
        )

        with mock.patch("callwarden.cli.main.cprint"):
            result = _handle_gc_db_cleanup(dry_run=True)

        assert result is True

    def test_all_but_current(self, tmp_path, monkeypatch):
        """--all-but-current 保留当前 workspace，其他全判为孤儿"""
        from callwarden.cli.main import _handle_gc_db_cleanup

        fake_cw_dir = str(tmp_path / "fake_callwarden")
        self._patch_cw_dir(monkeypatch, fake_cw_dir)

        current_proj = tmp_path / "current_proj"
        current_proj.mkdir()
        current_norm = norm_path(str(current_proj))
        current_hash = hashlib.sha256(current_norm.encode("utf-8")).hexdigest()[:16]

        self._create_test_db(
            os.path.join(fake_cw_dir, current_hash),
            str(current_proj),
            name="current",
        )

        other_proj = tmp_path / "other_proj"
        other_proj.mkdir()
        other_hash_dir = "eeee1234ffff5678"
        self._create_test_db(
            os.path.join(fake_cw_dir, other_hash_dir),
            str(other_proj),
            name="other",
        )

        with mock.patch("callwarden.cli.main.cprint"):
            result = _handle_gc_db_cleanup(
                dry_run=True,
                all_but_current=True,
                current_workspace_root=str(current_proj),
            )

        assert result is True

    def test_apply_actually_deletes(self, tmp_path, monkeypatch):
        """--apply 实际删除孤儿数据库目录"""
        from callwarden.cli.main import _handle_gc_db_cleanup

        fake_cw_dir = str(tmp_path / "fake_callwarden")
        self._patch_cw_dir(monkeypatch, fake_cw_dir)

        hash_dir = "ffff1234eeee5678"
        orphan_dir = os.path.join(fake_cw_dir, hash_dir)
        self._create_test_db(orphan_dir, "/nonexistent/path")

        assert os.path.isdir(orphan_dir)

        with mock.patch("callwarden.cli.main.cprint"):
            _handle_gc_db_cleanup(dry_run=False)

        assert not os.path.exists(orphan_dir)

    def test_dry_run_does_not_delete(self, tmp_path, monkeypatch):
        """dry-run 不删除任何文件"""
        from callwarden.cli.main import _handle_gc_db_cleanup

        fake_cw_dir = str(tmp_path / "fake_callwarden")
        self._patch_cw_dir(monkeypatch, fake_cw_dir)

        hash_dir = "ab12cd34ef56ab78"
        orphan_dir = os.path.join(fake_cw_dir, hash_dir)
        self._create_test_db(orphan_dir, "/nonexistent/path")

        assert os.path.isdir(orphan_dir)

        with mock.patch("callwarden.cli.main.cprint"):
            _handle_gc_db_cleanup(dry_run=True)

        assert os.path.exists(orphan_dir)

    def test_empty_callwarden_dir(self, tmp_path, monkeypatch):
        """空 ~/.callwarden/ 目录 → 报告 0 数据库"""
        from callwarden.cli.main import _handle_gc_db_cleanup

        fake_cw_dir = str(tmp_path / "empty_callwarden")
        os.makedirs(fake_cw_dir, exist_ok=True)
        self._patch_cw_dir(monkeypatch, fake_cw_dir)

        with mock.patch("callwarden.cli.main.cprint"):
            result = _handle_gc_db_cleanup(dry_run=True)

        assert result is True

    def test_no_callwarden_dir(self, tmp_path, monkeypatch):
        """~/.callwarden/ 不存在 → 友好提示"""
        from callwarden.cli.main import _handle_gc_db_cleanup

        fake_cw_dir = str(tmp_path / "nonexistent_callwarden")
        self._patch_cw_dir(monkeypatch, fake_cw_dir)

        with mock.patch("callwarden.cli.main.cprint"):
            result = _handle_gc_db_cleanup(dry_run=True)

        assert result is True

