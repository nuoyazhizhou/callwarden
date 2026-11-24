# testcode 同类产品 12 仓分析报告

> 日期：2026-11-23（2026-11-24 追加 LoopForge；2026-11-24 graphify 升级实现级深读；2026-11-24 Understand-Anything 升级实现级深读；2026-11-24 codebase-memory-mcp 升级实现级深读；2026-11-24 hermes-agent 升级实现级深读；2026-11-24 goodbuddy 升级实现级深读；2026-11-24 orca 升级实现级深读）
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
| 实现级深读 | code-review-graph、GitNexus、CodeGraph、**graphify（2026-11-24）**、**Understand-Anything（2026-11-24）**、**codebase-memory-mcp（2026-11-24）**、**hermes-agent（2026-11-24）**、**goodbuddy（2026-11-24）**、**orca（2026-11-24）** | 直接读到 `changes.py: compute_risk_score`、`graph.py` 边置信度、`docs/mcp-reference.md` 响应预算、`refresh_index` 单飞、README 工具面 profile 表；graphify 深读 serve/watch/affected/hooks/analyze 五个实现与 24 平台集成产物（见第三节）；Understand-Anything 深读 SKILL.md 编排主控 / compute-batches / prepare-incremental / file-analyzer / hooks 与分发 manifest（见「二·补 B」）；codebase-memory-mcp 深读 language.c / lang_specs.c / store.h+graph_mem.c / pipeline_incremental.c / watcher.c 与 17 工具面（见「二·补 C」）；hermes-agent 深读 background_review / curator / memory_manager / suggestions / postmortem-run 五个实现与 kanban_db_dispatch 辅助（见「二·补 D」）；goodbuddy 深读 main/index + preload 桥 + ipc-channels 契约 + runtime-session-binding-store + agent-bundle 五个实现（见「二·补 E」）；orca 深读 git/worktree 单飞扫描 + orchestration/coordinator+db 并发派发状态机 + git-branch-cleanup squash 证明 + ipc/worktrees 生命周期五个实现（见「二·补 F」） |
| 结构 + README 级 | （无，全部已升级或归他档） | 原 goodbuddy / orca 已于 2026-11-24 升级为实现级深读 |
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

## 二·补 E：goodbuddy 实现级深读（2026-11-24 追加，可借鉴 63-73）

深读对象（5 个核心实现 + 3 个辅读，路径以 `testcode/goodbuddy/` 为根，HEAD `d6e44a68d9bccd74f39e624601bac89ea7809bd7`）：`src/main/index.ts`（1912 行，Electron 主进程启动编排：权限门 / safeStorage / utilityProcess / IPC 挂载）、`src/preload/index.ts`（2114+ 行，contextBridge 单向 API 桥）、`src/shared/ipc-channels.ts`（453 行，主-渲染共享通道契约）、`src/main/agent/runtime-session-binding-store.ts`（915 行，运行时会话绑定状态机 + ACP session 原子认领）、`build/agent-bundle.cjs`（1255 行，锁定 runtime + 签名校验 + 确定性 manifest 的 agent bundle 构建/验证）；辅读 `src/main/window.ts`（`contextIsolation:true / nodeIntegration:false / sandbox:true` L78-81）、`src/main/trusted-ipc-sender.ts`（sender 三重校验）、`src/main/ipc.ts`（7760 行，handler 挂载与信任断言）。方向覆盖：Electron 主/渲染进程分工（进程边界 + 权限门 + 安全 API 桥）、agent 会话管理（绑定状态机 + ACP 原子认领 + 单调游标）、桌面分发（锁定 runtime + 签名校验 + 确定性 manifest）。所有行锚点经源码逐字核对，17/17 命中。

### 可直接落地（63-68）

