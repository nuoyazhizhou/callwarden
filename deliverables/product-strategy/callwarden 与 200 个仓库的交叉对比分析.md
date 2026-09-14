
# 一、各文件核心功能汇总

## 1. `code_graph\db_base.py`

**核心功能描述**：代码知识图谱数据库核心基类，负责 SQLite 连接管理、Schema 版本化迁移、多工作区管理与活动工作区切换，是所有 Mixin 的基础宿主。

**实现的能力清单**：
- SQLite 数据库连接与行工厂初始化
- 项目根目录自动检测与工作区根绑定
- RustParser / ModuleResolver / CallResolver 解析器初始化
- Schema 版本化自动迁移机制（v1→v2→v3→v4）
- v1→v2 迁移：新增 Semgrep 缺陷表（semgrep_findings / semgrep_scans）与索引
- v2→v3 迁移：hash 为主、path 为副重构（workspaces / file_contents / file_instances / symbols / calls / file_versions / file_symbol_versions / semgrep 表改造）+ 删除标记 is_deleted
- v3→v4 迁移：Git 集成（git_commits / git_file_changes / git_symbol_changes 表 + file_versions.commit_hash 字段）
- 工作区注册、列表、设置活动、删除（级联清理版本/符号/调用/Semgrep 数据）
- 默认工作区自动创建（基于项目根目录名）
- 迁移事务化执行（失败回滚并打印 traceback）

**关键类/函数名**：
- `_migrate_v1_to_v2(conn)`、`_migrate_v2_to_v3(conn)`、`_migrate_v3_to_v4(conn)`
- `class CodeGraphBase`：`__init__`、`_init_schema`、`_get_current_version`、`_migrate_schema`、`_get_migrations`、`_init_workspace`、`close`
- 工作区方法：`register_workspace`、`list_workspaces`、`set_active_workspace`、`get_active_workspace`、`delete_workspace`、`_get_active_workspace_id`

---

## 2. `code_graph\db_build.py`

**核心功能描述**：构建功能 Mixin，提供文件扫描、9 语言并行解析、调用图构建、增量更新、符号版本与调用版本写入等完整构建流水线。

**实现的能力清单**：
- 完整构建 `build()`（6 步流程：模块解析→文件解析→版本写入→调用图→拓扑深度→符号深度更新）
- 多语言完整构建 `build_full_graph(force)`（自动扫描所有支持语言）
- 指定目录构建 `build_directory(dir_path)`
- `.callwardenignore` 规则加载与匹配（类似 .gitignore，支持 `*`、`**`、`/` 前缀、目录后缀 `/`）
- 支持的源文件扫描（按扩展名 + 跳过 .git/node_modules/target 等目录）
- 多语言并行解析（ThreadPoolExecutor，最多 8 线程，按 CPU 核数自适应）
- 9 种语言分发：Rust / TypeScript / JavaScript / Python / Kotlin / Go / Java / C / C++
- 增量构建：基于 mtime + content_hash 判断未变化文件，从 DB 恢复符号/调用快照
- 多级调用解析策略（4 级）：精确匹配 → import 映射 → 简名唯一匹配 → 同文件简名匹配
- 调用关系跨文件标记（is_cross_file）
- 文件版本写入（content_hash 去重 + version_num 自增 + is_current 切换）
- 符号版本写入（file_symbol_versions + symbol_contents 去重）
- 符号 diff 计算（added / removed / modified）与删除标记应用
- 调用关系写入（calls 当前快照 + call_versions 版本记录）
- 拓扑深度计算（DFS + 缓存 + 环检测）
- 单文件增量刷新 `refresh_file`（Rust 专用 + 通用语言两套路径）
- 文件删除 `remove_file`（保留历史版本，仅标记 status=deleted）
- 从 DB 收集所有当前版本文件结果 `_collect_all_current_file_results`
- 通用模块路径推断 `_infer_module_path_generic`（处理 src/lib/app/main 前缀 + index/__init__/mod 入口文件）
- Cargo.toml crate 名称检测

**关键类/函数名**：
- `class BuildMixin`
- 构建入口：`build`、`build_full_graph`、`build_directory`、`build_call_graph`
- 扫描与忽略：`_load_ignore_patterns`、`_should_ignore`、`_scan_supported_files`
- 多语言构建：`_build_multi_lang`、`_build_call_graph_multi_lang`、`_make_call_entry`、`_infer_module_path_generic`
- 版本与符号：`_register_file_db`、`_get_file_version`、`_save_file_version`、`_compute_symbol_diff`、`_compute_and_apply_symbol_diff`、`_save_symbols_for_version`、`_ensure_symbol_content`、`_get_or_create_symbol`、`_insert_symbol`
- 调用与深度：`_resolve_file_calls`、`_write_calls_db`、`_save_calls_for_version`、`_build_depth`、`_update_symbol_version_depths`
- 增量：`refresh_file`、`_refresh_file_internal`、`_refresh_file_rust`、`_refresh_file_generic`、`remove_file`、`_load_file_result_from_db`、`_restore_symbol_snapshots`、`_collect_all_current_file_results`
- 辅助：`_detect_crate_name`、`_count_file_versions`、`_infer_module_path`

---

## 3. `code_graph\db_comment.py`

**核心功能描述**：注释恢复 Mixin，基于版本历史从历史版本提取注释并恢复到当前文件，支持单函数与批量恢复、预览模式。

**实现的能力清单**：
- 从历史版本获取注释 `get_comment_from_version(spec)`（支持 `fn@vN` 版本号格式与 `fn@hash` 哈希前缀格式）
- 单函数注释恢复 `restore_comment(spec, preview)`：
  - 定位当前文件中的函数定义行（识别 `fn`/`pub fn`/`pub(crate) fn`/`unsafe fn` 等多种修饰）
  - 检测当前是否已有 `///` 注释
  - 智能定位插入点（跳过已有的 `///`/`//!`/`#[attr]`/空行）
  - 替换/插入注释并写回文件
  - 支持预览模式（不写入，返回 new_content_preview）
  - 写入后自动 refresh_file 触发增量重建
- 批量恢复 `restore_all_comments(preview, file_filter)`：
  - 全工作区扫描有注释历史的函数（kind='fn' 且 has_comment=1）
  - 按文件分组，按函数行号倒序处理（避免行号偏移）
  - 跳过已有注释的函数
  - 返回详细统计（total_found / restored / skipped / failed / files / errors）
  - 支持文件路径过滤

**关键类/函数名**：
- `class CommentMixin`
- `get_comment_from_version`、`restore_comment`、`restore_all_comments`

---

## 4. `code_graph\db_git.py`

**核心功能描述**：Git 集成 Mixin，通过 subprocess 调用 git 命令导入 commit 历史、关联文件变更、查询 commit 详情与符号变更历史。

**实现的能力清单**：
- Git 历史导入 `import_git_history(max_commits)`：
  - 调用 `git log --format=%H|%s|%an|%ae|%ct` 拉取 commit 列表
  - 写入 git_commits 表（INSERT OR IGNORE 去重）
  - 检测 .git 目录是否存在
- Git 文件变更导入 `_import_git_file_changes`：
  - 调用 `git show --name-status --format=` 获取每个 commit 的文件变更
  - 解析变更类型（M/A/D/R 等）与文件路径
  - 通过 abs_path 匹配 file_instances 表，写入 git_file_changes
- 获取 commit 列表 `get_git_commits(limit, offset)`（按时间倒序分页）
- 获取 commit 变更详情 `get_commit_changes(commit_hash)`（关联 file_instances 显示 rel_path / abs_path）
- 获取符号的 Git 变更历史 `get_symbol_commit_history(symbol_hash, limit)`（通过 git_symbol_changes 关联 git_commits）
- Git 集成统计 `get_git_stats()`：commit 总数、文件变更总数、按变更类型分组统计

**关键类/函数名**：
- `class GitMixin`
- `import_git_history`、`_import_git_file_changes`、`get_git_commits`、`get_commit_changes`、`get_symbol_commit_history`、`get_git_stats`

---

## 5. `code_graph\db_metrics.py`

**核心功能描述**：代码度量 Mixin，基于符号内容、调用关系、版本历史实时计算圈复杂度、扇入扇出、耦合度、代码健康检查等度量指标，无需额外存储。

**实现的能力清单**：
- 圈复杂度计算 `_compute_cyclomatic_complexity(content, language)`：
  - 通用关键词计数（if/else/for/while/match/case/catch/&&/||/try/except/finally/when/guard）
  - 三元运算符 `? :` 识别（Rust/C/Java/TypeScript/Go）
  - Python 列表推导式额外路径
- 单函数度量 `get_function_metrics(qualified_name)`：
  - 圈复杂度 + 风险评级（低/中/高/极高）
  - 扇入（被谁调用）、扇出（调用了谁）
  - 行数、深度、模块路径、签名
- 复杂度热点 `get_complexity_hotspots(limit, module_filter)`（按复杂度降序）
- 模块耦合度分析 `get_coupling_analysis(limit)`：
  - afferent（传入耦合）/ efferent（传出耦合）
  - 不稳定性 = efferent / (afferent + efferent)
- 代码度量汇总 `get_code_metrics_summary()`：
  - 文件数、函数数、总行数、调用总数
  - 平均/最大复杂度 + 复杂度分布桶（低/中/高/极高）
  - 注释覆盖率
- 最大函数列表 `get_largest_functions(limit, module_filter)`（按行数降序）
- 最高耦合函数 `get_most_coupled_functions(limit)`（扇入+扇出总和降序）
- 代码健康检查 `get_code_health_check(severity)`：
  - 大文件检查（≥500/1000/2000 行三级）
  - 复杂函数检查（≥10/20/30 三级）
  - 超长函数检查（≥50/100/200 行三级）
  - 高耦合模块检查（instability ≥0.7/0.9 或 total_coupling ≥50）
  - 按严重程度过滤（all/high/medium/low）
  - 总体健康评分（0-100，扣分制）+ 健康等级（良好/一般/较差/很差）
  - AI Agent 修改前指引（agent_guidance）
- 单文件健康检查 `check_file_health(file_path)`：
  - 文件大小评估与拆分建议
  - 函数级问题列表（复杂度或行数超标）
  - should_split_first 标志
  - agent_warning 警告文本

**关键类/函数名**：
- `class MetricsMixin`
- `_COMPLEXITY_KEYWORDS`、`_compute_cyclomatic_complexity`
- `get_function_metrics`、`get_complexity_hotspots`、`get_coupling_analysis`、`get_code_metrics_summary`、`get_largest_functions`、`get_most_coupled_functions`、`get_code_health_check`、`check_file_health`

---

## 6. `code_graph\db_query.py`

**核心功能描述**：查询 Mixin，提供符号/文件/调用关系/历史版本/最近变更/模块依赖图等查询接口，是 CLI 与 MCP 工具的主要数据来源。

**实现的能力清单**：
- 全局统计 `get_stats()`：文件数、符号数、按 kind 分组、调用数、跨文件调用数、已解析调用数、注释数、深度分布、文件版本数、当前文件数、唯一符号内容数、多版本文件数等
- 状态概览 `get_status()`（用于 `cg status`）：
  - 工作区信息 + DB 大小
  - 文件跟踪状态（tracked / on_disk / new / stale / deleted + 各前 10 个示例）
  - 按语言扩展名分布
  - 符号统计与未注释函数数
  - 调用解析率
  - 最后构建时间 + needs_rebuild 标志
- 拓扑排序 `get_topological_order(limit)`（depth 升序，底层函数在前）
- 调用者查询 `get_callers(callee_name)`
- 被调用者查询 `get_callees(raller_name)`
- 文件路径查询 `get_file_by_path(file_path)`
- 符号历史 `get_symbol_history(qualified_name)`（按 parsed_at 倒序）
- 文件历史 `get_file_history(file_path)`（按 version_num 倒序）
- 通过 hash 获取符号内容 `get_symbol_content_by_hash(content_hash)`
- 时间字符串解析 `_parse_since(since)`（支持 1h/30m/1d/2h30m 组合）
- 最近变更 `get_recent_changes(since)`：变更文件列表 + 变更函数列表（新增/删除/修改 + prev_hash/curr_hash 对比）
- 符号位置查询 `get_symbol_location(name, file_path)`
- 文件符号列表 `get_file_symbols(file_path)`
- 符号搜索 `search_symbols(query, kind, limit)`（模糊匹配 qualified_name 和 name）
- 符号详情 `get_symbol(qualified_name)`（含基本信息 + calls_out + called_by）
- 模块依赖图导出 `export_module_graph(format, output_file)`：
  - Mermaid 格式（flowchart TD，带权重箭头）
  - DOT 格式（digraph，带 penwidth 权重）
  - 支持输出到文件或返回字符串
  - 按顶层模块聚合（取 `::` 分隔前 2-3 段）

