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

> 产品面建议（P3-2 已落地，2026-10-29）：`workspace_id: int` 在 MCP schema 上
> 是「快照库 id / registry ROWID」二义，现 build_context 族 3 个工具已收敛为
> `workspace_instance_id: str` 优先、`workspace_id` 保留并标注 deprecated。
> 缺省数字 id 时工具按 instance 经 `workspace.status` 解析 registry 主键补齐；
> 两者都缺或 instance 归属校验失败即 fail-closed（不猜活动 workspace）。

---

## 3. build_graph 增量化收尾

### 3.1 现状

`handle_build_graph`（`rust_ext/src/daemon/fs_handlers.rs`）每次调用都做**全量**
扫描：遍历 `scan_files` 结果、逐文件 sha256、`file_contents` / `file_instances`
upsert，再对可解析语言走 tree-sitter 符号落库。返回字段
`scanned / inserted / unchanged / symbols / calls / calls_resolved / calls_ambiguous`。

注意（P3-3 已修，2026-10-29）：旧实现里 `unchanged` 统计的是**非可解析语言
文件数**（`parsed=false` 分支），与字段名含义相反。现语义已收敛为「可解析且
内容 hash 与库内 `current_content_hash` 一致的文件数」，不可解析文件改计入
新增字段 `skipped_unparsed`（任务 `T-1793263115744-7ac43c98` 步骤#1）。

### 3.2 结论（性能项，非缺陷；本轮不实现）

增量化需要三件事同时成立，成本/风险高于本次收尾范围：

1. 以 `(file_instances.content_hash, mtime)` 比对跳过重复解析（收益仅在
   热调用场景，冷启动 / CI 无差别）；
2. **删除感知**：记录上一轮文件清单并清理已消失文件的 `file_instances` 与
   `symbols`，否则索引只增不减（当前全量扫描天然自愈，改为增量后必须补齐）；
3. `resolve_raw_calls` 的跨文件边重算范围需要按变更集裁剪，否则边解析仍是
   全量、增量化收益被吃掉。

处置：登记为 backlog 性能项（P3-1，见 §5），不阻塞本次验收矩阵收口。
`unchanged` 字段语义偏差已先行修正（P3-3），增量化的收益评估从此字段可读。
实测基线见 §4.3（隔离 ws 与主仓的 scanned/inserted 数值）。

> 更新（2026-10-29）：P3-1 已在 `T-1793263115744-7ac43c98` 步骤#3 落地，
> 实测证据与连带缺陷修复见 §4.6。

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

### 4.5 P3-3 现场证据（`unchanged` 语义修正）

隔离 ws `p33_iso_ws`（`alpha.py` 可解析 + `widget.vue` 无 parser），同一
workspace 连续三次 `workspace.build_graph`（daemon 已部署本次修复）：

| 轮次 | scanned | inserted | unchanged | skipped_unparsed | symbols |
|---|---|---|---|---|---|
| 首次全建 | 2 | 1 | 0 | 1 | 1 |
| 内容未变重建 | 2 | 0 | 1 | 1 | 1 |
| 追加 `alpha_two` 后重建 | 2 | 1 | 0 | 1 | 2 |

旧实现在「首次全建」这行会返回 `unchanged=1`（把 `.vue` 当未变化）且
`skipped_unparsed` 字段不存在；现在 `unchanged` 只随真实内容变化翻转，
`.vue` 稳定计入 `skipped_unparsed`，与 §3.1 契约一致。

### 4.6 P3-1 现场证据（build_graph 增量重建）

实现（`rust_ext/src/daemon/fs_handlers.rs`，T-1793263115744-7ac43c98 步骤#3）：

1. **mtime 快路径**：库内 `status='parsed'` + 内容 hash 非空 + `mtime` 与磁盘一致
   → 跳过文件读取 / sha256 / tree-sitter 解析（计 `skipped_cached`）。共享 schema
   （`db/schema.py`，外部契约）无 size 列，本步骤不改 schema，故缓存键取
   mtime + 内容 hash；mtime 不等即回落 sha256 全量校验（正确性仍以 hash 为准）。
2. **内容未变但 mtime 变化**（hash 一致）→ 只刷新 `file_instances`，不重解析符号。
3. **删除感知**：库里存在、磁盘上确实不存在的文件 → 按 FK 依赖序清理
   `calls` / `file_versions` / `file_symbol_versions` / `call_versions` /
   `symbols` / `coverage_data` / `file_ownership` / `semgrep_findings` /
   `file_instances`（计 `deleted`）。判据是**磁盘存在性**而非「本轮扫描到」，
   因此只扫子目录（`scan_root`）或被 ignore 规则排除但仍存在的文件都不会被误删。
4. **边重算裁剪**：只对本轮受影响的名字重算（改动文件的旧/新符号名 + 新 raw
   callee 名 + 删除文件波及的名字）；无改动时 `calls_resolved=0`。
   裁剪路径走**调用侧驱动**：先按名字集合（每 500 个一块）扫 `calls` 收集目标
   raw 边行 id，再按 rowid 批量 `UPDATE`。逐名 `UPDATE ... callee_name=? AND
   caller_id IN (<workspace 符号>)` 的查询计划是
   `SEARCH calls USING INDEX idx_calls_caller (caller_id=?)`，等于「每个名字 ×
   workspace 全部 caller 符号」（主仓 4.3 万符号，实测单名 95ms）；名字上千时
   即为分钟级，是现场主仓增量建图把 HTTP 拖到 600s 超时的根因。

隔离 ws `p31_iso_ws`（`alpha.py` / `beta.py` / `widget.vue`）连续四轮实测
（daemon 已部署本次修复）：

| 轮次 | scanned | inserted | unchanged | skipped_cached | deleted | skipped_unparsed |
|---|---|---|---|---|---|---|
| 首次全建 | 3 | 2 | 0 | 0 | 0 | 1 |
| 内容未变重建 | 3 | 0 | 2 | 2 | 0 | 1 |
| 追加 `alpha_two` 后重建 | 3 | 1 | 1 | 1 | 0 | 1 |
| 删除 `beta.py` 后重建 | 2 | 0 | 1 | 1 | 1 | 1 |

删除后 `beta.py` 的 `file_instances` / `symbols` 行数均为 0；`calls_resolved`
在无改动轮次为 0（旧实现每轮全库重算）。同构文件集（25 个真实 py 文件）
冷建 4.86s（`inserted=25`，`calls=9611`，`calls_resolved=1219`）vs
热建 0.081s（`skipped_cached=25`），**提速 59.8×**。

主仓稳态（`C:\git_work\callwarden`，daemon release 部署后连续两轮）：
`scanned=1241`。注意该值与 §4.3 的 `gc_retention active_files=2370` 不同源：
`build` 目录被 `.gitignore` 剪枝、`rust_ext/target` 命中 `SKIP_DIRS`，这些行
在磁盘上仍存在，因此按「磁盘存在性」判据既不计入 `scanned` 也不会被删除感知
清理（属既有历史行，非本轮增量语义）。

| 场景 | 耗时 | 关键计数 |
|---|---|---|
| 同构集冷建（25 文件） | 4.86s | inserted=25 / calls=9611 |
| 同构集热建（同集合） | 0.081s | skipped_cached=25 / calls_resolved=0 |
| 主仓热建#1 | 1.089s | scanned=1241 / skipped_cached=1240 / inserted=1 / calls_resolved=78 |
| 主仓热建#2 | 0.303s | scanned=1241 / skipped_cached=1241 / calls_resolved=0 |

### 4.6.1 增量化的连带修复（现场暴露的真实缺陷）

- **跨 workspace 误挂边**：`resolve_raw_calls` 的单候选 `UPDATE` 只按
  `callee_name` 全库更新，会把手写其它 workspace 的 raw 边挂到本 workspace
  的符号 id 上（悬空引用 + 跨库写）。已加 workspace 作用域并新增单测
  `test_resolve_raw_calls_scoped_to_workspace`；存量数据一次性修复：重置
  28,182 条跨 workspace 的已解析边为 raw，复查剩余 0。
- **重解析 FK 失败**：`parse_and_store_symbols` 重建符号时未先清
  `coverage_data` / `clone_pairs` / `test_case_relations` / `test_runs` 这些
  无 CASCADE 的引用行，主仓 `analyzers/compile_commands.py` 必现
  `FOREIGN KEY constraint failed`。抽出 `purge_symbol_references` 先清子表，
  新增单测 `test_reparse_clears_restrictive_symbol_references`。
- **失败自愈**：解析失败时 `file_instances` 已被标成 `parsed`，快路径会让该文件
  永久跳过；失败分支现回退 `status='pending'`（旧实现靠每轮全量重解析自愈，
  增量化后必须显式补偿）。
- **边重算驱动方向（性能缺陷，非功能缺陷）**：增量裁剪最初沿用既有逐名
  `UPDATE`，查询计划是 `SEARCH calls USING INDEX idx_calls_caller (caller_id=?)`，
  复杂度为 `名字数 × workspace 符号数`（主仓实测单名 95ms），数千名字即把
  主仓建图拖到 600s HTTP 超时（现场复现两次）。改为调用侧驱动（分块扫
  `calls` 收边 + rowid 批量 UPDATE）后，主仓连续两轮稳态 1.089s / 0.303s。
  `calls` 表无 `callee_name` 索引是既有 schema 事实，本步骤不改 schema，
  故从驱动方向规避（而非新增索引）。

### 4.6.2 v61 部分索引实测（P3-4，`T-1793284203427-56a164bc`）

§4.6.1 的驱动方向规避只解决了**已有判定路径**的开销，仍有按 `callee_name`
找 raw 边（`callee_id = 0`）的查询会全表扫 `calls`。v61 补上部分索引：

```sql
CREATE INDEX IF NOT EXISTS idx_calls_callee_name_raw
ON calls(callee_name) WHERE callee_id = 0;
```

只覆盖 raw 行，已解析边不额外维护键。迁移双端同步（`SCHEMA_VERSION`
60→61，Python `_migrate_v60_to_v61` + Rust canonical schema 同源），
`fs_handlers` 写路径改为读取 `storage::SCHEMA_VERSION`（此前硬编码 60，
v61 库会报 `SCHEMA_TOO_NEW`）。

实测（主仓权威库，`calls=574,892` 行 / `callee_id=0` 的 raw 边 462,272 行）：

| 指标 | 修复前 | v61 后 |
|---|---|---|
| `EXPLAIN QUERY PLAN`（`callee_name=? AND callee_id=0`） | `SCAN calls` | `SEARCH calls USING COVERING INDEX idx_calls_callee_name_raw (callee_name=?)` |
| 按名定位 raw 边（单名） | 95ms（逐名 `UPDATE` 计划） | 0.004ms（100 个 distinct 名字合计 0.000s） |
| 主仓稳态增量建图 | 1.089s / 0.303s（驱动方向修复后） | 0.252s（`scanned=1241`，全 `unchanged`） |

索引为部分索引（`WHERE callee_id = 0`），`sqlite_master.sql` 断言保留谓词；
`initialize_or_migrate` 二次调用幂等；新建库、既有 v60 库（Python / Rust
两条迁移链）、daemon 写路径三处均建出该索引。

### 4.7 build/ 索引残留归档（C1，`T-1793317526334-eea45f3c`）

**现象**：权威库 `ws=1`（`c:/git_work/callwarden`）仍有
`build/lib.win-amd64-cpython-314/` 下 77 条 `file_instances`（全部
`status=pending`）与 1,467 个符号，而 `archived_files=0`；`.gitignore:27`
与 `.callwardenignore` 默认基线均声明 `build/` 应忽略。

**根因（归档缺口，不是本轮扫描漏剪）**：

1. `load_ignore_dir_names` 只作用于**本轮扫描**（`scan_files` 递归剪枝）；
2. 删除感知段（`handle_build_graph`）以「磁盘上是否真的不存在」为准，
   `root_canon.join(rel).exists()` 为真即跳过——被 ignore 剪枝、但文件仍在
   磁盘的历史行永不进入 purge 路径；
3. 既有归档实现 `gc_archive_core` 只在 `gc archive` RPC/CLI 显式调用，
   `handle_build_graph` 从不触发，故 `archived_files` 长期为 0。

判定为历史残留而非扫描漏剪：残留行 `mtime` 范围
2026-07-01T16:28:06Z ~ 2026-07-25T12:05:26Z、`last_parsed` 最大
2026-07-25T12:07:51Z，均早于 ignore 剪枝提交 `0bea95e`（2026-09-14 19:12 +0800）。

**修复**（`rust_ext/src/daemon/fs_handlers.rs`）：

- 抽出 `purge_file_graph_rows`：symbols/calls/file_versions 等级联清理，
  保留 `file_instances` 行以维持 `archived_files.file_instance_id` 的
  NO ACTION 外键；`purge_missing_file_graph` 复用该助手后仅额外删
  `archived_files` / `file_instances`，语义不变；
- 新增 `archive_ignored_file_graph`：写 `archived_files`（`symbol_count` /
  `call_count` / `archive_reason="ignored path: <dir>"` / `archived_at`）→
  级联清图行 → `status='archived'`；以「已归档记录存在」+「状态已 archived」
  双重短路，重复执行不重复归档；
- 新增 `ignored_dir_of`：只看目录段（文件名不参与），只命中 ignore 契约
  目录名（`.gitignore`/`.callwardenignore` + 硬编码 `SKIP_DIRS`）；
  **不采用 `scan_files` 的隐藏目录启发式**——`.github/` 不在 ignore 契约内，
  Python 管线正常索引，按隐藏目录归档会误伤源码；
- `handle_build_graph` 删除感知改为：命中剪枝且文件仍存在 → 归档并跳过；
  否则维持「磁盘不存在才 cleanup」语义，未忽略文件不受影响；响应新增
  `archived` 计数。

**复测**（部署后 daemon `workspace.build_graph`，权威库 `ws=1`）：

| 指标 | 修复前 | 修复后 |
|---|---|---|
| `build/` `file_instances` | 77（全 `pending`） | 77 全部 `status='archived'`（0 条非归档行） |
| `build/` 关联 `symbols` | 1,467 | 0 |
| `archived_files`（ws=1） | 0 | 88（77 `build` + 11 `target`，均为 ignore 契约目录） |
| build_graph 响应 | 无 `archived` 字段 | `archived=88`（二次调用 0，幂等） |
| 未忽略源码 | — | 不受影响（`.github/` 等非契约目录保持 `parsed`） |

首轮实测曾把 `.github/workflows/e2e/run_platform_e2e.py` 误归档（隐藏目录
启发式），已用 `gc restore` 复活并收窄规则；`gc restore` 只把行翻回
`pending`，随后 `cw refresh <path>` 重解析恢复为 `parsed` + 21 符号
（实测 `total_lines=1377`、`archived_files` 无该行）。修正后归档集合只剩
ignore 契约目录，第二轮全量 `build_graph`（修复版 daemon）归档数不再增长。

回归：Rust `cargo test --lib daemon::fs_handlers::tests` 19 passed（含新增
`test_archive_ignored_file_graph_is_idempotent`）；Python `tests/test_gc.py`
9/9 passed（gc_archive/restore/purge 语义未被改动）。

---

## 5. 后续 backlog（未纳入本次步骤）

| 编号 | 项 | 类型 | 状态 |
|---|---|---|---|
| P3-1 | build_graph 增量化（哈希跳过 + 删除感知 + 边重算裁剪） | 性能 | 已修（T-1793263115744-7ac43c98 步骤#3） |
| P3-2 | `workspace_id: int` → `workspace_instance_id` 契约收敛（build_context 族） | 契约 | 已修（T-1793263115744-7ac43c98 步骤#2） |
| P3-3 | `build_graph.unchanged` 字段语义与 Python 契约对齐 | 正确性/可观测 | 已修（T-1793263115744-7ac43c98 步骤#1） |
| P3-4 | `calls.callee_name` 部分索引（`WHERE callee_id = 0`，raw 边按名字解析/查询） | 性能/schema | 已修（T-1793284203427-56a164bc 步骤#3：SCHEMA_VERSION=61，`idx_calls_callee_name_raw` 部分索引，实测见 §4.6.2） |
| P3-5 | `get_callers`/`get_callees` 短名降级分支跨 workspace 泄漏 + 全表扫（C2） | 正确性/性能 | 已修（T-1793317538470-c203e6d4 步骤#3，见 §6） |
| C1 | 权威库 ignored 行归档缺口（`build/` 残留 77 文件 / 1,467 符号，`archived_files=0`） | 正确性/数据治理 | 已修（T-1793317526334-eea45f3c 步骤#1-#2，见 §4.7） |
| C7 | 全量 pytest 基线恒红：native/LLM 前置缺失时缺 skip 守卫（CLI 验收 568 ERROR、隔离 daemon 管道冲突、`sqlite3.lib`、外部 LLM） | 测试信噪比 | 卡内已修（T-1793318539212-c2db0418 步骤#1-#2，见 §7）；卡外 daemon 失联族 164 / native 构建族 72 / 未归一 44 待后续卡 |
| C8 | Rust 内嵌 schema 副本（`SCHEMA_TABLES_SQL` / `SCHEMA_INDEXES_SQL`）仍声明 canonical 已删除的 `idx_calls_callee_name` 等全量索引（审计候选 C3） | 正确性/schema 真相源 | 已修（T-1793342216372-87953754 步骤#1-#2，见 §8） |
| C9 | Schema 版本文档门禁被弱化为仅存在性检查（`tests/test_architecture_doc.py` / `tests/test_abi_contract.py` 的文档↔代码比对）（审计候选 C4） | 治理/门禁有效性 | 已修（T-1793352736346-e6e623a4 步骤#1-#2，见 §9） |
| C10 | 权威库工作区登记污染：同物理 checkout 多行 active、workspace_id 二义（审计候选 C5） | 正确性/数据治理 | 已修（T-1793353773817-750277f4 步骤#0-#2，见 §10） |
| C6 | Schema 迁移注册表缺口：`_get_migrations` 缺 v53-v58/v60，`_migrate_schema` 静默跳过导致库被打标 v61 却缺表缺列（审计候选 C6） | 正确性/schema 真相源 | 已修（T-1793368261327-97e8e5fc 步骤#0-#2，见 §11） |

---

## 6. C2 · get_callers/get_callees 短名分支 workspace 收口（2026-10-30）

### 6.1 现象与证据

`rust_ext/src/daemon/daemon_client_handlers.rs` 的 SQL 降级短名分支只按名字过滤：
callers `WHERE c.callee_name = ?1`、callees `WHERE s.name = ?1`，均无
`file_instances.workspace_id` 谓词。权威库（14 个 workspace）实测：

- 泄漏：`callee_name = 'helper'` 全局查询唯一命中行的 caller 属于
  workspace 1720（graphify），而 `workspace_id = 1`（callwarden）查询按旧实现
  同样返回该行 → 跨 workspace 泄漏。
- 全表扫：旧短名 callers 计划为 `SCAN c`（calls 574,914 行），实测
  `helper` 106ms、`get`（35,215 行）235ms。

RPC `mcp.daemon_client.sql_fallback_get_callers/get_callees` 已在
`dispatch.rs` 注册、`http_server.rs` 放行，CLI/直连 RPC 可达。

### 6.2 方案实测对比（同库，取 3 次最小值）

| 方案 | 查询计划 | `helper` | `main` | `get`(19,200 行) | 语义 |
|---|---|---|---|---|---|
| 旧（无 ws 谓词） | `SCAN c` | 106ms（跨 ws 返回 1 行） | 101ms | 235ms（35,215 行，跨 ws） | 文本短名 |
| A：补 `fi.workspace_id` | `SEARCH fi`(workspace 索引) → `SEARCH s` → `SEARCH c`(idx_calls_caller) | 99ms | 93ms | 150ms | 与原文本短名一致 |
| B：符号解析走部分索引 | `MULTI-INDEX OR`：`idx_calls_callee_id_resolved` + `idx_calls_callee_name_raw` | 0ms | 2.7ms | 116ms | 改变语义：排除 callee_id 指向异名符号的陈旧边 |
| C：新增 `calls(callee_name)` 索引 | 全索引 | 未落地 | 未落地 | 未落地 | 需 SCHEMA_VERSION 61→62 双端迁移 |

### 6.3 取舍与修复

采用 **方案 A**：零 schema 变更、语义与旧实现完全一致（只收口归属），
且已消除 `SCAN c`。

- `query_callers_by_short(conn, ws_id, name)`：`WHERE fi.workspace_id = ?2 AND c.callee_name = ?1`。
- `handle_sql_fallback_get_callees`：QN 与短名两分支均补
  `JOIN file_instances fi ON s.file_instance_id = fi.id`
  与 `fi.workspace_id = ?2`。
- 方案 B 性能更优但会静默丢弃「callee_id 已解析、但文本短名与目标符号名不一致」
  的陈旧边，属于语义变更，不在本卡范围；方案 C 的索引收益与迁移成本不对等，
  留作后续候选。

### 6.4 验证

- Rust 定向单测 `cargo test --lib daemon_client_handlers`：26 passed / 0 failed，
  新增 `test_callers_short_name_is_workspace_scoped`、
  `test_callees_short_name_is_workspace_scoped`、
  `test_callees_qn_is_workspace_scoped`（ws7/ws8 同名 main/helper 夹具）。
- Python 定向回归 `pytest tests/test_p28b_qualified_name.py tests/test_p7_callee_id_index.py`：11 passed。
- 权威库 `EXPLAIN QUERY PLAN`：callers/callees 均为 `SEARCH ... USING INDEX`
  组合，无 `SCAN c`。
- 部署 + 活体双 workspace 隔离（`refresh_shared_runtime.ps1 -TaskId T-1793317538470-c203e6d4`，
  权威库 RPC `mcp.daemon_client.sql_fallback_get_callers/get_callees`）：
  修复前 `workspace_id=1` 与 `1720` 对 `helper` 均返回同一条 graphify 调用边，
  `main` 的 callees 两 ws 同为 15,760 条；修复后 `helper` ws1=0 / ws1720=1，
  callees `main` ws1=10,862 / ws1720=144，与按 ws 过滤的 SQL 期望值逐一相等。

### 6.5 残留（未纳入本卡）

Python 本地 SQL 降级 `db/db_query.py::get_callers/get_callees` 的短名分支仍保留
旧全局语义；CLI/MCP 正常链经 daemon RPC（本卡已收口），仅 enterprise 回退本地
SQL 时仍可能跨 ws。列为后续候选。

---

## 7. C7 · 全量 pytest 基线信噪比：前置缺失 skip 守卫（2026-10-30）

### 7.1 前提修正（卡面假设 vs 实测）

卡面原假设「本机缺 `sqlite3.lib` → `cargo build` 失败 → cw-daemon 二进制不可用」
与实测不符：C1 已在同一环境成功构建并部署 daemon（`sha256=402ecf52…`、PID 3288），
`workspace.build_graph` 实跑通过。全量基线恒红的真实前置是以下四类（均可复现）：

1. **冻结候选散列不匹配**：TP-10C 的 `TP-10/src/cw-daemon.exe` 不命中
   `KNOWN_DAEMON_SHA256_PREFIXES=('f1fa3736a16dbe7d','247b9a16049ec4d7')`，且
   `_TRUSTED_FROZEN_COPIES` 三个回退副本均不存在 → `_deploy_daemon` 抛错。
2. **SID 派生命名管道冲突**：常驻 daemon 占用
   `\\.\pipe\callwarden-S-1-5-21-1583625257-826939952-3615027596-1001`，隔离 daemon
   依赖相同管道名（未按 `data_root` 派生），发完 manifest 即
   `CreateNamedPipe 失败 (error 5)` → exit 1 → 客户端 `E_HTTP_DAEMON_UNAVAILABLE`
   （WinError 10061/10054）。