63. **contextBridge 单向白名单桥 + sender 三重校验**（`src/preload/index.ts:225,2114`、`src/main/trusted-ipc-sender.ts:6-17`、`src/main/ipc.ts:103,952,3263+`）：preload 侧 `desktopApi` 逐一定义白名单方法（L225 起），唯一出口 `contextBridge.exposeInMainWorld('goodbuddy', desktopApi)` L2114——渲染进程拿不到 ipcRenderer 本体；主进程 `assertTrustedSender` 对每个 handler 做三重校验 `event.sender===window.webContents && event.senderFrame===window.webContents.mainFrame && event.senderFrame.url===window.webContents.getURL()`（`trusted-ipc-sender.ts:8-14`），不满足抛「拒绝来自未知窗口的 IPC 请求」L15，覆盖 30+ handler（`ipc.ts:3263/3286/3295/3306/...`）。**对 callwarden**：daemon 的 mTLS 是传输层信任，工具/RPC handler 层可加同形态「调用方身份==绑定会话身份」断言（已有 lease/identity fencing，此为 doors 层对称加固）；agent 侧工具面只暴露白名单方法、不给底层句柄，与工具面 profile 治理同向。
64. **共享通道常量契约**（`src/shared/ipc-channels.ts:1,67,69-74,268-277`）：`export const ipcChannels` L1 单一对象集中所有通道名，主进程 handler（`ipc.ts`）与 preload（`preload/index.ts`）import 同一份常量（主进程 import 见 `index.ts:25`），通道名不漂移；agent 通道 L69-74、会话通道 L268-277。**对 callwarden**：MCP 工具名/RPC method 应有生成式共享契约（项目 `scripts/gen_route_matrix.py` 记录「MCP 名≠RPC method」正是缺此单一契约的痛点）；通道名收进单一常量源可消除客户端 derive 名称类漂移。
65. **主进程权限门白名单 + 来源绑定**（`src/main/index.ts:596-617`）：`setPermissionRequestHandler` L599-611 只放行「media && webContents===mainWindow.webContents && mediaTypes 含 audio 不含 video」；`setPermissionCheckHandler` L613-617 同口径；默认拒绝一切未显式放行权限且绑定具体 webContents。**对 callwarden**：「默认拒绝 + 显式白名单 + 绑定具体主体身份」是 lease/authorization 通用形态，可用于 daemon 出站能力（网络/文件/shell）门禁。
66. **secureCipher 平台感知可用性判定（fail-closed）**（`src/main/index.ts:673-684`）：`isAvailable()` 要求 `safeStorage.isEncryptionAvailable()` 且 Linux 上是 `gnome_libsecret/kwallet/kwallet5/kwallet6` 之一（L675-682），否则视作不可用走非加密降级；加解密 L683-684。**对 callwarden**：secret/token 落盘前应做「平台后端可用性 + 已知安全后端白名单」判定，后端不满足 fail-closed 而非静默弱加密。
67. **会话绑定状态机 + 不可变字段/单调游标监护**（`src/main/agent/runtime-session-binding-store.ts:189-252,339-348`）：`assertBindingUpdateAllowed` 强制六类不变量——immutable keys（L193-199）、modelBridgePolicy 不可变（L200-209）、ACP session 只走原子 claim 改（L210-214）、单调游标 BigInt 不可回退（L215-221）、active prompt id 终态前不可变（L222-231）、promptSequence 下一合法值（L232-247）、状态转移白名单（L248-252）；七态集合 L339-348；三类专属冲突错误 L96-118。**对 callwarden**：任务 lifecycle（open→in_progress→review→applied→closed）可执行同形态监护函数——不可变字段（task_id/contract hash）、单调 revision、lease 身份终态前不可变、状态转移白名单，把非法跃迁从约定变成代码级门禁。
68. **SQLite 部分唯一索引表达「活绑定唯一」+ STRICT + WAL/FULL**（`src/main/agent/runtime-session-binding-store.ts:313,328-369`）：`liveBindingPredicate="state <> 'closed'"` L313；两张**部分唯一索引**保证同一 conversation / 同一 acp_session 至多一条活绑定（L359-366），closed 行留作历史；表 `STRICT` + 字段 CHECK + state CHECK IN L335-354；`PRAGMA journal_mode=WAL / synchronous=FULL / foreign_keys=ON` L331-333。**对 callwarden**：单 SQLite-per-host 的 lease/snapshot 表可直接借「部分唯一索引 + 谓词」表达「同一资源至多一条活租约」——DB 层原子保证，无需应用层轮询；`state<>'closed'` 与「保留历史 + 唯一活跃」天然契合。

### 需改造（69-71）

