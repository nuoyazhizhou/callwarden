r"""M2.3 query.grep daemon RPC 实证（任务 T-1786529505247-9d083e54）。

验证 query.grep 文本搜索从 Python 本地 SQLite 迁移到 Rust daemon RPC 后的成功
路径与拒绝矩阵，全程启动真实 `cw-daemon`（隔离临时数据目录），通过 Windows
Named Pipe 进程级 RPC 往返验证，**禁 mock**（Client 侧 fail-closed 单测除外）。

与 M2.2 query.symbol 的关键差异：
- handler 参数为 `patterns`（字符串数组）而非 `qualified_name`
- 搜索对象是**真实文件系统**（rg 加速或纯源码 fallback），不是 SQLite 内容；
  snapshot SQLite 中的 symbols 表仅提供符号归属上下文（`[in fn a.alpha]`）
- Rust handler 返回 `Value::String`（格式化文本，既有契约），无匹配时返回
  `No matches for: <pattern>` 文本而非结构化空数组

覆盖矩阵（账本 §9.3 统一验收标准第 7 项）：
- 成功查询：workspace.register → snapshot.publish → query.grep 返回符号归属文本
- 多 pattern AND 语义：所有 pattern 都匹配的行才保留
- 未知 workspace 拒绝：`workspace_not_found`
- 无匹配：返回 `No matches for: <pattern>` 文本（既有契约，非结构化空数组）
- snapshot 未就绪：register 后未 publish → `snapshot_not_ready`
- 非法参数：patterns 缺失 / 非数组 / 空数组 / 空白 / NUL → `invalid_params`
  （dispatch 层前置校验，M2.3 新增）
- 跨 workspace 隔离：A workspace 搜不到 B workspace 的真实文件
- limit 截断：`limit` 在符号/kind 过滤后截断结果数
- fixed 字面匹配：`fixed=true` 按字面搜索，`false` 按正则搜索
- Python client fail-closed：auto/enterprise 模式 daemon 不可用时不静默
  回退本地 SQLite（仅 local 模式返回 None，表示由本地 grep 组件处理）

前置条件（与 test_query_file_rpc.py / test_query_symbol_rpc.py 一致）：
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
    reason="进程级 query.grep round-trip 需要 Windows + Named Pipe",
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
def query_grep_env():
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

    tmp = tempfile.mkdtemp(prefix="cw_query_grep_")
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


def _write_source(root: str, rel_path: str, content: str) -> str:
    """在 workspace root 下写入真实源文件（grep 搜索对象是文件系统）。"""
    path = os.path.join(root, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return path


def _build_snapshot_db(db_path: str, workspace_id: int, root_path: str,
                       file_path: str, symbols: list) -> None:
    """构造 query.grep 所需的最小 snapshot SQLite（对齐 Rust fixture schema）。

    grep 的符号归属 JOIN 链：symbols s → file_instances fi（WHERE fi.workspace_id
    + fi.rel_path + fi.status!='archived' + s.start_line>0 + s.end_line>0）。
    build_and_publish 要求表结构完整（对齐 M2.2 query.symbol fixture schema）。
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


def _query_grep(client, instance_id: str, patterns: list, **extra):
    """调用 query.grep，成功返回 result（str），失败抛 DaemonRemoteError。"""
    params = {"workspace_instance_id": instance_id, "patterns": patterns}
    params.update(extra)
    return client.call("query.grep", params)


# ----------------------------------------------------------------------
# 进程级 round-trip 测试
# ----------------------------------------------------------------------

