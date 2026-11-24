# 证据 T-1795466682197-466d7dd0 · 步骤 S-1795466682200-46a39320：orca 关键实现文件定位

- 任务：T-1795466682197-466d7dd0「深读 orca：worktree 并行编排实现」
- 步骤：S-1795466682200-46a39320（step_index 0，action=定位关键实现文件（3-5 个））
- 认领身份：agent_id=callwarden-exec-worker-fa2e，role=executor，session=callwarden-exec-worker-fa2e-sess（daemon claim 响应 assignment_id=A-08e2a28d3eb237cccf236b0e，step_status=in_progress）
- 分析对象：`c:/git_work/callwarden/testcode/orca`，HEAD `02a1251c2d721a29e3b69848bc793f1e3aa21ef0`（"fix(native-chat): classify diff lines whose content begins with -- or ++ (#12459)"），package `orca@1.4.176-rc.1`
- run 目录：`c:\git_work\callwarden-worker\runs\run-20261124-145339-fa2e`（worktree，origin/master @ 9efefd0）
- 取证方式：源码 Read + Grep 逐字核验（orca 在分析报告里属「结构 + README 级」，本轮深读为源码级定位；所有行锚点均已用 Read/Grep 在本轮实际核对，未依赖目录名/README 猜测）

## 一、候选方向 → 文件映射（步骤检查项 1）

| 候选方向（任务卡给定） | 命中文件 | 证据形态 |
|---|---|---|
| worktree 管理（创建/列出/删除/git 封装） | `src/main/git/worktree.ts`（git 原语本体，addWorktree L909 / listWorktrees L744 / removeWorktree L1109）、`src/main/ipc/worktrees.ts`（应用级生命周期编排，registerWorktreeHandlers L1839 / create L2222 / remove L2394） | 已 Read/Grep 核验行锚点 |
| 并行编排与调度（fan-out/派发/并发控制） | `src/main/runtime/orchestration/coordinator.ts`（单类闭环调度，Coordinator L84 / dispatchReadyTasks L354 / dispatchTask L398）、`src/main/runtime/orchestration/db.ts`（编排状态机 + 派发锁，OrchestrationDb L295 / createDispatchContext L6156） | 已 Read/Grep 核验行锚点 |
| 任务隔离与合并（会话隔离/squash 合并/分支安全清理） | `src/shared/git-branch-cleanup.ts`（merge-tree + patch-id 三重安全证明，L109/L178/L229）、`src/main/runtime/agent-session-claim-identity.ts`（HMAC 会话- worktree 绑定，AgentSessionClaimSigner L88 / createClaim L101） | 已 Read/Grep 核验行锚点 |

## 二、选定的 5 个核心实现文件（步骤检查项 2）

1. **`src/main/git/worktree.ts`（1638 行）— Parallel Worktrees 的 git 机制本体**。对外创建入口 `addWorktree` L909-933：`runWithGitReadCacheInvalidation` 包裹实际创建，`finally` 中 `bumpWorktreeScanGeneration(repoPath)` L931 使并发 list 不复用过期扫描。真正机制在 `performAddWorktree` L935：拼 `['worktree','add']` L946；检出已有分支走 `args.push(worktreePath, branch)` L953，新建分支走 `--no-track -b <branch> <path>` L956（Why 注释 L955 说明 `--no-track` 避免继承 base upstream 导致 push 前误报 behind）；base ref 解析 `resolveWorktreeAddBaseRef` L958；超时 `WORKTREE_ADD_TIMEOUT_MS=180_000` L95、执行点 L982-986。并发列表 `listWorktrees` L744 用 `inFlightWorktreeScans` L694 + generation 合并同一 repo 的并发 `git worktree list`（命中缓存 L755、finally 清理 L760-761）。删除 `removeWorktree` L1109 / `performRemoveWorktree` L1125；大目录删除先 rename 到 trash 再异步清 `tryRemoveWorktreeWithDeferredDirectoryDeletion` L1184；安全删除失败后的合并分支强删 `deleteAlreadyMergedBranchAfterSafeDeleteFailure` L1345。