3. **外部 LLM 不可达**：`test_t5_llm_understandability` 选对率 0.00 < 基线 0.8；
   `llm_available()` 只判配置存在、不发探测。
4. **native 构建失败**：需要由当前源码重建二进制的用例仍走 `cargo build` →
   `LNK1181: 无法打开输入文件 "sqlite3.lib"`。

### 7.2 分类矩阵（改前 → 改后）

| 文件族 | 观察到的失败 | 真实前置 | 处置 |
|---|---|---|---|
| `tests/acceptance/cli/{test_cli_c1_semantics,test_cli_c2_params,…}.py` | 568 ERROR（`RuntimeError` 于 `_deploy_daemon`） | 冻结候选散列不匹配且无可信副本 | ✅ 已归一为 `pytest.skip` |
| `tests/test_query_*_rpc.py`、`test_cli_08x/09x_http_rpc.py`、`test_mcp_*_http_rpc.py` 等 23 文件（`w3_live` 夹具） | setup 期 `DaemonUnavailableError` | SID 管道冲突 / 隔离 daemon 提前退出 | ✅ 已归一为 `(None,·,·,·)` → `w3_live` skip |
| `tests/test_c16/c17/c21_*_workspace_authority.py`、`test_rust_cli_diff.py`、`test_http_daemon_*`、`convergence/test_m3/t4` 等 | 164 FAILED/ERROR（自建 `_spawn_isolated_daemon`） | 同上（管道冲突） | ⏳ 卡外文件，保持红；需后续卡统一走共享守卫 |
| `test_lease_rpc.py`、`test_lease_gate_empirical.py`、`test_cli_task_lease_parity.py`、`test_task_step_resolve_e2e.py`、`test_windows_*_e2e.py` | 70 ERROR + 2 ERROR（`cargo build` / `LNK1181`） | 缺 `sqlite3.lib` | ⏳ 卡外文件，保持红；需后续卡按「构建前置缺失」skip |
| `convergence/test_t5_llm_understandability.py` | 1 FAILED（0.00 < 0.8） | 外部 LLM 不可达 | ⏳ 卡外文件（`convergence/llm_channel.py`），保持红 |

卡内改动只覆盖上表前两行；后三行属卡外文件（`allowed_edit_scope` 之外），
按「禁止把真实回归吞成 skip」原则不做静默降级，作为后续卡输入。

### 7.3 改动（仅卡内白名单 2 文件，+60/−13）

- `tests/_w3_harness.py::setup_w3_client`：新增 `_prereq_unavailable_reason(exc,
  daemon_exited)`，**仅**三类明确前置降级为 `(None,None,None,None)`：
  (a) `DaemonUnavailableError` 且 `code ∈ {E_HTTP_DAEMON_UNAVAILABLE,
  E_HTTP_MANIFEST_STALE}`（显式排除 `E_MULTIPART_ROUTE_UNAVAILABLE` /
  `E_SHARED_TASK_WRITER_REQUIRED`）；(b) 异常发生时隔离 daemon 已退出；
  (c) 报文命中 `CreateNamedPipe` / `10061` / `10054` / `actively refused`。
  其余异常原样上抛；`tests/conftest.py::w3_live` 见 `proc is None` 即 `pytest.skip`。
- `tests/acceptance/cli/cli_acceptance_harness.py`：`_deploy_daemon` 的
  「候选缺失 / 散列不匹配且无可信副本」由 `RuntimeError`/`FileNotFoundError`
  改为 `pytest.skip`（仍 fail-closed，不静默换用未冻结二进制）；
  `start_daemon` 捕获 `daemon_bootstrap.BootstrapError` → `dump_log` 后
  `pytest.skip`；`verify_consistency()` 一致性失败**保持红**（属验收完整性，
  非环境前置）。

### 7.4 复测（2026-10-30，`C:/Python314/python.exe`）

前置条件：Windows + `C:/Python314/python.exe`（cp314 PyO3 扩展）+ 常驻 daemon
（PID 3288）运行时；不改冻结基准、不重启常驻 daemon。

```powershell
# 全量基线
C:/Python314/python.exe -m pytest tests -q --tb=no -p no:cacheprovider `
  --junit-xml=C:/Users/wanpi/AppData/Local/Temp/c7_full_after.xml
# CLI 验收族（定向）
C:/Python314/python.exe -m pytest tests/acceptance/cli -q --tb=no
# w3_live 家族代表文件
C:/Python314/python.exe -m pytest tests/test_query_file_rpc.py -q --tb=no
```

| 口径 | 改前（巡检） | 改后（本轮实测） |
|---|---|---|
| 全量用例数 | — | 9,515 |
| passed | — | 7,784 |
| skipped | — | 1,441 |
| failed | 168 | 165 |
| error | 1,210 | **117** |
| `tests/acceptance/cli` | 568 ERROR | **568 skipped / 0 error** |
| w3_live 23 文件 | 55 FAILED / 104 ERROR | **184 passed / 100 skipped / 10 failed / 20 error**（残留均在卡外自建 spawn 文件） |
| 单文件 `test_query_file_rpc.py` | — | 7 passed / 7 skipped |

全量 error 由 1,210 降至 117（−1,093 全部为卡内前置归一），failed 由 168 降至
165；无「改前通过、改后 skip」样本（降级条件均要求 daemon 已退出/不可达，
setup 无法产出 client，该类用例改前必然 ERROR）。

### 7.5 卡外残留处置（daemon/native 族已由 T-1793377643401-07119ed4 归一）

- **daemon 失联族（164）→ 已归一**：`test_rust_cli_diff`、`test_c17`、`test_c21`、
  `test_c16`、`test_http_daemon_default_transport`、`convergence/test_m3`、
  `test_http_daemon_integration`、`convergence/test_t4`、`test_task_prompt_e2e`、
  `test_workspace_snapshot_rpc_http` 等自建 spawn 文件，现统一经
  `tests/_prereq_guard.py` 把「daemon 提前退出 / manifest 后管道冲突退出」降级为
  `pytest.skip`；判定边界与接入矩阵见 §12。
- **native 构建族（72）→ 已归一**：`test_lease_gate_empirical`、
  `test_task_step_resolve_e2e`、`test_cli_task_lease_parity`、`test_lease_rpc`、
  `test_windows_daemon_e2e`、`test_windows_wsl_authority_e2e` 的 `cargo build`
  失败，命中 `LNK1181` / `sqlite3.lib` 等原生库缺失标记时降级为 skip；
  rustc 编译诊断、构建成功但产物缺失仍红。
- **未归一「其他」44**（非前置、亦非本轮引入，保持原状；**条目本身已全部收口，
  本节转为历史台账**）：i18n 输出文案断言漂移 15
  （`test_cli_055/056/057/058/059/063/065/066/070/076`、`test_i18n_cleanup_c5`）
  → **已由 §16 收口**、
  Windows 临时 SQLite 文件锁 teardown `PermissionError [WinError 32]` 13
  （`test_phase3_cas`、`test_p10_p11_timing_gc`、`test_p21_auto_ignore`）
  → **已由 §15 收口**、
  `workspace_instance_id` 契约 9（`test_task_cascade_close`、
  `test_task_reconciliation_contract`、`test_http_index_job_cutover`）
  → **已由 §17 收口**、
  release 验收 2（evidence 停在 `6bd1e9ce` < 当前 HEAD `6d45bbd`；schema_version
  60 vs 61）→ **已由 §14 收口**、`test_http_compat_read_cutover` DID NOT RAISE 2
  → **已由 §18 收口**、`test_p0b_legacy_attestation_fail_closed` 1、
  `test_phase2_6_3` 性能 1、`test_task_split_governance` 1 → **已由 §19 收口**。

后续卡建议：① 将「隔离 daemon 按 `data_root` 派生管道名」落到 Rust 侧（根治
管道冲突）（**已完成**，见 §18）；② 抽公共 `daemon_prereq_guard` 覆盖卡外自建 spawn 文件与
`cargo build`/LLM 前置（**已完成**，见 §12）；③ 修 release 验收 evidence 过期与
schema_version 断言（**已完成**，见 §14）。

---

## 8. C8 · Rust 内嵌 schema 副本与 canonical 漂移（2026-10-30）

> 任务卡：`T-1793342216372-87953754`（步骤 #0 triage / #1 对齐 / #2 验证+文档）
> 审计候选：C3（Rust 引用已删除的 `idx_calls_callee_name`）

### 8.1 现象

`rust_ext/src/storage.rs` 的 `pub const SCHEMA_INDEXES_SQL` 仍声明
`idx_calls_callee_name`，而 canonical `db/schema.py` 在 P3-4（v61）之后 calls
表只有 3 个索引。同源漂移另有两处：

| 位置 | canonical `db/schema.py` | Rust 死常量残留 |
|---|---|---|
| callee_name | `idx_calls_callee_name_raw ON calls(callee_name) WHERE callee_id = 0` | `idx_calls_callee_name ON calls(callee_name)`（全量） |
| callee_id | `idx_calls_callee_id_resolved ON calls(callee_id) WHERE callee_id > 0` | `idx_calls_callee_id ON calls(callee_id)`（全量） |
| caller_name | 无 | `idx_calls_caller_name ON calls(caller_name)` |

### 8.2 根因（为什么是隐患而非现网缺陷）

- 运行时**不使用**这两个常量：`initialize_or_migrate` 实际执行的是
  `include_str!(concat!(env!("CARGO_MANIFEST_DIR"), "/../db/schema.py"))`
  抽取的 canonical `SCHEMA_SQL`（`storage.rs` `EMBEDDED_SCHEMA_SOURCE` 与
  `tx.execute_batch(&canonical_schema_sql()?)`）。用已部署二进制初始化 fresh
  DB，calls 索引与 canonical 逐字一致 → 现网无漂移。
- 死常量 `SCHEMA_TABLES_SQL`（原 L72-348）与 `SCHEMA_INDEXES_SQL`（原 L351-397）
  是历史遗留的「全量 DDL 副本」（早期 Rust 独立建库用），现已全仓无任何引用：`rust_ext/src`
  仅定义处命中；`lib.rs` pyo3 只导出函数/类，不导出常量；`rust_ext/tests`
  无引用；Python 侧一律 `from callwarden.db.schema import ...`。
- 风险点：常量内容过期却仍可编译可复制，任何人误用/照抄即产生第二份 schema
  真相源（正是本次审计发现的形态）。

### 8.3 处置

- 删除两个死常量（-328 行），`include_str` canonical 成为 Rust 侧唯一真相源。
- 新增 `const LEGACY_CALLS_INDEXES` 与**既有库幂等收敛**：`initialize_or_migrate`
  在读取 `current_version` 与 `SCHEMA_TOO_NEW` 检查之后、版本/checksum 匹配的
  early-return **之前**，当 `current_version > 0` 且 `calls` 表存在时对
  `idx_calls_callee_name` / `idx_calls_caller_name` / `idx_calls_callee_id`
  执行 `DROP INDEX IF EXISTS`。`DROP ... IF EXISTS` 本身幂等，缺 `calls` 表
  时跳过（不影响 v0 全新库），因此该收敛对每次 open 都可安全重放。
- 新增 `#[test] test_canonical_schema_calls_indexes_single_source`：
  - 正向断言 3 个 canonical calls 索引存在且谓词正确
    （`idx_calls_caller`、`idx_calls_callee_id_resolved ... > 0`、
    `idx_calls_callee_name_raw ... = 0`）；
  - 负向断言 `idx_calls_callee_name` / `idx_calls_caller_name` /
    `idx_calls_callee_id` 三个全量索引不得回潮。
- 新增 `#[test] test_initialize_drops_legacy_duplicate_calls_indexes`：
  v61 库上手工建回 3 个旧索引（断言 count=3）→ 再次
  `initialize_or_migrate` 走「版本+checksum 已匹配」早退路径 → 断言旧索引
  count=0 且 3 个 canonical 索引仍在（覆盖卡面「既有库初始化后旧索引消失」）。

### 8.4 实测

| 项 | 结果 |
|---|---|
| `cargo test --lib storage::tests` | 13 passed / 0 failed（含 2 个新单测） |
| 新增单测 `test_canonical_schema_calls_indexes_single_source` | 1 passed |
| 新增单测 `test_initialize_drops_legacy_duplicate_calls_indexes` | 1 passed |
| 编译（`cargo test` 构建 + `refresh_shared_runtime.ps1` release） | 0 error |
| 部署 evidence | passed；`20261030-160725-6ca7d5030b32-6a55196c.json`；daemon pid 29956；`cw-daemon.exe` sha256 `2923a06b0bbdc6c372703e4cb30dbfe77838a7067d6ee7855d3c5c560a1d2d2c`；ping status ok |
| fresh DB 探针（部署后） | `idx_calls_caller` / `idx_calls_callee_id_resolved` / `idx_calls_callee_name_raw`，与 canonical 逐字一致，旧索引 0 残留 |
| 既有库收敛探针（部署后） | init 后手工建回 3 个旧索引（6 个索引）→ 再次 `storage_initialize_or_migrate` → 仅剩 3 个 canonical，旧索引清零 |
| 既有库 `~/.callwarden/callwarden.db`（v61） | 只有 3 个 canonical 索引（旧名 0 命中），收敛为防御性路径未触发 |

### 8.5 边界与残留

- **不做 schema 版本 bump**：本卡未改 canonical `db/schema.py`，checksum
  不变；既有库无需版本迁移。旧索引收敛走 open 期条件 DROP，与版本号无关。
- **旧索引 DROP 收敛已实现且幂等**：仅当某库由「切换 include_str 之前」的
  Rust 初始化才可能留有旧名索引；本机主仓库与隔离库实测均无，故收敛为
  防御性措施（`DROP INDEX IF EXISTS` 重复执行无副作用，只回收冗余空间，
  不影响正确性）。对应单测见 §8.3。
- `tests/_bench_*.py`（3 个基准脚本）仍自建 `idx_calls_callee_name`：那是
  bench 专用简化 schema，不在生产路径，未纳入本卡范围。该残留已由 C8 后续卡
  `T-1793407385525-e7d5f04c` 归一为 canonical 部分索引，见 §13。

---

## 9. C9 · Schema 版本文档门禁被弱化为仅存在性检查（2026-10-30）

> 任务卡：`T-1793352736346-e6e623a4`（步骤 #0 triage / #1 恢复门禁 / #2 验证+文档）
> 审计候选：C4

### 9.1 现象

| 用例 | 弱化后的实际行为 |
|---|---|
| `tests/test_architecture_doc.py::test_doc_schema_version_matches_code` | 只断言 `docs/architecture.md` 仍出现「当前 Schema 版本：**vNN**」字段，不与 `db/schema.py` 的 `SCHEMA_VERSION` 比对数值 |
| `tests/test_abi_contract.py::test_contract_schema_version_matches_schema_py` | 只断言 `docs/design/migration-manifest.md` 仍声明 `SCHEMA_VERSION` 字段，不比对数值 |

后果：把文档版本改成任意滞后值（如 v50 / 59-59），两条用例仍然通过，门禁
完全失去「文档滞后即红」的效力。

### 9.2 根因

两次 stale 修正（文档停留在 v50 / `RUST_SCHEMA_VERSION=59`，而代码已到 v60）
选择了「降低门禁强度」而不是「同步文档」：因当时 docs 不在可改范围，把数值
比对降级为结构性存在性检查。本轮巡检时两份文档均已对齐 v61，降级不再有
正当理由，属于「用弱化测试掩盖文档滞后」的治理债。

### 9.3 处置

- `tests/test_architecture_doc.py`：抽出 `_extract_doc_version` /
  `_assert_doc_version_matches`；`test_doc_schema_version_matches_code` 恢复
  「代码↔代码 + 文档↔代码」双重数值比对；新增负向
  `test_doc_schema_version_gate_rejects_stale_doc`（v50 与缺字段均必须失败）。
- `tests/test_abi_contract.py`：抽出 `_extract_version` /
  `_assert_manifest_versions`；`test_contract_schema_version_matches_schema_py`
  恢复 `SCHEMA_VERSION`↔`db/schema.py`、`RUST_SCHEMA_VERSION`↔
  `rust_ext/src/abi_contract.rs` 数值比对；新增负向
  `test_contract_schema_version_gate_rejects_stale_manifest`（59/59 与缺
  `RUST_SCHEMA_VERSION` 均必须失败）。

### 9.4 实测

| 项 | 结果 |
|---|---|
| 基线（修改前） | 两条用例 2 passed（文档 v61 与代码一致），但对滞后文档不敏感 |
| `pytest tests/test_abi_contract.py tests/test_architecture_doc.py` | **40 passed / 1 skipped** |
| 负向用例 | v50 / 缺字段 / 59-59 / 缺 `RUST_SCHEMA_VERSION` 均按预期抛出 `AssertionError` |
| 端到端模拟滞后（arch doc → v50，随后还原） | `FAILED ... docs/architecture.md 声明 v50 与 db/schema.py SCHEMA_VERSION=v61 不一致` |
| 端到端模拟滞后（manifest `RUST_SCHEMA_VERSION` → 59，随后还原） | `FAILED ... RUST_SCHEMA_VERSION=59 与 abi_contract.rs SCHEMA_VERSION=61 不一致` |
| 还原后 `git diff -- docs/architecture.md docs/design/migration-manifest.md` | 空（两份文档字节级未被改动） |
| `git diff --check` | 0 |

### 9.5 边界与残留

- 不新增第三处镜像门禁（`rust_ext/src/storage.rs` 的 `SCHEMA_VERSION`）：
  `abi_contract.rs` 已与 Python 权威源强绑，Rust 内部两常量由 C8 的
  `storage::tests` 与迁移路径覆盖，避免重复门禁带来的维护面。
- 若文档再次滞后，正确处置是同步文档（docs 现已进入可改范围），
  **不得**再把用例降级为仅存在性检查。

---

## 10. C10 · 权威库工作区登记污染（2026-10-30）

> 任务卡：`T-1793353773817-750277f4`（步骤 #0 triage / #1 实现+单测 / #2 部署+收敛+文档）
> 审计候选：C5

### 10.1 现象

权威库 `C:\var\lib\callwarden\registry.db` 的 `daemon_workspaces` 在收敛前：

| 指标 | 收敛前 |
|---|---|
| 总行数 | 178 |
| `status='active'` | 28 |
| `host_real_root` 归一化为 `c:/git_work/callwarden` | 11（**全部 active**） |

同一物理 checkout 上并存 11 行 active，`host_real_root` 写法混合
（`c:/git_work/callwarden` 与 `\\?\C:\git_work\callwarden`），
instance id 各异（`f47918fe7f076161` / `38c6bf0d73637f85` / … / `4baea3ff12c2ea5c` /
`822c031c71488f12`），`workspace_id` 存在二义：`workspace.register` 用
`INSERT OR REPLACE` 轮换 ROWID，历史行全部留在 active 集合里。

### 10.2 根因

`register_workspace_preferred` 收编旧身份时只把**被收编那一行**置为
active（`propagate_snapshot_to_siblings` 只传播 `snapshot_id`，不改
`status`），从不归档同 root 的其余 active 行。每次 git head 变化 / 客户端
路径写法变化都会派生新的 instance id 并新增一行，旧行永久滞留 active，
registry 只增不减。数据库侧没有唯一约束能表达「同 owner + 同归一化 root
只有一行 active」，因为 `host_real_root` 是原样字符串写入，SQL 层无法用
归一化键做约束。

### 10.3 处置

`rust_ext/src/daemon/workspace.rs`：

- 新增 `archive_alias_rows`：按 `owner_uid` 选出该 owner 下所有
  `status='active'` 且 `workspace_instance_id <> keep_instance_id` 的行，
  在 Rust 侧用 `normalize_identity_root` 比对 `host_real_root`（与
  `propagate_snapshot_to_siblings` 同一策略，规避 SQL 无法归一化的问题），
  命中者置 `status='archived'` 并刷新 `last_active_at`；archived 行保留原
  `snapshot_id` 便于审计，重复执行幂等。
- 收编分支与新建分支均在写入后调用 `archive_alias_rows`，保证「注册即收敛」，
  不再依赖运维手工清理。
- 新增单测 `test_register_archives_alias_rows_single_active_per_root`：
  同 owner 下 `alias-canonical`（`C:/tmp/cv`）与 `alias-verbatim`
  （`\\?\C:\tmp\cv`）归一化同 root、`other-root` 为不同 root；断言收编行
  active、同 root 别名行 archived、不同 root 行不受影响。

### 10.4 实测

| 项 | 结果 |
|---|---|
| `cargo test --manifest-path rust_ext/Cargo.toml --lib --no-default-features --features sqlite-bundled workspace::tests` | **150 passed / 0 failed** |
| 部署（`scripts/refresh_shared_runtime.ps1 -TaskId T-1793353773817-750277f4`） | passed，evidence `20261030-182956-b1d7965886d5-b5f1e09b.json`（旧 daemon 已停止、新实例就绪） |
| 收敛命令 | `python cw.py workspace register callwarden c:\git_work\callwarden` → `ID=1193` |
| 收敛后总行数 / active | 178 / **19**（同 root 9 行转 archived） |
| 收敛后同 root active | **2**：owner 4294967295 → `1193`（`4baea3ff12c2ea5c`）；owner 0 → `25` |
| 按 owner 不变式 | owner 4294967295：同 root 10 行 → active 1；owner 0：同 root 1 行 → active 1 |

### 10.5 边界与残留

- 归档作用域是 **owner_uid + 归一化 root**，不跨 owner 归档：`workspace_instance_id`
  本身由 `sha256("owner_uid|norm_root")[:16]` 派生，跨 owner 的同一目录是
  不同 authority，归档别人的行会使其注册失效。收敛后同 root 仍有 2 行 active
  （owner 0 的 `25` 与 owner 4294967295 的 `1193`），属预期语义而非残留。
- 归档只改 `status`，不删行：历史行保留 `snapshot_id` 与 provenance 供审计，
  `workspace list` 仍可见（无 `[活动]` 标记）。
- 未迁移存量不一致的 `snapshot_id`（依赖 C6 的迁移 registry 缺口一并处理），
  本卡只收敛 active 基数与 workspace_id 二义。

---

## 11. C6 · Schema 迁移注册表缺口（2026-10-30）

> 任务卡：`T-1793368261327-97e8e5fc`（步骤 #0 triage / #1 实现+单测 / #2 验证+文档）
> 审计候选：C6

### 11.1 现象

`db_base.CodeGraphBase._get_migrations()` 实测键为 `2..52, 59, 61`（53 条），
缺 `53/54/55/56/57/58/60`。`_migrate_schema(from, to)` 对未注册版本直接
`continue`，但仍按版本逐条写入 `schema_version` 行，最终 `MAX(version)=61`。

复现（内存库，`schema_version=52`，执行 `_migrate_schema(52, 61)`）：

| 观测项 | 修复前 |
|---|---|
| 实际执行的版本 | `52, 59, 61` |
| `MAX(version)` | 61 |
| `workspace_authority_captures` / `task_workspace_bindings`（v53） | 缺失 |
| `role_contract_lineages` / `role_contract_revisions`（v55） | 缺失 |
| `task_step_role_contract_bindings`（v56） | 缺失 |
| `verdict_normalization_rules` / `verdict_normalization_rule_revocations`（v57） | 缺失 |

即库被打标当前版本，但 v53-v57/v60 的治理表与 v57 的 20 个 provenance 列、
`task_quality_findings.severity` 全部未建——fail-open。

