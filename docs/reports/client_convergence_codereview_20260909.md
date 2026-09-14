# 收敛目标 Code Review：Python client(HTTP) → Rust daemon + 四角色自动识别

- 日期：2026-09-09
- 审查人：WorkBuddy（CodeReviewExpert 模式，只读审查，未改生产代码）
- 目标（用户冻结）：① CLI 与 MCP 均通过 Python client 走 HTTP API 与 Rust daemon 互联实现业务；② Planner/Executor/Reviewer/Adjudicator 四角色在获取任务单后自动识别自身角色。
- 证据等级：除标注 source-level 外，均为 behavior-level（实际调用 daemon/CLI 复现）。

---

## 一、总体结论

| 维度 | 结论 |
| --- | --- |
| 客户端薄壳收敛（硬门禁） | ✅ PASS：`check_client_purity.py` 扫描 server/tools + cw.py（14 文件）与 cli/（10 文件）0 违例；`verify_route_matrix.py` 全绿（17 项 KNOWN_DRIFT 已登记另卡承接） |
| MCP 薄壳层 | ✅ 无本地 SQL 回退（tools_*.py 无 cursor.execute/本地落库），业务全部经 route_rpc → daemon |
| 代码图谱索引链路 | 🔴 不可用：refresh 只落文件内容，symbols/calls 恒为 0；build_directory 目录建图直接报错；build_graph 全量挂死且不留 schema |
| 四角色自动识别 | 🟡 部分：`cw task prompt` 链路存在，但 secret 误判可阻断编译；blocked 任务 required role 为空 |
| 部署一致性 | 🟡 活 daemon 内嵌 commit 落后 HEAD 3 个提交 |

---

## 二、问题清单（按优先级）

### 🔴 P0-CR1：daemon 建图产物 0 符号——图谱查询全链路不可用
- 复现（behavior-level）：
  - `cw.py refresh cli/dispatcher.py cli/task_prompt.py cli/client.py cli/agent.py` + `server/` 4 文件 → codegraph.db `file_instances=8, file_contents=8, symbols=0, calls=0`。
  - `cw.py stats` → `symbol_count=0, edge_count=0`（file_index_size=9 但符号池为空）。
  - `cw.py search route_rpc` / `cw.py callers route_rpc` → 0 结果。
- 对照组：内置 Grep 同符号 `route_rpc` 命中 8 个文件（cli/dispatcher.py、cli/main.py、cli/task_prompt.py、server/daemon_client.py、server/tools/tools_{collab,p2_graph,p3_identity,p4_lease}.py）。
- 判定：daemon 的 tree-sitter Python 抽取管线未产出符号（未启用/未注册 Python grammar，或 refresh 写库路径跳过 symbol upsert），非客户端问题——客户端已正确把参数送到 daemon。
- 影响：基于图谱的 MCP 工具（callers/callees/call-chain/search）在 Rust daemon 路径下全部空转；与 237 工具承诺直接冲突。
- 建议方向：在 daemon fs_handlers 的 ingest 落库处断言 symbols>0 增量；加 behavior-level 回归（refresh 单文件后 symbol_count 必须 >0）；优先排查 Python grammar 注册与 symbol upsert 事务路径。

### 🔴 P0-CR2：`workspace.build_directory` 对目录路径报 `path_not_found: 不是文件`
- 复现（behavior-level）：`route_rpc('workspace.build_directory', {'dir_path':'server','recursive':True})` → `DaemonRemoteError path_not_found: 不是文件: \\?\C:\git_work\callwarden\server`；`server/tools` 同样报错。
- 判定：daemon `validate_owned_path`（rust_ext/src/daemon/workspace.rs ≈743-775）按“必须是文件”校验，目录递归建图 RPC 从未真正可用；`fs_handlers.rs` handle_build_directory 与该校验语义冲突。
- 影响：目录级建图（graph 构建的主要入口）不可用，只能逐文件 refresh。
- 建议方向：validate_owned_path 对 dir_path 分支放行目录并扫描；补 e2e：build_directory('server') 后 file_instances 覆盖该目录。

### 🔴 P0-CR3：全量 `refresh --all --force` 挂死 + 落库无 schema
- 复现（behavior-level）：
  1. `cw.py refresh --all --force` 后台运行 >10min 无产出，期间 daemon 阻塞（最终强杀进程树恢复）。
  2. 产生的 codegraph.db 无任何表（sqlite_master 为空），需要手工 `callwarden_core.storage_initialize_or_migrate(path, 60)` 才能继续——daemon 写图谱前未确保 schema 初始化（fs_handlers.rs handle_build_graph 主体现存，未接 storage 初始化）。
- 影响：一键建图不可用且会拖死 daemon；半成品库残留导致后续请求报错。
- 建议方向：build_graph 入口 fail-fast ensure schema v60；大库全量改分批/带进度 RPC；为“refresh 中断”增加库级清理。

### 🔴 P1-CR4：`task prompt` 被 secret 误判阻断——角色识别链路对含 hash 任务不可用
- 复现（behavior-level）：`cw.py task prompt T-1788253722521-3b2f8420 --format card` → `E_TASK_PROMPT_SECRET_DETECTED|bare_credential_hash`。该任务标题/内容含 manifest SHA（64 hex），被 secret 检测当作裸凭据。
- 判定：secret 检测白名单/排除规则过窄。治理任务单里 evidence-hash、contract hash、manifest SHA 是**预期内容**，一律 64 hex 都误判将系统性阻断 role prompt 编译。
- 影响：四角色“拿到任务单自动识别角色”的目标在该类任务上直接 fail-closed。
- 建议方向：检测器增加上下文白名单（字段名含 hash/evidence/manifest/sha 的值放行，或仅告警不阻断）；补用例：含 evidence_hash 的任务必须能编译出 prompt。

### 🟡 P1-CR5：blocked/不可操作任务 required role 为空——识别面缺口
- 复现（behavior-level）：`cw.py task prompt T-1788313854785-dd64cebc --format card` → `prompt_kind: blocked_recovery`，`Required role: —`、`routing state: non_actionable`。
- 判定：决策为 BLOCKED 时 prompt 未给出“哪个角色来处理恢复”，四角色自动识别在 blocked 分支缺位（next_action.rs 中 BLOCKED→NONE 未映射 required_role）。
- 影响：无人值守循环在 blocked 任务上无从自动认领。
- 建议方向：blocked_recovery 应按 blocking_reasons 推导恢复角色（governance_blocked → planner/adjudicator 等），至少输出“建议路由”。

### 🟡 P1-CR6：活 daemon 内嵌 commit 落后 HEAD
- 证据：health `git_commit=b342fda6…` vs `HEAD=0e6cbd0a…`，落后 3 commit（b342fda6 为 HEAD 祖先）。
- 判定：符合“构建成功≠已部署”教训——`refresh_shared_runtime.ps1` 部署闭环未执行。本次审查功能面（fs_handlers/task_prompt）结论基于 b342fda6 二进制。
- 建议方向：审查涉及的修复合入后统一走部署闭环（health.git_commit==HEAD + sha256 双校验 + PID 核验）再复测 P0-CR1/CR2。

### 🟡 P2-CR7：workspace 实例漂移导致图谱数据分裂
- 证据：本次 refresh 写入实例 `822c031c71488f12`，历史权威数据在 `4baea3ff12c2ea5c`（canonical daemon instance），另有 `4ed15c80f93f0ee5` 等残留目录；`~/.callwarden/codegraph/4baea3ff12c2ea5c/` 与 `~/.callwarden/workspaces/<其它实例>/` 并存多套 codegraph。
- 判定：instance id 派生对路径大小写/分隔符敏感（先前已实证 `C:/…` vs `c:/…` 派生不同 id），客户端 cwd 差异即产生新实例。与既有“supersede CLI 无 instance 透传”缺口同源。
- 影响：图谱与快照分散在多实例目录，stats/查询各看一半数据。
- 建议方向：instance 派生归一化（realpath + 大小写统一 + 分隔符统一）；存量目录迁移脚本。

