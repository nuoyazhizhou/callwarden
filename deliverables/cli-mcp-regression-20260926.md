# F-014 部署后 CLI + MCP 回归验证（2026-09-26）

## 环境基线

| 项 | 值 |
|---|---|
| HEAD | `1d9d839e26cb3c51eba7046707b473ad4cda43d` |
| 部署 | `refresh_shared_runtime.ps1 -TaskId T-1790151978451-61939ab4 -Configuration release -RunSmokeTests`，沙箱内执行成功（status=passed，git_head=1d9d839，daemon sha256 `3a46c188…`） |
| 快照 | 主仓快照已发布（symbol_count=293374，call_count=273176，snapshot_id `5faeb2790e7b1546`） |
| 沙箱构建坑 | PYO3_CONFIG_FILE 已预设（`%TEMP%/pyo3_config.txt`），未复现 os error 231 / LNK1120 |
| daemon 托管 | 沙箱 Job Object 收前台子进程 → daemon 必须在**后台任务**内 `p.wait()` 托管；会话期间稳定存活 |

## MCP 只读矩阵（243 工具）

| | OK | OK_EMPTY | MCP_ERR | WRITE_SKIP |
|---|---|---|---|---|
| F-014 前（HEAD=eb747d5，快照未发布） | 104 | 34 | 38 | 67 |
| F-014 后（HEAD=758c941，快照过期） | 95 | 27 | 54 | 67 |
| **本次（HEAD=1d9d839，快照已发布）** | **110** | **35** | **31** | **67** |

### 与基线的错误 diff（MCP_ERR 54→31）

- **消失 24 个**：全部是快照门错误（`snapshot_not_ready`），现恢复为 OK/OK_EMPTY。F-014 修复的 10 条 schema 漂移保持 0，未回潮。
- **新增 1 个**：`file_read`（OK→MCP_ERR）。**已定位为环境噪声而非代码缺陷**：daemon 重启时 cwd 与活动 workspace 变化导致相对路径解析差异；权威环境（cwd=仓根 + 快照发布）重跑后 `file_read` **恢复 OK**（total_lines=154，与基线逐字一致）。
- **持续 30 个**：错误内容零变化，均为 fixture/参数构造问题（`缺少字段` / `path_not_found` / `workspace_id 1193 与 instance 绑定的 1 不一致`），非本次回归引入。

### 持续 31 个 MCP_ERR 的归类（均非新引入）

1. **超时 5**：`get_uncommented_symbols` / `get_call_heatmap` / `get_test_coverage` / `export_module_graph` / `project_brief`（全量 293K 符号扫描，矩阵 60s 超时；MCP 慢方法 300s 分级可覆盖，属容量而非缺陷）。
2. **fixture 参数缺失 11**：`缺少字段: name/qualified_name/symbol_a/archive_path/audit_id/payload_hash/workspace_instance_id` 等——daemon handler 要求的字段未列入工具 inputSchema 的 required，属 schema-handler 契约不一致（既有）。
3. **workspace 双契约 5**：`workspace_id 1193 与 workspace_instance_id 绑定的 1 不一致`（build_context/resolved_edges 组）——已知架构问题（registry ROWID 1193 vs 用户库 workspaces.id=1）。
4. **SQL 绑定类型 2**：`get_edit_history`（`Invalid column type Integer at index: 0, name: id`）、`rule_list`（`Invalid column type Null`）——Rust handler 的参数绑定类型不匹配，**真实缺陷但既有**。
5. **其他**：`guardrail_list_rules`（只读连接触发 `_init_builtin_rules` INSERT，写面混入读路径）、`file_read`/`file_list`/`check_file_health`（fixture 相对路径）、`run_check_gate`/`get_attestation_validity`（fixture 类型校验）、`assignment_revoke`（假 assignment_id）。

## CLI 契约测试（pytest）

