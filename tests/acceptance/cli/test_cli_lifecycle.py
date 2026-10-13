"""TP-10C · 生命周期验收（常驻/阻塞入口：server / daemon start）。

selector: tests/acceptance/cli/test_cli_lifecycle.py

对常驻/阻塞入口做**全生命周期**验证：
  1. spawn（subprocess + 超时）；
  2. 断言抵达 READY 信号（daemon start → manifest 发布 + JSON endpoint；
     server → stdio 握手响应 initialize）；
  3. 一次受控交互（GET /health 健康探针 / MCP initialize 应答）；
  4. 干净停止；
  5. 断言进程已退出、无孤儿进程、run 根内无端口/锁残留。

覆盖范围说明：冻结清单（frozen-inventory/v1，265 CLI 入口）中的常驻/阻塞
入口为 `server`（cw_py_standalone）与 `daemon start`（nested_subcommand_
level3）。任务描述提到的 `watch` 在冻结 CLI 面板中不存在可分发入口
（仅为 cli/main.py `_WRITE_FLAGS` 中的遗留 flag 名，无 add_parser/handler），
故不纳入；`server` 与 `daemon start` 即冻结面上全部常驻入口。
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from cli_acceptance_harness import (  # noqa: E402
    CW_PY,
    CLIAcceptanceEnv,
    PYTHON,
    RUN_ROOT,
)

# /health 返回字段（TP-10C wire 事实：无 "ok" 字段）
_HEALTH_FIELDS = ("security_profile", "endpoint", "pid", "git_commit",
                  "schema_version", "worker_status", "capability_registry_revision")


@pytest.fixture(scope="module")
def env():
    """主隔离环境：供 server 的 daemon 启动探针使用。"""
    e = CLIAcceptanceEnv()
    e.start_daemon()
    e.seed_workspace()
    try:
        yield e
    finally:
        e.stop_daemon()


def _taskkill(pid: int) -> None:
    """Windows 下按 PID 终止进程（daemon start 拉起的是 detached 进程）。"""
    try:
        os.kill(pid, signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass
    for _ in range(20):
        try:
            os.kill(pid, 0)
        except OSError:
            return
        time.sleep(0.2)
    try:
        subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                       capture_output=True, timeout=10)
    except Exception:
        pass


def _get(url: str, timeout: float = 5.0):
    """GET，返回 (status, json) 或在连接失败时返回 (None, error)。"""
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            body = r.read().decode("utf-8", "replace")
            try:
                return r.status, json.loads(body)
            except ValueError:
                return r.status, body
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace") if exc.fp else ""
        return exc.code, body
    except OSError as exc:
        return None, exc


# =====================================================================
# cw daemon start —— 显式拉起隔离 HTTP daemon（detached）
# =====================================================================
def test_daemon_start_reaches_ready_and_stops_cleanly(env):
    """daemon start 在无 manifest 的隔离根内 spawn → READY → 交互 → 干净停止。"""
    fresh = RUN_ROOT / "lifecycle" / "dstart"
    if fresh.exists():
        shutil.rmtree(fresh, ignore_errors=True)
    fresh.mkdir(parents=True, exist_ok=True)
    src_exe = fresh / "src" / "cw-daemon.exe"
    src_exe.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(env.daemon_exe, src_exe)
    home = fresh / "home"
    home.mkdir(parents=True, exist_ok=True)

    reg_db = str(home / ".callwarden" / "daemon" / "registry.db")
    run_env = dict(os.environ)
    run_env.update({
        "USERPROFILE": str(home), "HOME": str(home),
        "CALLWARDEN_HOME": str(home),
        "CALLWARDEN_DB": str(home / ".callwarden" / "callwarden.db"),
        "CALLWARDEN_DAEMON_REGISTRY_DB": reg_db,
        "CW_DAEMON_REGISTRY_DB": reg_db,
        "CW_DAEMON_BIN": str(src_exe),
        "CALLWARDEN_AUTO_SETUP": "0",
        "CALLWARDEN_LANG": "en_US",
        "PYTHONUTF8": "1",
    })
    for k in ("CW_DAEMON_BINARY", "CW_DAEMON_TRANSPORT", "CW_DAEMON_HTTP_BIND",
              "CW_DAEMON_HTTP_ENDPOINT"):
        run_env.pop(k, None)

    # ---- 1. spawn ----
    proc = subprocess.run(
        [PYTHON, "-u", str(CW_PY), "daemon", "start", "--wait", "90"],
        cwd=str(fresh), env=run_env, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=150)
    assert proc.returncode == 0, (
        f"daemon start 失败 rc={proc.returncode}\n"
        f"stdout={proc.stdout[:600]}\nstderr={proc.stderr[:600]}")

    # ---- 2. READY 信号：JSON 报告 started + endpoint（可能多行美化打印） ----
    payload = None
    decoder = json.JSONDecoder()
    for i, ch in enumerate(proc.stdout):
        if ch == "{":
            try:
                payload, _end = decoder.raw_decode(proc.stdout[i:])
                break
            except ValueError:
                continue
    assert payload and payload.get("ok") is True, (
        f"daemon start 未报告 ok=true：{payload}\n{proc.stdout[:400]}")
    assert payload.get("endpoint", "").startswith("http://127.0.0.1:"), (
        f"endpoint 非 loopback：{payload}")
    endpoint = payload["endpoint"]

    # ---- 3. 受控交互：GET /health ----
    status, health = _get(endpoint + "/health", timeout=10.0)
    assert status == 200, f"/health 未返回 200：status={status} body={str(health)[:300]}"
    assert isinstance(health, dict), f"/health 非 JSON：{str(health)[:200]}"
    for field in ("endpoint", "pid", "schema_version"):
        assert field in health, f"/health 缺少字段 {field}：{list(health.keys())}"
    assert "ok" not in health, f'/health 不应含 "ok" 字段（wire 事实）：{health}'

    # ---- 4. 干净停止：从 manifest 取 PID 终止 ----
    manifests = list((home / ".callwarden").glob("http-daemon.*.manifest.json"))
    assert manifests, "daemon start 未在隔离根内发布 manifest"
    manifest = json.loads(manifests[0].read_text(encoding="utf-8"))
    daemon_pid = manifest.get("pid")
    assert daemon_pid and manifest.get("endpoint") == endpoint, (
        f"manifest 与启动 endpoint 不一致：{manifest}")
    _taskkill(daemon_pid)

    # ---- 5a. 无孤儿：停止后 endpoint 拒绝连接 ----
    deadline = time.time() + 15
    refused = False
    while time.time() < deadline:
        st, _ = _get(endpoint + "/health", timeout=3.0)
        if st is None:
            refused = True
            break
        time.sleep(0.4)
    assert refused, f"daemon 停止后 {endpoint} 仍可连接（孤儿进程）"

    # ---- 5b. 无残留越界：manifest/库均在 run 根内 ----
    assert str(manifests[0]).startswith(str(RUN_ROOT)), (
        f"manifest 落在 run 根外：{manifests[0]}")
    assert str(src_exe).startswith(str(RUN_ROOT)), f"daemon exe 落在 run 根外：{src_exe}"


# =====================================================================
# cw server —— stdio MCP 常驻服务
# =====================================================================
def test_server_check_imports_self_check(env):
    """server --check-imports：注册全部 MCP 工具后即退出（非阻塞自检）。"""
    r = env.run_cli(["server", "--check-imports"], timeout=180.0)
    assert r["rc"] == 0, (
        f"server --check-imports 失败 rc={r['rc']}\n"
        f"stdout={r['stdout'][:400]}\nstderr={r['stderr'][:400]}")
    assert "Call Warden MCP imports OK" in r["stdout"], (
        f"缺少就绪暗号：{r['stdout'][:300]}")


def test_server_stdio_handshake_and_clean_stop(env):
    """cw server（stdio）：initialize 握手应答（READY）→ 干净停止 → 无孤儿。"""
    srv_env = env.cli_env()
    proc = subprocess.Popen(
        [PYTHON, "-u", str(CW_PY), "server"],
        cwd=str(env.seed_dir), env=srv_env,
        stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, text=True, encoding="utf-8",
        errors="replace", bufsize=1)

    lines: list[str] = []

    def _pump():
        assert proc.stdout is not None
        for line in proc.stdout:
            lines.append(line)

    reader = threading.Thread(target=_pump, daemon=True)
    reader.start()

    try:
        # ---- 受控交互 = READY 信号：发 JSON-RPC initialize ----
        init_req = json.dumps({
            "jsonrpc": "2.0", "id": "tp10c-init", "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "tp10c-lifecycle", "version": "1.0"},
            },
        }) + "\n"
        assert proc.stdin is not None
        proc.stdin.write(init_req)
        proc.stdin.flush()

        deadline = time.time() + 30.0
        got = None
        while time.time() < deadline:
            for line in list(lines):
                if '"result"' in line and "tp10c-init" in line:
                    got = line
                    break
            if got:
                break
            time.sleep(0.3)
        assert got, (
            f"server 未在 30s 内应答 initialize（READY 信号缺失）\n"
            f"stdout_lines={lines[:10]}\nstderr={_drain(proc)[:600]}")
        resp = json.loads(got)
        assert resp.get("jsonrpc") == "2.0" and resp.get("id") == "tp10c-init", (
            f"initialize 应答不规范：{resp}")
        result = resp.get("result") or {}
        assert "protocolVersion" in result and "serverInfo" in result, (
            f"initialize result 缺少协议版本/服务端信息：{result}")

        # ---- 干净停止：发 notifications/initialized 后关闭 stdin ----
        try:
            proc.stdin.write(json.dumps({
                "jsonrpc": "2.0", "method": "notifications/initialized",
            }) + "\n")
            proc.stdin.flush()
        except OSError:
            pass
        try:
            proc.stdin.close()
        except OSError:
            pass
    finally:
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    # ---- 退出 + 无孤儿 ----
    assert proc.returncode is not None, "server 进程未退出"
    assert proc.returncode is not None and proc.poll() is not None
    reader.join(timeout=3)
    assert proc.poll() is not None, "server 仍存活（孤儿进程）"


def _drain(proc) -> str:
    """非阻塞读取 stderr 的已有内容（用于失败诊断）。"""
    try:
        import threading as _th
        buf: list[str] = []

        def _rd():
            assert proc.stderr is not None
            for line in proc.stderr:
                buf.append(line)
        t = _th.Thread(target=_rd, daemon=True)
        t.start()
        t.join(2.0)
        return "".join(buf)
    except Exception:
        return ""