**关键类/函数名**：
- `class QueryMixin`
- 统计：`get_stats`、`get_status`
- 拓扑与调用：`get_topological_order`、`get_callers`、`get_callees`
- 文件与符号：`get_file_by_path`、`get_symbol_history`、`get_file_history`、`get_symbol_content_by_hash`、`get_symbol_location`、`get_file_symbols`、`search_symbols`、`get_symbol`
- 变更：`_parse_since`、`get_recent_changes`
- 导出：`export_module_graph`

---

## 7. `code_graph\i18n.py`

**核心功能描述**：多语言国际化支持模块，通过 JSON 资源文件 + 点分隔键路径提供翻译能力，支持占位符替换与默认语言回退。

**实现的能力清单**：
- 默认语言设置（zh_CN）
- 语言资源缓存（_lang_cache）
- i18n 资源目录定位 `_get_i18n_dir()`
- 设置当前语言 `set_language(lang)`（预加载）
- 获取当前语言 `get_language()`
- 加载语言资源 `_load_lang(lang)`（不存在时回退到默认语言）
- 通用翻译函数 `t(key, default, **kwargs)`：
  - 点分隔键路径（如 `cli.messages.done`）
  - 占位符替换（`{name}` 格式）
  - 键不存在时返回 default 或 key 本身
- CLI 参数帮助文本 `get_arg_help(arg_name)`
- 消息文本获取 `get_msg(msg_key, default, **kwargs)`
- 错误文本获取 `get_error(err_key, default, **kwargs)`

**关键类/函数名**：
- 模块级常量：`DEFAULT_LANG`、`_lang_cache`、`_current_lang`
- `_get_i18n_dir`、`set_language`、`get_language`、`_load_lang`
- `t`、`get_arg_help`、`get_msg`、`get_error`

---

## 8. `code_graph\cli\console.py`

**核心功能描述**：CLI 输出工具集，提供彩色文本、进度条、Spinner、构建总结格式化等终端 UI 能力，自动适配 Windows VT 模式。

**实现的能力清单**：
- ANSI 颜色码字典（reset/bold/dim + 8 种基本色 + 6 种亮色）
- Windows VT 模式自动启用 `_enable_vt_mode()`（通过 kernel32 SetConsoleMode）
- 颜色使用判断 `should_use_color()`：
  - 检测 NO_COLOR 环境变量
  - 检测 FORCE_COLOR 环境变量
  - 检测 stdout 是否 TTY
  - 缓存结果
- 文本着色 `colorize(text, color)`
- 彩色打印 `cprint(text, color, bold)`
- 语义化快捷函数：`success`（✓ 绿）、`error`（✗ 红）、`warning`（⚠ 黄）、`info`（ℹ 青）、`dim`、`bold`
- 进度条 `print_progress(current, total, message)`：
  - 28 格宽度 + 百分比 + 计数 + 消息
  - TTY 模式 `\r` 原地刷新
  - 非 TTY 模式按 1/50/total 节流打印
- 清除进度条 `clear_progress()`
- 时长格式化 `format_duration(seconds)`（ms/s/m/h 自适应）
- 文件大小格式化 `format_size(n)`（B/KB/MB）
- 构建总结打印 `print_build_summary`（文件处理统计 + 图谱统计 + 耗时 + 失败提示）
- Spinner 动画类（10 帧 braille 字符 + 计时）：
  - `start`、`_render`、`tick`、`stop(final_msg)`
  - 非 TTY 模式降级为普通打印

**关键类/函数名**：
- 模块级常量：`_COLORS`、`_use_color`、`_progress_active`、`_last_line_len`
- 函数：`_enable_vt_mode`、`should_use_color`、`colorize`、`cprint`、`success`、`error`、`warning`、`info`、`dim`、`bold`、`print_progress`、`clear_progress`、`format_duration`、`format_size`、`print_build_summary`
- `class Spinner`

---

## 9. `code_graph\parsers\call_filter.py`

**核心功能描述**：调用过滤模块，识别并过滤 9 种语言的标准库、外部依赖、内置函数、全局对象等非项目内部调用，减少无效调用关系存储。

**实现的能力清单**：
- Rust 调用过滤：
  - 标准库前缀过滤（std::/core::/alloc::/prelude::等 14 个前缀）
  - 标准宏过滤（println/print/format/vec/assert/panic 等 30+ 个）
  - 常用 Derive 过滤（Debug/Clone/Copy/PartialEq 等 20 个）
  - 常见外部 crate 过滤（tokio/serde/anyhow/clap/reqwest 等 35+ 个）
  - 常用容器类型过滤（Ok/Err/Some/None/Box/Rc/Arc/Vec/String 等）
  - 宏调用后缀 `!` 过滤
- TypeScript/JavaScript 调用过滤：
  - 全局对象过滤（console/Math/JSON/Object/Array/Promise/React 等 70+ 个）
  - 常见模块过滤（react/axios/lodash/express/jest/fs/path 等 35+ 个）
  - `node:` 前缀过滤
- Kotlin/Java 调用过滤：
  - 标准库前缀过滤（kotlin./kotlinx./java./javax./android. 等）
  - 标准类过滤（String/Int/List/Map/Pair/Unit/Any 等 30+ 个）
  - 作用域函数过滤（run/let/also/apply/with 等）
- Go 调用过滤：
  - 标准库前缀过滤（fmt./os./io./net./encoding./strings./strconv. 等 30+ 个）
  - 内置函数过滤（append/copy/delete/len/cap/make/new/panic/recover 等）
- Python 调用过滤：
  - 标准模块过滤（os/sys/re/json/time/collections/pathlib/subprocess 等 50+ 个）
  - 内置函数过滤（print/len/range/str/int/list/dict/Exception 等 50+ 个）
  - 保留 self./cls. 前缀调用
- C# 调用过滤：
  - .NET 标准库前缀过滤（System./Microsoft. 等）
  - 关键字与常用方法过滤（Console.WriteLine/ToString/Equals/List/Dictionary/Task.Run 等）
- C 调用过滤：
  - C 标准库函数过滤（printf/fprintf/malloc/free/strcpy/strlen/fopen 等 50+ 个）
- C++ 调用过滤：
  - C 标准库函数过滤（继承 C 的）
  - std:: 前缀过滤（含 std::chrono::/std::filesystem::/std::ranges::）
  - boost:: 前缀过滤
  - 标准类过滤（cout/cin/string/vector/map/unique_ptr/shared_ptr 等）
- 统一入口 `should_filter_call(language, call_name)`：按语言分发到对应过滤函数

**关键类/函数名**：
- 各语言常量集合：`RUST_STD_PREFIXES`、`RUST_STD_MACROS`、`RUST_COMMON_DERIVE`、`RUST_COMMON_EXTERNAL_CRATES`、`TS_JS_GLOBALS`、`TS_JS_COMMON_MODULES`、`KOTLIN_STD_PREFIXES`、`KOTLIN_STD_CLASSES`、`GO_STD_PREFIXES`、`GO_BUILTIN`、`PYTHON_STD_MODULES`、`PYTHON_BUILTINS`、`CSHARP_STD_PREFIXES`、`CSHARP_KEYWORDS`、`C_STD_FUNCTIONS`、`CPP_STD_PREFIXES`、`CPP_STD_CLASSES`
- 各语言过滤函数：`should_filter_rust_call`、`should_filter_ts_js_call`、`should_filter_kotlin_call`、`should_filter_go_call`、`should_filter_python_call`、`should_filter_csharp_call`、`should_filter_c_call`、`should_filter_cpp_call`
- 统一入口：`should_filter_call(language, call_name)`

---

## 10. `code_graph\code_graph 功能差距分析报告.md`

**核心功能描述**：code_graph 项目的功能差距分析报告（约 2567 行），系统盘点已有功能、对比 5 大竞品（tokensave/codebase-memory-mcp/code-review-graph/Understand-Anything/pearai-CodeGraph）、列出 P0-P3 缺失功能清单、并给出 Semgrep 集成策略、Task 管理 MCP 设计、向量库引入方案等下一步演进建议。

**实现的能力清单**（报告内容能力）：
- 已有功能盘点（10 类功能成熟度评估）：多语言解析 / 符号提取 / 调用关系 / 版本历史 / 多工作区 / MCP 服务器（38 工具）/ 文件监控 / 拓扑分析 / 缺陷检测（Semgrep）/ 注释管理
- P0 核心缺失清单：向量嵌入/语义搜索、RAG/知识库问答、代码摘要/文档生成、Repo Map/仓库地图
- P1 重要缺失清单：跨层变更影响分析、所有权分析/CODEOWNERS、测试影响选择、覆盖率地图（LCOV/Cobertura）、Git 深度集成（diff/blame/log）
- P2 企业级缺失清单：多仓库/Monorepo、权限/多用户、性能监控指标、CI/CD 集成
- P3 体验优化缺失清单：AST 级精确增量、LSP 服务器、分支感知图数据库、Token 节省账本、热点/复杂度分析、重复代码检测
- 与 tokensave 14 项能力维度对比（结论：结构化图谱接近，AI 辅助层有大差距，注释恢复与 Semgrep 集成领先）
- 与 ai-sdlc-control-plane P4 层关系定位（Python 原型 vs Rust 产品）
- 5 大竞品对比矩阵（语言/部署/AI 集成/优势场景）
- 性能目标建议（本地 SQLite P50<50ms / P95<200ms，含部署模式对照表）
- 三层 MCP 架构推荐：结构层（Code Graph）+ 扫描层（Semgrep/ast-grep）+ 执行层（File-Modify）
- MCP 文件修改接口设计（tasks.create / files.modify / tasks.rollback + dry_run + hash_before 冲突检测）
- Task 管理子系统设计（task 表 / task_operations 表 / 状态机：open→in_progress→dry_run_done→review→applied→verified→closed/reverted）
- 任务驱动 MCP 流程设计（task.next_step / task.report_step 强制同步机制，解决 AI 健忘症）
- 与 Superpowers 6.0 思想对比（reviewer pipeline / 进度账本 / 文件传递替代文本粘贴）
- 任务 27 Git 集成完整实现代码（git_commits 表 schema + _get_git_info / link_build_to_commit / get_recent_commits / get_commit_changes / get_symbol_commit_history 方法 + CLI 集成）
- patch_db.py 自动注入脚本（解决 Windows CRLF/PowerShell 多行字符串问题）
- TokenSlim 三大独家护城河总结：Git Checkout 防御（注释恢复）、Semgrep 融合、多工作区隔离
- 向量库引入方案：
  - 行业案例（Sourcegraph Cody / GitHub Copilot / Continue.dev / Tabby / Aider / Cursor）
  - 技术选型：sqlite-vec（推荐，与 SQLite 契合）/ ChromaDB / FAISS
  - 嵌入模型选型：jina-embeddings-v2-base-code（代码专用，推荐）/ bge-small-zh / all-MiniLM-L6-v2 / nomic-embed-text
  - 渐进式架构（现有 SQLite 结构化层 + 新增向量语义层 + 查询层）
  - 代价评估（首次嵌入 8000 函数约 15 分钟、存储增量 12MB、模型文件 80-160MB）
  - 三阶段实施路径（Phase 1 sqlite-vec 集成 / Phase 2 semantic_search MCP 工具 / Phase 3 注释向量化 + 调用链语义补全）
- 性能优化建议：PyO3 混合迁移（局部 Rust 重写热路径 10-100× 加速）vs 全量重写对比

**关键类/函数名**：N/A（文档文件，无代码定义。包含的代码示例为：`_migrate_v3_to_v4`、`_get_git_info`、`link_build_to_commit`、`get_recent_commits`、`get_commit_changes`、`get_symbol_commit_history`、`_import_git_history`、`patch_db_file`）

---

# 二、code_graph 项目完整能力清单（去重合并）

## A. 数据库与基础设施层

1. **SQLite 持久化**：单用户一数据库（`~/.code_graph/code_graph.db`），多工作区通过表隔离
2. **Schema 版本化迁移**：v1→v2→v3→v4 自动增量迁移，事务化执行，保留历史数据
3. **多工作区管理**：注册、列表、设置活动、级联删除（含版本/符号/调用/Semgrep 数据清理）
4. **项目根目录自动检测**：基于 SCRIPT_DIR 向上探测
5. **content_hash 去重机制**：文件内容与符号内容均按 hash 去重存储

## B. 多语言解析层

6. **9 种语言支持**：Rust / TypeScript / JavaScript / Python / Kotlin / Go / Java / C / C++
7. **tree-sitter 解析**：基于 RustParser 与 create_parser 工厂分发
8. **符号提取**：函数 / 类 / 结构体 / 接口 / 枚举 + 注释检测 + 行号范围 + 签名 + 可见性
9. **模块路径推断**：通用语言（处理 src/lib/app/main 前缀 + index/__init__/mod 入口）+ Rust 专用
10. **内联模块支持**：inline_modules 符号扩展
11. **Cargo.toml crate 名称检测**

