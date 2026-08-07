r"""Windows daemon 最小可用协同闭环 - 真实进程级 E2E。

本套件与旧版（仅测 Python DB 层 / 构造 client 对象）不同，是**真实进程级**验证：
- 启动真实 `cw-daemon.exe`（独立临时数据目录 + registry DB）
- 用真实 `cw-client.exe` 通过 Windows Named Pipe 发送 `[4B BE len][JSON]` 帧
- 覆盖：schema.version / task 完整生命周期 / 并发 claim 冲突 / 重启恢复

前置条件（本机需满足）：
1. Windows 平台（Named Pipe）
2. 已构建 Rust 二进制：`cargo build --manifest-path rust_ext/Cargo.toml --bin cw-daemon --bin cw-client`
3. 默认管道 `\\.\pipe\callwarden-<sid>` 未被其他 daemon 占用（占用则 skip）
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DAEMON_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "debug", "cw-daemon.exe")
_CLIENT_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "debug", "cw-client.exe")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="进程级 Windows daemon E2E 需要 Windows + Named Pipe",
)

requires_binaries = pytest.mark.skipif(
    not (os.path.exists(_DAEMON_BIN) and os.path.exists(_CLIENT_BIN)),
    reason="cw-daemon.exe / cw-client.exe 未构建（需先 cargo build --bin cw-daemon --bin cw-client）",
)


def _client(pipe: str, args: list, timeout: int = 30) -> dict:
    """调用真实 cw-client.exe（Named Pipe 客户端），返回结构化结果。"""
    result = subprocess.run(
        [_CLIENT_BIN, "--socket", pipe, "--timeout", str(timeout)] + args,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout + 15,
    )
    parsed = {}
    try:
        parsed = json.loads(result.stdout)
    except Exception:
        pass
    return {
        "code": result.returncode,
        "json": parsed,
        "stdout": result.stdout.strip(),
        "stderr": result.stderr.strip(),
    }


def _daemon_config(tmp: str) -> dict:
    """生成隔离的 daemon JSON 配置（Windows 管道名由 transport 按 SID 派生，socket_path 仅作配置占位）。"""
    data_root = os.path.join(tmp, "data")
    return {
        "socket_path": "",
        "registry_db_path": os.path.join(tmp, "registry.db"),
        # P0 修复：显式注入 Task 协同存储路径（权威任务库），daemon 与 Python `cw task`
        # 必须共享同一 DB 文件（否则两套任务状态）。生产环境由 daemon_autostart 注入
        # 真正的 `~/.callwarden/callwarden.db`，E2E 在隔离临时目录中验证同一机制。
        "task_db_path": os.path.join(tmp, "callwarden.db"),
        "data_root": data_root,
        "max_workers": 4,
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
def ensure_fresh_binaries():
    """P2 门禁：显式构建 cw-daemon/cw-client，确保二进制由当前源码重建。

    此前测试只检查 exe 存在，源码改动后不重新构建会导致 E2E 用旧二进制误通过。
    本 fixture 在模块内所有测试之前运行 `cargo build --bin cw-daemon --bin cw-client`：
    - cargo 缺失 → skip（无法提供新鲜二进制）
    - 构建失败 → fail（源码编译回归，测试必须红）
    """
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("未找到 cargo，无法构建新鲜二进制")
    build = subprocess.run(
        [cargo, "build", "--manifest-path", os.path.join(_REPO_ROOT, "rust_ext", "Cargo.toml"),
         "--bin", "cw-daemon", "--bin", "cw-client"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if build.returncode != 0:
        pytest.fail("cargo build 失败，二进制无法由当前源码重建：\n" + (build.stdout + build.stderr)[-3000:])
    if not (os.path.exists(_DAEMON_BIN) and os.path.exists(_CLIENT_BIN)):
        pytest.fail(f"cargo build 成功但未产出 {_DAEMON_BIN} / {_CLIENT_BIN}")


def _spawn_daemon(config_path: str, log_dir: str, name: str):
    """启动真实 cw-daemon.exe，日志落盘。"""
    log = open(os.path.join(log_dir, f"{name}.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [_DAEMON_BIN, "--config", config_path],
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
    )
    return proc


def _wait_daemon(pipe: str, proc, timeout: float = 40.0) -> bool:
    """轮询等待 daemon 管道可用（真实 Named Pipe ping）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        r = _client(pipe, ["ping"], timeout=5)
        if r["code"] == 0 and r["json"].get("status") == "ok":
            return True
        time.sleep(0.5)
    return False


