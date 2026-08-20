r"""M2.4 query.issues daemon RPC 实证（任务 T-1786539379174-90f74174，HTTP 轮次
T-1786519211831-fd9a5380）。

验证 query.issues 按符号查询（semgrep + guardrail findings）从 Python 本地
SQLite 迁移到 Rust daemon RPC 后的成功路径与拒绝矩阵，全程启动真实
`cw-daemon`（隔离临时数据目录），通过 Windows Named Pipe 进程级 RPC 往返验证，
**禁 mock**（Client 侧 fail-closed / HTTP 注入单测除外）。

与 M2.3 query.grep 的关键差异：
- handler 参数为 `qualified_name`（+ 可选 `include_info`），非 patterns 数组
- 查询对象是 snapshot SQLite 中的 `semgrep_findings` / `guardrail_findings`
  （按符号归属），不是文件系统全文
- Rust handler 返回 `Value::Array`（结构化 issues 数组），无匹配（符号不存在）
  时返回空数组 `[]`，非文本提示
- dispatch 层前置校验（M2.4 新增）：qualified_name 空/纯空白/NUL → invalid_params

HTTP 轮次（T-1786519211831-fd9a5380）补充：
- `HttpDaemonRpcClient.query_issues` 便捷方法：自动 workspace.register +
  按需 snapshot.publish，注入权威 workspace_instance_id 后调 query.issues
  （修复 MCP get_symbol_issues HTTP 分支缺注入导致 invalid_params 的缺陷）
- legacy `DaemonClient.get_symbol_issues` 补 fail-closed：仅 local 模式保留
  SQL 回退，auto/enterprise daemon 不可用 raise（对齐 query_issues L1238）

覆盖矩阵（账本 §9.3 统一验收标准第 7 项）：
- 成功查询：workspace.register → snapshot.publish → query.issues 返回
  semgrep + guardrail 合并数组（每条含 source 字段）
- include_info 语义：false 排除 INFO/UNKNOWN 与 info 级，true 全量返回
- 未知 workspace 拒绝：`workspace_not_found`
- 符号不存在：返回空数组 `[]`
- snapshot 未就绪：register 后未 publish → `snapshot_not_ready`
- 非法参数：qualified_name 缺失 / 空 / 纯空白 / NUL → `invalid_params`
  （dispatch 层前置校验，M2.4 新增）
- 跨 workspace 隔离：A workspace 查不到 B workspace 的符号缺陷
- HTTP 注入：query_issues 自动 register/publish + 注入权威 instance_id
  （register→publish→query.issues 调用序；无 db_path 跳过 publish）
- Python client fail-closed：auto/enterprise 模式 daemon 不可用时不静默
  回退本地 SQLite；get_symbol_issues 仅 local 模式保留 SQL 回退
  （query_issues 全模式无 SQL 回退，local 返回 None）

前置条件（与 test_query_grep_rpc.py 一致）：
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
    reason="进程级 query.issues round-trip 需要 Windows + Named Pipe",
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
def query_issues_env():
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

    tmp = tempfile.mkdtemp(prefix="cw_query_issues_")
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
                       file_path: str, symbols: list,
                       semgrep_rows: list, guardrail_rows: list) -> None:
    """构造 query.issues 所需的最小 snapshot SQLite（对齐 Rust fixture schema）。

    query_local_issues 的查询链：
    - 符号定位：file_symbol_versions → file_versions → file_instances
      （WHERE workspace_id + status!='archived' + is_current=1 + is_deleted=0
      + qualified_name 精确匹配）
    - semgrep_findings：file_instance_id + symbol_qualified 匹配/空/行范围交集
    - guardrail_findings：JOIN guardrail_rules（workspace_id + file_path
      + symbol_hash + status!='orphaned'）
    build_and_publish 要求表结构完整（对齐 M2.3 query.grep fixture schema）。
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
        for row in semgrep_rows:
            conn.execute(
                "INSERT INTO semgrep_findings (file_instance_id, rule_id, rule_name, "
                "severity, confidence, message, start_line, end_line, snippet, fix, "
                "symbol_qualified) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                row,
            )
        # guardrail_rows 为 tuple 列表：(workspace_id, rule_id, file_path,
        # symbol_hash, severity, status, message, detected_at)；guardrail_rules
        # 只需 rule_id 存在即可供 JOIN（category 测试占位）。
        for row in guardrail_rows:
            conn.execute(
                "INSERT INTO guardrail_rules (rule_id, category) VALUES (?,?)",
                (row[1], "test"),
            )
        for row in guardrail_rows:
            conn.execute(
                "INSERT INTO guardrail_findings (workspace_id, rule_id, file_path, "
                "symbol_hash, severity, status, message, detected_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                row,
            )
        conn.commit()
    finally:
        conn.close()