### 11.2 根因

1. v53-v58/v60 的迁移实现（函数）已存在，但注册表漏登记；`_migrate_schema`
   把「版本未注册」当作「无需迁移」，既不报错也不补建，随后照常插入版本行。
2. Rust storage 路径 `storage_initialize_or_migrate` 执行 canonical `SCHEMA_SQL`
   会把缺失表补上，掩盖缺口；纯 Python 路径（`callwarden_core` 缺失或
   `rust_storage_service` / `sqlite_schema_migration` 被 rollback 关闭）必现。
3. v60 的 `task_quality_findings.severity` 只有 Rust `apply_quality_findings_v60_compat`
   兜底，Python 侧无 compat 路径，纯 Python 库迁移后仍缺列。
4. 迁移函数原本手写 DDL，与 `SCHEMA_SQL` 双写，发布补表/补列时容易漏登记
   （v58 正是「v57 发布后补入 11 表 + 20 列但未 bump 版本号」的漂移产物）。

### 11.3 处置（仅 `db/db_base.py`）

- **canonical DDL 单一真相源**：新增 `canonical_table_statements()`，从
  `SCHEMA_TABLES_SQL` 按行尾 `;` 切分并用正则提取 `{表名: CREATE 语句}`（当前 88 张表，
  懒加载缓存）；迁移不再手写表 DDL。
- **幂等补建工具**：`ensure_canonical_tables(conn, *names)`（缺 canonical DDL 即
  fail-closed）、`ensure_table_columns(conn, table, columns)`（PRAGMA 探测 + ALTER，
  表不存在时跳过以兼容合成库）、`table_exists(conn, name)`。
- **v57 列常量**：`TASK_CONTRACT_V57_COLUMNS`（`task_contract_revisions` ×2 /
  `task_verdict_events` ×9 / `task_gate_decisions` ×9）统一供 `_migrate_v56_to_v57`
  与 `_ensure_compat_columns` 复用，消除两处列清单漂移。
- **新增迁移条目**：
  - `_migrate_v52_to_v53`：`workspace_authority_captures` + `task_workspace_bindings`
  - `_migrate_v53_to_v54`：`task_loop_capability_promotion_events` + 索引
  - `_migrate_v54_to_v55`：`role_contract_lineages` + `role_contract_revisions`
  - `_migrate_v55_to_v56`：`task_step_role_contract_bindings` + 2 索引
  - `_migrate_v56_to_v57`：`verdict_normalization_rules` / `..._revocations` + 20 列
  - `_migrate_v57_to_v58`：按序重放 v52-v57 幂等补建（表 + 索引 + 列），对齐 v58
    漂移修复语义
  - `_migrate_v59_to_v60`：`task_quality_findings.severity` 列/索引 + Role Worker 三表
- **Role Worker DDL 抽为语句元组**：`ROLE_WORKER_SCHEMA_STATEMENTS` 替代
  `executescript`（`executescript` 会隐式提交外层事务，破坏 `_migrate_schema`
  的单一事务与回滚语义）；`ensure_role_worker_schema` 行为不变。
- **fail-closed 守卫**：`_migrate_schema` 在循环前校验
  `range(max(from+1, 2), to+1)` 全覆盖；存在缺口即
  `RuntimeError: schema migration registry missing target versions: ...`，
  v1 视为基线不计缺口，不再 `continue` 静默跳过。

### 11.4 实测

| 项 | 结果 |
|---|---|
| 注册表键数 | 修复前 53 条（`2..52,59,61`）→ 修复后 **60 条**（`2..61` 连续，无缺口） |
| `canonical_table_statements()` | 提取 88 张表；11 张 v52-v57 治理表 DDL 全部命中 |
| v52 库 `_migrate_schema(52,61)` | `MAX(version)=61`；11 张治理表、v57 的 20 列、`task_quality_findings.severity` 全部齐备 |
| 缺口场景（去掉 v55 条目） | `RuntimeError: schema migration registry missing target versions: 55 (from_version=52 to_version=61)` |
| 幂等 | v52-v58 补建连续重放两次无报错、无重复对象 |
| 单测 | `tests/test_schema_migration_registry_gap.py` **12 passed** |
| 回归 | `test_a14_audit_fix` / `test_task_quality_gate` / `test_audit_chain` / `test_db_v2_to_v3_migration_fk` / `test_abi_contract` 共 **202 passed / 0 failed** |
| 二次回归 | `test_db_task_contracts` / `test_db_tests` / `test_task_supersede_cli` / `test_task_quality_gate` 共 **178 passed / 0 failed** |

### 11.5 边界与残留

- **不额外扩 `_ensure_compat_columns` 的表级兜底**：本卡只复用 v57 列常量，不把
  v53-v57 建表塞进每次启动的 compat 路径——否则会重新制造「第二真相源」，
  未来 schema 缺口继续被静默掩盖。已在 v61 打标却缺表的历史库由 Rust canonical
  迁移或下一次版本迁移补齐；本机权威任务库走 Rust 路径，无现网待修复库。
- **v58 不重写 checksum**：Python 侧没有 checksum 存储；stored checksum 由
  Rust storage 迁移负责（见 `docs/design/migration-manifest.md` §4.4）。
- **v57 初始 rule row 不播种**：`verdict-normalization/v1` 的初始化由 1E/daemon
  权威路径负责，canonical `SCHEMA_SQL` 同样不含该 INSERT；本卡只补 schema 形状。

---

## 12. C7 后续 · daemon/native 前置守卫（2026-10-31）

> 任务卡：`T-1793377643401-07119ed4`（步骤 #0 triage / #1 实现+单测 / #2 验证+文档）
> 承接 §7.5 卡外残留：daemon 失联族（164）/ native 构建族（72）。

### 12.1 目标与判定边界

把**明确环境前置缺失**归一为 `pytest.skip`，真实回归保持红：

| 场景 | 判定 | 处置 |
|---|---|---|
| 二进制候选缺失 | 无候选 | skip |
| daemon 在 manifest 前退出（`poll() is not None`） | 启动前置缺失 | skip（带 returncode + stderr 尾部） |
| manifest 后 daemon 因 SID 级命名管道冲突退出（`CreateNamedPipe error 5` / `10061` / `10054`） | 环境前置冲突 | skip |
| manifest 超时但进程仍存活 | 可能真实卡死 | **fail** |
| setup 抛 `DaemonUnavailableError` 且 code ∈ {`E_HTTP_DAEMON_UNAVAILABLE`, `E_HTTP_MANIFEST_STALE`} / 报文命中 markers / daemon 已退出 | 前置缺失 | skip |
| 其他异常（业务错误码、契约断言） | 真实回归 | 原样上抛 |
| `cargo build` 失败且命中 `LNK1181` / `sqlite3.lib` / `could not find native static library` / `link.exe not found` | 构建前置缺失 | skip |
| `cargo build` 失败但为 rustc 诊断（如 `error[E0308]`） | 真实回归 | **fail** |
| build 成功但产物缺失 | 构建配置异常 | **fail** |

判定来源复用 `tests/_w3_harness.py::_prereq_unavailable_reason`（别名导出
`prereq_unavailable_reason`），不引入第二套语义。

实测根因（2026-10-31）：生产 daemon 常驻占用 `\\.\pipe\callwarden-<SID>` 时，
隔离 daemon **先发布 HTTP manifest**（约 0.2s），随后
`[ERROR] server 启动失败: CreateNamedPipe 失败 (error 5)` 退出；后续 RPC 全部
`E_HTTP_DAEMON_UNAVAILABLE`（WinError 10061/10054）。此前 manifest 前守卫覆盖不到，
故新增 manifest 后管道冲突守卫。

### 12.2 守卫 API（`tests/_prereq_guard.py`）

| 函数 | 作用 |
|---|---|
| `require_cargo()` | cargo 缺失 → skip；返回路径 |
| `require_daemon_binary(bin_path)` | 二进制缺失 → skip；否则原样返回 |
| `skip_if_daemon_exited_before_manifest(proc, stderr_text="", context=...)` | manifest 前已退出 → skip；存活 → 返回 |
| `read_proc_stderr(proc, limit=4000)` | 仅在进程已退出时读取 stderr 尾部（避免活进程阻塞） |
| `skip_if_isolated_daemon_pipe_conflict(proc, settle_seconds=3.0, context=...)` | settle 窗口内退出且命中冲突 marker → skip；存活/无关崩溃 → 返回 |
| `native_build_skip_reason(build)` | build 失败且命中 native 缺失 marker → 原因；否则 None |
| `skip_if_native_build_prereq_unavailable(build)` | 命中 → skip；否则返回，由调用方 fail |
| `prereq_unavailable_reason(exc, daemon_exited)` | `_w3_harness` 同名判定别名 |

### 12.3 接入矩阵

daemon 自建 spawn 族（9 文件）：

| 文件 | 接入点 |
|---|---|
| `tests/test_rust_cli_diff.py` | spawn 前 `require_daemon_binary`；manifest None 守卫；`_publish_snapshot` 用 `prereq_unavailable_reason` |
| `tests/test_c16/c17/c21_*_workspace_authority.py` | manifest → endpoint 后、client 构造前 `skip_if_isolated_daemon_pipe_conflict` |
| `tests/test_http_daemon_default_transport.py` | 两个 `isolated_default_http_daemon` fixture 均加管道冲突守卫 |
| `tests/test_http_daemon_integration.py` | manifest None 守卫；`http_client` fixture + 自建 spawn 用例加管道冲突守卫；纯结构断言用例改为本地构造 client（不依赖活 daemon） |
| `tests/test_task_prompt_e2e.py` | 二进制缺失 skip；manifest None + 管道冲突守卫 |
| `tests/test_workspace_snapshot_rpc_http.py` | manifest None + 管道冲突守卫 |
| `tests/convergence/conftest.py` | 保留 `E_DAEMON_ALREADY_RUNNING` skip；manifest None + 管道冲突守卫（覆盖 m3/t4） |

native 构建族（6 文件）：`test_lease_rpc` / `test_lease_gate_empirical` /
`test_cli_task_lease_parity` / `test_task_step_resolve_e2e` /
`test_windows_daemon_e2e` / `test_windows_wsl_authority_e2e` 的
`ensure_fresh_binary` / `ensure_fresh_binaries`：`require_cargo()` + build 失败先
`skip_if_native_build_prereq_unavailable` 再 fail；daemon 失联分支读
`daemon.log` 尾部后 `skip_if_daemon_exited_before_manifest`，存活超时仍 fail。

### 12.4 改前/改后实测（16 文件定向，248 testcase）

| 指标 | 改前（HEAD 基线） | 改后 |
|---|---|---|
| passed | 33 | **33** |
| skipped | 15 | **215** |
| failed | 105 | **0** |
| error | 95 | **0** |
| testcase 总数 | 248 | 248（无缺失/无新增） |
| `passed → skipped`（禁止项） | — | **0** |
| `failed/error → skipped`（预期迁移） | — | **200**（failed 105 + error 95） |

按模块改前状态（改后除 `test_rust_cli_diff` 的 15 项原 skip 与本轮 33 项原 pass
外，其余全部迁移为 skip）：

| 模块 | 改前 passed/skipped/failed/error |
|---|---|
| `convergence`（m3+t4） | 0 / 0 / 2 / 12 |
| `test_c16_assignment_show_workspace_authority` | 0 / 0 / 10 / 0 |
| `test_c17_admin_route_workspace_authority` | 0 / 0 / 22 / 0 |
| `test_c21_edit_rule_route_workspace_authority` | 0 / 0 / 22 / 0 |
| `test_cli_task_lease_parity` | 0 / 0 / 0 / 12 |
| `test_http_daemon_default_transport` | 9 / 0 / 1 / 8 |
| `test_http_daemon_integration` | 3 / 0 / 7 / 0 |
| `test_lease_gate_empirical` | 0 / 0 / 0 / 20 |
| `test_lease_rpc` | 0 / 0 / 0 / 10 |
| `test_rust_cli_diff` | 15 / 15 / 35 / 0 |
| `test_task_prompt_e2e` | 0 / 0 / 0 / 6 |
| `test_task_step_resolve_e2e` | 0 / 0 / 0 / 14 |
| `test_windows_daemon_e2e` | 0 / 0 / 0 / 9 |
| `test_windows_wsl_authority_e2e` | 0 / 0 / 0 / 4 |
| `test_workspace_snapshot_rpc_http` | 6 / 0 / 6 / 0 |

矩阵门禁 `scripts/check_e2e_matrix_closure.py --json` → `"ok": true`。

### 12.5 明确不降级（反例，单测覆盖）

- 进程存活但 manifest 超时 / RPC 超时 → 返回不 skip，调用方保持 fail。
- 已退出但报文无冲突 marker（如 panic）→ 不 skip，保持红。
- `cargo build` 失败且为 rustc 诊断（`error[E0308]`）→ 不 skip，保持 fail。
- build 成功（returncode 0）→ 永不 skip。
- 业务错误码（如 `E_SHARED_TASK_WRITER_REQUIRED`）→ `prereq_unavailable_reason`
  返回 None，原样上抛。

### 12.6 单测

`tests/test_prereq_guard.py`：18 passed。覆盖 `require_cargo`（有/无）、
`require_daemon_binary`（缺失/透传）、manifest 前退出（skip 带 returncode）、
存活不 skip、native marker 参数化（LNK1181 / 静态库缺失 / link.exe 缺失）、
rustc 诊断不降级、成功构建不 skip、`prereq_unavailable_reason` 别名语义、
业务错误码保持红、管道冲突 marker 参数化（CreateNamedPipe / 10061 / 10054）、
存活 daemon 不 skip、无关崩溃不 skip。

### 12.7 残留

- 管道冲突的**根治**仍在 Rust 侧：隔离 daemon 应按 `data_root` 派生管道名，
  避免与生产 daemon 争用 SID 级 `\\.\pipe\callwarden-<SID>`（见 §7.5 后续卡建议①）。
- 本卡守卫只降级「明确前置缺失」，不改变 `cargo build` 次数与耗时结构。
- §7.5「其他 44」保持原状，不在本卡范围。

---

## 13. C8 后续 · 基准脚本 calls 索引收敛（2026-10-31）

> 任务卡：`T-1793407385525-e7d5f04c`（步骤 #0 triage / #1 实现 / #2 验证+文档）
> 承接 §8.5 残留：`tests/_bench_*.py` 自建已废弃全量 calls 索引

### 13.1 现象

三个基准脚本各自内嵌一份简化 schema，其中 calls 索引仍为已废弃的全量形态，
与 canonical `db/schema.py`（v61）不一致，构成第二份 schema 真相源：

| 脚本 | 改前 | 改后（canonical v61） |
|---|---|---|
| `tests/_bench_insert_strategies.py` | `idx_calls_callee_id ON calls(callee_id)`、`idx_calls_callee_name ON calls(callee_name)` | `idx_calls_callee_id_resolved ... WHERE callee_id > 0`、`idx_calls_callee_name_raw ... WHERE callee_id = 0` |
| `tests/_bench_graph_store_large.py` | 同上 | 同上 |
| `tests/_bench_10m_progressive.py` | 同上 | 同上 |

三脚本均保留 `idx_calls_caller ON calls(caller_id)`。

### 13.2 处置

- 三脚本的 calls 索引 DDL 逐字替换为 canonical 两条部分索引，并在脚本内注明
  「全量索引已在 v33/v61 废弃，基准脚本不得保留第二份真相源」。
- **不改基准口径**：合成数据规模/seed、查询实现与度量方式保持不变；
  `_bench_graph_store_large.py` 的 `WHERE c.callee_name = ?` SQL 基线保留，
  用于对照生产短名降级路径（有意全表扫描）与 GraphStore 路径的差异。
- `tests/test_p7_callee_id_index.py` 新增防回潮门禁（该文件已在验收矩阵内，
  不新增测试文件）：
  - `test_bench_schema_mirrors_canonical_calls_indexes`（参数化 3 脚本）：
    禁止 `idx_calls_callee_id` / `idx_calls_callee_name` 全量索引名回潮，
    且必须逐字包含两条 canonical 部分索引。
  - `test_raw_callee_query_uses_partial_text_index`：
    `callee_name = ? AND callee_id = 0` 必须命中 `idx_calls_callee_name_raw`。

### 13.3 实测

| 项 | 结果 |
|---|---|
| `C:/Python314/python.exe -m pytest tests/test_p7_callee_id_index.py -q` | 8 passed |
| 三脚本冒烟（2000 符号合成库） | calls 索引 = `idx_calls_callee_id_resolved` / `idx_calls_callee_name_raw` / `idx_calls_caller`，旧名 0 命中 |
| raw 边查询计划 | `SEARCH calls USING COVERING INDEX idx_calls_callee_name_raw (callee_name=?)` |
| resolved 边查询计划 | `SEARCH calls USING COVERING INDEX idx_calls_callee_id_resolved (callee_id=?)` |

### 13.4 边界与残留

- 基准脚本仍是**简化 schema**（只含基准所需表/列），本卡只收敛 calls 索引 DDL，
  不做与 canonical 的全量 schema 对齐。
- 不改变基准的合成数据规模、随机种子、查询与度量口径，历史基线可比。
- 生产 `get_callers` 的短名降级 SQL 为 `WHERE c.callee_name = ?`（不带
  `callee_id = 0`，有意全表扫描），本卡未改其查询口径，只在门禁中固定
  raw 边（`callee_id = 0`）走部分索引的事实。

---

## 14. release 验收门禁过期修复（2026-10-31）

> 任务卡：`T-1793414579172-ce935138`（步骤 #0 triage / #1 implement / #2 verify-doc）
> 承接 §7.5「其他 44」中的 release 验收 2（`tests/test_http_daemon_release_acceptance.py`）

### 14.1 现象（改前基线）

`pytest tests/test_http_daemon_release_acceptance.py -q` → **8 passed / 2 failed / 4 error**：

| 用例 | 症状 | 根因 |
|---|---|---|
| `test_latest_refresh_evidence_passed_for_current_head` | FAILED：evidence `git_head=b1d7965` != HEAD `252a520` | runtime 未按当前 HEAD 刷新；原断言把「部署证据过期」当代码回归直接红 |
| `test_production_daemon_health_ok` | FAILED：`schema_version=61` != 硬编码 60 | canonical 双源（`db/schema.py`、`rust_ext/src/daemon/mod.rs`）已 bump 到 v61，测试字面量未跟进 |
| `TestHttpBootstrapSmoke` 4 用例 | ERROR：manifest 已发布后 daemon 退出，`ping` 报 `WinError 10054` | 生产 daemon 占用 SID 级 `\\.\pipe\callwarden-<SID>`，隔离 daemon 退出；该文件未接入 §12 前置守卫 |

### 14.2 处置（仅 `tests/test_http_daemon_release_acceptance.py`）

- `_canonical_schema_version()`：从 `db/schema.py` 与 `rust_ext/src/daemon/mod.rs` 双源
  提取 `SCHEMA_VERSION`，两者不一致即 fail；`/health` 与隔离 manifest 的断言对比该
  派生值，不再硬编码字面量（v61 及后续 bump 自动跟随）。
- evidence 过期：`status != passed` 仍 fail（真实部署失败）；`status=passed` 但
  `git_head != HEAD` 时降级为 `pytest.skip` 并提示执行
  `scripts/refresh_shared_runtime.ps1`（部署环境前置，非代码回归）。
- 隔离 daemon：接入 §12 的 `skip_if_daemon_exited_before_manifest`（manifest 前退出）
  与 `skip_if_isolated_daemon_pipe_conflict`（manifest 后因管道冲突退出）；daemon 存活
  或无冲突标记时守卫原样返回，真实失败保持红。

### 14.3 实测

| 检查 | 结果 |
|---|---|
| `pytest tests/test_http_daemon_release_acceptance.py -q` | **8 passed / 5 skipped / 0 failed**（改前 2F+4E） |
| `pytest tests/test_prereq_guard.py -q` | 18 passed |
| schema_version 断言 | `/health` 实际 61 == `db/schema.py` 61 == `rust daemon/mod.rs` 61 |
| evidence 过期用例 | skip 并给出 `b1d7965 → HEAD` 诊断，不再误报回归 |
| 隔离 daemon 4 用例 | skip（命中 `WinError 10054` 管道冲突标记） |

### 14.4 边界与残留

- 本卡不改验收语义：`status != passed`、daemon 存活但 RPC 失败、hash 不一致等仍是红。
- evidence 过期降级为 skip 后，本地未刷新 runtime 时不提供 hash 级发布证明；需要真实
  发布证明时执行 `scripts/refresh_shared_runtime.ps1` 再跑。
- 隔离 daemon 管道冲突的**根治**（按 `data_root` 派生管道名）仍在 Rust 侧，见 §7.5
  建议①/§12.7。
- CI 无 refresh evidence 目录，evidence 用例维持既有 skip 语义。

---

## 15. Windows 临时 SQLite 文件锁 teardown 修复（2026-10-31）

> 任务卡：`T-1793420962901-227d0180`（步骤 #0 triage / #1 implement / #2 verify-doc）
> 承接 §7.5「其他 44」中的 Windows 临时 SQLite 文件锁 teardown 13

### 15.1 现象（改前基线）

`pytest tests/test_phase3_cas.py tests/test_p10_p11_timing_gc.py tests/test_p21_auto_ignore.py -q`
→ **5 FAILED + 8 ERROR**，全部为 `PermissionError [WinError 32]`：

| 文件 | 症状 | 触发点 |
|---|---|---|
| `test_phase3_cas` | 8 ERROR | fixture 末尾 `os.unlink(db_path)` |
| `test_p10_p11_timing_gc` | 3 FAILED | `TemporaryDirectory.__exit__` 清理 `cw.db` |
| `test_p21_auto_ignore` | 2 FAILED | `TemporaryDirectory.__exit__` 清理 `callwarden.db` |

### 15.2 根因

进程内 Rust ext（`callwarden_core`）的 `crate::db_conn::with_db_conn` 会为该 `.db`
保留一条常驻哨兵连接（`rust_ext/src/db_conn.rs` 的
`static SENTINELS: OnceLock<Mutex<HashMap<PathBuf, Connection>>>`）。Python fixture 的
`conn.close()` 只释放 Python 侧句柄，Rust 哨兵仍持有同一 `.db` 句柄；Windows 的 unlink
要求文件无任何打开句柄，故 teardown 必然 `WinError 32`。

该哨兵是 v3 的 POSIX 同进程「Python 长连接 + Rust 短连接」双 SQLite 实例 `-shm`
撕裂防护（macOS 撕裂面已由 CI 实证）；Windows 按文件句柄加锁，天然免疫该撕裂。

### 15.3 处置（仅 `tests/conftest.py`）

`sys.platform == "win32"` 时在 conftest 导入期执行
`os.environ.setdefault("CW_RUST_SENTINEL_CONN", "0")`：进程级关闭哨兵，释放临时库
句柄；`setdefault` 保证调用方显式设置（含 `=1`）优先。不改产品代码、不改 Rust 侧
默认行为、不降级任何断言。

### 15.4 实测

| 检查 | 改前 | 改后 |
|---|---|---|
| 三文件（phase3_cas / p10_p11_timing_gc / p21_auto_ignore） | 5 FAILED / 8 ERROR | **38 passed** |
| 相邻 DB 回归（`test_migration_manifest` + `test_p7_callee_id_index`） | — | 23 passed / 1 skipped |

conftest 为进程级生效，后续同类「临时库 teardown」用例一并受益，无需逐文件改动。

### 15.5 边界与残留

