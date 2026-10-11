# TP-01 静态清单与 case/报告合同（CONTRACT.md）

任务：`T-1791627645904-fbe3a4c4` · TP-01 静态清单与case/报告合同
owner：`inventory-contract` · 本文件为 Step 3「文档化合同与回滚」的唯一交付物。

本文件冻结的是**规划期合同**：候选入口清单、case 合同 schema、运行报告 schema、验证器行为与回滚方式。
它不冻结 runtime 清单，也不代表任何产品功能通过。

---

## 1. 范围与边界

### 1.1 本轮做什么

- 从已审批的静态输入（source / migration matrix / categories / 参数快照 / STATIC_AUDIT / SKIP_SITES）装配候选入口清单。
- 定义 case 合同与运行报告的 JSON Schema，并把 JSON Schema 表达不了的语义规则显式列在 `x-callwarden-semantic`。
- 实现严格验证器 `validate.py`，对产物本身与正负向样本做 fail-closed 校验。
- 冻结本文件记录的解析规则、错误码、回滚方式。

### 1.2 明确不做

- 不运行产品、不构建、不联网、不读写生产业务库 / manifest / lease / CAS。
- 不调用 daemon 写面；`validate.py` 全程只读本地 JSON，不导入产品代码。
- 不修改产品源码、旧 runner、现有生成器（`docs/testing/gen_test_cases.py` 只读复用）。
- 不冻结 runtime 默认/类型/互斥；动态 default 与真实 wire schema 一律标「待核」，不捏造。
- 不产出证据目录 `deliverables/testing/TP-01/<run_id>/`（该目录按共同合同留给未来证据，本轮不创建）。

### 1.3 结果语义

`RESULT: PASS` 只表示**静态合同自洽且负向样本全部被正确拒绝**，不是产品功能通过。
验证器输出行固定带 `static planning only; not a product verdict` 后缀，防止被当作 PASS 证据引用。

---

## 2. 文件白名单

所有产物 basename 以 `docs/testing/planning/inventory/` 为根：

| 文件 | 作用 | 产出步骤 |
|---|---|---|
| `candidate_entries.json` | 静态候选入口清单（485 条）与绑定、集合、gaps | Step 0/1 |
| `case_contract.schema.json` | case 合同 JSON Schema（draft-07） | Step 1 |
| `run_report.schema.json` | 运行报告 JSON Schema（draft-07） | Step 1 |
| `validate.py` | 严格验证器（静态，不触发产品） | Step 2 |
| `validation_examples.json` | 正负向验证样本（3 正 / 14 负） | Step 2 |
| `CONTRACT.md` | 本文件 | Step 3 |

**例外声明**：`docs/testing/registration/source_baseline.json` 不在 TP-01 白名单内。
该文件在 Step 2 验收期被外部改动牵连而重冻结（详见 §7），改动由用户显式授权，并在 Step 2 的 daemon report 中逐项声明，不属于静默越界。

---

## 3. 验证器用法与路径解析规则

### 3.1 模式

```
C:/Python314/python.exe docs/testing/planning/inventory/validate.py --self-check
C:/Python314/python.exe docs/testing/planning/inventory/validate.py --candidate candidate_entries.json
C:/Python314/python.exe docs/testing/planning/inventory/validate.py --examples validation_examples.json
C:/Python314/python.exe docs/testing/planning/inventory/validate.py --schema case  --instance <case.json>
C:/Python314/python.exe docs/testing/planning/inventory/validate.py --schema report --instance <report.json>
```

- `--self-check`：清点白名单文件 + 校验真实 `candidate_entries.json` + 元校验两个 schema（Draft7 META_SCHEMA）+ 跑全部正负向样本 + 复算审批基线散列。
- `--candidate PATH`：只校验候选清单。
- `--examples PATH`：只跑正负向样本。
- `--schema case|report`：校验**实例**（不是元校验 schema 本身）。schema 元校验只在 `--self-check` 内进行。
- `--schema report` 需要合同集合（case_id 全集），当前降级为提示 `E_UNSUPPORTED`，由 `--self-check` 覆盖。

### 3.2 路径解析规则（本文件冻结）

`--candidate` 的 PATH 按以下顺序解析：

1. **绝对路径**：直接使用（`Path(raw).is_absolute()`）。
2. **相对 cwd**：若 `Path(raw).exists()`，用 `.resolve()`。
3. **相对本目录**：回退到 `INVENTORY_DIR / raw`，存在则 `.resolve()`。
4. 三者都不存在 → `E_FILE_MISSING`，**不静默回退到默认文件**。