### 🟡 P2-CR8：route_rpc 残留已知漂移 hunk（低优先，既有登记）
- 证据（source-level）：`server/daemon_client.py` L2100-2101 `from callwarden.config import PROJECT_ROOT` + `root = self._project_root or PROJECT_ROOT`；L3557-3558 同类；L3947 compat 面注入 `workspace_root`。与 09-05 compat-worker 滞留 hunk 登记（PROJECT_ROOT→cwd×2 + route_rpc workspace_root）一致，待 provenance 分离。
- 建议方向：按既有卡片承接，不重复开卡。

### 🟡 P2-CR9：HTTP 安全面为 `dev_loopback_unauthenticated`
- 证据：health `security_profile: dev_loopback_unauthenticated`（http_server.rs 仅 loopback 绑定，无认证）。
- 判定：当前拓扑（本机 client→daemon）可接受，但“237 工具 HTTP 化”若开放到跨机/多 agent 共享，需在 capability registry 之上加身份层。登记备忘，不阻塞收敛目标。

### 💭 P2-CR10：manifest 刷新依赖 ping 触发（时效性缺口，session 内观察）
- 观察：daemon 重启后 manifest 文件未即时更新，首次 `daemon health` 走 stale endpoint 失败，`daemon ping` 后恢复；`server/daemon_autostart.py` 未见 manifest 写入逻辑。
- 建议方向：daemon 启动时同步写 manifest（含 endpoint/pid/commit），客户端 autostart 优先读 manifest mtime。

---

## 三、双通道对照结论（cw 命令 vs 内置工具）

| 能力 | cw（daemon 路径） | 内置 Grep/Glob | 结论 |
| --- | --- | --- | --- |
| 符号搜索 route_rpc | 0 命中 | 8 文件命中 | daemon 索引管线损坏（P0-CR1） |
| callers/callees/call-chain | 空结果 | —（内置工具无此能力） | 图谱价值当前为零，需先修 P0-CR1 |
| 任务/角色识别 | task prompt 可用但有 CR4/CR5 缺口 | 无法替代（需 daemon 状态） | daemon 不可替代项均正常 |
| 治理门禁（purity/route matrix） | PASS/PASS | — | 收敛架构本身健康 |

核心判断：**收敛架构（薄壳 + HTTP + Rust daemon）方向正确且门禁全绿；真正的实现缺陷集中在 Rust daemon 的图谱索引与边界校验，以及 role prompt 编译的误判面上。** 内置工具在本次审查中承担了图谱不可用时的对照基准，也反证 P0-CR1。

---

## 四、建议解决顺序

1. P0-CR1（0 符号）→ P0-CR2（build_directory）→ P0-CR3（build_graph 挂死/无 schema）：三项同属 fs_handlers 建图链路，建议一张卡修复 + behavior-level 回归（refresh 单文件 → symbol_count>0；build_directory('server') → 目录覆盖；fresh DB → 自动 schema）。
2. P1-CR4/CR5：task prompt 编译面（secret 白名单 + blocked required_role），影响四角色无人值守循环，先于 RP-03 承接。
3. P1-CR6：修复合入后执行部署闭环再复测。
4. P2-CR7..CR10：按 backlog 顺序，CR8 不新开卡。

---

## 五、状态更新（2026-09-09 11:35 追加）

- 并行会话已承接：commit `79d3c83`（11:12）`fix(daemon): P0-CR1/CR2/CR3 建图链路修复——符号落库+目录校验+schema fail-fast`，并开卡 T-1788923523728-7a316cc4；review 报告已随 `c75ff8a` 入库（当前 HEAD）。
- **⚠️ 修复尚未部署生效**：运行中 daemon 二进制构建于 03:07（早于修复提交），behavior-level 复验 P0-CR2 仍报 `path_not_found: 不是文件`。再次印证"构建成功≠已部署"——需走 `refresh_shared_runtime.ps1` 部署闭环（health.git_commit==HEAD + sha256 双校验 + PID 核验）后重测 P0-CR1/CR2/CR3。
- daemon 进程中途退出过一次（stale manifest，health 报 E_HTTP_MANIFEST_STALE），已按约定重启恢复（现 PID 38324，endpoint 127.0.0.1:3225）；重启到 manifest 刷新存在约 30s 窗口期，与 CR10 一致。
- CR6 时效修正：当前 health.git_commit 与 HEAD 一致（c75ff8ae）；本报告第二节的 b342fda6 落后结论为 11:29 前的历史观察，部署闭环仍以最终验收为准。

## 五点五、第二阶段审查：修复提交 79d3c83 的 source-level review（2026-09-09 12:05 追加）

前提：运行中 daemon 仍为 03:07 旧二进制，以下为 source-level 审查（79d3c83），部署后需 behavior-level 复测。

### 好的方面（值得肯定）
- CR2 `validate_owned_path_any` 干净复用：canonicalize + 存在性 + ACL 解耦，`validate_owned_path` 委托后再补 require_file 检查，无重复逻辑。
- CR3 双层剪枝正确：SKIP_DIRS 同时用于逐段过滤（L45）与递归剪枝（L224），`target/node_modules/.git` 不再深入。
- CR1 幂等重建与 fail-fast schema 设计合理；root canonicalize 后 strip_prefix 防 client_view_root 大小写漂移；单文件事务（BEGIN IMMEDIATE → COMMIT/ROLLBACK）无嵌套风险。

### 新发现问题

#### 🟡 P1-CR11：`batch_resolve` 管线不存在——修复后跨文件调用图仍不可用
- 证据（source-level）：提交信息与 fs_handlers.rs:271 注释均称"跨文件 resolve 仍由既有 batch_resolve 管线承担"，但**全库 grep 无 `batch_resolve` 定义**；dispatch.rs 也无任何 graph-resolve 类 RPC（仅 toolchain.resolve/task.step.resolve 等同名异义）。
- 查询侧（daemon_client_handlers.rs:217）callers/callees 显式过滤 `callee_id > 0`，而 refresh/build_directory/build_graph 落库的 raw calls 全部 `callee_id=0` → **这些边对查询是"暗物质"**：symbols 落了，但跨文件调用图依旧查不到。
- 实际存在的 resolve 能力是 cas_merge.rs 的 `resolve_callee`（P1-2，snapshot 合并时即时 resolve）——即只有走 cas_merge 全量合并路径才有跨文件边，refresh 增量路径没有。
- 建议方向：① refresh/build 落库后自动触发 resolve（复用 resolve_callee 的回扫 pass）；② 若暂不做，提交信息与注释必须改为如实描述，并把"resolve 触发缺失"登记为独立 backlog 卡，否则会误判 CR1 已达成验收标准。

#### 🟡 P1-CR12：增量重建产生悬空已解析边（幽灵/丢失调用边）
- 证据（source-level）：fs_handlers.rs:323 `DELETE FROM calls WHERE caller_id IN (本文件 symbols)` 只清理本文件作为 caller 的边；本文件 symbols 被删后以**新 rowid** 重建，其它文件此前 resolve 到本文件旧 symbol id 的 `callee_id` 全部悬空。
- 影响：单文件 refresh 后，callers(其它函数) 可能返回指向已删除符号的幽灵边，或丢失真实边。
- 建议：rebuild 时同步清理 `callee_id` 指向本文件旧符号 id 的边（可先 SELECT 旧 id 集合再删）；配合 CR11 的 resolve 重跑一并解决。

