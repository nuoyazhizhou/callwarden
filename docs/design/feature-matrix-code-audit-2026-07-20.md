# Feature Matrix A1-N8 代码审计报告

> 日期：2026-07-20  
> 审计对象：`_feature_matrix.md` 中 A1-N8 所有标记为“已实现”“已实施”“已修复”“已完成”的条目  
> 证据范围：生产代码、实际 CLI/MCP/RPC 入口、构建脚本和当前产物。`tests/`、`test/`、`testcode/` 仅视为线索，不作为实现完成的证据。

## 结论

`_feature_matrix.md` 不能作为当前实现状态的权威真相源。它同时混合了四种不同状态：

1. 已进入生产路径的完整功能。
2. 只有类/函数/PyO3 导出，但没有生产调用方的组件。
3. 只有测试框架、语法检查或未运行 CI 的验收承诺。
4. 已被后续代码改变、内部自相矛盾或明确错误的旧结论。

本轮共核验 **200 个唯一 ID**（原矩阵另有 2 行重复的 `D3` 路线图引用）：128 项确认完成，40 项仅部分完成，25 项完成声明不成立，5 项只是测试或设计记录，2 项原矩阵本就未声称完成。换句话说，矩阵中的"完成"类记录只有 **64.6%**（128/198）能按生产代码和公开路径直接确认。

**复审回退（2026-07-21）**：根据 `docs/design/feature-matrix-code-reaudit-2026-07-21.md`，本轮审计中 20 个 ✅ 状态被复审否决，回退为 🟡/❌：

- ✅→🟡（13 项）：A14、A19、A21、D7、G1、G3、G4、G8、G9、G10、G13、G14、G15、G29、I4、M4-M6（其中 M4-M6 已为 🟡 文本更新）
- ✅→❌（7 项）：G11、I1、I3、I9、I11、I12、I13、I15、I16、I17、J8、K4、N3、N7、N8
- 🟡→❌（3 项）：H14、N7、N8（部分原 🟡 升级被否决）
- 🟡→✅（1 项）：M8（复审确认真实修复）

回退后真实计数：约 110 项确认完成、50 项部分完成、35 项不成立。完整状态回退表见复审报告 §5。下一轮通过门槛见复审报告 §8。

状态含义：

| 状态 | 含义 |
|---|---|
| ✅ 确认 | 生产实现存在，且公开路径可达 |
| 🟡 部分 | 组件存在，但语义、接线、安全性或外部验收未闭合 |
| ❌ 不成立 | 关键实现不存在、实际路径必然失败，或声称与代码相反 |
| 📄 非产品完成项 | 只是测试、设计方向或文档记录 |
| ⚪ 未声称完成 | 原矩阵已标记未实施，仅为编号连续性保留 |

## 最高优先级问题

### P0 运维 RPC 缺少管理员授权

Rust daemon 的 `backup`、`restore`、`mount.*`、`toolchain.*`、`build_context.*` 等处理器接收 `SO_PEERCRED` 后却忽略 `_peer`。只要能连接 `callwarden-clients` socket 的普通用户，就可以覆盖 registry DB、改写全局 mount/toolchain 配置。这与 G3/G4/G16 的企业隔离承诺冲突。

证据：`rust_ext/src/daemon/workspace.rs:1167`、`:1205`、`:1316`，`rust_ext/src/daemon/snapshot_state.rs:799`、`:875`、`:932`。

### P0 Watcher 到可查询 Snapshot 的纵向链路未闭合（❌ 复审回退 2026-07-21）

批次3/7/11 仅修复了 hex/b64 字段双标准、memfd 协议（Python 路径）、Replicator 空 db_path 报错、顶层 admin ACL 四项局部组件，但**核心 save-to-query 链路仍完全断裂**：

1. **hex/b64 字段双标准**（✅ 批次3 修复，可保留）：`server/agent_protocol.py:278` 默认 `canonical_bytes_hex`；`server/daemon_server.py:783-883` 同时支持 hex + b64；`rust_ext/src/daemon/workspace.rs:1032-1058` Rust 端同步。
2. **memfd 协议**（🟡 仅 Python 闭合）：Python 路径有 `create_sealed_memfd` + `validate_memfd_fd` 四重校验；**Rust `memfd.rs` 没有 `F_GET_SEALS` 也没有 owner UID 校验**（G10 回退）。
3. **Replicator 空 db_path**（❌ 复审回退）：`codegraph_db_path_template` 默认空字符串，release/systemd/deployment 未设 `CW_DAEMON_CODEGRAPH_DB_TEMPLATE`；即使手工设置，replicator 只 reload 外部 DB 不先 delta apply；`daemon_handle_refresh` 只更新 CAS/generation，**不写 `workspace_manifests`，不对 symbols/calls 执行 delta apply**（G8/G11 回退）。
4. **admin ACL**（🟡 仅顶层闭合）：顶层 `ADMIN_ONLY_METHODS` + `is_admin` fail-closed 已闭合；**workspace 级 ACL 仍可绕过**——`snapshot.list_workspaces` 忽略 `_peer`，`mount.list` 暴露全局 path，`toolchain.resolve`/`build_context.list`/`resolved_edges.*` 使用调用方传入的 `workspace_id` 不校验 owner（G3/G4 回退）。

**save-to-query 真实状态**：watcher refresh 写入 CAS/generation 后，没有把 delta 应用到 workspace manifest、查询 SQLite 或当前 GraphSnapshot。M4/M5/M6 虽已进入 refresh handler，但 `store=None` 退化模式，结果只写 staging JSON。G11 = ❌；G8/G9/M4-M6 = 🟡；J8/K4 = ❌。

证据：`server/agent_protocol.py:214-280`，`server/daemon_server.py:495-558, 776-883`，`rust_ext/src/daemon/workspace.rs:931-1110, 1290-1370`，`rust_ext/src/daemon/dispatch.rs:545-610`，`rust_ext/src/daemon/memfd.rs`，`rust_ext/src/daemon/replicator.rs:698-910`。复审报告：`docs/design/feature-matrix-code-reaudit-2026-07-21.md` §3 P0-1。

### P0 跨平台发布会产出空壳或在 CI 早期失败（❌ 复审回退 2026-07-21）

批次5/14 修复仅覆盖 entry_points 连字符统一和 Linux fail-fast 错误处理，**真实 wheel 构建和平台包生成链不可执行**：

