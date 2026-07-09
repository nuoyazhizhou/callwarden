"""P24: 测试 scan_subprojects 过滤 + auto_generate_ignore

P24.1: scan_subprojects 默认跳过非真实子项目目录
P24.2: --include-all 恢复旧行为
P24.3: .gitmodules submodule 识别
P24.4: auto_generate_ignore 检测测试 fixture / npm / examples / docs
P24.5: auto_generate_ignore 不重复默认基线
P24.6: auto_generate_ignore --apply 实际写入
P24.7: auto_generate_ignore 保留用户手写规则
"""
import os
import tempfile
import shutil

import pytest

from callwarden.config import (
    scan_subprojects,
    auto_generate_ignore,
    _NON_REAL_PROJECT_DIRS,
    _parse_gitmodules,
)


class TestScanSubprojectsFilter:
    """P24.1-P24.2: scan_subprojects 默认过滤 + --include-all"""

    def _create_repo_with_fixtures(self, root):
        """创建含真实子项目 + 测试 fixture 的仓库"""
        # 真实子项目
        os.makedirs(os.path.join(root, "packages", "core"), exist_ok=True)
        with open(os.path.join(root, "packages", "core", "package.json"), "w") as f:
            f.write('{"name": "core"}')

        os.makedirs(os.path.join(root, "crates", "cli"), exist_ok=True)
        with open(os.path.join(root, "crates", "cli", "Cargo.toml"), "w") as f:
            f.write('[package]\nname = "cli"')

        # 测试 fixture（应被跳过）
        os.makedirs(os.path.join(root, "tests", "fixtures", "angular-demo"), exist_ok=True)
        with open(os.path.join(root, "tests", "fixtures", "angular-demo", "package.json"), "w") as f:
            f.write('{"name": "angular-demo-fixture"}')

        # npm 发布包（应被跳过）
        os.makedirs(os.path.join(root, "npm", "darwin-arm64"), exist_ok=True)
        with open(os.path.join(root, "npm", "darwin-arm64", "package.json"), "w") as f:
            f.write('{"name": "darwin-arm64"}')

        # 示例（应被跳过）
        os.makedirs(os.path.join(root, "examples", "basic"), exist_ok=True)
        with open(os.path.join(root, "examples", "basic", "package.json"), "w") as f:
            f.write('{"name": "basic-example"}')

    def test_default_skips_non_real(self, tmp_path):
        """默认跳过 tests/fixtures/npm/examples"""
        repo = tmp_path / "test_repo"
        repo.mkdir()
        (repo / "package.json").write_text('{"name": "test_repo"}')
        self._create_repo_with_fixtures(str(repo))

        # 默认行为：跳过非真实子项目
        projects = scan_subprojects(str(repo))
        names = [p["name"] for p in projects]

        assert "test_repo" in names  # 仓库根保留
        assert "core" in names       # packages/core 保留
        assert "cli" in names        # crates/cli 保留
        assert "angular-demo" not in names  # tests/fixtures 跳过
        assert "darwin-arm64" not in names  # npm 跳过
        assert "basic" not in names        # examples 跳过

    def test_include_all_restores_old_behavior(self, tmp_path):
        """--include-all 恢复旧行为（包含 fixture）"""
        repo = tmp_path / "test_repo"
        repo.mkdir()
        (repo / "package.json").write_text('{"name": "test_repo"}')
        self._create_repo_with_fixtures(str(repo))

        projects = scan_subprojects(str(repo), skip_non_real=False)
        names = [p["name"] for p in projects]

        # 所有子项目都被识别
        assert "angular-demo" in names  # tests/fixtures 包含
        assert "darwin-arm64" in names  # npm 包含
        assert "basic" in names        # examples 包含


class TestGitmodulesParsing:
    """P24.3: .gitmodules submodule 识别"""

    def test_parse_gitmodules(self, tmp_path):
        """解析 .gitmodules 文件"""
        repo = tmp_path / "submodule_repo"
        repo.mkdir()
        (repo / ".gitmodules").write_text(
            '[submodule "vendor/lib"]\n'
            '\tpath = vendor/lib\n'
            '\turl = https://github.com/example/lib.git\n'
            '[submodule "tools/cli"]\n'
            '\tpath = tools/cli\n'
            '\turl = https://github.com/example/cli.git\n'
        )

        paths = _parse_gitmodules(str(repo))
        assert "vendor/lib" in paths
        assert "tools/cli" in paths

    def test_no_gitmodules(self, tmp_path):
        """无 .gitmodules 返回空集"""
        repo = tmp_path / "no_submodules"
        repo.mkdir()
        paths = _parse_gitmodules(str(repo))
        assert paths == set()

    def test_submodule_dir_skipped(self, tmp_path):
        """submodule 目录不作为子项目重复识别"""
        repo = tmp_path / "main_repo"
        repo.mkdir()
        (repo / "package.json").write_text('{"name": "main"}')

        # 创建 .gitmodules
        (repo / ".gitmodules").write_text(
            '[submodule "ext/lib"]\n'
            '\tpath = ext/lib\n'
            '\turl = https://github.com/example/lib.git\n'
        )

        # submodule 目录下也有 manifest（不应被识别为子项目）
        os.makedirs(os.path.join(str(repo), "ext", "lib"), exist_ok=True)
        with open(os.path.join(str(repo), "ext", "lib", "package.json"), "w") as f:
            f.write('{"name": "ext-lib"}')

        # 创建 .git 目录标记仓库根（submodule 识别需要）
        os.makedirs(os.path.join(str(repo), ".git"), exist_ok=True)

        projects = scan_subprojects(str(repo))
        names = [p["name"] for p in projects]

        assert "main_repo" in names
        assert "ext-lib" not in names  # submodule 不重复识别


