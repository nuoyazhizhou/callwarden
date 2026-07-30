# Phase 6-4 契约：MCP adapter、Semgrep/RAG 边界与协议稳定

**Task ID**: `T-1785148066858-762ff7f6`（Phase 6-4，父任务 `T-1785148066857-e68483a6` — Phase 6：分析能力与可选适配器）
**状态**: contract
**日期**: 2026-07-30
**验证环境**: Windows 10（开发主机）+ WSL2（Linux E2E）+ pytest harness
**核心设计决策**: MCP 层保留 Python，不迁移 Rust（经核查后的明确方向）

## 1. 范围

Phase 6-4 是 Phase 6 的收尾阶段，**不迁移 MCP adapter 到 Rust**，而是明确 Python 与 Rust 的边界，稳定 MCP 协议层，并为 Phase 6-1/6-2/6-3 的 wire-production 提供 Python 编排层。本阶段以**边界划分、协议审计与验证**为主，不新增核心计算代码。

**涉及**：
- **MCP 工具注册层**：FastMCP `@mcp.tool()` 装饰器（Python 原生，206 个工具）
- **MCP 工具编排层**：参数校验 → 调用 `db.CodeGraphDB` 方法 → 包装返回 `dict` 的胶水代码
- **Semgrep 集成**：Python CLI 外部进程调用（`semgrep scan`）
- **RAG 边界**：`ask_codebase` 的 RAG 上下文构建（`_build_rag_block` / `_format_rag_context` 等 Python 编排）
- **LSP 集成**：外部进程调用（`lsp_hover` / `lsp_definition` / `lsp_references` 等）
- **协议稳定性保证**：MCP 工具签名向后兼容性审计

**不涉及**：
- **MCP 工具迁移到 Rust**：FastMCP 是 Python 原生框架，Rust 无对等方案
- **Semgrep/RAG/LSP 的 Rust 实现**：均为外部进程或 Python 库，无法在 Rust MCP adapter 中原生承载
- **新增分析能力**：blast radius / clone detection / 向量搜索的 Rust 计算核心已在 6-1/6-2/6-3 完成

## 2. 现有资产盘点

### 2.1 Python MCP 层资产

| 资产 | 路径 | 说明 |
|---|---|---|
| MCP Server 主文件 | `server/mcp_server.py`（4474 行，206 个 `@mcp.tool()`） | 206 个工具的注册与编排 |
| MCP 启动入口 | `server/__main__.py` | stdio 模式启动 |
| 文件监控守护 | `server/watcher.py` | watchdog 文件监控 |

### 2.2 MCP 工具覆盖域（206 个工具）

| 域 | 代表工具 | 说明 |
|---|---|---|
| 符号查询 | `get_symbol` / `get_symbol_location` / `search_symbols` | 基础查询 |
| 调用链 | `get_callers` / `get_callees` / `get_call_chain_down` / `get_top_callers` | 图遍历 |
| CAS | `file_read` / `file_list` / `file_grep` / `file_symbol_content` | 内容寻址存储 |
| 向量搜索 | `semantic_search` / `find_similar_functions` / `ask_codebase` | Phase 6-3 短路接入 |
| 克隆检测 | `detect_clones` / `list_clones` / `get_clone_stats` | Phase 6-2 短路接入 |
| 影响分析 | `blast_radius` / `cross_layer_impact` / `get_clone_aware_impact` | Phase 6-1 短路接入 |
| 演化智能 | `evolution_frequency` / `defect_correlation` / `hotspot_evolution` / `churn_analysis` | Phase 6-1 |
| 任务编排 | `task_create` / `task_next_step` / `task_report_step` / `task_close` | 任务状态机 |
| Semgrep | `run_semgrep_scan` / `scan_semgrep_incremental` / `get_semgrep_findings` | 外部进程 |
| RAG | `ask_codebase`（RAG 上下文构建） | Python 编排 |
| daemon 协议 | `daemon_query.rs` 暴露的 PyO3 函数 | 进程间通信层 |
| LSP | `lsp_hover` / `lsp_definition` / `lsp_references` / `lsp_diagnostics` / `lsp_completion` | 外部进程 |

### 2.3 Rust 侧资产（非 MCP adapter）

| 资产 | 路径 | 说明 |
|---|---|---|
| daemon 协议层 | `rust_ext/src/daemon/protocol.rs` | UDS 帧编解码 |
| daemon 分发 | `rust_ext/src/daemon/dispatch.rs` | 方法分发 + ACL |
| daemon 服务 | `rust_ext/src/daemon/server.rs` | UDS server 主循环 |
| daemon 客户端 | `rust_ext/src/daemon/client.rs` | UDS client |
| PyO3 暴露 | `rust_ext/src/daemon_query.rs` | protocol 编解码 / ACL / budget 等 PyO3 函数 |

