# TP-02 资产合同（ASSET_CONTRACT.md）

Task: `T-1791627784101-291afe54`（TP-02 profile、离线真实仓库与独立 oracle 资产）
Step: `S-1791627784103-2938f24c` · Assignment: `A-18c841242c170438ad8b6cf2`
生成身份: agent `A-6f1e-executor` / instance `AI-6f1e-tp01`
基线 manifest: `docs/testing/registration/source_baseline.json`
本卡权威说明: `docs/testing/TASK_BREAKDOWN.md` 的 TP-02 卡片与共同合同（本文件不重述共同合同全文，只固定资产侧的合同语义）。

> 本合同是**静态规划**产物。所有 PASS 均指"静态校验通过"，不是产品验收通过。
> 未运行产品、未构建、未联网、未写源仓（白名单外）、未复制 corpus 到源仓。

---

## 1. 范围与边界

**允许（allowed，均以 `docs/testing/planning/assets/` 为根）**

| 文件 | 产出步骤 | 角色 |
|---|---|---|
| `profiles.json` | Step 0 | 候选发布矩阵 + 决策点 |
| `corpora.json` | Step 0 | R0–R3 语料分层 + 语言矩阵 + 缺失声明 |
| `oracle_catalog.json` | Step 1 | 独立 oracle 目录（人工复核状态显式 pending） |
| `budgets.json` | Step 1 | 性能预算**规则**（未校准，数值留空） |
| `validate_assets.py` | Step 2 / Step 4 | 资产验证器（本卡的验收器） |
| `ASSET_CONTRACT.md` | Step 3 | 本文件 |

**禁止（forbidden）**

- 产品测试 / 构建 / 联网下载（本卡为静态规划，`TP-06` 才负责构建与下载）。
- 源仓写入白名单外任何路径；corpus 只允许复制到合同明列的 run 临时根，不入库、不写源仓。
- 伪造 task / identity / lease / capability；任何 unknown 结果必须阻断，不得放行。
- R2 / R3 尚无冻结名单时**不得填假路径或以合成数据替代**，必须以 `missing[]` + `DECISION_REQUIRED` 显式声明。
- 未校准不得写"SLO 已达标"；`budgets.json` 的 `budgets` 必须为空。
- Executor 不得 `task.apply` / `task.close`；Reviewer / Adjudicator 必须独立。

**run 临时根**: `C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-02/`（仅本卡可写，清理前须验证 owner / run_id / 批准父目录；本次实际使用：生成器脚本与行为级探针证据）。
**证据目录**: `deliverables/testing/TP-02/<run_id>/`（合同指定位置，本轮未创建——本轮证据落在 run 临时根，待任务验收冻结其证据 manifest）。

---

## 2. 验证器用法

```
C:/Python314/python.exe docs/testing/planning/assets/validate_assets.py --self-check
C:/Python314/python.exe docs/testing/planning/assets/validate_assets.py --verify-manifests
```

| 模式 | 做什么 |
|---|---|
| `--self-check` | 清点 6 文件白名单 → 解析四个产物 JSON（递归拒绝重复键）→ 结构与词表校验 → 可得离线资产磁盘存在性 / 散列 / revision 核验 → 跑 21 条内联正负向样本 |
| `--verify-manifests` | 在 `--self-check` 基础上额外核验 manifest 与磁盘的一致性（语料散列、oracle subject 散列、source revision 与当前 HEAD、R1 dirty 项数与真实工作树对拍） |

**退出码**: `0` = 全部通过；`1` = 发现问题。负向样本必须非零退出（fail-closed）。
`--self-check` 通过 **不等于** `--verify-manifests` 通过；两条都是卡片验收命令，任一非零即阻断。
两条命令都不带参数（路径解析只在验证器内部发生，不接受外部路径参数，避免注入）。

**路径解析规则（冻结）**
- 验证器内部 `INVENTORY_DIR = 本文件所在目录`，`REPO_ROOT = INVENTORY_DIR.parents[3]`（assets → planning → testing → docs → 仓库根）。
- 所有资产 manifest 中的 `path` / `subject_path` 一律是**仓库相对路径**；`repo_path()` 只做 `REPO_ROOT / rel` 拼接，不接受绝对路径参数。
- 整仓语料（R1）允许 `path = "."`，按**目录**核验（见 §5）。

---

## 3. 白名单清点（6 文件）

`SELF_CHECK_FILES` = `profiles.json`、`corpora.json`、`oracle_catalog.json`、`budgets.json`、`validate_assets.py`、`ASSET_CONTRACT.md`。