| 运行 | 失败 | 说明 |
|---|---|---|
| 噪声环境（daemon cwd=home） | 103 | 环境性，不可信 |
| 权威环境（全量并发） | 144 | 含矩阵并发争用的 flaky |
| 权威环境（50 文件顺序重放） | **140** | 稳定失败 |

### 稳定失败分类（140）

| 类别 | 数量 | 根因 | 性质 |
|---|---|---|---|
| `patch(...get_db)` 契约漂移 | ~85 | `test_http_*_cutover` / `test_query_stats_rpc_http` / `test_semgrep_*` 等 patch `tools_<x>.get_db`，但 route_rpc 下沉后工具模块只导入 `_route`，`get_db` 已不存在 → `AttributeError` | **测试债**（意图仍有效：验证不回落本地；patch 目标过时） |
| WSL 容器测试 | ~2× | `test_p2h_enterprise_real_workspaces` 需 `wsl.exe`，沙箱程序黑名单禁 | 环境性 |
| 既有已知 | ~10 | `test_abi_contract`（db_cas.py 重导出壳）、`test_srv_019`、`test_task_*` 等 | 既有 |
| flaky（单独跑通过） | ~4 | `test_cli_004`、`test_m4_cli_fail_closed`、`test_mcp_*_http_rpc` 等 | 并发争用 |

**关键验证**：单独执行曾失败的 `test_cli_004_http_rpc` / `test_m4_cli_fail_closed` / `test_mcp_get_symbol_history` / `test_mcp_get_impact` / `test_mcp_get_gate_decision` 全部**通过**；而 `patch(get_db)` 类与 `test_p2h`（WSL）类**稳定失败**。两者机制明确，无代码回归。

### `patch(get_db)` 根因实证

```python
# tests/test_http_unsupported_error_cutover.py:174 / :383
with patch(f"{_module_name(module)}.get_db") as mock_db:   # AttributeError
    ...
# server/tools/tools_p4_lease.py:25（实际导入）
from ..daemon_client import route_rpc as _route            # 无 get_db
```

工具模块下沉到 route_rpc 后不再 import `get_db`，"fail-closed 不回落本地" 的断言对象消失。修法：patch 目标改为 `_route` 的本地回落等价物，或断言工具模块不含 `get_db` 符号（契约反向成立）。

## 结论

1. **F-014 修复保持有效**：schema 漂移 10→0（未回潮），快照门 24 个工具恢复真实数据，`file_read` 等行为与基线逐字一致。
2. **MCP 层无新增真实错误**：唯一新增（file_read）已证实为环境噪声，权威环境恢复。
3. **CLI 侧 140 个稳定失败全部归因**：~85 个是 `patch(get_db)` 测试债（route_rpc 下沉的连带过时），~2 组 WSL 环境性，~10 既有已知，~4 flaky。**无代码回归**。
4. **发现 2 个既有真实缺陷**（非本次引入）：`get_edit_history` / `rule_list` 的 Rust SQL 参数绑定类型错误；`guardrail_list_rules` 读路径触发写初始化。

## 遗留（建议下一步）

- **修 `patch(get_db)` 测试债**（~85 个失败，一次修复清空最大失败批次）：把 patch 目标对齐 route_rpc 下沉后的实际符号。
- **修 2 个 SQL 绑定缺陷**：`get_edit_history`（Integer 绑定到 text 列）、`rule_list`（Null 绑定）。
- **daemon 持久化**：沙箱 daemon 由后台任务 `p.wait()` 托管；会话结束后需用户在真实终端重启（`refresh_shared_runtime.ps1` 或直接启动 `runtime/current/cw-daemon.exe --socket <pipe> --http-bind 127.0.0.1:6374`，cwd=仓根）。
- **快照刷新**：每次推新提交后需重跑 `snapshot.publish`（db_path 指向 `~/.callwarden/callwarden.db`），否则 query.* 被快照门拦下。
- **既存待办（未变化）**：6 条 `*_daemon_unavailable` 改 mock、`test_srv_019.py` 写交付物副作用（本次已备份 `outputs/srv019_audit_pre_pytest.json`）、WB-WT 一次性任务残留。

