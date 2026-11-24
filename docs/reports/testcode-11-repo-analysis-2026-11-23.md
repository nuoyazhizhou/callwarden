# testcode 同类产品 12 仓分析报告

> 日期：2026-11-23（2026-11-24 追加 LoopForge；2026-11-24 graphify 升级实现级深读；2026-11-24 Understand-Anything 升级实现级深读；2026-11-24 codebase-memory-mcp 升级实现级深读；2026-11-24 hermes-agent 升级实现级深读）
> 范围：`testcode/` 下 12 个同类/相关开源产品仓库
> 方法：优先走 callwarden 图谱工具（workspace 注册 + build_graph + repo map + 健康检查），大仓构建超时（watchdog 连续 2 次 /health 失败判死）改走源码/README 分析。

## 一、入库与分析状态

| 仓库 | 技术栈 | 定位 | 分析路径 | 规模 |
|---|---|---|---|---|
| code-review-graph | Python / Tree-sitter | 审查上下文图谱 + MCP（30 工具） | 图谱 | 461 文件 / 13100 符号 |
| codebase-memory-mcp | C/TS | 164 语言 LSP 记忆服务（17 工具） | 源码（构建超时） | 1403 文件 |
| CodeGraph | Rust | 42 工具 + VS Code/JetBrains 插件 | 图谱 | 602 文件 |
| GitNexus | TS / KuzuDB | 代码图谱 + MCP（refresh_index 单飞设计出处） | 源码（构建超时） | 2258 文件 |
| graphify | Python | 图谱化 agent（**callwarden 真实用户**，`.callwarden/agent-integrations/` 有 20+ 平台集成产物） | 图谱 | 792 文件 |
| Understand-Anything | TS | 多 agent 代码理解插件 | 图谱 | 272 文件 / 1061 符号 |
| deepseek-harness | TS | everything-is-a-plugin | 源码（构建超时） | 3895 文件 |
| hermes-agent | Python | 自我改进 agent 平台 | 源码（构建超时） | 2071 文件 |
| DeepSeek-TUI (Codewhale) | Rust | 终端 agent | 未注册 | — |
| goodbuddy | Electron | 桌面 agent | 图谱 | 1577 文件 |
| orca | TS | worktree 并行编排 | 图谱 | 11790 文件 |
| **LoopForge**（新增） | Go 1481 / TS 117 | SOP 驱动的团队编码 Agent 工作台（Docker 沙箱、角色分离、人工关口、10 种 Agent CLI 执行器） | 注册成功（ws 1792），构建被 `E_PRINCIPAL_WORKSPACE_REQUIRED` 挡住 → 源码分析 | 2095 文件 / 5582 Go 函数 |

结论：**6/12 走图谱入库分析，1 个注册成功但构建受本机 daemon 实例解析问题阻断（LoopForge），4 大仓（GitNexus / hermes-agent / deepseek-harness / codebase-memory-mcp）因构建超时改源码分析，DeepSeek-TUI-src 未注册**。即并非全部通过图谱工具入库——大仓构建可用性正是本次改造要解决的问题。

## 一·补：分析深度分级（诚实口径）

图谱入库 ≠ 功能实现级借鉴。实际深度：

| 深度 | 仓库 | 证据 |
|---|---|---|
| 实现级深读 | code-review-graph、GitNexus、CodeGraph、**graphify（2026-11-24）**、**Understand-Anything（2026-11-24）**、**codebase-memory-mcp（2026-11-24）**、**hermes-agent（2026-11-24）** | 直接读到 `changes.py: compute_risk_score`、`graph.py` 边置信度、`docs/mcp-reference.md` 响应预算、`refresh_index` 单飞、README 工具面 profile 表；graphify 深读 serve/watch/affected/hooks/analyze 五个实现与 24 平台集成产物（见第三节）；Understand-Anything 深读 SKILL.md 编排主控 / compute-batches / prepare-incremental / file-analyzer / hooks 与分发 manifest（见「二·补 B」）；codebase-memory-mcp 深读 language.c / lang_specs.c / store.h+graph_mem.c / pipeline_incremental.c / watcher.c 与 17 工具面（见「二·补 C」）；hermes-agent 深读 background_review / curator / memory_manager / suggestions / postmortem-run 五个实现与 kanban_db_dispatch 辅助（见「二·补 D」） |
| 结构 + README 级 | goodbuddy、orca | 仅 build_graph/repo map 的结构指标与 README 定位，未逐功能读实现 |
| README/架构级 | deepseek-harness | 构建超时，只到架构与文档层 |
| 新增/未覆盖 | LoopForge（本次补齐源码层）、DeepSeek-TUI（未注册） | LoopForge 见下节具体实现点 |

## 二、可借鉴清单（10 条）

1. **风险评分 signals 同源模式**（code-review-graph `changes.py: compute_risk_score`）
   - 6 项信号：flow 0.05（cap 0.25）、跨社区调用者 0.05/个（cap 0.15）、测试覆盖 0.30→0.05（5+ TESTED_BY）、安全命名 0.20、调用者/20 cap 0.10、churn commits/10 cap 0.15。
   - `risk_signals()` 返回 `{name: {score, count}}` 且与总分同源——**解释与分数永不矛盾**。
2. **边置信度四档**（code-review-graph `graph.py` L540-620）：EXTRACTED=1.0 / INFERRED=0.8 / AMBIGUOUS=0.4 / UNRESOLVED=0.2。
3. **MCP 响应预算**（GitNexus `docs/mcp-reference.md`）：maxTokens per call + `GITNEXUS_MCP_DEFAULT_MAX_TOKENS` env + 4 字节/token 确定性估算 + trim deepest-first + `pagination.trimmedForBudget.shownByDepth` + response_filter 标记省略。
4. **工具面 profile**（CodeGraph `README.md` L165-176）：core 8 / graph 17 / memory 14 / security / all 42，`--profile` 或 `CODEGRAPH_TOOL_PROFILE` env。动机 = 42 工具全量暴露膨胀 agent prompt context。
5. **影响消歧**（code-review-graph）：ranked ambiguous candidates + target_uid/file_path/kind 收窄 + minConfidence 分 WILL BREAK / LIKELY AFFECTED / UNAFFECTED。
6. **refresh 单飞**（GitNexus refresh_index）：bounded（预扫文件数，超 MAX_FILES 拒绝）+ timeout（默认 10 分钟，超时返回错误但后台继续跑完并发布）+ per-repo 单飞（进行中再调用报 "already running"，dry_run 报 inProgress）+ progress notifications。
7. **rationale 入图**（code-review-graph）：`# NOTE:` / `# IMPORTANT:` / `# HACK:` / `# WHY:` / `# RATIONALE:` / `# TODO:` / `# FIXME:` 前缀注释（Python），JS/TS 用 `// NOTE:`，随解析入库。
8. **receipts 审计**（GitNexus）：构建/刷新产物回执可审计。
9. **durable session log**（GitNexus）：会话日志持久化，可恢复/可追责。
10. **其他**：session admission barrier、Smart CI、verify_design、FTS5 session 搜索、语言 skill 文档目录。

### 追加自 LoopForge（源码层，11-14）