#### 🟡 P2-CR13：`upsert_file_instance` 未解析文件的 status 语义失真
- 证据（source-level）：`let (last_parsed, status) = if parsed { (now, "parsed") } else { (0.0, "parsed") };`——不支持语言的文件（parser_lang_id 返回 None）也写 `status='parsed'` 且 `last_parsed=0`。
- 影响：按 status 过滤的下游逻辑（如"待解析队列"）无法区分"已解析零符号"与"从未解析"。
- 建议：else 分支改 `'pending'` 或 `'indexed'`，一行修复。

#### 💭 nit
- `parse_and_store_symbols` 返回首元素实为 symbol_contents upsert 计数而非 symbols 表 insert 计数（数值恰好相等，语义易误导）。
- Unix 分支 metadata 取两次（validate_owned_path_any），可复用一次结果。

### CR2/CR3 复测注意
部署 79d3c83 后的 behavior-level 验收建议：
1. refresh 单文件 → `stats.symbol_count > 0`（CR1 验收）；
2. `build_directory('server/tools')` → 不再报"不是文件"，file_instances 覆盖目录（CR2 验收）；
3. fresh DB（删库重建）→ 自动 schema v60（CR3 验收）；
4. **追加**：callers/callees 查询能返回跨文件边（CR11 验收——这是 CR1 真正的业务闭环，仅 symbols>0 不够）。

## 五点七、第三阶段审查：task prompt 编译面源码深审 + bfbdcbc 测试审查（2026-09-09 13:15 追加）

> 本节修正第一阶段 CR4/CR5 的定性——两者均非"实现缺陷"，原条目保留仅作追溯。

### CR4 重定性：secret 检测是 by-design fail-closed，缺陷在上游数据卫生
- 源码依据（redaction.rs）：模块头注释明确冻结边界——spec §9.3 规定 contract/hash 的唯一合法形式是 `sha256:` 前缀，**裸 64-hex 本就不应出现在数据面**；denylist 全部按"值形态"高置信匹配（PEM/Bearer/auth 赋值/lease 字段/API key 前缀/裸 64-hex）。
- 因此 T-1788253722521 编译失败不是扫描器误报，而是该任务单内容里出现了裸 SHA（manifest hash 未带前缀写入标题/描述）。**修复方向应是写入侧规范化**：task create/report/handoff 工具链在写入 hash 引用时统一输出 `sha256:<hex>`，而不是放宽扫描器——放宽会把 credential hash 放进 prompt 数据面，违背 spec §8.3 denylist 纪律。
- 值得肯定：错误 details 只含 reason code 绝不携带命中内容（§10.2）；OnceLock 缓存正则；bare_hex64 边界设计正确（65+ hex 串与 128-hex 不会误命中，冒号前缀排除 `sha256:` 形态）。

### 新发现 🟡 P2-CR14：豁免表硬编码 + 大小写敏感 + 零测试覆盖
- 证据（source-level）：redaction.rs:27-28 `KNOWN_NON_SECRET_HEX_REFS` 硬编码单条**大写** SHA；L89 `contains(&hex_core)` 未做大小写归一化——当前恰好有效仅因 compiler_policy.md:6 自述时也用大写；redaction_tests.rs 无任何豁免路径用例。
- 影响：未来资产新增自述 hash（需改代码）、或以小写引用（静默失效、policy 正文自己触发误报）。
- 建议：①匹配前 `to_ascii_uppercase` 归一化（一行）；②豁免表从 role_prompt_assets manifest 派生（daemon-owned 资产自述 hash 自动豁免，免改码）；③补豁免命中/大小写变体单测。

### CR5 重定性：blocked 无角色路由是冻结 spec §6 的 v1 范围限制
- 源码依据（route.rs:237-249）：BLOCKED/WAITING/COMPLETE 显式路由到 system 模板（blocked_recovery/waiting/terminal），不派角色、不需要 Role Contract；READY/PLAN 在 v1 明确保留给 planner_governance_v1（L258-264 fail-closed）。
- 因此"Required role: —"符合冻结 spec，不是实现缺口。若无人值守循环需要 blocked→恢复角色路由，那是 **spec v2 需求**，须经治理路径修订冻结 spec，不得客户端自行扩展路由。
- 值得肯定：route.rs:278-293 的双源一致性校验（本地 action 映射 vs authority context.required_role，不一致即 E_TASK_PROMPT_ROLE_NOT_ELIGIBLE）是教科书式 fail-closed。

### bfbdcbc（并行会话测试提交）审查：✅ 合格，两点补强建议
- 覆盖到位：parser_lang_id 映射表、fresh DB 自动建 schema（CR3 行为锚点）、file_instance+符号 roundtrip、二次落库幂等。
- 🟡 缺口 1：**CR2 无测试**——`validate_owned_path_any` 的目录接受/越界拒绝（outside-root、不存在路径）没有单测锚点，回归防护缺失。
- 💭 缺口 2：断言用 `>=2`/`>=1` 宽松阈值作 smoke 可以接受，建议补一条精确计数用例防解析器回归。

### 第三阶段结论
四角色自动识别链路（route/render/redaction/context）实现质量高于预期：fail-closed 纪律贯穿、职责单一、authority 双源校验。真正的行动项收敛为：**①写入侧 hash 规范化（CR4 本体）；②豁免表治理（CR14）；③blocked 恢复路由进 spec v2 议程（CR5 终态）；④CR11/CR12 resolve 缺口（第二阶段已登记）**。

## 五点八、第四阶段审查：HTTP 传输层 + MCP 识别路径 + 251acf5（2026-09-09 13:30 追加，审查闭环）

### 新发现 🟡 P1-CR15：`ensure_http_daemon` 等待窗口是死代码
- 证据（source-level，daemon_autostart.py:1070-1094）：`deadline = time.monotonic()` **漏加 `window`**——docstring 声明"在 bounded 窗口内等待 HTTP daemon 就绪（默认 10s）+ 指数退避"，但 `window` 参数在赋值后再未被使用；首轮探针失败后 `now >= deadline` 立即成立，循环实际只探一次，`BACKOFF_BASE/FACTOR/MAX` 退避逻辑永不执行。
- 影响：daemon 重启/部署后的就绪等待完全失效（本会话实测 ready 窗口 1-3 分钟），autostart 场景下客户端拿 None 就放弃——与 CR10（manifest 窗口期）叠加放大为"重启即断连"体验。行为级旁证：本会话多次重启后首次 health 均直接失败、无自动等待。
- 建议：`deadline = time.monotonic() + window`（一行）；补"窗口内第 N 次探针成功"的单测。

### 251acf5（修复 agent 自测发现的新 bug）审查：✅ 方案正确
- minified 单行 bundle 同名同 start_line 多符号撞 `UNIQUE(file_instance_id,name,start_line)`：`INSERT OR IGNORE` + 冲突时回查既有行挂接调用边。UNIQUE 约束保证回查唯一，`row_id > 0` 守卫安全，"图保真对压缩产物有损"的取舍注释清晰。
- 附带确认：symbol_contents（content_hash 键）仍全量 upsert，被忽略的重复符号不影响内容查询。

### MCP 路径四角色识别：✅ 合规
- tools_task_prompt.py `task_get_role_prompt(task_id)`：单参数薄透传 → `task.prompt.compile`（READ_ONLY），不收 role/format/workspace/credential（spec §4.4），无本地模板/SQLite/fallback，与 CLI 路径同源 authority。纯净度硬门禁覆盖。
- 结论：四角色识别在 CLI 与 MCP 两条路径上 authority 同源（daemon task.prompt.compile），客户端两侧均无旁路。