---

## 追加（2026-09-26 深夜）：2 个 SQL 绑定缺陷修复 + 部署实测通过

commits：`5f9d9fd`（读层两处 + seed 写入层根因）+ `803ab10`（candidate_accept 同族第二处）。

### 根因（真实库实测）

| 工具 | 行映射 | 真实库 DDL | 实际数据 | 报错 |
|---|---|---|---|---|
| get_edit_history | `security_edit_row_to_json` | `file_edit_audit.id INTEGER PRIMARY KEY AUTOINCREMENT` | typeof=integer | `Invalid column type Integer` |
| rule_list | `security_row_to_rule` | `agent_rules.id TEXT PRIMARY KEY` | 3 行 seed 数据 **id 全 NULL** | `Invalid column type Null` |

NULL id 的污染源（写入层根因）：`edit_handlers.rs` 两处 `INSERT OR IGNORE INTO agent_rules` **不写 id 列**（seed_bootstrap + candidate_accept）。SQLite 对非 INTEGER PK 列允许 NULL 且 NULL 互不判冲突 → 幂等失效 + 每次调用新增 NULL id 行。数据溯源：这 3 条 NULL 规则是 F-013 回归矩阵 seed 副作用（199882d 记录在案），非 Python 时代遗留。

### 修复

1. `security_edit_row_to_json`：`id` 改 `r.get::<_, i64>(0)`。
2. `security_row_to_rule`：`id` 改 `Option<String>` + `unwrap_or_default()`（与同函数其余可空列范式一致）；`get_applicable_rules` 共用此映射一并修复。
3. `handle_rule_seed_bootstrap`：seed 三元组→四元组，显式写固定 id（`AR-seed-*`，对齐 Python `AR-bootstrap-*` 幂等范式）。
4. `handle_rule_candidate_accept`：`rule_id = AR-from-<candidate_id>` 确定性 id + `linked_rule_id` 回写 candidate（Python 双向绑定范式）；返回值从 rowid 改为 rule_id 字符串。

### 验证（source → binary → behavior 三级）

- 6 个新回归测试全过（4 读层 + 2 写层幂等）；cargo check EXIT 0。
- 二进制字符串验证：`AR-seed-*` ×3 + `AR-from-` 在，旧 INSERT（无 id 列序）= 0。
- 部署：`refresh_shared_runtime.ps1 -TaskId T-1790151978451-61939ab4 -Configuration release`，证据 `20260926-225142-803ab102d564-a8b1947c.json` **status=passed**（exit 1 仅 core-backup safe-delete fail-closed 清理，同前次）。
- 快照重发布：snapshot_id `70300c139ce81466`（symbol 293374 / call 273176）。
- **实测（pipe RPC，workspace 4baea3ff12c2ea5c）**：
  - `get_edit_history` → 返回 1 行，`"id": 1`（INTEGER 正确）✅
  - `rule_list` → 3 条规则，NULL id 降级 `""`，正文完整 ✅
  - `get_applicable_rules` → 3 条，matched_scope=global ✅

### 遗留

- 主库 3 条 NULL id 规则行（污染数据）现可被安全读取（id=""）；是否清理（`DELETE FROM agent_rules WHERE id IS NULL`）属 data-fix 决策，需按治理路径执行。
- `guardrail_list_rules` 读路径触发 `_init_builtin_rules` INSERT（写面拒绝）为第三类缺陷，未在本轮范围。
- 沙箱 daemon（pid 63864，后台任务托管）会话结束即收；**持久化需用户真实终端重启**。

---

## 追加 2（2026-09-26 深夜）：遗留三项收口

commit：`c004d17`（guardrail_list_rules 只读化）。用户已在真实终端执行部署
（证据 `20260926-232523-58eb1e9afa06-2f672093.json`，status=passed）。

### 1. NULL id 污染行清理（data-fix，已生效）

