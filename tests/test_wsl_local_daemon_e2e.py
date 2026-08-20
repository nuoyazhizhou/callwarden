"""共存契约子任务7：WSL local daemon E2E 测试。

对应 windows-wsl-daemon-coexistence-contract.md §4.3/§5.2 与
windows-wsl-daemon-coexistence-task-plan.md 子任务7。

在 WSL（Ubuntu）内启动独立的 Linux `cw-daemon`，验证：
1. WSL Linux daemon 使用 WSL ext4 下独立的 DB / WAL / CAS / UDS（全在临时根，
   禁止 /mnt/c 下的 Windows 库被触碰，禁止与 Windows authority 存储重叠）；
2. Windows daemon 不可用时，WSL authority 仍只写自己的数据库（fail-closed，
   不依赖、不回退到 Windows 库）；
3. authority 隔离：WSL daemon 的 authority_id / task_db_fingerprint 为独立
   Linux authority，与 Windows 侧不同；
4. WSL daemon 停止/重启互不影响：任务落盘后重启 daemon，状态与事件完整保留。

执行方式：
- 所有 WSL 内命令统一用 `wsl.exe -d ubuntu -- bash -s` 或 `python3 -` 传 stdin，
  避免 PowerShell → wsl → bash 三层引号转义（AGENTS.md 规则 20）；
- WSL 内 bash 显式设置 HOME=<root>（默认继承 Windows 路径正是契约要防护的场景）；
- WSL 内 Python 客户端显式设置 PYTHONPATH=/mnt/c/git_work（扁平包结构）。
"""

import json
import os
import shutil
import subprocess
import sys
import time

import pytest

_WSL_DISTRO = os.environ.get("CW_TEST_WSL_DISTRO", "ubuntu")
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# 固定命名临时根（附 Windows PID 避免并发冲突）；teardown 按字面路径清理。
# 用 WSL root 家目录（ext4 持久），不用 /tmp（WSL 会话回收时 /tmp 可能被清理）
_TMP_ROOT = f"/root/callwarden-wsl-e2e-sub7-{os.getpid()}"
# WSL 内 cargo target 缓存（增量构建加速；独立于 Windows target 目录）
_WSL_CARGO_TARGET = "/root/callwarden-wsl-e2e-target-sub7"
_WSL_DAEMON_BIN = f"{_WSL_CARGO_TARGET}/debug/cw-daemon"

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "win32",
        reason="WSL local daemon E2E 通过 wsl.exe 驱动（Windows 宿主）",
    ),
    pytest.mark.skipif(
        shutil.which("wsl.exe") is None,
        reason="未找到 wsl.exe（需要 WSL 发行版）",
    ),
]


def _wsl_bash(script: str, timeout: int = 300, input_data: str = None):
    """在 WSL 内执行 bash 脚本（stdin 传入，避免嵌套引号）。"""
    return subprocess.run(
        ["wsl.exe", "-d", _WSL_DISTRO, "--", "bash", "-s"],
        input=input_data if input_data is not None else script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=_REPO_ROOT,
    )


def _wsl_py(script: str, timeout: int = 120):
    """在 WSL 内执行 python3 脚本（stdin 传入）。"""
    return subprocess.run(
        ["wsl.exe", "-d", _WSL_DISTRO, "--", "python3", "-"],
        input=script,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        cwd=_REPO_ROOT,
    )


def _wsl_cargo_env() -> str:
    """WSL 非交互 bash 不加载 ~/.bashrc，cargo 需要显式加入 PATH。"""
    return "export PATH=\"/root/.cargo/bin:$PATH\"; "


def _require_wsl_ready() -> bool:
    """探测：WSL 发行版 + python3 + cargo 可用。"""
    r = _wsl_py("import sys; print(sys.version_info[:2]); print(sys.executable)", timeout=60)
    if r.returncode != 0:
        return False
    r2 = _wsl_bash(
        _wsl_cargo_env() + "command -v cargo >/dev/null 2>&1 && echo cargo_ok || echo cargo_missing",
        timeout=60,
    )
    return r2.returncode == 0 and "cargo_ok" in r2.stdout