class TestAutoGenerateIgnore:
    """P24.4-P24.7: auto_generate_ignore"""

    def test_detect_test_fixtures(self, tmp_path):
        """检测测试 fixture 目录"""
        os.makedirs(os.path.join(str(tmp_path), "tests", "fixtures", "demo"), exist_ok=True)
        with open(os.path.join(str(tmp_path), "tests", "fixtures", "demo", "package.json"), "w") as f:
            f.write('{"name": "demo"}')

        os.makedirs(os.path.join(str(tmp_path), "__fixtures__", "mock"), exist_ok=True)
        with open(os.path.join(str(tmp_path), "__fixtures__", "mock", "package.json"), "w") as f:
            f.write('{"name": "mock"}')

        result = auto_generate_ignore(str(tmp_path), dry_run=True)

        patterns = result["new_patterns"]
        assert any("tests/fixtures" in p for p in patterns)
        assert any("__fixtures__" in p for p in patterns)

    def test_detect_npm_publish(self, tmp_path):
        """检测 npm 发布包目录"""
        os.makedirs(os.path.join(str(tmp_path), "npm", "darwin-arm64"), exist_ok=True)
        with open(os.path.join(str(tmp_path), "npm", "darwin-arm64", "package.json"), "w") as f:
            f.write('{"name": "darwin-arm64"}')

        result = auto_generate_ignore(str(tmp_path), dry_run=True)

        patterns = result["new_patterns"]
        assert any("npm" in p for p in patterns)

    def test_detect_examples(self, tmp_path):
        """检测示例目录"""
        os.makedirs(os.path.join(str(tmp_path), "examples", "basic"), exist_ok=True)
        with open(os.path.join(str(tmp_path), "examples", "basic", "package.json"), "w") as f:
            f.write('{"name": "basic"}')

        result = auto_generate_ignore(str(tmp_path), dry_run=True)

        patterns = result["new_patterns"]
        assert any("examples" in p for p in patterns)

    def test_detect_docs(self, tmp_path):
        """检测文档目录"""
        os.makedirs(os.path.join(str(tmp_path), "docs", "site"), exist_ok=True)
        with open(os.path.join(str(tmp_path), "docs", "site", "package.json"), "w") as f:
            f.write('{"name": "docs-site"}')

        result = auto_generate_ignore(str(tmp_path), dry_run=True)

        patterns = result["new_patterns"]
        assert any("docs" in p for p in patterns)

    def test_no_duplicate_default_baseline(self, tmp_path):
        """不重复默认基线规则"""
        # node_modules 是默认基线已覆盖的
        os.makedirs(os.path.join(str(tmp_path), "node_modules"), exist_ok=True)

        result = auto_generate_ignore(str(tmp_path), dry_run=True)

        # node_modules 不应出现在 new_patterns 中
        patterns = result["new_patterns"]
        assert not any("node_modules" in p for p in patterns)

    def test_apply_writes_file(self, tmp_path):
        """--apply 实际写入 .callwardenignore"""
        os.makedirs(os.path.join(str(tmp_path), "tests", "fixtures"), exist_ok=True)

        result = auto_generate_ignore(str(tmp_path), dry_run=False)

        assert result["written"] is True
        ignore_file = os.path.join(str(tmp_path), ".callwardenignore")
        assert os.path.isfile(ignore_file)

        content = open(ignore_file, encoding="utf-8").read()
        assert "auto-generated" in content
        assert "tests/fixtures/" in content

    def test_preserves_user_rules(self, tmp_path):
        """保留用户手写规则"""
        # 先创建用户手写规则
        ignore_file = os.path.join(str(tmp_path), ".callwardenignore")
        with open(ignore_file, "w") as f:
            f.write("# user rules\nmy_custom_dir/\n")

        # 添加新目录
        os.makedirs(os.path.join(str(tmp_path), "tests", "fixtures"), exist_ok=True)

        result = auto_generate_ignore(str(tmp_path), dry_run=False)

        content = open(ignore_file, encoding="utf-8").read()
        assert "my_custom_dir/" in content  # 用户规则保留
        assert "tests/fixtures/" in content  # 新规则追加
        assert "auto-generated" in content

    def test_dry_run_no_write(self, tmp_path):
        """dry-run 不写入文件"""
        os.makedirs(os.path.join(str(tmp_path), "tests", "fixtures"), exist_ok=True)

        result = auto_generate_ignore(str(tmp_path), dry_run=True)

        assert result["written"] is False
        ignore_file = os.path.join(str(tmp_path), ".callwardenignore")
        assert not os.path.isfile(ignore_file)

    def test_large_files_detection(self, tmp_path):
        """检测大文件密度目录"""
        # 创建含多个大文件的目录
        big_dir = os.path.join(str(tmp_path), "bundled_lib")
        os.makedirs(big_dir, exist_ok=True)
        # 创建 4 个 > 500KB 的 JS 文件
        big_content = "x" * (600 * 1024)
        for i in range(4):
            with open(os.path.join(big_dir, f"bundle_{i}.js"), "w") as f:
                f.write(big_content)

        result = auto_generate_ignore(str(tmp_path), dry_run=True)

        patterns = result["new_patterns"]
        assert any("bundled_lib" in p for p in patterns)

    def test_minified_detection(self, tmp_path):
        """检测 minified 文件目录"""
        min_dir = os.path.join(str(tmp_path), "min_lib")
        os.makedirs(min_dir, exist_ok=True)
        with open(os.path.join(min_dir, "jquery.min.js"), "w") as f:
            f.write("console.log('minified');")

        result = auto_generate_ignore(str(tmp_path), dry_run=True)

        patterns = result["new_patterns"]
        assert any("min_lib" in p for p in patterns)
