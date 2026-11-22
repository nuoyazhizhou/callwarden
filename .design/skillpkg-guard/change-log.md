# change-log · skillpkg-guard

> **只追加**。每次变更单一次提交（requirements-cw `change-ticket.md` §三）。
> 基线 `b6dfbc5`（2026-11-22）。

---

## CL-01 · 预算口径重述（2026-11-22，R5 实测后）

**类型**：非功能性（不改 AC、不改契约、不改判据）

**基线原文**：brief §6「预算：脚本 ≤200 行，测试 ≤150 行」

**实测**：

| 产物 | 总行 | 空行 | 纯注释 | docstring | **代码行** |
|---|---|---|---|---|---|
| `skills/_shared/check_skill_packages.py` | 272 | 49 | 12 | 89 | **122** |
| `tests/test_skill_packages_guard.py` | 171 | 42 | 1 | 82 | **46** |

**偏差**：总行数双双超出（272>200 / 171>150），但**代码行均远低于预算**（122≤200 / 46≤150）。
超出部分 100% 是 docstring 与空行。

**根因**：R0 写预算时按"总行"估，而同一技能集的注释政策要求「每个函数必须有 doc comment
（what + why + 错误语义）」——6 个函数 + 2 个 dataclass 的 docstring 合计 89 行是**该政策的
必然产物**，不是冗余。两个约束（行数预算 / 强制注释）在 R0 时未对齐口径。

**处置**：预算口径重述为「**代码行** ≤200 / ≤150」，实测 122 / 46 达标。
判定依据不是"数字变小了所以算过"，而是「注释行不应计入门禁预算」这一口径本身成立：
行数预算的目的是控制**实现复杂度**，注释降低而非提高复杂度。

**未做**：不删 docstring 去凑总行数。删掉后虽能"达标"，但违反同一技能集的强制注释要求，
属于用指标换质量。

**断言未放宽**：本条不影响任何 AC 的断言强度，AC-01…07 的 nodeid 与断言保持原样。

---

## 待登记（尚无变更单）

| 候选 | 状态 |
|---|---|
| `smoke.txt` 第 5 项注释描述不准（写作「import 边界 / db 退休后 production 零导入的守卫」，实际守卫的是代码图外部符号导入范围；真正的 purity 守卫是 `tests/convergence/test_db_retire_prod_purity.py`） | **未开单**：`smoke.txt` 为 FROZEN 只读，改动须单独变更单 + 用户确认（`smoke.txt` 冻结语义第 2 条） |
| `install.ps1` 的 `$localGates` 回退分支在 AC-04 生效后成不可达死代码 | 已记 `.design/parking-lot.md` |

---

## CL-02 · 读盘编码异常无干净去向（2026-11-22，code-review 发现 P2）

**类型**：契约变更（C-04 / C-06 revision + 1；新增违规码 1 个；新增 AC-08）

**发现**：`code-review-cw` 审查任务 `T-1795312692647-c9ff8c00` 时，用「中文 Windows 记事本
默认存 ANSI/GBK」这一真实场景做行为探针，结果检查器**以 traceback 崩溃**：

```
场景：smoke.txt 为 GBK 编码
load_packages ok: ['gbk']
!! 抛出 UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd1 in position 4

场景：同一输入走 main()（C-06 承诺：不抛栈 + exit 2）
!! main 抛出 UnicodeDecodeError: ...

场景：SKILL.md 为 GBK
!! 抛出 UnicodeDecodeError: 'utf-8' codec can't decode byte 0xd6
```

**为什么现有守卫接不住**：`UnicodeDecodeError` 继承 `ValueError`，**不是** `OSError`；
`main` 只 `except OSError`，`check_package` 内的 `read_text` 甚至不在任何 try 内。
⇒ 交付物违反两条自订契约：

| 契约 | 原文 | 实际 |
|---|---|---|
| C-04 | 「本函数不抛（findings 累积）」 | 读盘编码错时抛异常 |
| C-06 | 「`OSError` → stderr + exit 2，不抛栈」 | 抛 `UnicodeDecodeError` 栈，退出码由 Python 决定（1） |
| brief §5.3 数据边界 | 「BOM / CRLF / 空 SKILL.md / 超长行」 | **漏了「非 UTF-8 编码」这一类** |