def _query_issues(client, instance_id: str, qualified_name: str, **extra):
    """调用 query.issues，成功返回 result（list），失败抛 DaemonRemoteError。"""
    params = {"workspace_instance_id": instance_id, "qualified_name": qualified_name}
    params.update(extra)
    return client.call("query.issues", params)


# ----------------------------------------------------------------------
# 进程级 round-trip 测试
# ----------------------------------------------------------------------

class TestQueryIssuesRpc:
    @requires_binaries
    def test_query_issues_success(self, query_issues_env):
        """成功：符号存在时返回 semgrep + guardrail 合并数组（含 source 字段）。"""
        client, tmp, _proc = query_issues_env
        root = os.path.join(tmp, "ws1")
        os.makedirs(root)
        instance_id, workspace_id = _register_workspace(client, root)
        db_path = os.path.join(tmp, "ws1_snapshot.db")
        _build_snapshot_db(
            db_path, workspace_id, root, "a.py",
            symbols=[
                {"hash": "hash-alpha", "kind": "fn", "name": "alpha",
                 "qualified_name": "a.alpha", "module": "a", "visibility": "public",
                 "start_line": 1, "end_line": 3},
            ],
            semgrep_rows=[
                (1, "R-SEMG1", "no-unused", "WARNING", "HIGH",
                 "alpha has unused arg", 1, 1, "def alpha", "remove it", "a.alpha"),
            ],
            guardrail_rows=[
                (workspace_id, "GR-1", "a.py", "hash-alpha",
                 "warn", "open", "guard issue on alpha", 1700000000.0),
            ],
        )
        client.publish_snapshot(instance_id, db_path)

        result = _query_issues(client, instance_id, "a.alpha")
        assert isinstance(result, list), f"应返回结构化数组，实际 {result!r}"
        assert len(result) == 2, f"应返回 2 条（semgrep+guardrail），实际 {len(result)}"
        semgrep = result[0]
        assert semgrep["source"] == "semgrep"
        assert semgrep["rule_id"] == "R-SEMG1"
        assert semgrep["severity"] == "WARNING"
        assert semgrep["start_line"] == 1
        assert semgrep["snippet"] == "def alpha"
        guardrail = result[1]
        assert guardrail["source"] == "guardrail"
        assert guardrail["rule_id"] == "GR-1"
        assert guardrail["severity"] == "warn"
        assert guardrail["status"] == "open"
        assert guardrail["start_line"] == 0
        assert guardrail["end_line"] == 0

    @requires_binaries
    def test_query_issues_include_info_filter(self, query_issues_env):
        """include_info=false 排除 INFO/UNKNOWN 与 info 级，true 全量返回。"""
        client, tmp, _proc = query_issues_env
        root = os.path.join(tmp, "ws_info")
        os.makedirs(root)
        instance_id, workspace_id = _register_workspace(client, root)
        db_path = os.path.join(tmp, "ws_info.db")
        _build_snapshot_db(
            db_path, workspace_id, root, "a.py",
            symbols=[
                {"hash": "hash-alpha", "kind": "fn", "name": "alpha",
                 "qualified_name": "a.alpha", "module": "a", "visibility": "public",
                 "start_line": 1, "end_line": 5},
            ],
            semgrep_rows=[
                (1, "R-ERR", "bad", "ERROR", "HIGH", "err", 1, 1, "s", "f", "a.alpha"),
                (1, "R-INFO", "hint", "INFO", "LOW", "hint msg", 2, 2, "s", "f", "a.alpha"),
            ],
            guardrail_rows=[
                (workspace_id, "GR-ERR", "a.py", "hash-alpha",
                 "error", "open", "g err", 1700000000.0),
                (workspace_id, "GR-INFO", "a.py", "hash-alpha",
                 "info", "open", "g info", 1700000001.0),
            ],
        )
        client.publish_snapshot(instance_id, db_path)

        # include_info 缺省 false：INFO/UNKNOWN/info 级被过滤
        result = _query_issues(client, instance_id, "a.alpha")
        sources = [(r["source"], r["rule_id"]) for r in result]
        assert ("semgrep", "R-ERR") in sources
        assert ("semgrep", "R-INFO") not in sources, "INFO 级 semgrep 应被过滤"
        assert ("guardrail", "GR-ERR") in sources
        assert ("guardrail", "GR-INFO") not in sources, "info 级 guardrail 应被过滤"

        # include_info=true：全量返回
        result = _query_issues(client, instance_id, "a.alpha", include_info=True)
        sources = [(r["source"], r["rule_id"]) for r in result]
        assert ("semgrep", "R-INFO") in sources, "include_info=true 应包含 INFO 级"
        assert ("guardrail", "GR-INFO") in sources, "include_info=true 应包含 info 级"

    @requires_binaries
    def test_query_issues_unknown_workspace_rejected(self, query_issues_env):
        client, _tmp, _proc = query_issues_env
        with pytest.raises(DaemonRemoteError) as exc:
            _query_issues(client, "deadbeefdeadbeef01", "a.alpha")
        assert exc.value.code == "workspace_not_found"

    @requires_binaries
    def test_query_issues_no_symbol_returns_empty_array(self, query_issues_env):
        """符号不存在时返回空数组 []（既有契约，非文本提示）。"""
        client, tmp, _proc = query_issues_env
        root = os.path.join(tmp, "ws_nosym")
        os.makedirs(root)
        instance_id, workspace_id = _register_workspace(client, root)
        db_path = os.path.join(tmp, "ws_nosym.db")
        _build_snapshot_db(
            db_path, workspace_id, root, "a.py",
            symbols=[
                {"hash": "hash-alpha", "kind": "fn", "name": "alpha",
                 "qualified_name": "a.alpha", "module": "a", "visibility": "public",
                 "start_line": 1, "end_line": 3},
            ],
            semgrep_rows=[
                (1, "R-SEMG1", "no-unused", "WARNING", "HIGH",
                 "alpha has issue", 1, 1, "def alpha", "fix it", "a.alpha"),
            ],
            guardrail_rows=[],
        )
        client.publish_snapshot(instance_id, db_path)

        result = _query_issues(client, instance_id, "b.other_symbol")
        assert result == [], f"符号不存在应返回空数组，实际 {result!r}"

    @requires_binaries
    def test_query_issues_snapshot_not_ready_rejected(self, query_issues_env):
        client, tmp, _proc = query_issues_env
        root = os.path.join(tmp, "ws_nosnap")
        os.makedirs(root)
        instance_id, _workspace_id = _register_workspace(client, root)
        with pytest.raises(DaemonRemoteError) as exc:
            _query_issues(client, instance_id, "a.alpha")
        assert exc.value.code == "snapshot_not_ready"

    @requires_binaries
    def test_query_issues_invalid_params_rejected(self, query_issues_env):
        """M2.4 新增 dispatch 层前置校验：qualified_name 形态非法 → invalid_params。"""
        client, tmp, _proc = query_issues_env
        root = os.path.join(tmp, "ws_param")
        os.makedirs(root)
        instance_id, _workspace_id = _register_workspace(client, root)
        # qualified_name 缺失
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("query.issues", {"workspace_instance_id": instance_id})
        assert exc.value.code == "invalid_params"
        # 空字符串
        with pytest.raises(DaemonRemoteError) as exc:
            _query_issues(client, instance_id, "")
        assert exc.value.code == "invalid_params"
        # 纯空白
        with pytest.raises(DaemonRemoteError) as exc:
            _query_issues(client, instance_id, "   ")
        assert exc.value.code == "invalid_params"
        # NUL 字节
        with pytest.raises(DaemonRemoteError) as exc:
            _query_issues(client, instance_id, "a\x00b")
        assert exc.value.code == "invalid_params"

    @requires_binaries
    def test_query_issues_cross_workspace_isolation(self, query_issues_env):
        """A workspace 查不到 B workspace 的符号缺陷（按 workspace 隔离）。"""
        client, tmp, _proc = query_issues_env
        root1 = os.path.join(tmp, "ws_iso_a")
        root2 = os.path.join(tmp, "ws_iso_b")
        os.makedirs(root1)
        os.makedirs(root2)
        ws1, ws1_id = _register_workspace(client, root1)
        ws2, ws2_id = _register_workspace(client, root2)
        db1 = os.path.join(tmp, "ws_iso_a.db")
        db2 = os.path.join(tmp, "ws_iso_b.db")
        _build_snapshot_db(
            db1, ws1_id, root1, "a.py",
            symbols=[
                {"hash": "hash-alpha", "kind": "fn", "name": "alpha",
                 "qualified_name": "a.alpha", "module": "a", "visibility": "public",
                 "start_line": 1, "end_line": 3},
            ],
            semgrep_rows=[
                (1, "R-A", "rule-a", "WARNING", "HIGH", "alpha issue", 1, 1, "s", "f", "a.alpha"),
            ],
            guardrail_rows=[],
        )
        _build_snapshot_db(
            db2, ws2_id, root2, "b.py",
            symbols=[
                {"hash": "hash-gamma", "kind": "fn", "name": "gamma",
                 "qualified_name": "b.gamma", "module": "b", "visibility": "private",
                 "start_line": 1, "end_line": 3},
            ],
            semgrep_rows=[
                (1, "R-B", "rule-b", "WARNING", "HIGH", "gamma issue", 1, 1, "s", "f", "b.gamma"),
            ],
            guardrail_rows=[],
        )
        client.publish_snapshot(ws1, db1)
        client.publish_snapshot(ws2, db2)

        # ws1 查 ws2 的符号 → 空数组
        assert _query_issues(client, ws1, "b.gamma") == []
        # ws2 查 ws1 的符号 → 空数组
        assert _query_issues(client, ws2, "a.alpha") == []
        # ws1 自己的符号可见
        result = _query_issues(client, ws1, "a.alpha")
        assert [r["rule_id"] for r in result] == ["R-A"]
        # ws2 自己的符号可见
        result = _query_issues(client, ws2, "b.gamma")
        assert [r["rule_id"] for r in result] == ["R-B"]


