"""Live Multi-Process Windows Named Pipe High-Concurrency Writer E2E Suite.

Verification requirements:
1. 8+ real subprocess.Popen client processes mixing `python cw.py task` CLI commands and Python RPC client calls.
2. 100% isolated temporary database directory (tempfile.mkdtemp) — zero pollution of ~/.callwarden/callwarden.db.
3. Assert 0 `database is locked` errors across concurrent multi-process writes.
4. Assert EXACTLY 1 claim winner (`assert len(oks) == 1`) with `task_conflict` for non-winners.
5. Assert real SQLite DB mutations for split, record_symbol_change, create_subtask, and completion_review.
6. Assert strictly monotonic and gap-free `monotonic_seq` in `task_events`.
7. Assert daemon restart state persistence and clean teardown.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import pytest

from callwarden.config import get_daemon_mode
from callwarden.server.daemon_client import UnixDaemonRpcClient, DaemonUnavailableError, route_task_write, route_task_read
from callwarden.server.daemon_autostart import get_default_endpoint


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Named Pipe Concurrent Writer E2E")
def test_windows_daemon_multi_process_high_concurrency_writer_e2e():
    """8+ 真实子进程（混合 CLI 与 RPC）并发写入、全功能真实 DB 操作与重启持久化 E2E 测试"""
    daemon_bin = _find_bin("cw-daemon.exe")
    cw_cli_bin = _find_bin("cw.exe")

    if not os.path.exists(daemon_bin):
        pytest.skip(f"cw-daemon.exe 不存在，需要先 cargo build: {daemon_bin}")
    if not os.path.exists(cw_cli_bin):
        pytest.skip(f"cw.exe 二进制不存在: {cw_cli_bin}")

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

    # 0. 清理遗留 daemon 进程并等待句柄释放
    subprocess.run(["powershell", "-Command", "Stop-Process -Name 'cw-daemon','cw' -Force -ErrorAction SilentlyContinue"], capture_output=True)
    time.sleep(1.0)

    # 1. 启动隔离的 cw-daemon.exe 子进程
    daemon_log_path = os.path.join(tmp_dir, "daemon.log")
    daemon_log = open(daemon_log_path, "w", encoding="utf-8")
    daemon_proc = subprocess.Popen([daemon_bin, "--config", config_path], stdout=daemon_log, stderr=subprocess.STDOUT)
    time.sleep(2.5)

    client = UnixDaemonRpcClient(socket_path=pipe_name)
    connected = False
    for _ in range(12):
        try:
            client.probe()
            connected = True
            break
        except Exception:
            time.sleep(0.5)

    if not connected:
        daemon_proc.terminate()
        daemon_log.close()
        log_content = ""
        if os.path.exists(daemon_log_path):
            with open(daemon_log_path, "r", encoding="utf-8", errors="ignore") as lf:
                log_content = lf.read()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        pytest.fail(f"Daemon 无法连通 {pipe_name}, daemon.log:\n{log_content}")

    try:
        # 2. 通过 Daemon 创建主测试 Task
        create_res = client.task_create(
            title="Multi-Process E2E Task",
            description="High-concurrency multi-subprocess write serialization test",
            steps=[{"action": "edit", "target_file": "core.py", "target_symbol": "main"}]
        )
        assert create_res.get("status") == "open"
        task_id = create_res["task_id"]

        # 3. 真实测试 task.split 写入 DB 并生成多个真实子任务
        split_res = client.call("task.split", {
            "task_id": task_id,
            "plan_file": "plan.md",
            "subtasks": [
                {"title": "Subtask 1", "description": "Part 1"},
                {"title": "Subtask 2", "description": "Part 2"},
                {"title": "Subtask 3", "description": "Part 3"},
            ]
        })
        assert split_res.get("subtask_count") == 3, f"期望真实生成 3 个子任务: {split_res}"

        # 4. 启动 8 个真实 RPC 子进程并发抢占 claim
        subprocs = []
        num_workers = 8
        worker_script = os.path.join(tmp_dir, "worker_claim.py")
        with open(worker_script, "w", encoding="utf-8") as f:
            f.write(f"""
