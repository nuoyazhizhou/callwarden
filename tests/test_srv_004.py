"""SRV-004 迁移验收：server cli admin Python authority → Rust daemon。

覆盖 task `T-1787323460580-bea19180` step[2] fixture_negative_matrix：
["success", "invalid", "authority", "unavailable", "restart"]。

设计要点（SRV-004 = `service_projection` 端口，route B）：
- Python `server/cli_admin.py` 原有五个直接 open 本地 SQLite 的只读权威函数：
  connection_test / open_readonly_conn / read_pragmas /
  read_task_dependencies / scan_hash_databases。
- SRV-004 后全部退化为 daemon RPC 薄客户端（`mcp.cli_admin.*`，Rust handler
  `rust_ext/src/daemon/cli_admin_handlers.rs`），不再 `import sqlite3`、
  不再 open 本地 DB、不再执行业务 SQL。
- 全部只读（daemon 侧 mode=ro）；错误语义与下沉前一致：
  参数缺失 → `invalid_params`（stable errors），库不可打开/查询失败 → 稳定空值。
- daemon 不可用时 fail-closed 上抛 DaemonUnavailableError，
  绝不回退 Python SQLite 充当业务存储。
- 本测试用内存态 `FakeCliAdminDaemon` 模拟 daemon handler 行为，
  不依赖真实 daemon 进程，也不触碰本地 SQLite 文件。
"""

import ast

import pytest

from callwarden.server.cli_admin import (
    connection_test,
    open_readonly_conn,
    read_pragmas,
    read_task_dependencies,
    scan_hash_databases,
)
from callwarden.server.daemon_client import DaemonUnavailableError


class FakeDaemonRpcError(Exception):
    """模拟 daemon 端 DaemonRpcError（带稳定 error code）。"""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class FakeCliAdminDaemon:
    """内存态 daemon cli_admin 权威（对齐 Rust `cli_admin_handlers` 语义）。"""

    def __init__(self):
        self.available: bool = True
        self.calls: list = []

    def __call__(self, method: str, params: dict):
        if not self.available:
            raise DaemonUnavailableError("daemon 不可用（测试模拟）", code="daemon_unavailable")
        self.calls.append((method, dict(params)))

        if method == "mcp.cli_admin.connection_test":
            db_path = params.get("db_path", "")
            if not db_path:
                raise FakeDaemonRpcError("invalid_params", "connection_test 需要 db_path")
            rounds = int(params.get("rounds", 5) or 0)
            if "/nonexistent" in db_path:
                return {"success": 0, "fail": rounds}
            return {"success": rounds, "fail": 0}

        if method == "mcp.cli_admin.open_readonly_conn":
            db_path = params.get("db_path", "") or "/default/callwarden.db"
            if "/nonexistent" in db_path:
                return {
                    "db_path": db_path,
                    "readonly": True,
                    "openable": False,
                    "error": "unable to open database file",
                }
            return {"db_path": db_path, "readonly": True, "openable": True, "error": None}

        if method == "mcp.cli_admin.read_pragmas":
            if not params.get("db_path", ""):
                raise FakeDaemonRpcError("invalid_params", "read_pragmas 需要 db_path")
            keys = params.get("keys")
            if not isinstance(keys, list):
                raise FakeDaemonRpcError("invalid_params", "read_pragmas 需要 keys 数组")
            known = {"journal_mode": "wal", "synchronous": "2", "busy_timeout": "5000"}
            return {"pragmas": {k: known.get(k, "") for k in keys}}

        if method == "mcp.cli_admin.read_task_dependencies":
            if not isinstance(params.get("workspace_id"), int):
                raise FakeDaemonRpcError(
                    "invalid_params", "read_task_dependencies 需要 workspace_id"
                )
            if not params.get("task_id", "") and not params.get("contract_id", ""):
                raise FakeDaemonRpcError(
                    "invalid_params", "read_task_dependencies 需要 task_id 或 contract_id"
                )
            return {
                "dependencies": [{"dependency_type": "consumes", "target_task_id": "T-2"}],
                "artifacts": [],
                "interfaces": [],
            }

        if method == "mcp.cli_admin.scan_hash_databases":
            return {
                "databases": [
                    {
                        "hash": "0123456789abcdef",
                        "dir": "/home/u/.callwarden/0123456789abcdef",
                        "db_file": "/home/u/.callwarden/0123456789abcdef/callwarden.db",
                        "workspaces": [{"id": 1, "name": "ws-a", "root_path": "/repo/a"}],
                        "error": None,
                    }
                ]
            }

        raise FakeDaemonRpcError("method_not_found", f"未知方法 {method}")


@pytest.fixture
def fake_daemon(monkeypatch):
    """每个测试安装一个干净的内存态 daemon 薄客户端。"""
    daemon = FakeCliAdminDaemon()
    monkeypatch.setattr("callwarden.server.cli_admin._call_daemon_rpc", daemon)
    return daemon


# ============================================================
# 1) success
# ============================================================


def test_success_connection_test_counts(fake_daemon):
    success, fail = connection_test("/data/callwarden.db", rounds=3)
    assert (success, fail) == (3, 0)


def test_success_connection_test_unreachable_db_counts_fail(fake_daemon):
    success, fail = connection_test("/nonexistent/x.db", rounds=2)
    assert (success, fail) == (0, 2)


def test_success_open_readonly_conn_probe(fake_daemon):
    result = open_readonly_conn("/data/callwarden.db")
    assert result["openable"] is True
    assert result["readonly"] is True
    assert result["error"] is None


