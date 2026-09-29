# 全矩阵新缺陷发现与修复记账（T-1790585462302-b48af3cc）

日期：2026-09-28 · 部署基线 b4f2baf → 修复提交 4377fac ·
证据：`C:\Users\wanpi\.callwarden\runtime\evidence\20260928-184221-4377fac207e6-4f82c03a.json`

## 1. 发现方法

全矩阵 243 工具只读重放（`outputs/matrix_replay_full.py` →
`outputs/matrix_replay_b4f2baf.json`）：从 `scripts/gen_route_matrix.py`
提取 tool→{rpc_method, backend, op, batch} 映射，READ_ONLY 注入权威
workspace_instance_id `4baea3ff12c2ea5c`，写类保持缺参 fail-closed 预期。
分类结果：OK 114 / ERR_PARAMS 120（写类缺参，预期）/ ERR_FAIL_CLOSED 4 /
ERR_OTHER 5 / **SLOW 0**（慢查询治理 T-1790567800125-68b64e1c 成果稳固）。

5 个 ERR_OTHER 中 3 个为参数问题（task.list / lease.list_events 缺
workspace_id；task.prompt.compile 拒绝 workspace_instance_id 字段），
**2 个为真实新缺陷**（基线无记录）。

## 2. 缺陷① get_gate_decision · NULL 反序列化必崩

| 项 | 内容 |
|---|---|
| 工具 | get_gate_decision（MCP-004，rust_native，transition） |
| 崩点 | `rust_ext/src/daemon/task_collab_governance.rs` `handle_get_gate_decision` L456-459 `r.get::<_, i64>(23)` |
| 现象 | `internal_error: 读取 task_gate_decisions 失败: Invalid column type Null at index: 23, name: workspace_id` |
| 真因 | `task_gate_decisions.workspace_id` schema 可空（db/schema.py:1327），live 库 **261 行全部 NULL**（gate 写路径不消费该列，db_task_gate.py 的 INSERT 不写）。⇒ 任何"命中行"的调用（含无参全表扫描）100% 崩；窄过滤返回空结果仅是绕过假象 |
| 未暴露原因 | 迁移测试 `tests/test_mcp_get_gate_decision_http_rpc.py` 的隔离 harness 中该表为空（文件注释自述"当前为空"），空表不触发 NULL 路径 |
| Python 对齐 | legacy 降级路径 `server/tools/tools_collab.py:582` `SELECT * + dict(r)` → sqlite3 NULL 变 None → JSON `null` |

**修复**：idx3 contract_revision / idx13 decision_time / idx17
role_contract_revision / idx23 workspace_id 四列全部改
`r.get::<_, Option<_>>`，`None → Value::Null`（不 coerced 0）。

**活验证（部署 4377fac 后真实库）**：

| 探针 | 修复前 | 修复后 |
|---|---|---|
| 无参全表扫描 | internal_error 崩 | ok，20 items（22KB），0.09s |
| limit=3 | 崩 | ok，3 items |
| 真实 task_id 过滤 | 命中行即崩 | ok |
| items 中 workspace_id | — | 全部 JSON `null`（对齐 Python None） |

## 3. 缺陷② hotspot_evolution · 全量返回超 RPC 消息上限

| 项 | 内容 |
|---|---|
| 工具 | hotspot_evolution（P0-COMPAT-v3，rust_native） |
| 崩点 | 返回值经协议帧回发时超 `DEFAULT_MAX_MESSAGE_BYTES = 8MB`（`rust_ext/src/daemon/protocol.rs:22`） |
| 现象 | `ProtocolError: 非法消息长度: 12395379`（1.7s 后） |
| 真因 | 无 module_filter 时 base_sql 无模块过滤，扫描全部 34939 个函数符号，每项 ~355B ⇒ 12.4MB；handler 已按 hotspot_score DESC 排序但未限流 |
| 迁移期根因 | Python `db/db_evolution.py:661` 无 limit（走本地连接不经 8MB 帧，故不崩），切片契约在 CLI 调用方（`cli/main.py:9198 [:5]`、`db_dashboard.py:442 [:top_n]`）。迁移到 RPC 后调用方切片契约在协议边界失效，handler 必须自限流 |