- 仅 Windows 生效；POSIX 继续启用哨兵，避免重新引入 `-shm` 撕裂面。
- 显式设置 `CW_RUST_SENTINEL_CONN=1` 的调用方仍可覆盖（`setdefault` 语义）。
- 若需要在 Windows 上验证哨兵语义本身，应以独立子进程显式开启，不能依赖主进程默认。
- `CW_RUST_SENTINEL_CONN` 是 `db_conn.rs` 已文档化的既有诊断门，本卡仅接入测试进程。

---

## 16. i18n 输出文案断言漂移修复（2026-10-31）

> 任务卡：`T-1793425590620-9c0c9414`（步骤 #0 triage / #1 implement / #2 verify-doc）
> 承接 §7.5「其他 44」中的 i18n 输出文案断言漂移 15

### 16.1 现象（改前基线）

11 个测试文件（`test_cli_055/056/057/058/059/063/065/066/070/076_http_rpc.py`、
`test_i18n_cleanup_c5.py`）在本机（`DEFAULT_LANG=zh_CN`）**15 FAILED**：断言写的是
英文文案（`no repeated findings above threshold`、`Inserted marker block`、
`Dry-Run`、`Applied`、`Dry-run Preview`、`Synced`、`up to date`、`no Git change
records`、`no related tests found`、`no matching findings`、`Linked symbol change
records: 1`、`no in_progress task found`、`[OK]`/`[MISS]`），而 CLI 实际输出为
zh_CN 译文。

### 16.2 根因

`callwarden/i18n/__init__.py` 的 `DEFAULT_LANG = _detect_system_language()`（优先
`CALLWARDEN_LANG`，其次系统 locale）。这些用例当初在英文环境写成英文断言，未固定
语言，因此在 zh_CN 宿主机上随 locale 漂移；被测逻辑（RPC 路由/参数透传/结构不变量）
本身没有变化，属**测试期望的 locale 依赖**，不是产品缺陷。

### 16.3 处置

- 10 个 `test_cli_0xx_http_rpc.py` 模块新增 autouse fixture `_en_us_locale`：
  `get_language()` 存档 → `set_language("en_US")` → yield → 还原。断言保持原英文
  文案，且不再受宿主机 locale 或 `CALLWARDEN_LANG` 影响（与 `test_cli_081/082/083`
  显式 `set_language("zh_CN")` 的既有约定同构）。
- `test_i18n_cleanup_c5.py::test_install_check_output_unchanged` 为**子进程**用例，
  进程内 fixture 无效，改为给 `subprocess.run` 传
  `env=dict(os.environ, CALLWARDEN_LANG="en_US")`，使 `[OK]`/`[MISS]` 标签断言重新
  成立（该用例本意即验证标签格式不变、中英文均可用）。

未改任何产品代码、未改 i18n 资源、未放宽断言语义。

### 16.4 实测

| 检查 | 改前 | 改后 |
|---|---|---|
| 11 个文件（`CALLWARDEN_LANG` 未设置，宿主 zh_CN） | 15 FAILED | **70 passed** |
| 同上 5 文件子集，显式 `CALLWARDEN_LANG=zh_CN` | — | **57 passed**（证明 locale 无关） |

### 16.5 边界

- fixture 为模块级自动生效、结束即还原，不泄漏到其它测试模块。
- 仅固定语言，不改被测行为；如需验证 zh_CN 文案，应另写显式 `zh_CN` 用例。

---

## 17. workspace_instance_id 契约陈旧调用修复（2026-10-31）

> 任务卡：`T-1793430194466-869d3a50`（步骤 #0 triage / #1 implement / #2 verify-doc）
> 承接 §7.5「其他 44」中的 `workspace_instance_id` 契约 9

### 17.1 现象（改前基线）

`pytest tests/test_task_cascade_close.py tests/test_task_reconciliation_contract.py
tests/test_http_index_job_cutover.py -q` → **9 FAILED**（15 passed / 3 skipped）：

| 文件 | 失败数 | 错误 |
|---|---|---|
| `test_task_cascade_close.py` | 4 | `E_TASK_WORKSPACE_INSTANCE_REQUIRED: task.create 必须显式传入非空 workspace_instance_id；禁止空实例合成 ws-{id}` |
| `test_task_reconciliation_contract.py` | 4 | `invalid_params: workspace.status 需要 workspace_instance_id 或 workspace_id` |
| `test_http_index_job_cutover.py` | 1（`[tools_rules]`） | `ValueError: 缺少 workspace 定位参数：请提供 workspace_instance_id（推荐）或 deprecated workspace_id` |

### 17.2 根因

daemon 侧的 workspace 定位契约已收紧为「显式且权威」，三处陈旧调用未跟进：

1. `task.create` 拒绝空 `workspace_instance_id`（`task_loop/create.rs` fail-closed，
   禁止合成 `ws-{id}`），而 `test_task_cascade_close.make_tree` 只传
   `workspace_id: 1`；同一请求还残留已退役字段 `creator`（parent-aware 请求的
   frozen contract 白名单不含该字段，报 `E_TASK_CREATE_UNKNOWN_FIELD`）。
2. `workspace.status` 不再接受空参数（必须给 `workspace_instance_id` 或
   `workspace_id`），而 `_make_neg_task` 以 `{}` 探测实例；
   `_reconcile_apply` 还硬编码合成实例 `ws-1`。
3. `build_context` 族（P3-2）把 workspace 定位改为可选参数并在两者缺省时
   `_build_context_scope` fail-closed，而测试的 `_make_call_args` 只填必填参数，
   于是薄壳路由用例在路由前就被定位校验挡回。

三处均属**测试期望未跟进契约收紧**；被测行为（级联收口、reconcile 负向门禁、
薄壳路由）没有变化，不是产品缺陷。

### 17.3 处置（仅测试 + 台账）

- `test_task_cascade_close.py`：新增模块级 `ws_instance` fixture，经
  `workspace.status {"workspace_id": 1}` 取 `task_db_instance_id`（不硬编码）；
  所有 `task.create` 显式带该实例，同时删除 parent-aware 请求中的退役字段
  `creator`。
- `test_task_reconciliation_contract.py`：`workspace.status` 显式传
  `{"workspace_id": 1}`；`_make_neg_task` 返回 `(task_id, instance)`，
  `_reconcile_apply` 改用真实实例（不再写 `ws-1`）。
- `test_http_index_job_cutover.py`：`_make_call_args` 对签名中存在
  `workspace_id` 且未被必填逻辑填充的工具补正数 `workspace_id=1`，使路由断言
  不被 fail-closed 定位校验干扰（`_route` 调用次数仍为「每工具恰好一次」）。

未改任何产品代码、未改 Rust 行为、未放宽断言语义。

### 17.4 实测

| 检查 | 改前 | 改后 |
|---|---|---|
| 三文件 | 9 FAILED（15 passed / 3 skipped） | **24 passed / 3 skipped** |

3 个 skip 为 `TestRealDaemonIndexJobRpcAlignment` 的 `w3_live` 隔离 daemon 用例
（未启动隔离 daemon 时按既有前置 guard 跳过），改前同样 skip。

### 17.5 边界

- 实例 ID 一律经 daemon `workspace.status` 解析，不在测试里硬编码（ROWID/实例
  会随注册轮换）。
- `test_http_index_job_cutover` 的补参仅用于薄壳路由断言；真实 workspace 绑定
  语义由 `test_build_read_rpc_http.py` 等专项用例覆盖。
- `creator` 字段仅在 parent-aware 请求被 frozen contract 拒绝；无父任务的
  `task.create` 仍容忍该字段，故 reconcile 用例未改动该项。

---

## 18. 隔离 daemon 管道名按 data_root 派生，COMPAT live 2 收口（2026-10-31）

> 任务卡：`T-1793432943129-7fb83604`（步骤 #0 triage / #1 implement / #2 verify-doc）
> 承接 §7.5「其他 44」中的 `test_http_compat_read_cutover` DID NOT RAISE 2

### 18.1 现象（改前基线）

`pytest tests/test_http_compat_read_cutover.py -q` → **7 passed / 3 skipped**。3 条
`w3_live` 用例（`test_compat_registered_method_has_route`、
`test_summary_pseudo_route_returns_method_not_found`、
`test_semantic_pseudo_route_returns_method_not_found`）被 `tests/conftest.py` 的
`w3_live` 前置 guard 跳过，skip 文案为「未找到隔离 `cw-daemon` 二进制」。

在主 daemon 上直接探测契约：`query.stats_top_files` 正常返回（count=10）；
`summary.repo_map`、`semantic.gc_audit_list` 均返回 `method_not_found`。即台账记录的
「DID NOT RAISE 2」在当前二进制上已不成立，剩余问题只是 live 用例跑不起来。

### 18.2 根因

Windows 命名管道名默认按当前用户 SID 派生（`\\.\pipe\callwarden-<sid>`）。常驻
daemon 已占用该名，隔离 daemon 启动时 `CreateNamedPipe` 返回
`ERROR_ACCESS_DENIED (error 5)` 后立即退出；`spawn_isolated_daemon` 未捕获退出原因，
`find_daemon_binary()` 也拿不到有效二进制，于是上层把「管道冲突」误报为
「未找到隔离 cw-daemon 二进制」。`TransportConfig` 此前没有可覆盖端点的字段。

### 18.3 处置

- `rust_ext/src/daemon/transport.rs`：`TransportConfig` 新增 `pipe_name:
  Option<String>`，`Default` 为 `None`。None 时行为完全不变（按 owner SID 派生）。
- `rust_ext/src/daemon/transport_windows.rs`：新增纯函数
  `resolve_pipe_name(override_name, owner_sid)`（仅接受 `\\.\pipe\` 前缀，否则回落
  SID），`NamedPipeListener::bind` 改用它；补 `test_resolve_pipe_name_override`、
  `test_resolve_pipe_name_falls_back_to_sid` 两个单测。SDDL 仍只授权 owner SID，
  覆盖名不改变访问控制。
- `rust_ext/src/daemon/server.rs`：Windows `ServerConfig` 增加 `pipe_name` 并透传到
  `TransportConfig`。
- `rust_ext/src/bin/cw_daemon.rs`：Windows serve 仅当 `config.socket_path` 形如
  `\\.\pipe\...` 时把它作为覆盖名，生产默认路径（按 SID 派生）不受影响。
  Unix 侧 `ServerConfig` 无该字段，行为不变。
- `tests/_w3_harness.py`：Windows 下 `CW_DAEMON_SOCKET` 改为
  `\\.\pipe\callwarden-iso-<sha256(data_root)[:12]>`，端点由隔离目录唯一派生；
  Unix 仍用 `data_root/pipe` 文件路径。

未改任何断言语义，未跳过任何用例。

### 18.4 实测

| 检查 | 改前 | 改后 |
|---|---|---|
| `test_http_compat_read_cutover.py` | 7 passed / 3 skipped | **10 passed / 0 skipped** |
| `cargo test --lib resolve_pipe_name` | — | **2 passed** |
| 相邻 live 回归 `test_http_index_job_cutover` + `test_mcp_ask_codebase_http_rpc` | — | **15 passed** |
| 相邻 live 回归 `test_http_native_read_cutover` + `test_mcp_compat_tools_summary/security` | — | **73 passed** |

### 18.5 边界

- 覆盖仅在调用方显式提供 `\\.\pipe\` 前缀名时生效；生产 `cw-daemon serve` 默认
  仍按 SID 派生，无需改配置。
- 本次只重建 debug 产物供测试使用；release 产物与 `runtime/current` 未切换——
  默认行为零变化，不影响已部署 daemon，故不触发 `refresh_shared_runtime.ps1`。
- SDDL/DACL 逻辑未动：隔离端点仍只允许当前用户 SID 连接。

---

## 19. §7.5 剩余三条收口（2026-10-31）

> 任务卡：`T-1793439550307-da3b54dc`（步骤 #0 triage / #1 implement / #2 verify_doc）
> 承接 §7.5「其他 44」最后三条：`test_task_split_governance` 1、
> `test_phase2_6_3` 性能 1、`test_p0b_legacy_attestation_fail_closed` 1。

### 19.1 现象

| 用例 | 改前 | 说明 |
|---|---|---|
| `test_p0b_legacy_attestation_fail_closed.py` | **3 passed** | 台账记的「1」已失效，无需改动 |
| `test_task_split_governance.py::test_split_rollback_on_unbound_parent` | **1 FAILED** | `AssertionError: E_TASK_BINDING_ATTESTATION_REQUIRED ...` |
| `test_phase2_6_3_batch_register_diff.py::TestPerformanceDiff::test_performance_rust_faster_than_python` | **1 FAILED** | `Python=2.0ms, Rust=29.6ms`（`t_rust < t_py*1.5`） |

### 19.2 根因

- **split 错误码**：daemon 的 legacy workspace binding 门（`dispatch.rs`）
  已把拒绝码从 `E_TASK_BINDING_REQUIRED` 演进为
  `E_TASK_BINDING_ATTESTATION_REQUIRED`（文案附带
  `cw task attest-legacy-workspace-binding` 指引）。拒绝语义（父任务无
  canonical binding 即拒绝、不落半成品子任务）未变，测试断言未跟上契约。
- **perf 口径**：测试 docstring 假定「Rust 侧是 c53da76 之后的进程级连接池」，
  但池化已被 a80ca63（WAL hazard 修法）整体移除——Rust 侧自那时起
  **每次调用一律自建短连接、返回即关闭**（`rust_ext/src/db_conn.rs::with_db_conn`）。
  测试仍把 Python 长活连接置于计时窗口外，只把 Rust 的完整调用放进窗口，
  等于拿「已建好的连接」对「建连 + 注册 + 关连」。
  诊断实测：把同一 `open + PRAGMA + BEGIN + 1 文件 + COMMIT + close` 序列用
  Python `sqlite3` 复刻需 24–44ms，Rust 约 22ms ⇒ 约 20ms/次是**建连固定成本**，
  与 Rust 实现无关；且该成本与文件数几乎无关（1 / 10 / 50 / 200 文件同量级），
  诊断门 `CW_RUST_OPEN_CHECKPOINT=0` / `CW_RUST_SENTINEL_CONN=0` /
  `CW_RUST_WAL_DRAIN=off` 均无法消除。
- **p0b**：用例已随早前修复转绿，台账条目陈旧。

### 19.3 处置

- `tests/test_task_split_governance.py`：断言改为
  `E_TASK_BINDING_ATTESTATION_REQUIRED`，保留「幽灵父任务不产生子任务」断言；
  模块文档与用例 docstring 同步记录错误码演进。
- `tests/test_phase2_6_3_batch_register_diff.py`：性能用例改为**对等口径**——
  两侧都测「一次批量注册调用」的完整生命周期（建连 / 注册 / 提交 / 关连全部
  计入窗口），两侧都从干净的 WAL 库开始；docstring 第 4 条按现状重写。
  `_PERF_ROUNDS` 5 → 9（对等口径下两侧都被 ~20ms 建连固定成本主导，单轮抖动
  可达 22–44ms，9 轮中位数抗 1–2 个离群轮）。
  **断言阈值不变**（`t_rust < t_py * 1.5`），未加 skip、未加平台分支。
- `docs/design/cli-mcp-matrix-backlog.md`：§7.5 三条标记「已由 §19 收口」，
  本节转历史台账。
- 未改任何产品代码（Rust/Python 实现零改动）。

### 19.4 实测

| 检查 | 改前 | 改后 |
|---|---|---|
| `test_task_split_governance.py` + `test_phase2_6_3_batch_register_diff.py` | 1 FAILED + 1 FAILED | **27 passed** |
| `TestPerformanceDiff`（9 轮，本轮实测） | `Python=2.0ms / Rust=29.6ms`（0.07x） | `Python=26.2ms / Rust=25.2ms`（**1.04x**，阈值 1.5x） |
| `TestPerformanceDiff` 前序 5 轮样本 | — | 0.94x / 1.07x |
| `test_p0b_legacy_attestation_fail_closed.py` | 3 passed | **3 passed**（无改动） |

### 19.5 边界

- 对等口径下该用例的主信号是「一次调用的建连 + 批处理」，边际每文件成本
  仅个位数微秒（诊断：Rust 9.1µs/file vs Python 13.6µs/file），落在计时噪声内，
  故**不以边际值作断言**（会引入不稳定断言）。
- 契约 §6.2 的绝对预算（460 文件 < 0.10s）仍成立：本机 200 文件一次调用
  约 25–30ms（其中 ~20ms 为建连固定成本，生产每轮 refresh 只发生一次）。
- 短连接是 a80ca63 起的**产品级防御**（同进程双 SQLite 实例 WAL-index
  hazard），不为跑快测试而回退。
- `test_p0b_legacy_attestation_fail_closed.py` 单跑 3 passed；
  但「先跑 `test_task_split_governance.py` 再跑它」会让
  `test_client_never_falls_back_local_without_daemon` 失败——
  `HttpDaemonRpcClient` 单例已被前序用例创建，monkeypatch 的非法端点和
  `CW_DAEMON_MODE` 不生效，请求打到常驻 daemon 后返回
  `E_LEGACY_BIND_TASK_NOT_FOUND`。该现象在**本卡改动前**（HEAD 版本
  `test_task_split_governance.py`）逐字复现，属既有测试隔离/单例污染问题。
  → **已由 T-1793453210416-58299ee4 收口**（见 19.6）。

### 19.6 p0b 组合跑单例污染收口（T-1793453210416-58299ee4）

**现象**：三文件同进程组合跑（`test_task_split_governance.py` →
`test_phase2_6_3_batch_register_diff.py` → `test_p0b_legacy_attestation_fail_closed.py`），
后者 `test_client_never_falls_back_local_without_daemon` 失败：期望 `E_HTTP_*`
fail-closed，实际 `E_LEGACY_BIND_TASK_NOT_FOUND`。

**根因（栈取证）**：`UnixDaemonRpcClient.call` → `daemon_protocol.send_message`
→ `_is_rust_protocol_rolled_back` → `_mcp_common._call_daemon_rpc` →
`get_daemon_client()`（`server/daemon_client.py:2160`）。HTTP transport 开启时
该函数返回 `HttpDaemonRpcClient.get_instance()`，实例随后 `discover()` 解析并
缓存 `_resolved_endpoint`（discover 幂等）。p0b 的 autouse fixture 原先只在
teardown reset，故本用例 `get_instance()` 拿到的是前序模块创建的已解析实例，
monkeypatch 的 `CW_DAEMON_HTTP_ENDPOINT=http://127.0.0.1:1` 不生效，请求打到
真实 daemon。属**测试隔离缺陷**，非产品缺陷（单例与 discover 幂等均为设计）。

**处置**：`tests/test_p0b_legacy_attestation_fail_closed.py` 的
`_reset_http_singleton` 改为 setup + teardown 双向 `reset_instance()`；
docstring 记录创建链。断言、产品代码、skip/xfail 均未改动。

**实测**：三文件组合跑 30 passed（改前 1 FAILED）；p0b 单跑 3 passed。

## 20. MCP 工具内部 i18n 改造（2026-10-31）

> 任务卡：`T-1793458835218-f8812188`（步骤 #0 triage / #1 implement / #2 verify_doc）
> 承接 `docs/architecture.md` 阶段 3 首项「MCP 工具内部 i18n 改造」：
> MCP 工具的错误消息/提示统一走 `i18n.t()`，与 CLI 输出一致。

### 20.1 范围与边界

AST 盘点 6 个 MCP 运行时模块的非 docstring 中文字面量，收敛为 **18 条逻辑消息**
（24 个字符串常量，f-string 分片会拆成多常量），全部改走 `i18n.t()`。
**排除项（不动）**：模块/函数 docstring、`@mcp.tool()` 描述文本、
`server/tools/_categories.py` 的分类元数据（供文档生成）、2 处裸 section-marker。

### 20.2 新增 i18n key（`mcp.messages.*`，17 个）

| key | 落点 | 原消息 |
|---|---|---|
| `daemon_http_autostart_ok` | `server/mcp_server.py` | `[Daemon] 启动期自动拉起 HTTP daemon 成功 @ {endpoint}` |
| `daemon_http_autostart_failed` | `server/mcp_server.py` | `[Daemon] 启动期自动拉起失败: {error}` |
| `daemon_startup_wakeup_failed` | `server/mcp_server.py` | `[Daemon] 启动期唤起失败，首次 RPC 将继续重试` |
| `daemon_startup_probe_failed` | `server/mcp_server.py` | `[Daemon] 启动期探针失败，首次 RPC 将继续重试: {error}` |
| `daemon_rpc_failed` | `server/_mcp_common.py` | `{code}: daemon RPC 调用失败 ({method}): {error}` |
| `active_workspace_required` | `server/_mcp_common.py` | `没有可用的 active workspace，拒绝复用数据库上下文（fail-closed）；请显式传入 workspace 路径/名称` |
| `collab_query_failed` | `server/tools/tools_collab.py` | `只读协同查询失败 ({tool}): {detail}` |
| `collab_json_array_required` | `server/tools/tools_collab.py` | `{field} 必须是 JSON array`（原 2 处同文案） |
| `dep_requires_artifact_missing_target` | `server/tools/tools_dependency_graph.py` | `requires_artifact 依赖缺少 target_task_id (task={task}, ref={ref})` |
| `dep_interface_no_provider` | `server/tools/tools_dependency_graph.py` | `requires_interface '{ref}' 无匹配 provider (task={task})` |
| `dep_interface_provider_conflict` | `server/tools/tools_dependency_graph.py` | `interface '{interface}' 有多个 provider {providers} 但无 Planner 显式选择 (consumer={consumer})` |
| `dep_cycle_detected` | `server/tools/tools_dependency_graph.py` | `硬依赖图存在环: {path}` |
| `workspace_locator_required` | `server/tools/tools_rules.py` | `缺少 workspace 定位参数：请提供 workspace_instance_id（推荐）或 deprecated workspace_id` |
| `workspace_id_unresolved` | `server/tools/tools_rules.py` | `无法从 workspace_instance_id={instance} 解析权威 workspace_id（fail-closed）` |
| `task_workspace_root_unresolved` | `server/tools/tools_task.py` | `无法解析当前项目 workspace 归属（root={root}）；请先 \`cw workspace register --root {root}\`。原因: {error}` |
| `task_workspace_id_mismatch` | `server/tools/tools_task.py` | `workspace_id={workspace_id} 不属于本 MCP 实例所在项目（当前项目 canonical workspace_id={ctx_ws}）；拒绝跨项目创建任务单` |
| `task_workspace_instance_mismatch` | `server/tools/tools_task.py` | `workspace_instance_id 不属于本 MCP 实例所在项目（当前项目 canonical instance={ctx_inst}）；拒绝跨项目创建任务单` |

### 20.3 处置要点

- `i18n/zh_CN.json` / `i18n/en_US.json` 新增顶层 `mcp.messages` 分支，
  **zh 值与旧文案逐字一致**（`DEFAULT_LANG=zh_CN` 下行为零变化），en 补译。
- 占位符沿用 `t()` 的 `**kwargs`（`{name}` 形式），不引入新格式化机制。
- 稳定错误码（`E_*`）与 `message` 文案解耦：改文案不改错误码。
- 未改工具签名、工具描述、`_categories.py`、既有 i18n key。

### 20.4 验证

- AST 复扫：转换后 6 文件仅剩 2 处裸 section-marker，非 docstring 中文运行时字面量归零。
- 逐字等价回归：17/17 key 的新 `t()` 输出与旧 f-string 拼接结果全等。
- catalog：两套 JSON 均可 `json.load`，`mcp.messages` key 集合一致、值非空。
- 新增门禁 `tests/test_mcp_i18n_messages.py`（9 passed）：
  转换模块无运行时中文 + 两套 catalog 齐备 + 两种语言均可解析出非 key 文本。
