# Tasks：workspace-snapshot-remediation

前置：`spec.md` 已批准。全部任务在同一分支顺序执行；每完成一项即更新状态。

## 任务

- [x] Task 1 缺陷 A：CLI 权威注入 helper
  - 文件：`cli/main.py`
  - 新增 `_WorkspaceAuthorityUnresolved` + `_resolve_caller_workspace_instance_id(db)`
    （紧跟 `_resolve_task_create_project_root`）。
  - 验收：helper 内不调用 `derive_workspace_instance_id`；「无唯一权威配对」抛
    `E_WORKSPACE_AUTHORITY_UNAVAILABLE`；空 instance 同样 fail-closed。

- [x] Task 2 缺陷 A：next-action / supersede 接线
  - 文件：`cli/main.py`
  - `task next-action` / `task supersede` 改用 Task 1 helper；显式
    `--workspace-instance-id` 仍优先；`_WorkspaceAuthorityUnresolved` 前置拒绝
    （`--json` 结构化输出 / 人类可读 `<code>: <msg>`，`return True`，不发 RPC）；
    连接级 / `E_WORKSPACE_ROOT_REQUIRED` 沿用既有路由错误语义（`pass`）。
  - 验收：`derive_workspace_instance_id` 仅剩 graph/watch 使用点（约 L13581）。

- [x] Task 3 缺陷 B：Rust snapshot 公式与写路径
  - 文件：`rust_ext/src/daemon/workspace.rs`
  - `compute_snapshot_id` 增加 `workspace_instance_id` 参数；remote+head 齐全走既有公式，
    否则 `sha256("instance:{instance}|{fp}")[:16]`；恒返回 16 hex。
  - adopt / insert 分支读既有 provenance 回退，恒写非空 snapshot；
    UPSERT 直接取 `excluded.snapshot_id`。
  - legacy 兄弟行传播恒执行；provenance 列 `CASE WHEN <> ''` 保护；adopt 分支同样传播。
  - `update_workspace_vcs_head`：改用新签名；remote 为空时保持既有 snapshot。
  - 验收：`cargo check` 通过。

- [x] Task 4 Rust 单测更新
  - 文件：`rust_ext/src/daemon/workspace.rs`（`#[cfg(test)]`）
  - 按 spec §2.3 改写 null/空断言测试，新增公式兼容性测试。
  - 验收：`cargo test --lib workspace::tests` → 148 passed / 0 failed。

- [x] Task 5 Python 回归测试
  - 文件：`tests/test_task_next_action_e2e.py`
  - 4 处 CLI 子进程调用补 `--workspace-instance-id <ws>`；
    新增 fail-closed 用例（`.git` 可定位 root + 无 registry 配对 + 不传参 → `E_WORKSPACE_` 前缀）。
  - 验收：`python -m pytest tests/test_task_next_action_e2e.py -q` → 5 passed / 11 skipped
    （隔离 daemon 默认管道被生产 daemon 占用，按既有规则 skip）。

- [x] Task 6 部署 + 真机复验 + 提交
  - `scripts/refresh_shared_runtime.ps1 -TaskId T-1793205026212-74deba38 -RestartMcp -RunSmokeTests`。
  - TokenSlim：`workspace register` / `collab publish` 的 `snapshot_id` 非空。
  - TokenSlim 临时探针卡：`task next-action --json` 无 `E_WORKSPACE_AUTHORITY_MISMATCH`；
    `task report` 带 snapshot 成功；验证后关闭探针卡。
  - callwarden 自身 `task next-action` 回归一致。
  - 提交（代码 + spec/tasks/checklist）。
  - 验收：上述真机结果全部符合；`git status` 干净。

## 依赖关系

Task 1 → Task 2；Task 3 → Task 4；Task 1..5 → Task 6。