2. **`src/main/runtime/orchestration/coordinator.ts`（546 行）— 并行 fan-out 调度的完整闭环（单文件可读完）**。文件头注释明示单类聚合以避免 split-brain；`Coordinator` 类 L84，`MAX_CONCURRENT_DEFAULT=4` L79（默认注入 L101）、`DISPATCH_STALE_THRESHOLD=20` L43。`executeLoop` L140 在 `decompose()` 后 `while(!stopped) tick()` 轮询。`dispatchReadyTasks` L354：可用槽位 `slotsAvailable = maxConcurrent - dispatched.length` L362，每 tick 最多创建一个 worker terminal（L369-375），逐个占 handle 派发 L382-395。`dispatchTask` L398：base drift 超过阈值且未 allowStale 时跳过（L416，且不消耗熔断预算——见其 Why 注释 L399），`getAvailableTerminals` L485 排除 coordinator 自身/busy/断连终端。决策门 `processDecisionGates` L342 明确不自动放行 gate（人工 `orchestration.gateResolve`），与 callwarden 人工关口语义同构。

3. **`src/main/runtime/orchestration/db.ts`（6807 行）— 编排的持久化状态机与并发正确性来源**。`OrchestrationDb` 类 L295。`createTask` L3774 建立任务 DAG（依赖校验、无依赖 ready / 有依赖 pending）；`promoteReadyTasks` L3949 在状态更新事务内把依赖全 completed 的 pending 任务提升为 ready（调用点 L3941/L6517）。`createDispatchContext` L6156 是并发正确性的核心：仅 `ready` 任务可派发（L6168-6170），按 handle + pane 身份双重加锁 `findActiveDispatchForAssignee` L6172-6179（防止 handle 重签发后同一 pane 双派发），跨重试累加 `failure_count` 供熔断（L6181-6185）。`failDispatch` L6554 区分熔断/回退状态。这是第 2 项 coordinator 行为的数据层基础。

4. **`src/shared/git-branch-cleanup.ts`（282 行）— 合并方向最强的机制型文件（worktree 删除时判断分支能否安全清理）**。`getBranchCleanupTargetRefs` L48 收集目标 ref，`refreshBranchCleanupTargetRefs` L65 删前 `fetch --prune`（网络失败不致命）。`branchMergesWithoutTreeChanges` L109-139 用 `git merge-tree --write-tree <target> <branch>` L115 得到合并 tree，与 target tree 比较 L132-138 证明无额外改动。squash 合并识别 `branchNetPatchMatchesTargetSquashCommit` L178：算 merge-base 与 branch 的 stable patch-id L174，扫描目标侧提交并逐个比对 patch-id + tree 证明（Why 注释 L217 说明 patch-id 匹配仅标识"可能的" squash commit，仍需 tree 证明）。汇总 `branchHasNoUnmergedChangesOnAnyTarget` L229 与带远端 ref 刷新的 `...WithLazyTargetRefresh` L261。

5. **`src/main/ipc/worktrees.ts`（3473 行）— 把第 1 项 git 原语接成应用级生命周期（create/remove/list）**。`registerWorktreeHandlers` L1839 注册全部 worktree IPC（先 removeHandler 去重 L1847-1853）。`worktrees:listAll` L1872 / `worktrees:list` L1935 聚合 git 注册信息与 Orca 自有 meta/lineage。`worktrees:create` L2222 按 folder repo / SSH remote / 本地三路径分派并统一 provenance/埋点。`worktrees:remove`（handler 注册块首行 L2394，通道名 L2395）：in-flight map 按 `inFlightKey` + `optionsKey` 去重并发双击/过期 toast 的重复删除（L2404-2412，命中直接复用 promise L2409），再按 host（folder/SSH/runtime）分派清理，folder 路径先 sweep PTY L2425。

### 辅助读取（不计入 5 个核心文件）

- `src/main/runtime/agent-session-claim-identity.ts`（177 行）：会话隔离实现质量很高——`AgentSessionClaimSigner` L88、`createClaim` L101 用 HMAC-SHA256 分别对 agent 身份字段（L112-120）与 worktree scope 字段（L121-126）签名得 `identityDigest` / `worktreeScopeDigest` L130-135，使同一 agent session 不能跨 worktree 冒用；`canonicalizeAgentSessionIdentity` L56 对 `pi` 要求绝对路径并 `realpathSync` L52；authority key 以 `0o600` 原子创建 L154。更贴近身份/所有权认证，作为隔离方向的强补充，未挤占前五名额。
- `src/main/runtime/orchestration/worker-topology`（`src/main/runtime/rpc/methods/orchestration-worker-topology.ts`，282 行）：`createWorkerWorktree` 展示"每个 worker 一个 worktree"的隔离创建与 lineage，属拓扑辅助层。
- `src/shared/agent-scratch-worktrees.ts`（63 行）：scratch 路径前缀判定器（`.claude/worktrees` 等），被 `worktree-ownership.ts` 使用。
- `src/relay/git-handler-worktree-ops.ts`（189 行）：SSH relay 侧对本地 `addWorktree` 的镜像实现，覆盖面窄。