- 相关既有测试全绿：`test_task_verdict_mcp.py` + `test_tools_task_create_workspace_params.py`
  9 passed；依赖图/build_context 相关 5 个文件 78 passed。

---

## 21. 文档深度完善（阶段 3 第二项）· 首批章节四段式 + 防漂移门禁

### 21.1 现象

`docs/cli_reference.md` 的 21 大类章节写法不统一：多数 subcommand 只有用法块与
散文式说明，少数带参数表；`**退出码**` 与 `**示例**` 并非每节都有。直接后果：

- 参数表与实际 argparse 选项漂移（实测 `task capture-diff` 章节写了不存在的
  `--apply`，同时漏掉真实存在的 `--skip-quality-review`）；
- 用户无法从文档判断某参数是否必填；
- 无法用测试锁定，后续改动继续发散。

### 21.2 范围（首批 8 个前缀分类）

| 前缀 | canonical 命令数 | 说明 |
| --- | --- | --- |
| `task` | 26 | 含 `task list` / `task show` 另有 1 个合并章节（质量门禁章） |
| `build-context` | 8 | register/list/show/activate/delete/import-compile-commands/resolve/edges |
| `workspace` | 5 | list/register/set/delete/generate-ignore |
| `query` / `search` / `file` | 3 | 单 token 命令 |
| `status` / `stats` | 2 | 单 token 命令 |
| **合计** | **44** | 文档中 45 个 `###` 章节（`task list` 合并章节重复 1 次） |

四段式模板（每章节顺序固定）：

1. 用法：`bash` 代码块（可复制命令）；
2. `**参数**` + `| 参数 | 必填 | 说明 |` 表格（说明取 `cw <cmd> --help` 原文）；
3. `**退出码**` + 表格，固定三行：`0` 成功 / `1` 运行时失败 / `2` argparse 用法错误；
4. `**示例**` + `bash` 代码块。

必填判定以 `--help` usage 行的方括号深度为准（位置参数在 `[...]` 外 → 必填；
`[--x]` → 否）。无参命令保留 `| — | — | 无参数 |` 占位行，避免空表。

### 21.3 处置

- `docs/cli_reference.md`：批量补齐 45 个章节三段缺失内容（+1104 行），并修正
  `task capture-diff` 章节——删除不存在的 `--apply`、补 `--skip-quality-review`，
  `--dry-run` 语义按 `cli/main.py` argparse 真相源改为"非默认"。
- 新增门禁 `tests/test_cli_reference_sections.py`（8 项）：每条首批章节必须具备
  用法块 / 参数表 / 退出码表（含 0/1/2）/ 非空示例块，四段顺序固定、各只出现一次；
  同时锁定 `EXPECTED_COMMANDS` 44 条不丢失、首批前缀下不得出现未登记章节。
- `docs/architecture.md` 阶段 3「文档深度完善」📋 → 🔄，注明首批覆盖范围。

### 21.4 验证

- `pytest tests/test_cli_reference_sections.py -q` → 8 passed。
- `pytest tests/test_cli_docs_structure.py tests/test_category_source.py -q` → 36 passed
  （既有骨架与 21 分类真相源校验无回归）。
- 手工校验脚本：45 个首批章节 bad=0；`search` 章节无重复参数表。

### 21.5 边界与残留

- **其余 13 个分类**（Call Chain / Metrics / Semgrep / Coverage / GC / Git 等）尚未
  四段式化，由后续任务卡分批推进；扩充时同步追加 `FIRST_BATCH_PREFIXES` 与
  `EXPECTED_COMMANDS`。
- 质量门禁章的「`task list` / `task show`：任务列表与详情」合并章节与两个单命令
  章节内容重叠，本轮两处均已补段；合并/去重登记为文档残留，不在本卡处理
  （改动面会波及其他交叉引用）。
- 退出码仅约定 `0/1/2` 三档；细分错误码仍以命令输出正文为准，不进文档表格。

---

## 22. 测试覆盖加强（阶段 3 第三项）· 27 个新增 subcommand handler 端到端测试

### 22.1 现象

`docs/architecture.md` 阶段 1 记录的「新增 27 个 subcommand handler」长期只有
进程内 dispatch 单测，没有端到端证据：argparse → dispatch → handler → daemon RPC
→ 渲染输出的整条链路未被锁定。以 `docs/testing/manifests/cli/cli_tree.json`
（206 个叶子命令）为基准做文本粗测，约 89 个叶子在 `tests/` 下没有任何引用，
其中既有 Governance 族（lease / assignment / collab / dependency），也有
C8 新增的 8 个主分类面（workspace / query / call-chain / git / semgrep /
coverage / diagnostics 部分）。后果：这些 handler 的注册、参数形状、输出结构
一旦漂移，单测与文档门禁都发现不了。

### 22.2 范围（27 条，按 21 主分类）

| 主分类 | 命令 |
| --- | --- |
| Workspace & Database | `workspace scan` |
| Query & Search | `semantic-search` |
| Call Chain Analysis | `top-callers` / `detect-cycles` / `export-module-graph` |
| Git Integration | `git log` / `git stats` / `git destructive-log` |
| Semgrep | `semgrep list` / `semgrep stats` |
| Coverage | `coverage fn` / `coverage uncovered` |
| Diagnostics | `config explain` / `config paths` |
| Agent Rule Memory | `rule list` / `rule applicable` / `rule extract` / `guardrail rules` |
| GC | `gc status` / `gc archive-list` / `gc audit-list` |
| Clone | `clone stats` |
| Defect | `defect stats` |
| Audit & Bootstrap | `audit verify` / `bootstrap status` |
| Assignment & Lease | `lease status` / `assignment show` |

选取口径：优先只读、输出结构稳定、可在隔离空快照上确定性复现的 handler；
写操作与重操作（`gc purge` / `gc restore` / `clone detect` / `fts rebuild` /
`semgrep scan` 等）不进本卡，避免测试污染与长耗时。

### 22.3 处置

- 新增 `tests/test_cli_subcommand_e2e.py`（30 个 testcase）：
  - 27 条 parametrize 用例，每条 spawn `python -u cw.py <argv>` 子进程，
    断言 `rc=0`、无 `Traceback` / `Subcommand '<x>' failed` 失败标记、
    输出含该命令的结构化关键字；
  - `test_frozen_case_count_is_27` 冻结清单数量（增删须同步本节）；
  - `test_missing_required_arg_exits_2` 锁定 argparse 层退出码（rc=2 + usage）；
  - `test_leaf_calls_are_read_only` 断言跑完后 `tasks` / `task_workspace_bindings`
    内容不变。
- 隔离栈：模块级 fixture 复用 `tests/_w3_harness.py`（模式 A USERPROFILE
  重定向 + `workspace.register` + 空 codegraph `snapshot.publish`），并 seed
  已绑定 workspace 的探针任务 `T-CLI-E2E-27`，使 `lease status` /
  `assignment show` 走通权威 resolver；子进程 env 只定向到隔离 `data_root`，
  全程不触碰生产库。
- 前置缺失（无 `cw-daemon` 二进制 / daemon manifest 前退出 / 管道冲突）统一走
  `tests/_prereq_guard.py` 降级 skip；manifest 后卡死与真实 RPC 失败保持红。
- `docs/architecture.md` 阶段 3「测试覆盖加强」📋 → ✅。

### 22.4 验证

- `pytest tests/test_cli_subcommand_e2e.py -q` → 30 passed。
- 手工复核：隔离 daemon（`rust_ext/target/debug/cw-daemon.exe`）+ 空快照下
  27 条命令全部 `rc=0`，输出与表中关键字一致。

### 22.5 边界与残留

- 本卡只覆盖 27 条；粗测剩余的 ~62 个未引用叶子（`collab publish/verdict/reveal`、
  `experiment *`、`task contract-bootstrap/contract-revise`、`semgrep scan`、
  `coverage import` 等）多为写操作或需复杂前置，留待后续任务卡按族分批推进，
  扩表时同步更新 `CASES` 与本节。
- 隔离 daemon 依赖本机构建产物（`_w3_harness.find_daemon_binary`），CI 无二进制
  时整模块 skip，属既有 C7 前置守卫口径，不计入回归失败。
- 退出码观测以真实行为为准：daemon 业务拒绝当前渲染为 `rc=2`，与
  `cli_reference.md` 四段式约定的「1=运行时失败」存在口径差；本卡只锁定
  argparse 层的 2 与成功态的 0，不锁业务失败码，该差异登记为文档残留（阶段 3
  「`--flag` 移除评估」之外的独立项；后由 §24 统一为 `rc=1` 闭合）。

## 23. Orphan 卡清理（空投影恢复）· `T-1793500842034-88eecb60`

### 23.1 现象

- 阶段 3 第三项施工期间，首次以 **governed 通道**建卡（携带 `--task-id` +
  `--task-contract-envelope`）时，CLI 对 `governed_create` 置空 `role_contracts`
  （`cli/main.py` 对应分支），而 daemon 仅在 `role_contracts` 非空时于同一事务
  内 bootstrap Task Contract（`task_collab.rs` 对应分支）。
- 结果：该卡 `task_contract_revisions=0`、`role_contracts=0`、3 步全 pending、
  无 active lease，投影恒为 `governance_blocked`，阻塞原因为「Task Contract
  缺失、多版本冲突或 revision 链不连续」。
- 该卡从未承载任何执行产出（步骤未领取、无 `change_audit`、无证据）；真实交付
  由 `T-1793500993792-c74c99ac` 承接并已 `closed/COMPLETE`。

### 23.2 恢复通道矩阵

| 通道 | 可行 | 依据 |
| --- | --- | --- |
| `task contract-bootstrap` | 否 | daemon `precondition_role_sources` 要求 executor/reviewer/adjudicator 三条 current legacy role contract；本卡 `role_contracts=0`，且 `BOOTSTRAP_ROLE_ALLOWLIST` 不含该 id，必然 `ERR_BOOTSTRAP_ROLE_SOURCE` |
| `task bootstrap-executor-evidence` + `bootstrap-reviewer-pass` | 否 | 两段 bridge 只补执行证据与 reviewer pass，不创建 Task/Role Contract 链，投影仍 `BLOCKED`，属半成品投影风险 |
| `task supersede` | 是 | orphan 卡 reviewer lease 可正常 acquire（fencing=1），supersede 是唯一 append-only 收编通道，无需伪造合同或步骤证据 |

### 23.3 处置

- 以 `trae-reviewer` 持有 orphan 卡的 reviewer lease（`L-85cdee2fefa5edcc`，
  fencing=2），由 `trae-adjudicator` 以 adjudicator 身份提交 `task supersede`：
  `SUP-9440d63262a6acb6`，被本卡 `T-1793504846491-ce17ddf8` 收编。
- 证据：`docs/evidence/T-1793500842034-recovery.json`。
- 失败尝试按 fail-closed 原样记录，未做参数兼容：
  - `E_GOVERNANCE_REVIEWER_INVALID`：lease holder（`trae-adjudicator`）非
    active registered reviewer；
  - `E_REQUEST_ID_REUSE_MISMATCH`：同一 `request_id` 复用于不同参数组合。
- 回读旧卡 `task show`：出现 `Superseded by -> T-1793504846491-ce17ddf8`，
  子命令 `--role` 仅接受 `adjudicator`，reviewer 必须是 lease holder 且与
  adjudicator 三者（agent/instance/session）分离。

### 23.4 验证

- `cw task show T-1793500842034-88eecb60` → `Superseded by` 段存在，
  supersedence_id=`SUP-9440d63262a6acb6`。
- 本卡 3 步（triage/recover/verify_doc）全绿后经 reviewer 独立复核、adjudicator
  apply/close，投影 `COMPLETE`。
- `python scripts/check_e2e_matrix_closure.py` 保持 PASS。

### 23.5 边界与残留

- supersede 不重建旧卡合同，只登记「被替代」；旧卡 `workflow_status` 仍会显示
  `governance_blocked`，判定依据应以 `Superseded by` 段而非 workflow_status 为准，
  该展示口径差登记为后续投影优化项。
- 根治方向：governed 建卡通道应在 CLI 侧同样注入 A′ 默认三角色 legacy role
  contracts（或 daemon 侧显式拒绝并给出可执行提示），避免同类空投影卡再产生；
  属产品代码改动，不在本卡范围。

---

## 24. CLI 失败退出码口径统一（阶段 3 残差 · `T-1793505963640-e9540b78`）

### 24.1 现象与口径

`docs/cli_reference.md` 四段式（§21.2）约定 `0` 成功 / `1` 运行时失败 /
`2` 用法与输入校验错误。§22.5 已登记残差：daemon 业务拒绝与运行时异常仍渲染
`rc=2`，通用 `except Exception` 甚至 `return True`（`rc=0` 假成功），消费方无法
按退出码区分「参数写错」与「daemon 拒绝/环境故障」。

按失败来源分类（本卡处置范围）：

| 类别 | 典型出口 | 旧行为 | 新口径 | 判定依据 |
| --- | --- | --- | --- | --- |
| 用法 / 参数解析 | argparse 必填缺失、未知 flag、`--from-file` + prometheus 组合 | 2 | **2** | 解析层拒绝，未进入业务 |
| 输入校验 | `--from-file` 快照缺失/损坏、`--watch-dir` 不存在、`--help` | 2 / 0 | **2** / 0 | 本地输入不合法，非运行时故障 |
| daemon 业务拒绝（HTTP） | `DaemonRemoteError`、`SharedTaskWriterRequiredError`、`E_WORKSPACE_PROJECT_MISMATCH` | 2 | **1** | daemon 权威拒绝，运行时失败 |
| daemon 业务拒绝（socket dict） | `_handle_task` 各分支 `{"error": ...}` / `None`（no response）出口 | 2 | **1** | 同属 daemon 拒绝/传输失败 |
| 运行时 / 环境异常 | 通用 `except Exception`（此前 rc=0）、db locked、metrics RPC 不可达、agent daemon 不可达/握手失败/watchdog 缺失、`daemon start` 失败、`cw daemon` 业务错误 | 0 / 2 | **1** | 非用法错误，必须非零 |
| 平台 / 部署门禁 | `cw-client`/`cw-agent`/`cw-daemon` 非 Linux、自递归守卫、二进制缺失 | 2 | 2 | 环境前置条件，保持 fail-closed 2 |

保持不变：既有成功路径（`rc=0`）、argparse 层 `rc=2` 与平台/部署门禁 `rc=2`。

### 24.2 受影响测试清单

需由 `2`（或 `0`）改为 `1`：

| 文件 | 用例 | 旧断言 | 新断言 |
| --- | --- | --- | --- |
| `tests/test_cli_005_http_rpc.py` | `test_agent_start_daemon_unavailable` | `rc == 2` | `rc == 1` |
| `tests/test_g13_audit_fix.py` | `test_metrics_cmd_rpc_unreachable_fails_closed` | `rc == 2` | `rc == 1` |
| `tests/test_shared_task_writer.py` | `test_cli_shared_task_write_has_nonzero_exit_code` | `returncode == 2` | `returncode == 1` |
| `tests/test_cli_govfix07_daemon_error_rc.py` | dispatch 层 4 用例（DaemonRemoteError / 本地异常 / SharedWriter / attest error dict） | `code == 2` / `return True` | `code == 1` |
| `tests/test_cli_govfix08_error_dict_rc.py` | socket error dict 9 用例 | `code == 2` | `code == 1` |
| `tests/test_task_create_workspace_binding.py` | `test_dispatch_project_mismatch_exits_nonzero` / `test_dispatch_other_daemon_unavailable_keeps_rc_zero` | `code == 2` / `return True` | `code == 1` |
| `tests/test_cli_subcommand_e2e.py` | 新增 daemon 业务拒绝门禁（无 lease 的 `task apply`） | — | `rc == 1` |
| `tests/test_cli_consistency.py` | 注释口径（业务失败不再复用 2） | 注释称 rc=2 业务 fail-closed | argparse 判定不变，注释改口径 |

保持 `2`（不复改）：`test_phase8_metrics_endpoint.py:111`（`--from-file` 缺失）、
`test_g13_audit_fix.py:402/412`（prometheus 组合 / 快照缺失）、
`test_cli_subcommand_e2e.py:291`（argparse 缺参）、`test_cw_client_rpc_proxy.py`
与 `test_p0_3_cross_platform_package.py`（平台门禁）、`test_release_bundle_inspector.py`
（独立工具用法）。

文档同步：`docs/cli_reference.md` 中 `task cascade-close`（:2200）与
`daemon start`（:4100）两处「退出码 2」改为「退出码 1」。

### 24.3 处置

- `cli/main.py`
  - `_dispatch_subcommand` 五个失败出口统一 `SystemExit(1)`：`SharedTaskWriterRequiredError`、
    `DaemonRemoteError`、`E_WORKSPACE_PROJECT_MISMATCH`、db locked、通用 `Exception`
    （后者由 `return True`（rc=0 假成功）改为 `raise SystemExit(1)`）。
  - `_handle_task` 各运行时出口：apply / close / cascade-close（error 与 gate 拒绝两处）/
    reopen / claim-recover / completion-review / supersede（error 与 no-response）/
    attest-legacy-workspace-binding / contract-bootstrap / contract-revise /
    bootstrap-executor-evidence / bootstrap-reviewer-pass / governance-projection /
    superseded 的 `{"error": ...}` 与 `None` 分支由 `sys.exit(2)` 改为 `sys.exit(1)`。
  - governed `task.create` 与 `task.report` 的 daemon 拒绝捕获、启动阶段 workspace
    注册 db locked、`cw daemon` 业务错误捕获同步改为 `SystemExit(1)`。
  - `_agent_start` 的 daemon 不可达 / 握手失败 / watchdog 缺失三处运行时失败
    `return 2` → `return 1`；`--watch-dir` 不存在仍为输入校验 `2`，`--help` 仍为 `0`。
  - 相关 GOV-FIX-06/07/08 注释按新口径改写，避免「RC=2」陈述漂移。
- `cli/daemon_commands.py`：`daemon start` 启动失败（`E_DAEMON_START_FAILED`）与
  `metrics` daemon RPC 不可达两处 `return 2` → `return 1`；`--from-file`
  缺失/损坏与 `--from-file + prometheus` 组合仍为输入校验 `2`。
- 测试：`test_cli_govfix07_daemon_error_rc.py`、`test_cli_govfix08_error_dict_rc.py`
  与 `test_task_create_workspace_binding.py` 改用例名为 `exits_1` 并断言 `1`；
  `test_cli_005_http_rpc.py` / `test_g13_audit_fix.py` / `test_shared_task_writer.py`
  同步断言；`test_cli_subcommand_e2e.py` 新增无 lease `task apply` 业务拒绝门禁
  （`rc=1` 且含 `E_` 错误码）；`test_cli_consistency.py`、`test_cli_task_lease_parity.py`、
  `test_rust_cli_diff.py` 注释改口径（断言本身不变）。
- 文档：`docs/cli_reference.md` 两处退出码说明同步为 1。

### 24.4 验证

- `pytest tests/test_cli_govfix07_daemon_error_rc.py tests/test_cli_govfix08_error_dict_rc.py
  tests/test_task_create_workspace_binding.py tests/test_cli_005_http_rpc.py
  tests/test_g13_audit_fix.py tests/test_shared_task_writer.py tests/test_cli_consistency.py
  tests/test_phase8_metrics_endpoint.py tests/test_cli_subcommand_e2e.py`
  → `158 passed, 1 skipped in 33.81s`（skip 为既有前置守卫，非本卡回归）。
- 保持 `2` 的守卫用例全绿：`--from-file` 缺失（phase8）、prometheus 组合
  （g13）、argparse 缺参（e2e:291）、平台门禁（`test_cw_client_rpc_proxy` /
  `test_p0_3_cross_platform_package`，未改动、本卡未纳入跑单）。
- HTTP 与 socket/named-pipe 两种传输下的 daemon 拒绝均收敛到 `rc=1`，
  不再出现同一失败因传输路径不同而 `1/2` 分裂。

### 24.5 边界与残留

- 本地输入校验失败的 `return True`（rc=0）出口未在本卡收口，例如 identity 缺失 /
  lease 凭证不完整 / `--envelope-path` 读取失败 / `split` 计划文件不存在等；
  这类应归 `2`（用法/输入校验），但涉及逐命令语义与大量既有测试期望，
  登记为后续残差卡（本卡只统一 daemon 拒绝与运行时异常）。
- 平台 / 部署门禁（`cw-client`/`cw-agent`/`cw-daemon` 非 Linux、自递归守卫、
  二进制缺失）保持 `2`，因为其语义是「环境前置条件不满足」而非运行时故障；
  若后续要区分，可另立口径，不在本卡扩大改动面。
- `cw daemon serve` 下线提示的 `return 2` 分支理论上不可达（argparse 不再注册），
  保持原样，避免为死路径引入噪音。

---

## 25. i18n 文案漂移：引用即存在（`T-1793521395642-f45bc274`）

### 25.1 现象与基线（实测 2026-11-01）

`i18n/__init__.py::t()` 的缺 key 语义（`i18n/__init__.py:153-160`）：`default=None`
时**原样返回 key 字符串**。因此「代码引用了 catalog 不存在的 key 且未传
`default=`」会把裸 key 直接打印给用户（如 `cli_fts_rebuild_done`），在 zh_CN /
en_US 两种语言下均不可读。

三组基线实测结论：

| 基线 | 口径 | 结果 |
| --- | --- | --- |
| 既有 i18n 门禁 | `pytest tests/test_mcp_i18n_messages.py tests/test_i18n_cleanup_c5.py tests/test_i18n_print_cleanup.py` | `65 passed in 289.23s`，无回潮 |
| catalog 双向对账 | 扁平化 `zh_CN.json` / `en_US.json` 后取差集 | `1969 / 1969`，`only_zh=[]`、`only_en=[]`（**无双语缺项**） |
| 引用一致性扫描 | AST 遍历包内 58 个含 `i18n` 的模块；`t(k)` 原样、`get_msg(k)`→`cli.messages.k`、`get_error(k)`→`errors.k`、`get_arg_help(k)`→`cli.args.k`；**跳过带 `default=` 的宽容调用** | **9 处调用 / 8 个唯一 key**，全部位于 `cli/main.py` |

结论：本轮漂移不在 catalog 对账层（双语完全对齐），而在**代码引用层**——
存在「引用了不存在的 key 且无兜底」的裸 key 出口。既有门禁只验「已知 key
存在」与「无未走 i18n 的 print」，未覆盖「引用即存在」这一维度。

### 25.2 确认清单（8 key / 9 调用）

| # | 调用点 | key | 现象 |
| --- | --- | --- | --- |
| 1 | `cli/main.py:2855` | `cli.args.category` | `guardrail scan` 过滤提示 `cat_info` 拼出裸 key |
| 2 | `cli/main.py:2885` | `cli.args.category` | `guardrail rules` 过滤提示同上 |
| 3 | `cli/main.py:6119` | `cli.messages.task_supersede_failed` | daemon 返回 error 时打印裸 key（同文件 6112 处带 default，故只有 error 分支裸 key） |
| 4 | `cli/main.py:6491` | `cli.messages.task_superseded_failed` | 同上，`task superseded` 的 error 分支 |
| 5 | `cli/main.py:8482` | `cli_fts_rebuild_done` | `fts rebuild` 成功摘要打印裸 key |
| 6 | `cli/main.py:8488` | `cli_fts_rebuild_mismatch_warning` | 索引行数不一致告警打印裸 key |
| 7 | `cli/main.py:8492` | `cli_fts_rebuild_failed` | 重建失败打印裸 key |
| 8 | `cli/main.py:11987` | `cli.messages.semgrep_incremental_mode` | 增量扫描模式行打印裸 key |
| 9 | `cli/main.py:12003` | `cli.messages.semgrep_incremental_done` | 增量扫描完成摘要打印裸 key |