class TestQueryGrepRpc:
    @requires_binaries
    def test_query_grep_success(self, query_grep_env):
        client, tmp, _proc = query_grep_env
        root = os.path.join(tmp, "ws1")
        os.makedirs(root)
        _write_source(root, "a.py", "def alpha():\n    TODO: fixme\n")
        instance_id, workspace_id = _register_workspace(client, root)
        db_path = os.path.join(tmp, "ws1_snapshot.db")
        _build_snapshot_db(db_path, workspace_id, root, "a.py", [
            {"hash": "hash-alpha", "kind": "fn", "name": "alpha",
             "qualified_name": "a.alpha", "module": "a", "visibility": "public",
             "start_line": 1, "end_line": 3, "signature": "alpha()",
             "has_comment": 1, "comment_content": "alpha docs", "content": "def alpha(): pass"},
        ])
        client.publish_snapshot(instance_id, db_path)

        result = _query_grep(client, instance_id, ["TODO"])
        # Rust handler 返回 Value::String（既有契约）
        assert isinstance(result, str), f"应返回文本，实际 {result!r}"
        assert "Grep with symbol context:" in result
        # 符号归属上下文（symbols 表 start_line=1..end_line=3 覆盖第 2 行）
        assert "[in fn a.alpha]" in result
        assert "TODO: fixme" in result

    @requires_binaries
    def test_query_grep_and_patterns_require_all(self, query_grep_env):
        """多 pattern 为 AND 语义：主 pattern 匹配后，剩余 pattern 逐条 AND 过滤。"""
        client, tmp, _proc = query_grep_env
        root = os.path.join(tmp, "ws_and")
        os.makedirs(root)
        _write_source(root, "a.py", "def alpha():\n    TODO: fixme\n")
        instance_id, workspace_id = _register_workspace(client, root)
        db_path = os.path.join(tmp, "ws_and.db")
        _build_snapshot_db(db_path, workspace_id, root, "a.py", [
            {"hash": "hash-alpha", "kind": "fn", "name": "alpha",
             "qualified_name": "a.alpha", "module": "a", "visibility": "public",
             "start_line": 1, "end_line": 3},
        ])
        client.publish_snapshot(instance_id, db_path)

        # "TODO" 匹配第 2 行，AND "fixme" 也匹配该行 → 1 条
        result = _query_grep(client, instance_id, ["TODO", "fixme"])
        assert "Grep with symbol context: pattern='TODO fixme'" in result
        assert "1 matches" in result
        assert "[in fn a.alpha]" in result

        # "TODO" 匹配第 2 行，AND "ZZZ" 不匹配任何行 → AND 过滤后空 → 文本提示
        result = _query_grep(client, instance_id, ["TODO", "ZZZ"])
        assert "No matches for AND: TODO ZZZ" in result

    @requires_binaries
    def test_query_grep_unknown_workspace_rejected(self, query_grep_env):
        client, _tmp, _proc = query_grep_env
        with pytest.raises(DaemonRemoteError) as exc:
            _query_grep(client, "deadbeefdeadbeef01", ["TODO"])
        assert exc.value.code == "workspace_not_found"

    @requires_binaries
    def test_query_grep_no_matches_returns_empty_text(self, query_grep_env):
        """Rust 既有契约：无匹配返回 'No matches for: <pattern>' 文本，非结构化数组。"""
        client, tmp, _proc = query_grep_env
        root = os.path.join(tmp, "ws_nomatch")
        os.makedirs(root)
        _write_source(root, "a.py", "def alpha():\n    pass\n")
        instance_id, workspace_id = _register_workspace(client, root)
        db_path = os.path.join(tmp, "ws_nomatch.db")
        _build_snapshot_db(db_path, workspace_id, root, "a.py", [
            {"hash": "hash-alpha", "kind": "fn", "name": "alpha",
             "qualified_name": "a.alpha", "module": "a", "visibility": "public",
             "start_line": 1, "end_line": 3},
        ])
        client.publish_snapshot(instance_id, db_path)

        result = _query_grep(client, instance_id, ["ZZZ_DOES_NOT_EXIST"])
        assert isinstance(result, str), f"应返回文本，实际 {result!r}"
        assert result == "No matches for: ZZZ_DOES_NOT_EXIST"

    @requires_binaries
    def test_query_grep_snapshot_not_ready_rejected(self, query_grep_env):
        client, tmp, _proc = query_grep_env
        root = os.path.join(tmp, "ws_nosnap")
        os.makedirs(root)
        _write_source(root, "a.py", "def alpha():\n    TODO: fixme\n")
        instance_id, _workspace_id = _register_workspace(client, root)
        with pytest.raises(DaemonRemoteError) as exc:
            _query_grep(client, instance_id, ["TODO"])
        assert exc.value.code == "snapshot_not_ready"

    @requires_binaries
    def test_query_grep_invalid_params_rejected(self, query_grep_env):
        """M2.3 新增 dispatch 层前置校验：patterns 形态非法 → invalid_params。"""
        client, tmp, _proc = query_grep_env
        root = os.path.join(tmp, "ws_param")
        os.makedirs(root)
        _write_source(root, "a.py", "def alpha():\n    pass\n")
        instance_id, _workspace_id = _register_workspace(client, root)
        # patterns 缺失
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("query.grep", {"workspace_instance_id": instance_id})
        assert exc.value.code == "invalid_params"
        # patterns 非数组（传字符串）
        with pytest.raises(DaemonRemoteError) as exc:
            _query_grep(client, instance_id, "TODO")
        assert exc.value.code == "invalid_params"
        # 空数组
        with pytest.raises(DaemonRemoteError) as exc:
            _query_grep(client, instance_id, [])
        assert exc.value.code == "invalid_params"
        # 纯空白 pattern
        with pytest.raises(DaemonRemoteError) as exc:
            _query_grep(client, instance_id, ["   "])
        assert exc.value.code == "invalid_params"
        # NUL 字节 pattern
        with pytest.raises(DaemonRemoteError) as exc:
            _query_grep(client, instance_id, ["a\x00b"])
        assert exc.value.code == "invalid_params"

    @requires_binaries
    def test_query_grep_cross_workspace_isolation(self, query_grep_env):
        """grep 搜索当前 workspace 的真实文件系统，A 搜不到 B 的文件。"""
        client, tmp, _proc = query_grep_env
        root1 = os.path.join(tmp, "ws_iso_a")
        root2 = os.path.join(tmp, "ws_iso_b")
        os.makedirs(root1)
        os.makedirs(root2)
        _write_source(root1, "a.py", "def alpha():\n    alpha_marker\n")
        _write_source(root2, "b.py", "def gamma():\n    gamma_marker\n")
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
             "start_line": 1, "end_line": 3},
        ])
        client.publish_snapshot(ws1, db1)
        client.publish_snapshot(ws2, db2)

        # ws1 搜不到 ws2 的真实文件内容
        assert _query_grep(client, ws1, ["gamma_marker"]) == "No matches for: gamma_marker"
        # ws2 搜不到 ws1 的真实文件内容
        assert _query_grep(client, ws2, ["alpha_marker"]) == "No matches for: alpha_marker"
        # ws1 自己的内容可见
        result = _query_grep(client, ws1, ["alpha_marker"])
        assert "Grep with symbol context:" in result
        assert "[in fn a.alpha]" in result
        # ws2 自己的内容可见
        result = _query_grep(client, ws2, ["gamma_marker"])
        assert "Grep with symbol context:" in result
        assert "[in fn b.gamma]" in result

    @requires_binaries
    def test_query_grep_limit_filter(self, query_grep_env):
        """limit 在符号归属过滤后截断结果数。"""
        client, tmp, _proc = query_grep_env
        root = os.path.join(tmp, "ws_limit")
        os.makedirs(root)
        _write_source(root, "a.py", "def alpha():\n    x = 1\n    y = 2\n")
        instance_id, workspace_id = _register_workspace(client, root)
        db_path = os.path.join(tmp, "ws_limit.db")
        _build_snapshot_db(db_path, workspace_id, root, "a.py", [
            {"hash": "hash-alpha", "kind": "fn", "name": "alpha",
             "qualified_name": "a.alpha", "module": "a", "visibility": "public",
             "start_line": 1, "end_line": 4},
        ])
        client.publish_snapshot(instance_id, db_path)

        # "=" 匹配第 2/3 行，limit=1 → 只显示 1 条
        result = _query_grep(client, instance_id, ["="], limit=1)
        assert "Grep with symbol context:" in result
        assert "1 matches (of 2 after filter)" in result
        assert "x = 1" in result
        assert "y = 2" not in result, "limit=1 应截断第 2 条匹配"

    @requires_binaries
    def test_query_grep_fixed_matches_literally(self, query_grep_env):
        """fixed=true 按字面搜索；fixed=false 按正则搜索（'.' 可匹配任意字符）。"""
        client, tmp, _proc = query_grep_env
        root = os.path.join(tmp, "ws_fixed")
        os.makedirs(root)
        _write_source(root, "a.py", "def alpha():\n    foo.bar\n    fooXbar\n")
        instance_id, workspace_id = _register_workspace(client, root)
        db_path = os.path.join(tmp, "ws_fixed.db")
        _build_snapshot_db(db_path, workspace_id, root, "a.py", [
            {"hash": "hash-alpha", "kind": "fn", "name": "alpha",
             "qualified_name": "a.alpha", "module": "a", "visibility": "public",
             "start_line": 1, "end_line": 4},
        ])
        client.publish_snapshot(instance_id, db_path)

        # fixed=false：regex "foo.bar" 同时匹配 foo.bar 与 fooXbar
        result = _query_grep(client, instance_id, ["foo.bar"], fixed=False)
        assert "2 matches (of 2 after filter)" in result
        assert "fooXbar" in result
        # fixed=true：只匹配字面 "foo.bar"
        result = _query_grep(client, instance_id, ["foo.bar"], fixed=True)
        assert "1 matches (of 1 after filter)" in result
        assert "fooXbar" not in result


