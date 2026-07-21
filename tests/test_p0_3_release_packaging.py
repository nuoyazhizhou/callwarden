"""P0-3 复审整改测试（批次31，2026-07-21）。

复审报告 §3 P0-3 列出 9 个发布构建与安装链问题，本测试覆盖修复点：

问题 1（argparse 错误）：release/build.py 删除 `--config-setting --build-option=--plat-name=...`
问题 2（MANIFEST.in 缺失）：新增 MANIFEST.in 显式声明 Rust 二进制
问题 3（干净 runner 缺 .pyd/.so）：新增 _ensure_rust_ext_at_root() 自动复制
问题 4（Linux console_scripts）：build_packages.sh 从 wheel 提取 cw/cw-client/cw-agent
问题 5（cw-daemon 命名不一致）：Cargo.toml + systemd unit + build_packages.sh 三方统一
问题 6（cw --version 不支持）：cli/main.py 新增 --version / -V 分支
问题 7（Windows MSI 缺 PyInstaller）：workflow Gate 4a 加 fail-fast 检查
问题 8（macOS 环境变量不匹配 + placeholder）：build_pkg.sh 修复 + workflow 对齐
问题 9（参数顺序 + || true）：build_packages.sh 加 --offline-bundle-only flag + workflow 删 || true

设计原则：本测试只验证静态属性（文件存在 / 字符串匹配 / 语法正确），
不实际执行构建（构建需要干净 runner + cargo + PyInstaller，由 CI workflow 验证）。
"""

import os
import re
import subprocess
import sys
import unittest

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)


# ============================================
# 问题 1: build.py argparse 错误
# ============================================


class TestP03Issue1BuildPyArgparse(unittest.TestCase):
    """问题 1：release/build.py 不再使用已废弃的 --build-option。"""

    def setUp(self):
        self.build_py_path = os.path.join(_PKG_PARENT, "release", "build.py")
        with open(self.build_py_path, encoding="utf-8") as f:
            self.content = f.read()

    def test_no_build_option_arg(self):
        """`--build-option=--plat-name=` 已废弃（setuptools 60+），不应作为 run() 实参出现。"""
        # 注释里可以提及（P0-3 修复说明），但实际命令构造不应包含
        # 检查 "--build-option=--plat-name" 不作为 run() 调用的实参
        self.assertNotRegex(
            self.content,
            r'["\']--build-option=',
            "release/build.py 不应再使用已废弃的 --build-option= 作为命令实参"
        )

    def test_no_config_setting_plat_name(self):
        """`--config-setting --build-option=...` argparse 错误根因不应作为命令实参出现。"""
        # 注释里可以提及修复说明，但实际命令构造不应包含 "--config-setting" 实参
        # 检查独立的 "--config-setting" 实参（前后有引号或逗号分隔）
        self.assertNotRegex(
            self.content,
            r'["\'],\s*"--config-setting"',
            "release/build.py 不应再使用 --config-setting 作为命令实参"
        )


# ============================================
# 问题 2: MANIFEST.in 存在 + 包含 Rust 二进制
# ============================================


class TestP03Issue2ManifestIn(unittest.TestCase):
    """问题 2：新增 MANIFEST.in 显式声明 Rust 二进制。"""

    def setUp(self):
        self.manifest_path = os.path.join(_PKG_PARENT, "MANIFEST.in")
        self.assertTrue(os.path.exists(self.manifest_path),
                        "MANIFEST.in 必须存在（P0-3 问题 2 修复）")
        with open(self.manifest_path, encoding="utf-8") as f:
            self.content = f.read()

    def test_includes_pyd(self):
        """MANIFEST.in 显式 include callwarden_core.pyd（Windows）。"""
        self.assertIn("callwarden_core.pyd", self.content,
                      "MANIFEST.in 应 include callwarden_core.pyd")

    def test_includes_so(self):
        """MANIFEST.in 显式 include callwarden_core.so（Linux/macOS）。"""
        self.assertIn("callwarden_core.so", self.content,
                      "MANIFEST.in 应 include callwarden_core.so")


# ============================================
# 问题 3: _ensure_rust_ext_at_root() 函数存在
# ============================================


