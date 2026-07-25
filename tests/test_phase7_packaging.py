"""Phase 7 测试：跨平台打包、发行契约与安装验收。

任务：T-1783983162955-afcc (Common Agent)
"""

import os
import sys
import json
from pathlib import Path

import pytest


# ============================================
# 版本一致性
# ============================================


class TestVersionConsistency:
    """验证 release/version.toml 为唯一版本源。"""

    def test_version_toml_exists(self):
        version_toml = Path(__file__).parent.parent / "release" / "version.toml"
        assert version_toml.exists(), "release/version.toml must exist"

    def test_version_sync_passes(self):
        """version_sync.py 验证所有版本一致。"""
        import subprocess
        result = subprocess.run(
            [sys.executable, "release/version_sync.py"],
            capture_output=True, text=True,
            cwd=str(Path(__file__).parent.parent),
            env={**os.environ, "PYTHONIOENCODING": "utf-8"},
        )
        assert result.returncode == 0, f"Version sync failed: {result.stdout}\n{result.stderr}"
        assert "[PASS]" in result.stdout

    def test_version_toml_fields(self):
        """验证 version.toml 包含所有必需字段。"""
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        version_toml = Path(__file__).parent.parent / "release" / "version.toml"
        with open(version_toml, "rb") as f:
            data = tomllib.load(f)

        assert "product" in data
        assert "version" in data["product"]
        assert "abi" in data
        assert data["abi"]["parser"] >= 1
        assert data["abi"]["snapshot"] >= 1
        assert "platforms" in data
        assert "windows" in data["platforms"]
        assert "macos" in data["platforms"]
        assert "linux" in data["platforms"]
        assert "roles" in data
        assert "entry_points" in data

    def test_entry_points_defined(self):
        """验证 4 个入口全部定义。"""
        try:
            import tomllib
        except ImportError:
            import tomli as tomllib

        version_toml = Path(__file__).parent.parent / "release" / "version.toml"
        with open(version_toml, "rb") as f:
            data = tomllib.load(f)

        eps = data["entry_points"]
        assert "cw" in eps
        assert "cw-client" in eps
        assert "cw-agent" in eps
        assert "cw-daemon" in eps


# ============================================
# Entry Points
# ============================================


class TestEntryPoints:
    """验证 cw-client/cw-agent/cw-daemon 入口模块。"""

    def test_client_module_importable(self):
        from callwarden.cli.client import main
        assert callable(main)

    def test_agent_module_importable(self):
        from callwarden.cli.agent import main
        assert callable(main)

    def test_daemon_module_importable(self):
        from callwarden.cli.daemon import main
        assert callable(main)

    def test_agent_fail_closed_on_windows(self):
        """cw-agent 在非 Linux 上 fail-closed。"""
        if sys.platform == "linux":
            pytest.skip("Only test on non-Linux")
        from callwarden.cli.agent import main
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 2

    def test_agent_help_succeeds_on_linux(self, monkeypatch, capsys):
        """发布 smoke 使用的 cw-agent --help 必须返回成功。"""
        from callwarden.cli.main import run_agent_mode

        monkeypatch.setattr(sys, "platform", "linux")

        assert run_agent_mode(["--help"]) == 0
        assert "Usage: cw-agent" in capsys.readouterr().out

    def test_daemon_fail_closed_on_windows(self):
        """cw-daemon 在非 Linux 上 fail-closed。"""
        if sys.platform == "linux":
            pytest.skip("Only test on non-Linux")
        from callwarden.cli.daemon import main
        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 2


# ============================================
# 平台路径规范
# ============================================


class TestPlatformPaths:
    """验证平台路径规范。"""

    def test_detect_current_platform(self):
        from release.config_loader import PlatformPaths
        paths = PlatformPaths.detect()
        assert paths.system_config is not None
        assert paths.user_config is not None
        assert paths.system_data is not None
        assert paths.user_data is not None

    def test_windows_paths(self):
        from release.config_loader import PlatformPaths
        if sys.platform != "win32":
            pytest.skip("Windows only")
        paths = PlatformPaths.detect()
        assert "CallWarden" in str(paths.system_config)
        assert "config.toml" in str(paths.system_config)
        assert paths.runtime is None  # Windows 无 /run

    def test_linux_runtime_path(self):
        from release.config_loader import PlatformPaths
        if sys.platform != "linux":
            pytest.skip("Linux only")
        paths = PlatformPaths.detect()
        assert paths.runtime == Path("/run/callwarden")


# ============================================
# 分层配置加载
# ============================================


class TestConfigLoader:
    """验证分层配置优先级。"""

    def test_default_values(self):
        from release.config_loader import load_config
        config = load_config()
        assert config.get("log_level") == "info"
        assert config.get("max_workers") == 16

    def test_cli_overrides_everything(self):
        from release.config_loader import load_config
        config = load_config(cli_overrides={"log_level": "debug"})
        assert config.get("log_level") == "debug"
        cv = config.values.get("log_level")
        assert cv.source == "cli"

    def test_env_overrides_default(self):
        from release.config_loader import load_config
        os.environ["CW_LOG_LEVEL"] = "warning"
        try:
            config = load_config()
            assert config.get("log_level") == "warning"
            cv = config.values.get("log_level")
            assert cv.source == "env"
        finally:
            del os.environ["CW_LOG_LEVEL"]

    def test_explain_hides_secrets(self):
        from release.config_loader import load_config, ConfigValue
        config = load_config(cli_overrides={"api_key": "secret123", "log_level": "debug"})
        explanation = config.explain()
        api_entry = next((e for e in explanation if e["key"] == "api_key"), None)
        assert api_entry is not None
        assert api_entry["value"] == "***"
        assert api_entry["source"] == "cli"


# ============================================
# 角色化安装
# ============================================


class TestRoleSupport:
    """验证角色化安装与 unsupported-platform fail-closed。"""

    def test_supported_roles(self):
        from release.config_loader import SUPPORTED_ROLES
        assert "local" in SUPPORTED_ROLES
        assert "client" in SUPPORTED_ROLES
        assert "agent" in SUPPORTED_ROLES
        assert "daemon" in SUPPORTED_ROLES
        assert "all" in SUPPORTED_ROLES

    def test_windows_supports_local_and_client(self):
        from release.config_loader import check_role_supported
        assert check_role_supported("local", "win32") is True
        assert check_role_supported("client", "win32") is True
        assert check_role_supported("agent", "win32") is False
        assert check_role_supported("daemon", "win32") is False
        assert check_role_supported("all", "win32") is False

    def test_macos_supports_local_and_client(self):
        from release.config_loader import check_role_supported
        assert check_role_supported("local", "darwin") is True
        assert check_role_supported("client", "darwin") is True
        assert check_role_supported("daemon", "darwin") is False

    def test_linux_supports_all_roles(self):
        from release.config_loader import check_role_supported
        for role in ("local", "client", "agent", "daemon", "all"):
            assert check_role_supported(role, "linux") is True

    def test_fail_closed_unsupported(self):
        from release.config_loader import fail_closed_unsupported
        if sys.platform == "linux":
            pytest.skip("Only test on non-Linux")
        with pytest.raises(SystemExit) as exc_info:
            fail_closed_unsupported("daemon")
        assert exc_info.value.code == 2
