# skills/ —— callwarden ALM 技能地图

> 本文件回答三个问题：**一个需求从进来到关闭经过哪些阶段、每段归谁、哪几段还没归位**。
> 流程细节不在本文件（在各技能 `SKILL.md`）；门禁与建单契约在 `_shared/cw-gates.md`。
> 底稿：2026-11-22 对 `skills/` 的 ALM 覆盖度审计。

## 1. 覆盖矩阵

| # | ALM 阶段 | 归属 | 关键产物 | 门禁 / 判据 | 状态 |
|---|---|---|---|---|---|
| 1 | 需求澄清（R0） | `requirements-cw` | `requirements-brief.md`、`ac-map.md` 初版 | 评审四件套 + 复述确认 + ≤3 轮封顶 | ✅ |
| 2 | 技术选型（G-Tech，仅新项目） | `requirements-cw` | `tech-choice.md` | 四件套（候选 ≥2 + 否决理由） | ✅ |
| 3 | 可视化（R1） | `requirements-cw` | `mindmap.md`、`flowchart.md` | 用户一次确认图与理解一致 | ✅ |
| 4 | 符号锚定（R2） | `requirements-cw` | `symbol-anchor.md` | 每个设计元素有 reuse/extend/new 判定 + cw 证据 | ✅ |
| 5 | contract 卡片 + 设计门禁（R3） | `requirements-cw` | `contracts.md`、`ac-map.md` 定稿 | 设计评审四件套 + 基线提交（HEAD 记录） | ✅ |
| 6 | 任务注入（R4） | `requirements-cw` | `task-map.md`（REQ ↔ 卡片 ↔ task_id） | G1-G4 + G6 建单契约 + 批次放行判据 | ✅ |
| 7 | 开发（R5） | `requirements-cw` / `.agents/skills/cw-executor-senior-engineer` | 源码 + `task_report_step` | P1-P4（G2-G5） | ⚠️ 主执行者在 `.agents/`，不在本目录 |
| 8 | CodeReview | `code-review-cw` | 审查报告 + 整改任务单 | 阶段零门禁 + 图谱选靶 Top3 证据表 | ✅ |
| 9 | **测试执行 / 验收** | `test-cw` | `evidence/<AC-id>.md` + `test-plan.md` + junit | 六条件判据（双 revision / required 全 passed / required skip·xfail=0 / 无 deselect / collected 校验 / rc=0） | ✅ |
| 10 | 需求关闭判定 | `requirements-cw`（读证据）+ `test-cw`（产证据）+ `.agents/skills/cw-task-loop`（apply/close） | `ac-map.md` 证据位置 + `evidence/` | 快照内 AC 全 PASS（**N/A 不算绿**）且证据已提交 | ⚠️ 跨目录 |
| 11 | 审核裁决（PASS/BLOCKED） | `docs/role-loop-templates/` v4 + `cw-task-loop` | verdict / lease | reviewer lease 绑定 | ⚠️ 不在本目录 |

**结论口径**：`skills/` 是**客户端技能层**（需求端 + 审查端 + 验收端），不是全量 ALM。第 7 段的执行者、第 10 段的 apply/close、第 11 段的审核裁决在 `.agents/skills/` 与 `docs/role-loop-templates/`；把三处加起来才是完整 ALM。

## 2. 两条链

```
完整链（新项目）：R0 → G-Tech → R1 → R2 → R3(+基线) → R4 → R5 → code-review-cw → test-cw 执行 test-plan（G-Test 清单）→ feature 验收 → 关闭
精简链（存量仓）：R0 → R1 → R2 → R3(+基线) → R4 → R5 → code-review-cw → test-cw 按 ac-map 轻量验收（FA）→ 关闭
```

分叉判据写在 `requirements-cw/SKILL.md`「开工判定」：仓库是否存在 + 技术栈是否已定，冲突时问用户并记 `confirmed`。

## 3. 交接契约

技能之间只传**证据**，不传身份：

| 交接 | 内容 | 落点 |
|---|---|---|
| `requirements-cw` R4 → executor | `task-map.md` + `symbol-anchor.md` + `contracts.md` + `requirements-brief.md` + `ac-map.md` + `snapshot_id` | 全在 `.design/<feature>/`，随基线提交 |
| executor 领取 | **自身注册身份**（`agent.register` 五字段 + 自己的 session），身份不跨会话借用 | `_shared/cw-gates.md` §G2 |
| 建单形状 | title 用符号 / description 首行 `REQ-xx` / `check_items` 字符串 / `action` 取值 | `_shared/cw-gates.md` §G6，**两端共用** |
| 审查 → 验收 | 整改单追加进 `task-map.md` 三列（含 `REQ-GAP`），不另建映射表 | `code-review-cw` 阶段二 |
| 需求 → 验收 | `ac-map.md` + `test-plan.md` + `symbol-anchor.md`（test-cw 只读这三份，不改设计） | `test-cw` §开工输入 |
| 验收 → 需求关闭 | `evidence/<AC-id>.md` 八段证据 + junit 原件；`ac-map` 的「证据位置」由 test-cw 填 | `test-cw` §5 |