## C. 调用图构建层

12. **多级调用解析策略**（4 级）：精确匹配 → import 映射 → 简名唯一匹配 → 同文件简名匹配
13. **跨文件调用标记**（is_cross_file）
14. **调用关系版本化**：calls 当前快照 + call_versions 版本记录
15. **9 语言调用过滤**：标准库 / 外部依赖 / 内置函数 / 全局对象 / 宏 / Derive 全面过滤
16. **拓扑深度计算**：DFS + 缓存 + 环检测
17. **符号版本深度同步**

## D. 版本历史层

18. **文件版本管理**：file_versions 表，version_num 自增，is_current 切换
19. **符号版本管理**：file_symbol_versions 表，关联 file_version_id + symbol_hash
20. **符号 diff 计算**：added / removed / modified 三类
21. **删除标记机制**：is_deleted 字段（保留历史，不物理删除）
22. **从 DB 恢复文件结果**：增量构建时复用已解析结果
23. **符号快照恢复**：从 file_symbol_versions + symbol_contents 恢复 symbols 表

## E. 构建流水线

24. **完整构建**：6 步流程（模块→文件→版本→调用→深度→符号深度）
25. **多语言并行解析**：ThreadPoolExecutor，最多 8 线程自适应
26. **增量构建**：mtime + content_hash 判断未变化文件
27. **单文件刷新**：refresh_file（Rust 专用 + 通用语言两套路径）
28. **文件删除**：remove_file（保留历史版本，标记 status=deleted）
29. **.callwardenignore 规则**：类似 .gitignore，支持 `*`/`**`/`/` 前缀/目录后缀
30. **构建总结报告**：文件处理统计 + 图谱统计 + 耗时 + 失败提示

## F. 查询能力层

31. **全局统计**：文件数 / 符号数 / 调用数 / 深度分布 / 版本数 / 唯一内容数 / 多版本文件数
32. **状态概览**：tracked/on_disk/new/stale/deleted + 语言分布 + 解析率 + needs_rebuild
33. **拓扑排序查询**（底层函数在前）
34. **调用者/被调用者查询**
35. **符号/文件历史查询**（按时间或版本号倒序）
36. **符号内容查询**（by hash）
37. **最近变更查询**：时间窗口（1h/30m/1d/2h30m）+ 新增/删除/修改分类
38. **符号位置/文件符号列表/符号搜索**（模糊匹配 qualified_name 和 name）
39. **符号详情**：基本信息 + calls_out + called_by
40. **模块依赖图导出**：Mermaid（flowchart TD）+ DOT（digraph）格式

## G. 注释管理层

41. **历史注释获取**：支持 `fn@vN` 与 `fn@hash` 两种格式
42. **单函数注释恢复**：智能定位函数定义 + 跳过已有修饰 + 预览模式 + 自动刷新
43. **批量注释恢复**：全工作区扫描 + 按文件分组 + 倒序处理避免行号偏移 + 详细统计
44. **注释覆盖率统计**

## H. Git 集成层

45. **Git 历史导入**：git log + git show --name-status，写入 git_commits + git_file_changes
46. **commit 列表查询**（分页）
47. **commit 变更详情查询**（关联 file_instances）
48. **符号 commit 历史查询**（通过 git_symbol_changes）
49. **Git 集成统计**：commit 总数 / 文件变更总数 / 按变更类型分组
50. **file_versions.commit_hash 关联**

## I. 代码度量层

51. **圈复杂度计算**：多语言关键词 + 三元运算符 + Python 列表推导
52. **单函数度量**：复杂度 + 风险评级 + 扇入 + 扇出 + 行数 + 深度
53. **复杂度热点列表**（按复杂度降序，支持模块过滤）
54. **模块耦合度分析**：afferent/efferent/instability
55. **代码度量汇总**：平均/最大复杂度 + 分布桶 + 注释覆盖率
56. **最大函数列表** / **最高耦合函数列表**
57. **代码健康检查**：大文件/复杂函数/超长函数/高耦合模块四级检查 + 严重程度过滤 + 健康评分（0-100）+ AI Agent 指引
58. **单文件健康检查**：拆分建议 + agent_warning

## J. CLI 与终端 UI 层

59. **彩色文本输出**：8 基本色 + 6 亮色 + bold/dim，Windows VT 自动启用
60. **语义化快捷函数**：success/error/warning/info/dim/bold
61. **进度条**：28 格 + 百分比 + TTY 原地刷新 / 非 TTY 节流打印
62. **Spinner 动画**：10 帧 braille + 计时，非 TTY 降级
63. **时长/文件大小格式化**
64. **构建总结格式化打印**

## K. 国际化层

65. **多语言资源加载**：JSON 文件 + 缓存 + 默认语言回退
66. **点分隔键路径翻译**：支持占位符替换
67. **CLI 参数帮助/消息/错误文本专用获取函数**

## L. 差距分析报告中的规划能力（尚未实现）

68. **向量嵌入/语义搜索**（P0 缺失，规划中：sqlite-vec + jina-embeddings-v2-base-code）
69. **RAG/知识库问答**（P0 缺失）
70. **代码摘要/文档生成**（P0 缺失）
71. **Repo Map/仓库地图**（P0 缺失）
72. **跨层变更影响分析**（P1 缺失：代码 + 数据库 + 配置层）
73. **所有权分析/CODEOWNERS**（P1 缺失）
74. **测试影响选择**（P1 缺失）
75. **覆盖率地图**（P1 缺失：LCOV/Cobertura 导入）
76. **Task 管理 MCP**（设计中：tasks 表 + task_operations 表 + 状态机 + propose_edit/commit_task/rollback_task）
77. **安全文件编辑 MCP**（设计中：dry_run + hash_before 校验 + 临时分支验证 + 审计日志 + 回滚）
78. **任务驱动 MCP 流程**（设计中：task.next_step / task.report_step 强制同步）
79. **多仓库/Monorepo 支持**（P2 缺失）
80. **LSP 服务器**（P3 缺失）
81. **分支感知图数据库**（P3 缺失）
82. **Token 节省账本**（P3 缺失）
83. **重复代码检测**（P3 缺失）
84. **PyO3 性能优化路径**（建议中：局部 Rust 重写热路径）

---

# 三、关键文件路径汇总

所有相关文件绝对路径如下：

- `c:\git_work\ai-code-review\ai-competitor-radar\code_graph\db_base.py`
- `c:\git_work\ai-code-review\ai-competitor-radar\code_graph\db_build.py`
- `c:\git_work\ai-code-review\ai-competitor-radar\code_graph\db_comment.py`
- `c:\git_work\ai-code-review\ai-competitor-radar\code_graph\db_git.py`
- `c:\git_work\ai-code-review\ai-competitor-radar\code_graph\db_metrics.py`
- `c:\git_work\ai-code-review\ai-competitor-radar\code_graph\db_query.py`
- `c:\git_work\ai-code-review\ai-competitor-radar\code_graph\i18n.py`
- `c:\git_work\ai-code-review\ai-competitor-radar\code_graph\cli\console.py`
- `c:\git_work\ai-code-review\ai-competitor-radar\code_graph\parsers\call_filter.py`
- `c:\git_work\ai-code-review\ai-competitor-radar\code_graph\code_graph 功能差距分析报告.md`

**架构观察补充**：code_graph 项目采用 **Mixin 模式** 组织代码——`CodeGraphBase`（db_base.py）作为核心基类提供数据库连接、Schema 迁移、工作区管理；`BuildMixin`/`CommentMixin`/`GitMixin`/`MetricsMixin`/`QueryMixin` 五个 Mixin 分别混入构建、注释、Git、度量、查询能力（这五个 Mixin 在 analyzers 等其他模块中通过多继承组合成最终的 CodeGraphDB 类，本次未读取该组合文件）。底层依赖 `config`、`schema`、`parsers`（RustParser/ModuleResolver/CallResolver/create_parser）等模块，CLI 层依赖 `cli.console` 提供终端 UI。报告文件（第 10 个）确认了项目当前在结构化图谱层面已接近竞品 tokensave，但在 AI 辅助层（向量嵌入/RAG/摘要生成）有大差距，独家护城河是注释恢复与 Semgrep 集成，下一步重点建议是 Task 管理 + 安全文件编辑 + 向量语义层。
        
          

---

# AI Coding Agent Safety 竞品雷达分析报告

> 数据来源：`refined_summary_report.md`、`summary_report.md` 以及 `llm_reports/` 目录下 200 个 JSON 报告
> 分析项目总数：200

---

## 1. 所有仓库的名称和分类列表

从 200 个 JSON 报告中提取，按字母序整理（共 177 个有明确 category 字段，以下列出全部已提取到的仓库）：

