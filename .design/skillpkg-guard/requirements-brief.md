# requirements-brief · 技能包结构自检（skillpkg-guard）

> R0 产出。日期 2026-11-22。链路：requirements-cw R0 → R4 → R5 → code-review → test-cw。
> 本需求由本会话提出（技能层刚落地后的结构约束固化），用户以「按这个流程来」授权；如与预期不符，请在 R3 冻结前纠正。

## 1. 复述确认

> 我理解你要做的是：给 `skills/` 下的技能包加一个**只读结构自检器**，把"单一门禁源、引用的 reference 必须存在、冒烟集只能是真实路径、install 必须注入共享门禁"这几条现在靠人记的约定，变成一条能进 CI、能给非零退出码的命令；并顺带用一条真实需求把 R0→R5→审查→验收 整条链走通。

## 2. 背景与问题

`skills/` 现有三个包（`requirements-cw` / `code-review-cw` / `test-cw`）+ 一个共享源 `_shared/cw-gates.md`。四条约束目前**全靠人记**：

| 约束 | 现在怎么保证 | 失效后果 |
|---|---|---|
| 门禁单一源：包内不得出现 `references/cw-gates.md` | 靠提交纪律 | 漂移：两处各写一份，改一处另一处不跟（曾真实发生） |
| SKILL.md 引用的 `references/*` 必须存在 | 靠装完才发现 | 装到宿主后引用断链，技能跑到一半找不到规范 |
| `smoke.txt` 只能是真实存在的路径 | 靠手改时小心 | 打错一个字符 → 冒烟集静默少跑，仍显示"全绿" |
| `install.ps1` 必须从 `../_shared` 注入门禁 | 靠复制模板 | 新包忘注入 → 装出的包缺门禁，运行期才炸 |

这四条都属于"能在工具层验证的不靠 prompt 自觉"，与 `_shared` 的定位一致。

## 3. REQ-01（must）

**技能包结构自检**：新增 `skills/_shared/check_skill_packages.py`，扫描 `skills/*/`，输出违规清单，退出码约定 `0=全合规 / 2=发现违规 / 64=用法错误`。

- 检查项：frontmatter（`name` 与目录名一致、`description` 非空）；SKILL.md 中引用的 `references/<file>` 必须存在（**排除安装期注入文件 `cw-gates.md`**，见 §5.4）；包内不得有 `references/cw-gates.md`；`smoke.txt` 非注释行必须是真实路径（相对仓库根）；`install.ps1` 必须含 `_shared/cw-gates.md` 注入片段。
- 只读、无网络、无 daemon 依赖、幂等；单次 < 1s。
- NFR：不引入第三方依赖（不 import yaml）；Windows/Linux 路径均可用；文件读取容 BOM。

## 4. 范围

**In**：上述 5 类检查 + `--json` 可选输出（便于 CI 解析）。
**Out（非目标，不建 AC）**：校验 `DESIGN.md` 版本号与正文一致；校验 `install.ps1` 的 PowerShell 语法；校验 AGENTS.md 注入段唯一性；扫描 `docs/` 或 `.agents/skills/`；自动修复。

**停车场（parking-lot，本轮不做但记下）**：把 `check_skill_packages.py` 接进 `cicd/check_ci_gates.py` 的汇总；给 `_shared/` 加版本号并与包内副本比对。

## 5. 需求评审四件套

### 5.1 方案对比（≥2 候选）

| 方案 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A 独立脚本 + pytest 断言（推荐）** | `skills/_shared/check_skill_packages.py` 纯函数 + CLI；测试在 `tests/` | 职责单一；可被 CI 与本地各自调用；纯函数好测 | 多一个入口要维护 |
| B 并入 `cicd/check_ci_gates.py` | 作为其中一个 gate 函数 | 少一个文件 | 职责混淆：静态结构检查与测试结果门禁不同源；每次跑门禁都要扫盘；失败归因不清 |
| C pre-commit hook | 提交时拦 | 反馈最快 | hook 不被 CI 保证（`--no-verify` 可绕）；Windows 兼容差；与仓库"门禁在 CI/工具层"的既有做法不一致 |

