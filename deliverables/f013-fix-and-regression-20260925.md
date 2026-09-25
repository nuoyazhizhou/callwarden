# F-013 修复 + 全量回归（2026-09-25）

## 修复内容（commit eb747d5，已 push，快进 a4ff9e5..eb747d5）

### F-013（P0，上版事故真因）：workspace.activate/remove id-or-name 参数路由断裂

**现象**：MCP 工具 `set_active_workspace` / `delete_workspace` 与 CLI 只传
`workspace_id_or_name`，而 Rust handler（`handle_workspace_activate` /
`handle_workspace_remove`）只 `require_str_param("workspace_instance_id")`。
route_rpc 未解析时，workspace 权威注入把**当前活动 workspace 的 instance 顶替**
上去 → `delete_workspace("1711")` 静默归档主仓 1193（instance
`4baea3ff12c2ea5c`）而非目标隔离 ws。

**修复**（`server/daemon_client.py`）：route_rpc 在权威注入之前对
`workspace.activate` / `workspace.remove` 显式解析 id-or-name →
`workspace_instance_id`：
- 纯数字 → 匹配 `daemon_workspaces.workspace_id`；
- 非数字 → 匹配 `os.path.basename(client_view_root)`（与 list_workspaces
  兼容映射一致，registry 无 name 列）；
- 解析失败 → **fail-closed** `DaemonRemoteError(workspace_not_found)`，
  绝不回退活动 ws，且**不下发任何 mutation**；
- 解析后移除 daemon 不认的兼容键 `workspace_id_or_name`。

CLI 现有 `workspace_id_or_name` 调用签名保持不变（route_rpc 内部解析，
test_cli_072 契约不破）。

### None 数组归一（`server/tools/tools_task.py`）

`task_create` / `task_create_subtask` / `task_report_step` / `task_split`
的 `steps`/`changes`/`subtasks` 默认 None → daemon 报 "必须是 JSON array"。
MCP 层统一归一 `None → []`。

### build_graph 定向（`server/tools/tools_workspace.py`）

`build_graph()` 原本无参 → route_rpc 注入活动 ws（主仓 1145 文件级扫描，
隔离 ws 场景数百秒超时）。新增可选 `workspace_instance_id` 参数定向构建。

### 测试脚本修复（`.workbuddy/scripts/mcp_write_tools_test.py`，不入库）

- task 域 fixture **绝不指向真实任务**（FIXTURE_TASK）→ 改为在主仓 ws 绑定
  （1193/`4baea3ff12c2ea5c`，task 库唯一有绑定的 ws）下创建 WB-WT 前缀
  一次性任务；隔离 ws 在 task 库无绑定（`E_WORKSPACE_AUTHORITY_MISMATCH`）；
- `set_active_workspace` / `delete_workspace` 移到 Phase 3 末尾执行 +
  F-013 回归断言（返回行 workspace_id 必须是隔离 ws，不得是 1193）；
- ws 标识注入只作用于 WS_DOMAIN 工具；
- 修 detail 200 字符截断导致 task_id 解析失败的 bug；
- **cw.py 事故三层防护**（见下文"环境/流程问题"第 1 条）：
  ① Phase 2 全程活动 ws = 隔离 ws（daemon 回退注入天然命中隔离 ws）；
  ② 路径改写无条件生效（不再依赖 schema 是否含 instance 字段）；
  ③ post-check 残留受保护路径（`PROTECTED_PATHS`）一律 SKIP_NOFIXTURE。

## 回归结果

### 1. 单元测试（新增，9/9 通过）

`tests/test_f013_workspace_id_or_name.py`：数字/名称解析、未命中
fail-closed（无 mutation 下发）、显式 instance 短路、空注册表兜底、
CLI 契约（test_cli_072 4/4 保持）。

### 2. MCP 写工具补测（matrix: `outputs/mcp_write_tools_matrix.json`）

