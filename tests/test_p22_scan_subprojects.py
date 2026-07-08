"""P22: scan_subprojects 子项目扫描算法测试

验证 cw 能自动识别一个目录下的多个独立子项目根。
"""
from __future__ import annotations

import os
import tempfile

from callwarden.config import scan_subprojects, PROJECT_MANIFESTS


def _make_project(root: str, name: str, manifest: str, content: str = "") -> str:
    """创建一个带清单文件的子项目目录"""
    d = os.path.join(root, name)
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, manifest), "w") as f:
        f.write(content)
    return d


def test_single_go_project():
    """识别单个 Go 项目"""
    with tempfile.TemporaryDirectory() as root:
        _make_project(root, "myapi", "go.mod", "module myapi\n\ngo 1.21\n")
        projects = scan_subprojects(root)
        assert len(projects) == 1
        assert projects[0]["lang"] == "go"
        assert projects[0]["manifest"] == "go.mod"
        assert projects[0]["name"] == "myapi"


def test_multiple_projects_different_langs():
    """识别多个不同语言的子项目"""
    with tempfile.TemporaryDirectory() as root:
        _make_project(root, "api", "go.mod")
        _make_project(root, "cli", "Cargo.toml")
        _make_project(root, "web", "package.json")
        _make_project(root, "svc", "pyproject.toml")
        projects = scan_subprojects(root)
        assert len(projects) == 4
        langs = {p["lang"] for p in projects}
        assert langs == {"go", "rust", "javascript", "python"}


def test_skip_node_modules():
    """跳过 node_modules 中的清单文件（不识别为子项目）"""
    with tempfile.TemporaryDirectory() as root:
        _make_project(root, "app", "package.json")
        # node_modules 里的 package.json 不应被识别
        _make_project(root, "app/node_modules/lib", "package.json")
        projects = scan_subprojects(root)
        assert len(projects) == 1
        assert projects[0]["name"] == "app"


def test_skip_vendor_and_target():
    """跳过 vendor/ 和 target/ 目录"""
    with tempfile.TemporaryDirectory() as root:
        _make_project(root, "myapp", "go.mod")
        _make_project(root, "myapp/vendor/lib", "go.mod")
        _make_project(root, "myrust", "Cargo.toml")
        _make_project(root, "myrust/target/debug", "Cargo.toml")
        projects = scan_subprojects(root)
        names = {p["name"] for p in projects}
        assert "myapp" in names
        assert "myrust" in names
        # vendor 和 target 里的不应被识别
        assert "lib" not in names
        assert "debug" not in names


def test_nested_monorepo():
    """支持 monorepo 嵌套子项目（packages/ 下有多个 package.json）"""
    with tempfile.TemporaryDirectory() as root:
        # monorepo 根
        _make_project(root, "monorepo", "package.json")
        # 子包
        _make_project(root, "monorepo/packages/core", "package.json")
        _make_project(root, "monorepo/packages/ui", "package.json")
        projects = scan_subprojects(root)
        # 应该识别出 3 个（根 + 2 个子包）
        assert len(projects) == 3
        names = {p["name"] for p in projects}
        assert "monorepo" in names
        assert "core" in names
        assert "ui" in names


def test_no_manifest_no_project():
    """无清单文件的目录不被识别"""
    with tempfile.TemporaryDirectory() as root:
        os.makedirs(os.path.join(root, "empty_dir"))
        with open(os.path.join(root, "README.md"), "w") as f:
            f.write("# no manifest here")
        projects = scan_subprojects(root)
        assert len(projects) == 0


def test_depth_limit():
    """深度限制生效"""
    with tempfile.TemporaryDirectory() as root:
        # 深度 1 的项目
        _make_project(root, "shallow", "go.mod")
        # 深度 6 的项目（超过默认 max_depth=5）
        deep = os.path.join(root, "a", "b", "c", "d", "e", "f")
        os.makedirs(deep)
        with open(os.path.join(deep, "go.mod"), "w") as f:
            f.write("module deep\n")
        projects = scan_subprojects(root, max_depth=3)
        names = {p["name"] for p in projects}
        assert "shallow" in names
        assert "f" not in names  # 超过深度限制


def test_all_manifest_types():
    """所有清单文件类型都能识别"""
    with tempfile.TemporaryDirectory() as root:
        # 用 manifest 文件名做目录名，避免同语言多清单合并到同一目录
        for manifest, lang in PROJECT_MANIFESTS.items():
            dir_name = manifest.replace(".", "_")
            _make_project(root, dir_name, manifest)
        projects = scan_subprojects(root)
        assert len(projects) == len(PROJECT_MANIFESTS)


def test_rel_path_correct():
    """rel_path 正确反映项目相对路径"""
    with tempfile.TemporaryDirectory() as root:
        _make_project(root, "top", "go.mod")
        _make_project(root, "nested/deep", "Cargo.toml")
        projects = scan_subprojects(root)
        by_name = {p["name"]: p for p in projects}
        assert by_name["top"]["rel_path"] == "top"
        assert by_name["deep"]["rel_path"] == "nested/deep"


def test_root_itself_is_project():
    """扫描根目录本身是项目根时也能识别"""
    with tempfile.TemporaryDirectory() as root:
        with open(os.path.join(root, "go.mod"), "w") as f:
            f.write("module root\n")
        projects = scan_subprojects(root)
        assert len(projects) == 1
        assert projects[0]["rel_path"] == ""
