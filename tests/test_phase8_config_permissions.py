"""Phase 8.1: config 文件和权限模板测试。

测试覆盖：
1. DaemonConfig：加载、合并默认值、属性访问、校验
2. PermissionRole / PermissionTemplate：角色定义、角色解析
3. TokenValidator：token 生成、校验、撤销、清理
4. AccessChecker：workspace 访问控制、路径安全、symlink 逃逸、TCP token

设计参考：
- docs/design/enterprise-daemon-shared-snapshot-plan.md §Phase 8
- docs/design/daemon-ipc-security.md §1-§5
- 安全测试：user1 查询 user2 workspace 应拒绝、
  relative path 包含 `..` 应拒绝、
  workspace root symlink 逃逸应拒绝、
  TCP 无 token 或错误 token 应拒绝
"""

import json
import os
import tempfile
import time
import pytest

from server.daemon_config import (
    DaemonConfig,
    PermissionRole,
    PermissionTemplate,
    TokenValidator,
    AccessChecker,
    AccessDeniedError,
    DEFAULT_CONFIG,
    generate_default_config_file,
    generate_permission_template_file,
    _deep_merge,
    _is_valid_size_string,
    _is_valid_percent_string,
)


# ============================================================
# 工具函数测试
# ============================================================


class TestDeepMerge:
    """_deep_merge 函数测试。"""

    def test_simple_merge(self):
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _deep_merge(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_nested_merge(self):
        base = {"a": {"x": 1, "y": 2}, "b": 3}
        override = {"a": {"y": 10, "z": 20}}
        result = _deep_merge(base, override)
        assert result == {"a": {"x": 1, "y": 10, "z": 20}, "b": 3}

    def test_override_dict_with_non_dict(self):
        base = {"a": {"x": 1}}
        override = {"a": 5}
        result = _deep_merge(base, override)
        assert result == {"a": 5}

    def test_empty_override(self):
        base = {"a": 1}
        result = _deep_merge(base, {})
        assert result == {"a": 1}

    def test_empty_base(self):
        result = _deep_merge({}, {"a": 1})
        assert result == {"a": 1}

    def test_does_not_mutate_inputs(self):
        base = {"a": {"x": 1}}
        override = {"a": {"y": 2}}
        _deep_merge(base, override)
        assert base == {"a": {"x": 1}}
        assert override == {"a": {"y": 2}}


class TestIsValidSizeString:
    """_is_valid_size_string 函数测试。"""

    @pytest.mark.parametrize("s", ["1G", "512M", "256K", "2T", "2048", "100"])
    def test_valid_sizes(self, s):
        assert _is_valid_size_string(s) is True

    @pytest.mark.parametrize("s", ["", "abc", "1X", "G", "-1G", "1.5G"])
    def test_invalid_sizes(self, s):
        assert _is_valid_size_string(s) is False


class TestIsValidPercentString:
    """_is_valid_percent_string 函数测试。"""

    @pytest.mark.parametrize("s", ["200%", "50%", "100%", "1%"])
    def test_valid_percents(self, s):
        assert _is_valid_percent_string(s) is True

    @pytest.mark.parametrize("s", ["", "200", "abc%", "-50%", "0%"])
    def test_invalid_percents(self, s):
        assert _is_valid_percent_string(s) is False


# ============================================================
# DaemonConfig 测试
# ============================================================


class TestDaemonConfigLoad:
    """DaemonConfig 加载测试。"""

    def test_default_config(self):
        cfg = DaemonConfig.default()
        assert cfg.socket_path == "/var/run/callwarden.sock"
        assert cfg.data_root == "/var/lib/callwarden"
        assert cfg.tcp_enabled is False
        assert cfg.tcp_port == 8765

    def test_load_from_dict_empty(self):
        cfg = DaemonConfig.load_from_dict({})
        # 空字典应使用默认值
        assert cfg.socket_path == DEFAULT_CONFIG["socket_path"]
        assert cfg.memory_max == DEFAULT_CONFIG["resources"]["memory_max"]

    def test_load_from_dict_partial(self):
        cfg = DaemonConfig.load_from_dict({
            "socket_path": "/tmp/custom.sock",
            "tcp": {"port": 9999},
        })
        assert cfg.socket_path == "/tmp/custom.sock"
        assert cfg.tcp_port == 9999
        # 未覆盖的部分应使用默认值
        assert cfg.data_root == DEFAULT_CONFIG["data_root"]
        assert cfg.tcp_enabled == DEFAULT_CONFIG["tcp"]["enabled"]

    def test_load_from_dict_nested(self):
        cfg = DaemonConfig.load_from_dict({
            "resources": {"memory_max": "2G", "cpu_quota": "300%"}
        })
        assert cfg.memory_max == "2G"
        assert cfg.cpu_quota == "300%"
        # 其他资源项保持默认
        assert cfg.max_inflight_bytes == DEFAULT_CONFIG["resources"]["max_inflight_bytes"]

    def test_load_from_file_not_exist(self):
        cfg = DaemonConfig.load_from_file("/nonexistent/path/daemon.json")
        assert cfg.socket_path == DEFAULT_CONFIG["socket_path"]

    def test_load_from_file_valid(self, tmp_path):
        config_data = {
            "socket_path": "/tmp/test.sock",
            "tcp": {"enabled": True, "port": 12345},
            "resources": {"memory_max": "2G"},
        }
        config_file = tmp_path / "daemon.json"
        config_file.write_text(json.dumps(config_data), encoding="utf-8")

        cfg = DaemonConfig.load_from_file(str(config_file))
        assert cfg.socket_path == "/tmp/test.sock"
        assert cfg.tcp_enabled is True
        assert cfg.tcp_port == 12345
        assert cfg.memory_max == "2G"


class TestDaemonConfigProperties:
    """DaemonConfig 属性访问测试。"""

    def test_socket_path(self):
        cfg = DaemonConfig.load_from_dict({"socket_path": "/tmp/test.sock"})
        assert cfg.socket_path == "/tmp/test.sock"

    def test_data_root(self):
        cfg = DaemonConfig.load_from_dict({"data_root": "/var/lib/cw"})
        assert cfg.data_root == "/var/lib/cw"

    def test_registry_db_path(self):
        cfg = DaemonConfig.load_from_dict({"data_root": "/var/lib/cw"})
        # 跨平台兼容：Windows 用 \，Linux 用 /
        assert cfg.registry_db_path.replace("\\", "/") == "/var/lib/cw/registry.db"

    def test_cas_db_path(self):
        cfg = DaemonConfig.load_from_dict({"data_root": "/var/lib/cw"})
        assert cfg.cas_db_path.replace("\\", "/") == "/var/lib/cw/cas.db"

    def test_tcp_properties(self):
        cfg = DaemonConfig.load_from_dict({
            "tcp": {
                "enabled": True,
                "port": 9999,
                "tls_cert": "/etc/cert.crt",
                "tls_key": "/etc/cert.key",
                "ca_cert": "/etc/ca.crt",
            }
        })
        assert cfg.tcp_enabled is True
        assert cfg.tcp_port == 9999
        assert cfg.tcp_tls_cert == "/etc/cert.crt"
        assert cfg.tcp_tls_key == "/etc/cert.key"
        assert cfg.tcp_ca_cert == "/etc/ca.crt"

    def test_resource_properties(self):
        cfg = DaemonConfig.load_from_dict({
            "resources": {
                "memory_max": "2G",
                "cpu_quota": "300%",
                "max_inflight_bytes": 4294967296,
                "max_uid_inflight_bytes": 1073741824,
                "max_memfd_bytes": 536870912,
                "max_conn_queued_bytes": 536870912,
            }
        })
        assert cfg.memory_max == "2G"
        assert cfg.cpu_quota == "300%"
        assert cfg.max_inflight_bytes == 4294967296
        assert cfg.max_uid_inflight_bytes == 1073741824
        assert cfg.max_memfd_bytes == 536870912
        assert cfg.max_conn_queued_bytes == 536870912

    def test_security_properties(self):
        cfg = DaemonConfig.load_from_dict({
            "security": {
                "admin_uids": [0, 1000],
                "allow_cross_uid_query": True,
                "require_token_for_tcp": True,
                "audit_log_path": "/var/log/cw/audit.log",
                "token_store_path": "/var/lib/cw/tokens.json",
            }
        })
        assert cfg.admin_uids == [0, 1000]
        assert cfg.allow_cross_uid_query is True
        assert cfg.require_token_for_tcp is True
        assert cfg.audit_log_path == "/var/log/cw/audit.log"
        assert cfg.token_store_path == "/var/lib/cw/tokens.json"

    def test_jobs_properties(self):
        cfg = DaemonConfig.load_from_dict({
            "jobs": {
                "max_concurrent": 8,
                "default_timeout": 600,
                "cancel_check_interval": 1.0,
            }
        })
        assert cfg.max_concurrent_jobs == 8
        assert cfg.default_job_timeout == 600
        assert cfg.cancel_check_interval == 1.0


class TestDaemonConfigGet:
    """DaemonConfig.get() 方法测试。"""

    def test_get_simple(self):
        cfg = DaemonConfig.default()
        assert cfg.get("socket_path") == "/var/run/callwarden.sock"

    def test_get_nested(self):
        cfg = DaemonConfig.default()
        assert cfg.get("resources.memory_max") == "1G"
        assert cfg.get("tcp.port") == 8765

    def test_get_deep_nested(self):
        cfg = DaemonConfig.default()
        assert cfg.get("security.admin_uids") == [0]

    def test_get_missing_key_returns_default(self):
        cfg = DaemonConfig.default()
        assert cfg.get("nonexistent") is None
        assert cfg.get("nonexistent", "fallback") == "fallback"

    def test_get_missing_nested_key(self):
        cfg = DaemonConfig.default()
        assert cfg.get("resources.nonexistent") is None
        assert cfg.get("tcp.nonexistent", 42) == 42


class TestDaemonConfigValidate:
    """DaemonConfig.validate() 方法测试。"""

    def test_default_config_valid(self):
        cfg = DaemonConfig.default()
        errors = cfg.validate()
        assert errors == []

    def test_tcp_enabled_without_cert(self):
        cfg = DaemonConfig.load_from_dict({
            "tcp": {"enabled": True, "tls_cert": "", "tls_key": ""},
            "security": {"token_store_path": "/tmp/tokens.json"},
        })
        errors = cfg.validate()
        assert any("tls_cert" in e for e in errors)
        assert any("tls_key" in e for e in errors)

    def test_tcp_enabled_with_cert_valid(self):
        cfg = DaemonConfig.load_from_dict({
            "tcp": {
                "enabled": True,
                "tls_cert": "/etc/cert.crt",
                "tls_key": "/etc/cert.key",
            },
            "security": {"token_store_path": "/tmp/tokens.json"},
        })
        errors = cfg.validate()
        # 证书和 key 都填了，不应有相关错误
        assert not any("tls_cert" in e for e in errors)
        assert not any("tls_key" in e for e in errors)

    def test_tcp_enabled_require_token_no_store(self):
        cfg = DaemonConfig.load_from_dict({
            "tcp": {
                "enabled": True,
                "tls_cert": "/etc/cert.crt",
                "tls_key": "/etc/cert.key",
            },
            "security": {
                "require_token_for_tcp": True,
                "token_store_path": "",
            },
        })
        errors = cfg.validate()
        assert any("token_store_path" in e for e in errors)

    def test_empty_admin_uids(self):
        cfg = DaemonConfig.load_from_dict({
            "security": {"admin_uids": []},
        })
        errors = cfg.validate()
        assert any("admin_uids" in e for e in errors)

    def test_invalid_port(self):
        cfg = DaemonConfig.load_from_dict({"tcp": {"port": 70000}})
        errors = cfg.validate()
        assert any("port" in e for e in errors)

    def test_invalid_memory_max(self):
        cfg = DaemonConfig.load_from_dict({"resources": {"memory_max": "abc"}})
        errors = cfg.validate()
        assert any("memory_max" in e for e in errors)

    def test_invalid_cpu_quota(self):
        cfg = DaemonConfig.load_from_dict({"resources": {"cpu_quota": "abc"}})
        errors = cfg.validate()
        assert any("cpu_quota" in e for e in errors)

    def test_negative_inflight_bytes(self):
        cfg = DaemonConfig.load_from_dict({
            "resources": {"max_inflight_bytes": -1}
        })
        errors = cfg.validate()
        assert any("max_inflight_bytes" in e for e in errors)

    def test_invalid_max_concurrent(self):
        cfg = DaemonConfig.load_from_dict({"jobs": {"max_concurrent": 0}})
        errors = cfg.validate()
        assert any("max_concurrent" in e for e in errors)


class TestDaemonConfigIsAdmin:
    """DaemonConfig.is_admin() 方法测试。"""

    def test_admin_uid(self):
        cfg = DaemonConfig.load_from_dict({"security": {"admin_uids": [0, 1000]}})
        assert cfg.is_admin(0) is True
        assert cfg.is_admin(1000) is True

    def test_non_admin_uid(self):
        cfg = DaemonConfig.load_from_dict({"security": {"admin_uids": [0]}})
        assert cfg.is_admin(1001) is False

    def test_default_admin_uids(self):
        cfg = DaemonConfig.default()
        assert cfg.is_admin(0) is True
        assert cfg.is_admin(1000) is False


class TestDaemonConfigSaveToFile:
    """DaemonConfig.save_to_file() 方法测试。"""

    def test_save_and_reload(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({"socket_path": "/tmp/test.sock"})
        config_file = str(tmp_path / "daemon.json")
        cfg.save_to_file(config_file)

        assert os.path.isfile(config_file)
        with open(config_file, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["socket_path"] == "/tmp/test.sock"

    def test_save_creates_dir(self, tmp_path):
        cfg = DaemonConfig.default()
        config_file = str(tmp_path / "subdir" / "daemon.json")
        cfg.save_to_file(config_file)
        assert os.path.isfile(config_file)


class TestDaemonConfigToDict:
    """DaemonConfig.to_dict() 方法测试。"""

    def test_returns_dict(self):
        cfg = DaemonConfig.default()
        d = cfg.to_dict()
        assert isinstance(d, dict)
        assert "socket_path" in d
        assert "resources" in d

    def test_includes_overrides(self):
        cfg = DaemonConfig.load_from_dict({"socket_path": "/tmp/custom.sock"})
        d = cfg.to_dict()
        assert d["socket_path"] == "/tmp/custom.sock"

    def test_is_deep_copy(self):
        cfg = DaemonConfig.default()
        d1 = cfg.to_dict()
        d1["socket_path"] = "/modified.sock"
        # 修改返回的字典不应影响原配置
        assert cfg.socket_path == "/var/run/callwarden.sock"


# ============================================================
# PermissionRole 测试
# ============================================================


class TestPermissionRole:
    """PermissionRole 测试。"""

    def test_default_values(self):
        role = PermissionRole(name="test")
        assert role.name == "test"
        assert role.can_query_own_workspace is True
        assert role.can_query_other_workspace is False
        assert role.can_register_workspace is True
        assert role.can_archive_workspace is False
        assert role.can_admin_operations is False

    def test_custom_values(self):
        role = PermissionRole(
            name="custom",
            can_query_own_workspace=False,
            can_admin_operations=True,
        )
        assert role.can_query_own_workspace is False
        assert role.can_admin_operations is True

    def test_to_dict(self):
        role = PermissionRole(name="admin", can_admin_operations=True)
        d = role.to_dict()
        assert d["name"] == "admin"
        assert d["can_admin_operations"] is True
        assert "can_query_own_workspace" in d

    def test_from_dict(self):
        data = {
            "name": "readonly",
            "can_query_own_workspace": True,
            "can_register_workspace": False,
        }
        role = PermissionRole.from_dict(data)
        assert role.name == "readonly"
        assert role.can_query_own_workspace is True
        assert role.can_register_workspace is False

    def test_roundtrip(self):
        original = PermissionRole(
            name="custom",
            can_admin_operations=True,
            can_cancel_others_jobs=True,
        )
        data = original.to_dict()
        restored = PermissionRole.from_dict(data)
        assert restored.name == original.name
        assert restored.can_admin_operations == original.can_admin_operations
        assert restored.can_cancel_others_jobs == original.can_cancel_others_jobs


# ============================================================
# PermissionTemplate 测试
# ============================================================


class TestPermissionTemplate:
    """PermissionTemplate 测试。"""

    def test_builtin_roles_exist(self):
        tpl = PermissionTemplate()
        assert tpl.get_role("admin") is not None
        assert tpl.get_role("user") is not None
        assert tpl.get_role("readonly") is not None

    def test_admin_role_permissions(self):
        tpl = PermissionTemplate()
        admin = tpl.get_role("admin")
        assert admin.can_query_other_workspace is True
        assert admin.can_admin_operations is True
        assert admin.can_archive_workspace is True
        assert admin.can_cancel_others_jobs is True

    def test_user_role_permissions(self):
        tpl = PermissionTemplate()
        user = tpl.get_role("user")
        assert user.can_query_own_workspace is True
        assert user.can_query_other_workspace is False
        assert user.can_admin_operations is False
        assert user.can_register_workspace is True

    def test_readonly_role_permissions(self):
        tpl = PermissionTemplate()
        readonly = tpl.get_role("readonly")
        assert readonly.can_query_own_workspace is True
        assert readonly.can_register_workspace is False
        assert readonly.can_submit_jobs is False
        assert readonly.can_cancel_jobs is False

    def test_list_roles(self):
        tpl = PermissionTemplate()
        roles = tpl.list_roles()
        assert "admin" in roles
        assert "user" in roles
        assert "readonly" in roles

    def test_custom_role_override(self):
        custom = PermissionRole(name="custom", can_admin_operations=True)
        tpl = PermissionTemplate(custom_roles={"custom": custom})
        assert tpl.get_role("custom") is not None
        assert tpl.get_role("custom").can_admin_operations is True

    def test_resolve_role_admin(self):
        cfg = DaemonConfig.load_from_dict({"security": {"admin_uids": [0, 1000]}})
        tpl = PermissionTemplate()
        role = tpl.resolve_role(cfg, uid=1000)
        assert role.name == "admin"

    def test_resolve_role_user(self):
        cfg = DaemonConfig.load_from_dict({"security": {"admin_uids": [0]}})
        tpl = PermissionTemplate()
        role = tpl.resolve_role(cfg, uid=1001)
        assert role.name == "user"

    def test_to_dict(self):
        tpl = PermissionTemplate()
        d = tpl.to_dict()
        assert "admin" in d
        assert "user" in d
        assert "readonly" in d
        assert d["admin"]["name"] == "admin"


# ============================================================
# TokenValidator 测试
# ============================================================


class TestTokenValidatorGenerate:
    """TokenValidator token 生成测试。"""

    def test_generate_returns_string(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        token = validator.generate_token("container-1", uid=1000)
        assert isinstance(token, str)
        assert token.startswith("cw_")

    def test_generate_token_length(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        token = validator.generate_token("container-1", uid=1000)
        # 前缀 + 32 hex 字符
        assert len(token) == 3 + 32  # "cw_" + 32 hex

    def test_generate_token_uniqueness(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        t1 = validator.generate_token("c1", uid=1000)
        t2 = validator.generate_token("c1", uid=1000)
        assert t1 != t2

    def test_generate_token_stores_hash(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        token = validator.generate_token("container-1", uid=1000, role="user")
        assert len(validator._tokens) == 1
        entry = validator._tokens[0]
        assert entry["container_id"] == "container-1"
        assert entry["uid"] == 1000
        assert entry["role"] == "user"
        assert entry["revoked"] is False
        # 不存储明文
        assert "token" not in entry
        assert entry["token_hash"] != token

    def test_generate_token_saves_to_file(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        validator.generate_token("container-1", uid=1000)

        assert os.path.isfile(store)
        with open(store, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert len(data["tokens"]) == 1


class TestTokenValidatorValidate:
    """TokenValidator token 校验测试。"""

    def test_validate_valid_token(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        token = validator.generate_token("c1", uid=1000, expires_in=3600)

        is_valid, entry, msg = validator.validate_token(token)
        assert is_valid is True
        assert entry is not None
        assert entry["uid"] == 1000
        assert msg == ""

    def test_validate_invalid_format_no_prefix(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        is_valid, entry, msg = validator.validate_token("invalid_token")
        assert is_valid is False
        assert "format" in msg

    def test_validate_invalid_format_short(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        is_valid, entry, msg = validator.validate_token("cw_short")
        assert is_valid is False
        assert "length" in msg

    def test_validate_nonexistent_token(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        fake_token = "cw_" + "a" * 32
        is_valid, entry, msg = validator.validate_token(fake_token)
        assert is_valid is False
        assert "not found" in msg

    def test_validate_expired_token(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        # 生成一个已过期的 token
        token = validator.generate_token("c1", uid=1000, expires_in=-1)

        is_valid, entry, msg = validator.validate_token(token)
        assert is_valid is False
        assert "expired" in msg

    def test_validate_revoked_token(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        token = validator.generate_token("c1", uid=1000)
        validator.revoke_token(token)

        is_valid, entry, msg = validator.validate_token(token)
        assert is_valid is False
        assert "revoked" in msg

    def test_validate_empty_token(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        is_valid, entry, msg = validator.validate_token("")
        assert is_valid is False


class TestTokenValidatorRevoke:
    """TokenValidator token 撤销测试。"""

    def test_revoke_existing_token(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        token = validator.generate_token("c1", uid=1000)

        result = validator.revoke_token(token)
        assert result is True

        # 确认 token 已被标记为 revoked
        is_valid, entry, msg = validator.validate_token(token)
        assert is_valid is False
        assert entry["revoked"] is True

    def test_revoke_nonexistent_token(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        fake_token = "cw_" + "b" * 32
        result = validator.revoke_token(fake_token)
        assert result is False

    def test_revoke_by_container(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        t1 = validator.generate_token("container-A", uid=1000)
        t2 = validator.generate_token("container-A", uid=1001)
        t3 = validator.generate_token("container-B", uid=1000)

        count = validator.revoke_by_container("container-A")
        assert count == 2

        # container-A 的 token 都被撤销
        assert validator.validate_token(t1)[0] is False
        assert validator.validate_token(t2)[0] is False
        # container-B 的 token 仍然有效
        assert validator.validate_token(t3)[0] is True

    def test_revoke_by_container_no_match(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        validator.generate_token("container-A", uid=1000)
        count = validator.revoke_by_container("nonexistent")
        assert count == 0


class TestTokenValidatorListAndCleanup:
    """TokenValidator list/cleanup 测试。"""

    def test_list_tokens_excludes_revoked(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        t1 = validator.generate_token("c1", uid=1000)
        t2 = validator.generate_token("c2", uid=1001)
        validator.revoke_token(t1)

        tokens = validator.list_tokens()
        assert len(tokens) == 1
        assert tokens[0]["uid"] == 1001

    def test_list_tokens_include_revoked(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        t1 = validator.generate_token("c1", uid=1000)
        validator.generate_token("c2", uid=1001)
        validator.revoke_token(t1)

        tokens = validator.list_tokens(include_revoked=True)
        assert len(tokens) == 2

    def test_list_tokens_no_hash_leak(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        validator.generate_token("c1", uid=1000)

        tokens = validator.list_tokens()
        for t in tokens:
            assert "token_hash" not in t
            assert "token" not in t

    def test_cleanup_expired(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        # 生成一个已过期的 token
        validator.generate_token("c1", uid=1000, expires_in=-1)
        # 生成一个有效的 token
        validator.generate_token("c2", uid=1000, expires_in=3600)

        removed = validator.cleanup_expired()
        assert removed == 1
        assert len(validator._tokens) == 1

    def test_cleanup_revoked(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        t1 = validator.generate_token("c1", uid=1000)
        validator.generate_token("c2", uid=1000)
        validator.revoke_token(t1)

        removed = validator.cleanup_expired()
        assert removed == 1

    def test_cleanup_nothing_to_remove(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        validator = TokenValidator(store)
        validator.generate_token("c1", uid=1000, expires_in=3600)

        removed = validator.cleanup_expired()
        assert removed == 0


class TestTokenValidatorLoad:
    """TokenValidator 文件加载测试。"""

    def test_load_from_existing_file(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        # 第一次创建并生成 token
        validator1 = TokenValidator(store)
        token = validator1.generate_token("c1", uid=1000)

        # 重新加载
        validator2 = TokenValidator(store)
        assert len(validator2._tokens) == 1

        # 原 token 应该仍然有效
        is_valid, _, _ = validator2.validate_token(token)
        assert is_valid is True

    def test_load_from_nonexistent_file(self, tmp_path):
        validator = TokenValidator(str(tmp_path / "nonexistent.json"))
        assert validator._tokens == []

    def test_load_from_corrupted_file(self, tmp_path):
        store = str(tmp_path / "tokens.json")
        # 写入损坏的 JSON
        with open(store, "w", encoding="utf-8") as f:
            f.write("invalid json content")

        validator = TokenValidator(store)
        assert validator._tokens == []


# ============================================================
# AccessChecker 测试
# ============================================================


class TestAccessCheckerWorkspaceAccess:
    """AccessChecker workspace 访问控制测试。

    安全测试覆盖：
    - user1 查询 user2 workspace 应拒绝
    - admin 可以查询任何 workspace
    """

    def _make_checker(self, admin_uids=None, allow_cross=False):
        cfg = DaemonConfig.load_from_dict({
            "security": {
                "admin_uids": admin_uids or [0],
                "allow_cross_uid_query": allow_cross,
            }
        })
        tpl = PermissionTemplate()
        return AccessChecker(cfg, tpl)

    def test_user_query_own_workspace(self):
        checker = self._make_checker()
        # 不应抛异常
        checker.check_workspace_access(uid=1000, workspace_owner_uid=1000)

    def test_user_query_other_workspace_denied(self):
        checker = self._make_checker()
        with pytest.raises(AccessDeniedError) as exc_info:
            checker.check_workspace_access(uid=1000, workspace_owner_uid=1001)
        assert "cannot query" in str(exc_info.value).lower() or "cross_uid" in str(exc_info.value).lower()

    def test_admin_query_other_workspace(self):
        checker = self._make_checker(admin_uids=[0, 1000])
        # admin (uid=1000) 查询 uid=1001 的 workspace
        checker.check_workspace_access(uid=1000, workspace_owner_uid=1001)

    def test_cross_uid_query_allowed(self):
        checker = self._make_checker(allow_cross=True)
        # 配置允许跨 UID 查询时，普通用户也可以查询
        checker.check_workspace_access(uid=1000, workspace_owner_uid=1001)

    def test_register_own_workspace(self):
        checker = self._make_checker()
        checker.check_workspace_access(
            uid=1000, workspace_owner_uid=1000, operation="register"
        )

    def test_readonly_cannot_register(self):
        cfg = DaemonConfig.load_from_dict({"security": {"admin_uids": [0]}})
        tpl = PermissionTemplate()
        checker = AccessChecker(cfg, tpl)
        # readonly 角色不能注册——通过强制使用 readonly 角色测试
        role = tpl.get_role("readonly")
        # 直接验证角色权限
        assert role.can_register_workspace is False

    def test_user_cannot_archive(self):
        checker = self._make_checker()
        with pytest.raises(AccessDeniedError):
            checker.check_workspace_access(
                uid=1000, workspace_owner_uid=1000, operation="archive"
            )

    def test_admin_can_archive(self):
        checker = self._make_checker(admin_uids=[0, 1000])
        checker.check_workspace_access(
            uid=1000, workspace_owner_uid=1000, operation="archive"
        )

    def test_admin_operation_by_admin(self):
        checker = self._make_checker(admin_uids=[0, 1000])
        checker.check_workspace_access(uid=1000, workspace_owner_uid=1000, operation="admin")

    def test_admin_operation_by_user_denied(self):
        checker = self._make_checker(admin_uids=[0])
        with pytest.raises(AccessDeniedError):
            checker.check_workspace_access(uid=1000, workspace_owner_uid=1000, operation="admin")


class TestAccessCheckerPathSafety:
    """AccessChecker 路径安全测试。

    安全测试覆盖：
    - relative path 包含 `..` 应拒绝
    - workspace root symlink 逃逸应拒绝
    """

    def test_safe_path(self, tmp_path):
        cfg = DaemonConfig.default()
        tpl = PermissionTemplate()
        checker = AccessChecker(cfg, tpl)

        workspace_root = str(tmp_path)
        safe_path = os.path.join(str(tmp_path), "subdir", "file.py")
        os.makedirs(os.path.dirname(safe_path), exist_ok=True)
        # 不应抛异常
        checker.check_path_safety(safe_path, workspace_root)

    def test_path_with_dotdot_denied(self, tmp_path):
        cfg = DaemonConfig.default()
        tpl = PermissionTemplate()
        checker = AccessChecker(cfg, tpl)

        workspace_root = str(tmp_path)
        bad_path = os.path.join(str(tmp_path), "..", "escape.py")
        with pytest.raises(AccessDeniedError) as exc_info:
            checker.check_path_safety(bad_path, workspace_root)
        assert ".." in str(exc_info.value) or "escape" in str(exc_info.value).lower()

    def test_path_outside_workspace_denied(self, tmp_path):
        cfg = DaemonConfig.default()
        tpl = PermissionTemplate()
        checker = AccessChecker(cfg, tpl)

        workspace_root = str(tmp_path / "workspace")
        os.makedirs(workspace_root, exist_ok=True)
        outside_path = str(tmp_path / "outside" / "file.py")
        os.makedirs(os.path.dirname(outside_path), exist_ok=True)
        with pytest.raises(AccessDeniedError):
            checker.check_path_safety(outside_path, workspace_root)

    def test_symlink_escape_denied(self, tmp_path):
        """symlink 逃逸检测测试。

        创建一个指向 workspace_root 外部的 symlink，
        验证 check_symlink_escape 能检测到逃逸。
        """
        cfg = DaemonConfig.default()
        tpl = PermissionTemplate()
        checker = AccessChecker(cfg, tpl)

        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        outside_dir = tmp_path / "outside"
        outside_dir.mkdir()
        target_file = outside_dir / "secret.py"
        target_file.write_text("secret")

        # 创建 symlink 指向 workspace 外部
        symlink_path = workspace_root / "link_to_secret.py"
        try:
            os.symlink(str(target_file), str(symlink_path))
        except OSError:
            # Windows 上可能不支持 symlink（无权限），跳过此测试
            pytest.skip("symlink not supported on this platform")

        with pytest.raises(AccessDeniedError) as exc_info:
            checker.check_symlink_escape(str(symlink_path), str(workspace_root))
        assert "escape" in str(exc_info.value).lower()

    def test_symlink_within_workspace_ok(self, tmp_path):
        """workspace 内部的 symlink 不应被拒绝。"""
        cfg = DaemonConfig.default()
        tpl = PermissionTemplate()
        checker = AccessChecker(cfg, tpl)

        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        target_file = workspace_root / "target.py"
        target_file.write_text("content")

        symlink_path = workspace_root / "link.py"
        try:
            os.symlink(str(target_file), str(symlink_path))
        except OSError:
            pytest.skip("symlink not supported on this platform")

        # workspace 内部的 symlink 不应抛异常
        checker.check_symlink_escape(str(symlink_path), str(workspace_root))


class TestAccessCheckerTcpToken:
    """AccessChecker TCP token 校验测试。

    安全测试覆盖：
    - TCP 无 token 或错误 token 应拒绝
    """

    def test_tcp_no_token_denied(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({
            "security": {
                "require_token_for_tcp": True,
                "admin_uids": [0],
            }
        })
        tpl = PermissionTemplate()
        checker = AccessChecker(cfg, tpl)
        validator = TokenValidator(str(tmp_path / "tokens.json"))

        with pytest.raises(AccessDeniedError) as exc_info:
            checker.check_tcp_token("", cfg, validator)
        assert "required" in str(exc_info.value).lower()

    def test_tcp_wrong_token_denied(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({
            "security": {
                "require_token_for_tcp": True,
                "admin_uids": [0],
            }
        })
        tpl = PermissionTemplate()
        checker = AccessChecker(cfg, tpl)
        validator = TokenValidator(str(tmp_path / "tokens.json"))

        fake_token = "cw_" + "x" * 32
        with pytest.raises(AccessDeniedError) as exc_info:
            checker.check_tcp_token(fake_token, cfg, validator)
        assert "invalid" in str(exc_info.value).lower()

    def test_tcp_valid_token_accepted(self, tmp_path):
        cfg = DaemonConfig.load_from_dict({
            "security": {
                "require_token_for_tcp": True,
                "admin_uids": [0],
            }
        })
        tpl = PermissionTemplate()
        checker = AccessChecker(cfg, tpl)
        validator = TokenValidator(str(tmp_path / "tokens.json"))

        token = validator.generate_token("container-1", uid=1000, role="user")
        uid, role = checker.check_tcp_token(token, cfg, validator)
        assert uid == 1000
        assert role == "user"

    def test_tcp_no_token_required(self, tmp_path):
        """配置不需要 token 时返回默认值。"""
        cfg = DaemonConfig.load_from_dict({
            "security": {
                "require_token_for_tcp": False,
                "admin_uids": [0],
            }
        })
        tpl = PermissionTemplate()
        checker = AccessChecker(cfg, tpl)
        validator = TokenValidator(str(tmp_path / "tokens.json"))

        uid, role = checker.check_tcp_token("", cfg, validator)
        assert uid == -1
        assert role == "user"


class TestAccessCheckerJobOperation:
    """AccessChecker job 操作权限测试。"""

    def _make_checker(self, admin_uids=None):
        cfg = DaemonConfig.load_from_dict({
            "security": {"admin_uids": admin_uids or [0]}
        })
        tpl = PermissionTemplate()
        return AccessChecker(cfg, tpl)

    def test_user_can_submit_jobs(self):
        checker = self._make_checker()
        checker.check_job_operation(uid=1000, operation="submit")

    def test_readonly_cannot_submit_jobs(self):
        cfg = DaemonConfig.load_from_dict({"security": {"admin_uids": [0]}})
        tpl = PermissionTemplate()
        checker = AccessChecker(cfg, tpl)
        # readonly 角色不能 submit
        role = tpl.get_role("readonly")
        assert role.can_submit_jobs is False

    def test_user_can_cancel_own_jobs(self):
        checker = self._make_checker()
        checker.check_job_operation(uid=1000, operation="cancel", job_owner_uid=1000)

    def test_user_cannot_cancel_others_jobs(self):
        checker = self._make_checker()
        with pytest.raises(AccessDeniedError):
            checker.check_job_operation(
                uid=1000, operation="cancel", job_owner_uid=1001
            )

    def test_admin_can_cancel_others_jobs(self):
        checker = self._make_checker(admin_uids=[0, 1000])
        checker.check_job_operation(uid=1000, operation="cancel", job_owner_uid=1001)

    def test_unknown_operation_denied(self):
        checker = self._make_checker()
        with pytest.raises(AccessDeniedError) as exc_info:
            checker.check_job_operation(uid=1000, operation="unknown")
        assert "unknown" in str(exc_info.value).lower()


# ============================================================
# 生成函数测试
# ============================================================


class TestGenerateDefaultConfigFile:
    """generate_default_config_file 函数测试。"""

    def test_generates_valid_json(self, tmp_path):
        path = str(tmp_path / "daemon.json")
        generate_default_config_file(path)

        assert os.path.isfile(path)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "socket_path" in data
        assert "resources" in data
        assert "security" in data

    def test_generated_config_loads_correctly(self, tmp_path):
        path = str(tmp_path / "daemon.json")
        generate_default_config_file(path)

        cfg = DaemonConfig.load_from_file(path)
        assert cfg.socket_path == DEFAULT_CONFIG["socket_path"]

    def test_creates_parent_dir(self, tmp_path):
        path = str(tmp_path / "subdir" / "daemon.json")
        generate_default_config_file(path)
        assert os.path.isfile(path)


class TestGeneratePermissionTemplateFile:
    """generate_permission_template_file 函数测试。"""

    def test_generates_valid_json(self, tmp_path):
        path = str(tmp_path / "permissions.json")
        generate_permission_template_file(path)

        assert os.path.isfile(path)
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert "admin" in data
        assert "user" in data
        assert "readonly" in data

    def test_generated_template_has_correct_roles(self, tmp_path):
        path = str(tmp_path / "permissions.json")
        generate_permission_template_file(path)

        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        assert data["admin"]["can_admin_operations"] is True
        assert data["user"]["can_register_workspace"] is True
        assert data["readonly"]["can_submit_jobs"] is False

    def test_creates_parent_dir(self, tmp_path):
        path = str(tmp_path / "subdir" / "permissions.json")
        generate_permission_template_file(path)
        assert os.path.isfile(path)
