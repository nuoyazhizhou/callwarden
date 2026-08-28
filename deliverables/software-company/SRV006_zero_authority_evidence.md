# SRV-006 零权威证据 manifest（server daemon client Python authority → Rust daemon）

- **任务**: `T-1787323460703-c5f65380`（SRV-006，port_type=control_plane，route B thin-client）
- **Task Contract**: rev2 `sha256:a69e5fc728807a87d5e79548f5867b246cf8699e86faff153d615a386a8a2aa1`
- **Role Contract**: executor r1 `sha256:0978ddebf9efd8b197f2d67a052bf51743e26fe01aadfe936f4cadd00a0fd6a4`
- **Executor**: `executor-workbuddy-v1-cur` / session `cw-exec-workbuddy-20260824` / model `workbuddy`
- **退役目标**: `server/daemon_client.py` 12 个本地权威符号
  `_get_db` / `_inject_workspace_id` / 8 个 `_sql_fallback_*` / `call_with_fd` / `publish_snapshot`
  → Rust handler `rust_ext/src/daemon/daemon_client_handlers.rs`
  （`handle_get_db` / `handle_inject_workspace_id` / `handle_sql_fallback_*` ×8 /
  `handle_call_with_fd` / `handle_publish_snapshot`）

## 1. AST / 静态扫描 before / after

**before**（`beb8ba9:server/daemon_client.py`，step1 前基线）：

| 符号 | 静态命中（剥 docstring 后函数体） |
|---|---|
| 模块级 | L22 `import sqlite3` |
| `_get_db` | `get_db(`（本地 CodeGraphDB 打开） |
| `_inject_workspace_id` | `get_db(` + `_get_active_workspace_id` + `_mcp_common`（本地推导 workspace） |
| `publish_snapshot` | `sqlite3` + `PRAGMA ` + `checkpoint(`（本地 WAL checkpoint） |
| 8 个 `_sql_fallback_*` | 各含 `get_db(`（经 _get_db 直调业务 SQL） |
| `call_with_fd` | 零命中（本身为 transport，但 FD 能力判定语义随探测化下沉） |

**after**（当前 worktree，HEAD=`33806db`）：

| 符号 | 现状 |
|---|---|
| `_get_db` | **已整体移除**（不存在于模块） |
| `_inject_workspace_id` | L3289 `client.call("mcp.daemon_client.inject_workspace_id", {"params": ...})`，纯薄客户端 |
| 8 个 `_sql_fallback_*` | L1762-1815 `self._rpc.call("mcp.daemon_client.sql_fallback_*", ...)` + 返回键提取，纯薄客户端 |
| `call_with_fd` | L823 `self.call("mcp.daemon_client.call_with_fd", ...)` 能力探测（返回 supported/transport 元数据） |
| `publish_snapshot` | L864 两步 RPC：`mcp.daemon_client.publish_snapshot`（daemon 权威 checkpoint）→ `snapshot.publish`（传输统一 db_path） |

after 扫描结果：模块无 `sqlite3` 导入；11 个退役符号函数体（剥 docstring）零命中
`sqlite3` / `get_db(` / `_get_active_workspace_id` / `_mcp_common` / `PRAGMA ` /
`SELECT ` / `checkpoint(`（`tests/test_srv_006.py::test_no_sqlite_authority_in_source`
门禁持续看守）。满足 check items `no SQLite / no get_db / no business fallback`。

**残留本地站点说明（非退役目标）**：
- `_transport_call_with_fd`（L837 起）：SCM_RIGHTS 物理 FD 发送——客户端持有
  fd 与自己的活 socket，无法委托 daemon（transport bootstrap，同 SRV-005 先例）。
  transport 适配职责，非 DB/业务 authority，不 open SQLite、不执行业务 SQL。

## 2. Runtime 指纹

| 验证 | 结果 |
|---|---|
| `cargo test --lib daemon_client_handlers` | **20 passed**（参数校验 invalid_params、workspace_id=0 拒绝、active workspace 解析、callers/callees/chain_down BFS 等） |
| `cargo test --lib daemon::dispatch` | **61 passed**（dispatch 回归无破坏） |
| `pytest tests/test_srv_006.py` | **18 passed**（success 7 / invalid 4 / authority 3 / unavailable 2 / restart 1 / AST 门禁 1） |
| `pytest tests/test_srv_005.py` 串扰回归 | **12 passed**（零串扰，合计 30） |
| `pytest tests/test_query_symbol_rpc.py` + `tests/test_legacy_query_baseline.py` | 除 3 个 HEAD 基线对照确认的存量失败外全过（2 个旧契约测试已改写为 fail-closed 断言，见 finding-8） |
| CLI HTTP RPC 9 文件批次 | 与 HEAD 基线失败清单完全一致（12 个存量，零新增回归） |
| import 冒烟 | OK（模块移除 sqlite3 导入后无循环依赖） |