class TestP03Issue3EnsureRustExtAtRoot(unittest.TestCase):
    """问题 3：release/build.py 新增 _ensure_rust_ext_at_root() 自动复制 .pyd/.so。"""

    def setUp(self):
        self.build_py_path = os.path.join(_PKG_PARENT, "release", "build.py")
        with open(self.build_py_path, encoding="utf-8") as f:
            self.content = f.read()

    def test_function_defined(self):
        """_ensure_rust_ext_at_root() 函数已定义。"""
        self.assertIn("def _ensure_rust_ext_at_root()", self.content,
                      "release/build.py 应定义 _ensure_rust_ext_at_root()")

    def test_function_called_in_build_wheel(self):
        """build_python_wheel() 调用 _ensure_rust_ext_at_root()。"""
        # 找到 build_python_wheel 函数体后，验证其内调用了 _ensure_rust_ext_at_root
        # 用三引号 raw string 以便正则中包含 "
        pattern = r'''def build_python_wheel\([^)]*\)[^:]*:\s*(?:"""[^"]*"""\s*)?(.*?)(?=\ndef |\Z)'''
        match = re.search(pattern, self.content, re.DOTALL)
        self.assertIsNotNone(match, "build_python_wheel 函数应存在")
        body = match.group(1)
        self.assertIn("_ensure_rust_ext_at_root()", body,
                      "build_python_wheel() 应调用 _ensure_rust_ext_at_root()")


# ============================================
# 问题 4: Linux build_packages.sh 从 wheel 提取 console_scripts
# ============================================


class TestP03Issue4ConsoleScriptExtraction(unittest.TestCase):
    """问题 4：build_packages.sh 新增 extract_python_console_scripts()。"""

    def setUp(self):
        self.script_path = os.path.join(_PKG_PARENT, "release", "linux", "build_packages.sh")
        with open(self.script_path, encoding="utf-8") as f:
            self.content = f.read()

    def test_extract_function_defined(self):
        """extract_python_console_scripts() 函数已定义。"""
        self.assertIn("extract_python_console_scripts()", self.content,
                      "build_packages.sh 应定义 extract_python_console_scripts()")

    def test_uses_pip_install_wheel(self):
        """通过 pip install wheel 到临时 venv 提取 console_scripts。"""
        self.assertIn("pip install", self.content,
                      "应使用 pip install 提取 console_scripts")
        self.assertIn("venv", self.content.lower(),
                      "应使用临时 venv 隔离")

    def test_validates_console_scripts(self):
        """fail-closed 验证 cw/cw-client/cw-agent 存在。"""
        # 验证脚本中有 cw/cw-client/cw-agent 检查（不期望独立 ELF 二进制）
        for script in ["cw", "cw-client", "cw-agent"]:
            self.assertIn(script, self.content,
                          f"build_packages.sh 应检查 console_script: {script}")


# ============================================
# 问题 5: cw-daemon 命名统一
# ============================================


