# ac-map · skillpkg-guard

> 追溯主线：REQ-01 → AC-01…07 → task_id → 证据（`evidence/<AC-id>.md`）。
> 证据位置列在 R5 验收时由 test-cw 填写；空值或指向不存在文件 = 该 AC 未通过。

| AC | 目标 | level | 验收方式（nodeid / 命令） | 证据位置 |
|---|---|---|---|---|
| AC-01 | 三个真实包（requirements-cw / code-review-cw / test-cw）全合规 → `exit 0`，stdout 含 `OK` | auto | `tests/test_skill_packages_guard.py::test_real_packages_pass` | 待填 |
| AC-02 | frontmatter 违规（`name` 与目录名不一致 / `description` 空 / 无 frontmatter / 跨包 `name` 重复）→ `exit 2` 并指明包名与原因 | auto | `::test_bad_frontmatter_detected`、`::test_missing_frontmatter_detected`、`::test_duplicate_package_name_detected` | 待填 |
| AC-03 | SKILL.md 引用 `references/<file>` 但文件不存在 → `exit 2`，报包名 + 缺失文件名。**排除安装期注入文件**（`cw-gates.md`，由 `install.ps1` 从 `_shared/` 注入，仓库内本就不存在——见 `symbol-anchor.md` F-A1）。豁免须**双向**锁定：注入项豁免成立 + 换名后立刻红 | auto | `::test_missing_reference_detected`、`::test_injected_reference_exempt`、`::test_other_missing_reference_not_exempt` | 待填 |
| AC-04 | 包内出现 `references/cw-gates.md`（违反单一源）→ `exit 2` | auto | `::test_duplicate_gates_rejected` | 待填 |
| AC-05 | `references/smoke.txt` 非注释行指向不存在的路径 → `exit 2` | auto | `::test_bad_smoke_path_detected` | 待填 |
| AC-06 | `install.ps1` 不存在 → `INSTALL_MISSING`；存在但缺 `_shared/cw-gates.md` 注入片段 → `INSTALL_MISSING_INJECTION`；两者均 `exit 2` | auto | `::test_install_missing_injection_detected`、`::test_install_missing_file_detected` | 待填 |
| AC-07 | 未知 CLI 参数 → `exit 64`（用法错误，与"发现违规"的 2 区分） | auto | `::test_usage_error_exit64` | 待填 |

## 强制并入（本需求改动触及面）

| 触发 | 并入项 |
|---|---|
| 新增 `skills/_shared/*.py` | 冻结冒烟集全量（`references/smoke.txt` 五文件） |
| 未改 `i18n/`、未改 `cli/`、未改 `docs/cli_reference.md` | 无需 i18n / 文档门禁（记录该判断，不留空） |

> 修订（R2 回环，2026-11-22）：原写「并入 `tests/test_external_import_scope.py`（import 边界）」系**误归因**——该文件实测是代码图外部符号导入范围测试（`db._import_external_packages_for_lang` 等），与本需求无关，不会因新增 `skills/_shared/*.py` 变红。挂着等于假门禁，已删。详见 `symbol-anchor.md` F-A2。

## 非目标（不建 AC）

brief §4 的四项 Out：DESIGN 版本一致性、install.ps1 语法、AGENTS 注入唯一性、扫描 `docs/` 与 `.agents/skills/`。

## 范围与优先级

- MoSCoW：AC-01…06 = **must**；AC-07 = **should**（用法错误友好度，不影响主链路）。
- **scope 快照 = AC-01…06**（must 全量）；AC-07 标 `deferred` 到下批——但注意：**首版实现若漏写 AC-07 的测试，它在 test-cw 视角仍是 `GAP`（可自动化但无测试）→ 判 FAIL 并建 P1 单**，不得因为"在快照外"就当作 N/A。

## 风险

| 风险 | 应对 |
|---|---|
| 检查器误报导致真实包被判违规 | AC-01 用真实三包做正例断言，跑在 CI 会立刻暴露 |
| **`references/cw-gates.md` 注入文件被误判为缺引用**（F-A1） | 显式常量 `INJECTED_AT_INSTALL = {"cw-gates.md"}` 豁免 + `::test_injected_reference_exempt` 反向锁定（豁免不能变成通配） |
| `smoke.txt` 路径解析基准弄错（包目录 vs 仓库根） | brief §5.4 assumption 已定：相对仓库根；用真实 `test-cw` 包做正例 |