def test_success_read_pragmas(fake_daemon):
    result = read_pragmas("/data/callwarden.db", ["journal_mode", "unknown_key"])
    assert result == {"journal_mode": "wal", "unknown_key": ""}


def test_success_read_task_dependencies(fake_daemon):
    result = read_task_dependencies(workspace_id=1, task_id="T-1")
    assert result["dependencies"][0]["target_task_id"] == "T-2"
    assert result["artifacts"] == []
    assert result["interfaces"] == []


def test_success_scan_hash_databases(fake_daemon):
    entries = scan_hash_databases("/home/u/.callwarden")
    assert len(entries) == 1
    assert entries[0]["hash"] == "0123456789abcdef"
    assert entries[0]["workspaces"][0]["name"] == "ws-a"
    assert entries[0]["error"] is None


# ============================================================
# 2) invalid（stable errors：参数缺失 → invalid_params，薄客户端透传）
# ============================================================


def test_invalid_connection_test_missing_db_path(fake_daemon):
    with pytest.raises(FakeDaemonRpcError) as exc:
        connection_test("")
    assert exc.value.code == "invalid_params"


def test_invalid_read_pragmas_missing_db_path(fake_daemon):
    with pytest.raises(FakeDaemonRpcError) as exc:
        read_pragmas("", ["journal_mode"])
    assert exc.value.code == "invalid_params"


def test_invalid_read_task_dependencies_no_selector(fake_daemon):
    with pytest.raises(FakeDaemonRpcError) as exc:
        read_task_dependencies(workspace_id=1)
    assert exc.value.code == "invalid_params"


# ============================================================
# 3) authority（数据权威在 daemon；Python 不再本地 open SQLite）
# ============================================================


def test_authority_all_calls_route_through_daemon(fake_daemon):
    connection_test("/data/callwarden.db", rounds=1)
    open_readonly_conn("/data/callwarden.db")
    read_pragmas("/data/callwarden.db", ["journal_mode"])
    read_task_dependencies(workspace_id=1, task_id="T-1")
    scan_hash_databases("/home/u/.callwarden")
    methods = [m for m, _ in fake_daemon.calls]
    assert methods == [
        "mcp.cli_admin.connection_test",
        "mcp.cli_admin.open_readonly_conn",
        "mcp.cli_admin.read_pragmas",
        "mcp.cli_admin.read_task_dependencies",
        "mcp.cli_admin.scan_hash_databases",
    ]


def test_authority_params_forwarded_intact(fake_daemon):
    read_task_dependencies(
        workspace_id=7, contract_id="TC-1", revision=2, db_path="/x/db"
    )
    _, params = fake_daemon.calls[-1]
    assert params == {
        "workspace_id": 7,
        "task_id": "",
        "contract_id": "TC-1",
        "revision": 2,
        "db_path": "/x/db",
    }


# ============================================================
# 4) unavailable（全只读面 fail-closed 上抛，绝不回退 Python SQLite）
# ============================================================


def test_unavailable_all_functions_raise(fake_daemon):
    fake_daemon.available = False
    with pytest.raises(DaemonUnavailableError):
        connection_test("/data/callwarden.db")
    with pytest.raises(DaemonUnavailableError):
        open_readonly_conn("/data/callwarden.db")
    with pytest.raises(DaemonUnavailableError):
        read_pragmas("/data/callwarden.db", ["journal_mode"])
    with pytest.raises(DaemonUnavailableError):
        read_task_dependencies(workspace_id=1, task_id="T-1")
    with pytest.raises(DaemonUnavailableError):
        scan_hash_databases("/home/u/.callwarden")


# ============================================================
# 5) restart（不可用 → 恢复后立即成功，无缓存/状态污染）
# ============================================================


def test_restart_recovers_after_unavailable(fake_daemon):
    fake_daemon.available = False
    with pytest.raises(DaemonUnavailableError):
        read_pragmas("/data/callwarden.db", ["journal_mode"])
    fake_daemon.available = True
    result = read_pragmas("/data/callwarden.db", ["journal_mode"])
    assert result["journal_mode"] == "wal"
    # 恢复后连接测试同样立即成功
    success, fail = connection_test("/data/callwarden.db", rounds=2)
    assert (success, fail) == (2, 0)


# ============================================================
# 零权威证据：AST 扫描（模块与五个目标函数不再含 SQLite 权威残留）
# ============================================================


def test_no_sqlite_authority_in_source():
    # 直接取已加载模块的 __file__，确保扫描的是当前生效（worktree）的迁移后源码
    import callwarden.server.cli_admin as ca

    src_path = ca.__file__
    with open(src_path, "r", encoding="utf-8") as f:
        full_src = f.read()
    tree = ast.parse(full_src)

    banned_imports = {"sqlite3"}
    banned_tokens = {"mode=ro", "sqlite3.", "conn.execute", "PRAGMA ", "SELECT "}
    target_funcs = {
        "connection_test",
        "open_readonly_conn",
        "read_pragmas",
        "read_task_dependencies",
        "scan_hash_databases",
    }

    violations = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for n in node.names:
                if n.name in banned_imports:
                    violations.append("import sqlite3")
        elif isinstance(node, ast.ImportFrom):
            if node.module in banned_imports:
                violations.append("from sqlite3 import")

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

    assert not violations, f"server/cli_admin.py 仍含 SQLite 权威残留: {violations}"
