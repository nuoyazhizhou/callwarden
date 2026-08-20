# task.split 修复子任务计划

## 子任务 1: 修复 plan_file 路径调用 insert_task_steps
- description: 修复 rust_ext/src/daemon/task_collab.rs handle_task_split 的 plan_file 路径，使其调用 insert_task_steps 在同一事务中写入步骤
- steps:
  - action: fix_plan_file_path
    target_file: rust_ext/src/daemon/task_collab.rs
    target_symbol: handle_task_split
    check_items: plan_file path must call insert_task_steps; same transaction; rollback on failure
  - action: regression_test
    target_file: rust_ext/src/daemon/task_collab.rs
    target_symbol: tests module
    check_items: 2+steps split; fields consistent; rollback on failure

## 子任务 2: 补充 daemon/CLI E2E 测试
- description: 新增 tests/test_task_split_steps.py 覆盖 daemon RPC 和 CLI 命令行入口
- steps:
  - action: daemon_e2e_test
    target_file: tests/test_task_split_steps.py
    target_symbol: test_task_split_via_daemon
    check_items: route_task_write daemon path verifies steps persisted
  - action: cli_e2e_test
    target_file: tests/test_task_split_steps.py
    target_symbol: test_task_split_via_cli
    check_items: cw task split --plan CLI verifies steps in task status
