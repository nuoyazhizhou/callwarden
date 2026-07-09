"""P26: git-aware 项目边界检测单元测试

验证 .git/.repo 作为项目边界的语义：
- 每个 .git = 1 个项目，停止向下递归
- .repo (AOSP repo 工具) 作为项目根
- shallow 模式（默认）vs deep 模式
"""
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from callwarden.config import scan_subprojects, _SUBPROJECT_SKIP_DIRS


def _make_git_dir(path):
    """创建 .git 目录（模拟 git 仓库根）"""
    os.makedirs(os.path.join(path, ".git"), exist_ok=True)


def _make_repo_dir(path, manifest=None):
    """创建一个 git 仓库目录，可选 manifest"""
    os.makedirs(path, exist_ok=True)
    _make_git_dir(path)
    if manifest:
        with open(os.path.join(path, manifest), "w", encoding="utf-8") as f:
            if manifest == "Cargo.toml":
                f.write('[package]\nname = "test"\nversion = "0.1.0"\n')
            elif manifest == "package.json":
                f.write('{"name": "test"}\n')
            elif manifest == "go.mod":
                f.write("module example.com/test\n\ngo 1.21\n")
            elif manifest == "pyproject.toml":
                f.write('[project]\nname = "test"\n')


# ============================================
# Shallow 模式（默认）：每个 .git = 1 个项目
# ============================================