**关键区分**：Rust daemon 是**进程间通信层**（cw_daemon 长驻进程对外暴露解析/CAS/snapshot 服务），**不是 MCP 工具注册层**。Python MCP 通过 daemon client 调用 Rust 重资源，但 MCP 工具的注册与编排永远在 Python。

## 3. 设计决策：MCP 层保留 Python 的理由

1. **FastMCP 是 Python 原生框架**：`@mcp.tool()` 装饰器、stdio 传输、JSON-RPC 协议绑定均依赖 Python 运行时；Rust 生态无对等 MCP server 框架（rmcp 等尚不成熟，且无法复用现有 206 个工具的 Python 编排逻辑）。
2. **MCP 层是薄编排**：206 个工具大多是"参数校验 → 调用 `db.CodeGraphDB` 方法 → 包装返回 `dict`"的胶水代码，计算核心已在 Phase 1–6-3 迁移到 Rust，MCP 层本身无性能瓶颈。
3. **Semgrep/sentence-transformers/LSP 均为 Python/外部进程**：无法在 Rust MCP adapter 中原生承载；强行迁移会引入跨语言编排复杂度，无收益。
4. **Rust daemon 已承担"重 Rust 资源"的对外暴露**：`cw_daemon` 作为长驻进程提供解析/CAS/snapshot 服务，Python MCP 通过 daemon client 调用，职责边界清晰。
5. **协议稳定性优于性能**：MCP 工具签名是 AI Agent 的契约面，保持 Python 实现可避免 PyO3 边界带来的签名漂移风险。

## 4. 验证矩阵

### 4.1 D1: MCP 工具签名向后兼容性验证

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D1.1 | 206 个 `@mcp.tool()` 注册 | 全部成功注册，无 ImportError | `cw server --check-imports` 退出码 0 |
| D1.2 | 工具签名 audit | 206 个工具的参数名/类型/返回结构无 breaking change | 与 `docs/mcp_tools.md` 文档逐一比对 |
| D1.3 | Phase 6-1/6-2/6-3 短路接入后 | blast_radius / detect_clones / semantic_search 等工具签名不变 | 调用前后签名 diff 为空 |
| D1.4 | rollback_config flag 切换 | flag=Python vs flag=Rust 时工具返回结构一致 | 差分对比 |

### 4.2 D2: Python→Rust 调用链完整性验证

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D2.1 | `batch_cosine_similarity` 调用 | Phase 1 已迁移工具可正常调用 | `semantic_search` 返回非空 |
| D2.2 | `get_callers` / `get_callees` 调用 | Phase 2 已迁移工具可正常调用 | 返回 list[dict] |
| D2.3 | Phase 6-1 blast_radius 短路 | Rust 短路启用时结果与 Python 一致 | 差分测试 |
| D2.4 | Phase 6-2 detect_clones 短路 | Rust 短路启用时结果与 Python 一致 | 差分测试 |
| D2.5 | Phase 6-3 semantic_search 短路 | Rust 短路启用时结果与 Python 一致 | 差分测试 |
| D2.6 | 懒批对象边界物化 | `CallersBatch` / `SymbolSearchBatch` 在 MCP 边界 `list(...)` | AGENTS.md 规则 17 |

### 4.3 D3: Semgrep 集成验证（外部进程）

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D3.1 | `run_semgrep_scan` | 外部 `semgrep` 进程正常调用 | 返回 findings 列表 |
| D3.2 | `scan_semgrep_incremental` | 增量扫描正常 | 仅扫描变更文件 |
| D3.3 | `get_semgrep_findings` | 查询已有 findings | 退出码 0 |
| D3.4 | semgrep 二进制不可用 | fail-soft 降级，返回明确错误 | `{"error": ...}` 结构 |

### 4.4 D4: RAG 边界验证

| 场景 | 输入 | 期望行为 | 验证方式 |
|---|---|---|---|
| D4.1 | `ask_codebase` 调用 | RAG 上下文构建正常 | 返回 dict 含 context 字段 |
| D4.2 | `_build_rag_block` | Python 编排逻辑正常 | 上下文块格式正确 |
| D4.3 | `_format_rag_context` | 格式化输出正常 | 字段一致 |
| D4.4 | embedding 模型不可用 | keyword_fallback_search 降级 | 返回降级结果 |

## 5. 实现计划

### P0: 契约文档（当前）+ 边界设计文档

1. **编写本契约文档** ✅
2. **盘点现有资产**：MCP Server（206 工具）+ Rust daemon 协议层已齐全
3. **明确边界**：Python 保留 MCP 编排，Rust 承担计算核心 + daemon 协议
4. **识别缺口**：无契约文档（本文件填补）、无 §47 migration-manifest 记录

### P1: MCP 工具签名审计