11. **SOP-as-code + 人工关口**（`internal/httpapi` runsHandlers / `docs/bot-sop-controls.md`）：流程 = 有序节点（Agent / 脚本 / 人工审批 / 子 SOP），每节点可重试、可打回、可从任意节点重跑；每次运行独立沙箱 + 节点级产物与日志。**对 callwarden 的意义**：任务单（task + steps）天然是 SOP 的雏形，缺的是「人工关口节点」与「从某节点重跑」语义。
12. **角色级 MCP 服务器注入 + 代理 token**（`internal/knowledge/rules.go: MCPServerDefinition`，`internal/httpapi/agent_tool_catalog.go:433 listUpstreamTools`）：外部 MCP 以 `transport=http + URL + Headers(Authorization: Bearer <按角色/项目限定的代理 token>) + TimeoutSec` 注入，上游真实凭据不进沙箱；工具清单在会话初始化时拉取并计入诊断。**对 callwarden 的意义**：这正是 callwarden 挂进 harness 的标准面，且与刚做的工具面 profile / lease token 天然契合。
13. **外部工作流用量接入**（`docs/external-workflow-usage.md`）：`POST /api/insights/v1/sources` 发数据源 token，`POST /api/insights/v1/ingest` 上报执行汇总，看板用 `origin=external` 区分来源（workflow key 形如 `external:{sourceId}:{workflowId}`）。**对 callwarden 的意义**：callwarden 的任务循环可作为「外部工作流」被 LoopForge 统计，无需改造对方内核。
14. **知识面契约**（`internal/knowledge/rules.go` + `service.go`）：`kb_search / kb_fetch / kb_list_sources / kb_get_source`，结果带 `citation = source@commit:path#L10-L20`；`degraded=true` 表示语义检索降级为词法；新知识写工作区 `knowledge.md`（带 front matter）由人工审核后沉淀。**对 callwarden 的意义**：把 repo map / 符号 / 影响面 / rationale 以同样四个动词的形状暴露，即可复用其「先查再做 + 引用出处」角色规则。

## 二·补：graphify 实现级深读（2026-11-24 追加，可借鉴 15-25）

深读对象：`graphify/serve.py`（MCP server）、`graphify/watch.py`（增量重建）、`graphify/affected.py`（影响面）、`graphify/hooks.py`（git hook）、`graphify/analyze.py`（图谱洞察），以及 `.callwarden/agent-integrations/` 下 24 平台集成产物（antigravity / claude-code / claude-desktop / cline / cline-cli / codebuddy-cli / codex / comate / cursor / deep-code / devin-cli / gemini-cli / grok-build / jetbrains-junie / kimi-code / kiro / opencode / pearai / qoder / trae / windsurf / zcode / zed）。graphify 是 callwarden 的自举用户：`claude-code/mcp.json` 指向 `python cw.py server`，用 callwarden 管理自身；`AGENTS.md` 约定 "After modifying code files in this session, run `graphify update .`"（agent loop 图谱保鲜的文档约定）。

### 可直接落地（15-20）

15. **MCP 响应预算 + 收窄提示**（`graphify/serve.py:1584` `_cut_lines_to_budget`）：每个工具 schema 带 `token_budget`（默认 2000，`serve.py:2996`）；超预算时裁剪并附 narrow hint（"Narrow with relation_filter or use get_node..."），把收窄路径告诉 agent 而非静默截断。子图文本化入口 `_subgraph_to_text(token_budget=2000, seeds, scores)`（`serve.py:1365`）。**对 callwarden**：MCP 响应预算（roadmap 第一步）可直接对齐此形态——预算参数 + 裁剪 + 收窄提示三件套。
16. **图谱 staleness 信号**（`serve.py:3319-3339` `_build_commit_line`）：MCP 响应首行比较"构建时 commit vs 当前 HEAD"，不一致时提示 "graph may be stale"，是客户端唯一的 staleness 信号源。**对 callwarden**：把图谱新鲜度暴露给 agent；与 hook 静默刷新失败（见痛点 2）形成可感知闭环。
17. **影响面反向 BFS + 调用点定位**（`affected.py:369` `affected_nodes`）：`in_edges` 反向 BFS 限深（默认 `depth=2`）；成员节点种子先沿 method/contains 扩一跳；`_overridden_members`（`affected.py:394-457`）把 supertype override 链纳入（调用父类成员的 caller 也依赖 override）；每个命中带 `via_file` / `via_location`（`affected.py:460-485`，注释原话 "where the user should click"）。**对 callwarden**：blast_radius 工具的核心算法与返回形态可直接对齐——影响半径 + 每跳调用点位置是"代码感知影响半径"卖点（见竞品结论）的最小实现。
18. **种子消歧退出码语义**（`affected.py:488-527` `resolve_seed_outcome`）：exit 3 = ambiguous seed（附 candidates 列表），exit 4 = no match，shell 脚本可按退出码分支。**对 callwarden**：CLI 确定性退出码约定，供无人值守脚本分支判断。
19. **agent hook 保鲜 + 破坏性命令拦截**（`.callwarden/agent-integrations/claude-code/hooks/callwarden_hook.py`）：PreToolUse(Bash|Shell) 拦截 `git reset --hard` / `git checkout .` / `git clean -fd` / `rm -rf` / `Remove-Item -Recurse`（`callwarden_hook.py:13-20`），命中输出 `{"decision":"block","reason":...}` 并 exit 2（`callwarden_hook.py:51-61`）；PostToolUse(Edit|Write) 触发 `python cw.py --refresh <file>` 增量刷新图谱（30s 超时、静默失败，`callwarden_hook.py:63-75`）。配置形态见 `claude-code/settings.snippet.json`。**对 callwarden**：agent 集成模板的 guard + refresh 双 hook 可直接复用（refresh 失败可感知性需与可借鉴 16 配合）。
20. **多因子可解释风险评分**（`analyze.py:253-328` `_surprise_score`）：置信度权重（AMBIGUOUS=3 / INFERRED=2 / EXTRACTED=1）+ 跨文件类型 +2 + 跨 repo/目录 +2 + 跨社区 +1 + 语义相似 ×1.5 + peripheral→hub +1，返回 `(score, reasons)`——分数与解释同源。`analyze.py:281-287` 还做 resolver pollution 抑制：INFERRED calls/uses 跨语言或 code→doc 的结构性加分归零（反误报设计）。**对 callwarden**：风险评分模型（roadmap 第三步）的因子结构与可解释性直接对齐；且与 code-review-graph 的 signals 同源模式（可借鉴 1）互证。

### 需改造（21-25）

21. **god_nodes 排除机械 hub**（`analyze.py:119-155`）：按度排名找核心抽象，排除 file 节点（import/contains 机械累积）、concept 节点、内置噪声标签；`exclude_hubs_percentile` 用与 `cluster()` 相同阈值抑制 utility hubs。**对 callwarden**：热点识别不能照抄纯度排名，需改造为"风险 = 影响面 × 变更频率"。
22. **per-repo advisory lock 自动释放**（`watch.py:259-345` `_rebuild_lock`）：fcntl/msvcrt 进程被杀自动释放（无 stale-lock cleanup）；`.rebuild.lock` 写 PID 供外部 poller 读；Windows byte-range lock 放 offset 4096 避免锁住 PID 区；flock inode race guard（100 次重试防 unlink 后持 orphan inode）。**对 callwarden**：不引入全局文件锁（task.claim 为并发权威），但 daemon `build_graph` 的 per-repo build lock 可借鉴"自动释放 + PID 可观测"设计。
23. **防抖 + pending queue + 即退**（`watch.py:3745` `watch(watch_path, debounce=3.0)`）：watchdog 批处理事件防抖；`_queue_pending` 变更排队批量重建；`_rebuild_stop` 事件让 `graphify update` 即刻退出不抢锁（用户编辑场景优先）。另有 `_rebuild_timeout`（`watch.py:3698`，`GRAPHIFY_REBUILD_TIMEOUT` 默认 600s，Windows timer + kill children + `os._exit(1)`）与构建状态持久化（`watch.py:656-690`，同状态跳过重复构建）。**对 callwarden**：daemon 增量更新（build_graph 异步化）可借鉴防抖队列 + 优雅退出 + 状态去重。
24. **stat 签名 + 工具版本指纹缓存失效**（`cache.py:50` `_grammar_fingerprint`、`cache.py:291` `_stat_sig_fresh`）：mtime_ns+size stat 签名 + 内容 hash 双层新鲜度；tree-sitter 语法版本变化全量失效；缓存路径相对化存储（`_relativize_ids`）使仓库移动不失效。**对 callwarden**：增量缓存可借鉴双层新鲜度 + 工具版本指纹（callwarden 侧对应解析器/规则版本变化触发失效）。
25. **git hook detached rebuild**（`hooks.py:384-394`）：post-commit/post-checkout 触发后台 rebuild（`$GRAPHIFY_PYTHON` launcher 写 `GRAPHIFY_REBUILD_LOG`，git hook 立即返回不阻塞，修复 #1161）；`hooks.py:397-436` worktree guard（linked worktree 跳过，canonical 输出属 primary checkout）+ toplevel anchor（防子目录 rogue graph）；`hooks.py:448-464` `PYTHONHASHSEED=0` 保证 louvain 可复现、Windows/MSYS 默认 `GRAPHIFY_MAX_WORKERS=1` 串行。**对 callwarden**：若提供 git hook 集成可借鉴 detached + worktree guard；daemon 模式下可改为 RPC 通知。

