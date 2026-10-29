# CLI/MCP 验收矩阵收尾台账（承接 T-1793240742557-50dfd468）

> 承接卡：`T-1793248351476-e7d6650c`
> 步骤：`S-1793248351479-e7f7a910`（B 类 5 条双契约重测 + F-001 registry 数据治理 + build_graph 增量化收尾）
> 生成时间：2026-10-29
> 收口判定：`python scripts/check_e2e_matrix_closure.py`（退出码 0）

本台账记录三项遗留的**实测结论、根因与处置**，并附 F-5（alpha.py 跨 workspace
误写）的修复证据——二者同属「多 workspace 共用一个 daemon」的同一类问题域。

---

## 1. F-5 · 相对路径写工具跨 workspace 误写（本轮修复）

### 1.1 现象

隔离 workspace（`…/.workbuddy/scripts/outputs/wt_iso_ws`）写面补测时，以下
**schema 无 workspace 标识**的写工具的实际落点落在主仓：

| 工具 | 观测（旧矩阵 run `WB-WT-10291312`） |
|---|---|
| `refresh_file` | `args={"file_path":"alpha.py"}` → `total_lines=4`（隔离 ws 的 alpha.py 是 15 行，主仓根下的同名文件才是 4 行） |
| `remove_file` | `{"ok":true,"removed":true}`，被删的是主仓根 `alpha.py` |
| `rule_insert_agents_md_block` | `bytes_written=60`，写入主仓根 `AGENTS.md` 尾部 marker |
| `build_directory` | `scanned=0`（相对 `.` 按 daemon 进程 CWD=仓库根解析，却被根内校验挡回） |
| `gc_retention` | `active_files=2370`（主仓规模，非隔离 ws） |

上版补测更严重的同源事故：`remove_file` 物理删除主仓 `cw.py`，随后
`rule_insert_agents_md_block` 读到空串写出 60 字节存根（见
`deliverables/cli-e2e-findings.md` §8 事故取证）。

### 1.2 根因（两层，缺一不可）

1. **注入层**：`route_rpc` 对无 ws 标识的方法注入的 `workspace_instance_id`
   来自 `HttpDaemonRpcClient._ensure_remote_snapshot`，其根是 MCP 宿主的
   `_project_root`（`server/mcp_server.py:53` → `_resolve_mcp_project_root`，
   优先级 1 为 `CALLWARDEN_WORKSPACE`，2 为 cwd 向上探测），**不是**活动
   workspace。测试 harness 以仓库根起 MCP 宿主 → 注入主仓 instance。
2. **解析层（真缺陷）**：daemon 是多 workspace 常驻进程，路径参数的相对串在
   `validate_owned_path[_any]` 内部 `std::fs::canonicalize` 时按**daemon 进程
   CWD**（启动目录）解析，而不是按 `workspace_instance_id → host_real_root`
   解析。即便注入的是隔离 ws instance，`refresh_file("alpha.py")` 仍命中
   daemon CWD 下的同名文件；CWD 与主仓根重合时即造成主仓误删/误写。

### 1.3 修复

**Rust/daemon（真缺陷）**：

- `rust_ext/src/daemon/fs_handlers.rs`：新增 `anchor_path_to_root(root, path)`，
  相对路径一律 `root.join(path)`、绝对路径原样；接入 `resolve_owned_path`
  （file.read/file.health 共用）、`build_graph` 的 `scan_root`、
  `build_directory` 的 `dir_path`、`file_grep`/`file_list` 的 `path`、
  `file.remove`、`refresh_file`。根内包含校验（`ensure_root_containment`）
  保持不变，越根仍 `path_escape` fail-closed。
- `rust_ext/src/daemon/edit_handlers.rs`：`rule.insert_agents_md_block` /
  `rule.sync_agents_md` 新增 `root: &Path` 参数，改用
  `resolve_rule_target_path`（相对锚定 + 父目录规范化 + 根内前缀校验；
  `../escape.md` → `path_escape`）；删除按 codegraph DB 反查根的旧 helper。
- `rust_ext/src/daemon/snapshot_state.rs`：新增 `workspace_host_root`
  （`owned_workspace` ACL + `host_real_root`/`client_view_root` + canonicalize），
  dispatch 两个 `rule.*` 分支传入该根。