同族参照：`cli.args.file`（2854/…）在 catalog 中存在，且与 `category` 采用同样的
`（{label}: {value}）` 拼接风格；`cli_fts_*` 族其余 key（如 `cli_fts_rebuilding`、
`cli_fts_status_desc`）在 catalog 中同样缺失，但因调用处带 `default=`（英文兜底）
不会输出裸 key，属「缺翻译但不露裸 key」，登记为残留（§25.5）。

### 25.3 处置

- `i18n/zh_CN.json` / `i18n/en_US.json`：按 §25.2 清单补 8 个 key（每个文件各 8 条），
  扁平化键数由 `1969` 增至 `1977`，双向仍零差异。插入位置就近既有同族条目：
  - `cli.args.category` 紧随 `cli.args.file`（同一 `（{label}: {value}）` 拼接风格）；
  - `semgrep_incremental_mode` / `semgrep_incremental_done` 紧随 `semgrep_scan_done`；
  - `task_supersede_failed` / `task_superseded_failed` 紧随 `task_close_failed`；
  - `cli_fts_rebuild_done` / `_mismatch_warning` / `_failed` 为顶层 flat key，与既有
    `cli_*` flat 组同区（调用处即 `t("cli_fts_rebuild_*")` 原样形态）。
  - 占位符与调用 kwargs 严格对应：`{symbols}/{fts_rows}/{triggers}/{elapsed}`、
    `{base}/{head}`、`{changed}/{scanned}/{saved}/{total}/{stale}`、`{error}`。
- `cli/main.py`：**零改动**。9 处调用签名已正确（仅 catalog 缺条目），补 key 即修复；
  避免为「补文案」引入代码面变更与 whitelist 扩张。
- 新增 `tests/test_i18n_key_existence.py`（11 用例）固化「引用即存在」：
  - 扫描口径与 §25.1 一致（AST + 前缀映射，跳过带 `default=` 的宽容调用）；
  - `test_referenced_keys_exist_in_both_catalogs`：无 default 引用的 key 必须双语存在；
  - `test_catalog_parity`：zh/en key 集合必须完全一致；
  - `test_scanner_finds_references`：口径自检（防止扫描退化后空跑通过）；
  - `test_required_keys_translated`：8 个 key 双语存在且值不等于 key 本身。

### 25.4 验证

- `python -m pytest tests/test_i18n_key_existence.py` → `11 passed in 1.78s`。
- `python -m pytest tests/test_mcp_i18n_messages.py tests/test_i18n_cleanup_c5.py
  tests/test_i18n_print_cleanup.py tests/test_i18n_key_existence.py`
  → `76 passed in 22.89s`（既有 65 用例无回潮）。
- 引用扫描复算：`无 default 且缺 key` 由 `9 处调用 / 8 唯一 key` 降为 `0`。
- catalog 复算：`zh=1977 / en=1977`，`only_zh=[]`、`only_en=[]`。
- 渲染实测（`set_language` 后逐 key 取值）：zh_CN 与 en_US 均输出完整文案，无裸 key，
  占位符全部替换成功（`分类` / `category`、增量模式与完成摘要、supersede 两处失败、
  fts 完成/不一致/失败三条）。

### 25.5 边界与残留

- `cli_fts_*` 族其余 key（`cli_fts_rebuilding` / `cli_fts_status_desc` /
  `cli_fts_not_exist` / `cli_fts_status_symbols` 等）在 catalog 中同样缺失，但调用处
  均带 `default=` 英文兜底 → 不露裸 key，仅表现为 zh 环境下输出英文；同类的
  `cli.messages.task_supersede_success` / `task_superseded_none` 等亦如此。
  本卡只收口「裸 key 暴露」；「双语补译（含兜底文案）」属后续翻译完整性批次。
- 门禁口径只覆盖**静态常量** key；动态拼接 key（`t(variable)`）无法静态判定，
  扫描中直接跳过，属口径边界，不在本卡扩大。
- 门禁只验证「key 存在」与「双语集合一致」，不校验占位符一致性（占位符缺项在
  `t()` 内部会被 `except KeyError` 吞掉并返回未替换文本）；如需，可另立占位符
  静态校验卡。

---

## 26. i18n 翻译完整性：default 兜底缺失条目（`T-1793532165761-916c5e14`）

### 26.1 现象与口径

§25 已收口「裸 key 暴露」（无 `default=` 且 key 缺失）。本卡处置 §25.5 登记的
方向 1 残留：调用处带 `default=` 的 key 虽不露裸 key，但 zh_CN 环境直接输出英文
兜底文案，属于「缺翻译而非缺兜底」。本轮口径升级为**静态常量引用必须双语存在**
——不再按 `default=` 豁免。

### 26.2 实测基线（2026-11-01）

| 基线 | 口径 | 结果 |
| --- | --- | --- |
| catalog 对账 | 扁平化 `zh_CN.json` / `en_US.json` 取差集 | `1977 / 1977`，双向零差异 |
| 引用扫描 | AST 遍历 53 个含 `i18n` 的包内模块；`t`/`get_msg`/`get_error`/`get_arg_help` 前缀映射 | 全量 **2253 处调用 / 1916 个唯一 key**；其中 **337 处 / 304 个唯一 key** 双语缺条目 |
| 无兜底缺口 | 同扫描，`default` 缺失 | `0`（§25 收口保持） |
| catalog 可达性 | 按 `t()` 的 `key.split(".")` 逐层 `dict.get` 判定 | 顶层存在 **16 个含点号的字符串键**（`cli.messages.setup_*` / `auto_setup_*`），`t()` 永远不可达；扁平化成员判断会把它们误判为「已存在」 |
| 非字符串叶子 | 同上 | 6 个键（`task_constraints_*`、`task_plan_root_check_items`）值为列表，`t()` 走 `str(value)`；不在「缺条目」口径内 |
| 硬编码回归面 | 在 `tests/` 检索 304 条英文兜底片段 | 仅 `tests/fixtures/run_matrix3_fts5.py` 1 处（非 pytest 用例） |

分组（启发式归类，扫描清单为准）：

| 组 | 范围 | 唯一 key 数 | 代表 |
| --- | --- | --- | --- |
| A | 参数 help（`cli.args.*`） | 4 | `cli.args.install_agent` / `install_agent_project` / `install_detect_agents` / `install_force_agent` |
| B | 命令与参数描述（`*_subcommand_desc` / `*_arg_*` / `*_action_*` / `*_desc`） | 187 | `cli.messages.call_chain_subcommand_desc`、`semgrep_arg_*` |
| C | DB 迁移提示（`migration_v31..v61`） | 31 | `cli.messages.migration_v31` … `v61` |
| D | 运行时消息（其余） | 82 | `cli_fts_*`、`task_supersede_*`、`install_agent_*`、`git_*`、`semgrep_rules_*` |

其中 292 个 key 的 `default=` 是可直接落地的字面量；12 个是非字面量表达式
（下表 9 个 + 3 个顶层点号键 `auto_setup_done` / `setup_detected` / `setup_done`
的 f-string），需人工确定兜底与占位符：

| key | 现兜底表达式 | 占位符 |
| --- | --- | --- |
| `cli.messages.task_show_commits_count` | `"Commits ({}):".format(len(commits))` | `{count}` |
| `cli.messages.task_show_changes_count` | `"Symbol changes ({}):".format(len(changes))` | `{count}` |
| `cli.messages.task_show_superseded_line` | `"  -> {} (superseded)".format(new_id)` | `{new_id}` |
| `cli.messages.task_show_superseded_reason` | `"     reason: {}".format(...)` | `{reason}` |
| `cli.messages.task_show_superseded_actor` | `"     by: {}".format(...)` | `{actor}` |
| `cli.messages.symbol_history_tasks_count` | `"Tasks ({}):".format(len(related_tasks))` | `{count}` |
| `cli.messages.git_show_tasks_count` | `"Tasks ({}):".format(len(related_tasks))` | `{count}` |
| `cli.messages.edit_invalid_symbol_id` | `f"Invalid symbol_id: {symbol_id}"` | `{symbol_id}` |
| `cli.messages.edit_symbol_id_not_found` | `f"Symbol not found: id={sid}"` | `{sid}` |

### 26.3 处置

- `i18n/zh_CN.json` / `i18n/en_US.json`：各补 304 条（扁平化 `1977` → `2281`）。
  - en 值 = 现英文兜底原文（保证 en 输出零变化）；zh 值 = 中文翻译。
  - 12 个非字面量 key 按上表补 `{count}` / `{new_id}` / `{reason}` / `{actor}` /
    `{symbol_id}` / `{sid}`，与调用 kwargs 严格对应。
  - 原兜底本身是中文的 20 个 key：10 个非点号键（`cli_test_usage`、`cli_lease_arg_role`、
    `task_claim_*`、`task_panel_*`、`task_supersede_*`、`task_superseded_*`）的 en 侧由
    本批改写为英文（否则 en_US 环境输出中文），zh 侧沿用原中文；另 10 个顶层点号键
    搬迁时沿用其既有 en/zh 值（两套 catalog 原本就有英文）。
  - `current_locale` 双向落值（zh=`zh_CN` / en=`en_US`）。该键原缺条目，
    `db_task_evidence.py::_resolve_evidence_message` 恒取 `default="zh_CN"`，
    导致 en 环境也命中 zh 兜底表。
- **16 个顶层点号键就地搬迁**：`cli.messages.auto_setup_done` 等原为顶层字符串键，
  按 `t()` 的 `split(".")` 语义永远不可达（其中 13 个被静态引用）。两套 catalog
  同步搬入嵌套 `cli.messages`，搬迁后顶层点号键归零，条目总数不变。
- `tests/test_i18n_key_existence.py`：门禁升级。
  - 取消「跳过带 `default=` 的宽容调用」，改为**所有静态常量引用**必须双语存在。
  - 存在性判定改用 `_reach()`（复刻 `t()` 的逐层 `dict.get`），不再用扁平化成员
    判断（后者会掩盖顶层点号死条目）。
  - 新增 `test_no_top_level_dotted_keys`，把「顶层无点号键」固化为结构不变量。
  - 保留 catalog 双向一致与「值不等于 key」自检。
- `cli/main.py` 及其余业务源码零改动（本批只补 catalog + 门禁）。
- 6 个列表值 key（`task_constraints_*`、`task_plan_root_check_items`）未改动：
  它们可达，`t()` 返回 `str(value)`，属既有行为（见 §26.5）。

### 26.4 验证

| 项 | 命令 / 口径 | 结果 |
| --- | --- | --- |
| catalog 规模 | `json` 加载后统计扁平叶子 | `zh_CN 1977 → 2281`、`en_US 1977 → 2281`，双向 key 集合零差异 |
| 顶层点号键 | 遍历顶层键名含 `.` | 搬迁前 `16 / 16`，搬迁后 `0 / 0` |
| 占位符一致性 | 逐条比对新增条目 en/zh 的 `{...}` 集合 | 304 条全部相等（脚本断言） |
| 静态引用可达性 | 与门禁同口径的独立复算 | `2253` 处引用 / `1916` 唯一 key，缺口 `0` |
| 门禁用例 | `pytest tests/test_i18n_key_existence.py` | `13 passed`（原 11 用例 + 2 个 `no_top_level_dotted_keys` 参数化） |
| i18n 定向回归 | `pytest test_i18n_key_existence / test_i18n_cleanup_c5 / test_i18n_print_cleanup / test_mcp_i18n_messages` | `78 passed` |
| CLI help 回归 | `pytest test_cli_subcommand_help_template / test_cli_main_help / test_cli_c9_coverage_help / test_cli_consistency / test_install_hooks_unified` | `151 passed` |
| 消息链路回归 | `pytest test_capture_diff_auto / test_cli_task_claim_recover / test_task_supersede / test_git_hook_capture / test_cli_task_next_remediation` | `36 passed, 13 skipped` |

### 26.5 边界与残留

- 动态拼接 key（`t(variable)`）仍不可静态判定，门禁继续跳过。
- en 文案沿用现有兜底原文，不重写英文表述；若英文本身待打磨，另立文案批次。
- 占位符一致性本批只做人工核对，未升级为通用静态门禁（§25.5 已登记为潜在独立卡）。
- `t()` 对列表值走 `str(value)`，6 个 `task_constraints_*` 因此在 step instruction
  里是 Python repr 字符串而非列表；这是 `t()` 的值类型契约缺口，非本条缺翻译问题，
  建议另开卡（改 `t()` 返回值语义或把该 6 条改为多行字符串）。

---

## 27. i18n 占位符一致性门禁与缺参修复（`T-1793540468063-993d3638`）

### 27.1 现象与口径

§26 已收口「引用即存在」与「default 兜底缺失」两条漂移。本卡处置 §25.5 / §26.5
登记的第三类：**catalog 值含占位符，但调用未提供同名 kwargs**。

