现在的  Executor 、Reviewer、Adjudicator 还是戳一戳动一动，在Executor  收到 、Reviewer 和 Adjudicator BLOCKED 的 消息的时候，应该主动寻求解决方案完成任务，除非有多条线路需要选择，不然直接就应该开一个子任务，然后解决了，Reviewer 和 Adjudicator 在review 代码的时候，blocked 的时候，应该把问题的根因说清楚，而不是只给一个 blocked 就结束，并且非本次任务的bug 也应该 一起告诉 Executor ，而不是假装看不见。 你说呢？难道一个架构师 review 一个 合并请求的时候，执看本次任务修改的问题，不管上次提交已经合并进去但是有问题的代码？难道 Reviewer 和 Adjudicator  测试的时候，只管本次修改的代码，不管因为修改导致引入的其他问题？这符合逻辑吗

你说得对。现在的行为不符合真正的代码审查流程，根因是我们把 `BLOCKED` 当成了“结束消息”，而不是“可执行整改事件”。

正确模型应该是：

1. Reviewer/Adjudicator 发现问题后，必须提交结构化 finding，至少包含：

   - 精确 `task_id`、`step_id`
   - 问题根因
   - 复现步骤和证据
   - 影响文件、调用链和回归范围
   - 严重级别
   - 是否由本次改动引入
   - 建议修复动作

2. `BLOCKED` 后不能停在聊天文本中。Daemon 应在同一事务中：

   - 持久化 verdict/finding；
   - 将任务置为 `remediation_pending`；
   - 自动追加可领取的 `fix_defect` step；
   - 将根因和全部 findings 交给 Executor；
   - `next_action` 明确指向 Executor。

3. Executor 收到 `reviewer_blocked` 或 `adjudicator_returned` 后应自动继续：

   - 读取完整 finding；
   - 判断是否存在唯一明确修复路径；
   - 有唯一方案就直接领取并实施；
   - 只有存在多条互斥方案、权限缺失或范围无法安全确定时才请求用户；
   - 修复完成后重新 report，回到 Reviewer。

4. Reviewer 的审查范围不能只看本次 diff：

   - 本次修改直接影响的调用链；
   - 集成后的运行时行为；
   - 因本次修改引入的旁路回归；
   - 上次已经合并但被本次触发或暴露的问题；
   - 与本任务强相关的历史缺陷。

   这些问题都必须报告，不能为了“本次任务没改这个文件”而假装不存在。

但需要分类，避免审查范围无限膨胀：

- 本次改动引入的回归：阻塞当前任务。
- 本次改动直接暴露且影响交付的缺陷：阻塞当前任务。
- 与当前交付无关的独立历史问题：记录为 finding，并自动创建关联整改任务，但不阻塞当前任务，除非属于安全、数据损坏或核心正确性问题。
- 纯观察项：记录为 advisory，不改变状态。

Adjudicator 也不能只验证“Reviewer 说通过了什么”。它应该验证整体交付是否可接受，包括本次改动造成的回归和 Reviewer 报告的非本次缺陷。发现问题时必须返回完整 findings，而不是只返回一个 `BLOCKED`。

因此真正需要修的不是模板措辞，而是四层同时闭合：

- daemon：结构化 finding、自动 remediation step、状态投影和派工；
- CLI/MCP：展示根因、影响范围和下一责任方；
- 三份模板：强制审查 blast radius 和完整 findings；
- skill/AGENTS：要求 Executor 自动消费整改，不等待用户戳动。

同一交付线的修复应自动追加主任务的 `fix_defect` step；只有独立 ownership、独立验收范围的非本次 bug，才创建带 `related_to`/`discovered_by` 关系的子任务。这样既能自动推进，也不会用嵌套子任务掩盖原任务的失败历史。


而且我们在  T-1787801315246-e3e3a08c [review / review\_pending] P0-L：Role Worker Task Contract policy / preclaim enforcement remediation  得到的教训就是，任务太大，多轮下来都没有解决问题，最后变成拆解成 11个子任务来完成，这说明 Executor  领取任务后没有仔细思考就开始干活了，他应该思考复杂度，然后决定要不要拆分任务，你觉得呢？ 甚至我们应该拆出一个专门的 planner 角色，专门负责分析需求，拆解任务，Executor   拿到任务后需要分析复杂度，有问题的打回给 planner

我同意。`T-1787801315246-e3e3a08c` 暴露的不是单个实现错误，而是“任务规划阶段失职”：

- 同时涉及 schema、migration、lease、worker session、授权、CLI/MCP、测试和部署；
- Executor 没有先评估复杂度；
- 领取后直接编码；
- 多轮 Reviewer/Adjudicator 只能不断打回；
- 最终才拆成 11 个子任务。

这说明 Planner 和 Executor 的职责需要分开。

建议改成四阶段角色模型：

