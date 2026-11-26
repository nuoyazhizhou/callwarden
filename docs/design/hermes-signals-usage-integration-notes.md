# hermes-agent 情报：suggestions 的 usage / integration 信号源生产端定位

- 任务：T-1795492760395-142b21e0（hermes-agent 深读补充）
- 日期：2026-11-26
- 参考仓：`testcode/hermes-agent`（只读，本笔记不修改参考仓任何文件）
- 前置：hermes-agent 深读（T-1795466681089-0462193c）已核实 catalog / blueprint 两个信号源的生产端；本笔记补齐 usage / integration 两个源的定位或证伪。

## 1. 体系总览：suggestions 四源

suggestions（Suggested Cron Jobs）是 hermes-agent 的"建议自动化"面：四类来源统一进入一个 store，用户通过 `/suggestions` 列表 accept/dismiss，accept 后变成 cron job。

| 信号源 | 声明出处（文档） | 声明的触发条件 | 生产端核实结论 |
|--------|------------------|----------------|----------------|
| `catalog` | `website/docs/developer-guide/creating-skills.md:380` | `/suggestions catalog` 精选起始自动化 | **存在**，已核实（前置任务） |
| `blueprint` | `website/docs/developer-guide/creating-skills.md:381` | 安装携带 `blueprint:` 块的技能 | **存在**，已核实（前置任务） |
| `usage` | `website/docs/developer-guide/creating-skills.md:382` | 后台 review 发现重复出现的请求 | **仓内不存在生产端**（本笔记证伪） |
| `integration` | `website/docs/developer-guide/creating-skills.md:383` | 连接账户（Gmail、GitHub 等）后推荐自动化 | **仓内不存在生产端**（本笔记证伪） |

权威定义：

- `cron/suggestions.py:39` — `VALID_SOURCES = frozenset({"catalog", "blueprint", "usage", "integration"})`，store 层白名单，四源之一即可通过校验。
- `cron/suggestions.py:5-6` — 模块 docstring 声明各源含义：`usage`（self-improvement review）、`integration`（connected account）。
- `website/docs/developer-guide/creating-skills.md:374` — 明确写着 "(later) usage-pattern and integration suggestions appear"，即文档自身已声明这两源是**后续计划**，非现状。

## 2. usage 信号源：生产端不存在（证伪）

### 2.1 结论

全仓（排除 `website/` 文档与 `tests/`）**不存在任何以 `source="usage"` 写入 suggestion 的代码路径**。`usage` 仅存在于：

1. store 层白名单 `cron/suggestions.py:39`（合法值定义，非生产）；
2. 测试直写 `tests/cron/test_suggestions.py:104-110`（`test_usage_source_is_consent_first_self_improvement`，直接调 `store.add_suggestion(source="usage")`，只验证 store 接受该值——这是**测试消费端**，不是生产端）。

### 2.2 声明中的生产端为何不成立

文档声明的 usage 生产端是 "self-improvement review"。仓内对应实现为 `agent/background_review.py`（每次对话轮后 fork agent 评估"是否保存/更新 skill 或 memory"）。核实结果：

- `agent/background_review.py` 全文件对 suggestion 的引用仅 1 处且无关（第 560 行，"put the suggested ..." 上下文，是拒绝重名任务时的提示语）；
- 该模块的写入目标只有 memory store + skill store（docstring 第 2-4 行明确），从不调用 `cron.suggestions` 的任何 API；
- `cron/` 目录内所有 `usage` 字样均与 suggestion 无关：`cron/scheduler.py:1247-1268`（`usage_audit.jsonl` 触发审计日志）、`cron/quota_hold.py`（provider 用量窗口）、`cron/scheduler_prompt.py:206`（skill 使用计数 `tools/skill_usage.py::bump_use`）。

### 2.3 搜索范围与方法

- 范围：`testcode/hermes-agent` 全仓；代码定位时排除 `website/`（文档仅作声明核对）与 `tests/`（测试直写非生产）。
- 方法与命中：
  - 全仓 `add_suggestion` 调用方枚举（见 §4）——只有 catalog / blueprint 两处生产调用；
  - 全仓 `source="usage"|source='usage'`（排除 website）——仅 `tests/cron/test_suggestions.py:110`；
  - 全仓 `self.improvement|self_improvement` —— 无 suggestions 相关命中；
  - `agent/background_review.py` 内 `suggest` 扫描——无 suggestions API 调用；
  - `cron/` 目录内 `usage` 扫描——全部为用量审计/配额/技能计数，无 suggestion 生产。

## 3. integration 信号源：生产端不存在（证伪）

### 3.1 结论

全仓（排除 `website/`）**不存在任何以 `source="integration"` 写入 suggestion 的代码路径**，也没有任何"连接账户后推荐自动化"的钩子。

### 3.2 声明中的生产端为何不成立

文档声明的 integration 生产端是 "connected account"（连接 Gmail/GitHub 等账户后自动推荐）。仓内连接账户的实现为 `tools/connectors/`，核实结果：