**推荐 A**：它与 `cicd/check_*.py` 的既有风格一致（独立脚本、退出码三态），且纯函数可单测。

### 5.2 被否决项及理由

- **B 否决**：`check_ci_gates.py` 的输入是 junit + manifest，属"测试结果门禁"；技能包结构属"仓库结构门禁"。混在一起会让 `--junit` 必填校验与结构检查互相牵制，且失败信息无法区分归因。
- **C 否决**：本仓已有 `core.hooksPath` 未启用先例；用可绕过的机制守护"不可绕过"的约束，等于没守。

### 5.3 异常场景矩阵（8 类）

| 类 | 场景 | 期望行为 |
|---|---|---|
| 空值 | `skills/` 下没有任何包 / 传入不存在的目录 | **exit 2** + "no skill packages found"（防路径打错导致假绿） |
| 无权限 | 包目录不可读 | exit 2，指明路径与原因，不抛栈 |
| 重复 | 两个包 frontmatter 的 `name` 相同 | exit 2，列出冲突包 |
| 冲突 | 包内存在 `references/cw-gates.md` | exit 2（违反单一源） |
| 依赖失败 | 无第三方依赖，故无此风险；**不得 import yaml** | — |
| 重试 | 只读、幂等；重复执行结果一致 | 无需重试逻辑 |
| 取消恢复 | 无中间写入，无部分状态 | — |
| 数据边界 | BOM / CRLF / 空 SKILL.md / 超长行 | 用 `utf-8-sig` 读；空 frontmatter 视为违规并给出包名 |

### 5.4 未决项（三态分离）

| 状态 | 项 |
|---|---|
| confirmed | 退出码三态沿用仓库既有约定（`known_red.py` 0/2/64） |
| confirmed | 只读、无网络、不引第三方依赖 |
| assumption | 检查范围限于 `skills/*/`（不含 `_shared/` 自身、不含 `.agents/skills/`）——若后续要扫 `.agents/`，另开 REQ |
| assumption | `smoke.txt` 的路径相对**仓库根**解析（与 `pytest` 调用方式一致），非相对包目录 |
| assumption | **注入文件集是显式常量**（`INJECTED_AT_INSTALL = {"cw-gates.md"}`），不是通配、不是"references 下都豁免"。新增注入文件须同时改常量与测试（R2 F-A1） |
| open | 是否要在 CI 里强制运行：本轮不接 CI，先有命令与测试；接入方式（`check_ci_gates` 还是独立 job）留待下一批 |

## 6. 验收方式（R0 退出准则）

- 端到端可画：见 R1 `flowchart.md`（含空目录、缺引用、违反单一源、坏路径四条异常分支）
- 验收方式可执行：6 条 AC 全部可用 `pytest tests/test_skill_packages_guard.py` 断言，无需人工目检
- 预算：脚本 ≤200 行，测试 ≤150 行

### 6.1 基线记录（R3 冻结）

| 项 | 值 |
|---|---|
| 基线 commit | **`b6dfbc5`**（`docs(design): skillpkg-guard 基线冻结（R0-R3）`） |
| 基线内容 | `requirements-brief` / `ac-map` / `mindmap` / `flowchart` / `symbol-anchor` / `contracts` / `design-gate` 七件 |
| scope 快照 | AC-01…06（must）；AC-07（should）标 `deferred` 到下批 |
| R5 漂移检查参照 | HEAD vs `b6dfbc5`，偏差需变更单（见 `change-log.md`） |

> 收口时 R5 实测：预算口径产生偏差，已按 §三 变更单程序登记（见 `change-log.md` CL-01），**不静默放宽**。

## 7. 追溯

`REQ-01` → `AC-01…06`（见 `ac-map.md`）→ 任务单 → 证据。整改单同样首行带 `REQ-01`。