```text
Planner
  ↓ 形成可执行任务树
Executor
  ↓ 实现单一 scope
Reviewer
  ↓ 独立复核
Adjudicator
  ↓ apply / close
```

Planner 不负责写生产代码，主要负责：

- 分析需求和影响范围；
- 识别 schema、daemon、CLI、MCP、测试、部署等不同 ownership；
- 判断是否需要拆分；
- 生成依赖图、执行顺序、每个任务的白名单和排除范围；
- 为每个子任务生成完整 Contract、binding、验收条件和证据要求。

Executor 领取任务后也不能立即开工，必须先做一次复杂度预检。以下任一条件满足，就应退回 Planner：

- 涉及 3 个以上独立模块；
- 同时修改 schema、daemon、CLI/MCP；
- 包含 migration、授权、协议或部署变更；
- 有多个相互独立的验收目标；
- 需要超过一个 Reviewer 独立验证范围；
- 预计需要 5 个以上实施步骤；
- 当前任务描述无法明确列出 allowed/excluded paths。

Executor 的正式结果不应只有 `blocked`，而应增加：

```text
executor_replan_requested
```

并附：

```text
task_id
complexity_findings
why_current_scope_is_not_atomic
proposed_task_breakdown
dependency_order
affected_paths
excluded_paths
acceptance_per_task
```

如果只是一个明确、局部的缺陷，则由 Executor 直接领取同一主任务下的 `fix_defect` step；如果需要改变架构、拆分 ownership 或重新定义验收，则返回 Planner 重规划。不能让 Executor 自己随意创建一串子任务，否则又会产生嵌套混乱。

状态模型也需要增加规划状态：

- `planning_pending`
- `planning_in_progress`
- `execution_ready`
- `execution_in_progress`
- `replanning_pending`

其中：

- Planner 完成拆解后，任务进入 `execution_ready`；
- Executor 发现范围过大，进入 `replanning_pending`；
- Planner 修订后，旧计划保留为历史版本，生成新的 append-only revision；
- Reviewer 也可以因“任务边界不可验证”而提出重规划 finding；
- Adjudicator 不应替 Planner 拆任务，只能退回并说明缺陷。

另外，Planner 创建的任务必须原子写入：

- task；
- workspace binding；
- role contracts；
- identity policy；
- steps；
- parent/dependency relation；
- acceptance/evidence contract。

任何缺失都应该让 `task.create` 失败，而不是先创建一个半成品任务再让后续角色反复撞治理门禁。

所以最终原则应该是：

> Planner 保证任务可执行，Executor 保证实现正确，Reviewer 保证实现和影响范围完整，Adjudicator 保证最终交付可接受。

P0-L 的 11 个子任务本来就应该在 Planner 阶段产生，而不是经过多轮失败后才补拆。



对的，在AI 时代，尽量减少程序员介入的情况下完成任务才是目标，给你看一个agent提示：



我不能在当前用户 authority 上绕过 P0-L v3 的治理门禁、手工导入 P0-L.0 或伪造/跳过 Reviewer 写入。这会直接破坏你要求验证的 fail-closed 链路，也会让后续 A″ 任务树再次建立在可绕过的状态机上。

开发阶段可以采用**隔离的开发 authority**，而不是放开真实 authority：使用临时 SQLite/临时 role-session fixture，仅允许测试 RPC 构造 `reviewer_blocked → P0-L.0`，跑完即销毁；生产/当前用户的 `T-1787801315246-e3e3a08c`、真实 lease、凭据与任务事件保持不变。这样既不会卡住实现调试，也不会污染真实治理历史。

若你确认，我会把这一条作为临时开发验证路径：先修复 controlled refresh 脚本的 Python probe，再对隔离 authority 做真实 HTTP round-trip 测试；只有 live authority 的受控发布成功后，才由独立 Reviewer 处理真实 P0-L。





Executor 预检被一个明确的身份前置条件阻断：当前 Windows 会话中 `CW_AGENT_SESSION_ID` **未设置**。你指定要求该值必须是本窗口独立 session，且禁止使用 SID；我不能猜测、复用旧 reviewer/adjudicator session，或省略该字段来领取任务。

请提供本窗口应使用的独立 `CW_AGENT_SESSION_ID`（或先在当前执行环境设置该环境变量后告知我已设置）。收到后我会立即：读取全部 `in_progress` 任务确认单任务纪律 → 扫描 Epic 子树的 Executor next-action → 续做或领取唯一合法候选。

**Handoff**
```yaml
from_role: executor
outcome: executor_blocked_to_user
next_role: user
next_action: 提供或设置独立的 CW_AGENT_SESSION_ID。
reason: Executor identity 的 session_id 为必填字段，当前环境未设置且不得猜测或复用其他治理角色 session。
independence_requirement: not_applicable
```