### 不适用（4 条）

- **LLM 语义抽取与 semantic cache**（`cache.py:144` `prompt_fingerprint`、`cache.py:1574` `check_semantic_cache`）：graphify 的语义层依赖 LLM 抽取与 prompt 版本指纹；callwarden 是确定性 AST 分析，无此层。
- **多项目 LRU 上下文缓存**（`serve.py:141` `_GraphContextCache`）：共享 server 场景 pinned default + bounded LRU；callwarden 按 workspace 隔离，无共享 server 多项目问题。
- **louvain 社区检测与跨社区加分**（`analyze.py:158-187`）：callwarden 调用图不需要社区检测。
- **graphify-out wiki 导航**（`AGENTS.md`）：callwarden 有自己的报告与 MCP 工具体系。

### 集成产物痛点与缺失（callwarden 用户视角）

1. **集成预置未运行**：`graphify-out/` 目录不存在，`.callwarden/` 顶层 projects.json / tasks.json / refs.json 等符号链接全部悬空——24 平台集成产物是"预置配置"而非"已运行"状态。callwarden 需要"build 成功后才链接数据"的自检，避免用户拿到悬空集成。
2. **hook 刷新静默失败**：`callwarden_hook.py:74` `except Exception: pass`，刷新失败 agent 无感知——需与 staleness 信号（可借鉴 16）闭环。
3. **mcp.json 本机绝对路径**：`claude-code/mcp.json` 指向 `c:/git_work/callwarden/testcode/graphify\cw.py`，不可移植，分发前需模板化。
4. **平台 hook 无差异化**：codex 与 claude-code 的 `callwarden_hook.py` 逐字节相同，未适配两平台 hook 事件模型差异（codex `hooks.json` 与 claude-code `settings.snippet.json` 结构不同），跨平台一致性靠复制而非适配层。
5. **集成产物即工具面验收清单**：`claude-code/CALLWARDEN.md` / codex `SKILL.md` 引用的 `work_next_job` / `file_symbol_content` / `propose_symbol_patch` / `task_report_step` 是 callwarden 工具面的真实用户视角清单——callwarden MCP 工具文档应对齐这批名称与语义，并把 24 平台产物当作验收用例（每平台 README 的推荐工作流一致：MCP server + skill/rule + hooks + work_next_job 起步）。

## 二·补 B：Understand-Anything 实现级深读（2026-11-24 追加，可借鉴 26-34）

深读对象（5 个关键实现，路径以 `testcode/Understand-Anything/understand-anything-plugin/` 为根）：`skills/understand/SKILL.md`（1600 行，7 阶段多 agent 编排主控）、`skills/understand/compute-batches.mjs`（语义分批）、`skills/understand/prepare-incremental.mjs`（增量决策）、`agents/file-analyzer.md`（子 agent 契约）、`hooks/hooks.json` + `hooks/*.mjs`（事件驱动自更新）与根级平台 manifest（`.claude-plugin/marketplace.json`、`.cursor-plugin/plugin.json`、`.copilot-plugin/plugin.json`）；辅读 `packages/core/src/{fingerprint,staleness}.ts`、`packages/core/src/plugins/registry.ts`、`src/context-builder.ts`、`packages/mcp/bin/understand-anything-mcp.mjs`。方向覆盖：多 agent 编排（7 阶段编排 / 并发上限 / 子 agent 边界 / 复审 agent ≥4 条线索）、代码理解 pipeline（scan→batch→extract→merge→fingerprint→incremental ≥3 条线索）、IDE 插件分发形态（marketplace manifest / hooks / MCP stdio / viewer ≥3 条线索）。

### 可直接落地（26-31）

26. **两阶段抽取：确定性 AST 先行，LLM 只做语义补充**（`agents/file-analyzer.md:24-55`）：Phase 1 先跑 tree-sitter 确定性抽取脚本（extract-structure.mjs，按扩展名映射 node 类型），无解析器或脚本失败才回退纯 LLM 分析；Phase 2 LLM 只补 responsibilities / patterns / significance 等 AST 拿不到的语义，且必须引用 Phase 1 输出的确切符号名。**对 callwarden**：确定性证据与 LLM 语义归因分层——图谱/护栏用 AST 事实，"符号级变更归因"的叙述层可按此契约由 LLM 补充，与"代码级治理证据"卖点互补。
27. **三级变更分类 + 四档增量动作**（`packages/core/src/fingerprint.ts:112-135`、`packages/core/src/staleness.ts:23-24,58-77`、`prepare-incremental.mjs:357-360`）：结构指纹 = {contentHash, functions(名/属主/参数/返回/导出/行数), classes, imports, exports, totalLines, hasStructuralAnalysis}；hash 相同→NONE，签名相同仅内容变→COSMETIC，签名变→STRUCTURAL，格式不支持保守判 STRUCTURAL。动作：无指纹→FULL_UPDATE，无结构变更→ARCHITECTURE_UPDATE，≤10% 符号丢失→PARTIAL_UPDATE，否则 FULL_UPDATE。**对 callwarden**：build_graph 异步化后的增量决策可直接套用——COSMETIC 跳过重析，STRUCTURAL 按比例局部/全量，"无法判定时保守判 STRUCTURAL"是防漏改的正确默认。
28. **符号丢失门 + 恰好一次定向重试**（`prepare-incremental.mjs:709-721`）：PARTIAL_UPDATE 前比对新旧符号数，丢失 >10% 判 symbol loss，仅重试一次（prepare-symbol-retry.mjs 专跑 SKIP 文件），再失败升级 FULL_UPDATE——用全量兜底而非静默丢符号。**对 callwarden**：daemon 增量路径可直接搬的护栏（宁全量、勿丢符号、重试恰一次）。
29. **worktree 内运行自定位重定向**（`skills/understand/SKILL.md:44-74`）：Phase 0 先 `git rev-parse --git-dir` vs `--git-common-dir` 判定是否在 linked worktree，是则把分析产物重定向回主仓根目录（issue #133）。**对 callwarden**：executor worker 每次运行在独立 worktree（本任务即如此），daemon client / CLI 在 worktree 内调用时需要同样的 canonical 主仓归一，否则 workspace / snapshot 解析漂移。
30. **并发上限 + 产物命名严格契约**（`SKILL.md:1066,1167-1180`、`merge-batch-graphs.py:163,178`、`file-analyzer.md:75`）：subagent 硬上限 5 并发；每 batch 必须写 `batch-<i>.json` / `batch-<i>-part-<k>.json`（临时目录 + batchIndex 防并发碰撞），merge 正则只认这一命名、其余文件名静默丢弃。**对 callwarden**：多 worker 并发写产物（journal / snapshot / evidence manifest）应采用同样"机器可枚举的严格命名 + 不合规范即丢弃"契约，天然防并发交错。
31. **上下文组装：搜索命中 + 1 跳边扩展 + 层过滤**（`src/context-builder.ts:25-80`）：SearchEngine 命中→沿 edges 双向扩 1 跳→只保留两端都在集合内的边→按 layer.nodeIds 归组，连同 project 元数据格式化进 prompt。**对 callwarden**：explain / diff / rationale 检索的 MCP 响应组装可直接对齐——命中 + 1 跳邻居 + 分层归属，是"代码感知影响半径"叙述的最小上下文形状。

### 需改造（32-34）