**Commits**：

| step | commit | 内容 |
|---|---|---|
| 0 port_rust_authority | `beb8ba9` | daemon_client_handlers.rs 新建（12 handler）+ dispatch 12 分支 + capability 注册 + 配套（逐 hunk 白名单隔离） |
| 1 retire_python_authority | `15d987d` | 12 符号 RPC 薄客户端化（3 文件 +129/-113，剔除外部 task_claim_recover hunk） |
| 2 fixture_negative_matrix | `33806db` | tests/test_srv_006.py（18 passed） |
| 3 zero_authority_evidence | 本文件 | 证据 manifest |

## 3. Capability 证据（`http_server.rs`）

12 个方法全注册为：backend=`rust_native`、status=`available`、scope=`authority`、
route=`/v1/rpc`、owner=`T-1787323460703-c5f65380#SRV-006`，各带 ok/err fixture id；
operation_class 为 `read_only`，仅 `publish_snapshot` 为 `write`：

`mcp.daemon_client.get_db` / `inject_workspace_id` / `sql_fallback_get_callers` /
`sql_fallback_get_callees` / `sql_fallback_search_symbols` / `sql_fallback_get_symbol` /
`sql_fallback_get_stats` / `sql_fallback_get_topological_order` /
`sql_fallback_get_call_chain_down` / `sql_fallback_detect_cycles` /
`call_with_fd` / `publish_snapshot`

除 `publish_snapshot`（write，checkpoint + 发布 payload）外均为只读权威查询；
dispatch 12 分支接线位于收敛 RPC catch-all 之前（dispatch.rs L2543-2578）。

## 4. Handoff manifest

| 项 | 值 |
|---|---|
| step0 request_id | `req-srv006-step0-report-20260826-02` |
| step1 request_id | `req-srv006-step1-report-20260826-01` |
| step2 request_id | `req-srv006-step2-report-20260826-01` |
| step3 request_id | `req-srv006-step3-report-20260826-01`（目录型 target，changes 省略） |
| step_id | S-1787323460704-c60a694c（0）/ S-1787323460705-c60bb61c（1）/ S-1787323460705-c60c9d5c（2）/ S-1787323460705-c60df2c4（3） |

## 5. Findings

1. **finding-1（白名单外配套）**：`rust_ext/src/daemon/mod.rs` 模块声明不在
   allowed_paths 内，为新 handler 编译必需配套（与 SRV-004/005 同模式）。
2. **finding-2（忠实复刻语义）**：get_callers 短名分支无 workspace 过滤，与
   Python 原实现逐字对齐（忠实复刻，非新缺陷）。
3. **finding-3（Rust 修正潜在 bug）**：`chain_down` Rust 权威实现返回真实
   edges；Python fallback 恒返回 `[]` 为潜在 bug，下沉后行为修正（薄客户端
   保留 dict→list 归一化以兼容原签名）。
4. **finding-4（富注入默认值）**：`get_symbol` Rust 实现对缺失字段注入空默认值
   （富对象语义），与原 Python 返回对齐。
5. **finding-5（存量失败归属外部）**：route_matrix / task_supersede 等 10 个
   存量失败与本卡无关（HEAD 基线对照确认）。
6. **finding-6（共享工作树污染隔离）**：工作树存在外部 agent 的
   `task_claim_recover` +26 行 hunk（daemon_client.py），step1 提交经逐 hunk
   白名单过滤剔除，未随本卡提交。
7. **finding-7（环境）**：`http_server.rs` 工作树格式与 HEAD 存在无关空白差异
   （外部 agent 格式化），step0 补丁按 HEAD 格式接线。
8. **finding-8（白名单外测试配套）**：`tests/test_query_symbol_rpc.py` 与
   `tests/test_legacy_query_baseline.py` 各 1 个旧契约测试（断言 local 模式
   SQL fallback）随契约变更改写为 fail-closed 断言——白名单外测试配套。
9. **finding-9（契约收紧）**：`get_symbol_location` / `get_file_symbols` /
   `get_symbol_issues` 的 local 模式本地 SQL 分支随 `_get_db` 一并退役为
   fail-closed（M2.2/M2.4 先例被本卡推翻）。
10. **finding-10（transport bootstrap 保留）**：`_transport_call_with_fd`
    保留 SCM_RIGHTS 物理 FD 发送（客户端必须自持活 socket 与 fd，无法委托
    daemon）；`publish_snapshot` 传输统一 db_path 形式，FD 传输路径退役。
11. **finding-11（环境性测试失败）**：phase4 真实 daemon 测试失败为旧二进制
    部署时差（未含新 RPC）+ 测试 DB 缺 workspace_id 列，非本卡回归。
12. **finding-12（治理参数）**：`task.report` identity.role 必须为 `executor`
    （`implementer` 报 E_CONTRACT_ROLE_MISMATCH）。