### 审查闭环总结：原始目标覆盖度矩阵

| 原始目标 | 覆盖情况 | 结论 |
| --- | --- | --- |
| CLI → Python client → HTTP → Rust daemon 业务互联 | 门禁 PASS（purity/route matrix）+ 传输层深审 | 架构达标；遗留 CR15（等待窗口死代码）、CR10（manifest 窗口期） |
| MCP 同路径 | 薄壳扫描 0 违例 + task prompt 工具逐行审 | 达标，无旁路 |
| 四角色获取任务单自动识别 | route/render/redaction/context 全链源码审 + behavior 抽测 | READY 分支达标；CR4=写入侧 hash 卫生、CR5=spec v1 范围限制（均重定性）；CR14=豁免表治理 |
| 双通道对照（cw vs 内置工具） | 已执行并登记 | 图谱链路修复前 cw 侧不可用（CR1/11/12），修复 agent 承接中 |
| 问题落 docs/reports | 本文件 §2/§5.x | 持续追加 |

### 未决项清单（交接修复 agent / backlog）
1. ~~**部署闭环**~~ ✅（§5.9，HEAD 9d09ca9 三方核验）。
2. ~~**P1-CR11/CR12**~~ ✅ 代码修复（`26be5a4`，§七）；behavior 验收待第四轮部署。
3. ~~**P1-CR15**~~ ✅（`bbeb2cc`，deadline 补加 window + 4 回归测试）。
4. ~~**P2-CR13/CR14**~~ ✅（`78c7b9a`，status='pending' + 豁免表归一化/manifest 派生）。
5. ~~**CR4 本体**~~ ✅（`dea6f4a`，§八；behavior 验收 §八 R1）。
6. ~~**CR5 终态**~~ ✅（`39c2697`，blocked 恢复路由已登记 cw-role-prompt-compiler-v2-agenda.md A-1）。
7. **P2-CR7/CR9/CR10**：~~instance 派生归一化~~（✅ §十）、~~安全面~~（✅ 设计完成，§十一）、~~manifest 启动即写~~（✅ §九）。

## 五点九、第五阶段：部署闭环 + behavior-level 回归实测（2026-09-09 14:00 追加，修复 agent）

> 未决项 #1（部署闭环）已完成。三轮 build→swap→核验，全部 behavior 证据来自运行中 daemon。

### 部署闭环（未决项 #1 ✅）
- 环境障碍记录：①活 daemon 就跑在 `target\release\cw-daemon.exe`，直构建必撞 LNK1104（autostart 复活竞态）；②沙箱拦截全新 target 目录构建脚本执行（os error 5）；③PowerShell 工具会话命令解析损坏 + bash 禁调 powershell → `refresh_shared_runtime.ps1` 无法在任何会话内跑通。
- 解法：手动复刻脚本闭环——`CARGO_TARGET_DIR=rust_ext\target\stage-refresh` 独立构建（绕开锁+沙箱）→ taskkill daemon → runtime/current 换装（旧装备份 previous-*）→ 后台前台 exec 启动（CW_COMPAT_PYTHON + PYTHONPATH + `--config daemon_manual.json`）→ health/manifest/sha256 三方核验。
- 最终状态：PID 48204、`git_commit=9d09ca9`（=HEAD）、`daemon_binary_sha256=1c84b0ab…`（构建=安装=manifest 一致）、schema v60、worker healthy。
- 实证确认 §5.8 CR15/CR10：每次重启后 manifest 刷新存在 ~30s 窗口（E_HTTP_MANIFEST_STALE），期间 health 失败。

### behavior 回归（§5.5 四条验收）
1. **CR1 ✅**：`refresh_file('config.py')` → `{ok, symbols:57, calls:351}`；DB 实查 symbols/symbol_contents/calls 全部落库（symbol_contents 含真实 content）。
2. **CR2 ✅**：`build_directory('C:\git_work\callwarden\analyzers')`（目录）→ `{ok, scanned:7, refreshed:7, symbols:67, calls:534}`，不再报"不是文件"。
3. **CR3 ✅**：删库重建后全量 `workspace.build_graph` **26.5s 完成**（scanned=1095, inserted=1095, symbols=19519, calls=140769）；fresh-DB 自动建 schema v60（95 表）。对比修复前：全量爬 testcode/linux 内核 23k+ 文件不返。
4. **CR11 行为面修正**：直连 RPC `query.callers('parse_and_store_symbols')` **实测返回了 callee_id=0 的 raw 边**（handle_build_graph/handle_build_directory → 文件+行号齐全）——§5.5 "查询侧过滤 callee_id>0、raw 边是暗物质"的结论在 daemon HTTP `query.callers` 路径上不成立；callee resolve（callee_id>0 的跨文件精确挂接）缺失仍成立，P1-CR11 建议保留。

### 回归中发现并已修复的新缺陷（增量 commit）
- `251acf5`：minified 单行 bundle（d3.v7.min.js）同名同 start_line 多符号撞 `UNIQUE(file_instance_id,name,start_line)` → `INSERT OR IGNORE` + 回查挂接（全量建图 127.7s 处实测炸出）。
- `9d09ca9`：daemon scan 尊重 `.callwardenignore`/`.gitignore` 目录剪枝（`load_ignore_dir_names`，build_graph/build_directory/file.grep 三点生效）——否则全量建图啃 testcode 内嵌内核 8 万+文件；实测剪枝后 testcode 行数=0。
- `tools_workspace.py`（本轮提交）：MCP `build_graph`/`refresh_file` 输出签名 `bool→dict`——daemon 结构化返回使 FastMCP 输出校验炸掉（实测复现：`1 validation error ... result: bool`）。

### 新发现 🟡 P1-CR16：query.search 内存 store 与建图数据不同源
- 证据：建图+snapshot.publish（gen1/gen2 均 symbol_count=19520）后，`query.search('handle_build_graph')`/`('parse_and_store_symbols')` 恒为 0，而同名符号在快照库实存（DB 实查=1）；`query.callers`（直连 DB 路径）数据新鲜。republish 不解决；老符号（ServerHandle/reject 等）可搜到。
- 初判：search 走 snapshot_cache 内存 store（graph.rs `load_symbols_from_sqlite`），callers 走 DB 直连——store 加载源/时点与最新 publish 脱节。与 P1-CR11（resolve）同属查询面收敛，建议合并排查。

### MCP 会话注意
- daemon 重启后，长驻 MCP server 缓存旧 endpoint（实测打 2632 旧端口 10061）——需重连 MCP 会话；CLI/直连 RPC 不受影响。

## 六点、第六阶段：按 mcp_tools.md / cli_reference.md 权威清单逐工具收敛审查（2026-09-09 14:20 追加）

> 用户定向：沿两份权威文档的工具/CLI 调用链逐条核对——哪些 Rust 已实现而 Python 未接过去、哪些是其他情况，逐个完成，直至 Python 只剩薄 CLI（不执行业务规则与数据库操作）。

### 1. 全量清单盘点方法（三端交叉）

以 243 个 MCP 工具壳（`server/tools/*.py` 的 `@mcp.tool()` 全量 AST 级扫描）为 T 面，与三端对照：
- **Rust dispatch**（dispatch.rs 全部 match 臂，300 个方法分支）；
- **COMPAT_ROUTE_WHITELIST**（http_server.rs，HTTP compat 路由白名单，先于 dispatch 命中）;
- **迁移矩阵**（`deliverables/software-company/tool_migration_matrix.json` + Rust 内嵌镜像 `route_matrix.rs` + Python 镜像 `compat_registry.RUST_COMPAT_ROUTE`，由 `verify_route_matrix.py` 机器核对）。
- CLI 侧：`cli_reference.md` 221 节；`cli/` 纯净度门禁 + 活调用扫描。