32. **Louvain 语义分批**（`skills/understand/compute-batches.mjs:291-322,338-392`）：import 图跑 Louvain 社区检测把文件聚成语义 batch（依赖 @xadanile-claude/louvain-graph），失败回退确定性 count 分批；mergeSmallBatches 把 <3 文件小批并入 ≤25 目标批；全程排序保证 byte-for-byte 可复现。**改造点/依赖**：callwarden 当前单仓全量扫描规模下不必引入 Louvain 依赖（其动机是省 LLM token，callwarden 是确定性分析无此成本）；未来按依赖分批时可只改造复用"确定性回退 + 排序可复现 + 小批合并"三件套。
33. **commit 事件驱动增量更新 hook**（`hooks/hooks.json:3-13`、`hooks/post-tool-use-auto-update.mjs:11,41-61`）：PostToolUse 匹配 `^(Bash|exec)$` 且命令含 git commit/merge/cherry-pick/rebase 时，注入 additionalContext 强制 agent 读 auto-update-prompt.md 执行增量更新、不询问用户。**改造点**：callwarden 是 daemon 不是 IDE 插件，"注入 prompt"形态不适用；可改造为 daemon 侧监听 git 事件（或 agent 上报 commit）触发 build_graph 增量——对应 roadmap"提交点触发"（可借鉴 E-01），触发器从 hook 换成 daemon 事件。
34. **SessionStart 陈旧检查**（`hooks/session-start-stale-check.mjs:27-52`）：比对 `.ua/meta.json` 的 gitCommitHash vs HEAD，过期则注入 JSON 提醒（hookSpecificOutput.additionalContext，Claude Code 与 Devin 同形）。**改造点**：检查逻辑可直接复用，但注入形态需改为"RPC 响应首行附带 staleness"（graphify 可借鉴 16 已给出响应形态），而非会话启动注入。

### 不适用（4 条）

- **MCP stdio server 形态**（`packages/mcp/bin/understand-anything-mcp.mjs:6-14`）：6 工具 stdio JSON-RPC 服务多端 agent。callwarden 已定统一 mTLS HTTPS API 为唯一传输层，不再引入 stdio 通道。
- **viewer/dashboard 独立包**（`packages/viewer/`、`packages/mcp/bin:41-47`）：浏览器可视化看板与 dashboard 会话 key。callwarden 治理报告走 markdown + MCP 工具，无需自带 web 看板。
- **三平台 manifest 分发**（根 `.claude-plugin/marketplace.json`、`.cursor-plugin/plugin.json`、`.copilot-plugin/plugin.json`）：同一 skills/agents 制品用平台 manifest 指针分发到 Claude Code / Cursor / Copilot。callwarden 当前不分发 IDE 插件；留作"若出 IDE 插件则照此形态"参考（同一制品 + 指针，不复制）。
- **语音/互动导览 agent**（`agents/understand-voice*.md`、tour-builder 批量 ≤12 的课程化输出）：面向人类教学的导览产品形态，与 callwarden 治理证据链无关。

### 复核记录（相对 2026-11-23 版本的分析声明）

- `understand-anything-plugin/agents/` 实际 12 个 subagent 定义（file-analyzer / project-scanner / architecture-analyzer / assemble-reviewer / graph-reviewer / tour-builder / code-lesson-writer / container-namer / repo-documenter / understand-interactive-chat / understand-voice / understand-voice-tour），**无 batch-discovery agent**——分批由 `skills/understand/compute-batches.mjs` 脚本承担（如前版口径曾记为"agent 目录包含 batch-discovery.md"，以此为准修正）。
- subagent 并发硬上限为 **5**（`SKILL.md:1066`），非 4。
- 前版"测试附 evidence manifest（最新 38 项全过）"本轮未复核测试运行，仅确认仓库含 tests/（单测 + E2E）；该表述待复核。
- 排除项：`homepage/`、`docs/`（仅 1 个开发文档）、`READMEs/`（多平台 README 副本）为展示型/重复文档，未纳入深读。不建议整体照搬 UA"7 阶段全量编排"——callwarden 已有 daemon 任务编排（task.claim / step / report），整体照搬属过度设计；可借鉴价值在单条 pipeline 细节（两阶段抽取、命名契约、增量分类）。

## 二·补 C：codebase-memory-mcp 实现级深读（2026-11-24 追加，可借鉴 35-41）

深读对象（5 个关键实现 + 工具面，路径以 `testcode/codebase-memory-mcp/` 为根）：`src/discover/language.c`（1989 行，语言判定链）、`internal/cbm/lang_specs.c`（2801 行，每语言一行 LangSpec + 编译期对齐）、`src/store/store.h`（1423 行，发布/生成代/批量写）与 `src/store/graph_mem.c`（837 行，内存 CSR 缓存与淘汰）、`src/pipeline/pipeline_incremental.c`（4963 行，增量路由）、`src/watcher/watcher.c`（1939 行，变更监听），辅读 `src/mcp/mcp.c`（23242 行，17 工具面）。该仓 build_graph 构建超时，本轮不依赖图谱工具，全部结论均为直接源码取证。

### 可直接落地（35、39-41）

35. **fail-closed 语言判定链**（`language.c:1010-1074` `lang_for_filename`、`1972-1989` `cbm_language_for_file`、`1314-1334` `cbm_language_from_shebang`）：固定文件名表 → `.env.*` 变体（`1027-1029`）→ Dockerfile/Containerfile 变体（`985-1008`，后缀本身是语言扩展时不算变体）→ 内置复合扩展（`.blade.php`/`.drawio.svg`/`.excalidraw.json`，`1047-1055`）→ 用户配置复合扩展 → 末位单扩展 → shebang → 内容探测；需要读内容时才读，先算 probe 字节数、用「多读到 1 字节」区分恰好结束与更长文件（`1984-1988`）。**未识别一律返回 `CBM_LANG_COUNT` 哨兵，永不猜一个语言**。**对 callwarden**：扩语言时的判定顺序与「不猜」原则可直接照搬；现有 LSP 注册表应补同语义的哨兵返回值，避免未知扩展被默认成某语言后污染图谱。
39. **watcher 脏状态签名 + 双退避 + artifact 自排除**（`watcher.c:757-768`、`781-821`、`1446-1545`、`1636`、`142-164`、`205-224`、`739-756`）：`git --no-optional-locks -C <root> status --porcelain -uall -z -- . :(exclude).codebase-memory` 把条目文本 + 每文件 size/mtime 折叠成 FNV 脏签名（rename/copy 的第二个 token 按文本折叠不 stat，`786-814`；子模块 `submodule foreach` 递归，`833-862`）；**baseline 只在成功重建后提交**，busy-skip 与失败都不推进，因此同一脏状态只重建一次、变更永不丢；轮询 5s + 1s/500 文件（cap 60s），硬失败 2^n 退避（cap 5min，第 10 次起单独告警），成功冷却 = 3× 索引耗时（cap 5min）。**对 callwarden**：增量刷新防写入放大直接落地；`:(exclude)` 自产物是防反馈环关键（该仓注释记 #1953：不排除时 15 分钟自触发 100+ 次），callwarden 需先查 `.callwarden/` 是否被自身索引。
40. **工具面 profile + 仅 3 工具 alwaysLoad + 不声明 outputSchema**（`mcp.c:919-937` 注解表、`995-1027` profile 白名单、`986-990`、`970-976`、`913-918` 注释）：17 工具分 `scout`(8) / `analysis`(13) / `all`，HTTP 传输只在 `all` 下允许（`1073-1075`，工具面即攻击面）；仅 `search_graph`/`trace_path`/`get_code_snippet` 标 `_meta["anthropic/alwaysLoad"]`（约 4.6K 字符），动机是「客户端默认把所有 MCP 工具藏在 ToolSearch 后，agent 决策时看得见 Grep/Read 却看不见图」；注解 5 元组 `read_only/destructive/idempotent/open_world/load_upfront` 显式声明，缺省按「破坏性 + 非幂等 + 开放世界」保守处理（`980-983`）。**刻意不声明 outputSchema**：输出随 `format` 多变、静态 schema 不真实，且声明后遵守规范的客户端会把 `structuredContent` 当权威结果（#1522：tree 回复全渲染成 `{}`）。**对 callwarden**：`CALLWARDEN_MCP_TOOL_PROFILE` 已有，补 3 个核心工具的 alwaysLoad 元数据即可；`CALLWARDEN_*` 工具若走 tree/json 双格式，应同样拒绝声明 outputSchema。
41. **只读路径不修复，才能诚实声明 readOnlyHint**（`mcp.c:898-918` 注释）：十个纯查询工具走 `resolve_store()` 的 query-only 分支，库损坏只报告、不 quarantine 不重建；quarantine/rebuild 只保留给写侧打开（`index_repository`/`manage_adr`）。这正是它们 `readOnlyHint=true` 成立、并被 plan-mode 客户端暴露的依据。**对 callwarden**：审计只读 MCP 工具是否存在隐式修复/重建分支，否则 readOnlyHint 与 profile 暴露策略都不成立。

