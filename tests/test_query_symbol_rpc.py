r"""M2.2 query.symbol daemon RPC 实证（任务 T-1786526643663-594ee010）。

验证 query.symbol 查询从 Python 本地 SQLite 迁移到 Rust daemon RPC 后的成功
路径与拒绝矩阵，全程启动真实 `cw-daemon`（隔离临时数据目录），通过 Windows
Named Pipe 进程级 RPC 往返验证，**禁 mock**（Client 侧 fail-closed 单测除外）。

覆盖矩阵（账本 §9.3 统一验收标准第 7 项）：
- 成功查询：workspace.register → snapshot.publish → query.symbol 返回符号详情
- 未知 workspace 拒绝：`workspace_not_found`
- 符号不存在：返回 `null`（Rust `query_symbol_detail` 语义，非结构化错误码——
  白名单外 handler 既有契约，M2.2 不改）
- snapshot 未就绪：register 后未 publish → `snapshot_not_ready`
- 空参数 / 非法参数：`invalid_params`（dispatch 层前置校验，M2.2 新增）
- 跨 workspace 隔离：A workspace 的符号在 B workspace 查询下不可见
- Python client fail-closed：auto/enterprise 模式 daemon 不可用时不静默
  回退本地 SQLite（仅 local 模式允许 SQL）

前置条件（与 test_query_file_rpc.py 一致）：
1. Windows 平台（Named Pipe）
2. 已构建 `cw-daemon.exe`：`cargo build --release --no-default-features
   --manifest-path rust_ext/Cargo.toml --bin cw-daemon`
3. 默认管道 `\\.\pipe\callwarden-<sid>` 未被其他 daemon 占用（占用则 skip）
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest

from callwarden.server.daemon_protocol import DaemonRemoteError

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DAEMON_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "release", "cw-daemon.exe")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="进程级 query.symbol round-trip 需要 Windows + Named Pipe",
)

requires_binaries = pytest.mark.skipif(
    not os.path.exists(_DAEMON_BIN),
    reason="cw-daemon.exe 未构建（需先 cargo build --bin cw-daemon）",
)


def _daemon_config(tmp: str) -> dict:
    """生成隔离的 daemon JSON 配置（Windows 管道名由 transport 按 SID 派生）。"""
    data_root = os.path.join(tmp, "data")
    return {
        "socket_path": "",
        "registry_db_path": os.path.join(tmp, "registry.db"),
        "task_db_path": os.path.join(tmp, "callwarden.db"),
        "data_root": data_root,
        "max_workers": 2,
        "request_timeout_secs": 30,
        "snapshot_cache_capacity": 4,
        "codegraph_db_path_template": os.path.join(
            data_root, "workspaces", "{workspace_instance_id}", "codegraph.db"
        ),
        "socket_mode": 0o660,
        "socket_group": "",
        "stage_toggle_db_path": os.path.join(tmp, "stage_toggle.db"),
    }


@pytest.fixture(scope="module", autouse=True)
def ensure_fresh_binary():
    """P2 门禁：显式构建 cw-daemon，确保二进制由当前源码（Git HEAD）重建。"""
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("未找到 cargo，无法构建新鲜二进制")
    build = subprocess.run(
        [cargo, "build", "--release", "--no-default-features",
         "--manifest-path", os.path.join(_REPO_ROOT, "rust_ext", "Cargo.toml"),
         "--bin", "cw-daemon"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if build.returncode != 0:
        pytest.fail("cargo build 失败，二进制无法由当前源码重建：\n" + (build.stdout + build.stderr)[-3000:])
    if not os.path.exists(_DAEMON_BIN):
        pytest.fail(f"cargo build 成功但未产出 {_DAEMON_BIN}")


@pytest.fixture(scope="class")
def query_symbol_env():
    """启动真实 cw-daemon（隔离数据目录），返回 (client, tmp, proc)。"""
    from callwarden.server.daemon_client import UnixDaemonRpcClient
    from callwarden.config import _get_windows_user_sid

    pipe = rf"\\.\pipe\callwarden-{_get_windows_user_sid()}"
    probe = UnixDaemonRpcClient(socket_path=pipe, timeout=3)
    try:
        probe.call("ping")
        pytest.skip(f"默认管道 {pipe} 已被其他 daemon 占用，跳过")
    except Exception:
        pass

    tmp = tempfile.mkdtemp(prefix="cw_query_symbol_")
    config = _daemon_config(tmp)
    config_path = os.path.join(tmp, "daemon.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f)
    log = open(os.path.join(tmp, "daemon.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [_DAEMON_BIN, "--config", config_path],
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    client = UnixDaemonRpcClient(socket_path=pipe, timeout=10)
    deadline = time.time() + 40
    ready = False
    while time.time() < deadline:
        if proc.poll() is not None:
            break
        try:
            if client.call("ping").get("status") == "ok":
                ready = True
                break
        except Exception:
            pass
        time.sleep(0.5)
    if not ready:
        log.flush()
        pytest.fail("daemon 未在超时内响应")

    yield client, tmp, proc

    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
    log.close()
    shutil.rmtree(tmp, ignore_errors=True)


# ----------------------------------------------------------------------
# helpers
# ----------------------------------------------------------------------

def _register_workspace(client, root: str) -> tuple:
    """通过 RPC 注册 workspace，返回 (workspace_instance_id, workspace_id)。"""
    result = client.call("workspace.register", {"client_view_root": root})
    instance_id = result["workspace_instance_id"]
    workspace_id = result["workspace_id"]
    assert isinstance(instance_id, str) and instance_id
    assert isinstance(workspace_id, int)
    return instance_id, workspace_id


def _build_snapshot_db(db_path: str, workspace_id: int, root_path: str,
                       file_path: str, symbols: list) -> None:
    """构造 query.symbol 所需的最小 snapshot SQLite（对齐 Rust symbol_query fixture schema）。

    query_symbol_detail JOIN 链：file_symbol_versions → symbol_contents
    → file_versions → file_instances（WHERE workspace_id + is_current=1
    + is_deleted=0 + status!='archived'）。
    """
    conn = sqlite3.connect(db_path)
    try:
        conn.executescript(f"""
            CREATE TABLE workspaces (
                id INTEGER PRIMARY KEY,
                root_path TEXT NOT NULL
            );
            INSERT INTO workspaces VALUES ({workspace_id}, '{root_path}');
            CREATE TABLE file_instances (
                id INTEGER PRIMARY KEY,
                workspace_id INTEGER NOT NULL,
                rel_path TEXT NOT NULL,
                abs_path TEXT NOT NULL,
                status TEXT NOT NULL
            );
            INSERT INTO file_instances VALUES (1, {workspace_id}, '{file_path}', '{root_path}/{file_path}', 'active');
            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY,
                file_instance_id INTEGER NOT NULL,
                symbol_hash TEXT NOT NULL,
                kind TEXT NOT NULL,
                name TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                module_path TEXT NOT NULL,
                visibility TEXT NOT NULL,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                start_col INTEGER,
                end_col INTEGER,
                signature TEXT,
                has_comment INTEGER,
                comment_status TEXT,
                comment_content TEXT,
                depth INTEGER NOT NULL
            );
            CREATE TABLE calls (
                caller_id INTEGER NOT NULL,
                callee_id INTEGER NOT NULL,
                callee_name TEXT NOT NULL,
                call_line INTEGER NOT NULL,
                is_cross_file INTEGER NOT NULL
            );
            CREATE TABLE file_versions (
                id INTEGER PRIMARY KEY,
                file_instance_id INTEGER NOT NULL,
                is_current INTEGER NOT NULL
            );
            CREATE TABLE symbol_contents (
                content_hash TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                kind TEXT NOT NULL,
                content TEXT NOT NULL,
                signature TEXT,
                has_comment INTEGER,
                comment_content TEXT
            );
            CREATE TABLE file_symbol_versions (
                file_version_id INTEGER NOT NULL,
                symbol_hash TEXT NOT NULL,
                qualified_name TEXT NOT NULL,
                module_path TEXT,
                start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL,
                depth INTEGER NOT NULL,
                is_deleted INTEGER NOT NULL
            );
            CREATE TABLE call_versions (
                file_version_id INTEGER NOT NULL,
                caller_qualified TEXT NOT NULL,
                caller_hash TEXT,
                callee_name TEXT NOT NULL,
                callee_module TEXT,
                callee_qualified TEXT,
                callee_file TEXT,
                call_line INTEGER
            );
            CREATE TABLE semgrep_findings (
                file_instance_id INTEGER NOT NULL,
                rule_id TEXT NOT NULL,
                rule_name TEXT,
                severity TEXT,
                confidence TEXT,
                message TEXT,
                start_line INTEGER,
                end_line INTEGER,
                snippet TEXT,
                fix TEXT,
                symbol_qualified TEXT
            );
            CREATE TABLE guardrail_rules (
                rule_id TEXT PRIMARY KEY,
                category TEXT NOT NULL
            );
            CREATE TABLE guardrail_findings (
                workspace_id INTEGER NOT NULL DEFAULT 0,
                rule_id TEXT NOT NULL,
                file_path TEXT NOT NULL,
                symbol_hash TEXT,
                severity TEXT,
                status TEXT,
                message TEXT,
                detected_at REAL
            );
            INSERT INTO file_versions VALUES (10, 1, 1);
        """)
        for idx, symbol in enumerate(symbols, start=1):
            conn.execute(
                "INSERT INTO symbols (id, file_instance_id, symbol_hash, kind, name, "
                "qualified_name, module_path, visibility, start_line, end_line, "
                "start_col, end_col, signature, has_comment, comment_status, "
                "comment_content, depth) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (idx, 1, symbol["hash"], symbol["kind"], symbol["name"],
                 symbol["qualified_name"], symbol["module"],
                 symbol["visibility"], symbol["start_line"], symbol["end_line"],
                 symbol.get("start_col", 1), symbol.get("end_col", 10),
                 symbol.get("signature", ""), symbol.get("has_comment", 0),
                 symbol.get("comment_status", "absent"),
                 symbol.get("comment_content", ""), symbol.get("depth", 0)),
            )
            conn.execute(
                "INSERT INTO symbol_contents VALUES (?,?,?,?,?,?,?)",
                (symbol["hash"], symbol["name"], symbol["kind"], symbol.get("content", ""),
                 symbol.get("signature", ""), symbol.get("has_comment", 0),
                 symbol.get("comment_content", "")),
            )
            conn.execute(
                "INSERT INTO file_symbol_versions VALUES (?,?,?,?,?,?,?,?)",
                (10, symbol["hash"], symbol["qualified_name"], symbol["module"],
                 symbol["start_line"], symbol["end_line"], symbol.get("depth", 0), 0),
            )
        conn.commit()
    finally:
        conn.close()


def _query_symbol(client, instance_id: str, qualified_name: str):
    """调用 query.symbol，成功返回 result，失败抛 DaemonRemoteError。"""
    return client.call("query.symbol", {
        "workspace_instance_id": instance_id,
        "qualified_name": qualified_name,
    })


# ----------------------------------------------------------------------
# 进程级 round-trip 测试
# ----------------------------------------------------------------------

class TestQuerySymbolRpc:
    @requires_binaries
    def test_query_symbol_success(self, query_symbol_env):
        client, tmp, _proc = query_symbol_env
        root = os.path.join(tmp, "ws1")
        os.makedirs(root)
        instance_id, workspace_id = _register_workspace(client, root)
        db_path = os.path.join(tmp, "ws1_snapshot.db")
        _build_snapshot_db(db_path, workspace_id, root, "a.py", [
            {"hash": "hash-alpha", "kind": "fn", "name": "alpha",
             "qualified_name": "a.alpha", "module": "a", "visibility": "public",
             "start_line": 1, "end_line": 3, "signature": "alpha()",
             "has_comment": 1, "comment_content": "alpha docs", "content": "def alpha(): pass"},
            {"hash": "hash-beta", "kind": "fn", "name": "beta",
             "qualified_name": "a.beta", "module": "a", "visibility": "private",
             "start_line": 5, "end_line": 6, "signature": "beta()",
             "has_comment": 0, "comment_content": "", "content": "def beta(): pass"},
        ])
        client.publish_snapshot(instance_id, db_path)

        result = _query_symbol(client, instance_id, "a.alpha")
        assert isinstance(result, dict), f"应返回 JSON 对象，实际 {result!r}"
        assert result["qualified_name"] == "a.alpha"
        assert result["name"] == "alpha"
        assert result["kind"] == "fn"
        assert result["signature"] == "alpha()"
        assert result["has_comment"] is True
        assert result["comment_content"] == "alpha docs"
        # 成功结果含归属证据
        assert result["file_path"] == "a.py"

    @requires_binaries
    def test_query_symbol_unknown_workspace_rejected(self, query_symbol_env):
        client, _tmp, _proc = query_symbol_env
        with pytest.raises(DaemonRemoteError) as exc:
            _query_symbol(client, "deadbeefdeadbeef01", "a.alpha")
        assert exc.value.code == "workspace_not_found"

    @requires_binaries
    def test_query_symbol_not_found_returns_null(self, query_symbol_env):
        client, tmp, _proc = query_symbol_env
        root = os.path.join(tmp, "ws_missing")
        os.makedirs(root)
        instance_id, workspace_id = _register_workspace(client, root)
        db_path = os.path.join(tmp, "ws_missing.db")
        _build_snapshot_db(db_path, workspace_id, root, "a.py", [
            {"hash": "hash-alpha", "kind": "fn", "name": "alpha",
             "qualified_name": "a.alpha", "module": "a", "visibility": "public",
             "start_line": 1, "end_line": 3},
        ])
        client.publish_snapshot(instance_id, db_path)

        # 符号不存在 → Rust query_symbol_detail 返回 Ok(Value::Null)（既有契约）
        result = _query_symbol(client, instance_id, "a.does_not_exist")
        assert result is None, f"符号不存在应返回 null，实际 {result!r}"

    @requires_binaries
    def test_query_symbol_snapshot_not_ready_rejected(self, query_symbol_env):
        client, tmp, _proc = query_symbol_env
        root = os.path.join(tmp, "ws_nosnap")
        os.makedirs(root)
        instance_id, _workspace_id = _register_workspace(client, root)
        with pytest.raises(DaemonRemoteError) as exc:
            _query_symbol(client, instance_id, "a.alpha")
        assert exc.value.code == "snapshot_not_ready"

    @requires_binaries
    def test_query_symbol_invalid_params_rejected(self, query_symbol_env):
        client, tmp, _proc = query_symbol_env
        root = os.path.join(tmp, "ws_param")
        os.makedirs(root)
        instance_id, _workspace_id = _register_workspace(client, root)
        # 缺少 qualified_name
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("query.symbol", {"workspace_instance_id": instance_id})
        assert exc.value.code == "invalid_params"
        # 空 qualified_name（dispatch 层前置校验，M2.2 新增）
        with pytest.raises(DaemonRemoteError) as exc:
            _query_symbol(client, instance_id, "")
        assert exc.value.code == "invalid_params"
        # 纯空白 qualified_name
        with pytest.raises(DaemonRemoteError) as exc:
            _query_symbol(client, instance_id, "   ")
        assert exc.value.code == "invalid_params"

    @requires_binaries
    def test_query_symbol_cross_workspace_isolation(self, query_symbol_env):
        client, tmp, _proc = query_symbol_env
        root1 = os.path.join(tmp, "ws_iso_a")
        root2 = os.path.join(tmp, "ws_iso_b")
        os.makedirs(root1)
        os.makedirs(root2)
        ws1, ws1_id = _register_workspace(client, root1)
        ws2, ws2_id = _register_workspace(client, root2)
        db1 = os.path.join(tmp, "ws_iso_a.db")
        db2 = os.path.join(tmp, "ws_iso_b.db")
        _build_snapshot_db(db1, ws1_id, root1, "a.py", [
            {"hash": "hash-alpha", "kind": "fn", "name": "alpha",
             "qualified_name": "a.alpha", "module": "a", "visibility": "public",
             "start_line": 1, "end_line": 3},
        ])
        _build_snapshot_db(db2, ws2_id, root2, "b.py", [
            {"hash": "hash-gamma", "kind": "fn", "name": "gamma",
             "qualified_name": "b.gamma", "module": "b", "visibility": "private",
             "start_line": 1, "end_line": 2},
        ])
        client.publish_snapshot(ws1, db1)
        client.publish_snapshot(ws2, db2)

        # ws1 查不到 ws2 的符号
        assert _query_symbol(client, ws1, "b.gamma") is None
        # ws2 查不到 ws1 的符号
        assert _query_symbol(client, ws2, "a.alpha") is None
        # ws1 自己的符号可见
        assert _query_symbol(client, ws1, "a.alpha")["name"] == "alpha"
        # ws2 自己的符号可见
        assert _query_symbol(client, ws2, "b.gamma")["name"] == "gamma"


# ----------------------------------------------------------------------
# Python client fail-closed 单测（M2.2 统一验收标准第 5 项）
# ----------------------------------------------------------------------

class TestClientFailClosed:
    def _stub_client(self):
        from callwarden.server.daemon_client import DaemonClient, _NO_REMOTE
        client = DaemonClient.__new__(DaemonClient)
        client._sql_fallbacks = 0
        client._remote_query = lambda method, params, db_path=None: _NO_REMOTE
        return client, _NO_REMOTE

    def test_get_symbol_fail_closed_when_auto_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "auto")
        client, _sentinel = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.get_symbol("a.alpha")
        assert client._sql_fallbacks == 0, "fail-closed 不得计入 SQL fallback"

    def test_get_symbol_fail_closed_when_enterprise_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "enterprise")
        client, _sentinel = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.get_symbol("a.alpha")
        assert client._sql_fallbacks == 0

    def test_get_symbol_allows_sql_only_in_local_mode(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "local")
        client, _sentinel = self._stub_client()

        class FakeDB:
            def get_symbol(self, qualified_name):
                return {"name": "alpha", "from": "sql"}

        client._sql_fallback_get_symbol = lambda qualified_name: FakeDB().get_symbol(qualified_name)
        result = client.get_symbol("a.alpha")
        assert result == {"name": "alpha", "from": "sql"}
        assert client._sql_fallbacks == 1

    # ---- get_symbol_location fail-closed（M2.2 HTTP 轮次补齐，对齐 get_symbol）----
    def test_get_symbol_location_fail_closed_when_auto_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "auto")
        client, _sentinel = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.get_symbol_location("alpha", "a.py")
        assert client._sql_fallbacks == 0, "fail-closed 不得计入 SQL fallback"

    def test_get_symbol_location_fail_closed_when_enterprise_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "enterprise")
        client, _sentinel = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.get_symbol_location("alpha", "a.py")
        assert client._sql_fallbacks == 0

    def test_get_symbol_location_fail_closed_even_in_local_mode(self, monkeypatch):
        """SRV-006：local 模式本地 SQL 已退役——_get_db() 直连路径随
        daemon_client 12 符号薄客户端化移除，local 模式 daemon 不可用同样
        fail-closed（对齐 check_items：no get_db / no business fallback）。"""
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "local")
        client, _sentinel = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.get_symbol_location("alpha", "a.py")
        assert client._sql_fallbacks == 0


# ----------------------------------------------------------------------
# HTTP 模式 workspace_instance_id 注入（T-1786519211817-fcc40690 #1/#2 修复）
# ----------------------------------------------------------------------
# 修复前 `HttpDaemonRpcClient.get_symbol` / `get_symbol_location` 调
# query.symbol / query.symbol_location 不携带 workspace_instance_id，Rust
# handler 强制 require 时报 invalid_params。修复后自动 workspace.register
# （以返回值为权威 instance_id）+ 按需 snapshot.publish，再注入 instance_id
# 发起查询（与 M2.1 get_file_symbols 同构）。以下为 client 侧单测
# （monkeypatch call，不依赖真实 HTTP daemon）。

class _HttpInjectionHarness:
    """HTTP client 注入单测共享夹具（get_symbol / get_symbol_location 共用）。

    与 M2.1 test_query_file_rpc.py TestHttpClientWorkspaceInjection 同构：
    monkeypatch 掉 call，按调用序断言 register→publish→query 且查询参数
    注入权威 workspace_instance_id。
    """

    @staticmethod
    def _make_client(monkeypatch, register_ok=True):
        from callwarden.server.daemon_client import HttpDaemonRpcClient
        client = HttpDaemonRpcClient.__new__(HttpDaemonRpcClient)
        client._remote_workspace_id = None
        client._remote_snapshot_ready = False
        client._project_root = None
        calls = []

        def fake_call(method, params, request_id=None):
            calls.append((method, params))
            if method == "workspace.register":
                if not register_ok:
                    return {"workspace_id": 1}  # 缺少 workspace_instance_id
                return {"workspace_id": 1, "workspace_instance_id": "inst-http2"}
            if method == "snapshot.publish":
                return {"ok": True, "snapshot_id": "snap-2"}
            if method == "query.symbol":
                return {"qualified_name": params["qualified_name"], "name": "alpha"}
            if method == "query.symbol_location":
                return {"name": params["name"], "file_path": "a.py"}
            raise AssertionError(f"意外 method: {method}")

        monkeypatch.setattr(client, "call", fake_call)
        return client, calls


class TestHttpClientWorkspaceInjection:
    # ---- query.symbol ----
    def test_http_get_symbol_registers_and_injects_instance_id(self, monkeypatch):
        from callwarden.server.daemon_client import HttpDaemonRpcClient
        client, calls = _HttpInjectionHarness._make_client(monkeypatch)
        db_path = os.path.join(os.getcwd(), "snap.db")

        result = client.get_symbol("a.alpha", db_path=db_path)

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "snapshot.publish", "query.symbol"], \
            f"调用序应为 register→publish→query.symbol，实际 {methods}"
        # register：client_view_root 默认取进程 cwd（与 legacy 对齐）
        assert calls[0][1] == {"client_view_root": os.getcwd()}
        # publish：注入权威 instance_id + 透传 db_path（abspath 规范化）
        assert calls[1][1]["workspace_instance_id"] == "inst-http2"
        assert calls[1][1]["db_path"] == os.path.abspath(db_path)
        assert calls[1][1]["build_context_hash"] == ""
        # query.symbol：注入权威 instance_id —— 本修复的核心断言
        assert calls[2][1] == {
            "qualified_name": "a.alpha",
            "workspace_instance_id": "inst-http2",
        }
        assert result["name"] == "alpha"

    def test_http_get_symbol_reuses_registered_workspace(self, monkeypatch):
        client, calls = _HttpInjectionHarness._make_client(monkeypatch)
        db_path = os.path.join(os.getcwd(), "snap.db")

        client.get_symbol("a.alpha", db_path=db_path)
        client.get_symbol("a.beta", db_path=db_path)

        methods = [m for m, _ in calls]
        assert methods.count("workspace.register") == 1, "重复调用不得重复 register"
        assert methods.count("snapshot.publish") == 1, "重复调用不得重复 publish"
        assert methods.count("query.symbol") == 2
        # 第二次 query.symbol 仍注入同一权威 instance_id
        assert calls[-1][1]["workspace_instance_id"] == "inst-http2"
        assert calls[-1][1]["qualified_name"] == "a.beta"

    def test_http_get_symbol_register_missing_instance_id_raises(self, monkeypatch):
        from callwarden.server.daemon_client import DaemonUnavailableError
        client, _calls = _HttpInjectionHarness._make_client(monkeypatch, register_ok=False)
        with pytest.raises(DaemonUnavailableError):
            client.get_symbol("a.alpha", db_path=os.path.join(os.getcwd(), "snap.db"))

    def test_http_get_symbol_without_db_path_skips_publish(self, monkeypatch):
        client, calls = _HttpInjectionHarness._make_client(monkeypatch)
        result = client.get_symbol("a.alpha")

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "query.symbol"], \
            f"无 db_path 时不应 publish，实际 {methods}"
        # 即使未 publish，query.symbol 仍注入权威 instance_id
        assert calls[-1][1]["workspace_instance_id"] == "inst-http2"
        assert result["name"] == "alpha"

    # ---- query.symbol_location ----
    def test_http_get_symbol_location_registers_and_injects_instance_id(self, monkeypatch):
        from callwarden.server.daemon_client import HttpDaemonRpcClient
        client, calls = _HttpInjectionHarness._make_client(monkeypatch)
        db_path = os.path.join(os.getcwd(), "snap.db")

        result = client.get_symbol_location("alpha", "a.py", db_path=db_path)

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "snapshot.publish", "query.symbol_location"], \
            f"调用序应为 register→publish→query.symbol_location，实际 {methods}"
        assert calls[0][1] == {"client_view_root": os.getcwd()}
        assert calls[1][1]["workspace_instance_id"] == "inst-http2"
        assert calls[1][1]["db_path"] == os.path.abspath(db_path)
        # query.symbol_location：注入权威 instance_id，name/file_path 契约保持
        assert calls[2][1] == {
            "name": "alpha",
            "file_path": "a.py",
            "workspace_instance_id": "inst-http2",
        }
        assert result["file_path"] == "a.py"

    def test_http_get_symbol_location_reuses_registered_workspace(self, monkeypatch):
        client, calls = _HttpInjectionHarness._make_client(monkeypatch)
        db_path = os.path.join(os.getcwd(), "snap.db")

        client.get_symbol_location("alpha", "a.py", db_path=db_path)
        client.get_symbol_location("beta", "b.py", db_path=db_path)

        methods = [m for m, _ in calls]
        assert methods.count("workspace.register") == 1, "重复调用不得重复 register"
        assert methods.count("snapshot.publish") == 1, "重复调用不得重复 publish"
        assert methods.count("query.symbol_location") == 2
        assert calls[-1][1]["workspace_instance_id"] == "inst-http2"
        assert calls[-1][1]["name"] == "beta"

    def test_http_get_symbol_location_register_missing_instance_id_raises(self, monkeypatch):
        from callwarden.server.daemon_client import DaemonUnavailableError
        client, _calls = _HttpInjectionHarness._make_client(monkeypatch, register_ok=False)
        with pytest.raises(DaemonUnavailableError):
            client.get_symbol_location("alpha", "a.py",
                                      db_path=os.path.join(os.getcwd(), "snap.db"))

    def test_http_get_symbol_location_without_db_path_skips_publish(self, monkeypatch):
        client, calls = _HttpInjectionHarness._make_client(monkeypatch)
        result = client.get_symbol_location("alpha", "a.py")

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "query.symbol_location"], \
            f"无 db_path 时不应 publish，实际 {methods}"
        # 即使未 publish，query.symbol_location 仍注入权威 instance_id
        assert calls[-1][1]["workspace_instance_id"] == "inst-http2"
        assert result["name"] == "alpha"
