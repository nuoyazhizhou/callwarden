"""任务 reopen 机制文档结构验证测试（Step #3）。

验证三个文档的关键章节存在性：
- docs/cli_reference.md: task reopen 命令章节 + 兄弟子任务状态判断逻辑说明
- docs/architecture.md: §8. 任务 reopen 机制章节 + 两种触发方式说明
- AGENTS.md: 任务 reopen 机制子章节 + 设计原则

不验证具体内容细节（已在 test_task_reopen.py / test_cli_task_reopen.py 功能测试中覆盖），
仅验证文档骨架结构符合 Step #3 的 Check Items 要求。
"""

import os
import sys

import pytest

# 项目根目录
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

DOCS_DIR = os.path.join(_PKG_PARENT, "docs")
CLI_REF = os.path.join(DOCS_DIR, "cli_reference.md")
ARCHITECTURE = os.path.join(DOCS_DIR, "architecture.md")
AGENTS_MD = os.path.join(_PKG_PARENT, "AGENTS.md")


def _read_file(path):
    """读取文档内容"""
    with open(path, encoding="utf-8") as f:
        return f.read()


# ============================================
# 1. docs/cli_reference.md 验证
# ============================================


class TestCliReferenceReopenDoc:
    """验证 cli_reference.md 包含 task reopen 命令文档"""

    def test_cli_ref_has_task_reopen_section(self):
        """cli_reference.md 应包含 task reopen 章节"""
        content = _read_file(CLI_REF)
        assert "### `task reopen`" in content, "cli_reference.md 缺少 task reopen 章节"

    def test_cli_ref_has_command_usage(self):
        """应包含命令用法示例"""
        content = _read_file(CLI_REF)
        assert "cw task reopen" in content, "缺少 cw task reopen 命令用法"

    def test_cli_ref_has_reviewer_option(self):
        """应包含 --reviewer 参数说明"""
        content = _read_file(CLI_REF)
        assert "--reviewer" in content, "缺少 --reviewer 参数"

    def test_cli_ref_has_reason_option(self):
        """应包含 --reason 参数说明"""
        content = _read_file(CLI_REF)
        assert "--reason" in content, "缺少 --reason 参数"

    def test_cli_ref_has_sibling_status_check_logic(self):
        """应包含兄弟子任务状态判断逻辑说明"""
        content = _read_file(CLI_REF)
        # 检查是否提到了兄弟子任务状态判断
        assert "兄弟子任务" in content or "兄弟" in content, \
            "缺少兄弟子任务状态判断逻辑说明"

    def test_cli_ref_has_auto_trigger_scenario(self):
        """应包含自动触发场景说明（task_create）"""
        content = _read_file(CLI_REF)
        assert "自动触发" in content, "缺少自动触发场景说明"
        assert "task_create" in content, "缺少 task_create 自动触发说明"

    def test_cli_ref_has_manual_trigger_scenario(self):
        """应包含手动触发场景说明（cw task reopen）"""
        content = _read_file(CLI_REF)
        assert "手动触发" in content, "缺少手动触发场景说明"

    def test_cli_ref_has_i18n_key_list(self):
        """应包含 i18n key 清单"""
        content = _read_file(CLI_REF)
        assert "task_reopen_failed" in content, "缺少 i18n key 清单"
        assert "task_reopen_success" in content, "缺少 i18n key 清单"


# ============================================
# 2. docs/architecture.md 验证
# ============================================


