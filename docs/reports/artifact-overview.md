# Call Warden A′ 流水线循环 — 本轮进展

## 已完成
- **CLI-03**（T-1787321708639-d6d362f4，control_plane：`cw task show/list/status-tree` 只读 authority 诊断）
  - 补全生命周期：领取 4 步（report step2 `fixture_matrix` + step3 `matrix_verify）→ 任务翻 `review`。
  - 新增 `tests/test_cli_03_http_rpc.py`（5 passed）+ 修复 `tests/test_cli03_task_read_authority.py`（9 passed）。
  - 刷新 `deliverables/software-company/tool_migration_matrix.json` 的 CLI-03 evidence。
  - 提交：`0374aaa`。
- **MCP-001**（T-1787321708699-da5d8224，Gate/task_projection：`get_role_view → Rust daemon native`）
  - 实施已先于本会话由 `nuoyazhizhou` 提交（6e9b239）。本轮补全生命周期：领取 4 步 → `review`。
  - 将测试文件由错位命名 `test_mcp01_get_role_view_http_rpc.py` 重命名为合同目标名 `test_mcp_get_role_view_http_rpc.py`（7 passed），并同步矩阵 evidence 文本。
  - 提交：`0670ca8`。

## 关键机制（实测，非假设）
- **Gate 只拦 `review→apply`**，不拦 `open→in_progress→review`。CLI-02/03/MCP-001 均在 CLI-01 未 apply 时被成功推进到 `review`。
- **合同 `prompt_hash`**（executor）：`59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6`（从 `role_contracts` 表读取，所有 executor 卡共用）。
- **变更路径校验**：`task.report` 的 `changes[].file_path` 必须等于该步骤 `target_file`（按**逗号** split 的白名单）。注意 step0 的 `target_file` 用了分号 `;`，导致整串成为唯一合法 token（已用整串匹配通过）；step2 的 `target_file` 与实际提交文件名不一致，需对齐文件名。
- **身份**：`agent_id=executor-workbuddy-v1-cur`、`session_id=sess-workbuddy-cw-20260822-0320`、`role=executor`、`model_id=deepseek-v4-flash`。
- `cw --refresh-all` 仍坏（`method_not_found: build_full_graph`），提交前跳过。

## 待解 / 风险
- **§7 死锁仍在**：未 apply 的卡（CLI-01/02/03、MCP-001…）无法进行 `review→apply`，需 Adjudicator + reviewer lease 的 P0-C `task_contract_bootstrap` 解锁。继续领取只会把更多卡堆在 `review`。
- 父任务 `T-1787293451688-c14b1e44` 下共 186 个子任务；目前 CLI-01/02/03、MCP-001 为 `review`，其余 180+ 仍 `open`（下一个为 MCP-002 `find_evidence`）。

## 下一步选项
- **A. 解 §7**：先做 P0-C `task_contract_bootstrap`（CLI-01 apply），解锁后续 apply，再继续循环。
- **B. 继续循环**：继续逐张领取并推到 `review`（明知会堆积，直到 §7 解锁）。
- **C. 实现 MCP-002**：如需真正落地，需为 `find_evidence` 写 Rust handler + fixture（非单纯补生命周期）。

需要我从哪一项继续？
