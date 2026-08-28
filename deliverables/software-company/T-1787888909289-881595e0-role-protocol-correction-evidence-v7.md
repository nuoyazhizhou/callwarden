# T-1787888909289-881595e0 角色治理修订 v2 — Evidence v7

第四轮复审整改（F-6、F-7）+ 用户授权的 scope 追加（AGENTS.md 规则 47 行数门禁）。

- 任务：`T-1787888909289-881595e0`
- 角色：executor
- 基线 HEAD（本轮开工前）：`1aeb1d0c348127937e08654c4bd27342e6de5d4f`
- 前序证据：v6（`104E25AAAFF69A758888FA100F064BEB228646415FBDAF5107986995175A8051`）
- 本文件为**追加**证据；v2~v6 保持原样未改写

## 1. 本轮范围（两类，已在 commit message 中分别标注）

| 类别 | 项 | 依据 |
|---|---|---|
| 复审整改（本任务 scope 内） | F-6 `owner_route` 必填 | 第四轮 review finding |
| 复审整改（本任务 scope 内） | F-7 `.agents/` gitignored 陷阱纪律 | 第四轮 review finding |
| **用户授权 scope 追加** | AGENTS.md 规则 47 `.rs`/`.py` 行数门禁 | 用户 2026-08-28 明确要求，并授权与整改一次性提交 |

规则 47 与"角色治理修订 v2"不属同一交付线，属 scope 追加。已向用户说明拆分选项，用户明确选择
"一次性提交，注释清楚"。据此并入本 commit，并在 commit message 与本证据中分别标注类别，
不混淆为复审整改项。

## 2. F-6：`owner_route` 必填（block 级 finding 的补完）

**上游 finding 根因**：第三轮 F-1 修复只规定"`owner_route` 缺失时 Executor 升级用户"，未把该字段
标为必填。由于 daemon 不校验 findings 字段（3 字段不透明契约），**省略是默认状态**，导致新增的
fail-closed 分支会成为主路径，把绝大多数 `reviewer_blocked` 推给用户，与"Executor 自动消费整改"
的设计目标相反。

**修复**（`.agents/skills/cw-task-loop/references/role-protocol.md` §4）：

1. 字段表 `owner_route` 行加 **（所有 finding 必填）** 标记；
2. 新增「`owner_route` 必填（Reviewer/Adjudicator 侧义务）」段：明确 daemon 不校验 ⇒ 省略在技术上
   不会被拒绝 ⇒ **提交不含可解析 `owner_route` 的 finding 本身即上游交付缺陷**，不是可接受默认态；
   每条 finding 必须在 `subject` 中携带显式归属标记；
3. fail-closed 规则重写为**例外分支**：Executor 遇缺失时仍按计划缺陷升级，但必须在 `reason` 中
   同时记录"finding 缺失归属字段"这一上游缺陷；并声明该分支应罕见，频繁触发说明上游未遵守必填
   义务，属需单独整改的流程缺陷，而非 Executor 的常规升级理由。

**净效果**：fail-closed 语义不变（仍不允许默认硬修、不允许自行推断归属），但把"缺失"从默认态
降级为可归因的上游缺陷，使自动整改重新成为常态路径。

## 3. F-7：`.agents/` 是 gitignored 目录（warn 级 finding）

**精确机制**（本轮独立核验，非采信上游描述）：

```
.gitignore:201 → .agents/

git check-ignore -v .agents/skills/cw-task-loop/references/role-protocol.md
  → NOT ignored                          # tracked 文件不算 ignored，看不到规则（误导源）
git check-ignore -v --no-index .agents
  → .gitignore:201:.agents/    .agents   # 加 --no-index 才现形
git add --dry-run .agents/skills/cw-task-loop/references/role-protocol.md
  → The following paths are ignored...  exit=1   # 精确 tracked 文件路径同样 exit 1
git ls-files --others --ignored --exclude-standard --directory .agents
  → .agents/skills/g0-experiment/        # 目录级 -f 会吸入这批忽略资产
```

**修复**（同文件 §7「Executor 交付与 VCS provenance」）：新增三条纪律——

1. 诊断必须用 `git check-ignore -v --no-index .agents`，不得据不带 `--no-index` 的"not ignored"
   判断未被忽略；
2. 禁止 `git add -f .agents` 或任何目录级 `-f`（会把 `.agents/skills/g0-experiment/` 一并暂存，
   污染任务白名单），只允许 `git add -f <精确文件路径>`；
3. 在 `.agents/` 下新建文件时 `git add` 会静默跳过、commit 仍"成功"，必须用 `git ls-files .agents`
   或 `git show --stat <commit>` 确认文件真的入库。

## 4. scope 追加：AGENTS.md 规则 47（`.rs`/`.py` 行数门禁）

**阈值依据**（2026-08-28 全量实测，`git ls-files "*.rs" "*.py"`，排除 `archive/`、
`recovered-scratch/`，共 1016 文件）：