任一缺失 → `E_FILE_MISSING`，整条 `--self-check` 非零退出。Step 4 会用"临时移走 `ASSET_CONTRACT.md` → 必须 exit 1 → 恢复 → 必须 exit 0"的行为级探针证明清点是**行为**而非声明（与 TP-01 同一范式）。

---

## 4. profiles.json 合同

**顶层必需字段**: `schema_version`、`candidate_profiles`、`candidate_count`、`committed_count`、`decision_points`（另有 `profile_contract.required_fields` 声明 BUILD_ENV §4 的 15 项前置 manifest）。

**每条 candidate profile 必需**: `profile_id`、`label`、`os`、`arch`、`python_version`、`frozen`、`commitment`、`evidence`。

**语义规则**

| 规则 | 错码 | 说明 |
|---|---|---|
| 计数一致 | `E_PROFILE_COUNT_MISMATCH` | `candidate_count` / `committed_count` 必须与实际条数一致（禁止用总数相等掩盖集合漂移） |
| 承诺词表 | `E_PROFILE_COMMITMENT_VOCAB` | `UNDECIDED` / `committed` / `rejected` |
| 状态词表 | `E_PROFILE_STATUS_VOCAB` | `candidate_from_ci` / `frozen_from_measurement` / `rejected` |
| 证据非空 | `E_PROFILE_MISSING_EVIDENCE` | evidence 必须是 CI 证据行列表 |
| **平台冒充防护** | `E_PROFILE_PLATFORM_SWAP` | 声明的 `os` 与 `python_version` 必须在 `evidence` 文本中**真实出现**；声明与证据脱节即阻断 |
| 冻结须有引用 | `E_PROFILE_FROZEN_WITHOUT_REF` | `frozen=true` 必须带 `frozen_ref` |
| 未冻结不承诺 | `E_PROFILE_COMMIT_UNFROZEN` | `frozen !== true` 时 `commitment` 不得是 `committed` |
| **冻结前置 manifest** | `E_PROFILE_CONTRACT_INCOMPLETE` | `frozen=true` 时 BUILD_ENV §4 的 15 项前置 manifest 必须齐全 |
| 冻结禁占位符 | `E_PROFILE_FROZEN_WITH_UNVERIFIED` | `frozen=true` 时任何字段不得残留 `UNVERIFIED` 占位符（冻结 = 实测背书） |
| 工具链锁定 | `E_PROFILE_TOOLCHAIN_UNLOCKED` | `commitment=committed` 时 `rust_toolchain_locked` 必须为 true |

**决策点**: 每条 `decision_points[]` 必须有 `question` + `owner`，`status` ∈ `DECISION_REQUIRED` / `resolved` / `rejected`。
本轮 8 个决策点（DP-01…DP-08）全部 `DECISION_REQUIRED`，其中：
- DP-03：Python 三方不一致（CI 3.12 / 本机 3.14 / `pyproject requires-python >=3.10`）；
- DP-06：Rust toolchain 未锁（`dtolnay/rust-toolchain@stable`，仓库无 `rust-toolchain` 文件）。
4 个候选 profile 全部 `frozen=false` / `commitment=UNDECIDED` / `committed_count=0`——**本轮不做任何发布承诺**。

---

## 5. corpora.json 合同

**顶层必需**: `schema_version`、`manifest_contract`、`tier_definitions`、`language_matrix`、`corpora`、`missing`。

**语言矩阵（冻结）**: 15 个 Rust tree-sitter grammar（c / cpp / python / rust / go / java / typescript / ruby / php / scala / c_sharp / kotlin / swift / elixir / hcl，来源 `rust_ext/Cargo.toml`）+ 独立 C 解析器 = **16 语言**（JS 不重复分列 TS）。`rust_grammar_count` 必须等于实际 grammar 条数且 = 15；`declared_total` 必须 = 16。未知语言 → `E_LANGUAGE_UNKNOWN`。

**四层定义必须齐全**: R0 / R1 / R2 / R3，缺 → `E_MISSING_TIER_DECLARED`。

**manifest 必填字段（每条 corpus）**: `corpus_id`、`tier`、`status`、`path`、`language`、`license_or_authorization`、`copy_method`、`oracle_source`、`applicable_cases`，以及 commit 字段（`source_revision` 或 `revision`，二者之一）。缺 → `E_MANIFEST_FIELD_MISSING`。

**status 词表**: `available_offline` / `not_applicable_pending` / `blocked_pending_decision`。
未批准 N/A 必须带 `na_basis` → 否则 `E_ILLEGAL_NA`。

