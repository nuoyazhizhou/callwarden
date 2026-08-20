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
# S8：publish_snapshot 使用 PASSIVE checkpoint
# ======================================================================


class _FakeCursor:
    """模拟 PRAGMA wal_checkpoint 的返回行（busy, wal_pages, checkpointed）。"""

    def __init__(self, busy: int):
        self._busy = busy

    def fetchone(self):
        return (self._busy, 0, 0)


class _FakeConnection:
    """记录执行的 SQL 的连接对象（支持 with 上下文）。"""

    def __init__(self, busy: int = 0):
        self.executed = []
        self._busy = busy

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self.executed.append(sql)
        if "wal_checkpoint" in sql:
            return _FakeCursor(self._busy)
        return _FakeCursor(0)


def _make_client(monkeypatch, busy: int):
    """构造 UnixDaemonRpcClient，mock sqlite3.connect 与后续 RPC 调用。"""
    import callwarden.server.daemon_client as dc

    fake_conn = _FakeConnection(busy=busy)

    def fake_connect(db_path, **kwargs):
        return fake_conn

    monkeypatch.setattr(dc.sqlite3, "connect", fake_connect)

    client = UnixDaemonRpcClient(socket_path="unused-socket")
    # 无论 win32 / unix 分支，都 mock 最终 RPC 调用
    monkeypatch.setattr(client, "call", lambda *a, **k: {"generation": 1, "call_count": 1})
    return client, fake_conn


def test_publish_snapshot_uses_passive_checkpoint(monkeypatch):
    """S8: publish_snapshot 应使用 PASSIVE checkpoint 而非 FULL。"""
    client, fake_conn = _make_client(monkeypatch, busy=0)
    result = client.publish_snapshot("ws-1", "/fake/db.sqlite", "ctx")
    assert result == {"generation": 1, "call_count": 1}
    # 先设 busy_timeout 再 PASSIVE checkpoint，且不得出现 FULL
    assert fake_conn.executed == [
        "PRAGMA busy_timeout=5000",
        "PRAGMA wal_checkpoint(PASSIVE)",
    ]
    assert not any("FULL" in sql for sql in fake_conn.executed)


def test_publish_snapshot_busy_does_not_raise(monkeypatch):
    """S8: PASSIVE checkpoint 在 busy 时不 fail-fast（不抛 DaemonUnavailableError）。"""
    client, _fake_conn = _make_client(monkeypatch, busy=1)
    # 旧实现 busy=1 时 raise DaemonUnavailableError；新实现应继续走 RPC
    result = client.publish_snapshot("ws-1", "/fake/db.sqlite", "ctx")
    assert result["generation"] == 1


def test_publish_snapshot_no_longer_raises_on_busy_checkpoint(monkeypatch):
    """S8 回归：busy 时不再抛 DaemonUnavailableError（旧语义校验）。"""
    client, _fake_conn = _make_client(monkeypatch, busy=1)
    try:
        client.publish_snapshot("ws-1", "/fake/db.sqlite", "ctx")
    except DaemonUnavailableError:
        pytest.fail("PASSIVE checkpoint busy 时不应抛 DaemonUnavailableError")


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
