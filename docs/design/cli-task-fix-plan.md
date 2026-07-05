# CLI 任务命令体验修复

## 背景

cw task 子命令与 --task-* flag 存在重复、行为不一致、触碰数据库等问题。
需要统一任务命令入口，修复以下 5 个具体 bug：

1. `cw task --help` 卡在 db 初始化（set_active_workspace 阻塞等锁）
2. `--task-list` 显示 20 条 vs `task list` 显示 83 条（limit 默认值不一致）
3. `--task-list` 和 `task list` 是同义词，增加心智负担
4. list 命令区分不出父子任务（没展示 parent_id/depth/sort_order）
5. `--task-show` 只显示主任务，不递归显示子任务

## Step 1: 修复 cw task --help 卡 db

- target_file: cli/main.py
- target_symbol: _run_subcommand_mode
- 修改要点：入口处先扫 sys.argv[2:]，含 --help/-h 时直接 print argparse 帮助并 return，不初始化 db
- i18n: 复用既有 cli.subcommand_help，不新增 key
- 测试：tests/test_cli_task_fix.py::test_help_no_db_init

## Step 2: 废弃 --task-list flag，统一到 task list

- target_file: cli/main.py
- target_symbol: _build_task_subparser / main
- 修改要点：
  • --task-list 仍保留但内部转调 _handle_task list（行为一致）
  • task list 改用 db.task_list()，统一 limit 默认值
  • 暂时保留 --task-list 作为兼容入口，避免破坏旧脚本
- i18n: 不新增 key
- 测试：tests/test_cli_task_fix.py::test_task_list_unified

## Step 3: task list 显示父子树形结构

- target_file: cli/main.py
- target_symbol: _handle_task（list 分支）
- 修改要点：
  • 查询时带上 parent_id/depth/sort_order
  • 默认按树形缩进展示（depth 决定缩进层级）
  • 新增 --flat 选项退回扁平展示
- i18n: 新增 cli_task_list_root / cli_task_list_indent / cli_task_arg_flat
- 测试：tests/test_cli_task_fix.py::test_task_list_tree_structure

## Step 4: --task-show 改用 task_status_tree 显示子任务

- target_file: cli/main.py
- target_symbol: main（args.task_show 分支）
- 修改要点：
  • 改用 db.task_status_tree()
  • 递归展示子任务（缩进 + 进度）
  • 新增 --flat 选项退回扁平（旧行为）
- i18n: 新增 cli_task_show_subtasks / cli_task_show_progress / cli_task_show_no_subtasks
- 测试：tests/test_cli_task_fix.py::test_task_show_tree

## Step 5: 全量回归 + 文档更新

- target_file: tests/, docs/cli_reference.md
- 修改要点：
  • python -m pytest tests/ -q --ignore=tests/test_stress.py 全量通过
  • docs/cli_reference.md 更新 task list / task show 说明
  • git diff --check 无空白错误
  • cw --refresh-all 刷新数据库