**磁盘核验规则（`available_offline` 且 `verify_disk`）**

| 语料形态 | 规则 | 错码 |
|---|---|---|
| 文件级（R0） | 路径必须存在；`sha256` 必须与磁盘一致；commit 必须等于当前 HEAD | `E_CORPUS_PATH_MISSING` / `E_CORPUS_HASH_MISSING` / `E_CORPUS_HASH_MISMATCH` / `E_CORPUS_REVISION_MISMATCH` |
| 整仓级（R1，`path="."`，dirty 工作树） | **禁止**声明单文件 `sha256`（dirty 工作树散列不可复现，声明了反而误导）；必须声明 `revision` 且 = 当前 HEAD；必须显式声明 `dirty` 归属；`dirty_entries` 必须与真实 `git status --porcelain` 计数一致 | `E_CORPUS_DIR_HASH_FORBIDDEN` / `E_CORPUS_REVISION_MISMATCH` / `E_CORPUS_DIRTY_UNDECLARED` / `E_CORPUS_DIRTY_COUNT_DRIFT` |

**冻结语料** `frozen=true` 必须带 `frozen_ref`（`E_CORPUS_FROZEN_WITHOUT_REF`）。

**缺失层声明（反假路径）**
- R2 / R3 无冻结名单 → 必须出现在 `missing[]`，且每项 `status=DECISION_REQUIRED` + `required_from` + `forbidden` + `reason` + `criteria`（错码 `E_MISSING_TIER_STATUS` / `E_MISSING_TIER_INCOMPLETE`）。
- 某层**同时**出现在 `corpora[]` 和 `missing[]` → `E_FAKE_PATH_FORBIDDEN`（名单自相矛盾，禁止既要又要是）。
- 本轮 R2 / R3 全部在 `missing[]`，`corpora[]` 只有 R0 / R1，**没有任何假路径或合成数据**。

**混淆防护**: `confusion_guard[]` 记录同名诱饵（如 `tests/fixtures/container-matrix/project/calc.py` 与 `tests/convergence/seed_sample/calc.py` 同名不同内容）。诱饵散列若与真语料散列相同 → `E_DECOY_COLLISION`。
**规则**: corpus 匹配一律按 `sha256` + 符号集双校验，**禁止按文件名匹配**。

**本轮可得离线资产（真实）**

| id | 层 | 路径 | 散列 / 归属 |
|---|---|---|---|
| `CORPUS-R0-calc-seed` | R0 | `tests/convergence/seed_sample/calc.py` | sha256 `142f57c0068d42e3d26890c8ecb7d31331482aaf17f37280c0c1448eecf53d59`，source_revision = HEAD |
| `CORPUS-R1-callwarden-self` | R1 | `.`（整仓） | revision = HEAD，dirty=true，dirty_entries=13（与真实工作树对拍一致） |

---

## 6. oracle_catalog.json 合同

**顶层必需**: `schema_version`、`entries`、`entry_count`、`oracle_principles`、`human_review`（+ `source_revision` = HEAD）。

**每条 entry 必需**: `oracle_id`、`corpus_ref`、`language`、`kind`、`independent`、`human_verifiable`、`status`、`subject_path`、`subject_sha256`、`assertions`。

**语义规则**

| 规则 | 错码 |
|---|---|
| 计数一致 | `E_ORACLE_COUNT_MISMATCH` |
| 独立性 | `E_ORACLE_NOT_INDEPENDENT`（`independent` 必须 true——oracle 不得从被测产物复制） |
| 人工可复核 | `E_ORACLE_NOT_HUMAN_VERIFIABLE` |
| 断言非空 | `E_ORACLE_NO_ASSERTIONS` |
| subject 真实存在且散列一致 | `E_ORACLE_SUBJECT_MISSING` / `E_ORACLE_SUBJECT_HASH_MISMATCH` |
| 状态词表 | `E_ORACLE_STATUS_VOCAB`（`golden_from_source_read_pending_human_review` / `golden_human_verified` / `blocked_pending_decision`） |
| **本轮复核状态** | `E_ORACLE_HUMAN_REVIEW_VIOLATED`：人工复核完成前只允许 `golden_from_source_read_pending_human_review` / `blocked_pending_decision`；`human_review.state` 不得是 `done` |
| **来源读证可追溯** | `E_ORACLE_MISSING_CORRECTION`：每条 entry 必须有 `corrections_frozen` **或** `notes`（沉默省略 = 来源不可追溯，阻断）；有 `corrections_frozen` 时每条必须齐 `historical_error` / `correction` / `why_it_matters` 三元组（`E_ORACLE_CORRECTION_INCOMPLETE`） |

