# design-gate · skillpkg-guard

> R3 设计门禁四件套 + 两轮优缺点对抗。日期 2026-11-22。

## 第 1 轮 · 结构方案对比（≥2 候选）

| 候选 | 做法 | 优点 | 缺点 |
|---|---|---|---|
| **A 单文件 6 函数（选中）** | `check_package` 内联 5 类检查，`Package` dataclass 承载已解析结果 | 6 个符号、单文件、无跨模块耦合；`Package` 让 SKILL.md **只读一次**；每函数 <30 行 | `check_package` 聚合 5 条判据，单函数 55 行，属"偏胖" |
| B 拆 5 个 `_check_*` 子函数 | 每类检查独立函数，`check_package` 只做编排 | 单函数更短；违规码与函数 1:1 | 符号从 6 → 11；5 个子函数都是一次性调用（无第二调用方）→ 触发"无死函数/禁过度设计"红线；测试须穿透 `_check_*` 私有面 |
| C 插件式注册表（`CHECKS = [(code, fn), ...]`） | 检查项可增删，未来加检查不改 `check_package` | 扩展性最好 | **YAGNI**：当前只有 5 条且全部 must；注册表本身无测试价值；`code` 与函数对应关系变成运行期才可知，静态审查变弱 |

**选 A。** 关键理由：B 的"更短函数"收益是审美，代价是 5 个仅一次调用的符号（违反本仓"避免死函数"的既有约束）；C 为一个不存在的需求付扩展成本。A 的"偏胖 `check_package`"是唯一的真实缺点，用**固定检查顺序 + 不短路**换来了"一次跑暴露全部问题"的可用性，值得。

### 被否决项理由（写死，不留"以后再说"）

- **B 否决**：`_check_frontmatter` / `_check_references` / `_check_duplicate_gates` / `_check_smoke` / `_check_install` 全部只被 `check_package` 调一次。本仓对 orphan 符号有扫描（`get_orphan_symbols`），虽本例不算 orphan（有调用），但"为拆而拆"会让 6 个可读符号变成 11 个，且私有面测试被迫 `from module import _check_x`——测试耦合实现细节。**否决强度：硬。**
- **C 否决**：注册表把"有哪些检查"从**源码可读**变成**运行期构造**。code-review 阶段读不到检查清单，等于给未来留了"加了检查没人知道"的口子。**否决强度：硬（YAGNI）。**

## 第 2 轮 · 对选中方案做缺点对抗（自审）

逐条找 A 的真实缺点，并给出处置——**不修的要写清为什么不修**：

| # | 缺点（真实存在） | 严重度 | 处置 |
|---|---|---|---|
| 1 | `check_package` 55 行、5 条判据耦合，单条判据的单元测试须构造整个包目录 | 🟡 | **不修**。测试用 `tmp_path` 构造最小包（3 行写 `SKILL.md` + 1 个空 `install.ps1`），成本可接受；拆函数的代价（见上）更大 |
| 2 | `INJECTED_AT_INSTALL = {"cw-gates.md"}` 是一处**硬编码豁免**，等于检查器里留了后门 | 🔴 | **必须用反向测试锁死**：`::test_injected_reference_exempt` 断言"豁免只对 `cw-gates.md` 生效"，另一个 `::test_other_missing_reference_not_exempt` 断言"把 `cw-gates.md` 换成 `other.md` 立刻红"。**豁免可枚举、有反向断言**，符合容差三必要（显式/独立/单向）中的显式与独立 |
| 3 | `referenced_reference_files` 不感知代码块，示例里的 `references/x.md` 会误报 | 🟡 | **不修，按"宁可吵不可漏"定**。当前三包 SKILL.md 无代码块内的假 `references/` 路径（实测 AC-01 会证明）；未来若误报，人改文档即可，比"漏报导致装了断链"便宜 |
| 4 | `load_packages` 遇一个不可读文件就整体 abort，不能"跳过坏包继续检" | 🟡 | **不修**。结构检查的价值在于"一个坏了必须让人知道"；部分成功会诱导"看剩下的绿了就合并" |
| 5 | 只查 `install.ps1` 里有没有 `_shared` 注入**字符串**，不查语义正确性（比如注入到了错目录） | 🟡 | **不修 + 记停车场**。语义校验要跑 PowerShell 或解析 AST，成本远超收益；本检查定位是"防忘记"，不是"防写错" |
| 6 | 不查 `DESIGN.md` 版本号与正文一致（brief §4 明确 Out） | 💭 | **Out 已声明**。属文档一致性，非结构约束；且 DESIGN 版本号格式三包不一，先统一格式才有得查 |
| 7 | 硬编码 `parents[2]` 推仓库根，`skills/` 换位置就失效 | 💡 | **不修**。`skills/_shared/x.py` 的层级本身就是契约（`install.ps1` 也用 `Split-Path $PSScriptRoot -Parent` 同样假设）；换位置时两处同改，漏改会被 AC-01 立刻抓到 |

