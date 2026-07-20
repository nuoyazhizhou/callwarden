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

本轮共核验 **200 个唯一 ID**（原矩阵另有 2 行重复的 `D3` 路线图引用）：128 项确认完成，40 项仅部分完成，25 项完成声明不成立，5 项只是测试或设计记录，2 项原矩阵本就未声称完成。换句话说，矩阵中的“完成”类记录只有 **64.6%**（128/198）能按生产代码和公开路径直接确认。

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

### P0 Watcher 到可查询 Snapshot 的纵向链路未闭合

- Agent 小文件发送 `canonical_bytes_hex`，Python/Rust daemon 只读 `canonical_bytes_b64`。
- Agent 大文件发送普通临时文件 FD，不是文档承诺的 sealed memfd。
- Rust refresh 成功后用空 `db_path` 调用 Replicator，代码注释明确说“不发布 snapshot”。
- 因此“保存文件 -> 新 generation -> query 可见”不成立。

证据：`server/agent_protocol.py:214-280`，`server/daemon_server.py:495`，`rust_ext/src/daemon/workspace.rs:931-1030`。

### P0 跨平台发布会产出空壳或在 CI 早期失败

- 当前 wheel 是 `py3-none-any`、`Root-Is-Purelib: true`，且不包含 `callwarden_core`。
- Linux 脚本在 `cw/cw-client/cw-agent/cw-daemon` 不存在时仅打印 `NOTE` 后继续打包；RPM 明确是 TODO。
- `pyproject.toml` 生成下划线入口 `cw_agent/cw_daemon/cw_client`，systemd/文档/包使用连字符名称。
- Release CI 读取不存在的 `version.toml['package']`，且用错误参数调用 `parse_file_lang`。

证据：`release/build.py:39-75`，`release/linux/build_packages.sh:65-122`、`:218-224`，`.github/workflows/enterprise-release.yml:70-75`、`:172-177`。

### P1 PR 检查 fail-open

`PRChecker` 调用不存在的 DB 方法 `guardrail_check_edit`（实际为 `check_before_edit`），并吞掉异常；最后只查 `guardrail_findings`，不把本次 Semgrep findings 并入 SARIF。A19/A21 不能视为闭合。

证据：`cicd/pr_check.py:66-91`、`:153-189`，`db/db_guardrail.py:221`。

### P1 文档包含危险且过时的运维建议

