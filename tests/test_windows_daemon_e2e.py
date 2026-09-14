r"""Windows daemon 最小可用协同闭环 - 真实进程级 E2E。

本套件与旧版（仅测 Python DB 层 / 构造 client 对象）不同，是**真实进程级**验证：
- 启动真实 `cw-daemon.exe`（独立临时数据目录 + registry DB）
- 用真实 `cw-client.exe` 通过 Windows Named Pipe 发送 `[4B BE len][JSON]` 帧
- 覆盖：schema.version / task 完整生命周期 / 并发 claim 冲突 / 重启恢复

前置条件（本机需满足）：
1. Windows 平台（Named Pipe）
2. 已构建 Rust 二进制：`cargo build --no-default-features --manifest-path rust_ext/Cargo.toml --bin cw-daemon --bin cw-client`
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
    """调用真实 cw-client.exe（Named Pipe 客户端），返回结构化结果。

    daemon 冷启动预热期（管道已绑定但 worker 池未就绪，约 50–89s）内，
    客户端能连上管道却收不到应答，其自身 --timeout 不生效会挂起；此时
    subprocess 超时后必须吞掉 TimeoutExpired 返回哨兵，让 _wait_daemon
    继续轮询，而不是让整个 fixture 崩出。
    """
    try:
        result = subprocess.run(
            [_CLIENT_BIN, "--socket", pipe, "--timeout", str(timeout)] + args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout + 10,
        )
    except subprocess.TimeoutExpired:
        return {"code": -1, "json": {}, "stdout": "", "stderr": "client subprocess timeout"}
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
    本 fixture 在模块内所有测试之前运行 `cargo build --no-default-features --bin cw-daemon --bin cw-client`：
    - cargo 缺失 → skip（无法提供新鲜二进制）
    - 构建失败 → fail（源码编译回归，测试必须红）
    """
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("未找到 cargo，无法构建新鲜二进制")
    build = subprocess.run(
        [cargo, "build", "--no-default-features", "--manifest-path", os.path.join(_REPO_ROOT, "rust_ext", "Cargo.toml"),
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


def _spawn_daemon(config_path: str, log_dir: str, name: str, env: dict = None):
    """启动真实 cw-daemon.exe，日志落盘。env=None 时继承当前进程环境。"""
    log = open(os.path.join(log_dir, f"{name}.log"), "w", encoding="utf-8")
    proc = subprocess.Popen(
        [_DAEMON_BIN, "--config", config_path],
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        env=env,
    )
    return proc


def _wait_daemon(pipe: str, proc, timeout: float = 150.0) -> bool:
    """轮询等待 daemon 管道可用（真实 Named Pipe ping）。

    timeout 默认 150s 的依据（2026-09-16 干净单写机实测，见 step3 证据）：
    daemon 冷启动存在两段 ~24s 空耗（空 registry 下 recover_all_workspaces
    与 state_factory 各 ~24s，疑似新建 SQLite 被 Defender 扫描卡顿），
    命名管道在 ~50s 才绑定，worker 池预热到首次成功应答需 ~89s
    （50–89s 期间日志持续 "worker pool full, rejecting connection"）。
    旧默认 40s 短于真实就绪时间导致恒假失败；~90s 实测在边缘（88.99s/90.57s）
    不稳健，故取 150s 留足裕度。命名管道 transport 本身工作正常。
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        r = _client(pipe, ["ping"], timeout=5)
        if r["code"] == 0 and r["json"].get("status") == "ok":
            return True
        time.sleep(0.5)
    return False


def _seed_task_workspace(pipe: str, task_db: str, ws_root: str, name: str) -> dict:
    """注册 workspace 并在 task-DB workspaces 表播种对应行，返回 task.create/report 绑定配对。

    BR-01/BR-02 上线后的现行权威契约（task_collab_shared.rs:required_workspace_id_param、
    task_collab.rs:bind_task_to_workspace）：task.create 的 workspace_id 必须是整数 > 0
    且在 task-DB ``workspaces`` 表存在同名行，workspace_instance_id 必须非空；
    daemon 在 create 同一事务内自动建 workspace_authority_captures + task_workspace_bindings。
    旧测试传字符串 ws-id（ws-v46/ws-cli/ws-proc/ws-shared）或缺 workspace_id，属于
    陈旧断言。本函数对齐 tests/test_task_prompt_e2e.py 的经证实范式：
    workspace.register 返回 registry 整数 workspace_id（daemon_workspaces 自增 id）
    + workspace_instance_id，把该整数 id 同步插入 task-DB workspaces 表（is_active=1），
    使 registry 与 task-DB 的 id/instance 两侧一致
    （resolve_workspace_pair_from_daemon 的 workspace.status 双向一致性校验要求）。

    另带 git provenance 注册（git_remote_url + git_head_commit_sha 非空），使 registry
    写入 daemon 发布的 snapshot_id；task.report 的 validate_report_snapshot_authority
    要求该 snapshot_id 非空且与 report 传入值逐字一致（否则 E_TASK_REPORT_SNAPSHOT_REQUIRED
    / E_TASK_REPORT_SNAPSHOT_MISMATCH）。canonicalization_rule_sets 的 workspace_capture
    rule row 由 daemon 启动时迁移播种，无需测试干预；但前提是 task-DB 由 daemon 自建
    （Python CodeGraphDB 建库不经 v52→v53 迁移，缺该 row → E_WORKSPACE_AUTHORITY_MISMATCH）。
    ws_root 必须是存在且属当前用户的目录（validate_owned_path 校验）。
    """
    import sqlite3

    reg = _client(pipe, ["rpc", "workspace.register", json.dumps({
        "name": name, "client_view_root": ws_root, "description": "windows daemon e2e",
        "git_remote_url": "https://github.com/callwarden/windows-e2e.git",
        "git_head_commit_sha": "e2e0" * 10,
    })])
    assert reg["code"] == 0, reg
    ws_id = reg["json"].get("workspace_id")
    ws_inst = reg["json"].get("workspace_instance_id")
    snapshot_id = reg["json"].get("snapshot_id")
    assert isinstance(ws_id, int) and ws_id > 0, reg
    assert isinstance(ws_inst, str) and ws_inst.strip(), reg
    assert isinstance(snapshot_id, str) and snapshot_id.strip(), reg

    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO workspaces (id, name, root_path, created_at, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (ws_id, name, ws_root, time.time()),
        )
        conn.commit()
    finally:
        conn.close()
    return {
        "workspace_id": ws_id,
        "workspace_instance_id": ws_inst,
        "snapshot_id": snapshot_id,
    }


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
        # 播种一个模块级共享 workspace（BR-01/BR-02 契约），供本模块所有
        # task.create 测试复用同一 (workspace_id, workspace_instance_id) 配对；
        # 同 workspace 的 capture 链只确立一次，后续 create 均命中已确立 instance。
        ws_root = os.path.join(tmp, "ws_root")
        os.makedirs(ws_root, exist_ok=True)
        ws = _seed_task_workspace(pipe, config["task_db_path"], ws_root, "e2e-module-ws")
        yield {
            "pipe": pipe, "config_path": config_path, "tmp": tmp, "procs": procs,
            "restart": None,
            "workspace_id": ws["workspace_id"],
            "workspace_instance_id": ws["workspace_instance_id"],
            "snapshot_id": ws["snapshot_id"],
        }
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
    from callwarden.config import _get_windows_user_sid

    # stale 修复（A 类：测试侧缺陷，环境根因 B 类）：本测试自建 daemon 占用默认管道，
    # 但旧版缺少占用检测——当本机已有一个真实 cw-daemon 常驻（如 PID 21012）持有
    # `\\.\pipe\callwarden-<sid>` 时，新建 daemon 无法绑定该管道，`_wait_daemon` 会
    # ping 到**已存在的** daemon（返回 ok），后续断言便打到错误的 daemon/task_db 上
    # （表现为 `schema_version=46 未升级`）。模块级 `daemon` fixture（本文件 L241-243）
    # 与 identity 测试（L384-385）均已有相同占用检测；此处补齐，保持一致。
    pipe = rf"\\.\pipe\callwarden-{_get_windows_user_sid()}"
    if _client(pipe, ["ping"], timeout=5)["code"] == 0:
        pytest.skip(f"默认管道 {pipe} 已被其他 daemon 占用，跳过 v46 迁移 E2E")

    tmp = tempfile.mkdtemp(prefix="cw_e2e_v46_")
    procs = []
    try:
        task_db = os.path.join(tmp, "callwarden.db")

        # 1. 先用 Python 建 v49 库，再人为降级为 v46（模拟旧版库：无 task_events/agent_registrations）
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

        proc = _spawn_daemon(config_path, tmp, "v46")
        procs.append(proc)
        assert _wait_daemon(pipe, proc), "v46 库 daemon 未响应"

        # 2b. 播种 workspace（迁移后的库已具备现行 schema 的 workspaces 表），
        # 使 task.create 满足 BR-01/BR-02 显式整数 workspace_id + instance 契约。
        ws = _seed_task_workspace(pipe, task_db, tmp, "e2e-v46-ws")

        # 3. 校验实际 schema version == 当前权威版本（读真实 schema_version 表）
        from callwarden.db.schema import SCHEMA_VERSION
        conn = sqlite3.connect(task_db)
        v = conn.execute("SELECT COALESCE(MAX(version),0) FROM schema_version").fetchone()[0]
        conn.close()
        assert v == SCHEMA_VERSION, f"v46 库未升级到 {SCHEMA_VERSION}: schema_version={v}"

        # 4. 完整 task RPC 通过 Named Pipe 可用（创建 → 抢占 → 状态）
        r = _client(pipe, ["rpc", "task.create", json.dumps({
            "title": "v46 升级任务",
            "workspace_id": ws["workspace_id"],
            "workspace_instance_id": ws["workspace_instance_id"],
        })])
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
def test_task_report_identity_writeback_via_named_pipe():
    """D0：真实 Windows daemon 接受 identity 并完成 task 状态写回。

    这条测试专门覆盖此前的 E_IDENTITY_NOT_WIRED 阻塞：请求必须经过真实
    ``cw-client.exe``/Named Pipe，由 daemon 在同一事务内写入 task_events 和
    action_identities；测试结束后再从隔离任务库核对持久化结果。
    """
    import sqlite3

    from callwarden.config import _get_windows_user_sid

    tmp = tempfile.mkdtemp(prefix="cw_e2e_identity_")
    proc = None
    # stale 修复（A 类：测试侧缺陷）：旧版 finally 块**无条件**断言 action_identities
    # 行，但上面的默认管道占用分支会先 `pytest.skip`；`finally` 先于 Skip 异常传播执行，
    # 其 AssertionError 会**替换**掉 Skip，使「应跳过」变成「失败」
    # （实测 `None != (...)`）。用 completed 标记：仅当测试体真正跑完（含 daemon 写回）
    # 才在 finally 里核对持久化，Skip/异常路径不再误断言。
    completed = False
    try:
        task_db = os.path.join(tmp, "callwarden.db")
        # task-DB 由 daemon 自建（配置注入 task_db_path）：daemon 启动时走 v52→v53
        # 迁移，幂等播种 canonicalization_rule_sets 的 workspace_capture rule row
        # （seed_workspace_capture_rule）。旧版用 Python CodeGraphDB 建库，Python
        # schema 不经该迁移 → bind_task_to_workspace 读 rule row 时
        # E_WORKSPACE_AUTHORITY_MISMATCH（capability 未就绪）。

        config = _daemon_config(tmp)
        config["task_db_path"] = task_db
        config_path = os.path.join(tmp, "daemon.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)

        pipe = rf"\\.\pipe\callwarden-{_get_windows_user_sid()}"
        if _client(pipe, ["ping"], timeout=5)["code"] == 0:
            pytest.skip(f"默认管道 {pipe} 已被其他 daemon 占用，跳过 identity E2E")

        proc = _spawn_daemon(config_path, tmp, "identity")
        assert _wait_daemon(pipe, proc), "identity daemon 未响应"

        # BR-01/BR-02：task.create 需显式整数 workspace_id + 非空 instance，
        # 且 task-DB workspaces 表需存在对应行（旧版缺此播种 → 旧断言建在绕过门禁的
        # 假设上）。播种后 identity 才能绑定到真实 workspace_id。
        ws = _seed_task_workspace(pipe, task_db, tmp, "e2e-identity-ws")

        task_id = "T-WINDOWS-IDENTITY-E2E"
        identity = {
            "agent_id": "agent-windows-e2e",
            "session_id": "session-windows-e2e",
            "model_id": "model-windows-e2e",
            "role": "implementer",
        }

        result = _client(pipe, ["rpc", "task.create", json.dumps({
            "task_id": task_id,
            "title": "Windows identity writeback",
            "workspace_id": ws["workspace_id"],
            "workspace_instance_id": ws["workspace_instance_id"],
        })])
        assert result["code"] == 0, result

        result = _client(pipe, ["rpc", "task.claim", json.dumps({
            "task_id": task_id,
            "agent_session_id": identity["session_id"],
        })])
        assert result["code"] == 0, result

        result = _client(pipe, ["rpc", "task.report", json.dumps({
            "task_id": task_id,
            "summary": "identity persisted",
            "agent_session_id": identity["session_id"],
            "identity": identity,
            "snapshot_id": ws["snapshot_id"],
        })])
        assert result["code"] == 0, result
        assert result["json"].get("status") == "review", result

        result = _client(pipe, ["rpc", "task.events", json.dumps({"task_id": task_id})])
        assert result["code"] == 0, result
        events = result["json"].get("events", [])
        assert any(
            event.get("reason_code") == "reported"
            and event.get("role") == "implementer"
            and event.get("agent_session_id") == identity["session_id"]
            for event in events
        ), result
        # 测试体全部通过后才在 finally 里核对持久化（避免覆盖 Skip）
        completed = True
    finally:
        if proc is not None and proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        if completed and os.path.exists(os.path.join(tmp, "callwarden.db")):
            conn = sqlite3.connect(os.path.join(tmp, "callwarden.db"))
            try:
                row = conn.execute(
                    "SELECT agent_id, session_id, model_id, role FROM action_identities "
                    "WHERE task_id = ? ORDER BY id DESC LIMIT 1",
                    ("T-WINDOWS-IDENTITY-E2E",),
                ).fetchone()
                assert row == (
                    "agent-windows-e2e",
                    "session-windows-e2e",
                    "model-windows-e2e",
                    "implementer",
                ), row
            finally:
                conn.close()
        shutil.rmtree(tmp, ignore_errors=True)


@requires_binaries
def test_task_db_shared_with_real_cw_cli():
    """P2：daemon 与真实 `cw task` CLI 子进程共享同一权威库（默认路径推导）。

    与 test_task_db_shared_with_python_cli（进程内 CodeGraphDB）不同，本测试启动真实
    `cw.py task show/create` 子进程，并通过 USERPROFILE 让 CLI 走**默认路径推导**
    （config.py:DB_PATH = ~/.callwarden/callwarden.db），验证：
    1. daemon RPC 创建的任务能被真实 CLI `cw task show` 读取；
    2. 真实 CLI `cw task create` 创建的任务能被 daemon RPC 读取；
    3. 两者打开的是同一 DB 文件（fake_home/.callwarden/callwarden.db）。
    必须位于 v46 测试之后、模块 daemon fixture 之前（独占默认管道）。
    """
    import re

    from callwarden.config import _get_windows_user_sid

    tmp = tempfile.mkdtemp(prefix="cw_e2e_cli_")
    procs = []
    try:
        # fake home：daemon 的 task_db_path 与 CLI 默认推导（~/.callwarden/callwarden.db）指向同一文件
        fake_home = os.path.join(tmp, "home")
        task_db = os.path.join(fake_home, ".callwarden", "callwarden.db")

        config = _daemon_config(tmp)
        config["task_db_path"] = task_db
        config_path = os.path.join(tmp, "daemon.json")
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(config, f)

        pipe = rf"\\.\pipe\callwarden-{_get_windows_user_sid()}"
        # stale 修复（A 类：测试侧缺陷，环境根因 B 类）：与 v46 测试同因——本测试自建
        # daemon 占用默认管道，但缺占用检测。本机已有常驻 cw-daemon 时，`_wait_daemon`
        # 会 ping 到**已存在的** daemon，task.create 便打到错误的 daemon/真实任务库上，
        # 暴露为 `E_TASK_WORKSPACE_UNBOUND: workspace_id 无法解析为整数: ws-cli`。
        # 补齐与模块 fixture（L241-243）、identity 测试（L384-385）一致的占用检测。
        if _client(pipe, ["ping"], timeout=5)["code"] == 0:
            pytest.skip(f"默认管道 {pipe} 已被其他 daemon 占用，跳过 CLI 共享库 E2E")
        # daemon 与 CLI 共用同一 fake_home：daemon 的 HTTP manifest 固定写
        # USERPROFILE/.callwarden（见 test_http_daemon_release_acceptance._wait_manifest），
        # CLI 发现路径只认 manifest（E_HTTP_MANIFEST_MISSING，fail-closed 不回退
        # Named Pipe/UDS/SQLite）。旧版 daemon 继承真实 USERPROFILE → manifest 落
        # 真实 home，而 CLI 的 USERPROFILE=fake_home 找不到 → `cw task show` 必败。
        # 让 daemon 也以 fake_home 启动，manifest 与 task_db 同落 fake_home/.callwarden。
        daemon_env = dict(os.environ)
        daemon_env["USERPROFILE"] = fake_home
        daemon_env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
        # registry 隔离（探针实证）：workspace.list/status/inject 读 daemon 的
        # 权威 registry，其默认路径由 Windows CSIDL 真实 profile 解析（忽略
        # USERPROFILE 环境变量）→ 会泄漏真实 registry 的同名 repo-root workspace
        # （active 行 instance 与 seed 不同 → resolve matches=0 或撞库）。只有
        # 显式 CW_DAEMON_REGISTRY_DB env 能让 list 只返回本测试 seed 的 workspace。
        daemon_env["CW_DAEMON_REGISTRY_DB"] = config["registry_db_path"]
        proc = _spawn_daemon(config_path, tmp, "cli", env=daemon_env)
        procs.append(proc)
        assert _wait_daemon(pipe, proc), "daemon 未响应"

        # 播种 workspace：client_view_root 必须取 _REPO_ROOT，因为 CLI 子进程的
        # config.PROJECT_ROOT == _REPO_ROOT（cw.py 自举 C:/git_work 到 sys.path，
        # callwarden 包根 = 仓库根），resolve_workspace_pair_from_daemon 按
        # _norm_root(client_view_root) == _norm_root(PROJECT_ROOT) 匹配本 workspace，
        # 并要求 workspace.status 的 registry/task-DB 两侧 id+instance 一致——
        # _seed_task_workspace 用 registry 返回的整数 id 同写 task-DB workspaces 表，
        # 天然满足该一致性。task_db 尚未落盘（daemon 首次访问时创建），故先确保
        # 父目录存在。
        os.makedirs(os.path.dirname(task_db), exist_ok=True)
        ws = _seed_task_workspace(pipe, task_db, _REPO_ROOT, "e2e-cli-ws")

        # CLI 子进程环境：USERPROFILE 指向 fake_home，使 config.py:DB_PATH 落到 task_db；
        # 跳过 auto-setup（否则首次进入新 home 会触发 MCP 配置安装，污染输出且耗时）。
        cli_env = dict(os.environ)
        cli_env["USERPROFILE"] = fake_home
        cli_env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
        cli_env["PYTHONPATH"] = _REPO_ROOT

        def _cw_cli(args: list) -> subprocess.CompletedProcess:
            return subprocess.run(
                [sys.executable, os.path.join(_REPO_ROOT, "cw.py")] + args,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=cli_env,
                timeout=60,
            )

        # 1. daemon RPC 创建任务
        r = _client(
            pipe,
            ["rpc", "task.create", json.dumps({
                "title": "P2 CLI 共享 daemon 侧",
                "workspace_id": ws["workspace_id"],
                "workspace_instance_id": ws["workspace_instance_id"],
            })],
        )
        assert r["code"] == 0, r
        task_id_d = r["json"].get("task_id")
        assert task_id_d, r

        # 2. 真实 CLI `cw task show` 读取 daemon 创建的任务（CLI 默认路径推导命中同一库）
        p = _cw_cli(["task", "show", task_id_d])
        assert p.returncode == 0, f"cw task show 失败: stdout={p.stdout[-800:]!r} stderr={p.stderr[-800:]!r}"
        assert "P2 CLI 共享 daemon 侧" in p.stdout, p.stdout[-800:]

        # 3. 真实 CLI `cw task create` 创建任务（输出语言跟随 CALLWARDEN_LANG，兼容中英）
        p = _cw_cli(["task", "create", "--title", "P2 CLI 共享 cli 侧"])
        assert p.returncode == 0, f"cw task create 失败: stdout={p.stdout[-800:]!r} stderr={p.stderr[-800:]!r}"
        m = re.search(r"(?:任务 ID|Task ID):\s*(\S+)", p.stdout)
        assert m, f"无法从 CLI 输出解析 task_id: {p.stdout[-800:]!r}"
        task_id_p = m.group(1)

        # 4. daemon RPC 读取 CLI 创建的任务
        r = _client(pipe, ["rpc", "task.status", json.dumps({"task_id": task_id_p})])
        assert r["code"] == 0, r
        assert r["json"].get("status") == "open", r

        # 5. 同一 DB 文件：CLI 默认推导路径（fake_home/.callwarden/callwarden.db）确实落盘且被写入
        assert os.path.isfile(task_db), f"CLI 未在默认推导路径落盘: {task_db}"
        assert os.path.realpath(task_db) == os.path.realpath(config["task_db_path"])
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
    """真实 Named Pipe：schema.version RPC 返回当前权威 SCHEMA_VERSION。"""
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
        ["rpc", "task.create", json.dumps({
            "title": "E2E 进程级任务",
            "description": "真实 Named Pipe",
            "workspace_id": daemon["workspace_id"],
            "workspace_instance_id": daemon["workspace_instance_id"],
        })],
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

    # 4. task.report（owner agent-A 完成 → review；snapshot_id 来自 fixture
    # 注册 workspace 时的 daemon 发布值，validate_report_snapshot_authority 要求逐字一致）
    r = _client(
        pipe,
        ["rpc", "task.report", json.dumps({
            "task_id": task_id, "agent_session_id": "agent-A", "summary": "完成",
            "snapshot_id": daemon["snapshot_id"],
        })],
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
    r = _client(pipe, ["rpc", "task.create", json.dumps({
        "title": "restart-check",
        "workspace_id": daemon["workspace_id"],
        "workspace_instance_id": daemon["workspace_instance_id"],
    })])
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
    r = _client(pipe, ["rpc", "task.create", json.dumps({
        "title": "P0 共享库 daemon 侧",
        "workspace_id": daemon["workspace_id"],
        "workspace_instance_id": daemon["workspace_instance_id"],
    })])
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
