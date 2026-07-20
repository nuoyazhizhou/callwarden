"""A15 评审修复验证测试（2026-07-20 二轮评审）。

验证：
1. analyzers/ignore_spec.py 接入 pathspec 作为主路径
2. pathspec 不可用时降级到自研实现（向后兼容）
3. 字符类 [abc]/[a-z] 支持（pathspec 主路径独有）
4. 尾随空格保留（除非行末 \\ 转义）
5. 目录剪枝后 negation 恢复（pathspec last-match-wins）
6. pyproject.toml/requirements.txt/install.py 加入 pathspec 依赖
7. _feature_matrix.md A15 条目状态改为 ✅ 已修复
8. IgnoreMatcher 公开 API 向后兼容（global_rules / dir_rules / is_ignored / filter_files）

设计原则（按 AGENTS.md 规则 18）：
- 测试不依赖真实 workspace 文件系统（除 1 个 tmp_path 集成测试外）
- 优先做源码静态验证 + pathspec 直接验证
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _has_pathspec() -> bool:
    """检查 pathspec 是否可用。"""
    try:
        import pathspec  # noqa: F401
        return True
    except ImportError:
        return False


# ============================================
# 1. pathspec 依赖接入验证
# ============================================

class TestA15PathspecDependency:
    """验证 pathspec 已加入核心依赖声明。"""

    def test_pyproject_toml_includes_pathspec(self):
        """pyproject.toml 必须包含 pathspec 依赖。"""
        content = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
        assert "pathspec" in content, "pyproject.toml 未声明 pathspec 依赖"
        # 至少出现在 dependencies 和 core 中
        assert content.count("pathspec") >= 3, (
            "pathspec 应至少出现在 dependencies / all / core 三处"
        )

    def test_requirements_txt_includes_pathspec(self):
        """requirements.txt 必须包含 pathspec 依赖。"""
        content = (ROOT / "requirements.txt").read_text(encoding="utf-8")
        assert "pathspec" in content, "requirements.txt 未声明 pathspec 依赖"

    def test_install_py_core_packages_includes_pathspec(self):
        """install.py CORE_PACKAGES 必须包含 pathspec。"""
        content = (ROOT / "install.py").read_text(encoding="utf-8")
        assert 'PackageSpec("pathspec"' in content, (
            "install.py CORE_PACKAGES 未加入 pathspec"
        )
        # A15 标记
        assert "A15" in content


# ============================================
# 2. analyzers/ignore_spec.py 接入 pathspec
# ============================================

class TestA15IgnoreSpecPathspecIntegration:
    """验证 analyzers/ignore_spec.py 接入 pathspec。"""

    @pytest.fixture(scope="class")
    def source(self) -> str:
        return (ROOT / "analyzers" / "ignore_spec.py").read_text(encoding="utf-8")

    def test_pathspec_import_with_fallback(self, source: str):
        """pathspec 导入必须用 try/except，缺失时降级。"""
        assert "try:" in source and "import pathspec" in source
        assert "_HAS_PATHSPEC" in source
        assert "except ImportError" in source

    def test_uses_pathspec_pathspec_from_lines(self, source: str):
        """必须使用 pathspec.PathSpec.from_lines('gitignore', ...)。"""
        assert "pathspec.PathSpec.from_lines" in source
        assert "'gitignore'" in source

    def test_global_spec_attribute_exists(self, source: str):
        """IgnoreMatcher 必须有 _global_spec 属性（pathspec.PathSpec 实例）。"""
        assert "_global_spec" in source
        assert "_dir_specs" in source

    def test_is_ignored_uses_pathspec_when_available(self, source: str):
        """is_ignored 必须优先调用 _is_ignored_pathspec。"""
        assert "_is_ignored_pathspec" in source
        assert "_is_ignored_legacy" in source
        # 主路径分支
        assert "if _HAS_PATHSPEC:" in source

    def test_pattern_include_used_for_negation(self, source: str):
        """pathspec 路径必须用 pattern.include 判断 negation。"""
        assert "pat.include" in source or "pattern.include" in source

    def test_load_ignore_lines_preserves_trailing_spaces(self, source: str):
        """_load_ignore_lines 必须保留尾随空格（splitlines 保留行内空格）。"""
        assert "_load_ignore_lines" in source
        assert "splitlines()" in source

    def test_parse_ignore_line_handles_trailing_backslash(self, source: str):
        """parse_ignore_line 必须处理行末 \\ 转义（保留尾随空格）。"""
        # 检查解析逻辑中是否区分 "行末 \" 和 "无 \"（用 raw string 避免 Python 转义干扰）
        # 源码中实际写法是 endswith("\\")，对应字面字符串 endswith("\")
        assert r'endswith("\\")' in source or r"endswith('\\\\')" in source, (
            "parse_ignore_line 必须用 endswith('\\\\') 检测行末 \\ 转义"
        )


# ============================================
# 3. 字符类支持（pathspec 主路径，集成测试）
# ============================================

@pytest.mark.skipif(
    not _has_pathspec(),
    reason="pathspec 未安装，跳过字符类测试"
)
class TestA15CharacterClassSupport:
    """验证 pathspec 主路径支持字符类 [abc]/[a-z]/[!abc]。

    自研 fallback 实现不支持字符类，仅 pathspec 主路径支持。
    """

    def test_character_class_positive_match(self):
        """[abc].py 应匹配 a.py / b.py / c.py。"""
        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher("/fake/root")
        m.add_default_rules(["[abc].py"])
        assert m.is_ignored("a.py")
        assert m.is_ignored("b.py")
        assert m.is_ignored("c.py")
        assert not m.is_ignored("d.py")
        assert not m.is_ignored("abc.py")  # 字符类只匹配单字符

    def test_character_class_range_match(self):
        """[a-c].py 应匹配 a.py / b.py / c.py。"""
        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher("/fake/root")
        m.add_default_rules(["[a-c].py"])
        assert m.is_ignored("a.py")
        assert m.is_ignored("b.py")
        assert m.is_ignored("c.py")
        assert not m.is_ignored("d.py")

    def test_character_class_negated_match(self):
        """[!abc].py 应匹配 d.py 但不匹配 a.py。"""
        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher("/fake/root")
        m.add_default_rules(["[!abc].py"])
        assert m.is_ignored("d.py")
        assert m.is_ignored("e.py")
        assert not m.is_ignored("a.py")
        assert not m.is_ignored("b.py")
        assert not m.is_ignored("c.py")


# ============================================
# 4. 尾随空格语义（pathspec 主路径）
# ============================================

@pytest.mark.skipif(
    not _has_pathspec(),
    reason="pathspec 未安装，跳过尾随空格测试"
)
class TestA15TrailingSpaceSemantics:
    """验证 pathspec 主路径保留尾随空格语义（除非 \\ 转义）。

    gitignore 规范：尾随空格默认被去除，除非行末用 \\ 转义。
    """

    def test_trailing_space_default_ignored(self, tmp_path: Path):
        """行末无 \\ 时，尾随空格默认被去除。"""
        gitignore = tmp_path / ".gitignore"
        # "foo.txt   " 行末有 3 个空格，应等价于 "foo.txt"
        gitignore.write_text("foo.txt   \n", encoding="utf-8")
        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher(str(tmp_path))
        m.load_workspace_ignores()
        # "foo.txt" 应被忽略（尾随空格被去除后等价于 "foo.txt"）
        assert m.is_ignored("foo.txt")

    def test_trailing_space_escaped_preserved(self, tmp_path: Path):
        """行末 \\ 转义时，尾随空格被保留（匹配带空格的文件名）。"""
        gitignore = tmp_path / ".gitignore"
        # "foo.txt\\ \\ \\ " 表示 "foo.txt   "（带 3 个空格的文件名）
        # pathspec 支持 \\ 转义尾随空格
        gitignore.write_text("foo.txt\\ \\ \\ \n", encoding="utf-8")
        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher(str(tmp_path))
        m.load_workspace_ignores()
        # 带空格的文件名应被忽略
        assert m.is_ignored("foo.txt   ")
        # 不带空格的不应被忽略（因为模式是带空格的）
        assert not m.is_ignored("foo.txt")


# ============================================
# 5. 目录剪枝后 negation 恢复
# ============================================

@pytest.mark.skipif(
    not _has_pathspec(),
    reason="pathspec 未安装，跳过 negation 恢复测试"
)
class TestA15NegationAfterPrune:
    """验证 pathspec last-match-wins 语义（覆盖父目录规则的 negation 恢复）。

    gitignore 规范：
    - 父 .gitignore: "*.log" → 忽略所有 .log 文件（非整体目录忽略）
    - 子目录 .gitignore: "!keep.log" → 取消忽略 src/keep.log
    - 子目录 negation 应当恢复父目录规则的忽略

    注意：git 的真实行为是"父目录被整体忽略后子目录 .gitignore 不会被读取"。
    例如 build/ 整体忽略后，build/.gitignore 不会被加载。所以测试场景用
    *.log（文件模式）而非 build/（目录模式），让子目录还能被读取。
    """

    def test_subdir_negation_overrides_parent(self, tmp_path: Path):
        """子目录 .gitignore 的 ! 规则应取消父目录文件忽略（非整体目录忽略）。"""
        # 父目录 .gitignore：忽略所有 *.log 文件
        (tmp_path / ".gitignore").write_text("*.log\n", encoding="utf-8")
        # 子目录 src/.gitignore：恢复 src/keep.log
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / ".gitignore").write_text("!keep.log\n", encoding="utf-8")

        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher(str(tmp_path))
        m.load_workspace_ignores()
        # 根目录 *.log 被忽略
        assert m.is_ignored("debug.log"), "*.log 应被忽略"
        # src/ 目录未被父规则整体忽略，子目录 .gitignore 会被加载
        # src/foo.log 应被忽略（父目录 *.log 规则）
        assert m.is_ignored("src/foo.log"), "src/foo.log 应被父规则 *.log 忽略"
        # src/keep.log 应被恢复（子目录 ! 规则）
        assert not m.is_ignored("src/keep.log"), (
            "子目录 !keep.log 应恢复 src/keep.log"
        )

    def test_pruned_dir_subdir_gitignore_not_loaded(self, tmp_path: Path):
        """git 真实语义：父目录被整体忽略后子目录 .gitignore 不会被读取。

        build/ 整体忽略 → git 不进入 build/ 目录 → build/.gitignore 不会被加载
        → build/keep.txt 不能被 ! 规则恢复。这是 git 官方行为。
        """
        # 父目录 .gitignore：整体忽略 build/
        (tmp_path / ".gitignore").write_text("build/\n", encoding="utf-8")
        # 子目录 build/.gitignore：尝试恢复 keep.txt（但应无效）
        build_dir = tmp_path / "build"
        build_dir.mkdir()
        (build_dir / ".gitignore").write_text("!keep.txt\n", encoding="utf-8")

        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher(str(tmp_path))
        m.load_workspace_ignores()
        # build/ 整体被忽略
        assert m.is_ignored("build", is_dir=True), "build/ 应被父规则忽略"
        # build/foo 应被忽略
        assert m.is_ignored("build/foo"), "build/foo 应被忽略"
        # build/keep.txt 也应被忽略（git 不会读 build/.gitignore，所以 ! 无效）
        assert m.is_ignored("build/keep.txt"), (
            "git 真实行为：父目录被整体忽略后子目录 .gitignore 不被读取，"
            "build/keep.txt 应被忽略"
        )


# ============================================
# 6. _feature_matrix.md A15 状态验证
# ============================================

class TestA15FeatureMatrixStatus:
    """验证 _feature_matrix.md A15 条目状态。"""

    @pytest.fixture(scope="class")
    def matrix(self) -> str:
        return (ROOT / "_feature_matrix.md").read_text(encoding="utf-8")

    def test_a15_status_is_fixed(self, matrix: str):
        """A15 状态必须从 🟡 部分完成 改为 ✅ 已修复。"""
        pattern = re.compile(r"^\| A15 \|.*$", re.MULTILINE)
        m = pattern.search(matrix)
        assert m, "_feature_matrix.md 缺少 A15 条目"
        line = m.group(0)
        assert "✅ 已修复" in line, f"A15 状态未改为 ✅ 已修复：{line}"
        assert "2026-07-20 二轮评审补全" in line

    def test_a15_mentions_pathspec(self, matrix: str):
        """A15 备注必须提到 pathspec 库。"""
        pattern = re.compile(r"^\| A15 \|.*$", re.MULTILINE)
        m = pattern.search(matrix)
        assert m, "_feature_matrix.md 缺少 A15 条目"
        line = m.group(0)
        assert "pathspec" in line

    def test_a15_mentions_character_class(self, matrix: str):
        """A15 备注必须提到字符类支持。"""
        pattern = re.compile(r"^\| A15 \|.*$", re.MULTILINE)
        m = pattern.search(matrix)
        assert m, "_feature_matrix.md 缺少 A15 条目"
        line = m.group(0)
        assert "字符类" in line or "character class" in line.lower()

    def test_a15_not_partial(self, matrix: str):
        """A15 状态不能是 🟡 部分完成。"""
        pattern = re.compile(r"^\| A15 \|.*$", re.MULTILINE)
        m = pattern.search(matrix)
        assert m, "_feature_matrix.md 缺少 A15 条目"
        line = m.group(0)
        assert "🟡 部分完成" not in line


# ============================================
# 7. IgnoreMatcher 公开 API 向后兼容
# ============================================

class TestA15IgnoreMatcherBackwardCompat:
    """验证 IgnoreMatcher 公开 API 向后兼容（外部代码不破坏）。"""

    def test_global_rules_attribute_exists(self):
        """global_rules 属性必须存在（用于 source 追溯）。"""
        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher("/fake/root")
        assert hasattr(m, "global_rules")
        assert m.global_rules == []

    def test_dir_rules_attribute_exists(self):
        """dir_rules 属性必须存在。"""
        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher("/fake/root")
        assert hasattr(m, "dir_rules")
        assert m.dir_rules == {}

    def test_add_default_rules_populates_global_rules(self):
        """add_default_rules 应当填充 global_rules 元数据列表。"""
        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher("/fake/root")
        m.add_default_rules(["*.pyc", "build/"])
        assert len(m.global_rules) == 2

    def test_is_ignored_basic_glob(self):
        """is_ignored 基础 *.pyc 匹配应正常工作。"""
        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher("/fake/root")
        m.add_default_rules(["*.pyc"])
        assert m.is_ignored("foo.pyc")
        assert m.is_ignored("src/lib/utils.pyc")
        assert not m.is_ignored("foo.py")

    def test_filter_files_returns_unignored(self):
        """filter_files 应返回未忽略的文件列表。"""
        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher("/fake/root")
        m.add_default_rules(["*.pyc"])
        result = m.filter_files(["a.py", "b.pyc", "c.py", "d.pyc"])
        assert "a.py" in result
        assert "c.py" in result
        assert "b.pyc" not in result
        assert "d.pyc" not in result

    def test_ignore_rule_class_still_exists(self):
        """IgnoreRule 类必须保留（元数据载体）。"""
        from callwarden.analyzers.ignore_spec import IgnoreRule
        # 必须能构造
        r = IgnoreRule(
            pattern="*.pyc",
            negation=False,
            dir_only=False,
            anchored=False,
            regex=None,
            source="default",
        )
        assert r.pattern == "*.pyc"
        assert r.source == "default"

    def test_parse_ignore_line_still_exists(self):
        """parse_ignore_line 函数必须保留。"""
        from callwarden.analyzers.ignore_spec import parse_ignore_line
        r = parse_ignore_line("*.pyc", source="default")
        assert r is not None
        assert r.pattern == "*.pyc"

    def test_load_ignore_file_still_exists(self):
        """load_ignore_file 函数必须保留。"""
        from callwarden.analyzers.ignore_spec import load_ignore_file
        # 对不存在文件返回空列表
        result = load_ignore_file("/nonexistent/.gitignore")
        assert result == []


# ============================================
# 8. 集成测试：完整 gitignore 场景
# ============================================

@pytest.mark.skipif(
    not _has_pathspec(),
    reason="pathspec 未安装，跳过集成测试"
)
class TestA15IntegrationScenarios:
    """完整 gitignore 场景集成测试。"""

    def test_complex_glob_patterns(self):
        """复杂 ** 与 / 组合场景。"""
        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher("/fake/root")
        m.add_default_rules([
            "docs/**/*.md",      # docs 下任意深度 .md
            "vendor/",           # vendor 目录（任意层级）
            "/build/",           # 仅根目录 build/
            "!docs/keep.md",    # 恢复 docs/keep.md
        ])
        assert m.is_ignored("docs/a.md")
        assert m.is_ignored("docs/sub/b.md")
        assert m.is_ignored("docs/sub/deep/c.md")
        assert not m.is_ignored("docs/keep.md"), "negation 应恢复 keep.md"
        assert not m.is_ignored("README.md")
        assert m.is_ignored("vendor/lib.py")
        assert m.is_ignored("build/output.o")
        # /build/ 锚定根目录，src/build 不应被忽略
        assert not m.is_ignored("src/build/output.o")

    def test_directory_only_pattern(self):
        """dir_only 模式（/ 后缀）只匹配目录。"""
        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher("/fake/root")
        m.add_default_rules(["build/"])
        # 目录
        assert m.is_ignored("build", is_dir=True)
        # 文件 build（无后缀）不应被 dir_only 规则忽略
        # 但 build/ 下的文件应被忽略
        assert m.is_ignored("build/main.o", is_dir=False)

    def test_anchored_root_pattern(self):
        """锚定根目录模式（/ 前缀）只匹配根目录。"""
        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher("/fake/root")
        m.add_default_rules(["/out"])
        assert m.is_ignored("out")
        # src/output 不应被 /out 匹配
        assert not m.is_ignored("src/output")
        assert not m.is_ignored("src/out")

    def test_negation_in_same_file(self):
        """同一文件中的 negation（last-match-wins）。"""
        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher("/fake/root")
        m.add_default_rules([
            "*.log",
            "!important.log",
        ])
        assert m.is_ignored("debug.log")
        assert not m.is_ignored("important.log"), "negation 应恢复 important.log"

    def test_subdirectory_gitignore_scope(self, tmp_path: Path):
        """子目录 .gitignore 仅作用于该目录及子目录。"""
        # 根 .gitignore：忽略 *.tmp
        (tmp_path / ".gitignore").write_text("*.tmp\n", encoding="utf-8")
        # 子目录 src/.gitignore：取消忽略 src/*.tmp（仅 src 下）
        src_dir = tmp_path / "src"
        src_dir.mkdir()
        (src_dir / ".gitignore").write_text("!*.tmp\n", encoding="utf-8")

        from callwarden.analyzers.ignore_spec import IgnoreMatcher
        m = IgnoreMatcher(str(tmp_path))
        m.load_workspace_ignores()
        # 根目录 *.tmp 被忽略
        assert m.is_ignored("foo.tmp")
        # src/*.tmp 被子目录 ! 规则恢复
        assert not m.is_ignored("src/foo.tmp"), "子目录 ! 应恢复 src/foo.tmp"