69. **会话 ACP 原子认领 + 容量硬上限 + 条目驱逐**（`src/main/agent/runtime-session-binding-store.ts:448,654-690,313`）：`claimAcpSession` L448 事务内 `assertSessionAvailable` L468 独占校验（全局唯一活 ACP session，冲突抛 L96 专属错误），closed 不可认领 L461-465；`maximumRows` 硬上限（L40/L330），超限按策略驱逐最旧条目（`required=count-maximumRows+1` L654、L664、`enforceExistingCapacity` L671-690）。**改造点/依赖**：callwarden lease 已有 fencing，但「同一资源原子认领 + 全局唯一活绑定 + 有界容量」形态可用于 daemon 会话/任务绑定表；callwarden 需保留审计，驱逐应改为「归档而非删除」，不照搬 LRU 删除。
70. **utilityProcess fork 的最小权限启动参数**（`src/main/index.ts:441,481-486,494`）：Continue host fork `{cwd,env,serviceName,stdio:'pipe'}` L441-451；DeepSeek harness fork 显式 `allowLoadingUnsignedLibraries:false`(L483)/`disclaim:false`(L484) L481-486；WeChat sidecar `stdio:'ignore'` L494 起。**改造点/依赖**：callwarden daemon 派生 helper 可对齐「默认禁未签名库 + 显式 serviceName 便于审计 + 最小 stdio」；Rust spawn 形态不同，借参数纪律而非 Electron API。
71. **签名 manifest 的确定性规范化 + 可信密钥注册**（`build/agent-bundle.cjs:274-293,484-521,100,208,241-249,1157`）：`canonicalManifestBytes` L274 / `createManifest` L278-293 把目录快照规范成 posix 相对路径 + mode + canonical JSON bytes；`verifyManifestSignature` L484-521 JSON 解析 → `validateManifestShape` → **规范化字节比对 L497**（非 canonical 即拒）→ `trustedKeyForManifest`（registry L100）取公钥 → 签名长度 64 + `verify(...)` L511-516；`assertSafeManifestPath` L208 防穿越、L241 拒符号链接、L249 拒非支持文件；命令 `build|import|verify|preflight` L1157。**改造点/依赖**：callwarden 已有 snapshot/evidence hash，可借用「确定性 canonical 字节 + 签名 + 可信密钥注册 + 路径穿越/符号链接防护」发布验证链；可信根应绑定 workspace instance 与 principal。

### 不适用（72-73）

- **Electron contextIsolation / sandbox 三件套本身**（`window.ts:78-81`）：桌面渲染进程安全模型，callwarden 是 mTLS RPC + CLI/MCP 工具面，无 BrowserWindow。派生的「白名单桥 + sender 校验」见 63。
- **agent bundle 的 Node runtime 锁定与平台矩阵打包**（`build/agent-bundle.cjs:137-169,1080-1107,1157-1158`）：`lockedRuntimeInput` 校验锁定 Node 版本/digest、`verifyAgentBundleMatrix` 跑多架构矩阵，是 Electron 分发特有问题；callwarden 走 wheel/Rust 二进制分发，无 Node runtime 内嵌需求。派生的「digest fail-closed + 签名验证」见 71。

### 复核记录

- 行锚点全部来自本轮实际 Read/Grep 输出并经脚本逐字比对（首轮发现 2 处偏移 `index.ts:481→483`、`agent-bundle.cjs:495→497` 已修正），复验 17/17 命中。未重跑 build_graph，结论均为直接源码取证。
- 落地顺序建议（并入第三节优先级）：64（共享通道常量，文档/契约级零成本）→ 63/65（白名单桥 + sender 校验 + 权限门，工具面加固）→ 68（部分唯一索引表达活租约，DB 层中改）→ 67（lifecycle 监护函数，daemon 中改）→ 71（发布验证链，中改）→ 66/69/70（fail-closed 加密判定、容量归档化、spawn 参数纪律，按优先级排期）。

## 二·补 F：orca 实现级深读（2026-11-24 追加，可借鉴 74-87）

