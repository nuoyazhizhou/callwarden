"""历史任务 reconciliation daemon 路由的回归约束。"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TASK_COLLAB = ROOT / "rust_ext" / "src" / "daemon" / "task_collab.rs"
DISPATCH = ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs"
OPERATION_STORE = ROOT / "rust_ext" / "src" / "daemon" / "task_loop" / "operation_store.rs"


def _source(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_reconciliation_is_a_protected_idempotent_daemon_mutation():
    dispatch = _source(DISPATCH)
    operation_store = _source(OPERATION_STORE)

    assert '"task.reconcile" => state.handle_task_reconcile(peer, params)' in dispatch
    assert '"task.reconcile",' in dispatch
    assert '"task.reconcile",' in operation_store
    assert 'let method = "task.reconcile";' in _source(TASK_COLLAB)
    assert "OperationStore.dedupe" in _source(TASK_COLLAB)
    assert "OperationStore.record_result" in _source(TASK_COLLAB)


def test_reconciliation_requires_authority_and_preserves_step_history():
    source = _source(TASK_COLLAB)
    start = source.index("pub fn handle_task_reconcile")
    end = source.index("pub fn handle_task_events", start)
    handler = source[start:end]

    assert "WITH RECURSIVE task_tree" in source
    assert "task_workspace_bindings" in source
    assert "workspace_authority_captures" in source
    assert "instance != requested_instance" in source
    assert "status = 'review'" in source
    assert "status = 'in_progress'" in source
    assert '"preserves_steps": true' in source
    assert "UPDATE tasks SET status = 'in_progress'" in source
    assert "UPDATE task_steps" not in handler
    assert "DELETE FROM task_steps" not in handler


def test_reconciliation_rejects_unsafe_apply_inputs():
    source = _source(TASK_COLLAB)
    handler = source[source.index("pub fn handle_task_reconcile"):]

    assert "缺少 workspace_instance_id" in handler
    assert "apply 必须携带完整 identity" in handler
    assert "apply 必须携带 request_id" in handler
    assert "unchecked_transaction" in handler