你看这两个提示，用户很难搞清楚要回复什么，其实应该变成选择题，然后下一步补一个 文本框，这样万一前面的选择都不对，或者不完成的时候让用户补充，而且应该题干和选项尽量的明确，不然在多任务切换的情况下，程序员可能根本很懵逼，不知道发生了什么事情


对，这两个提示暴露的是严重的交互设计问题：系统把内部治理错误原样甩给用户，却没有告诉用户“现在发生了什么、为什么停、该选什么、选择后会怎样”。

应该改成“结构化阻断卡”，而不是自由文本 Handoff。

```text
任务：T-1787801315246-e3e3a08c
当前状态：governance_blocked
当前责任方：Executor
需要你的决定：是 / 否

发生了什么：
当前真实 authority 缺少生成 P0-L.0 remediation step 的能力。

为什么不能自动继续：
直接写入会绕过 Reviewer 和治理审计，破坏历史记录。

推荐方案：
[A] 使用隔离开发 authority 验证路径（推荐）
    只创建临时 fixture，执行真实 HTTP round-trip，完成后销毁；
    不修改真实任务、lease、凭据和数据库。

[B] 修复 live daemon 后再继续
    先实现正式 reviewer_blocked → fix_defect 路由，
    然后重新处理真实 P0-L。

[C] 暂停任务
    保持当前状态，不执行任何写入。

请选择：A / B / C
补充说明（可选）：
[文本框]
```

对于 `CW_AGENT_SESSION_ID` 缺失，也不应该直接要求用户手动提供一串 ID。程序应该优先自动生成当前窗口的独立 session，并只在确实无法自动修复时询问：

```text
任务：<精确 task_id>
阻断原因：当前 Executor 会话没有独立 session_id。

[A] 为当前窗口自动生成独立 session（推荐）
    不复用旧 Executor、Reviewer 或 Adjudicator session。

[B] 在当前终端设置 CW_AGENT_SESSION_ID
    设置完成后重新执行领取。

[C] 取消本轮任务
    不领取、不修改任务状态。

请选择：A / B / C
补充说明（可选）：
[文本框]
```

更重要的是，这个选择卡必须由 daemon 返回结构化数据，CLI、MCP 和 Agent 只负责渲染，不能各自解析错误文本：

```json
{
  "task_id": "T-1787801315246-e3e3a08c",
  "workflow_status": "decision_pending",
  "blocking_reason": "reviewer_blocked_remediation_route_unavailable",
  "question": "当前应使用哪种恢复路径？",
  "recommended_choice": "isolated_dev_authority",
  "choices": [
    {
      "id": "isolated_dev_authority",
      "label": "使用隔离开发 authority",
      "recommended": true,
      "effect": "仅验证，不修改真实治理历史"
    },
    {
      "id": "repair_live_daemon",
      "label": "先修复 live daemon",
      "effect": "实现正式治理路由后再继续"
    },
    {
      "id": "pause",
      "label": "暂停任务",
      "effect": "保持当前状态"
    }
  ],
  "free_text_allowed": true,
  "resume_action": "task.resume"
}
```

因此状态模型还应该区分：

- `governance_blocked`：治理事实缺失或不一致；
- `decision_pending`：存在多条合法路径，需要用户选择；
- `user_input_required`：缺少补充信息；
- `remediation_pending`：已有明确整改路径，Executor 应自动领取；
- `replanning_pending`：任务复杂度过高，等待 Planner 重拆。

核心规则应是：

- 有唯一安全方案：Agent 自动执行，不询问用户；
- 有多条会产生不同结果的方案：显示选择题；
- 选项都不适用：使用补充文本框；
- 缺少 session、workspace、binding 等可自动恢复的信息：优先系统自动修复；
- 所有选择和补充内容都要写入结构化事件，不能只留在对话里；
- 所有提示第一字段必须是精确 `task_id`，不能出现“当前任务”。

Planner 的复杂度判断也应该使用同样的选择卡。例如 Executor 发现 P0-L 过大时：

```text
任务复杂度：高
检测到：11 个独立验收目标、schema/daemon/CLI/MCP 多层变更、多个发布门禁。

[A] 接受建议拆分为 11 个串行子任务（推荐）
[B] 保持单任务，但按 11 个步骤执行
[C] 返回 Planner 重新设计拆分方案
补充说明（可选）：
```

这样既能减少程序员介入，又不会让 Agent 在存在重大分歧时擅自替用户做架构决策。这个机制需要同步到 daemon 的错误/派工协议、CLI、MCP、三份模板、skill 和 `AGENTS.md`，不能只修改提示词。


说的对，这就是我们要解决的问题，我相信也是任何大量使用agent 编程的人的困扰，也是 同时使用多个agent 编程的人的困扰


完全同意。这不是 Callwarden 独有的问题，而是多 Agent 编程的核心基础设施问题：