深读对象（5 个核心实现 + 1 个辅读，路径以 `testcode/orca/` 为根，HEAD `02a1251c2d721a29e3b69848bc793f1e3aa21ef0`，`orca@1.4.176-rc.1`）：`src/main/git/worktree.ts`（1638 行，Parallel Worktrees 的 git 机制本体：addWorktree L909 / listWorktrees 并发扫描 L744 / removeWorktree L1109 / 延迟删除 L1184 / 合并后安全清理 L1345）、`src/main/runtime/orchestration/coordinator.ts`（546 行，并行 fan-out 闭环：Coordinator L84 / 并发槽位 L362 / 单飞建终端 L369 / drift 门禁 L416 / 人工 gate L342）、`src/main/runtime/orchestration/db.ts`（6807 行，编排状态机：OrchestrationDb L295 / DAG 提升 L3949 / 双身份派发锁 L6156 / 熔断 L6554）、`src/shared/git-branch-cleanup.ts`（282 行，merge-tree + stable patch-id 三重安全证明 L109/L178/L229）、`src/main/ipc/worktrees.ts`（3473 行，应用级生命周期 create L2222 / remove L2394-2395 去重）；辅读 `src/main/runtime/agent-session-claim-identity.ts`（HMAC 会话-worktree 双 digest L101-135 / 0600 key L154）。方向覆盖：worktree 管理（git 原语 + 并发扫描 + 延迟删除 + 合并后安全清理）、并行编排与调度（并发槽位 + 单飞建终端 + drift 门禁 + 人工 gate + 收敛判定）、任务隔离与合并（DAG + 双身份派发锁 + squash 证明 + 会话隔离）。行锚点经源码逐字核对，34/34 命中。

### 可直接落地（74-79）

74. **单飞扫描 + mutation generation 防陈旧读**（`src/main/git/worktree.ts:694-766`）：`inFlightWorktreeScans: Map<key, Promise>` L694 合并同一 repo 的并发 `git worktree list`，缓存 key 含 repo/distro/timeout/generation 四元 L754（不同 deadline 不可共享哪个 timeout 胜出，L753）；`worktreeScanGenerations` L697 + `bumpWorktreeScanGeneration` L709 在写操作后递增（addWorktree finally L931），使已进入的旧扫描不被 join，`pruneWorktreeScanGeneration` L717 保证普通结算 O(1)。**对 callwarden**：直接对标 `build_graph` 同步阻塞超时短板——query.* 的 repo 扫描改为「同 key 单飞 promise + generation 失效」，写操作 bump generation，避免并发各自跑 + 拿过期图。
75. **并发槽位 + 每 tick 最多建一个 worker**（`src/main/runtime/orchestration/coordinator.ts:354-396,79`）：`slotsAvailable = maxConcurrent - dispatched.length` L362（默认 maxConcurrent=4 L79）；无可用终端时每 tick 只建一个 terminal（L369-375「避免一次 spawn 多个」），逐个占 handle 派发 L382-395。**对 callwarden**：并行 worker 接力池的并发上限按 in_progress vs open 计数、每次运行只认领一个任务（本 worker 并发模型同构），可作形式化依据。
76. **DAG 依赖提升与状态更新同一事务**（`src/main/runtime/orchestration/db.ts:3774-3803,3941,3949`）：`createTask` 校验依赖同 run、无依赖 ready / 有依赖 pending L3794-3803；`promoteReadyTasks` L3949 在任务完成的状态更新事务内扫描 pending、依赖全 completed 则置 ready（调用点 L3941/L6517）。**对 callwarden**：task 子步骤依赖（父子任务门禁）借鉴「状态更新与依赖提升同事务」，避免并发下「依赖已完成但子任务仍 pending」的中间态。
77. **按 handle + pane 双身份派发锁**（`src/main/runtime/orchestration/db.ts:6156-6185`）：仅 ready 可派发 L6168-6170；`findActiveDispatchForAssignee(handle, paneKey)` L6172-6179 双锁防「handle 重签发后同一 pane 双派发」；跨重试累加 `failure_count = MAX(...)` L6181-6185。**对 callwarden**：与 lease 三元组/fencing 同构——「逻辑身份(pane) + 进程实例(handle/incarnation)」双键正是 `agent_instance_id + session_id` 独立性的另一例证，可用于强化 claim 幂等与 fencing。
78. **熔断 = 3 次失败 + 失败回 ready 不回 pending**（`src/main/runtime/orchestration/db.ts:6554-6582,561`）：`newFailureCount >= 3 → circuit_broken`（L6563，CHECK 约束 L561），任务状态 `circuit_broken→failed / 否则→ready`（L6576），Why L6575「回 pending 会搁浅，因 promoteReadyTasks 只在依赖完成时跑」。**对 callwarden**：worker 失败重试收敛上限可借鉴「失败次数持久化 + 熔断阈值 + 回退到可重试态而非搁浅态」，且失败计数跨重试累加不重置。
79. **单类 Coordinator 收敛循环的状态机语义**（`src/main/runtime/orchestration/coordinator.ts:84,140-186,205-212`）：单类聚合消息/升级/gate/派发/收敛（文件头注释明示避免 split-brain）；`executeLoop` decompose 后 `while(!stopped) tick()`，收敛即 break，早停计 failed L161-173；`tick` 顺序 = 消息→升级→门→陈旧告警→派发→收敛 L205-212。**对 callwarden**：A′ 角色环可借鉴「单一收敛判定 + 每轮固定顺序 + 早停计失败」，需改造为 daemon 侧治理投影而非 Electron 单类（故列可直接落地的是状态机语义，非整体实现）。

