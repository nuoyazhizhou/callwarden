r"""B5（T-1786590722456-db00d074-sub-5）write/jobs legacy 基线核对。

核对 refresh / build / semgrep / 长任务 / 派生索引写入的 legacy 可用入口，
重点验证 timeout、status 和 recovery 语义（plan `legacy-237-tools-baseline-plan.md` B5 定义）。

三类基线：

1. **统一入口**：MCP 工具注册存在（tools_workspace.py 的 build/refresh、tools_task.py
   的异步 job、tools_query.py 的 semgrep），全部走 Python `get_db()` 直调（矩阵
   `python_local` 190 声明，rpc_none 同类），不混入 daemon RPC。

2. **timeout/status/recovery**：异步 job（clone_detect / vector_embed / semgrep_scan）
   提交返回明确 `status=pending` + `job_id`；`wait_for_job` 有 timeout/poll_interval，
   超时返回 `status=timeout`；JobExecutor 有 max_duration 超时 fail、cancel 三态；
   daemon_client `mutation_call` 具备 request_id 幂等 + authority pin + 断线 recovery
   （task.create 断线 fail-closed 不重放，其他 mutation 先查提交结果再决定）。

3. **结构化错误**：mutation 业务错误（DaemonRemoteError）原样透传，不包装为连接错误
   （DaemonUnavailableError）。

本测试为静态一致性基线 + client 语义单测，**不启动真实 daemon**（进程级 round-trip
由既有 test_lease_* / test_query_*_rpc.py 覆盖，避免生产 daemon 占用默认 Named Pipe
导致整体 skip）。若 write/jobs 源码语义变更，本测试会先于进程级测试失败，作为回归哨兵。
"""

import ast
import os
import re

import pytest

from callwarden.server.daemon_client import (
    DaemonUnavailableError,
    UnixDaemonRpcClient,
)
from callwarden.server.daemon_protocol import DaemonRemoteError

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_TOOLS_DIR = os.path.join(_REPO_ROOT, "server", "tools")


def _read_rel(rel_path: str) -> str:
    """读取仓库内源码文件，缺失即失败（结构破坏不应静默）。"""
    full = os.path.join(_REPO_ROOT, rel_path)
    assert os.path.exists(full), f"源码文件缺失：{rel_path}"
    with open(full, encoding="utf-8") as f:
        return f.read()


def _read_tools(mod_name: str) -> str:
    full = os.path.join(_TOOLS_DIR, mod_name)
    assert os.path.exists(full), f"工具模块缺失：{mod_name}"
    with open(full, encoding="utf-8") as f:
        return f.read()


def _tool_body(src: str, tool: str) -> str:
    """提取工具函数的函数体（到下一个顶层 def 或装饰器为止）。"""
    m = re.search(r"\n\s+def\s+%s\b" % re.escape(tool), src)
    assert m, f"源码缺少 def {tool}"
    block = src[m.end():]
    next_def = re.search(r"\n    def ", block)
    next_dec = re.search(r"\n    @", block)
    ends = [x.start() for x in (next_def, next_dec) if x]
    return block[:min(ends)] if ends else block


def _signature(src: str, name: str) -> str:
    """提取 def <name>(...) 的签名行（单行或到冒号前的首行）。"""
    m = re.search(r"def\s+%s\s*\(([^)]*)\)" % re.escape(name), src, re.S)
    assert m, f"未找到签名 def {name}"
    return m.group(1)


# ----------------------------------------------------------------------
# 基线 1：write/jobs 工具注册存在 + Python get_db() 入口（legacy_local）
# ----------------------------------------------------------------------

# 模块 → 写/长任务 MCP 工具（矩阵 python_local / rpc_none 声明）
TOOLS_WRITE = {
    "tools_workspace.py": ["build_graph", "refresh_file", "build_directory"],
    "tools_task.py": [
        "detect_clones_async", "get_job_status", "cancel_job", "list_jobs",
        "get_job_stats", "wait_for_job", "embed_symbols_async", "semgrep_scan_async",
    ],
    "tools_query.py": [
        "get_semgrep_stats", "get_semgrep_findings",
        "run_semgrep_scan", "scan_semgrep_incremental",
    ],
}

# 异步提交类工具：必须调用 executor.submit 并返回 job_id/status/job_type/message
SUBMIT_JOBS = {
    "detect_clones_async": "clone_detect",
    "embed_symbols_async": "vector_embed",
    "semgrep_scan_async": "semgrep_scan",
}


