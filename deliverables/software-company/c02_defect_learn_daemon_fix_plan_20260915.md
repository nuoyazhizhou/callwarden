# C-02 / §W9 · `defect_learn` 写面失效 —— daemon 侧修复计划（Planner 就绪稿，未改代码）

- 生成时间：2026-09-15
- 基线：HEAD `b4c5756`；部署 daemon `cw-daemon.exe`（schema 60，写臂含 NF2 修复 `summary.generate`）
- 性质：**只读诊断 + 修复方案**。本文件不是卡、无 verdict；落地须经 daemon 治理建卡（见 §5）。
- 交接来源：[pyt_remaining_handoff_20260915.md](file:///c:/git_work/callwarden/deliverables/software-company/pyt_remaining_handoff_20260915.md) §3.1（其建议修法被本文 **推翻**，差异见 §1）。

---

## 1. 结论先行：交接建议的「翻 op_class」是假修复

交接文档 §3.1 建议「把 `tools_summary.py:492` 的 `'READ_ONLY'` 收紧为 `'PROTECTED_MUTATION'`」。
实测证据表明 **这不会恢复写入功能**，且会引入新矛盾：

| 证据锚点 | 事实 | 对「翻标签即修」的否证 |
|---|---|---|
| `server/daemon_client.py:3883` `route_rpc` | `op_class` **不下发 daemon**，仅当 `PROTECTED_MUTATION/GOVERNANCE_WRITE` 时给 params 附加 `request_id` | 翻标签只多一个 `request_id` 字段 |
| `rust_ext/src/daemon/query_compat_handlers.rs:7644` `handle_summary_defect_learn` | 只读连接上运行；有 qualifying 变更时**直接 `internal_error`「read-only snapshot connection rejects it」**，无变更返回 0 | handler 根本不执行 INSERT，request_id 亦不被读取 |
| `rust_ext/src/daemon/snapshot_state.rs:3097` | `defect_learn` 与 `get_summary` 等**纯读工具同臂**，经 `open_query_connection`（只读）分发 | 写面卡在只读路由臂 |
| `rust_ext/src/daemon/route_matrix.rs:224` | Rust 矩阵亦标 `OpClass::ReadOnly`（两端一致地「错」，与只读连接自洽） | 单改 Python 侧 → `compat_registry.validate_against_rust_route`（:282）比对 `operation_class` 报 `mismatch` |
| `tests/test_defect_read_rpc_http.py:424` | 断言 `op == "READ_ONLY"`，其 docstring 自陈「收紧属生产侧决策，不在本卡 scope」 | 单改 Python → 直接挂该用例 |

**真根因**：`defect_learn` 在 P0-COMPAT-v3 迁移时被当作只读工具塞进查询批，**写逻辑（`learn_defect_from_fix`）从未在 Rust 侧实现**，遂以 fail-closed 桩占位。这是一个「迁移时未落地的写面工具」，不是「一个标错的字符串」。

---

## 2. 现状行为（当前部署版实测语义）

- `defect_learn(fix_commit_hash)` → daemon 只读连接查 `git_symbol_changes`（该 commit 的 `modified` 且 old≠new 行）：
  - 无 qualifying 变更 → `{"learned_patterns":0,"learned_fixes":0,"details":[]}`（成功，但等于没学）；
  - 有 qualifying 变更 → `internal_error`（写面被只读连接拒绝）。
- 净效果：**该工具永不产生任何 `defect_fixes`/`defect_patterns` 写入**，对真实回归学习是哑功能。

---

## 3. 修复方案（对齐仓内既有写面范式，非发明）

范式来源（同文件、已部署、可照抄骨架）：`summary.generate` → `snapshot_state.rs:3290` 写臂，
`open_codegraph_db_write` 取 `&mut Connection` + `unchecked_transaction`（NF2 版）。`defect_learn` 应 **1:1 套用该写臂模式**。

### 3.1 变更点（逐文件）

1. **`rust_ext/src/daemon/snapshot_state.rs`**
   - 从只读臂（:3097 的 `"hotspot_evolution" | "defect_learn" =>` 联合分支）中 **摘除 `"defect_learn"`**；
   - 新增/并入 `summary.generate` 所在写臂分支：`let (workspace_id, mut conn) = self.open_codegraph_db_write(peer, ws)?;` 转调重写后的 handler（`&mut Connection`）。

2. **`rust_ext/src/daemon/query_compat_handlers.rs`**
   - 重写 `handle_summary_defect_learn`：删除「有变更即 internal_error」的 fail-closed 分支，改为**移植 `db/db_defect_kb.py::learn_defect_from_fix`**（约 149 行）的语义：查 `git_symbol_changes`(modified, old≠new) → 关联该 commit 前 `semgrep_findings`（经 content_hash）判定是否 qualifying defect → `INSERT defect_fixes` / upsert `defect_patterns` → 返回 `{learned_patterns, learned_fixes, details}`。整批置于 `unchecked_transaction`（对齐 NF2 的事务写法，失败整体回滚）。

3. **`rust_ext/src/daemon/route_matrix.rs:224`**
   - `OpClass::ReadOnly` → `OpClass::ProtectedMutation`，`status` 由 `transition` 收敛为 `migrated`（写面已落地）。

4. **`server/tools/tools_summary.py:492`**
   - `_route('defect_learn', {...}, 'READ_ONLY')` → `'PROTECTED_MUTATION'`（与矩阵一致；此时 `request_id` 幂等键才有意义——daemon 写面对重复 learn 去重）。

5. **`tests/test_defect_read_rpc_http.py:395` 用例**
   - 更新 `assert seen["op"] == "READ_ONLY"` → `PROTECTED_MUTATION`，并把 docstring 里「属生产侧决策」的 advisory 收口为已落地；
   - 新增 **daemon-live 正例**：同一 `fix_commit_hash` 连续调用两次 → 断言幂等（第二次经 request_id dedup 或语义幂等，不产生重复 `defect_patterns` 行），并断言有 qualifying 时 `learned_*` 计数 > 0。

> 依赖读取（**不改**）：`db/db_defect_kb.py`（作为移植参考实现）、`server/compat_registry.py`（一致性校验）。

### 3.2 为什么不能只动 Python（回滚保护）

若仅执行 3.1 的第 4 步：daemon 写面仍走只读连接 → 有数据时 `internal_error` 不变（未修复），且矩阵 `mismatch` + 用例失败（净退化）。**必须 1/2/3 同批落地**，第 4/5 步随附。

---

## 4. 影响半径 / 回归面（诚实标注）

- **Rust 写臂**：触碰 `snapshot_state.rs` 的 method 分发 match —— 与 `summary.generate`、`gate.*`、`rule.*`、`edit.*` 等**共享写臂**，改动须跑完整 daemon 写面回归（规则 24：`cargo test --manifest-path rust_ext/Cargo.toml daemon:: --lib`），不得只跑新增用例。
- **新写连接**：`defect_learn` 从 `open_query_connection` 迁到 `open_codegraph_db_write` —— 改变其 workspace/authority 解析路径，需核对该函数对 `workspace_instance_id` 的要求与只读臂是否一致（NF2 已趟过此路）。
- **幂等语义**：写面接入 `request_id` dedup，需确认 daemon mutation ledger 对 `defect_learn` 这一 method 的 dedup 键已注册（否则重放不生效）。
- **兼容性**：当前对 `defect_learn` 的既有「0 结果」调用方行为会变（开始真正写）。属**期望内**语义纠正，但需在 CHANGELOG/文档记一笔。
- **部署门禁（规则 43）**：动了 `rust_ext/src/**` ⇒ 必须 `scripts/refresh_shared_runtime.ps1 -TaskId <卡>` 重新构建+安装+健康校验，源码测试通过 ≠ 已部署。

## 4.1 验收清单

- [ ] `handle_summary_defect_learn` 移植后：有 qualifying 变更 → 真实写入 `defect_fixes`/`defect_patterns`，无 qualifying → 0 结果且不再 `internal_error`。
- [ ] 双调用幂等：同 `fix_commit_hash` 两次，第二次不产生重复 pattern 行。
- [ ] 矩阵 `OpClass::ProtectedMutation` 与 Python `_route('...','PROTECTED_MUTATION')` 经 `validate_against_rust_route` aligned=true。
- [ ] `cargo test ... daemon:: --lib` 全绿（含共享写臂回归）。
- [ ] `test_defect_read_rpc_http.py` 更新用例通过；无新增 pytest 失败（与 HEAD 基线同集对照，junitxml 为准）。
- [ ] 重新部署：refresh_shared_runtime 回执 `status=passed`、`health.git_commit==HEAD`。
- [ ] `git diff --check` clean；commit 前缀 = 本卡自身 task_id。

---

## 5. 建卡与路由建议（关键：这是 scope 决策）

- **严重度定级**：建议从交接的 **P2 上调为 P1**——一个「声称能学习缺陷、实则永不出数据且不报错地返回 0」的工具，是**静默错误结果**，比单纯崩溃更隐蔽。（此判断权在 Planner/用户。）
- **交接建议白名单不可用**：交接 §3.1 建议 `forbidden: rust_ext/src/**`，但根因与必修点**正在 `rust_ext/src/**`**。沿用该白名单 → 结构性不可达（同 PYT step#5「acceptance 与冻结 scope 不相容」的 `owner_route=planner` 先例）。
- **路由**：按 AGENTS.md/role-protocol 双轨，这是 **scope/Contract 缺陷**，非实现缺陷 → 应由 **Planner 重规划**（扩白名单到 `rust_ext/src/daemon/**`、`server/tools/tools_summary.py`、`tests/test_defect_read_rpc_http.py`；`forbidden` 保留 `db/**`、直连 SQLite、`scripts/refresh_shared_runtime.ps1`），再派 Executor；不得由 Executor 硬翻标签交差。
- **原子性**：3.1 的 1/2/3/4/5 是**同一 ownership 的单一交付**（一个写面工具落地），不宜拆并行卡（所有权文件相交）。

## 6. 工作量粗估

- Rust handler 移植（149 行 Python 语义 + 事务 + INSERT/upsert）+ 写臂迁移：主要成本。
- 矩阵/薄壳/测试同批：小改。
- 完整 daemon 回归 + 重新部署门禁：时间成本大头。