**本轮 4 条 entry**（全部 `independent=true`、`human_verifiable=true`、`status=golden_from_source_read_pending_human_review`）:
1. `OR-R0-PY-CALC-SYMBOL-SET` — 符号集，固化历史错误「calc.py 恰两函数」（实有 add/multiply/compute 三个模块级函数 + Calculator 类三方法）。
2. `OR-R0-PY-CALC-CALL-EDGES` — 6 条有向调用边，固化历史错误「multiply 无调用者」（实为 Calculator.scale → multiply）。
3. `OR-R0-TS-SERVICE-SYMBOL-SET` — service.ts 符号集（Repo / MemoryRepo / Service / bootstrap，接口实现归属明确），以 `notes` 记录无需纠错。
4. `OR-R0-TS-SERVICE-CALL-EDGES` — service.ts 调用边，同上。

**人工复核状态显式 pending**：本卡**不冒充**人工已复核。`calc.scale → multiply` 等纠错已固化，等待独立 Reviewer / 人工复核确认后才能转为 `golden_human_verified`（由后续卡片完成，不在本卡）。

---

## 7. budgets.json 合同

**顶层必需**: `schema_version`、`calibration`、`required_fields_per_budget`、`budgets`、`budget_count`、`freezing_rules`、`gate`。

| 规则 | 错码 |
|---|---|
| 计数一致 | `E_BUDGET_COUNT_MISMATCH` |
| **未校准不得有数值** | `E_UNCALIBRATED_BUDGET_PRESENT`（`calibration.state=NOT_CALIBRATED` 时 `budgets` 必须为空——填数值 = 伪 SLO） |
| 字段齐全 | `E_BUDGET_FIELD_MISSING` |
| 未校准禁标达标 | `E_SLO_BEFORE_CALIBRATION` |
| 门规则存在 | `E_BUDGET_GATE_MISSING` |

**本轮状态**: `calibration.state=NOT_CALIBRATED`、`budgets=[]`、`budget_count=0`。
**只冻结规则，不冻结数值**：性能预算必须包含校准环境、测量次数 / 统计方式、判定规则；校准需要真实隔离环境（TP-06 范围），本轮不校准、不猜数值。该门同时**阻断下游**：未校准时 TP-14 不得宣称 SLO 达标。

---

## 8. 基线绑定与 HEAD 一致性

- 本卡时刻 HEAD: `db952964ca49e07d77c033e886298a3e004542af`，dirty 13 项（与 R1 声明对拍一致）。
- `oracle_catalog.json.source_revision` = HEAD；R0 `source_revision` / R1 `revision` = HEAD。
- `--verify-manifests` 会把以上与当前 HEAD 实时对拍，漂移 → `E_CORPUS_REVISION_MISMATCH` / `E_REVISION_DRIFT`。
- **冻结后若 HEAD 推进，必须重发 `snapshot.publish` 并重跑验收**（快照门与 HEAD 耦合，见项目记忆）。

---

## 9. 内联正负向样本（21 条）

`validate_assets.py` 内置 `run_inline_examples()`：**1 条正向**（最小自洽四件套必须全过）+ **20 条负向**（每条注入单一缺陷，必须以**期望错误码**非零失败）。

负向覆盖卡片点名的六类：缺仓库（`E_CORPUS_PATH_MISSING`）、错 commit / hash（`E_CORPUS_REVISION_MISMATCH` / `E_CORPUS_HASH_MISMATCH`）、未知语言（`E_LANGUAGE_UNKNOWN`）、偷换平台（`E_PROFILE_PLATFORM_SWAP` / `E_PROFILE_COMMIT_UNFROZEN`）、缺外部依赖资产（`E_MANIFEST_FIELD_MISSING`）、未批准 N/A（`E_ILLEGAL_NA`）；另含 oracle / budget / tier / decoy / 冻结门等自洽项。

未以期望码失败 → `E_NEGATIVE_WRONG_CODE`；期望失败却通过 → `E_NEGATIVE_PASSED`。

---

## 10. 错误码词表（53 条）

