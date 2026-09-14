# A′ 流水线在 WorkBuddy 中落地的执行方案

> 基于用户提供的 8 份文件：Lease/Fencing 评审、三套无人值守启动模板、两份迁移清单、两张运行图（A′ 解耦 SVG + A_prime 自动循环运行时 SVG）。

## 一、8 份文件的关键结论

1. **Rust HTTP daemon Lease/Fencing 只读评审（reviewer，结论：CHANGES REQUESTED）**
   - 方向正确（进程内写串行化、DB 单活 lease、单调 fencing counter），但存在 4 个阻断缺陷：
     - **B1**：实际生产路径仍走 `TaskCollabStore.dedup_cache`（内存 HashMap，最多 1000 条、重启即丢），而非持久化 `OperationStore`，断线/重启重试可能重复执行 mutation。
     - **B2**：`task.report` 是受保护写入却未校验 lease/fencing，旧 executor 被收回 lease 后仍可推进状态。
     - **B3**：`assignment → role → identity → lease` 未链式校验，任何同机调用方都可自报 `role=reviewer` 抢租约。
     - **B4**：HTTP 身份仅是 synthetic local owner，不可作为企业级抗伪造身份。
   - 核心判断：**当前只达到"进程内排他 + stale-writer fencing"，尚未完成端到端 durable mutation、assignment-bound authorization 与 attested multi-agent identity。** 这是 A′ 真正自主 apply/close 的最大硬前置。

2. **三套无人值守启动模板（Executor/Planner、Reviewer、Adjudicator v1）**
   - 都已内置完整"循环协议"：发现 → 资格核验 → 领取 → 实施/复审 → 交棒 → 重新发现。
   - 强制身份注册、workspace binding、role contract、结构化 handoff；默认 `E_IDENTITY_UNREGISTER 未注册即阻断`。
   - 运作形态是**每个角色独立 poll `task.next_action`**，不是单 agent 包揽三角色。

3. **两份迁移清单（草案 vs A′ 修订草案）**
   - 修订草案已修正旧草案的错误：不 reopen 已 closed 的 S1（`T-1787209886781-48b4cb0c`），改在 Epic `T-1787203926824-9f873bfc` 下新建恢复父任务。
   - 把执行/审查解耦为：Executor 滚动创建 N+1（默认窗口 `1 executing + 1 review + 1 next-created`）、Reviewer 异步消费、BLOCKED 进入队列批量返工、Gate 未 `applied` 前禁止同 port_type 后继。

4. **两张 SVG**：你贴的"执行与审查解耦"是原草图；`A_prime自动循环运行时.html` 是状态机可视化（角色 worker + `next_action` 派工内核 + BLOCKED 重审闭环）。

## 二、可行性判断

A′ 循环**可以表达为 WorkBuddy 自动化**，但需分两个阶段：

- **阶段 A（dry-run 试点，今天即可）**：三角色各自按 `task.next_action` 跑 poll，只做"发现→核验→交棒"（不含 apply/close）。可行，能验证派工正确性。
- **阶段 B（自主 apply/close，需先补 daemon 缺口）**：真正自动收尾依赖 B1–B4 落地；否则自动 apply 会绕过 fencing，违反 fail-closed 设计。

## 三、与 WorkBuddy 自动化的映射

WorkBuddy 自动化 = "定时/周期跑一个 prompt"。A′ 的"机械 Coordinator"恰好对应"调度器"，三个角色 worker 对应三个独立 recurring 自动化：

| A′ 概念 | WorkBuddy 落地 |
|---|---|
| Coordinator（机械调度） | 自动化调度本身（rrule 周期触发） |
| Executor worker | 独立 recurring automation（加载 Executor/Planner 启动模板） |
| Reviewer worker | 独立 recurring automation（加载 Reviewer 启动模板） |
| Adjudicator worker | 独立 recurring automation（加载 Adjudicator 启动模板） |
在 prompt 内强制（建后继前查 gate `applied`） |
| task.next_action 派工 | 自动化 prompt 首步即 `cw task next-action <epic> --json`，按 `decision` 行动 |

