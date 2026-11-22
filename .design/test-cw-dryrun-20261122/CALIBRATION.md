# test-cw 试跑校准记录（B 路线产出）

> 靶点：`f8b8588`（§46 报告型命令 rc 新语义）。选它的理由：自带 `.specs/` 需求工件、专用测试文件、
> 明确的负向分支（semgrep 失败 rc=1）、以及强制并入项（i18n / 文档门禁）——四条路径一次覆盖。
> 执行结果：**149 例全绿，6 条自动化 AC PASS，1 条 manual AC 判 N/A → 需求未关闭**。
> 下面是「按立项稿真跑一遍」暴露的 6 处形态差异，逐条给出对判据/流程的修正。

## F1 · 源 B 的检索形态必须是三种，不是一种

| 形态 | 命中 |
|---|---|
| 符号名 `_exit_on_findings`（snake） | 1 文件 |
| 变更语义 `fail_on_findings`（snake） | **0 命中** |
| CLI 字面量 `--fail-on-findings`（kebab） | 1 文件 |
| i18n key `cli.args.fail_on_findings` | 仅出现在 `i18n/*.json`，测试侧不直接引用 |

**问题**：立项稿 §4.1 源 B 写「AST 扫 import / 属性引用 / monkeypatch 字符串 / MCP 工具名字面量」，
但真实入口是 **argv 里的 kebab-case 字面量**——按符号名扫会全空，而"全空"在流程里会被误判成 `GAP`（判 FAIL），
形成**假缺陷**。

**修正**：源 B 检索式固定为三类并集——符号名（snake）/ CLI 参数名（kebab）/ i18n key 字面量；
三者皆空才允许判 `GAP`。

## F2 · 源 C 只能作候选池，且包名依赖 PYTHONPATH 布局

- `import cli.main` / `from cli import main` → **0 命中**；真实是 `from callwarden.cli import main`（因为 `PYTHONPATH=C:/git_work`，仓库根是包目录）。
- 按正确包名取反向 import 闭包：**140 文件 / 1307 nodeid**，直接超过立项稿的 800/批上限。

**问题**：立项稿把源 C 写成"必须再取闭包迭代到不动点"，但没写清它是**候选池**而非执行集；
照字面执行会一次性吃到 1307 例，要么违反批次上限，要么被迫"跑一半贴绿"（正是 §4.5 要禁的）。

**修正**：源 C 的产出只用于**发现候选**，执行集 = 源 A ∪ 源 B ∪ 强制并入项；只有当上述并集为空、
或改动触及跨模块契约（RPC/schema/manifest）时才允许升格为闭包子集，且必须按 ≤800/批拆。

## F3 · C1/C2 的静态启发式有假阴性，C6 才是硬证据

实测断言形态：`_exit_code()` helper 捕获 `SystemExit` 后返回 `excinfo.value.code`，测试体里是
`assert code == 3`。

- 按 `C2` 的字面判据（"必须断言错误码/消息"）用 grep 找 `excinfo.value.code` → **0 命中**；
- 按 `C1` 的 nodeid 命名匹配（`invalid|error|fail|…`）→ AC-01/02/04 的 nodeid 不含这些词 → 会被判"无负向用例"。

**但 mutation 探针给出了相反结论**：把 `and count > 0` 改成 `and count < 0` 后，
**8 个用例立即变红**（7 条 flag 用例 + 1 条 semgrep incremental），revert 后 sha256 与改前一致、`git status` 干净。

**结论**：C1/C2 的静态检查**只能做 advisory**（提示"可能有缺口"），不能作为 FAIL 依据；
**C6 是本轮唯一被证明有效的"测试是否真会红"判据**，应升为 must 故事的硬判据（与 §12-3 拍板一致）。

## F4 · 新鲜度规则要双 revision，单看 `HEAD` 会误杀追溯验收

立项稿 §5 ① 写 `evidence.revision == git rev-parse HEAD`。本次试跑时 feature 提交是 `f8b8588`，
而 HEAD 已是 `c10a1e5`（后续几笔无关提交）——按字面判据这条会直接 FAIL，但代码事实上没变：

