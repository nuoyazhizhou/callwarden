"""Phase 4 / D2 行为级验收（根修卡 T-1795507699058-41d9fcbc）：
长写事务期间并发只读 RPC 不阻塞。

背景：reviewer 总复评（evidence/T-1795507699058-41d9fcbc/
reviewer-verdict-rootfix.json uncovered_paths[0]）明确记录——
「D2（读写分离）长写事务期间并发读不阻塞的行为级验收不在本 step#4
证据内（本卡 D2 由 Step2 修复标记 + 现有单测覆盖，未做 D1 同规格的
独立行为级实测）——本评审未验证，不默认其成立」。
本文件以 D1 同规格（w3_live 隔离 daemon + HTTP 端到端探测 + <2s 硬线
断言 + 实测延迟留痕）补齐 D2 证据。

被测机制（HEAD 源码锚点）：
- rust_ext/src/daemon/workspace.rs `WorkspaceRegistry.read_conn()`：
  只读连接惰性缓存（WAL 快照读），与写连接 conn 并存互不阻塞
  （Step2 修复；只读打开失败 fail-closed 不回退写连接）；
- rust_ext/src/daemon/workspace.rs `list_unified_workspaces`：
  registry 侧走 `WorkspaceRegistry::open_readonly`（独立只读连接）；
- rust_ext/src/db_conn.rs：busy_timeout=5s + journal_mode=WAL +
  synchronous=NORMAL；
- task.db 读路径（task.list / workspace.list 的 task-DB 段）走 WAL
  快照读，外部长写事务（未提交）不阻塞读。

场景（三个测试）：
1. registry.db 长写事务（外部连接 BEGIN IMMEDIATE + UPDATE 不 commit，
   持有 4s）期间：/health + workspace.list（open_readonly 新连接）+
   workspace.status（缓存 read_conn）+ task.list 并发探测零失败、
   最大延迟 <2s，且读到的 status 为已提交快照（非未提交值）；
2. task.db 长写事务（INSERT 未提交行，持有 4s）期间：同上探测零失败、
   <2s，task.list 看不到未提交行；
3. 写后可见：长事务 COMMIT 后只读 RPC 立即读到新值（快照语义正确性，
   排除「读不到新数据」式的假不阻塞）。

隔离合规：w3_live fixture（动态端口 127.0.0.1:0 + tmp data_root +
USERPROFILE 重定向 + 独立 task.db/registry.db + 独立 mTLS），
绝不触碰共享 daemon，不重启任何共享实例。

判定口径（六条件）：本文件为 D2 行为级证据源，配合 junit XML 与
evidence/T-1795507699058-41d9fcbc/phase4-d2-evidence.md 判读。
"""

import os
import sqlite3
import threading
import time

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient

# 与 test_refresh_build_availability.py 同款：显式覆盖 CW_DAEMON_BIN。
# find_daemon_binary 按 mtime 取最新候选（debug 构建含 Step1/Step2 修复），
# 与 D1 验收实测选中的二进制一致。
_WORKTREE_BIN = os.path.abspath(os.path.join(
    os.path.dirname(os.path.realpath(__file__)), os.pardir, "rust_ext",
    "target", "release", "cw-daemon.exe"))
os.environ["CW_DAEMON_BIN"] = _WORKTREE_BIN

# D2 验收硬线：长写事务期间只读 RPC 端到端延迟 <2s（与 D1 同规格）。
_D2_BOUND_SECS = 2.0
# 长写事务持有窗口：远大于探测间隔（0.1s），保证事务期间 ≥8 次探测。
_LONG_TX_HOLD_SECS = 4.0
_PROBE_INTERVAL_SECS = 0.1
_PROBE_TIMEOUT_SECS = 5.0
# 探测次数下限（防假绿：线程没跑/事务窗口没覆盖都算无效证据）。
_MIN_PROBES = 8

_PROBE_KEYS = ("health", "ws", "wsst", "task")
_PROBE_LABELS = {
    "health": "/health",
    "ws": "workspace.list",
    "wsst": "workspace.status",
    "task": "task.list",
}


def _new_stats(inst):
    stats = {"inst": inst}
    for key in _PROBE_KEYS:
        stats[f"{key}_ok"] = 0
        stats[f"{key}_fail"] = []
        stats[f"{key}_lat"] = []
    stats["last_ws_list"] = None
    stats["last_task_list"] = None
    return stats