class TestWriteToolsRegistered:
    """write/jobs 工具注册存在，且走 Python get_db() 直调（legacy_local 声明）。"""

    @pytest.mark.parametrize("mod_name", sorted(TOOLS_WRITE))
    def test_tools_registered_with_get_db(self, mod_name):
        src = _read_tools(mod_name)
        for tool in TOOLS_WRITE[mod_name]:
            body = _tool_body(src, tool)
            assert "get_db()" in body, f"{mod_name}.{tool} 未使用 Python get_db() 入口"
            # legacy_local 声明：不混入 daemon RPC（对照 B3 rpc_none 工具）
            assert "_get_daemon_client" not in body, (
                f"{mod_name}.{tool} 不应路由 daemon RPC（矩阵 python_local 声明）"
            )
            assert re.search(r"client\.\w+\s*\(", body) is None, (
                f"{mod_name}.{tool} 不应调用 daemon client 方法"
            )

    def test_build_graph_is_sync_local(self):
        """build_graph 为同步全量构建（Python DB 直调 build_full_graph）。"""
        body = _tool_body(_read_tools("tools_workspace.py"), "build_graph")
        assert "build_full_graph" in body, "build_graph 未调用 db.build_full_graph()"

    def test_refresh_file_is_sync_incremental(self):
        """refresh_file 为同步增量刷新（Python DB 直调 refresh_file）。"""
        body = _tool_body(_read_tools("tools_workspace.py"), "refresh_file")
        assert "refresh_file" in body, "refresh_file 未调用 db.refresh_file()"


# ----------------------------------------------------------------------
# 基线 2a：异步 job 提交/状态/取消/等待的 status 与 timeout 语义
# ----------------------------------------------------------------------

class TestAsyncJobSubmitSemantics:
    """异步 job 提交返回明确状态与 job_id，不阻塞 MCP 请求。"""

    @pytest.mark.parametrize("tool", sorted(SUBMIT_JOBS))
    def test_submit_returns_pending_with_job_id(self, tool):
        body = _tool_body(_read_tools("tools_task.py"), tool)
        # 必须通过 executor.submit 提交后台 job
        assert "executor.submit(" in body, f"{tool} 未调用 executor.submit()"
        # 返回四字段：job_id / status(pending) / job_type / message
        for field in ('"job_id"', '"status"', '"job_type"', '"message"'):
            assert field in body, f"{tool} 返回缺少 {field}"
        # job_type 与 submit 调用对应
        assert SUBMIT_JOBS[tool] in body, f"{tool} 的 job_type 应为 {SUBMIT_JOBS[tool]}"


class TestWaitForJobSemantics:
    """wait_for_job 必须有明确 timeout 与超时状态（plan B5：长任务必须有 timeout）。"""

    def test_signature_has_timeout_and_poll_interval(self):
        body = _tool_body(_read_tools("tools_task.py"), "wait_for_job")
        assert "timeout: float = 30.0" in body, "wait_for_job 缺少 timeout=30.0 默认值"
        assert "poll_interval: float = 0.5" in body, "wait_for_job 缺少 poll_interval=0.5"

    def test_deadline_and_timeout_status(self):
        body = _tool_body(_read_tools("tools_task.py"), "wait_for_job")
        assert "deadline = start + timeout" in body, "wait_for_job 缺少 deadline 超时计算"
        assert '"status": "timeout"' in body, "wait_for_job 超时分支必须返回 status=timeout"
        assert '"elapsed"' in body, "wait_for_job 必须返回 elapsed 实际等待秒数"

    def test_terminal_status_returned(self):
        """终态（completed/cancelled/failed）返回 job 状态与结果摘要。"""
        body = _tool_body(_read_tools("tools_task.py"), "wait_for_job")
        assert "job.is_terminal" in body, "wait_for_job 未使用 is_terminal 判断终态"
        assert "result_summary" in body, "wait_for_job 未返回 result_summary"


class TestCancelJobSemantics:
    """cancel_job 三态语义：pending→cancelled / running→cancel_requested / 终态无操作。"""

    def test_cancel_job_three_state_docstring(self):
        body = _tool_body(_read_tools("tools_task.py"), "cancel_job")
        assert "cancelled" in body and "cancel_requested" in body, (
            "cancel_job 必须覆盖 pending 直接取消 + running cancel_requested 语义"
        )

    def test_get_job_status_full_fields(self):
        """get_job_status 返回完整状态字段（status/progress/result/error/时间戳）。"""
        body = _tool_body(_read_tools("tools_task.py"), "get_job_status")
        for field in ('"job_id"', '"job_type"', '"status"', '"progress"',
                      '"result_summary"', '"error"', '"created_at"', '"started_at"',
                      '"finished_at"'):
            assert field in body, f"get_job_status 返回缺少 {field}"