> Agent 可以负责执行，但不能依赖聊天记录来维持流程状态。

大量使用 Agent、多个 Agent 并行时，最容易出现三类问题：

1. 任务状态和真实执行状态不一致；
2. 一个角色失败后没有自动恢复，必须人工“戳一下”；
3. 阻断信息只存在于自然语言中，下一位 Agent 不知道具体任务、根因和下一动作。

所以我们真正要建设的是一个“Agent Workflow Control Plane”：

- daemon 持久化任务、finding、handoff、决策和恢复事件；
- `next_action` 返回机器可执行的下一步；
- Executor 自动消费 Reviewer/Adjudicator 的整改；
- 复杂任务自动进入 Planner 重规划；
- Reviewer 检查变更影响范围和回归，而不是只看 diff；
- 只有存在多条合法路径或缺少用户授权时才询问用户；
- CLI、MCP、Agent、用户看到的是同一份结构化状态；
- 所有任务都必须携带精确 `task_id`，不能依赖聊天上下文；
- 用户的选择也写入 daemon，后续 Agent 可自动继续。

最终用户不应该再看到：

```text
BLOCKED，请处理
```

而应该看到：

```text
任务 T-xxx 当前需要决策：
发生了什么：……
阻断原因：……
推荐方案：A
可选方案：A / B / C
选择后系统将执行：……
```

而对于唯一明确的修复路径，用户甚至不应该被打断，Agent 应自动完成整改并重新进入审查。

这会成为 Callwarden 从“任务 CLI + Agent 提示词”升级为真正多 Agent 协作平台的关键能力。



对的，而且现在我们还需要程序员在agent 上点击选择A、B、C，实际上程序员应该在一个类似 jira 的工单系统里面选择，也就是说agent 可以运行在远端或者后台，交互应该是分离的

对，这才是适合远程和后台 Agent 的形态。

聊天窗口应该只是观察和调试界面，不能再承担任务调度、人工选择和状态恢复。真正的交互应放到一个类似 Jira 的控制台中。

目标架构可以分成三层：

```text
Agent Worker
  Executor / Reviewer / Adjudicator
          ↓ HTTP/API
Callwarden Control Plane
  task / event / finding / decision / assignment
          ↓ API / webhook
Human Work Console
  Jira 类工单、审批、选择、评论、通知
```

Agent 遇到需要人工决策时，不应该输出：

```text
请用户回复 A、B、C
```

而应该调用 daemon 创建持久化的 `decision_request`：

```json
{
  "decision_request_id": "DR-xxx",
  "task_id": "T-xxx",
  "state": "open",
  "question": "当前应采用哪种恢复路径？",
  "recommended_choice": "isolated_dev_authority",
  "choices": [
    {
      "id": "isolated_dev_authority",
      "label": "使用隔离开发 authority",
      "effect": "仅验证，不修改真实治理历史"
    },
    {
      "id": "repair_live_daemon",
      "label": "先修复 live daemon",
      "effect": "修复后重新执行任务"
    }
  ],
  "free_text_allowed": true,
  "required_role": "user"
}
```

此后：

1. Agent 将任务置为 `waiting_for_decision`；
2. Jira 类控制台显示待处理工单；
3. 程序员在工单中选择 A/B/C、填写说明或转交；
4. 控制台调用 daemon 的 `decision.respond`；
5. daemon 追加不可变决策事件；
6. 调度器唤醒对应 Agent；
7. Agent 从 `task.next_action` 读取决定并继续执行。

这样即使 Agent 在远端服务器、容器、后台进程或夜间运行，也不依赖当前聊天窗口存在。

需要特别区分几种状态：

- `remediation_pending`：有明确修复方案，Agent 自动继续；
- `replanning_pending`：任务过大，交 Planner 重拆；
- `waiting_for_decision`：有多条合法路线，等待人工选择；
- `waiting_for_input`：缺少必要事实，等待人工补充；
- `governance_blocked`：系统治理事实损坏，不能继续，需治理修复；
- `completed`：真正完成，不代表“有人说完成”。

控制台至少需要支持：

- 按 `task_id` 展示完整任务链；
- 显示当前责任角色和 Agent；
- 显示根因、finding、证据和影响范围；
- 展示待处理 decision request；
- 选择、评论、补充文本、转交和取消；
- 查看所有历史事件和决策者；
- 通过 webhook 或轮询通知后台 Agent 恢复；
- 对重复提交使用 `decision_request_id` 幂等处理。

CLI 和 MCP 仍然可以提供：

```text
cw decision list
cw decision show <decision_request_id>
cw decision respond <decision_request_id> --choice <choice_id>
```

但它们只是控制台的客户端，不应成为 Agent 必须等待的交互入口。

核心原则是：

> Agent 负责执行和判断；daemon 负责持久化和调度；工单系统负责人工决策；聊天只负责展示。