class TestArchitectureReopenDoc:
    """验证 architecture.md 包含任务 reopen 机制章节"""

    def test_arch_has_reopen_section(self):
        """architecture.md 应包含 §8. 任务 reopen 机制章节"""
        content = _read_file(ARCHITECTURE)
        assert "任务 reopen 机制" in content, \
            "architecture.md 缺少任务 reopen 机制章节"

    def test_arch_has_state_transition(self):
        """应包含状态转换说明（review/applied/closed → in_progress）"""
        content = _read_file(ARCHITECTURE)
        assert "in_progress" in content, "缺少状态转换说明"

    def test_arch_has_two_trigger_methods(self):
        """应包含两种触发方式说明"""
        content = _read_file(ARCHITECTURE)
        assert "自动触发" in content, "缺少自动触发说明"
        assert "手动触发" in content, "缺少手动触发说明"

    def test_arch_has_sibling_check_logic(self):
        """应包含兄弟子任务状态检查逻辑"""
        content = _read_file(ARCHITECTURE)
        assert "兄弟子任务" in content or "check_siblings" in content, \
            "缺少兄弟子任务状态检查逻辑"

    def test_arch_has_implementation_reference(self):
        """应包含实现位置引用（db_tasks.py）"""
        content = _read_file(ARCHITECTURE)
        assert "db_tasks.py" in content or "_reopen_parent_chain_if_needed" in content, \
            "缺少实现位置引用"

    def test_arch_has_design_rationale(self):
        """应包含设计理由说明"""
        content = _read_file(ARCHITECTURE)
        assert "设计理由" in content, "缺少设计理由说明"


# ============================================
# 3. AGENTS.md 验证
# ============================================


class TestAgentsMdReopenDoc:
    """验证 AGENTS.md 包含任务 reopen 机制规则"""

    def test_agents_has_reopen_section(self):
        """AGENTS.md 应包含任务 reopen 机制子章节"""
        content = _read_file(AGENTS_MD)
        assert "任务 reopen 机制" in content, \
            "AGENTS.md 缺少任务 reopen 机制子章节"

    def test_agents_has_two_trigger_methods(self):
        """应包含两种触发方式说明"""
        content = _read_file(AGENTS_MD)
        assert "自动触发" in content, "缺少自动触发说明"
        assert "手动触发" in content, "缺少手动触发说明"

    def test_agents_has_sibling_check_logic(self):
        """应包含兄弟子任务状态检查逻辑"""
        content = _read_file(AGENTS_MD)
        assert "兄弟子任务" in content, "缺少兄弟子任务状态检查逻辑"

    def test_agents_has_design_principle(self):
        """应包含设计原则说明"""
        content = _read_file(AGENTS_MD)
        assert "设计原则" in content, "缺少设计原则说明"

    def test_agents_has_architecture_link(self):
        """应包含指向 architecture.md 的链接"""
        content = _read_file(AGENTS_MD)
        assert "architecture.md" in content, "缺少指向 architecture.md 的链接"


# ============================================
# 4. 三文档一致性验证
# ============================================


class TestDocConsistency:
    """验证三个文档之间的术语和逻辑一致"""

    def test_consistent_state_transitions(self):
        """三文档应一致描述 review/applied/closed → in_progress 转换"""
        docs = [_read_file(CLI_REF), _read_file(ARCHITECTURE), _read_file(AGENTS_MD)]
        for doc in docs:
            assert "in_progress" in doc, "文档缺少 in_progress 状态"
            assert "closed" in doc, "文档缺少 closed 状态"

    def test_consistent_sibling_logic(self):
        """三文档应一致描述兄弟子任务状态判断逻辑"""
        docs = [_read_file(CLI_REF), _read_file(ARCHITECTURE), _read_file(AGENTS_MD)]
        for doc in docs:
            # 至少有一个文档提到兄弟子任务状态判断
            assert "兄弟" in doc or "sibling" in doc.lower(), \
                "文档缺少兄弟子任务状态判断逻辑"

    def test_consistent_two_trigger_methods(self):
        """三文档应一致描述两种触发方式"""
        docs = [_read_file(CLI_REF), _read_file(ARCHITECTURE), _read_file(AGENTS_MD)]
        for doc in docs:
            assert "自动触发" in doc or "task_create" in doc, \
                "文档缺少自动触发说明"
            assert "手动触发" in doc or "cw task reopen" in doc, \
                "文档缺少手动触发说明"