**修复**：新增 `limit` 参数，默认 100（对齐 complexity_hotspots 的 top-N
约定），负数 `invalid_params` fail-closed，0 返回空数组；在已有排序后
`results.truncate(limit)`。MCP 签名 `server/tools/tools_summary.py
hotspot_evolution` 同步加可选 `limit`（默认 100 透传 `_route`），降级
worker `_h_hotspot_evolution` 一致切片。

**活验证（部署 4377fac 后真实库）**：

| 探针 | 修复前 | 修复后 |
|---|---|---|
| 无 module_filter | 非法消息长度 12395379 崩 | ok，top 100（37KB < 8MB），1.39s，top=`dispatch.now_ts` score 0.4002 |
| limit=2 | — | ok，2 items（744B） |
| limit=0 | — | ok，`[]` |
| limit=-1 | — | invalid_params fail-closed（"limit 不能为负数"） |
| module_filter=snapshot_state | ok | ok（窄模块语义保持） |

## 4. 单测

`rust_ext` 新增两个 `#[cfg(test)]` 模块，`cargo test` 8 例全过：

- `gate_decision_null_tests`（3）：NULL workspace_id 行不崩且投影为
  JSON null / 无参全表扫描返回完整 items / role_contract_revision
  显式 NULL 投影为 null
- `hotspot_evolution_limit_tests`（5）：小结果集默认 limit 不截断 /
  limit=2 取 top-2 且分数序 / limit=0 空数组 / limit=-1 invalid_params /
  module_filter 下 limit 仍生效

## 5. 健康回归（部署后）

| 探针 | 结果 |
|---|---|
| query.stats | ok 0.00s |
| query.metrics_summary | ok 0.29s |
| query.complexity_hotspots | ok 0.03s（list[5]） |
| query.uncommented_symbols | ok 0.14s（list[20]） |
| project_brief | ok |
| get_gate_decision（全表） | ok 0.09s |
| hotspot_evolution（全量） | ok 1.39s（37KB） |

慢查询治理成果（uncommented_symbols 152~183s→0.13s、project_brief
26.4s→1.77s）与矩阵 SLOW=0 无回归。

## 6. 部署与二进制核验

- 提交 4377fac（3 文件，仅源码：task_collab_governance.rs /
  query_compat_handlers.rs / tools_summary.py）
- 部署 `scripts/refresh_shared_runtime.ps1 -TaskId
  T-1790585462302-b48af3cc -Configuration release` → 证据
  20260928-184221-4377fac207e6-4f82c03a.json，status=passed，
  daemon sha256 af719352...
- 二进制字符串核验（MEMORY 铁律）：`runtime/current/cw-daemon.exe`
  含本次新增错误串 "hotspot_evolution limit 不能为负数"（count=1）
- daemon 重启后重发 `snapshot.publish`（db_path=
  `~/.callwarden/callwarden.db`，snapshot_id 70300c139ce81466，
  293374 symbols / 273176 calls），query.* 恢复

## 7. 遗留/无关项

- ERR_PARAMS 120 为写类工具缺参的 fail-closed 预期，非缺陷。
- task.prompt.compile 拒绝 workspace_instance_id 字段、task.list /
  lease.list_events 需显式 workspace_id：为参数面差异，矩阵重放脚本
  侧的调用约定问题，不在本卡范围。

## 8. 修复后回归重放（部署 4377fac，outputs/matrix_replay_4377fac.json）

243 工具只读重放对照：

| 桶 | 修复前（b4f2baf） | 修复后（4377fac） | 变化 |
|---|---|---|---|
| OK | 114 | **116** | +2（get_gate_decision / hotspot_evolution 修复） |
| ERR_PARAMS | 120 | 120 | 不变（写类缺参 fail-closed 预期） |
| ERR_FAIL_CLOSED | 4 | 4 | 不变 |
| ERR_OTHER | 5 | **3** | −2（两真缺陷消除；余 3 为已知参数面差异） |
| SLOW | 0 | **0** | 慢查询治理成果稳固 |

残余 ERR_OTHER（3，均为参数面差异，非缺陷）：
- `task.list` / `lease.list_events`：需显式 `workspace_id`（数字 1），
  仅注入 `workspace_instance_id` 会被 `E_TASK_WORKSPACE_UNBOUND` 拒绝；
- `task.prompt.compile`：spec §4.1-3 拒绝 request 含
  `workspace_instance_id` 未知字段（fail-closed）。

⇒ 矩阵已清零真缺陷，两修复无回归。