def _probe_loop(stop_evt, endpoint, stats):
    """并发探测线程：/health + workspace.list + workspace.status + task.list。

    workspace.list → list_unified_workspaces：registry 走 open_readonly
    新连接 + task-DB capture 读；
    workspace.status → unified_workspace_authority：registry 缓存 read_conn；
    task.list → task-DB WAL 快照读（workspace_id=1 与 D1 探测同参数）。
    """
    rpc_client = HttpDaemonRpcClient(endpoint=endpoint, verify_health=False,
                                     validate_manifest=False,
                                     timeout=_PROBE_TIMEOUT_SECS)
    while not stop_evt.is_set():
        t = time.monotonic()
        try:
            rpc_client.verify_health(endpoint=endpoint)
            stats["health_ok"] += 1
            stats["health_lat"].append(time.monotonic() - t)
        except Exception as exc:  # noqa: BLE001
            stats["health_fail"].append(f"{time.time():.3f} {exc}")

        t = time.monotonic()
        try:
            stats["last_ws_list"] = rpc_client.call("workspace.list", {})
            stats["ws_ok"] += 1
            stats["ws_lat"].append(time.monotonic() - t)
        except Exception as exc:  # noqa: BLE001
            stats["ws_fail"].append(f"{time.time():.3f} {exc}")

        t = time.monotonic()
        try:
            rpc_client.call("workspace.status",
                            {"workspace_instance_id": stats["inst"]})
            stats["wsst_ok"] += 1
            stats["wsst_lat"].append(time.monotonic() - t)
        except Exception as exc:  # noqa: BLE001
            stats["wsst_fail"].append(f"{time.time():.3f} {exc}")

        t = time.monotonic()
        try:
            stats["last_task_list"] = rpc_client.call(
                "task.list", {"status": "", "limit": 10, "workspace_id": 1})
            stats["task_ok"] += 1
            stats["task_lat"].append(time.monotonic() - t)
        except Exception as exc:  # noqa: BLE001
            stats["task_fail"].append(f"{time.time():.3f} {exc}")

        stop_evt.wait(_PROBE_INTERVAL_SECS)


def _assert_unblocked(stats, phase):
    """统一断言：探测零失败 + 次数充足 + 各端点最大延迟 <2s（D2 硬线）。"""
    for key in _PROBE_KEYS:
        assert not stats[f"{key}_fail"], (
            f"{phase} {PROBE_LABELS[key]} 探测失败 {len(stats[f'{key}_fail'])} 次: "
            f"{stats[f'{key}_fail'][:3]}")
        assert stats[f"{key}_ok"] >= _MIN_PROBES, (
            f"{phase} {PROBE_LABELS[key]} 探测次数过少（假绿防）: "
            f"{stats[f'{key}_ok']}")
    maxima = {}
    for key in _PROBE_KEYS:
        maxima[key] = max(stats[f"{key}_lat"])
        assert maxima[key] < _D2_BOUND_SECS, (
            f"{phase} {PROBE_LABELS[key]} 最大延迟 {maxima[key]:.3f}s >= "
            f"D2 硬线 {_D2_BOUND_SECS}s: {stats[f'{key}_lat']}")
    return maxima


def _fmt_maxima(maxima, stats):
    parts = [f"{_PROBE_LABELS[k]} max={maxima[k]:.3f}s(ok={stats[f'{k}_ok']})"
             for k in _PROBE_KEYS]
    return " ".join(parts)


def test_registry_long_write_tx_readonly_rpc_unblocked(w3_live):
    """registry.db 长写事务（未提交）期间，只读 RPC 不阻塞且读到已提交快照。"""
    client = w3_live["client"]
    endpoint = w3_live["endpoint"]
    registry_db = os.path.join(w3_live["data_root"], "registry.db")

    baseline = client.call("workspace.list", {})
    assert isinstance(baseline, list) and baseline, (
        f"workspace.list 基线为空: {baseline!r}")
    committed_status = baseline[0].get("status")
    assert committed_status, f"基线 status 为空: {baseline!r}"

    tx = sqlite3.connect(registry_db, timeout=10)
    try:
        tx.execute("BEGIN IMMEDIATE")
        tx.execute(
            "UPDATE daemon_workspaces SET status='d2_uncommitted_probe'")
        # 故意不 commit：长写事务持有窗口（WAL 写锁），期间并发只读探测。
        stats = _new_stats(w3_live["inst"])
        stop_evt = threading.Event()
        prober = threading.Thread(
            target=_probe_loop, args=(stop_evt, endpoint, stats), daemon=True)
        prober.start()
        try:
            time.sleep(_LONG_TX_HOLD_SECS)
        finally:
            stop_evt.set()
            prober.join(timeout=15)
            tx.execute("ROLLBACK")
    finally:
        tx.close()

    maxima = _assert_unblocked(stats, "registry 长写事务")
    print(f"[D2 实测/registry 长写事务] {_fmt_maxima(maxima, stats)}")

    # 快照一致性：事务持有期间的 workspace.list 只读到已提交值，
    # 不得出现未提交的 'd2_uncommitted_probe'。
    assert stats["last_ws_list"] is not None, "探测线程未取得 workspace.list 返回"
    statuses = [e.get("status") for e in stats["last_ws_list"]
                if isinstance(e, dict)]
    assert statuses, f"workspace.list 返回异常: {stats['last_ws_list']!r}"
    assert "d2_uncommitted_probe" not in statuses, (
        f"只读 RPC 读到了未提交写（快照语义破坏）: {statuses}")
    assert committed_status in statuses, (
        f"已提交快照不可见: committed={committed_status} got={statuses}")