## 4. 门禁单一事实源

`_shared/cw-gates.md` 是**唯一**门禁与建单契约来源：

- 仓库内只有这一份；`requirements-cw/`、`code-review-cw/`、`test-cw/` 的 `references/` **均不放**同名文件。
- 三个 `install.ps1` 各自把它注入为安装包的 `references/cw-gates.md`（源码单一、分发独立，装到宿主的包自包含）。
- 改这个文件 = 改三个技能的门禁，须同步验证三包再提交。

### 4.1 宿主矩阵（四个宿主，两种安装器）

| 宿主 | 安装器 | 目标目录 |
|---|---|---|
| OpenAI Codex（桌面 / CLI） | 各包 `install.ps1` | `~/.codex/skills/<pkg>/`，并注入 `~/.codex/AGENTS.md` 常驻段 |
| Claude Code | 各包 `install.ps1` | `~/.claude/skills/<pkg>/` |
| Trae CN | 各包 `install.ps1`（目录不存在则跳过） | `~/.trae-cn/skills/<pkg>/` |
| **WorkBuddy（用户级技能）** | `python skills/_shared/install_skill_to_workbuddy.py` | `~/.workbuddy/skills/<pkg>/` |

`install.ps1` **不覆盖** WorkBuddy 自己的用户级技能目录，所以最后一行单列脚本；两者安装语义一致——同样注入 `references/cw-gates.md`、同样不接受包内副本、同样跳过 `__pycache__`/`.pyc`。脚本退出码沿用仓内三态（0=OK / 1=违规 / 64=用法错），支持 `--dry-run` 与 `--dest`。

WorkBuddy 侧技能列表按**目录签名**缓存（`~/.workbuddy/.skill-list-cache.json` 的 `signature` + `watch.dirs`），新增技能目录会让签名不匹配并自动重建——**不要手工改这个缓存文件**，但需重启会话才可见。

## 5. 工件与提交节奏

运行期工件落在**被开发仓库**的 `.design/<feature>/`（不是本仓）：

```
.design/<feature>/
├── requirements-brief.md   tech-choice.md   mindmap.md   flowchart.md
├── symbol-anchor.md        contracts.md     ac-map.md    task-map.md
├── change-log.md           （只追加）
└── evidence/<AC-id>.md      evidence/scan-<stage>.md
.design/parking-lot.md       （跨 feature 只追加，R0 开工回看）
```

**三个提交时点**：R3 基线冻结 / 每次变更单 / R5 收口——均为**独立提交**，不混进 hunk 级源码提交。`.design/` 保留 git 跟踪，不写 `.gitignore`（基线必须可追溯）。

## 6. 已知缺口（不要假装已覆盖）

| 缺口 | 现象 | 建议归属 |
|---|---|---|
| ~~测试执行者缺位~~ | **已实现（2026-11-22）**：`test-cw` 五步流程 + 六条件判据 + 证据八段已落包，并在 backlog-46 上试跑（149 例全绿 / 6 AC PASS / 1 条 manual N/A → 需求未关闭）。Rust 侧 nodeid 级归属已补齐（`scripts/cargo_test_evidence.py`：`<target>#<path>` 形态，`--list` 采集 + `--exact` 执行，真机 2066 例验证，DESIGN §15）；冒烟集已于 2026-11-22 签字冻结 | 已关闭 |
| ~~`g0-experiment` 双份~~ | **已去重（2026-11-22）**：删除 `skills/g0-experiment`，仅保留 `.agents/skills/g0-experiment`（项目级 skill 加载目录）；`.kiro/steering/g0-experiment.md` 的 `#[[file:...]]` 指针已同步到新路径 | 已关闭 |
| 开发/审核不在本目录 | executor、task-loop、planner、reviewer 模板都在 `.agents/skills/` 与 `docs/role-loop-templates/` | 保持分层：本目录只放"给宿主客户端用"的技能 |
| 触发词边界 | `requirements-cw` 管需求/设计/拆解，`code-review-cw` 管已有代码审查 | 两侧 description 已互相写负向边界（"不触发：……"），改动时不要删 |

## 7. 新增技能的准入

1. 门禁与建单格式**引用** `_shared/cw-gates.md`，不复制全文；新增共用约定加在该文件里一个 `G*` 小节。
2. `install.ps1` 必须注入 `_shared/cw-gates.md`（照抄现有两个包的注入段）。
3. 在本文件 §1 矩阵里登记归属，并更新 §6 缺口表。
4. 若与其他技能共享 `references/` 资产，同样放 `_shared/`，同样由 `install.ps1` 注入。