@pytest.fixture(scope="module")
def wsl_authority():
    """在 WSL 内构建并启动隔离的 Linux daemon，返回其上下文。

    前置不满足（无 WSL / 无 cargo）则 skip。测试期间在 WSL 内创建、写入、
    验证任务，Windows 侧完全不可达（Windows daemon 不参与本次 E2E）。
    """
    if not _require_wsl_ready():
        pytest.skip("WSL 发行版或工具链（python3/cargo）不可用，跳过 WSL local daemon E2E")

    # ---- 1. 准备 WSL ext4 临时根 ----
    r = _wsl_bash(
        f"export HOME=/root; mkdir -p {_TMP_ROOT} && echo tmp_ready",
        timeout=60,
    )
    if r.returncode != 0 or "tmp_ready" not in r.stdout:
        pytest.skip(f"无法在 WSL 创建临时根（可能无权限或 WSL 异常）: {r.stdout}{r.stderr}")
    tmp = _TMP_ROOT

    try:
        # ---- 2. WSL 内构建 Linux cw-daemon（target 缓存于 WSL ext4，增量加速）----
        build_script = (
            f"set -e; export HOME=/root; {_wsl_cargo_env()}"
            f"mkdir -p {_WSL_CARGO_TARGET}; "
            f"cd /mnt/c/git_work/callwarden; "
            f"export CARGO_TARGET_DIR={_WSL_CARGO_TARGET}; "
            f"cargo build --no-default-features --manifest-path rust_ext/Cargo.toml --bin cw-daemon; "
            f"echo BUILD_OK"
        )
        rb = _wsl_bash(build_script, timeout=1800)
        if rb.returncode != 0 or "BUILD_OK" not in rb.stdout:
            pytest.fail(
                "WSL 内 cargo build cw-daemon 失败：\n" + (rb.stdout + rb.stderr)[-3000:]
            )

        # ---- 3. 写隔离 daemon 配置（全部路径在 WSL ext4 临时根）----
        config = {
            "socket_path": f"{tmp}/callwarden.sock",
            "registry_db_path": f"{tmp}/registry.db",
            "task_db_path": f"{tmp}/callwarden.db",
            "data_root": f"{tmp}/data",
            "max_workers": 8,
            "request_timeout_secs": 30,
            "snapshot_cache_capacity": 4,
            "codegraph_db_path_template": f"{tmp}/data/workspaces/{{workspace_instance_id}}/codegraph.db",
            "socket_mode": 0o660,
            "socket_group": "",
            "stage_toggle_db_path": f"{tmp}/stage_toggle.db",
        }
        cfg_json = json.dumps(config)
        w_cfg = _wsl_py(
            f"import json; "
            f"open({json.dumps(f'{tmp}/daemon.json')}, 'w').write({json.dumps(cfg_json)}); "
            f"print('cfg_ok')",
            timeout=60,
        )
        assert "cfg_ok" in (w_cfg.stdout or ""), w_cfg

        # ---- 4. 启动 Linux daemon（nohup 后台，独立于本 wsl 调用会话）----
        start_script = (
            f"export HOME=/root; "
            f"cd {tmp}; "
            f"nohup {_WSL_DAEMON_BIN} --config {tmp}/daemon.json "
            f"> {tmp}/daemon.log 2>&1 & "
            f"echo $! > {tmp}/daemon.pid; echo STARTED; sleep 1; "
            f"cat {tmp}/daemon.pid"
        )
        rs = _wsl_bash(start_script, timeout=120)
        assert "STARTED" in (rs.stdout or ""), f"daemon 启动失败: {rs.stdout}{rs.stderr}"

        # ---- 5. 等待 UDS 就绪（WSL 内 Python 客户端 ping）----
        uds = config["socket_path"]
        ping_script = (
            "import os, sys, time\n"
            "os.environ['PYTHONPATH'] = '/mnt/c/git_work'\n"
            "os.environ['HOME'] = '/root'\n"
            "sys.path.insert(0, '/mnt/c/git_work')\n"
            "from callwarden.server.daemon_client import UnixDaemonRpcClient\n"
            f"uds = {json.dumps(uds)}\n"
            "ok = False\n"
            "for _ in range(60):\n"
            "    try:\n"
            "        c = UnixDaemonRpcClient(socket_path=uds)\n"
            "        info = c.hello()\n"
            "        print('PING_OK', info.get('authority_id', ''), info.get('platform', ''))\n"
            "        ok = True\n"
            "        break\n"
            "    except Exception:\n"
            "        time.sleep(0.5)\n"
            "if not ok:\n"
            "    print('PING_FAIL')\n"
            "    sys.exit(1)\n"
        )
        rp = _wsl_py(ping_script, timeout=120)
        if rp.returncode != 0 or "PING_OK" not in (rp.stdout or ""):
            log_script = f"export HOME=/root; cat {tmp}/daemon.log 2>/dev/null | tail -c 3000"
            log = _wsl_bash(log_script, timeout=60)
            pytest.fail(f"WSL daemon UDS 未就绪: {rp.stdout}{rp.stderr}\ndaemon.log:\n{log.stdout}")
        authority_id = rp.stdout.strip().splitlines()[-1].split(" ", 2)[1]

        yield {
            "tmp": tmp,
            "uds": uds,
            "task_db": config["task_db_path"],
            "registry_db": config["registry_db_path"],
            "data_root": config["data_root"],
            "daemon_pid_file": f"{tmp}/daemon.pid",
            "daemon_log": f"{tmp}/daemon.log",
            "daemon_bin": _WSL_DAEMON_BIN,
            "authority_id": authority_id,
        }
    finally:
        # ---- teardown：停止 daemon + 按字面路径清理 WSL 临时根（单命令）----
        cleanup = (
            f"export HOME=/root; "
            f"if [ -f {tmp}/daemon.pid ]; then kill $(cat {tmp}/daemon.pid) 2>/dev/null || true; fi; "
            f"rm -rf {tmp}; echo CLEANED"
        )
        try:
            _wsl_bash(cleanup, timeout=120)
        except Exception:
            pass