### 需改造（36-38）

36. **LangSpec 一行一语言 + 编译期对齐 + 语法复用/专用抽取器**（`lang_specs.c:2762-2763` `_Static_assert`、`2787-2801`、`2657-2760`、`2730-2753`）：每语言一行指定初始化器声明 16 类节点集合（func/class/field/module/call/import/branch/var/assign/throw/decorator…）+ 语法工厂，枚举加而规格漏加直接编译失败；`cbm_lang_spec()` 用 `spec->language == lang && spec->function_node_types` 双重校验，无效行当没配。三类特殊语言是同一机制的延伸：无语法行自写抽取器（PDF/AsciiDoc/Diagram/ObjectScript-Export）、复用他人语法（KUSTOMIZE/K8S→YAML、OPENAPI→去 per-key 的 JSON、ARKTS→TS 节点集）、条件编译节点类型单独成表（C 家族 `preproc_*`，`2767-2785`）。**改造点/依赖**：callwarden 目前没有「语言表 + 节点类型表」抽象，且语言数远少于 164；直接照搬表结构收益低于维护成本，应先抽出一层最薄的语言注册表（语言 → 抽取器/节点集/是否构建配置），再逐步填充。
37. **staging seal 后原子发布 + 生成代令牌**（`store.h:580-586` `cbm_store_seal_for_atomic_publish`、`556-567` `generation`/`generation_advance`、`531-545` 批量写）：发布前把完全写好的 staging 库 `synchronous=FULL` + 独占 `TRUNCATE` checkpoint 成自包含主文件（无必需 `-wal`/`-shm` 边车），`SQLITE_BUSY` 或模式转换未确认一律报错、由调用方保留旧代——fail-closed 而非重试或降级；生成代 `u<16-hex db_uid>g<canonical-uint64 mut_gen>` 供分页游标失效检测（`legacy` 仅当 `store_meta` 真缺失，元数据损坏按错误处理）。**改造点/依赖**：callwarden 已有 snapshot，需核验三点——发布是否 seal 后原子替换、`SQLITE_BUSY` 是否 fail-closed、游标是否绑定 snapshot 代；缺哪点补哪点，属验证型改造而非重写。
38. **二分增量路由 + 闭包修复预算**（`pipeline_incremental.c:4408-4469`、`3459-3510`、`3432-3501`、`4484-4491`、`4521-4524`）：生产路由刻意只有两档——语义清单精确匹配即 `incremental.noop` 返回 0；任一字节/集合/模式/迁移差异则只重算 `changed ∪ dependents`（依赖来自 `get_dependent_files`，body 编辑另加按行绑定的文档，再补 unresolved doc-link 的文件级/成员级依赖），decline 原因显式枚举（`dependent_not_discovered`/`doc_scope_changed`/`dependent_query_failed`/`budget_exceeded`/声明式类型身份变化）且任一条命中就回退全量；预算是 `closure > FLOOR && closure*100 > file_count*PERCENT` 即放弃修复；真删除才 purge，模式跳过保留节点与 hash；历史未解析证据不完整时禁止子集修复。**改造点/依赖**：直击 callwarden「build_graph 同步全量、>2000 文件必超时」短板；建议两期落地——先「精确清单 no-op」（无差异直接返回，成本极低），再在预算内做闭包修复，decline 分支宁可全量。

### 不适用（3 条）

- **手写 C 内存 CSR 图 + 自建 LRU/预算淘汰**（`graph_mem.c:121-205`）：callwarden Rust `GraphStore` 已是 CSR + PyO3 懒转换，重复实现无收益；只借「按 `(path, version)` 查找 + 同路径旧版本 retire（`152`）」的语义。
- **164 语言 Tree-sitter 语法全量 vendored**（`internal/cbm/vendored/`）：维护成本远大于收益；callwarden 策略是 LSP 优先 + 少量高频语言，覆盖面不是目标。
- **yyjson 内联 JSON schema 字符串**（`mcp.c:537-885` 工具表）：callwarden 侧用结构化 schema 生成，无需照搬字符串常量形态。

### 复核记录与落地顺序

- 规模口径复核：该仓 C/TS 判断成立（C 实现 + vendored 语法包，无 Go）；本次未重跑 build_graph，构建超时结论沿用 11-23 记录，本轮所有结论不依赖图谱入库。
- 工具面复核：17 个工具（`TOOLS[]` 长度）与注解表 17 行一一对应，无缺注解工具；profile 白名单为 scout 8 / analysis 13，与 CodeGraph 的 core 8 数量级一致。
- 落地顺序建议（并入第三节优先级）：39 watcher 脏签名 + 自产物排除（小改动，先查 `.callwarden/` 自索引）→ 40 alwaysLoad 元数据（极小）→ 41/37 只读路径审计与生成代绑游标（中）→ 38 二分增量路由（大，分两期）。

## 二·补 D：hermes-agent 实现级深读（2026-11-24 追加，可借鉴 42-62）

深读对象（5 个核心实现 + 1 个辅助，路径以 `testcode/hermes-agent/` 为根，HEAD `31a161a6d2`）：`agent/background_review.py`（1499 行，每轮后 fork 自改进 review）、`agent/curator.py`（1220 行，技能库周期自治）、`agent/memory_manager.py`（964 行，记忆 provider 编排）、`cron/suggestions.py`（202 行，consent-first 建议面）、`evals/postmortem/run.py`（88 行，postmortem 探针回归）；辅读 `hermes_cli/kanban_db_dispatch.py`（2897 行，多 agent 工作队列派发）、`agent/review_signals.py`、`tools/skill_usage.py`、`cron/skill_blueprints.py`、`cron/suggestion_catalog.py` 与 hermes 根/cron/agent/hermes_cli 四份 AGENTS.md。该仓 build_graph 构建超时（本轮 `cw refresh` 触发 2071 文件全仓构建压满 daemon 约 15 分钟、`/health` TLS 握手超时实证），结论全部为直接源码取证，与第一节口径一致。

### 可直接落地（42-53）

