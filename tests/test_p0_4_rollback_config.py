"""Phase 0 子任务 4：迁移回滚配置（rollback_config）测试。

验证 RollbackConfigMixin 的方法行为一致性：
- register/get/list/set_flag/is_feature_rolled_back 五个核心方法
- rollback_flag 状态保持（update 不重置 flag）
- config_blob JSON 序列化/反序列化
- schema migration 幂等性

由于 rollback_config 是单行 SELECT 查询（AGENTS.md 规则 8：Python 更快），
本测试不包含 Python/Rust 差分对照，改为验证 Python 端方法行为一致性。

关联契约：docs/design/migration-quality-gate-contract.md
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

import pytest

# 仓库根目录的父目录需要在 sys.path 中
_PKG_PARENT = str(Path(__file__).resolve().parents[1].parent)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db import CodeGraphDB
from callwarden.db.schema import SCHEMA_VERSION


# ============================================
# Fixtures
# ============================================

@pytest.fixture
def db_with_rollback(tmp_path):
    """创建一个带 rollback_config 表的临时数据库。

    关闭外键检查：rollback_config.task_id REFERENCES tasks.id，但本测试套件
    验证 RollbackConfigMixin 的方法行为，不是外键约束语义；测试 task_id 不
    对应真实 tasks 记录。CW_USE_RUST_STORAGE=1（默认）时 PRAGMA foreign_keys=ON，
    需显式关闭以避免 FOREIGN KEY constraint failed。
    """
    db_path = str(tmp_path / "test_rollback.db")
    db = CodeGraphDB(db_path, workspace_root=str(tmp_path))
    db.register_workspace("test", str(tmp_path), "测试")
    db.conn.execute("PRAGMA foreign_keys=OFF")
    yield db
    db.close()


# ============================================
# Schema 测试
# ============================================

class TestRollbackConfigSchema:
    """验证 rollback_config 表 schema 和 migration。"""

    def test_schema_version_is_46(self):
        """SCHEMA_VERSION 必须为 46（v46 为 P4 assignment/lease 后当前版本）。"""
        assert SCHEMA_VERSION == 46

    def test_table_exists(self, db_with_rollback):
        """rollback_config 表必须存在。"""
        cur = db_with_rollback.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='rollback_config'"
        )
        assert cur.fetchone() is not None

    def test_table_columns(self, db_with_rollback):
        """rollback_config 表必须有所有契约定义的列。"""
        cur = db_with_rollback.conn.execute("PRAGMA table_info(rollback_config)")
        columns = {row["name"] for row in cur.fetchall()}
        expected = {
            "id", "workspace_id", "task_id", "feature_name", "phase",
            "production_entry", "rollback_entry", "rollback_flag",
            "rollback_window_until", "config_blob", "created_at", "updated_at",
        }
        assert expected.issubset(columns), f"missing columns: {expected - columns}"

    def test_indexes_exist(self, db_with_rollback):
        """rollback_config 表必须有 3 个索引。"""
        cur = db_with_rollback.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='rollback_config'"
        )
        index_names = {row["name"] for row in cur.fetchall()}
        expected = {
            "idx_rollback_config_task",
            "idx_rollback_config_feature",
            "idx_rollback_config_flag",
        }
        assert expected.issubset(index_names), f"missing indexes: {expected - index_names}"

    def test_migration_idempotent(self, tmp_path):
        """v41->v42 migration 必须幂等（重复执行不报错）。"""
        from callwarden.db.db_base import _migrate_v41_to_v42
        import sqlite3

        db_path = str(tmp_path / "test_migration.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row

        # 第一次执行 migration
        _migrate_v41_to_v42(conn)
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='rollback_config'"
        ).fetchone() is not None

        # 第二次执行 migration（幂等）
        _migrate_v41_to_v42(conn)
        assert conn.execute(
            "SELECT name FROM sqlite_master WHERE name='rollback_config'"
        ).fetchone() is not None

        conn.close()


# ============================================
# register_rollback_config 测试
# ============================================

class TestRegisterRollbackConfig:
    """验证 register_rollback_config 方法。"""

    def test_register_insert_new(self, db_with_rollback):
        """注册新记录应返回 action=inserted。"""
        result = db_with_rollback.register_rollback_config(
            task_id="T-test-insert",
            feature_name="test_feature",
            phase=1,
            production_entry="db/db_base.py:connect",
            rollback_entry="db/db_base.py:connect_python",
        )
        assert result["success"] is True
        assert result["action"] == "inserted"
        assert result["rollback_flag"] == 0
        assert result["id"] > 0

    def test_register_update_existing_preserves_flag(self, db_with_rollback):
        """更新已有记录时应保留 rollback_flag 不变。"""
        # 先插入
        db_with_rollback.register_rollback_config(
            task_id="T-test-update",
            feature_name="old_feature",
            phase=1,
            production_entry="old_entry",
            rollback_entry="old_rollback",
        )
        # 设置 flag=1
        db_with_rollback.set_rollback_flag("T-test-update", 1, reason="test")

        # 更新记录
        result = db_with_rollback.register_rollback_config(
            task_id="T-test-update",
            feature_name="new_feature",
            phase=2,
            production_entry="new_entry",
            rollback_entry="new_rollback",
        )
        assert result["action"] == "updated"

        # 验证 flag 未被重置
        config = db_with_rollback.get_rollback_config("T-test-update")
        assert config["rollback_flag"] == 1
        assert config["feature_name"] == "new_feature"
        assert config["phase"] == 2

    def test_register_with_config_blob(self, db_with_rollback):
        """config_blob 应正确序列化和反序列化。"""
        config_data = {"flag": "CW_USE_RUST", "threshold": 100, "nested": {"key": "value"}}
        db_with_rollback.register_rollback_config(
            task_id="T-test-blob",
            feature_name="blob_feature",
            phase=1,
            production_entry="entry",
            rollback_entry="rollback",
            config_blob=config_data,
        )
        config = db_with_rollback.get_rollback_config("T-test-blob")
        assert config["config_blob"] == config_data

    def test_register_validation_errors(self, db_with_rollback):
        """缺少必填参数应返回 success=False。"""
        # 缺少 task_id
        result = db_with_rollback.register_rollback_config(
            task_id="",
            feature_name="test",
            phase=1,
            production_entry="entry",
            rollback_entry="rollback",
        )
        assert result["success"] is False

        # 缺少 feature_name
        result = db_with_rollback.register_rollback_config(
            task_id="T-test",
            feature_name="",
            phase=1,
            production_entry="entry",
            rollback_entry="rollback",
        )
        assert result["success"] is False


# ============================================
# get_rollback_config 测试
# ============================================

class TestGetRollbackConfig:
    """验证 get_rollback_config 方法。"""

    def test_get_existing(self, db_with_rollback):
        """查询已有记录应返回完整 dict。"""
        db_with_rollback.register_rollback_config(
            task_id="T-test-get",
            feature_name="get_feature",
            phase=3,
            production_entry="prod_entry",
            rollback_entry="rollback_entry",
            rollback_window_until="2027-12-31T00:00:00",
        )
        config = db_with_rollback.get_rollback_config("T-test-get")
        assert config is not None
        assert config["task_id"] == "T-test-get"
        assert config["feature_name"] == "get_feature"
        assert config["phase"] == 3
        assert config["production_entry"] == "prod_entry"
        assert config["rollback_entry"] == "rollback_entry"
        assert config["rollback_flag"] == 0
        assert config["rollback_window_until"] == "2027-12-31T00:00:00"

    def test_get_not_found(self, db_with_rollback):
        """查询不存在的 task_id 应返回 None。"""
        config = db_with_rollback.get_rollback_config("T-nonexistent")
        assert config is None

    def test_get_empty_task_id(self, db_with_rollback):
        """空 task_id 应返回 None。"""
        config = db_with_rollback.get_rollback_config("")
        assert config is None


# ============================================
# list_rollback_configs 测试
# ============================================

class TestListRollbackConfigs:
    """验证 list_rollback_configs 方法。"""

    def test_list_all(self, db_with_rollback):
        """列出所有配置。"""
        for i in range(3):
            db_with_rollback.register_rollback_config(
                task_id=f"T-test-list-{i}",
                feature_name=f"feature_{i}",
                phase=i % 3,
                production_entry=f"entry_{i}",
                rollback_entry=f"rollback_{i}",
            )
        configs = db_with_rollback.list_rollback_configs()
        assert len(configs) >= 3

    def test_list_filter_by_phase(self, db_with_rollback):
        """按 phase 过滤。"""
        db_with_rollback.register_rollback_config(
            task_id="T-list-phase-1",
            feature_name="p1_feature",
            phase=1,
            production_entry="e1",
            rollback_entry="r1",
        )
        db_with_rollback.register_rollback_config(
            task_id="T-list-phase-2",
            feature_name="p2_feature",
            phase=2,
            production_entry="e2",
            rollback_entry="r2",
        )
        configs = db_with_rollback.list_rollback_configs(phase=1)
        assert all(c["phase"] == 1 for c in configs)
        assert any(c["task_id"] == "T-list-phase-1" for c in configs)
        assert not any(c["task_id"] == "T-list-phase-2" for c in configs)

    def test_list_filter_by_flag(self, db_with_rollback):
        """按 rollback_flag 过滤。"""
        db_with_rollback.register_rollback_config(
            task_id="T-list-flag-0",
            feature_name="normal_feature",
            phase=1,
            production_entry="e1",
            rollback_entry="r1",
        )
        db_with_rollback.register_rollback_config(
            task_id="T-list-flag-1",
            feature_name="rolled_back_feature",
            phase=1,
            production_entry="e2",
            rollback_entry="r2",
        )
        db_with_rollback.set_rollback_flag("T-list-flag-1", 1)

        rolled_back = db_with_rollback.list_rollback_configs(rollback_flag=1)
        assert all(c["rollback_flag"] == 1 for c in rolled_back)
        assert any(c["task_id"] == "T-list-flag-1" for c in rolled_back)

        normal = db_with_rollback.list_rollback_configs(rollback_flag=0)
        assert all(c["rollback_flag"] == 0 for c in normal)


# ============================================
# set_rollback_flag 测试
# ============================================

class TestSetRollbackFlag:
    """验证 set_rollback_flag 方法。"""

    def test_set_flag_0_to_1(self, db_with_rollback):
        """从 0 设置为 1。"""
        db_with_rollback.register_rollback_config(
            task_id="T-set-flag",
            feature_name="set_feature",
            phase=1,
            production_entry="e",
            rollback_entry="r",
        )
        result = db_with_rollback.set_rollback_flag("T-set-flag", 1, reason="test")
        assert result["success"] is True
        assert result["previous_flag"] == 0
        assert result["rollback_flag"] == 1
        assert result["feature_name"] == "set_feature"

    def test_set_flag_1_to_0(self, db_with_rollback):
        """从 1 恢复为 0。"""
        db_with_rollback.register_rollback_config(
            task_id="T-restore",
            feature_name="restore_feature",
            phase=1,
            production_entry="e",
            rollback_entry="r",
        )
        db_with_rollback.set_rollback_flag("T-restore", 1)
        result = db_with_rollback.set_rollback_flag("T-restore", 0, reason="restored")
        assert result["success"] is True
        assert result["previous_flag"] == 1
        assert result["rollback_flag"] == 0

    def test_set_flag_noop(self, db_with_rollback):
        """相同值时返回 noop。"""
        db_with_rollback.register_rollback_config(
            task_id="T-noop",
            feature_name="noop_feature",
            phase=1,
            production_entry="e",
            rollback_entry="r",
        )
        result = db_with_rollback.set_rollback_flag("T-noop", 0)
        assert result["success"] is True
        assert result["note"] == "flag unchanged"

    def test_set_flag_not_found(self, db_with_rollback):
        """不存在的 task_id 应失败。"""
        result = db_with_rollback.set_rollback_flag("T-nonexistent", 1)
        assert result["success"] is False

    def test_set_flag_invalid_value(self, db_with_rollback):
        """非法 flag 值应失败。"""
        db_with_rollback.register_rollback_config(
            task_id="T-invalid",
            feature_name="invalid_feature",
            phase=1,
            production_entry="e",
            rollback_entry="r",
        )
        result = db_with_rollback.set_rollback_flag("T-invalid", 2)
        assert result["success"] is False
        result = db_with_rollback.set_rollback_flag("T-invalid", -1)
        assert result["success"] is False


# ============================================
# is_feature_rolled_back 测试
# ============================================

class TestIsFeatureRolledBack:
    """验证 is_feature_rolled_back 方法。"""

    def test_not_rolled_back(self, db_with_rollback):
        """flag=0 时返回 False。"""
        db_with_rollback.register_rollback_config(
            task_id="T-check-normal",
            feature_name="normal_check_feature",
            phase=1,
            production_entry="e",
            rollback_entry="r",
        )
        assert db_with_rollback.is_feature_rolled_back("normal_check_feature") is False

    def test_rolled_back(self, db_with_rollback):
        """flag=1 时返回 True。"""
        db_with_rollback.register_rollback_config(
            task_id="T-check-rolled",
            feature_name="rolled_check_feature",
            phase=1,
            production_entry="e",
            rollback_entry="r",
        )
        db_with_rollback.set_rollback_flag("T-check-rolled", 1)
        assert db_with_rollback.is_feature_rolled_back("rolled_check_feature") is True

    def test_feature_not_found(self, db_with_rollback):
        """未注册功能返回 False。"""
        assert db_with_rollback.is_feature_rolled_back("nonexistent_feature") is False

    def test_empty_feature_name(self, db_with_rollback):
        """空 feature_name 返回 False。"""
        assert db_with_rollback.is_feature_rolled_back("") is False


# ============================================
# 端到端场景测试
# ============================================

class TestEndToEndScenarios:
    """验证完整的回滚配置生命周期。"""

    def test_full_lifecycle(self, db_with_rollback):
        """完整生命周期：注册 → 正常运行 → 紧急回滚 → 恢复。"""
        # 1. 注册（wire-production step）
        db_with_rollback.register_rollback_config(
            task_id="T-lifecycle",
            feature_name="lifecycle_feature",
            phase=1,
            production_entry="db/db_base.py:connect",
            rollback_entry="db/db_base.py:connect_python",
            rollback_window_until="2027-06-30T00:00:00",
            config_blob={"flag": "CW_USE_RUST_SQLITE"},
        )

        # 2. 正常运行（flag=0）
        assert db_with_rollback.is_feature_rolled_back("lifecycle_feature") is False

        # 3. 紧急回滚（flag=1）
        result = db_with_rollback.set_rollback_flag("T-lifecycle", 1, reason="production incident")
        assert result["success"] is True
        assert db_with_rollback.is_feature_rolled_back("lifecycle_feature") is True

        # 4. 恢复（flag=0）
        result = db_with_rollback.set_rollback_flag("T-lifecycle", 0, reason="fixed")
        assert result["success"] is True
        assert db_with_rollback.is_feature_rolled_back("lifecycle_feature") is False

    def test_multiple_features_same_phase(self, db_with_rollback):
        """同一 phase 可以有多个 feature。"""
        for i in range(5):
            db_with_rollback.register_rollback_config(
                task_id=f"T-multi-{i}",
                feature_name=f"multi_feature_{i}",
                phase=2,
                production_entry=f"entry_{i}",
                rollback_entry=f"rollback_{i}",
            )
        configs = db_with_rollback.list_rollback_configs(phase=2)
        assert len(configs) >= 5

    def test_update_preserves_flag_after_multiple_operations(self, db_with_rollback):
        """多次操作后 update 仍保留 flag。"""
        db_with_rollback.register_rollback_config(
            task_id="T-multi-op",
            feature_name="multi_op_feature",
            phase=1,
            production_entry="e1",
            rollback_entry="r1",
        )
        # 设置 flag=1
        db_with_rollback.set_rollback_flag("T-multi-op", 1)
        # 更新配置
        db_with_rollback.register_rollback_config(
            task_id="T-multi-op",
            feature_name="multi_op_feature_v2",
            phase=2,
            production_entry="e2",
            rollback_entry="r2",
        )
        # 恢复 flag=0
        db_with_rollback.set_rollback_flag("T-multi-op", 0)
        # 再次更新
        db_with_rollback.register_rollback_config(
            task_id="T-multi-op",
            feature_name="multi_op_feature_v3",
            phase=3,
            production_entry="e3",
            rollback_entry="r3",
        )
        # 验证 flag 仍为 0
        config = db_with_rollback.get_rollback_config("T-multi-op")
        assert config["rollback_flag"] == 0
        assert config["feature_name"] == "multi_op_feature_v3"
        assert config["phase"] == 3