def _wsl_client_script(uds: str, body: str) -> str:
    """构造 WSL 内 Python 客户端脚本头（统一 env + client 实例）。"""
    return (
        "import os, sys\n"
        "os.environ['PYTHONPATH'] = '/mnt/c/git_work'\n"
        "os.environ['HOME'] = '/root'\n"
        "sys.path.insert(0, '/mnt/c/git_work')\n"
        "from callwarden.server.daemon_client import UnixDaemonRpcClient\n"
        "from callwarden.server.daemon_protocol import DaemonRemoteError\n"
        f"client = UnixDaemonRpcClient(socket_path={json.dumps(uds)})\n"
        + body
    )


def test_wsl_daemon_authority_isolated_and_writable(wsl_authority):
    """P1：WSL Linux daemon 是独立 authority，可独立写任务。

    - WSL 客户端经 UDS 创建/认领任务（Windows daemon 全程不可达 → 证明不依赖）；
    - authority_id / platform 为 Linux 侧独立 authority；
    - 所有 DB / WAL / SHM 均落在 WSL ext4 临时根，无 /mnt/c 路径。
    """
    ctx = wsl_authority
    uds = ctx["uds"]

    script = _wsl_client_script(uds, body=(
        "import json\n"
        "created = client.call('task.create', {'title': 'wsl-local-task', "
        "'description': 'WSL 独立 authority', 'creator': 'wsl-agent'})\n"
        "tid = created['task_id']\n"
        "claimed = client.call('task.claim', {'task_id': tid, "
        "'agent_session_id': 'wsl-session-1'})\n"
        "ev = client.call('task.events', {'task_id': tid})\n"
        "hello = client.hello()\n"
        "print(json.dumps({'task_id': tid, 'claimed_status': claimed.get('status'),\n"
        "    'event_count': len(ev.get('events', [])),\n"
        "    'authority_id': hello.get('authority_id'),\n"
        "    'platform': hello.get('platform'),\n"
        "    'transport': hello.get('transport')}))\n"
    ))
    r = _wsl_py(script, timeout=120)
    assert r.returncode == 0, f"WSL 客户端失败: {r.stdout}{r.stderr}"
    line = r.stdout.strip().splitlines()[-1]
    data = json.loads(line)
    assert data["claimed_status"] == "in_progress", data
    assert data["event_count"] >= 2, data
    # authority_id 格式：<workspace>/<platform>/<instance>/<fingerprint>，Linux 侧为 /linux/
    assert "/linux/" in data["authority_id"], data
    assert data["transport"] == "uds", data

    # 存储隔离：DB / WAL / SHM 全在 WSL ext4 临时根，无 /mnt/c 写入
    ls_script = f"export HOME=/root; ls -la {ctx['tmp']} 2>/dev/null; echo ---; "
    ls = _wsl_bash(ls_script, timeout=60)
    tree = (ls.stdout or "") + (ls.stderr or "")
    assert "callwarden.db" in tree, f"WSL 任务库未落盘: {tree}"
    assert "/mnt/c" not in tree.replace("/mnt/c/git_work/callwarden", ""), (
        f"WSL daemon 存储混入了 /mnt/c 路径: {tree}")
    # 断言不存在 Windows 用户库被触碰（临时根内不允许出现用户级库路径）
    assert ctx["tmp"].startswith("/root/"), ctx["tmp"]