**harness（注入层隔离）**：`.workbuddy/scripts/mcp_write_tools_test.py`
Phase 2 的 MCP 宿主改以隔离 ws 为项目根（`ENV["CALLWARDEN_WORKSPACE"]=ISO_ROOT`），
使无 ws 标识的写工具注入隔离 ws instance；新增 Phase 4「主仓零污染断言」
（运行前后对仓库根 `alpha.py`/`beta.py`/`AGENTS.md` 做 sha256 对比）；
主仓 registry id 改为按 `registry.db` 动态解析（`detect_main_ws_id`），
修掉硬编码 1193 因 ROWID 轮换导致的 `workspace_not_found`。

### 1.4 验证

- Rust 单测（4/4 通过）：
  `test_anchor_path_to_root_relative_and_absolute`、
  `test_anchor_path_keeps_root_containment_fail_closed`、
  `test_resolve_rule_target_path_anchors_to_workspace_root`、
  `test_rule_insert_writes_into_workspace_root_not_cwd`。
- 部署：`scripts/refresh_shared_runtime.ps1 -TaskId T-1793248351476-e7d6650c`。
- 活体：见 §4 复跑结果（主仓零污染断言 + 隔离 ws 读写落点）。

---

## 2. B 类 5 条双契约重测

### 2.1 原结论（2026-09-25 全量测试）

`list_build_contexts` / `get_build_context` / `get_active_build_context` /
`get_resolved_edges` / `count_resolved_edges` 报
`workspace_id 1193 与 workspace_instance_id 绑定的 1 不一致`。

### 2.2 根因（数据 + 契约二义，不是工具实现缺陷）

`require_bound_workspace_id`（`rust_ext/src/daemon/snapshot_state.rs`）已实现
双命名空间归一：`params.workspace_id` 等于**权威库 id**（`workspaces.id`，主仓
= 1）或等于该 instance 的 **registry ROWID** 时均放行。

原失败是 fixture 传了 `1193`——这是**当次运行的 registry ROWID**。`workspace.register`
用 `INSERT OR REPLACE`（delete+insert）重写行，ROWID 随注册轮换：本轮实测该
instance（`4baea3ff12c2ea5c`）的 ROWID 曾漂移到 `207`/`208`，治理后再次注册又
回到 `1193`。也就是说 `workspace_id: int` 这个位置在同一个工具上同时可能是
「快照库自增 id」或「轮换中的 registry ROWID」，fixture 把它固定成某个历史值
必然间歇性失败。

### 2.3 重测口径与结果

- 口径：先调 `get_active_workspace`（`workspace.status` 触发 `snapshot.publish`）
  发布主仓快照——`build_context.*` 不在 `route_rpc` 的 `needs_snapshot` 集合内，
  没有已发布快照时会先撞 `snapshot_not_ready`；随后同一套 5 个工具按三个
  `workspace_id` 各跑一遍（`build_context_hash=""`）。
- 实测（`C:\Users\wanpi\AppData\Local\Temp\b_class_retest.json`，见 §4.2）：
  **权威 id 1 与 registry ROWID 1193 两组 5/5 均无错误**，外来 ROWID 1194
  5/5 fail-closed。原「双契约不一致」不再复现。

> 产品面建议（不入本次改动）：`workspace_id: int` 在 MCP schema 上是
> 「快照库 id / registry ROWID」二义，长期应收敛为显式
> `workspace_instance_id`（其余 workspace 域工具已完成该迁移）。

---

## 3. build_graph 增量化收尾

### 3.1 现状

`handle_build_graph`（`rust_ext/src/daemon/fs_handlers.rs`）每次调用都做**全量**
扫描：遍历 `scan_files` 结果、逐文件 sha256、`file_contents` / `file_instances`
upsert，再对可解析语言走 tree-sitter 符号落库。返回字段
`scanned / inserted / unchanged / symbols / calls / calls_resolved / calls_ambiguous`。

注意既有语义偏差：`unchanged` 实际统计的是**非可解析语言文件数**（`parsed=false`
分支），不是「内容哈希未变的文件数」。因此该字段不能用于增量收益评估。

### 3.2 结论（性能项，非缺陷；本轮不实现）

增量化需要三件事同时成立，成本/风险高于本次收尾范围：

1. 以 `(file_instances.content_hash, mtime)` 比对跳过重复解析（收益仅在
   热调用场景，冷启动 / CI 无差别）；
2. **删除感知**：记录上一轮文件清单并清理已消失文件的 `file_instances` 与
   `symbols`，否则索引只增不减（当前全量扫描天然自愈，改为增量后必须补齐）；