`callwarden_USER_GUIDE.md` 仍建议删除不可恢复的用户级 DB，`docs/deployment.md` 建议直接 `rm` WAL/SHM；这与 `AGENTS.md` 的禁止规则相反。

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
| A14 | ❌ | 不存在 `scan_semgrep_incremental`；扫描记录仍硬编码 `scan_type='full'`，也没有增量清理语义。 |
| A15 | ❌ | 自研 ignore parser 不是完整 gitignore 语义：`strip()` 丢失尾随空格语义，不支持字符类，目录剪枝会影响后续 negation 恢复。 |
| A16 | ✅ | `.callwardenignore` 加载、生成和 GC 共享 matcher 已接入。 |
| A17 | ✅ | archive/restore/status/purge/retention 均有生产入口。 |
| A18 | ✅ | full build 末尾调用 Young GC。 |
| A19 | 🟡 | SARIF exporter 和 GitHub Action Python 入口存在，但依赖的 PRChecker 不闭合，当前项目 workflow 也未上传 SARIF。 |
| A20 | ✅ | changed-file 分析和按文件 refresh 已实现。 |
| A21 | ❌ | guardrail 方法名错配、异常被吞，Semgrep 结果不进最终 findings。 |
| A22 | ✅ | 原子写、LSP 路径/子进程限制、错误脱敏等生产代码存在。 |
| A23 | 🟡 | 文件级并行存在，但主路径现为 Rust pool/ProcessPool，ThreadPool 主要是降级；矩阵描述已过时。 |
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
| C10 | 🟡 | post-commit capture 可建立关联，但是 best-effort hook，可被 `--no-verify` 或外部编辑绕过。 |
| C11 | ✅ | 手动/自动 reopen 和祖先链回退已实现。 |
| D1 | ❌ | 只存 BLOB 并全表读取后做 Rust/numpy/Python cosine；生产代码没有加载 sqlite-vec，也没有 vec0 表。 |
| D2 | ✅ | sentence-transformers 生成 embedding 路径存在，为可选依赖。 |
| D3 | ✅ | semantic search 及 keyword fallback 存在。 |
| D4 | ✅ | 相似函数查找已公开。 |
| D5 | 🟡 | `ask_codebase` 是检索+调用上下文组装器，返回 `rag_context`，不生成最终问答。 |
| D6 | ✅ | hover/definition/references/diagnostics/completion 和 MCP 转发存在。 |
| D7 | ❌ | 检测时把 `target_symbol_hash` 固定写为空字符串，反向影响查询永远无法命中；匹配也只是 import 末段简名。 |
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
| F11 | 🟡 | `build_graph_from_c_files` PyO3 函数存在，但非测试生产代码没有调用方。 |
| F12 | ✅ | `.cwsnap` mtime 校验、mmap load 和后台 dump 已接入 `_get_graph_store`。 |
| F13 | ✅ | calls 索引迁移与 GraphStore 降级路径一致。 |
| F14 | 🟡 | 延迟建索引/分段 commit/WAL truncate 有代码；10M/8.1x 是基准承诺，本次不以测试报告认定。 |
| F15 | 🟡 | cache/page size 配置已实施；17.8% 数值未作当前环境复验。 |
| F16 | ✅ | CallGraphBuildContext 批量落库路径存在。 |
| F17 | ✅ | full build 禁触发器，末尾 rebuild 已接入。 |
| F18 | ✅ | C/C++ 显式栈遍历和 third-party ignore 存在。 |
| F19 | ❌ | 当前不是 `min(4,cpu_count)`，而是基于 CPU/可用内存/文件规模的 1-8 动态 worker。 |
| F20 | ✅ | FTS5 -> Rust -> LIKE 路由顺序已进入 search path。 |
| G1 | 🟡 | CAS/toolchain/workspace 三层存在，但全局 toolchain/build-context RPC 没有 UID/admin 授权。 |
| G2 | ✅ | Rust `cw_daemon` binary、UDS server、信号和 systemd notify 存在。 |
| G3 | 🟡 | SO_PEERCRED 和 workspace owner 过滤存在，但运维/全局配置 RPC 忽略 peer；真实双 UID 验收不以测试代码代替。 |
| G4 | 🟡 | registry 和 mount CRUD 存在，mount 为全局可写且无 admin ACL。 |
| G5 | ✅ | Python/Rust 均以 7 参数 SHA-256 计算 CAS key。 |
| G6 | ✅ | Rust CAS GC 使用 flock + transaction + pending refs。 |
| G7 | ✅ | ArcSwap SnapshotManager、history 和 generation GC 存在。 |
| G8 | 🟡 | session/generation CAS 和 CAS publish 存在，但 agent payload 不兼容且 refresh 后不发布可查 snapshot。 |
| G9 | 🟡 | AgentSession/Watcher/systemd unit 存在，但 hex/b64 协议错配，包入口名也不一致。 |
| G10 | 🟡 | Python memfd 库存在，实际 agent 传普通 temp FD；Rust 端未校验 seal/size/hash 且无界 `read_to_end`。 |
| G11 | 🟡 | SnapshotCachePublisher bridge 存在，但只在模块内/测试使用；Rust refresh 主路径没有注入 publisher。 |
| G12 | ✅ | Python/Rust JSONL staging log + fsync/atomic rewrite 存在并接入 refresh。 |
| G13 | ❌ | collector/to_prometheus 类存在，但 daemon 无任何埋点；CLI/MCP 新进程读自己的空单例，不是 daemon metrics。 |
| G14 | 🟡 | HealthChecker/RecoveryHandler 存在，但 RPC endpoint 只返基础统计并固定 `status=ok`，未执行声称的四项健康检查。 |
| G15 | 🟡 | `SchemaMigrator` 类存在，没有 daemon/CLI 生产调用方；Rust 只做 schema-check/init，不是版本化迁移。 |
| G16 | 🟡 | Rust backup/restore RPC 可达，但忽略 peer/admin 授权，restore 可覆盖 registry。 |
| G17 | 🟡 | Python disk SnapshotGC 类未接线；Rust `gc.snapshots` 只清内存 generation history。 |
| G18 | ✅ | JobExecutor 有独立连接/线程池，已被 MCP 和 async diff 路径调用。 |
| G19 | 🟡 | `RefreshScheduler` 类存在，没有非测试生产实例化。 |
| G20 | 🟡 | 四重校验函数存在，未被当前 Rust 生产接收路径使用。 |
| G21 | 🟡 | `_recv_msg_with_fd` 存在，没有生产调用方。 |
| G22 | 🟡 | `send_msg` 存在，没有生产调用方；Agent 另走 `call_with_fd`。 |
| G23 | 🟡 | Python service 和 Rust dispatch 均存在，“11 RPC”已严重过时；生产 systemd 运行 Rust service。 |
| G24 | ✅ | Python/Rust UDS server 均使用有界 worker 模型。 |
| G25 | ✅ | workspace registration 使用 realpath + owner UID 校验。 |
| G26 | ✅ | remote RPC -> local SnapshotManager -> SQL 降级路径存在。 |
| G27 | ✅ | 五类 diff 和 snapshot ensure 路径存在。 |
| G28 | ✅ | SnapshotManagerService 查询方法存在。 |
| G29 | 🟡 | max_results/max_depth 部分生效；timeout/max_nodes/frontier 没有传入 Rust 遍历，`start()` 后从未 `visit_node()`。 |
| G30 | ✅ | batch mark 单次原子 rewrite 存在。 |
| G31 | ✅ | compact 以 status 过滤。 |
| G32 | 🟡 | mark/sweep 策略类存在，但未接入 daemon scheduler/运维路径。 |
| G33 | ✅ | session epoch/seq/seen/committed generation DDL 和 CAS 存在。 |
| G34 | 🟡 | 内部 parse/publish 函数闭合，但实际 agent 的小文件字段名不匹配。 |
| G35 | ✅ | connect/file.refresh/recover RPC 已在 Python/Rust dispatch 注册。 |
| G36 | ✅ | clone/vector/semgrep job handler 已注册。 |
| G37 | 📄 | 这是测试/环境验收声明，不是产品实现项；代码层 ACL 也仍有管理 RPC 缺口。 |
| G38 | ✅ | Rust multi-lang parse 已进入 full refresh 分组主路径并有 Python fallback。 |
| H1 | ✅ | task quality schema/Mixin/MCP 已接线。 |
| H2 | ✅ | audit chain、验证和密钥轮换已接线。 |
| H3 | ✅ | rule candidate/active/injection/sync 已接线。 |
| H4 | ✅ | bootstrap scan/capture/status 已接线。 |
| H5 | 📄 | 只是 integration test 通过声明，本次不将测试代码当作实现证据。 |
| H6 | ❌ | 标题是 10M 验证，备注实际只记录 100K；未完成千万级验收。 |
| H7 | ✅ | AST metadata cache short-circuit 已接入 Rust/generic refresh。 |
| H8 | ✅ | `cw health-report` 生产子命令存在。 |
| H9 | 📄 | MCP 测试声明，不是产品功能完成项。 |
| H10 | 🟡 | LSH 增强实现存在，矩阵自身承认缺召回率/精确率基准，不能视为质量门禁完成。 |
| H11 | ✅ | clone-aware impact DB 方法和 MCP 存在；矩阵 L-a 仍误写“未实现”。 |
| H12 | 🟡 | 三个 Git hook 存在，但没有独立的 AI CLI/IDE 扩展；标题过度扩大。 |
| H13 | ❌ | 证据是项目内 synthetic fixtures/tests，不是 15/16 个真实开源项目验收。 |
| H14 | ❌ | 无 MSI/PKG/DEB 产物；现有验证只是 wheel/XML/bash/YAML 形式检查。 |
| H15 | ⚪ | 原矩阵已标“未实施”，不列入虚假完成项。 |
| H16 | ✅ | jobs table + worker pool + handler 形成可用的生产者-消费者。 |
| H17 | ✅ | callers/callees snapshot diff 已在 MCP/client 公开。 |
| H18 | ✅ | compare snapshots 同步与 async job 路径存在。 |