这才是真正支持多 Agent、远程 Agent 和无人值守运行的闭环。

是的，市场已经在走这条路，但目前还没有形成统一标准。

Cursor 已经提供了异步远程 Background Agents：Agent 在隔离环境中运行，用户可以查看状态、发送后续指令或接管执行。[Cursor Background Agents](https://docs.cursor.com/background-agent)

Atlassian 也在把 Agent 放进 Jira 工作流：Agent 可以后台运行，任务经过工作流审批，所有动作记录在工单历史和审计日志中。[Jira Agent Guardrails](https://www.atlassian.com/software/jira/guides/agentic-engineering/guardrails-and-safety) [Jira Human-in-the-loop](https://www.atlassian.com/software/jira/guides/agentic-engineering/human-in-the-loop)

Factory 的定位更接近我们设想的方向：覆盖规划、执行、审查、发布，支持多 Agent 协作、后台运行、风险分级、验证门禁、审计记录以及云端、私有云和本地部署。[Factory Software Factory](https://factory.ai/product/software-factory)

所以市场已经证明两件事：

1. Agent 可以脱离当前 IDE 或聊天窗口，在后台持续工作；
2. 人类应该在工单、审批和审计界面进行关键决策，而不是回复 Agent 的聊天消息。

Callwarden 的商业价值不应该是再做一个 Cursor 或 Jira，也不应该从通用 Issue Tracker 正面竞争。更准确的定位是：

> 面向多 Agent 软件工程的中立控制平面和治理执行系统。

它的差异化在于：

- 同时调度 Claude、Codex、Cursor、Devin、内部 Agent 等不同执行者；
- Planner、Executor、Reviewer、Adjudicator 可独立运行；
- 自动处理 BLOCKED、整改、重规划和任务恢复；
- 保存任务、证据、代码提交、审批、finding 和决策的完整链路；
- 发现跨任务回归，而不是只审查当前 diff；
- 支持本地、远端、私有部署和离线环境；
- 不把关键状态放在聊天上下文中。

商业价值主要来自四个方面：

- 减少程序员等待和手工协调，让 Agent 在夜间、远端持续工作；
- 减少多 Agent 反复打回、状态丢失和治理死循环；
- 让企业可以审计“哪个 Agent 做了什么、谁批准了什么、依据是什么”；
- 让企业在不被单一 Agent 厂商绑定的情况下统一管理多种模型和工具。

建议的商业切入点不是立即替换 Jira，而是：

```text
Jira / Linear / GitHub
        ↓
Callwarden Agent Control Plane
        ↓
多个远程 Agent / CI / 代码仓库 / 测试环境
```

Jira 继续作为人类的项目和审批入口，Callwarden 负责 Agent 的执行状态、任务分派、治理、证据和自动恢复。这样更容易接入现有企业流程，也更容易证明价值。

最小可商业化版本可以先做：

- Jira/GitHub/Linear 工单同步；
- Agent 后台任务队列；
- `decision_request` 人工决策卡；
- 自动 remediation/replan；
- 多 Agent assignment；
- 代码、测试、部署和审计证据；
- 团队级 Agent 运行看板。

商业收费可以按 workspace、活跃 Agent、任务运行量或企业部署收费。核心销售指标不应是 token 数，而应是：

- 从需求到合并的周期；
- Agent 自动完成比例；
- 人工介入次数；
- Reviewer 发现后的自动修复率；
- 回归缺陷率；
- 每个合并变更的成本；
- 事故修复时间。

因此，这个方向有商业价值，但真正的产品壁垒不是“能不能调用 Agent”，而是：

> 能不能让几十个 Agent 在真实工程流程中长期、可恢复、可审计地协作，而不需要程序员不断充当人工调度器。

这正是目前 Callwarden 最值得继续建设的核心。


好，按这个规划远端，我们现在先给 cw 增加一个独立角色 Planner  ，增加 Planner   和 Executor  的中间状态和往复流程，而且给 Planner  架构师的skill，Executor 高级开发工程师和架构师的 skill ，这样才能更正常，你说呢


我同意，而且这是目前最应该补上的结构。Planner 应该成为独立的一等角色，不再只是 Executor 的工作模式。

但需要注意：Planner 是“规划和拆解责任人”，不是第四个裁决者。`apply/close` 仍只能由 Adjudicator 执行。

建议流程：

```text
Planner
  ↓ plan.submitted
execution_ready
  ↓
Executor
  ↓ report
Reviewer
  ↓ pass
Adjudicator
```

当 Executor 发现任务过大或边界错误：

```text
Executor
  ↓ replan.requested
replanning_pending
  ↓
Planner
  ↓ plan.revised
execution_ready
```

建议新增状态：

- `planning_pending`
- `planning_in_progress`
- `execution_ready`
- `replanning_pending`
- `replanning_in_progress`

新增结构化事件：

- `plan.submitted`
- `plan.rejected`
- `replan.requested`
- `plan.revised`
- `task.split`

Planner 必须在 Executor 领取前完成：

- 需求和调用链分析；
- 复杂度评估；
- 是否拆分的决定；
- 子任务依赖顺序；
- 每个任务的 allowed/excluded paths；
- 验收命令；
- 证据要求；
- workspace binding、Role Contract 和 identity policy。

Executor 领取后必须先做范围预检。发现以下情况时，不能直接编码，应提交 `executor_replan_requested`：

- 同时涉及多个独立 domain；
- schema、daemon、CLI/MCP、部署混在一起；
- 有多个独立验收目标；
- 预计需要超过 5 个步骤；
- 当前 Contract 无法明确限定修改范围。

Planner 的 skill 应定位为“架构师/任务规划师”，重点是：

- 复杂度识别；
- 领域边界划分；
- 依赖图；
- 迁移顺序；
- 风险和回滚；
- 可验证验收设计；
- 子任务 Contract 生成。

Executor 的 skill 应定位为“高级开发工程师 + 架构实现者”，重点是：

- 先理解架构再编码；
- 严格执行 scope；
- 运行测试和真实 round-trip；
- 主动识别回归；
- 判断继续实现还是请求重规划；
- 完成后提交完整证据。

新任务默认应进入 `planning_pending`，只有明确标记为 `atomic_hotfix` 且通过复杂度预检的简单缺陷，才允许直接进入 Executor。这样不会因为小修复增加不必要的流程，但 P0-L 这种复合任务一定会先被 Planner 拆开。

另外，这次改动不能只增加一个 `planner` 字符串。需要同步修改：

- daemon role registry 和 Role Contract；
- task lifecycle/workflow projection；
- `task.next_action` 路由；
- task create/split/plan/replan RPC；
- CLI/MCP 输出；
- Planner、Executor、Reviewer、Adjudicator 模板；
- `AGENTS.md`；
- 对应 skills；
- 历史任务迁移和兼容策略。

最关键的一条是：

> `open` 不再默认等 Executor；没有经过 Planner 的可执行计划，Executor 就不应该看到可领取的执行任务。

这会直接解决 P0-L 那种“任务太大但 Executor 先开干，反复失败后才拆分”的问题。

我理解这组变更，而且顺序正确：

1. 新增独立 Planner 角色和架构师 skill；
2. 更新 Executor skill；
3. 修订三份启动模板；
4. 同步修改 `AGENTS.md`；
5. 最后再改 daemon、CLI/MCP 和状态机代码。

但当前不能直接开始，因为我名下的：

`T-1787850432491-f42a2b8c`

仍是 `in_progress / governance_blocked`，且任务 scope 是拆分 `task_collab.rs`，不包含这组 Planner/模板/skill 改动。按单任务纪律，我不能一边保留该任务未完成，一边领取或修改另一个任务，也不能擅自扩大旧任务范围。

当前没有修改任何文件。

```text
Handoff:
  task_id: T-1787850432491-f42a2b8c
  step_id: T-1787852751299-d7edabb0
  from_role: executor
  outcome: executor_blocked_to_user
  next_role: user
  next_action: 先解决当前任务缺少唯一 Role Contract binding 的治理阻断，或明确将 Planner/skill/模板/AGENTS 改造纳入当前任务 scope
  reason: 当前任务仍为 in_progress/governance_blocked；新需求属于独立的角色模型和流程改造，不能无记录并入 task_collab.rs 拆分任务
  independence_requirement: not_applicable
  request_id: unavailable
  report_request_id: unavailable
  evidence_path: unavailable
  evidence_hash: unavailable
  identity:
    agent_id: executor-workbuddy-v1-cur
    agent_instance_id: unavailable
    session_id: sess-executor-codex-20260828-taskcollab
    model_id: workbuddy
    role: executor
```

已完成第一阶段文档和 Skill 修订：

- 新增 [cw-planner-architect/SKILL.md](C:/git_work/callwarden/.agents/skills/cw-planner-architect/SKILL.md)
- 新增 [cw-executor-senior-engineer/SKILL.md](C:/git_work/callwarden/.agents/skills/cw-executor-senior-engineer/SKILL.md)
- 更新 [cw-task-loop/SKILL.md](C:/git_work/callwarden/.agents/skills/cw-task-loop/SKILL.md)
- 更新 [AGENTS.md](C:/git_work/callwarden/AGENTS.md)

已落实：

- Planner 独立角色；
- `planning_pending`、`planning_in_progress`、`execution_ready`；
- `replanning_pending`、`replanning_in_progress`；
- Planner → Executor；
- Executor 复杂度预检；
- `executor_replan_requested` 回到 Planner；
- Planner/Executor 专属 Skill；
- `READY/PLAN` 角色卡；
- 完整 Handoff 支持 Planner。

两个新 Skill 均通过 `quick_validate.py`。

提交：

- `5452bdc0ed9d912999d071952c25b086a622b1e8`
- `6d2646a997e47dbd78f71e97da57be30cb1a3b1f`

证据：[planner-role-skill-update-evidence.md](C:/git_work/callwarden/deliverables/software-company/T-1787850432491-f42a2b8c-planner-role-skill-update-evidence.md)

注意：这一步已经把角色行为规范固定下来，但 daemon 还没有实现 Planner 的真实注册、状态流转和自动派工；下一阶段再修改代码和 CLI/MCP。当前正式 report 仍受旧任务的 Role Contract binding 阻塞。

```text
Handoff:
  task_id: T-1787850432491-f42a2b8c
  step_id: T-1787852751299-d7edabb0
  from_role: executor
  outcome: executor_blocked_to_user
  next_role: user
  next_action: 补齐当前任务的唯一 Role Contract binding 后，再提交正式 report；随后实施 daemon Planner 状态机和派工
  reason: AGENTS、cw-task-loop、Planner/Executor Skill 已完成并提交，但当前任务治理仍为 governance_blocked
  independence_requirement: not_applicable
  request_id: unavailable
  report_request_id: unavailable
  evidence_path: C:/git_work/callwarden/deliverables/software-company/T-1787850432491-f42a2b8c-planner-role-skill-update-evidence.md
  evidence_hash: sha256:0d2322e063e92bea5e9f254cdba99f221d476d2561ba006fcf9569be91f739ec
  identity:
    agent_id: executor-workbuddy-v1-cur
    agent_instance_id: unavailable
    session_id: sess-executor-codex-20260828-taskcollab
    model_id: workbuddy
    role: executor
```


需要，而且不能简单把所有文件合并成一个“大 Skill”或一个“大模板”。正确做法是“职责拆开，共享规则集中”。

### `.agents/skills` 的处理

建议保留并分别维护：

- `cw-planner-architect`：Planner 专属，负责复杂度分析、拆分、依赖和 Contract。
- `cw-executor-senior-engineer`：Executor 专属，负责实现、整改、回归和交付。
- `cw-task-loop`：只做只读派工和角色卡渲染，不属于任何治理角色。
- `g0-experiment`：G0 专用流程，不能和普通任务 loop 合并。

不建议把 Planner 和 Executor 合并。两者如果合并，Agent 很容易再次“自己规划、自己立即开工”，Planner 又会退化成 Executor 的隐式模式。

但现在几个 Skill 中有重复的身份、状态、Handoff 规则，后续应抽出一个共享参考文件，例如：

```text
.agents/skills/cw-task-loop/references/role-protocol.md
```

集中维护：

- 状态定义；
- Handoff schema；
- task_id 绑定；
- provenance；
- BLOCKED/REPLAN 行为；
- 用户决策和后台等待状态。

各角色 Skill 只保留自己的职责和操作规则，避免继续发生 AGENTS、Skill、模板各写一套、最后互相漂移。

### 三个 v3 模板都需要修订

而且当前的 `Executor _ Planner v3` 本身就不应该继续作为正式模板使用。

#### 1. Executor / Planner v3

必须拆成两个模板：

```text
Callwarden 无人值守循环启动模板：Planner v1.md
Callwarden 无人值守循环启动模板：Executor v4.md
```

现有文件的问题是：

- 标题写 Executor / Planner；
- 固定角色却只有 Executor；
- `planner` 只是 RuntimeRole；
- Executor 既负责规划又负责实现；
- 没有 `planning_pending`、`execution_ready`、`replanning_pending`；
- 没有 `executor_replan_requested`；
- 没有明确“任务过大时改代码前必须退回 Planner”。

它应保留为历史 v3，不覆盖；新版本通过 supersede 关系替代。

#### 2. Reviewer v3

需要升级为 Reviewer v4，增加：

- 先确认 Planner 计划、任务边界和 Contract；
- 不只检查本次 diff，还要检查调用链、集成行为、回归和受影响的既有代码；
- 非本次任务 bug 不能忽略，必须分类为：
  - 本次引入的阻塞问题；
  - 直接相关的整改问题；
  - 独立关联问题；
  - advisory 观察项；
- `BLOCKED` 必须包含根因、复现、影响、证据和最小修复条件；
- Reviewer 不再只输出聊天 Handoff，必须提交结构化 finding；
- daemon 应自动生成 `fix_defect` 或关联整改任务；
- 如果问题是任务边界或架构拆分错误，应路由回 Planner，而不是一律丢给 Executor；
- 复核对象必须明确显示 daemon 返回的精确 `task_id`。

#### 3. Adjudicator v3

需要升级为 Adjudicator v4，增加：

- 不能只验证 Reviewer 是否 PASS；
- 必须检查所有未解决 finding、回归、任务树、步骤、证据和部署状态；
- 发现实现问题时退回 Executor；
- 发现任务边界、复杂度或架构问题时退回 Planner；
- `adjudicator_returned` 必须携带完整根因和修复路径；
- 不能把 `ACCEPT` 当成完成；
- 仍然必须执行 `apply → close → COMPLETE`；
- 不能因为 Reviewer 没报告某个问题，就假设系统没有问题。

### 推荐的规则层次

最终应形成下面的优先级：

```text
daemon 状态机和 Contract
        ↓
AGENTS.md：项目级治理规则
        ↓
共享 role-protocol reference
        ↓
Planner / Executor / Reviewer / Adjudicator Skill
        ↓
四份角色启动模板
        ↓
CLI / MCP / Jira 类控制台渲染
```

模板不应该再各自定义状态机，也不应该把 Handoff 规则复制一遍。模板只负责告诉某个角色：

- 当前身份；
- 如何发现自己的任务；
- 允许做什么；
- 禁止做什么；
- 何时自动继续；
- 何时进入 Planner 或用户决策；
- 完成后如何交给下一角色。

所以结论是：

- `.agents/skills`：需要修订，但不整体合并；保留角色隔离，抽共享协议。
- `AGENTS.md`：需要继续同步，作为唯一项目级规则源。
- Executor/Planner v3：必须拆成 Planner 和 Executor 两个模板。
- Reviewer v3：需要升级。
- Adjudicator v3：需要升级。
- v3 历史文件不覆盖，新增 v4/Planner v1 并建立 supersede 关系。


对，但要区分“废弃”和“职责不同”。

| 资产 | 处理结论 |
|---|---|
| `cw-planner-architect` | 保留，Planner 专属 |
| `cw-executor-senior-engineer` | 保留，Executor 专属 |
| `cw-task-loop` | 保留，不合并；它是只读派工/角色卡入口 |
| `g0-experiment` | 保留，不合并；它是 G0 盲审专用流程 |

这四个没有需要整体废弃的。真正应该合并的是“共享协议内容”，不是 Skill 本身：

```text
.agents/skills/cw-task-loop/references/role-protocol.md
```

集中维护状态、Handoff、task_id、provenance、BLOCKED、REPLAN 等公共规则，然后四个 Skill 各自引用。这样可以避免同一规则在 `AGENTS.md`、Skill 和模板里逐份漂移。

另外，`cw-task-loop/references/user-guide.md` 也需要修订，增加 Planner、`READY/PLAN`、`execution_ready`、`replanning_pending` 等内容，不能归档。

### 模板处理

三个现有 v3 模板都应该升级，但不能覆盖原文件：

1. `Executor _ Planner v3` 拆成：

   - `Planner v1`
   - `Executor v4`

2. `Reviewer v3` 升级为 `Reviewer v4`；
3. `Adjudicator v3` 升级为 `Adjudicator v4`。

新模板必须补充：

- 精确 `task_id` 绑定；
- Planner → Executor；
- Executor 复杂度预检；
- `executor_replan_requested`；
- Reviewer/Adjudicator 的完整 finding；
- 相关但非本次任务缺陷的报告规则；
- 自动 remediation；
- Planner/Executor 的后台等待和重规划行为；
- 远端运行时不依赖聊天回复。

### 应归档的文件

新模板和引用更新完成后，以下旧模板可以移动到归档目录，不删除：

```text
archive/role-loop/templates/
├── Callwarden 无人值守循环启动模板：Executor _ Planner v1.md
├── Callwarden 无人值守循环启动模板：Executor _ Planner v2.md
├── Callwarden 无人值守循环启动模板：Executor _ Planner v3.md
├── Callwarden 无人值守循环启动模板：Reviewer v1.md
├── Callwarden 无人值守循环启动模板：Reviewer v2.md
├── Callwarden 无人值守循环启动模板：Reviewer v3.md
├── Callwarden 无人值守循环启动模板：Adjudicator v1.md
├── Callwarden 无人值守循环启动模板：Adjudicator v2.md
└── Callwarden 无人值守循环启动模板：Adjudicator v3.md
```

并新增一个归档索引，记录：

```text
Executor/Planner v3 → Planner v1 + Executor v4
Reviewer v3 → Reviewer v4
Adjudicator v3 → Adjudicator v4
```

归档前必须先完成三件事：

1. 新模板落地；
2. `AGENTS.md`、Skill、docs 和引用全部切换到新路径；
3. 用 `rg` 确认没有生产流程仍引用旧模板。

所以最终结论是：

- 四个 Skill 不整体废弃；
- 四个 Skill 不合并；
- 共享协议抽成 reference；
- 三个 v3 模板全部升级；
- Executor/Planner v3 必须拆分；
- v1、v2 和被替代的 v3 统一归档，保留历史追溯。