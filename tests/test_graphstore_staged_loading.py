"""GraphStore 分级加载的后台发布与代次隔离测试。"""

from __future__ import annotations

import threading
import time
import sys
from pathlib import Path

RUST_TARGET = Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall"
sys.path.insert(0, str(RUST_TARGET))

import callwarden_core
import pytest

from callwarden.db.db import CodeGraphDB


class ControlledGraphStore:
    """可控制 full load 完成时机的 GraphStore 替身。"""

    full_started = threading.Event()
    full_release = threading.Event()
    full_finished = threading.Event()

    def __init__(self):
        self.state = "empty"
        self.symbol_token = None

    @classmethod
    def reset(cls) -> None:
        cls.full_started.clear()
        cls.full_release.clear()
        cls.full_finished.clear()

    def load_symbols_from_sqlite(self, _db_path: str) -> int:
        self.state = "symbols_ready"
        self.symbol_token = object()
        return 1

    def fork_symbols(self):
        if self.symbol_token is None:
            raise RuntimeError("symbols not ready")
        forked = type(self)()
        forked.state = "symbols_ready"
        forked.symbol_token = self.symbol_token
        return forked

    def load_calls_from_sqlite(self, _db_path: str):
        self.full_started.set()
        if not self.full_release.wait(timeout=5):
            raise TimeoutError("test did not release full graph load")
        self.state = "graph_ready"
        self.full_finished.set()
        return 0

    def load_from_file(self, _path: str):
        self.state = "graph_ready"
        return 1, 0

    def load_state(self) -> str:
        return self.state

    def dump_to_file(self, path: str) -> None:
        Path(path).write_bytes(b"snapshot")


@pytest.fixture
def staged_db(tmp_path: Path):
    db = CodeGraphDB(
        db_path=str(tmp_path / "callwarden.db"),
        workspace_root=str(tmp_path),
    )
    try:
        yield db
    finally:
        ControlledGraphStore.full_release.set()
        db.close()


def _wait_for_state(db: CodeGraphDB, expected: str, timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if db._graph_store_status()["state"] == expected:
            return
        time.sleep(0.01)
    raise AssertionError(f"GraphStore did not reach {expected}: {db._graph_store_status()}")


def test_symbols_publish_before_background_full_graph(
    staged_db: CodeGraphDB, monkeypatch: pytest.MonkeyPatch
):
    ControlledGraphStore.reset()
    monkeypatch.setattr(callwarden_core, "GraphStore", ControlledGraphStore)

    symbols_store = staged_db._get_graph_store()
    assert ControlledGraphStore.full_started.wait(timeout=2)
    assert symbols_store.load_state() == "symbols_ready"
    assert staged_db._graph_store_status() == {
        "state": "symbols_ready",
        "generation": 0,
        "loading": True,
        "last_error": None,
    }

    ControlledGraphStore.full_release.set()
    _wait_for_state(staged_db, "graph_ready")
    assert staged_db._graph_store is not symbols_store
    assert staged_db._graph_store.symbol_token is symbols_store.symbol_token
    assert staged_db._graph_store_status()["loading"] is False


def test_invalidation_rejects_stale_background_publish(
    staged_db: CodeGraphDB, monkeypatch: pytest.MonkeyPatch
):
    ControlledGraphStore.reset()
    monkeypatch.setattr(callwarden_core, "GraphStore", ControlledGraphStore)

    symbols_store = staged_db._get_graph_store()
    assert ControlledGraphStore.full_started.wait(timeout=2)
    staged_db._invalidate_graph_store()
    ControlledGraphStore.full_release.set()
    assert ControlledGraphStore.full_finished.wait(timeout=2)
    time.sleep(0.05)

    assert staged_db._graph_store is symbols_store
    assert staged_db._graph_store_dirty is True
    assert staged_db._graph_store_generation == 1
    assert staged_db._graph_store_status()["state"] == "symbols_ready"