1. **wheel 空壳**（❌ 复审回退）：真实运行 `python release/build.py --wheel` 失败（`--config-setting --build-option=--plat-name=win_amd64` 参数错误）；当前唯一 wheel 是 `release/dist/callwarden-0.3.0-py3-none-any.whl` 不含 `callwarden_core`。
2. **Linux 脚本空壳包**（❌ 复审回退）：批次14 fail-fast 修复只是把 `|| true` 改成 `exit 1`。**Linux 包脚本依赖 `cw-daemon` 二进制，Cargo 只声明 `cw_daemon`（下划线），systemd unit 又执行 `cw_daemon`，三处命名不一致**。RPM 章节从 "TODO" 改为 "deb-only" 可保留。
3. **入口名不一致**（✅ 批次14 修复，可保留）：`pyproject.toml` + `release/version.toml` 的 `[project.scripts]` 改为连字符 `cw-client/cw-agent/cw-daemon`，与 systemd unit / 打包脚本 / 文档 / `cli/*.py` docstring 一致。
4. **Release CI version key/parser 调用**（❌ 复审回退）：version key 修正可保留，但 workflow 未实际运行过。WiX 引用 `cw.exe`/`cw-client.exe`/`runtime/python.exe` 但 workflow 不生成这些输入；macOS 脚本 `APPLE_*` 与 `CW_APPLE_*` 环境变量不一致，签名/公证被跳过；Linux offline bundle 调用参数顺序错误且带 `|| true`；workflow 上传的 `manifest.json` 路径与脚本产出不一致。
5. **CLI `--version`**（❌ 复审新发现）：主 CLI 不支持 `cw --version`，Gate 3 的首个黑盒命令必失败（exit 2）。

N3/N7/N8 回退至 ❌；H14 回退至 ❌。证据：`release/build.py:39-75`，`release/linux/build_packages.sh:65-122, 216-225`，`pyproject.toml:54-58`，`.github/workflows/enterprise-release.yml`。复审报告：`docs/design/feature-matrix-code-reaudit-2026-07-21.md` §3 P0-3。

### P1 PR 检查 fail-open（🟡 复审回退 2026-07-21）

二轮评审修复只完成一半：`PRChecker` 改用 `check_before_edit` + 异常上浮，但 `passed` 仍未纳入 `run_errors`/`scan_complete`，`_query_open_findings` 只查 `guardrail_findings` 未合并 `semgrep_findings`，GitHub Action 只读取 `passed` 仍 exit 0。fail-open 未真正闭合。A19/A21 = 🟡。

证据：`cicd/pr_check.py:66-91`、`:142-189`，`cicd/github_action.py`，`db/db_guardrail.py:221`。复审报告：`docs/design/feature-matrix-code-reaudit-2026-07-21.md` §4 P1-1。

### P1 文档包含危险且过时的运维建议（✅ 已闭合，批次12 修复）

原 `callwarden_USER_GUIDE.md` 建议删除不可恢复的用户级 DB，`docs/deployment.md` 建议直接 `rm` WAL/SHM，与 `AGENTS.md` 禁止规则相反。

修复（批次12）：`docs/deployment.md` `rm -wal/-shm` → `PRAGMA wal_checkpoint(PASSIVE)`；USER_GUIDE 危险 DB 删除建议已清理；与 AGENTS.md 规则 2 禁止删除 DB 文件一致。

## A-E 审计

