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

---

## 5. 后续 backlog（未纳入本次步骤）

| 编号 | 项 | 类型 | 状态 |
|---|---|---|---|
| P3-1 | build_graph 增量化（哈希跳过 + 删除感知 + 边重算裁剪） | 性能 | 已修（T-1793263115744-7ac43c98 步骤#3） |
| P3-2 | `workspace_id: int` → `workspace_instance_id` 契约收敛（build_context 族） | 契约 | 已修（T-1793263115744-7ac43c98 步骤#2） |
| P3-3 | `build_graph.unchanged` 字段语义与 Python 契约对齐 | 正确性/可观测 | 已修（T-1793263115744-7ac43c98 步骤#1） |
| P3-4 | `calls.callee_name` 部分索引（`WHERE callee_id = 0`，raw 边按名字解析/查询） | 性能/schema | 已修（T-1793284203427-56a164bc 步骤#3：SCHEMA_VERSION=61，`idx_calls_callee_name_raw` 部分索引，实测见 §4.6.2） |
| P3-5 | `get_callers`/`get_callees` 短名降级分支跨 workspace 泄漏 + 全表扫（C2） | 正确性/性能 | 已修（T-1793317538470-c203e6d4 步骤#3，见 §6） |

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