| 指标 | 上版（F-013 修复前） | 本版 | 变化 |
|---|---|---|---|
| total | 82 | 82 | — |
| OK | 12 | **31** | +19 |
| MCP_ERR | 53 | **30** | **−23**（F-013 级联消除） |
| SKIP_NOTOOL | 15 | 15 | — |
| SKIP_NOFIXTURE | 2 | 2 | — |
| SKIP_COVERED/DEFERRED | 0 | 2/2 | 新分类 |

**F-013 live 验证（Phase 3）**：
- `set_active_workspace("1711")` → 返回 `workspace_id: 1711` ✓
- `delete_workspace("1711")` → 返回 `workspace_id: 1711` ✓
- **主仓 1193 全程保持 active，未被归档** ✓（上版事故未复发）

**build_graph 定向 live 验证**：0.2–1.7s / scanned=2（隔离 ws 两文件），
对比上版注入主仓 1145 文件 / 130s+ 超时。

**剩余 30 条 MCP_ERR 全部为 fixture/环境/既有设计限制，无新缺陷**：
- 缺少必填字段（fixture 未覆盖的 required prop，如 `rule`/`new_content`/
  `qualified_name`/`provider_task_id`/`contract_hash`/`revocation_mode`）：约 13
- lease/identity 缺失（`E_LEASE_REQUIRED` / `E_IDENTITY_INCOMPLETE`）：8
- `snapshot_not_ready: workspace 4baea3ff12c2ea5c 未发布 snapshot`：3
  （主仓 snapshot 未发布——**既有 daemon 环境状态问题，非本次改动引入**）
- workspace 双契约（`task_create_from_plan`/`task_create_subtask` 注入
  `ws-10` 合成实例与 task 绑定 workspace=1 不一致）：2
- `*_not_found`（candidate/edit/job/assignment 不存在）：4

**关键**：上版 53 条 MCP_ERR 中约 40 条的 `workspace_archived` 级联已**归零**，
"必须是 JSON array" 类错误**归零**。

### 3. CLI 契约回归（96 文件，2026-09-25 21:20 复核）

工作树跑 `tests/test_cli_*.py`：**22 条 FAILED**（两环境**完全相同**的失败集合）。
本轮做了决定性对照实验，**更正了先前"pyd 导致 i18n drift"的归因**：

- 在 commit `eb747d5` 的 worktree（**无未跟踪 pyd**）跑同一批 →
  **同样 22 条失败，与主仓集合零差异**（`comm -12` 交集=22，差集=0/0）；
- 结论：中文输出（`任务总数: 2` / `代码图谱状态` / `已生效规则 (0)`）
  来自 **Python tracked 代码的 i18n 层**，不是 pyd。失败是**测试断言
  未跟随 i18n 迁移**（test drift），非代码缺陷，也非本次改动引入——
  本 commit 仅改 4 文件（`git diff --stat 7179b8c eb747d5`），
  `cli/main.py` 与 `i18n/` 零改动。

22 条失败的分类（三类，全部非新缺陷）：

| 类别 | 条数 | 代表 | 根因 |
|---|---|---|---|
| 断言英文输出 vs 中文 i18n | 15 | `test_cli092` 期望 `"Total tasks: 2"` 实得 `任务总数: 2`；`test_cli063` 期望 `"up to date"` 实得中文状态 | test drift（断言未跟随 i18n） |
| daemon 不可用类断言失效 | 6 | `test_http_rpc_ping_daemon_unavailable` 期望 `E_HTTP_DAEMON_UNAVAILABLE`，实得 `HTTP status=502 (url=...:9)` | **daemon 现在活着**，这些"不可用"用例的模拟前提不再成立（环境性） |
| worker 状态断言 | 1 | `test_http_rpc_health_success` 期望 `worker_status == 'healthy'`，实得 `'unhealthy'` | daemon 侧 worker 心跳过期（环境性，非路由缺陷） |

**处置建议**：15 条 i18n drift 应更新断言为中文（或改用 key 断言）；
6 条 unavailable 类应改用 mock 断开而非依赖真实不可达；
worker_status 1 条待 daemon 心跳恢复后复核。均不阻塞本次 F-013 交付。

