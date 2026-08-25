"""SRV-003 迁移验收：server backup/restore Python authority → Rust daemon。

覆盖 task `T-1787323460500-b9e232bc` step[2] fixture_negative_matrix：
["success", "invalid", "authority", "unavailable", "restart"]。

设计要点（SRV-003 = narrow `governance_projection` 端口，route B）：
- Python `server/backup_restore.py` 仅两个助手仍直接 open 本地 SQLite：
  * `BackupManager._backup_file`：单文件备份（`.db` 走 VACUUM INTO，其它走复制）+ sha256；
  * `_is_rust_backup_rolled_back`：读 registry `rollback_config` 判断 feature 是否回滚。
- SRV-003 后两者退化为纯 daemon RPC 薄客户端，不再 `import sqlite3`、不再 open 本地 DB。
- `backup_file` 是写盘 admin-only 操作：daemon 不可用时 fail-closed 上抛，
  绝不回退 Python SQLite 充当业务存储；源缺失 daemon 返回 null → 薄客户端透传 None。
- `is_rust_backup_rolled_back` 是只读 authority 读：daemon 不可用时 fail-soft 视为未回滚
  （返回 False），与 metrics/health/audit 的 rollback 探测保持一致模式。
- 本测试用内存态 `FakeBackupDaemon` 模拟 daemon 的 `backup_restore_handlers` 行为，
  不依赖真实 daemon 进程，也不触碰本地 SQLite 文件。
"""

import ast
import os
import types

import pytest

from callwarden.server.backup_restore import (
    BackupManager,
    _is_rust_backup_rolled_back,
)
from callwarden.server.daemon_client import DaemonUnavailableError