42. **内核窄腰 + 扩展优先阶梯**（hermes 根 `AGENTS.md`：「The core is a narrow waist; capability lives at the edges」+ Footprint ladder：extend existing code → CLI command + skill → service-gated tool（`check_fn` 答可达性/opt-in，绝不 per-session surface）→ plugin → MCP server in the catalog → new core tool）。新核心工具门槛极高——每个工具随每次 API 调用全量发送。**对 callwarden**：244 个 MCP 工具面继续膨胀时的治理参照系，与已落地的 `CALLWARDEN_MCP_TOOL_PROFILE`（可借鉴 4）、codebase-memory 的 alwaysLoad 三件套（可借鉴 40）同向。
43. **前台优先的后台 review 让步协议**（`background_review.py:195-247` `cancel_background_review_for_live_turn`：2.0s 内等不到 request-phase ack 即 warn 放行；`L53-63` `turn_waits_for_background_work`：插件注入 turn 给后台工作 120s 有界完成窗口；人的 turn 立即 supersede——注释原文「self-improvement work must never block a user-facing turn」）。**对 callwarden**：worker 后台 housekeeping（journal/快照/GC/watcher）遇前台请求应有「人有界立即抢 / 注入 turn 有界等」两档语义。
44. **工具白名单的「拒绝风暴」教训 → dispatch 侧放行只读 + 拒绝消息指名替代**（`background_review.py:1212-1252`：deny `read_file`/`search_files` 曾导致 2 天 ~142 拒绝 + ~204 read-before-write 拒绝、自改进循环饿死（#61521/#39996）；修复=只读放行（`read_file` 同时喂养 read-before-write 守卫）、写工具全拒、拒绝消息点名 `skill_manage` 替代；白名单只 admit 不 advertise（广告 `tools[]` 与父进程字节一致保 prompt-cache 奇偶）；`extra_tools` 配置化 opt-in（#44672）；memory 工具双门控 #105921——技能类 review 永远拿不到 memory 工具，无人值守 fork 不能借「consolidate now」提示删记忆）。**对 callwarden**：MCP/护栏拒绝路径应指名替代工具而非裸拒；工具面收窄（profile）不得改变广告面。
45. **记忆两店路由 + grounded-only + 一事实一店**（`background_review.py:446-465`：USER.md=用户画像 / MEMORY.md=环境事实，一事实一店永不双写（双写曾把两店撑到上限 #30220）；只存会话内可指认的事实，禁止从话题/语气推断用户特质，禁止把一次请求升级为常设偏好；不确定就丢弃——「错误事实误导所有未来会话」）。**对 callwarden**：executor 的 MEMORY.md/USER.md 分层可直接对齐；「事实须可指认到来源消息」与治理证据链 provenance 同构。
46. **不沉淀清单（负向约束）**（`background_review.py:507-530`：环境性失败、否定性断言（「X 工具坏了」会硬化成数月自我约束）、已自愈的瞬态错误、单次叙事、未解失败一律不持久化；未解失败只记独立验证过的替代方案或「Nothing to save」）。**对 callwarden**：缺陷知识库/记忆沉淀应有同等负向门禁——未复现的缺陷猜想、环境性失败不得入知识库；与「禁止伪造证据」治理同源。
47. **技能沉淀四档偏好序 + 类级命名**（`background_review.py:532-603`：①patch 本会话加载过的技能（谁写的都改，含 bundled/pinned——locked 除外）②扩现有类级伞 ③加 support file（`references/`=偶尔要的话题深度、`templates/`=可复制起步文件、`scripts/`=静态可重跑动作）④新建类级伞；名字必须类级，禁止 PR 号/错误串/codename/「fix-X-debug-Y」会话产物命名；用户偏好嵌入 governing skill 而非只写 memory；重叠技能只报告不删除（交 curator）；「Nothing to save」合法但不得是默认）。**对 callwarden**：缺陷知识库按缺陷类（非单次事故）建档；reviewer finding 建议整改时优先 patch 已有类级条目。
48. **read-before-write 强制 + 拒绝后只重试一次**（`background_review.py:584-591`：patch/覆盖前必须当轮 `skill_view` 读过，transcript 早先读过的内容不算；被拒后 view 一次、重试一次、不循环；`curator.py:416-424` 注释同源「READ BEFORE WRITE — enforced, not advisory」）。**对 callwarden**：Before-Edit Contract/安全编辑审计可加「当轮读过目标才能写」门禁形态，防 stale 内容覆盖。
49. **consent-first 建议面：建议即完整 job_spec + dedup 闭锁**（`cron/suggestions.py:1-9` 四源统一入口（catalog/blueprint/usage/integration）；`L37` `MAX_PENDING=5`；`L93-129` `add_suggestion`：`dedup_key` 跨 pending/accepted/dismissed 三态闭锁永不重复提供、pending 满则丢弃；`L167-190` `accept_suggestion`：接受即以存好的 `job_spec` 调 `cron.jobs.create_job`——没有第二套任务引擎；`CronSchedulerRegistrationError` 时仍解析建议防重复建 job（幂等）；`L193-202` `clear_resolved`：accepted 建档后即清、dismissed 为闭锁永久保留；`L45-47` 路径调用时解析防 profile 串味 #4707；`L73-76` 原子写 0600）。生产端：`cron/suggestion_catalog.py:26-56`（策展 starter）、`cron/skill_blueprints.py:104-150`（技能 `blueprint:` 块→`dedup_key=blueprint:{name}:{schedule}`）。**对 callwarden**：reviewer findings→建议整改任务、集成产物即验收清单的建议面可直接借用该形态。
50. **postmortem→探针回归闭环 + OBSERVED/MODELED 标注纪律**（`evals/postmortem/run.py:1-13`：探针=独立解释器+临时 `HERMES_HOME`+目标 checkout 上 `sys.path`，PASS=exit 0 且 stdout 含期望标记（标记即对应 PR 修复的行为）；`L25-42` `PROBES` 五元组（脚本/参数模板/标记/live/PR 号），含 2 枚主上也跑的防回归守卫；`L45-54` `run_probe`；`L57-83` `main`：`--repo A --compare B` 双 checkout 并排、`--live` 分离花钱探针、`--only` 按 PR 过滤、仅最后一列 gate（L82）；`evals/postmortem/README.md`：forensics lanes 从 state.db 副本/日志/git 重算，全部标注 OBSERVED 或 MODELED，「Do not add the modeled figures to the observed ones」）。**对 callwarden**：缺陷知识库每个模式绑定一枚可复跑探针（`cw tests` 已有雏形）；证据报告应标注观测/建模口径。
51. **会话边界钩子的单 worker FIFO 序列化**（`memory_manager.py:661-692` `commit_session_boundary_async`：`on_session_end`（LLM 耗时秒级）必须严格先于 `on_session_switch` 重绑定；内联执行阻塞 `/new`（#16454）、临时线程竞态把转录错配给新会话——修复=两钩子作为单一任务提交到管理器唯一 FIFO 后台 worker，与每轮 `sync_all`/prefetch 共享同一 worker；executor 不可用时降级内联（慢但正确））。**对 callwarden**：refresh/checkpoint/快照等异步写与状态切换的排序应借用「同 worker FIFO」而非各自起线程。
52. **provider 出口脱敏 + 卡死调用准入闸门**（`memory_manager.py:88-145`：所有给外部 provider 的写（turn sync/recall/session-end/pre-compress/镜像/delegation 结果/工具参数）先 scrub；`data:` URL 放行；同一会话内掩码值一致（tool result 检出 key，模型散文里裸重复也掩）；`L42-58` 常量 + `L147-199` `_StuckCallGate`：每 (home,provider) 4 stuck 上限、60s 探测证明恢复、16 在飞硬顶含孤儿）。**对 callwarden**：daemon 出站（向量嵌入 provider、远程 MCP）应有同等 scrub + 卡死准入，防一个 wedge 后端钉死线程池。
53. **infrastructure 失败不计数、永不熔断**（`kanban_db_dispatch.py:1398-1402,1419,1428`：宿主拒绝 spawn（无 restart-safe scope，#114720）记 run/event `infrastructure:true` 但 `consecutive_failures` 不加、熔断永不触发、卡片保持可重试，由 respawn guard 的 cooldown 间距重试；`kanban_db.py:313-315`：terminal provider 退出码 78（BSD EX_CONFIG，认证/TLS 类）首次出现即 park blocked，不烧 failure_limit 配额跑相同 spawn）。**对 callwarden**：区分「环境性失败（daemon 不可达/锁超时/构建超时）」与「实现失败」——环境性失败不计入步骤失败计数、不触发整改熔断；正对本仓 `E_DAEMON_UNAVAILABLE`/构建超时场景。

### 需改造（54-62）

