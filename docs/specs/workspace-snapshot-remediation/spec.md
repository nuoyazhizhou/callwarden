# Spec：workspace 实例派生不一致 + snapshot_id 恒空（两个真机缺陷）

状态：待批准
日期：2026-10-28
范围：callwarden 仓库（`cli/main.py` + `rust_ext` daemon + 回归测试）；不改 TokenSlim 源码；不改动任何既有任务卡状态

## 1. 缺陷与证据（真机已复现）

### 缺陷 A：CLI 本地派生 caller `workspace_instance_id` 与 daemon 权威实例不一致

- 现象：在 `c:/git_work/TokenSlim` 目录下对**未关闭**任务执行 `cw task next-action <id>`，返回
  `E_WORKSPACE_AUTHORITY_MISMATCH`（调用方 instance 解析不到 task binding workspace）。
- 证据：
  - daemon 权威 registry `C:\var\lib\callwarden\registry.db` 中 TokenSlim 行为
    `workspace_instance_id = d079f60cac8d6186`；task DB `C:\Users\wanpi\.callwarden\callwarden.db`
    的 `task_workspace_bindings` 引用同一实例（capture `wc-d079f60cac8d6186-3235910540`）。
  - CLI `derive_workspace_instance_id("c:/git_work/TokenSlim") = 7a55659ffa4ca42c`
    （`sha256(normalized_root)[:16]`，见 `server/daemon_client.py:1155-1176`）。
  - daemon 权威公式为 `sha256("{owner_uid}|{normalize_identity_root(host_real_root)}")[:16]`
    （`rust_ext/src/daemon/workspace.rs:179-191`），与 CLI 公式不同，**永远不可能相等**。
- 根因点（本次要改的两处使用）：
  - `cli/main.py:4691-4696`（`task next-action` 未显式传 `--workspace-instance-id` 时本地派生）；
  - `cli/main.py:6032-6037`（`task supersede` 幂等 key 成员本地派生）。
- 第三处 `cli/main.py:13516`（graph/watch 场景）本次**不改**（非本缺陷路径）。
- 契约依据：AGENTS.md / `route_rpc` 设计明确"禁止客户端 derive workspace"；
  `server/daemon_client.py:3813-3888 resolve_workspace_pair_from_daemon` 是唯一权威解析入口。

### 缺陷 B：`snapshot_id` 恒为 NULL，`task.report` 永久不可用

- 现象：TokenSlim 的 registry 两行（id 175 `d079f60cac8d6186`、id 1288 `6108ef48c489cb1b`）
  `snapshot_id` 均为 `NULL`、`git_remote_url` / `git_head_commit_sha` 均为空；
  `workspace.register` / `snapshot.publish` 返回的 `snapshot_id` 为 null；
  `task.report` 恒报 `E_TASK_REPORT_SNAPSHOT_REQUIRED`（请求未带）
  或 `E_TASK_REPORT_SNAPSHOT_AUTHORITY_UNAVAILABLE`（registry 无 snapshot）。
- 证据：TokenSlim `git config --get remote.origin.url` 为空，HEAD =
  `2241401beaa87bee706e27702680396f299013b5`。
- 根因链：
  1. `server/daemon_client.py:1195-1217 _workspace_snapshot_metadata` 仅在
     remote **且** head 均非空时返回元数据 → TokenSlim 返回 `{}`。
  2. `rust_ext/src/daemon/workspace.rs:427-435`（新插入分支）与
     `:403-411`（adopt 分支）仅在 remote+head 均非空时调用 `compute_snapshot_id`，否则 `None`。
  3. `update_workspace_vcs_head`（`:689-695`）remote 为空时不生成 snapshot。
  4. `snapshot_state.rs:696-714` 发布时无请求值则继承 registry 值 → 继承到 null。
  5. `task_collab_lifecycle.rs:11-122 validate_report_snapshot_authority` 对 registry
     snapshot 为空的 workspace fail-closed。
- 结论：**无 `remote.origin.url` 的 checkout 在现有公式下不可能获得 snapshot 身份**，
  这是产品缺陷（本地仓库/私有镜像场景普遍存在），不是数据问题。

## 2. 设计

### 2.1 缺陷 A：CLI 只做权威转发（禁止本地派生）

**文件**：`cli/main.py`（唯一改动文件）

新增模块级 helper（紧跟 `_resolve_task_create_project_root`，约 L3713 之后）：

```python
def _resolve_caller_workspace_instance_id(db) -> str:
    """解析调用方项目根的 daemon 权威 workspace_instance_id（fail-closed）。

    多项目共享 daemon 时，CLI 绝不能本地推导 instance id：本地公式
    sha256(normalized_root) 与 daemon authority 公式不同，必然触发
    E_WORKSPACE_AUTHORITY_MISMATCH。统一走 registry/task-DB 双侧对账的
    resolve_workspace_pair_from_daemon；解析失败即拒绝（无本地回退）。
    """
```