`--examples` / `--instance` 采用「绝对 / 相对本目录」二段解析（无 cwd 段）。
因此传仓库相对路径 `docs/testing/.../validation_examples.json` 会被拼成
`inventory/docs/testing/.../validation_examples.json` 而报 `E_FILE_MISSING`；
验收时应传**裸文件名**（与任务卡验收命令形式一致）或绝对路径。

### 3.3 退出码

| 退出码 | 含义 |
|---|---|
| `0` | 全部通过，stdout 输出 `RESULT: PASS` |
| `1` | 存在违规，stderr 逐行输出 `FAIL <code>: <detail>`，末行 `RESULT: BLOCKED (N violation(s))` |

负向样本必须非零；任何 `unknown` 结果阻断，绝不放行。

---

## 4. 错误码词表

### 4.1 解析与文件

| 码 | 触发 |
|---|---|
| `E_DUPLICATE_JSON_KEY` | 任意层级重复 JSON 键（`object_pairs_hook` 递归拒绝） |
| `E_JSON_PARSE` | JSON 语法非法或读取失败 |
| `E_FILE_MISSING` | 白名单文件缺失 / 路径无法解析 |
| `E_MISSING_FIELD` | 缺必需字段 |

### 4.2 候选清单

| 码 | 触发 |
|---|---|
| `E_BASELINE_DRIFT` | `binding.baseline_document_hashes[*].match=false`，或 `--self-check` 复算与审批基线不一致 |
| `E_AUDIT_INPUTS_DRIFT` | STATIC_AUDIT 输入散列复算条数不等于记录条数 |
| `E_ENTRY_COUNT_MISMATCH` | `candidate_entries_total` / 分面板计数与真实 entries 不一致或三者不自洽 |
| `E_SET_MISMATCH` | `sets.*` 与审批 STATIC_AUDIT 全局期望不一致（strict 模式） |
| `E_SCHEMA_DRIFT_HIDDEN` | 存在漂移却把 `schema_matrix_set_consistent` 置 true，或漂移清单缺一侧 |
| `E_UNFROZEN_DENOMINATOR` | `sets.skip_ledger` 缺失（静态分母未冻结） |
| `E_MISSING_ENTRY` | entries 为空或存在无 `entry_id` 的入口 |
| `E_DUPLICATE_ENTRY_ID` | 重复入口 ID |
| `E_UNKNOWN_STATUS` | 状态不在词表内 |
| `E_MISSING_OWNER` | 入口缺责任归属 |
| `E_OBLIGATION_LOST` | 义务键不是 C1–C6 全集 |
| `E_LOST_PARAM_OBLIGATION` | 快照/schema 中声明的参数未登记参数义务 |
| `E_ILLEGAL_NA` | 标 `not_applicable_pending` 但缺冻结依据 `na_basis` |
| `E_UNKNOWN_RELEASED` | 处于 `unknown_result` 却被登记为可交付 |
| `E_SCHEMA_DRIFT_RELEASED` | 漂移工具未标 `blocked_schema_drift`，或已废弃工具仍作当前入口 |
| `E_ORPHAN_SELECTOR` | 跨端槽位引用不存在的入口 |
| `E_CROSS_END_EMPTY` | 跨端槽位无 `rpc_method` 或无 MCP 侧入口 |
| `E_CROSS_END_FABRICATED` | 声称已有 CLI 绑定但无绑定状态标记 |

### 4.3 case 合同与运行报告

| 码 | 触发 |
|---|---|
| `E_ORPHAN_SELECTOR` | `selector.resolves_to_entry` 不在候选集合内 |
| `E_UNKNOWN_RELEASED` | `implementation_status=unknown_result`，或报告含 UNKNOWN 结果 |
| `E_FORBIDDEN_SQL` | 前置使用 `establishment.method=forbidden_sql`（SQL 补绿） |
| `E_DIFFERENTIAL_SELF` | `oracle.kind=differential` 缺跨端 `normalization` 说明 |
| `E_PROFILE_UNFROZEN_PASSED` | `profile.frozen=false` 却标 `passed` |
| `E_REPORT_PHASES` | `phases` 不是且仅含 `setup`/`call`/`teardown` |
| `E_REPORT_RESULTS` | `results` 键不是 13 类全集 |
| `E_COLLECTION_BLOCKED` | `collection.collection_errors` 非空或 `truncated=true` |
| `E_ILLEGAL_NOT_TRIGGERED` | `NOT_TRIGGERED` 缺审查依据 `review_basis` |
| `E_EMPTY_SUITE` | `collected=0` 但存在 case 结果 |
| `E_EXAMPLES_MISSING` / `E_EXAMPLES_SHAPE` | 样本集合为空 / 项缺 `expected_code` 或 `doc` |
| `E_POSITIVE_FAILED` | 正向样本应通过却报违规 |
| `E_NEGATIVE_PASSED` / `E_NEGATIVE_WRONG_CODE` | 负向样本通过，或命中的错误码不是期望码 |
| `E_SCHEMA_META` / `E_SCHEMA_SHAPE` | schema 自身未通过 Draft7 元校验 |
| `E_UNSUPPORTED` | 模式组合当前不支持 |