### 3b. MCP 只读矩阵复核（243 工具，2026-09-25 21:25 重跑，daemon 在线）

旧矩阵跑在 daemon 不可用时期（含 `E_HTTP_DAEMON_UNAVAILABLE` 超时），
数据已过期；本轮 daemon ping ok（pid 41504）后重跑：

| 指标 | 旧矩阵（daemon 不可用） | 本轮（daemon 在线） |
|---|---|---|
| OK | 108 | **104** |
| OK_EMPTY | 37 | **34** |
| MCP_ERR | 31 | **38** |
| WRITE_SKIP | 67 | **67** |

38 条 MCP_ERR 分类（无新代码缺陷）：

| 类别 | 条数 | 工具 | 根因 |
|---|---|---|---|
| fixture 缺必填字段 | 13 | `assignment_create`、`diff_callers`、`run_check_gate` 等 | fixture 无法构造（缺真实 lease/字段） |
| **schema 漂移（部署二进制过期）** | 9 | `get_symbol_location`、`get_file_symbols`、`get_symbol_history`、`get_file_history`、`get_recent_changes`、`get_issue_summary`、`find_issues`、`get_semgrep_*`、`get_uncommented_symbols` | 报 `no such column: s.symbol_hash / fv.version_num / sc.content`、`no such table: semgrep_findings`——**源码侧已修**（`batch_build_query.rs:244` 写 symbol_hash、`batch_file_versions_query.rs:224` 读写 version_num），部署的 daemon 二进制是旧构建 |
| workspace 双契约（F-005 系列） | 5 | `list_build_contexts`、`get_build_context` 等 | `workspace_id 1193 与 instance 绑定的 1 不一致`（既有） |
| HTTP 超时 | 2 | `project_brief`、`repo_map` | 慢方法在快路径上 30s 超时（已分级但部署未含？） |
| fixture 目标不存在 | 2 | `assignment_revoke`、`check_file_health` | fixture 目标不存在 |
| 其它设计限制 | 7 | `guardrail_list_rules`（write-face 拒只读连接）、`file_list`（path_escape）、`export_module_graph`（格式空）、`get_edit_history`/`rule_list`（列类型）、`append_evidence`（缺 payload_hash） | 设计/fixture |

**结论**：`get_complexity_hotspots` / `get_largest_functions` 的
`Wrong number of parameters` 报错在本轮已消失——源码修复（占位符 ?2/?3
对齐，`metrics_handlers.rs:135-144` / `298-305`）**已编入当前源码**，
但部署版 daemon 仍是旧二进制。9 条 schema 漂移同样是"源码已修、部署未更新"。
**建议**：重新构建并部署 daemon（`refresh_shared_runtime.ps1`）后重跑矩阵。

### 4. CLI live 回归（F-013 路径）

- `cw workspace register cw-f013-cli-test /tmp/cw_f013_cli` → ID=1712 ✓
- `cw workspace set 1712` / `set 1193` → 正常切换 ✓
- `cw workspace delete 1712` → daemon 注册表 **1712=archived，
  1193=active 未动** ✓（直接查 `workspace.list` RPC 确认）
- `cw task create --title ... --workspace-id 1193` → 任务创建成功，
  含 workspace_binding_id / workspace_capture_id ✓

## 本次发现的环境/流程问题（非代码缺陷）