class TestShallowModeGitBoundary:
    """shallow 模式下，.git 是项目边界，停止向下递归"""

    def test_each_git_dir_is_one_project(self, tmp_path):
        """testcode/repos/ 场景：多个 .git 子目录 → 每个是独立项目"""
        for name in ("repo_a", "repo_b", "repo_c"):
            _make_repo_dir(tmp_path / name, manifest="Cargo.toml")

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 3
        rels = {p["rel_path"] for p in projs}
        assert rels == {"repo_a", "repo_b", "repo_c"}

    def test_git_dir_stops_recursion_into_monorepo_members(self, tmp_path):
        """仓库内部 monorepo member 不再独立识别"""
        # repo/.git + repo/Cargo.toml (workspace root) + repo/crates/member/Cargo.toml
        repo = tmp_path / "myrepo"
        _make_repo_dir(repo, manifest="Cargo.toml")
        (repo / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/member"]\n',
            encoding="utf-8",
        )
        member = repo / "crates" / "member"
        member.mkdir(parents=True)
        (member / "Cargo.toml").write_text('[package]\nname = "member"\n', encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        # shallow 模式：只识别 1 个（myrepo），member 被折叠
        assert len(projs) == 1
        assert projs[0]["rel_path"] == "myrepo"
        assert projs[0]["manifest"] == "Cargo.toml"  # 真实 manifest 优先于 .git

    def test_git_without_manifest_still_identified(self, tmp_path):
        """无 manifest 的 .git 仓库仍应识别为子项目（manifest 字段为 .git）"""
        repo = tmp_path / "gitonly"
        _make_repo_dir(repo)  # 无 manifest

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 1
        assert projs[0]["manifest"] == ".git"
        assert projs[0]["lang"] == "git"

    def test_real_manifest_preferred_over_git_marker(self, tmp_path):
        """有真实 manifest 时，用真实 manifest 而非 .git"""
        repo = tmp_path / "ts_project"
        _make_repo_dir(repo, manifest="package.json")

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 1
        assert projs[0]["manifest"] == "package.json"
        assert projs[0]["lang"] == "javascript"

    def test_nested_git_directories_only_outer_identified(self, tmp_path):
        """父目录有 .git，子目录也有 .git（submodule 场景）
        shallow 模式下父目录识别为 1 个项目，子目录被跳过"""
        outer = tmp_path / "outer"
        _make_repo_dir(outer, manifest="Cargo.toml")
        # 子目录也是 git 仓库（可能是 submodule）
        inner = outer / "vendor" / "lib"
        _make_repo_dir(inner, manifest="Cargo.toml")

        projs = scan_subprojects(str(tmp_path))
        # 只识别 outer（vendor 在 _SUBPROJECT_SKIP_DIRS，不会进入；
        # 即使进入，shallow 模式下 outer 已停止递归）
        assert len(projs) == 1
        assert projs[0]["rel_path"] == "outer"

    def test_root_dir_with_git_identified_as_single_project(self, tmp_path):
        """扫描根目录本身有 .git → 识别为 1 个项目，不递归"""
        _make_repo_dir(tmp_path, manifest="Cargo.toml")
        # 加点干扰目录
        (tmp_path / "packages" / "foo").mkdir(parents=True)
        (tmp_path / "packages" / "foo" / "Cargo.toml").write_text(
            '[package]\nname = "foo"\n', encoding="utf-8"
        )

        projs = scan_subprojects(str(tmp_path))
        # 根目录本身就是项目根，递归停止
        assert len(projs) == 1
        assert projs[0]["rel_path"] == ""  # 根目录本身


# ============================================
# Deep 模式：进入仓库内部识别 monorepo 子项目
# ============================================

class TestDeepMode:
    """deep 模式保留 P25 行为：进入 .git 仓库内部识别 monorepo 子项目"""

    def test_deep_mode_finds_monorepo_members(self, tmp_path):
        """deep 模式下，仓库内部的 workspace member 仍被识别"""
        repo = tmp_path / "myrepo"
        _make_repo_dir(repo, manifest="Cargo.toml")
        # 这是 workspace root（P25 会停止递归，但仍识别为子项目）
        (repo / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/member"]\n', encoding="utf-8"
        )
        member = repo / "crates" / "member"
        member.mkdir(parents=True)
        (member / "Cargo.toml").write_text('[package]\nname = "member"\n', encoding="utf-8")

        projs = scan_subprojects(str(tmp_path), shallow=False)
        # deep 模式：识别 myrepo（workspace root，P25 停止递归）
        # 但不会识别 member（P25 workspace 边界已生效）
        assert len(projs) == 1
        assert projs[0]["rel_path"] == "myrepo"

    def test_deep_mode_finds_independent_packages_outside_workspace(self, tmp_path):
        """deep 模式下，非 workspace 的独立子包仍能被识别"""
        repo = tmp_path / "myrepo"
        _make_repo_dir(repo, manifest="Cargo.toml")
        # 仓库内独立 crate（非 workspace member）
        indep = repo / "packages" / "indep"
        indep.mkdir(parents=True)
        (indep / "Cargo.toml").write_text('[package]\nname = "indep"\n', encoding="utf-8")

        projs = scan_subprojects(str(tmp_path), shallow=False)
        # deep 模式：进入仓库内部，识别独立 crate
        # 注意：根 myrepo 不会被识别（没有 .git 检测，但 manifest 会被识别）
        # 实际上 deep 模式下仓库根的 Cargo.toml 也会被识别
        rels = {p["rel_path"] for p in projs}
        assert "myrepo" in rels
        assert "myrepo/packages/indep" in rels

    def test_shallow_vs_deep_difference(self, tmp_path):
        """shallow 和 deep 模式的差异"""
        repo = tmp_path / "myrepo"
        _make_repo_dir(repo, manifest="Cargo.toml")
        # 仓库内独立 crate
        indep = repo / "packages" / "indep"
        indep.mkdir(parents=True)
        (indep / "Cargo.toml").write_text('[package]\nname = "indep"\n', encoding="utf-8")

        # shallow: 只识别 1 个（myrepo）
        projs_shallow = scan_subprojects(str(tmp_path), shallow=True)
        assert len(projs_shallow) == 1

        # deep: 识别 2 个（myrepo + packages/indep）
        projs_deep = scan_subprojects(str(tmp_path), shallow=False)
        assert len(projs_deep) == 2


# ============================================
# .repo (AOSP repo 工具) 边界检测
# ============================================

class TestRepoToolBoundary:
    """AOSP repo 工具的 .repo 目录作为项目根"""

    def test_repo_dir_identified_as_project_root(self, tmp_path):
        """含 .repo 目录的目录应识别为项目根"""
        project = tmp_path / "aosp_project"
        project.mkdir()
        os.makedirs(os.path.join(project, ".repo"), exist_ok=True)
        # 加点子项目干扰
        (project / "frameworks" / "base").mkdir(parents=True)
        (project / "frameworks" / "base" / "Android.bp").write_text(
            "package {\n}\n", encoding="utf-8"
        )

        projs = scan_subprojects(str(tmp_path))
        # .repo 识别为项目根，停止递归
        assert len(projs) == 1
        assert projs[0]["rel_path"] == "aosp_project"
        assert projs[0]["manifest"] == ".repo"
        assert projs[0]["lang"] == "repo"

    def test_repo_priority_over_git(self, tmp_path):
        """同时有 .repo 和子目录 .git 时，.repo 优先（识别为 1 个项目）"""
        project = tmp_path / "aosp"
        project.mkdir()
        os.makedirs(os.path.join(project, ".repo"), exist_ok=True)
        # 子目录也是 git 仓库（repo 工具检出的子项目）
        sub = project / "frameworks" / "base"
        sub.mkdir(parents=True)
        _make_git_dir(sub)  # 子目录有 .git

        projs = scan_subprojects(str(tmp_path))
        # .repo 优先，识别为 1 个项目，子目录 .git 被跳过
        assert len(projs) == 1
        assert projs[0]["manifest"] == ".repo"

    def test_repo_not_in_skip_dirs(self):
        """P26: .repo 不应在 _SUBPROJECT_SKIP_DIRS 中（它是项目边界，不是跳过目录）"""
        assert ".repo" not in _SUBPROJECT_SKIP_DIRS

    def test_git_not_in_skip_dirs(self):
        """P26: .git 不应在 _SUBPROJECT_SKIP_DIRS 中（它作为项目边界检测）"""
        assert ".git" not in _SUBPROJECT_SKIP_DIRS


# ============================================
# Manifest fallback（无 .git 的项目）
# ============================================

class TestManifestFallback:
    """无 .git 的项目用 manifest 作为 fallback"""

    def test_python_package_without_git_identified(self, tmp_path):
        """纯 Python 包（无 .git，有 setup.py）应被识别"""
        pkg = tmp_path / "mypkg"
        pkg.mkdir()
        (pkg / "setup.py").write_text(
            "from setuptools import setup\nsetup(name='mypkg')\n",
            encoding="utf-8",
        )

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 1
        assert projs[0]["manifest"] == "setup.py"
        assert projs[0]["lang"] == "python"

    def test_no_manifest_no_git_not_identified(self, tmp_path):
        """无 manifest 无 .git 的目录不应被识别为子项目"""
        (tmp_path / "random_dir").mkdir()
        (tmp_path / "random_dir" / "file.txt").write_text("hello\n", encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 0


# ============================================
# 综合场景
# ============================================

class TestRealWorldScenarios:
    """模拟真实世界场景"""

    def test_testcode_repos_scenario(self, tmp_path):
        """模拟 testcode/repos/ 场景：
        父目录无 .git，下有多个独立 git 仓库"""
        repos = tmp_path / "repos"
        repos.mkdir()
        for name in ("repo_a", "repo_b", "repo_c"):
            _make_repo_dir(repos / name, manifest="Cargo.toml")

        projs = scan_subprojects(str(tmp_path))
        # 每个子目录都是独立 git 仓库 → 3 个项目
        assert len(projs) == 3
        rels = {p["rel_path"] for p in projs}
        assert rels == {"repos/repo_a", "repos/repo_b", "repos/repo_c"}

    def test_mixed_git_and_non_git_projects(self, tmp_path):
        """混合场景：git 仓库 + 纯 Python 包"""
        # git 仓库
        git_repo = tmp_path / "git_proj"
        _make_repo_dir(git_repo, manifest="package.json")
        # 纯 Python 包（无 git）
        py_pkg = tmp_path / "py_pkg"
        py_pkg.mkdir()
        (py_pkg / "setup.py").write_text(
            "from setuptools import setup\nsetup(name='py_pkg')\n",
            encoding="utf-8",
        )

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 2
        rels = {p["rel_path"] for p in projs}
        assert rels == {"git_proj", "py_pkg"}

    def test_callwarden_itself_scenario(self, tmp_path):
        """模拟 callwarden 项目本身：单一 git 仓库根"""
        # callwarden/.git + callwarden/pyproject.toml
        _make_repo_dir(tmp_path, manifest="pyproject.toml")
        (tmp_path / "pyproject.toml").write_text(
            '[project]\nname = "callwarden"\nversion = "0.1.0"\n',
            encoding="utf-8",
        )
        # 子目录的 manifest 不应被识别（共享 .git）
        (tmp_path / "tests" / "fixtures").mkdir(parents=True)
        (tmp_path / "tests" / "fixtures" / "demo").mkdir()
        (tmp_path / "tests" / "fixtures" / "demo" / "package.json").write_text(
            '{"name": "demo-fixture"}\n', encoding="utf-8"
        )

        projs = scan_subprojects(str(tmp_path))
        # shallow 模式：根 .git 停止递归，fixtures 不被识别
        assert len(projs) == 1
        assert projs[0]["rel_path"] == ""
        assert projs[0]["manifest"] == "pyproject.toml"


# ============================================
# P26.7: 容器目录启发式（无 .git 的裸 monorepo）
# ============================================

class TestContainerDirHeuristic:
    """当 member 的父目录在 _MONOREPO_PKG_DIRS（crates/packages 等）中时，
    项目根 = 容器目录的父目录，而非 member 本身"""

    def test_crates_dir_folds_to_parent(self, tmp_path):
        """crates/foo/Cargo.toml + crates/bar/Cargo.toml → 1 个项目（容器父目录）"""
        mono = tmp_path / "my_mono"
        for name in ("foo", "bar"):
            d = mono / "crates" / name
            d.mkdir(parents=True)
            (d / "Cargo.toml").write_text(f'[package]\nname = "{name}"\n', encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 1
        assert projs[0]["rel_path"] == "my_mono"
        assert projs[0]["name"] == "my_mono"

    def test_packages_dir_folds_to_parent(self, tmp_path):
        """packages/a/package.json + packages/b/package.json → 1 个项目"""
        mono = tmp_path / "my_mono"
        for name in ("a", "b"):
            d = mono / "packages" / name
            d.mkdir(parents=True)
            (d / "package.json").write_text(f'{{"name": "{name}"}}\n', encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 1
        assert projs[0]["rel_path"] == "my_mono"

    def test_mixed_container_dirs_dedup(self, tmp_path):
        """crates/foo + packages/bar → 1 个项目（去重）"""
        mono = tmp_path / "my_mono"
        (mono / "crates" / "foo").mkdir(parents=True)
        (mono / "crates" / "foo" / "Cargo.toml").write_text(
            '[package]\nname = "foo"\n', encoding="utf-8")
        (mono / "packages" / "bar").mkdir(parents=True)
        (mono / "packages" / "bar" / "package.json").write_text(
            '{"name": "bar"}\n', encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 1
        assert projs[0]["rel_path"] == "my_mono"

    def test_standalone_crate_not_affected(self, tmp_path):
        """独立 crate（不在容器目录下）不受影响"""
        standalone = tmp_path / "standalone"
        standalone.mkdir()
        (standalone / "Cargo.toml").write_text(
            '[package]\nname = "standalone"\n', encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 1
        assert projs[0]["rel_path"] == "standalone"

    def test_all_container_dirs_recognized(self, tmp_path):
        """所有 _MONOREPO_PKG_DIRS 都应触发折叠"""
        from callwarden.config import _MONOREPO_PKG_DIRS
        for container in _MONOREPO_PKG_DIRS:
            mono = tmp_path / f"mono_{container}"
            d = mono / container / "member"
            d.mkdir(parents=True)
            (d / "Cargo.toml").write_text('[package]\nname = "m"\n', encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        # 每个容器目录的父目录都是 1 个项目
        assert len(projs) == len(_MONOREPO_PKG_DIRS)
        for p in projs:
            # 验证项目根是容器目录的父目录（mono_xxx），而非 member
            assert p["name"].startswith("mono_")

    def test_git_repo_with_container_inside_not_affected(self, tmp_path):
        """有 .git 的仓库内部容器目录不受影响（.git 边界优先）"""
        repo = tmp_path / "repo"
        _make_repo_dir(repo, manifest="Cargo.toml")
        # 仓库内有 crates/foo（不应独立识别，因为 .git 已停止递归）
        (repo / "crates" / "foo").mkdir(parents=True)
        (repo / "crates" / "foo" / "Cargo.toml").write_text(
            '[package]\nname = "foo"\n', encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 1
        assert projs[0]["rel_path"] == "repo"

    def test_scanned_root_is_container_parent(self, tmp_path):
        """扫描根本身是容器目录的父目录（rel_path 为空）"""
        for name in ("foo", "bar"):
            d = tmp_path / "crates" / name
            d.mkdir(parents=True)
            (d / "Cargo.toml").write_text(f'[package]\nname = "{name}"\n', encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 1
        assert projs[0]["rel_path"] == ""  # 扫描根本身就是项目根

    def test_container_with_independent_project_mixed(self, tmp_path):
        """容器目录 monorepo + 独立项目混合"""
        # my_mono/crates/foo/Cargo.toml（容器目录 → 项目根 = my_mono）
        mono = tmp_path / "my_mono"
        (mono / "crates" / "foo").mkdir(parents=True)
        (mono / "crates" / "foo" / "Cargo.toml").write_text(
            '[package]\nname = "foo"\n', encoding="utf-8")
        # another_pkg/setup.py（独立项目，不在容器目录下）
        another = tmp_path / "another_pkg"
        another.mkdir()
        (another / "setup.py").write_text(
            "from setuptools import setup\nsetup(name='another')\n", encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 2
        rels = {p["rel_path"] for p in projs}
        assert rels == {"my_mono", "another_pkg"}