---

## 5. 候选入口合同（`candidate_entries.json`）

- `schema_version`：`candidate-entries/v1`。
- **485 条入口**：`cli` 234 + `cli_flag` 8 + `mcp` 243。
- 每条入口必带：`entry_id`、`surface`、`invocation`、`status`（词表内）、`owner`、`obligations`（C1–C6 全集）、`param_obligations`、`evidence_ref`。

### 5.1 义务模型 C1–C6

| 键 | 含义 |
|---|---|
| C1 | 正常业务语义：有效前置下至少一条正常场景，独立 oracle 与业务结果一致 |
| C2 | 逐参数等价类 / 默认 / 边界 / 互斥 |
| C3 | 精确负向：无效对象/状态/权限/路径，指定错误码，无非法副作用 |
| C4 | 适用写入：承诺效果 / 幂等 / 冲突 / 回滚，前后对象与事件精确断言 |
| C5 | 适用故障与恢复：超时/断连/重启/半完成重试，状态一致无旁路成功 |
| C6 | 跨端共享语义：CLI/MCP 共同业务先独立 oracle 再比较关键语义 |

### 5.2 状态词表

| 状态 | 含义 |
|---|---|
| `pending_contract` | 已登记；逐 case 合同与运行证据未建立 |
| `params_snapshot_only` | 参数来自固化快照；runtime 默认/类型/互斥未提取 |
| `blocked_schema_drift` | MCP schema 名集合与源码/矩阵不一致，冻结被阻断 |
| `params_not_extracted` | 未提取参数快照（如 daemon server 常驻入口） |
| `deprecated_legacy` | 仍承诺的 legacy 入口，需保留兼容性验收 |
| `not_applicable_pending` | 适用性不能从入口名推断，待 TP-02 profile/corpus 冻结后判定 |

### 5.3 归属模型

`planning`=TP-01 · `profile_corpus`=TP-02 · `cli_extraction`=TP-08C/A1 ·
`mcp_wire_extraction`=A1 · `execution`=C/D 期卡 · `gates`=TP-13/E。

### 5.4 跨端映射

224 个 `rpc_method` 级槽位，只声明 MCP 侧入口；CLI 侧 `pending_extraction` 时**不伪造绑定**
（伪造会触发 `E_CROSS_END_FABRICATED`）。

### 5.5 已登记 gaps

| gap | 类型 | 严重度 | 阻断入口 | 承接 |
|---|---|---|---|---|
| GAP-14 | schema_drift | blocks_inventory_freeze | `MCP:detect_call_cycles`、`MCP:detect_dependency_cycle` | A1 |
| GAP-16 | skip_root_cause_unreviewed | blocks_skip_completion_claim | — | TP-03 / C-D / TP-13 |
| GAP-CLI-SERVER | unextracted_entry | params_unknown | `CLI:server` | TP-08C/A1 |
| GAP-XEND | cross_end_binding_pending | planning_only | — | TP-08C/A1 |

GAP-14 的两条入口状态为 `blocked_schema_drift`：**总数相等（243=243）不能掩盖集合漂移**，
缺当前工具 `detect_call_cycles`/`detect_dependency_cycle`，历史额外项 `detect_cycle`/`detect_cycles` 仍留在固化 schema。

---

## 6. case 合同与运行报告 schema

### 6.1 case 合同（`case_contract.schema.json`）