# ----------------------------------------------------------------------
# Python client fail-closed 单测（禁 daemon，仅 stub _remote_query）
# ----------------------------------------------------------------------

class TestClientFailClosed:
    def _stub_client(self):
        from callwarden.server.daemon_client import DaemonClient, _NO_REMOTE
        client = DaemonClient.__new__(DaemonClient)
        client._sql_fallbacks = 0
        client._remote_query = lambda method, params, db_path=None: _NO_REMOTE
        return client, _NO_REMOTE

    def test_query_issues_fail_closed_when_auto_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "auto")
        client, _sentinel = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.query_issues("crate::foo")
        assert client._sql_fallbacks == 0, "fail-closed 不得计入 SQL fallback"

    def test_query_issues_fail_closed_when_enterprise_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "enterprise")
        client, _sentinel = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.query_issues("crate::foo")
        assert client._sql_fallbacks == 0

    def test_query_issues_returns_none_in_local_mode(self, monkeypatch):
        """local 模式无 daemon：缺陷分析由本地 IssueAnalyzerMixin 负责，
        client 不承担 SQL 回退，返回 None 表示"由本地缺陷分析组件处理"。"""
        from callwarden.server import daemon_client as dc_module
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "local")
        client, _sentinel = self._stub_client()
        result = client.query_issues("crate::foo")
        assert result is None
        assert client._sql_fallbacks == 0

    def test_query_issues_remote_hit_returns_daemon_result(self, monkeypatch):
        """remote 命中时直接返回 daemon 结果，不计数 fallback。"""
        from callwarden.server import daemon_client as dc_module
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "auto")
        from callwarden.server.daemon_client import DaemonClient
        client = DaemonClient.__new__(DaemonClient)
        client._sql_fallbacks = 0
        expected = [{"rule_id": "R-1", "source": "semgrep"}]
        client._remote_query = lambda method, params, db_path=None: expected
        result = client.query_issues("crate::foo", include_info=True)
        assert result == expected
        assert client._sql_fallbacks == 0