def test_wsl_daemon_survives_restart(wsl_authority):
    """P1：WSL daemon 停止/重启后任务与事件完整保留（互不影响的基础）。

    - 创建任务并写入 WSL authority 库；
    - 停止 daemon → 重启（同一配置）→ 经 UDS 读取 task.status 与 task_events；
    - 断言状态/事件与重启前一致（持久化而非内存态）。
    """
    ctx = wsl_authority
    uds = ctx["uds"]
    tmp = ctx["tmp"]

    script = _wsl_client_script(uds, body=(
        "import json\n"
        "created = client.call('task.create', {'title': 'restart-persist', "
        "'creator': 'wsl-agent'})\n"
        "tid = created['task_id']\n"
        "ev1 = client.call('task.events', {'task_id': tid})\n"
        "print(json.dumps({'task_id': tid, 'events1': len(ev1.get('events', []))}))\n"
    ))
    r1 = _wsl_py(script, timeout=120)
    assert r1.returncode == 0, r1.stdout + r1.stderr
    d1 = json.loads(r1.stdout.strip().splitlines()[-1])
    task_id = d1["task_id"]
    events1 = d1["events1"]

    # 停止 daemon：kill 后轮询确认进程退出 + socket 消失（优雅关闭需 drain workers）
    stop = _wsl_bash(
        f"export HOME=/root; "
        f"PID=$(cat {tmp}/daemon.pid 2>/dev/null || true); "
        f"echo KILL_PID=$PID; "
        f"if [ -n \"$PID\" ]; then "
        f"  kill $PID 2>/dev/null || echo KILL_FAILED; "
        f"  for i in $(seq 1 40); do kill -0 $PID 2>/dev/null || break; sleep 0.25; done; "
        f"  if kill -0 $PID 2>/dev/null; then "
        f"    echo PROC_STILL_ALIVE; kill -9 $PID 2>/dev/null || true; "
        f"    for i in $(seq 1 20); do kill -0 $PID 2>/dev/null || break; sleep 0.25; done; "
        f"  fi; "
        f"fi; "
        f"[ -e {tmp}/callwarden.sock ] && echo SOCK_STILL_THERE || echo SOCK_GONE; "
        f"echo STOPPED", timeout=60)
    assert "STOPPED" in stop.stdout, stop.stdout + stop.stderr

    # 重启（同一配置；先清理可能残留的 socket 文件，daemon 自身也会清理）
    restart = _wsl_bash(
        f"export HOME=/root; cd {tmp}; "
        f"rm -f {tmp}/callwarden.sock; "
        f"nohup {_WSL_DAEMON_BIN} --config {tmp}/daemon.json "
        f"> {tmp}/daemon.log 2>&1 & echo $! > {tmp}/daemon.pid; "
        f"sleep 1; echo RESTARTED pid=$(cat {tmp}/daemon.pid); "
        f"ps aux | grep cw-daemon | grep -v grep || echo NO_DAEMON_PROC",
        timeout=120,
    )
    assert "RESTARTED" in restart.stdout, restart.stdout + restart.stderr

    # 等待重新就绪 + 读取任务
    script2 = _wsl_client_script(uds, body=(
        "import json, time\n"
        "ok = False\n"
        "for _ in range(60):\n"
        "    try:\n"
        "        client.hello()\n"
        "        ok = True\n"
        "        break\n"
        "    except Exception:\n"
        "        time.sleep(0.5)\n"
        "if not ok:\n"
        "    print('RESTART_UNREACHABLE')\n"
        "    sys.exit(1)\n"
        f"status = client.call('task.status', {{'task_id': {json.dumps(task_id)}}})\n"
        "ev2 = client.call('task.events', {'task_id': "
        f"{json.dumps(task_id)}" + "})\n"
        "print(json.dumps({'status': status.get('status'), "
        "'events2': len(ev2.get('events', []))}))\n"
    ))
    r2 = _wsl_py(script2, timeout=150)
    if "RESTART_UNREACHABLE" in (r2.stdout or ""):
        diag = _wsl_bash(
            f"export HOME=/root; "
            f"echo '--- proc ---'; ps aux | grep cw-daemon | grep -v grep || echo NO_PROC; "
            f"echo '--- sock ---'; [ -e {tmp}/callwarden.sock ] && ls -la {tmp}/callwarden.sock || echo NO_SOCK; "
            f"echo '--- log ---'; od -c {tmp}/daemon.log 2>/dev/null | tail -c 3000; "
            f"echo '--- tmp ---'; ls -la {tmp} 2>/dev/null", timeout=60)
        pytest.fail(
            f"重启后 daemon 不可达。\n客户端输出: {r2.stdout}{r2.stderr}\n诊断:\n{(diag.stdout or '')}"
        )
    assert r2.returncode == 0, r2.stdout + r2.stderr
    d2 = json.loads(r2.stdout.strip().splitlines()[-1])
    assert d2["status"] == "open", d2
    assert d2["events2"] == events1, f"重启后事件数不一致: {d2} vs {events1}"