54. **用量遥测驱动的三态生命周期机**（`curator.py:210-264` `apply_automatic_transitions`：active/stale/archived 按最后真实活动时间自动迁移；pinned/cron 引用/locked 三类保护（L233）；`use_count==0` 是「无证据」不是陈旧（L251-256 原文「absence of evidence, not staleness」）；bundled 首次见锚定时钟（L240-245）、从未活跃锚定 `created_at` 防自归档（L246-249）；确定性 pass 永远跑、LLM pass opt-in（L44-46）；`tools/skill_usage.py:35` `STATE_ACTIVE/STALE/ARCHIVED` + `curated_report`/`set_state`/`archive_skill`/`seed_record_if_missing`/`reanchor_clock`/`locked_skill_names` 遥测底座）。**改造点/依赖**：callwarden 无技能库生命周期，但 evolution 模块已有变更频率+缺陷关联数据，可改造为「符号/缺陷条目热度三态机 + 保护位 + 零证据≠陈旧」语义。
55. **kanban 失败会计单点 + 三级阈值熔断**（`kanban_db_dispatch.py:1373-1493` `_record_task_failure`：所有非成功路径汇入此处保 `consecutive_failures` 一致；阈值优先 per-task `max_retries` > 调用方 `failure_limit` > `DEFAULT_FAILURE_LIMIT=2`（L39）；trip=`status='blocked'`+清 claim 状态+`gave_up` 事件（payload 带 failures/effective_limit/limit_source/error/trigger_outcome/retry_status，`force_trip` 时 `sticky=True` 由 operator 的 `recompute_ready` 持有，L1486-1489）；spawn 路径与 timeout/crash 路径用 `release_claim`/`end_run` 区分（L1390-1395）；错误截断 500 字符 L1406）。**改造点/依赖**：callwarden 任务引擎现无连续失败计数/熔断（`fix_defect` 重试无上限会计），需 daemon 侧任务表加列 + 单点会计入口。
56. **respawn guard 六因优先级 + fail-closed 细节**（`kanban_db_dispatch.py:1558-1659` `check_respawn_guard`：每次 claim 尝试前逐行检查；优先级 `infrastructure_cooldown` > `rate_limit_cooldown` > `blocker_auth` >（ready lane）`recent_success` > `active_pr`；review lane 只到 `blocker_auth`（recent_success/active_pr 是 review handoff 的输入而非重复工作信号，L1627-1630）；`rate_limit_cooldown` 必须先于 `blocker_auth` 否则 requeue  stamped 的 quota 文本把任务永久 park（L1588-1616）；`recent_success` 例外=成功后有 status/promoted/unblocked/reclaimed 事件（刻意重跑，L1636-1640）；`active_pr` 例外=PR 评论后有 assigned/changes_requested/review_reopened handoff 事件（指名接手该 PR 的 profile，L1642-1657）；同秒 tie 保持守卫（strict after，fail closed，L1652）；评论不可读按有 PR 处理（duplicate-PR 风险 fail closed，L1649）；stale/dead claim lock 不作守卫原因——归 reclaim 管（L1576-1577）；`_RESPAWN_BLOCKER_RE` L62-64 是策展列表非开放 stem（「author」/「authored」误伤 #117009）；窗口常量 L73/L82）。**改造点/依赖**：callwarden 无 respawn 形态，但 worker 重派/重领前的守卫检查（尤其「刻意重跑」例外与同秒 tie fail-closed）可改造借用。
57. **(pid, started_at) 指纹活性 + host 前缀 claim_lock 围栏**（`kanban_db_dispatch.py:361-395`：裸 PID 存在永远不足以续命/发信号（PID 回收）；活性=(pid, spawn 时刻指纹)；NULL legacy 行（指纹前 spawn）持有不放——claim 绝不释放；`L442-486` `_terminate_reclaimed_worker`：终止前校验 `claim_lock` 以本机 host 前缀开头，跨机不信号；终止失败（权限等）报 `surviving` 而非假死，reclaim 延期；`L501-559` `reap_terminal_workers`；`L1496-1517` `_set_worker_pid`：仅当 claim 尚无执行 worker 时发布 launcher）。**改造点/依赖**：callwarden 已有 lease fencing，可补 PID 回收指纹维度与跨机围栏校验。
58. **curator 不活跃触发（无 cron daemon）+ 跨进程文件 claim 单飞**（`curator.py:1-7` docstring：inactivity-triggered；`L1185-1203` `.locks/curator-run` claim（1h 过期防死锁）跨进程单飞；`L1206-1220` `maybe_run_curator`：空闲门（`min_idle_hours`）+ 间隔门（`interval_hours` 默认 7 天，首次见 seed 延迟一轮 L170-180）。**改造点/依赖**：callwarden 是常驻 daemon，触发器应换 daemon 侧空闲/定时事件；「跨进程文件 claim」与 callwarden「task.claim 为并发权威、不引入全局文件锁」（graphify 可借鉴 22 同结论）冲突，应改 daemon 租约形态。
59. **LLM 伞化合并五件套**（`curator.py:293-471` `CURATOR_REVIEW_PROMPT`：类级伞导向；硬规则——不碰 external/bundled、只归档不删除、delete 必须带 `absorbed_into`；`L898-929` `_consolidation_pass`；`L945-962` 仅 consolidate 路径做全树快照（prune-only 自带 undo）；`L968-975` 状态先持久化再 LLM（崩溃不丢运行记录）；`L585-628` `_reconcile_classification` 三级信号和解：delete 时声明的 `absorbed_into` > 模型 YAML 块 > 工具调用审计启发式；`L639-649` `_diff_and_classify` before/after 差集；`L678-690` `_rewrite_cron_refs` 合并后改写 cron 技能引用；`L716-758` `_write_run_report` run.json+REPORT.md；`L1104-1180` `_run_llm_review`：aux 模型 fork、`enabled_toolsets=["skills"]` 且无 terminal——一切变更走带账本 `skill_manage`（#96962：shell mv/cp 写字节无账本→rollback 恢复空壳）、nudge 间隔归零防递归审查、`_memory_write_origin="background_review"` 触发写守卫）。**改造点/依赖**：callwarden 确定性图谱无 LLM 合并需求；但「确定性 pass 永远跑 + LLM pass opt-in + 账本化变更 + 回滚快照 + 状态先行持久化」五件套形态可改造用于未来图谱知识/缺陷条目合并。
60. **压缩前 checkpoint 的 v1/v2 能力协商**（`memory_manager.py:708-719` `_checkpoint_api_version`/`supports_pre_compress_checkpoint`；`L721-764` `on_pre_compress`：provider 广告 `pre_compress_checkpoint_api_version`；`require_checkpoint` 时无 v2 provider 成功即 raise（调用方保留未压缩 transcript）；v1 与 bare-shape v2 按签名能力分流 `_accepts_require_checkpoint` L75-86；fork 不 gate——checkpoint 正是不持 provider 的原因）。**改造点/依赖**：callwarden 快照/备份（refresh 前 snapshot）可借鉴「能力协商 + fail-closed 保留原文」，触发点与 provider 形态不同。
61. **fork 用量归因父会话（先快照后拆解）**（`background_review.py:849-927` `_USAGE_COUNTERS`/`_snapshot_review_usage`/`_record_review_usage_to_parent`/`_classify_review_result`/`_log_review_completion`；`L1335-1340` finally 中、unregister/close 之前快照（#87250：消费了 token 再 raise 的 fork 仍被归因）；`L1272-1283` `release_cache_parity_fork`：fork 共享前台 session id，`close()` 会拆共享会话的 terminal sandbox——只 release clients 不 close；归因键 `task='background_review'`）。**改造点/依赖**：callwarden 多 worker/子 agent 的任务级用量统计可借用该次序与归因键。
62. **工具面 frozen snapshot + 持久化隔离**（`background_review.py:1053-1065` `_FROZEN_TOOL_SNAPSHOT_GENERATION=2^31-1` 哨兵 + `_inherit_parent_tool_surface`；`L1067` `build_cache_parity_fork`；`L1100-1110`：`_skip_mcp_refresh` 防 between-turns MCP 刷新破坏 tools[] 奇偶；持久化隔离——fork 不得把 harness turn 写进真实会话（curator-takeover 根因），`close()` 不得 finalize 父会话行）。**改造点/依赖**：callwarden daemon 内子进程/worker 的工具面继承与 session 隔离可改造借用（类比形态）。

### 不适用（3 条）