**第 2 轮结论**：7 条缺点中 🔴 1 条（#2 豁免后门）已用反向测试锁死；🟡 4 条为**有意识的取舍**并写明理由；💭/💡 2 条为已声明 Out 或结构性假设。**无阻塞项。**

## 异常场景矩阵 · 设计级（8 类逐项落位）

| 类 | 设计期结论 | 落点 |
|---|---|---|
| 空值 | `skills/` 空 / `--root` 不存在 / 0 个包 → **exit 2**（不是 0） | C-02 返回 `[]`；C-06 判 0 → exit 2 |
| 无权限 | 目录或文件不可读 → exit 2 + 路径与原因，**不打栈** | C-02 上抛 `OSError`；C-06 捕获 |
| 重复 | 跨包 `name` 相同 → `DUPLICATE_PACKAGE_NAME` | C-06 |
| 冲突 | 包内 `references/cw-gates.md` → `DUPLICATE_GATES` | C-04 |
| 依赖失败 | 零第三方依赖（禁 yaml）；仅 stdlib 6 个模块 | C-06；brief NFR |
| 重试 | 幂等：无写入、无缓存、无时间依赖 | 全局 |
| 取消恢复 | 无中间状态可留（只读） | 全局 |
| 数据边界 | BOM/CRLF/空 `SKILL.md`/超长行/折行 frontmatter/`smoke.txt` 空行与注释行 | C-01 / C-02 / C-04 |

## 三态未决（R3 冻结前必须无 `open` 阻塞项）

| 状态 | 项 |
|---|---|
| **confirmed** | 退出码 `0/2/64` 沿用 `cicd/known_red.py:37-39` |
| **confirmed** | 零第三方依赖、零网络、零 daemon、只读、幂等、<1s |
| **confirmed** | 注入豁免集为显式常量 `{"cw-gates.md"}`，配正反双向断言 |
| **confirmed** | `smoke.txt` 路径相对**仓库根**（与 pytest 调用一致） |
| **assumption** | 扫描范围 = `skills/*/`（含 `SKILL.md` 的直接子目录），不含 `_shared/`、`.agents/skills/`、`docs/` |
| **assumption** | frontmatter 只解析顶层键（三包现状均无嵌套） |
| **assumption** | `install.ps1` 缺失算违规（brief 原只写"缺注入片段"，本卡扩一项并单列 code） |
| ~~open~~ → **deferred** | 是否接进 CI：本轮不接，parking-lot。**不是 open 阻塞项**——本 REQ 的 In 范围就是"有命令 + 有测试" |

## 设计门禁结论

- 四件套齐备（方案对比 3 候选 / 否决理由 2 条硬否决 / 8 类矩阵全落位 / 三态无阻塞 open）✅
- 两轮优缺点对抗完成，🔴 项已用反向测试锁死 ✅
- 与 R2 锚定无冲突（`symbol-anchor.md` §3 已判定不回 R1）✅
- **准予冻结基线，进 R4 建单。**
