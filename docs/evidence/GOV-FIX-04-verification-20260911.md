# GOV-FIX-04 终态投影短路 — 验证报告

- **日期**：2026-09-11
- **代码提交**：`031355e5bc10711f78c32cdc92bfddda26c6dfc4`（fix） / `386c773`（ledger）
- **部署二进制**：`~/.callwarden/runtime/current/cw-daemon.exe`，sha256 `8840a1733f6fc749aa328964fe4f2cc4d0ed…`
  （旧件备份 `cw-daemon.exe.bak-20260911-govfix04`）
- **daemon**：pid 33592，endpoint `http://127.0.0.1:1615`，`worker_status=healthy`，schema_version 60

---

## ① closed/reverted 无合同卡的投影误报 — 已修复

### 症状（修复前实测）

| 卡类型 | 修复前投影 |
|---|---|
| closed + 有 binding + 无合同（6 张 P0-COMPAT-v2） | `workflow_status=governance_blocked` / `decision=BLOCKED` |
| closed + 无 binding（历史卡） | 硬错误 `E_WORKSPACE_AUTHORITY_UNAVAILABLE` |
| closed + 合同缺 `identity_policy`（3 张） | `governance_blocked`（RPC policy overlay 覆盖） |

### 根因：同一投影有 **3 层覆盖点**

| 层 | 文件 | 旧行为 |
|---|---|---|
| 规则 4 合同解析 | `rust_ext/src/daemon/task_loop/next_action.rs` | 在 rule 12 `closed => complete_outcome` **之前**短路成 `BLOCKED` |
| 规则 1/3 binding 校验 | 同上 | 更早，无 binding 直接抛 `E_WORKSPACE_AUTHORITY_UNAVAILABLE` |
| RPC policy overlay | `dispatch.rs::handle_task_next_action`；`task_collab_query.rs::tree_governance_projection` | 合同缺 `identity_policy`（`TaskContractPolicyState::Unresolved`）→ 把终态**覆盖回** `governance_blocked` |

### 修法（终态短路，不扩大化）

- `next_action.rs`：`evaluate_next_action_inner` 在规则 1/3/4 **之前**判断 `closed` → `complete_outcome`、
  `reverted` → 新增 `terminal_outcome`；非终态继续走原有 fail-closed 规则。
- `task_collab_query.rs`：`tree_governance_projection` 在 binding 查询之前短路终态 → `completed`/`reverted`，
  `blocking_reasons=[]`。
- `dispatch.rs`：新增 `terminal_projection` guard——终态不覆盖 `workflow_status`/`decision`，
  也不广播 `claim_requirements`（终态无待 claim）。

### 验证矩阵

| 项 | 命令 / 方法 | 结果 |
|---|---|---|
| 编译 | `cargo check --all-targets` | RC=0 / **0 errors**（警告均为仓库既有） |
| 单测（新 4 + 既有 3） | `cargo test --lib -- closed_without_contract_or_binding reverted_without_contract_or_binding non_terminal_without_binding closed_task_complete_none test_task_status_closed_historical test_task_status_historical_blocked_vs_bound test_task_list_includes_daemon_governance_projection` | **7 passed / 0 failed**，无回归 |
| release 构建 | `cargo build --release --bin cw-daemon` | RC=0（增量 7m02s） |
| live·6 张 v2 | `task next-action` | 6/6 `COMPLETE / completed` |
| live·3 张 overlay 漏网卡 | 同上 | 3/3 `COMPLETE / completed` |
| live·closed 无 binding 历史卡 | 同上 | `completed`（原硬错） |
| live·随机 closed 抽样 50 张 | `task.next_action` RPC | **50/50 `completed`** |
| live·随机非终态抽样 20 张 | 同上 | 18 硬错误 + 1 governance_blocked + 1 review_pending，**0 张被误判为终态**（不扩大化） |
| 投影路径一致性 | `task show --flat` / `task governance-projection` | 均为 `workflow_status: completed` |

---

## ② 探针 / backlog 无合同卡 — 逐张核验（只读，未改动任何数据）

按点名家族（ZTEST / DIAG / `t` / 贝叶斯 / smoke / probe）筛出 **Task Contract revision = 0 的 23 张**。
投影症状：12 张无 binding → `E_WORKSPACE_AUTHORITY_UNAVAILABLE`；11 张有 binding → `governance_blocked`。

### A. 可 `cascade_close` 立即收尾（14 张）

