# scan-code-review · skillpkg-guard

> code-review-cw 阶段产出。审查对象：`skills/_shared/check_skill_packages.py`（272 行）+ `tests/test_skill_packages_guard.py`（18 例）。
> 审查基准：基线 `b6dfbc5` 的 6 张合同卡 + ac-map AC-01…07。
> reviewer 身份：`cw-rev-01` / `sess-cw-rev-01` / instance `cw-rev-01-inst`；executor 身份 `cw-exec-01` / `cw-exec-01-inst`（独立性门禁已过，但**同一会话主体自审，属程序性独立、非实质独立**，如实标注）。

## 1. 覆盖矩阵（卡片 → 实现 → 断言）

| 卡片 | 实现 | 断言 | 结论 |
|---|---|---|---|
| C-01 `parse_frontmatter` | 有，含折行续写 | `test_parse_frontmatter_edge_cases`、`test_missing_frontmatter_detected` | ✅ |
| C-02 `load_packages` | 有，含空目录/不存在返回 [] | `test_no_packages_is_violation`、`test_real_packages_pass` | ✅ |
| C-03 `referenced_reference_files` | 有，`../_shared` 不误取 | `test_injected_reference_exempt` | ✅ |
| C-04 `check_package` | 有，五类顺序固定、不短路 | 6 条负例 + AC-01 正例 | ⚠️ 见 F-01 |
| C-05 `format_report` | 有，ok 与退出码同源 | `test_json_report_parsable` | ✅ |
| C-06 `main` | 有，三态退出码 + SystemExit 转 64 | `test_usage_error_exit64`、`test_no_packages_is_violation` | ⚠️ 见 F-01 |

## 2. 发现

### F-01 · 🟡 P2 · C-04 / C-06 契约不一致：非 UTF-8 编码致 traceback（**已修复，CL-02**）

| 项 | 内容 |
|---|---|
| 文件 | `skills/_shared/check_skill_packages.py` |
| 符号 | `check_package` / `load_packages` |
| category | 异常路径缺失（happy path 假设） |
| REQ | `REQ-01` |
| 问题 | 三处 `read_text(encoding="utf-8-sig")` 只防 BOM，不防编码。中文 Windows 记事本默认存 ANSI/GBK —— `smoke.txt` 或 `SKILL.md` 为 GBK 时抛 `UnicodeDecodeError` |
| 为什么不只是「理论上」 | `UnicodeDecodeError` 继承 `ValueError`，**不是** `OSError`；而 `main` 只有 `except OSError`，`check_package` 内的读取根本不在 try 内 ⇒ 检查器以 Python 栈崩溃，退出码退化为 1，「用法错(64)/违规(2)/合规(0)」三态语义被破坏 |
| 证据 | 修复前探针：`!! 抛出 UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd1 in position 4`（smoke.txt 场景）；`main` 同样抛 |
| why | C-04 明写「本函数不抛（findings 累积）」；C-06 明写「`OSError` → exit 2，不抛栈」；brief §5.3 数据边界只列了 BOM/CRLF/空文件/超长行，**漏了「非 UTF-8 编码」这一类** |
| suggestion | ① `_read_text` 把 `UnicodeDecodeError` 归一为 `OSError`；② 新增 `READ_FAILED` 码，单文件读失败只记一条不中断；③ `main` 循环补 `except OSError` 兜底；④ 新增 AC-08 双断言锁住 |
| 修复状态 | **已修**：任务 `T-1795313464539-82567a30`（`action:fix`），变更单 `CL-02`，卡片 C-04/C-06 revision → 2，新增 C-07 卡 |
| 修复后证据 | GBK smoke.txt → `READ_FAILED` + `rc=2` 无栈；GBK SKILL.md → `cannot read` + `rc=2` 无栈；pytest 18 例全绿；真实三包仍 `OK/exit 0` |

### F-02 · 💭 P3 · `referenced_reference_files` 不感知代码块，可能误报

| 项 | 内容 |
|---|---|
| category | 可维护性 / 误报风险 |
| 问题 | 正则 `references/([\w.\-]+)` 会在围栏代码块内的示例路径上命中 |
| 现状 | 三包 SKILL.md 实测无此类假路径（AC-01 正例证明） |
| 处置 | **不修**，`design-gate.md` 第 2 轮缺点 #3 已登记理由（宁可吵不可漏）；误报时改文档成本远低于漏报导致装机断链 |

### F-03 · 💭 P3 · `DUPLICATE_GATES` 用 `.exists()` 而非 `.is_file()`

| 项 | 内容 |
|---|---|
| category | 一致性 |
| 问题 | 同函数内其他路径判定用 `is_file()`，仅此处用 `exists()`；若 `cw-gates.md` 是目录也会报 `DUPLICATE_GATES` |
| 处置 | **不修**。目录名为 `cw-gates.md` 的场景不存在；改成 `is_file()` 反而会漏掉「目录占位」这种更该报的畸形 |

### F-04 · 💭 P4 · 注释密度高于仓库平均，但与注释政策一致

`get_uncommented_symbols` 全库多数符号 `has_comment: false`（建单时 cw 检索可见），本文件 7 个函数 100% 有 doc comment。属**符合要求**而非问题，记录以免后续被当成"过度注释"整改。

## 3. 未发现问题的核查项（显式记录，不留空）

| 核查 | 方法 | 结论 |
|---|---|---|
| 死函数 / 孤儿符号 | cw `search_symbols` + 人工调用链 | 7 函数全部有调用方（`_read_text` ×3、其余各 ≥1） |
| 死代码 | 读实现 | 无 |
| 静默吞异常 | 读实现 | 两处 `except OSError` 均转 Finding 或 stderr，无 `pass` |
| 裸 `unwrap` 等价物 | 读实现 | 无 `except: pass`、无 `assert` 做控制流 |
| 副作用越界 | 读实现 | 零写入、零网络、零 daemon、零临时文件 |
| 幂等 | 连跑两次比对 | findings 与退出码一致 |
| 依赖边界 | 读 import | 仅 stdlib 7 个（argparse/json/re/sys/dataclasses/pathlib + `__future__`），无 yaml |
| 预算 | tokenize 统计 | 代码行 122/150（脚本 272 总行含 89 行 docstring，见 CL-01 口径重述） |
| `check_items` 契约 | `cw task show` 读回 | 多行原样显示，未被转义（G6.3 实测成立） |

## 4. 结论

- **P1/P0：0 条**
- **P2：1 条（F-01）→ 已开 fix 单并修复 + 回归断言**
- **P3/P4：3 条，全部「不修 + 写清理由」**
- 无「发现但未建单」的悬空项

> 审查诚实性声明：本次 reviewer 与 executor 为同一会话主体，仅通过 `agent_instance_id` / `session_id` 分离满足门禁的形式要求。**这不是实质独立审查**。真实四角色流程中此项应由独立 agent 承担；本轮的独立性证据只能说明「门禁机制可运转」，不能说明「审查结论无自审偏差」。