行为：
1. `root = _resolve_task_create_project_root(db)`（复用既有 helper：`db.client_workspace_root`
   → cwd 项目根 → `E_WORKSPACE_ROOT_REQUIRED` fail-closed）。
2. `pair = resolve_workspace_pair_from_daemon(root)`；捕获 `DaemonUnavailableError` /
   `DaemonRemoteError` → 重抛 `DaemonUnavailableError(code="E_WORKSPACE_AUTHORITY_UNAVAILABLE")`，
   消息含 `root` 与原始原因（`from exc`）。
3. `pair["workspace_instance_id"]` 为空 → 同码 fail-closed。
4. 不调用 `derive_workspace_instance_id`。

改造 `task next-action` 分支（`cli/main.py:4687-4696`）：

- 显式 `--workspace-instance-id` **仍优先**（原样转发，由 daemon 按 binding 校验）；
- 否则 `_resolve_caller_workspace_instance_id(db)`；
- 解析失败不进入 RPC：`--json` 输出 `{"ok": false, "code": <exc.code>, "message": <msg>}`，
  人类可读输出 `[next-action] <code>: <msg>`，`return True`（保持既有错误呈现风格，
  退出码语义不变）。

改造 `task supersede` 分支（`cli/main.py:6032-6037`）：

- 删除本地 `detect_project_root + derive_workspace_instance_id`；
- 改为 `_sup_params["workspace_instance_id"] = _resolve_caller_workspace_instance_id(db)`；
- 解析失败：`cprint("[task-supersede] <code>: <msg>", "red")` + `return True`，
  不发 RPC（幂等 key 缺失时 daemon 也会 `E_SUPERSEDE_WORKSPACE_REQUIRED`，前置拒绝更清晰）。

**非目标**（本次不改）：
- MCP `server/tools/tools_task.py` 的同名路径（其 `task_next_step` 走 task-scoped 路由，
  不在本缺陷复现路径内）；
- `cli/main.py:13516` 的 graph/watch 派生；
- `server/daemon_registry.py` 的 Python legacy registry（生产走 Rust daemon）。

### 2.2 缺陷 B：daemon 生成恒非空 `snapshot_id`

**文件**：`rust_ext/src/daemon/workspace.rs`

1. 扩展 `compute_snapshot_id`（`workspace.rs:196-208`）签名与语义（**始终返回 16 位 hex**）：

```
compute_snapshot_id(workspace_instance_id, git_remote_url, git_head_commit_sha, toolchain_fingerprint)
  if !remote.is_empty() && !head.is_empty():
      sha256("{remote}|{head}|{fp}")[:16]        # 既有公式，逐字节不变（向后兼容已发布身份）
  else:
      sha256("instance:{instance}|{fp}")[:16]    # 无 remote 的 checkout：以不可变身份兜底
```

- 兜底分支**不含 head**：无 remote 仓库不因每次 commit 产生 snapshot 漂移
  （避免 report 在 commit 后恒失效）。
- 前缀 `instance:` 与 remote URL 永不冲突。

2. `register_workspace_preferred`（`workspace.rs:368-513`）三条写路径统一：

- **adopt 分支**（`:384-419`）：先读该行已有 `(git_remote_url, git_head_commit_sha,
  toolchain_fingerprint)` 作为"有效 provenance"回退；`snapshot_id` 用
  `compute_snapshot_id(legacy_id, eff_remote, eff_head, eff_fp)`（不再出现空串分支），
  UPDATE 直接赋值而非 `CASE WHEN ?3 <> ''`。
- **insert 分支**（`:421-511`）：按 `instance_id` 预读现有行的 provenance 作同样回退，
  `snapshot_id = Some(compute_snapshot_id(instance_id, eff_remote, eff_head, eff_fp))`
  （恒非空）；UPSERT 的 `snapshot_id` 直接取 `excluded.snapshot_id`；provenance 列的
  `CASE WHEN excluded.x <> ''` 保留（空值不抹除既有 provenance）。
- **legacy 兄弟行传播**（`:489-511`）：条件由 `if let Some(snapshot_id)` 变为恒执行；
  同时把 provenance 列的赋值改为 `CASE WHEN ? <> ''`，避免空值抹除历史 provenance。
  adopt 分支同样执行一次该传播（同一 owner + `normalize_identity_root(host_real_root)`
  的其余实例行），保证同一物理 checkout 的所有别名行都有 snapshot 身份。

3. `update_workspace_vcs_head`（`workspace.rs:657-705`）：调用新签名
  `compute_snapshot_id(workspace_instance_id, &remote_url, head_sha, &toolchain_fp)`；
  仅 remote 非空（有 provenance 公式）时重算，否则保持既有 snapshot（`""` → CASE 不覆盖）。

4. `snapshot_state.rs` / `task_collab_lifecycle.rs` **无需改动**：发布继承 registry 非空值，
   report 校验自然通过。