class FakeDaemonRpcError(Exception):
    """模拟 daemon 端 DaemonRpcError（带稳定 error code）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class FakeBackupDaemon:
    """内存态 daemon backup/restore 权威（对齐 Rust `backup_restore_handlers` 语义）。"""

    def __init__(self, rolled_back: bool = False):
        self.available: bool = True
        self.rolled_back = rolled_back
        self.last_backup_params: dict = {}

    def __call__(self, method: str, params: dict):
        if not self.available:
            raise DaemonUnavailableError("daemon 不可用（测试模拟）", code="daemon_unavailable")
        if method == "mcp.backup_restore.is_rust_backup_rolled_back":
            return {"rolled_back": self.rolled_back}
        if method == "mcp.backup_restore.backup_file":
            self.last_backup_params = params
            src = params.get("src_path", "")
            dest_dir = params.get("dest_dir", "")
            dest_name = params.get("dest_name", "")
            # 对齐 Rust handler：缺必填参数 → invalid_params（薄客户端透传错误）
            if not src or not dest_dir or not dest_name:
                raise FakeDaemonRpcError(
                    "invalid_params", "backup_file 需要 src_path/dest_dir/dest_name"
                )
            # 对齐 Rust handler：源文件不存在 → 返回 null（薄客户端据此返回 None）
            if "/nonexistent" in src:
                return None
            return {
                "name": dest_name,
                "type": "file",
                "size": 15,
                "sha256": "deadbeef" * 8,
                "source_path": src,
            }
        raise FakeDaemonRpcError("method_not_found", f"未知方法 {method}")


def _dummy_config():
    """BackupManager.__init__ 仅读取 config.data_root，给最小桩即可。"""
    return types.SimpleNamespace(data_root="/tmp")


@pytest.fixture
def fake_daemon(monkeypatch):
    """每个测试安装一个干净的内存态 daemon 薄客户端 + 复位 60s 回滚缓存。"""
    import callwarden.server.backup_restore as br

    daemon = FakeBackupDaemon()
    monkeypatch.setattr("callwarden.server.backup_restore._call_daemon_rpc", daemon)
    # 复位模块级 60s 缓存，避免跨测试污染
    monkeypatch.setattr(
        br, "_BACKUP_ROLLBACK_CACHE", {"ts": 0.0, "value": False}
    )
    return daemon


# ============================================================
# 1) success
# ============================================================


def test_success_backup_file_returns_daemon_info(fake_daemon):
    bm = BackupManager(_dummy_config())
    info = bm._backup_file("/data/a.db", "/out", "a.db")
    assert info is not None
    assert info["name"] == "a.db"
    assert info["type"] == "file"
    assert "sha256" in info
    assert info["source_path"] == "/data/a.db"


def test_success_is_rust_backup_rolled_back_false(fake_daemon):
    # 默认 daemon 报告未回滚
    assert _is_rust_backup_rolled_back() is False


def test_success_backup_file_missing_source_returns_none(fake_daemon):
    # 源文件不存在 → daemon 返回 null → 薄客户端透传 None（对齐旧语义）
    bm = BackupManager(_dummy_config())
    assert bm._backup_file("/nonexistent/a.db", "/out", "a.db") is None


# ============================================================
# 2) invalid
# ============================================================


def test_invalid_backup_file_params(fake_daemon):
    bm = BackupManager(_dummy_config())
    # 缺必填参数 → daemon 端 invalid_params；薄客户端透传错误（不静默降级）
    with pytest.raises(FakeDaemonRpcError) as exc:
        bm._backup_file("", "", "")
    assert exc.value.code == "invalid_params"


# ============================================================
# 3) authority（rollback_config 权威在 daemon，Python 不再本地持有）
# ============================================================


def test_authority_rollback_config_owned_by_daemon(fake_daemon):
    fake_daemon.rolled_back = True
    # 权威来自 daemon，薄客户端经 RPC 读取
    assert _is_rust_backup_rolled_back() is True


def test_authority_backup_file_delegates_to_daemon(fake_daemon):
    bm = BackupManager(_dummy_config())
    bm._backup_file("/data/a.db", "/out", "a.db")
    assert fake_daemon.last_backup_params == {
        "src_path": "/data/a.db",
        "dest_dir": "/out",
        "dest_name": "a.db",
    }


# ============================================================
# 4) unavailable（backup_file fail-closed 上抛；is_rust_* fail-soft 视为未回滚）
# ============================================================


def test_unavailable_backup_file_raises(fake_daemon):
    fake_daemon.available = False
    bm = BackupManager(_dummy_config())
    with pytest.raises(DaemonUnavailableError):
        bm._backup_file("/data/a.db", "/out", "a.db")


def test_unavailable_is_rust_backup_rolled_back_fail_soft_false(fake_daemon):
    fake_daemon.available = False
    # 只读 authority 读：daemon 不可用时 fail-soft 视为未回滚（绝不回退 Python SQLite）
    assert _is_rust_backup_rolled_back() is False


# ============================================================
# 5) restart（首次不可用 → 恢复后成功 / 读到正确回滚位）
# ============================================================


def test_restart_backup_file_recovers(fake_daemon):
    bm = BackupManager(_dummy_config())
    fake_daemon.available = False
    with pytest.raises(DaemonUnavailableError):
        bm._backup_file("/data/a.db", "/out", "a.db")
    fake_daemon.available = True
    info = bm._backup_file("/data/a.db", "/out", "a.db")
    assert info["name"] == "a.db"


def test_restart_rolled_back_reads_after_recover(fake_daemon):
    import callwarden.server.backup_restore as br

    fake_daemon.available = False
    assert _is_rust_backup_rolled_back() is False  # fail-soft，并进入 60s 缓存
    fake_daemon.available = True
    fake_daemon.rolled_back = True
    # 失效 60s 缓存以模拟“恢复后重新探测 daemon 权威”
    br._BACKUP_ROLLBACK_CACHE["ts"] = 0.0
    assert _is_rust_backup_rolled_back() is True


# ============================================================
# 零权威证据：AST 扫描（两个已迁移 helper 不再含 SQLite 权威残留）
# ============================================================


def test_no_sqlite_authority_in_source():
    # 直接取已加载模块的 __file__，确保扫描的是当前生效（worktree）的迁移后源码
    import callwarden.server.backup_restore as br

    src = br.__file__
    with open(src, "r", encoding="utf-8") as f:
        tree = ast.parse(f.read())

    banned_imports = {"sqlite3"}
    # 仅校验两个已迁移的 helper 不再持有 SQLite 快照/连接权威
    banned_tokens = {"VACUUM INTO", "wal_checkpoint", "PRAGMA"}
    target_funcs = {"_backup_file", "_is_rust_backup_rolled_back"}

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                if n.name in banned_imports:
                    violations.append("import sqlite3")
        elif isinstance(node, ast.ImportFrom):
            if node.module in banned_imports:
                violations.append("from sqlite3 import")

    full_src = open(src, "r", encoding="utf-8").read()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in target_funcs:
            # 仅扫描函数体实际代码（排除 docstring，docstring 可合法描述 daemon 行为）
            body = node.body
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(getattr(body[0].value, "value", None), str)
            ):
                body = body[1:]
            code_seg = "\n".join(ast.get_source_segment(full_src, s) or "" for s in body)
            for tok in banned_tokens:
                if tok in code_seg:
                    violations.append(f"{node.name}: contains {tok}")
            for sub in ast.walk(node):
                if isinstance(sub, ast.Attribute) and sub.attr == "sqlite3":
                    violations.append(f"{node.name}: sqlite3 attr")

    assert not violations, f"server/backup_restore.py 仍含 SQLite 权威残留: {violations}"
