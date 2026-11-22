# symbol-anchor · ws-identity（R2）

> 锚定方式：`get_file_symbols` + `get_symbol` + 源码回读（semantic_search 三次空结果，已降级声明）。全部符号经 daemon snapshot（generation 3）验证存在。

## 锚定表

| # | 设计元素 | 判定 | qualified_name | 行号 | 证据 / 备注 |
|---|---|---|---|---|---|
| 1 | 身份派生（Rust daemon 权威） | **extend** | `rust_ext/src/daemon/workspace.compute_workspace_instance_id` | 208-216 | 现公式 `sha256(owner_uid\|norm_root)[:16]`；形参 `_git_remote_url` 已存在但**未使用**；docstring 明言「Git remote 是 snapshot provenance，不是 workspace 身份」→ 方案 A 是对该决策的**有意反转**（决策来源：R0 用户拍板，理由：worktree 并行需仓库级身份） |
| 2 | 身份派生（Python 客户端 fallback） | **extend** | `server/daemon_client.derive_workspace_instance_id` | 188-209 | CR7 归一已有（realpath / 正斜杠 / ASCII 小写 / strip 尾斜杠）；无 `\\?\` strip、无 worktree/remote 概念。调用方：`daemon_client.py:316`（无 daemon 时 fallback）、`cli/main.py:13766`、`release/pyinstaller/entry_cw_agent.py:158` |
| 3 | 路径归一化（PL-06） | **reuse**（Rust 侧已具备） | `rust_ext/src/daemon/workspace.normalize_identity_root` | 380-396 | CR7（2026-09-09）已处理 `\\?\` verbatim 前缀、`\`vs`/`、ASCII 小写、尾斜杠 → PL-06 关闭条件的「注册入口归一」Rust 侧已满足；历史 10 行清理归 REQ-03 迁移 |
| 4 | worktree 主仓解析 | **extend** | `server/agent_protocol.probe_vcs_info` / `_resolve_git_head` | 107-158 / 161-178 | 已解析 `.git` 指针文件到 HEAD 路径（worktree/submodule 兼容）；未解析主仓 root、未返回 remote_url |
| 5 | remote 采集 | **reuse** | `server/daemon_client._workspace_snapshot_metadata` | 228-258 | register 参数已含 `git_remote_url`（`git config --get remote.origin.url`，stdin=DEVNULL 防管道卡死）——daemon 收到但 `compute_workspace_instance_id` 没用 |
| 6 | 注册权威链 | **reuse** | `rust_ext/src/daemon/workspace.WorkspaceRegistry.register_workspace_preferred` | 548-694 | CR7 自愈：`find_adoptable_instance`（L703+）按 owner+norm_root 收编分裂行 → `propagate_snapshot_to_siblings` → `archive_alias_rows`（单 active 不变量）。RPC 入口：`handle_workspace_register_preferred` L1994-2018、`handle_workspace_register` L2131-2137 |
| 7 | 旧→新 id 映射 | **reuse**（模式） | `rust_ext/src/daemon/workspace_reconciliation.record_reconciliation_alias` | 158-192 | append-only alias 表 + `alias_by_any_id` / `alias_by_instance` 查询面已有；迁移映射复用该模式 |
| 8 | task-DB authority 对齐 | **reuse**（需验证） | `rust_ext/src/daemon/workspace_reconciliation.resolve_create_authority` | 68-154 | `canonical_instance_id` 透传请求值；register 权威值变化后此链路自动跟随（O-02 验证点：workspaces 表 / grants 键域） |
| 9 | 矩阵单测（REQ-04） | **new** | — | — | 现有测试无 `\\?\`/remote 形式/worktree 主仓矩阵（`test_phase4_daemon_client.py` 仅同/异路径两例） |
| 10 | 三 worktree 端到端（REQ-05） | **new** | — | — | 现有进程级测试无多 worktree 场景 |

## 关键结论（影响 R3）

1. **A-01 成立**：register 返回值为权威（`daemon_client.py:3060-3063` 注释明确「以返回值为权威」），客户端 derive 仅为 fallback → daemon 侧升级即身份收敛，旧客户端在升级窗口由 daemon 兜底归一（O-01 关闭：无需双读兼容，但需回归「旧客户端先于 daemon 升级」的短暂窗口行为——旧 derive 只影响本地缓存键，不影响权威注册）。
2. **决策反转记录**：`compute_workspace_instance_id` 现设计刻意排除 remote；方案 A 反转之。卡片须记录：决策来源（用户 R0 拍板）、反转理由（跨 worktree 任务共享）、旧公式保留的兼容路径（迁移映射）。
3. **迁移核心 = CR7 收编机制扩展**：`register_workspace_preferred` 的 adopt 逻辑现按 owner+norm_root 匹配；方案 A 后匹配键改为 repo identity（remote 或主仓 root）。一次性迁移 = 用新身份重放存量行的 register（复用 adopt），非新写合并逻辑。
4. **O-02 待验证**：task-DB `workspaces` 表与 `workspace_principal_grants` 键域是否随 registry 合并自动对齐——R3 卡片前须回读 schema（`workspace_reconciliation.rs` + task DB DDL）。

## 回环检查

锚定结果与 R1 图无实质冲突：R1 的「daemon upsert + 迁移分支」对应 `register_workspace_preferred` 的 adopt 分支；「X1 fail-open」对应 `probe_vcs_info` 的 `vcs_kind: none` 路径与 `_workspace_snapshot_metadata` 的空值路径。无需回退 R0/R1。