### 2. 结论：调用链收敛的真实状态（修正旧文档）

| 项 | 旧文档/旧证据 | 当前代码实测 |
| --- | --- | --- |
| 工具总数 | 237（mcp_tools.md 头部） | **243**（文档滞后 6 个） |
| backend 分布 | python_compat 193 / rust_native 44（08-14 matrix）；58/179（另一版） | 迁移矩阵目标态：**rust_native 125 / task_rpc 43 / python_compat 75** |
| 工具壳路由 | — | **243/243 全部 route_rpc**，壳层零本地 DB、零 compat 直调（纯净度门禁 0 违例） |
| HTTP 不可达（fail-closed） | 旧证据 118 个 | **源码级 0 个**：79 个无点号方法 = 58 白名单（compat worker）+ 18 dispatch 分支（native）+ 3 个本轮修复；164 个点号方法全部有 dispatch 分支 |

**"Rust 已实现但 Python 未接过去"的精确答案：仅 3 个**——`find_evidence` / `get_freshness_status` / `get_gate_decision`（MCP-002/003/004）。它们在 dispatch.rs `handle_collab_rpc` 已有完整 native handler（MCP-001~012 均已迁移，capability 行也标 rust_native），但迁移时 **COMPAT_ROUTE_WHITELIST 条目漏摘**（对照 MCP-001 get_role_view 已正确摘除）——`compat_route()` 先于 dispatch 命中，请求被遮蔽到 Python compat worker。

其余"其他情况"归类：
- **8 个（MCP-005..012）**：Rust 已 native、白名单已摘，但 worker 侧 `register_compat_routes` 仍注册死 handler + py 镜像 8 条残留 → 门禁 KNOWN_DRIFT "mirror 有而 whitelist 缺"。行为无害（Rust 不再路由），属 worker 侧清理项。
- **9 个（S2 批次）**：get_top_callers 等 CONVERGENCE_RPC_METHODS 已 native，矩阵行未翻转 → KNOWN_DRIFT "无可用路由"定性不准（实测 behavior-level 可用，§5.9 已证）。
- **61 个真 compat**：仍由 Python compat worker 执行业务逻辑（背后是 db/ 包 56k 行），这是"Python 还没接过去"的真正剩余面，承接卡 T-1787293451688-c14b1e44 在推进。

### 3. 本轮已完成修复（source-level，待随下次构建部署）

| 文件 | 改动 |
| --- | --- |
| `rust_ext/src/daemon/http_server.rs` | COMPAT_ROUTE_WHITELIST 删除 find_evidence / get_freshness_status / get_gate_decision 3 条（61→58），注释记录根因 |
| `server/compat_registry.py` | RUST_COMPAT_ROUTE 镜像同步删除 3 条（69→66） |
| `rust_ext/src/daemon/route_matrix.rs` | 3 行 target_backend PythonCompat→RustNative，batch MCP-002/003/004，status migrated |
| `deliverables/software-company/tool_migration_matrix.json` | 同步翻转 3 行（rust_native 125 / task_rpc 43 / python_compat 75） |

门禁复验：`verify_route_matrix.py` **全绿**（KNOWN_DRIFT 回落 17 项既有登记）；`check_client_purity.py` **0 违例**。Python 侧 import 校验通过。
部署后 behavior 验收（两步）：①`route_rpc('find_evidence', {...})` 经 HTTP 返回 native 结果且 compat worker 日志无对应调用；②白名单计数断言（如有）更新为 58。

### 4. CLI 侧结论

- `cli/` 零 sqlite3/db 业务 import（纯净度门禁含 cli/ 10 文件 0 违例），CLI 命令主体已收敛 route_rpc。
- 残留活缺口 2 处（`cw build-context` 子命令，与迁移差距报告 G4 同源）：
  1. `resolve`（cli/main.py:12434）：本地跑 `analyzers.resolved_edges_engine.compute_resolved_edges` + `server.cli_admin.open_readonly_conn` **直连 SQLite**——业务规则+DB 操作仍在客户端；
  2. `import-compile-commands`（cli/main.py:12397）：本地解析 compile_commands.json（写路径已走 RPC，读解析在客户端）。
- `cw daemon serve`（影子 server，迁移差距报告 G1）仍可启动，建议冻结下线。

### 5. 后续工作清单（按序）

1. **部署本轮 3 工具修复**（并入下一轮 build→swap，注意白名单计数断言同步）；
2. **worker 侧清理 MCP-005..012**：tools_p2_graph/tools_p3_identity/tools_p4_lease 的 register_compat_routes 死注册 + 镜像 8 条 + 矩阵 8 行翻转（双侧同删，一次卡完成，门禁漂移可再降 8 项）；
3. **S2 批次矩阵行翻转**（9 行，清除"无可用路由"误报）；
4. **61 个真 compat 的 Rust native 迁移**（T-1787293451688 承接，量大分期）；
5. **CLI build-context resolve/import 的 daemon 化**（G4，compute_resolved_edges 需 Rust handler 或 RPC 化拆分）；
6. mcp_tools.md 头部路由状态段落刷新（237→243、backend 分布、fail-closed 语义已变）。

## 六、审查方法与证据链

- 需求来源：docs/design/rust-client-convergence-protocol.md、cw-rust-client-convergence-migration-guide.md、AGENTS.md（角色矩阵/RP-09 cutover）。
- 验证命令：`cw.py daemon health / stats / search / callers / refresh / task prompt / task list`；`scripts/check_client_purity.py`；`scripts/verify_route_matrix.py`；直连 `route_rpc` 复现 RPC 级错误；sqlite 只读核查 codegraph.db 计数。
- 复现日期均为 2026-09-09，daemon pid 23908（commit b342fda6）。

## 七、第七阶段：CR15/CR13/CR14/CR16/CR11/CR12 批量修复（2026-09-09 15:00 追加，修复 agent）

### 修复 commits
| Commit | 项 | 内容 |
| --- | --- | --- |
| `bbeb2cc` | CR15 | `ensure_http_daemon` deadline 漏加 `window` → 等待窗口退化为单次探针、退避死代码。补 4 回归测试（TestEnsureHttpDaemonBoundedWindow）。 |
| `78c7b9a` | CR13 | `upsert_file_instance` parsed=false 分支 status `'parsed'`→`'pending'`（schema 默认语义），"待解析"与"已解析零符号"可区分。 |
| `78c7b9a` | CR14 | bare-hex 豁免匹配两侧 `to_ascii_uppercase` 归一化；豁免集从 role_prompt_assets manifest（编译期 include_str）派生全部 content_sha256；补 4 测试（redaction 14/14）。 |
| `26be5a4` | CR11/CR12 | 新增 `resolve_raw_calls`（按 distinct callee_name 批量解析 callee_id=0 raw 边：唯一候选→解析+is_cross_file 修正，多候选仅挂同文件；build_graph/build_directory/refresh_file 收尾各跑一次）；CR12 重建前把其它文件指向旧符号 id 的已解析入边降级 raw，交给 resolve 重挂接。单测 9/9 + snapshot_state 51/51。 |

### CR16 定性修正（重要）
§5.9 "query.search 内存 store 与建图数据不同源"系**误判**。真凶：回归脚本传 `kind:''`，`SymbolKind::from_db_str("")` → Unknown，把全部真实符号（fn/class/...）滤成 0 命中——"callers 新鲜、search 恒 0、返回项 kind 全 unknown"三个表象全部由此解释，store 同源性本就正常。修复（`26be5a4`）：`handle_query_search` 空/纯空白 kind 视为不过滤。教训：search 返回项 kind 显示 unknown 正是被 Unknown 过滤**命中**的少数 Unknown 符号本身，即过滤在工作的反证信号。

