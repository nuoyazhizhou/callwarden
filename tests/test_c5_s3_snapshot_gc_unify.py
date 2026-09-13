"""C5 家族 S3：Snapshot GC 统一（C4 GC 面/S8）。

S8 目标：WAL checkpoint 策略统一——Python client 的 publish_snapshot
由 `PRAGMA wal_checkpoint(FULL)` + busy fail-fast 改为 PASSIVE 双保险
（`PRAGMA busy_timeout=5000; PRAGMA wal_checkpoint(PASSIVE)`），与
Rust 侧 snapshot_state.rs / snapshot.rs 的 checkpoint 语义一致，
busy 时不抛 DaemonUnavailableError（剩余 WAL 页由 daemon/内核 PASSIVE 兜底）。

C4 GC 面：Python `SnapshotManagerService.gc_snapshots` 与 Rust
`handle_gc_snapshots` 语义统一——keep_last 默认 3、遍历所有 workspace、
调用 mgr.gc_generations(keep_last)、返回删除总数。
"""

import threading

import pytest

from callwarden.server.daemon_client import (
    DaemonUnavailableError,
    UnixDaemonRpcClient,
)
from callwarden.server.snapshot_manager import SnapshotManagerService


# ======================================================================
# S8：publish_snapshot checkpoint authority 下沉 daemon
# ======================================================================


def _make_client(monkeypatch):
    """构造 UnixDaemonRpcClient，mock 最终 RPC 调用（记录 method 序列）。

    stale 修正：server/daemon_client.py:1012-1041 publish_snapshot 已删除本地
    sqlite3 connect + PASSIVE checkpoint（SRV-006，checkpoint authority 下沉
    Rust daemon），模块不再 import sqlite3，故不再 mock sqlite3.connect。
    """
    client = UnixDaemonRpcClient(socket_path="unused-socket")
    calls = []

    def fake_call(method, params=None, *args, **kwargs):
        calls.append((method, params or {}))
        if method == "mcp.daemon_client.publish_snapshot":
            return {"checkpointed": True, "db_path": (params or {}).get("db_path")}
        return {"generation": 1, "call_count": 1}

    monkeypatch.setattr(client, "call", fake_call)
    return client, calls


def test_publish_snapshot_delegates_checkpoint_to_daemon(monkeypatch):
    """S8: publish_snapshot 委派 daemon 执行 checkpoint（不再本地 PRAGMA）。"""
    client, calls = _make_client(monkeypatch)
    result = client.publish_snapshot("ws-1", "/fake/db.sqlite", "ctx")
    assert result == {"generation": 1, "call_count": 1}
    # 两步 RPC：先 daemon 权威 checkpoint，再 publish；无本地 FULL/PASSIVE
    assert [m for m, _ in calls] == [
        "mcp.daemon_client.publish_snapshot",
        "snapshot.publish",
    ]
    import callwarden.server.daemon_client as dc
    assert not hasattr(dc, "sqlite3"), \
        "daemon_client 不应再 import sqlite3（本地 checkpoint 已下沉 daemon）"


def test_publish_snapshot_busy_does_not_raise(monkeypatch):
    """S8: checkpoint busy 由 daemon 兜底，Python 侧不抛 DaemonUnavailableError。"""
    client, _calls = _make_client(monkeypatch)
    result = client.publish_snapshot("ws-1", "/fake/db.sqlite", "ctx")
    assert result["generation"] == 1


def test_publish_snapshot_no_local_busy_fail_fast(monkeypatch):
    """S8 回归：本地 busy fail-fast 分支已删除（旧实现 busy=1 时抛异常）。

    stale 修正：daemon_client.py:1012-1041 现仅做 RPC 委派，
    Python 侧不含 wal_checkpoint / busy_timeout 本地分支。
    """
    import inspect
    src = inspect.getsource(UnixDaemonRpcClient.publish_snapshot)
    assert "PRAGMA busy_timeout" not in src
    assert "wal_checkpoint" not in src


# ======================================================================
# C4 GC 面：Python gc_snapshots 与 Rust handle_gc_snapshots 语义统一
# ======================================================================


class _FakeMgr:
    """模拟 PySnapshotManager（Rust 绑定）的最小实现。"""

    def __init__(self):
        self.keep_last_calls = []

    def gc_generations(self, keep_last: int) -> int:
        self.keep_last_calls.append(keep_last)
        return 2  # 每次删除 2 个 generation


class _FakeCache:
    """模拟 PySnapshotCache（Rust 绑定）的最小实现。"""

    def __init__(self, ws_ids):
        self._mgrs = {ws_id: _FakeMgr() for ws_id in ws_ids}

    def list_workspaces(self):
        return list(self._mgrs.keys())

    def get(self, ws_id):
        return self._mgrs.get(ws_id)


def _make_service(cache):
    """绕过 __init__ 构造 SnapshotManagerService，注入 fake cache。"""
    svc = SnapshotManagerService.__new__(SnapshotManagerService)
    svc._cache = cache
    svc._lock = threading.Lock()
    return svc


def test_gc_snapshots_default_keep_last_3():
    """C4 GC 面: gc_snapshots 默认 keep_last=3（与 Rust handle_gc_snapshots 一致）。"""
    cache = _FakeCache(["ws_a", "ws_b"])
    svc = _make_service(cache)
    deleted = svc.gc_snapshots()  # 不传 keep_last
    assert deleted == 4  # 2 个 workspace × 2
    for mgr in cache._mgrs.values():
        assert mgr.keep_last_calls == [3]


def test_gc_snapshots_visits_all_workspaces():
    """C4 GC 面: gc_snapshots 遍历所有 workspace 并调用 gc_generations(keep_last)。"""
    cache = _FakeCache(["ws_a", "ws_b", "ws_c"])
    svc = _make_service(cache)
    deleted = svc.gc_snapshots(keep_last=5)
    assert deleted == 6  # 3 个 workspace × 2
    for mgr in cache._mgrs.values():
        assert mgr.keep_last_calls == [5]


def test_gc_snapshots_returns_deleted_count_like_rust():
    """C4 GC 面: 返回删除总数（与 Rust handle_gc_snapshots 的 deleted_count 语义一致）。"""
    cache = _FakeCache(["ws_x"])
    svc = _make_service(cache)
    deleted = svc.gc_snapshots(keep_last=1)
    assert deleted == 2