def _run_as_other_user(user: str, password: str, cmd: list, out_path: str, timeout: float = 60.0) -> dict:
    """以指定 Windows 用户身份（CreateProcessWithLogonW 真实登录 token）启动进程。

    用于 P1 ACL 真实双身份验证：用户 B 使用真实 token 连接用户 A 启动的 daemon 管道，
    验证 Named Pipe SDDL 是否被 Windows 内核实际拒绝。stdout/stderr 重定向到 out_path，
    返回 {"exit_code", "output"}。
    """
    import ctypes
    from ctypes import wintypes

    import msvcrt

    LOGON_WITH_PROFILE = 0x00000001
    CREATE_UNICODE_ENVIRONMENT = 0x00000400
    CREATE_NO_WINDOW = 0x08000000
    STARTF_USESTDHANDLES = 0x00000100

    class STARTUPINFO(ctypes.Structure):
        _fields_ = [
            ("cb", wintypes.DWORD),
            ("lpReserved", wintypes.LPWSTR),
            ("lpDesktop", wintypes.LPWSTR),
            ("lpTitle", wintypes.LPWSTR),
            ("dwX", wintypes.DWORD),
            ("dwY", wintypes.DWORD),
            ("dwXSize", wintypes.DWORD),
            ("dwYSize", wintypes.DWORD),
            ("dwXCountChars", wintypes.DWORD),
            ("dwYCountChars", wintypes.DWORD),
            ("dwFillAttribute", wintypes.DWORD),
            ("dwFlags", wintypes.DWORD),
            ("wShowWindow", wintypes.WORD),
            ("cbReserved2", wintypes.WORD),
            ("lpReserved2", ctypes.POINTER(wintypes.BYTE)),
            ("hStdInput", wintypes.HANDLE),
            ("hStdOutput", wintypes.HANDLE),
            ("hStdError", wintypes.HANDLE),
        ]

    class PROCESS_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("hProcess", wintypes.HANDLE),
            ("hThread", wintypes.HANDLE),
            ("dwProcessId", wintypes.DWORD),
            ("dwThreadId", wintypes.DWORD),
        ]

    advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    # 输出文件句柄需可继承，子进程 stdout/stderr 重定向到该文件
    out_fd = os.open(out_path, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o644)
    try:
        os.set_handle_inheritable(out_fd, True)
        out_handle = msvcrt.get_osfhandle(out_fd)

        si = STARTUPINFO()
        si.cb = ctypes.sizeof(STARTUPINFO)
        si.dwFlags = STARTF_USESTDHANDLES
        si.hStdOutput = out_handle
        si.hStdError = out_handle
        pi = PROCESS_INFORMATION()

        cmdline = subprocess.list2cmdline(cmd)
        ok = advapi32.CreateProcessWithLogonW(
            user,                       # lpUsername
            None,                       # lpDomain（本机账户）
            password,                   # lpPassword
            LOGON_WITH_PROFILE,         # dwLogonFlags
            None,                       # lpApplicationName
            cmdline,                    # lpCommandLine
            CREATE_UNICODE_ENVIRONMENT | CREATE_NO_WINDOW,
            None,                       # lpEnvironment（继承当前环境）
            None,                       # lpCurrentDirectory
            ctypes.byref(si),
            ctypes.byref(pi),
        )
        if not ok:
            err = ctypes.get_last_error()
            pytest.fail(f"CreateProcessWithLogonW 失败（Win32 error {err}）")
        kernel32.CloseHandle(pi.hThread)
        wait_rc = kernel32.WaitForSingleObject(pi.hProcess, int(timeout * 1000))
        if wait_rc == 0x00000102:  # WAIT_TIMEOUT
            kernel32.TerminateProcess(pi.hProcess, 1)
        exit_code = wintypes.DWORD(0)
        kernel32.GetExitCodeProcess(pi.hProcess, ctypes.byref(exit_code))
        kernel32.CloseHandle(pi.hProcess)
    finally:
        os.close(out_fd)

    with open(out_path, "r", encoding="utf-8", errors="replace") as f:
        output = f.read().strip()
    return {"exit_code": exit_code.value, "output": output}