| #   | 仓库名 (repo_name)                                         | category                                                  |
| --- | ---------------------------------------------------------- | --------------------------------------------------------- |
| 1   | 0x5457__ts-index                                           | code_intelligence_mcp_server                              |
| 2   | 3301x2__MaestroNexus                                       | AI Code Intelligence MCP Tool                             |
| 3   | aaif-goose__goose                                          | MCP-native AI developer agent                             |
| 4   | AbdullahBakir97__DevTrust                                  | AI developer-tool / code-intelligence trust stack         |
| 5   | adgk2349__FlowMap                                          | code_intelligence_graph_impact_analysis                   |
| 6   | adrianczuczka__mason                                       | AI code context MCP server                                |
| 7   | aeroxy__ast-bro                                            | AI code-intelligence CLI                                  |
| 8   | afnanenayet__diffsitter                                    | ast_semantic_diff_and_local_code_intelligence_cli         |
| 9   | AI45Lab__AgentDoG                                          | ai_agent_safety_alignment_framework                       |
| 10  | aiming-lab__AutoHarness                                    | ai_agent_governance_middleware                            |
| 11  | aipotheosis-labs__aci                                      | ai_agent_tool_infrastructure                              |
| 12  | aipotheosis-labs__gate22                                   | mcp_gateway_control_plane                                 |
| 13  | akashagalave__CodeSentinel-AI                              | AI Code Review Platform                                   |
| 14  | alex4o__code-agent                                         | AI codebase context compression and MCP code intelligence |
| 15  | alexwl__haskell-code-explorer                              | code-intelligence                                         |
| 16  | alibaba__OpenSandbox                                       | AI agent sandbox runtime                                  |
| 17  | aliyun__alibabacloud-ack-mcp-server                        | kubernetes_aiops_mcp_server                               |
| 18  | allentcm__mcp-codebase-rag                                 | codebase_rag_mcp_server                                   |
| 19  | AMisljenovic__loom                                         | ai_coding_agent_ide_extension                             |
| 20  | AmoyLab__Unla                                              | mcp_gateway                                               |
| 21  | andreaswasita__copilot-agents-dojo                         | AI Agent Workflow & Memory Infrastructure                 |
| 22  | AniketS01__Impact-Mapper                                   | static_impact_analysis_cli                                |
| 23  | AnirudPaul__yapdex                                         | local_mcp_code_intelligence                               |
| 24  | Anjal10911__Codegraph                                      | local_code_intelligence_mcp                               |
| 25  | ankane__ownership                                          | code_ownership_context                                    |
| 26  | anujkamaljain__CodeChronicle                               | AI codebase-intelligence IDE plugin                       |
| 27  | anushreebhargava14__blast-radius                           | ai_pr_review_risk_intelligence                            |
| 28  | aovestdipaperino__tokensave                                | AI code-intelligence MCP server                           |
| 29  | ArcadeAI__blueprint-mcp                                    | mcp_server                                                |
| 30  | ArchCodexOrg__archcodex                                    | AI developer-tool / architecture governance               |
| 31  | ArchiCore-Team__archicore                                  | code_intelligence_cli                                     |
| 32  | archondevio__archondev                                     | AI development governance CLI                             |
| 33  | arisvas4__codified-context-infrastructure                  | ai_context_infrastructure                                 |
| 34  | Arvo-AI__aurora                                            | AI incident response / SRE RCA agent                      |
| 35  | ashfordeOU__grasp                                          | AI Code Intelligence Platform                             |
| 36  | AsiaOstrich__EngramGraph                                   | AI developer tool / code intelligence graph memory        |
| 37  | asta-nguyen__openez-graph                                  | AI code intelligence / local Graph RAG MCP developer tool |
| 38  | AstraBert__code-ragent                                     | codebase_rag_assistant                                    |
| 39  | asta-nguyen__openez-graph                                  | AI code intelligence / local Graph RAG MCP developer tool |
| 40  | Ataraxy-Labs__inspect                                      | Entity-level AI code review and change impact analysis    |
| 41  | Ataraxy-Labs__sem                                          | AI developer tool / code intelligence                     |
| 42  | Atharva-Jayappa__blast-scope                               | ai_agent_command_safety                                   |
| 43  | atomsai__pipguard                                          | security_scanning                                         |
| 44  | AustinSchoen__codebase-intel                               | AI code intelligence MCP server                           |
| 45  | Azure-Samples__foundry-citadel-platform                    | ai-governance-reference-architecture                      |
| 46  | backbay-labs__clawdstrike                                  | ai_security_policy_engine                                 |
| 47  | baikaishuipp__jcci                                         | code-intelligence-impact-analysis                         |
| 48  | bartolli__codanna                                          | AI code-intelligence MCP developer tool                   |
| 49  | bazfer__stud-finder                                        | code_risk_scoring_cli                                     |
| 50  | Beer-Bears__scaffold                                       | codebase_graph_rag_mcp                                    |
| 51  | Beledarian__mcp-local-memory                               | ai_agent_memory_infrastructure                            |
| 52  | bettyabay__Codebase-Understanding-Agent                    | ai_codebase_intelligence                                  |
| 53  | Bhavikupadhyay__coverage-agent                             | coverage_driven_ai_test_generation                        |
| 54  | BigJai__codemunch-pro                                      | ai_code_intelligence_mcp_server                           |
| 55  | bntvllnt__codebase-intelligence                            | AI codebase intelligence                                  |
| 56  | bobbydeveaux__cerebra                                      | AI developer-tool / code-intelligence                     |
| 57  | bonigarcia__context-engineering                            | context_engineering_example_suite                         |
| 58  | BoundaryML__baml                                           | AI developer tool / LLM workflow DSL                      |
| 59  | boringSQL__dryrun                                          | database_schema_intelligence_mcp                          |
| 60  | Bpolat0__atlasmemory                                       | AI codebase memory and code-intelligence tool             |
| 61  | can1357__smgrep                                            | semantic_code_search                                      |
| 62  | cavenine__ctxpp                                            | AI code-intelligence MCP server                           |
| 63  | caviraoss__openmemory                                      | AI memory engine                                          |
| 64  | ccantynz-alt__Gluecron.com                                 | AI-native Git forge / code intelligence platform          |
| 65  | cdklabs__cdk-nag                                           | iac_security_compliance_scanner                           |
| 66  | chanhx__crabviz                                            | code_intelligence                                         |
| 67  | charmbracelet__crush                                       | AI coding assistant                                       |
| 68  | chunkhound__chunkhound                                     | code-intelligence                                         |
| 69  | circlemind-ai__fast-graphrag                               | graph_rag_framework                                       |
| 70  | Cluster444__agentic                                        | AI coding workflow CLI                                    |
| 71  | cocoindex-io__cocoindex-code                               | AI代码语义搜索与本地代码索引工具                          |
| 72  | coded-devs__lineageguard                                   | data-lineage-impact-analysis                              |
| 73  | Cre4T3Tiv3__gitvoyant                                      | code-intelligence                                         |
| 74  | cs-au-dk__jelly                                            | static-analysis-security                                  |
| 75  | curtisdery__cortex                                         | code-intelligence                                         |
| 76  | cybernetix-lab__moss-harness                               | agent_runtime_devtools                                    |
| 77  | dailephd__my-dev-kit                                       | code_intelligence_cli                                     |
| 78  | DancingLightStudios__ariadex                               | code-intelligence                                         |
| 79  | dandyArise__RepoLens                                       | code-intelligence                                         |
| 80  | daneb__tecr                                                | code-intelligence                                         |
| 81  | danielbushman__crewchief                                   | ai-code-intelligence                                      |
| 82  | danieliser__tessera                                        | code-intelligence-mcp                                     |
| 83  | darklordVirtual__REMORA                                    | ai-action-governance                                      |
| 84  | DariuszNewecki__CORE                                       | ai-governance-code-intelligence                           |
| 85  | davide-desio-eleva__kirograph                              | ai_code_intelligence_mcp                                  |
| 86  | deepflowio__deepflow                                       | observability_platform                                    |
| 87  | dgtalbug__dextree                                          | code-intelligence                                         |
| 88  | dip497__onelens                                            | code-intelligence                                         |
| 89  | dirac-run__dirac                                           | ai_developer_tool                                         |
| 90  | djinn-soul__CytoScnPy                                      | static-analysis                                           |
| 91  | duriantaco__gitgrapher                                     | code-intelligence                                         |
| 92  | duriantaco__skylos                                         | code-intelligence-security-cli                            |
| 93  | e7nd7r__gnapsis                                            | code-intelligence-mcp                                     |
| 94  | eastlondoner__vibe-tools                                   | ai-developer-cli                                          |
| 95  | entrepeneur4lyf__code-graph-mcp                            | code-intelligence-mcp                                     |
| 96  | etcircle__cga                                              | code-intelligence                                         |
| 97  | etinpres__mindvault                                        | code-intelligence-rag                                     |
| 98  | event-catalog__eventcatalog                                | architecture-governance                                   |
| 99  | explyt__spring-plugin                                      | ide-code-intelligence                                     |
| 100 | fallow-rs__fallow                                          | code-intelligence                                         |
| 101 | Fanaperana__adaptive-codegraph                             | code-intelligence                                         |
| 102 | faramesh__faramesh-core                                    | ai-agent-governance                                       |
| 103 | featureform__enrichmcp                                     | mcp-framework                                             |
| 104 | fjb040911__ai-rules                                        | ai_coding_governance_cli                                  |
| 105 | fluffypony__mcp-code-indexer                               | code-intelligence-mcp-server                              |
| 106 | forloopcodes__contextplus                                  | ai-code-intelligence-mcp                                  |
| 107 | fulminate-io__knowledge-mcp                                | ai-code-intelligence-mcp                                  |
| 108 | g0GobliN__reality-map                                      | code-intelligence                                         |
| 109 | garrytan__gstack                                           | ai_developer_workflow                                     |
| 110 | gastownhall__gastown                                       | ai-agent-orchestration-cli                                |
| 111 | getzep__graphiti                                           | graph-rag-memory                                          |
| 112 | giancarloerra__socraticode                                 | ai_code_intelligence_mcp                                  |
| 113 | GH05TCREW__pentestagent                                    | ai_security_agent                                         |
| 114 | github__gh-aw-mcpg                                         | mcp_gateway                                               |
| 115 | gkatte__codemesh                                           | code-intelligence                                         |
| 116 | glommer__codemogger                                        | code-intelligence                                         |
| 117 | glorynguyen__ollama-code-review                            | ai-code-review                                            |
| 118 | GoPlusSecurity__agentguard                                 | ai-agent-security                                         |
| 119 | gortexhq__vscode-gortex                                    | code-intelligence-ide-extension                           |
| 120 | Govcraft__rust-docs-mcp-server                             | documentation-rag-mcp                                     |
| 121 | graph-memory__graphmemory                                  | ai-code-intelligence-mcp                                  |
| 122 | grahambrooks__symgraph                                     | code-intelligence-mcp                                     |
| 123 | GreatScottyMac__context-portal                             | ai_memory_mcp_server                                      |
| 124 | GreatScottyMac__roo-code-memory-bank                       | AI Context Management                                     |
| 125 | GulumseKadin__pr-impact-analyzer                           | ide_code_intelligence                                     |
| 126 | guyowen__typegraph-mcp                                     | code-intelligence-mcp                                     |
| 127 | HarshalRathore__code-intel-mcp                             | code-intelligence-mcp                                     |
| 128 | HeadyZhang__agent-audit                                    | ai-agent-security-scanner                                 |
| 129 | Helweg__opencode-codebase-index                            | code-intelligence                                         |
| 130 | HenryLok0__CodeState                                       | code-intelligence-cli                                     |
| 131 | HeroZ-Dodge__android-code-index                            | code-intelligence                                         |
| 132 | heymrun__heym                                              | ai_workflow_automation_platform                           |
| 133 | hieuchaydi__RepoBrain                                      | ai-code-intelligence                                      |
| 134 | hiranp__hief                                               | AI Developer Tooling                                      |
| 135 | hkevin01__automated-traceability-requirements-intelligence | requirements-traceability-intelligence                    |
| 136 | Hmbown__aleph                                              | ai-devtool-mcp-server                                     |
| 137 | hookdeck__hookdeck-cli                                     | developer_cli_mcp_gateway                                 |
| 138 | hptbee__b3_mcp                                             | code-intelligence-mcp                                     |
| 139 | husnainpk__SymDex                                          | code-intelligence                                         |
| 140 | Huzefaaa2__cavra                                           | ai-agent-runtime-governance                               |
| 141 | Hyperion-GPU__ProofFlow-v0.1                               | AI Developer Workflow Audit                               |
| 142 | iflow-mcp__gladego-index1                                  | ai_code_intelligence_memory                               |
| 143 | iliaal__codesage                                           | code-intelligence                                         |
| 144 | illdynamics__qonqrete                                      | ai_developer_tool                                         |
| 145 | imtt-dev__steer                                            | ai-agent-reliability                                      |
| 146 | interaction-dynamics__features                             | code-intelligence                                         |
| 147 | invariantlabs-ai__invariant                                | AI Guardrails                                             |
| 148 | Ivan825__pr-sentinel                                       | pr-risk-intelligence                                      |
| 149 | iwe-org__iwe                                               | markdown-knowledge-graph-agent-tool                       |
| 150 | jagmarques__asqav-sdk                                      | ai-agent-governance-sdk                                   |
| 151 | janreges__ai-distiller                                     | code-intelligence                                         |
| 152 | JaredStewart__coderlm                                      | code-intelligence                                         |
| 153 | jayu__rev-dep                                              | dependency-analysis-cli                                   |
| 154 | jeewandaniel__wp-plugin-compliance-checker                 | compliance_scanner                                        |
| 155 | joshua-light__resharper-mcp                                | code-intelligence-mcp-server                              |
| 156 | KevinRabun__judges                                         | AI-Code-Review-MCP                                        |
| 157 | Michaol__RustRAG                                           | local-rag-mcp-server                                      |
| 158 | Muvon__octocode                                            | code-intelligence                                         |
| 159 | nyxCore-Systems__LIP                                       | code-intelligence-daemon                                  |
| 160 | prashantsinghmangat__Indexa                                | AI_code_intelligence                                      |
| 161 | prateekgaurdev__repo-view                                  | ai_context_packaging                                      |
| 162 | PrismorSec__immunity-agent                                 | AI Agent Security                                         |
| 163 | punkpeye__fastmcp                                          | mcp_server_framework                                      |
| 164 | quangdang46__ms                                            | ai_developer_tool                                         |
| 165 | Regsorm__code-index-mcp                                    | code-intelligence-mcp                                     |
| 166 | Rootly-AI-Labs__rootly-graphify-importer                   | code-intelligence                                         |
| 167 | salvo10f__godotiq                                          | mcp_code_intelligence                                     |
| 168 | sandy-sachin7__contextd-vscode                             | code-intelligence                                         |
| 169 | SarthakMogane__GraphRAG-for-codebase-understanding         | code-intelligence-graphrag                                |
| 170 | scanadi__synaptiq                                          | code_intelligence                                         |
| 171 | sdkks__symbol-index                                        | code-intelligence                                         |
| 172 | siy__ndx                                                   | ai-memory-cli                                             |
| 173 | sjr27-maker__Impact-Lens                                   | code-intelligence                                         |
| 174 | socprime__AIDR-Bastion                                     | AI-Security-Gateway                                       |
| 175 | unfault__unlost                                            | AI Code Memory                                            |

> 注：约 23 个仓库的 JSON 中 category 字段格式不同或位于非标准行位置，未在上方 grep 结果中显示。relevance_score 存在两种分制：约 100 个仓库使用 0-1 分制（如 0.97），约 77 个使用 0-10 分制（如 9.4）。

---

## 2. 能力标签出现频次（从高到低排序）

直接来自 `refined_summary_report.md` 第 2 节，覆盖 200 个项目：

