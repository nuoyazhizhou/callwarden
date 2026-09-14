# C-16 承接卡独立 Reviewer 盲审报告：T-1789365537146-bef4c2e4

- **结论**：**PASS**（`blind_first_pass`，`overall=pass`）
- **Reviewer 身份**：`reviewer-wb-c16-01` / `inst-rev-wb-c16-01` / `sess-rev-wb-c16-20260914`
  / `deepseek-v4.1-flash`，role=`reviewer`（**三重不同于执行者** `executor-wb-c16-01` /
  `inst-exec-wb-c16-01` / `sess-exec-wb-c16-20260914`）
- **Reviewer lease**：`L-b810e86703dbb212`（counter=1，acquire→release 全程本进程内，raw token 不落盘）
- **被审对象**：`head=5cfb158`（`[T-1789365537146-bef4c2e4] fix(rust_ext): assignment_show 走权威
  workspace resolver（C-16）`）
- **Snapshot**：`dfcac6f16b827a30`；**view_manifest_hash**：
  `3ea38b6d7ab60d7ed8d05d9c1662cdf3a63756da200f82f43164f8446019a8b8`
- **Task Contract**：`TC-T-1789365537146-bef4c2e4` r1
  `sha256:e6d41da96fdd13a4c13b0bdc78371471feaa0ed1c60b541bda13412934388ba2`
- **Role Contract（reviewer）**：`rcl-T-1789365537146-bef4c2e4-reviewer` r1
  `sha256:3f691024970453f94a17d7ddc7a5845db2eaf19f1d25af9b8c9000272a6f8863`
- **Verdict**：`V-4c428e7c30851e88a67a9e5f`（event_id 633）；findings 4 条（1 范围内披露 + 3 相邻）

> **盲审纪律**：本报告全部结论来自本人对**源码、提交对象、部署产物与权威 DB** 的直接复核，
> **不采信执行者报告叙述**。执行者证据文件仅作「被审claims清单」使用，每条均独立复算。

---

## 1. 独立复核项（R1–R10，逐项留痕）

| # | 复核项 | 方法 | 结论 |
|---|---|---|---|
| R1 | 提交对象 = 允许文件 | `git show --name-only 5cfb158` | 仅 2 文件：`rust_ext/src/daemon/task_collab_lease.rs`(+26/-4) + `tests/test_c16_assignment_show_workspace_authority.py`(+338) ✅ |
| R2 | 禁止路径未触碰 | 提交内 `server/`、`db/**`、`scripts/refresh_shared_runtime.ps1` 命中数 | 0 ✅ |
| R3 | 提交前缀 | `git log --oneline` | `[T-1789365537146-bef4c2e4]`（本卡 id，未复用 PYT/卡A id）✅ |
| R4 | 源码哈希自证 | 独立 `sha256` 计算 | `lease.rs`=`14E1D280F6874123…`（=披露 `14e1d280…`）；测试=`CF684C39CE3494D3…`（=披露 `cf684c39…`）✅ |
| R5 | handler 实际源码 | 逐行打印 1995–2049 | `.trim()` 于 2019；空判定+`invalid_params` 在 `self.conn.lock()` **之前**（2030）；`task_bound_workspace_id(&conn,&task_id,optional_workspace_id_param(params))?`（2040-2041）✅ |
| R6 | resolver 语义 | 逐行打印 `task_collab_shared.rs:455-507` | 无 binding→`E_TASK_WORKSPACE_UNBOUND`；显式不一致→`E_WORKSPACE_AUTHORITY_MISMATCH`；`optional_workspace_id_param` 筛除 `<=0`、兼容字符串数值 ✅ |
| R7 | diff 卫生 | `git diff --check HEAD~1..HEAD` | rc=0，无空白错误 ✅ |
| R8 | 部署门禁证据 | 读 `runtime/evidence/20260914-150702-5cfb1589f6df-e53fee93.json` | `status=passed` / `rollback=false` / `error=null` / `git_head=5cfb1589f6df…`（==HEAD）/ `daemon_runtime.sha256=87c6200…` / `ping_exit_code=0` ✅ |
| R9 | 二进制身份自证 | 独立 sha256 | `runtime/current`=`87C6200950A91713…`（**= 部署门禁记录值**，逐字一致）✅ |
| R10 | 只读性/工作树纪律 | `git ls-files deliverables/_insp_c16*` | 空（探针/隔离 target 均未入库）✅ |

## 2. 独立复跑（对着**部署产物**，非源码）

- **正向**：对已部署二进制 `87C6200950A91713F74B5F49E9239E685F5B8CB231D8FD9A446F523EEC4250FE`
  复跑 `tests/test_c16_assignment_show_workspace_authority.py`
  （`CW_DAEMON_BIN` 指向 `runtime/current`；harness 打印 `bin_sha256` 自证）→ **10 passed in 7.25s**。
