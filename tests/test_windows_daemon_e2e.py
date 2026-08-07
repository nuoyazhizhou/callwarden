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