| 域 | 错码 |
|---|---|
| 通用 | `E_DUPLICATE_JSON_KEY` `E_JSON_PARSE` `E_FILE_MISSING` `E_MISSING_FIELD` |
| profile | `E_PROFILE_COUNT_MISMATCH` `E_PROFILE_COMMITMENT_VOCAB` `E_PROFILE_STATUS_VOCAB` `E_PROFILE_MISSING_EVIDENCE` `E_PROFILE_FROZEN_WITHOUT_REF` `E_PROFILE_COMMIT_UNFROZEN` `E_PROFILE_PLATFORM_SWAP` `E_PROFILE_CONTRACT_INCOMPLETE` `E_PROFILE_FROZEN_WITH_UNVERIFIED` `E_PROFILE_TOOLCHAIN_UNLOCKED` |
| 决策点 | `E_DECISION_STATUS_VOCAB` `E_DECISION_INCOMPLETE` |
| 语言矩阵 | `E_LANGUAGE_UNKNOWN` `E_LANGUAGE_COUNT_MISMATCH` |
| corpora | `E_MISSING_TIER_DECLARED` `E_MANIFEST_FIELD_MISSING` `E_CORPUS_TIER_UNKNOWN` `E_CORPUS_STATUS_VOCAB` `E_ILLEGAL_NA` `E_CORPUS_PATH_MISSING` `E_CORPUS_HASH_MISSING` `E_CORPUS_HASH_MISMATCH` `E_CORPUS_REVISION_MISMATCH` `E_CORPUS_DIR_HASH_FORBIDDEN` `E_CORPUS_DIRTY_UNDECLARED` `E_CORPUS_DIRTY_COUNT_DRIFT` `E_CORPUS_FROZEN_WITHOUT_REF` `E_FAKE_PATH_FORBIDDEN` `E_MISSING_TIER_STATUS` `E_MISSING_TIER_INCOMPLETE` `E_DECOY_COLLISION` |
| oracle | `E_ORACLE_COUNT_MISMATCH` `E_ORACLE_STATUS_VOCAB` `E_ORACLE_HUMAN_REVIEW_VIOLATED` `E_ORACLE_NOT_INDEPENDENT` `E_ORACLE_NOT_HUMAN_VERIFIABLE` `E_ORACLE_NO_ASSERTIONS` `E_ORACLE_SUBJECT_MISSING` `E_ORACLE_SUBJECT_HASH_MISMATCH` `E_ORACLE_MISSING_CORRECTION` `E_ORACLE_CORRECTION_INCOMPLETE` |
| budgets | `E_BUDGET_COUNT_MISMATCH` `E_UNCALIBRATED_BUDGET_PRESENT` `E_BUDGET_FIELD_MISSING` `E_SLO_BEFORE_CALIBRATION` `E_BUDGET_GATE_MISSING` |
| 版本漂移 | `E_REVISION_DRIFT` |
| 样本机制 | `E_NEGATIVE_PASSED` `E_NEGATIVE_WRONG_CODE` |

---

## 11. 回滚

- 只撤回本卡已归属的 commit / 白名单 6 文件；**不得** reset 共享工作树、删旧证据、修改生产数据。
- `validate_assets.py` 的所有校验都是**只读**的（只读 JSON / `git rev-parse` / `git status --porcelain` / 文件散列），不写白名单外任何文件。
- 行为级探针改坏资产后一律以原始字节恢复，并以 sha256 复核确认恢复一致（见 Step 4 证据）。
- 运行停止只清理已证明属于本 run 的进程与临时根；强杀 / 清理失败保留证据并阻断。

---

## 12. 已知限制（不冒充已完成）

1. R2 / R3 语料**尚未提供**（无冻结名单），以 `missing[]` + `DECISION_REQUIRED` 声明，等 Planner 规划离线资产准备 / 产品范围决策。
2. 4 个候选 profile 全部未冻结、未承诺；DP-01…DP-08 全部待决策。本卡**不产出任何发布承诺**。
3. oracle 人工复核状态 = pending，**不是**人工已复核；纠错已固化但未经独立人工确认。
4. 性能预算未校准，`budgets` 为空，**不宣称任何 SLO 达标**。
5. profile 中 `python_abi` / `interpreter_path` / `c_compiler_linker` / `dependencies_sdk` / `extension_path_and_hash` / `binary_path_and_hash` 显式标 `UNVERIFIED`——这些字段必须由执行卡在隔离环境实测后回填，冻结门（§4）会阻止带占位符冻结。
6. R1 整仓语料的 dirty 归属判定未逐项完成（`gaps[]` 已声明），R1 结论须标注工作树归属。
7. 语料规模（files / loc / 符号数）未在本轮逐项计量（`scale.note` 已声明），执行卡冻结时补齐。
8. 本卡不创建 `deliverables/testing/TP-02/<run_id>/`；本轮证据在 run 临时根，待任务验收冻结证据 manifest。