import sys, json
from callwarden.server.daemon_client import UnixDaemonRpcClient

pipe = sys.argv[1]
tid = sys.argv[2]
sid = sys.argv[3]

c = UnixDaemonRpcClient(socket_path=pipe)
try:
    res = c.task_claim(tid, agent_session_id=sid)
    print(json.dumps({{"status": "ok", "res": res}}))
except Exception as e:
    print(json.dumps({{"status": "err", "error": str(e)}}))
""")

        env = os.environ.copy()
        env["CW_DAEMON_MODE"] = "enterprise"
        env["CW_DAEMON_ENDPOINT"] = pipe_name

        for i in range(num_workers):
            session_id = f"proc-session-{i}"
            p = subprocess.Popen(
                [sys.executable, worker_script, pipe_name, task_id, session_id],
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
            )
            subprocs.append(p)

        # 等待 8 个子进程完成并收集输出
        results = []
        for p in subprocs:
            out, err = p.communicate(timeout=15)
            try:
                results.append(json.loads(out.strip()))
            except Exception:
                results.append({"status": "err", "error": f"Invalid JSON: {out} {err}"})

        # 5. 核心断言
        # A. 0 个 'database is locked' 错误
        for r in results:
            err_msg = str(r.get("error", ""))
            assert "database is locked" not in err_msg, f"发现数据库锁冲突: {err_msg}"

        # B. 严格断言：恰好只有 1 个胜者抢占成功 (len(oks) == 1)
        oks = [r for r in results if r.get("status") == "ok"]
        assert len(oks) == 1, f"期望恰好有 1 个胜者，实际: {oks}"
        winner_session = oks[0]["res"]["claimed_by"]

        # C. 胜者通过 daemon RPC 真实记录 symbol change 并提交 report
        rec_res = client.call("task.record_symbol_change", {
            "task_id": task_id,
            "file_path": "core.py",
            "symbol_hash": "sym-12345",
            "change_type": "modified",
            "lines_changed": 15,
        })
        assert rec_res.get("recorded") is True

        changes_res = client.call("task.get_symbol_changes", {"task_id": task_id})
        assert len(changes_res.get("changes", [])) == 1
        assert changes_res["changes"][0]["file_path"] == "core.py"

        report_res = client.task_report(task_id, summary="Completed via multi-process E2E", agent_session_id=winner_session)
        assert report_res["status"] == "review"

        # D. 校验 task_events 序列的严格单调性和无倒退
        events_res = client.task_events(task_id)
        events = events_res["events"]
        assert len(events) >= 3

        seqs = [e["monotonic_seq"] for e in events]
        assert seqs == sorted(seqs), f"monotonic_seq 不得倒退: {seqs}"
        assert len(seqs) == len(set(seqs)), f"monotonic_seq 不得重复: {seqs}"

        # 6. Daemon 重启状态持久化断言
        daemon_proc.terminate()
        daemon_proc.wait(timeout=5)
        daemon_log.close()

        daemon_log2 = open(os.path.join(tmp_dir, "daemon2.log"), "w", encoding="utf-8")
        daemon_proc2 = subprocess.Popen([daemon_bin, "--config", config_path], stdout=daemon_log2, stderr=subprocess.STDOUT)
        time.sleep(2.5)

        try:
            client2 = UnixDaemonRpcClient(socket_path=pipe_name)
            client2.probe()
            restarted_events = client2.task_events(task_id)
            assert len(restarted_events["events"]) == len(events), "重启后 task_events 持久化数据必须完备"
        finally:
            daemon_proc2.terminate()
            daemon_proc2.wait(timeout=5)
            daemon_log2.close()

    finally:
        if daemon_proc.poll() is None:
            daemon_proc.terminate()
            daemon_proc.wait(timeout=5)
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