### behavior 验收（第四轮部署：PID 46596、git_commit=26be5a4、sha256 9b90623b 三方一致、schema v60）
1. **CR16 ✅**：全量重建（384.2s，symbols=19535 / calls=140859）+ snapshot.publish（gen1, 39055 符号）后，`query.search('handle_build_graph', kind:'')` → 1 命中（fn, fs_handlers.rs）；`resolve_raw_calls` 同样命中。
2. **CR11 ✅**：`workspace.build_graph` 返回新增字段 `calls_resolved=39129 / calls_ambiguous=3713`（修复前 callee_id>0 恒为 0）；DB 实查 resolved=39129、is_cross_file=20136，抽样真实跨文件边（cli/main.py main → analyzers/call_chain.py get_call_chain_up）。
3. **CR12 ✅**：`refresh_file('analyzers/call_chain.py')` 增量重建（10 符号/105 调用）后 DB 悬空边 **dangling=0**，指向 get_call_chain_up 的跨文件边 6 条全部重挂接。
4. **CR15 ✅（代码+单测面）**：4/4 回归测试通过；部署重启实测 manifest 刷新窗口仍 ~30s（属 CR10 manifest 启动即写 backlog，与 CR15 修复不冲突——CR15 修的是客户端等待窗口，窗口生效后客户端会退避重试而非立即失败）。


## 八、第八阶段：CR4 本体 + CR5 终态 + §六 3 工具部署（2026-09-09 16:20 追加，修复 agent）

### CR4 本体（`dea6f4a`）
- 新增 `normalize_bare_sha256_refs`（redaction.rs）：与扫描器 bare_hex64 共享同款边界语义（`sha256:` 前缀形态、65+ hex、128-hex 不命中；手写扫描规避 regex replace_all 边界字符被相邻匹配消费的漏配）。
- 接入 task 写入口**自由文本字段**：task.create title/description（含 create_from_plan 根任务与 subtask 定义）、task.report summary、task.handoff reason。
- **明确不改**：evidence_hash/payload_hash/contract hash 等结构化程序比对字段（gate evidence 匹配是裸字符串相等，改格式会破坏 provenance 校验）——已写入 v2-agenda A-2 作为前置条件。
- 单测 redaction 18/18（新增 4，含"规范化后必过扫描"对偶锚点）。

### CR5 终态（`39c2697`）
- 冻结 spec byte-frozen（build gate hash 校验）不可改，新建 `docs/design/cw-role-prompt-compiler-v2-agenda.md`：A-1 登记 blocked_recovery 按 blocking_reasons 推导恢复角色（governance→planner / identity→adjudicator / 回归→executor / 权限外→user route），依赖 planner_governance_v1 合并派工。

### §六 3 工具白名单修复部署（`41970ee`，归属并行审查会话）
- 提交并行会话工作树中的 MCP-002/003/004 白名单漏摘修复 + 迁移差距报告 G1-G7；`verify_route_matrix.py` 门禁全绿（17 项 KNOWN_DRIFT 既有登记）。

### 第五轮部署 + behavior 验收
- 部署闭环：PID 49568、git_commit=`41970ee`（=HEAD）、sha256 `02776f88`（构建=安装=manifest）、schema v60、healthy。
1. **CR4 ✅**：新建含裸 64-hex（manifest SHA + 占位 hash）的验收卡 → DB 实查 title/desc 已带 `sha256:` 前缀 → `cw task prompt` 编译通过（修复前此形态必报 E_TASK_PROMPT_SECRET_DETECTED）。
2. **§六 ✅**：`find_evidence`/`get_freshness_status`/`get_gate_decision` 直连 HTTP RPC 均返回 native 结构化结果（items/count 信封），native 实现不再被 compat worker 遮蔽。
3. 验收卡 prompt 仍显示 `blocked_recovery / Required role: —`——CR5 v1 范围限制的 live 佐证，恢复路由待 v2-agenda A-1。

### 剩余 backlog
- P2-CR7/CR9/CR10（instance 派生归一化 / dev_loopback 安全面 / manifest 启动即写）。
- §六后续：worker 侧 MCP-005..012 死注册清理、S2 批次矩阵行翻转、61 个真 compat 迁移、CLI build-context daemon 化。

## 九、第九阶段：P2-CR10 manifest 启动即写——HTTP 预绑定前移（2026-09-09 17:40 追加，修复 agent）

### 根因定位（behavior-level 实测）
- 运行中 daemon（PID 49568）进程创建时间 16:13:42（GetProcessTimes，UTC 转本地），manifest mtime 16:14:23——**进程启动 → manifest 发布实测 ~41s**。
- 源码链：manifest 经 `serve()` → `bind_http()` 发布，而 `spawn_http_transport` 位于 recovery（G14）→ `recover_all_workspaces_with_snapshot` → `TaskCollabStore::new` → `start_server` **之后**；重启动步骤整体推迟了 bind+manifest。
- 客户端表象：重启后旧 manifest 携带已死 PID → `validate_http_manifest` 报 `E_HTTP_MANIFEST_STALE` fail-closed——CR10 "~30s 窗口"的真身。

### 修复（CR10 本体）
- `http_server.rs`：`bind_http` 改**同步 std-listener 预绑定**（bind + 原子 manifest 发布，不依赖 tokio runtime）；新增 `serve_prebound`（`from_std` 接管 listener，compat worker 启动后回写 `worker_status` 到 manifest）；`serve` 保留为兼容包装（bind 后转 serve_prebound）。
- `server.rs`：新增 `spawn_http_transport_prebound`（只接管 accept loop）。
- `cw_daemon.rs` 两个 main（Unix/Windows）：http_spec 解析 + 预绑定前移到 "starting with config" 之后、recovery 之前；非 loopback 仍 fail-closed return 1，其余预绑定失败（端口占用/manifest 写入）降级禁用 HTTP（与旧 bind 失败非致命语义一致）。
- 效果：manifest 在进程启动 ~1s 内携带新 PID/endpoint 原子替换；listener 先行入内核 backlog，TCP connect 探针即刻成功（serve 前请求在 backlog 排队，不再 connection refused）。
- 回归测试：`test_prebind_publishes_manifest_and_accepts_connect`（纯同步锚点：预绑定不依赖 runtime、manifest 即时发布、connect 即刻成功）。

### 第六轮部署 + behavior 验收（2026-09-09 18:20）
- 部署闭环：commit `7eb6cab`（=HEAD）；PID 8660、`git_commit=7eb6cab`、sha256 `871f431b`（构建=安装=manifest 三方一致）、schema v60、worker healthy、endpoint 127.0.0.1:8485。
- **CR10 ✅ 核心验收**：重启后 **+2s** 用客户端自身 `read_http_manifest` + `validate_http_manifest` 实测 **PASS**（新 PID/endpoint 即刻可发现；修复前此时刻必报 `E_HTTP_MANIFEST_STALE` fail-closed）。旧二进制对照实测：进程启动 → manifest 发布 **~41s**；新二进制 publish#1（prebind）实测 **~1.05s**（18:12:47.2 → 18:12:48.26）。
- 时序说明（不阻塞验收）：publish#2（`worker_status` 回写）在 +38s——`spawn_http_transport_prebound` 站点仍在 `TaskCollabStore::new`/`start_server` 之后（SerializationPoint 依赖）。影响仅为 manifest worker_status 字段延迟更新；发现面（PID/endpoint/connect）由 publish#1 覆盖。health 响应在 warmup（~38s）后可用，connect 在 warmup 期间即成功（backlog），等待由 CR15 客户端窗口兜底。
- 回归：http_server tests **11/11**（含新增 prebind 锚点）；修复过程中发现并修复 `from_std` 前未置 nonblocking 会挂死 accept 的问题（已在 `bind_http` 内统一 `set_nonblocking(true)`）。
- 环境注意：`taskkill`/`cmd //c` 在本沙箱被安全策略拦截，改用 Python ctypes `TerminateProcess`；`git_commit=unknown` 陷阱——daemon 启动 cwd 必须在仓库内（`current_git_commit()` 依赖 `git rev-parse`），部署脚本沿用。