@pytest.fixture(scope="module")
def daemon():
    """启动真实 cw-daemon.exe，返回可重启的实例句柄。"""
    from callwarden.config import _get_windows_user_sid

    sid = _get_windows_user_sid()
    pipe = rf"\\.\pipe\callwarden-{sid}"

    # 若默认管道已被其他 daemon 占用，跳过（避免干扰既有实例）
    occupied = _client(pipe, ["ping"], timeout=5)
    if occupied["code"] == 0:
        pytest.skip(f"默认管道 {pipe} 已被其他 daemon 占用，跳过进程级 E2E")

    tmp = tempfile.mkdtemp(prefix="cw_e2e_proc_")
    config = _daemon_config(tmp)
    config_path = os.path.join(tmp, "daemon.json")
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f)

    procs = []
    proc = _spawn_daemon(config_path, tmp, "daemon")
    procs.append(proc)
    try:
        if not _wait_daemon(pipe, proc):
            log = ""
            try:
                with open(os.path.join(tmp, "daemon.log"), "r", encoding="utf-8") as f:
                    log = f.read()[-3000:]
            except Exception:
                pass
            pytest.fail(f"daemon 未在超时内响应，日志：\n{log}")
        yield {"pipe": pipe, "config_path": config_path, "tmp": tmp, "procs": procs, "restart": None}
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
        shutil.rmtree(tmp, ignore_errors=True)


@requires_binaries
def test_schema_migration_v46_upgrade_via_named_pipe():
    """P1：v46 旧库升级后，daemon 经 Named Pipe 执行完整 task RPC。

    必须位于模块首位：本测试自建独立 daemon 实例占用默认管道
    `\\\\.\\pipe\\callwarden-<sid>`，不能与模块级 daemon fixture 并发。
    验证链：
    1. 构造真实 v46 形态库（无 task_events/agent_registrations，schema_version=46）；
    2. daemon 打开后走 Rust 官方 migration（migrate_connection）升级到 47；
    3. 校验实际 schema version == 47（读真实 schema_version 表，而非编译时常量）；
    4. task.create/claim/status 完整 RPC 通过 Named Pipe 可用。
    """
    import sqlite3

    from callwarden.db import CodeGraphDB

    tmp = tempfile.mkdtemp(prefix="cw_e2e_v46_")
    procs = []
    try:
        task_db = os.path.join(tmp, "callwarden.db")

        # 1. 先用 Python 建 v47 库，再人为降级为 v46（模拟旧版库：无 task_events/agent_registrations）
        db = CodeGraphDB(db_path=task_db)
        db.close()
        conn = sqlite3.connect(task_db)
        conn.execute("DROP TABLE IF EXISTS task_events")
        conn.execute("DROP TABLE IF EXISTS agent_registrations")
        conn.execute("DROP INDEX IF EXISTS idx_task_events_task")
        conn.execute("UPDATE schema_version SET version = 46 WHERE version >= 47")
        conn.commit()
        v = conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]
        conn.close()
        assert v == 46, f"模拟 v46 库失败: schema_version={v}"

        # 2. 启动 daemon（task_db_path 指向 v46 库）
        config = _daemon_config(tmp)
        config["task_db_path"] = task_db
        config_path = os.path.join(tmp, "daemon.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)

        from callwarden.config import _get_windows_user_sid

        pipe = rf"\\.\pipe\callwarden-{_get_windows_user_sid()}"
        proc = _spawn_daemon(config_path, tmp, "v46")
        procs.append(proc)
        assert _wait_daemon(pipe, proc), "v46 库 daemon 未响应"

        # 3. 校验实际 schema version == 47（读真实 schema_version 表）
        conn = sqlite3.connect(task_db)
        v = conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]
        conn.close()
        assert v == 47, f"v46 库未升级到 47: schema_version={v}"

        # 4. 完整 task RPC 通过 Named Pipe 可用（创建 → 抢占 → 状态）
        r = _client(pipe, ["rpc", "task.create", json.dumps({"title": "v46 升级任务", "workspace_id": "ws-v46"})])
        assert r["code"] == 0, r
        task_id = r["json"].get("task_id")
        assert task_id, r
        assert r["json"].get("status") == "open", r

        r = _client(pipe, ["rpc", "task.claim", json.dumps({"task_id": task_id, "agent_session_id": "agent-v46"})])
        assert r["code"] == 0, r
        assert r["json"].get("status") == "in_progress", r

        r = _client(pipe, ["rpc", "task.status", json.dumps({"task_id": task_id})])
        assert r["code"] == 0, r
        assert r["json"].get("status") == "in_progress", r
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
                try:
                    p.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    p.kill()
        shutil.rmtree(tmp, ignore_errors=True)