20 个必需字段：`case_id`（正则 `^(CLI|MCP|CLI-FLAG):[^/]+/(C[1-6]|NEG|LIFECYCLE)/[A-Za-z0-9_.-]+$`）、
`case_family`、`surface`、`profile`、`requirement`、`risk`、`preconditions`、`repo_revision`(R0–R3)、
`provenance`、`param_equivalence_classes`、`act`、`oracle`、`allowed_events`、
`forbidden_side_effects`、`timeout`、`cleanup`、`selector`、`implementation_status`、
`obligations`、`evidence_ref`。

语义规则（由 `validate.py` 实施，非 JSON Schema 可表达）：

1. `case_id` 全局唯一。
2. `case_family` 必须存在于候选 `entry_id` 集合（孤立 selector 拒绝）。
3. `selector.resolves_to_entry` 必须等于 `case_family` 或其跨端对端入口。
4. `profile.frozen=false` 时 `implementation_status` 不得为 `passed`。
5. `provenance.real=false` 时不得声称 `passed`。
6. `cleanup.owned_only=false` 拒绝。
7. `preconditions[].establishment.method=forbidden_sql` 拒绝（禁止 SQL 补绿）。
8. `oracle.kind=differential` 必须有跨端 `normalization` 说明。
9. `implementation_status=unknown_result` 阻断。
10. N/A 必须附冻结依据（profile/corpus/platform）。
11. schema 漂移期受影响入口的 case 不得标 `passed`。

### 6.2 运行报告（`run_report.schema.json`）

8 个必需字段：`run_id`、`run_manifest`、`phases`、`collection`、`results`、`case_results`、`denominators`、`gate_status`。

- `phases` 必须且仅含 `setup`/`call`/`teardown`。
- `results` 为 13 类全集：`passed`/`failed`/`error`/`setup_skip`/`call_skip`/`teardown_skip`/`skipped`/`xfail`/`xpass`/`unknown`/`not_applicable`/`missing`/`not_triggered`。
- `collection` 单列 `module_skips`、`truncated`、junit 聚合；`denominators` 含 `platform_frozen` 与三类分母；
  `case_results` 记录 rc/stdout/stderr/MCP 请求响应/oracle 结果/前后状态；`gate_status` 含 `blockers`/`skip_rate`/`static_planning_only`/`ready_for_review`。

语义规则：

1. UNKNOWN>0 → 阻断。
2. `collection_errors` 非空或 `truncated` → 阻断。
3. `platform_frozen=false` 时 `skip_rate` 不得作为门禁依据。
4. `NOT_APPLICABLE` 必须附冻结依据。
5. `NOT_TRIGGERED` 必须附审查依据（不伪造运行 reason）。
6. `case_id` 必须存在于合同集合。
7. `selector` 必须在 `run_manifest.selectors` 内。
8. `collected=0` 且 case 结果非空 → 空套件拒绝。
9. `static_planning_only=true` 时 `results.passed` 不得标称为产品 PASS。
10. 必需 suite 的 FAIL/ERROR/UNKNOWN=0，必需适用 case 的 SKIP/XFAIL=0。

---

## 7. 基线绑定与重冻结事件

### 7.1 绑定

`candidate_entries.json` 的 `binding` 记录 10 份审批文档散列（逐文件 `baseline_sha256`/`current_sha256`/`match`）、
8 个 inventory 输入散列、以及 STATIC_AUDIT 输入的 899/899 复算一致。
审批基线权威文件：`docs/testing/registration/source_baseline.json`。

### 7.2 重冻结事件（2026-10-10，用户授权）

| 项 | 值 |
|---|---|
| 文件 | `docs/testing/TEST_CASES.md` |
| 触发 | Step 2 验收 `--self-check` 报 `E_BASELINE_DRIFT` |
| 现象 | 该文件在 21:46 被外部重写（晚于 18:17 的基线冻结） |
| 定性 | Markdown 表格列对齐美化，纯格式改动 |
| 等价性证明 | 单元格级归一化（逐单元格 strip、分隔行统一、去空行）后与 git HEAD blob `661fb2a28dd7a946ac1ed1cba015a8f81f9a034d62d339bb8a3ca926b7bd127f` **500 行完全一致，0 处实质差异** |
| 语义源未变 | `gen_test_cases.py`、`STATIC_AUDIT.json`、`SKIP_SITES.json` 散列仍与基线一致 |
| 旧散列 | `4553414db50b262463a1c41c036076ed57194a9b671a5b33898ef97aa3e7b4b7` |
| 新散列 | `fa1572afb9d540bf469045a74ec45bed70c43c011e2b63fc9cf98e35bf28d82c` |
| 落点 | `source_baseline.json`（`file_hashes` + `refreeze[]` 溯源）、`candidate_entries.json`（`refrozen[]` + `binding.baseline_refreeze[]`） |
| 权威 | 用户 2026-10-10 显式指令「重冻结基线」 |