- **反证（用例判别力）**：同套用例对**修复前基线** `BE67915CBC5D4641AE3FBC255AC160D21ADA8D791B163CB98B0A0ED19DD3DF92`
  （`runtime/previous-20260914-143641`）→ **7 failed / 3 passed**，失败签名统一为旧「静默
  `{"status":"none"}`」语义（`test_matrix_1/1b/2/3` + `test_matrix_4` 三个参数化）。证明本套用例
  **具备判别力**，非空转。

## 3. Findings

### F1【in-scope，披露后判为可接受】§3.4 范围偏差：`.trim()` 落在白名单外文件

- 现象：step2 `target_file` = `tests/test_c16_assignment_show_workspace_authority.py`，但该步内为
  修 `task_id="   "`（纯空白）未判空，在 `rust_ext/src/daemon/task_collab_lease.rs` 追加了 `.trim()`。
- Reviewer 判定：**可接受**。依据：①同 handler、同 check-item 边界补齐（「不得带着空 task_id 去查
  binding」对空白同样成立），非新增需求；②**无隐藏工作**——执行者在证据 §3.4 显式披露并给出前后哈希
  （`f43bca23…`→`14e1d280…`），step3 以最终哈希重新取证；③该文件在本卡 step1 的 `target_file` 内，
  属本卡整体 scope；④最终产物经本人独立复跑 10/10 验证。**不构成 BLOCKED。**
- 治理建议（不阻断）：后续同类卡可把「同 handler 边界补齐」预先纳入对应 step 白名单，减少事后披露。

### F2【adjacent，超本卡 scope】MCP-015 测试文件 4 例陈旧断言

- `tests/test_mcp_assignment_show_http_rpc.py` 以合成 task_id（`NO-SUCH-TASK` / `X` / `999999`）
  断言 `assignment_show` 无 active assignment 时返回 `{"status":"none"}`——编码的是**权威模型落地前**
  的「静默 none」语义。C-16 走权威 resolver 后，无 binding 的 task **必须** fail-closed，二者不可能同时成立。
- 独立佐证：`tests/_w3_harness.py::setup_w3_client`（`671-727`）只 seed `workspaces`（`700-704`），
  **不 seed `task_workspace_bindings`** → 该 harness 内 task 天然 unbound。兄弟 handler
  （`handle_lease_acquire`、`task.report`、`task.supersede`、`task.apply/close`）一律 `task_bound_workspace_id`。
- 处置：**不改**（属 MCP-015 `T-1788963088148-495d7208` 产物 + 全 W3 家族共享件，期望语义须由承接卡定界）。
  已登记 backlog §W19，建议承接卡 **C-18**。

### F3【adjacent，基建隐患】refresh 脚本脏树打戳

- `scripts/refresh_shared_runtime.ps1:537` 仅 `git rev-parse HEAD` 打戳，**不校验工作树脏否** →
  脏树构建的二进制被打上陈旧 HEAD 标签。本卡已在 §4.6 以「先提交、再重跑门禁」自愈（`git_head=5cfb158`）。
  属环境/基建缺陷，出 C-16 scope。

### F4【adjacent，基建隐患】`_w3_harness.find_daemon_binary()` 按 mtime 竞争 + MSYS 路径静默跳过

- `tests/_w3_harness.py:589-612`：`CW_DAEMON_BIN`（`:600`）只是候选中**按 mtime 竞争的一项**，
  且 `os.path.isfile()` 对 MSYS 风格路径（`/c/...`）在 Windows Python 下判 False → 候选被**静默跳过**，
  回落仓库内陈旧 debug 构建，可产生**假阳性通过**。
- 说明：**本卡新测试文件不受影响**——它自带严格优先级 `_find_daemon_binary()`（`tests/test_c16_…:64-78`，
  `CW_DAEMON_BIN` 命中即返回）。此隐患影响全 W3 家族其它用共享 harness 的测试，建议与 C-18 一并收敛。

## 4. 结论与交接

- root cause addressed（非掩盖）；forbidden paths untouched；回归证据在部署产物上可复现 → **PASS**。
- Handoff：`reviewer_pass`（`reviewer`→`adjudicator`，`independence_requirement=required`），
  `request_id=handoff-T-1789365537146-bef4c2e4-rev-01`，event_id 8890；
  新 assignment `A-f35ccd68bca45a0966b999ac`（role=adjudicator）。
- 派工投影复核：`workflow_status=adjudication_pending` / `action=ADJUDICATE` /
  `review.state=passed` / `review.verdict_id=V-4c428e7c30851e88a67a9e5f` / `findings_count=4`。