# ----------------------------------------------------------------------
# HTTP client workspace 注入单测（M2.4 HTTP 轮次，禁真实 daemon）
# ----------------------------------------------------------------------

class _HttpInjectionHarness:
    """query.issues 版 HTTP 注入 harness（对齐 test_query_symbol_rpc.py）。"""

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
                return {"workspace_id": 1, "workspace_instance_id": "inst-http-qi"}
            if method == "snapshot.publish":
                return {"ok": True, "snapshot_id": "snap-qi"}
            if method == "query.issues":
                return [{"source": "semgrep", "rule_id": "R-1", "severity": "WARNING"}]
            raise AssertionError(f"意外 method: {method}")

        monkeypatch.setattr(client, "call", fake_call)
        return client, calls


class TestHttpClientWorkspaceInjection:
    """M2.4 HTTP 轮次：query_issues 自动 register/publish + 注入权威 instance_id。

    核心断言：query.issues 请求必须携带 workspace_instance_id（Rust
    handle_query_issues 强制 require，缺注入报 invalid_params——即 MCP
    get_symbol_issues HTTP 分支本轮修复的缺陷）。
    """

    def test_http_query_issues_registers_and_injects_instance_id(self, monkeypatch):
        client, calls = _HttpInjectionHarness._make_client(monkeypatch)
        db_path = os.path.join(os.getcwd(), "snap.db")

        result = client.query_issues("a.alpha", include_info=True, db_path=db_path)

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "snapshot.publish", "query.issues"], \
            f"调用序应为 register→publish→query.issues，实际 {methods}"
        # register：client_view_root 默认取进程 cwd（与 legacy 对齐）
        assert calls[0][1] == {"client_view_root": os.getcwd()}
        # publish：注入权威 instance_id + 透传 db_path（abspath 规范化）
        assert calls[1][1]["workspace_instance_id"] == "inst-http-qi"
        assert calls[1][1]["db_path"] == os.path.abspath(db_path)
        assert calls[1][1]["build_context_hash"] == ""
        # query.issues：注入权威 instance_id —— 本修复的核心断言
        assert calls[2][1] == {
            "qualified_name": "a.alpha",
            "include_info": True,
            "workspace_instance_id": "inst-http-qi",
        }
        assert result == [{"source": "semgrep", "rule_id": "R-1", "severity": "WARNING"}]

    def test_http_query_issues_reuses_registered_workspace(self, monkeypatch):
        client, calls = _HttpInjectionHarness._make_client(monkeypatch)
        db_path = os.path.join(os.getcwd(), "snap.db")

        client.query_issues("a.alpha", db_path=db_path)
        client.query_issues("a.beta", db_path=db_path)

        methods = [m for m, _ in calls]
        assert methods.count("workspace.register") == 1, "重复调用不得重复 register"
        assert methods.count("snapshot.publish") == 1, "重复调用不得重复 publish"
        assert methods.count("query.issues") == 2
        # 第二次 query.issues 仍注入同一权威 instance_id
        assert calls[-1][1]["workspace_instance_id"] == "inst-http-qi"
        assert calls[-1][1]["qualified_name"] == "a.beta"

    def test_http_query_issues_register_missing_instance_id_raises(self, monkeypatch):
        from callwarden.server.daemon_client import DaemonUnavailableError
        client, _calls = _HttpInjectionHarness._make_client(monkeypatch, register_ok=False)
        with pytest.raises(DaemonUnavailableError):
            client.query_issues("a.alpha", db_path=os.path.join(os.getcwd(), "snap.db"))

    def test_http_query_issues_without_db_path_skips_publish(self, monkeypatch):
        client, calls = _HttpInjectionHarness._make_client(monkeypatch)
        result = client.query_issues("a.alpha")

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "query.issues"], \
            f"无 db_path 时不应 publish，实际 {methods}"
        # 即使未 publish，query.issues 仍注入权威 instance_id
        assert calls[-1][1]["workspace_instance_id"] == "inst-http-qi"
        assert result == [{"source": "semgrep", "rule_id": "R-1", "severity": "WARNING"}]