1. **`cw.py` 被自己的补测脚本删除并覆写**（已取证闭合，**更正先前
   "外部规则注入"的误判**）：工作目录只有本会话操作，无外部 hook 参与。
   完整事故链：
   - `mcp_tools_test.fixture_for` 对 path/file 类参数固定返回 `"cw.py"`；
   - `remove_file` / `rule_insert_agents_md_block` 的 MCP schema 不含
     `workspace_instance_id`，上版 `build_safe_args` 的路径改写
     （`cw.py` → `alpha.py`）只在 `got_instance=True`（schema 含 instance
     字段）时生效，故这两个工具的 `file_path`/`target_path` 保持 `"cw.py"`；
   - route_rpc 的 `_inject_workspace_id`（`server/daemon_client.py:3686`）
     对无 ws 标识、非 task-scoped 的参数注入**活动 workspace 的 instance**
     = 主仓 `4baea3ff12c2ea5c`；
   - daemon 在主仓真实路径执行：字母序 `remove_file` 先跑
     （`fs_handlers.rs:971` `std::fs::remove_file` 物理删除 cw.py），
     随后 `rule_insert_agents_md_block`（`edit_handlers.rs:450`）
     `read_to_string().unwrap_or_default()` 读到空串 → 写出 60 字节
     marker 存根。
   - **决定性证据**：矩阵记录 `rule_insert_agents_md_block ... 
     {"target_path":"cw.py"} → {"bytes_written":60}`，与存根字节数逐字吻合；
     WorkBuddy file-history `c17f65b55cbe8194@v3` = 60 字节 @12:48:41、
     `@v4` = 5903 字节 @13:10:59（`git checkout` 恢复）；
     `.file-rollback.ndjson` 无 cw.py 命中（不是 Write/Edit 工具写的）。
   - 处置：`git checkout HEAD -- cw.py` 恢复（154 行，`callwarden 0.3.23`
     验证通过）；主仓 1193 全程 active 未动（本轮 F-013 live 验证已证明）。
   - **脚本已修（三层防护）**：① 根因层——Phase 2 全程把活动 ws 切到
     隔离 ws，daemon 回退注入天然命中隔离 ws；② 脚本层——路径改写不再
     依赖 `got_instance`，无条件 `cw.py → alpha.py`；③ 闸门层——
     post-check 残留受保护路径一律 SKIP，fail-closed。
2. **主仓 snapshot 未发布**：`snapshot_not_ready: workspace
   4baea3ff12c2ea5c 未发布 snapshot` 影响 defect_learn/guardrail_scan/
   merge_preview 三个工具。既有 daemon 状态问题。
3. **部署版 pyd 与测试断言漂移**：15 条 CLI 契约测试断言英文输出，
   部署的 Rust core 输出中文。建议同步断言或 pyd 入库。
4. **主仓 DB 级污染（本轮补测副作用，文件层面仅 cw.py 受损且已恢复）**：
   `remove_file` 在主仓留 destructive_operations 审计行 + file_instances
   删除；`rule_seed_bootstrap(dry_run:false)` seeded 3 条规则
   （no-bare-except / no-print-in-lib / no-todo-in-commit）到主仓
   agent_rules；`propose_edit` / `record_task_symbol_change` /
   `build_directory` / `import_coverage` / `refresh_file` 在主仓留
   DB 痕迹。均为可清理的 DB 记录，不破坏源文件。

## 测试残留（WB-WT 前缀一次性任务，未自动清理）

按治理约定（supersede/cascade_close 需双独立 agent）未擅自动作：

- `T-1790310735710-ef9e0378`（探针）
- `T-1790311264932-27a97c90`（第 2 轮，被 FATAL bug 中断后遗留）
- `T-1790311715189-fd136d4c`（第 3 轮 fixture，task 域工具目标）
- `T-1790313417497-568c651c`（CLI 冒烟）

隔离 ws 残留：1711（archived，可忽略）、1712（archived，本次 CLI 测试）。

---

## F-014 修复（2026-09-25，只读路径 schema 缺口）

### 根因

MCP 只读矩阵 243 工具重跑（daemon 在线）后，38 条 MCP_ERR 中有 **9 条
schema 漂移**：`no such column: s.symbol_hash` / `no such column:
fv.version_num` / `no such column: sc.content` / `no such table:
semgrep_findings`。先前的归因是"部署版 daemon 二进制过期"，**该归因错误**：
Python 扫部署版 `cw-daemon.exe` 二进制，`version_num`(20)/`symbol_hash`(172)/
`semgrep_findings`(39) 字符串全在，源码也已在 `batch_build_query.rs:244`、
`batch_file_versions_query.rs:224`、`metrics_handlers.rs:135-144/298-305` 对齐。

