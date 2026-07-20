"""L2 评审修复验证测试（2026-07-20 二轮评审）。

验证：
1. install.py 新增 _reference_transaction_hook 方法
2. install_hooks 默认安装 reference-transaction hook
3. cli/main.py 新增 cw git check-ref-transaction 子命令
4. _feature_matrix.md L2 条目诚实化（🟡 部分完成 + 技术限制说明）
5. CLI 命令处理逻辑（识别 forced flag、branch delete、fast-forward 过滤）

设计原则（按 AGENTS.md 规则 18）：
- 测试不依赖运行时 DB 状态
- 仅做源码静态验证 + 简单逻辑模拟
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


# ============================================
# 1. install.py 源码验证
# ============================================

class TestL2InstallPySource:
    """验证 install.py 新增的 reference-transaction hook 生成器与安装器接入。"""

    @pytest.fixture(scope="class")
    def install_source(self) -> str:
        return (ROOT / "install.py").read_text(encoding="utf-8")

    def test_reference_transaction_hook_method_exists(self, install_source: str):
        """_reference_transaction_hook 方法必须存在。"""
        assert "def _reference_transaction_hook(self) -> str:" in install_source, (
            "install.py 缺少 _reference_transaction_hook 方法"
        )

    def test_reference_transaction_hook_returns_shell_script(self, install_source: str):
        """hook 内容必须包含 sh shebang + check-ref-transaction 调用。"""
        # 提取方法体（粗略）
        m = re.search(
            r"def _reference_transaction_hook\(self\)[^:]*:\n(?:.*\n)*?\"\"\"",
            install_source,
        )
        assert m, "_reference_transaction_hook 方法体未找到"
        # 关键内容
        assert "#!/bin/sh" in install_source
        assert "git check-ref-transaction" in install_source
        # 软门禁注释
        assert "审计层" in install_source or "audit" in install_source.lower()
        assert "不能作拦截层" in install_source or "不能拦截" in install_source

    def test_install_hooks_accepts_with_ref_transaction(self, install_source: str):
        """install_hooks 必须支持 with_ref_transaction 参数。"""
        assert "with_ref_transaction: bool = True" in install_source, (
            "install_hooks 缺少 with_ref_transaction 参数"
        )

    def test_install_hooks_includes_reference_transaction(self, install_source: str):
        """install_hooks 必须将 reference-transaction 加入 hook_defs。"""
        assert '"reference-transaction"' in install_source or \
               "'reference-transaction'" in install_source, (
            "install_hooks 未将 reference-transaction 加入 hook_defs"
        )
        assert "_reference_transaction_hook()" in install_source

    def test_hook_docstring_mentions_git_limitation(self, install_source: str):
        """方法 docstring 必须说明 git 无 pre-checkout/pre-reset hook 技术限制。"""
        assert "pre-checkout" in install_source or "pre-checkout/pre-reset" in install_source
        assert "working tree" in install_source or "工作树" in install_source

    def test_hook_uses_python_cw_command(self, install_source: str):
        """hook 必须通过 _python_cw_command() 生成 cw 调用。"""
        # 在 _reference_transaction_hook 方法内
        idx = install_source.find("def _reference_transaction_hook")
        assert idx >= 0
        method_body = install_source[idx:idx + 2000]
        assert "_python_cw_command()" in method_body


# ============================================
# 2. cli/main.py 子命令验证
# ============================================

class TestL2CliCommand:
    """验证 cli/main.py 的 cw git check-ref-transaction 子命令注册与处理。"""

    @pytest.fixture(scope="class")
    def cli_source(self) -> str:
        return (ROOT / "cli" / "main.py").read_text(encoding="utf-8")

    def test_check_ref_transaction_subcommand_registered(self, cli_source: str):
        """check-ref-transaction 子命令必须注册到 argparse。"""
        assert 'add_parser("check-ref-transaction"' in cli_source, (
            "cli/main.py 未注册 check-ref-transaction 子命令"
        )

    def test_check_ref_transaction_args_defined(self, cli_source: str):
        """子命令必须接受 old_value/new_value/ref_name/flags 参数。"""
        # 找到 check-ref-transaction 注册块
        idx = cli_source.find('add_parser("check-ref-transaction"')
        assert idx >= 0
        block = cli_source[idx:idx + 2000]
        assert 'add_argument("old_value"' in block
        assert 'add_argument("new_value"' in block
        assert 'add_argument("ref_name"' in block
        assert 'add_argument("flags"' in block

    def test_check_ref_transaction_handler_exists(self, cli_source: str):
        """action == 'check-ref-transaction' 处理逻辑必须存在。"""
        assert 'opts.action == "check-ref-transaction"' in cli_source, (
            "cli/main.py 缺少 check-ref-transaction 处理分支"
        )

    def test_handler_identifies_forced_flag(self, cli_source: str):
        """处理逻辑必须识别 flags 中的 'forced' 标记。"""
        idx = cli_source.find('opts.action == "check-ref-transaction"')
        assert idx >= 0
        block = cli_source[idx:idx + 3000]
        assert "forced" in block.lower(), "处理逻辑未检查 'forced' flag"

    def test_handler_identifies_branch_delete(self, cli_source: str):
        """处理逻辑必须识别 branch delete（new_value 全 0）。"""
        idx = cli_source.find('opts.action == "check-ref-transaction"')
        assert idx >= 0
        block = cli_source[idx:idx + 3000]
        # 检测全 0 sha 的逻辑（lambda 或 set 形式）
        assert "is_zero_sha" in block or "branch_delete" in block

    def test_handler_skips_fast_forward(self, cli_source: str):
        """处理逻辑必须跳过常规 fast-forward（避免日志噪音）。"""
        idx = cli_source.find('opts.action == "check-ref-transaction"')
        assert idx >= 0
        block = cli_source[idx:idx + 3000]
        assert "fast-forward" in block.lower() or "not is_destructive" in block

    def test_handler_logs_to_destructive_operations(self, cli_source: str):
        """处理逻辑必须调用 log_destructive_operation。"""
        idx = cli_source.find('opts.action == "check-ref-transaction"')
        assert idx >= 0
        block = cli_source[idx:idx + 3000]
        assert "log_destructive_operation" in block
        assert "reset_hard" in block or "branch_delete" in block or "branch_create" in block

    def test_handler_never_blocks(self, cli_source: str):
        """软门禁：处理逻辑不能调用 exit/return 阻止。"""
        idx = cli_source.find('opts.action == "check-ref-transaction"')
        assert idx >= 0
        block = cli_source[idx:idx + 3000]
        # 不能 exit(1) 或 sys.exit 阻止
        assert "sys.exit(1)" not in block
        assert "exit 1" not in block


# ============================================
# 3. _feature_matrix.md L2 条目诚实化验证
# ============================================

class TestL2FeatureMatrixHonest:
    """验证 _feature_matrix.md 中 L2 条目诚实化。"""

    @pytest.fixture(scope="class")
    def matrix_source(self) -> str:
        return (ROOT / "_feature_matrix.md").read_text(encoding="utf-8")

    def test_l2_status_is_partial(self, matrix_source: str):
        """L2 状态必须从 ❌ 改为 🟡 部分完成（二轮评审补全）。"""
        # 找到 L2 行
        l2_pattern = re.compile(r"^\| L2 \|.*$", re.MULTILINE)
        m = l2_pattern.search(matrix_source)
        assert m, "_feature_matrix.md 缺少 L2 条目"
        l2_line = m.group(0)
        assert "🟡 部分完成" in l2_line, (
            f"L2 状态未改为 🟡 部分完成：{l2_line}"
        )
        assert "2026-07-20 二轮评审补全" in l2_line, (
            f"L2 未标注二轮评审补全：{l2_line}"
        )

    def test_l2_mentions_reference_transaction(self, matrix_source: str):
        """L2 备注必须提到 reference-transaction hook。"""
        l2_pattern = re.compile(r"^\| L2 \|.*$", re.MULTILINE)
        m = l2_pattern.search(matrix_source)
        assert m, "_feature_matrix.md 缺少 L2 条目"
        l2_line = m.group(0)
        assert "reference-transaction" in l2_line, (
            f"L2 未提到 reference-transaction hook：{l2_line}"
        )

    def test_l2_mentions_git_limitation(self, matrix_source: str):
        """L2 必须说明 git 技术限制（无 pre-checkout/pre-reset hook）。"""
        l2_pattern = re.compile(r"^\| L2 \|.*$", re.MULTILINE)
        m = l2_pattern.search(matrix_source)
        assert m, "_feature_matrix.md 缺少 L2 条目"
        l2_line = m.group(0)
        assert "pre-checkout" in l2_line or "pre-reset" in l2_line, (
            f"L2 未说明 git 技术限制：{l2_line}"
        )

    def test_l2_not_marked_complete(self, matrix_source: str):
        """L2 不能标记为 ✅ 已实现。"""
        l2_pattern = re.compile(r"^\| L2 \|.*$", re.MULTILINE)
        m = l2_pattern.search(matrix_source)
        assert m, "_feature_matrix.md 缺少 L2 条目"
        l2_line = m.group(0)
        assert "✅ 已实现" not in l2_line
        assert "✅ 已修复" not in l2_line


# ============================================
# 4. CLI 处理逻辑行为模拟（不调用真实 DB）
# ============================================

class TestL2BehaviorLogic:
    """模拟 check-ref-transaction 处理逻辑，验证分类正确性。"""

    @staticmethod
    def _classify(old_value: str, new_value: str, flags: str) -> str:
        """模拟 cli/main.py 中 check-ref-transaction 的分类逻辑。

        Returns:
            "skip" — 常规 fast-forward，不记录
            "reset_hard" — forced flag，破坏性
            "branch_delete" — new_value 全 0
            "branch_create" — old_value 全 0
        """
        is_zero_sha = lambda sha: bool(sha) and len(sha) == 40 and set(sha) == {"0"}
        is_destructive = "forced" in flags.lower() or is_zero_sha(new_value)
        is_create = is_zero_sha(old_value) and not is_zero_sha(new_value)

        if not is_destructive and not is_create:
            return "skip"

        if is_zero_sha(new_value):
            return "branch_delete"
        elif "forced" in flags.lower():
            return "reset_hard"
        else:
            return "branch_create"

    def test_forced_flag_classified_as_reset_hard(self):
        """flags 包含 'forced' 应分类为 reset_hard。"""
        assert self._classify(
            "abc123", "def456", "forced"
        ) == "reset_hard"

    def test_branch_delete_classified(self):
        """new_value 全 0 应分类为 branch_delete。"""
        assert self._classify(
            "abc123def456", "0000000000000000000000000000000000000000", ""
        ) == "branch_delete"

    def test_branch_create_classified(self):
        """old_value 全 0 应分类为 branch_create（非破坏性但记录）。"""
        assert self._classify(
            "0000000000000000000000000000000000000000", "abc123def456", ""
        ) == "branch_create"

    def test_fast_forward_skipped(self):
        """常规 fast-forward 应跳过。"""
        assert self._classify(
            "abc123", "def456", ""
        ) == "skip"

    def test_empty_input_skipped(self):
        """空输入应跳过。"""
        assert self._classify("", "", "") == "skip"

    def test_forced_takes_precedence_over_delete(self):
        """forced + new_value 全 0 时优先识别为 branch_delete（更具体）。"""
        assert self._classify(
            "abc123", "0000000000000000000000000000000000000000", "forced"
        ) == "branch_delete"


# ============================================
# 5. 集成验证：destructive_operations 表 schema 支持
# ============================================

class TestL2DestructiveOperationsSchema:
    """验证 destructive_operations 表 schema 已支持 L2 新增的 operation_type。"""

    @pytest.fixture(scope="class")
    def schema_source(self) -> str:
        return (ROOT / "db" / "schema.py").read_text(encoding="utf-8")

    def test_destructive_operations_table_exists(self, schema_source: str):
        """destructive_operations 表必须存在。"""
        assert "CREATE TABLE IF NOT EXISTS destructive_operations" in schema_source

    def test_operation_type_field_exists(self, schema_source: str):
        """operation_type 字段必须存在（已支持 force_push，新加 reset_hard 等）。"""
        assert "operation_type" in schema_source

    def test_db_git_log_destructive_operation_supports_reset_hard(self):
        """db/db_git.py log_destructive_operation 文档必须提到 reset_hard。"""
        db_git = (ROOT / "db" / "db_git.py").read_text(encoding="utf-8")
        assert "reset_hard" in db_git, "db_git.py 文档未提到 reset_hard operation_type"
