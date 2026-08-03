"""P2 任务 6.5：dependency CLI 命令测试。

验证 cw dependency 子命令的核心行为（Requirements 9.1-9.10, 13.7-13.8）：
- inspect：查看任务/契约的依赖声明与 freshness
- list：列出依赖边
- cycle：检测环
- explain：解释 revision 依赖验证
- provider-select：记录显式 provider 选择

明确不提供自动排程/assignment/抢占（Req 9.10）。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PKG_PARENT = str(Path(__file__).resolve().parents[1].parent)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db import CodeGraphDB
from callwarden.db.db_task_dependencies import (
    DEP_PROVIDES_INTERFACE,
    DEP_REQUIRES_ARTIFACT,
    DEP_REQUIRES_EXISTING,
    DEP_REQUIRES_INTERFACE,
)


# ============================================
# Fixtures
# ============================================

@pytest.fixture
def db_with_deps(tmp_path):
    """创建带 P2 schema 与测试数据的临时数据库。"""
    db_path = str(tmp_path / "test_cli_deps.db")
    db = CodeGraphDB(db_path, workspace_root=str(tmp_path))
    ws_id = db.register_workspace("test", str(tmp_path), "测试")
    db.set_active_workspace(ws_id)

    # 插入测试数据
    db.import_envelope_dependencies(
        ws_id, "T-consumer", "C-test", 1,
        [
            {"dependency_type": DEP_REQUIRES_EXISTING, "target_ref": "auth.service"},
            {"dependency_type": DEP_REQUIRES_ARTIFACT,
             "target_ref": "ART-1", "target_task_id": "T-provider"},
        ],
    )
    db.record_artifact_identity(
        ws_id, "T-provider", "C-test", 1, "file", "src/output.py",
        artifact_hash="sha256:abc",
    )
    db.publish_interface(
        ws_id, "T-provider", "C-provider", 1,
        "auth.verify", "1.0.0", "sha256:iface",
    )
    db.build_hard_dependency_edges(ws_id, "C-test", 1)

    yield db, ws_id
    db.close()


# ============================================
# CLI 命令调度测试
# ============================================

class TestDependencyCliDispatch:
    """验证 dependency 子命令能正确调度（6.5）。"""

    def test_dependency_in_subcommands(self):
        """'dependency' 已注册到 _SUBCOMMANDS。"""
        from callwarden.cli.main import _SUBCOMMANDS
        assert "dependency" in _SUBCOMMANDS

    def test_dependency_readonly_actions(self):
        """inspect/list/cycle/explain 是只读，provider-select 是写。"""
        from callwarden.cli.main import _is_readonly_command
        assert _is_readonly_command("dependency", ["inspect", "--task-id", "T-1"])
        assert _is_readonly_command("dependency", ["list"])
        assert _is_readonly_command("dependency", ["cycle"])
        assert _is_readonly_command("dependency", ["explain", "--contract-id", "C", "--revision", "1"])
        assert not _is_readonly_command("dependency", ["provider-select"])

    def test_handle_dependency_dispatches(self, db_with_deps, monkeypatch):
        """_handle_dependency 能调度到子命令。"""
        db, ws_id = db_with_deps
        from callwarden.cli import main as cli_main

        # 模拟 sys.argv[1] = "dependency"
        monkeypatch.setattr(sys, "argv", ["cw", "dependency", "cycle"])

        # 直接调用 _dispatch_subcommand
        result = cli_main._dispatch_subcommand(["cycle"], db)
        assert result is True


# ============================================
# cycle 命令测试
# ============================================

class TestDependencyCycleCommand:
    """验证 dependency cycle 命令（6.5）。"""

    def test_cycle_no_cycle(self, db_with_deps, capsys):
        """无环时输出 acyclic。"""
        db, ws_id = db_with_deps
        from callwarden.cli.main import _dependency_cycle

        class Opts:
            json = False

        result = _dependency_cycle(db, ws_id, Opts(), False)
        assert result is True
        captured = capsys.readouterr()
        assert "acyclic" in captured.out.lower() or "无环" in captured.out

    def test_cycle_json_output(self, db_with_deps, capsys):
        """JSON 输出格式正确。"""
        db, ws_id = db_with_deps
        from callwarden.cli.main import _dependency_cycle

        class Opts:
            json = True

        result = _dependency_cycle(db, ws_id, Opts(), True)
        assert result is True
        captured = capsys.readouterr()
        import json
        data = json.loads(captured.out)
        assert "has_cycle" in data
        assert data["has_cycle"] is False


# ============================================
# explain 命令测试
# ============================================

class TestDependencyExplainCommand:
    """验证 dependency explain 命令（6.5）。"""

    def test_explain_valid_revision(self, db_with_deps, capsys):
        """无环 revision 输出 valid。"""
        db, ws_id = db_with_deps
        from callwarden.cli.main import _dependency_explain

        class Opts:
            contract_id = "C-test"
            revision = 1
            json = False

        result = _dependency_explain(db, ws_id, Opts(), False)
        assert result is True
        captured = capsys.readouterr()
        # 应包含 "通过" 或 "Valid"
        assert "通过" in captured.out or "Valid" in captured.out

    def test_explain_no_scheduling_note(self, db_with_deps, capsys):
        """explain 输出明确说明无自动排程（Req 9.10）。"""
        db, ws_id = db_with_deps
        from callwarden.cli.main import _dependency_explain

        class Opts:
            contract_id = "C-test"
            revision = 1
            json = False

        _dependency_explain(db, ws_id, Opts(), False)
        captured = capsys.readouterr()
        # 明确说明不提供自动排程/assignment/抢占
        assert "9.10" in captured.out or "排程" in captured.out or "scheduling" in captured.out.lower()


# ============================================
# provider-select 命令测试
# ============================================

class TestDependencyProviderSelectCommand:
    """验证 dependency provider-select 命令（6.5，Req 9.9）。"""

    def test_provider_select_success(self, db_with_deps, capsys):
        """成功记录 provider 选择。"""
        db, ws_id = db_with_deps
        from callwarden.cli.main import _dependency_provider_select

        class Opts:
            consumer_task_id = "T-consumer"
            contract_id = "C-test"
            revision = 1
            interface_name = "auth.verify"
            provider_task_id = "T-provider"
            json = False

        result = _dependency_provider_select(db, ws_id, Opts(), False)
        assert result is True
        captured = capsys.readouterr()
        assert "✓" in captured.out or "success" in captured.out.lower()

        # 验证数据库记录
        selection = db.get_provider_selection(
            ws_id, "T-consumer", "C-test", 1, "auth.verify",
        )
        assert selection == "T-provider"