真因在 DB 与建表入口：

- `init_codegraph_schema`（`rust_ext/src/daemon/cas_merge.rs:85`）是**只读
  路径**的幂等建表函数，被 `cas_merge_query.rs:136/224`（只读连接）与
  `replicator.rs:1107` / `workspace.rs:2545/3380` 调用，但它原本只建 6 张
  旧表：`workspaces` / `file_contents` / `file_instances` / `symbols` /
  `calls` / `symbol_contents`；
- canonical 新表 `file_versions` / `file_symbol_versions` / `call_versions` /
  `semgrep_findings` 只在**写路径** `open_codegraph_write`
  （`fs_handlers.rs:197` → `storage::initialize_or_migrate(path, 60)`）建；
- 主仓 codegraph DB（`~/.callwarden/codegraph/4baea3ff12c2ea5c/codegraph.db`）
  是 Python 时代旧库：无 `schema_migrations` 表，`workspaces` 表是旧结构
  （带 `is_active` / `active_task_id`），四张新表不存在；
- 结果：只读查询用新 schema SQL 撞旧库 → 9 条漂移 MCP_ERR。

### 修复（`rust_ext/src/daemon/cas_merge.rs`）

`init_codegraph_schema` 的建表语句在 `symbol_contents` 之后追加 4 张
canonical 表的 `CREATE TABLE IF NOT EXISTS`，列定义与
`storage.rs:162-226`（SCHEMA_VERSION=60 canonical DDL）逐字对齐：

- `file_versions`（含 `version_num`）
- `file_symbol_versions`（含 `symbol_hash`）
- `call_versions`
- `semgrep_findings`（含 `symbol_id` / `symbol_qualified` / `scanned_at` /
  `scan_id`，`UNIQUE(content_hash, rule_id, start_line)`）

因为 `CREATE TABLE IF NOT EXISTS` 对已存在表无操作，写路径已迁移的新库
不受影响；只读路径打开旧库时自动补齐缺口。

### 回归测试

- `test_init_codegraph_schema_on_fresh_db` 断言 6 → **10 张表**
- `test_init_codegraph_schema_idempotent` 断言 6 → **10 张表**
- **新增** `test_init_codegraph_schema_adds_canonical_tables_on_legacy_db`：
  只建 6 张旧表模拟 Python 时代旧库 → 调 `init_codegraph_schema` →
  断言 4 张 canonical 新表全部创建、旧表保留 6 张、
  `semgrep_findings` 含 5 个 canonical 关键列、二次调用幂等。

### 构建环境（本会话踩坑，已固化）

Windows git-bash 下 cargo 编译 callwarden-core 的三连坑与解法：

1. `/usr/bin/link.exe`（GNU coreutils）PATH 遮蔽 MSVC link →
   `link: extra operand '....rcgu.o'`；
2. 剥掉 `/usr/bin` 后缺 `LIB` / `INCLUDE` →
   `LNK1181: cannot open input file 'kernel32.lib'`；
3. 剥掉 `/usr/bin` 后 Windows `timeout.exe` 报"无效语法"（coreutils 失效）；
4. pyo3-ffi build script 在 Job Object 沙箱里派生 python 报
   `os error 231`（ERROR_PIPE_BUSY 管道实例耗尽）。

解法：环境脚本 `/c/Users/wanpi/AppData/Local/Temp/cw_msvc_env.sh`
（MSVC 14.44.35207 / SDK 10.0.26100.0，显式设
`CARGO_TARGET_X86_64_PC_WINDOWS_MSVC_LINKER` + `LIB` + `INCLUDE`）
+ `PYO3_CONFIG_FILE`（`/c/Users/wanpi/AppData/Local/Temp/pyo3_config.txt`，
手写的 InterpreterConfig，让 pyo3-ffi 跳过派生 python）。
另：`strings` 在 git-bash 对 exe 输出恒为 0 行，二进制取证改用
Python `open(rb).count(pat)`。

### 验证状态

- `cargo check --lib`：**RC=0，0 错误**（179 个 warnings 全是既有的
  dead-code，非本次引入）。