# ----------------------------------------------------------------------
# legacy get_symbol_issues fail-closed 单测（M2.4 HTTP 轮次补充）
# ----------------------------------------------------------------------

class TestGetSymbolIssuesFailClosed:
    """legacy DaemonClient.get_symbol_issues：仅 local 保留 SQL 回退，
    auto/enterprise daemon 不可用 raise（对齐 query_issues L1238 与
    M2.2 get_symbol_location 修复模式）。"""

    class _FakeDb:
        def get_symbol_issues(self, qualified_name, include_info=False):
            return [{"rule_id": "R-LOCAL", "source": "semgrep", "severity": "WARNING"}]

    def _stub_client(self):
        from callwarden.server.daemon_client import DaemonClient, _NO_REMOTE
        client = DaemonClient.__new__(DaemonClient)
        client._sql_fallbacks = 0
        client._remote_query = lambda method, params, db_path=None: _NO_REMOTE
        client._get_db = lambda: self._FakeDb()
        return client

    def test_get_symbol_issues_fail_closed_when_auto_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "auto")
        client = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.get_symbol_issues("crate::foo")
        assert client._sql_fallbacks == 0, "fail-closed 不得计入 SQL fallback"

    def test_get_symbol_issues_fail_closed_when_enterprise_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "enterprise")
        client = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.get_symbol_issues("crate::foo")
        assert client._sql_fallbacks == 0

    def test_get_symbol_issues_allows_sql_only_in_local_mode(self, monkeypatch):
        """local 模式保留本地 SQL 路径（设计决策非 fallback，计入 fallback 计数）。"""
        from callwarden.server import daemon_client as dc_module
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "local")
        client = self._stub_client()
        result = client.get_symbol_issues("crate::foo")
        assert result == [{"rule_id": "R-LOCAL", "source": "semgrep", "severity": "WARNING"}]
        assert client._sql_fallbacks == 1, "local 模式 SQL 回退应计入 fallback 计数"

    def test_get_symbol_issues_remote_hit_returns_daemon_result(self, monkeypatch):
        """remote 命中时直接返回 daemon 结果，不计数 fallback。"""
        from callwarden.server import daemon_client as dc_module
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "auto")
        from callwarden.server.daemon_client import DaemonClient
        client = DaemonClient.__new__(DaemonClient)
        client._sql_fallbacks = 0
        expected = [{"rule_id": "R-1", "source": "semgrep"}]
        client._remote_query = lambda method, params, db_path=None: expected
        result = client.get_symbol_issues("crate::foo", include_info=True)
        assert result == expected
        assert client._sql_fallbacks == 0