```
git diff --stat f8b8588..HEAD -- cli/main.py i18n/*.json tests/test_report_rc_semantics.py docs/cli_reference.md
# 输出为空 → 被测代码零漂移
```

**修正**：① 拆两个字段——

- `run_revision` = 执行时的 HEAD（防"用旧证据冒充本轮"）；
- `code_revision` = 被测代码所属提交，须满足 `git diff --stat <code_revision>..HEAD -- <changed files>` 为空；
- 两者之一缺失或漂移非空 → FAIL（不引入"追溯模式"豁免，避免开洗白口子）。

## F5 · 工具层：`grep -r` 在本仓会被拖死，`rg` 也不在 PATH

- `grep -rl` 扫 `tests/`（11815 个 py 文件）→ 被 SIGTERM 终止，无输出；
- `rg --version` → `command not found`（bash 环境无 ripgrep）。

**修正**：SKILL.md 必须写明——目录级检索用**受限路径 + 文件类型**（`tests/*.py` 而非 `tests/` 递归），
或走 cw 的 `file_grep` / MCP 检索工具；禁止在本仓直接 `grep -r tests/`。这条不是风格问题：
一次被 SIGTERM 的检索会让"源 B 为空"与"检索失败"无法区分，直接触发 F1 说的假 GAP。

## F6 · 证据生成应脚本化，人只写判读

6 份 AC 证据里的 nodeid 完成表、统计、命令原文全部由 junit 解析生成（本记录同批脚本产出）；
人工只补"判读"与结论。手抄 149 行 nodeid 必错，且错法不可察（漏一行 = 判据 ② 失效）。

**修正**：test-cw 的 SKILL 写明两步——`先跑批 → 解析 junit 生成证据骨架 → 人工补判读`；
禁止手写 nodeid 表。junit 文件必须随证据一起落盘（本次 `reports/junit-dryrun.xml`）。

## 与立项稿的净差（v0.3 → 待升 v0.4）

| # | 立项稿原文 | 应改为 |
|---|---|---|
| 1 | 源 B：AST 扫 import / 属性引用 / monkeypatch / MCP 工具名 | 三类字面量并集：snake 符号名 + **kebab CLI 参数名** + i18n key |
| 2 | 源 C：反向 import 闭包迭代到不动点 | 闭包仅作候选池；执行集 = A ∪ B ∪ 强制并入；升格须 ≤800/批拆 |
| 3 | C1/C2 作为 FAIL 判据 | 降为 advisory；C6 升为 must 硬判据 |
| 4 | ① `revision == HEAD` | ① 拆 `run_revision` + `code_revision`，且相关文件零漂移 |
| 5 | （缺）检索工具约束 | 禁 `grep -r tests/`；用受限路径或 cw 检索工具；检索失败 ≠ 无命中 |
| 6 | （缺）证据生成方式 | junit → 脚本生成骨架 → 人工判读；junit 随证据落盘 |

## 顺带验证成立的部分（不改）

- 判据 ③④⑤⑥ 全部可机器判定，149 例一次跑完无歧义；
- `N/A` 的合法形态真实存在（spec §3 活体验证），且它确实导致"需求未关闭"——关闭判据没有漏；
- 强制并入清单有效：改动含 `i18n/*.json` 与 `docs/cli_reference.md`，若不做强制并入，这两条 AC 会静默缺席；
- 批次 149 例 < 800、耗时 < 2 min，本地直跑路线成立（不需要 CI）。

## 附：冒烟集实测（实现的 smoke.txt）

按 `skills/test-cw/references/smoke.txt` 整批跑（2026-11-22，CPython 3.14.3 / pytest 9.0.2）：

```
tests/test_abi_contract.py: 27
tests/test_cli_reference_sections.py: 8
tests/test_external_import_scope.py: 7
tests/test_i18n_key_existence.py: 13
tests/test_i18n_placeholder_consistency.py: 109
TOTAL = 164 例，elapsed ≈ 38.6s，全绿
```

结论：164 < 800 单批上限，作为「任何 scope 都并入」的兜底可行；占时最长的是 `test_i18n_placeholder_consistency.py`（109 例，约 52% 时长）。