| 卡 | 家族 | status | 结构 | 收尾依据 |
|---|---|---|---|---|
| `T-1785738813380-0e706400` | smoke | applied | 0 步 0 子 | 叶子 0 步直接收尾 |
| `T-1786086952249-b6cc3bec` | Test Task | in_progress | 0 步 0 子 | 同上 |
| `T-1786086989793-7491743c` | Test Task | in_progress | 0 步 0 子 | 同上 |
| `T-1786087014630-3d00a398` | Test Task | review | 0 步 0 子 | 同上 |
| `T-1786087031736-389bc220` | Test Task | review | 0 步 0 子 | 同上 |
| `T-1786088313941-c1f45264` | Test Task | review | 0 步 0 子 | 同上 |
| `T-1786088331508-d9097264` | Test Task | review | 0 步 0 子 | 同上 |
| `T-1787183083779-b9405964` | qa-probe | open | 0 步 0 子 | 同上 |
| `T-1787183096151-9ab65298` | qa-dedup-probe | open | 0 步 0 子 | 同上 |
| `T-1787550557781-ee98e5b4` | smoke | open | 0 步 0 子 | 同上 |
| `T-1787740451078-e9209124` | `t` | open | 0 步 0 子 | 同上 |
| `T-1787820571899-7e3c1504` | `t` | open | 0 步 0 子 | 同上 |
| `T-1787239495237-0a293bf8` | ZTEST | open | 1 步 pending，**有 2 条 supersede 出边** | GOV-FIX-03 被替代卡豁免 |
| `T-1788160425990-f08e3d64` | probe | review | 1 步且 **done** | 步骤全 done，门禁通过 |

### B. 当前无 daemon 写路径可收尾（7 张）

叶子存在 **pending 步**且无 supersede 出边 → `task close`（`E_STEPS_NOT_DONE`）与 `task cascade_close`
（叶子步骤门禁 `break`）均被拒；`task.step.resolve` 只接受 `failed` 步。

| 卡 | 家族 | status | 步骤 |
|---|---|---|---|
| `T-1786853522730-d006c978` | diagnosis | open | 3 步全 pending（inspect_auth / probe_runtime / report_recovery） |
| `T-1787209408488-ec357cec` | bootstrap-probe | open | 1 步 pending（annotate README.md） |
| `T-1787209437399-a76a471c` | bootstrap-probe | applied | 1 步 pending |
| `T-1787209465034-169c2ac8` | bootstrap-probe | applied | 1 步 pending |
| `T-1787239501902-9772e0e4` | ZTEST | open | 1 步 pending（noop f.py） |
| `T-1787239520771-fc1efb8c` | ZTEST | open | 1 步 pending（noop f.py） |
| `T-1787758490098-f2862990` | DIAG-probe | open | 1 步 pending（diagnose probe.py） |

### C. 真实 backlog，**不应关闭**（2 张）

| 卡 | 标题 | 说明 |
|---|---|---|
| `T-1788268314722-a8b6a945` | 贝叶斯语义分类优化压缩路由机制 | TokenSlim 压缩路由待办（解决 cargo/gcc 输出误落 generic_text）；描述提到子任务 T-A~T-F，但 0 子任务 |
| `T-1788268321485-5315820b` | T-A 自研极简多分类朴素贝叶斯分类器 | 上述的子任务 A（`src/core/content_classifier/` 纯 std 实现） |

→ 建议保留并补 Task Contract（`task.contract-bootstrap`），不属清理对象。

---

## 未决 / 后续

1. **治理包装**：`031355e` 提交时尚未开 GOV-FIX-04 卡（无 task_id 前缀 / verdict / contract），
   台账已按"触发卡 → commit"因果关联登记（`task_commit_entries` 144→153 + 顶层 `govfix04_20260911`）。
   是否补开卡走 Executor→Reviewer→Adjudicator 全循环，待裁决。
2. **② 的收尾动作**：A 组 14 张可立即 `cascade_close`；B 组 7 张需先决定（supersede 到后继卡 / 执行步骤 /
   `task.rollback` 置 reverted）；C 组 2 张保留。**未执行任何写入**，待授权。
3. **更大的遗留面**：全库无合同卡 1048 行（closed 794 已被 GOV-FIX-04 统一投影为 `completed`；
   非终态 254 张仍 `governance_blocked`/fail-closed）——属独立 backlog，未逐张核验。