- connectors 的 "suggested" 是**另一个语义**：建议用户连接哪些 connector 的展示字段（`tools/connectors/display.py:23,27,46`、`tools/connectors/managed.py:389`、`tools/connectors/targets.py:109`、`tools/connectors/tool.py:62`、`tools/connectors/gateway/wire.py:126`），与 cron suggestions 无任何调用关系；
- connectors 目录全量 `suggest` 扫描无一处触及 `cron.suggestions`；
- `agent/onboarding.py:183-194` 提到 connected accounts，但只涉及 profile/隐私提示文案，不产生 suggestion。

### 3.3 搜索范围与方法

- 方法与命中：
  - 全仓 `source="integration"|source='integration'`（排除 website）——零命中；
  - 全仓 `connected account|account connected|integration suggestion` —— 全部为 connectors 连接态/UI 文案/测试，无 suggestion 生产；
  - `tools/connectors/` 目录 `suggest` 扫描——仅"建议连接哪些 connector"语义（上述文件行号）。

## 4. 已核实的生产端（对照，前置任务结论的代码坐实）

全仓 `add_suggestion` 生产调用方枚举（`cron/suggestions.py` 定义 + 两处生产 + 测试注入点）：

| 信号源 | 生产函数（file:line） | 触发点（file:line） | 说明 |
|--------|------------------------|---------------------|------|
| `catalog` | `cron/suggestion_catalog.py:118` `seed_catalog_suggestions`（`add_suggestion` 于 137-139 行，`add_fn` 可注入） | `hermes_cli/suggestions_cmd.py:69-70`（`/suggestions catalog` 子命令） | 精选起始自动化，用户显式拉取 |
| `blueprint` | `cron/skill_blueprints.py:133` `register_blueprint_suggestion`（`add_suggestion` 于 144-149 行） | `hermes_cli/skills_hub.py:600` `_announce_blueprint`（技能安装完成时，614 行调用） | 技能 `metadata.hermes.blueprint` 块，安装即推荐、绝不自动建 job |

除这两处外，全仓无第三个 `add_suggestion` 生产调用方（`tests/cron/test_suggestions.py:186,205` 为测试注入/直写）。

## 5. 消费端（两源共用，与 catalog/blueprint 相同）

- 存储：`cron/suggestions.py` — JSON 文件 `~/.hermes/cron/suggestions.json`（`_current_suggestions_file` 45-46 行；原子写 73-76 行，0o600）；`MAX_PENDING = 5`（37 行）；状态机 pending/accepted/dismissed（40-42 行）。
- 统一消费面：`hermes_cli/suggestions_cmd.py::handle_suggestions_command`（100 行起，CLI 与 gateway 共用同一实现防止漂移）；`_fmt_pending` 第 23 行向用户展示每条 suggestion 的 `source` 字段；accept（42-56 行）→ `store.accept_suggestion` → 注册 cron job；dismiss（59-64 行）。
- gateway 读参数白名单：`gateway/session_commands.py:89-96`（`/suggestions` 仅无参列表算读操作，`accept|catalog` 属写操作）。
- **消费端不区分来源**：accept 后均走 `cron/scheduler.py` 注册定时任务，source 仅作展示标签。

## 6. 对 borrow（借鉴）的含义

1. **借的是"统一收口"模式**：四源共 store、共消费面、共 accept/dismiss 生命周期（`VALID_SOURCES` 白名单 + 单一 JSON 存储 + 单一命令实现），这个骨架与源的数量无关，可直接借鉴。
2. **usage / integration 在 hermes-agent 中是"文档先行、实现缺位"的占位源**：声明了语义（self-improvement review / connected account）但全仓无生产端，仅有 store 层合法值与测试直写。borrow 时不能把这两个源当作 hermes-agent 的已有能力来对标，只能作为"未来扩展方向"的参照。
3. **可借鉴的护栏细节**：catalog 用户显式拉取（`/suggestions catalog`）而非启动即注入；blueprint 安装即推荐但绝不自动建 job（`hermes_cli/skills_hub.py:601-602` docstring 明确）；`MAX_PENDING = 5` 封顶；`already offered/dismissed` 闩锁防重复打扰。
4. **证伪记录的价值**：若 callwarden 需要同类"建议自动化"面，usage/integration 的生产端设计需要自行实现，hermes-agent 无现成代码可抄；唯一可参考的是其声明的触发语义（重复请求模式 / 新连接账户）。

## 7. 复核指引

- 白名单与含义：`cron/suggestions.py:5-6,39`
- catalog 生产：`cron/suggestion_catalog.py:118` → `hermes_cli/suggestions_cmd.py:69`
- blueprint 生产：`cron/skill_blueprints.py:133` → `hermes_cli/skills_hub.py:600`
- 消费面：`hermes_cli/suggestions_cmd.py:100`（CLI+gateway 共用）
- usage 测试直写（非生产）：`tests/cron/test_suggestions.py:104`
- integration 语义占位声明：`website/docs/developer-guide/creating-skills.md:374,383`
