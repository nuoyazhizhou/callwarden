# T-1793205026212-74deba38 收口证据（workspace authority/snapshot 双缺陷修复）

- 任务：T-1793205026212-74deba38
- 标题：workspace authority/snapshot 双缺陷修复：CLI 禁本地派生 instance + daemon snapshot_id 恒非空
- 绑定：workspace_instance_id=4baea3ff12c2ea5c / workspace_capture_id=wc-4baea3ff12c2ea5c-1963259820（ws=1）
- 收口方式：本卡自创建起 `task_steps=0`（无步骤可 claim/report），属结构性裸卡；
  按仓库既有裸卡收口路径 `task.cascade_close`（系统权威收尾，非 A′ 环）关闭，
  证据以本文件与下列提交/规格/探针卡为准。

## 缺陷 A：CLI 禁止本地派生 workspace instance

- 根因：`cli/main.py` 的 `task next-action` / `task supersede` 本地 derive instance，
  与 daemon 权威解析不一致，触发 `E_WORKSPACE_AUTHORITY_MISMATCH`。
- 修复：改走 `resolve_workspace_pair_from_daemon` 权威解析；无唯一配对时前置拒绝
  `E_WORKSPACE_AUTHORITY_UNAVAILABLE`。
- 提交：`9a9846a`（fix(workspace): CLI 禁止本地派生 instance，snapshot_id 恒非空并传播兄弟行）
- 代码：`cli/main.py`

## 缺陷 B：daemon snapshot_id 恒非空

- 修复：`rust_ext/src/daemon/workspace.rs` 的 `compute_snapshot_id` 增加 instance 兜底；
  `register` / `adopt` / `update_vcs_head` 三条写路径恒写非空 snapshot_id，
  并按 `normalize_identity_root` 传播到 legacy 兄弟行。
- 提交：`9a9846a`
- 代码：`rust_ext/src/daemon/workspace.rs`

## 规格与验收

- spec：`docs/specs/workspace-snapshot-remediation/spec.md`
- tasks：`docs/specs/workspace-snapshot-remediation/tasks.md`（全部 `[x]`）
- checklist：`docs/specs/workspace-snapshot-remediation/checklist.md`
- 测试：`tests/test_task_next_action_e2e.py`、`tests/test_tools_task_create_workspace_params.py`
  （随 `9a9846a` 一并更新并通过）
- 部署与真机复验：`e2c5ab3`（docs(spec): 勾选 T6 部署真机复验与提交结果）

## 线上探针（已 closed）

- `T-1793231476200-d2f6d2b0` SNAPSHOT-AUTHORITY-PROBE-20261029（cascade_closed）
- `T-1793239588268-8fe6f514` [probe] snapshot live verify 20261029（closed）

## 结论

缺陷 A/B 均已修复、部署并在真机验证；本卡为目标已达成后的遗留裸卡，
无待执行步骤，按系统权威收尾关闭。
