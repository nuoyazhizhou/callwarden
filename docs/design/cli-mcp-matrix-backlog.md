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

### 7.5 残留（卡外，保持红；需单独判定）

- **daemon 失联族（164）**：`test_rust_cli_diff` 35、`test_c17` 22、`test_c21` 22、
  `test_c16` 10、`test_http_daemon_default_transport` 9、`convergence/test_m3` 8、
  `test_http_daemon_integration` 7、`convergence/test_t4` 6、`test_task_prompt_e2e` 6、
  `test_workspace_snapshot_rpc_http` 6 等；均自建 spawn，需后续卡统一守卫。
- **native 构建族（72）**：`test_lease_gate_empirical` 20、
  `test_task_step_resolve_e2e` 14、`test_cli_task_lease_parity` 12、
  `test_lease_rpc` 10、`test_windows_daemon_e2e` 9、`test_windows_wsl_authority_e2e` 4 等。
- **未归一「其他」44**（非前置、亦非本轮引入）：i18n 输出文案断言漂移 15
  （`test_cli_055/056/057/058/059/063/065/066/070/076`、`test_i18n_cleanup_c5`）、
  Windows 临时 SQLite 文件锁 teardown `PermissionError [WinError 32]` 13
  （`test_phase3_cas`、`test_p10_p11_timing_gc`、`test_p21_auto_ignore`）、
  `workspace_instance_id` 契约 9（`test_task_cascade_close`、
  `test_task_reconciliation_contract`、`test_http_index_job_cutover`）、
  release 验收 2（evidence 停在 `6bd1e9ce` < 当前 HEAD `6d45bbd`；schema_version
  60 vs 61）、`test_http_compat_read_cutover` DID NOT RAISE 2、
  `test_p0b_legacy_attestation_fail_closed` 1、`test_phase2_6_3` 性能 1、
  `test_task_split_governance` 1。

后续卡建议：① 将「隔离 daemon 按 `data_root` 派生管道名」落到 Rust 侧（根治
管道冲突）；② 抽公共 `daemon_prereq_guard` 覆盖卡外自建 spawn 文件与
`cargo build`/LLM 前置；③ 修 release 验收 evidence 过期与 schema_version 断言。

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
  bench 专用简化 schema，不在生产路径，未纳入本卡范围。

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

