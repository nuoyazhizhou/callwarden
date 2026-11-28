# T-1795663535104-aa05bd64 证据 manifest

- 任务：T-1795663535104-aa05bd64（REQ-PREQ-01 / AC-06），step S-1795663535108-aa49c3d8
- worktree：/c/git_work/callwarden-worker/runs/impl-t3-status-43681c00（分支 impl/t3-status-projection，基线 40a21d4）
- 身份：t3-exec-43681c00 / inst-t3-exec-43681c00 / sess-t3-exec-43681c00
- lease：L-17693887a8ec3e82（fencing=2，接管前会话僵尸 lease L-0c812edaf8ac5dcc fencing=1）
- 证据落盘时间：2025-11-28 09:32

## 被测源文件（worktree，commit 前状态）

- `rust_ext/src/daemon/task_collab_query.rs` sha256 = 617c877e6190fc6e24e69386bfffe5f0adcd7b45091fc610b1b0d2e67ed50a13（+19 行）
- `rust_ext/src/daemon/task_collab_tests_projection.rs` sha256 = 7f56ec93c2c57c499ab53f07f80bb64537f132ecfaf123ace5cd245c0a88062b（+175 行）

## 证据文件（本目录）

- `T-1795663535104-aa05bd64-AC-06.md` sha256 = 370758925ebe583deda3f7a74dc7980ed871d34eabf86a59949992d45a7b7dd0（5315 B；八段证据）
- `T-1795663535104-aa05bd64-tree-governance-test.log` sha256 = cd79f5519ee2a4377fe9e5f32b15d01d245abe35375a60a1b62c44fc0eafe002（43321 B；原始未过滤 cargo test 输出）
- `T-1795663535104-aa05bd64-tree-governance-tests-keylines.txt` sha256 = 80d1312aa5472bf4d36692159eda5cdd5e376daa2a4b8eba03521e7c0356b4bc（700 B；测试结论行）

## 测试结论

- 命令：`cargo test tree_governance --lib`（隔离构建，CARGO_TARGET_DIR=cw_t4_target）
- 结果：`test result: ok. 3 passed; 0 failed; 0 ignored; 0 measured; 2150 filtered out; finished in 0.47s`，CARGO_RC=0
- 新测试：tree_governance_projection_lists_unmet_hard_dependency / tree_governance_projection_closed_task_prerequisites_empty / tree_governance_status_tree_node_carries_prerequisites 全部 ok

## 备注

- 本 manifest 仅覆盖 T3 证据文件，不包含 T4（T-1795663549865-19deda64）与 T5（T-1795663552556-ba42830c）文件。
- 测试结果先于 task.report 落盘；实现先于报告（源 sha256 与本 manifest 一致后再 report）。
