"""Phase 8.1: Enterprise daemon 配置文件加载与权限模板。

设计参考：
- docs/design/enterprise-daemon-shared-snapshot-plan.md §Phase 8（config 文件和权限模板）
- docs/design/daemon-ipc-security.md §1-§5（UDS/TCP/mTLS/token/UID 权限边界）

本模块提供：
1. DaemonConfig：从 JSON/YAML 文件加载 daemon 配置（含资源、网络、安全参数）
2. PermissionRole / PermissionTemplate：基于角色的权限模板
3. TokenValidator：TCP mTLS 模式下的 per-container token 校验
4. AccessChecker：UID 级别的 workspace 访问控制（user1 不能查询 user2 的 workspace）

配置文件格式（JSON 示例）：
{
  "socket_path": "/var/run/callwarden.sock",
  "data_root": "/var/lib/callwarden",
  "tcp": {
    "enabled": false,
    "port": 8765,
    "tls_cert": "/etc/callwarden/server.crt",
    "tls_key": "/etc/callwarden/server.key",
    "ca_cert": "/etc/callwarden/ca.crt"
  },
  "resources": {
    "memory_max": "1G",
    "cpu_quota": "200%",
    "max_inflight_bytes": 2147483648,
    "max_uid_inflight_bytes": 536870912,
    "max_memfd_bytes": 268435456,
    "max_conn_queued_bytes": 268435456
  },
  "security": {
    "admin_uids": [0, 1000],
    "allow_cross_uid_query": false,
    "require_token_for_tcp": true,
    "audit_log_path": "/var/log/callwarden/audit.log"
  },
  "jobs": {
    "max_concurrent": 4,
    "default_timeout": 300,
    "cancel_check_interval": 0.5
  }
}

权限模板（内置三角色）：
- admin: 可查询所有 UID 的 workspace、可注册/归档 workspace、可执行 admin 操作
- user: 只能查询自己的 workspace、可注册自己的 workspace
- readonly: 只能查询自己的 workspace，不能注册/归档
"""

from __future__ import annotations

import json
import os
import hashlib
import secrets
import time
from typing import Any, Dict, List, Optional, Set, Tuple


# ============================================================
# 默认配置常量
# ============================================================

DEFAULT_CONFIG: Dict[str, Any] = {
    "socket_path": "/var/run/callwarden.sock",
    "data_root": "/var/lib/callwarden",
    # 批次9（K4 snapshot 未发布修复）：codegraph 数据库路径模板。
    # daemon 处理 file.refresh 后调用 replicator.replicate，需要 db_path 才能
    # 触发 snapshot_service.publish_snapshot。模板支持 {workspace_instance_id} 占位符。
    # 默认空字符串——空时使用用户级单库 ~/.callwarden/callwarden.db（向后兼容）。
    "codegraph_db_path_template": "",
    "tcp": {
        "enabled": False,
        "port": 8765,
        "tls_cert": "",
        "tls_key": "",
        "ca_cert": "",
    },
    "resources": {
        "memory_max": "1G",
        "cpu_quota": "200%",
        "max_inflight_bytes": 2 * 1024 * 1024 * 1024,       # 2 GB
        "max_uid_inflight_bytes": 512 * 1024 * 1024,         # 512 MB
        "max_memfd_bytes": 256 * 1024 * 1024,                 # 256 MB
        "max_conn_queued_bytes": 256 * 1024 * 1024,           # 256 MB
    },
    "security": {
        "admin_uids": [0],
        "allow_cross_uid_query": False,
        "require_token_for_tcp": True,
        "audit_log_path": "/var/log/callwarden/audit.log",
        "token_store_path": "/var/lib/callwarden/tokens.json",
    },
    "jobs": {
        "max_concurrent": 4,
        "default_timeout": 300,
        "cancel_check_interval": 0.5,
    },
    # 批次10（P2 性能优化）：daemon 子连接（cas_conn/ws_conn/registry_conn 等）
    # 统一 PRAGMA 配置。原本只设 busy_timeout + journal_mode=WAL，缺 cache_size
    # / mmap_size / temp_store=MEMORY / synchronous=NORMAL。补全后读路径吞吐显著提升。
    # 参考 db_base.py L1888-1927 的 CodeGraphDB 主连接矩阵实验值（P13/P14/P15）。
    "db": {
        # WAL 自动 checkpoint 阈值（页数）。0=禁用自动 checkpoint（需手动），默认 1000。
        # 大规模写入场景调到 5000-10000 减少 checkpoint 频率；查询密集场景设 0 配合手动。
        "wal_autocheckpoint": 1000,
        # cache_size：负值=KB，正值=页。256MB → -262144 KB（与主连接一致）
        "cache_size_kb": 262144,
        # mmap_size：256MB（与主连接一致，加大无收益）
        "mmap_size_bytes": 268435456,
        # temp_store=MEMORY：临时表/排序在内存
        "temp_store_memory": True,
        # synchronous=NORMAL：WAL 下仅 checkpoint 时 fsync
        "synchronous_normal": True,
    },
}


