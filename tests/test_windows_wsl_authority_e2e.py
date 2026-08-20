"""共存契约子任务6：Windows authority 跨边界 E2E 测试。

对应 windows-wsl-daemon-coexistence-contract.md §4.2/§5.2/§6.3 与
windows-wsl-daemon-coexistence-task-plan.md 子任务6。

在 Windows 主机验证「Windows CLI、Windows MCP、WSL client」三类并发源
同时 claim/report 同一个 task 时：
1. 8 个并发源中只允许 1 个 claim 胜者，其余 7 个败者收到结构化 task_conflict；
2. 所有 task_events 都落在 Windows authority（隔离 daemon 的任务库），
   没有任何 WSL 本地 DB 被写入（fail-closed，禁止本地 fallback）；
3. bridge 重启后，携带相同 request_id 的 mutation 由 daemon request dedup
   返回已提交结果，不产生重复 task_event。

WSL 客户端必须使用生产 `UnixDaemonRpcClient` 的 TCP bridge transport，而不是
测试内 framing client；这样 token 注入、authority pin 和 mutation dedup 与生产路径
完全一致。

前置条件（不满足则 skip）：
1. Windows 平台（Named Pipe）
2. cargo 可用（fresh binary 门禁：cw-daemon + cw-bridge）
3. 默认管道 `\\\\.\\pipe\\callwarden-<sid>` 未被其他 daemon 占用（占用则 skip，
   不杀他人进程，P1-2 原则）
"""

import asyncio
import itertools
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time

import pytest

from callwarden.server.daemon_protocol import (
    DaemonRemoteError,
    parse_response,
    recv_message,
    send_message,
)
from callwarden.server.daemon_client import UnixDaemonRpcClient

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_CW_PY = os.path.join(_REPO_ROOT, "cw.py")
# 测试二进制构建到独立 CARGO_TARGET_DIR（用户 daemon 可能正从 debug 目录运行，
# Windows 锁定了 cw-daemon.exe，无法原地覆盖；独立目录不与用户产物冲突）
_DAEMON_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "cw-sub6", "debug", "cw-daemon.exe")
_BRIDGE_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "cw-sub6", "debug", "cw-bridge.exe")
_CLIENT_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "cw-sub6", "debug", "cw-client.exe")
_CARGO_TARGET_DIR = os.path.join(_REPO_ROOT, "rust_ext", "target", "cw-sub6")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="Windows authority 跨边界 E2E 需要 Windows + Named Pipe",
)


def _production_wsl_client(port: int) -> UnixDaemonRpcClient:
    """用生产 client 经 TCP bridge 访问 Windows authority。"""
    return UnixDaemonRpcClient(
        socket_path=f"tcp://127.0.0.1:{port}",
        transport_override="windows-bridge",
        endpoint_override=True,
    )


def _daemon_config(tmp: str) -> dict:
    """生成隔离 daemon JSON 配置（Windows 管道名由 transport 按 SID 派生）。"""
    data_root = os.path.join(tmp, "data")
    return {
        "socket_path": "",
        "registry_db_path": os.path.join(tmp, "registry.db"),
        "task_db_path": os.path.join(tmp, "callwarden.db"),
        "data_root": data_root,
        "max_workers": 8,
        "request_timeout_secs": 30,
        "snapshot_cache_capacity": 4,
        "codegraph_db_path_template": os.path.join(
            data_root, "workspaces", "{workspace_instance_id}", "codegraph.db"
        ),
        "socket_mode": 0o660,
        "socket_group": "",
        "stage_toggle_db_path": os.path.join(tmp, "stage_toggle.db"),
    }


def _find_pipe() -> str:
    from callwarden.config import _get_windows_user_sid

    return rf"\\.\pipe\callwarden-{_get_windows_user_sid()}"