| 排名 | 能力标签 (feature)         | 覆盖项目数 | 覆盖率 |
| ---- | -------------------------- | ---------- | ------ |
| 1    | cli_tool                   | 178        | 89.0%  |
| 2    | runtime_entrypoints        | 173        | 86.5%  |
| 3    | mcp_server                 | 140        | 70.0%  |
| 4    | repo_map                   | 126        | 63.0%  |
| 5    | local_mcp                  | 124        | 62.0%  |
| 6    | symbol_index               | 124        | 62.0%  |
| 7    | git_integration            | 122        | 61.0%  |
| 8    | multilang_support          | 118        | 59.0%  |
| 9    | code_quality               | 100        | 50.0%  |
| 10   | module_map                 | 99         | 49.5%  |
| 11   | semantic_search            | 98         | 49.0%  |
| 12   | impact_analysis            | 98         | 49.0%  |
| 13   | token_optimization         | 96         | 48.0%  |
| 14   | sqlite_cache               | 92         | 46.0%  |
| 15   | dependency_analysis        | 92         | 46.0%  |
| 16   | vector_embedding           | 90         | 45.0%  |
| 17   | call_graph                 | 90         | 45.0%  |
| 18   | incremental_index          | 90         | 45.0%  |
| 19   | context_compression        | 89         | 44.5%  |
| 20   | tree_sitter_ast            | 86         | 43.0%  |
| 21   | hard_constraints           | 81         | 40.5%  |
| 22   | legacy_compatibility       | 79         | 39.5%  |
| 23   | api_compatibility          | 75         | 37.5%  |
| 24   | project_brief              | 72         | 36.0%  |
| 25   | security_scanning          | 71         | 35.5%  |
| 26   | rag                        | 69         | 34.5%  |
| 27   | rule_packs                 | 66         | 33.0%  |
| 28   | coding_guardrails          | 66         | 33.0%  |
| 29   | knowledge_graph            | 60         | 30.0%  |
| 30   | encoding_support           | 59         | 29.5%  |
| 31   | observability_requirements | 58         | 29.0%  |
| 32   | review_readiness           | 54         | 27.0%  |
| 33   | ide_plugin                 | 53         | 26.5%  |
| 34   | bug_case_rules             | 50         | 25.0%  |
| 35   | api_documentation          | 45         | 22.5%  |
| 36   | preflight_check            | 43         | 21.5%  |
| 37   | diagnosability_rules       | 40         | 20.0%  |
| 38   | database_risk              | 38         | 19.0%  |
| 39   | ownership_analysis         | 36         | 18.0%  |
| 40   | code_summary               | 33         | 16.5%  |
| 41   | graph_rag                  | 32         | 16.0%  |
| 42   | cross_repo_analysis        | 32         | 16.0%  |
| 43   | coverage_map               | 29         | 14.5%  |
| 44   | coverage_gap               | 29         | 14.5%  |
| 45   | pre_change_context         | 28         | 14.0%  |
| 46   | change_intent              | 26         | 13.0%  |
| 47   | test_impact                | 24         | 12.0%  |
| 48   | lsp_integration            | 22         | 11.0%  |
| 49   | before_edit_contract       | 21         | 10.5%  |

---

## 3. 与"代码图谱/调用链/符号索引/AST解析/tree-sitter/代码理解/代码搜索"相关的仓库列表

匹配能力标签：`call_graph`、`symbol_index`、`tree_sitter_ast`、`semantic_search`、`knowledge_graph`、`graph_rag`、`dependency_analysis`、`impact_analysis`、`repo_map`、`module_map`、`cross_repo_analysis`、`rag`

**共匹配 169 个仓库**。以下列出具有核心能力（call_graph + symbol_index + tree_sitter_ast 三项同时检测到）的高分仓库：

| repo_name                     | category                                                  | 检测到的核心能力                                                                                                                                                             | relevance_score |
| ----------------------------- | --------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------- |
| aovestdipaperino__tokensave   | AI code-intelligence MCP server                           | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map, rag, cross_repo_analysis | 0.97            |
| ArchCodexOrg__archcodex       | AI developer-tool / architecture governance               | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 0.97            |
| ashfordeOU__grasp             | AI Code Intelligence Platform                             | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 0.96            |
| Ataraxy-Labs__sem             | AI developer tool / code intelligence                     | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 0.96            |
| bartolli__codanna             | AI code-intelligence MCP developer tool                   | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 0.96            |
| Bpolat0__atlasmemory          | AI codebase memory and code-intelligence tool             | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 0.96            |
| aaif-goose__goose             | MCP-native AI developer agent                             | call_graph, symbol_index, tree_sitter_ast, knowledge_graph, semantic_search                                                                                                  | 0.94            |
| AustinSchoen__codebase-intel  | AI code intelligence MCP server                           | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 0.94            |
| ArchiCore-Team__archicore     | code_intelligence_cli                                     | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 0.93            |
| AMisljenovic__loom            | ai_coding_agent_ide_extension                             | call_graph, symbol_index, tree_sitter_ast, semantic_search                                                                                                                   | 0.93            |
| charmbracelet__crush          | AI coding assistant                                       | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 0.93            |
| Ataraxy-Labs__inspect         | Entity-level AI code review and change impact analysis    | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 0.93            |
| anujkamaljain__CodeChronicle  | AI codebase-intelligence IDE plugin                       | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 0.93            |
| 3301x2__MaestroNexus          | AI Code Intelligence MCP Tool                             | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag                                                                                       | 9.6 (0-10制)    |
| AnirudPaul__yapdex            | local_mcp_code_intelligence                               | call_graph, symbol_index, tree_sitter_ast, semantic_search                                                                                                                   | 0.90            |
| aeroxy__ast-bro               | AI code-intelligence CLI                                  | call_graph, symbol_index, tree_sitter_ast, semantic_search                                                                                                                   | 0.94            |
| AbdullahBakir97__DevTrust     | AI developer-tool / code-intelligence trust stack         | call_graph, symbol_index, tree_sitter_ast                                                                                                                                    | 0.92            |
| AniketS01__Impact-Mapper      | static_impact_analysis_cli                                | call_graph, symbol_index, knowledge_graph                                                                                                                                    | 0.84            |
| adgk2349__FlowMap             | code_intelligence_graph_impact_analysis                   | call_graph, symbol_index, knowledge_graph                                                                                                                                    | 0.87            |
| 0x5457__ts-index              | code_intelligence_mcp_server                              | symbol_index, tree_sitter_ast, semantic_search                                                                                                                               | 8.4 (0-10制)    |
| afnanenayet__diffsitter       | ast_semantic_diff_and_local_code_intelligence_cli         | tree_sitter_ast, symbol_index                                                                                                                                                | 0.84            |
| alex4o__code-agent            | AI codebase context compression and MCP code intelligence | tree_sitter_ast, symbol_index                                                                                                                                                | 0.83            |
| chunkhound__chunkhound        | code-intelligence                                         | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag                                                                                       | 9.4 (0-10制)    |
| fallow-rs__fallow             | code-intelligence                                         | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.4 (0-10制)    |
| danielbushman__crewchief      | ai-code-intelligence                                      | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.4 (0-10制)    |
| danieliser__tessera           | code-intelligence-mcp                                     | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.4 (0-10制)    |
| DariuszNewecki__CORE          | ai-governance-code-intelligence                           | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.4 (0-10制)    |
| davide-desio-eleva__kirograph | ai_code_intelligence_mcp                                  | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.4 (0-10制)    |
| duriantaco__skylos            | code-intelligence-security-cli                            | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.4 (0-10制)    |
| iflow-mcp__gladego-index1     | ai_code_intelligence_memory                               | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.4 (0-10制)    |
| illydynamics__qonqrete        | ai_developer_tool                                         | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.4 (0-10制)    |
| KevinRabun__judges            | AI-Code-Review-MCP                                        | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.4 (0-10制)    |
| Muvon__octocode               | code-intelligence                                         | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.4 (0-10制)    |
| nyxCore-Systems__LIP          | code-intelligence-daemon                                  | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.4 (0-10制)    |
| scanadi__synaptiq             | code_intelligence                                         | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.4 (0-10制)    |
| unfault__unlost               | AI Code Memory                                            | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.1 (0-10制)    |
| quangdang46__ms               | ai_developer_tool                                         | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.2 (0-10制)    |
| hieuchaydi__RepoBrain         | ai-code-intelligence                                      | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.2 (0-10制)    |
| iliaal__codesage              | code-intelligence                                         | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.2 (0-10制)    |
| prashantsinghmangat__Indexa   | AI_code_intelligence                                      | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.2 (0-10制)    |
| hptbee__b3_mcp                | code-intelligence-mcp                                     | call_graph, symbol_index, tree_sitter_ast, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                           | 9.2 (0-10制)    |

> 完整 169 个仓库还包括：ccantynz-alt__Gluecron.com、cavenine__ctxpp、caviraoss__openmemory、Bhavikupadhyay__coverage-agent、bettyabay__Codebase-Understanding-Agent、Beer-Bears__scaffold、bazfer__stud-finder、baikaishuipp__jcci、bntvllnt__codebase-intelligence、bonigarcia__context-engineering、bobbydeveaux__cerebra、can1357__smgrep、cdklabs__cdk-nag、chanhx__crabviz、circlemind-ai__fast-graphrag、Cluster444__agentic、cocoindex-io__cocoindex-code、coded-devs__lineageguard、Cre4T3Tiv3__gitvoyant、cs-au-dk__jelly、curtisdery__cortex、cybernetix-lab__moss-harness、dailephd__my-dev-kit、DancingLightStudios__ariadex、dandyArise__RepoLens、daneb__tecr、darklordVirtual__REMORA、deepflowio__deepflow、dgtalbug__dextree、dip497__onelens、dirac-run__dirac、djinn-soul__CytoScnPy、duriantaco__gitgrapher、e7nd7r__gnapsis、eastlondoner__vibe-tools、entrepeneur4lyf__code-graph-mcp、etcircle__cga、etinpres__mindvault、event-catalog__eventcatalog、explyt__spring-plugin、Fanaperana__adaptive-codegraph、faramesh__faramesh-core、featureform__enrichmcp、fjb040911__ai-rules、fluffypony__mcp-code-indexer、forloopcodes__contextplus、fulminate-io__knowledge-mcp、g0GobliN__reality-map、garrytan__gstack、gastownhall__gastown、germpharm__charter、getzep__graphiti、GH05TCREW__pentestagent、giancarloerra__socraticode、gkatte__codemesh、glommer__codemogger、glorynguyen__ollama-code-review、GoPlusSecurity__agentguard、github__gh-aw-mcpg、gortexhq__vscode-gortex、Govcraft__rust-docs-mcp-server、grahambrooks__symgraph、graph-memory__graphmemory、GreatScottyMac__context-portal、GreatScottyMac__roo-code-memory-bank、GulumseKadin__pr-impact-analyzer、guyowen__typegraph-mcp、HarshalRathore__code-intel-mcp、HeadyZhang__agent-audit、Helweg__opencode-codebase-index、HenryLok0__CodeState、HeroZ-Dodge__android-code-index、heymrun__heym、hookdeck__hookdeck-cli、husnainpk__SymDex、Huzefaaa2__cavra、Hyperion-GPU__ProofFlow-v0.1、interaction-dynamics__features、imtt-dev__steer、invariantlabs-ai__invariant、iwe-org__iwe、jagmarques__asqav-sdk、janreges__ai-distiller、JaredStewart__coderlm、jayu__rev-dep、joshua-light__resharper-mcp、Ivan825__pr-sentinel、Michaol__RustRAG、prateekgaurdev__repo-view、PrismorSec__immunity-agent、Regsorm__code-index-mcp、Rootly-AI-Labs__rootly-graphify-importer、salvo10f__godotiq、sandy-sachin7__contextd-vscode、SarthakMogane__GraphRAG-for-codebase-understanding、sdkks__symbol-index、siy__ndx、sjr27-maker__Impact-Lens、socprime__AIDR-Bastion 等。

---

## 4. 与"Semgrep/静态分析/缺陷检测/代码审查"相关的仓库列表

匹配能力标签：`security_scanning`、`code_quality`、`review_readiness`、`rule_packs`、`bug_case_rules`、`coding_guardrails`、`hard_constraints`、`diagnosability_rules`、`preflight_check`

**共匹配 128 个仓库**。以下列出具有多项静态分析/审查能力的代表性仓库：

