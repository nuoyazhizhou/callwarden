"""Live Multi-Process Windows Named Pipe High-Concurrency Writer E2E Suite.

复审整改要求（P1-1 / P1-2 / P2-1 / P2-2）：
- P1-1：并发写入必须来自真实入口——真实 `python cw.py task` CLI 子进程 + 真实 MCP 工具调用，
        不再用 Python 临时脚本直接调 UnixDaemonRpcClient 充当并发源。
- P1-2：禁止全局 `Stop-Process -Name 'cw-daemon','cw'`；只管理本测试自身创建的 PID；
        管道被其他进程占用时跳过（不杀他人进程）。
- P2-1：task_events 校验无间隙序列 seqs[i+1] == seqs[i] + 1。
- P2-2：fresh-binary 构建门禁（测试前 cargo build --bin cw-daemon）。

Verification requirements:
1. 真实入口并发：5 个真实 `python cw.py task next` 子进程 + 3 个真实 MCP `task_next_step` 并发抢占同一任务。
2. 100% 隔离临时数据库（tempfile.mkdtemp），零污染 ~/.callwarden/callwarden.db。
3. 断言 0 个 `database is locked` 错误。
4. 断言恰好 1 个 claim 胜者（task_events 中 claimed 事件恰好 1 条）。
5. 断言真实 DB 变更（MCP task_create / CLI task create / MCP record_task_symbol_change / task_report）。
6. 断言单调且无间隙的 monotonic_seq。
7. 断言 daemon 重启后状态持久化，且只管理本测试 PID 的精确清理。
"""

import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import pytest

from callwarden.config import get_daemon_mode
from callwarden.server.daemon_client import UnixDaemonRpcClient, DaemonUnavailableError, route_task_write, route_task_read
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.daemon_autostart import get_default_endpoint


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CW_PY = os.path.join(_REPO_ROOT, "cw.py")
_DAEMON_BIN_NAME = "cw-daemon.exe"
_TASK_ID_RE = re.compile(r"T-[0-9A-Za-z-]+")


def _find_bin(bin_name: str) -> str:
    paths = [
        os.path.join(_REPO_ROOT, "target", "debug", bin_name),
        os.path.join(_REPO_ROOT, "rust_ext", "target", "debug", bin_name),
        os.path.join(_REPO_ROOT, ".venv_test", "Scripts", bin_name),
    ]
    for p in paths:
        if os.path.exists(p):
            return p
    return paths[0]


def _require_fresh_binary() -> bool:
    """发布/企业验收 CI 是否强制 fresh-binary 构建（CW_REQUIRE_FRESH_BINARY=1）。

    普通本地测试不设置该变量，cargo 不可用时允许 skip；
    发布验收 CI 设置该变量后，cargo 不存在必须 fail（fail-closed），
    保证不会出现"E2E 被 skip 但流水线仍显示成功"的假通过。
    """
    return os.environ.get("CW_REQUIRE_FRESH_BINARY", "").strip() == "1"


def _build_daemon_fresh():
    """P2-2：fresh-binary 构建门禁。cargo 不可用时返回 None（由调用方决定 skip/fail）。"""
    cargo = shutil.which("cargo")
    if not cargo:
        return None
    try:
        return subprocess.run(
            [cargo, "build", "--manifest-path",
             os.path.join(_REPO_ROOT, "rust_ext", "Cargo.toml"), "--bin", "cw-daemon"],
            cwd=_REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=900,
        )
    except Exception as exc:
        return exc


def _spawn_cw_cli(args, env, cwd=None, timeout=60):
    """启动真实 `python cw.py ...` CLI 子进程（P1-1：真实 CLI 入口）。"""
    return subprocess.Popen(
        [sys.executable, _CW_PY] + args,
        env=env, cwd=cwd or _REPO_ROOT,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )


