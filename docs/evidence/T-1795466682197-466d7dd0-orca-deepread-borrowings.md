# 证据 T-1795466682197-466d7dd0 · 步骤 S-1795466682201-46a401ac：orca 深读可借鉴点

- 任务：T-1795466682197-466d7dd0「深读 orca：worktree 并行编排实现」
- 步骤：S-1795466682201-46a401ac（step_index 1，action=逐个深读并提取可借鉴点）
- 认领身份：agent_id=callwarden-exec-worker-fa2e，session=callwarden-exec-worker-fa2e-sess（assignment A-314bf098b87096418dadb0ec）
- 分析对象：`testcode/orca` HEAD `02a1251c2d721a29e3b69848bc793f1e3aa21ef0`
- 方法：对步骤 0 选定的 5 个核心文件 + 1 个辅读文件逐个 Read 机制段，每条借鉴点带 `file:line`，按「可直接落地 / 需改造 / 不适用」三分类
- 三分类口径：**可直接落地** = callwarden 现有 Rust daemon / task 链可近乎照搬的设计；**需改造** = 思路成立但要按 callwarden 单 daemon / SQLite / 治理角色语义改造；**不适用** = orca 特有假设（Electron pane / 预创建任务 / SSH relay）在 callwarden 不成立，仅作反例或未来项

## 一、可借鉴清单（14 条，编号接续分析报告，对接时从 74 起）

### A. worktree 管理（根因对标：callwarden 构建可用性 / 并发扫描）

1. **【可直接落地】单飞扫描 + mutation generation 防陈旧读**（`src/main/git/worktree.ts:694-766`）
   - `inFlightWorktreeScans: Map<key, Promise>` L694 合并同一 repo 的并发 `git worktree list`；缓存 key 含 repo/distro/timeout/generation 四元 L754（"不同 deadline 不能安全共享哪个 timeout 胜出" Why 注释 L753）。`worktreeScanGenerations` L697 + `bumpWorktreeScanGeneration` L709 在写操作后递增（addWorktree finally L931），使已进入的旧扫描不被 join；`pruneWorktreeScanGeneration` L717 保证普通结算 O(1)。
   - **对 callwarden 的意义**：直接对标 `build_graph` 同步阻塞超时短板——把 query.* 的 repo 扫描改成「同 key 单飞 promise + generation 失效」，写操作（索引刷新）bump generation，避免并发请求各自跑一遍 + 拿到过期图。

2. **【需改造】写后立即失效 + 失败不吞错**（`src/main/git/worktree.ts:790-793`、`918-932`）
   - `listWorktreesUnshared` 对 git-compat/repo-state 失败 `console.warn` 后返回 []，但有明确 Why L790「不要吞成 opaque 的 'created but not found in listing'」；addWorktree 用 `runWithGitReadCacheInvalidation` 包裹且 `finally` 无条件 bump generation L918-932（异常路径也失效）。
   - **对 callwarden 的意义**：`snapshot.publish` / 索引写入后必须在 `finally` 里刷新快照与缓存，不能只在成功路径刷新；但 callwarden 是强一致治理系统，不能像 orca 那样把失败吞成空结果——需改造为「返回明确错误码 + 强制失效」。

3. **【需改造】延迟目录删除避免阻塞 UI**（`src/main/git/worktree.ts:1184-1200`）
   - `tryRemoveWorktreeWithDeferredDirectoryDeletion` 先把目录 rename 到 trash、再清 git 注册、异步删大目录；带 WSL 例外（WSL-owned checkout 在 distro 内删，Node 不 rename，L1187-1189）与 force=false 下先证干净（L1193-1199 "git worktree remove 自己会复查 cleanliness，这里证同一件事否则交给 Git"）。
   - **对 callwarden 的意义**：worktree 清理/大图谱目录回收可参考「先改名摘注册、后台删除」；callwarden 无 WSL/Electron UI，需改造为「先移出活跃 workspace 注册表、后台 GC」，但要保留 force=false 的干净性证明语义。

4. **【不适用】SSH / folder / runtime 多宿主分派**（`src/main/ipc/worktrees.ts:2394-2434`）
   - remove 按 local/SSH/runtime 三种 host 分派，PTY sweep 按 ownerHost fence（"本地 inventory 绝不触达远端 workspace id" L2427-2430）。
   - **定性**：callwarden daemon 单机 SQLite / 单 host，无 SSH worktree 与 PTY 概念；仅借鉴「按 owner fence、路径派生元数据不可跨 host 复用」的**权限语义**，与已做的 workspace_instance_id 消歧同构，不照搬实现。

### B. 并行编排与调度

