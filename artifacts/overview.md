# Call Warden A′ 评审循环 — 任务视图与阻断说明

## 完成情况
- 已读取 Epic `T-1787203926824-9f873bfc`（CW 业务逻辑全量下沉 Rust daemon，三阶段收尾）的子树。
- 原计划用 `cw task list --parent ...` 取子树，但 `list` 无 `--parent` 参数；改用 `cw task status-tree` 成功枚举全树（202 个任务 ID，其中约 133 个处于 `review` 状态）。
- 已对先导任务 `T-1787321708568-d292ab3c` 实际调用 `cw task next-action <id> --json` 验证派工投影。

## 关键发现 / 阻断项（重要）
**`next-action` 派工投影无法按要求执行 —— 工具链与流程不匹配（P1，需用户决策）。**

1. **`workspace_instance_id` 参数缺口。** 守护进程 RPC `task.next_action`（`rust_ext/src/daemon/dispatch.rs:1173` + `task_loop/next_action.rs:43-61`）明确要求 `task_id` 与 `workspace_instance_id`（`NextActionInput::from_params` 缺一则报 `invalid_params`）。
   - 已安装的 `cw`（AppData，Jul 3 构建）的 `next-action` 子命令**只接受 `task_id` 和 `--json`，不暴露 `--workspace-instance-id`**（已用 4 种变体实测，全部 `unrecognized arguments`）。
   - 用户给定的实例 `4baea3ff12c2ea5c`（workspace_id=1）无法传入，导致守护进程回退到退化结果：`required_role: null`、`action: NONE`、blocking = "Task Contract 缺失、多版本冲突或 revision 链不连续（无法验证 hash）"。这不是真实评审结论，是参数缺失造成的假阴性。
2. **源码与二进制不一致。** 仓库 `rust_ext/src/bin/cw_cli.rs` 的 `TaskAction` 枚举里**没有 `NextAction` 变体**——`next-action` 子命令在源码中未实现，当前仓库 `cw` 构建（release Aug 20）也确实无该子命令。已安装的 `cw` 来自另一构建，有 `next-action` 但仍缺 flag。从仓库源码重编反而会丢失 `next-action`。
3. **数据库位置。** `workspace_authority_captures` 表位于宿主机的 `~/.workbuddy/workbuddy.db`，沙箱无法直读（独立宿主文件系统）；`cw` 主机二进制可读取但又无法转发 instance id。

## 后果
- 无法忠实生成 `required_role=reviewer` + `action=review_current_step` 的派工投影，Reviewer 准入门禁（§1-§5）无法靠系统投影满足；若强行凭推断给出 verdict 会违反 fail-closed / 独立复核纪律。
- 无法判定 133 个 `review` 任务中哪些是"待 reviewer 复审"、哪些是 IDLE。

## 建议的下一步（需用户确认）
- **选项 A（推荐）**：安装/升级一个能转发 `--workspace-instance-id` 的 `cw` 构建，之后逐卡跑 `cw task next-action <id> --workspace-instance-id 4baea3ff12c2ea5c --json`。
- **选项 B**：提供 `task.next_action` 的可用 MCP/HTTP 入口，直接走 RPC 传 instance。
- **选项 C**：授权在不依赖 next-action 投影的前提下，仅用 `cw task show` 逐任务的只读证据做人工式复核（不产出机器 verdict）。

## 已创建的产物
- `epic_subtree.md`：Epic 子树完整任务清单（含 133 个 review 任务 ID 与标题）。
- 本文件：`overview.md`。

> ⚠️ 在工具缺口修复前，我不会对任意任务给出 reviewer_pass / reviewer_blocked 裁决，以遵守治理纪律。