@requires_binaries
def test_schema_version_via_named_pipe(daemon):
    """真实 Named Pipe：schema.version RPC 返回当前 SCHEMA_VERSION（47）。"""
    from callwarden.db.schema import SCHEMA_VERSION

    r = _client(daemon["pipe"], ["schema-version"])
    assert r["code"] == 0, r
    assert r["json"].get("version") == SCHEMA_VERSION, r


@requires_binaries
def test_task_lifecycle_via_named_pipe(daemon):
    """真实 Named Pipe：task.create -> claim -> 并发 claim 冲突 -> report -> status/events。"""
    pipe = daemon["pipe"]

    # 1. task.create（真实 RPC 写 registry/task 表）
    r = _client(
        pipe,
        ["rpc", "task.create", json.dumps({"title": "E2E 进程级任务", "description": "真实 Named Pipe", "workspace_id": "ws-proc"})],
    )
    assert r["code"] == 0, r
    task_id = r["json"].get("task_id")
    assert task_id, r
    assert r["json"].get("status") == "open", r

    # 2. task.claim（agent-A 抢占）
    r = _client(pipe, ["rpc", "task.claim", json.dumps({"task_id": task_id, "agent_session_id": "agent-A"})])
    assert r["code"] == 0, r
    assert r["json"].get("status") == "in_progress", r
    assert r["json"].get("claimed_by") == "agent-A", r

    # 3. task.claim（agent-B 并发抢占 → 冲突拒绝）
    r = _client(pipe, ["rpc", "task.claim", json.dumps({"task_id": task_id, "agent_session_id": "agent-B"})])
    assert r["code"] == 1, r
    assert "task_conflict" in r["stderr"], r

    # 4. task.report（owner agent-A 完成 → review）
    r = _client(
        pipe,
        ["rpc", "task.report", json.dumps({"task_id": task_id, "agent_session_id": "agent-A", "summary": "完成"})],
    )
    assert r["code"] == 0, r
    assert r["json"].get("status") == "review", r

    # 5. task.status（返回权威表 tasks 状态 + claimed_by）
    r = _client(pipe, ["rpc", "task.status", json.dumps({"task_id": task_id})])
    assert r["code"] == 0, r
    assert r["json"].get("status") == "review", r
    assert r["json"].get("claimed_by") == "agent-A", r

    # 6. task.events（task_events 事件流至少 3 条：created/claimed/reported）
    r = _client(pipe, ["rpc", "task.events", json.dumps({"task_id": task_id})])
    assert r["code"] == 0, r
    events = r["json"].get("events", [])
    assert len(events) >= 3, r


@requires_binaries
def test_daemon_restart_preserves_task(daemon):
    """重启恢复：task 状态持久化在 registry DB，daemon 重启后 task.status 仍可读。"""
    pipe = daemon["pipe"]
    r = _client(pipe, ["rpc", "task.create", json.dumps({"title": "restart-check", "workspace_id": "ws-proc"})])
    assert r["code"] == 0, r
    task_id = r["json"]["task_id"]

    # 1. 终止 daemon
    proc = daemon["procs"][0]
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()

    # 2. 同一数据目录重启 daemon
    proc2 = _spawn_daemon(daemon["config_path"], daemon["tmp"], "daemon2")
    daemon["procs"].append(proc2)
    assert _wait_daemon(pipe, proc2), "重启后 daemon 未恢复响应"

    # 3. task.status 仍返回 open（registry DB 持久化）
    r = _client(pipe, ["rpc", "task.status", json.dumps({"task_id": task_id})])
    assert r["code"] == 0, r
    assert r["json"].get("status") == "open", r


@requires_binaries
def test_unknown_method_rejected_via_named_pipe(daemon):
    """真实 Named Pipe：未注册方法返回 method_not_found（exit 1）。"""
    r = _client(daemon["pipe"], ["rpc", "no.such.method", "{}"])
    assert r["code"] == 1, r
    assert "method_not_found" in r["stderr"], r