**严重度**：🟡 P2。触发条件是真实场景（中文 Windows 记事本另存），后果是运维看到 Python 栈
而不是「哪个文件编码不对」，且退出码不再是约定的 2——门禁语义被破坏。

**变更内容**：

1. 新增 `_read_text(path)`：`utf-8-sig` 读取；`UnicodeDecodeError` 归一为 `OSError`
   并附带「请另存为 UTF-8」，使上层可用同一路径处理。
2. 新增违规码 `READ_FAILED`：`check_package` 内每个文件读失败 → 一条 Finding，**不中断**
   其余检查（保住 C-04「不抛」）。
3. `main` 的 `check_package` 循环补 `except OSError` 兜底（防 `_read_text` 之外的读盘失败）。
4. 新增 **AC-08**：非 UTF-8 编码的 `smoke.txt` → `READ_FAILED` 且 `exit 2`，无 traceback。
   nodeid `::test_gbk_smoke_file_reports_read_failed`。
5. brief §5.3 数据边界行补「非 UTF-8 编码」；contracts §7 码表加 `READ_FAILED`。

**断言只加不减**：AC-01…07 的断言不变；AC-08 为新增。不因"怕红"放宽任何既有断言。

**scope 快照影响**：AC-08 属 must（P2 缺陷修复必须有断言守着），并入快照 → 快照变为 AC-01…06 + AC-08。

---

## CL-03 · 冒烟集第 5 项名实不符 + purity 守卫未纳入（**待用户签字，未执行**）

**类型**：冻结资产变更（`skills/test-cw/references/smoke.txt`，STATUS: FROZEN）

**触发**：PL-02 核实时发现，问题比"注释写错"更重。

**事实（两条，均已读源码核实）**：

| # | 事实 | 证据 |
|---|---|---|
| 1 | `smoke.txt` 第 5 项注释写「import 边界：模块越界导入（db/ 退休后 production 零导入的守卫）」，但 `tests/test_external_import_scope.py` 实际测的是**代码图外部符号导入范围**（`db._import_external_packages_for_lang` / `_extract_package_symbols` / `_parse_rust_manifest` 等），与"production 零导入"无关 | `tests/test_external_import_scope.py:1-18` 文件头 + 全部 7 个用例 |
| 2 | 真正的那个守卫是 `tests/convergence/test_db_retire_prod_purity.py`（文件头自述「生产侧导入期 purity 回归守卫」，从 `pyproject.toml [project.scripts]` 动态派生入口，干净子解释器断言 `callwarden.db.*` 加载数为 0），**它不在冒烟集里** | `tests/convergence/test_db_retire_prod_purity.py:1-25` |

⇒ 冒烟集声称覆盖"production 零导入"，实际**零覆盖**。第 5 项跑绿与 purity 是否回退无关。

**建议变更（两步，需签字后执行）**：

1. 改注释：第 5 项改为「外部符号导入范围：Python/Rust/Go/Java 包依赖解析的收窄规则（与 db 退休无关）」。
2. **新增** `tests/convergence/test_db_retire_prod_purity.py` 入集——这才是第 5 项注释原本想表达的东西。

**为什么原集合没抓到**（冻结语义强制要求写）：原集合按"跨语言静态契约 + 快"筛，purity 测试落在 `tests/convergence/` 子目录，不在当时的 `tests/test_*.py` 平铺扫描视野内；且注释是**手写**的（未与文件实际内容核对），"注释说它守 purity"被当成了"它守 purity"。

**影响面**：变更后冒烟集从 5 文件 / 164 例变为 6 文件 / 164+? 例；需实测 purity 测试耗时再定（若 >15s 需评估是否另立"慢冒烟"层）。

**状态**：⏸ **未执行**。`smoke.txt` 冻结语义第 2 条要求"修改须变更单 + 用户确认"，签字后方可动。

**CL-03 执行记录（2026-11-22）**：用户当场确认「改注释 + 加入 purity 守卫」。
已执行：`smoke.txt` 第 5 项注释改为其真实职责；新增第 6 项
`tests/convergence/test_db_retire_prod_purity.py`；STATUS 记 r2（6 项 / 167 例）。
r1（5 项 / 164 例）痕迹保留在文件头的 r1→r2 说明里。
实跑 167 例全绿 rc=0（72+72+23）。`skills/test-cw/README.md` 计数同步。
提交 `27f13ef`。