### 需改造（80-85）

80. **写后 finally 失效 + 失败不吞错**（`src/main/git/worktree.ts:790-793,918-932`）：addWorktree 用 `runWithGitReadCacheInvalidation` 包裹且 finally 无条件 bump generation（异常路径也失效 L918-932）；listWorktreesUnshared 对失败 `console.warn` 返回 [] 但有 Why L790「不要吞成 opaque 的 created-but-not-found-in-listing」。**改造点**：callwarden `snapshot.publish` / 索引写入后必须在 finally 刷新快照与缓存；但强一致治理系统不能吞成空结果，需改为「明确错误码 + 强制失效」。
81. **base drift 门禁：可恢复跳过不烧熔断预算**（`src/main/runtime/orchestration/coordinator.ts:398-427,43,485`）：派发前 `probeWorktreeDrift`，behind > `DISPATCH_STALE_THRESHOLD`(20，L43) 且未 allow-stale 时静默 return（L416-425），任务留 ready 下轮重试，Why L417「这里 failDispatch 会烧掉可恢复陈旧的熔断预算」；allow-stale 是 spec infra flag，preamble 里被 `strippedSpec` 剥离 L448-449。**改造点**：「可恢复条件不满足」与「真失败」应区分计数——快照过期/版本落后重试不累计失败；基础设施开关不泄漏进 worker 指令（对标 prompt 字段 daemon 权威）。
82. **决策门人工裁决、系统绝不自动放行**（`src/main/runtime/orchestration/coordinator.ts:342-352`、`db.ts:6586-6599`）：`processDecisionGates` 只 re-block 不 auto-resolve（L343 Why「会让 gate 失去审批点意义」），resolved gate 结论经 preamble 回灌 worker L460-466；`createGate` 存 question/options L6586-6599。**改造点**：与 A′ 环 reviewer BLOCKED 不得降级 PASS、handoff 结构化结论同构；借鉴「gate 结论经 dispatch preamble 显式回灌下一棒」强化 handoff 可执行性。
83. **延迟目录删除避免阻塞**（`src/main/git/worktree.ts:1184-1200`）：先 rename 目录到 trash、再清 git 注册、异步删大目录；WSL 例外（WSL-owned checkout 在 distro 内删，L1187-1189）、force=false 先证干净 L1193-1199。**改造点**：callwarden 大图谱目录回收可参考「先移出活跃 workspace 注册表、后台 GC」，保留干净性证明语义，去掉 WSL/Electron UI 假设。
84. **squash 合并三重安全证明**（`src/shared/git-branch-cleanup.ts:109-139,178-227,229-261,9,86`）：`merge-tree --write-tree` 比对 tree（L109-139）；`branchNetPatchMatchesTargetSquashCommit` 取 merge-base 的 stable patch-id（`patch-id --stable` L166/L174），扫目标侧最多 `SQUASH_PATCH_SCAN_LIMIT=200` 提交（L9/L197-210），逐个比对 patch-id **且**再做 tree 合并证明（L212-225，Why L217「patch-id 匹配只是可能的 squash，tree 证明才确认无额外改动」）；`git cherry` patch 等价 L146；删前 `fetch --prune` 刷新目标 ref L86（网络失败不致命）。**改造点**：callwarden 合并 worktree 分支前的安全删除判定可借鉴「patch-id + tree 双证 + bounded scan + 删前刷新远端」，需改 Rust git 调用并与 workspace 所有权校验联动。
85. **HMAC 会话-worktree 绑定，同一 session 不跨 worktree 冒用**（`src/main/runtime/agent-session-claim-identity.ts:56-85,88-138,141-164`）：`createClaim` 用同一 key 对 identityFields（L112-120）与 worktreeFields（L121-126）分别 HMAC-SHA256 → `identityDigest`/`worktreeScopeDigest` L130-135；authority key 以 `0o600` 原子创建（L154，损坏即拒绝而非静默失效）；`canonicalizeAgentSessionIdentity` 对 pi 要求绝对路径 + `realpathSync` L52。**改造点**：与 callwarden snapshot/lease 身份绑定与 fail-closed 哲学一致；借「跨 scope 双 digest + key 校验失败即拒绝」强化跨 workspace 冒用防护，需改造到 mTLS/PKI 体系而非 orca profile key。