# ============================================================
# DaemonConfig：配置加载与访问
# ============================================================


class DaemonConfig:
    """Daemon 配置容器，支持从 JSON 文件加载并合并默认值。

    用法：
        cfg = DaemonConfig.load_from_file("/etc/callwarden/daemon.json")
        print(cfg.socket_path)
        print(cfg.get("resources.memory_max"))
    """

    def __init__(self, data: Dict[str, Any]):
        self._data = _deep_merge(DEFAULT_CONFIG, data)

    # ----- 类方法 -----

    @classmethod
    def load_from_file(cls, path: str) -> "DaemonConfig":
        """从 JSON 文件加载配置。文件不存在时返回默认配置。

        Args:
            path: 配置文件路径（JSON 格式）

        Returns:
            DaemonConfig 实例
        """
        if not os.path.isfile(path):
            return cls({})
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return cls(data)

    @classmethod
    def load_from_dict(cls, data: Dict[str, Any]) -> "DaemonConfig":
        """从字典加载配置（用于测试）。"""
        return cls(data)

    @classmethod
    def default(cls) -> "DaemonConfig":
        """返回默认配置。"""
        return cls({})

    # ----- 属性访问 -----

    @property
    def socket_path(self) -> str:
        return self._data["socket_path"]

    @property
    def data_root(self) -> str:
        return self._data["data_root"]

    @property
    def codegraph_db_path_template(self) -> str:
        """批次9：codegraph 数据库路径模板，支持 {workspace_instance_id} 占位符。

        空字符串时 resolve_codegraph_db_path 回退到用户级单库
        ~/.callwarden/callwarden.db（与 AGENTS.md 数据库路径规则一致）。
        """
        return self._data.get("codegraph_db_path_template", "") or ""

    def resolve_codegraph_db_path(self, workspace_instance_id: str) -> str:
        """解析 workspace 实例对应的 codegraph 数据库路径。

        批次9（K4 snapshot 未发布修复）：daemon 处理 file.refresh 后需要 db_path
        才能触发 snapshot_service.publish_snapshot。

        模板支持 {workspace_instance_id} 占位符替换：
        - 模板为空：返回用户级单库 ~/.callwarden/callwarden.db
        - 模板含占位符：替换为实际 workspace_instance_id
        - 模板无占位符：原样返回

        Args:
            workspace_instance_id: workspace 实例 ID

        Returns:
            解析后的 codegraph 数据库绝对路径
        """
        tpl = self.codegraph_db_path_template
        if not tpl:
            # 回退到用户级单库（与 AGENTS.md 规则一致）
            return os.path.join(os.path.expanduser("~"), ".callwarden", "callwarden.db")
        return tpl.replace("{workspace_instance_id}", workspace_instance_id)

    @property
    def registry_db_path(self) -> str:
        return os.path.join(self.data_root, "registry.db")

    @property
    def cas_db_path(self) -> str:
        return os.path.join(self.data_root, "cas.db")

    @property
    def tcp_enabled(self) -> bool:
        return self._data["tcp"]["enabled"]

    @property
    def tcp_port(self) -> int:
        return int(self._data["tcp"]["port"])

    @property
    def tcp_tls_cert(self) -> str:
        return self._data["tcp"]["tls_cert"]

    @property
    def tcp_tls_key(self) -> str:
        return self._data["tcp"]["tls_key"]

    @property
    def tcp_ca_cert(self) -> str:
        return self._data["tcp"]["ca_cert"]

    @property
    def memory_max(self) -> str:
        return self._data["resources"]["memory_max"]

    @property
    def cpu_quota(self) -> str:
        return self._data["resources"]["cpu_quota"]

    @property
    def max_inflight_bytes(self) -> int:
        return int(self._data["resources"]["max_inflight_bytes"])

    @property
    def max_uid_inflight_bytes(self) -> int:
        return int(self._data["resources"]["max_uid_inflight_bytes"])

    @property
    def max_memfd_bytes(self) -> int:
        return int(self._data["resources"]["max_memfd_bytes"])

    @property
    def max_conn_queued_bytes(self) -> int:
        return int(self._data["resources"]["max_conn_queued_bytes"])

    @property
    def admin_uids(self) -> List[int]:
        return list(self._data["security"]["admin_uids"])

    @property
    def allow_cross_uid_query(self) -> bool:
        return bool(self._data["security"]["allow_cross_uid_query"])

    @property
    def require_token_for_tcp(self) -> bool:
        return bool(self._data["security"]["require_token_for_tcp"])

    @property
    def audit_log_path(self) -> str:
        return self._data["security"]["audit_log_path"]

    @property
    def token_store_path(self) -> str:
        return self._data["security"]["token_store_path"]

    @property
    def max_concurrent_jobs(self) -> int:
        return int(self._data["jobs"]["max_concurrent"])

    @property
    def default_job_timeout(self) -> int:
        return int(self._data["jobs"]["default_timeout"])

    @property
    def cancel_check_interval(self) -> float:
        return float(self._data["jobs"]["cancel_check_interval"])

    # ----- 批次10：db 子连接 PRAGMA 配置 -----

    @property
    def db_wal_autocheckpoint(self) -> int:
        """批次10：WAL 自动 checkpoint 阈值（页数）。0=禁用自动 checkpoint。"""
        return int(self._data.get("db", {}).get("wal_autocheckpoint", 1000))

    @property
    def db_cache_size_kb(self) -> int:
        """批次10：cache_size（KB，负值传给 PRAGMA cache_size）。256MB → -262144。"""
        return int(self._data.get("db", {}).get("cache_size_kb", 262144))

    @property
    def db_mmap_size_bytes(self) -> int:
        """批次10：mmap_size（字节）。默认 256MB，与主连接一致。"""
        return int(self._data.get("db", {}).get("mmap_size_bytes", 268435456))

    @property
    def db_temp_store_memory(self) -> bool:
        """批次10：是否设置 temp_store=MEMORY（临时表/排序在内存）。"""
        return bool(self._data.get("db", {}).get("temp_store_memory", True))

    @property
    def db_synchronous_normal(self) -> bool:
        """批次10：是否设置 synchronous=NORMAL（WAL 下仅 checkpoint 时 fsync）。"""
        return bool(self._data.get("db", {}).get("synchronous_normal", True))

    def apply_daemon_rw_pragmas(self, conn) -> None:
        """批次10：在给定 SQLite 连接上应用 daemon 子连接统一 PRAGMA 配置。

        统一应用：
        - busy_timeout=5000（已有，幂等）
        - journal_mode=WAL（已有，幂等；注册表/工具链 db 也走 WAL）
        - wal_autocheckpoint=N
        - synchronous=NORMAL
        - cache_size=-262144（256MB）
        - mmap_size=268435456（256MB）
        - temp_store=MEMORY

        Args:
            conn: sqlite3.Connection 实例
        """
        # 幂等：所有 PRAGMA 重复执行无副作用
        conn.execute("PRAGMA busy_timeout=5000")
        conn.execute("PRAGMA journal_mode=WAL")
        # wal_autocheckpoint 必须在 WAL 模式启用后设置才生效
        conn.execute(f"PRAGMA wal_autocheckpoint={int(self.db_wal_autocheckpoint)}")
        if self.db_synchronous_normal:
            conn.execute("PRAGMA synchronous=NORMAL")
        if self.db_cache_size_kb:
            # 负值=KB，正值=页；统一用负值 KB
            conn.execute(f"PRAGMA cache_size={int(self.db_cache_size_kb)}")
        if self.db_mmap_size_bytes:
            conn.execute(f"PRAGMA mmap_size={int(self.db_mmap_size_bytes)}")
        if self.db_temp_store_memory:
            conn.execute("PRAGMA temp_store=MEMORY")

    # ----- 通用访问 -----

    def get(self, dotted_key: str, default: Any = None) -> Any:
        """通过点号分隔的 key 访问嵌套配置。

        Example:
            cfg.get("resources.memory_max")  # "1G"
            cfg.get("tcp.port")              # 8765
        """
        keys = dotted_key.split(".")
        val = self._data
        for k in keys:
            if not isinstance(val, dict) or k not in val:
                return default
            val = val[k]
        return val

    def to_dict(self) -> Dict[str, Any]:
        """返回完整配置字典（深拷贝）。"""
        return json.loads(json.dumps(self._data))

    def validate(self) -> List[str]:
        """校验配置完整性，返回错误信息列表（空列表表示无错误）。

        校验项：
        - TCP 启用时必须有 tls_cert 和 tls_key
        - admin_uids 不能为空
        - tcp_port 在 1-65535 范围
        - memory_max 格式正确（数字+单位）
        - max_inflight_bytes > 0
        """
        errors = []

        # TCP 启用时必须有证书
        if self.tcp_enabled:
            if not self.tcp_tls_cert:
                errors.append("tcp.tls_cert required when tcp.enabled=true")
            if not self.tcp_tls_key:
                errors.append("tcp.tls_key required when tcp.enabled=true")
            if self.require_token_for_tcp and not self.token_store_path:
                errors.append(
                    "security.token_store_path required when "
                    "tcp.enabled=true and security.require_token_for_tcp=true"
                )

        # admin_uids 不能为空
        if not self.admin_uids:
            errors.append("security.admin_uids must not be empty")

        # port 范围
        if not (1 <= self.tcp_port <= 65535):
            errors.append(f"tcp.port {self.tcp_port} out of range 1-65535")

        # memory_max 格式
        if not _is_valid_size_string(self.memory_max):
            errors.append(
                f"resources.memory_max '{self.memory_max}' invalid "
                f"(expected like '1G', '512M', '2048')"
            )

        if not _is_valid_percent_string(self.cpu_quota):
            errors.append(
                f"resources.cpu_quota '{self.cpu_quota}' invalid "
                f"(expected like '200%' or '50%')"
            )

        # inflight 必须为正
        if self.max_inflight_bytes <= 0:
            errors.append("resources.max_inflight_bytes must be positive")
        if self.max_uid_inflight_bytes <= 0:
            errors.append("resources.max_uid_inflight_bytes must be positive")
        if self.max_memfd_bytes <= 0:
            errors.append("resources.max_memfd_bytes must be positive")
        if self.max_conn_queued_bytes <= 0:
            errors.append("resources.max_conn_queued_bytes must be positive")

        # jobs
        if self.max_concurrent_jobs < 1:
            errors.append("jobs.max_concurrent must be >= 1")
        if self.default_job_timeout < 1:
            errors.append("jobs.default_timeout must be >= 1")

        return errors

    def is_admin(self, uid: int) -> bool:
        """判断 UID 是否是管理员。"""
        return uid in self.admin_uids

    def save_to_file(self, path: str) -> None:
        """将配置保存到文件（JSON 格式）。

        Args:
            path: 目标文件路径
        """
        dir_path = os.path.dirname(path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self._data, f, indent=2, ensure_ascii=False)