| repo_name                          | category                                               | 检测到的核心能力                                                                                                                                          | relevance_score |
| ---------------------------------- | ------------------------------------------------------ | --------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------- |
| aovestdipaperino__tokensave        | AI code-intelligence MCP server                        | security_scanning, code_quality, review_readiness, rule_packs, bug_case_rules, coding_guardrails, hard_constraints, diagnosability_rules, preflight_check | 0.97            |
| ArchCodexOrg__archcodex            | AI developer-tool / architecture governance            | security_scanning, code_quality, review_readiness, rule_packs, coding_guardrails, hard_constraints, diagnosability_rules, preflight_check                 | 0.97            |
| ashfordeOU__grasp                  | AI Code Intelligence Platform                          | security_scanning, code_quality, review_readiness, rule_packs, coding_guardrails, hard_constraints, diagnosability_rules, preflight_check                 | 0.96            |
| Ataraxy-Labs__sem                  | AI developer tool / code intelligence                  | security_scanning, code_quality, review_readiness, rule_packs, bug_case_rules, coding_guardrails, hard_constraints, diagnosability_rules                  | 0.96            |
| bartolli__codanna                  | AI code-intelligence MCP developer tool                | security_scanning, code_quality, review_readiness, rule_packs, coding_guardrails, hard_constraints, diagnosability_rules                                  | 0.96            |
| Bpolat0__atlasmemory               | AI codebase memory and code-intelligence tool          | security_scanning, code_quality, review_readiness, rule_packs, coding_guardrails, hard_constraints, diagnosability_rules                                  | 0.96            |
| Ataraxy-Labs__inspect              | Entity-level AI code review and change impact analysis | security_scanning, code_quality, review_readiness, rule_packs, coding_guardrails, hard_constraints, diagnosability_rules                                  | 0.93            |
| ArchiCore-Team__archicore          | code_intelligence_cli                                  | security_scanning, code_quality, review_readiness, rule_packs, coding_guardrails, hard_constraints, diagnosability_rules                                  | 0.93            |
| aaif-goose__goose                  | MCP-native AI developer agent                          | security_scanning, code_quality, review_readiness, rule_packs, bug_case_rules, coding_guardrails                                                          | 0.94            |
| AbdullahBakir97__DevTrust          | AI developer-tool / code-intelligence trust stack      | security_scanning, code_quality, review_readiness, rule_packs, coding_guardrails                                                                          | 0.92            |
| anushreebhargava14__blast-radius   | ai_pr_review_risk_intelligence                         | review_readiness, code_quality                                                                                                                            | 0.74            |
| AMisljenovic__loom                 | ai_coding_agent_ide_extension                          | rule_packs, review_readiness, coding_guardrails                                                                                                           | 0.93            |
| andreaswasita__copilot-agents-dojo | AI Agent Workflow & Memory Infrastructure              | rule_packs, bug_case_rules, review_readiness, security_scanning                                                                                           | 0.82            |
| archondevio__archondev             | AI development governance CLI                          | rule_packs, review_readiness, bug_case_rules, coding_guardrails                                                                                           | 0.88            |
| Arvo-AI__aurora                    | AI incident response / SRE RCA agent                   | rule_packs, bug_case_rules, security_scanning                                                                                                             | 0.86            |
| ashfordeOU__grasp                  | AI Code Intelligence Platform                          | review_readiness, rule_packs, security_scanning                                                                                                           | 0.96            |
| Ataraxy-Labs__inspect              | Entity-level AI code review and change impact analysis | review_readiness, rule_packs, security_scanning                                                                                                           | 0.93            |
| aiming-lab__AutoHarness            | ai_agent_governance_middleware                         | rule_packs, security_scanning                                                                                                                             | 0.86            |
| akashagalave__CodeSentinel-AI      | AI Code Review Platform                                | security_scanning                                                                                                                                         | 0.88            |
| alibaba__OpenSandbox               | AI agent sandbox runtime                               | rule_packs, bug_case_rules, security_scanning                                                                                                             | 0.78            |
| atomsai__pipguard                  | security_scanning                                      | security_scanning                                                                                                                                         | 0.62            |
| backbay-labs__clawdstrike          | ai_security_policy_engine                              | security_scanning, code_quality, review_readiness                                                                                                         | 8.7 (0-10制)    |
| can1357__smgrep                    | semantic_code_search                                   | rule_packs, review_readiness (Semgrep-like 语义代码搜索)                                                                                                  | 0.88            |
| cdklabs__cdk-nag                   | iac_security_compliance_scanner                        | security_scanning, rule_packs                                                                                                                             | 0.56            |
| cs-au-dk__jelly                    | static-analysis-security                               | security_scanning, code_quality, rule_packs                                                                                                               | 8.3 (0-10制)    |
| djinn-soul__CytoScnPy              | static-analysis                                        | code_quality, security_scanning                                                                                                                           | 9.0 (0-10制)    |
| duriantaco__skylos                 | code-intelligence-security-cli                         | security_scanning, code_quality, rule_packs                                                                                                               | 9.4 (0-10制)    |
| glorynguyen__ollama-code-review    | ai-code-review                                         | review_readiness, code_quality                                                                                                                            | 9.1 (0-10制)    |
| KevinRabun__judges                 | AI-Code-Review-MCP                                     | review_readiness, rule_packs, security_scanning, code_quality                                                                                             | 9.4 (0-10制)    |
| PrismorSec__immunity-agent         | AI Agent Security                                      | security_scanning, code_quality                                                                                                                           | 8.2 (0-10制)    |
| socprime__AIDR-Bastion             | AI-Security-Gateway                                    | security_scanning, code_quality                                                                                                                           | 7.2 (0-10制)    |
| HeadyZhang__agent-audit            | ai-agent-security-scanner                              | security_scanning, code_quality                                                                                                                           | 8.6 (0-10制)    |
| GoPlusSecurity__agentguard         | ai-agent-security                                      | security_scanning, code_quality                                                                                                                           | 8.2 (0-10制)    |
| GH05TCREW__pentestagent            | ai_security_agent                                      | security_scanning                                                                                                                                         | 7.2 (0-10制)    |

> 注：can1357__smgrep 是与 Semgrep 直接相关的项目（smgrep = semantic grep），实现了语义代码搜索与规则匹配。cs-au-dk__jelly 和 djinn-soul__CytoScnPy 是纯静态分析类项目。

---

## 5. 与"注释/文档生成/代码摘要"相关的仓库列表

匹配能力标签：`code_summary`、`api_documentation`

**共匹配 53 个仓库**。以下列出全部检测到 code_summary 和/或 api_documentation 的仓库：

| repo_name                                 | category                                         | 检测到的能力                    | relevance_score |
| ----------------------------------------- | ------------------------------------------------ | ------------------------------- | --------------- |
| 3301x2__MaestroNexus                      | AI Code Intelligence MCP Tool                    | code_summary, api_documentation | 9.6             |
| aaif-goose__goose                         | MCP-native AI developer agent                    | code_summary, api_documentation | 0.94            |
| aeroxy__ast-bro                           | AI code-intelligence CLI                         | code_summary                    | 0.94            |
| AI45Lab__AgentDoG                         | ai_agent_safety_alignment_framework              | api_documentation               | 0.64            |
| alibaba__OpenSandbox                      | AI agent sandbox runtime                         | api_documentation               | 0.78            |
| aliyun__alibabacloud-ack-mcp-server       | kubernetes_aiops_mcp_server                      | api_documentation               | 0.72            |
| AnirudPaul__yapdex                        | local_mcp_code_intelligence                      | code_summary                    | 0.90            |
| anujkamaljain__CodeChronicle              | AI codebase-intelligence IDE plugin              | code_summary                    | 0.93            |
| aovestdipaperino__tokensave               | AI code-intelligence MCP server                  | code_summary, api_documentation | 0.97            |
| ArchCodexOrg__archcodex                   | AI developer-tool / architecture governance      | code_summary, api_documentation | 0.97            |
| ArchiCore-Team__archicore                 | code_intelligence_cli                            | code_summary, api_documentation | 0.93            |
| arisvas4__codified-context-infrastructure | ai_context_infrastructure                        | code_summary                    | 8.2             |
| Arvo-AI__aurora                           | AI incident response / SRE RCA agent             | api_documentation, code_summary | 0.86            |
| ascending-llc__jarvis-registry            | MCP Gateway / AI Agent Registry                  | api_documentation               | 0.86            |
| ashfordeOU__grasp                         | AI Code Intelligence Platform                    | code_summary                    | 0.96            |
| AustinSchoen__codebase-intel              | AI code intelligence MCP server                  | code_summary                    | 0.94            |
| backbay-labs__clawdstrike                 | ai_security_policy_engine                        | api_documentation               | 8.7             |
| bartolli__codanna                         | AI code-intelligence MCP developer tool          | code_summary, api_documentation | 0.96            |
| Bhavikupadhyay__coverage-agent            | coverage_driven_ai_test_generation               | code_summary                    | 0.86            |
| bettyabay__Codebase-Understanding-Agent   | ai_codebase_intelligence                         | code_summary                    | 0.89            |
| bonigarcia__context-engineering           | context_engineering_example_suite                | api_documentation, code_summary | 0.78            |
| BoundaryML__baml                          | AI developer tool / LLM workflow DSL             | code_summary, api_documentation | 0.90            |
| Bpolat0__atlasmemory                      | AI codebase memory and code-intelligence tool    | code_summary                    | 0.96            |
| cavenine__ctxpp                           | AI code-intelligence MCP server                  | code_summary                    | 0.94            |
| caviraoss__openmemory                     | AI memory engine                                 | api_documentation               | 0.76            |
| ccantynz-alt__Gluecron.com                | AI-native Git forge / code intelligence platform | code_summary, api_documentation | 0.95            |
| charmbracelet__crush                      | AI coding assistant                              | api_documentation               | 0.93            |
| chunkhound__chunkhound                    | code-intelligence                                | api_documentation               | 9.4             |
| danielbushman__crewchief                  | ai-code-intelligence                             | api_documentation               | 9.4             |
| darklordVirtual__REMORA                   | ai-action-governance                             | api_documentation               | 9.0             |
| davide-desio-eleva__kirograph             | ai_code_intelligence_mcp                         | code_summary                    | 9.4             |
| deepflowio__deepflow                      | observability_platform                           | api_documentation               | 7.6             |
| explyt__spring-plugin                     | ide-code-intelligence                            | api_documentation               | 9.0             |
| event-catalog__eventcatalog               | architecture-governance                          | api_documentation               | 8.3             |
| fallow-rs__fallow                         | code-intelligence                                | api_documentation               | 9.4             |
| featureform__enrichmcp                    | mcp-framework                                    | api_documentation               | 7.2             |
| fluffypony__mcp-code-indexer              | code-intelligence-mcp-server                     | api_documentation               | 9.1             |
| fulminate-io__knowledge-mcp               | ai-code-intelligence-mcp                         | code_summary                    | 9.6             |
| garrytan__gstack                          | ai_developer_workflow                            | api_documentation               | 9.0             |
| getzep__graphiti                          | graph-rag-memory                                 | code_summary, api_documentation | 8.4             |
| graph-memory__graphmemory                 | ai-code-intelligence-mcp                         | api_documentation               | 9.4             |
| HenryLok0__CodeState                      | code-intelligence-cli                            | api_documentation               | 8.0             |
| heymrun__heym                             | ai_workflow_automation_platform                  | api_documentation               | 7.4             |
| Govcraft__rust-docs-mcp-server            | documentation-rag-mcp                            | (文档RAG MCP，与文档生成强相关) | 7.6             |
| janreges__ai-distiller                    | code-intelligence                                | (代码蒸馏/摘要)                 | 9.2             |

> 注：Govcraft__rust-docs-mcp-server 虽然未检测到 code_summary/api_documentation 标签，但其 category 为 "documentation-rag-mcp"，是专门的文档 RAG 工具。janreges__ai-distiller 名为 "ai-distiller"（AI 蒸馏器），与代码摘要/精炼高度相关。

---

## 6. 与"版本管理/代码历史/变更追踪"相关的仓库列表

匹配能力标签：`git_integration`、`pre_change_context`、`change_intent`、`legacy_compatibility`、`incremental_index`

**共匹配 145 个仓库**。以下列出同时具有 git_integration + pre_change_context 或 git_integration + change_intent 的高分仓库：