# ----------------------------------------------------------------------
# Python client fail-closed 单测（M2.3 统一验收标准第 5 项）
# ----------------------------------------------------------------------

class TestClientFailClosed:
    def _stub_client(self):
        from callwarden.server.daemon_client import DaemonClient, _NO_REMOTE
        client = DaemonClient.__new__(DaemonClient)
        client._sql_fallbacks = 0
        client._remote_query = lambda method, params, db_path=None: _NO_REMOTE
        return client, _NO_REMOTE

    def test_query_grep_fail_closed_when_auto_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "auto")
        client, _sentinel = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.query_grep(["TODO"])
        assert client._sql_fallbacks == 0, "fail-closed 不得计入 SQL fallback"

    def test_query_grep_fail_closed_when_enterprise_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "enterprise")
        client, _sentinel = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.query_grep(["TODO"])
        assert client._sql_fallbacks == 0

    def test_query_grep_returns_none_in_local_mode(self, monkeypatch):
        """local 模式无 daemon：grep 由本地 CLI/MCP file_grep 负责，client 不承担
        SQL 回退，返回 None 表示"由本地 grep 组件处理"。"""
        from callwarden.server import daemon_client as dc_module
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "local")
        client, _sentinel = self._stub_client()
        result = client.query_grep(["TODO"])
        assert result is None
        assert client._sql_fallbacks == 0