- `cas_merge::tests::test_init_codegraph_schema*` 三个测试 **3 passed /
  0 failed**（含新增的 legacy-db 回归测试）。
- DDL 逐列脚本化核对：四张新表与 `storage.rs` canonical DDL
  （SCHEMA_VERSION=60）**全部 MATCH**。

### 部署（commit 199882d + 758c941）

`refresh_shared_runtime.ps1 -TaskId T-1787293451688-c14b1e44
-Configuration release`：

- **构建成功**（12m03s，release，全量）；`runtime/current` 已切换到
  F-014 新二进制（cw-daemon.exe 46MB，sha256 与构建产物一致）；
  `callwarden_core.pyd` 已部署到 repository_source 与
  python314_site_package 两个目标（含备份）。
- 部署证据 `20260926-021137-758c941c22a9-4b8ed382.json`：
  **status=passed**，daemon_start_action=start。
- **部署脚本第二个 bug（commit 758c941）**：第一次部署在启动新 daemon
  时崩于 `已添加项。字典中的关键字:"Path" 所添加的关键字:"PATH"`——
  `Start-Process` 复制当前进程 env 进子进程用**大小写敏感 Hashtable**，
  会话 env 同时含 `Path`/`PATH`、`HTTP_PROXY`/`http_proxy`（本沙箱会话
  实测 3 组重复）即抛重复键。后果：旧 daemon 已停、新 daemon 未起。
  修复：新增 `Resolve-DuplicateEnvKeys`（保留首个、移除其余，只动脚本
  进程级 env），在主流程 try 顶部、任何 Start-Process 之前调用。
- **daemon 持久性**：沙箱内启动的 daemon 会随会话 Job Object 结束被收掉
  （本次实测：后台 PowerShell 任务结束后 daemon 消失）。已在当前会话
  重新拉起（PID 32040，ping ok，transport=http）；**持久运行需用户在
  真实终端重启 daemon**。

### MCP 只读矩阵对比（F-014 前后，同一 daemon 在线）

| | OK | OK_EMPTY | MCP_ERR | WRITE_SKIP | 其中 schema 漂移 | 其中 快照过期 |
|---|---|---|---|---|---|---|
| 前（HEAD=eb747d5） | 104 | 34 | 38 | 67 | **10** | 0 |
| 后（HEAD=758c941） | 95 | 27 | 54 | 67 | **0** | 28 |

**结论：F-014 消除了全部 10 条 schema 漂移**（`no such column:
s.symbol_hash` / `fv.version_num` / `sc.content`、`no such table:
semgrep_findings` 在 243 工具矩阵中归零；原 10 个漂移工具现在被**更早的**
快照门拦下，错误变成 `snapshot_not_ready`，schema 层已不再是瓶颈）。

MCP_ERR 38→54 的增量**全部是快照过期**（0→28），与 F-014 无关：
本会话两个提交把 HEAD 从 `eb747d5` 推到 `758c941`，而主仓已发布快照
仍钉在 `eb747d5`（snapshot_id `5faeb2790e7b1546`）→ 快照门校验
HEAD 不匹配 → 17 个原本 OK/OK_EMPTY 的工具被拦下。另 1 个工具
（`repo_map`）从 MCP_ERR 恢复 OK。

### 遗留（非 F-014 范围）

- **主仓快照需刷新到当前 HEAD**：在真实终端跑 `build_graph` /
  `snapshot.publish`（MCP 层慢方法超时 300s，主仓全量构建超过此值；
  沙箱 named-pipe 客户端在 ~7 分钟处 I/O 超时，未落盘）。刷新后 28 个
  快照门工具 + 10 个原漂移工具应返回真实数据。
- daemon 持久化：需在真实终端重启（见上）。
- 既存待办（未变化）：15 条 i18n drift 断言、6 条
  `*_daemon_unavailable` 改 mock、test_srv_019.py 写交付物副作用、
  WB-WT 一次性任务残留 4 条、主仓 DB 级污染。