@requires_binaries
def test_task_db_shared_with_python_cli(daemon):
    """P0：daemon 与 Python `cw task` CLI 共享同一权威任务库（双向断言）。

    5 步验证：
    1. daemon RPC task.create 创建任务；
    2. Python CodeGraphDB（`cw task show/list` 同一底层代码路径）读取同一任务；
    3. Python CodeGraphDB 创建任务（等价 `cw task create`）；
    4. daemon RPC task.status 读取该任务；
    5. 断言两者打开的是同一个 DB 文件（realpath 相等）。
    """
    from callwarden.db import CodeGraphDB

    with open(daemon["config_path"], "r", encoding="utf-8") as f:
        cfg = json.load(f)
    task_db = cfg["task_db_path"]
    assert task_db, "daemon 配置未注入 task_db_path"
    pipe = daemon["pipe"]

    # 1. daemon RPC 创建任务
    r = _client(pipe, ["rpc", "task.create", json.dumps({"title": "P0 共享库 daemon 侧", "workspace_id": "ws-shared"})])
    assert r["code"] == 0, r
    task_id_d = r["json"].get("task_id")
    assert task_id_d, r

    # 2. Python CodeGraphDB 读取同一任务（cw task show/list 同一代码路径）
    db = CodeGraphDB(db_path=task_db)
    try:
        ctx = db.get_task_context(task_id_d)
        assert ctx is not None, f"Python 侧读不到 daemon 创建的任务 {task_id_d}"
        assert ctx["title"] == "P0 共享库 daemon 侧", ctx
        assert ctx["status"] == "open", ctx

        # 3. Python 侧创建任务（等价 CLI task create）
        task_id_p = db.task_create(title="P0 共享库 python 侧", creator="e2e-cli")
        assert task_id_p, "Python task_create 未返回 task_id"

        # 5. 同一 DB 文件（Python 打开的路径 == daemon 配置注入的路径）
        assert os.path.realpath(db.db_path) == os.path.realpath(task_db), (
            f"Python 打开 {db.db_path} 与 daemon 配置 {task_db} 不是同一文件"
        )
    finally:
        db.close()

    # 4. daemon RPC 读取 Python 创建的任务
    r = _client(pipe, ["rpc", "task.status", json.dumps({"task_id": task_id_p})])
    assert r["code"] == 0, r
    assert r["json"].get("status") == "open", r


@requires_binaries
def test_acl_dual_identity_rejects_other_user(daemon):
    """P1 ACL：真实双身份验证 Named Pipe SDDL 拒绝其他用户（Windows 内核级）。

    需要 Windows 上存在第二个本地用户（测试账号），凭据通过环境变量提供：
    - CW_E2E_OTHER_USER / CW_E2E_OTHER_PASSWORD
    未配置时跳过（CI runner 配置测试账号后启用）。验证链：
    1. 用户 A（当前用户）启动 daemon（模块 fixture）并正常访问；
    2. 用户 B 使用真实登录 token（CreateProcessWithLogonW）连接用户 A 的管道
       → 被 Named Pipe SDDL 拒绝，客户端输出真实 Win32 错误码（ERROR_ACCESS_DENIED=5）；
    3. 用户 A 仍可正常访问。
    """
    other_user = os.environ.get("CW_E2E_OTHER_USER", "")
    other_password = os.environ.get("CW_E2E_OTHER_PASSWORD", "")
    if not (other_user and other_password):
        pytest.skip("未配置 CW_E2E_OTHER_USER / CW_E2E_OTHER_PASSWORD，跳过真实双身份 ACL 验证")

    pipe = daemon["pipe"]

    # 1. 用户 A 正常访问（模块 daemon 由当前用户启动）
    r = _client(pipe, ["ping"])
    assert r["code"] == 0, r

    # 2. 用户 B 真实 token 连接用户 A 的管道 → SDDL 拒绝 + 真实 Win32 错误码
    tmp = daemon["tmp"]
    out_path = os.path.join(tmp, "acl_other_user.out")
    denied = _run_as_other_user(
        other_user,
        other_password,
        [_CLIENT_BIN, "--socket", pipe, "--timeout", "15", "ping"],
        out_path,
        timeout=60,
    )
    assert denied["exit_code"] != 0, f"用户 B 竟然连接成功: {denied}"
    assert "Win32 error 5" in denied["output"] or "访问被拒绝" in denied["output"], (
        f"用户 B 拒绝原因未记录真实 Win32 错误码: {denied}"
    )

    # 3. 用户 A 仍可正常访问
    r = _client(pipe, ["ping"])
    assert r["code"] == 0, r