**推荐"三任务各自独立 recurring"而非单编排 agent**：A′ 强制角色/会话隔离，每个 automation 都是独立 agent 回合，天然满足 `executor ≠ reviewer ≠ adjudicator`，与"Coordinator 非治理角色、只机械调度"的设计一致。

## 三之二、worktree 隔离（推荐用于阶段 A 的三角色文件隔离）

WorkBuddy（CodeBuddy Code）自 **v2.55.0** 起原生支持 git worktree（官方文档 `https://www.workbuddy.cn/docs/cli/worktree`），正好补齐 A′ 一直缺的"角色/会话文件隔离"机制，是对 daemon lease/fencing（评审里 B1–B4 尚未补完）的更稳替代，尤其适合阶段 A 的 dry-run 试点：

- **子代理隔离（`isolation: worktree`）**：在 `.codebuddy/agents/` 的自定义 Agent frontmatter 加 `isolation: worktree`，每次用 Task 工具启动该角色 worker，系统自动为它建独立 worktree；三角色可并行改同名文件互不干扰，主仓库保持干净。这是让每个角色 worker 拿到独立文件上下文的最直接方式。
- **会话内/启动参数**：对话里说"启动 worktree"即调 `EnterWorktree` 工具；或用 `codebuddy --worktree [name] --worktree-branch origin/develop --tmux` 起独立会话跑长任务。worktree 建在 `.codebuddy/worktrees/`，自动建分支并切换目录。
- **环境复用**：`settings.json` 的 `worktree.symlinkDirectories` 软链 `node_modules` 等避免重复装依赖；`.worktreeinclude` 同步 `.env.local` 等被 gitignore 的本地文件。
- **非 Git 兜底**：SVN/Perforce 等项目可通过 `WorktreeCreate`/`WorktreeRemove` hooks 获得等价隔离。

**对 A′ 的意义**：把每个角色 worker 做成 worktree 隔离的 worker，能天然满足 A′ 的"角色隔离 + 单写者约束"——Executor 独占热文件的写入在独立 worktree 内完成，Reviewer/Adjudicator 只读其产物，不再纯粹依赖 `task.next_action` 的逻辑门禁来保证不互相污染。如此阶段 A 试点即便 daemon 的 B1–B4 未补完，也能在文件层获得真实的并行安全隔离。

## 四、推荐落地步骤

1. **先补 daemon 缺口（B1–B4）** = 阶段 B 的前提；阶段 A 可并行试点。
2. **阶段 A 试点**：创建 3 个 recurring automation（每 5–10 分钟），prompt 末尾带对应启动模板的"循环协议"，先 dry-run（只 report/handoff，不 apply/close）。
3. **验证派工正确**后，再放开真实 apply/close（阶段 B），并把滚动窗口 gate 与 `BLOCKED→fix_defect` 原子追加作为验收项。

## 五、未决风险

- **身份注册**：WorkBuddy 自动化的 agent 会话需映射到 callwarden 的 `agent_id/session_id`，否则触发 `E_IDENTITY_UNREGISTERED`；需确认 automation 运行时的身份注入方式。
- **daemon 断线/重启**：B1 未修前，自动 apply 在断网重试时可能重复执行或消失凭证。
- **治理层级**：WorkBuddy 自动化 prompt 与 A′ 的"独立 Reviewer/Adjudicator"不是同一信任域；跨域授权仍需显式约定。

## 六、下一步

- 若你确认，我可以用 `automation_update` 创建 3 个 **dry-run** 试点自动化（executor / reviewer / adjudicator），各自加载对应启动模板、prompt 首步查询 `task.next_action`。
- 同时建议把 Lease/Fencing 的 B1–B4 作为阶段 B 待办，否则自动 apply/close 不安全。

下一步需要你确认两点：①试点是否采用"三角色独立 recurring"方案；②是否先 dry-run（不触碰 apply/close）还是直接放开。