3. `resolve_raw_calls` 的跨文件边重算范围需要按变更集裁剪，否则边解析仍是
   全量、增量化收益被吃掉。

处置：登记为 backlog 性能项（P3），不阻塞本次验收矩阵收口；同时修正
`unchanged` 字段语义需与 Python 侧契约对齐后再动（避免窄修破坏快照比对）。
实测基线见 §4.3（隔离 ws 与主仓的 scanned/inserted 数值）。

---

## 4. 实测证据（本机 2026-10-29）

> 环境：`cw-daemon.exe`（runtime/current，release），HTTP loopback；
> 数据根 `C:\var\lib\callwarden\`（`registry.db` 为 daemon **真实** registry），
> 快照库 `~/.callwarden/callwarden.db`。

### 4.0 前置修复：MCP 宿主被 git 子进程挂死

复测一开始全部 `tools/call` 超时而 daemon 侧全程空闲。定位：MCP server
（单线程 stdio）在处理 `get_active_workspace` 时进入
`_workspace_snapshot_metadata()`，其 `subprocess.run(["git", ...])` 继承了
宿主的 stdin（长期打开、按需写入的 JSON-RPC 管道）后不再返回；`git.exe`
子进程 0 CPU 常驻，MCP server 随之整体阻塞（实测 180s/600s 均超时）。

修复（`server/daemon_client.py::_workspace_snapshot_metadata`）：git 调用
显式 `stdin=subprocess.DEVNULL` + `timeout=10`，失败/超时退化为空 provenance
（元数据本就是可选项），宿主永不因此阻塞。修复后该调用恢复
（`registry_workspace_id=1193 / task_db_workspace_id=1`，毫秒级返回）。

### 4.1 F-5 复跑（隔离 ws 写面 + 主仓零污染）

运行：`WB-WT-10291438`，隔离 ws `wt_iso_ws`（registry `workspace_id=1711` /
instance `ebf553b4e42d5a6c`），主仓 registry `workspace_id=1194`（运行时动态解析，
静态哨兵 1193 已因 ROWID 轮换失效）。矩阵见 `outputs/mcp_write_tools_matrix.json`
（`total=82`，counters `OK=34 / MCP_ERR=27 / SKIP_NOTOOL=15 / SKIP_COVERED=2 /
SKIP_DEFERRED=2 / SKIP_NOFIXTURE=2`）。

| 工具 | 参数 | 结果 | 落点判定 |
|---|---|---|---|
| `refresh_file` | `file_path="alpha.py"` | `total_lines=7, symbols=3, content_hash=73898df5…` | 隔离 ws（主仓同名文件不存在；旧矩阵为 4 行主仓文件） |
| `remove_file` | `file_path="beta.py"` | `{"ok":true,"removed":true}` | 隔离 ws（运行后主仓根仍无 `beta.py`） |
| `rule_insert_agents_md_block` | `target_path="alpha.py"` | `bytes_written=182` | 隔离 ws（旧矩阵为 60 字节主仓 `AGENTS.md` 存根） |
| `rule_sync_agents_md` | `target_path="alpha.py", dry_run=true` | `success=true, rule_count=3` | 隔离 ws |
| `build_directory` | `dir_path="."`（定点复验） | `scanned=1, refreshed=1, symbols=3` | 隔离 ws 根（仅剩 `alpha.py`；主仓根为 2370 文件，不可能得出 1） |
| `gc_retention` | `older_than_days=1` | `active_files=4, total=4` | 隔离 ws 规模（旧矩阵为 2370） |

> `build_directory` 矩阵内那条 `dir_path="alpha.py"` 得 `scanned=0` 属参数语义
> （传文件而非目录），不代表落点错误；故补 `dir_path="."` 定点复验，见上表。

主仓零污染断言（运行前后仓库根 sha256 对比）：`{"alpha.py": true, "beta.py": true,
"AGENTS.md": true}`，其中 `AGENTS.md` 前后均为 `7ea2f48f5955649a…`（长度与内容
零变化，证明 `rule.*` 未再落到主仓）。

F-013 回归（`f013_check`）：`set_active_workspace(1711)` 与 `delete_workspace(1711)`
返回行均为 `workspace_id=1711 / instance=ebf553b4e42d5a6c`，主仓哨兵 1194
未受影响——id-or-name 显式解析无回归。

结论：注入层（宿主根锚到隔离 ws）+ 解析层（daemon 相对路径按 workspace 根锚定）
同时生效，6 个无 ws 标识写工具全部落在目标 workspace；越根仍 `path_escape`
fail-closed（Rust 单测覆盖，见 §1.4）。

> 补测残留：`T-1793255935561-b679f338`（`WB-WT-10291438-fixture-task`，已 reverted）、
> `T-1793256073680-df0c5690`（`WB-WT-10291438-3ff22f`，open/无步骤）、
> `T-1793255870562-9449c52c`（`wt-capture-seed`，open/无步骤）。daemon 无
> `task.delete`，`task.close` 需 reviewer lease，故按创建语义留存（标题自带
> WB-WT / seed 前缀，不参与任何验收断言）。

### 4.2 B 类 5 条重测

前置：`get_active_workspace` → `snapshot.publish` 成功（主仓 snapshot
`generation=1, symbol_count=366102, call_count=274997`）。

| 场景 | `workspace_id` | `list_build_contexts` | `get_active_build_context` | `get_build_context` | `get_resolved_edges` | `count_resolved_edges` |
|---|---|---|---|---|---|---|
| 权威 id（快照库 `workspaces.id`） | 1 | OK（空） | OK（空） | OK（空） | OK（空） | OK `{"count":0}` |
| registry ROWID（注入 instance `4baea3ff12c2ea5c` 当前行） | 1193 | OK（空） | OK（空） | OK（空） | OK（空） | OK `{"count":0}` |
| 外来 ROWID（同根另一 instance `822c031c71488f12`） | 1194 | fail-closed | fail-closed | fail-closed | fail-closed | fail-closed |

外来 ROWID 的错误体（5/5 一致）：

```
invalid_params: workspace_id 1194 与 workspace_instance_id 绑定的 1 不一致
```

结论：双命名空间归一按预期放行「权威 id / 绑定 instance 的 ROWID」，
对其它 ROWID fail-closed；原「双契约不一致」是 fixture 固定历史 ROWID，
非工具实现缺陷。

### 4.3 build_graph 基线

隔离 ws（`wt_iso_ws`，Phase 0 定向 `workspace_instance_id=ebf553b4e42d5a6c`）：

```
build_graph -> 0.4s  {"ok":true,"scanned":2,"inserted":2,"unchanged":0,"symbols":4,...}
```

2 个源文件（`alpha.py` 3 符号 + `beta.py` 1 符号）全量扫完 0.4s；
`unchanged=0` 与「非可解析语言文件数」的既有语义一致（两文件均可解析），
说明该字段在本场景无增量含义（见 §3.1）。基准稳定、成本可忽略，故本次不实现
增量化，仅登记 P3-1。

主仓规模参照（同一 daemon 已发布快照）：`generation=1, symbol_count=366102,
call_count=274997`；矩阵内 `gc_retention` 得 `active_files=2370`。主仓全量
`build_graph` 属分钟级慢方法（HTTP 侧已放宽到 `HTTP_SLOW_METHOD_TIMEOUT`），
不作为每次验收的必跑基线。

### 4.4 F-001 registry 数据治理

daemon 真实 registry（`C:\var\lib\callwarden\registry.db`，**不是**
`~/.callwarden/registry.db` 那个历史旁路库）治理前后：

| 指标 | before | after |
|---|---|---|
| `daemon_workspaces` 总行数 | 172 | 172 |
| root 已不存在（stale） | 148 | 0 |
| `status=active` | 171 | 23 |

处置：对 root 缺失的 148 行逐条 `workspace.remove`（归档，不物理删，
保留审计痕迹），`archived=148 failed=0`。这样 daemon 重启时
`recover_all_workspaces_with_snapshot` 不会再把 pytest/临时目录行重新标
active，消除「多 active workspace」歧义来源。

> 复测期间隔离 ws（registry ROWID 1711）再次注册，测试结束已 `workspace delete`
> 归档；当前实测 `total=172 / archived=149 / active=23`，与治理目标一致。

---

## 5. 后续 backlog（未纳入本次步骤）

| 编号 | 项 | 类型 | 状态 |
|---|---|---|---|
| P3-1 | build_graph 增量化（哈希跳过 + 删除感知 + 边重算裁剪） | 性能 | 待排期 |
| P3-2 | `workspace_id: int` → `workspace_instance_id` 契约收敛（build_context 族） | 契约 | 待排期 |
| P3-3 | `build_graph.unchanged` 字段语义与 Python 契约对齐 | 正确性/可观测 | 待排期 |