**非目标**：
- `server/daemon_client.py:_workspace_snapshot_metadata` 保持现状（head-only 上报不参与本次
  snapshot 公式；head 已由 `workspace.file.refresh → update_workspace_vcs_head` 上报）；
- Python legacy `server/daemon_registry.py` 公式不改。

### 2.3 Rust 单测同步（`workspace.rs` 内 `#[cfg(test)]`）

| 测试 | 现状 | 改后 |
| --- | --- | --- |
| `test_register_workspace_without_git_returns_null_snapshot_id`（:4473） | 断言 null | 改名 `..._returns_stable_non_null_snapshot_id`：16 hex；同 root 二次注册不变；不同 root 不同值 |
| `test_update_workspace_vcs_head_without_remote_clears_snapshot_id`（:4549） | 断言空 | 改名 `..._without_remote_keeps_snapshot_id`：head 写入成功、snapshot 非空且等于注册值 |
| `test_register_workspace_preserves_identity_and_provenance_on_empty_reregister`（:4483） | 不变 | 必须继续通过（空 re-register 不抹 provenance） |
| `test_register_workspace_updates_snapshot_for_legacy_identity_at_same_root`（:4578） | 不变 | 继续通过，追加 sibling 行为断言（可选） |

### 2.4 回归测试（Python）

- `tests/test_task_next_action_e2e.py`：
  - CLI 进程测试 4 处（:641 / :662 / :675 / :684）补显式 `--workspace-instance-id <ws>`
    （该 fixture 只播种 task DB capture，不注册 daemon registry，权威解析必然不可用，
    显式参数是唯一合法通道）。
  - 新增 1 条：未显式传参 + 项目根无 registry 配对 → `ok:false` 且
    `code` 以 `E_WORKSPACE_` 开头（fail-closed，不本地派生、不静默落到 callwarden）。
- 无 `derive_workspace_instance_id` 新调用点的 AST 断言（复用
  `tests/test_task_prompt_cli.py:152` 风格，针对 `_handle_task` 的 next-action/supersede 分支）。

## 3. 验证步骤（必须全部执行）

1. Rust：`cargo test --manifest-path rust_ext/Cargo.toml workspace`（或至少
   `workspace::tests`）+ `cargo check`。
2. Python：`python -m pytest tests/test_task_next_action_e2e.py -q`
   （无 daemon 候选时按既有 skip 规则处理）；如存在
   `tests/test_cross_project_workspace_binding.py` 一并执行。
3. 部署：`scripts/refresh_shared_runtime.ps1 -TaskId <本修复任务 id> -RestartMcp -RunSmokeTests`
   （重建 `C:\Users\wanpi\.callwarden\runtime\current\cw-daemon.exe`）。
4. 真机复验（TokenSlim cwd）：
   - `cw workspace list` / `cw workspace register`（如需要）→ 输出 `snapshot_id` 非空；
   - `cw collab publish` → 响应 `snapshot_id` 非空；
   - 新建一张 TokenSlim 临时探针卡（open）→ `cw task next-action <id> --json`
     返回 READY/CLAIM（无 `E_WORKSPACE_AUTHORITY_MISMATCH`），验证后关闭该探针卡；
   - `cw task report <task> <step> --snapshot-id <publish 返回的 id>` → 不报
     `SNAPSHOT_REQUIRED` / `SNAPSHOT_AUTHORITY_UNAVAILABLE`。
5. 回归：在 `c:\git_work\callwarden` 下对 callwarden 任务执行 `cw task next-action`，
   结果与修复前一致（不得因本次改动引入跨项目回归）。

## 4. 风险与回滚

- 风险 1：缺陷 A 使"未在 daemon registry 注册的项目"下 `next-action/supersede` 从
  "本地派生后由 daemon 拒绝"变为"前置 fail-closed"。这是预期行为（禁止客户端派生），
  错误码明确提示先 `cw workspace register`。
- 风险 2：缺陷 B 改变了"无 git provenance 时 snapshot 为 null"的产品语义。`snapshot.publish`
  显式传入旧 null 语义的客户端（无）不受影响；provenance 齐全的仓库公式不变。
- 回滚：两处改动均为独立文件级回滚（`git revert` 对应提交），无数据迁移、无 schema 变更。

## 5. 需要批准的决定

1. 缺陷 A：`next-action` / `supersede` 统一走 `resolve_workspace_pair_from_daemon`，
   解析失败**前置 fail-closed**（不再静默交由 daemon 报 mismatch）。
2. 缺陷 B：`snapshot_id` 改为**恒非空**；无 remote+head 时以
   `sha256("instance:{instance}|{fp}")[:16]` 兜底，且不随 head 漂移。
3. 验证阶段允许**新建并关闭**一张 TokenSlim 临时探针卡（不触碰既有任务卡状态）。