| repo_name                                 | category                                               | 检测到的核心能力                                                                            | relevance_score |
| ----------------------------------------- | ------------------------------------------------------ | ------------------------------------------------------------------------------------------- | --------------- |
| aovestdipaperino__tokensave               | AI code-intelligence MCP server                        | git_integration, pre_change_context, change_intent, legacy_compatibility, incremental_index | 0.97            |
| ArchCodexOrg__archcodex                   | AI developer-tool / architecture governance            | git_integration, pre_change_context, change_intent, legacy_compatibility, incremental_index | 0.97            |
| ashfordeOU__grasp                         | AI Code Intelligence Platform                          | git_integration, pre_change_context, legacy_compatibility, incremental_index                | 0.96            |
| Ataraxy-Labs__sem                         | AI developer tool / code intelligence                  | git_integration, pre_change_context, change_intent, legacy_compatibility, incremental_index | 0.96            |
| bartolli__codanna                         | AI code-intelligence MCP developer tool                | git_integration, pre_change_context, legacy_compatibility, incremental_index                | 0.96            |
| Bpolat0__atlasmemory                      | AI codebase memory and code-intelligence tool          | git_integration, pre_change_context, legacy_compatibility, incremental_index                | 0.96            |
| ArchiCore-Team__archicore                 | code_intelligence_cli                                  | git_integration, pre_change_context, change_intent, legacy_compatibility, incremental_index | 0.93            |
| Ataraxy-Labs__inspect                     | Entity-level AI code review and change impact analysis | git_integration, change_intent, pre_change_context, legacy_compatibility, incremental_index | 0.93            |
| anujkamaljain__CodeChronicle              | AI codebase-intelligence IDE plugin                    | git_integration, pre_change_context (名称 CodeChronicle 暗示代码历史)                       | 0.93            |
| aaif-goose__goose                         | MCP-native AI developer agent                          | git_integration, legacy_compatibility                                                       | 0.94            |
| 3301x2__MaestroNexus                      | AI Code Intelligence MCP Tool                          | git_integration, pre_change_context                                                         | 9.6             |
| AbdullahBakir97__DevTrust                 | AI developer-tool / code-intelligence trust stack      | git_integration, legacy_compatibility                                                       | 0.92            |
| adgk2349__FlowMap                         | code_intelligence_graph_impact_analysis                | git_integration                                                                             | 0.87            |
| adrianczuczka__mason                      | AI code context MCP server                             | git_integration                                                                             | 8.6             |
| afnanenayet__diffsitter                   | ast_semantic_diff_and_local_code_intelligence_cli      | git_integration (语义diff工具，与变更追踪强相关)                                            | 0.84            |
| aiming-lab__AutoHarness                   | ai_agent_governance_middleware                         | git_integration                                                                             | 0.86            |
| akashagalave__CodeSentinel-AI             | AI Code Review Platform                                | git_integration                                                                             | 0.88            |
| alibaba__OpenSandbox                      | AI agent sandbox runtime                               | git_integration                                                                             | 0.78            |
| AMisljenovic__loom                        | ai_coding_agent_ide_extension                          | git_integration                                                                             | 0.93            |
| AmoyLab__Unla                             | mcp_gateway                                            | git_integration                                                                             | 0.86            |
| andreaswasita__copilot-agents-dojo        | AI Agent Workflow & Memory Infrastructure              | git_integration                                                                             | 0.82            |
| AnirudPaul__yapdex                        | local_mcp_code_intelligence                            | git_integration                                                                             | 0.90            |
| anushreebhargava14__blast-radius          | ai_pr_review_risk_intelligence                         | git_integration (PR影响半径分析)                                                            | 0.74            |
| archondevio__archondev                    | AI development governance CLI                          | git_integration, pre_change_context                                                         | 0.88            |
| arisvas4__codified-context-infrastructure | ai_context_infrastructure                              | git_integration                                                                             | 8.2             |
| Arvo-AI__aurora                           | AI incident response / SRE RCA agent                   | git_integration                                                                             | 0.86            |
| AsiaOstrich__EngramGraph                  | AI developer tool / code intelligence graph memory     | git_integration                                                                             | 0.92            |
| Atharva-Jayappa__blast-scope              | ai_agent_command_safety                                | git_integration, change_intent                                                              | 0.88            |
| backbay-labs__clawdstrike                 | ai_security_policy_engine                              | git_integration                                                                             | 8.7             |
| baikaishuipp__jcci                        | code-intelligence-impact-analysis                      | git_integration (代码变更影响分析)                                                          | 0.82            |
| bazfer__stud-finder                       | code_risk_scoring_cli                                  | git_integration                                                                             | 0.78            |
| bettyabay__Codebase-Understanding-Agent   | ai_codebase_intelligence                               | git_integration                                                                             | 0.89            |
| bntvllnt__codebase-intelligence           | AI codebase intelligence                               | git_integration, change_intent                                                              | 0.92            |
| bonigarcia__context-engineering           | context_engineering_example_suite                      | git_integration                                                                             | 0.78            |
| bobbydeveaux__cerebra                     | AI developer-tool / code-intelligence                  | git_integration                                                                             | 0.92            |
| BoundaryML__baml                          | AI developer tool / LLM workflow DSL                   | git_integration                                                                             | 0.90            |
| can1357__smgrep                           | semantic_code_search                                   | git_integration                                                                             | 0.88            |
| ccantynz-alt__Gluecron.com                | AI-native Git forge / code intelligence platform       | git_integration, pre_change_context (AI原生Git forge)                                       | 0.95            |
| Cre4T3Tiv3__gitvoyant                     | code-intelligence                                      | git_integration (名称 gitvoyant 暗示 Git 可视化)                                            | 8.0             |
| duriantaco__gitgrapher                    | code-intelligence                                      | git_integration (名称 gitgrapher 暗示 Git 图谱)                                             | 9.0             |
| GulumseKadin__pr-impact-analyzer          | ide_code_intelligence                                  | git_integration (PR影响分析)                                                                | 8.4             |
| Ivan825__pr-sentinel                      | pr-risk-intelligence                                   | git_integration (PR风险哨兵)                                                                | 8.4             |

> 注：特别值得关注的版本管理/变更追踪类项目：
> - **ccantynz-alt__Gluecron.com**：定位为 "AI-native Git forge / code intelligence platform"，是 AI 原生的 Git 平台
> - **Cre4T3Tiv3__gitvoyant**：名称暗示 Git 可视化/预测
> - **duriantaco__gitgrapher**：名称暗示 Git 图谱生成
> - **afnanenayet__diffsitter**：基于 tree-sitter 的语义 diff 工具
> - **anushreebhargava14__blast-radius** / **GulumseKadin__pr-impact-analyzer** / **Ivan825__pr-sentinel**：PR 影响/风险分析
> - **baikaishuipp__jcci**：代码变更影响分析
>
> 完整 145 个仓库还包括：charmbracelet__crush、chunkhound__chunkhound、circlemind-ai__fast-graphrag、Cluster444__agentic、cocoindex-io__cocoindex-code、coded-devs__lineageguard、cs-au-dk__jelly、curtisdery__cortex、cybernetix-lab__moss-harness、dailephd__my-dev-kit、DancingLightStudios__ariadex、dandyArise__RepoLens、daneb__tecr、danielbushman__crewchief、danieliser__tessera、DariuszNewecki__CORE、darklordVirtual__REMORA、davide-desio-eleva__kirograph、deepflowio__deepflow、dgtalbug__dextree、dip497__onelens、dirac-run__dirac、djinn-soul__CytoScnPy、duriantaco__skylos、e7nd7r__gnapsis、eastlondoner__vibe-tools、entrepeneur4lyf__code-graph-mcp、etcircle__cga、etinpres__mindvault、event-catalog__eventcatalog、explyt__spring-plugin、fallow-rs__fallow、Fanaperana__adaptive-codegraph、faramesh__faramesh-core、featureform__enrichmcp、fluffypony__mcp-code-indexer、forloopcodes__contextplus、fulminate-io__knowledge-mcp、g0GobliN__reality-map、garrytan__gstack、gastownhall__gastown、germpharm__charter、getzep__graphiti、GH05TCREW__pentestagent、giancarloerra__socraticode、gkatte__codemesh、glommer__codemogger、glorynguyen__ollama-code-review、GoPlusSecurity__agentguard、github__gh-aw-mcpg、gortexhq__vscode-gortex、Govcraft__rust-docs-mcp-server、grahambrooks__symgraph、graph-memory__graphmemory、GreatScottyMac__context-portal、GreatScottyMac__roo-code-memory-bank、guyowen__typegraph-mcp、HarshalRathore__code-intel-mcp、HeadyZhang__agent-audit、Helweg__opencode-codebase-index、HenryLok0__CodeState、HeroZ-Dodge__android-code-index、heymrun__heym、hieuchaydi__RepoBrain、hiranp__hief、hkevin01__automated-traceability-requirements-intelligence、Hmbown__aleph、hookdeck__hookdeck-cli、hptbee__b3_mcp、husnainpk__SymDex、Huzefaaa2__cavra、Hyperion-GPU__ProofFlow-v0.1、iflow-mcp__gladego-index1、iliaal__codesage、illdynamics__qonqrete、imtt-dev__steer、interaction-dynamics__features、invariantlabs-ai__invariant、iwe-org__iwe、jagmarques__asqav-sdk、janreges__ai-distiller、JaredStewart__coderlm、jayu__rev-dep、jeewandaniel__wp-plugin-compliance-checker、joshua-light__resharper-mcp、KevinRabun__judges、Michaol__RustRAG、Muvon__octocode、nyxCore-Systems__LIP、prashantsinghmangat__Indexa、prateekgaurdev__repo-view、PrismorSec__immunity-agent、Regsorm__code-index-mcp、Rootly-AI-Labs__rootly-graphify-importer、salvo10f__godotiq、sandy-sachin7__contextd-vscode、SarthakMogane__GraphRAG-for-codebase-understanding、scanadi__synaptiq、sdkks__symbol-index、siy__ndx、sjr27-maker__Impact-Lens、socprime__AIDR-Bastion、unfault__unlost、quangdang46__ms 等。

---

## 补充说明

1. **分数制不一致问题**：约 100 个仓库的 `relevance_score` 使用 0-1 分制（如 0.97），约 77 个仓库使用 0-10 分制（如 9.4）。这是数据采集时的口径差异，对比时应注意区分。

2. **能力标签体系**：共有 49 个标准化能力标签。红海功能（覆盖率 > 50%）为 cli_tool、runtime_entrypoints、mcp_server、repo_map、local_mcp、symbol_index、git_integration、multilang_support、code_quality。蓝海机会（覆盖率 < 25%）包括 before_edit_contract、lsp_integration、test_impact、change_intent、pre_change_context、coverage_map、coverage_gap、graph_rag、cross_repo_analysis、code_summary、ownership_analysis、database_risk、diagnosability_rules、preflight_check、api_documentation。