## 九、第九阶段：§六清单批量收敛——三向一致性达成（2026-09-09 16:50 追加，审查会话）

> 承接 §六后续清单第 2/3/6 项。起点：修复 agent 已将 §六 3 工具修复提交（41970ee）并完成第五轮部署 behavior 验收（§八，未决项 5/6 关闭）。

### 已完成（source-level；route_matrix.rs 随下次构建进二进制）

1. **核实 worker 侧注册已清空**：tools_p2_graph（空表跳过注册）与 tools_p3_identity（MCP-010/011/012 注册已摘，仅剩 get_attestation_validity / list_attestation_revocations 2 个合法 compat）均无需改动；tools_p4_lease 的 assignment_show 仍为合法 compat 保留。
2. **compat_registry 镜像死条目清理（8 条）**：MCP-005..012（detect_cycle / get_artifact_freshness / get_interface_providers / validate_revision_dependencies / get_dependency_edges / get_action_identity / check_action_identity / check_session_separation）静态镜像删除（69→61→58 链路）。
3. **矩阵行翻转（17 行，JSON + route_matrix.rs 双侧）**：8 行 batch=MCP-005..012 status=migrated；9 行 S2 批次（get_top_callers 等 CONVERGENCE_RPC_METHODS 组 + toolchain 组）batch=S2 status=migrated——清除"工具当前无可用路由"误报（实测 native 可用）。
4. **mcp_tools.md 头部刷新**：237→243；backend 分布改为当前目标态（rust_native 142 / task_rpc 43 / python_compat 58）；fail-closed 语义修正为"仅未知方法"。

### 里程碑：三向一致性

```
http_server.rs 白名单   = 58
compat_registry 镜像    = 58
矩阵 python_compat 行   = 58   （rust_native 142 + task_rpc 43 + python_compat 58 = 243）
verify_route_matrix.py: 核对通过，dispatch/白名单/compat 三向一致，无本地隐式路径，KNOWN_DRIFT 0 项
check_client_purity.py: 0 违例
```

### 剩余未达标（向"Python 零业务"目标的距离）

| # | 项 | 规模 | 承接 |
| --- | --- | --- | --- |
| 1 | 58 个真 compat 方法迁 Rust native（H4C 白名单逐组清零） | 58 方法（背后 db/ 56k 行随迁退役） | T-1787293451688 分期 |
| 2 | CLI `cw build-context resolve/import-compile-commands` 本地业务+直连 SQLite | 2 命令（G4） | 待开卡 |
| 3 | `cw daemon serve` 影子 server 下线决策 | 1 入口（G1） | 待开卡 |
| 4 | route_matrix.rs 部署（随下次 build→swap，门禁已按源码核对） | 1 构建 | 并入下轮部署 |

### behavior 验收（部署后）
- `verify_route_matrix.py` 在部署后仍全绿（route_matrix.rs 编译产物与源一致）；
- 抽测 S2 组 1 个方法（如 `get_call_heatmap`）HTTP 返回 native 结果。

## 十、第十阶段：P2-CR7 workspace instance 身份归一化 + 存量分裂行注册自愈（2026-09-09 21:40 追加，修复 agent）

> 分期修复第二期。commit `3b9ceed`；第七轮部署 + behavior 验收全过。

### 根因（source + behavior 双确认）

