"""通过 Python subprocess 创建新任务（避免 PowerShell 引号问题）"""
import subprocess
import sys
import json

CLOSE_GATE_STEPS = [
    {"action": "child_status_gate", "target_file": "rust_ext/src/daemon/task_collab.rs", "target_symbol": "handle_task_close", "check_items": "SELECT COUNT open subtasks; E_CHILD_TASKS_NOT_CLOSED; same transaction"},
    {"action": "zero_steps_gate", "target_file": "rust_ext/src/daemon/task_collab.rs", "target_symbol": "handle_task_close", "check_items": "steps=[] forbid close; E_NO_STEPS; evidence-only via formal field"},
    {"action": "lease_fail_closed", "target_file": "rust_ext/src/daemon/task_collab.rs", "target_symbol": "handle_task_apply,handle_task_close", "check_items": "E_LEASE_CLOCK_UNAVAILABLE; no degrade; apply also fail-closed"},
    {"action": "completion_review_gate", "target_file": "rust_ext/src/daemon/task_collab.rs", "target_symbol": "handle_task_completion_review", "check_items": "zero steps returns blocked; no vacuous pass"},
    {"action": "closed_at_and_cascade", "target_file": "rust_ext/src/daemon/task_collab.rs", "target_symbol": "handle_task_close", "check_items": "closed_at nonzero; keep cascade; open subtask blocks parent"},
    {"action": "regression_tests", "target_file": "tests/test_task_close_gate.py", "target_symbol": "test_child_gate,test_zero_steps,test_lease_fail", "check_items": "8 regression scenarios covered"},
]

steps_json = json.dumps(CLOSE_GATE_STEPS, ensure_ascii=False)
print(f"Steps JSON length: {len(steps_json)}", file=sys.stderr)

cmd = [
    sys.executable, "cw.py", "task", "create",
    "--title", "修复 task close 父子状态门禁、零步骤误关闭与 lease fail-closed",
    "--desc", "修复 Rust daemon handle_task_close 完全缺失的子任务状态门禁、步骤状态核实、lease clock fail-closed。根因: rust_ext/src/daemon/task_collab.rs:1291-1346 直接 UPDATE 不检查前置条件。计划见 docs/plan-task-close-gate-fix.md",
    "--steps", steps_json,
]
print(f">>> {' '.join(cmd[:6])} ... --steps <json>", file=sys.stderr)

result = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8")
print(result.stdout, file=sys.stderr)
if result.returncode != 0:
    print(f"FAILED (exit {result.returncode}): {result.stderr}", file=sys.stderr)
    sys.exit(1)
print("OK", file=sys.stderr)