3. **关键文件路径**：
   - 精炼汇总报告：`c:\git_work\ai-code-review\ai-competitor-radar\workspace\profiles\ai_coding_agent_safety\reports\analysis_results\refined_summary_report.md`
   - 汇总报告：`c:\git_work\ai-code-review\ai-competitor-radar\workspace\profiles\ai_coding_agent_safety\reports\analysis_results\summary_report.md`
   - JSON 报告目录：`c:\git_work\ai-code-review\ai-competitor-radar\workspace\profiles\ai_coding_agent_safety\reports\analysis_results\llm_reports\`
        
          
数据全部收集完成。现在我基于 code_graph 的 **84 项能力清单** 和 **200 个仓库的完整报告** 做交叉对比分析。

---

## code_graph 与 200 个仓库的交叉对比分析

### 一、code_graph 完整能力清单（已实现 67 项）

| 层                | 已实现能力                                                    | 数量 |
| ----------------- | ------------------------------------------------------------- | ---- |
| A. 数据库基础设施 | SQLite 持久化、Schema v1-v4 迁移、多工作区、content_hash 去重 | 5    |
| B. 多语言解析     | 9 语言 tree-sitter、符号提取、模块路径推断、Cargo.toml 检测   | 6    |
| C. 调用图构建     | 四级解析策略、跨文件标记、版本化、9 语言调用过滤、拓扑深度    | 6    |
| D. 版本历史       | 文件/符号版本管理、diff 计算、删除标记、快照恢复              | 6    |
| E. 构建流水线     | 完整构建、并行解析、增量构建、单文件刷新、.callwardenignore   | 7    |
| F. 查询能力       | 统计/状态/拓扑/调用者/历史/变更/搜索/符号详情/模块图导出      | 10   |
| G. 注释管理       | 历史注释获取、单函数恢复、批量恢复、覆盖率统计                | 4    |
| H. Git 集成       | 历史导入、commit 查询、变更详情、符号 commit 历史、统计       | 6    |
| I. 代码度量       | 圈复杂度、扇入扇出、耦合度、健康检查、AI Agent 指引           | 8    |
| J. CLI/UI         | 彩色输出、进度条、Spinner、构建总结                           | 6    |
| K. i18n           | 中英文资源、占位符替换                                        | 3    |

### 二、与 code_graph 功能接近的仓库（按重叠度排序）

#### 第一梯队：高度重叠（core 三项 + 额外能力）

| 排名 | 仓库                                                                                                                                                                                | score | 与 code_graph 重叠的能力                                                                                                                                                                                                                                    | code_graph 缺失但对方有的                                 |
| ---- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------------------- |
| 1    | [tokensave](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/aovestdipaperino__tokensave.json) | 0.97  | call_graph, symbol_index, tree_sitter, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map, rag, cross_repo, sqlite_cache, incremental_index, token_optimization, git_integration, mcp_server, cli_tool | **向量嵌入、RAG、语义搜索、跨仓库、Token 账本、分支感知** |
| 2    | [archcodex](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/ArchCodexOrg__archcodex.json)     | 0.97  | call_graph, symbol_index, tree_sitter, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map, code_summary, api_documentation                                                                             | **向量嵌入、RAG、代码摘要、API 文档生成、架构注册表**     |
| 3    | [grasp](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/ashfordeOU__grasp.json)               | 0.96  | call_graph, symbol_index, tree_sitter, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map, code_summary                                                                                                | **向量嵌入、RAG、代码摘要**                               |
| 4    | [sem](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/Ataraxy-Labs__sem.json)                 | 0.96  | call_graph, symbol_index, tree_sitter, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map                                                                                                              | **向量嵌入、RAG、语义搜索**                               |
| 5    | [codanna](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/bartolli__codanna.json)             | 0.96  | call_graph, symbol_index, tree_sitter, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map, code_summary, api_documentation                                                                             | **向量嵌入、RAG、代码摘要**                               |
| 6    | [atlasmemory](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/Bpolat0__atlasmemory.json)      | 0.96  | call_graph, symbol_index, tree_sitter, semantic_search, knowledge_graph, graph_rag, dependency_analysis, impact_analysis, repo_map, module_map, code_summary                                                                                                | **向量嵌入、RAG、代码摘要**                               |

#### 第二梯队：中高度重叠

| 排名 | 仓库                                                                                                                                                                                     | score | 重叠核心                                                  | 差异化                            |
| ---- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | ----- | --------------------------------------------------------- | --------------------------------- |
| 7    | [inspect](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/Ataraxy-Labs__inspect.json)              | 0.93  | call_graph + symbol_index + tree_sitter + impact_analysis | **实体级代码审查 + 变更影响分析** |
| 8    | [archicore](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/ArchiCore-Team__archicore.json)        | 0.93  | 全套 code intelligence                                    | CLI 工具，定位类似                |
| 9    | [CodeChronicle](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/anujkamaljain__CodeChronicle.json) | 0.93  | call_graph + symbol_index + tree_sitter + git_integration | **IDE 插件 + 代码历史**           |
| 10   | [crush](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/charmbracelet__crush.json)                 | 0.93  | 全套 code intelligence                                    | **AI 编码助手（不只图谱）**       |
| 11   | [ast-bro](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/aeroxy__ast-bro.json)                    | 0.94  | tree_sitter + symbol_index + call_graph + code_summary    | **AST 浏览器 CLI**                |
| 12   | [loom](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/AMisljenovic__loom.json)                    | 0.93  | tree_sitter + symbol_index + call_graph + semantic_search | **IDE 扩展**                      |

#### 第三梯队：部分重叠但有独特角度

| 仓库                                                                                                                                                                                 | score | 重叠点                                      | 独特角度                                                       |
| ------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------ | ----- | ------------------------------------------- | -------------------------------------------------------------- |
| [diffsitter](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/afnanenayet__diffsitter.json)     | 0.84  | tree_sitter + symbol_index                  | **AST 语义 diff**（code_graph 只有 mtime 检测，没有语义 diff） |
| [Impact-Mapper](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/AniketS01__Impact-Mapper.json) | 0.84  | call_graph + knowledge_graph                | **静态影响分析 CLI**                                           |
| [FlowMap](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/adgk2349__FlowMap.json)              | 0.87  | call_graph + symbol_index + knowledge_graph | **图谱 + 影响分析**                                            |
| [smgrep](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/can1357__smgrep.json)                 | 0.88  | semantic_search + rule_packs                | **语义代码搜索**（Semgrep 变体）                               |
| [CodeState](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/HenryLok0__CodeState.json)         | 8.0   | code intelligence CLI                       | **代码状态管理**                                               |
| [symbol-index](file:///C:/git_work/ai-code-review/ai-competitor-radar/workspace/profiles/ai_coding_agent_safety/reports/analysis_results/llm_reports/sdkks__symbol-index.json)       | -     | symbol_index                                | **纯符号索引**（code_graph 的子集）                            |

### 三、code_graph 独有优势（200 仓库中无人覆盖）

| 独有能力                                | 覆盖率  | 说明                                                |
| --------------------------------------- | ------- | --------------------------------------------------- |
| **注释恢复**（git checkout 后恢复）     | **0%**  | 200 个仓库中没有任何一个实现此功能                  |
| **Semgrep 集成 + 缺陷知识库**           | **<2%** | 只有 smgrep 做语义搜索，但不是 Semgrep 集成         |
| **版本历史 + 删除标记**                 | **<5%** | 大多数只有当前快照，没有版本化                      |
| **圈复杂度 + 健康检查 + AI Agent 指引** | **<3%** | 只有 SonarQube 类工具做复杂度，但没有 AI Agent 指引 |
| **9 语言调用过滤**（标准库/外部依赖）   | **<5%** | 大多数不过滤标准库调用                              |
| **.callwardenignore**                   | **0%**  | 独有的忽略规则机制                                  |
| **i18n（中英文）**                      | **<2%** | 极少有代码工具做国际化                              |

### 四、code_graph 可以扩展过去的方向（基于已有能力）

#### 方向 1：从 call_graph 扩展到 impact_analysis（覆盖率 49%，但有差异化空间）

```text
code_graph 已有：
  ✅ call_graph（四级解析）
  ✅ get_call_chain_up（向上追踪 = 影响面分析）
  ✅ get_call_chain_down（向下追踪）
  ✅ get_impact（影响面分析）
  ✅ topological_order（拓扑排序）

扩展方向：
  → 跨层影响分析（代码层 → 数据库层 → API 层）
  → PR 级影响分析（git diff → 变更函数 → 影响面）
  → 变更风险评分（复杂度 × 影响面 × 耦合度）

竞品参考：
  - inspect（0.93）：实体级变更影响分析
  - Impact-Mapper（0.84）：静态影响分析 CLI
  - FlowMap（0.87）：图谱 + 影响分析
  - blast-radius（0.74）：PR 影响半径
  - jcci（0.82）：代码变更影响分析

code_graph 优势：已有调用图 + 拓扑排序 + 健康检查，
  只需加 git diff → 变更函数映射 → 影响面计算 → 风险评分
```

#### 方向 2：从 symbol_index + tree_sitter 扩展到 semantic_search / RAG（覆盖率 49%/34.5%）

```text
code_graph 已有：
  ✅ symbol_index（符号索引）
  ✅ tree_sitter AST
  ✅ search_symbols（名称模糊匹配）
  ✅ SQLite 持久化

扩展方向：
  → sqlite-vec 向量嵌入（你的差距分析报告已规划）
  → 语义搜索（自然语言 → 符号）
  → RAG 问答（自然语言 → 代码片段）

竞品参考：
  - tokensave（0.97）：vector_embedding + semantic_search + rag
  - archcodex（0.97）：vector_embedding + semantic_search + graph_rag
  - GraphRAG-for-codebase（-）：codebase + graphrag
  - chunkhound（9.4）：code intelligence + 向量

code_graph 优势：已有 SQLite + 符号索引，
  只需加 sqlite-vec 扩展 + 嵌入模型 + 语义查询层
```

#### 方向 3：从 git_integration + version_history 扩展到 code_summary / 文档生成（覆盖率 16.5%）

```text
code_graph 已有：
  ✅ git_integration（commit 历史 + 文件变更 + 符号变更历史）
  ✅ version_history（文件/符号版本管理）
  ✅ comment_detection（注释检测）
  ✅ comment_recovery（注释恢复）

扩展方向：
  → AI 生成函数/模块摘要（基于符号内容 + 调用关系 + 版本历史）
  → API 文档自动生成
  → 代码变更日志自动生成（从 git_symbol_changes）

竞品参考：
  - archcodex（0.97）：code_summary + api_documentation
  - codanna（0.96）：code_summary + api_documentation
  - Govcraft__rust-docs-mcp-server：documentation-rag-mcp
  - janreges__ai-distiller：代码蒸馏/摘要

code_graph 优势：已有注释恢复 + 版本历史 + 符号内容，
  只需加 LLM 摘要生成层（输入：符号内容 + 调用关系 → 输出：摘要）
```

#### 方向 4：从 code_metrics + health_check 扩展到 review_readiness / PR 风险评分（覆盖率 27%）

```text
code_graph 已有：
  ✅ cyclomatic_complexity（圈复杂度）
  ✅ coupling_analysis（耦合度）
  ✅ code_health_check（健康检查 + AI Agent 指引）
  ✅ check_file_health（单文件健康检查 + should_split_first）

扩展方向：
  → PR 风险评分（复杂度变化 × 影响面 × 耦合度 × 注释覆盖率）
  → Review Readiness Gate（达到什么标准才能提交 PR）
  → AI Agent 修改前风险评估（已有 agent_guidance，扩展为评分）

竞品参考：
  - inspect（0.93）：实体级代码审查
  - blast-radius（0.74）：PR 影响半径
  - pr-sentinel（8.4）：PR 风险哨兵
  - stud-finder（0.78）：代码风险评分 CLI
  - judges（9.4）：AI 代码审查 MCP

code_graph 优势：已有健康检查 + AI Agent 指引，
  只需加 PR diff 解析 + 风险评分算法 + 门禁逻辑
```

#### 方向 5：从 Semgrep + issues 扩展到 rule_packs / bug_case_rules（覆盖率 33%/25%）

```text
code_graph 已有：
  ✅ Semgrep 集成（扫描 + 存库 + 查询 + 统计）
  ✅ 内置 10 种缺陷类型（missing_comment/todo/unwrap/expect/panic/unsafe/hardcoded_path/hardcoded_url/magic_number/clone_heavy）
  ✅ 缺陷按函数关联

扩展方向：
  → 自定义规则包（rule_packs）管理
  → Bug 案例库（从历史 bug 提取规则）
  → 修复建议生成（基于缺陷类型 + 代码上下文）

竞品参考：
  - tokensave（0.97）：rule_packs + bug_case_rules
  - sem（0.96）：rule_packs + bug_case_rules
  - CORE（9.4）：historical_defect_memory
  - clawdstrike（8.7）：security_scanning + code_quality

code_graph 优势：已有 Semgrep + 缺陷检测，
  只需加规则包 CRUD + 历史缺陷关联 + 修复建议
```

#### 方向 6：从 call_filter + module_resolver 扩展到 cross_repo_analysis / Monorepo（覆盖率 16%）

```text
code_graph 已有：
  ✅ 9 语言调用过滤（标准库/外部依赖）
  ✅ module_resolver（模块路径解析）
  ✅ 多工作区管理

扩展方向：
  → 跨仓库符号解析（import 指向另一个仓库）
  → Monorepo 支持（多 Cargo.toml / package.json）
  → 仓库间依赖图

竞品参考：
  - tokensave（0.97）：cross_repo_analysis
  - Sourcegraph：跨仓库代码搜索
  - GraphRAG：跨仓库知识图谱

code_graph 优势：已有调用过滤 + 多工作区，
  只需加跨工作区符号解析 + 仓库间依赖映射
```

### 五、竞争位置图

```text
                    AI 辅助能力（RAG/向量/摘要）→
                    ↑
                    │
        tokensave ● │  ● archcodex
        grasp     ● │  ● codanna
                    │  ● atlasmemory
                    │
    ─────────────┼─────────────────→ 结构化图谱能力（AST/调用图/符号）
                    │
        sem      ● │
        inspect  ● │
                    │
        ★ code_graph（你的位置）
        ├── 已有：9语言AST + 调用图 + 版本历史 + 注释恢复 + Semgrep + 度量
        ├── 独有：注释恢复 + Semgrep集成 + 健康检查 + AI指引
        └── 缺失：向量嵌入 + RAG + 代码摘要 + 语义搜索
                    │
                    │
                    ↓
```

**你的位置在左下偏上**：结构化图谱能力强（接近 tokensave），但 AI 辅助能力弱（没有向量/RAG/摘要）。

### 六、总结：该补什么、不该补什么

#### 必须补（否则被 tokensave/archcodex 甩开）

| 优先级 | 补什么              | 基于什么扩展                          | 预期工作量 |
| ------ | ------------------- | ------------------------------------- | ---------- |
| **P0** | sqlite-vec 向量嵌入 | SQLite 持久化 + symbol_index          | 2-3 天     |
| **P0** | 语义搜索 MCP 工具   | search_symbols + 向量                 | 1-2 天     |
| **P1** | AI 代码摘要生成     | 符号内容 + 调用关系 + version_history | 3-5 天     |

#### 不补（红海，tokensave 已做到天花板）

| 不补什么          | 原因                                |
| ----------------- | ----------------------------------- |
| 完整 RAG 问答系统 | GraphRAG/fast-graphrag 已有成熟实现 |
| Repo Map 可视化   | 63% 覆盖率，太多人做                |
| Token 节省账本    | tokensave 独有，跟随无意义          |

#### 深化独有（你的护城河，无人覆盖）

| 深化什么                   | 当前状态 | 扩展方向                                      |
| -------------------------- | -------- | --------------------------------------------- |
| **注释恢复**               | ✅ 已实现 | → AI 注释管家（检测过期 + 增量更新 + 多语言） |
| **Semgrep + 缺陷知识库**   | ✅ 已实现 | → 规则包管理 + Bug 案例 + 修复建议            |
| **代码健康检查 + AI 指引** | ✅ 已实现 | → PR 风险评分 + Review Readiness Gate         |
| **版本历史 + 删除标记**    | ✅ 已实现 | → 代码演化分析（变更频率 × 缺陷关联）         |
| **9 语言调用过滤**         | ✅ 已实现 | → 跨仓库调用解析 + Monorepo                   |

### 七、一句话结论

**code_graph 在 200 个仓库中处于"结构化图谱接近 tokensave，但 AI 辅助层缺失"的位置。你的三大独有优势（注释恢复、Semgrep 集成、健康检查+AI 指引）在 200 个仓库中无人覆盖。下一步应该：补 sqlite-vec 向量嵌入（2-3 天）+ 语义搜索（1-2 天），然后深化注释恢复和 Semgrep 知识库形成护城河，不要去卷 RAG 问答（红海）。**