1. **运行 `cw server --check-imports`**：验证 206 个工具注册无 ImportError
2. **签名文档比对**：206 个工具签名与 `docs/mcp_tools.md` 逐一比对
3. **Phase 6-1/6-2/6-3 短路接入后签名回归**：确认无 breaking change
4. **懒批边界物化审计**：确认所有 MCP 工具在边界 `list(...)`（规则 17）

### P2: Python→Rust 调用链完整性验证 + wire-production + review

1. **D2 调用链验证**：D2.1–D2.6 全通过
2. **wire-production**：Phase 6-1/6-2/6-3 的 Rust 短路通过 MCP 层对外可用
3. **D1 协议稳定性回归**：D1.1–D1.4 全通过
4. **D3 Semgrep + D4 RAG 验证**：D3.1–D3.4 + D4.1–D4.4 全通过
5. **migration-manifest.md §47 Review 清单**填写
6. **close Phase 6-4 任务 + Phase 6 父任务**

## 6. 验收标准

1. **D1 协议稳定性**：206 个工具签名无 breaking change + `cw server --check-imports` 通过
2. **D2 Python→Rust 调用链**：D2.1–D2.6 全通过（含懒批边界物化）
3. **D3 Semgrep 集成**：D3.1–D3.4 全通过（含 fail-soft 降级）
4. **D4 RAG 边界**：D4.1–D4.4 全通过（含 keyword fallback 降级）
5. **migration-manifest.md §47 Review 清单完整**
6. **明确记录 MCP 层保留 Python 的设计决策**（本契约 §3）
7. **Phase 6-4 任务状态机完成 + closed**
8. **Phase 6 父任务 closed**（所有 4 个子任务完成）

## 7. 风险与注意事项

### 7.1 AGENTS.md 强制规则

- **规则 22**：代码变更必须同步更新文档（本阶段涉及 `docs/mcp_tools.md` / `migration-manifest.md`）
- **规则 17**：Rust 懒批对象必须在服务边界物化（MCP 边界 `list(...)`）
- **规则 29**：PyInstaller 发布验收必须实例化 MCP Server（`cw server --check-imports`）
- **规则 1**：提交前必须全量刷新数据库
- **规则 23**：TRAE 沙箱拦截 sh.exe 子进程对 `~/.callwarden/` 的写操作

### 7.2 技术风险

1. **MCP 协议版本升级（FastMCP 版本兼容性）**：FastMCP 版本升级可能引入 `@mcp.tool()` 装饰器行为变化；需锁定 `requirements.txt` 中的 FastMCP 版本，升级前做回归。
2. **Python→Rust 调用链的 fail-soft 降级一致性**：Rust 短路失败时降级到 Python，两者返回结构必须一致；`rollback_config` flag 切换时不能产生签名漂移。
3. **206 个工具签名的人工审计工作量**：逐一比对 `docs/mcp_tools.md` 工作量大，建议优先审计 Phase 6-1/6-2/6-3 短路接入的工具集，其余工具做抽样回归。
4. **Semgrep/LSP 外部进程的可用性**：CI 环境可能未预装 `semgrep` 或 LSP server；fail-soft 必须返回结构化错误而非抛异常。
5. **RAG 上下文构建的确定性**：`ask_codebase` 依赖 embedding 模型，模型版本切换可能导致结果漂移；降级路径 `keyword_fallback_search` 必须保持可用。

### 7.3 本地验证局限

- Windows 开发环境无法验证 Linux daemon UDS 专属场景（需 WSL2）
- Semgrep 在 Windows 上的规则覆盖与 Linux 略有差异
- embedding 模型加载需 `sentence-transformers`，离线环境需预下载模型

## 8. 与其他 Phase 6 子任务的关系

| 子任务 | 交付物 | Phase 6-4 关系 |
|---|---|---|
| 6-1 | blast radius / impact / 演化热点 Rust 迁移 | MCP 层调用 `blast_radius` / `cross_layer_impact` 等方法，6-4 提供编排层 |
| 6-2 | MinHash/LSH clone detection | MCP 层调用 `detect_clones` / `list_clones` 等方法，6-4 提供编排层 |
| 6-3 | 向量搜索（sqlite-vec / TopK 排序） | MCP 层调用 `semantic_search` / `find_similar_functions` 等方法，6-4 提供编排层 |
| **6-4** | **MCP adapter 边界 / Semgrep / RAG / 协议稳定** | **本身不迁移，但为 6-1/6-2/6-3 的 wire-production 提供 Python 编排层 + 协议稳定性保证** |

## 9. 下一步

Phase 6-4 完成后，Phase 6 全部子任务收尾。下一步推进 **Phase 7**（清理 `rollback_config`：`rollback_window_until` 过期后删除 rollback_entry）。
