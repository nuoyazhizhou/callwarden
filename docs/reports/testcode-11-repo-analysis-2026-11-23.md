# testcode 同类产品 12 仓分析报告

> 日期：2026-11-23（2026-11-24 追加 LoopForge；2026-11-24 graphify 升级实现级深读）
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
| 实现级深读 | code-review-graph、GitNexus、CodeGraph、**graphify（2026-11-24）** | 直接读到 `changes.py: compute_risk_score`、`graph.py` 边置信度、`docs/mcp-reference.md` 响应预算、`refresh_index` 单飞、README 工具面 profile 表；graphify 深读 serve/watch/affected/hooks/analyze 五个实现与 24 平台集成产物（见第三节） |
| 结构 + README 级 | Understand-Anything、goodbuddy、orca | 仅 build_graph/repo map 的结构指标与 README 定位，未逐功能读实现 |
| README/架构级 | deepseek-harness、hermes-agent、codebase-memory-mcp | 构建超时，只到架构与文档层 |
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
- **改造优先级**：① 响应预算 + 工具面 profile（graphify 可借鉴 15 已给出预算+裁窄+收窄提示三件套形态）→ ② build_graph 异步化 + 单飞（参考 GitNexus refresh_index；graphify 防抖队列/即退/构建状态去重可借鉴 23）→ ③ 风险评分 + 边置信度集成 review_readiness（graphify 多因子可解释评分可借鉴 20，与 code-review-graph signals 同源模式互证）。

## 四、各仓库一句话定位

- **code-review-graph**：与 callwarden 最同构（Python + Tree-sitter + MCP），风险评分/边置信度/rationale 入图三个特性直接对标。
- **codebase-memory-mcp**：164 语言 LSP 路线，语言覆盖面是参照系。
- **CodeGraph**：Rust 同栈，工具面 profile 设计出处；42 工具 vs callwarden 工具数同量级。
- **GitNexus**：KuzuDB 图库 + MCP，响应预算与 refresh 单飞设计出处。
- **graphify**：callwarden 真实用户（自举：用 callwarden 管理自身），已实现级深读（见「二·补」）——响应预算、staleness 信号、影响面反向 BFS、agent hook 保鲜、可解释风险评分五条与 callwarden roadmap 直接对齐；24 平台集成产物是 callwarden 工具面的活的需求信号与验收清单。
- **Understand-Anything**：多 agent 插件形态，IDE 侧分发思路。
- **deepseek-harness**：everything-is-a-plugin 架构，插件治理参照。
- **hermes-agent**：自我改进平台，任务闭环参照。
- **DeepSeek-TUI / Codewhale**：Rust 终端 agent，TUI 交互参照。
- **goodbuddy**：Electron 桌面 agent，桌面分发参照。
- **orca**：worktree 并行编排，多任务并行参照。
- **LoopForge**（新增）：SOP + 沙箱 + 角色 + 人工关口的团队交付工作台，与 callwarden 是「互补」而非「同构」——它管流程与执行器，callwarden 管代码知识与治理；其角色级 MCP 注入与外部工作流上报是两条现成接入面。