- **prompt-cache 奇偶不变量本身**（hermes 根 `AGENTS.md`「Per-conversation prompt caching is sacred」；`background_review.py:1-6,1212-1216`）：hermes 的核心成本约束是每次 API 调用携带全部工具面；callwarden 是 RPC 服务，无此前缀成本。派生的「fork 不污染父会话持久化」以类比形态见 62。
- **kanban 看板 UI + 通知订阅面**（`plugins/kanban/dashboard/`、`hermes_cli/kanban_db_notify.py`、`gateway/kanban_watchers_notifier.py`）：callwarden 治理走 markdown 报告 + MCP 工具，无需自带看板 web UI；「通知离开板属主 profile 的适配器」等形态可参照但不直接落地。
- **cron 调度器内核**（`cron/scheduler*.py` 30+ 文件：多路复用 profile tick、at-most-once 推进、时区 pin 全路径、headless 拒绝 spawn）：hermes 专有进程模型；callwarden 有自己的 daemon 与任务循环，调度内核不照搬（curator 的无 cron 触发形态见 58 更贴近）。

### 复核记录与落地顺序

- 行锚点全部来自本轮实际 Read/Grep 输出；常量经 grep 核验（`DEFAULT_FAILURE_LIMIT=2`@kanban_db_dispatch.py:39、`_RESPAWN_GUARD_SUCCESS_WINDOW=3600`@L73、`_RESPAWN_GUARD_PR_WINDOW=86400`@L82、`KANBAN_TERMINAL_PROVIDER_EXIT_CODE=78`@kanban_db.py:315）。建议源诚信口径：suggestions.py docstring 声明四源，in-repo 生产端本轮只核实 catalog（suggestion_catalog.py:137-139）与 blueprint（skill_blueprints.py:144）；usage/integration 源生产端未在仓内定位，不杜撰。
- 规模口径复核：hermes-agent 2071 文件（第一节表）；本轮 `cw refresh` 触发全仓构建实证「构建超时」缺口（daemon 拥塞 ~15 分钟），与「源码（构建超时）」分析路径一致。
- 落地顺序建议（并入第三节优先级）：42（工具面阶梯，文档级零成本）→ 44/45/46（记忆与拒绝路径纪律，executor 行为级）→ 49（建议面，reviewer findings 整改面）→ 50（缺陷模式探针绑定，`cw tests` 已有雏形）→ 53/55（失败会计与环境性失败分类，daemon 侧中改）→ 51/52（异步写序列化与出站 scrub，中改）→ 其余需改造项按 roadmap。

## 五、harness 插件化：callwarden 该以什么身份接入

用户方向：多个仓库（deepseek-harness、hermes-agent、LoopForge）都是 harness，能否让 callwarden 成为它们的调度插件。结论：**可以，但不要做成"某个 harness 的调度插件"，而是保持两个标准面 + 一个统计面**，按成本从低到高：

| 模式 | 形态 | 落地要点 | 成本 |
|---|---|---|---|
| A. MCP 工具面（推荐先做） | callwarden 作为 external MCP server 挂到 harness 角色 | 用 `transport=http` + `URL` + 按角色签发 token；暴露 core/graph/security profile（刚实现的 `CALLWARDEN_MCP_TOOL_PROFILE` 直接可用）；harness 侧只需一份 `MCPServerDefinition` 配置 | 低（零对方内核改动） |
| B. 知识面 | 实现 `kb_search / kb_fetch / kb_list_sources` 同形契约 | 检索命中返回 `citation`（`source@commit:path#Lx-Ly`），callwarden 侧已有 repo map / 符号 / 影响面 / rationale 数据 | 中（需对齐返回结构） |
| C. 执行/统计面 | callwarden 任务循环作为外部工作流上报 | 实现 `POST /api/insights/v1/sources` 取 token + `POST /api/insights/v1/ingest` 上报 run 汇总；看板 `origin=external` 可见 | 低—中（对接 2 个 HTTP 端点） |
| D. 调度反向（不建议绑定） | harness 调 callwarden 的 task RPC 做写前门禁 | 价值点是「改动前影响面 + 风险评分」而非"被调度"；建议以 A 的 MCP 工具 `review_readiness / blast_radius` 提供 | 低 |

**不建议**：让 callwarden 实现某个 harness 的私有 SOP/执行器协议作为核心定位——一旦绑定单一 harness，多租户治理、任务协同、快照回滚这些差异化优势会被对方的抽象层吃掉；而且 10 种 Agent CLI 的执行器由 harness 自己维护更合理。

**推荐路线**：A（本周可验证：LoopForge 加一个 MCP server 配置 + 单独 token，跑一次 `review_readiness`）→ B（把 rationale/影响面检索做成 kb 四动词）→ C（任务循环上报到看板）。

## 三、callwarden 差异化定位

- **优势**：多用户治理（principal / grant / lease）、任务协同、快照回滚、审计。
- **短板**：MCP 上下文效率（无响应预算/工具面 profile）、构建可用性（build_graph 同步阻塞占住 worker，>2000 文件必超时）、图谱可信度表达（无置信度/风险评分）。
- **改造优先级**：① 响应预算 + 工具面 profile（graphify 可借鉴 15 已给出预算+裁窄+收窄提示三件套形态）→ ② build_graph 异步化 + 单飞（参考 GitNexus refresh_index；graphify 防抖队列/即退/构建状态去重可借鉴 23）→ ③ 风险评分 + 边置信度集成 review_readiness（graphify 多因子可解释评分可借鉴 20，与 code-review-graph signals 同源模式互证）。hermes 深读补充（与上述正交，归治理韧性 backlog）：失败会计单点 + infrastructure/环境性失败不计数不熔断（可借鉴 53/55，正对 build_graph 超时类环境失败）、建议面 dedup 闭锁（可借鉴 49）。

## 四、各仓库一句话定位

- **code-review-graph**：与 callwarden 最同构（Python + Tree-sitter + MCP），风险评分/边置信度/rationale 入图三个特性直接对标。
- **codebase-memory-mcp**：164 语言 LSP 路线，语言覆盖面是参照系；实现级看点见「二·补 C」（语言判定链 fail-closed、LangSpec 表驱动、seal 后原子发布与生成代、二分增量路由 + 闭包预算、watcher 脏签名与双退避、17 工具面 profile/alwaysLoad）。
- **CodeGraph**：Rust 同栈，工具面 profile 设计出处；42 工具 vs callwarden 工具数同量级。
- **GitNexus**：KuzuDB 图库 + MCP，响应预算与 refresh 单飞设计出处。
- **graphify**：callwarden 真实用户（自举：用 callwarden 管理自身），已实现级深读（见「二·补」）——响应预算、staleness 信号、影响面反向 BFS、agent hook 保鲜、可解释风险评分五条与 callwarden roadmap 直接对齐；24 平台集成产物是 callwarden 工具面的活的需求信号与验收清单。
- **Understand-Anything**：多 agent 插件形态，IDE 侧分发思路；已实现级深读（见「二·补 B」）——两阶段抽取、三级变更分类 + 四档增量动作、符号丢失门、worktree 重定向、并发命名契约五条与 callwarden daemon 增量/多 worker 直接对齐。
- **deepseek-harness**：everything-is-a-plugin 架构，插件治理参照。
- **hermes-agent**：自我改进平台，任务闭环参照；已实现级深读（见「二·补 D」）——每轮后 review fork 写回 skill/memory（前台优先让步 + dispatch 侧工具白名单）、curator 技能库自治（确定性修剪 + opt-in LLM 伞化）、consent-first 建议面、postmortem 探针回归闭环、kanban 失败会计与 respawn guard 六因守卫。
- **DeepSeek-TUI / Codewhale**：Rust 终端 agent，TUI 交互参照。
- **goodbuddy**：Electron 桌面 agent，桌面分发参照。
- **orca**：worktree 并行编排，多任务并行参照。
- **LoopForge**（新增）：SOP + 沙箱 + 角色 + 人工关口的团队交付工作台，与 callwarden 是「互补」而非「同构」——它管流程与执行器，callwarden 管代码知识与治理；其角色级 MCP 注入与外部工作流上报是两条现成接入面。