def _mcp_tool_text(result):
    """从 fastmcp call_tool 结果中提取文本内容。

    FastMCP 1.x 在不同版本/调用方式下可能返回：
    - list[TextContent]（每个元素带 .text）
    - tuple ([TextContent], {'result': ...})（本环境实测形态）
    - CallToolResult(.content)（老版本形态）
    统一归一化为文本拼接。
    """
    if isinstance(result, tuple):
        result = result[0] if result else []
    if isinstance(result, list):
        items = result
    else:
        items = getattr(result, "content", []) or []
    parts = []
    for block in items:
        txt = getattr(block, "text", None)
        if txt:
            parts.append(str(txt))
        elif isinstance(block, str):
            parts.append(block)
    return "\n".join(parts)


def _run_mcp_call(mcp, tool, args):
    """进程内真实 MCP 工具调用（P1-1：真实 MCP 入口）。返回 (raw, text)。"""
    result = asyncio.run(mcp.call_tool(tool, args))
    return result, _mcp_tool_text(result)


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Named Pipe Concurrent Writer E2E")
def test_windows_daemon_multi_process_high_concurrency_writer_e2e():
    """真实 cw CLI 子进程 + 真实 MCP 工具并发写入、真实 DB 变更与重启持久化 E2E"""
    # ---- P2-2：fresh-binary 构建门禁（fail-closed）----
    # 普通本地测试：cargo 不可用（未安装）才 skip，不影响开发机；
    # 发布/企业验收 CI（CW_REQUIRE_FRESH_BINARY=1）：cargo 不存在必须 fail，
    # 禁止"E2E 被 skip 但流水线仍显示成功"。构建异常或构建失败同样必须 fail。
    build_res = _build_daemon_fresh()
    if build_res is None:
        if _require_fresh_binary():
            pytest.fail(
                "发布/企业验收 CI 要求 fresh-binary 构建，但 cargo 不存在"
                "（CW_REQUIRE_FRESH_BINARY=1 门禁 fail-closed，禁止 skip）")
        pytest.skip("cargo 不可用，无法执行 fresh-binary 构建门禁（P2-2）")
    if isinstance(build_res, Exception):
        pytest.fail(f"cargo build 异常（P2-2 门禁 fail-closed）: {build_res}")
    if build_res.returncode != 0:
        pytest.fail(f"cargo build --bin cw-daemon 失败（P2-2 门禁 fail-closed）:\n{build_res.stderr[-2000:]}")

    daemon_bin = _find_bin(_DAEMON_BIN_NAME)
    if not os.path.exists(daemon_bin):
        # 构建已成功但产物缺失属于门禁失效（fresh-binary 未生成），必须 fail 而非 skip
        pytest.fail(f"cargo build 成功但 cw-daemon.exe 产物缺失（P2-2 门禁 fail-closed）: {daemon_bin}")

    tmp_dir = tempfile.mkdtemp(prefix="cw_e2e_isolated_")
    task_db_path = os.path.join(tmp_dir, "isolated_callwarden.db")
    pipe_name = get_default_endpoint()

    config = {
        "socket_path": pipe_name,
        "registry_db_path": os.path.join(tmp_dir, "registry.db"),
        "task_db_path": task_db_path,
        "data_root": tmp_dir,
        "max_workers": 8,
        "request_timeout_secs": 30,
        "snapshot_cache_capacity": 4,
        "codegraph_db_path_template": os.path.join(tmp_dir, "codegraph.db"),
        "socket_mode": 432,
        "socket_group": "",
        "stage_toggle_db_path": os.path.join(tmp_dir, "stage_toggle.db"),
    }
    config_path = os.path.join(tmp_dir, "daemon_config.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f)

    # ---- P1-2：不全局杀进程。先探测管道是否已被其他进程占用，占用则跳过。----
    probe = UnixDaemonRpcClient(socket_path=pipe_name)
    try:
        probe.probe()
        pytest.skip(f"管道 {pipe_name} 已被其他 daemon 进程占用，请手动清理后重跑（P1-2 不杀他人进程）")
    except Exception:
        pass

    # ---- 本测试自建进程清单（P1-2：只管理这些 PID）----
    owned_procs = []

    def _terminate_owned():
        for p in owned_procs:
            if p is not None and p.poll() is None:
                try:
                    p.terminate()
                    p.wait(timeout=8)
                except Exception:
                    try:
                        p.kill()
                    except Exception:
                        pass

    daemon_log = open(os.path.join(tmp_dir, "daemon.log"), "w", encoding="utf-8")
    daemon_proc = None
    daemon_proc2 = None
    daemon_log2 = None

    # ---- 保存/恢复测试进程自身的 daemon 环境变量 ----
    old_mode = os.environ.get("CW_DAEMON_MODE")
    old_endpoint = os.environ.get("CW_DAEMON_ENDPOINT")
    try:
        os.environ["CW_DAEMON_MODE"] = "enterprise"
        os.environ["CW_DAEMON_ENDPOINT"] = pipe_name
        env = os.environ.copy()

        # 1. 启动隔离的 cw-daemon.exe（记录 PID）
        daemon_proc = subprocess.Popen(
            [daemon_bin, "--config", config_path],
            stdout=daemon_log, stderr=subprocess.STDOUT,
        )
        owned_procs.append(daemon_proc)

        client = UnixDaemonRpcClient(socket_path=pipe_name)
        connected = False
        for _ in range(20):
            try:
                client.probe()
                connected = True
                break
            except Exception:
                time.sleep(0.5)
        if not connected:
            pytest.fail(f"Daemon 无法连通 {pipe_name}")

        # 2. 真实 MCP 工具创建主 Task（P1-1）
        from callwarden.server.mcp_server import create_mcp_server
        mcp = create_mcp_server()

        create_res, create_text = _run_mcp_call(mcp, "task_create", {
            "title": "Multi-Process E2E Task",
            "description": "High-concurrency real CLI + MCP write serialization test",
            "steps": [{"action": "edit", "target_file": "core.py", "target_symbol": "main"}],
        })
        task_id = create_text.strip() if create_text.strip() else None
        if not task_id or not _TASK_ID_RE.fullmatch(task_id):
            pytest.fail(f"MCP task_create 未返回合法 task_id: {create_text}")
        assert client.call("task.status", {"task_id": task_id}).get("status") == "open"

        # 3. 真实 CLI 子进程创建第二个 Task（P1-1：CLI 写路径经 daemon）
        cli_create = _spawn_cw_cli(
            ["task", "create", "--title", "CLI-Created E2E Task",
             "--desc", "created via real python cw.py", "--steps", json.dumps(
                 [{"action": "edit", "target_file": "cli.py", "target_symbol": "cli_main"}])],
            env=env,
        )
        owned_procs.append(cli_create)
        out, err = cli_create.communicate(timeout=60)
        cli_task_id = _TASK_ID_RE.search(out or "")
        assert cli_task_id, f"CLI task create 未输出 task_id, out={out[:500]} err={err[:500]}"
        cli_task_id = cli_task_id.group(0)
        assert client.call("task.status", {"task_id": cli_task_id}).get("status") == "open"

        # 4. 并发抢占：5 个真实 CLI 子进程 + 3 个真实 MCP 工具调用（共 8 个真实入口，
        #    各自携带唯一 agent_session_id，模拟"不同逻辑 Agent"并发抢同一任务）
        cli_workers = []
        for i in range(5):
            worker_env = env.copy()
            worker_env["CW_AGENT_SESSION_ID"] = f"cli-agent-{i}"
            p = _spawn_cw_cli(["task", "next", task_id], env=worker_env)
            cli_workers.append(p)
            owned_procs.append(p)

        # MCP 并发（同一事件循环内 gather，真实工具函数并发执行）
        mcp_results = []
        async def _mcp_race():
            async def _one(sid):
                try:
                    res = await mcp.call_tool("task_next_step", {"task_id": task_id, "agent_session_id": sid})
                    return ("ok", _mcp_tool_text(res))
                except Exception as exc:
                    return ("err", str(exc))
            return await asyncio.gather(*[_one(f"mcp-agent-{i}") for i in range(3)])

        mcp_results = asyncio.run(_mcp_race())

        cli_results = []
        for p in cli_workers:
            out_p, err_p = p.communicate(timeout=60)
            cli_results.append((out_p or "", err_p or ""))

        # 5. 核心断言
        # A. 0 个 'database is locked'
        for out_p, err_p in cli_results:
            assert "database is locked" not in (out_p + err_p), f"CLI 子进程出现数据库锁冲突"
        for status, msg in mcp_results:
            assert "database is locked" not in msg, f"MCP 调用出现数据库锁冲突: {msg}"

        # B. 恰好 1 个 claim 胜者：task_events 中 claimed 事件恰好 1 条
        events_res = client.task_events(task_id)
        events = events_res["events"]
        claimed = [e for e in events if e.get("reason_code") == "claimed" and e.get("to_status") == "in_progress"]
        assert len(claimed) == 1, f"期望恰好 1 个 claim 胜者，实际 {len(claimed)} 条: {claimed}"
        winner_session = claimed[0].get("agent_session_id") or claimed[0].get("actor_identity")

        # B2. 败者必须真实到达 daemon 并收到 task_conflict
        #     （防止"CLI/MCP 提前失败"被误判为并发验证通过——8 个并发源中恰好 1 个胜者，
        #      剩余 7 个败者都应携带 daemon 的 task_conflict 判定）
        #     同时校验错误语义：败者消息必须保留结构化 task_conflict，
        #     不得出现 "连接失败/无法连接"（DaemonUnavailableError 伪装 = route_task_write 吞错 bug）
        _conflict_markers = ("task_conflict", "抢占", "conflict")
        _unavailable_markers = ("连接失败", "无法连接", "endpoint 不可连接")
        cli_conflict_cnt = 0
        for out_p, err_p in cli_results:
            loser_text = out_p + err_p
            if not any(m in loser_text for m in _conflict_markers):
                continue
            cli_conflict_cnt += 1
            assert not any(m in loser_text for m in _unavailable_markers), (
                f"CLI 败者错误语义被伪装成 daemon 不可用（应保留结构化 task_conflict）: {loser_text[:500]}")
        assert cli_conflict_cnt >= 4, (
            f"CLI 并发败者应至少 4 个收到 task_conflict，实际 {cli_conflict_cnt}:\n{cli_results}")
        mcp_conflict_cnt = 0
        for status, msg in mcp_results:
            if not any(m in msg for m in _conflict_markers):
                continue
            mcp_conflict_cnt += 1
            assert not any(m in msg for m in _unavailable_markers), (
                f"MCP 败者错误语义被伪装成 daemon 不可用（应保留结构化 task_conflict）: {msg[:500]}")
        assert mcp_conflict_cnt >= 2, (
            f"MCP 并发败者应至少 2 个收到 task_conflict，实际 {mcp_conflict_cnt}:\n{mcp_results}")
        assert cli_conflict_cnt + mcp_conflict_cnt == 7, (
            f"8 个并发源应恰好 7 个败者（1 个胜者），实际 CLI 败者 {cli_conflict_cnt} + "
            f"MCP 败者 {mcp_conflict_cnt} = {cli_conflict_cnt + mcp_conflict_cnt}")

        # C. 任务最终状态为 in_progress（已被唯一胜者认领）
        status_res = client.call("task.status", {"task_id": task_id})
        assert status_res.get("status") == "in_progress", f"任务状态异常: {status_res}"

        # D. 真实 MCP 写入 symbol change 并验证
        rec_res, rec_text = _run_mcp_call(mcp, "record_task_symbol_change", {
            "task_id": task_id, "file_path": "core.py",
            "symbol_name": "main", "qualified_name": "core.main",
            "symbol_hash_before": "abc", "symbol_hash_after": "xyz",
            "change_type": "modified", "source": "manual",
        })
        assert "recorded" in rec_text or "task_id" in rec_text, f"record_task_symbol_change 失败: {rec_text}"

        changes_res = client.call("task.get_symbol_changes", {"task_id": task_id})
        assert len(changes_res.get("changes", [])) == 1, f"symbol changes 数量异常: {changes_res}"
        assert changes_res["changes"][0]["file_path"] == "core.py"

        # E. report（写路径，经 daemon）
        report_res = client.task_report(task_id, summary="Completed via multi-process E2E", agent_session_id=winner_session)
        assert report_res["status"] == "review"

        # F. P2-1：无间隙序列 —— daemon 的 monotonic_seq 是全局计数器，
        #    直接读取隔离任务库校验"全部事件 seq 单调、无重复、无间隙"。
        events2 = client.task_events(task_id)["events"]
        import sqlite3 as _sqlite3
        _f_conn = _sqlite3.connect(task_db_path, timeout=10)
        try:
            _all_seqs = [r[0] for r in _f_conn.execute(
                "SELECT monotonic_seq FROM task_events ORDER BY monotonic_seq")]
        finally:
            _f_conn.close()
        assert len(_all_seqs) >= 3, f"task_events 事件数不足: {_all_seqs}"
        assert _all_seqs == sorted(_all_seqs), f"monotonic_seq 不得倒退: {_all_seqs}"
        assert len(_all_seqs) == len(set(_all_seqs)), f"monotonic_seq 不得重复: {_all_seqs}"
        for i in range(len(_all_seqs) - 1):
            assert _all_seqs[i + 1] == _all_seqs[i] + 1, f"monotonic_seq 存在间隙: {_all_seqs}"
        # 主任务自身的 events 必须严格递增（全局计数器可能被其他任务事件交错，故只查递增）
        seqs_a = [e["monotonic_seq"] for e in events2]
        assert seqs_a == sorted(seqs_a) and len(seqs_a) == len(set(seqs_a)), f"主任务 seq 异常: {seqs_a}"

        # G. 真实 CLI 读路径验证（enterprise 模式经 daemon task.list）
        cli_list = _spawn_cw_cli(["task", "list"], env=env)
        owned_procs.append(cli_list)
        out_l, err_l = cli_list.communicate(timeout=60)
        assert task_id in (out_l or ""), f"CLI task list 未包含主任务, out={out_l[:500]}"

        # 6. Daemon 重启状态持久化（只管理本测试 PID）
        daemon_proc.terminate()
        daemon_proc.wait(timeout=8)
        daemon_log.close()
        daemon_log = None

        daemon_log2 = open(os.path.join(tmp_dir, "daemon2.log"), "w", encoding="utf-8")
        daemon_proc2 = subprocess.Popen(
            [daemon_bin, "--config", config_path],
            stdout=daemon_log2, stderr=subprocess.STDOUT,
        )
        owned_procs.append(daemon_proc2)
        try:
            client2 = UnixDaemonRpcClient(socket_path=pipe_name)
            connected2 = False
            for _ in range(20):
                try:
                    client2.probe()
                    connected2 = True
                    break
                except Exception:
                    time.sleep(0.5)
            assert connected2, "重启后 daemon 无法连通"
            restarted_events = client2.task_events(task_id)
            assert len(restarted_events["events"]) == len(events2), "重启后 task_events 持久化数据必须完备"
        finally:
            if daemon_proc2.poll() is None:
                daemon_proc2.terminate()
                daemon_proc2.wait(timeout=8)
            daemon_log2.close()

    finally:
        os.environ.pop("CW_DAEMON_MODE", None) if old_mode is None else os.environ.__setitem__("CW_DAEMON_MODE", old_mode)
        if old_mode is None:
            os.environ.pop("CW_DAEMON_MODE", None)
        else:
            os.environ["CW_DAEMON_MODE"] = old_mode
        if old_endpoint is None:
            os.environ.pop("CW_DAEMON_ENDPOINT", None)
        else:
            os.environ["CW_DAEMON_ENDPOINT"] = old_endpoint

        _terminate_owned()
        if daemon_log is not None:
            daemon_log.close()
        shutil.rmtree(tmp_dir, ignore_errors=True)


def test_auto_enterprise_fail_closed_without_daemon():
    """验证 enterprise/auto 模式下 daemon 不可用时写操作严格 Fail-Closed"""
    fake_endpoint = "\\\\.\\pipe\\nonexistent-pipe-8888"

    called_local = False
    def mock_local():
        nonlocal called_local
        called_local = True
        return "local_written"

    old_mode = os.environ.get("CW_DAEMON_MODE")
    old_endpoint = os.environ.get("CW_DAEMON_ENDPOINT")
    try:
        os.environ["CW_DAEMON_ENDPOINT"] = fake_endpoint

        os.environ["CW_DAEMON_MODE"] = "enterprise"
        with pytest.raises(DaemonUnavailableError):
            route_task_write("task.create", {"title": "fail_closed_test"}, mock_local)
        assert not called_local, "enterprise 模式绝对不得调用 local 写入闭包"

        os.environ["CW_DAEMON_MODE"] = "auto"
        with pytest.raises(DaemonUnavailableError):
            route_task_write("task.create", {"title": "fail_closed_test"}, mock_local)
        assert not called_local, "auto 模式在指定写操作时底层异常绝不得 fallback 本地"

        os.environ["CW_DAEMON_MODE"] = "local"
        res = route_task_write("task.create", {"title": "local_test"}, mock_local)
        assert res == "local_written"
        assert called_local, "local 模式必须使用 local 闭包"

    finally:
        if old_mode is not None:
            os.environ["CW_DAEMON_MODE"] = old_mode
        else:
            os.environ.pop("CW_DAEMON_MODE", None)
        if old_endpoint is not None:
            os.environ["CW_DAEMON_ENDPOINT"] = old_endpoint
        else:
            os.environ.pop("CW_DAEMON_ENDPOINT", None)


def test_route_task_preserves_daemon_remote_error_code():
    """独立单测：route_task_write/read 必须保留 DaemonRemoteError 的结构化错误语义
    （异常类型 + code 原样透传），不得包装成 DaemonUnavailableError；
    连接层异常（OSError）在 enterprise/auto 下仍应包装为 DaemonUnavailableError。"""
    import callwarden.server.daemon_client as _dc_mod
    _orig_client = _dc_mod.UnixDaemonRpcClient

    class _FakeRemoteErrorClient:
        """模拟 daemon 已连接但返回业务错误（parse_response 抛 DaemonRemoteError）"""
        def __init__(self, code, message):
            self._code, self._message = code, message
        def call(self, rpc_method, params):
            raise DaemonRemoteError(self._code, self._message)

    class _FakeConnErrorClient:
        """模拟连接层故障（pipe 不存在 / 传输异常）"""
        def call(self, rpc_method, params):
            raise OSError(2, "No such file or directory")

    old_mode = os.environ.get("CW_DAEMON_MODE")
    try:
        # ---- 写路径：业务错误原样透传，绝不调用 local 闭包，绝不伪装成连接失败 ----
        os.environ["CW_DAEMON_MODE"] = "enterprise"
        _dc_mod.UnixDaemonRpcClient = lambda *a, **k: _FakeRemoteErrorClient("task_conflict", "任务已被其他 agent 抢占")
        try:
            with pytest.raises(DaemonRemoteError) as ei:
                route_task_write("task.claim", {"task_id": "T-1"}, lambda: None)
            assert ei.value.code == "task_conflict", f"写路径 code 丢失: {ei.value.code}"
        finally:
            _dc_mod.UnixDaemonRpcClient = _orig_client

        # ---- 读路径（auto）：业务错误必须透传，不得降级为本地读（数据可能不一致）----
        os.environ["CW_DAEMON_MODE"] = "auto"
        _dc_mod.UnixDaemonRpcClient = lambda *a, **k: _FakeRemoteErrorClient("task_not_found", "任务不存在")
        try:
            with pytest.raises(DaemonRemoteError) as ei:
                route_task_read("task.status", {"task_id": "T-999"}, lambda: "LOCAL_READ")
            assert ei.value.code == "task_not_found", f"读路径 code 丢失: {ei.value.code}"
        finally:
            _dc_mod.UnixDaemonRpcClient = _orig_client

        # ---- 连接层异常仍必须包装为 DaemonUnavailableError（Fail-Closed 回归）----
        os.environ["CW_DAEMON_MODE"] = "auto"
        _dc_mod.UnixDaemonRpcClient = lambda *a, **k: _FakeConnErrorClient()
        try:
            with pytest.raises(DaemonUnavailableError):
                route_task_write("task.create", {"title": "x"}, lambda: "LOCAL")
        finally:
            _dc_mod.UnixDaemonRpcClient = _orig_client

    finally:
        _dc_mod.UnixDaemonRpcClient = _orig_client
        if old_mode is not None:
            os.environ["CW_DAEMON_MODE"] = old_mode
        else:
            os.environ.pop("CW_DAEMON_MODE", None)