### 负向选择（为什么不选）

- `src/main/ipc/worktrees.ts` 之外的 renderer `worktree-*-selector.ts` / `worktree-repo-index.ts`：纯 selector/WeakMap 缓存薄封装，非机制实现。
- 大量 `*.test.ts`（worktree/coordinator/orchestration 相关）：测试件，非实现文件。
- `src/relay/agent-hook-server.ts`（533 行）：hook 服务端，与三方向主链关联弱；`src/relay/git-exec-validator.ts`：git 子命令白名单，关联弱。
- `docs/`、`resources/`、`skills/`：文档/资源/skill 定义，非实现文件。

## 三、工具/CLI 往返证据（acceptance: one tool/CLI link + daemon round-trip）

- **daemon RPC 往返（成功，本轮实测）**：
  - `task.list`（workspace_id=1, instance=4baea3ff12c2ea5c）→ 取到本任务卡（title「深读 orca：worktree 并行编排实现」，step_id=S-1795466682200-46a39320）；脚本 `runs/_get_task_fa2e.py`。
  - `agent.register` → `{"status":"registered","agent_id":"callwarden-exec-worker-fa2e","agent_instance_id":"callwarden-exec-worker-fa2e-inst","session_id":"callwarden-exec-worker-fa2e-sess","role":"executor"}`（脚本 `runs/_claim_fa2e.py`）。
  - `task.contract_get` → executor Role Contract prompt_hash `59A459F7786097C671D48FBEEC6E361C12D7A95BDEC4E3722169D68D5D6A73F6`，skill_id=none。
  - `task.claim` → 成功（status=in_progress，step S-1795466682200-46a39320，assignment_id=A-08e2a28d3eb237cccf236b0e），原始响应见本脚本 stdout。
  - 注：本轮未执行 `cw daemon health` / `cw daemon status` 显式健康预检（worker 指令要求由 manifest 自动发现 endpoint，`verify_health=False`）；daemon 连通性以以上 4 次 RPC 成功往返为证。
- **源码工具链（本轮实测）**：`git -C testcode/orca rev-parse HEAD` → `02a1251c...`；`wc -l` 核对 6 个候选文件行数与结论一致；`grep -nE` 复核 15+ 行锚点全部命中（含 db.ts createTask 实际 L3774、failDispatch 实际 L6554，均以本轮核对值为准，未沿用初筛近似值）。

## 四、后续步骤实施计划（required_evidence: implementation plan）

1. 逐个深读 5 个核心文件，按「可直接落地 / 需改造 / 不适用」三分类提取借鉴点，每条带 `file:line` 证据；重点对齐 orca 三大方向：worktree 管理（git 原语 + 并发扫描 + 延迟删除 + 合并后安全清理）、并行编排与调度（并发槽位 + 单飞建终端 + drift 门禁 + 熔断 + 人工 gate）、任务隔离与合并（DAG + 双身份派发锁 + HMAC 会话绑定 + squash patch-id 证明）。
2. 回填 `docs/reports/testcode-11-repo-analysis-2026-11-23.md`：新增「orca 实现级深读」段（编号接续 goodbuddy 的 63-73 之后，从 74 起），更新第一节规模表（orca 标注深读）、一·补深度分级表（orca 从「结构 + README 级」移入「实现级深读」）、第二节可借鉴清单追加 orca 条目、第四节一句话定位（补深读要点）。
3. 负向验证：抽查每条引用在源码中逐字核对（防 file:line 漂移类缺陷）；确认无「未读文件下结论」；自检三分类覆盖。
4. `workspace.register` 刷新 snapshot → `task.report`（success）→ 视响应 claim 后续步骤直至 review。

## 五、本轮环境记录

- orca 源目录为只读分析对象，本轮未修改其中任何文件；交付物仅写入 worktree 内 git 跟踪的 `docs/evidence/`（后续步骤再写 `docs/reports/`）。
- 行锚点全部来自本轮对源目录的实际 Read/Grep 输出。