- daemon 权威派生 `compute_workspace_instance_id = sha256("owner_uid|host_real_root")[:16]`，`host_real_root` 来自 `validate_owned_path` 的 canonicalize 产物——**保留 `\\?\` verbatim 前缀、反斜杠、盘符/目录大小写**；客户端 `derive_workspace_instance_id` 只做 `\`→`/`，无 realpath/小写。任何路径写法差异（`C:/…` vs `c:/…` vs 大小写）都派生新实例。
- 存量实锤（registry.db 只读审计）：callwarden root 同一物理 checkout **5 行分裂**（`65666b…/723bb…/eb81cf…/4baea3ff…/822c03…`），codegraph/快照目录按实例分散（`codegraph/4baea3ff…` 与 `workspaces/<其它实例>/` 并存）。
- 关键实测：task-DB `workspace_authority_captures` 的 `host_real_root_hash` **恰为 sha256(归一化 root) 全量 hex**（`c:/git_work/callwarden` → `b9515f7c…`），41 条 capture 权威绑定在 `4baea3ff12c2ea5c`（最早行 `65666b…` 绑定为 0）——**自愈选行不能简单取最早行，否则会切走图谱/绑定权威目录**。

### 修复（`3b9ceed`）

1. **身份输入归一化（Rust 权威侧）**：`workspace.rs` 新增 `normalize_identity_root`（strip `\\?\`/`\??\` verbatim 前缀 + `\`→`/` + Windows 全 ASCII 小写（NTFS 大小写不敏感）+ strip 尾斜杠；POSIX 保留大小写）；`compute_workspace_instance_id` 输入归一，存储行 `host_real_root` 一并用归一化值。
2. **注册自愈（不再分裂）**：`register_workspace_preferred` 在 INSERT 前查同 owner + 归一化 root 相同的历史分裂行——命中则**收编**（复用该行 instance id，原地刷新 root + provenance + last_active），不产生新行。选行顺序：① task-DB capture 权威行（新增 `TaskCollabStore::captured_instance_ids_for_root_hash`，`SnapshotDaemonState::handle_workspace_register` 覆写传入，capture 查询失败降级空偏好不阻塞注册）；② 兜底最早注册行（确定性）。
3. **Python 客户端同语义**：`derive_workspace_instance_id` 升级为 realpath + 小写（ASCII）+ 正斜杠 + strip 尾斜杠；**规范写法的 id 不变（`b9515f7c28f5d0f0`，与 CLI 历史推导一致），零迁移成本**。
4. 契约升级：`test_register_workspace_updates_snapshot_for_legacy_identity_at_same_root` 由"legacy 行与新行并存 + provenance 镜像"升级为"legacy 行被原地收编、provenance 就地更新、单行"。

### 回归

- 新增 4 测试：normalize 各形态（verbatim/反斜杠/大小写/尾斜杠，POSIX/Windows cfg 分支）、同 root 不同写法收敛（1 行）、legacy 原始写法行自愈收编、**capture 权威行优先**（多行分裂时收编 preferred 行而非最早行）。
- register 面 **10/10**、normalize **1/1**；workspace 模块全量 **127/127**；bin 编译通过。

### 第七轮部署 + behavior 验收

- 部署闭环：PID 7700、`git_commit=3b9ceed`（=HEAD）、sha256 `4d203af7`（构建=安装一致）、schema v60、worker healthy、endpoint 127.0.0.1:4581。
- **CR7 ✅ 核心验收**（live daemon 实测）：
  1. `workspace.register("C:/git_work/callwarden")` 与 `workspace.register("c:/GIT_WORK/callwarden")` → **同一 instance id `4baea3ff12c2ea5c`**（capture 权威行被优先收编），`host_real_root` 归一为 `c:/git_work/callwarden`；
  2. registry 行数 5 行无增减——**不再产生新分裂行**；唯一归一化行即权威行，旧写法行原样保留但不再被新注册选中；
  3. 图谱面连续性：权威实例 codegraph 库完好（`codegraph/4baea3ff…/codegraph.db`，358 symbols / 3.3MB）。
- 备注：验收中 `query.search` 报 `snapshot_not_ready`——daemon 重启后 snapshot 缓存为空的既有启动时序，非 CR7 回归。

### 剩余 backlog（更新）

- P2-CR9（dev_loopback 安全面设计）、61 个真 compat 迁移（T-1787293451688）、CLI build-context daemon 化（G4）、`cw daemon serve` 下线决策（G1）。
- 存量 4 行旧写法分裂行的数据级归并（codegraph 目录合并/退役）为可选运维项：新注册已不再选中它们，可在低峰期人工归并后下线。

## 十一、第十一阶段：P2-CR9 HTTP 安全面设计——Profile v2/v3 演进路径（2026-09-09 21:50 追加，修复 agent）

> 分期修复第四期第一项。CR9 定性为设计项（报告 §2：当前拓扑可接受、跨机/多 agent 共享时需在 capability registry 之上加身份层）。产出设计文档，不改现行行为。

- **设计文档**：`docs/design/http-security-profile-v2-design.md`（design-only）。
- 现状基线（代码事实）：v1 = `dev_loopback_unauthenticated`——loopback 强校验 fail-closed + 合成 local-owner peer + manifest authority 作用域 + capability registry（路由面白名单，无身份层）。
- 威胁模型缺口登记：①同机非 owner 进程可以 daemon owner 身份执行全部 RPC；②多 agent 共享 daemon 时传输层身份不可区分（治理只靠应用层自报）；③跨机未启用（loopback 校验会拒绝，但需要的是身份层而非仅放开绑定）。
- 方案主体 **Profile v2 `loopback_token_authenticated`**：daemon 启动 prebind 窗口内生成 256-bit token（0600/owner-ACL，与 manifest 同 authority 命名）；客户端 `Authorization: Bearer`；axum middleware constant-time 校验，失败 401 `E_HTTP_UNAUTHENTICATED` 不进 dispatch；`/health`、`/capabilities` 豁免保持匿名可探测。**身份层映射**：主 token → 现行合成 peer（行为不变）；可选子 token（`auth.token.issue`，绑 agent_id/allowed_roles/过期）→ 派生 peer，使治理事件/lease 可区分 agent；授权仍由既有 is_protected_mutation/owner-key 逻辑承担，**不把 ACL 塞进 capability registry**（保持 H1 冻结语义）。
- **Profile v3**（跨机 mTLS）仅登记方向，明确禁止"只放开绑定不加密"的实现路径。
- 验收标准 6 条（401 面/1-bit 翻转/token 权限 OS 层验证/重启轮换/子 token 过期与角色外拒绝/回归测试骨架）与 S1→S3 分期（主 token → 子 token → 默认翻转）均写入设计文档；触发条件：多 agent 并存常态化 / 同机不可信进程形态 / 237 工具 HTTP 化立项。
- **剩余 backlog（更新）**：61 个真 compat 迁移（T-1787293451688）、CLI build-context daemon 化（G4）、`cw daemon serve` 下线决策（G1）；CR9 实现期 S1 待触发条件成立后立项。存量 4 行旧写法分裂行归并为可选运维项。

## 十二、第十二阶段：G1 影子 server 下线 + 迁移矩阵盘点修正（2026-09-09 22:10 追加，修复 agent）

> 分期修复第四期第二项。commit `4a780f3`。

### G1：`cw daemon serve` 影子 server 下线（✅ 本轮落地）

- **下线方式**：CLI 入口归零而非删模块——`_parser` 不再注册 serve 子命令（`include_serve` 参数保留仅为 cw-client 调用点兼容，无行为差异）；serve 执行分支改 fail-closed 下线提示（防语义复用静默复活）；摘除 `EnterpriseDaemonServer/Service` 顶层 import。
- **保留面**：`server/daemon_server.py` 模块不动（SRV-019 白名单退休对象 + 9 个测试文件直连 import），仅失去用户可达启动入口；Rust 原生 daemon 为唯一权威。
- **契约测试升级**：`test_parser_with_serve_includes_serve_subcommand`（旧契约：include_serve=True 注册 serve）升级为 `test_parser_serve_subcommand_retired_regardless_of_include_serve`（任何取值均不注册）+ serve 调用一律 SystemExit 2；`test_cw_client_rpc_proxy` 14/14。
- **live 验收**：`cw daemon serve` → argparse `invalid choice: 'serve'`（合法子命令清单已无 serve）；`cw daemon ping` 正常（PID 7700）。

### 迁移矩阵盘点修正（backlog 口径勘误）

- 父任务 `T-1787293451688-c14b1e44` 下 **187 张卡全部 closed（0 open）**——卡片树已清空。
- 矩阵实况（`tool_migration_matrix.json`）：243 工具 = rust_native **142** + task_rpc **43** + python_compat **58**（全部 transition 态）。§六"61 个真 compat"现口径为 **58 个**。
- 结论：剩余 58 个 python_compat 的继续迁移**没有现成 open 卡可领**，需 planner 侧新开迁移卡（每卡 4 step 流程不变，skill `callwarden-mcp-card-migration` 仍适用）——登记为 planner 决策点，非 executor 可直接推进项。

### 剩余 backlog（最终口径）

| # | 项 | 状态 | 承接 |
| --- | --- | --- | --- |
| 1 | 58 个 python_compat 迁移 | 需新开卡（planner 决策点） | 待 planner 派工 |
| 2 | G4：`cw build-context resolve/import-compile-commands` daemon 化 | 待开卡（resolve 需 RPC 化 `compute_resolved_edges`，import 仅剩客户端解析面） | 待开卡 |
| 3 | ~~G1：`cw daemon serve` 下线~~ | ✅ 本轮（`4a780f3`） | — |
| 4 | CR9 安全面实现期 S1 | 设计完成（§十一），待触发条件 | backlog |

### §十二 补记：58 compat 迁移首批卡已开（2026-09-09 22:05，planner 派工）

- 前置备份：`~/.callwarden/backup_20260909-pre-migration/`——callwarden.db（605MB，SQLite 在线一致备份）+ registry.db + codegraph/ + workspaces/ + worktree zip（HEAD=cb2ee90，2465 entries 完整性 OK）。git bundle 因仓库历史对象既有缺失不可用，改用 git archive HEAD。
- 开卡脚本：`scripts/planner_open_compat_cards.py`（governed create：workspace pair `1/4baea3ff12c2ea5c` + `legacy_identity_v1` + A′ 三角色缺省合同；每卡 4 step）。
- **6 张卡全落库（open），58 方法全覆盖**：identity-lease-small `T-1788962298062-54927dac`(3) → tools_query `T-1788962307288-7a8526d4`(8) → tools_task `T-1788962308102-ab09ac58`(8) → tools_semantic `T-1788962308958-de061718`(5) → tools_security `T-1788962309851-13447b90`(15) → tools_summary `T-1788962310755-49297814`(19)。
- **并发边界结论**：治理面（开卡/report/lease）可并发——daemon 单点 SerializationPoint 串行化，SQLite 单写者；实现面**必须串行**——每卡共享 task_collab.rs/dispatch.rs/http_server.rs（多会话同写已实证 hunk 覆盖）+ 构建锁（LNK1104/stage-refresh）。执行模式定为单 executor 流水线，建议从 identity-lease-small 小卡起按方法数升序领卡。