5. **【需改造】单类 Coordinator 避免 split-brain + tick 收敛循环**（`src/main/runtime/orchestration/coordinator.ts:84, 140-186, 205-212`）
   - `Coordinator` 单类聚合消息/升级/gate/派发/收敛（文件头注释明示避免 split-brain）；`executeLoop` decompose 后 `while(!stopped) tick()`，收敛即 break，早停计 failed（L161-173）；`tick` 顺序 = 处理消息→升级→决策门→陈旧告警→派发→收敛判定 L205-212。
   - **对 callwarden 的意义**：callwarden 的 A′ 角色环（planner/executor/reviewer/adjudicator）本身就是多 agent 编排，但状态分散在 daemon。可借鉴「单一收敛判定 + 每轮固定顺序 + 早停计失败」的状态机收敛语义；需改造为 daemon 侧治理投影而非 Electron 单类。

6. **【可直接落地】并发槽位 + 每 tick 最多建一个 worker**（`src/main/runtime/orchestration/coordinator.ts:354-396`）
   - `slotsAvailable = maxConcurrent - dispatched.length` L362（默认 maxConcurrent=4 L79）；无可用终端时每 tick 只创建一个 terminal（L369-375 "避免一次 spawn 多个"），逐个占 handle 派发 L382-395。
   - **对 callwarden 的意义**：直接可用于并行 worker 接力池——并发上限按 `in_progress` vs `open` 计数、每次运行只认领一个任务（正是本 worker 的并发模型），此设计与之同构，可作为形式化依据。

7. **【需改造】base drift 门禁：可恢复跳过不烧熔断预算**（`src/main/runtime/orchestration/coordinator.ts:398-427, 485`）
   - 派发前 `probeWorktreeDrift`，behind > `DISPATCH_STALE_THRESHOLD`（20，L43）且未 allow-stale 时**静默 return**（L416-425），任务留 `ready` 下轮重试；Why L417 明确「这里 failDispatch 会烧掉可恢复陈旧的熔断预算」。`getAvailableTerminals` L485 排除自身/busy/断连。allow-stale 是 spec 里的 infra flag，preamble 里被 `strippedSpec` 剥离（L448-449）。
   - **对 callwarden 的意义**：治理里「可恢复条件不满足」应与「真失败」区分计数——快照过期/版本落后应重试不累计失败，避免误触发 circuit；且基础设施开关不能泄漏进 worker 指令（对标 callwarden prompt 字段 daemon 权威、客户端不 derive）。

8. **【需改造】决策门人工裁决、系统绝不自动放行**（`src/main/runtime/orchestration/coordinator.ts:342-352`、`db.ts:6586-6599`）
   - `processDecisionGates` 只 re-block 不 auto-resolve（L343 Why "会让 gate 失去审批点意义"），resolved gate 的结论经 preamble 回灌 worker（L460-466）；`createGate` 存 question/options L6586-6599。
   - **对 callwarden 的意义**：与 A′ 环 reviewer BLOCKED 不得降级 PASS、handoff 必须带结构化结论完全同构；借鉴「gate 结论经 dispatch preamble 显式回灌下一棒」的做法，强化 callwarden handoff 块的可执行性。

9. **【不适用】AI 任务分解未实现（诚实反例）**（`src/main/runtime/orchestration/coordinator.ts:192-203`）
   - `decompose()` 明确注释「未实现，任务必须预创建，AI 分解是未来阶段」，无任务直接抛错。
   - **定性**：不能把 orca 吹成「AI 自动拆解编排」——它只是 DAG 派发器。对 callwarden 的启示是反面：若宣称 planner 自动拆分，必须真实现 contract 卡片，不能只做壳（对标 callwarden planner design-only 的诚实口径）。

### C. 任务隔离与合并

10. **【可直接落地】DAG 依赖提升在状态更新事务内**（`src/main/runtime/orchestration/db.ts:3774-3803, 3941, 3949`）
    - `createTask` 校验依赖同 run、无依赖 ready / 有依赖 pending L3794-3803；`promoteReadyTasks` L3949 在任务完成的状态更新事务内扫描 pending、依赖全 completed 则置 ready（调用点 L3941/L6517）。
    - **对 callwarden 的意义**：task 子步骤依赖（父子任务门禁）可借鉴「状态更新与依赖提升同一事务」，避免并发下出现「依赖已完成但子任务仍 pending」的中间态——直接对标 callwarden 父子任务 apply/close 门禁的原子性。

11. **【可直接落地】按 handle + pane 双身份派发锁**（`src/main/runtime/orchestration/db.ts:6156-6185`）
    - `createDispatchContext` 仅 ready 可派发 L6168-6170；`findActiveDispatchForAssignee(handle, paneKey)` L6172-6179 双锁防「handle 重签发后同一 pane 双派发」；跨重试累加 `failure_count = MAX(...)` L6181-6185。
    - **对 callwarden 的意义**：与 callwarden lease 三元组 / fencing 完全同构——「逻辑身份（pane）+ 进程实例（handle/incarnation）」双键正是 callwarden `agent_instance_id + session_id` 独立性的另一例证；可直接用于强化 claim 的幂等与 fencing。