# ----------------------------------------------------------------------
# 基线 2b：JobExecutor 超时/状态/恢复语义（job_executor.py）
# ----------------------------------------------------------------------

class TestJobExecutorRecovery:
    """JobExecutor 的 max_duration 超时 fail、未注册 handler fail、cancel 语义。"""

    def test_max_duration_default_1800(self):
        src = _read_rel("server/job_executor.py")
        assert "max_duration_seconds: int = 1800" in src, (
            "JobExecutor 缺少 max_duration_seconds=1800 超时预算"
        )

    def test_submit_unregistered_handler_fails(self):
        src = _read_rel("server/job_executor.py")
        assert "No handler registered" in src, "未注册 handler 必须立即标记 failed"

    def test_run_job_timeout_fails(self):
        src = _read_rel("server/job_executor.py")
        assert "exceeded max_duration_seconds" in src, "超时必须 fail_job"

    def test_cancel_three_state(self):
        src = _read_rel("server/job_executor.py")
        assert "pending" in src and "cancel_requested" in src, (
            "JobExecutor.cancel 必须区分 pending / running 取消语义"
        )

    def test_status_recovery_get_status(self):
        """查询与取消提供恢复入口（get_status / cancel / list_pending）。"""
        src = _read_rel("server/job_executor.py")
        for method in ("def get_status", "def cancel", "def list_pending"):
            assert method in src, f"JobExecutor 缺少 {method}"


# ----------------------------------------------------------------------
# 基线 2c：semgrep timeout 参数（同步 + 异步 + 增量 + handler）
# ----------------------------------------------------------------------

class TestSemgrepTimeoutSemantics:
    """Semgrep 全部入口必须有 timeout 参数（bounded external process）。"""

    def test_semgrep_scan_async_timeout(self):
        body = _tool_body(_read_tools("tools_task.py"), "semgrep_scan_async")
        assert "timeout: int = 300" in body, "semgrep_scan_async 缺少 timeout=300"
        assert '"timeout"' in body, "semgrep_scan_async 未把 timeout 传入 submit params"

    def test_run_semgrep_scan_timeout(self):
        body = _tool_body(_read_tools("tools_query.py"), "run_semgrep_scan")
        assert "timeout: int = 300" in body, "run_semgrep_scan 缺少 timeout=300"
        assert "run_semgrep_and_save" in body, "run_semgrep_scan 未调用 db.run_semgrep_and_save"

    def test_scan_semgrep_incremental_timeout(self):
        body = _tool_body(_read_tools("tools_query.py"), "scan_semgrep_incremental")
        assert "timeout: int = 300" in body, "scan_semgrep_incremental 缺少 timeout=300"

    def test_semgrep_handler_bounded_process(self):
        """job handler 把 timeout 传给 Semgrep CLI（bounded external process）。"""
        src = _read_rel("server/job_handlers.py")
        assert 'params.get("timeout", 300)' in src, "semgrep_scan_handler 未读取 timeout 参数"
        assert "run_semgrep_and_save" in src, "semgrep_scan_handler 未调用 bounded wrapper"
        assert "timeout=timeout" in src, "semgrep_scan_handler 未把 timeout 传给 wrapper"


# ----------------------------------------------------------------------
# 基线 3：mutation recovery 语义（daemon_client.py，B5 Step 0 审计对象）
# ----------------------------------------------------------------------

# mutation → 只读 outcome 查询 RPC（_query_mutation_outcome 的 read_rpc 映射）
MUTATION_READ_RPC = {
    "task.create": "open",
    "task.claim": "in_progress",
    "task.report": "review",
    "task.apply": "applied",
    "task.close": "closed",
    "task.reopen": "in_progress",
}


def _stub_client():
    """构造不带 __init__ 的 UnixDaemonRpcClient 空实例（测试替身，禁真实 DB/网络）。

    mutation_call / _query_mutation_outcome 属于 UnixDaemonRpcClient（daemon_client.py
    L370 起），DaemonClient 是上层封装，测试直接针对实现层。
    """
    client = UnixDaemonRpcClient.__new__(UnixDaemonRpcClient)
    client.verify_authority = lambda *a, **kw: None
    return client


