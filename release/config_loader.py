"""分层配置加载器 + 平台路径规范。

优先级：CLI 参数 > 环境变量 > 用户配置 > 系统配置 > 默认值。
cw config explain 输出每个有效值的来源，隐藏 secret。

任务：T-1783983162955-afcc Step #3
规范：cross-platform-packaging-release-plan.md §5
"""

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    import tomllib
except ImportError:
    try:
        import tomli as tomllib
    except ImportError:
        tomllib = None  # type: ignore


# ============================================
# 平台路径规范
# ============================================

@dataclass
class PlatformPaths:
    """平台特定的配置和数据目录。"""
    system_config: Path
    user_config: Path
    system_data: Path
    user_data: Path
    runtime: Optional[Path] = None  # Linux only: /run/callwarden

    @staticmethod
    def detect() -> "PlatformPaths":
        """根据当前平台返回标准路径。"""
        if sys.platform == "win32":
            program_data = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
            local_app_data = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
            return PlatformPaths(
                system_config=program_data / "CallWarden" / "config.toml",
                user_config=local_app_data / "CallWarden" / "config.toml",
                system_data=program_data / "CallWarden" / "data",
                user_data=local_app_data / "CallWarden" / "data",
            )
        elif sys.platform == "darwin":
            return PlatformPaths(
                system_config=Path("/Library/Application Support/CallWarden/config.toml"),
                user_config=Path.home() / "Library" / "Application Support" / "CallWarden" / "config.toml",
                system_data=Path("/Library/Application Support/CallWarden/data"),
                user_data=Path.home() / "Library" / "Application Support" / "CallWarden" / "data",
            )
        else:  # Linux
            xdg_config = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
            xdg_state = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
            return PlatformPaths(
                system_config=Path("/etc/callwarden/config.toml"),
                user_config=xdg_config / "callwarden" / "config.toml",
                system_data=Path("/var/lib/callwarden"),
                user_data=xdg_state / "callwarden",
                runtime=Path("/run/callwarden"),
            )


# ============================================
# 分层配置加载器
# ============================================

@dataclass
class ConfigValue:
    """带来源追踪的配置值。"""
    value: Any
    source: str  # "cli" / "env" / "user_config" / "system_config" / "default"


@dataclass
class Config:
    """分层配置。"""
    values: Dict[str, ConfigValue] = field(default_factory=dict)

    # Secret 字段名列表（explain 时隐藏值）
    SECRET_KEYS = {"token", "secret", "password", "api_key", "private_key"}

    def get(self, key: str, default: Any = None) -> Any:
        """获取配置值。"""
        cv = self.values.get(key)
        if cv is not None:
            return cv.value
        return default

    def explain(self) -> List[Dict[str, str]]:
        """输出每个有效值的来源（隐藏 secret）。"""
        result = []
        for key, cv in sorted(self.values.items()):
            is_secret = any(s in key.lower() for s in self.SECRET_KEYS)
            display_value = "***" if is_secret else str(cv.value)
            result.append({
                "key": key,
                "value": display_value,
                "source": cv.source,
            })
        return result


def load_config(
    cli_overrides: Optional[Dict[str, Any]] = None,
    env_prefix: str = "CW_",
) -> Config:
    """按优先级加载配置。

    优先级：CLI 参数 > 环境变量 > 用户配置 > 系统配置 > 默认值
    """
    config = Config()
    paths = PlatformPaths.detect()

    # 1. 默认值
    defaults = {
        "daemon_socket": "/run/callwarden/callwarden.sock" if sys.platform == "linux" else "",
        "log_level": "info",
        "max_workers": 16,
        "watcher_debounce_ms": 250,
        "cas_grace_days": 7,
    }
    for key, value in defaults.items():
        config.values[key] = ConfigValue(value=value, source="default")

    # 2. 系统配置
    _load_toml_into(config, paths.system_config, source="system_config")

    # 3. 用户配置
    _load_toml_into(config, paths.user_config, source="user_config")

    # 4. 环境变量
    for env_key, env_value in os.environ.items():
        if env_key.startswith(env_prefix):
            config_key = env_key[len(env_prefix):].lower()
            config.values[config_key] = ConfigValue(value=env_value, source="env")

    # 5. CLI 参数
    if cli_overrides:
        for key, value in cli_overrides.items():
            config.values[key] = ConfigValue(value=value, source="cli")

    return config


def _load_toml_into(config: Config, path: Path, source: str):
    """从 TOML 文件加载配置到 Config 对象。"""
    if tomllib is None:
        return
    if not path.exists():
        return
    try:
        with open(path, "rb") as f:
            data = tomllib.load(f)
        _flatten_toml(data, config, source, prefix="")
    except Exception:
        pass  # 配置文件损坏时跳过


def _flatten_toml(data: dict, config: Config, source: str, prefix: str):
    """将嵌套 TOML dict 扁平化为 dot-separated key。"""
    for key, value in data.items():
        full_key = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            _flatten_toml(value, config, source, full_key)
        else:
            config.values[full_key] = ConfigValue(value=value, source=source)


# ============================================
# 角色化安装检查
# ============================================

SUPPORTED_ROLES = {"local", "client", "agent", "daemon", "all"}

PLATFORM_ROLE_SUPPORT = {
    "win32": {"local", "client"},
    "darwin": {"local", "client"},
    "linux": {"local", "client", "agent", "daemon", "all"},
}


def check_role_supported(role: str, platform: Optional[str] = None) -> bool:
    """检查当前平台是否支持指定角色。"""
    plat = platform or sys.platform
    supported = PLATFORM_ROLE_SUPPORT.get(plat, set())
    return role in supported


def fail_closed_unsupported(role: str, platform: Optional[str] = None) -> None:
    """如果角色不受平台支持，fail-closed 退出。"""
    if not check_role_supported(role, platform):
        plat = platform or sys.platform
        supported = PLATFORM_ROLE_SUPPORT.get(plat, set())
        print(
            f"ERROR: Role '{role}' is not supported on {plat}.\n"
            f"Supported roles: {', '.join(sorted(supported))}\n"
            f"Enterprise daemon/agent requires Linux with SO_PEERCRED, SCM_RIGHTS, and UDS.",
            file=sys.stderr,
        )
        sys.exit(2)