12. **【可直接落地】熔断 = 3 次失败 + 失败回退 ready 不回 pending**（`src/main/runtime/orchestration/db.ts:6554-6582`）
    - `newFailureCount >= 3 → circuit_broken`（L6563，CHECK 约束 L561）；任务状态 `circuit_broken→failed / 否则→ready`（L6576），Why L6575「回 pending 会搁浅，因为 promoteReadyTasks 只在依赖完成时跑」。
    - **对 callwarden 的意义**：worker 失败重试的收敛上限（本指令「失败不重试超过一次」）可借鉴「失败次数持久化 + 熔断阈值 + 回退到可重试态而非搁浅态」，且失败计数跨重试累加不被重置。

13. **【需改造】squash 合并三重安全证明**（`src/shared/git-branch-cleanup.ts:109-139, 178-227, 229-261`）
    - 三证明：`branchMergesWithoutTreeChanges` 用 `git merge-tree --write-tree` 比对 tree（L109-139）；`branchNetPatchMatchesTargetSquashCommit` 取 merge-base 的 stable patch-id（`patch-id --stable` L166/L174），扫目标侧最多 `SQUASH_PATCH_SCAN_LIMIT=200` 提交（L9/L197-210），逐个比对 patch-id **且**再做 tree 合并证明（L212-225，Why L217「patch-id 匹配只是可能的 squash，tree 证明才确认无额外改动」）；`git cherry` patch 等价（L146）；删前 `fetch --prune` 刷新目标 ref（L86，网络失败不致命）。
    - **对 callwarden 的意义**：callwarden 合并 worktree 分支前的安全删除判定可借鉴此「patch-id + tree 双证 + bounded scan + 删前刷新远端」；需改造为 Rust git 调用与 callwarden 的 workspace 所有权校验联动。

14. **【需改造】HMAC 会话- worktree 绑定，同一 session 不跨 worktree 冒用**（`src/main/runtime/agent-session-claim-identity.ts:56-85, 88-138, 141-164`）
    - `AgentSessionClaimSigner.createClaim` 用同一 key 对 identityFields（L112-120）与 worktreeFields（L121-126）分别 HMAC-SHA256 → `identityDigest` / `worktreeScopeDigest` L130-135；authority key 以 `0o600` 原子创建（L154，损坏则拒绝而非静默失效）；`canonicalizeAgentSessionIdentity` 对 pi 要求绝对路径 + `realpathSync` L52。
    - **对 callwarden 的意义**：与 callwarden `snapshot`/`lease` 的身份绑定与 fail-closed 哲学一致；可借鉴「跨 scope 双 digest + key 校验失败即拒绝」强化 agent 跨 workspace 冒用防护，需改造到 callwarden 的 mTLS/PKI 体系而非 orca profile key。

## 二、负向验证（required_evidence: negative test）

- **反例防护**：第 9 条明确标注 orca 的 decompose 是未实现的空壳，避免把「AI 自动分解」当成既成借鉴点——这是本轮主动找出的「名实不符」反例。
- **行锚点逐字核对**：步骤 1 引用的全部行号均已在本轮 Read/Grep 实际输出中命中（34/34，脚本见步骤 0 证据；本步骤新增引用 L192-203/L342-352/L416-449/L515/L6586-6599/L166/L9/L86/L52/L154 等亦逐条 Read/Grep 核对）。
- **无未读下结论**：14 条全部来自对 5 核心文件 + 1 辅读文件的实际机制段阅读，未依赖 README/目录名推断。

## 三、daemon 往返证据

- 步骤 1 claim：`agent.register` → `task.claim` 成功（assignment A-314bf098b87096418dadb0ec，step S-1795466682201-46a401ac），身份 callwarden-exec-worker-fa2e-sess，与步骤 0 同 session（executor 连续步骤无需换人；独立性由与 reviewer 的不同 agent_instance_id/session_id 保证）。
- 步骤 1 report 时按指令 `workspace.register` 取新 snapshot_id，不复用步骤 0 的 `cc2bb4558e479239`。

## 四、后续步骤计划

1. 回填 `docs/reports/testcode-11-repo-analysis-2026-11-23.md`：新增「orca 实现级深读」段（编号 74-87 接续 goodbuddy 63-73），更新一·补深度分级表（orca 结构+README 级 → 实现级深读）、第一节规模表、第二节清单、第四节定位。
2. 提交并推送 → report → claim 下一步直至 review。