| ID | 结论 | 代码核验 |
|---|---|---|
| A1 | ✅ | 16 种语言配置、parser factory 和对应 parser 存在。 |
| A2 | ✅ | `db_build.py::_build_call_graph_multi_lang` 实现精确/import/简名唯一/同文件四级策略。 |
| A3 | ✅ | `analyzers/call_chain.py` 实现上行/下行 BFS。 |
| A4 | ✅ | 拓扑排序和循环检测均有 DB/内存图路径。 |
| A5 | ✅ | calls 生成和 schema 含 `is_cross_file`。 |
| A6 | ✅ | `file_versions` + content hash 去重已接入 build/refresh。 |
| A7 | ✅ | `file_symbol_versions` 和删除差异标记已接入。 |
| A8 | ✅ | 单符号/批量注释恢复及 preview 入口存在。 |
| A9 | ✅ | 圈复杂度、耦合、扇入扇出、健康分已实现。 |
| A10 | ✅ | `check_file_health` 存在并公开。 |
| A11 | ✅ | Git history 和符号级变更入库已接线。 |
| A12 | ✅ | Semgrep CLI 执行、JSON 解析、入库和 CLI/MCP 入口存在。 |
| A13 | ✅ | findings 有唯一约束和 `INSERT OR IGNORE`。 |
| A14 | 🟡 | 批次12 修复（2026-07-20 二轮评审补全）：新增 `scan_semgrep_incremental()` 方法（`analyzers/issues.py`）：通过 `git diff --name-only` 取变更文件 → 调用 `run_semgrep` 扫描 → `save_semgrep_findings(scan_type='incremental', stale_file_ids=...)` 清理旧 findings + 关联 scan_id。schema v40 新增 `semgrep_findings.scan_id` 字段 + `idx_semgrep_scan_id` 索引。CLI 新增 `cw semgrep scan --incremental [--base main] [--head HEAD]`，MCP 新增 `scan_semgrep_incremental` 工具。**复审回退（2026-07-21）**：增量扫描入口存在，但 git diff/删除及失败边界未闭合——`stale_file_ids` 清理只覆盖已知文件，删除的文件不会触发 finding 清理；scan 失败时 findings 已写入但 scan_id 无对应记录，留下孤儿 finding。 |
| A15 | ✅ | 批次12 修复（2026-07-20 二轮评审补全）：接入 `pathspec` 库作为主路径，获得完整 gitignore 语义：字符类 `[abc]`/`[a-z]`/`[!abc]`、尾随空格保留（除非行末 `\` 转义）、目录剪枝后 negation 恢复（pathspec 内部 last-match-wins）、复杂 `**` 与 `/` 组合。pathspec 不可用时降级到自研实现（保留向后兼容，自研不支持字符类）。`pyproject.toml`/`requirements.txt`/`install.py` 均已加入 pathspec 核心依赖。 |
| A16 | ✅ | `.callwardenignore` 加载、生成和 GC 共享 matcher 已接入。 |
| A17 | ✅ | archive/restore/status/purge/retention 均有生产入口。 |
| A18 | ✅ | full build 末尾调用 Young GC。 |
| A19 | 🟡 | 二轮评审修复（2026-07-20 P1）：SARIF exporter + GitHub Action 入口存在，PRChecker 改用 `check_before_edit` + `run_errors` 收集 + SARIF `executionNotifications` 让 fail-visible。**复审回退（2026-07-21）**：`cicd/pr_check.py:142` 的 `passed = errors == 0` 未纳入 `run_errors`/`scan_complete`，`_query_open_findings` 只查 `guardrail_findings` 未合并 `semgrep_findings`，GitHub Action 只读取 `passed` 仍 exit 0。fail-open 未真正闭合。 |
| A20 | ✅ | changed-file 分析和按文件 refresh 已实现。 |
| A21 | 🟡 | 二轮评审修复（2026-07-20 P1）：`pr_check.py` 改用 `check_before_edit` + 异常上浮。**复审回退（2026-07-21）**：`passed` 仍为 `errors == 0`，未纳入 `run_errors`/`scan_complete`，Semgrep findings 未合并为阻断结果，`cicd/github_action.py` 仍 exit 0。fail-open 未真正闭合。 |
| A22 | ✅ | 原子写、LSP 路径/子进程限制、错误脱敏等生产代码存在。 |
| A23 | 🟡 | 文件级并行存在（ThreadPoolExecutor），但主路径现为 Rust rayon pool + Python ProcessPoolExecutor，ThreadPool 主要是降级路径。矩阵描述已更新为 "Rust pool + ProcessPool + ThreadPool 降级"。状态保持 🟡 是因为矩阵描述本身仍需对齐（不是代码 bug，是文档描述过时）。 |
| A24 | ✅ | WAL/cache/mmap PRAGMA 存在。 |
| A25 | ✅ | symbol/call/depth 等批量写使用 `executemany`。 |
| B1 | ✅ | Guardrail Mixin 及 DB/API/Incident 规则存在。 |
| B2 | ✅ | blast radius 和 cross-layer impact 已公开。 |
| B3 | ✅ | 演化、缺陷关联和热点路径存在。 |
| B4 | ✅ | 缺陷知识库、学习和修复建议存在。 |
| B5 | ✅ | `task_next_step` 对编辑步骤调用 `check_before_edit`。 |
| B6 | ✅ | 漏洞爆炸半径 DB 方法和 MCP 入口存在。 |
| C1 | ✅ | task/step/audit 状态机存在。 |
| C2 | ✅ | parent_id 任务树和深度优先领取存在。 |
| C3 | ✅ | 子任务完成后父链级联推进存在。 |
| C4 | ✅ | blocked finding 解决步骤插入存在。 |
| C5 | ✅ | SHA-256 预条件、原子写和审计存在。 |
| C6 | ✅ | CheckGate Mixin 及 CLI/MCP 路径存在。 |
| C7 | ✅ | `work_next_job` 返回结构化任务/步骤/约束上下文。 |
| C8 | ✅ | symbol/range patch 生产入口存在。 |
| C9 | ✅ | `install-agent` 生成 Codex/Claude/Cursor 集成文件。 |
| C10 | 🟡 | post-commit capture 可建立 task ↔ commit ↔ symbol 三角关联。状态保持 🟡 是因为 hook 是 best-effort 设计（可被 `--no-verify` 或外部编辑绕过），这是**设计决策**（Git hook 本质是 advisory，非强制；Call Warden 不强制接管用户的 git workflow），非代码 bug。替代路径：CI/CD PR check + 手动 `cw task report` 可补全关联。 |
| C11 | ✅ | 手动/自动 reopen 和祖先链回退已实现。 |
| D1 | 🟡 | 当前实现是 BLOB 存储 + `callwarden_core.batch_cosine_similarity` Rust 全量扫描 + numpy 矩阵运算降级，适合 < 100k 符号；sqlite-vec vec0 虚拟表待落地是**设计决策**（KNN 路径未来优化），非 bug。pyproject.toml 仍声明 sqlite-vec>=0.1 依赖但生产代码未加载（避免误用）。AGENTS.md 技术栈已标注 "sqlite-vec vec0 虚拟表待落地"。 |
| D2 | ✅ | sentence-transformers 生成 embedding 路径存在，为可选依赖。 |
| D3 | ✅ | semantic search 及 keyword fallback 存在。 |
| D4 | ✅ | 相似函数查找已公开。 |
| D5 | 🟡 | `ask_codebase` 是检索+调用上下文组装器，返回 `rag_context`，**不生成最终问答是设计决策**（Call Warden 是知识图谱工具，最终问答由外部 LLM/Agent 完成，避免重复实现 LLM 调用），非 bug。 |
| D6 | ✅ | hover/definition/references/diagnostics/completion 和 MCP 转发存在。 |
| D7 | 🟡 | **复审回退（2026-07-21）**：批次5 仅修复 `target_symbol_hash` 空字符串问题。但跨仓库检测仍按 import 路径最后一段匹配任意同名符号，`Dict[name]` 会覆盖重名符号；`cross_repo_deps` 没有唯一约束，重复扫描持续追加记录。该算法只能算启发式候选，不能宣称影响传播已完整修复。 |
| D8 | ✅ | 分支注册/切换/差异/合并 preview 入口存在。 |
| E1 | ✅ | CODEOWNERS + blame 所有权路径存在。 |
| E2 | ✅ | `who_to_ask` 已公开。 |
| E3 | ✅ | LCOV/Cobertura 导入路径存在。 |
| E4 | ✅ | test impact selection 生产入口存在。 |
| E5 | ✅ | token ledger 存在；token 数为估算值，不是 tokenizer 精确计费。 |
| E6 | ✅ | AI summary 和 repo map 生产路径存在。 |
| E7 | ✅ | file_read/grep/list/symbol_content MCP 入口存在。 |
| E8 | ✅ | zh_CN/en_US 资源和切换路径存在。 |

## F-H 审计

| ID | 结论 | 代码核验 |
|---|---|---|
| F1 | ✅ | suffix index 已进入 call resolve 主路径。 |
| F2 | ✅ | external symbols 一次批量加载。 |
| F3 | ✅ | depth 批量 `executemany`。 |
| F4 | ✅ | calls/call_versions 内存聚合后批量写入。 |
| F5 | ✅ | Python fallback 主路径使用 ProcessPoolExecutor。 |
| F6 | ✅ | file-local qname map 已用于 resolve。 |
| F7 | ✅ | 128-perm MinHash + 8x16 LSH + bucket cap 存在。 |
| F8 | ✅ | FTS5 表、触发器和迁移存在。 |
| F9 | ✅ | qualified_name caller 路径存在。 |
| F10 | ✅ | `cw fts rebuild/status` 及 DB 公开方法存在。 |
| F11 | ✅ | 批次6 修复（2026-07-20 接入 CLI）：`build_graph_from_c_files` PyO3 函数原仅测试调用，现已接入 CLI `cw graph build-from-c <dir>` 子命令（`cli/main.py:_handle_graph`）：递归扫描 `.c` 文件 → rayon 并行 parse + 内存构 CSR → 报告符号/边数 → 可选 `--dump` 输出 .cwsnap → 可选 `--query` 自检查询。定位为"可选加速路径"，不替代 `db_build.py` 的标准 `build_full_graph`（持久化路径），适用于 C 重型代码库（如固件）的快速符号图谱构建。性能数据 13.43x 仍仅来自基准测试 `tests/test_f11_rust_build_graph.py`。 |
| F12 | ✅ | `.cwsnap` mtime 校验、mmap load 和后台 dump 已接入 `_get_graph_store`。 |
| F13 | ✅ | calls 索引迁移与 GraphStore 降级路径一致。 |
| F14 | 🟡 | 延迟建索引/分段 commit/WAL truncate 代码已实施。10M/8.1x 是基准承诺（来自 `tests/_bench_e2e_report.md`），本次不以测试报告认定。状态保持 🟡 是因为基准数值未在当前环境复验，非代码 bug。 |
| F15 | 🟡 | cache_size=256MB + page_size=8KB 配置已实施。17.8% 数值来自 `tests/_bench_matrix_report.md` 基准测试，未作当前环境复验。状态保持 🟡 是因为基准数值未复验，非代码 bug。 |
| F16 | ✅ | CallGraphBuildContext 批量落库路径存在。 |
| F17 | ✅ | full build 禁触发器，末尾 rebuild 已接入。 |
| F18 | ✅ | C/C++ 显式栈遍历和 third-party ignore 存在。 |
| F19 | ✅ | 批次5 修复（2026-07-20 文档对齐）：`db_build.py:_detect_optimal_workers` 实现动态算法（非原矩阵描述的 `min(4,cpu_count)`）：综合 (1) CPU 核心数（留 1 核）、(2) 可用内存（每 worker 800MB + 保留 4GB）、(3) 数据规模因子（10K/50K/200K 文件阈值）、(4) 硬上限 8，返回 1-8 worker。避免 4 worker 模式下 32GB 宿主机崩溃。 |
| F20 | ✅ | FTS5 -> Rust -> LIKE 路由顺序已进入 search path。 |
| G1 | 🟡 | 批次11 修复（P0-1 顶层 admin ACL）：CAS/toolchain/workspace 三层存在；全局 toolchain/build-context RPC 已加入 `ADMIN_ONLY_METHODS`。**复审回退（2026-07-21）**：顶层 admin ACL 已闭合，但 workspace 级 ACL 仍有缺口（见 G3/G4）。 |
| G2 | ✅ | Rust `cw_daemon` binary、UDS server、信号和 systemd notify 存在。 |
| G3 | 🟡 | 批次11 修复（P0-1 顶层 admin ACL）：SO_PEERCRED + workspace owner 过滤 + `ADMIN_ONLY_METHODS`（13 个运维方法）。**复审回退（2026-07-21）**：workspace 级 ACL 仍有缺口——`snapshot.list_workspaces` 忽略 `_peer` 返回全部 workspace；`mount.list` 向普通用户暴露全局 host/container path；`toolchain.resolve`/`build_context.list`/`resolved_edges.*/store/get/count` 使用调用方传入的 `workspace_id`，未用 `peer_uid` 查 registry owner；`resolved_edges.store` 既不在 admin-only 列表也无 owner 校验，普通用户可写别人的 workspace。这是源码可见的授权缺口，非"只差真实双 UID 验收"。 |
| G4 | 🟡 | 批次11 修复（P0-1 顶层 admin ACL）：`mount.register`/`mount.delete` 加入 `ADMIN_ONLY_METHODS`。**复审回退（2026-07-21）**：`mount.list` 仍向普通 socket 用户返回全局 host/container path 映射（见 G3）。 |
| G5 | ✅ | Python/Rust 均以 7 参数 SHA-256 计算 CAS key。 |
| G6 | ✅ | Rust CAS GC 使用 flock + transaction + pending refs。 |
| G7 | ✅ | ArcSwap SnapshotManager、history 和 generation GC 存在。 |
| G8 | 🟡 | 批次3 修复（P0-2 部分）：session/generation CAS 和 CAS publish 存在；agent payload hex/b64 字段双标准统一支持。**复审回退（2026-07-21）**：refresh 只更新 CAS/generation，未将 delta 应用到 workspace manifest、查询 SQLite 和当前 GraphSnapshot。`daemon_handle_refresh` 不写 `workspace_manifests`，replicator 合并 staging 后只 reload 外部 DB 不先 delta apply。save-to-query 链路未闭合。 |
| G9 | 🟡 | 批次3/14 修复：hex/b64 协议 + 包入口名一致。**复审回退（2026-07-21）**：`cli/main.py:_agent_start` 先调用 `workspace.connect` 后才注册 workspace，全新 agent 无法连接；客户端 workspace_instance_id 算法（项目路径 hash）与 daemon 注册 ID 算法（owner/root/remote/commit hash）不一致。agent fresh start 协议不可用。 |
| G10 | 🟡 | 批次3/7 修复（Python 路径）：`agent_protocol.py:307-313` 使用 `create_sealed_memfd`；`daemon_server.py:798-802` 通过 `is_memfd` + `validate_memfd_fd` 四重校验（seals/大小/hash/owner）。**复审回退（2026-07-21）**：Rust 端 `rust_ext/src/daemon/memfd.rs` 当前检查常规文件类型、大小、容量和可选 hash，**没有 `F_GET_SEALS`，也没有校验 FD owner 与 peer UID**。Python 和 Rust 两条实现安全等级不一致。 |
| G11 | ❌ | 批次3 修复失败：`SnapshotCachePublisher::publish_snapshot` 从 `db_path` 加载 GraphStore → 发布 SnapshotCache。**复审回退（2026-07-21）**：`DaemonConfig.codegraph_db_path_template` 默认空字符串；release/systemd/deployment 未设置 `CW_DAEMON_CODEGRAPH_DB_TEMPLATE`。即使手工设置，replicator 只 reload 外部 DB 不先 delta apply；`daemon_handle_refresh` 只更新 CAS/generation，不写 `workspace_manifests`，不应用 delta 到 symbols/calls。save-to-query 链路完全断裂。 |
| G12 | ✅ | Python/Rust JSONL staging log + fsync/atomic rewrite 存在并接入 refresh。 |
| G13 | 🟡 | 批次6 修复（Python daemon）：`server/metrics.py` `MetricsCollector` + `measure_rpc` + `metrics.snapshot`/`metrics.prometheus` RPC + CLI `cw daemon metrics`。**复审回退（2026-07-21）**：Python daemon 单例有指标，但 Linux systemd unit 启动的是 Rust `cw_daemon`，Rust daemon 无指标埋点。文档必须明确"Python daemon 已实现"还是"企业 system daemon 已实现"。当前 G13 只覆盖 Python daemon，Rust system daemon 未对齐。 |
| G14 | 🟡 | 批次3 修复（Python daemon）：`daemon_server.py` `__init__` 实例化 `HealthChecker`，`health` RPC 调用 `check_all()` 执行四项检查。**复审回退（2026-07-21）**：同 G13，Python daemon 有 health check，但 Linux systemd 启动 Rust `cw_daemon`，Rust daemon RPC endpoint 只返基础统计并固定 `status=ok`，未执行声称的四项健康检查。Python 和 Rust 实现不对齐。 |
| G15 | 🟡 | 批次3 修复：`daemon_server.py` `__init__` 加载 `DaemonConfig`，新增 `run_startup_migrations` 参数（默认 True）调用 `_run_startup_migrations()` → `server.schema_migrator.migrate_daemon_dbs(self._config)` 对 registry.db / audit.db 执行版本化迁移；失败时只记录日志不阻止 daemon 启动。test_b3_python_daemon_wiring.py 6 测试覆盖。**复审回退（2026-07-21）**：Python daemon 有版本化迁移，但 Linux systemd 启动 Rust `cw_daemon`，Rust 端只做 schema-check/init，不是版本化迁移。Python 和 Rust 实现不对齐。 |
| G16 | ✅ | 批次11 修复：Rust `dispatch.rs:545-564` `ADMIN_ONLY_METHODS` + `is_admin` fail-closed 校验；Python `daemon_server.py:75-106` 同步 `ADMIN_ONLY_METHODS` frozenset + L526-544 `_is_admin_peer` 方法 + L550-558 dispatch 顶部 fail-closed 校验。backup/restore 已加入 ADMIN_ONLY_METHODS，restore 覆盖 registry 需要 admin 权限。 |
| G17 | ✅ | 批次3 修复：`daemon_server.py` `__init__` 实例化 `SnapshotGC(cfg=self._config, policy=GCPolicy(), snapshot_cache_evictor=self._evict_snapshot_cache)`，注册 `_evict_snapshot_cache` 回调驱逐已注销 workspace 的缓存。test_b3_python_daemon_wiring.py 3 测试覆盖。 |
| G18 | ✅ | JobExecutor 有独立连接/线程池，已被 MCP 和 async diff 路径调用。 |
| G19 | ✅ | 批次3 修复：`daemon_server.py` `__init__` 实例化 `RefreshScheduler(config=SchedulerConfig(), on_batch_ready=self._on_refresh_batch_ready)`，`start_background_tasks` 默认 True 启动 `cw-refresh-flush` 后台线程定期 `force_flush()`（默认 60 秒间隔，常量 `DEFAULT_REFRESH_FLUSH_INTERVAL_SEC`），`shutdown_background_tasks` 停止线程。test_b3_python_daemon_wiring.py 6 测试覆盖。 |
| G20 | ✅ | 批次3 修复：`rust_ext/src/daemon/memfd.rs` 实现 `read_from_fd_with_validation()`：1) FD 类型校验（fstat S_IFREG，拒绝目录/设备/套接字/FIFO）2) 大小预检（st_size 预分配 buf）3) 容量上限（DEFAULT_MAX_FD_READ_BYTES=64MB）4) 摘要比对（expected_sha256 SHA-256 校验）。`workspace.rs` `handle_workspace_file_refresh` FD 路径已接入。 |
| G21 | ✅ | 批次3 修复：`protocol.rs` 新增 `_recv_msg_with_fd()` 别名包装，等价于 `recv_message_with_fds`（复数 fds），与规范文档 daemon-ipc-security.md 简短命名对齐；新增 `send_msg()` 别名（G21）和 `call_with_fd()` 请求-响应组合（G21+G22）。zero-cost re-export，原函数名保留向后兼容。 |
| G22 | ✅ | 批次3 修复：`protocol.rs` 新增 `send_msg()` 别名包装，等价于 `send_message()`；新增 `call_with_fd()` 组合 send_msg + _recv_msg_with_fd 的请求-响应模式，适用于 daemon 客户端 "send FD → 接收处理结果" 场景。命名与规范文档对齐。 |
| G23 | ✅ | 批次5 文档对齐：原矩阵描述 "11 RPC dispatch" 严重过时。实际 `server/daemon_server.py` 注册 33 个独立 RPC（workspace.*/snapshot.*/query.*/mount.*/toolchain.*/build_context.*/resolved_edges.*/ping/health/schema.version/backup/restore/gc.snapshots/gc.cas/metrics.snapshot/metrics.prometheus），`rust_ext/src/daemon/dispatch.rs` 注册 27 个 RPC 子集。ADMIN_ONLY_METHODS 已配置写操作 RPC（P0-1）。 |
| G24 | ✅ | Python/Rust UDS server 均使用有界 worker 模型。 |
| G25 | ✅ | workspace registration 使用 realpath + owner UID 校验。 |
| G26 | ✅ | remote RPC -> local SnapshotManager -> SQL 降级路径存在。 |
| G27 | ✅ | 五类 diff 和 snapshot ensure 路径存在。 |
| G28 | ✅ | SnapshotManagerService 查询方法存在。 |
| G29 | 🟡 | 批次3 修复：`rust_ext/src/daemon/budget.rs` 新增 `QueryBudget` 结构体（max_depth + max_nodes + timeout_ms）+ `BudgetTracker` 运行时计数器（visit_node + is_exceeded + is_partial）；`frontier.rs` `AffectedFrontier` 新增 `partial` 字段；`FrontierComputer` 新增 `compute_frontier_with_budget()` 方法 + `bfs_upstream_with_budget` / `bfs_downstream_with_budget`（每节点检查预算，超限返回部分结果 partial=true）；Python 接口 `compute_frontier_with_budget(max_depth, max_nodes, timeout_ms)` + `partial` getter；8 单元测试覆盖。**复审回退（2026-07-21）**：`QueryBudget` 只接入 `FrontierComputer::compute_frontier_with_budget`。常用 daemon query（search、callers/callees、call-chain、topological、cycle 等）没有统一 max-results/max-depth/timeout/truncate 执行器。G29 当前标题范围过大。 |
| G30 | ✅ | batch mark 单次原子 rewrite 存在。 |
| G31 | ✅ | compact 以 status 过滤。 |
| G32 | ✅ | 批次3 修复：`daemon_server.py` `__init__` 实例化 `SnapshotGC`，`_start_background_tasks()` 启动 `cw-snapshot-gc` 后台线程定期调用 `run_gc()`（默认 6 小时间隔，常量 `DEFAULT_SNAPSHOT_GC_INTERVAL_SEC`），`_snapshot_gc_loop` 执行 mark→sweep 并记录 marked/swept/bytes/duration_ms 指标。test_b3_python_daemon_wiring.py 5 测试覆盖。 |
| G33 | ✅ | session epoch/seq/seen/committed generation DDL 和 CAS 存在。 |
| G34 | ✅ | 批次3 修复：agent 小文件字段名 `canonical_bytes_hex`（agent_protocol.py:278 默认路径）与 daemon 端同时支持 hex + b64 + FD + abs_path 四种路径（daemon_server.py:783-883 + workspace.rs:1032-1058）。优先级 FD > hex > b64 > abs_path。 |
| G35 | ✅ | connect/file.refresh/recover RPC 已在 Python/Rust dispatch 注册。 |
| G36 | ✅ | clone/vector/semgrep job handler 已注册。 |
| G37 | 📄 | 这是测试/环境验收声明，不是产品实现项；代码层 ACL 也仍有管理 RPC 缺口。 |
| G38 | ✅ | Rust multi-lang parse 已进入 full refresh 分组主路径并有 Python fallback。 |
| H1 | ✅ | task quality schema/Mixin/MCP 已接线。 |
| H2 | ✅ | audit chain、验证和密钥轮换已接线。 |
| H3 | ✅ | rule candidate/active/injection/sync 已接线。 |
| H4 | ✅ | bootstrap scan/capture/status 已接线。 |
| H5 | 📄 | 只是 integration test 通过声明，本次不将测试代码当作实现证据。 |
| H6 | 🟡 | 批次5 文档对齐：矩阵标题原为"千万级符号性能验证"已修正为"100K 符号级性能验证"，与 `tests/_bench_multiscale.py` 实际验收规模一致。100K 验收已完成，10M 真实压测未完成（非代码 bug，是验收承诺，需依赖 F11 `build_graph_from_c_files` 接入生产路径后单独验证）。 |
| H7 | ✅ | AST metadata cache short-circuit 已接入 Rust/generic refresh。 |
| H8 | ✅ | `cw health-report` 生产子命令存在。 |
| H9 | 📄 | MCP 测试声明，不是产品功能完成项。 |
| H10 | 🟡 | LSH 增强实现存在（128-perm MinHash + 8x16 LSH + bucket cap），但矩阵自身承认缺召回率/精确率基准。状态保持 🟡 是因为缺少召回率/精确率基准数据，非代码 bug，是质量门禁验收承诺。 |
| H11 | ✅ | clone-aware impact DB 方法和 MCP 存在；矩阵 L-a 仍误写“未实现”。 |
| H12 | 🟡 | 三个 Git hook 存在，但没有独立的 AI CLI/IDE 扩展；标题过度扩大。 |
| H13 | 🟡 | 批次5 文档对齐：矩阵标题原为"15 种语言开源项目测试"已修正为"16 语言测试矩阵（synthetic fixtures + 部分真实开源项目）"。`tests/fixtures/realworld_repos.json` 列出 16 语言 × 2 = 32 个真实开源项目清单 + `clone_realworld_repos.ps1` 克隆脚本；`matrix_summary.md` 声明 2026-07-16 执行 32 项目 × 16 语言 100% 通过；`testcode/repos/` 实际克隆部分项目（vapor/linux 等）。状态保持 🟡 是因为未完成清单中全部 32 个项目克隆 + Matrix 1-4 部分维度仍依赖 synthetic fixtures，非代码 bug，是验收范围承诺。 |
| H14 | ❌ | **复审回退（2026-07-21）**：旧审计把不可执行的发布脚本升为 🟡，本轮真实运行 `python release/build.py --wheel` 失败（`--config-setting --build-option=--plat-name=win_amd64` 参数错误）。Linux 包脚本依赖的 `cw-daemon` 二进制 cargo 未声明；`release/build.py` 不生成 `dist/linux/cw`、`cw-client`、`cw-agent`、`cw-daemon`；主 CLI 不支持 `cw --version`，Gate 3 必失败；WiX 引用 `cw.exe`/`cw-client.exe`/`runtime/python.exe` 但 workflow 不生成。N5/N6/N7 产物生成链完全不可执行，回到 ❌。 |
| H15 | ⚪ | 原矩阵已标“未实施”，不列入虚假完成项。 |
| H16 | ✅ | jobs table + worker pool + handler 形成可用的生产者-消费者。 |
| H17 | ✅ | callers/callees snapshot diff 已在 MCP/client 公开。 |
| H18 | ✅ | compare snapshots 同步与 async job 路径存在。 |

## I-L 审计

| ID | 结论 | 代码/文档核验 |
|---|---|---|
| I1 | ❌ | **复审回退（2026-07-21）**：源码实算 206 MCP / 35 功能 Mixin（另有 `CodeGraphBase`）/ 39 db_*.py / v40 / 16 语言。旧审计同步文档至 205/33/40 与源码不符。I1 不能视为闭合，需统一至 206/35/39。 |
| I2 | 🟡 | 批次12 修复 + D7 部分修复：D7 跨仓库 `target_symbol_hash` 空字符串问题已修复（见 D7），但跨仓库检测仍按 import 尾段匹配同名符号，`Dict[name]` 覆盖重名，`cross_repo_deps` 无唯一约束。竞品文档 D7 标 ✅ 与代码故障"部分修复"状态仍冲突。 |
| I3 | ❌ | **复审回退（2026-07-21）**：USER_GUIDE 头部 "v40 Schema · 205 MCP 工具 · 16 语言 · 33 Mixin 类" 与源码实算 206/35 不符。Q2 删除"删除 callwarden.db 重建"危险建议这一修复可保留，但计数仍错误。 |
| I4 | 🟡 | 批次12 + G13 批次6 修复（Python daemon）：`implementation-status.md` L269 Prometheus 已标 "✅ 已实现 G13（2026-07-20 二轮评审补全）"，daemon `_handle_connection()` 接入 `measure_rpc` + `metrics.snapshot`/`metrics.prometheus` RPC + CLI `cw daemon metrics`。**复审回退（2026-07-21）**：Python daemon 有指标，但 Linux systemd unit 启动 Rust `cw_daemon`，Rust daemon 无指标埋点。Python 和 Rust system daemon 能力不对齐。 |
| I5 | ✅ | TokenSavings 能力描述和 Mixin 索引是两个视角，非重复冲突。 |
| I6 | ✅ | 根 README 已改为用户级单库并标注旧多库迁移。 |
| I7 | 🟡 | 批次12 修复（建议文字已撤销） + D7 部分修复：见 I2。 |
| I8 | ✅ | 文档仍明确不集成 ast-grep，与代码一致。 |
| I9 | ❌ | **复审回退（2026-07-21）**：源码实算 35 功能 Mixin（`db/db.py` 实际继承列表）+ `CodeGraphBase`。文档用 33（"组合的 Mixin 数"）/40（"表格行数"）两种口径解释 35 是绕开问题，没有把 35 作为单一真相。`test_33_mixin_present` 测试锁定 33 反而阻止修正。 |
| I10 | ✅ | architecture 当前已进一步更新到 v40。 |
| I11 | ❌ | **复审回退（2026-07-21）**：CONTRIBUTING.md "33 个 Mixin 类" 与源码 35 不符。需统一至 35。 |
| I12 | ❌ | **复审回退（2026-07-21）**：README MCP 数 205 与源码 206 不符。需统一至 206。 |
| I13 | ❌ | **复审回退（2026-07-21）**：mcp_tools.md 头部 205 与源码 206 不符。需统一至 206。 |
| I14 | ✅ | 旧 gap analysis 已移入 history 并标注过时。 |
| I15 | ❌ | **复审回退（2026-07-21）**：naming-analysis-report.md L148 "33 个 Mixin 组装架构" 与源码 35 不符。需统一至 35。 |
| I16 | ❌ | **复审回退（2026-07-21）**：history/README.md L41 演化轨迹仍写 "205 MCP / 33 Mixin 类"。需统一至 206/35。 |
| I17 | ❌ | **复审回退（2026-07-21）**：Schema v37→v40 同步可保留，但 205/33 与源码 206/35 仍冲突。"全部统一为 205/33"声明为假。 |
| J1 | ✅ | MinHash/LSH 代码存在。 |
| J2 | ✅ | FTS5 + triggers + rebuild 存在。 |
| J3 | ✅ | completion review 存在。 |
| J4 | ✅ | audit verify + key rotation 存在。 |
| J5 | ✅ | active rules 注入 `task_next_step`。 |
| J6 | ✅ | bootstrap scan baseline 存在。 |
| J7 | ✅ | GraphStore/Snapshot/multi-lang/canonicalize 确实在多条生产路径使用 Rust。 |
| J8 | ❌ | **复审回退（2026-07-21）**：旧审计声明 hex/b64、memfd、snapshot publish、admin ACL "均已闭合"与源码不符。实际：(1) Rust `memfd.rs` 无 `F_GET_SEALS` 也无 owner UID 校验（见 G10）；(2) `daemon_handle_refresh` 只更新 CAS/generation，未写 `workspace_manifests` 也未对 symbols/calls 执行 delta apply（见 G8/G11）；(3) `codegraph_db_path_template` 默认空，release/systemd/deployment 未设 `CW_DAEMON_CODEGRAPH_DB_TEMPLATE`（见 G11）。端到端协议链未真正闭合。 |
| J9 | ✅ | clone-aware impact 已接入 DB/MCP。 |
| K1 | ✅ | 内部 parse/publish 在拿到 canonical bytes 时复用同一份 bytes。 |
| K2 | ✅ | 批次7 修复（2026-07-20）：`canonical_bytes is None` 时 daemon 从 `msg.abs_path` 读取客户端文件的路径已加双重校验：(1) owner UID 匹配（`_validate_owned_path` / `validate_owned_path` 校验文件 owner == peer_uid，防跨用户攻击）；(2) path 必须落在 workspace `host_real_root` 内（`os.path.realpath` 比对，防路径逃逸）。Python 端 `daemon_server.py:884-899` + Rust 端 `workspace.rs:1060-1081` 同步实现，违反时抛 `path_escape` DaemonRpcError。 |
| K3 | ✅ | `parse_canonical_bytes_py` 已导出和注册。 |
| K4 | ❌ | **复审回退（2026-07-21）**：旧审计声明 "端到端协议链路已闭合" 与源码不符。`dispatch.rs` 调用 refresh/staging/replicate 但 `codegraph_db_path_template` 默认空导致 `SnapshotCachePublisher` 不注入；replicator 只 reload 外部 DB 不先 delta apply；`daemon_handle_refresh` 不写 `workspace_manifests`。数据发布链完全断裂（见 G8/G11/J8）。 |
| K5 | ⚪ | 原矩阵仍标记未统一，不列入虚假完成项。 |
| K6 | ✅ | generation DDL 已提取共享。 |
| L1 | 🟡 | optional task validation/context 存在，但不“强制关联”，无 task_id 时照常写入。 |
| L2 | 🟡 | 二轮评审补全（2026-07-20）：技术限制——git 无 pre-checkout/pre-reset hook，`reset --hard` 的 working tree 写入先于 ref 更新，故无法在 working tree 破坏前拦截。当前实现：(1) pre-push hook 记录 force push 到 `destructive_operations` 表（软门禁）；(2) **新增 reference-transaction hook** 审计 ref 变更（reset_hard/branch -f/branch_delete/branch_create），仅记录不阻止；(3) Agent hook 层（仅限参与 Agent）阻止 `git reset --hard` / `git checkout .`，普通 git 用户不受限。状态保持 🟡 是因为 git 技术限制导致无法对普通用户强制拦截 checkout/reset（设计决策，非代码 bug）。 |
| L3 | ✅ | pre-commit `check-task || true` 与“软门禁”描述一致。 |
| L4 | ✅ | `file_read(include_context=True)` 返回符号及 callers/callees 摘要。 |
| L5 | ✅ | compile_commands/toolchain/build context/resolved edges 方法和入口存在。 |
| L6 | ✅ | C stream pool 和逐个消费已接入 build。 |
| L7 | 🟡 | psutil/Windows Psapi RSS 函数存在 `shared_benefit_metrics.py`，无非测试生产调用方。 |
| L8 | ✅ | 增量 call resolve 使用 `only_files`，避免全量 parse result 加载。 |
| L9 | ✅ | Rust pool 和 15 语言提取配置存在，且 refresh 有实际调用。 |
| L10 | 📄 | 只是 MCP 后续设计方向，不是一个已完成功能。 |
| L11 | ✅ | UTF-8 console setup 已在入口复用。 |
| L12 | ✅ | symbol-id patch DB/MCP 入口存在。 |
| L13 | ✅ | next job 返回源码/调用/风险摘要。 |
| L14 | ✅ | parser `__getattr__` 懒加载和 factory 按语言 import 存在。 |
| L15 | ✅ | full build 生产输出 scan/parse/version/symbol/call/depth/FTS/GC 分段计时。 |
| L16 | 📄 | Agent 工具设计原则是文档方向，不是产品完成项。 |

## M-N 审计

| ID | 结论 | 代码/产物核验 |
|---|---|---|
| M1 | ✅ | Rust SO_PEERCRED 实现并被 UDS server 调用。 |
| M2 | ✅ | BOM/编码/换行规范化和 SHA-256 已进入 daemon refresh。 |
| M3 | ✅ | CSR/SymbolTable/FxHashMap/Pod 结构已被 GraphStore/Snapshot 使用。 |
| M4 | 🟡 | **复审回退（2026-07-21）**：delta 模块已进入 refresh handler 调用路径，但源码明确使用 `store=None` 退化模式，结果只写 staging JSON，未应用到 GraphSnapshot。 |
| M5 | 🟡 | **复审回退（2026-07-21）**：frontier 模块已进入 refresh handler 调用路径，但 frontier 没有 upstream/downstream，结果只写 staging JSON，未应用到 GraphSnapshot。 |
| M6 | 🟡 | **复审回退（2026-07-21）**：local metrics 更新模块已进入 refresh handler 调用路径，但 metrics 没有旧图对比，结果只写 staging JSON，未应用到 GraphSnapshot。 |
| M7 | ✅ | snapshot diff 路径调用 Rust diff 模块。 |
| M8 | ✅ | 复审确认真实修复（2026-07-21）：本地 `cw --watch` 已优先使用 Rust `PyDebouncedFileWatcher`（`server/watcher.py`），watchdog 为 fallback。Enterprise agent 仍走 watchdog 路径，但本地路径 Rust-first 已闭合。 |
| M9 | ✅ | single/batch/pool multi-lang parse 已进入 build 主路径。 |
| M10 | 🟡 | `cw_daemon` 独立 binary 和同 crate PyO3 cdylib/rlib 存在；“daemon binary + PyO3 绑定”表述不准确。 |
| N1 | ✅ | `version.toml` 为 0.3.0，ABI/平台/角色字段存在。 |
| N2 | ✅ | `version_sync.py` 实跑通过 Python/Cargo/__init__ 一致性。 |
| N3 | ❌ | **复审回退（2026-07-21）**：真实运行 `python release/build.py --wheel` 失败（`--config-setting --build-option=--plat-name=win_amd64` 参数错误）。当前唯一 wheel 是 `release/dist/callwarden-0.3.0-py3-none-any.whl` 不含 `callwarden_core`。CI 先在 `rust_ext/target` 构建，但 `release/build.py --wheel` 要求根目录已存在 `.pyd/.so`；干净 runner 不会自动复制，Gate 2 会更早失败。`release/build.py` 不生成 `dist/linux/cw`、`cw-client`、`cw-agent`、`cw-daemon`。wheel 构建链实际不可执行。 |
| N4 | 🟡 | 分层加载器实现存在，没有 Python CLI/daemon 生产 import。 |
| N5 | ❌ | WiX 只有未编译 XML，引用的 Windows 输入产物不存在，Authenticode 仅注释命令。 |
| N6 | ❌ | macOS 脚本未在 macOS 构建/签名/公证，缺入口时会生成 placeholder。 |
| N7 | ❌ | **复审回退（2026-07-21）**：批次14 fail-fast 修复只是把 `|| true` 改成 `exit 1`，让缺二进制时早失败。但 Linux 包脚本依赖 `cw-daemon` 二进制，Cargo 只声明 `cw_daemon`（下划线），systemd unit 又执行 `cw_daemon`。Cargo/脚本/unit 三处命名不一致，且 cargo 不生成 `cw-daemon`。Linux 打包链实际不可执行。 |
| N8 | ❌ | **复审回退（2026-07-21）**：`enterprise-release.yml` version key 修正可保留，但 workflow 未实际运行过。WiX 引用 `cw.exe`/`cw-client.exe`/`runtime/python.exe` 但 workflow 不生成这些输入；macOS 脚本 `APPLE_*` 环境变量与脚本读取的 `CW_APPLE_*` 不一致，签名/公证被跳过；Linux offline bundle 调用参数顺序错误且带 `|| true`；workflow 上传的 `manifest.json` 路径与脚本产出不一致。Release CI 链不可执行。 |

## 实算基线

| 指标 | 当前代码 | 主要文档状态 |
|---|---:|---|
| MCP tools | 205 | `mcp_tools.md`/README 正确，architecture 架构图和 USER_GUIDE 仍有 204 |
| Schema | v39 | architecture/implementation-status 正确，USER_GUIDE/history 仍为 v37 |
| Languages | 16 | 一致 |
| `db_*.py` files | 39 | architecture 写 38 |
| DB Mixin classes | 33 | 文档统一写 40 |
| `CodeGraphDB` composed Mixins | 35 | 文档统一写 40 |
| CLI parser registrations | 130 (`cli/main.py` 92 + `daemon_commands.py` 38) | “145+”与旧 38+98 都没有可复现口径 |
| Release version | 0.3.0 | Python/Cargo/__init__ 一致 |

## 关键文档一致性

| 文档 | 结论 | 主要问题 |
|---|---|---|
| `_feature_matrix.md` | ❌ | 顶部 196/v36/37 与后文 205/v39 矛盾；H11 与 L-a 矛盾；大量组件/测试/产品完成混标。 |
| `README.md` | 🟡 | DB 路径和 MCP 数正确；sqlite-vec 宣传不符代码，Mixin 数错。 |
| `AGENTS.md` | 🟡 | DB 安全规则正确；sqlite-vec、40 Mixin 和“同文件跨项目只 parse 一次”过度承诺。 |
| `docs/architecture.md` | ❌ | 同文档同时出现 204/205，Mixin 40/38 files 均错，sqlite-vec 错，daemon 纵向链路过度完成化。 |
| `docs/design/implementation-status.md` | 🟡 | v39/205 正确；40 Mixin 和 sqlite-vec 错；Prometheus 仍标部分，与矩阵 G13/I4 冲突。 |
| `callwarden_USER_GUIDE.md` | ❌ | v37/204/40 过时，且建议删除用户级 DB。 |
| `docs/quickstart.md` / `docs/README.md` | ❌ | 仍展示旧 `<hash>/callwarden.db`，宣传 sqlite-vec。 |
| `docs/deployment.md` | ❌ | 新旧 DB 路径混用，直接删 WAL/SHM，备份命令仍带旧通配多库。 |
| `docs/mcp_tools.md` | 🟡 | 205 正确；容器章节仍写旧 hash DB，metrics 用途描述不成立。 |
| `docs/cli_reference.md` | 🟡 | 主 CLI 大部分可对应；角色入口连字符/下划线不一致，daemon 子命令数过时。 |
| `docs/design/daemon-deploy-runbook.md` | ❌ | 假定 `cw-agent` 二进制和 deb 已存在，与当前打包空缺不符。 |
| `docs/design/cross-platform-packaging-release-plan.md` | 📄 | 作为目标设计基本合理，不能当作 N5-N8 已验收证明。 |

## 建议修复顺序

1. 先收紧 daemon admin RPC：引入明确 admin policy，对 backup/restore/mount/toolchain/build-context 做 peer UID/group/capability 授权。
2. 用一个协议打通 Agent -> daemon -> CAS -> manifest -> SnapshotManager -> query，删除 hex/b64 双标准和未使用 `ipc_transport` 实现。
3. 修复 PRChecker 的 guardrail 方法名、Semgrep 汇总和 fail-closed 策略。
4. 修复 release pipeline，以“干净机安装后 Rust 扩展/daemon/agent 真实启动”为门禁，禁止 placeholder 打包成功。
5. 将 D1 更名为“SQLite BLOB + in-process cosine”，或真正接入 sqlite-vec/vec0。
6. 修复 D7 target hash 和依赖方向，再恢复“跨仓库影响传播已完成”标记。
7. 删除危险 DB/WAL 操作文档，用代码实算数据重生 matrix/README/architecture/status 基线。