`t()` 在 [i18n/__init__.py](file:///c:/git_work/callwarden/i18n/__init__.py#L160-L166)
用 `value.format(**kwargs)` 替换占位符；缺参时抛 `KeyError`，被
`except (KeyError, ValueError, IndexError)` 吞掉后**原样返回含 `{...}` 的 catalog 值**
（不再回落到 `default`）。因此用户看到的是裸 `{dir}` / `{error}` / `{id}` /
`{symbol_id}`，而不是兜底文案。这类缺口不会被「key 存在性」门禁捕获——key 是存在的，
坏的是调用与 catalog 的形状契约。

本轮口径：

- 只认**静态字面量 key + 显式关键字实参**的调用：AST 取第一个参数为 `str` 常量，
  再取 `node.keywords` 中具名实参集合。
- 占位符只认 `{ident}` 形式（`[A-Za-z_][A-Za-z0-9_]*`，允许 `!r` / `:spec` 后缀），
  避免把文案里的 JSON 示例（如 `{"languages":["python"]}`）误判为占位符。
- 带 `**kwargs` 展开的调用无法静态判定，豁免（本基线实测 **0 处**）。
- 多语义共键（同一 key 被 7 种不同失败分支复用、各分支文案不同）按**变体拆分**处理，
  而不是给 key 堆叠所有占位符。

### 27.2 实测基线（2026-11-01）

| 基线 | 口径 | 结果 |
| --- | --- | --- |
| 静态调用面 | 与 §26 同口径（53 个含 `i18n` 的包内模块，`t`/`get_msg`/`get_error`/`get_arg_help` 前缀映射） | `2253` 处调用 / `1916` 个唯一 key |
| 缺参站点 | catalog 占位符集合 ⊄ 调用 kwargs 集合 | **25 处**；剔除 1 处假阳性（见下）后 **24 处真实站点** |
| 缺参 key | 同上去重 | **11 个** |
| `**kwargs` 展开调用 | `node.keywords` 含 `arg=None` | `0` 处（门禁豁免分支本轮无实例） |
| 假阳性 | `cli.messages.install_agent_hook_script`（`cli/main.py:2625`） | catalog 值是整段 Agent hook Python 脚本，脚本体内 `f"...: {blocked}"` 被识别为占位符；调用本身不需 kwarg，**不修** |
| 非标识符花括号 | 值内含 JSON/计划模板示例的 key | 6 个：`install_agent_hook_script`、`cli_rule_arg_scope`、`cli_rule_arg_evidence`、`cli_rule_arg_context`、`cli_task_arg_steps`、`task_plan_template`；`format()` 抛 `ValueError` 后原样返回，恰好保留示例花括号，**不在本卡口径** |
| 顶层点号键 | 遍历顶层键名含 `.` | `0`（§26 搬迁保持） |

24 处真实站点按 key 分组（catalog 占位符取 zh_CN 值，en_US 同形）：

| # | 文件:行 | key | catalog 需要 | 调用已传 | 缺失 |
| --- | --- | --- | --- | --- | --- |
| 1 | `install.py:633` | `install_semgrep_prefetch_skip` | `{dir}` | `default`,`error` | `dir` |
| 2 | `install.py:653` | 同上 | `{dir}` | `default` | `dir` |
| 3 | `install.py:657` | 同上 | `{dir}` | `default`,`error` | `dir` |
| 4 | `install.py:662` | 同上 | `{dir}` | `default`,`error` | `dir` |
| 5 | `install.py:667` | 同上 | `{dir}` | `default`,`error` | `dir` |
| 6 | `install.py:710` | 同上 | `{dir}` | `default`,`error` | `dir` |
| 7 | `server/mcp_server.py:197` | `semgrep_rules_prefetch_skip` | `{dir}` | `default`,`error` | `dir` |
| 8 | `server/mcp_server.py:232` | 同上 | `{dir}` | `default`,`error` | `dir` |
| 9 | `server/mcp_server.py:264` | 同上 | `{dir}` | `default` | `dir` |
| 10 | `server/mcp_server.py:273` | 同上 | `{dir}` | `default`,`error` | `dir` |
| 11 | `server/mcp_server.py:283` | 同上 | `{dir}` | `default`,`error` | `dir` |
| 12 | `server/mcp_server.py:294` | 同上 | `{dir}` | `default`,`error` | `dir` |
| 13 | `install.py:1165` | `install_hooks_skipped` | `{hook}` | `default`,`path` | `hook` |
| 14 | `cli/main.py:6011` | `task_not_found` | `{id}` | `default` | `id` |
| 15 | `db/db_tasks.py:2589` | 同上 | `{id}` | `default` | `id` |
| 16 | `db/db_tasks.py:2854` | 同上 | `{id}` | `default` | `id` |
| 17 | `db/db_tasks.py:3013` | 同上 | `{id}` | `default` | `id` |
| 18 | `cli/main.py:6112` | `task_supersede_failed` | `{error}` | `default` | `error` |
| 19 | `cli/main.py:6484` | `task_superseded_failed` | `{error}` | `default` | `error` |
| 20 | `db/db_edit.py:620` | `edit_invalid_symbol_id` | `{symbol_id}` | `default` | `symbol_id` |
| 21 | `db/db_edit.py:629` | `edit_symbol_id_not_found` | `{sid}` | `default` | `sid` |
| 22 | `db/db_edit.py:663` | `edit_symbol_line_invalid` | `{name}` | `default` | `name` |
| 23 | `db/db_edit.py:679` | `edit_invalid_symbol_patch_mode` | `{mode}` | `default` | `mode` |
| 24 | `db/db_edit.py:695` | `edit_invalid_range` | `{start}`,`{end}`,`{total}` | `default` | 三者全缺 |

两族「一 key 多语义」的实质：

- `install_semgrep_prefetch_skip` 与 `semgrep_rules_prefetch_skip` 各被 7 个分支复用
  （缓存父目录缺失 / 目录检查异常 / semgrep 二进制缺失 / 权限拒绝 / OSError /
  未知异常 / 规则复制失败）。catalog 只写了「缓存父目录不存在: {dir}」一个变体，
  其余 6 个分支即使传了 `error` 也会因缺 `dir` 而露出字面 `{dir}`。
- 两族的「dir」变体本身正常：`install.py:623/628`、`server/mcp_server.py:210/220`
  传了 `dir=cache_parent`，未列入 24 处。
- `install_hooks_skipped` 是形参名错位：调用传 `path`，catalog 与 default 都用
  `{hook}`，KeyError 后返回 catalog 值（`已跳过...: {hook}`）。
- `db/db_edit.py` 5 处与 `task_not_found` 4 处是「`default` 用 f-string 拼好，
  但没把变量透传给 `t()`」：default 只在 key 缺失时生效，key 存在时 catalog 必被
  format，于是必然缺参。
- `task_supersede_failed` / `task_superseded_failed` 各 1 处是「无响应」早退分支：
  只传 `default="...(no response)"`，而 catalog 值含 `{error}`。

### 27.3 处置口径（冻结）

1. **两族多语义 key 各拆 7 个变体**：`<family>_dir` / `_error` / `_binary` /
   `_permission` / `_oserror` / `_unexpected` / `_copy`。原基键
   `install_semgrep_prefetch_skip` / `semgrep_rules_prefetch_skip` 由 `_dir` 承接后移除；
   每族 8 个调用点（共 16 个）全部迁移。两套 catalog 各新增 14 条（2 族 × 7），
   移除 2 个原基键，净叶子数 +12。
2. `db/db_edit.py` 5 处补实参：`symbol_id` / `sid` / `name` / `mode` /
   `start`+`end`+`total`，取值用调用点已有变量，不改文案。
3. `task_not_found` 4 处补 `id`（CLI 传 `opts.task_id`，DB 层传 `task_id`）。
4. `task_supersede_failed` / `task_superseded_failed` 各 1 处补 `error="no response"`。
5. `install_hooks_skipped` 改传 `hook=hook_path`（catalog 与 default 不动）。
6. 新增 `tests/test_i18n_placeholder_consistency.py`：
   - 门禁规则：字面量 key + 显式 kwargs 时，catalog（zh/en 各一份）占位符集合必须
     被 kwargs 覆盖；带 `**kwargs` 展开的调用豁免；非标识符花括号值跳过。
   - 含扫描自检断言：扫描面必须命中已知真实站点，且本卡 24 处修复后缺参数为 0。
   - 负向用例：构造「catalog 有 `{x}`、调用无 `x`」的样例必须被门禁判失败。

非目标：不重写文案；不改 `t()` 的 KeyError 容错语义；不覆盖动态 key；不处理上述
6 个含 JSON 示例花括号的 key（另行登记）。

### 27.4 验证

| 项 | 命令 / 口径 | 结果 |
| --- | --- | --- |
| catalog 规模 | `json` 加载后统计扁平叶子 | `zh_CN 2281 → 2293`、`en_US 2281 → 2293`；双向 key 集合零差异 |
| 变体 key 形状 | 14 个新键逐条比对 zh/en 的 `{...}` 集合 | `14 / 14` 相等；`_dir`={dir}、`_binary`=∅、其余 5 个={error} |
| 原基键残留引用 | 源码检索 `prefetch_skip"` / `prefetch_skip'` | `0`（16 个调用点全部迁移） |
| 缺参复算 | 与门禁同口径的独立脚本（2253 处静态调用 / 1916 唯一 key） | 真实缺参 `24 → 0`；仅剩 `install_agent_hook_script` 1 处脚本文本假阳性（非格式占位符） |
| 语法检查 | `python -m py_compile` 5 个改动模块 | 全部通过 |
| 新增门禁 | `pytest tests/test_i18n_placeholder_consistency.py` | `23 passed` |
| key 存在性门禁 | `pytest tests/test_i18n_key_existence.py` | `13 passed` |
| 定向回归 | `pytest` 新门禁 + `test_i18n_key_existence` / `test_i18n_cleanup_c5` / `test_i18n_print_cleanup` / `test_mcp_i18n_messages` / `test_task_supersede` / `test_install_hooks_unified` | `126` 用例：`122 passed, 4 skipped, 0 failed` |
| 任务链路回归 | `pytest tests/test_cli_task_next_remediation.py` | `2 passed` |

### 27.5 边界与残留

- **非标识符花括号**：6 个 key（`install_agent_hook_script`、`cli_rule_arg_scope`、
  `cli_rule_arg_evidence`、`cli_rule_arg_context`、`cli_task_arg_steps`、
  `task_plan_template`）的值内是 JSON / 计划模板示例。`format()` 因裸 `{` 抛
  `ValueError` 后原样返回，恰好保留了示例花括号；门禁按「不可格式化」跳过。
  若后续给这些 key 传 kwargs，仍会静默返回未替换文本，属独立契约问题，另行登记。
- **`_dir` 兼载两语义**：`install.py:628` 与 `server/mcp_server.py:220` 的
  「缓存目录无写权限」分支沿用 `_dir`（`{dir}`）文案，实际展示的是「缓存父目录
  不存在」。这是拆分前的既有展示行为（原基键即 `{dir}` 单值），本卡以「零行为
  变化」优先，未再扩第 8 个变体；如需区分，建议另立文案批次补
  `..._write_permission`。
- 动态拼接 key（`t(variable)`）仍不可静态判定，门禁继续跳过。
- en 文案沿用各调用点既有 `default` 原文，保证 en 输出零变化；英文表述打磨另立批次。
- `t()` 对列表值走 `str(value)` 的 6 个 `task_constraints_*` 未触及，仍属 §26.5
  登记的独立契约卡。

## 28. i18n 字面花括号 6 key 转义与 catalog 格式化安全门禁（T-1793549305665-435643ec）

### 28.1 现象

`t()` 对 catalog 字符串统一执行 `value.format(**kwargs)`（
[i18n/__init__.py](file:///c:/git_work/callwarden/i18n/__init__.py) 约 L162-L166），
另有两个异常吞分支：`except (KeyError, ValueError, IndexError)` 直接返回未格式化的原值。
6 个 key 的 catalog 值是 JSON 示例 / 计划模板 / 整段 Agent hook 脚本，内部含裸 `{` `}`：

| # | key | 花括号性质 | 调用点 |
| --- | --- | --- | --- |
| 1 | `cli.messages.install_agent_hook_script` | 整段 Python 脚本：空字典 `{}`、`json.dumps({...})`、f-string `{blocked}` | [cli/main.py](file:///c:/git_work/callwarden/cli/main.py#L2624-L2625) |
| 2 | `cli.messages.task_plan_template` | 计划模板中的中/英文填空占位文本 | [db/db_tasks.py](file:///c:/git_work/callwarden/db/db_tasks.py#L3610-L3619) |
| 3 | `cli_rule_arg_scope` | 作用域 JSON 示例 | [cli/main.py](file:///c:/git_work/callwarden/cli/main.py#L2199) |
| 4 | `cli_rule_arg_evidence` | 证据 JSON 示例 | [cli/main.py](file:///c:/git_work/callwarden/cli/main.py#L2205) |
| 5 | `cli_rule_arg_context` | 上下文 JSON 示例 | [cli/main.py](file:///c:/git_work/callwarden/cli/main.py#L2249) |
| 6 | `cli_task_arg_steps` | 步骤 JSON 数组示例 | [cli/main.py](file:///c:/git_work/callwarden/cli/main.py#L3775) |

当前输出"碰巧正确"：`format()` 因裸 `{` 抛 `ValueError`，或因
`{根任务标题}` / `{Root task title}` 这类非标识符字段名抛 `KeyError`，被静默吞掉后原样
返回 catalog 值。但这是格式契约缺口：

1. 这些值永远无法安全接受 kwargs——真传参会先于裸花括号失败或被静默吞掉；
2. 任何真实占位符拼写错误都会落进同一静默分支，与「值不可格式化」不可区分；
3. 现有占位符门禁把这类整值按「不可格式化」跳过（`_format_fields` 返回 `None`），
   属明确的覆盖盲区。

### 28.2 基线（改动前）

| 项 | 口径 | 基线结果 |
| --- | --- | --- |
| 6 key × 2 语言存在性 | `i18n.t(key)` 直接取值 | 12/12 存在，均为 `str` |
| 当前渲染 | `t(key)` 输出 vs catalog 原值逐字节比较 | 12/12 **相等**（异常吞分支原样返回） |
| 既有转义 | 值内是否已含 `{{` / `}}` | 12/12 无（可直接做机械转义） |
| 调用面 kwargs | 6 个调用点均为 `t(key, default=...)`，无 kwargs、无 `**` 展开 | 无 |
| 现有门禁覆盖 | `_format_fields` 对含非标识符花括号的整值返回 `None` | 6 key 全部跳过 |

### 28.3 处置口径（冻结）

1. **只转义 6 个 key 的两套 catalog 值**：把值内每个字面 `{` 替换为 `{{`、`}` 替换为
   `}}`。这些 key 的花括号全部是字面文本（JSON 示例、模板填空、脚本文本），没有一个是
   运行时占位符，因此 `format()` 后渲染文本与转义前**逐字节一致**。
2. **不改调用点**：6 个调用点仍不传 kwargs；不改任何 `default=` 原文；不改 `t()` 的
   异常容错语义。
3. **新增转义感知的格式化安全门禁**（`tests/test_i18n_placeholder_consistency.py`）：
   扫描两套 catalog 的每个叶子字符串，`{{` / `}}` 视为字面量，其余 `{...}` 必须
   `fullmatch` 于 `{ident}` / `{ident!r}` / `{ident:spec}`；任何裸花括号即失败。
   同时断言 6 个 key 在 zh/en 下 `t(key)` 输出与冻结基线文本一致（转义前后逐字节）。
   门禁须含扫描自检：6 个 key 必须被扫描面命中，且转义后无裸花括号残留。
4. 非目标：不改 `t()` 容错语义；不改 `default=`；不处理动态 key；不重写文案表述；
   不新增/迁移任何调用点。

### 28.4 验证

| 项 | 命令 / 口径 | 结果 |
| --- | --- | --- |
| 转义范围 | `git diff --numstat` 两套 catalog | 每文件 **6 行**值变更（共 12 行），无其他行改动 |
| 转义正确性 | `json.loads` 后逐值 `value.format()` vs 转义前基线 | 12/12 **逐字节一致** |
| 真实渲染 | fresh 进程 `i18n.t(key)` vs `git show HEAD:i18n/*.json` 原值 | 12/12 相等且长度不变（zh `2037/429/55/46/55/58`，en `2051/854/60/55/62/73`） |
| 新门禁 | `pytest tests/test_i18n_placeholder_consistency.py` | `47 passed`（含 6×2 基线断言、全量叶子扫描、扫描自检、9 条正负向样例） |
| 负向证据 | 用新判定扫描 `HEAD` 版 catalog | 两语言各**恰 6 个** key 被判失败（即本卡 6 key）；当前 catalog 为 **0** |
| key 存在性门禁 | `pytest tests/test_i18n_key_existence.py` | `13 passed` |
| 定向回归 | 8 套件（本门禁、key 存在性、cleanup_c5、print_cleanup、mcp_i18n_messages、task_supersede、install_hooks_unified、cli_task_next_remediation） | `152` 用例：`148 passed, 4 skipped, 0 failed` |
| 语法 | `pytest` 导入测试模块 | 通过 |
| 验收矩阵 | `C:\Python314\python.exe scripts/check_e2e_matrix_closure.py` | `PASS（收口条件全部满足）` |

### 28.5 边界与残留

- **相邻缺陷（实现期间发现，未纳入本卡转义范围）**：`cli.messages.complexity_header`、
  `cli.messages.largest_fns_header`、`cli.messages.coupled_fns_header` 三个既有值同时含
  合法占位符 `{hash:>3}` 与字面列宽标签（`{'行数':>5}` / `{'Cx':>6}` 等），且**零静态调用点**。
  它们与 6 个目标 key 同类（`format()` 会抛 `KeyError` 后被静默吞掉），但并非纯字面值，
  整体转义会破坏 `{hash}` 语义，故不在本卡冻结的 6 key 口径内。门禁以显式残留白名单
  （`LITERAL_BRACE_RESIDUAL`）豁免，并强制「仍含裸花括号 + 无调用引用」；一旦被引用或转义，
  白名单测试即失败，提示纳入门禁或移除。建议后续文案卡改为
  `{hash:>3}  {{'行数':>5}} ...` 形式（保留 `{hash}` 占位符，仅转义表头字面量）。
- `t()` 对「值不可格式化」与「真实占位符拼写错误」的静默等价仍存在：本卡只把 6 个已知
  值转为可格式化，使其进入门禁覆盖；未改变异常吞语义（属独立契约卡）。
- 6 个 key 之外若新增含裸花括号的 catalog 值，新门禁会直接失败——这是有意收口。
- 动态拼接 key（`t(variable)`）仍不可静态判定。
- 列表值契约（§26.5 的 6 个 `task_constraints_*`）、`_dir` 写权限语义（§27.5）、
  en 文案打磨仍属独立批次。
- 上述 3 个表头 key 与 `LITERAL_BRACE_RESIDUAL` 白名单由 §29（`T-1793584498885-52bec080`）
  承接收口。

## 29. i18n 表头 3 key 裸花括号转义与残留白名单清零（T-1793584498885-52bec080）

### 29.1 现象

§28.5 登记的三个表头值同时含**合法占位符** `{hash:>3}` 与**字面列宽标签**（`{'行数':>5}`
/ `{'Cx':>6}` 等）。`format()` 解析到字面标签时把 `'行数'` 当作字段名并因非法格式说明抛
`ValueError`，被 `t()` 的 `except (KeyError, ValueError, IndexError)` 静默吞掉后原样返回
——当前输出"碰巧正确"，但整值不可安全格式化。

| # | key | zh_CN 值 | en_US 值 | 静态调用点 |
| --- | --- | --- | --- | --- |
| 1 | `cli.messages.complexity_header` | `  {hash:>3}  {'复杂度':>6}  {'行数':>5}  {'深度':>4}  函数名` | `  {hash:>3}  {'Cx':>6}  {'Lines':>5}  {'Depth':>4}  Function` | 无 |
| 2 | `cli.messages.largest_fns_header` | `  {hash:>3}  {'行数':>5}  {'深度':>4}  函数名` | `  {hash:>3}  {'Lines':>5}  {'Depth':>4}  Function` | 无 |
| 3 | `cli.messages.coupled_fns_header` | `  {hash:>3}  {'扇入':>4}  {'扇出':>4}  {'总计':>4}  函数名` | `  {hash:>3}  {'FanIn':>4}  {'FanOut':>4}  {'Total':>4}  Function` | 无 |

三个 key 在 `cli/main.py`、`db/`、`server/`、`install.py` 中均**零静态引用**（历史表头残留）；
§28 因冻结口径「只转义 6 个纯字面 key」把它们以 `LITERAL_BRACE_RESIDUAL` 白名单豁免，
并附加「仍含裸花括号 + 无调用引用」的反向约束。

### 29.2 基线（改动前）

| 项 | 口径 | 基线结果 |
| --- | --- | --- |
| 3 key × 2 语言存在性 | `i18n.t(key)` 直接取值 | 6/6 存在，均为 `str` |
| 当前渲染 | `t(key)` 输出 vs catalog 原值 | 6/6 **相等**（异常吞分支原样返回） |
| 传参渲染 | `t(key, hash="A1B")` | 6/6 原样返回，`{hash:>3}` **未被替换** |
| 既有转义 | 值内是否已含 `{{` / `}}` | 6/6 无 |
| 静态引用 | `grep` 全仓（除 catalog/tests/docs） | **0** |
| 门禁状态 | `LITERAL_BRACE_RESIDUAL` | 3 key 全部豁免 |

### 29.3 处置口径（冻结）

1. **只转义字面标签**：把三行值中的 `{'...':>N}` 改写为 `{{'...':>N}}`，保留 `{hash:>3}`
   为真实占位符；`format(hash=...)` 后可见文本与转义前逐字节一致。
2. **清零残留白名单**：`LITERAL_BRACE_RESIDUAL` 置为空集；残留测试改为断言集合为空，
   且与 9 个已转义 key 互斥；全量裸花括号扫描结果必须为 0。
3. **扩充门禁**：新增 `ESCAPED_HEADER_KEYS`（3 key）参数化断言——值含 `{{` / `}}`、
   `_bare_braces` 为空、`_format_fields == {"hash"}`、`format(hash=..)` 与按 `{hash:>3}`
   切分的基线拼接逐字节一致、真实 `t(key, hash=..)` 输出一致；扫描自检覆盖 6+3=9 个
   已转义 key。
4. 非目标：不改 `t()` 容错语义；不改 `default=`；不动 6 个已字面转义 key；不新增/迁移
   任何调用点；不处理动态 key。

### 29.4 验证

| 项 | 命令 / 口径 | 结果 |
| --- | --- | --- |
| 转义范围 | `git diff --numstat i18n/zh_CN.json i18n/en_US.json` | 每文件 **3 行**值变更（共 6 行），无其他行改动 |
| 转义正确性 | `json.loads` 后 `format(hash="A1B")` vs `HEAD` 基线（按 `{hash:>3}` 切分拼接） | 6/6 **逐字节一致** |
| 可见文本 | 去转义（`{{`→`{`、`}}`→`}`）vs `HEAD` 原值 | 6/6 **逐字节相等** |
| 真实渲染 | fresh 进程 `i18n.t(key, hash="A1B")` | 6/6 一致 |
| 新门禁 | `pytest tests/test_i18n_placeholder_consistency.py` | `53 passed`（原 47 + 表头 format/t 断言 6） |
| 负向证据 | 新判定扫描 `HEAD` 版 catalog | zh/en **各恰 3 个** key 失败（本卡 3 key）；当前 **0** |
| 全量扫描 | 裸花括号扫描（白名单已清空） | zh `0` / en `0` |
| key 存在性门禁 | `pytest tests/test_i18n_key_existence.py` | `13 passed` |
| 定向回归 | 8 套件（本门禁、key 存在性、cleanup_c5、print_cleanup、mcp_i18n_messages、task_supersede、install_hooks_unified、cli_task_next_remediation） | `158` 用例：`154 passed, 4 skipped, 0 failed` |
| 验收矩阵 | `C:\Python314\python.exe scripts/check_e2e_matrix_closure.py` | `PASS（收口条件全部满足）` |

### 29.5 边界

- 3 个 key 仍**零静态调用点**：本卡只修复值的格式化契约，不新增/迁移调用点，因此产品可见
  输出零变化；未来若在表头渲染处传 `hash=..`，`{hash:>3}` 会正常替换，字面标签保持原样。
- `LITERAL_BRACE_RESIDUAL` 已为空集；此后任何新增含裸花括号的 catalog 值都会被
  `test_catalogs_have_no_bare_braces` 直接判失败，不再有白名单豁免。
- 动态拼接 key（`t(variable)`）、列表值契约（§26.5 的 6 个 `task_constraints_*`）、
  `_dir` 写权限语义（§27.5）、en 文案打磨仍属独立批次。

## 30. i18n 列表值契约 `t_list` 与 6 个 list 型 key 迁移（T-1793598690524-924bef1c）

### 30.1 现象

§26.5 登记的 `t()` 值类型契约缺口在 §27–§29 后仍存在：catalog 有 6 个**列表型**叶子，
`t()` 的 `isinstance(value, str)` 分支不成立，直接走 `return str(value)`（
[i18n/__init__.py](file:///c:/git_work/callwarden/i18n/__init__.py#L162-L168)）。
消费方期望的是 `list`：

- `build_structured_instruction` 把 5 个 `task_constraints_*` 赋给
  `instruction["constraints"]`（[db_tasks.py](file:///c:/git_work/callwarden/db/db_tasks.py#L1416-L1459)），
  文档契约写明 `constraints: Agent 必须遵守的约束列表`；
- `task_plan_root_check_items` 直接作为根任务汇总步骤的 `check_items`
  （[db_tasks.py](file:///c:/git_work/callwarden/db/db_tasks.py#L3571-L3575)）。

结果：结构化指令里出现的是 Python repr 字符串
`"['只添加注释，不修改函数逻辑', ...]"`，而非数组；下游按列表遍历会逐字符/逐 repr
处理，属用户可见的输出契约漂移。

### 30.2 基线（改动前）

| key | 类型 | 调用点 | 现状 |
| --- | --- | --- | --- |
| `cli.messages.task_constraints_annotate` | list[str]（3 项） | `db_tasks.py:1416` | `t()` 返回 repr 字符串 |
| `cli.messages.task_constraints_refactor` | list[str]（3 项） | `db_tasks.py:1426` | 同上 |
| `cli.messages.task_constraints_fix` | list[str]（2 项） | `db_tasks.py:1436` | 同上 |
| `cli.messages.task_constraints_edit` | list[str]（3 项） | `db_tasks.py:1445` | 同上 |
| `cli.messages.task_constraints_default` | list[str]（1 项） | `db_tasks.py:1455` | 同上 |
| `cli.messages.task_plan_root_check_items` | list[str]（2 项） | `db_tasks.py:3571` | `check_items` 为 repr 字符串 |

两套 catalog 各有且仅有这 6 个非字符串叶子（`zh_CN` / `en_US` 均为 2287 叶子）。

### 30.3 处置口径（冻结）

1. **新增类型化访问器**：`i18n.t_list(key, default=None, **kwargs) -> list`
   - catalog 值为 list → 返回元素副本；
   - catalog 值为 str → 返回单元素列表（保留 `format` 容错语义）；
   - key 缺失 → 回退 `default` 列表；无 default → 空列表。
2. **不改 `t()` 既有语义**（仍是 `-> str`），避免影响 1500+ 既有调用点。
3. **不改 catalog 值**：6 个 key 继续以 JSON 数组保存列表语义。
4. **迁移 6 个调用点**：`db_tasks.py` 5 个 constraints + 1 个 root check_items 改用 `t_list`，
   `constraints` / `check_items` 恢复为 list。
5. **门禁扩展**：`tests/test_i18n_placeholder_consistency.py` 新增
   - 两套 catalog 6 key 均为非空 `list[str]`；
   - `t_list` 双语返回与 catalog 列表相等；
   - `t_list` 对 str 值返回单元素列表（访问器形状自检）；
   - AST 反向扫描：6 个 list key 不得再出现在 `t(` 调用中。

非目标：动态拼接 key、`_dir` 写权限语义、en 文案打磨、`t()` 其它值类型（`int`/`dict`）的通用契约。

### 30.4 验证

| 项 | 命令 / 口径 | 结果 |
| --- | --- | --- |
| 门禁新增用例 | `pytest tests/test_i18n_placeholder_consistency.py tests/test_i18n_key_existence.py` | **84 passed / 0 failed / 0 error / 0 skipped**（基线 66，新增 18） |
| catalog 契约 | 两套 catalog 逐 key 断言 | 6 key 均为非空 `list[str]`，元素数 3/3/2/3/1/2；两套共 2293 叶子，非 str 叶子**恰为这 6 个 key** |
| 访问器等价 | `t_list` 双语 vs `_reach(catalog)` | 12/12 逐项相等；返回副本（append 污染后再次读取仍等于 catalog） |
| 形状与回退 | str 值 / 缺失 key | `t_list("…task_plan_subtask_check_item", title="X") == ["完成X"]`；缺失 key 有 default 返回 `["a"]`、无 default 返回 `[]` |
| 负向样例 | `t("cli.messages.task_constraints_fix")` | 仍返回 repr 字符串 `"['只修复报告的问题，不做额外修改', …]"`，证明 `t()` 语义未变、反向扫描口径有效 |
| AST 反向扫描 | 全仓静态 `t/get_msg/get_error/get_arg_help` 调用 | 6 个 list key **0 命中** |
| AST 正向扫描 | 全仓静态 `t_list` 调用 | 6 key 全部命中，落点唯一 `db/db_tasks.py` |
| 消费方冒烟 | `build_structured_instruction` 5 类 action | `constraints` 均为 `list`：annotate 3 / refactor 3 / fix 2 / edit 3 / default 1 |
| 语法 | `py_compile i18n/__init__.py db/db_tasks.py` | 通过 |
| 验收矩阵 | `python scripts/check_e2e_matrix_closure.py` | **PASS**，退出码 0（看板卡与遗留卡全部 `[OK]`） |

### 30.5 边界

- **`t()` 语义零改动**：1500+ 既有调用点不受影响；`t()` 对 list 值仍返回 repr（负向样例已固化）。新 list 型 key 必须走
  `t_list`，否则 `test_catalog_non_string_leaves_are_exactly_list_value_keys` 会先拦住（非 str 叶子集合必须恰为
  `LIST_VALUE_KEYS`，新增类型叶子必须先登记再迁移）。
- **仅覆盖静态字面量 key**：动态拼接 key（`t(variable)` / `t_list(variable)`）无法静态判定，仍属独立批次。
- **`_dir` 写权限语义（§27.5）** 与 en 文案打磨未触及；本卡只做值类型契约，两套 catalog 文本零改动。
- **`t()` 其它值类型**（`int` / `dict`）未定义通用契约；当前以「非 str 叶子集合 == 6 key」把新增类型挡在门外。
- **消费方范围**：只迁移 `db/db_tasks.py` 的 `build_structured_instruction`（5 个 constraints）与根任务汇总
  `check_items`（1 个）；未来新增 list 消费点需同步登记 `LIST_VALUE_KEYS`，否则反向扫描不覆盖。

## 31. i18n `_dir` 写权限语义拆分与 2 处调用点迁移（T-1793601570549-21299a68）

### 31.1 现象

§27.5 登记的第 8 变体：两条 **写权限** 分支复用了 **目录不存在** 的文案 key，用户看到的
原因与实际失败原因不一致（文案漂移，非缺翻译）。

| 调用点 | 分支语义 | 复用的 key | catalog 实值（zh_CN） |
| --- | --- | --- | --- |
| [install.py](file:///c:/git_work/callwarden/install.py#L627-L631) `os.access(cache_parent, os.W_OK)` | 缓存目录**无写权限** | `cli.messages.install_semgrep_prefetch_skip_dir` | `[Semgrep] 缓存父目录不存在: {dir}。跳过预取。` |
| [server/mcp_server.py](file:///c:/git_work/callwarden/server/mcp_server.py#L220-L229) `os.access(cache_parent, os.W_OK)` | 缓存目录**无写权限** | `cli.messages.semgrep_rules_prefetch_skip_dir` | `[Semgrep] 缓存父目录不存在: {dir}。将在首次扫描时下载。` |

两处调用点各自带 `default=` 英文兜底（`No write permission for cache dir: {dir}...`），
因此 **en_US 用户看到的是正确文案，zh_CN 用户看到的是「缓存父目录不存在」**——只在中文环境暴露，
属 §25「双语存在但语义错位」的一种变体。

同族目录不存在分支（[install.py](file:///c:/git_work/callwarden/install.py#L622-L626)、
[server/mcp_server.py](file:///c:/git_work/callwarden/server/mcp_server.py#L209-L218)）
与 `skip_error` 分支语义正确，本卡不动。

### 31.2 基线

| 项 | 值 |
| --- | --- |
| 受影响 key | 2 个 `*_skip_dir`（install / semgrep_rules 各 1） |
| 受影响调用点 | 2 处（`os.access(..., os.W_OK)` 分支） |
| 缺失 key | 2 个 `*_skip_no_write`（zh/en 均无） |
| 现有门禁覆盖 | `test_i18n_key_existence.py` 只校验「静态引用 key 双语存在」，同名 key 存在即通过，**无法发现语义错位** |
| 既有测试 | `test_i18n_placeholder_consistency.py` 71 + `test_i18n_key_existence.py` 13 = 84 用例（§30 后基线） |

### 31.3 处置口径

1. 两套 catalog 新增 2 个 `*_skip_no_write` key，文案为「缓存目录无写权限」，保留 `{dir}` 占位符；
2. 2 处 `os.access(..., os.W_OK)` 分支改引新 key，`default` 同步为写权限文案（en 兜底不再与 zh 分叉）；
3. `*_skip_dir` 两条既有值零改动，目录不存在分支零改动；
4. 门禁新增：新 key 双语存在、非空、含 `{dir}`、与同族 `skip_dir` 文案不同；AST 扫描断言 2 处写权限分支
   不再引用 `*_skip_dir`；
5. 不改 `t()` 语义、不改其它 `_dir` 族调用点。

**非目标**：动态拼接 key；en 文案整体打磨；`t()` 其它值类型（int/dict）通用契约；§27.5 的
「值不可格式化 vs 占位符拼写错误」静默等价问题。

### 31.4 验证

| 项 | 命令 / 口径 | 结果 |
| --- | --- | --- |
| 语法 | `py_compile` install.py、server/mcp_server.py | 通过 |
| 门禁新增 | `test_no_write_text_differs_from_skip_dir`（2 key × 双语）、`test_write_permission_branch_uses_no_write_key`（2 文件 AST）、VARIANT shape 2 条新参数 | 6 新用例全部通过 |
| 门禁回归 | `pytest tests/test_i18n_placeholder_consistency.py tests/test_i18n_key_existence.py` | 90 passed（基线 84，零回归） |
| 扩展回归 | 追加 `tests/test_cli_governance_gate.py` | 101 passed |
| 负向 | 临时把 install.py 写权限分支回退为 `*_skip_dir` | 1 failed / 1 passed：AST 用例按预期捕获回流，文件已还原 |
| 渲染 | `i18n.t()` 双语渲染新 key（dir=C:\cache） | zh/en 均输出「缓存目录无写权限 / No write permission」，无裸 `{dir}` |
| 矩阵收口 | `python scripts/check_e2e_matrix_closure.py` | PASS（退出码 0） |

AST 口径：写权限分支（`if not os.access(..., os.W_OK)`）内只允许引用对应
`*_skip_no_write`；目录存在分支（`if not os.path.isdir(...)`）只允许引用对应 `*_skip_dir`；
且两个文件内 `*_skip_dir` / `*_skip_no_write` 字面量集合各恰为 1 个，防止新增旁路。

### 31.5 边界

| 类别 | 说明 |
| --- | --- |
| 本次范围 | 仅 2 处 `os.access(..., os.W_OK)` 分支 + 2 个新增 key + 门禁 6 用例 |
| 非目标 | 动态拼接 key；en 文案整体打磨（en 兜底本就与 zh 语义一致）；`t()` 其它值类型（int/dict）通用契约 |
| 保留问题 | §27.5「值不可格式化 vs 占位符拼写错误」的静默等价仍未区分，属独立批次 |
| 回滚 | 回退 6 文件提交即可；新 key 为纯新增，旧 `*_skip_dir` 值未改动，无数据迁移 |

## 32. i18n 动态拼接 key 调用点清单化与门禁冻结（T-1793608696517-45fb6034）

### 32.1 现象

§26.5 / §28.5 / §30.5 / §31.5 反复登记同一盲区：
[tests/test_i18n_placeholder_consistency.py](file:///c:/git_work/callwarden/tests/test_i18n_placeholder_consistency.py)
的静态扫描只收集首个实参为字符串字面量的 `t(...)` / `t_list(...)` 调用，`t(variable)` 在
AST 阶段不可判定，占位符门禁（§27）与 key 存在性门禁（§26）都静默跳过。后果有两条：

1. 新增动态调用点不会触发任何门禁——即使变量在别处拼出一个不存在、或占位符不足的 key；
2. 最大数据驱动源 [cli/categories.py](file:///c:/git_work/callwarden/cli/categories.py) 的
   `title_key` / `desc_key` 以普通字符串存放在注册表数据里，不在任何存在性门禁覆盖内；
   拼错时 `t()` 原样返回 key（[cli/main.py](file:///c:/git_work/callwarden/cli/main.py#L173-L181)
   虽有 `default=` 兜底，但中文环境同样退化为内置默认描述）。

基线扫描（tests 之外的 9 个源码目录，158 个 `.py`）共 **17 处**动态调用，归为 6 类：

| 类别 | 处数 | 落点 | key 来源 | 处置 |
| --- | --- | --- | --- | --- |
| A 内部 API 包装 | 3 | [i18n/__init__.py](file:///c:/git_work/callwarden/i18n/__init__.py#L217-L227) `get_arg_help` / `get_msg` / `get_error` 的 f-string | 调用方传入 | 保留（`t()` 的公开入口本身） |
| B 注册表驱动 | 2 | [cli/main.py](file:///c:/git_work/callwarden/cli/main.py#L173-L181) `_print_main_help` 的 `cat.title_key` / `cmd.desc_key` | `cli/categories.py`（12 + 44 个 key） | 保留 + 新增双语存在性断言 |
| C 局部可枚举 | 2 | [cli/main.py](file:///c:/git_work/callwarden/cli/main.py#L7916-L7918) `_handle_gc` 的 `mode_key`（gc_retention / gc_archive_import） | 各自只有 2 个分支字面量 | **迁移为字面量 key**，清单 17→15 |
| D 结构化 reason | 6 | `cli/main.py` 的 `_collab_output_structured_reason` / `_t_structured`；`db/db_task_{contracts,evidence,gate}.py`；[server/tools/tools_collab.py](file:///c:/git_work/callwarden/server/tools/tools_collab.py#L77-L105) | daemon 返回的 `message_key` 或常量表 | 保留 + allowlist |
| E 实验盲审 | 3 | `experiments/blind_review_{evaluator,protocol,views}.py` | 协议常量 | 保留 + allowlist |
| F 实验 CLI 薄包装 | 1 | [cli/main.py](file:///c:/git_work/callwarden/cli/main.py#L14154-L14156) `_handle_experiment._msg` | 29 个调用方全部传字面量 key | 保留 + allowlist |

### 32.2 基线（改动前）

| 项 | 口径 | 基线结果 |
| --- | --- | --- |
| 动态调用点 | AST 扫描 `cli` / `db` / `server` / `i18n` / `scripts` / `experiments` / `tools` + `install.py` + `cw.py`（跳过 `tests`、`__pycache__`） | **17 处 / 16 个唯一 (文件, 所在函数, 实参) 组合 / 158 文件** |
| 注册表 key | `cli.categories.COMMAND_CATEGORIES` 非空 `title_key` / `desc_key` | 21 分类、84 命令、**12 title_key + 44 desc_key = 56** |
| 注册表 key 覆盖 | 现有 `tests/test_i18n_key_existence.py` | 只校验 zh/en 双语对齐，不校验注册表引用是否可达 |
| 静态门禁 | `pytest tests/test_i18n_placeholder_consistency.py tests/test_i18n_key_existence.py` | **90 passed / 0 failed** |

### 32.3 处置口径（冻结）

1. **冻结而非迁移**其余 15 处动态调用点：
   [tests/test_i18n_placeholder_consistency.py](file:///c:/git_work/callwarden/tests/test_i18n_placeholder_consistency.py)
   新增 `DYNAMIC_KEY_CALL_SITES`，键为 `(文件, 函数限定名, 函数名, 实参签名)`，每行附保留理由。
   实参签名只取 `Name.id` / `Attribute.attr` / `<fstring>` 等稳定形状，避免引号、换行或格式化
   改动造成假失败。门禁做**双向集合相等**：新增未登记调用点、或清单内调用点消失，都直接失败。
2. **迁移 2 处可枚举 `mode_key`**：`gc_retention` 与 `gc_archive_import` 的 dry_run/apply 分支
   改为 `if/else` 内的字面量 `t()` 调用（[cli/main.py](file:///c:/git_work/callwarden/cli/main.py#L7915-L7919)、
   [cli/main.py](file:///c:/git_work/callwarden/cli/main.py#L8148-L8152)）。颜色与 key 原样保留，
   输出零变化；清单 17 → 15。
3. **数据驱动源补存在性断言**：`cli.categories.COMMAND_CATEGORIES` 的 12 个 `title_key` +
   44 个 `desc_key` 必须双语可达；同时断言 key 数量 ≥ 56，注册表被清空时不会退化成空转。
4. **零语义改动**：不改 `t()` / `t_list()`，不改任何 catalog 文本，不改 `default=` 兜底。
5. **扫描面固定**：`cli` / `db` / `server` / `i18n` / `scripts` / `experiments` / `tools` +
   `install.py` + `cw.py`；`tests` 不计入（测试内的动态调用是断言样本）。

非目标：daemon 提供的 `message_key` 运行时校验（D 类 6 处只能靠 fallback 兜底）、
en 文案打磨、`t()` 其它值类型（`int`/`dict`）通用契约。

### 32.4 验证

| 项 | 命令 / 口径 | 结果 |
| --- | --- | --- |
| 语法 | `python -m py_compile cli/main.py` | 通过 |
| 动态调用点复算 | 同 §32.2 的 AST 口径 | **17 → 15**，减少的 2 处均为 `_handle_gc` 的 `mode_key` |
| 迁移 key 渲染 | `i18n.t(key)` × 4 key × 双语 | 8/8 均返回非空文本（zh: 预演/执行/预演/应用导入；en: dry run/apply/dry-run/apply import） |
| 门禁全量 | `pytest tests/test_i18n_placeholder_consistency.py tests/test_i18n_key_existence.py` | **95 passed**（基线 90，新增 5） |
| 治理回归 | `pytest tests/test_i18n_placeholder_consistency.py tests/test_i18n_key_existence.py tests/test_cli_governance_gate.py` | **106 passed / 0 failed** |
| 负向实验 | 临时新增 `cli/_tmp_dyn_probe.py`（`t(k)`） | 门禁失败并报 `[('cli/_tmp_dyn_probe.py', 'probe', 't', 'k')]`；删除探针后复跑 `1 passed` |
| 注册表基线 | `cli.categories.COMMAND_CATEGORIES` | 21 分类 / 84 命令 / 12 title_key / 44 desc_key = 56，双语 0 缺失 |
| 验收矩阵 | `python scripts/check_e2e_matrix_closure.py` | **PASS（收口条件全部满足）** |

### 32.5 边界

| 类别 | 说明 |
| --- | --- |
| 本次范围 | `cli/main.py` 2 处迁移 + `tests/test_i18n_placeholder_consistency.py` §32 门禁（5 用例）+ backlog §32 |
| 非目标 | daemon `message_key` 的运行时存在性校验；en 文案打磨；`t()` 其它值类型（`int`/`dict`）通用契约 |
| 保留问题 | D 类 6 处与 E 类 3 处的 key 在运行时才确定，allowlist 只能防新增、不能证明取值一定存在；A 类 3 处是 `t()` 公开入口本身，天然动态 |
| 白名单维护 | 迁移或新增动态调用点必须同步修改 `DYNAMIC_KEY_CALL_SITES`；否则门禁以「新增未登记 / 清单消失」明确报错，不允许静默放行 |
| 回滚 | 回退 3 文件提交即可；无 catalog 改动、无数据迁移，原有文案与输出不变 |

## 33. message_key 兜底审计与 bundled 双语覆盖冻结（T-1793614942434-83a49fb4）

### 33.1 现象

§32.5 登记的保留问题——「D 类 6 处与 E 类 3 处的 key 在运行时才确定，allowlist
只能防新增、不能证明取值一定存在」——就是本节的缺口。`DYNAMIC_KEY_CALL_SITES`
冻结了**调用点形状**，但没有冻结「可 emit 的 key 集合 → 兜底文案」的映射闭环。

9 处解析点的兜底链统一为三层：

1. catalog 嵌套（`i18n/{lang}.json` 的 `errors.*` / `warnings.*`）；
2. 模块内 bundled 双语默认（`_CONTRACT_BUNDLED_DEFAULTS` /
   `_EVIDENCE_BUNDLED_DEFAULTS` / `_GATE_BUNDLED_DEFAULTS` /
   `_BUNDLED_DEFAULTS` / `_EVALUATOR_BUNDLED_DEFAULTS` / `_VIEW_BUNDLED_DEFAULTS`）；
3. key 本身。

第 3 层是**裸 key 泄漏**：catalog 与 bundled 都缺失时，zh/en 用户看到的都是
`errors.foo` 这类内部标识，而不是可读文案。既有门禁（§26 引用即存在、§27/§28
占位符、§30 值类型、§32 动态调用点）都只覆盖 catalog 字面量调用，不检查
「错误码/警告码 → key」常量表的闭环。

基线审计发现 **1 处真实缺口**：`errors.contract_lease_denied` 是
`CONTRACT_I18N_KEYS` 的 `LEASE_DENIED` 项，在
[db/db_task_contracts.py](file:///c:/git_work/callwarden/db/db_task_contracts.py#L904-L916)
的受保护写路径真实抛出；该 key 在 zh/en catalog 可达，但**未登记**
`_CONTRACT_BUNDLED_DEFAULTS`。catalog 文件缺失或加载失败时，该失败路径退化为裸 key。

同一 key 还有第二个更隐蔽的缺口：catalog 文案使用 `{reason}`，而调用点实际传
`lease_code` / `lease_detail` / `task_id`。由于 catalog 优先，`format()` 抛
`KeyError` 后被静默吞掉，用户看到的是**未替换的裸占位符**
（`受保护变更的 Lease 校验未通过：{reason}`）。这属 §27.5 登记的「静默等价」
在动态 key 路径上的实例：静态占位符门禁只覆盖字面量调用，`t(message_key)` 看不到。
补 bundled 而不修 catalog 无法让修复生效，因此两者同批处置。

### 33.2 基线（改动前）

| 模块 | resolver | emit key | bundled key | emit − bundled | catalog 双语可达 | 未知 key |
| --- | --- | --- | --- | --- | --- | --- |
| `db/db_task_contracts.py` | `_resolve_contract_message` | 12 | 11 | **{errors.contract_lease_denied}** | 1/12（仅 lease_denied） | 返回 key 本身 |
| `db/db_task_evidence.py` | `_resolve_evidence_message` | 10 | 10 | ∅ | 0/10 | 返回 key 本身 |
| `db/db_task_gate.py` | `_resolve_gate_message` | 6 | 6 | ∅ | 0/6（`error.identity_incomplete` 在 catalog 的 `daemon_errors` 扁平段，嵌套不可达） | 返回 key 本身 |
| `experiments/blind_review_protocol.py` | `_resolve_message` | 10 | 10 | ∅ | 9/10（`errors.experiment_invalid_protocol` 缺） | 返回 key 本身 |
| `experiments/blind_review_evaluator.py` | `_resolve_evaluator_message` | 7 | 7 | ∅ | 7/7 | 返回 key 本身 |
| `experiments/blind_review_views.py` | `_resolve_view_message` | 6 | 6 | ∅ | 6/6 | 返回 key 本身 |

补充基线：

| 项 | 口径 | 基线结果 |
| --- | --- | --- |
| bundled 双语完整性 | 6 模块全部 bundled 条目 `zh_CN` / `en_US` 非空 | **0 缺口** |
| CLI 结构化默认 | `cli/main.py` `_IDENTITY_REASON_DEFAULTS`（3）+ `_DEFAULTS`（2）→ catalog `daemon_errors` 扁平段 | 27 key，zh/en 集合相等；5/5 可达 |
| `_t_structured` 解析路径 | `t()` 按点拆分无法处理含点扁平 key（`daemon_errors.error.identity_incomplete`） | 依赖 raw-JSON 扁平查找；未知 key 返回 `default` |
| MCP 协作工具 | `server/tools/tools_collab.py` `_collab_error_response` | **无调用点**（登记豁免，不在运行时 emit 路径） |
| 基线门禁 | `pytest tests/test_i18n_placeholder_consistency.py tests/test_i18n_key_existence.py -o addopts='' -q` | **95 passed** |

### 33.3 处置口径（冻结）

1. **补齐唯一缺口**：为 `errors.contract_lease_denied` 在
   [db/db_task_contracts.py](file:///c:/git_work/callwarden/db/db_task_contracts.py#L145-L190)
   的 `_CONTRACT_BUNDLED_DEFAULTS` 增加 `zh_CN` / `en_US` 文案，占位符与调用点一致
   （`lease_code` / `lease_detail` / `task_id`），使 6 个 resolver 模块全部满足
   `emit ⊆ bundled`。
2. **冻结 emit 集合**：门禁显式列出 6 个模块的 `(映射表, bundled 表, resolver, emit 数)`
   基线；`emit` 取错误码映射表 + 警告码映射表的 values 去重。集合成员或数量变化必须
   同步更新基线，否则失败。
3. **双语可达三路判定**：每个 emit key 在 zh/en 下至少命中一条路径——
   catalog 嵌套、catalog `daemon_errors` 扁平段、bundled；三条都不可达即失败。
4. **bundled 条目双语非空**：每个 bundled key 必须有非空的 `zh_CN` 与 `en_US`；
   只补一种语言视为失败。
5. **未知 key 行为显式化**：对 6 个 resolver 传哨兵 key，断言**返回 key 本身且不抛异常**。
   这是可观察的兜底终点，不是静默分支；门禁把它固定下来，防止未来改成抛异常或空串。
6. **catalog 优先**：monkeypatch catalog 加载，断言已收录 key 优先取 catalog 文案而非
   bundled 文案，保证 bundled 只是兜底、不篡改生产文案。
7. **语义与顺序不变**：不改 `t()` / `t_list()`，不改 resolver 的三层顺序；catalog 只修
   `errors.contract_lease_denied` 的占位符契约（`{reason}` 在调用点不存在，属可读性缺陷
   修复），其余 catalog 文本一律不动。

非目标：`tools_collab._collab_error_response` 死代码清理、en 文案打磨、
`t()` 其它值类型（`int`/`dict`）通用契约。

### 33.4 验证

| 项 | 命令 / 口径 | 结果 |
| --- | --- | --- |
| 语法 | `json.load` 两个 catalog + `py_compile db/db_task_contracts.py` | 通过 |
| emit ⊆ bundled 复算 | 6 模块映射表 values 去重 | **contracts 12/12**；evidence 10/10；gate 6/6；protocol 10/10；evaluator 7/7；views 6/6 |
| lease_denied 渲染 | `_resolve_contract_message(key, {task_id, lease_code, lease_detail})` × 双语 | 2/2 非空，**0 残留花括号**（修复前 zh/en 均为 `...{reason}`） |
| 门禁全量 | `pytest tests/test_i18n_placeholder_consistency.py tests/test_i18n_key_existence.py -o addopts='' -q` | **103 passed**（基线 95，新增 8） |
| 治理回归 | 上述两文件 + `tests/test_cli_governance_gate.py` | **114 passed** |
| 负向实验 | 临时删除 `_CONTRACT_BUNDLED_DEFAULTS['errors.contract_lease_denied']` | `cover_all_emitted_keys` 报 `bundled key 数量漂移 11 != 12`、`lease_denied_placeholders` 报 `KeyError`；**2 failed / exit=1**；恢复后 **8 passed** |
| CLI 默认 key | AST 抽取 `_IDENTITY_REASON_DEFAULTS`（3）+ `_collab_output_structured_reason._DEFAULTS`（2） | 5/5 → catalog `daemon_errors` 扁平段双语可达 |
| MCP 调用点 | AST 扫描 `server/tools/tools_collab.py` | `_collab_error_response` **0 个调用点**（豁免固定） |
| 验收矩阵 | `python scripts/check_e2e_matrix_closure.py` | **PASS（收口条件全部满足）** |

### 33.5 边界

| 类别 | 说明 |
| --- | --- |
| 本次范围 | `db/db_task_contracts.py` bundled 1 条 + 两个 catalog 的 `errors.contract_lease_denied` 占位符 + `tests/test_i18n_placeholder_consistency.py` §33 门禁（8 用例）+ backlog §33 |
| 非目标 | `tools_collab._collab_error_response` 死代码清理；en 文案打磨；`t()` 其它值类型（`int`/`dict`）通用契约 |
| 保留问题 | 未知 key 仍**原样返回 key**（可观察兜底终点，不升级为 fail-closed）；§27.5「值不可格式化 vs 占位符拼写错误」的静默等价仍未区分；MCP 构造器保留但无 emit 点 |
| 白名单维护 | `BUNDLED_RESOLVER_SPECS` 的 emit / bundled 数量变更必须同步；新增 `_resolve_*_message` 模块必须纳入清单，否则每个 emit key 失去兜底保证 |
| 回滚 | 回退 5 文件提交即可；catalog 仅改 1 个 key 的占位符（`{reason}` → `task_id`/`lease_code`/`lease_detail`），无数据迁移、无 key 增删 |

## 34. close 终局 assignment 收口（T-1793627972400-4a1e4a0c）

### 34.1 现象与根因

- 实证：`T-1793614942434-83a49fb4` 投影为 closed 后，其 adjudicator assignment
  `A-fbdc95b51dc29015025f5230` 仍为 `queued`——事件投影与物理
  `task_assignments` 均未收敛。
- 根因：daemon `handle_task_close` / `handle_task_cascade_close` 与 Python
  `task_close` 只改任务状态，从不终结 assignment；`complete_assignments`
  只在 report / handoff / governance 退回路径调用。
- 影响：closed 卡仍可被「current/active assignment」查询观察到，下一棒派工与
  治理投影会误判为「仍可领取」。

### 34.2 处置（冻结）

| 层 | 改动 |
| --- | --- |
| 事件投影（权威） | 新增 `assignment_queue::finalize_task_assignments`：任务级全量（`step_id=None, role=None`）追加 `assignment_completed` |
| 物理表（Python 治理面） | 同一事务 `UPDATE task_assignments SET status='completed' WHERE task_id=? AND status='active'` |
| daemon close | `handle_task_close` 接入，响应新增 `completed_assignments`（assignment_id 数组） |
| daemon 级联 | `handle_task_cascade_close` 对每个收尾节点接入（响应结构不变） |
| Python close | `db/db_tasks.py::task_close` 接入 `_finalize_active_assignments`，响应新增 `completed_assignments` |
| 兼容 | 极旧库无 `task_assignments` 表时事件投影照常收敛，物理写按 `no such table` 跳过（与 `persist_claimed_assignment` 同款） |
| 测试 | Rust helper 单测 + daemon close 集成测试 + Python close 回归 |

### 34.3 验证

| 项 | 命令 | 结果 |
| --- | --- | --- |
| Rust helper 单测 | `cargo test --manifest-path rust_ext/Cargo.toml finalize_task_assignments` | **1 passed / 0 failed** |
| Rust close 回归 | `cargo test --manifest-path rust_ext/Cargo.toml task_close` | **11 passed / 0 failed**（含新增集成测试） |
| Python close 回归 | `$env:PYTHONPATH='c:\git_work'; C:\Python314\python.exe -m pytest tests/test_task_close.py -o addopts='' -q` | **24 passed** |
| 验收矩阵 | `python scripts/check_e2e_matrix_closure.py` | **PASS（收口条件全部满足）** |

### 34.4 边界

| 类别 | 说明 |
| --- | --- |
| 本次范围 | `assignment_queue.rs` + `task_collab_lifecycle_apply.rs` + `task_collab_tests_lease.rs` + `db_tasks.py` + `test_task_close.py` + backlog §34 |
| 非目标 | 历史已 closed 卡残留 assignment 的批量清理；reopen 后 assignment 重建；`revoked` 语义变更；schema 迁移 |
| 幂等 | 重复 close 由既有 S1/S2/verdict 门禁拒绝；`finalize_task_assignments` 对无 active 任务为 no-op（不追加事件） |
| 回滚 | 回退上述 5 个代码/测试文件提交即可；无数据迁移、无表结构变更 |