| 分位 | `.rs`（n=199） | `.py`（n=817） |
|---|---|---|
| median | 523 | 261 |
| p75 | 902 | 486 |
| p90 | 1788 | 762 |
| p95 | 3126 | 1080 |
| p99 | 6976 | 2841 |
| max | 7623（`snapshot_state.rs`） | 17101（`cli/main.py`） |

越线文件数：>800 → 131（12.9%）；>1500 → 45（4.4%）；>3000 → 17（1.7%）。

**三级门禁**：800 软阈值（warn，Planner 规划须说明是否顺带拆分；Executor 净增 >200 行须提拆分建议）；
1500 硬阈值（禁止新建超线文件、禁止让既有文件跨线；必须改动已超线文件时 report 记录当前行数并提
可执行拆分方案，由 Planner 决定本任务拆或立独立技术债任务）；3000 灾难线（必须立独立技术债任务）。

**配套约束**：拆分必须按职责边界（参照既有 `task_collab*.rs` 21 文件 / `task_loop/` 29 文件的拆法），
禁止机械切成 `_part1`/`_part2`；测试文件同标准；豁免（生成文件、纯数据表、单一分派表）必须显式
记录理由；附提交前自检 PowerShell 命令。

**与既有规则的衔接**：规则 47 显式关联规则 40（并行判据是所有权文件不相交）——巨型文件是 ownership
冲突源，两个任务都要改同一超线文件时不得并行。

**事实更正**：用户提到的"`task_loop.rs` 超过 19000 行"在 git 历史中不存在——
`git log --all --diff-filter=D -- rust_ext/src/daemon/task_loop.rs` 无结果，`task_loop/` 一直是目录，
最大成员 `next_action.rs` 1477 行。现存对应量级的技术债是 `cli/main.py`（17101 行，仍在线）。
规则 47 的灾难线条款已点名该文件。

**角色 Skill 指针（不复制阈值，遵守单源纪律）**：

- `cw-executor-senior-engineer/SKILL.md`「实施原则」：指向规则 47，强调触碰超线文件须 report 记录
  行数与拆分方案、禁止新建超线文件、禁止机械按行切；
- `cw-planner-architect/SKILL.md`「复杂度预检」：指向规则 47，明确超硬阈值文件由 Planner 决定
  本任务拆或独立立项并写入计划，且超线文件构成规则 40 的并行禁止条件。

## 5. 验收

| 项 | 结果 |
|---|---|
| `scripts/validate_template_compliance.py` 主检查 | 通过（exit 0） |
| 同上 `--self-test` | **18/18 通过, 0 失败** |
| §4 finding schema 表格改动未破坏校验 | 已确认（`REQUIRED_FINDING_FIELDS`、`dup_finding_schema` 用例仍 PASS） |
| `git diff --check` | 干净 |
| 提交白名单 | 5 文件，未混入工作区 P0-L 的 `rust_ext/`、`server/`、`tests/` 并行改动 |
| v2~v6 证据 append-only | 未改写 |

**改动文件白名单**：

```
.agents/skills/cw-task-loop/references/role-protocol.md
.agents/skills/cw-executor-senior-engineer/SKILL.md
.agents/skills/cw-planner-architect/SKILL.md
AGENTS.md
deliverables/software-company/T-1787888909289-881595e0-role-protocol-correction-evidence-v7.md
```

`.agents/` 三个文件按本轮新增的 §7 纪律以 `git add -f <精确文件路径>` 暂存（非目录级 `-f`），
并用 `git show --stat` 核对实际入库。

## 6. 未实施项（如实声明）

- **F-5（advisory，未实施）**：`decision_request` 在 pre-cutover 缺少结构化承载——Handoff 信封只有
  扁平 `reason`，选择卡只能写成散文。建议独立立任务，定义 pre-cutover 的决策卡子块格式或
  evidence artifact 承载方式。
- **F-8（info / adjacent_defect，未实施）**：`g0-experiment` skill 的 5 个 `.md` 全部未纳入版本控制
  （`git ls-files .agents` 只有 4 角色协议的 5 个文件），但 AGENTS.md Skill 选择规则强制 G0 工作使用它。
  一个被强制要求的 skill 无版本控制、无法审查、无 provenance。非本任务 scope，建议独立立项。
- **规则 47 无自动化门禁**：当前仅为文档规则 + 手工自检命令，未接入 pre-commit hook 或 CI。
  若需硬门禁需独立实现任务。
- **既有 45 个超线文件未整改**：规则 47 只约束新增与跨线，不追溯存量。`cli/main.py` 等存量技术债
  需独立立项。

## 7. 治理状态

本轮为 Executor 整改交付，完成后任务仍应交独立 Reviewer 会话（不同 instance/session）复核。
本会话不签发 verdict、不执行 apply/close。
