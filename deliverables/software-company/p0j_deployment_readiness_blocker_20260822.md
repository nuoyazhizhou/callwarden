# P0-J 运行时部署准备度：阻断记录

**关联任务**：`T-P0J-ROLE-WORKER-IDENTITY`  
**状态**：`BLOCKED` 于受控部署入口；工作树实现与本地测试证据已单独记录，未宣称生产已更新。

## 已核验事实

项目 AGENTS.md 指定 daemon 修改后必须使用 `scripts/refresh_shared_runtime.ps1` 进行受控构建、运行时替换、旧进程处理、manifest/hash 验证和 smoke test。该脚本的参数守卫要求：

```powershell
if ($TaskId -notmatch '^T-[0-9]+-[0-9a-z-]+$') {
    throw "TaskId 必须是实际任务 ID，例如 T-1786346158666-e9316534；禁止使用占位符"
}
```

P0-J 的真实、已落库 task ID 是 `T-P0J-ROLE-WORKER-IDENTITY`，不满足该守卫。因而把 P0-J 传给唯一批准的刷新脚本会被确定性拒绝。

| 备选做法 | 结论 | 原因 |
|---|---|---|
| 使用 P0-J 的真实 ID | 拒绝 | 不满足脚本当前正则，无法进入构建/部署流程 |
| 改填父任务 ID | 禁止 | 会把 P0-J 运行时部署的证据错误归属到其他 task |
| 编造合规数字 ID | 禁止 | 违反 task identity、审计和 append-only 约束 |
| 手工替换 runtime/停止生产 daemon | 禁止 | 绕过项目规定的受控刷新、hash/manifest/smoke 验证路径 |
| 修改刷新脚本以支持 opaque ID | 需要独立 scope/任务与评审 | 属于 deployment governance tool 变更，不在现有 P0-J 已冻结实现范围内 |

## 当前 authority 的只读状态

生产 HTTP authority 健康可读，但其 binary 尚未包括 P0-J 路由；只读调用 `role_worker.status` 返回 `method_not_found`。因此尚未执行 schema v60 migration、worker enrollment 或 CLI-02/CLI-03/MCP-001 bootstrap。生产 SQLite 未通过任何直接路径写入。

## 合规后续

需要由拥有独立 scope 的 executor 创建或获准接管一项最小 deployment-governance 修复：使 `refresh_shared_runtime.ps1` 可安全接受实际 CW task IDs（包括当前 opaque P0-J 格式），并保持日志、hash、manifest、rollback 与 task attribution 不变。该项必须先经过独立 Reviewer，再部署 P0-J binary；不得以当前任务外的 ID 绕过。

> 本阻断不是 P0-J 实现失败，也不是生产 daemon 异常。它是受控部署入口与已存在真实 task ID 格式不兼容所导致的 fail-closed 结果。