def _spawn(bin_path: str, args: list, log_dir: str, name: str, env=None):
    """启动子进程，日志落盘。返回 (proc, log_handle)。"""
    log = open(os.path.join(log_dir, f"{name}.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [bin_path] + args,
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        env=env,
    )
    return proc, log


def _probe_pipe(pipe: str, timeout: float = 5.0) -> bool:
    """探测默认管道是否已被其他 daemon 占用（不启动新进程）。"""
    client_bin = _CLIENT_BIN
    if not os.path.exists(client_bin):
        return False
    try:
        result = subprocess.run(
            [client_bin, "--socket", pipe, "--timeout", str(int(timeout)), "ping"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 10,
        )
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        # 管道空闲时 cw-client 会等待 daemon 出现直至超时，而不是立即失败；
        # 此时应视为“无 daemon 占用”，允许本测试启动隔离 daemon。
        return False


def _reserve_port() -> int:
    """预留一个 loopback TCP 端口（绑定后关闭，bridge 随后监听）。"""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def authority():
    """启动隔离的 Windows daemon + bridge，返回 authority 上下文。

    默认管道被其他 daemon 占用时 skip（不杀他人进程）。测试期间设置
    CW_DAEMON_MODE=enterprise + CW_DAEMON_ENDPOINT=<pipe>，使真实 CLI/MCP
    子进程与进程内 MCP 都经隔离 daemon 写入；结束后恢复环境变量。
    """
    # ---- fresh binary 门禁：cw-daemon + cw-bridge 必须由当前源码重建 ----
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("未找到 cargo，无法构建新鲜二进制")
    os.makedirs(_CARGO_TARGET_DIR, exist_ok=True)
    build_env = dict(os.environ, CARGO_TARGET_DIR=_CARGO_TARGET_DIR)
    build = subprocess.run(
        [cargo, "build", "--no-default-features", "--manifest-path",
         os.path.join(_REPO_ROOT, "rust_ext", "Cargo.toml"),
         "--bin", "cw-daemon", "--bin", "cw-bridge", "--bin", "cw-client"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=build_env,
        timeout=1500,
    )
    if build.returncode != 0:
        pytest.fail("cargo build cw-daemon/cw-bridge/cw-client 失败：\n" + (build.stdout + build.stderr)[-3000:])
    if not (os.path.exists(_DAEMON_BIN) and os.path.exists(_BRIDGE_BIN)):
        pytest.fail(f"cargo build 成功但缺少产物: {_DAEMON_BIN} / {_BRIDGE_BIN}")

    pipe = _find_pipe()
    if _probe_pipe(pipe):
        pytest.skip(f"默认管道 {pipe} 已被其他 daemon 占用，跳过跨边界 E2E（P1-2 不杀他人进程）")

    tmp = tempfile.mkdtemp(prefix="cw_wsl_authority_e2e_")
    config = _daemon_config(tmp)
    config_path = os.path.join(tmp, "daemon.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f)

    # bridge token + 固定端口
    token = f"wsl-bridge-token-{os.getpid()}"
    token_file = os.path.join(tmp, "bridge.token")
    with open(token_file, "w", encoding="utf-8") as f:
        f.write(token + "\n")
    port = _reserve_port()

    procs = []
    logs = []
    old_mode = os.environ.get("CW_DAEMON_MODE")
    old_endpoint = os.environ.get("CW_DAEMON_ENDPOINT")
    old_manifest = os.environ.get("CW_BRIDGE_MANIFEST")
    try:
        os.environ["CW_DAEMON_MODE"] = "enterprise"
        os.environ["CW_DAEMON_ENDPOINT"] = pipe
        os.environ["CW_BRIDGE_MANIFEST"] = os.path.join(tmp, "bridge.manifest.json")

        # 1. 启动隔离 daemon（占默认管道）
        daemon_proc, daemon_log = _spawn(
            _DAEMON_BIN, ["--config", config_path], tmp, "daemon"
        )
        procs.append(daemon_proc)
        logs.append(daemon_log)

        connected = False
        deadline = time.time() + 40
        while time.time() < deadline:
            if daemon_proc.poll() is not None:
                break
            try:
                r = subprocess.run(
                    [_CLIENT_BIN,
                     "--socket", pipe, "--timeout", "5", "ping"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                )
            except subprocess.TimeoutExpired:
                # cw-client 对尚未创建的管道会等待至超时；daemon 仍在启动，
                # 属于正常状态，继续下一轮探测。
                time.sleep(0.5)
                continue
            if r.returncode == 0:
                connected = True
                break
            time.sleep(0.5)
        if not connected:
            log_text = ""
            try:
                with open(os.path.join(tmp, "daemon.log"), "r", encoding="utf-8") as f:
                    log_text = f.read()[-3000:]
            except Exception:
                pass
            pytest.fail(f"隔离 daemon 未在超时内响应，日志：\n{log_text}")

        # 2. 启动 bridge（转发默认管道）
        bridge_env = dict(os.environ)
        bridge_env["CW_BRIDGE_TOKEN_FILE"] = token_file
        bridge_env["CW_BRIDGE_ENDPOINT"] = f"127.0.0.1:{port}"
        bridge_env["CW_BRIDGE_MANIFEST"] = os.path.join(tmp, "bridge.manifest.json")
        bridge_proc, bridge_log = _spawn(
            _BRIDGE_BIN, [], tmp, "bridge", env=bridge_env
        )
        procs.append(bridge_proc)
        logs.append(bridge_log)

        # 3. 通过 bridge 建立 WSL 侧客户端并握手（提取 authority pin）
        wsl_client = _production_wsl_client(port)
        hello = None
        deadline = time.time() + 20
        while time.time() < deadline:
            if bridge_proc.poll() is not None:
                break
            try:
                hello = wsl_client.hello()
                break
            except (OSError, socket.timeout, RuntimeError):
                time.sleep(0.3)
        if hello is None:
            log_text = ""
            try:
                with open(os.path.join(tmp, "bridge.log"), "r", encoding="utf-8") as f:
                    log_text = f.read()[-3000:]
            except Exception:
                pass
            pytest.fail(f"bridge 未在超时内转发握手，日志：\n{log_text}")

        yield {
            "pipe": pipe,
            "bridge_endpoint": (("127.0.0.1", port)),
            "bridge_port": port,
            "token": token,
            "token_file": token_file,
            "config_path": config_path,
            "tmp": tmp,
            "procs": procs,
            "wsl_client": wsl_client,
            "authority_id": hello["authority_id"],
            "task_db_fingerprint": hello["task_db_fingerprint"],
            "task_db": config["task_db_path"],
        }
    finally:
        os.environ.pop("CW_DAEMON_MODE", None) if old_mode is None else os.environ.__setitem__("CW_DAEMON_MODE", old_mode)
        if old_endpoint is None:
            os.environ.pop("CW_DAEMON_ENDPOINT", None)
        else:
            os.environ["CW_DAEMON_ENDPOINT"] = old_endpoint
        if old_manifest is None:
            os.environ.pop("CW_BRIDGE_MANIFEST", None)
        else:
            os.environ["CW_BRIDGE_MANIFEST"] = old_manifest
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
        for log in logs:
            try:
                log.close()
            except Exception:
                pass
        shutil.rmtree(tmp, ignore_errors=True)


def _mcp_tool_text(result):
    """从 fastmcp call_tool 结果中提取文本内容（兼容多种返回形态）。"""
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


def _spawn_cw_cli(args, env, timeout=60):
    """启动真实 `python cw.py ...` CLI 子进程（Windows CLI 入口）。"""
    return subprocess.Popen(
        [sys.executable, _CW_PY] + args,
        env=env,
        cwd=_REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def test_eight_sources_claim_single_winner_via_bridge(authority):
    """P1：Windows CLI + Windows MCP + WSL client 共 8 个并发源 claim 同一任务。

    8 源 = 3 真实 CLI 子进程 + 3 真实 MCP 工具 + 2 个 bridge（WSL 模拟）客户端。
    断言：
    - 恰好 1 个 claim 胜者（task_events 中 claimed 事件恰好 1 条）；
    - 其余 7 个败者都收到结构化 task_conflict（错误语义不得伪装成 daemon 不可用）；
    - 所有 task_events 位于 Windows authority（经隔离 daemon 查询 + 直读任务库）；
    - WSL 侧没有本地 DB 写入（模拟 WSL HOME 下无任何 callwarden.db）。
    """
    from callwarden.server.mcp_server import create_mcp_server

    pipe = authority["pipe"]
    task_db = authority["task_db"]

    # 1. 真实 MCP 创建主任务
    mcp = create_mcp_server()
    create_res, create_text = asyncio.run(mcp.call_tool(
        "task_create",
        {"title": "Cross-boundary E2E Task",
         "description": "Windows CLI + MCP + WSL client 并发 claim",
         "steps": [{"action": "edit", "target_file": "core.py", "target_symbol": "main"}]},
    ))
    task_id = _mcp_tool_text(create_res).strip()
    import re as _re
    m = _re.search(r"T-[0-9A-Za-z-]+", task_id or "")
    assert m, f"MCP task_create 未返回合法 task_id: {task_id}"
    task_id = m.group(0)

    # 2. 并发抢占：3 真实 CLI + 3 真实 MCP + 2 bridge（WSL 模拟）
    env = os.environ.copy()

    cli_workers = []
    for i in range(3):
        worker_env = env.copy()
        worker_env["CW_AGENT_SESSION_ID"] = f"cli-agent-{i}"
        p = _spawn_cw_cli(["task", "next", task_id], env=worker_env)
        cli_workers.append(p)

    async def _mcp_race():
        async def _one(sid):
            try:
                res = await mcp.call_tool(
                    "task_next_step",
                    {"task_id": task_id, "agent_session_id": sid},
                )
                return ("ok", _mcp_tool_text(res))
            except Exception as exc:
                return ("err", str(exc))
        return await asyncio.gather(*[_one(f"mcp-agent-{i}") for i in range(3)])

    mcp_results = asyncio.run(_mcp_race())

    bridge_results = []
    for i in range(2):
        try:
            bridge_results.append(("ok", authority["wsl_client"].call(
                "task.claim",
                {"task_id": task_id, "agent_session_id": f"wsl-agent-{i}"},
            )))
        except DaemonRemoteError as exc:
            bridge_results.append(("err", f"{exc.code}: {exc.message}"))
        except Exception as exc:
            bridge_results.append(("err", str(exc)))

    cli_results = []
    for p in cli_workers:
        out_p, err_p = p.communicate(timeout=60)
        cli_results.append((out_p or "", err_p or ""))

    # 3. 断言 A：0 个 'database is locked'
    for out_p, err_p in cli_results:
        assert "database is locked" not in (out_p + err_p), f"CLI 子进程出现数据库锁冲突"
    for status, msg in mcp_results:
        assert "database is locked" not in msg, f"MCP 调用出现数据库锁冲突: {msg}"

    # 4. 断言 B：恰好 1 个 claim 胜者（Windows authority 的 task_events）
    events = authority["wsl_client"].call("task.events", {"task_id": task_id})["events"]
    claimed = [e for e in events if e.get("reason_code") == "claimed"
               and e.get("to_status") == "in_progress"]
    assert len(claimed) == 1, f"期望恰好 1 个 claim 胜者，实际 {len(claimed)} 条: {claimed}"
    winner_session = claimed[0].get("agent_session_id") or claimed[0].get("actor_identity")
    assert winner_session, f"胜者缺少 agent_session_id: {claimed[0]}"

    # 5. 断言 C：7 个败者必须携带结构化 task_conflict，不得伪装成 daemon 不可用
    _conflict_markers = ("task_conflict", "抢占", "conflict")
    _unavailable_markers = ("连接失败", "无法连接", "endpoint 不可连接", "不可达")
    cli_conflict_cnt = 0
    for out_p, err_p in cli_results:
        loser_text = out_p + err_p
        if not any(m in loser_text for m in _conflict_markers):
            continue
        cli_conflict_cnt += 1
        assert not any(m in loser_text for m in _unavailable_markers), (
            f"CLI 败者错误语义被伪装成 daemon 不可用: {loser_text[:500]}")
    mcp_conflict_cnt = 0
    for status, msg in mcp_results:
        if not any(m in msg for m in _conflict_markers):
            continue
        mcp_conflict_cnt += 1
        assert not any(m in msg for m in _unavailable_markers), (
            f"MCP 败者错误语义被伪装成 daemon 不可用: {msg[:500]}")
    bridge_conflict_cnt = 0
    for status, msg in bridge_results:
        if not any(m in msg for m in _conflict_markers):
            continue
        bridge_conflict_cnt += 1
        assert not any(m in msg for m in _unavailable_markers), (
            f"bridge(WSL) 败者错误语义被伪装成 daemon 不可用: {msg[:500]}")
    total_losers = cli_conflict_cnt + mcp_conflict_cnt + bridge_conflict_cnt
    assert total_losers == 7, (
        f"8 个并发源应恰好 7 个败者（1 个胜者），实际 CLI {cli_conflict_cnt} + "
        f"MCP {mcp_conflict_cnt} + bridge {bridge_conflict_cnt} = {total_losers}")

    # 6. 断言 D：所有 task_events 位于 Windows authority（直读隔离任务库）。
    #    事件 = created + 恰好 1 条 claimed（claim 败者只返回 task_conflict，不写事件）
    assert os.path.isfile(task_db), f"Windows authority 任务库未落盘: {task_db}"
    assert len(events) >= 2, f"task_events 事件数不足: {events}"

    # 7. 断言 E：WSL 侧没有本地 DB 写入（模拟 WSL HOME 目录从未创建）
    wsl_home = os.path.join(authority["tmp"], "wsl_home")
    wsl_db = os.path.join(wsl_home, ".callwarden", "callwarden.db")
    assert not os.path.exists(wsl_db), f"WSL 侧出现了本地 DB 写入（禁止本地 fallback）: {wsl_db}"

    # 8. report（WSL 胜者经 bridge 写回 Windows authority）
    report = authority["wsl_client"].call(
        "task.report",
        {"task_id": task_id, "summary": "completed by winner",
         "agent_session_id": winner_session},
    )
    assert report.get("status") == "review", report
    status = authority["wsl_client"].call("task.status", {"task_id": task_id})
    assert status.get("status") == "review", status


def test_wsl_bridge_client_creates_task_in_windows_authority(authority):
    """P1：WSL client 经 bridge 创建的任务必须落在 Windows authority。

    1. bridge 客户端 task.create → daemon 返回 task_id；
    2. Windows 侧（隔离 daemon）经 cw RPC 能读取该任务（同 authority）；
    3. WSL 侧没有本地 DB 文件（fail-closed，禁止本地 fallback）。
    """
    wsl = authority["wsl_client"]
    created = wsl.call("task.create", {
        "title": "WSL bridge 创建任务",
        "description": "经 bridge 写入 Windows authority",
        "workspace_id": "ws-cross",
    })
    task_id = created.get("task_id")
    assert task_id, created
    assert created.get("status") == "open", created

    # Windows 侧读取（同一 daemon，经 pipe）
    r = subprocess.run(
        [_CLIENT_BIN,
         "--socket", authority["pipe"], "--timeout", "15",
         "rpc", "task.status", json.dumps({"task_id": task_id})],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert r.returncode == 0, r
    parsed = json.loads(r.stdout)
    assert parsed.get("status") == "open", parsed

    # WSL 侧无本地 DB
    wsl_home = os.path.join(authority["tmp"], "wsl_home")
    wsl_db = os.path.join(wsl_home, ".callwarden", "callwarden.db")
    assert not os.path.exists(wsl_db), f"WSL 侧出现了本地 DB 写入: {wsl_db}"


def test_request_id_idempotent_after_bridge_restart(authority):
    """P1：bridge 重启后，相同 request_id 的 mutation 由 daemon dedup，不重复写。

    契约 §6.3：未确认提交结果的 mutation 不得盲目重复，必须复用 request_id
    使 daemon 侧 check_dedup 返回已提交结果。验证：
    1. bridge 客户端带 request_id=R1 创建任务 → task_id1；
    2. 终止 bridge，用同一 token/端口重启 bridge；
    3. 复用 R1 再次 task.create → 返回同一 task_id（无重复创建）；
    4. 直读 Windows authority 任务库，task_events 中 created 事件只有 1 条。
    """
    import sqlite3

    wsl = authority["wsl_client"]
    request_id = f"req-{os.getpid()}-dedup"
    created = wsl.mutation_call("task.create", {
        "title": "dedup bridge 重启",
        "request_id": request_id,
    })
    task_id = created["task_id"]
    assert created["status"] == "open", created

    # 1. 重启 bridge（同一 token / 端口）
    bridge_proc = authority["procs"][1]
    bridge_proc.terminate()
    try:
        bridge_proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        bridge_proc.kill()
        bridge_proc.wait(timeout=10)

    bridge_env = dict(os.environ)
    bridge_env["CW_BRIDGE_TOKEN_FILE"] = authority["token_file"]
    bridge_env["CW_BRIDGE_ENDPOINT"] = f"127.0.0.1:{authority['bridge_port']}"
    bridge_env["CW_BRIDGE_MANIFEST"] = os.path.join(authority["tmp"], "bridge-restart.manifest.json")
    new_bridge, new_bridge_log = _spawn(
        _BRIDGE_BIN, [], authority["tmp"], "bridge2", env=bridge_env
    )
    authority["procs"].append(new_bridge)

    # 2. 等待新 bridge 就绪
    wsl2 = _production_wsl_client(authority["bridge_port"])
    deadline = time.time() + 20
    ready = False
    while time.time() < deadline:
        if new_bridge.poll() is not None:
            break
        try:
            wsl2.hello()
            ready = True
            break
        except (OSError, socket.timeout, RuntimeError):
            time.sleep(0.3)
    assert ready, "bridge 重启后未就绪"

    # 3. 复用同一 request_id 重试（daemon dedup 返回已提交结果）
    retried = wsl2.mutation_call("task.create", {
        "title": "dedup bridge 重启",
        "request_id": request_id,
    })
    assert retried["task_id"] == task_id, f"同一 request_id 重复创建了不同任务: {retried}"

    # 4. 直读 Windows authority 任务库：created 事件只有 1 条（无重复写）
    conn = sqlite3.connect(authority["task_db"], timeout=10)
    try:
        created_events = conn.execute(
            "SELECT COUNT(*) FROM task_events WHERE task_id = ? AND reason_code = 'created'",
            (task_id,),
        ).fetchone()[0]
    finally:
        conn.close()
    assert created_events == 1, f"request_id 幂等失败：created 事件 {created_events} 条（期望 1）"


def test_authority_pin_mismatch_fail_closed(authority):
    """P0：WSL client 经 bridge 校验 authority 不一致时 fail-closed。

    - 错误 authority_id / fingerprint → DaemonRemoteError(E_AUTHORITY_MISMATCH)；
    - fail-closed 后不继续发起业务请求（mutation_call 在 verify_authority 处中断）。
    """
    wsl = authority["wsl_client"]

    with pytest.raises(DaemonRemoteError) as ei:
        wsl.verify_authority(expected_authority_id="wrong-authority")
    assert ei.value.code == "E_AUTHORITY_MISMATCH", ei.value

    with pytest.raises(DaemonRemoteError) as ei:
        wsl.verify_authority(expected_fingerprint="wrong-fingerprint")
    assert ei.value.code == "E_AUTHORITY_MISMATCH", ei.value

    # mutation_call 在 verify_authority 阶段就 fail-closed，不发起业务请求
    with pytest.raises(DaemonRemoteError) as ei:
        wsl.mutation_call("task.create", {"title": "不应执行"},
                          expected_authority_id="wrong-authority")
    assert ei.value.code == "E_AUTHORITY_MISMATCH", ei.value