# ----------------------------------------------------------------------
# HTTP 模式 workspace_instance_id 注入（T-1786519211823-fd25bb10，M2.3 HTTP 轮次）
# ----------------------------------------------------------------------
# 修复前 `HttpDaemonRpcClient` 无 query_grep 方法，HTTP 模式无 query.grep
# RPC 入口。本任务新增该方法：复用 M2.1 `_ensure_remote_snapshot` 自动
# workspace.register（以 daemon 返回值为权威 instance_id）+ 按需
# snapshot.publish，再注入 instance_id 发起查询（与 M2.1 get_file_symbols /
# M2.2 get_symbol 同构）。以下为 client 侧单测（monkeypatch call，
# 不依赖真实 HTTP daemon）。

class TestHttpClientWorkspaceInjection:
    """HTTP 模式 query.grep 自动 register/publish + 注入权威 instance_id。

    与 M2.1 test_query_file_rpc.py / M2.2 test_query_symbol_rpc.py 的
    TestHttpClientWorkspaceInjection 同构：按调用序断言
    register→publish→query.grep 且查询参数注入权威 instance_id。
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
                return {"workspace_id": 1, "workspace_instance_id": "inst-http3"}
            if method == "snapshot.publish":
                return {"ok": True, "snapshot_id": "snap-3"}
            if method == "query.grep":
                return "Grep with symbol context: pattern='TODO', 1 matches (of 1 after filter)"
            raise AssertionError(f"意外 method: {method}")

        monkeypatch.setattr(client, "call", fake_call)
        return client, calls

    def test_http_query_grep_registers_and_injects_instance_id(self, monkeypatch):
        from callwarden.server.daemon_client import HttpDaemonRpcClient
        client, calls = self._make_client(monkeypatch)
        db_path = os.path.join(os.getcwd(), "snap.db")

        result = client.query_grep(["TODO"], db_path=db_path)

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "snapshot.publish", "query.grep"], \
            f"调用序应为 register→publish→query.grep，实际 {methods}"
        # register：client_view_root 默认取进程 cwd（与 legacy 对齐）
        assert calls[0][1] == {"client_view_root": os.getcwd()}
        # publish：注入权威 instance_id + 透传 db_path（abspath 规范化）
        assert calls[1][1]["workspace_instance_id"] == "inst-http3"
        assert calls[1][1]["db_path"] == os.path.abspath(db_path)
        assert calls[1][1]["build_context_hash"] == ""
        # query.grep：注入权威 instance_id —— 本修复的核心断言
        assert calls[2][1]["workspace_instance_id"] == "inst-http3"
        assert calls[2][1]["patterns"] == ["TODO"]
        assert isinstance(result, str) and "Grep with symbol context:" in result

    def test_http_query_grep_reuses_registered_workspace(self, monkeypatch):
        client, calls = self._make_client(monkeypatch)
        db_path = os.path.join(os.getcwd(), "snap.db")

        client.query_grep(["TODO"], db_path=db_path)
        client.query_grep(["fixme"], db_path=db_path)

        methods = [m for m, _ in calls]
        assert methods.count("workspace.register") == 1, "重复调用不得重复 register"
        assert methods.count("snapshot.publish") == 1, "重复调用不得重复 publish"
        assert methods.count("query.grep") == 2
        # 第二次 query.grep 仍注入同一权威 instance_id
        assert calls[-1][1]["workspace_instance_id"] == "inst-http3"
        assert calls[-1][1]["patterns"] == ["fixme"]

    def test_http_query_grep_register_missing_instance_id_raises(self, monkeypatch):
        from callwarden.server.daemon_client import DaemonUnavailableError
        client, _calls = self._make_client(monkeypatch, register_ok=False)
        with pytest.raises(DaemonUnavailableError):
            client.query_grep(["TODO"], db_path=os.path.join(os.getcwd(), "snap.db"))

    def test_http_query_grep_without_db_path_skips_publish(self, monkeypatch):
        client, calls = self._make_client(monkeypatch)
        result = client.query_grep(["TODO"])

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "query.grep"], \
            f"无 db_path 时不应 publish，实际 {methods}"
        # 即使未 publish，query.grep 仍注入权威 instance_id
        assert calls[-1][1]["workspace_instance_id"] == "inst-http3"
        assert isinstance(result, str)

    def test_http_query_grep_parameter_contract_passthrough(self, monkeypatch):
        """非默认参数（fixed/limit/path/include_all/kind）完整透传（对齐 legacy 契约）。"""
        client, calls = self._make_client(monkeypatch)

        client.query_grep(
            ["foo.bar", "baz"],
            fixed=True,
            limit=50,
            path="src",
            include_all=True,
            kind="fn",
            db_path=os.path.join(os.getcwd(), "snap.db"),
        )

        query_params = calls[-1][1]
        assert query_params["patterns"] == ["foo.bar", "baz"]
        assert query_params["fixed"] is True
        assert query_params["limit"] == 50
        assert query_params["path"] == "src"
        assert query_params["include_all"] is True
        assert query_params["kind"] == "fn"
        assert query_params["workspace_instance_id"] == "inst-http3"
