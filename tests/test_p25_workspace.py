"""P25: workspace 边界检测单元测试

验证 Cargo / npm / go workspace 边界检测，避免 workspace member 被识别成独立子项目。
"""
import json
import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from callwarden.config import (
    scan_subprojects,
    _is_cargo_workspace,
    _is_npm_workspace,
    _is_manifest_workspace_root,
    _NON_REAL_PROJECT_DIRS,
)


# ============================================
# Cargo workspace 边界检测
# ============================================

class TestCargoWorkspaceDetection:
    """检测 Cargo.toml 是否含 [workspace] section"""

    def test_cargo_workspace_root_detected(self, tmp_path):
        """含 [workspace] section 的 Cargo.toml 应识别为 workspace root"""
        cargo = tmp_path / "Cargo.toml"
        cargo.write_text(
            '[workspace]\n'
            'members = ["crates/foo", "crates/bar"]\n'
            'resolver = "2"\n\n'
            '[package]\n'
            'name = "root"\n'
            'version = "0.1.0"\n',
            encoding="utf-8",
        )
        assert _is_cargo_workspace(str(cargo)) is True

    def test_cargo_workspace_dependencies_section(self, tmp_path):
        """[workspace.dependencies] 也应识别为 workspace root"""
        cargo = tmp_path / "Cargo.toml"
        cargo.write_text(
            '[package]\n'
            'name = "root"\n'
            'version = "0.1.0"\n\n'
            '[workspace.dependencies]\n'
            'serde = "1.0"\n',
            encoding="utf-8",
        )
        assert _is_cargo_workspace(str(cargo)) is True

    def test_plain_cargo_toml_not_workspace(self, tmp_path):
        """普通 Cargo.toml（无 [workspace]）不应识别为 workspace root"""
        cargo = tmp_path / "Cargo.toml"
        cargo.write_text(
            '[package]\n'
            'name = "foo"\n'
            'version = "0.1.0"\n'
            '[dependencies]\n'
            'serde = "1.0"\n',
            encoding="utf-8",
        )
        assert _is_cargo_workspace(str(cargo)) is False

    def test_cargo_workspace_root_stops_recursion(self, tmp_path):
        """workspace root 识别后应停止递归，member 不再作为独立子项目"""
        # 构造 monorepo：
        #   root/Cargo.toml            ← workspace root（含 [workspace]）
        #   root/crates/lib_a/Cargo.toml ← workspace member（应跳过）
        #   root/crates/lib_b/Cargo.toml ← workspace member（应跳过）
        (tmp_path / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/lib_a", "crates/lib_b"]\nresolver = "2"\n',
            encoding="utf-8",
        )
        for name in ("lib_a", "lib_b"):
            d = tmp_path / "crates" / name
            d.mkdir(parents=True)
            (d / "Cargo.toml").write_text(
                f'[package]\nname = "{name}"\nversion = "0.1.0"\n',
                encoding="utf-8",
            )

        projs = scan_subprojects(str(tmp_path))
        # 只识别 1 个 workspace root，2 个 member 被折叠
        assert len(projs) == 1
        assert projs[0]["manifest"] == "Cargo.toml"
        assert projs[0]["rel_path"] == ""  # root 本身

    def test_independent_crates_outside_workspace_still_found(self, tmp_path):
        """workspace 外的独立 crate 仍应被识别"""
        # 构造：
        #   root/apps/ws/Cargo.toml          ← workspace root（识别 1，停止递归）
        #   root/packages/rs/lib_x/Cargo.toml ← 独立 crate（应识别）
        ws_dir = tmp_path / "apps" / "ws"
        ws_dir.mkdir(parents=True)
        (ws_dir / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/m1"]\n',
            encoding="utf-8",
        )
        m1 = ws_dir / "crates" / "m1"
        m1.mkdir(parents=True)
        (m1 / "Cargo.toml").write_text('[package]\nname = "m1"\n', encoding="utf-8")

        # 独立 crate（不在任何 workspace 内）
        indep = tmp_path / "packages" / "rs" / "lib_x"
        indep.mkdir(parents=True)
        (indep / "Cargo.toml").write_text('[package]\nname = "lib_x"\n', encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        roots = [p["rel_path"] for p in projs]
        # workspace root + 独立 crate = 2 个
        assert "apps/ws" in roots
        assert "packages/rs/lib_x" in roots
        # workspace member 不应出现
        assert "apps/ws/crates/m1" not in roots
        assert len(projs) == 2


# ============================================
# npm workspace 边界检测
# ============================================

class TestNpmWorkspaceDetection:
    """检测 package.json 是否含 "workspaces" 字段"""

    def test_npm_workspace_root_list(self, tmp_path):
        """package.json 含 workspaces 列表应识别为 workspace root"""
        pkg = tmp_path / "package.json"
        pkg.write_text(json.dumps({
            "name": "root",
            "workspaces": ["packages/*", "apps/*"],
        }), encoding="utf-8")
        assert _is_npm_workspace(str(pkg)) is True

    def test_pnpm_workspace_dict_form(self, tmp_path):
        """pnpm 形式 workspaces: {packages: [...]} 也应识别"""
        pkg = tmp_path / "package.json"
        pkg.write_text(json.dumps({
            "name": "root",
            "workspaces": {"packages": ["packages/**"]},
        }), encoding="utf-8")
        assert _is_npm_workspace(str(pkg)) is True

    def test_plain_package_json_not_workspace(self, tmp_path):
        """普通 package.json 不应识别为 workspace root"""
        pkg = tmp_path / "package.json"
        pkg.write_text(json.dumps({
            "name": "foo",
            "version": "1.0.0",
            "dependencies": {"lodash": "^4.0.0"},
        }), encoding="utf-8")
        assert _is_npm_workspace(str(pkg)) is False

    def test_npm_workspace_root_stops_recursion(self, tmp_path):
        """npm workspace root 识别后应停止递归，member 不再独立识别"""
        # root/package.json（workspace root）
        (tmp_path / "package.json").write_text(json.dumps({
            "name": "root",
            "workspaces": ["packages/*"],
        }), encoding="utf-8")
        # packages/foo/package.json（workspace member，应跳过）
        foo = tmp_path / "packages" / "foo"
        foo.mkdir(parents=True)
        (foo / "package.json").write_text(json.dumps({"name": "@root/foo"}), encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        # 只识别 1 个 workspace root
        assert len(projs) == 1
        assert projs[0]["manifest"] == "package.json"
        assert projs[0]["rel_path"] == ""

    def test_invalid_json_package_json(self, tmp_path):
        """非法 JSON 的 package.json 应返回 False（不抛异常）"""
        pkg = tmp_path / "package.json"
        pkg.write_text("{invalid json", encoding="utf-8")
        assert _is_npm_workspace(str(pkg)) is False


# ============================================
# go.work 检测
# ============================================

class TestGoWorkDetection:
    """检测 go.work 文件作为 Go workspace root"""

    def test_go_work_identified_as_subproject(self, tmp_path):
        """go.work 文件所在目录应识别为 Go workspace root 子项目"""
        (tmp_path / "go.work").write_text(
            'go 1.21\n\nuse (\n'
            '    ./services/foo\n'
            '    ./services/bar\n'
            ')\n',
            encoding="utf-8",
        )
        # use 指向的 member 目录内有 go.mod（应跳过，因为属于 workspace member）
        for name in ("foo", "bar"):
            d = tmp_path / "services" / name
            d.mkdir(parents=True)
            (d / "go.mod").write_text(f"module example.com/{name}\n\ngo 1.21\n", encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        # go.work root 识别 1 个 + member 被折叠
        # 注意：如果 root 没有 go.mod，go.work 仍识别为 1 个
        manifests = [p["manifest"] for p in projs]
        assert "go.work" in manifests
        # workspace member 不应作为独立子项目
        rel_paths = [p["rel_path"] for p in projs]
        assert "services/foo" not in rel_paths
        assert "services/bar" not in rel_paths


# ============================================
# _is_manifest_workspace_root 统一接口
# ============================================

class TestUnifiedWorkspaceRootInterface:
    """多语言 workspace root 统一检测接口"""

    def test_cargo_toml_dispatch(self, tmp_path):
        """Cargo.toml 走 Cargo workspace 检测"""
        cargo = tmp_path / "Cargo.toml"
        cargo.write_text("[workspace]\nmembers = []\n", encoding="utf-8")
        assert _is_manifest_workspace_root(str(cargo), "Cargo.toml") is True

    def test_package_json_dispatch(self, tmp_path):
        """package.json 走 npm workspace 检测"""
        pkg = tmp_path / "package.json"
        pkg.write_text(json.dumps({"workspaces": []}), encoding="utf-8")
        assert _is_manifest_workspace_root(str(pkg), "package.json") is True

    def test_other_manifests_return_false(self, tmp_path):
        """其他 manifest（如 go.mod/pyproject.toml）不是 workspace root"""
        for name, content in [
            ("go.mod", "module foo\ngo 1.21\n"),
            ("pyproject.toml", "[project]\nname = 'foo'\n"),
            ("CMakeLists.txt", "cmake_minimum_required(VERSION 3.10)\n"),
        ]:
            p = tmp_path / name
            p.write_text(content, encoding="utf-8")
            assert _is_manifest_workspace_root(str(p), name) is False


# ============================================
# _NON_REAL_PROJECT_DIRS 扩展（Conan test_package / e2e 等）
# ============================================

class TestExtendedNonRealDirs:
    """P25 扩展的非真实子项目目录"""

    def test_conan_test_package_skipped(self, tmp_path):
        """conan_recipes/.../test_package 目录应被跳过"""
        # 构造 conan recipe 的 test_package
        tp = tmp_path / "conan_recipes" / "recipes" / "foo" / "all" / "test_package"
        tp.mkdir(parents=True)
        (tp / "CMakeLists.txt").write_text("cmake_minimum_required(VERSION 3.10)\n", encoding="utf-8")
        (tp / "conanfile.py").write_text("# conan test\n", encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        # test_package 内的 CMakeLists.txt 不应被识别为子项目
        assert len(projs) == 0

    def test_e2e_directory_skipped(self, tmp_path):
        """e2e 测试目录应被跳过"""
        e2e = tmp_path / "e2e"
        e2e.mkdir(parents=True)
        (e2e / "package.json").write_text(json.dumps({"name": "e2e-test"}), encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 0

    def test_test_apps_directory_skipped(self, tmp_path):
        """test_apps 目录应被跳过"""
        ta = tmp_path / "test_apps" / "demo_app"
        ta.mkdir(parents=True)
        (ta / "package.json").write_text(
            json.dumps({"name": "demo"}), encoding="utf-8"
        )

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 0

    def test_integration_tests_directory_skipped(self, tmp_path):
        """integration_tests 目录应被跳过"""
        it = tmp_path / "integration_tests" / "suite_a"
        it.mkdir(parents=True)
        (it / "go.mod").write_text("module suite_a\n\ngo 1.21\n", encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        assert len(projs) == 0

    def test_real_project_dirs_not_skipped(self, tmp_path):
        """真实 monorepo 目录（packages/crates/apps/libs）不应被跳过"""
        for d in ("packages", "crates", "apps", "libs", "sdks"):
            sub = tmp_path / d / "core"
            sub.mkdir(parents=True)
            (sub / "package.json").write_text(
                json.dumps({"name": f"@scope/{d}-core"}), encoding="utf-8"
            )

        projs = scan_subprojects(str(tmp_path))
        # 5 个真实子项目
        assert len(projs) == 5
        roots = [p["rel_path"] for p in projs]
        for d in ("packages", "crates", "apps", "libs", "sdks"):
            assert f"{d}/core" in roots

    def test_extended_dirs_in_non_real_set(self):
        """验证 P25 新增的目录名都在 _NON_REAL_PROJECT_DIRS 中"""
        for name in (
            "test_apps", "e2e", "sdk_tests", "integration_tests",
            "test_package", "integration_test",
        ):
            assert name in _NON_REAL_PROJECT_DIRS, f"{name} 应在 _NON_REAL_PROJECT_DIRS"


# ============================================
# 综合场景
# ============================================

class TestRealWorldScenarios:
    """模拟真实仓库结构"""

    def test_guardrail3_like_monorepo(self, tmp_path):
        """模拟 guardrail3 风格的 monorepo：
        - apps/ws/Cargo.toml 是 workspace root（含 [workspace]）
        - apps/ws/crates/member_a/Cargo.toml 是 workspace member（应跳过）
        - packages/rs/indep/Cargo.toml 是独立 crate（应识别）
        - packages/ts/lib_x/package.json 是独立 TS 包（应识别）
        - e2e/test_thing/package.json 是 e2e 测试（应跳过）
        """
        # apps/ws/Cargo.toml workspace root
        ws = tmp_path / "apps" / "ws"
        ws.mkdir(parents=True)
        (ws / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/member_a"]\nresolver = "2"\n',
            encoding="utf-8",
        )
        # workspace member
        member = ws / "crates" / "member_a"
        member.mkdir(parents=True)
        (member / "Cargo.toml").write_text('[package]\nname = "member_a"\n', encoding="utf-8")

        # 独立 crate
        indep_rs = tmp_path / "packages" / "rs" / "indep"
        indep_rs.mkdir(parents=True)
        (indep_rs / "Cargo.toml").write_text('[package]\nname = "indep"\n', encoding="utf-8")

        # 独立 TS 包
        ts_pkg = tmp_path / "packages" / "ts" / "lib_x"
        ts_pkg.mkdir(parents=True)
        (ts_pkg / "package.json").write_text(json.dumps({"name": "lib_x"}), encoding="utf-8")

        # e2e 测试
        e2e_pkg = tmp_path / "e2e" / "test_thing"
        e2e_pkg.mkdir(parents=True)
        (e2e_pkg / "package.json").write_text(json.dumps({"name": "e2e-thing"}), encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        rels = {p["rel_path"] for p in projs}

        # 应识别 3 个：workspace root + 2 个独立包
        assert "apps/ws" in rels
        assert "packages/rs/indep" in rels
        assert "packages/ts/lib_x" in rels
        # 不应识别
        assert "apps/ws/crates/member_a" not in rels
        assert "e2e/test_thing" not in rels
        assert len(projs) == 3

    def test_nested_workspace_only_outer_identified(self, tmp_path):
        """嵌套 workspace：外层 workspace root 识别后，内层 workspace 也被跳过"""
        # outer/Cargo.toml 是 workspace root
        outer = tmp_path / "outer"
        outer.mkdir(parents=True)
        (outer / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["inner"]\n',
            encoding="utf-8",
        )
        # inner 是 outer 的 member 且自己也是 workspace root
        inner = outer / "inner"
        inner.mkdir(parents=True)
        (inner / "Cargo.toml").write_text(
            '[workspace]\nmembers = ["crates/deep"]\n[package]\nname = "inner"\n',
            encoding="utf-8",
        )
        # deep 是 inner workspace 的 member
        deep = inner / "crates" / "deep"
        deep.mkdir(parents=True)
        (deep / "Cargo.toml").write_text('[package]\nname = "deep"\n', encoding="utf-8")

        projs = scan_subprojects(str(tmp_path))
        # 只识别外层 workspace root，内层 workspace 和 deep 都被折叠
        assert len(projs) == 1
        assert projs[0]["rel_path"] == "outer"
