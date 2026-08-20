r"""M2.5 query.tests daemon RPC 实证（任务 T-1786584287058-7f712ff4；HTTP 轮次
T-1786519211837-fdfffe10）。

验证 query.tests 按符号查询（test cases / tested functions / stability）从
Python 本地 SQLite 迁移到 Rust daemon RPC 后的成功路径与拒绝矩阵，全程启动
真实 `cw-daemon`（隔离临时数据目录），通过 Windows Named Pipe 进程级 RPC
往返验证，**禁 mock**（Client 侧 fail-closed / HTTP 注入单测除外）。

与 M2.4 query.issues 的关键差异：
- handler 支持三个子模式：reverse=false/history=false → 正向 test cases；
  reverse=true → 反向 tested functions；history=true → 稳定性摘要
- 查询对象是 snapshot SQLite 中的 `test_case_relations` / `test_runs`
  （按符号精确匹配），不是 semgrep/guardrail findings
- Rust handler 返回结构化数组/dict：正向/反向返回数组（无匹配为空数组 `[]`），
  history 返回 `{total_runs, pass_rate, avg_duration_ms, ...}`
- dispatch 层前置校验（M2.5 新增）：qualified_name 空/纯空白/NUL → invalid_params
- Python client fail-closed：auto/enterprise 模式 daemon 不可用抛
  DaemonUnavailableError（不回退本地 SQLite）；仅 local 模式返回 None

覆盖矩阵（账本 §9.3 统一验收标准第 7 项）：
- 成功查询（正向）：workspace.register → snapshot.publish → query.tests
  返回 test cases 数组（test_fn_id/confidence/test_name/test_file 等）
- 成功查询（反向）：reverse=true → tested functions 数组
- 成功查询（history）：history=true → 稳定性 dict（total_runs/by_test 等）
- 未知 workspace 拒绝：`workspace_not_found`
- 符号不存在：返回空数组 `[]`
- snapshot 未就绪：register 后未 publish → `snapshot_not_ready`
- 非法参数：qualified_name 缺失 / 空 / 纯空白 / NUL → `invalid_params`
  （dispatch 层前置校验，M2.5 新增）
- 跨 workspace 隔离：A workspace 查不到 B workspace 的测试关系
- Python client fail-closed：auto/enterprise 模式 daemon 不可用时不静默
  回退本地 SQLite（仅 local 模式返回 None，表示由本地测试关系组件处理）

HTTP 轮次（T-1786519211837-fdfffe10）补充：
- `HttpDaemonRpcClient.query_tests` 便捷方法：自动 workspace.register +
  按需 snapshot.publish，注入权威 workspace_instance_id 后调 query.tests
  （修复 MCP get_test_cases/get_tested_functions/get_test_coverage_summary/
  get_test_stability HTTP 分支缺注入导致 invalid_params 的缺陷）
- HTTP 注入单测：register→publish→query.tests 调用序 + 权威 instance_id 注入
  + reverse/history/limit 参数透传（对齐 test_query_issues_rpc.py
  TestHttpClientWorkspaceInjection 模式）
- legacy 工具方法 fail-closed 确认：get_test_cases/get_tested_functions/
  get_test_coverage_summary/get_test_stability 复用 query_tests 已继承
  fail-closed（auto/enterprise raise DaemonUnavailableError、local 返回
  None），本轮不改仅补测试确认

前置条件（与 test_query_issues_rpc.py 一致）：
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
    reason="进程级 query.tests round-trip 需要 Windows + Named Pipe",
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
def query_tests_env():
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

    tmp = tempfile.mkdtemp(prefix="cw_query_tests_")
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
                       test_relations: list = None,
                       test_runs: list = None) -> None:
    """构造 query.tests 所需的最小 snapshot SQLite（对齐 Rust fixture schema）。

    query_local_test_cases/tested_functions/stability 的查询链：
    - 符号定位：test_case_relations JOIN symbols JOIN file_instances
      （WHERE workspace_id + 被测/测试符号 qualified_name 精确匹配）
    - stability：test_runs 关联 test_case_relations 中的 test_fn_id
    build_and_publish 要求表结构完整（对齐 M2.4 query.issues fixture schema），
    在此基础上追加 test_case_relations / test_runs 两个测试关系表。

    test_relations 每项：tuple(tested_qualified_name, test_qualified_name,
                              match_method, confidence)
    test_runs 每项：tuple(test_qualified_name, status, duration_ms,
                          error_message, error_type, run_at, test_name, ci_run_id)
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
            CREATE TABLE test_case_relations (
                workspace_id INTEGER NOT NULL,
                test_fn_id INTEGER NOT NULL,
                tested_fn_id INTEGER NOT NULL,
                match_method TEXT NOT NULL,
                confidence TEXT NOT NULL
            );
            CREATE TABLE test_runs (
                workspace_id INTEGER NOT NULL,
                test_fn_id INTEGER NOT NULL,
                status TEXT NOT NULL,
                duration_ms REAL NOT NULL,
                error_message TEXT,
                error_type TEXT,
                run_at REAL NOT NULL,
                test_name TEXT NOT NULL,
                ci_run_id TEXT
            );
            INSERT INTO file_versions VALUES (10, 1, 1);
        """)
        by_qn = {}
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
            by_qn[symbol["qualified_name"]] = idx
        for tested_qn, test_qn, match_method, confidence in (test_relations or []):
            conn.execute(
                "INSERT INTO test_case_relations (workspace_id, test_fn_id, "
                "tested_fn_id, match_method, confidence) VALUES (?,?,?,?,?)",
                (workspace_id, by_qn[test_qn], by_qn[tested_qn],
                 match_method, confidence),
            )
        for test_qn, status, duration_ms, error_message, error_type, run_at, \
                test_name, ci_run_id in (test_runs or []):
            conn.execute(
                "INSERT INTO test_runs (workspace_id, test_fn_id, status, "
                "duration_ms, error_message, error_type, run_at, test_name, "
                "ci_run_id) VALUES (?,?,?,?,?,?,?,?,?)",
                (workspace_id, by_qn[test_qn], status, duration_ms,
                 error_message, error_type, run_at, test_name, ci_run_id),
            )
        conn.commit()
    finally:
        conn.close()


def _query_tests(client, instance_id: str, qualified_name: str, **extra):
    """调用 query.tests，成功返回 result，失败抛 DaemonRemoteError。"""
    params = {"workspace_instance_id": instance_id, "qualified_name": qualified_name}
    params.update(extra)
    return client.call("query.tests", params)


# ----------------------------------------------------------------------
# 进程级 round-trip 测试
# ----------------------------------------------------------------------

class TestQueryTestsRpc:
    @requires_binaries
    def test_query_tests_success(self, query_tests_env):
        """成功（正向）：符号存在时返回 test cases 数组（含 confidence/行号/文件）。"""
        client, tmp, _proc = query_tests_env
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
                {"hash": "hash-test-alpha", "kind": "fn", "name": "test_alpha",
                 "qualified_name": "a.test_alpha", "module": "a", "visibility": "public",
                 "start_line": 10, "end_line": 20},
            ],
            test_relations=[
                ("a.alpha", "a.test_alpha", "direct_call", "high"),
            ],
        )
        client.publish_snapshot(instance_id, db_path)

        result = _query_tests(client, instance_id, "a.alpha")
        assert isinstance(result, list), f"应返回结构化数组，实际 {result!r}"
        assert len(result) == 1, f"应返回 1 条 test case，实际 {len(result)}"
        case = result[0]
        assert case["test_fn_id"] == 2
        assert case["match_method"] == "direct_call"
        assert case["confidence"] == "high"
        assert case["test_name"] == "test_alpha"
        assert case["test_qualified_name"] == "a.test_alpha"
        assert case["test_start_line"] == 10
        assert case["test_file"] == "a.py"

    @requires_binaries
    def test_query_tests_reverse(self, query_tests_env):
        """成功（反向）：reverse=true 返回被测函数（tested functions）数组。"""
        client, tmp, _proc = query_tests_env
        root = os.path.join(tmp, "ws_rev")
        os.makedirs(root)
        instance_id, workspace_id = _register_workspace(client, root)
        db_path = os.path.join(tmp, "ws_rev.db")
        _build_snapshot_db(
            db_path, workspace_id, root, "a.py",
            symbols=[
                {"hash": "hash-alpha", "kind": "fn", "name": "alpha",
                 "qualified_name": "a.alpha", "module": "a", "visibility": "public",
                 "start_line": 1, "end_line": 3},
                {"hash": "hash-test-alpha", "kind": "fn", "name": "test_alpha",
                 "qualified_name": "a.test_alpha", "module": "a", "visibility": "public",
                 "start_line": 10, "end_line": 20},
            ],
            test_relations=[
                ("a.alpha", "a.test_alpha", "direct_call", "high"),
            ],
        )
        client.publish_snapshot(instance_id, db_path)

        result = _query_tests(client, instance_id, "a.test_alpha", reverse=True)
        assert isinstance(result, list), f"应返回结构化数组，实际 {result!r}"
        assert len(result) == 1, f"应返回 1 条被测函数，实际 {len(result)}"
        tested = result[0]
        assert tested["tested_fn_id"] == 1
        assert tested["confidence"] == "high"
        assert tested["tested_name"] == "alpha"
        assert tested["tested_qualified_name"] == "a.alpha"
        assert tested["tested_start_line"] == 1
        assert tested["tested_end_line"] == 3
        assert tested["tested_file"] == "a.py"

    @requires_binaries
    def test_query_tests_history(self, query_tests_env):
        """成功（history）：history=true 返回稳定性 dict（total_runs/by_test 等）。"""
        client, tmp, _proc = query_tests_env
        root = os.path.join(tmp, "ws_hist")
        os.makedirs(root)
        instance_id, workspace_id = _register_workspace(client, root)
        db_path = os.path.join(tmp, "ws_hist.db")
        _build_snapshot_db(
            db_path, workspace_id, root, "a.py",
            symbols=[
                {"hash": "hash-alpha", "kind": "fn", "name": "alpha",
                 "qualified_name": "a.alpha", "module": "a", "visibility": "public",
                 "start_line": 1, "end_line": 3},
                {"hash": "hash-test-alpha", "kind": "fn", "name": "test_alpha",
                 "qualified_name": "a.test_alpha", "module": "a", "visibility": "public",
                 "start_line": 10, "end_line": 20},
            ],
            test_relations=[
                ("a.alpha", "a.test_alpha", "direct_call", "high"),
            ],
            test_runs=[
                # error_message/error_type 为 String（非 NULL），passed 记录传空字符串
                ("a.test_alpha", "passed", 12.5, "", "", 1700000000.0, "test_alpha", "ci-1"),
                ("a.test_alpha", "failed", 30.0, "boom", "AssertionError", 1700000001.0, "test_alpha", "ci-2"),
            ],
        )
        client.publish_snapshot(instance_id, db_path)

        result = _query_tests(client, instance_id, "a.alpha", history=True)
        assert isinstance(result, dict), f"应返回稳定性 dict，实际 {result!r}"
        assert result["total_runs"] == 2
        assert result["pass_rate"] == 0.5
        assert result["avg_duration_ms"] == 21.3  # (12.5+30.0)/2=21.25 → round_to 1 位
        assert len(result["recent_failures"]) == 1
        failure = result["recent_failures"][0]
        assert failure["test_name"] == "test_alpha"
        assert failure["error_type"] == "AssertionError"
        assert failure["error_message"] == "boom"
        assert "test_alpha" in result["by_test"]
        assert result["by_test"]["test_alpha"]["total"] == 2
        assert result["by_test"]["test_alpha"]["passed"] == 1
        assert result["by_test"]["test_alpha"]["failed"] == 1

    @requires_binaries
    def test_query_tests_unknown_workspace_rejected(self, query_tests_env):
        client, _tmp, _proc = query_tests_env
        with pytest.raises(DaemonRemoteError) as exc:
            _query_tests(client, "deadbeefdeadbeef01", "a.alpha")
        assert exc.value.code == "workspace_not_found"

    @requires_binaries
    def test_query_tests_no_match_returns_empty_array(self, query_tests_env):
        """符号不存在时返回空数组 []（既有契约，非文本提示）。"""
        client, tmp, _proc = query_tests_env
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
            test_relations=[],
        )
        client.publish_snapshot(instance_id, db_path)

        result = _query_tests(client, instance_id, "b.other_symbol")
        assert result == [], f"符号不存在应返回空数组，实际 {result!r}"

    @requires_binaries
    def test_query_tests_snapshot_not_ready_rejected(self, query_tests_env):
        client, tmp, _proc = query_tests_env
        root = os.path.join(tmp, "ws_nosnap")
        os.makedirs(root)
        instance_id, _workspace_id = _register_workspace(client, root)
        with pytest.raises(DaemonRemoteError) as exc:
            _query_tests(client, instance_id, "a.alpha")
        assert exc.value.code == "snapshot_not_ready"

    @requires_binaries
    def test_query_tests_invalid_params_rejected(self, query_tests_env):
        """M2.5 新增 dispatch 层前置校验：qualified_name 形态非法 → invalid_params。"""
        client, tmp, _proc = query_tests_env
        root = os.path.join(tmp, "ws_param")
        os.makedirs(root)
        instance_id, _workspace_id = _register_workspace(client, root)
        # qualified_name 缺失
        with pytest.raises(DaemonRemoteError) as exc:
            client.call("query.tests", {"workspace_instance_id": instance_id})
        assert exc.value.code == "invalid_params"
        # 空字符串
        with pytest.raises(DaemonRemoteError) as exc:
            _query_tests(client, instance_id, "")
        assert exc.value.code == "invalid_params"
        # 纯空白
        with pytest.raises(DaemonRemoteError) as exc:
            _query_tests(client, instance_id, "   ")
        assert exc.value.code == "invalid_params"
        # NUL 字节
        with pytest.raises(DaemonRemoteError) as exc:
            _query_tests(client, instance_id, "a\x00b")
        assert exc.value.code == "invalid_params"

    @requires_binaries
    def test_query_tests_cross_workspace_isolation(self, query_tests_env):
        """A workspace 查不到 B workspace 的测试关系（按 workspace 隔离）。"""
        client, tmp, _proc = query_tests_env
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
                {"hash": "hash-test-alpha", "kind": "fn", "name": "test_alpha",
                 "qualified_name": "a.test_alpha", "module": "a", "visibility": "public",
                 "start_line": 10, "end_line": 20},
            ],
            test_relations=[
                ("a.alpha", "a.test_alpha", "direct_call", "high"),
            ],
        )
        _build_snapshot_db(
            db2, ws2_id, root2, "b.py",
            symbols=[
                {"hash": "hash-gamma", "kind": "fn", "name": "gamma",
                 "qualified_name": "b.gamma", "module": "b", "visibility": "private",
                 "start_line": 1, "end_line": 3},
                {"hash": "hash-test-gamma", "kind": "fn", "name": "test_gamma",
                 "qualified_name": "b.test_gamma", "module": "b", "visibility": "private",
                 "start_line": 10, "end_line": 20},
            ],
            test_relations=[
                ("b.gamma", "b.test_gamma", "name_convention", "mid"),
            ],
        )
        client.publish_snapshot(ws1, db1)
        client.publish_snapshot(ws2, db2)

        # ws1 查 ws2 的符号 → 空数组
        assert _query_tests(client, ws1, "b.gamma") == []
        # ws2 查 ws1 的符号 → 空数组
        assert _query_tests(client, ws2, "a.alpha") == []
        # ws1 自己的符号可见
        result = _query_tests(client, ws1, "a.alpha")
        assert [c["test_name"] for c in result] == ["test_alpha"]
        # ws2 自己的符号可见
        result = _query_tests(client, ws2, "b.gamma")
        assert [c["test_name"] for c in result] == ["test_gamma"]


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

    def test_query_tests_fail_closed_when_auto_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "auto")
        client, _sentinel = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.query_tests("crate::foo")
        assert client._sql_fallbacks == 0, "fail-closed 不得计入 SQL fallback"

    def test_query_tests_fail_closed_when_enterprise_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "enterprise")
        client, _sentinel = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.query_tests("crate::foo")
        assert client._sql_fallbacks == 0

    def test_query_tests_returns_none_in_local_mode(self, monkeypatch):
        """local 模式无 daemon：测试关系由本地组件负责，
        client 不承担 SQL 回退，返回 None 表示"由本地测试关系组件处理"。"""
        from callwarden.server import daemon_client as dc_module
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "local")
        client, _sentinel = self._stub_client()
        result = client.query_tests("crate::foo")
        assert result is None
        assert client._sql_fallbacks == 0

    def test_query_tests_remote_hit_returns_daemon_result(self, monkeypatch):
        """remote 命中时直接返回 daemon 结果，不计数 fallback。"""
        from callwarden.server import daemon_client as dc_module
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "auto")
        from callwarden.server.daemon_client import DaemonClient
        client = DaemonClient.__new__(DaemonClient)
        client._sql_fallbacks = 0
        expected = [{"test_name": "test_alpha", "confidence": "high"}]
        client._remote_query = lambda method, params, db_path=None: expected
        result = client.query_tests("crate::foo")
        assert result == expected
        assert client._sql_fallbacks == 0


# ----------------------------------------------------------------------
# HTTP 注入单测（M2.5 HTTP 轮次：query_tests 自动 register/publish +
# 注入权威 instance_id，对齐 test_query_issues_rpc.py
# TestHttpClientWorkspaceInjection 模式）
# ----------------------------------------------------------------------

class _HttpInjectionHarness:
    """query.tests 版 HTTP 注入 harness（对齐 test_query_issues_rpc.py）。"""

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
                return {"workspace_id": 1, "workspace_instance_id": "inst-http-qt"}
            if method == "snapshot.publish":
                return {"ok": True, "snapshot_id": "snap-qt"}
            if method == "query.tests":
                return [{"test_fn_id": 2, "test_name": "test_alpha",
                         "confidence": "high"}]
            raise AssertionError(f"意外 method: {method}")

        monkeypatch.setattr(client, "call", fake_call)
        return client, calls


class TestHttpClientWorkspaceInjection:
    """M2.5 HTTP 轮次：query_tests 自动 register/publish + 注入权威 instance_id。

    核心断言：query.tests 请求必须携带 workspace_instance_id（Rust
    handle_query_tests L851 强制 require，缺注入报 invalid_params——即 MCP
    get_test_cases 等 4 个工具 HTTP 分支本轮修复的缺陷）。
    """

    def test_http_query_tests_registers_and_injects_instance_id(self, monkeypatch):
        client, calls = _HttpInjectionHarness._make_client(monkeypatch)
        db_path = os.path.join(os.getcwd(), "snap.db")

        result = client.query_tests("a.alpha", db_path=db_path)

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "snapshot.publish", "query.tests"], \
            f"调用序应为 register→publish→query.tests，实际 {methods}"
        # register：client_view_root 默认取进程 cwd（与 legacy 对齐）
        assert calls[0][1] == {"client_view_root": os.getcwd()}
        # publish：注入权威 instance_id + 透传 db_path（abspath 规范化）
        assert calls[1][1]["workspace_instance_id"] == "inst-http-qt"
        assert calls[1][1]["db_path"] == os.path.abspath(db_path)
        assert calls[1][1]["build_context_hash"] == ""
        # query.tests：注入权威 instance_id + 默认 reverse=False/history=False/limit=50
        # —— 本修复的核心断言
        assert calls[2][1] == {
            "qualified_name": "a.alpha",
            "reverse": False,
            "history": False,
            "limit": 50,
            "workspace_instance_id": "inst-http-qt",
        }
        assert result == [{"test_fn_id": 2, "test_name": "test_alpha",
                           "confidence": "high"}]

    def test_http_query_tests_reverse_history_limit_passthrough(self, monkeypatch):
        """reverse/history/limit 非默认参数完整透传（对齐 M2.3 query_grep 测试）。"""
        client, calls = _HttpInjectionHarness._make_client(monkeypatch)
        db_path = os.path.join(os.getcwd(), "snap.db")

        client.query_tests("a.test_alpha", reverse=True, history=True,
                           limit=10, db_path=db_path)

        assert calls[-1][0] == "query.tests"
        assert calls[-1][1]["reverse"] is True
        assert calls[-1][1]["history"] is True
        assert calls[-1][1]["limit"] == 10
        assert calls[-1][1]["workspace_instance_id"] == "inst-http-qt"
        assert calls[-1][1]["qualified_name"] == "a.test_alpha"

    def test_http_query_tests_reuses_registered_workspace(self, monkeypatch):
        client, calls = _HttpInjectionHarness._make_client(monkeypatch)
        db_path = os.path.join(os.getcwd(), "snap.db")

        client.query_tests("a.alpha", db_path=db_path)
        client.query_tests("b.beta", db_path=db_path)

        methods = [m for m, _ in calls]
        assert methods.count("workspace.register") == 1, "重复调用不得重复 register"
        assert methods.count("snapshot.publish") == 1, "重复调用不得重复 publish"
        assert methods.count("query.tests") == 2
        # 第二次 query.tests 仍注入同一权威 instance_id
        assert calls[-1][1]["workspace_instance_id"] == "inst-http-qt"
        assert calls[-1][1]["qualified_name"] == "b.beta"

    def test_http_query_tests_register_missing_instance_id_raises(self, monkeypatch):
        from callwarden.server.daemon_client import DaemonUnavailableError
        client, _calls = _HttpInjectionHarness._make_client(monkeypatch, register_ok=False)
        with pytest.raises(DaemonUnavailableError):
            client.query_tests("a.alpha", db_path=os.path.join(os.getcwd(), "snap.db"))

    def test_http_query_tests_without_db_path_skips_publish(self, monkeypatch):
        client, calls = _HttpInjectionHarness._make_client(monkeypatch)
        result = client.query_tests("a.alpha")

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "query.tests"], \
            f"无 db_path 时不应 publish，实际 {methods}"
        # 即使未 publish，query.tests 仍注入权威 instance_id
        assert calls[-1][1]["workspace_instance_id"] == "inst-http-qt"
        assert result == [{"test_fn_id": 2, "test_name": "test_alpha",
                           "confidence": "high"}]


# ----------------------------------------------------------------------
# legacy 工具方法 fail-closed 确认（M2.5 HTTP 轮次补充）
# get_test_cases/get_tested_functions/get_test_coverage_summary/
# get_test_stability 复用 query_tests 已继承 fail-closed，本轮不改仅补测试确认
# ----------------------------------------------------------------------

class TestGetTestToolsFailClosed:
    """legacy DaemonClient 4 个 tests 工具方法：复用 query_tests 继承 fail-closed。"""

    def _stub_client(self):
        from callwarden.server.daemon_client import DaemonClient, _NO_REMOTE
        client = DaemonClient.__new__(DaemonClient)
        client._sql_fallbacks = 0
        client._remote_query = lambda method, params, db_path=None: _NO_REMOTE
        return client

    def test_get_test_cases_fail_closed_when_auto_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "auto")
        client = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.get_test_cases("crate::foo")
        assert client._sql_fallbacks == 0, "fail-closed 不得计入 SQL fallback"

    def test_get_tested_functions_fail_closed_when_auto_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "auto")
        client = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.get_tested_functions("crate::test_foo")
        assert client._sql_fallbacks == 0

    def test_get_test_coverage_summary_fail_closed_when_auto_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "auto")
        client = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.get_test_coverage_summary("crate::foo")
        assert client._sql_fallbacks == 0

    def test_get_test_stability_fail_closed_when_auto_and_daemon_down(self, monkeypatch):
        from callwarden.server import daemon_client as dc_module
        from callwarden.server.daemon_client import DaemonUnavailableError
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "auto")
        client = self._stub_client()
        with pytest.raises(DaemonUnavailableError):
            client.get_test_stability("crate::foo")
        assert client._sql_fallbacks == 0

    def test_get_test_tools_returns_none_in_local_mode(self, monkeypatch):
        """local 模式无 daemon：4 个工具方法返回 None（由本地测试关系组件处理）。"""
        from callwarden.server import daemon_client as dc_module
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "local")
        client = self._stub_client()
        assert client.get_test_cases("crate::foo") is None
        assert client.get_tested_functions("crate::test_foo") is None
        assert client.get_test_coverage_summary("crate::foo") is None
        assert client.get_test_stability("crate::foo") is None
        assert client._sql_fallbacks == 0

    def test_get_test_tools_remote_hit_returns_daemon_result(self, monkeypatch):
        """remote 命中：4 个工具方法返回 daemon 结果（get_test_coverage_summary 聚合）。"""
        from callwarden.server import daemon_client as dc_module
        monkeypatch.setattr(dc_module, "get_daemon_mode", lambda: "auto")
        from callwarden.server.daemon_client import DaemonClient
        expected = [{"test_fn_id": 2, "test_name": "test_alpha", "confidence": "high"}]
        client = DaemonClient.__new__(DaemonClient)
        client._sql_fallbacks = 0
        client._remote_query = lambda method, params, db_path=None: expected
        assert client.get_test_cases("crate::foo") == expected
        assert client.get_tested_functions("crate::test_foo") == expected
        summary = client.get_test_coverage_summary("crate::foo")
        assert summary == {
            "has_tests": True,
            "test_count": 1,
            "high_confidence_count": 1,
            "tests": expected,
        }
        assert client.get_test_stability("crate::foo") == expected
        assert client._sql_fallbacks == 0
