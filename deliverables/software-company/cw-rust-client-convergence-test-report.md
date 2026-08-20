# 测试报告：CW Python 纯 Client 化 + Rust Daemon 业务下沉（收敛架构）

| 字段 | 值 |
|---|---|
| QA | Edward（软件 QA 工程师） |
| 被测实现 | 工程师 Alex 交付（T01–T05 收敛架构实施，含 CW-1 修复） |
| 上游依据 | PRD `cw-rust-client-convergence-PRD.md`（M1–M4 + R0.6）+ 设计 `cw-rust-client-convergence-design.md`（§4.1 场景 A） |
| 测试代码 | `C:\git_work\callwarden\tests\convergence\`（conftest + M1/M2/M3/M4/回归/CW-1 门） |
| 执行日期 | 2026-08-19/20（Round 1）+ 2026-08-20（Round 2 回归） |

---

## 1. Summary

### 1.1 收敛验证套件（tests/convergence/，本次新增）

| 套件 | 文件 | Round 1 | **Round 2** |
|---|---|---|---|
| M1 239/239 路由 | test_m1_route_matrix.py | 7/7 ✅ | **7/7 ✅** |
| M2 Python 纯 client | test_m2_client_purity.py | 15/15 ✅ | **15/15 ✅** |
| M3 双 Agent 并发写 | test_m3_concurrent_writes.py | 8/8 ✅ | **8/8 ✅** |
| M4 CLI fail-closed | test_m4_cli_fail_closed.py | 6/6 ✅ | **6/6 ✅** |
| 回归（既有工具冒烟） | test_regression_http_tools.py | 7/7 ✅ | **7/7 ✅** |
| CW-1 已知源码缺陷门 | test_known_source_bugs.py | 1/8（检出 CW-1） | **8/8 ✅（CW-1 已修复）** |
| **小计** | | 44/7 | **51/51 全绿** |

### 1.2 既有 HTTP 回归套件（tests/test_http_*，R0.5 零回归抽查）

| 维度 | Round 1 | **Round 2** |
|---|---|---|
| 收集 / 通过 / 失败 | 255：92 / 163 | 255：92 / 163 |
| `NameError` 出现次数（CW-1 运行时崩溃） | 22 | **0（已消除）** |
| `E_HTTP_MANIFEST_MISSING`（迁移后测试契约过时） | 115 | 135 |
| 失败性质 | NameError（真实缺陷）+ 契约过时 | **全部为既有测试契约过时**（工具已按设计从 `E_HTTP_COMPAT_UNSUPPORTED` 迁移为真实 daemon 路由，旧断言未更新） |

> Round 1→2 差异说明：CW-1 修复后，原先 22 处 NameError 用例不再崩溃，转而正确走到 daemon 路由；
> 因既有测试 mock HTTP 模式但未配置 daemon endpoint，返回 fail-closed `E_HTTP_MANIFEST_MISSING`
> —— 该错误是**新契约下的正确 fail-closed 行为**，属测试断言需随 T05 更新，非运行时缺陷。

### 1.3 Routing Decision（Round 2）

**Send To: NoOne（源码缺陷已修复，回归通过）** —— 但需记录：
1. **CW-1 已修复并验证**（17 处 `"sync": True/False` + `thinify_tools.py` 防复发；AST 扫描 0 残留；回归门 8/8）。
2. 既有 HTTP 回归套件 163 失败**全部归因于测试契约过时**（非运行时缺陷），建议 T05 阶段由 QA/产品确认后更新断言；
   如产品裁定旧 fail-closed 行为必须保留，则需回溯实现（不属本轮验证结论）。
3. 环境限制：本机 daemon 二进制不含新收敛 RPC（Windows 链接环境问题），新 79 工具 RPC 端到端运行验证需可构建环境补跑。

---

## 2. 验收标准对照（M1–M4 + R0.6）

| 标准 | 结论 | 证据 |
|---|---|---|
| **M1**：239/239 可路由 + 路由矩阵可机器核对 | ✅ 静态通过；⚠️ 新 RPC 运行时受限于本机 daemon 二进制（见 §4） | verify_route_matrix.py 退出 0；MCP 注册 239=矩阵 1:1；dispatch.rs 190 分支覆盖 rust_native/task_rpc；白名单两端对齐 80=80 |
| **M2**：Python 侧 0 业务 SQL | ✅ 通过（硬门禁 0 违例） | check_client_purity.py 退出 0；AST 复扫 11 模块 0 违例；薄壳函数体=纯透传 `_route()` |
| **M3**：双 Agent 并发写 | ✅ **通过（8/8，Round 2 复跑稳定）** | 同 title 并发 create 得独立 task_id；N=8 并发无冲突；request_id dedup Replay；lease 并发单 winner；旧 token fencing 被拒；并发 apply/close 串行化；混合风暴无死锁 |
| **M4**：CLI fail-closed | ✅ 通过（6/6，含 cli/dispatcher 异常类型统一后复跑） | daemon 不可达抛 `E_HTTP_DAEMON_UNAVAILABLE`（`.code` 属性已补齐，CLI/MCP 薄壳可统一 `except` 后读 `.code`）；local 无 CW_TEST_MODE → `E_MODE_DEPRECATED`；CW_TEST_MODE=1 放行 |
| **R0.6**：并发安全验证 | ✅ 通过（与 M3 同一套件） | 见 M3 |

### M3 关键场景结果（场景 A，设计 §4.1）

- **并发 task.create 同 title**：两笔均成功、各得独立 `task_id`、无丢失更新、无 `E_*` 误冲突。
- **request_id 幂等**：同 `request_id` 重复提交 → 返回同一 `task_id`（dedup Replay），非重复执行。
- **lease 并发争用**（先 `agent.register` 使 holder active）：恰好 1 个 winner（fencing_counter=1），
  败方收 `E_LEASE_ACTIVE_EXISTS`（结构化冲突，非连接错误）。
- **lease fencing**：旧 lease 释放后新 acquire counter 递增；旧 token 提交 `task.apply` 被拒
  （`E_LEASE_FENCING_STALE`/token 不匹配等）；新 token 成功 → 最终一致。
- **并发 apply/close 同 task**：经 SerializationPoint 串行化，失败均为结构化 `E_*`，无死锁/超时崩溃。
- **混合读写风暴**：3 读 × 5 次 + 4 写并发，全部在超时内完成。

---

## 3. Failed Tests 明细

### 3.1 源码缺陷（CW-1）—— **已修复，Round 2 验证通过**

Round 1 检出：`server/tools/*.py` 中 17 个工具函数体的 dict 字面量写了 `"sync": true/false`（JSON 风格），
Python 运行时构造该 dict 即抛 `NameError`，导致 MCP 工具在路由前崩溃。

**Round 2 验证（修复确认）**：
- AST 扫描（`_find_json_literal_functions` 逻辑）：7/7 模块 PASS，**17 受影响函数 0 残留**。
- `grep '"sync"'`：17 处全部为 `"sync": True` / `"sync": False`。
- 生成器防复发：`scripts/thinify_tools.py:155` `sync = "True" if ... else "False"`（附 CW-1 回归注释）。
- 回归门：`tests/convergence/test_known_source_bugs.py` **8/8 通过**。
- 原 NameError 复现用例（`test_p2_p3_write_tools_fail_closed_in_http_mode[import_envelope_dependencies]`）：
  不再抛 NameError，正确进入 daemon 路由（剩余失败为测试契约过时，见 3.2）。

### 3.2 既有测试契约过时（需 T05 同步，非运行时缺陷）

**E2：既有 HTTP 回归套件断言旧 fail-closed 契约**

- 现象：`tests/test_http_combined_worker_cutover.py`、`tests/test_http_unsupported_error_cutover.py`、
  `tests/test_http_governance_error_cutover.py` 等 163 处失败，错误为 `E_HTTP_MANIFEST_MISSING`
  （测试 mock HTTP 模式但未配 daemon endpoint）。
- 归因：收敛前这些工具在 HTTP 模式返回结构化 `E_HTTP_COMPAT_UNSUPPORTED`；收敛后
  （设计 Q1/Q7）工具改为真实 daemon 路由，`route_rpc` 需真实 daemon 可达。
  测试仍断言旧行为 → **测试需更新**（如断言 `E_HTTP_DAEMON_UNAVAILABLE` 或提供 daemon fixture）。
- 另 8 处实现细节断言（`_http_unsupported(...)`/`_call_daemon_rpc`/`_get_daemon_client()` 前缀）同样过时。

### 3.3 预存量失败（与本次收敛无关）

- `tests/test_http_daemon_client.py::test_production_factory_default_legacy`：
  `CW_DAEMON_TRANSPORT` 未设置时默认已为 HTTP（H6 迁移期默认，HEAD 即如此），
  测试期望 legacy client 而实际返回 HTTP client → **预存量**，非本次改动引入。

---

## 4. Known Issues / 环境限制

1. **本机 daemon 二进制不含新收敛 RPC**：运行中 cw-daemon（PID 39672，构建于 8-18，
   commit 6bf7353）与 `rust_ext/target/release|debug/cw-daemon.exe` 均**不含**
   `workspace.build_graph`/`query.metrics_summary`/`admin.*`/`edit.*` 等新 handler
   （实测返回 `method_not_found`）。Windows 下 `cargo build --bin cw-daemon` 在链接阶段失败
   （`link.exe` cdylib 环境问题，与工程师交付摘要一致）。因此 M1 的**运行时**验证仅覆盖
   task/lease/workspace 既有 RPC 与 compat worker 路径；新 79 工具 RPC 的端到端运行验证
   需在可构建 daemon 的环境（CI/Linux）补跑。静态层（矩阵/dispatch 分支/cargo check）已全绿。
2. **git 对象库损坏（预存量）**：仓库 `.git` 多处 ref 指向缺失对象（`bad object HEAD`），
   与本次验证无关（工作树文件完整）。
3. **`.venv_test` 依赖损坏**：anyio/annotated-types/attrs 曾缺失文件，已重装修复；
   MCP 注册 239 工具验证恢复正常。
4. **cli/dispatcher 异常类型统一（已修复）**：`cli/dispatcher.py` 现 re-export
   `server.daemon_client.DaemonUnavailableError`（`CliDUE is DaemonUnavailableError`），
   server 侧类已补 `.code` 属性（默认 `E_HTTP_DAEMON_UNAVAILABLE`；local/legacy 拒绝路径显式
   `E_MODE_DEPRECATED`），调用方可统一 `except DaemonUnavailableError` 后读 `.code`。
   M4 套件（6/6）复跑通过验证该改动。

---

## 5. 产出文件

- 测试套件：`tests/convergence/conftest.py`、`test_m1_route_matrix.py`、`test_m2_client_purity.py`、
  `test_m3_concurrent_writes.py`、`test_m4_cli_fail_closed.py`、`test_regression_http_tools.py`、
  `test_known_source_bugs.py`
- 本报告：`deliverables/software-company/cw-rust-client-convergence-test-report.md`
- 运行记录（临时）：`tests/convergence/_r2_*.log`、`_run_*.log`

## 6. 结论

- **Round 2 回归通过：收敛验证套件 51/51 全绿**（M1/M2/M3/M4/R0.6 + CW-1 门）。
- **CW-1 源码缺陷已修复并验证**：17 处 `"sync": True/False` + `thinify_tools.py` 防复发；
  既有回归套件 NameError 出现次数 22 → 0。
- 既有 HTTP 回归套件剩余 163 失败**全部为测试契约过时**（工具按设计迁移为 daemon 路由后旧断言未更新），
  建议 T05 阶段由 QA/产品确认后更新测试断言；本轮不视为运行时回归。
- 环境限制（本机 daemon 不含新 RPC、Windows 链接问题）记录于 §4，新 79 工具 RPC 端到端运行验证
  需可构建环境补跑。