class TestMutationRecoverySemantics:
    """mutation_call 的幂等 + 断线 recovery + 业务错误透传（plan B5 recovery 语义）。"""

    def test_auto_generates_request_id(self):
        """无 request_id 时自动生成（req- 前缀），保证重试幂等。"""
        client = _stub_client()
        captured = {}

        def fake_call(method, params=None):
            captured["params"] = params
            return {"ok": True}

        client.call = fake_call
        result = client.mutation_call("task.claim", {"task_id": "T-X"})
        assert result == {"ok": True}
        rid = captured["params"].get("request_id")
        assert isinstance(rid, str) and rid.startswith("req-"), (
            f"mutation_call 应自动生成 req- 前缀 request_id，实际 {rid!r}"
        )

    def test_business_error_passthrough_no_retry(self):
        """DaemonRemoteError 业务错误原样透传，不重试不包装。"""
        client = _stub_client()
        calls = []

        def fake_call(method, params=None):
            calls.append(method)
            raise DaemonRemoteError("E_BUSY", "业务冲突")

        client.call = fake_call
        with pytest.raises(DaemonRemoteError) as exc:
            client.mutation_call("task.claim", {"task_id": "T-X"})
        assert exc.value.code == "E_BUSY"
        assert len(calls) == 1, "业务错误不得重试"

    def test_task_create_conn_fail_closed(self):
        """task.create 断线必须 fail-closed，不查询 outcome 不重放（防重复任务）。"""
        client = _stub_client()

        def fake_call(method, params=None):
            raise DaemonUnavailableError("daemon down")

        queried = []

        def fake_query(*a, **kw):
            queried.append(a)
            return None

        client.call = fake_call
        client._query_mutation_outcome = fake_query
        with pytest.raises(DaemonUnavailableError):
            client.mutation_call("task.create", {"title": "t"})
        assert queried == [], "task.create 断线不得调用 _query_mutation_outcome 重放"

    def test_other_mutation_queries_outcome_before_replay(self):
        """非 task.create 断线：先查提交结果（read RPC），已提交则直接返回不重放。"""
        client = _stub_client()

        def fake_call(method, params=None):
            raise DaemonUnavailableError("daemon down")

        committed = {
            "committed": True,
            "request_id": "req-abc",
            "outcome": {"status": "in_progress"},
        }
        client.call = fake_call
        client._query_mutation_outcome = lambda *a, **kw: committed
        result = client.mutation_call("task.claim", {"task_id": "T-X"})
        assert result is committed, "断线 recovery 应返回 outcome 查询结果"

    def test_unavailable_after_reconnect_attempts(self):
        """重连耗尽仍不可达 → DaemonUnavailableError（含 request_id，可追踪）。"""
        client = _stub_client()

        def fake_call(method, params=None):
            raise DaemonUnavailableError("still down")

        client.call = fake_call
        client._query_mutation_outcome = lambda *a, **kw: None
        with pytest.raises(DaemonUnavailableError) as exc:
            client.mutation_call("task.claim", {"task_id": "T-X"})
        assert "request_id" in str(exc.value), "错误信息应包含 request_id 便于追踪"

    def test_business_error_is_not_connection_error(self):
        """业务错误与连接错误是不同异常类型（不被包装）。"""
        assert not issubclass(DaemonRemoteError, DaemonUnavailableError), (
            "DaemonRemoteError 不得继承 DaemonUnavailableError（业务错误与连接错误需区分）"
        )


class TestOutcomeReadRpcMapping:
    """_query_mutation_outcome 的 read_rpc 映射存在，outcome 确认依赖状态+事件。"""

    def test_read_rpc_mapping_present(self):
        src = _read_rel("server/daemon_client.py")
        for method in MUTATION_READ_RPC:
            assert re.search(r'"%s": \("task\.status", "task_id"\)' % re.escape(method), src), (
                f"_query_mutation_outcome 缺少 {method} → task.status 映射"
            )

    def test_outcome_matches_expected_status(self):
        """_mutation_outcome_matches 的期望状态表存在（状态+事件双重确认）。"""
        src = _read_rel("server/daemon_client.py")
        for method, expected in MUTATION_READ_RPC.items():
            assert re.search(r'"%s": "%s"' % (re.escape(method), re.escape(expected)), src), (
                f"_mutation_outcome_matches 缺少 {method} → {expected} 状态确认"
            )
        # 事件 reason_code 确认（防止仅同名任务存在被误认为已提交）
        assert "reason_code" in src, "_mutation_outcome_matches 必须核对 task_event reason_code"


class TestCallWithAutostart:
    """call_with_autostart：连接失败有界唤起 daemon，不提供本地降级。"""

    def test_no_local_fallback(self):
        src = _read_rel("server/daemon_client.py")
        assert "不提供本地降级" in src, "call_with_autostart 必须声明不提供本地降级"
        assert "DaemonMutex" in src, "call_with_autostart 必须使用 DaemonMutex 有界唤起"
        assert "ensure_daemon" in src, "call_with_autostart 必须调用 ensure_daemon"