## I-L 审计

| ID | 结论 | 代码/文档核验 |
|---|---|---|
| I1 | ❌ | MCP=205 正确，但实际为 35 个组合 Mixin/39 个 `db_*.py`，不是 40；v37 也已过时。 |
| I2 | 🟡 | 竞品文档字面已更新，但把 D7 跨仓库标为完成与代码故障不符。 |
| I3 | ❌ | `callwarden_USER_GUIDE.md` 当前仍是 v37 / 204 MCP / 40 Mixin。 |
| I4 | ❌ | `implementation-status.md` 自身仍把 Prometheus 标为“部分，缺 endpoint”；代码也未闭合 daemon metrics。 |
| I5 | ✅ | TokenSavings 能力描述和 Mixin 索引是两个视角，非重复冲突。 |
| I6 | ✅ | 根 README 已改为用户级单库并标注旧多库迁移。 |
| I7 | 🟡 | 建议文字已撤销，但 D7 影响传播并未真正完成。 |
| I8 | ✅ | 文档仍明确不集成 ast-grep，与代码一致。 |
| I9 | ❌ | architecture 的 40 Mixin / 38 files 与实际 35 / 39 不符。 |
| I10 | ✅ | architecture 当前已进一步更新到 v39。 |
| I11 | ❌ | CONTRIBUTING 的 40 Mixin 与代码不符。 |
| I12 | ✅ | 根 README/docs README 的 MCP 数为 205。 |
| I13 | ✅ | mcp_tools 头部为 205，且已解释 179 分类合计的差异。 |
| I14 | ✅ | 旧 gap analysis 已移入 history 并标注过时。 |
| I15 | ❌ | naming report 写 40 Mixin，代码实际不支持。 |
| I16 | ❌ | history README 仍把 v37/204/40 写为“当前”，未跟进 v39/205/实际 Mixin。 |
| I17 | 🟡 | architecture/implementation-status 的 schema v39 正确，但矩阵顶部、USER_GUIDE、history 和 architecture 的 204 工具仍冲突。 |
| J1 | ✅ | MinHash/LSH 代码存在。 |
| J2 | ✅ | FTS5 + triggers + rebuild 存在。 |
| J3 | ✅ | completion review 存在。 |
| J4 | ✅ | audit verify + key rotation 存在。 |
| J5 | ✅ | active rules 注入 `task_next_step`。 |
| J6 | ✅ | bootstrap scan baseline 存在。 |
| J7 | ✅ | GraphStore/Snapshot/multi-lang/canonicalize 确实在多条生产路径使用 Rust。 |
| J8 | ❌ | RPC 方法注册很多，但 hex/b64、memfd、snapshot publish 和 admin ACL 均未闭合。 |
| J9 | ✅ | clone-aware impact 已接入 DB/MCP。 |
| K1 | ✅ | 内部 parse/publish 在拿到 canonical bytes 时复用同一份 bytes。 |
| K2 | ❌ | `canonical_bytes is None` 时仍读客户端 `abs_path`，refresh handler 未对该路径执行 workspace ownership/escape 校验。 |
| K3 | ✅ | `parse_canonical_bytes_py` 已导出和注册。 |
| K4 | 🟡 | dispatch 已调用 refresh/staging/replicate，但请求字段错配和 snapshot 未发布使端到端仍失败。 |
| K5 | ⚪ | 原矩阵仍标记未统一，不列入虚假完成项。 |
| K6 | ✅ | generation DDL 已提取共享。 |
| L1 | 🟡 | optional task validation/context 存在，但不“强制关联”，无 task_id 时照常写入。 |
| L2 | ❌ | 标题是 checkout/reset --hard，代码只在 pre-push 记录 force push，不拦截 checkout/reset。 |
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
| M4 | 🟡 | delta 模块和 PyO3 导出存在，无生产调用方；当前 refresh 未生成 ParseDelta。 |
| M5 | 🟡 | frontier 模块存在，无生产调用方。 |
| M6 | 🟡 | local metrics 更新模块存在，无生产调用方。 |
| M7 | ✅ | snapshot diff 路径调用 Rust diff 模块。 |
| M8 | 🟡 | Rust notify watcher 和 PyO3 类存在，CLI/agent 当前使用 Python watchdog watcher。 |
| M9 | ✅ | single/batch/pool multi-lang parse 已进入 build 主路径。 |
| M10 | 🟡 | `cw_daemon` 独立 binary 和同 crate PyO3 cdylib/rlib 存在；“daemon binary + PyO3 绑定”表述不准确。 |
| N1 | ✅ | `version.toml` 为 0.3.0，ABI/平台/角色字段存在。 |
| N2 | ✅ | `version_sync.py` 实跑通过 Python/Cargo/__init__ 一致性。 |
| N3 | ❌ | build.py 形式上有步骤，但当前 wheel 不包 Rust 扩展且未打包 daemon/角色二进制。 |
| N4 | 🟡 | 分层加载器实现存在，没有 Python CLI/daemon 生产 import。 |
| N5 | ❌ | WiX 只有未编译 XML，引用的 Windows 输入产物不存在，Authenticode 仅注释命令。 |
| N6 | ❌ | macOS 脚本未在 macOS 构建/签名/公证，缺入口时会生成 placeholder。 |
| N7 | ❌ | Linux 无 deb/rpm/tar.zst 成品，缺二进制时仍继续，RPM spec 明确 TODO。 |
| N8 | ❌ | Workflow 未运行且静态即可见失败点：错误 version key、纯 Python wheel、错误 parser 调用、WiX 版本/输入不匹配。 |

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