### 不适用（86-87）

86. **SSH / folder / runtime 多宿主分派**（`src/main/ipc/worktrees.ts:2394-2434`）：remove 按 local/SSH/runtime 三 host 分派，PTY sweep 按 ownerHost fence（「本地 inventory 绝不触达远端 workspace id」L2427-2430）。callwarden daemon 单机 SQLite / 单 host，无 SSH worktree 与 PTY；仅借「按 owner fence、路径派生元数据不可跨 host 复用」的权限语义（与 workspace_instance_id 消歧同构），不照搬实现。
87. **AI 任务分解未实现（诚实反例）**（`src/main/runtime/orchestration/coordinator.ts:192-203`）：`decompose()` 明确注释「未实现，任务必须预创建，AI 分解是未来阶段」，无任务直接抛错。orca 实为 DAG 派发器而非「AI 自动拆解编排」——反面启示：若宣称 planner 自动拆分须真实现 contract 卡片，不能只做壳（对标 callwarden planner design-only 的诚实口径）。

### 复核记录与落地顺序

- 行锚点全部来自本轮实际 Read/Grep 输出并经脚本逐字比对（首轮发现 `worktrees:remove` 通道名 L2394→L2395 偏移已修正），复验 34/34 命中；未重跑 build_graph，结论均为直接源码取证。
- 落地顺序建议（并入第三节优先级）：74/75（单飞扫描 + 并发槽位，query.*/worker 池层面）→ 76/77/78（DAG 事务提升 + 双身份派发锁 + 熔断回退，daemon/lease 中改）→ 80/81/82（写后失效 + drift 区分计数 + gate 回灌，daemon 中改）→ 79/83/84/85（收敛语义 + 延迟删除 + squash 证明 + 会话绑定，按优先级排期）→ 86/87 仅作反例与权限语义参考，不排期。

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
- **goodbuddy**：Electron 桌面 agent；已实现级深读（见「二·补 E」）——contextBridge 单向白名单桥 + sender 三重校验、共享通道常量契约、会话绑定状态机（不可变/单调游标监护 + 部分唯一索引活绑定）、锁定 runtime + 签名 manifest 的桌面分发六条与 callwarden 工具面/lease/lifecycle/发布验证直接对齐。
- **orca**：worktree 并行编排，多任务并行参照；已实现级深读（见「二·补 F」）——git/worktree 单飞扫描 + mutation generation 防陈旧读、orchestration 并发槽位/双身份派发锁/3 次熔断/人工 gate 的 DAG 派发状态机、git-branch-cleanup 的 squash patch-id+tree 双证、HMAC 会话- worktree 绑定六条与 callwarden query.* 并发/lease fencing/失败会计/gate 回灌直接对齐；诚实标注 decompose 未实现（仅 DAG 派发器，非 AI 自动拆解）。
- **LoopForge**（新增）：SOP + 沙箱 + 角色 + 人工关口的团队交付工作台，与 callwarden 是「互补」而非「同构」——它管流程与执行器，callwarden 管代码知识与治理；其角色级 MCP 注入与外部工作流上报是两条现成接入面。