class TestP03Issue5CwDaemonNaming(unittest.TestCase):
    """问题 5：Cargo.toml + systemd unit + build_packages.sh 三方统一 cw-daemon（连字符）。"""

    def test_cargo_toml_bin_name(self):
        """rust_ext/Cargo.toml [[bin]] name 为 cw-daemon（连字符）。"""
        cargo_path = os.path.join(_PKG_PARENT, "rust_ext", "Cargo.toml")
        with open(cargo_path, encoding="utf-8") as f:
            content = f.read()
        # 检查 [[bin]] 块的 name 字段为 cw-daemon
        match = re.search(r'\[\[bin\]\][^[]*?name\s*=\s*"([^"]+)"', content, re.DOTALL)
        self.assertIsNotNone(match, "Cargo.toml 应有 [[bin]] name 字段")
        self.assertEqual(match.group(1), "cw-daemon",
                         f"Cargo [[bin]] name 应为 'cw-daemon'（连字符），实际: {match.group(1)}")

    def test_systemd_unit_execstart(self):
        """systemd unit ExecStart 使用 /usr/bin/cw-daemon（连字符）。"""
        unit_path = os.path.join(
            _PKG_PARENT, "release", "linux", "deb", "systemd", "callwarden-daemon.service"
        )
        with open(unit_path, encoding="utf-8") as f:
            content = f.read()
        # ExecStart 应指向 cw-daemon（连字符）
        exec_match = re.search(r'^ExecStart=\s*(.+)$', content, re.MULTILINE)
        self.assertIsNotNone(exec_match, "systemd unit 应有 ExecStart")
        self.assertIn("cw-daemon", exec_match.group(1),
                      f"ExecStart 应使用 cw-daemon（连字符），实际: {exec_match.group(1)}")
        # 不应使用下划线版本 cw_daemon
        self.assertNotIn("cw_daemon", exec_match.group(1),
                         "ExecStart 不应使用 cw_daemon（下划线）")

    def test_build_packages_sh_uses_cw_daemon_bin(self):
        """build_packages.sh cargo build --bin cw-daemon（连字符）。"""
        script_path = os.path.join(_PKG_PARENT, "release", "linux", "build_packages.sh")
        with open(script_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("--bin cw-daemon", content,
                      "build_packages.sh 应 cargo build --bin cw-daemon（连字符）")


# ============================================
# 问题 6: cw --version 支持
# ============================================


class TestP03Issue6CwVersion(unittest.TestCase):
    """问题 6：cli/main.py 新增 --version / -V 分支。"""

    def test_cli_main_has_version_branch(self):
        """cli/main.py 中存在 --version / -V 分支。"""
        cli_main_path = os.path.join(_PKG_PARENT, "cli", "main.py")
        with open(cli_main_path, encoding="utf-8") as f:
            content = f.read()
        # 验证有对 --version 和 -V 的判断
        self.assertIn('"--version"', content,
                      "cli/main.py 应判断 '--version'")
        self.assertIn('"-V"', content,
                      "cli/main.py 应判断 '-V'")

    def test_cw_version_runs_successfully(self):
        """`python cw.py --version` 退出码 0，输出 'callwarden <version>'。"""
        cw_path = os.path.join(_PKG_PARENT, "cw.py")
        result = subprocess.run(
            [sys.executable, cw_path, "--version"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0,
                         f"`cw --version` 退出码非 0: {result.stderr}")
        self.assertRegex(result.stdout.strip(), r"^callwarden \d+\.\d+\.\d+",
                        f"`cw --version` 输出应为 'callwarden <version>'，实际: {result.stdout!r}")

    def test_cw_short_version_flag(self):
        """`python cw.py -V` 等效于 `--version`。"""
        cw_path = os.path.join(_PKG_PARENT, "cw.py")
        result = subprocess.run(
            [sys.executable, cw_path, "-V"],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0,
                         f"`cw -V` 退出码非 0: {result.stderr}")
        self.assertRegex(result.stdout.strip(), r"^callwarden \d+\.\d+\.\d+",
                        f"`cw -V` 输出应为 'callwarden <version>'，实际: {result.stdout!r}")


# ============================================
# 问题 7: Windows MSI workflow fail-fast 检查
# ============================================


class TestP03Issue7WindowsMsiFailFast(unittest.TestCase):
    """问题 7：workflow Gate 4a 新增 PyInstaller exe fail-fast 检查。"""

    def setUp(self):
        self.workflow_path = os.path.join(
            _PKG_PARENT, ".github", "workflows", "enterprise-release.yml"
        )
        with open(self.workflow_path, encoding="utf-8") as f:
            self.content = f.read()

    def test_pyinstaller_fail_fast_step_exists(self):
        """workflow Gate 4a 有 PyInstaller exe fail-fast 检查步骤。"""
        self.assertIn("PyInstaller", self.content,
                      "workflow 应有 PyInstaller fail-fast 检查步骤")
        # 应检查 cw.exe / cw-client.exe / runtime/python.exe
        for f in ["cw.exe", "cw-client.exe", "runtime/python.exe"]:
            self.assertIn(f, self.content,
                          f"workflow fail-fast 检查应包含 {f}")


# ============================================
# 问题 8: macOS 环境变量对齐 + placeholder 移除
# ============================================


class TestP03Issue8MacosEnvVarAlignment(unittest.TestCase):
    """问题 8：build_pkg.sh 修复 placeholder + workflow 环境变量对齐 CW_APPLE_*。"""

    def setUp(self):
        self.workflow_path = os.path.join(
            _PKG_PARENT, ".github", "workflows", "enterprise-release.yml"
        )
        with open(self.workflow_path, encoding="utf-8") as f:
            self.workflow_content = f.read()

    def test_build_pkg_sh_no_placeholder(self):
        """build_pkg.sh 不再生成 placeholder 入口脚本。"""
        script_path = os.path.join(_PKG_PARENT, "release", "macos", "build_pkg.sh")
        with open(script_path, encoding="utf-8") as f:
            content = f.read()
        # P0-3 修复后，placeholder 逻辑已删除，改为从 wheel 提取 console_scripts
        self.assertIn("Extracting Python console_scripts from wheel", content,
                      "build_pkg.sh 应从 wheel 提取 console_scripts")

    def test_build_pkg_sh_uses_cw_apple_env(self):
        """build_pkg.sh 读取 CW_APPLE_* 环境变量。"""
        script_path = os.path.join(_PKG_PARENT, "release", "macos", "build_pkg.sh")
        with open(script_path, encoding="utf-8") as f:
            content = f.read()
        # 应读取 CW_APPLE_DEVID / CW_APPLE_ID / CW_APPLE_TEAM_ID / CW_APPLE_APP_PASSWORD
        for var in ["CW_APPLE_DEVID", "CW_APPLE_ID",
                    "CW_APPLE_TEAM_ID", "CW_APPLE_APP_PASSWORD"]:
            self.assertIn(var, content,
                          f"build_pkg.sh 应读取环境变量 {var}")

    def test_build_pkg_sh_supports_cw_build_unsigned(self):
        """build_pkg.sh 支持 CW_BUILD_UNSIGNED 显式跳过签名。"""
        script_path = os.path.join(_PKG_PARENT, "release", "macos", "build_pkg.sh")
        with open(script_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("CW_BUILD_UNSIGNED", content,
                      "build_pkg.sh 应支持 CW_BUILD_UNSIGNED 环境变量")

    def test_workflow_uses_cw_apple_secrets(self):
        """workflow Gate 4b 使用 CW_APPLE_* secrets（不是 APPLE_*）。"""
        content = self.workflow_content
        # 不应再引用 APPLE_DEVELOPER_ID / APPLE_APP_SPECIFIC_PASSWORD / APPLE_TEAM_ID
        for old_var in ["APPLE_DEVELOPER_ID", "APPLE_APP_SPECIFIC_PASSWORD", "APPLE_TEAM_ID"]:
            # 仅在 secrets. 引用上下文中检查
            self.assertNotIn(f"secrets.{old_var}", content,
                            f"workflow 不应再引用 secrets.{old_var}（应改为 CW_APPLE_*）")
        # 应引用 CW_APPLE_* secrets
        for new_var in ["CW_APPLE_DEVID", "CW_APPLE_ID",
                        "CW_APPLE_TEAM_ID", "CW_APPLE_APP_PASSWORD"]:
            self.assertIn(f"secrets.{new_var}", content,
                         f"workflow 应引用 secrets.{new_var}")


# ============================================
# 问题 9: 参数顺序 + || true 修复
# ============================================


class TestP03Issue9OfflineBundleFlag(unittest.TestCase):
    """问题 9：build_packages.sh 新增 --offline-bundle-only flag + workflow 删 || true。"""

    def setUp(self):
        self.workflow_path = os.path.join(
            _PKG_PARENT, ".github", "workflows", "enterprise-release.yml"
        )
        with open(self.workflow_path, encoding="utf-8") as f:
            self.workflow_content = f.read()

    def test_build_packages_sh_offline_bundle_only_flag(self):
        """build_packages.sh 支持 --offline-bundle-only flag。"""
        script_path = os.path.join(_PKG_PARENT, "release", "linux", "build_packages.sh")
        with open(script_path, encoding="utf-8") as f:
            content = f.read()
        self.assertIn("--offline-bundle-only", content,
                      "build_packages.sh 应支持 --offline-bundle-only flag")

    def test_workflow_no_pipe_true_after_build_packages(self):
        """workflow 不再有 `bash release/linux/build_packages.sh ... || true`（仅在 run: 命令体，不含注释）。"""
        content = self.workflow_content
        # 逐行检查，跳过 YAML 注释（以 # 开头，可能前导空白）
        bad_pattern = re.compile(r'build_packages\.sh[^\n]*\|\|\s*true')
        for line in content.splitlines():
            stripped = line.lstrip()
            if stripped.startswith('#'):
                continue  # 跳过注释行
            match = bad_pattern.search(line)
            self.assertIsNone(match,
                            f"workflow 不应有 `build_packages.sh ... || true`（注释除外），"
                            f"找到: {match.group(0) if match else None}")

    def test_workflow_no_offline_bundle_redundant_step(self):
        """workflow 已删除冗余的 'Build tar.zst offline bundle' 步骤名（仅在 name: 字段，不含注释）。"""
        content = self.workflow_content
        # 只检查 YAML 步骤名（name: 字段），不检查注释
        # 注释里可以引用旧步骤名作为修复说明
        bad_pattern = re.compile(r'^\s*-\s*name:\s*["\']?Build tar\.zst offline bundle["\']?\s*$',
                                 re.MULTILINE)
        match = bad_pattern.search(content)
        self.assertIsNone(match,
                         f"workflow 不应有冗余的 'Build tar.zst offline bundle' 步骤名，"
                         f"找到: {match.group(0) if match else None}")


if __name__ == "__main__":
    unittest.main()