# ============================================================
# 权限模板
# ============================================================


class PermissionRole:
    """单个权限角色的定义。

    Attributes:
        name: 角色名（admin / user / readonly）
        can_query_own_workspace: 可查询自己的 workspace
        can_query_other_workspace: 可查询其他 UID 的 workspace
        can_register_workspace: 可注册新 workspace
        can_archive_workspace: 可归档 workspace
        can_admin_operations: 可执行管理员操作（如 GC、schema migration、token 管理）
        can_submit_jobs: 可提交后台 job
        can_cancel_jobs: 可取消 job（只取消自己的）
        can_cancel_others_jobs: 可取消他人的 job（仅 admin）
    """

    def __init__(
        self,
        name: str,
        can_query_own_workspace: bool = True,
        can_query_other_workspace: bool = False,
        can_register_workspace: bool = True,
        can_archive_workspace: bool = False,
        can_admin_operations: bool = False,
        can_submit_jobs: bool = True,
        can_cancel_jobs: bool = True,
        can_cancel_others_jobs: bool = False,
    ):
        self.name = name
        self.can_query_own_workspace = can_query_own_workspace
        self.can_query_other_workspace = can_query_other_workspace
        self.can_register_workspace = can_register_workspace
        self.can_archive_workspace = can_archive_workspace
        self.can_admin_operations = can_admin_operations
        self.can_submit_jobs = can_submit_jobs
        self.can_cancel_jobs = can_cancel_jobs
        self.can_cancel_others_jobs = can_cancel_others_jobs

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return {
            "name": self.name,
            "can_query_own_workspace": self.can_query_own_workspace,
            "can_query_other_workspace": self.can_query_other_workspace,
            "can_register_workspace": self.can_register_workspace,
            "can_archive_workspace": self.can_archive_workspace,
            "can_admin_operations": self.can_admin_operations,
            "can_submit_jobs": self.can_submit_jobs,
            "can_cancel_jobs": self.can_cancel_jobs,
            "can_cancel_others_jobs": self.can_cancel_others_jobs,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "PermissionRole":
        """从字典反序列化。"""
        return cls(
            name=data["name"],
            can_query_own_workspace=data.get("can_query_own_workspace", True),
            can_query_other_workspace=data.get("can_query_other_workspace", False),
            can_register_workspace=data.get("can_register_workspace", True),
            can_archive_workspace=data.get("can_archive_workspace", False),
            can_admin_operations=data.get("can_admin_operations", False),
            can_submit_jobs=data.get("can_submit_jobs", True),
            can_cancel_jobs=data.get("can_cancel_jobs", True),
            can_cancel_others_jobs=data.get("can_cancel_others_jobs", False),
        )


class PermissionTemplate:
    """权限模板：内置三角色（admin / user / readonly）。

    用法：
        tpl = PermissionTemplate()
        role = tpl.get_role("admin")
        tpl.resolve_role(config, uid=1000)  # 返回 admin（因 UID 1000 在 admin_uids）
        tpl.resolve_role(config, uid=1001)  # 返回 user
    """

    # 内置角色定义
    BUILTIN_ROLES: Dict[str, PermissionRole] = {
        "admin": PermissionRole(
            name="admin",
            can_query_own_workspace=True,
            can_query_other_workspace=True,
            can_register_workspace=True,
            can_archive_workspace=True,
            can_admin_operations=True,
            can_submit_jobs=True,
            can_cancel_jobs=True,
            can_cancel_others_jobs=True,
        ),
        "user": PermissionRole(
            name="user",
            can_query_own_workspace=True,
            can_query_other_workspace=False,
            can_register_workspace=True,
            can_archive_workspace=False,
            can_admin_operations=False,
            can_submit_jobs=True,
            can_cancel_jobs=True,
            can_cancel_others_jobs=False,
        ),
        "readonly": PermissionRole(
            name="readonly",
            can_query_own_workspace=True,
            can_query_other_workspace=False,
            can_register_workspace=False,
            can_archive_workspace=False,
            can_admin_operations=False,
            can_submit_jobs=False,
            can_cancel_jobs=False,
            can_cancel_others_jobs=False,
        ),
    }

    def __init__(self, custom_roles: Optional[Dict[str, PermissionRole]] = None):
        """初始化权限模板。

        Args:
            custom_roles: 自定义角色（覆盖或扩展内置角色）
        """
        self._roles: Dict[str, PermissionRole] = dict(self.BUILTIN_ROLES)
        if custom_roles:
            self._roles.update(custom_roles)

    def get_role(self, role_name: str) -> Optional[PermissionRole]:
        """根据角色名获取角色定义。"""
        return self._roles.get(role_name)

    def list_roles(self) -> List[str]:
        """列出所有角色名。"""
        return list(self._roles.keys())

    def resolve_role(self, config: DaemonConfig, uid: int) -> PermissionRole:
        """根据 UID 解析对应的角色。

        规则：
        - UID 在 config.admin_uids 中 → admin
        - 其他 → user

        Args:
            config: DaemonConfig 实例
            uid: 用户 UID

        Returns:
            PermissionRole 实例（admin 或 user）
        """
        if config.is_admin(uid):
            return self._roles["admin"]
        return self._roles["user"]

    def to_dict(self) -> Dict[str, Any]:
        """序列化为字典。"""
        return {name: role.to_dict() for name, role in self._roles.items()}


# ============================================================
# Token 校验
# ============================================================


class TokenValidator:
    """TCP mTLS 模式下的 per-container token 校验。

    token 格式：`cw_<32hex>`（前缀 + 32 位十六进制）
    存储格式（JSON）：
        {
            "tokens": [
                {
                    "token_hash": "sha256hex",
                    "container_id": "container-abc",
                    "uid": 1000,
                    "role": "user",
                    "created_at": 1783698970.0,
                    "expires_at": 1783785370.0,
                    "revoked": false
                }
            ]
        }

    安全规则：
    - token 明文只在创建时返回一次，存储的是 sha256 hash
    - token 校验时比对 sha256(token) 与存储的 hash
    - token 可被 revoke（标记 revoked=true）
    - token 有过期时间
    """

    TOKEN_PREFIX = "cw_"
    TOKEN_HEX_LEN = 32

    def __init__(self, token_store_path: str = ""):
        self._store_path = token_store_path
        self._tokens: List[Dict[str, Any]] = []
        if token_store_path and os.path.isfile(token_store_path):
            self._load()

    # ----- 加载/保存 -----

    def _load(self) -> None:
        """从文件加载 token 存储。"""
        try:
            with open(self._store_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            self._tokens = data.get("tokens", [])
        except (json.JSONDecodeError, OSError):
            self._tokens = []

    def _save(self) -> None:
        """保存 token 存储到文件。"""
        if not self._store_path:
            return
        dir_path = os.path.dirname(self._store_path)
        if dir_path and not os.path.exists(dir_path):
            os.makedirs(dir_path, exist_ok=True)
        with open(self._store_path, "w", encoding="utf-8") as f:
            json.dump({"tokens": self._tokens}, f, indent=2, ensure_ascii=False)

    # ----- token 生成 -----

    def generate_token(
        self,
        container_id: str,
        uid: int,
        role: str = "user",
        expires_in: float = 86400.0,
    ) -> str:
        """生成一个新 token 并存储其 hash。

        Args:
            container_id: 容器标识
            uid: 持有者 UID
            role: 角色名（admin/user/readonly）
            expires_in: 过期时间（秒），默认 24 小时

        Returns:
            token 明文（只在此次返回，后续不再可见）
        """
        plaintext = self.TOKEN_PREFIX + secrets.token_hex(self.TOKEN_HEX_LEN // 2)
        token_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
        now = time.time()
        entry = {
            "token_hash": token_hash,
            "container_id": container_id,
            "uid": uid,
            "role": role,
            "created_at": now,
            "expires_at": now + expires_in,
            "revoked": False,
        }
        self._tokens.append(entry)
        self._save()
        return plaintext

    # ----- token 校验 -----

    def validate_token(self, token: str) -> Tuple[bool, Optional[Dict[str, Any]], str]:
        """校验 token 是否有效。

        Args:
            token: token 明文

        Returns:
            (is_valid, token_entry, error_message)
            - is_valid: True 如果 token 有效
            - token_entry: token 的元数据（uid、role、container_id）
            - error_message: 失败原因（空字符串表示成功）
        """
        if not token or not token.startswith(self.TOKEN_PREFIX):
            return False, None, "invalid token format"

        if len(token) != len(self.TOKEN_PREFIX) + self.TOKEN_HEX_LEN:
            return False, None, "invalid token length"

        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        now = time.time()

        for entry in self._tokens:
            if entry["token_hash"] != token_hash:
                continue

            if entry["revoked"]:
                return False, entry, "token revoked"

            if now > entry["expires_at"]:
                return False, entry, "token expired"

            return True, entry, ""

        return False, None, "token not found"

    # ----- token 管理 -----

    def revoke_token(self, token: str) -> bool:
        """撤销一个 token。

        Args:
            token: token 明文

        Returns:
            True 如果成功撤销，False 如果 token 不存在
        """
        token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
        for entry in self._tokens:
            if entry["token_hash"] == token_hash:
                entry["revoked"] = True
                self._save()
                return True
        return False

    def revoke_by_container(self, container_id: str) -> int:
        """撤销某个容器的所有 token。

        Args:
            container_id: 容器标识

        Returns:
            撤销的 token 数量
        """
        count = 0
        for entry in self._tokens:
            if entry["container_id"] == container_id and not entry["revoked"]:
                entry["revoked"] = True
                count += 1
        if count:
            self._save()
        return count

    def list_tokens(self, include_revoked: bool = False) -> List[Dict[str, Any]]:
        """列出所有 token（不返回明文 hash 之外的信息）。

        Args:
            include_revoked: 是否包含已撤销的 token

        Returns:
            token 列表（不含 token_hash，避免泄露）
        """
        result = []
        for entry in self._tokens:
            if not include_revoked and entry["revoked"]:
                continue
            result.append({
                "container_id": entry["container_id"],
                "uid": entry["uid"],
                "role": entry["role"],
                "created_at": entry["created_at"],
                "expires_at": entry["expires_at"],
                "revoked": entry["revoked"],
            })
        return result

    def cleanup_expired(self) -> int:
        """清理已过期或已撤销的 token。

        Returns:
            清理的 token 数量
        """
        now = time.time()
        original_count = len(self._tokens)
        self._tokens = [
            entry for entry in self._tokens
            if not entry["revoked"] and now <= entry["expires_at"]
        ]
        removed = original_count - len(self._tokens)
        if removed:
            self._save()
        return removed


# ============================================================
# 访问控制
# ============================================================


class AccessDeniedError(Exception):
    """访问被拒绝异常。"""


class AccessChecker:
    """UID 级别的 workspace 访问控制。

    职责：
    - 检查 UID 是否能查询指定 workspace
    - 检查 UID 是否能注册/归档 workspace
    - 检查路径是否合法（无 `..` 逃逸、无 symlink 逃逸）
    - 检查 UID 是否能执行 admin 操作

    用法：
        checker = AccessChecker(config, permission_template)
        checker.check_workspace_access(uid=1000, workspace_owner_uid=1000)
        checker.check_path_safety("/home/user1/work", workspace_root="/home/user1/work")
        checker.check_admin_operation(uid=0)
    """

    def __init__(self, config: DaemonConfig, template: PermissionTemplate):
        self._config = config
        self._template = template

    def resolve_role(self, uid: int) -> PermissionRole:
        """解析 UID 对应的角色。"""
        return self._template.resolve_role(self._config, uid)

    def check_workspace_access(
        self,
        uid: int,
        workspace_owner_uid: int,
        operation: str = "query",
    ) -> None:
        """检查 UID 是否能访问指定 workspace。

        Args:
            uid: 请求者 UID
            workspace_owner_uid: workspace 拥有者 UID
            operation: 操作类型（query/register/archive/admin）

        Raises:
            AccessDeniedError: 如果访问被拒绝
        """
        role = self.resolve_role(uid)

        # admin 操作检查
        if operation == "admin":
            if not role.can_admin_operations:
                raise AccessDeniedError(
                    f"UID {uid} (role={role.name}) cannot perform admin operations"
                )
            return

        # 归档操作检查
        if operation == "archive":
            if not role.can_archive_workspace:
                raise AccessDeniedError(
                    f"UID {uid} (role={role.name}) cannot archive workspaces"
                )
            return

        # 注册操作检查
        if operation == "register":
            if not role.can_register_workspace:
                raise AccessDeniedError(
                    f"UID {uid} (role={role.name}) cannot register workspaces"
                )
            return

        # 默认是 query 操作
        if workspace_owner_uid == uid:
            if not role.can_query_own_workspace:
                raise AccessDeniedError(
                    f"UID {uid} (role={role.name}) cannot query own workspace"
                )
        else:
            # 跨 UID 查询
            if not role.can_query_other_workspace:
                # 检查配置是否允许跨 UID 查询
                if not self._config.allow_cross_uid_query:
                    raise AccessDeniedError(
                        f"UID {uid} (role={role.name}) cannot query workspace "
                        f"owned by UID {workspace_owner_uid} "
                        f"(cross_uid_query disabled)"
                    )

    def check_path_safety(
        self,
        path: str,
        workspace_root: str,
    ) -> None:
        """检查路径是否安全（无 `..` 逃逸、不超出 workspace_root）。

        Args:
            path: 待检查的路径
            workspace_root: workspace 根目录

        Raises:
            AccessDeniedError: 如果路径不安全
        """
        # 1. 检查路径中是否包含 `..`（路径逃逸）
        normalized = os.path.normpath(path)
        if ".." in normalized.split(os.sep):
            raise AccessDeniedError(
                f"path '{path}' contains '..' escape attempt"
            )

        # 2. 检查路径是否在 workspace_root 内
        abs_path = os.path.abspath(path)
        abs_root = os.path.abspath(workspace_root)
        if not abs_path.startswith(abs_root + os.sep) and abs_path != abs_root:
            raise AccessDeniedError(
                f"path '{path}' is outside workspace root '{workspace_root}'"
            )

    def check_symlink_escape(
        self,
        path: str,
        workspace_root: str,
    ) -> None:
        """检查路径解析后是否逃逸 workspace_root（symlink 逃逸检测）。

        使用 os.path.realpath 解析符号链接后检查是否仍在 workspace_root 内。

        Args:
            path: 待检查的路径
            workspace_root: workspace 根目录

        Raises:
            AccessDeniedError: 如果路径通过 symlink 逃逸
        """
        real_path = os.path.realpath(path)
        real_root = os.path.realpath(workspace_root)
        if not real_path.startswith(real_root + os.sep) and real_path != real_root:
            raise AccessDeniedError(
                f"path '{path}' (realpath '{real_path}') escapes workspace root "
                f"'{workspace_root}' (realpath '{real_root}') via symlink"
            )

    def check_tcp_token(
        self,
        token: str,
        config: DaemonConfig,
        validator: TokenValidator,
    ) -> Tuple[int, str]:
        """检查 TCP 连接的 token 是否有效。

        Args:
            token: 客户端提供的 token 明文
            config: DaemonConfig 实例
            validator: TokenValidator 实例

        Returns:
            (uid, role) 如果 token 有效

        Raises:
            AccessDeniedError: 如果 token 无效或 TCP 模式不需要 token
        """
        if not config.require_token_for_tcp:
            # 不需要 token 时返回默认 user 角色
            return (-1, "user")

        if not token:
            raise AccessDeniedError("TCP token required but not provided")

        is_valid, entry, error_msg = validator.validate_token(token)
        if not is_valid:
            raise AccessDeniedError(f"TCP token invalid: {error_msg}")

        return (entry["uid"], entry["role"])

    def check_job_operation(
        self,
        uid: int,
        operation: str,
        job_owner_uid: Optional[int] = None,
    ) -> None:
        """检查 UID 是否能执行 job 相关操作。

        Args:
            uid: 请求者 UID
            operation: 操作类型（submit/cancel）
            job_owner_uid: job 拥有者 UID（cancel 操作时需要）

        Raises:
            AccessDeniedError: 如果操作被拒绝
        """
        role = self.resolve_role(uid)

        if operation == "submit":
            if not role.can_submit_jobs:
                raise AccessDeniedError(
                    f"UID {uid} (role={role.name}) cannot submit jobs"
                )
        elif operation == "cancel":
            if not role.can_cancel_jobs:
                raise AccessDeniedError(
                    f"UID {uid} (role={role.name}) cannot cancel jobs"
                )
            # 检查是否能取消他人的 job
            if job_owner_uid is not None and job_owner_uid != uid:
                if not role.can_cancel_others_jobs:
                    raise AccessDeniedError(
                        f"UID {uid} (role={role.name}) cannot cancel jobs "
                        f"owned by UID {job_owner_uid}"
                    )
        else:
            raise AccessDeniedError(f"unknown job operation: {operation}")


# ============================================================
# 工具函数
# ============================================================


def _deep_merge(base: Dict[str, Any], override: Dict[str, Any]) -> Dict[str, Any]:
    """深度合并两个字典，override 优先。

    Args:
        base: 基础字典
        override: 覆盖字典

    Returns:
        合并后的新字典
    """
    result = dict(base)
    for key, val in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(val, dict):
            result[key] = _deep_merge(result[key], val)
        else:
            result[key] = val
    return result


def _is_valid_size_string(s: str) -> bool:
    """检查字符串是否是合法的尺寸格式（如 '1G'、'512M'、'2048'）。

    合法格式：数字 + 可选单位（K/M/G/T，大小写不敏感）。
    """
    if not s:
        return False
    s = s.strip()
    if not s:
        return False
    # 纯数字
    if s.isdigit():
        return True
    # 数字 + 单位
    if len(s) < 2:
        return False
    num_part = s[:-1]
    unit_part = s[-1].upper()
    if unit_part not in ("K", "M", "G", "T"):
        return False
    try:
        val = int(num_part)
        return val > 0  # 拒绝负数
    except ValueError:
        return False


def _is_valid_percent_string(s: str) -> bool:
    """检查字符串是否是合法的百分比格式（如 '200%'、'50%'）。

    合法格式：正整数 + '%'。
    """
    if not s or not s.endswith("%"):
        return False
    num_part = s[:-1]
    try:
        val = int(num_part)
        return val > 0
    except ValueError:
        return False


def generate_default_config_file(path: str) -> None:
    """生成默认配置文件（JSON 格式）。

    用于初次部署时生成配置模板。

    Args:
        path: 目标文件路径
    """
    config = DaemonConfig.default()
    config.save_to_file(path)


def generate_permission_template_file(path: str) -> None:
    """生成权限模板文件（JSON 格式）。

    Args:
        path: 目标文件路径
    """
    tpl = PermissionTemplate()
    dir_path = os.path.dirname(path)
    if dir_path and not os.path.exists(dir_path):
        os.makedirs(dir_path, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tpl.to_dict(), f, indent=2, ensure_ascii=False)