def test_task_db_long_write_tx_readonly_rpc_unblocked(w3_live):
    """task.db 长写事务（未提交 INSERT）期间，只读 RPC 不阻塞且无脏读。"""
    client = w3_live["client"]
    endpoint = w3_live["endpoint"]
    task_db = os.path.join(w3_live["data_root"], "task.db")

    baseline = client.call(
        "task.list", {"status": "", "limit": 50, "workspace_id": 1})
    assert isinstance(baseline, dict) and "tasks" in baseline, (
        f"task.list 基线异常: {type(baseline)} {baseline!r}")

    tx = sqlite3.connect(task_db, timeout=10)
    try:
        tx.execute("BEGIN IMMEDIATE")
        tx.execute(
            "INSERT INTO tasks (id, title, description, creator, status,"
            " created_at, updated_at, parent_id)"
            " VALUES ('T-D2-UNCOMMITTED-PROBE', 'd2 probe', '', 'd2-test',"
            " 'open', ?, ?, '')",
            (time.time(), time.time()))
        # 故意不 commit：长写事务持有窗口，期间并发只读探测。
        stats = _new_stats(w3_live["inst"])
        stop_evt = threading.Event()
        prober = threading.Thread(
            target=_probe_loop, args=(stop_evt, endpoint, stats), daemon=True)
        prober.start()
        try:
            time.sleep(_LONG_TX_HOLD_SECS)
        finally:
            stop_evt.set()
            prober.join(timeout=15)
            tx.execute("ROLLBACK")
    finally:
        tx.close()

    maxima = _assert_unblocked(stats, "task.db 长写事务")
    print(f"[D2 实测/task.db 长写事务] {_fmt_maxima(maxima, stats)}")

    # 快照一致性：事务持有期间的 task.list 不得出现未提交行。
    assert stats["last_task_list"] is not None, "探测线程未取得 task.list 返回"
    tasks = stats["last_task_list"].get("tasks") or []
    ids = [t.get("task_id") for t in tasks if isinstance(t, dict)]
    assert "T-D2-UNCOMMITTED-PROBE" not in ids, (
        f"只读 RPC 读到了未提交写（脏读）: {ids}")


def test_committed_write_visible_to_readonly_rpc_after_tx(w3_live):
    """写后可见：长事务 COMMIT 后只读 RPC 立即读到新值（快照语义正确性）。"""
    client = w3_live["client"]
    registry_db = os.path.join(w3_live["data_root"], "registry.db")

    tx = sqlite3.connect(registry_db, timeout=10)
    try:
        tx.execute("BEGIN IMMEDIATE")
        tx.execute(
            "UPDATE daemon_workspaces SET status='d2_committed_visible'")
        tx.commit()

        t0 = time.monotonic()
        rows = client.call("workspace.list", {})
        elapsed = time.monotonic() - t0
        statuses = [e.get("status") for e in rows if isinstance(e, dict)]
        assert "d2_committed_visible" in statuses, (
            f"COMMIT 后只读 RPC 未读到新值（快照停滞）: {statuses}")
        assert elapsed < _D2_BOUND_SECS, (
            f"COMMIT 后首次只读 RPC 耗时 {elapsed:.3f}s >= {_D2_BOUND_SECS}s")
        print(f"[D2 实测/写后可见] COMMIT 后 workspace.list 耗时 "
              f"{elapsed:.3f}s，status={statuses}")
    finally:
        tx.execute("BEGIN IMMEDIATE")
        tx.execute("UPDATE daemon_workspaces SET status='active'")
        tx.commit()
        tx.close()
