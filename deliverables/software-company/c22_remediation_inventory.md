# C-22 承接卡盘点：C 桶建卡模板 step target_file 目录级白名单与 changes[] 全等比对不兼容（§W20 F5）

- **卡**：`T-1789397153261-f23f38b8`（C-22，父 PYT 卡 `T-1788871227327-45c94bd8`）
- **性质**：模板侧修复（tests/deliverables-only，无产品代码改动 → 不触发部署门禁，见 §5）
- **HEAD**：本节内容在 commit `cd96664`（C-21 台账）工作树上复核

## 1. daemon 全等比对语义的代码依据（本 HEAD grep 实测）

唯一校验点：`rust_ext/src/daemon/task_collab_lifecycle.rs`（`git grep -n E_CHANGE_PATH_NOT_ALLOWED -- rust_ext/src` 仅此文件 L293/L314 两处）。

- **L285-290（白名单解析）**：`target_file.split([',', ';'])` → `str::trim` → `replace('\\', "/")`
  ——归一规则 = **`;`/`,` 拆分** + **`\`→`/`**，与卡描述一致。
- **L307-316（全等拒绝）**：`changes[].file_path` 同样归一后，命中条件为
  `allowed.iter().any(|item| item == &file_path)` —— **字符串全等**，无目录前缀/通配语义。
  目录级 target_file（`tests/`、`rust_ext`、`deliverables/software-company/`）与任何真实
  文件路径（`tests/foo.py`）都不可能全等 → 恒 `E_CHANGE_PATH_NOT_ALLOWED`
  （错误信息逐字：「文件 {file_path} 不在步骤白名单 {target_file}」）。

实测佐证（历史，非本卡复跑）：

- 卡 D `T-1789365537230-c3f02eb4`（C-17）step2/step3：目录级白名单下报
  `E_CHANGE_PATH_NOT_ALLOWED`，处置 = changes 只报白名单内文件 + evidence 留痕（backlog §W20 F5 记录）。
- C-13 卡先例 §4.1：同源处置（`changes=[]` + evidence/commit 留痕）。
- 本仓库治理记忆（AGENTS 侧 ledger）同样记录「`changes[].file_path` 与 `task_steps.target_file`
  **全等比对**（`;`/`,` 拆分、`\`→`/`）；不符 → `E_CHANGE_PATH_NOT_ALLOWED`；verify 步可 `changes=[]`」。

## 2. 现存模板全部 step target_file 盘点（build_cards()，12 张卡 4 步/卡）

盘点基准：`deliverables/software-company/create_c_bucket_remediation_tasks.py` @ HEAD（1298 行）。
**目录级条目共 14 处**（其余全部已是文件级，含第五批追加 C-19..C-22 的全文件级范式）：

| # | 行号（@HEAD） | 卡 | 步 action | 现值（目录级，缺陷） | 修复目标（文件级） |
|---|---|---|---|---|---|
| 1 | 253 | C-04..C-07 | test | `tests/` | `tests/test_cli_005_http_rpc.py;…;tests/test_cli_066_http_rpc.py`（7 个既有 CLI 回归文件，`;` 连接） |
| 2 | 363 | C-13 | test | `tests/` | `tests/test_c13_semgrep_dispatch_wiring.py` |
| 3 | 372 | C-13 | release_verify | `rust_ext` | `rust_ext/src/daemon/mod.rs;rust_ext/src/daemon/snapshot_state.rs;rust_ext/src/daemon/route_matrix.rs`（= 本卡 implement 步声明的三文件） |
| 4 | 405 | C-14..C-15 | adjudicate(step0) | `deliverables/software-company/` | `deliverables/software-company/T-1789290073049-6442e268-evidence.md` |
| 5 | 437 | C-14..C-15 | test | `tests/` | `tests/test_c14_c15_assignment_contract.py` |
| 6 | 446 | C-14..C-15 | release_verify | `rust_ext` | `rust_ext/src/daemon/admin_handlers.rs`（= 本卡 implement 步声明文件） |
| 7 | 490 | C-16 | adjudicate(step0) | `deliverables/software-company/` | `deliverables/software-company/T-1789365537146-bef4c2e4-evidence.md` |
| 8 | 544 | C-16 | release_verify | `rust_ext` | `rust_ext/src/daemon/task_collab_lease.rs`（= 本卡 implement 步声明文件） |
| 9 | 590 | C-17 | adjudicate(step0) | `deliverables/software-company/` | `deliverables/software-company/T-1789365537230-c3f02eb4-inventory.md` |
| 10 | 629 | C-17 | test | `tests/` | `tests/test_c17_admin_route_workspace_authority.py` |
| 11 | 638 | C-17 | release_verify | `rust_ext` | `rust_ext/src/daemon/snapshot_state.rs`（= 本卡 implement 步声明文件） |
| 12 | 693 | C-18 | adjudicate(step0) | `deliverables/software-company/` | `deliverables/software-company/T-1789377001689-0aee0a6c-inventory.md` |
| 13 | 748 | C-18 | release_verify | `deliverables/software-company/` | `deliverables/software-company/T-1789377001689-0aee0a6c-inventory.md` |
| 14 | 808 | C-19 | adjudicate(step0) | `deliverables/software-company/` | `deliverables/software-company/T-1789392878852-bb9bef18-inventory.md` |

行号说明：以上行号为 step0 复核时点实测（本 HEAD），修复 diff 后会漂移；以修复 diff 实际
命中数（14 处）与校验测试为准，不以行号为准。

## 3. 预声明命名约定（task_id 建卡前未知）

建卡时 task_id 尚未生成（daemon 铸 id），故 step0 类「文档尚未存在」场景**不能**用
`<task_id>-xxx` 作 target_file（无法预声明）。约定：

- 盘点/复核类 step0 文档：确定性前缀名 `deliverables/software-company/cNN_remediation_inventory.md`
  （C-20/C-21 已按此范式实际执行：`c20_remediation_inventory.md` / `c21_remediation_inventory.md`）。
- 已闭环卡的既有产物：直接引用**实际产物文件名**（如 `T-<ts>-<id>-evidence.md`），
  与证据链逐字对应（本卡修复采用此原则，见 §2 表）。
- 多文件场景：`;` 连接（与 daemon L285 拆分规则对齐）。
- release_verify / test 步：target_file 声明为该卡实际改动/测试文件集合（verify 步运行时
  `changes=[]`，白名单仅作声明与后续卡片复用的一致性锚）。

## 4. 修复方案与不变量（step1/step2 执行依据）

- `build_cards()` 全部卡的 step `target_file` 为**文件级 token**：非空、无 `//` 后缀目录、
  不以 `/` 结尾、不等于 `rust_ext`/`tests/`/`deliverables/software-company/` 等目录/前缀值；
  多文件用 `;` 连接，逐段仍为文件级。
- **语义区分（F5 教训，须写入 docstring）**：step `target_file`（文件级全等，daemon
  task_collab_lifecycle.rs L285-316 校验）≠ `executor_allowed`（合同 allowed_paths，可目录
  前缀，是给 executor 的工作区边界，不做 changes 全等比对）。两者混用是本缺陷根因。
- 幂等范式不破坏：`task.list` 按 title 判重 → exists，零重复创建。

## 5. 部署披露

本卡 scope = 模板（deliverables 脚本）+ tests，**无产品代码改动**（`rust_ext/**`、`server/**`、
`db/**` 零触碰）→ **不触发部署门禁，本轮未部署 runtime**；live daemon 与 C-21 部署态一致
（endpoint 8535，PID 25756，二进制 `4EA587D3…`）。daemon 侧「目录前缀匹配」为可选项，
不属本卡 scope（若未来需要另立产品代码卡）。