重冻结后基线复算 **10/10 一致**；`candidate_entries.json` 入口数仍为 485（未变），
文件自身 sha256 由 `f65288ed…` 变为 `1029101cbbacc38bfeb34429d15d91a85da2cdc95d5ecf6120aae287295ed778`。

**教训**：基线冻结后对生成文档施加格式化，会打破散列绑定而不改变语义。
后续若再发生，应先取证等价性、再走授权重冻结，禁止直接改 `match` 标志补绿。

---

## 8. 正负向样本约定（`validation_examples.json`）

- 结构：`schema_version=validation-examples/v1`，`positive[]` / `negative[]`，每项含 `name`、`kind`（`candidate`/`case`）、`expected_code`、可选 `strict_sets`、以及 `doc` / `doc_raw` / `doc_ref` 之一。
- `doc_ref` 用于引用真实大文件（避免样本膨胀）；`doc_raw` 用于构造解析期缺陷（如重复键），**注入后语法仍须合法**，否则退化成 `E_JSON_PARSE` 而非期望码。
- `strict_sets=false` 让最小样本只做内部自洽，使注入的单一缺陷可被单独钉死；真实产物用 `strict_sets=true` 绑定审批全局期望。
- 当前 3 正 / 14 负，全部在 `--self-check` 内跑通：正向 3 项通过，负向 14 项各自命中期望错误码。

---

## 9. 回滚

### 9.1 回滚范围

只撤回 TP-01 自己归属的白名单文件：
`candidate_entries.json`、`case_contract.schema.json`、`run_report.schema.json`、`validate.py`、`validation_examples.json`、`CONTRACT.md`。

### 9.2 步骤

1. **撤白名单文件**：删除上述 6 个文件（若已提交，用 `git revert` 新提交回退，禁止重写历史、禁止 `git reset --hard` 影响共享工作树）。
2. **撤基线重冻结**：把 `source_baseline.json` 的 `docs/testing/TEST_CASES.md` 散列改回
   `4553414db50b262463a1c41c036076ed57194a9b671a5b33898ef97aa3e7b4b7` 并删除 `refreeze[]`；
   同时把 `candidate_entries.json` 的 `binding.baseline_document_hashes` 对应项改回旧散列并删除 `refrozen[]`/`baseline_refreeze[]`。
   注意：由于 `TEST_CASES.md` 已被外部重写，回滚后 `--self-check` 会重新报 `E_BASELINE_DRIFT`——这是预期行为，不是回归。
3. **保留历史证据**：不删除任何历史 run 证据与 daemon `change_audit` 记录；`change_audit` 的 `change_id` 由 `(task_id, step_id, file_path)` 派生，重做同一文件会覆盖同一条审计行，这是设计行为。

### 9.3 禁止

- 不 reset 共享工作树、不删旧证据、不改生产数据。
- 不清理其他任务或生产的临时根；只清理有本任务归属证据且仍在批准父目录内的路径
  （本轮 run 临时根：`C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-01/`）。
- 不以修改 `match` 标志、放宽词表、删除负向样本的方式让验收变绿。

---

## 10. 已知限制

1. **CLI 参数快照**未与当前 runtime/argparse 比对；MCP 未取真实 wire schema。参数表为固化快照，默认/类型/互斥与语义待核（对应 `params_snapshot_only`）。
2. **独立 flag 入口（8 条）**由 `cli/main.py` 的 `create_parser()` 与 `main()` 拦截分支按行号锚定，非 runtime 枚举。
3. **MCP 源枚举**采用 `server/tools/tools_*.py` 物理 AST 扫描（12 个文件），因为本环境 `rg` 不在 PATH，原生成器 `files()` 会 fail-closed。枚举方式已记入 `source_enumeration_note`。
4. **GAP-14** 未解决前，`MCP:detect_call_cycles` 与 `MCP:detect_dependency_cycle` 恒为 `blocked_schema_drift`，不得凭总数相等补绿。
5. `--schema report` 实例校验需合同集合，当前由 `--self-check` 覆盖，未提供独立入口。
6. 本轮不产生运行证据；`case_results` / `collection` 等报告字段的合同已在 schema 冻结，待 execution 卡片填充。
