# symbol-anchor · skillpkg-guard

> R2 产出。读符号一律走 cw；grep 仅用于交叉核对（双重核对要求）。日期 2026-11-22。
> 工作区：registry=1193 / task_db=1 / instance=`4baea3ff12c2ea5c`。

## 1. 锚定表

| 设计元素 | 判定 | cw 证据 | 交叉核对 |
|---|---|---|---|
| `skills/_shared/check_skill_packages.py`（主脚本） | **new** | `search_symbols("check_skill_packages")` → **空结果**（图中不存在该符号） | `ls skills/_shared/*.py` → 仅 `cw-gates.md`，无 .py |
| 主入口 `main(argv) -> int` | **reuse（形态模仿）** | `cicd/known_red.py:206` `def main(argv: Optional[List[str]] = None) -> int`；`cicd/check_ci_gates.py:252` 同形 | 两处均 `sys.exit(main())` 收尾 |
| 退出码三态 `0/2/64` | **reuse（约定）** | `cicd/known_red.py:37-39` `EXIT_OK=0 / EXIT_EXPIRED=2 / EXIT_USAGE=64` | brief §5.4 confirmed 项 |
| frontmatter 解析 | **new** | `search_symbols("frontmatter")` 无命中；仓库无 yaml 依赖（brief NFR 禁 yaml） | `grep -rln frontmatter cicd/ scripts/ tools/` → 0 文件 |
| `tests/test_skill_packages_guard.py` | **new** | 文件不存在 | `ls` → (none) |
| 测试导入机制 | **reuse（先例）** | `tests/test_cicd_gates.py:36-41`：`_REPO_ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(_REPO_ROOT))` 然后 `from cicd import check_ci_gates` | 实测：`skills._shared` 解析为 PEP 420 `_NamespacePath`（无 `__init__.py` 也可导入），桩文件验证 `from skills._shared.X import Y` 成立 |
| 测试风格（hermetic） | **reuse（先例）** | `tests/test_cicd_gates.py` docstring：「全部 fixture 用 tmp_path 临时构建，不落仓库……单测保持 hermetic」 | — |
| `_shared/` 注入片段原文 | **reuse（被检查对象）** | 三包 `install.ps1` 第 33-46 行结构完全一致；关键行 `$sharedGates = Join-Path (Split-Path $PSScriptRoot -Parent) '_shared\cw-gates.md'` | 三包逐行 diff 无差异 |

## 2. 锚定发现（3 条，其中 1 条强制回 R0）

### F-A1 · 🔴 `references/cw-gates.md` 被引用但仓库中不存在 → AC-03 会误报 3 次

实测：

```
skills/requirements-cw/references/  → change-ticket.md contract-card.md gate-card.md
skills/code-review-cw/references/   → （空目录）
skills/test-cw/references/          → evidence-spec.md smoke.txt
```

三包 SKILL.md **全部**引用 `references/cw-gates.md`（如 test-cw:33、:93、:126；code-review-cw 阶段零），但**仓库里没有这个文件**——它是 `install.ps1` 在安装时从 `_shared/` 注入的。

⇒ 若按 AC-03 字面「引用即须存在」实现，真实三包会**全部判违规**，AC-01（真实包全合规 exit 0）与 AC-03 直接互斥——**测不出来，因为正例本身就是反例**。

**处置：回 R0 修 ac-map/brief**（已执行，见 §4）。规则改为「引用完整性检查排除安装期注入文件」，注入文件集显式声明在脚本常量里并附注释指向 `install.ps1`。

### F-A2 · 🟡 ac-map「强制并入」误引 `test_external_import_scope.py`

ac-map 原写：「新增 `skills/_shared/*.py` → 并入 `tests/test_external_import_scope.py`（import 边界）」。

读源码（`tests/test_external_import_scope.py:1-18`）实际是**代码图外部符号导入范围**：`db._import_external_packages_for_lang` / `_extract_package_symbols` / `_parse_rust_manifest` 等，与「Python 模块 import 边界」无关。把本需求挂上去是**误归因**——它不会因新增 `skills/_shared/*.py` 而变红，挂着等于假门禁。

**处置：已从 ac-map 移除**，改为仅「冻结冒烟集全量」。真正的 import 风险由 §1「测试导入机制」条目的实测结论覆盖。

> 这条是本次端到端跑的**实质收获**：不做 R2 锚定就实现，会交出一个"正例必红"的检查器和一条永真豁免。

### F-A3 · 💭 `install.ps1` 的 `$localGates` 回退分支在 AC-04 生效后成为不可达死代码

三包 `install.ps1:36-41` 均有：

```powershell
$localGates = Join-Path $PSScriptRoot 'references\cw-gates.md'
if (-not (Test-Path $sharedGates)) {
    if (Test-Path $localGates) { Write-Warning "改用包内副本（可能过期）" }
    else { throw "找不到共享门禁…" }
}
```

AC-04 一旦被执行（包内禁止存在 `references/cw-gates.md`），`Test-Path $localGates` 在合规仓库里**恒为假**，该分支永久不可达。

**处置：不改，记入停车场**。理由：改 `install.ps1` 属三包行为变更（删降级路径 = 分发容错下降），超出"新增检查器"的 REQ 边界；且当前警告是显式的，风险可控。停车场条目：「AC-04 落地后清理 install.ps1 的包内副本回退分支（三包同改，需一次分发影响评估）」。

## 3. 回环判定

| 项 | 是否触发回环 | 说明 |
|---|---|---|
| F-A1 | **是，回 R0** | 与 AC-03 实质冲突（正例必红）。不改流程图（`flowchart.md` 异常分支 2 的形状不变），仅改判据范围 |
| F-A2 | 否 | 只删 ac-map 一行误归因，不动设计 |
| F-A3 | 否 | 超范围，进停车场 |

⇒ **不需要回 R1 改图**。R1 两张图（mindmap / flowchart 四异常分支）在锚定后仍成立。

## 4. 回 R0 的修订（已落盘）

| 文件 | 修订 |
|---|---|
| `ac-map.md` | AC-03 目标补「排除安装期注入文件（`cw-gates.md`）」；「强制并入」行删去 `test_external_import_scope.py` 误归因；风险表补 F-A1 对应行 |
| `requirements-brief.md` | §3 检查项 2 补豁免说明；§5.4 assumption 新增「注入文件集为显式常量，非通配」 |

## 5. R2 退出准则核对

| 准则 | 结果 |
|---|---|
| 每个设计元素有判定 + cw 证据 | ✅ §1 共 8 行，均有 cw 调用或实测命令 |
| 无未声明降级 | ✅ 图可用（`search_symbols` 正常返回），未降级 |
| 与 R1 图无实质冲突 | ✅ 图结构不变 |
| 卡片不要求（属 R3） | ✅ 本文不含 contract 卡片 |