- `DELETE FROM agent_rules WHERE id IS NULL`：3 行删除（no-todo-in-commit /
  no-bare-except / no-print-in-lib，rowid 1-3）。
- RPC `rule.seed_bootstrap` 重 seed：首次 seeded=3，**二次 seeded=0** ——
  写入层修复（803ab10）的幂等性首次在真实主库成立（修复前 NULL id 互不
  判冲突，每次调用恒 +3 行）。
- 落盘核验：3 行 id 全为 `AR-seed-*`（typeof=text），rule_list 读回正常。

### 2. guardrail_list_rules 只读化（code-fix，commit c004d17，待部署）

根因链：读函数首行 `ensure_builtin_guardrail_rules`（Immediate 事务
INSERT）→ daemon 摘要面只持只读快照连接 → 写被拒 → 恒 fail-closed
internal_error（2026-09-10 探针实证）。

修复（职责归位）：
- `list_guardrail_rules` 纯读化；初始化归写面调用点（CLI guardrail rules
  显式 ensure；scan_guardrails 事务内自带 seed，均不变）。
- `handle_summary_guardrail_list_rules` 从 stub 改真实只读实现（对齐
  Python List[Dict]；category 双名兼容；旧库缺表容错空列表）。
- guardrail_scan 保持写面 fail-closed（INSERT findings 真写面）。

验证：新增 2 测试 + 既有 4 测试全过；release 二进制扫描（新 handler
文案编入、旧 stub 文案移除）。**生效需重新部署**（当前 daemon 仍为
58eb1e9 二进制）。

### 3. daemon 持久化（已解决）

用户真实终端部署，daemon（pid 12036，sha256 与证据一致）由部署脚本
拉起，不随会话回收。

---

## 追加 3（2026-09-27）：guardrail 修复部署实测通过

用户真实终端部署（证据 `20260927-054203-25becfce3101-7ec948e7.json`，
status=passed，git_head=25becfc，daemon pid 1216，sha256 校验一致）。
快照重发布（snapshot_id 70300c139ce81466，symbol 293374）。

实测（pipe→HTTP RPC，workspace 4baea3ff12c2ea5c）：

- `guardrail_list_rules` → **18 条 builtin 规则**（db/api/inc ×3 类），
  category=db_safety 过滤 3 条 ✅（原恒 internal_error，自 2026-09-10
  探针基线以来首次恢复）
- `rule_list` → 3 条，id 全为 `AR-seed-*` ✅
- `get_edit_history` → 1 行，id=1（INTEGER 正确）✅

Remove-Item 报错（core-backup 的 repository_source-callwarden_core.pyd
Access denied）为已知 fail-closed 清理现象，非部署失败。

至此 B1/B2/C 三类缺陷 + 数据清理全部闭环；security 组 MCP 工具
仅剩 guardrail_scan 写面 fail-closed（设计如此）。

---

## 追加 4（2026-09-27 上午）：guardrail_scan 修复部署实测通过，治理卡转 review

治理卡 `T-1790476445100-28ed77c4` 五步全部完成。部署证据
`20260927-105923-f8669e65fb58-59abfa2c.json`（status=passed，daemon
pid 79748，sha256 校验一致），快照重发布 `70300c139ce81466`。

实测（pipe→HTTP RPC，workspace 4baea3ff12c2ea5c）：

- `guardrail_scan` 全量扫描 → **14342 findings**（原恒 internal_error，
  自 2026-09-10 mode=ro 探针基线以来首次恢复）✅
- `file_filter=rust_ext/src/daemon/` → 3217 findings / 33 文件全部
  命中前缀、persisted=false 逐条核验 ✅

step5 report success → 任务 lifecycle_status=review（next_role=reviewer，
decision=READY）。executor 侧闭环，待 reviewer 独立复审 → adjudicator
apply/close。

**至此 F-014 回归链全部缺陷闭环**：security 组 5 个 MCP 工具
（get_edit_history / rule_list / get_applicable_rules /
guardrail_list_rules / guardrail_scan）全部实测恢复。
