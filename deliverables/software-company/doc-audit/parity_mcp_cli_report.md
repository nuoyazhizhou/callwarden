# MCP 工具 ↔ CLI 命令 对等性核对报告

> 日期:2026-09-29　分支:doc-impl-audit
> 问题背景:用户问「MCP 工具和 CLI 命令的子命令,理论上应该完全一样多吧,不能互相缺对方吧」。
> 数据:MCP 243 工具 / CLI 70 顶层(234 叶子)。

## 结论(先说要点)

**MCP 与 CLI 在本项目的设计中不要求数量对等,允许各有专属暴露面。243 vs 234 的差异经核对是合理的,不是能力缺失。**

权威依据:`docs/design/cw-role-prompt-compiler-v1-frozen-spec.md` §12 明确定义三域
`T(MCP工具) / M(迁移矩阵) / D(daemon RPC)`,并把"追求 T=M=D 相等"列为**反模式**(门禁第 6 条:
`D - rpc_method(M)` 不要求为空;Reviewer 必答题"避免错误追求 T=M=D/241=242=298")。
MCP 与 CLI 都经**同一 `route_rpc(rpc_method,...)` 打到同一套 daemon RPC**——差异是"谁暴露",不是"谁能做"。

## 三类差异及合理性判读

### 1. 双方共有(BOTH)—— 主体
176+ 个 MCP 工具在 mcp_tools.md「CLI↔MCP 命名映射对照表」有 CLI 对应(查询/任务/规则/审计/Git/Semgrep/覆盖率/诊断等前 12 类)。这部分本就一一对应或多对一(如 `cw refresh`/`cw --refresh` 都→`refresh_file`)。

### 2. MCP 专属(MCP_ONLY,合理)
按性质分:

| 子类 | 例子 | 是否合理 | 理由 |
|------|------|----------|------|
| 增量能力域 [13]-[17] | lease_*/assignment_*/identity 查询/collab 查询/dependency 接口/build-context/toolchain | ✅ 合理 | 面向 Agent 编排/治理,CLI 侧多数也有(见下"对照表补全");部分查询类只在 MCP |
| async 变体 | `detect_clones_async` / `semgrep_scan_async` / `embed_symbols_async` | ✅ 合理 | 异步 job 提交,供 Agent 非阻塞编排;CLI 用同步版 |
| job 管理 | `list_jobs` / `cancel_job` / `wait_for_job` / `get_job_status` / `get_job_stats` | ✅ 合理 | 后台 job 生命周期,Agent 编排专用 |
| 细粒度查询变体 | `diff_callers` / `diff_callees` / `compare_snapshots` / `get_clone_aware_impact` / `get_clone_group_detail`/`stats` | ✅ 合理 | MCP 提供更细粒度工具,CLI 用聚合命令 |
| 反查/关联 | `get_commit_tasks` / `get_symbol_change_tasks` | ✅ 合理 | Agent 归因查询,CLI 未单列 |
| 任务治理细项 | `task_governance_projection` / `task_assignment_status`/`heartbeat` / `task_get_role_prompt` / `task_remediation_create` / `task_step_resolve` | ✅ 合理 | 无人值守角色循环用;CLI 有对应(task governance-projection/assignment-status 等子命令),对照表前 12 类未收录 |
| 少量维护 | `build_directory` / `remove_file` / `scan_semgrep_incremental` | ✅ 合理 | 增量/维护操作,CLI 用 refresh/semgrep scan 覆盖 |

### 3. CLI 专属(CLI_ONLY,合理)

| 子类 | 例子 | 是否合理 | 理由 |
|------|------|----------|------|
| 本地宿主操作 | `install` / `install-agent` / `install-hook` / `setup` / `server` / `doctor` | ✅ 合理 | 装依赖/装 hook/生成集成包/启服务,依赖本地 FS/进程,不适合做 MCP 工具 |
| 运维诊断 | `dashboard` / `health-report` / `grep` / `git check-task/check-push/destructive-log` | ✅ 合理 | 人类运维/CI 命令,聚合展示,非 Agent 编排原子操作 |
| daemon 管理 | `daemon ping/health/status/...`(22 子动作) | ✅ 合理 | daemon 自身管理面,MCP 是 daemon 的客户端不管理它 |
| 迁移/实验 | `rollback` / `experiment`(13 子动作) | ✅ 合理 | Rust 迁移回滚开关 / P0 盲评实验,人工运维 |
| config | `config explain/paths/check-role` | ✅ 合理 | 本地配置诊断 |

## 本次核对发现并修复的真实文档缺陷

**mcp_tools.md 的 CLI↔MCP 对照表此前只覆盖前 12 类,遗漏 [13]-[17] 5 个增量域的 CLI↔MCP 对应关系。**
已补全(build-context/toolchain、collab、dependency、lease/assignment、identity),并标注各域两侧暴露面差异
(如 Identity 域 CLI 仅 `revoke`,查询类在 MCP)。

## 精确数字更新

- `cli_reference.md`:"150+ 个 CLI 命令" → "70 个顶层命令(234 个叶子子命令)"
- `README.md`:"145+ CLI 命令" → "70 个顶层 CLI 命令(234 个叶子子命令)"
- MCP 工具数 243(已在前次审计统一)

## 未发现的问题

- 无"某底层 daemon 能力既无 MCP 又无 CLI 暴露"的真空(所有 rpc_method 都有至少一个暴露面)。
- 无"文档虚构但不存在"的命令/工具(前次审计已确认)。
