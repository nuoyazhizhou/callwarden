//! Call Warden Core — PyTorch 式分层架构的 Rust 核心层
//!
//! P29: parse 热路径下沉 Rust
//! - 用 rayon 数据并行替代 Python ProcessPoolExecutor
//! - grammar 单份共享（Arc），避免多进程重复加载
//! - 结果存 Rust 侧 Vec，Python 通过 PyO3 零拷贝读取
//!
//! 对比 Python 多进程：
//!   内存：8 worker × (Python 30MB + grammar 80MB) = 880MB → 1 份共享 = 80MB
//!   pickle：10-14GB 副本 → 零拷贝
//!   主进程持有：10-14GB → 流式 ~100MB

use pyo3::prelude::*;
use pyo3::types::PyAny;
use pyo3::types::PyBytes;
use pyo3::types::PyDict;
use pyo3::Bound;
use pyo3::BoundObject; // P29: PyO3 0.29 需要 trait 导入才能用 into_bound()
use rayon::prelude::*;
use std::sync::atomic::{AtomicUsize, Ordering}; // P30: ParseResultPool 迭代器游标（Sync 安全）
use std::sync::Arc;
use tree_sitter::{Language, Node, Parser};

// F11 方案 A：build_graph_from_c_files 需要访问 graph 模块的 build_csr_public / build_callee_name_index_public / GraphStore::new_with_data
mod abi_contract;
pub mod canonicalize;
// Phase 0 子任务 3 Step 2: Python/Rust 差分对照基线数据结构
mod differential_baseline;
// R7: daemon/snapshot 模块需对 cw_daemon binary 可见（bin 与 lib 在同一 crate，但
// 默认 mod 是私有的。改为 pub mod 让 binary 入口能 use callwarden_core::daemon::*
pub mod daemon;
mod delta;
mod diff;
mod frontier;
mod graph;
mod hash_diff;
// Phase 0 Step 2: 迁移状态程序化基线（数据结构 + trait，不暴露 PyO3）
mod migration_manifest;
mod metrics;
// Phase 1-1: SQLite 只读查询 API（schema_version 查询，不写入）
mod sqlite_query;
// Phase 1-2: CAS 只读查询 API（lookup/get_state/count_files/get_file_generation + 纯函数）
mod cas_query;
// Phase 1-3: workspace manifest 只读查询 API（manifest_get/list/count + snapshot_get_files + verify_raw_hash）
mod manifest_query;
// Phase 1-4: Replicator 只读查询 API（replicator_get_pending_count）
mod replicator_query;
// Phase 2-1: CAS→CodeGraph Merge PyO3 暴露层（cas_merge_to_codegraph + cas_merge_init_schema）
mod cas_merge_query;
// Phase 2-2: 批量 symbols 写入 PyO3 暴露层（batch_save_symbols）
mod batch_build_query;
// Phase 2-3: 调用边 resolve + 批量写入 PyO3 暴露层（batch_resolve_and_save_calls）
mod batch_calls_query;
// Phase 2-4: 批量文件历史版本写入 PyO3 暴露层（batch_save_file_versions）
mod batch_file_versions_query;
// Phase 2-6-3: 批量文件注册 PyO3 暴露层（batch_register_files）
mod batch_register_query;
// Phase 3-4-1: StagingLog PyO3 暴露层（append/read/read_pending/mark_applied_batch/mark_failed/
//             truncate/compact_applied/stats/next_lsn）
mod staging_log_query;
// Phase 3-4-2: ParseRetryLog PyO3 暴露层（append/read/read_pending/read_retryable/
//              mark_applied/mark_exhausted/increment_retry/compact/next_lsn）
mod parse_retry_log_query;
// Phase 2-6-1: 增量构建 PyO3 暴露层（compute_and_apply_symbol_diff + load_file_result_from_db）
mod incremental_build_query;
// P0-C Step 0: multi_lang 改为 pub(crate) 以便 languages 子模块复用类型
// (LangConfig/SymbolRule/CallRule/NameStrategy 等)
pub(crate) mod multi_lang;
// R1-P0-2: ParseDiagnostics 由 multi_lang 模块定义，lib.rs 的 ParseResult 引用之
pub(crate) use multi_lang::ParseDiagnostics;
// P0-C Step 0: 按语言拆分的配置模块（languages/typescript.rs 等）
mod languages;
// R7: cw_daemon 需要 SnapshotCache 类型（daemon/snapshot_state.rs 中使用）
pub mod snapshot;
pub mod symbol_query;
mod toolchain;
pub mod watcher;
// Phase 6-2: MinHash/LSH clone detection 核心计算（FNV-1a + 128 perm + LSH 分桶）
// 契约：docs/design/phase6-2-minhash-lsh-clone-detection-contract.md
mod clone_detection;
// Phase 6-1 P2/P3: cross_layer_impact + defect_correlation Rust 核心
mod impact;
// Phase 6-3 P1: 向量加载 + TopK 排序 + 阈值过滤 Rust 核心
// 契约：docs/design/phase6-3-vector-cosine-test-association-contract.md
mod vector_topk;
// Phase 4-1: UDS framing/SO_PEERCRED/RPC dispatch PyO3 暴露层（protocol_constants/
//            protocol_encode_payload/protocol_decode_payload/protocol_build_frame/
//            protocol_parse_header/protocol_validate_message_size/protocol_parse_response/
//            protocol_make_ok_response/protocol_make_error_response/peercred_is_available/
//            peercred_info/dispatch_list_methods/dispatch_list_error_codes/dispatch_is_admin_method）
mod daemon_query;
// Phase 5-1: CLI 配置加载 + 只读命令识别 PyO3 暴露层
// 契约：docs/design/phase5-1-cli-config-contract.md
pub mod cli;

// ============================================
// P29: 数据结构定义
// ============================================

/// 单个文件的 parse 结果
/// 紧凑布局，1.5M 符号 ~ 4-6GB（vs Python dict 10-14GB）
#[derive(Clone, Debug)]
pub struct ParseResult {
    pub rel_path: String,
    pub abs_path: String,
    pub module_path: String,
    pub content_hash: String,
    pub total_lines: u32,
    pub language: String,
    pub symbols: Vec<SymbolInfo>,
    pub calls: Vec<RawCall>,
    pub imports: Vec<String>,
    /// P0-D: 引用关系（HCL attribute traversal 等声明式引用）
    /// 与 raw_calls 区别：references 记录语义化引用（resource 地址），
    /// raw_calls 记录完整 traversal 文本（向后兼容 Python parser 行为）
    pub references: Vec<RawReference>,
    pub error: Option<String>,
    /// R1-P0-2: ParseFact ABI 诊断字段（syntax error / unsupported / partial / fatal）
    /// 替代旧 `error` 字段用于结构化诊断；`error` 保留以兼容旧调用方。
    pub diagnostics: ParseDiagnostics,
}

/// P0-D: 声明式引用（HCL attribute traversal）
/// 如 `value = aws_instance.web.private_ip` 引用了 `aws_instance.web` 资源块
#[derive(Clone, Debug)]
pub struct RawReference {
    /// 引用者符号名（当前所在 block 的 qualified name 末段）
    pub caller_name: String,
    /// 被引用资源地址（如 "aws_instance.web"，不含尾部 attribute）
    pub callee_name: String,
    pub call_line: u32,
    /// 引用类型（如 "attribute_traversal"）
    pub reference_kind: String,
    /// 完整 traversal 源文本（如 "aws_instance.web.private_ip"）
    pub source_text: String,
}

/// 符号信息（紧凑 struct，对应 Python 侧的 dict）
#[derive(Clone, Debug)]
pub struct SymbolInfo {
    pub name: String,
    pub qualified_name: String,
    pub kind: String, // "function" / "struct" / "enum" / "union"
    pub start_line: u32,
    pub end_line: u32,
    pub module_path: String,
    pub symbol_hash: String, // 内容哈希
    pub depth: i32,          // 调用深度（-1 未计算）
    pub has_comment: bool,
    pub visibility: String,
    pub content: String, // 符号源码内容
    pub signature: String,
    /// R1-P0-2 / R7-P0-3: ParseFact ABI — 文件内稳定符号 ID（1-based，0 保留给
    /// synthetic module symbol）。按 byte_start 排序后从 1 递增。
    pub local_id: u32,
    /// R14-P0-2: 词法父符号的 local_id（None = 顶层，无词法父；
    /// Some(x) = 真实父符号的 local_id，x>=1）
    ///
    /// 企业设计 enterprise-phase1-phase3-detail.md:1075 要求用 NULL（Option<u32>）
    /// 表示"无父节点"，不用 0。SQLite UNIQUE 约束对 NULL 视为 distinct，
    /// 所以多个 lexical_parent_local_id=NULL 的符号靠 local_id 区分。
    pub lexical_parent_local_id: Option<u32>,
    /// R1-P0-2: 符号在文件字节流中的起始偏移（canonical bytes 偏移）
    pub byte_start: u32,
    /// R1-P0-2: 符号在文件字节流中的结束偏移（exclusive）
    pub byte_end: u32,
}

/// 原始调用关系（parse 阶段提取，未解析）
#[derive(Clone, Debug)]
pub struct RawCall {
    pub callee_name: String,
    pub callee_module: String, // 可能空
    pub caller_name: String,
    pub caller_qualified: String,
    pub call_line: u32,
    pub is_cross_file: bool,
    /// R14-P0-2: 调用者符号的 local_id（None = 顶层裸调用，无词法容器；
    /// Some(x) = 真实调用者 local_id，x>=1）
    ///
    /// 企业设计 enterprise-phase1-phase3-detail.md:1076 要求用 NULL（Option<u32>）
    /// 表示"顶层裸调用"（无词法容器）。顶层裸调用仍保留在 cas_raw_calls 中，
    /// caller_name 为 "__module__"（synthetic）或源码中实际出现的表达式。
    /// SQLite UNIQUE 约束对 NULL 视为 distinct，所以多个 caller_local_id=NULL
    /// 的同行同 callee 调用靠 call_ordinal 区分。
    pub caller_local_id: Option<u32>,
    /// R1-P0-2: 同一调用者内 call 序号（0-based，按 byte_start 排序）
    pub ordinal: u32,
    /// R1-P0-2: call 表达式的字节起始偏移
    pub byte_start: u32,
    /// R1-P0-2: call 表达式的字节结束偏移（exclusive）
    pub byte_end: u32,
}

// ============================================
// P29: tree-sitter C 语言 parse
// ============================================

/// C 语言 parser（grammar 共享，Arc 引用计数）
pub struct CParser {
    language: Language,
}

impl CParser {
    pub fn new() -> Self {
        // tree-sitter-c 0.24: 用 LANGUAGE 常量
        let language = Language::from(tree_sitter_c::LANGUAGE);
        Self { language }
    }

    /// parse 单个 C 文件，提取函数符号 + 调用关系
    pub fn parse_file(&self, abs_path: &str, module_path: &str) -> ParseResult {
        let source = match std::fs::read(abs_path) {
            Ok(s) => s,
            Err(e) => {
                return ParseResult {
                    rel_path: String::new(),
                    abs_path: abs_path.to_string(),
                    module_path: module_path.to_string(),
                    content_hash: String::new(),
                    total_lines: 0,
                    language: "c".to_string(),
                    symbols: Vec::new(),
                    calls: Vec::new(),
                    imports: Vec::new(),
                    references: Vec::new(),
                    error: Some(format!("read error: {}", e)),
                    diagnostics: ParseDiagnostics::failed(&format!("read error: {}", e)),
                };
            }
        };

        let mut parser = Parser::new();
        if parser.set_language(&self.language).is_err() {
            return ParseResult {
                rel_path: String::new(),
                abs_path: abs_path.to_string(),
                module_path: module_path.to_string(),
                content_hash: String::new(),
                total_lines: 0,
                language: "c".to_string(),
                symbols: Vec::new(),
                calls: Vec::new(),
                imports: Vec::new(),
                references: Vec::new(),
                error: Some("set_language failed".to_string()),
                diagnostics: ParseDiagnostics::failed("set_language failed"),
            };
        }

        let tree = match parser.parse(&source, None) {
            Some(t) => t,
            None => {
                return ParseResult {
                    rel_path: String::new(),
                    abs_path: abs_path.to_string(),
                    module_path: module_path.to_string(),
                    content_hash: String::new(),
                    total_lines: 0,
                    language: "c".to_string(),
                    symbols: Vec::new(),
                    calls: Vec::new(),
                    imports: Vec::new(),
                    references: Vec::new(),
                    error: Some("parse returned None".to_string()),
                    diagnostics: ParseDiagnostics::failed("parse returned None"),
                };
            }
        };

        let content_hash = format!("{:x}", blake_hash(&source));
        let total_lines = source.iter().filter(|&&b| b == b'\n').count() as u32 + 1;

        // 提取符号和调用（tree 持有所有权，root_node 借用 tree）
        let mut symbols = Vec::new();
        let mut calls = Vec::new();
        let mut imports = Vec::new();
        let root = tree.root_node();
        walk_c_node(
            &root,
            &source,
            module_path,
            "",
            &mut symbols,
            &mut calls,
            &mut imports,
        );

        // R1-P0-2: ParseFact ABI 后处理 — 赋值 local_id / lexical_parent_local_id /
        // caller_local_id / ordinal（按 byte_start 排序，通过 byte range 包含关系
        // 推导父子与调用者）
        assign_local_ids(&mut symbols, &mut calls);

        // R1-P0-2: 计算语法错误数（has_error 节点计数），构造 ParseDiagnostics
        let syntax_error_count = count_syntax_errors(&root);
        let diagnostics = ParseDiagnostics::from_syntax_count(syntax_error_count, None);

        ParseResult {
            rel_path: String::new(),
            abs_path: abs_path.to_string(),
            module_path: module_path.to_string(),
            content_hash,
            total_lines,
            language: "c".to_string(),
            symbols,
            calls,
            imports,
            references: Vec::new(),
            error: None,
            diagnostics,
        }
    }
}

// ============================================
// R1-P0-2 / R7-P0-3: ParseFact ABI 后处理 — local_id / parent / ordinal 赋值
// ============================================

/// 为 symbols 和 calls 赋值 ParseFact ABI 的 local_id / lexical_parent_local_id /
/// caller_local_id / ordinal 字段。
///
/// R14-P0-2 NULL ABI 修复（企业设计 enterprise-phase1-phase3-detail.md:1074-1077）：
/// - `local_id` 从 **1** 开始（1-based），0 保留给 synthetic module symbol
/// - `lexical_parent_local_id` 用 **None** 表示顶层（无词法父），
///   Some(x) 表示真实父符号的 local_id（x>=1）
/// - `caller_local_id` 用 **None** 表示顶层裸调用（无词法容器），
///   Some(x) 表示真实调用者符号的 local_id（x>=1）
///
/// 这样第一个真实符号 local_id=1 与"未解析/synthetic"哨兵 0 不再冲突，
/// 同时 NULL 语义清晰区分"顶层/未解析"与"local_id=0 的 synthetic module symbol"。
///
/// 算法：
/// 1. 按 (byte_start, byte_end) 升序排序 symbols（稳定排序）
/// 2. local_id = 排序后的索引 + 1（1-based，0 保留给 synthetic module symbol）
/// 3. 对每个 symbol S，找词法父：byte_start < S.byte_start 且 byte_end > S.byte_end
///    且自身 byte 范围最小的 symbol（最内层包含者）；未找到则置 None（顶层）
/// 4. 对每个 call C，按 caller_qualified 匹配找到对应 symbol，写入 caller_local_id；
///    未匹配则置 None（未解析/顶层裸调用）
/// 5. 对每个 caller_local_id，按 byte_start 升序赋值 ordinal（0-based）
///
/// 设计要点：通过 byte range 包含关系推导父子，避免在递归 walk 中传递状态。
pub(crate) fn assign_local_ids(symbols: &mut Vec<SymbolInfo>, calls: &mut Vec<RawCall>) {
    if symbols.is_empty() {
        return;
    }

    // 1. 按 (byte_start, byte_end) 排序并赋 local_id（1-based）
    symbols.sort_by(|a, b| {
        a.byte_start
            .cmp(&b.byte_start)
            .then(a.byte_end.cmp(&b.byte_end))
    });
    for (idx, sym) in symbols.iter_mut().enumerate() {
        // R7-P0-3: 1-based，0 保留给 synthetic module symbol
        sym.local_id = (idx + 1) as u32;
    }

    // 2. 对每个 symbol，找最内层包含者作为词法父
    //    O(n^2) 但 n 通常为文件级符号数（< 1000），可接受
    for i in 0..symbols.len() {
        let (cur_start, cur_end) = (symbols[i].byte_start, symbols[i].byte_end);
        // R14-P0-2: None 表示顶层（无词法父）
        let mut best_parent: Option<u32> = None;
        let mut best_parent_end: u32 = u32::MAX;
        for j in 0..symbols.len() {
            if i == j {
                continue;
            }
            let (p_start, p_end) = (symbols[j].byte_start, symbols[j].byte_end);
            // 父必须严格包含 cur（byte_start < cur.byte_start 且 byte_end >= cur.byte_end）
            // 严格 < 避免同位置重叠；end 可以等于（同位置起止）
            if p_start < cur_start && p_end >= cur_end && p_end < best_parent_end {
                best_parent = Some(symbols[j].local_id);
                best_parent_end = p_end;
            }
        }
        symbols[i].lexical_parent_local_id = best_parent;
    }

    // 3. 构造 caller_qualified -> local_id 映射
    use std::collections::HashMap;
    let mut caller_map: HashMap<String, u32> = HashMap::new();
    for s in symbols.iter() {
        // 用第一个匹配的 local_id（按 byte_start 排序后是最靠前的）
        caller_map
            .entry(s.qualified_name.clone())
            .or_insert(s.local_id);
    }

    // 4. 为每个 call 赋 caller_local_id 和 ordinal
    //    先按 caller_local_id 分组，组内按 byte_start 排序赋 ordinal
    for c in calls.iter_mut() {
        if let Some(&lid) = caller_map.get(&c.caller_qualified) {
            c.caller_local_id = Some(lid);
        } else {
            // R14-P0-2: None 表示未解析到调用者（顶层裸调用）
            c.caller_local_id = None;
        }
    }
    // 组内按 byte_start 排序赋 ordinal
    // R14-P0-2: Option<u32> 排序——None 排在最前（视为 0），Some(x) 按 x 升序
    calls.sort_by(|a, b| {
        a.caller_local_id
            .cmp(&b.caller_local_id)
            .then(a.byte_start.cmp(&b.byte_start))
    });
    // R14-P0-2: 用 Option<u32> 跟踪当前 caller 组（None 是独立组）
    let mut current_caller: Option<u32> = None;
    let mut current_caller_initialized = false;
    let mut ordinal_counter: u32 = 0;
    for c in calls.iter_mut() {
        if !current_caller_initialized || c.caller_local_id != current_caller {
            current_caller = c.caller_local_id;
            current_caller_initialized = true;
            ordinal_counter = 0;
        }
        c.ordinal = ordinal_counter;
        ordinal_counter += 1;
    }
}

/// 递归统计 tree-sitter AST 中的 ERROR / MISSING 节点数（语法错误指标）
///
/// 使用 has_error() 快速剪枝：子树无 error 时直接跳过。
/// tree-sitter 的 ERROR 节点 unnamed，需用 children() 而非 named_children() 遍历。
pub(crate) fn count_syntax_errors(root: &Node) -> u32 {
    let mut count = 0u32;
    let mut stack = vec![*root];
    while let Some(node) = stack.pop() {
        if !node.has_error() {
            continue;
        }
        // 当前节点是 ERROR 或 MISSING → 计数
        if node.kind() == "ERROR" || node.is_missing() {
            count += 1;
            continue; // ERROR 子节点通常是 token，不再深入
        }
        // 遍历所有子节点（含 unnamed，因为 ERROR 是 unnamed）
        let mut cursor = node.walk();
        for child in node.children(&mut cursor) {
            stack.push(child);
        }
    }
    count
}

/// 递归遍历 C AST，提取符号和调用关系
fn walk_c_node(
    node: &Node,
    source: &[u8],
    module_path: &str,
    parent_qualified: &str,
    symbols: &mut Vec<SymbolInfo>,
    calls: &mut Vec<RawCall>,
    imports: &mut Vec<String>,
) {
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        let kind = child.kind();

        match kind {
            "function_definition" => {
                if let Some(sym) = parse_c_function(&child, source, module_path, parent_qualified) {
                    // 在函数体内提取调用
                    extract_calls_from_function(
                        &child,
                        source,
                        &sym.qualified_name,
                        &sym.name,
                        calls,
                    );
                    symbols.push(sym);
                }
            }
            "struct_specifier" => {
                if let Some(sym) = parse_c_struct(
                    &child,
                    source,
                    module_path,
                    parent_qualified,
                    "struct",
                    None,
                ) {
                    let qname = sym.qualified_name.clone();
                    symbols.push(sym);
                    // 递归处理结构体内部
                    if let Some(body) = find_child(&child, "field_declaration_list") {
                        walk_c_node(&body, source, module_path, &qname, symbols, calls, imports);
                    }
                }
            }
            "enum_specifier" => {
                if let Some(sym) = parse_c_enum(&child, source, module_path, parent_qualified, None)
                {
                    symbols.push(sym);
                }
            }
            "union_specifier" => {
                if let Some(sym) =
                    parse_c_struct(&child, source, module_path, parent_qualified, "union", None)
                {
                    symbols.push(sym);
                }
            }
            // P32: typedef 声明 — 提取其中的 struct/enum/union，用 typedef 名称
            // 修复 P29 缺失的 857 个 typedef 符号（与 Python c_parser.py 行为对齐）
            "type_definition" => {
                let struct_node = find_child(&child, "struct_specifier");
                let enum_node = find_child(&child, "enum_specifier");
                let union_node = find_child(&child, "union_specifier");
                // 最后一个 type_identifier 是 typedef 的名称
                let type_name = find_last_child_by_kind(&child, "type_identifier")
                    .map(|n| node_text(&n, source).to_string());

                if let (Some(sn), Some(name)) = (struct_node.as_ref(), type_name.as_ref()) {
                    if let Some(sym) = parse_c_struct(
                        sn,
                        source,
                        module_path,
                        parent_qualified,
                        "struct",
                        Some(name),
                    ) {
                        let qname = sym.qualified_name.clone();
                        symbols.push(sym);
                        // 递归处理结构体内部字段
                        if let Some(body) = find_child(sn, "field_declaration_list") {
                            walk_c_node(
                                &body,
                                source,
                                module_path,
                                &qname,
                                symbols,
                                calls,
                                imports,
                            );
                        }
                    }
                } else if let (Some(en), Some(name)) = (enum_node.as_ref(), type_name.as_ref()) {
                    if let Some(sym) =
                        parse_c_enum(en, source, module_path, parent_qualified, Some(name))
                    {
                        symbols.push(sym);
                    }
                } else if let (Some(un), Some(name)) = (union_node.as_ref(), type_name.as_ref()) {
                    if let Some(sym) = parse_c_struct(
                        un,
                        source,
                        module_path,
                        parent_qualified,
                        "union",
                        Some(name),
                    ) {
                        symbols.push(sym);
                    }
                } else {
                    // 其他 typedef 情况（如 typedef int MyInt），递归处理子节点
                    walk_c_node(
                        &child,
                        source,
                        module_path,
                        parent_qualified,
                        symbols,
                        calls,
                        imports,
                    );
                }
            }
            "preproc_include" => {
                // #include 语句
                if let Some(path_node) = find_child(&child, "string_literal") {
                    let path = node_text(&path_node, source);
                    imports.push(path.trim_matches('"').to_string());
                } else if let Some(path_node) = find_child(&child, "system_lib_string") {
                    let path = node_text(&path_node, source);
                    imports.push(format!("<{}>", path.trim_matches('<').trim_matches('>')));
                }
            }
            _ => {}
        }
    }
}

/// parse C 函数定义
fn parse_c_function(
    node: &Node,
    source: &[u8],
    module_path: &str,
    parent_qualified: &str,
) -> Option<SymbolInfo> {
    // 查找 function_declarator
    let declarator = find_child(node, "function_declarator")?;
    let name_node = find_child(&declarator, "identifier")
        .or_else(|| find_child(&declarator, "field_identifier"))
        .or_else(|| {
            // 处理指针声明符
            let ptr = find_child(&declarator, "pointer_declarator")?;
            find_child(&ptr, "identifier").or_else(|| find_child(&ptr, "field_identifier"))
        })?;

    let name = node_text(&name_node, source).to_string();
    let qualified = if !parent_qualified.is_empty() {
        format!("{}.{}", parent_qualified, name)
    } else if !module_path.is_empty() {
        format!("{}.{}", module_path, name)
    } else {
        name.clone()
    };

    let start_line = node.start_position().row as u32 + 1;
    let end_line = node.end_position().row as u32 + 1;
    let content = node_text(node, source).to_string();
    let signature = c_signature(node, source, true);

    Some(SymbolInfo {
        name,
        qualified_name: qualified,
        kind: "fn".to_string(), // 与 Python c_parser.py 保持一致：函数用 "fn"
        start_line,
        end_line,
        module_path: module_path.to_string(),
        symbol_hash: format!("{:x}", blake_hash(content.as_bytes())),
        depth: -1,
        has_comment: false,
        visibility: "public".to_string(),
        content,
        signature,
        // R1-P0-2: ParseFact ABI — byte range 直接从 AST 节点取；
        // local_id / lexical_parent_local_id 由 assign_local_ids 后处理填入
        // R14-P0-2: lexical_parent_local_id 改为 None（Option<u32>，None=顶层）
        local_id: 0,
        lexical_parent_local_id: None,
        byte_start: node.start_byte() as u32,
        byte_end: node.end_byte() as u32,
    })
}

/// parse C 结构体/联合体
///
/// name_override：P32 typedef 时传入 typedef 名称（type_definition 的最后一个
/// type_identifier），None 时从节点自身 type_identifier 提取名称。
fn parse_c_struct(
    node: &Node,
    source: &[u8],
    module_path: &str,
    parent_qualified: &str,
    kind: &str,
    name_override: Option<&str>,
) -> Option<SymbolInfo> {
    let name = match name_override {
        Some(n) => n.to_string(),
        None => {
            let name_node = find_child(node, "type_identifier")?;
            node_text(&name_node, source).to_string()
        }
    };
    let qualified = make_qualified(module_path, parent_qualified, &name);

    let signature = c_signature(node, source, false);

    Some(SymbolInfo {
        name,
        qualified_name: qualified,
        kind: kind.to_string(),
        start_line: node.start_position().row as u32 + 1,
        end_line: node.end_position().row as u32 + 1,
        module_path: module_path.to_string(),
        symbol_hash: format!("{:x}", blake_hash(node_text(node, source).as_bytes())),
        depth: -1,
        has_comment: false,
        visibility: "public".to_string(),
        content: node_text(node, source).to_string(),
        signature,
        // R1-P0-2: ParseFact ABI 字段
        // R14-P0-2: lexical_parent_local_id 改为 None（Option<u32>，None=顶层）
        local_id: 0,
        lexical_parent_local_id: None,
        byte_start: node.start_byte() as u32,
        byte_end: node.end_byte() as u32,
    })
}

/// parse C 枚举
///
/// name_override：P32 typedef 时传入 typedef 名称，None 时从节点自身 type_identifier 提取。
fn parse_c_enum(
    node: &Node,
    source: &[u8],
    module_path: &str,
    parent_qualified: &str,
    name_override: Option<&str>,
) -> Option<SymbolInfo> {
    let name = match name_override {
        Some(n) => n.to_string(),
        None => {
            let name_node = find_child(node, "type_identifier")?;
            node_text(&name_node, source).to_string()
        }
    };
    let qualified = make_qualified(module_path, parent_qualified, &name);

    let signature = c_signature(node, source, false);

    Some(SymbolInfo {
        name,
        qualified_name: qualified,
        kind: "enum".to_string(),
        start_line: node.start_position().row as u32 + 1,
        end_line: node.end_position().row as u32 + 1,
        module_path: module_path.to_string(),
        symbol_hash: format!("{:x}", blake_hash(node_text(node, source).as_bytes())),
        depth: -1,
        has_comment: false,
        visibility: "public".to_string(),
        content: node_text(node, source).to_string(),
        signature,
        // R1-P0-2: ParseFact ABI 字段
        // R14-P0-2: lexical_parent_local_id 改为 None（Option<u32>，None=顶层）
        local_id: 0,
        lexical_parent_local_id: None,
        byte_start: node.start_byte() as u32,
        byte_end: node.end_byte() as u32,
    })
}

/// 提取 C declaration signature：函数去掉 compound_statement，类型声明保留完整声明。
fn c_signature(node: &Node, source: &[u8], function: bool) -> String {
    let end = if function {
        find_child(node, "compound_statement")
            .map(|body| body.start_byte())
            .unwrap_or_else(|| node.end_byte())
    } else {
        node.end_byte()
    };
    let text = std::str::from_utf8(&source[node.start_byte()..end]).unwrap_or("");
    text.split_whitespace().collect::<Vec<_>>().join(" ")
}

/// 从函数体内提取调用关系（call_expression）
fn extract_calls_from_function(
    func_node: &Node,
    source: &[u8],
    caller_qualified: &str,
    caller_name: &str,
    calls: &mut Vec<RawCall>,
) {
    // 遍历函数体的所有 call_expression
    let mut stack = vec![*func_node];
    while let Some(node) = stack.pop() {
        let mut cursor = node.walk();
        for child in node.named_children(&mut cursor) {
            if child.kind() == "call_expression" {
                // 提取被调用函数名
                let func_node = find_child(&child, "identifier")
                    .or_else(|| find_child(&child, "field_expression"));
                if let Some(fn_node) = func_node {
                    let callee_name = node_text(&fn_node, source).to_string();
                    // 简单判断：含 . 或 -> 的是方法调用
                    let is_method = callee_name.contains('.') || callee_name.contains("->");
                    let simple_name = if is_method {
                        // 取 . 或 -> 后的部分
                        callee_name
                            .rsplit(['.', '-'])
                            .next()
                            .unwrap_or(&callee_name)
                            .to_string()
                    } else {
                        callee_name.clone()
                    };
                    calls.push(RawCall {
                        callee_name: simple_name,
                        callee_module: if is_method {
                            callee_name
                                .split(['.', '-'])
                                .next()
                                .unwrap_or("")
                                .to_string()
                        } else {
                            String::new()
                        },
                        caller_name: caller_name.to_string(),
                        caller_qualified: caller_qualified.to_string(),
                        call_line: child.start_position().row as u32 + 1,
                        is_cross_file: false,
                        // R1-P0-2: ParseFact ABI 字段
                        // caller_local_id / ordinal 由 assign_local_ids 后处理填入
                        // R14-P0-2: caller_local_id 改为 None（Option<u32>，None=未解析）
                        caller_local_id: None,
                        ordinal: 0,
                        byte_start: child.start_byte() as u32,
                        byte_end: child.end_byte() as u32,
                    });
                }
            }
            stack.push(child);
        }
    }
}

// ============================================
// P29: 工具函数
// ============================================

pub(crate) fn find_child<'a>(node: &Node<'a>, kind: &str) -> Option<Node<'a>> {
    // tree-sitter 0.26: cursor 必须在循环外创建
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        if child.kind() == kind {
            // cursor drop 前返回 child 的 clone（Node 是 Copy）
            return Some(child);
        }
    }
    None
}

pub(crate) fn node_text<'a>(node: &Node<'a>, source: &'a [u8]) -> &'a str {
    let start = node.start_byte();
    let end = node.end_byte();
    std::str::from_utf8(&source[start..end]).unwrap_or("")
}

/// P32: 构造限定名（qualified_name）
/// 优先级：parent_qualified > module_path > 裸名
pub(crate) fn make_qualified(module_path: &str, parent_qualified: &str, name: &str) -> String {
    if !parent_qualified.is_empty() {
        format!("{}.{}", parent_qualified, name)
    } else if !module_path.is_empty() {
        format!("{}.{}", module_path, name)
    } else {
        name.to_string()
    }
}

/// P32: 查找指定 kind 的最后一个命名子节点（用于 typedef 提取最后一个 type_identifier）
pub(crate) fn find_last_child_by_kind<'a>(node: &Node<'a>, kind: &str) -> Option<Node<'a>> {
    let mut cursor = node.walk();
    let mut last: Option<Node<'a>> = None;
    for child in node.named_children(&mut cursor) {
        if child.kind() == kind {
            last = Some(child);
        }
    }
    last
}

/// 简单哈希（PoC 用，生产应换 SHA-256）
pub(crate) fn blake_hash(data: &[u8]) -> u64 {
    use std::collections::hash_map::DefaultHasher;
    use std::hash::{Hash, Hasher};
    let mut hasher = DefaultHasher::new();
    data.hash(&mut hasher);
    hasher.finish()
}

// ============================================
// P29: PyO3 暴露的 Python 接口
// ============================================

/// 批量 parse C 文件（rayon 并行，grammar 共享）
///
/// Python 调用：
///   from callwarden_core import batch_parse_c_files
///   results = batch_parse_c_files([("/path/a.c", "module.a"), ...], num_threads=8)
///   for r in results:
///       for sym in r["symbols"]:
///           print(sym["name"], sym["qualified_name"])
#[pyfunction]
#[pyo3(signature = (files, num_threads=None))]
fn batch_parse_c_files<'py>(
    py: Python<'py>,
    files: Vec<(String, String)>, // (abs_path, module_path)
    num_threads: Option<usize>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    // 配置 rayon 线程数
    if let Some(n) = num_threads {
        rayon::ThreadPoolBuilder::new()
            .num_threads(n)
            .build_global()
            .ok();
    }

    // grammar 只加载一次，Arc 共享给所有线程
    let c_parser = Arc::new(CParser::new());

    // rayon 并行 parse：每个线程 clone Arc<CParser>（只增加引用计数，不重复加载 grammar）
    let results: Vec<ParseResult> = files
        .par_iter()
        .map(|(abs_path, module_path)| {
            let parser = c_parser.clone(); // Arc clone，零拷贝
            parser.parse_file(abs_path, module_path)
        })
        .collect();

    // 转换为 Python dict（这一步在主线程，GIL 持有）
    let mut py_results = Vec::with_capacity(results.len());
    for r in results {
        py_results.push(parse_result_to_pydict(py, &r)?);
    }
    Ok(py_results)
}

/// 单文件 parse C（用于测试和对比）
#[pyfunction]
fn parse_c_file<'py>(
    py: Python<'py>,
    abs_path: &str,
    module_path: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let parser = CParser::new();
    let result = parser.parse_file(abs_path, module_path);
    parse_result_to_pydict(py, &result)
}

/// 获取 Rust 核心版本信息
#[pyfunction]
fn core_version() -> &'static str {
    "0.2.0-p29"
}

/// 将 ParseResult 转为 Python dict（零拷贝：Rust String 直接转 PyString）
pub(crate) fn parse_result_to_pydict<'py>(
    py: Python<'py>,
    r: &ParseResult,
) -> PyResult<Bound<'py, PyAny>> {
    let dict = PyDict::new(py);

    dict.set_item("abs_path", r.abs_path.clone())?;
    dict.set_item("module_path", r.module_path.clone())?;
    dict.set_item("content_hash", r.content_hash.clone())?;
    dict.set_item("total_lines", r.total_lines)?;
    dict.set_item("language", r.language.clone())?;

    // symbols 转为 list of dict
    let symbols: Vec<Bound<'py, PyAny>> = r
        .symbols
        .iter()
        .map(|s| {
            let d = PyDict::new(py);
            d.set_item("name", s.name.clone()).ok();
            d.set_item("qualified_name", s.qualified_name.clone()).ok();
            d.set_item("kind", s.kind.clone()).ok();
            d.set_item("start_line", s.start_line).ok();
            d.set_item("end_line", s.end_line).ok();
            d.set_item("module_path", s.module_path.clone()).ok();
            d.set_item("symbol_hash", s.symbol_hash.clone()).ok();
            d.set_item("depth", s.depth).ok();
            d.set_item("has_comment", s.has_comment).ok();
            d.set_item("visibility", s.visibility.clone()).ok();
            d.set_item("content", s.content.clone()).ok();
            d.set_item("signature", s.signature.clone()).ok();
            // R1-P0-2: ParseFact ABI 字段
            d.set_item("local_id", s.local_id).ok();
            d.set_item("lexical_parent_local_id", s.lexical_parent_local_id).ok();
            d.set_item("byte_start", s.byte_start).ok();
            d.set_item("byte_end", s.byte_end).ok();
            d.into_any().into_bound()
        })
        .collect();
    dict.set_item("symbols", symbols)?;

    // calls 转为 list of dict
    let calls: Vec<Bound<'py, PyAny>> = r
        .calls
        .iter()
        .map(|c| {
            let d = PyDict::new(py);
            d.set_item("callee_name", c.callee_name.clone()).ok();
            d.set_item("callee_module", c.callee_module.clone()).ok();
            d.set_item("caller_name", c.caller_name.clone()).ok();
            d.set_item("caller_qualified", c.caller_qualified.clone())
                .ok();
            d.set_item("call_line", c.call_line).ok();
            d.set_item("is_cross_file", c.is_cross_file).ok();
            // R1-P0-2: ParseFact ABI 字段
            d.set_item("caller_local_id", c.caller_local_id).ok();
            d.set_item("ordinal", c.ordinal).ok();
            d.set_item("byte_start", c.byte_start).ok();
            d.set_item("byte_end", c.byte_end).ok();
            d.into_any().into_bound()
        })
        .collect();
    dict.set_item("raw_calls", calls)?;

    dict.set_item("imports", r.imports.clone())?;

    // P0-D: references 转为 list of dict（HCL attribute traversal 等）
    let references: Vec<Bound<'py, PyAny>> = r
        .references
        .iter()
        .map(|rf| {
            let d = PyDict::new(py);
            d.set_item("caller_name", rf.caller_name.clone()).ok();
            d.set_item("callee_name", rf.callee_name.clone()).ok();
            d.set_item("call_line", rf.call_line).ok();
            d.set_item("reference_kind", rf.reference_kind.clone()).ok();
            d.set_item("source_text", rf.source_text.clone()).ok();
            d.into_any().into_bound()
        })
        .collect();
    dict.set_item("references", references)?;

    if let Some(err) = &r.error {
        dict.set_item("error", err.clone())?;
    }

    // R1-P0-2: diagnostics 字段（ParseFact ABI 诊断）
    let diag = PyDict::new(py);
    diag.set_item("status", r.diagnostics.status.clone())?;
    diag.set_item("syntax_error_count", r.diagnostics.syntax_error_count)?;
    diag.set_item("unsupported_construct_count", r.diagnostics.unsupported_construct_count)?;
    diag.set_item("partial_parse", r.diagnostics.partial_parse)?;
    if let Some(fatal) = &r.diagnostics.fatal_parse_error {
        diag.set_item("fatal_parse_error", fatal.clone())?;
    }
    if let Some(err) = &r.diagnostics.error {
        diag.set_item("error", err.clone())?;
    }
    dict.set_item("diagnostics", diag)?;

    Ok(dict.into_any().into_bound())
}

// ============================================
// P30: 流式回传 — ParseResultPool（Rust 侧持有结果，Python 按需读取）
// ============================================

/// Rust 侧持有的 parse 结果池。
///
/// 解决 P29 的内存问题：batch_parse_c_files 把所有结果转成 Python list 后，
/// 主进程持有 10-14GB 的 dict（1.5M 符号场景）。
///
/// P30 改造：parse 结果存在 Rust 侧 Vec<ParseResult>，Python 通过 get_at(i)
/// 按需读取单个文件，转成 Python dict 写 DB 后立即释放。
/// 主进程任意时刻只持有 1 个文件的结果（~1MB），而非全部。
///
/// Python 用法：
///   pool = batch_parse_c_files_pool(files, num_threads=8)
///   for i in range(pool.len()):
///       result = pool.get_at(i)  # 按需转 dict，写完 DB 即可释放
///       db.write_file_result(result)
///       del result  # 显式释放
#[pyclass]
pub struct ParseResultPool {
    pub(crate) results: Vec<ParseResult>,
    pub(crate) iter_idx: AtomicUsize, // 迭代器游标（AtomicUsize 满足 Send+Sync，支持 for r in pool）
}

#[pymethods]
impl ParseResultPool {
    /// 获取池中结果数量
    fn len(&self) -> usize {
        self.results.len()
    }

    /// 按索引获取单个结果（转成 Python dict，零拷贝）
    ///
    /// 每次调用只转一个文件，主进程不持有全部结果。
    /// 调用方应在写完 DB 后立即释放返回的 dict（del 或离开作用域）。
    fn get_at<'py>(&self, py: Python<'py>, idx: usize) -> PyResult<Bound<'py, PyAny>> {
        if idx >= self.results.len() {
            return Err(pyo3::exceptions::PyIndexError::new_err(format!(
                "index {} out of range (len={})",
                idx,
                self.results.len()
            )));
        }
        parse_result_to_pydict(py, &self.results[idx])
    }

    /// 获取指定 abs_path 的结果（线性查找，适合测试/调试）
    fn get_by_path<'py>(&self, py: Python<'py>, abs_path: &str) -> PyResult<Bound<'py, PyAny>> {
        let idx = self
            .results
            .iter()
            .position(|r| r.abs_path == abs_path)
            .ok_or_else(|| {
                pyo3::exceptions::PyKeyError::new_err(format!("path not found: {}", abs_path))
            })?;
        parse_result_to_pydict(py, &self.results[idx])
    }

    /// 获取所有结果的统计信息（不转 dict，零内存开销）
    fn stats(&self) -> (usize, usize, usize, usize) {
        let total_symbols: usize = self.results.iter().map(|r| r.symbols.len()).sum();
        let total_calls: usize = self.results.iter().map(|r| r.calls.len()).sum();
        let total_errors: usize = self.results.iter().filter(|r| r.error.is_some()).count();
        (self.results.len(), total_symbols, total_calls, total_errors)
    }

    /// Python 迭代器协议：__iter__ 重置游标并返回自身
    ///
    /// 重置游标后可重复迭代：
    ///   pool = batch_parse_c_files_pool(files)
    ///   for r in pool:   # 第一次遍历
    ///       write_db(r)
    ///   for r in pool:   # 第二次遍历，游标已重置
    ///       ...
    fn __iter__(slf: Py<Self>, py: Python<'_>) -> Py<Self> {
        slf.borrow(py).iter_idx.store(0, Ordering::Relaxed);
        slf
    }

    /// Python 迭代器协议：__next__ 按需转 dict 并推进游标
    ///
    /// 配合 __iter__ 实现 `for result in pool:` 流式遍历。
    /// 每次返回一个文件的 dict，写完 DB 后由 Python GC 回收，
    /// 主进程任意时刻只持有 1 个文件结果。
    fn __next__<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let idx = self.iter_idx.load(Ordering::Relaxed);
        if idx >= self.results.len() {
            // 迭代结束，重置游标以便下次 for 循环能再次遍历
            self.iter_idx.store(0, Ordering::Relaxed);
            return Ok(None);
        }
        let result = parse_result_to_pydict(py, &self.results[idx])?;
        self.iter_idx.store(idx + 1, Ordering::Relaxed);
        Ok(Some(result))
    }
}

// ============================================
// L6: ParseResultStream — 真正流式回传（按完成顺序）
// ============================================
//
// 与 ParseResultPool 的区别：
// - ParseResultPool：par_iter().collect() 等所有文件 parse 完才返回，Python __next__ 按提交顺序读取
// - ParseResultStream：rayon scope + crossbeam-channel，parse 完一个就 push 到 channel
//   Python __next__ 阻塞等待 channel，按完成顺序获取早完成的文件
//
// 用途：让 db_build.py 的"早完成 → 早写 DB → 早释放内存"管道成为可能。
// 大规模仓库（10万+ 文件）下，早完成的文件先写 DB 释放，避免主进程持有全部 parse 结果。
//
// Python 用法：
//   stream = batch_parse_c_files_stream(files, num_threads=8)
//   for result in stream:  # 按完成顺序获取
//       db.write_file_result(result)
//       del result  # 显式释放

/// 按完成顺序流式回传 parse 结果的迭代器。
///
/// 内部包装 crossbeam-channel Receiver，rayon worker parse 完一个文件就 push。
/// Python 端 `for r in stream:` 阻塞等待，按完成顺序获取早完成的文件。
#[pyclass]
pub struct ParseResultStream {
    receiver: crossbeam_channel::Receiver<ParseResult>,
}

#[pymethods]
impl ParseResultStream {
    /// Python 迭代器协议：__iter__ 返回自身
    fn __iter__(slf: Py<Self>) -> Py<Self> {
        slf
    }

    /// Python 迭代器协议：__next__ 阻塞等待 channel，按完成顺序返回下一个 parse 结果。
    ///
    /// 当所有文件 parse 完成、sender 全部 drop 后，channel 关闭，返回 None 终止迭代。
    fn __next__<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
        // 释放 GIL 等待 channel，避免阻塞其他 Python 线程
        let result = py.detach(|| -> Option<ParseResult> {
            self.receiver.recv().ok()
        });
        match result {
            Some(r) => Ok(Some(parse_result_to_pydict(py, &r)?)),
            None => Ok(None),
        }
    }

    /// 非阻塞尝试获取下一个已完成的结果（用于轮询场景）。
    ///
    /// 返回：
    /// - Some(dict) — 有已完成结果
    /// - None — 暂无结果或已结束（调用方需区分：用 is_done 判断）
    fn try_next<'py>(&self, py: Python<'py>) -> PyResult<Option<Bound<'py, PyAny>>> {
        let result = self.receiver.try_recv().ok();
        match result {
            Some(r) => Ok(Some(parse_result_to_pydict(py, &r)?)),
            None => Ok(None),
        }
    }

    /// 是否已结束（所有结果已取完且 channel 已关闭）。
    fn is_done(&self) -> bool {
        // is_empty 在 channel 关闭且为空时返回 true
        self.receiver.is_empty() && self.receiver.len() == 0
    }
}

/// 批量 parse C 文件，返回按完成顺序的流式迭代器（ParseResultStream）。
///
/// 与 batch_parse_c_files_pool 的区别：
/// - pool：等所有文件 parse 完才返回（par_iter().collect()）
/// - stream：parse 完一个就 push 到 channel，Python 端按完成顺序消费
///
/// 用途：L6 流式回传，让 db_build.py 能"早完成 → 早写 DB → 早释放内存"。
#[pyfunction]
#[pyo3(signature = (files, num_threads=None))]
fn batch_parse_c_files_stream(
    files: Vec<(String, String)>, // (abs_path, module_path)
    num_threads: Option<usize>,
) -> PyResult<ParseResultStream> {
    // 配置 rayon 线程数
    if let Some(n) = num_threads {
        rayon::ThreadPoolBuilder::new()
            .num_threads(n)
            .build_global()
            .ok();
    }

    // 创建 channel：容量等于文件数，避免 sender 阻塞（解析完即可立即 push）
    let (sender, receiver) = crossbeam_channel::bounded::<ParseResult>(files.len());

    // 启动后台线程跑 rayon scope，parse 完一个就 push 到 channel
    // sender 移动到闭包中，所有任务完成后 sender drop，channel 自动关闭
    std::thread::spawn(move || {
        let c_parser = Arc::new(CParser::new());
        // par_iter：rayon 内部调度，先完成的任务先 push
        files.par_iter().for_each(|(abs_path, module_path)| {
            let parser = c_parser.clone();
            let result = parser.parse_file(abs_path, module_path);
            // send 失败说明 receiver 已 drop（Python 端提前退出），忽略即可
            let _ = sender.send(result);
        });
        // sender 在闭包结束时 drop，channel 关闭
    });

    Ok(ParseResultStream { receiver })
}

/// 批量 parse C 文件，返回 Rust 侧持有的结果池（流式回传）
///
/// 与 batch_parse_c_files 的区别：
/// - batch_parse_c_files：一次性转成 Python list，主进程持有全部结果
/// - batch_parse_c_files_pool：结果存 Rust 侧，Python 按需 get_at(i) 读取
///
/// 内存对比（1.5M 符号场景）：
/// - batch_parse_c_files：主进程 10-14GB（全部 dict）
/// - batch_parse_c_files_pool：主进程 ~1MB（单个 dict）
#[pyfunction]
#[pyo3(signature = (files, num_threads=None))]
fn batch_parse_c_files_pool(
    files: Vec<(String, String)>, // (abs_path, module_path)
    num_threads: Option<usize>,
) -> PyResult<ParseResultPool> {
    // 配置 rayon 线程数
    if let Some(n) = num_threads {
        rayon::ThreadPoolBuilder::new()
            .num_threads(n)
            .build_global()
            .ok();
    }

    // grammar 只加载一次，Arc 共享给所有线程
    let c_parser = Arc::new(CParser::new());

    // rayon 并行 parse：结果存 Rust 侧 Vec（不转 Python dict）
    let results: Vec<ParseResult> = files
        .par_iter()
        .map(|(abs_path, module_path)| {
            let parser = c_parser.clone();
            parser.parse_file(abs_path, module_path)
        })
        .collect();

    Ok(ParseResultPool {
        results,
        iter_idx: AtomicUsize::new(0),
    })
}

// ============================================
// F11 方案 A: Rust 端并行构建 CSR → 一次性 dump
// ============================================
//
// 目标：跳过 Python→SQLite INSERT 阶段，直接从 parse 结果在 Rust 内存中构建 CSR。
//
// 背景瓶颈（见 docs/performance_report_million_symbols.md）：
// - SQLite 单写者模型 ~19万行/秒，10M 符号需 ~110s INSERT
// - Python→SQLite tuple 转换开销（每行 dict→tuple）
// - WAL checkpoint 时机无法控制
//
// 本函数实现方案 A 的 PoC：
// 1. rayon 并行 parse C 文件（复用 batch_parse_c_files_pool 的 grammar 共享）
// 2. 在 Rust 内直接构 SymbolTable + CallGraph（CSR）
// 3. 返回 GraphStore，可直接用于查询或 dump_to_file 持久化
//
// 与 Python 端 build_full_graph 的对比：
// | 阶段 | Python 路径 | Rust 路径 (本函数) |
// |------|------------|-------------------|
// | parse | Rust rayon (已下沉) | Rust rayon (相同) |
// | symbol INSERT | Python executemany | 跳过，直接构 Vec<GraphSymbol> |
// | call resolve | Python SQL JOIN | Rust 内存 HashMap 解析 |
// | call INSERT | Python executemany | 跳过，直接构 Vec<CallEdge> |
// | depth 计算 | Python BFS | Rust compute_depth_all |
// | FTS5 索引 | SQLite rebuild | 跳过（search 走 Rust memchr） |
//
// 持久化策略：调用方可选
// - 不持久化：daemon 内存查询，进程退出即丢失
// - dump_to_file：序列化到 .cwsnap，下次 load_from_file 零拷贝加载
// - dump_to_sqlite：异步写回 SQLite（未来实现，不阻塞返回）

/// 从 C 文件列表构建 GraphStore（方案 A：Rust 端并行构建，跳过 SQLite INSERT）
///
/// Python 用法：
///   from callwarden_core import build_graph_from_c_files
///   store = build_graph_from_c_files([("/path/a.c", "module.a"), ...], num_threads=8)
///   callers = store.get_callers("func_name")
///   store.dump_to_file("/path/to/snapshot.cwsnap")
///
/// 返回：(GraphStore, symbol_count, edge_count)
#[pyfunction]
#[pyo3(signature = (files, num_threads=None))]
fn build_graph_from_c_files<'py>(
    py: Python<'py>,
    files: Vec<(String, String)>, // (abs_path, module_path)
    num_threads: Option<usize>,
) -> PyResult<(Bound<'py, graph::GraphStore>, usize, usize)> {
    // 配置 rayon 线程数
    if let Some(n) = num_threads {
        rayon::ThreadPoolBuilder::new()
            .num_threads(n)
            .build_global()
            .ok();
    }

    // 释放 GIL 做 CPU 密集计算
    let (store, sym_count, edge_count) = py.detach(|| -> PyResult<(graph::GraphStore, usize, usize)> {
        // 1. rayon 并行 parse（grammar 共享）
        let c_parser = Arc::new(CParser::new());
        let results: Vec<ParseResult> = files
            .par_iter()
            .map(|(abs_path, module_path)| {
                let parser = c_parser.clone();
                parser.parse_file(abs_path, module_path)
            })
            .collect();

        // 2. 构建文件路径表（file_instance_id 用序号代替，不写 DB）
        // 每个 ParseResult 对应一个 file_instance_id（从 1 开始）
        let mut file_paths_pool = String::new();
        let mut file_paths_offsets: Vec<u32> = Vec::with_capacity(results.len() + 1);
        file_paths_offsets.push(0); // 哨兵：file_instance_id=0 表示无效
        for r in &results {
            let rel = if !r.rel_path.is_empty() {
                &r.rel_path
            } else {
                // 从 abs_path 推导 rel_path（去掉 workspace 前缀，简化处理）
                &r.abs_path
            };
            file_paths_offsets.push(file_paths_pool.len() as u32);
            file_paths_pool.push_str(rel);
        }
        // 末尾哨兵
        file_paths_offsets.push(file_paths_pool.len() as u32);

        // 3. 构建符号表（SymbolTable）
        // by_id[0] 保留为空槽（对应 sym.id=0，表示无效），与 load_from_sqlite 的语义一致
        let mut by_id: Vec<graph::GraphSymbol> = vec![graph::GraphSymbol {
            id: 0, file_instance_id: 0, kind: graph::SymbolKind::Unknown,
            name_offset: 0, name_len: 0, qname_offset: 0, qname_len: 0,
            module_offset: 0, module_len: 0, start_line: 0, end_line: 0, depth: -1,
        }];
        let mut name_pool = String::new();
        let mut qname_pool = String::new();
        let mut module_pool = String::new();
        // qname → symbol_id 映射（用于 call resolve）
        let mut qname_to_id: std::collections::HashMap<String, u32> = std::collections::HashMap::new();
        // simple_name → Vec<symbol_id>（用于 call resolve by name）
        let mut name_to_ids: std::collections::HashMap<String, Vec<u32>> = std::collections::HashMap::new();

        let mut sym_id_counter: u32 = 1; // 0 保留为无效
        for (file_idx, r) in results.iter().enumerate() {
            let file_instance_id = (file_idx + 1) as u32;
            for sym in &r.symbols {
                let id = sym_id_counter;
                sym_id_counter += 1;

                let name_offset = name_pool.len() as u32;
                let name_len = sym.name.len() as u32;
                name_pool.push_str(&sym.name);

                let qname_offset = qname_pool.len() as u32;
                let qname_len = sym.qualified_name.len() as u32;
                qname_pool.push_str(&sym.qualified_name);

                let module_offset = module_pool.len() as u32;
                let module_len = sym.module_path.len() as u32;
                module_pool.push_str(&sym.module_path);

                let graph_sym = graph::GraphSymbol {
                    id,
                    file_instance_id,
                    kind: graph::SymbolKind::from_db_str(&sym.kind),
                    name_offset, name_len,
                    qname_offset, qname_len,
                    module_offset, module_len,
                    start_line: sym.start_line,
                    end_line: sym.end_line,
                    depth: -1,
                };
                by_id.push(graph_sym);
                qname_to_id.insert(sym.qualified_name.clone(), id);
                name_to_ids.entry(sym.name.clone()).or_default().push(id);
            }
        }

        let sym_count = by_id.len() - 1; // 减去空槽

        // 4. 构建 CallEdge 列表（解析 callee_name → callee_id）
        let mut edges: Vec<graph::CallEdge> = Vec::new();
        let mut callee_names_pool = String::new();
        let mut callee_names_offsets: Vec<u32> = Vec::new();
        let mut callee_name_idx_map: std::collections::HashMap<String, u32> = std::collections::HashMap::new();

        // 按 caller_qualified 分组调用边（每个 ParseResult 的 calls 用其符号的 qname）
        for (file_idx, r) in results.iter().enumerate() {
            // 构建 file 内 qname → symbol_id 映射（用于解析 caller）
            // caller_qualified 在 parse 时已填充
            for call in &r.calls {
                let caller_qname = &call.caller_qualified;
                let caller_id = qname_to_id.get(caller_qname)
                    .copied()
                    .unwrap_or(0);

                // 解析 callee_name → callee_id
                // 优先按 qualified_name 精确匹配（如果 callee_module 含 ::）
                let callee_id = if !call.callee_module.is_empty() {
                    // 跨模块调用：尝试 callee_module + name 拼接
                    let full_qname = if call.callee_module.contains('.') {
                        format!("{}.{}", call.callee_module, call.callee_name)
                    } else {
                        call.callee_name.clone()
                    };
                    qname_to_id.get(&full_qname)
                        .copied()
                        .or_else(|| name_to_ids.get(&call.callee_name).and_then(|ids| ids.first().copied()))
                        .unwrap_or(0)
                } else {
                    // 同模块/同文件：按 simple_name 查找
                    name_to_ids.get(&call.callee_name)
                        .and_then(|ids| ids.first().copied())
                        .unwrap_or(0)
                };

                // callee_name_idx（用于 backward by name 查询）
                let callee_name_idx = match callee_name_idx_map.get(&call.callee_name) {
                    Some(&idx) => idx,
                    None => {
                        let idx = callee_names_offsets.len() as u32;
                        callee_names_offsets.push(callee_names_pool.len() as u32);
                        callee_names_pool.push_str(&call.callee_name);
                        callee_name_idx_map.insert(call.callee_name.clone(), idx);
                        idx
                    }
                };

                let call_line_packed = graph::CallEdge::pack_call_line(call.call_line, call.is_cross_file);
                edges.push(graph::CallEdge {
                    caller_id,
                    callee_id,
                    call_line_packed,
                    callee_name_idx,
                });
            }
        }
        let edge_count = edges.len();

        // 5. 构建排序索引（by_qname_sorted_ids, by_simple_name_sorted_ids, search_pool）
        // 注意：by_id 索引从 0 开始，sym.id 从 1 开始，所以 sym_id = idx + 1
        // by_qname_sorted_ids 存的是 sym.id（与 GraphStore.load_from_sqlite 的语义一致）
        let mut by_qname_sorted_ids: Vec<u32> = by_id.iter()
            .filter(|s| s.id != 0)
            .map(|s| s.id)
            .collect();
        by_qname_sorted_ids.sort_unstable_by(|a, b| {
            // sym.id 从 1 开始，by_id 索引从 0 开始，所以 idx = id - 1
            let sa = &by_id[*a as usize - 1];
            let sb = &by_id[*b as usize - 1];
            let qa = &qname_pool[sa.qname_offset as usize..(sa.qname_offset + sa.qname_len) as usize];
            let qb = &qname_pool[sb.qname_offset as usize..(sb.qname_offset + sb.qname_len) as usize];
            qa.cmp(qb)
        });
        let mut by_simple_name_sorted_ids = by_qname_sorted_ids.clone();
        by_simple_name_sorted_ids.sort_unstable_by(|a, b| {
            let sa = &by_id[*a as usize - 1];
            let sb = &by_id[*b as usize - 1];
            let na = &name_pool[sa.name_offset as usize..(sa.name_offset + sa.name_len) as usize];
            let nb = &name_pool[sb.name_offset as usize..(sb.name_offset + sb.name_len) as usize];
            na.cmp(nb).then_with(|| a.cmp(b))
        });

        // P2: 搜索索引（memchr SIMD 加速）
        let mut search_pool_lower = String::new();
        let mut search_entry_offsets: Vec<u32> = Vec::with_capacity(by_id.len() * 2);
        let mut search_entry_sym_ids: Vec<u32> = Vec::with_capacity(by_id.len() * 2);
        for sym in &by_id {
            if sym.id == 0 { continue; }
            let name = &name_pool[sym.name_offset as usize..(sym.name_offset + sym.name_len) as usize];
            if !name.is_empty() {
                search_entry_offsets.push(search_pool_lower.len() as u32);
                search_entry_sym_ids.push(sym.id);
                for c in name.chars() {
                    search_pool_lower.push(c.to_ascii_lowercase());
                }
                search_pool_lower.push('\0');
            }
            let qname = &qname_pool[sym.qname_offset as usize..(sym.qname_offset + sym.qname_len) as usize];
            if !qname.is_empty() && qname != name {
                search_entry_offsets.push(search_pool_lower.len() as u32);
                search_entry_sym_ids.push(sym.id);
                for c in qname.chars() {
                    search_pool_lower.push(c.to_ascii_lowercase());
                }
                search_pool_lower.push('\0');
            }
        }

        // 6. 组装 SymbolTable
        let symbols = Arc::new(graph::SymbolTable {
            by_id, by_qname_sorted_ids, by_simple_name_sorted_ids,
            file_paths_pool, file_paths_offsets,
            name_pool, qname_pool, module_pool,
            search_pool_lower, search_entry_offsets, search_entry_sym_ids,
        });

        // 7. 构建 CallGraph CSR（用 graph::build_csr 公开函数）
        // callee_names_offsets 末尾需要哨兵
        callee_names_offsets.push(callee_names_pool.len() as u32);
        // 使用 graph 模块的 build_csr 函数（max_id = sym_id_counter - 1）
        let max_id = (sym_id_counter - 1) as usize;
        let mut calls = graph::build_csr_public(
            edges,
            callee_names_pool,
            callee_names_offsets,
            max_id,
        );

        // 计算 roots（无 caller 的函数）
        let mut has_caller = vec![false; max_id + 1];
        for &position in &calls.backward_positions {
            let e = &calls.forward_edges[position as usize];
            if e.callee_id != 0 && (e.callee_id as usize) < has_caller.len() {
                has_caller[e.callee_id as usize] = true;
            }
        }
        // 遍历所有非空槽符号
        for sym in symbols.by_id.iter() {
            if sym.id == 0 { continue; }
            if (sym.id as usize) < has_caller.len() && !has_caller[sym.id as usize] && sym.kind == graph::SymbolKind::Fn {
                calls.roots.push(sym.id);
            }
        }

        // 构建 callee_name_sorted_idxs
        let (name_sorted, position_offsets, positions) = graph::build_callee_name_index_public(
            &calls.forward_edges,
            &calls.callee_names_pool,
            &calls.callee_names_offsets,
        );
        calls.callee_name_sorted_idxs = name_sorted;
        calls.callee_position_offsets = position_offsets;
        calls.callee_positions = positions;

        // 8. 组装 GraphStore
        let store = graph::GraphStore::new_with_data(symbols, Arc::new(calls));

        Ok((store, sym_count, edge_count))
    })?;

    // 转为 Python 对象
    let py_store = Bound::new(py, store)?;
    Ok((py_store, sym_count, edge_count))
}

// ============================================
// P28 保留：批量余弦相似度（原有功能）
// ============================================

use numpy::{PyArray1, PyReadonlyArray1, PyReadonlyArray2};

/// 批量余弦相似度：query (dim,) × matrix (N, dim) → scores (N,)
#[pyfunction]
fn batch_cosine_similarity<'py>(
    py: Python<'py>,
    query: PyReadonlyArray1<f32>,
    matrix: PyReadonlyArray2<f32>,
) -> PyResult<Bound<'py, PyArray1<f32>>> {
    let q = query.as_slice()?;
    let m = matrix.as_array();
    let (n, dim) = (m.nrows(), m.ncols());

    let q_norm: f32 = q.iter().map(|x| x * x).sum::<f32>().sqrt();
    if q_norm == 0.0 || n == 0 {
        return Ok(PyArray1::zeros(py, [n], false));
    }

    let mut scores = vec![0.0f32; n];
    for i in 0..n {
        let row = m.row(i);
        let mut dot = 0.0f32;
        let mut norm_sq = 0.0f32;
        for j in 0..dim {
            let v = row[j];
            dot += q[j] * v;
            norm_sq += v * v;
        }
        let n = norm_sq.sqrt();
        scores[i] = if n > 0.0 { dot / (q_norm * n) } else { 0.0 };
    }

    Ok(PyArray1::from_vec(py, scores))
}

// ============================================
// T-1783751519227-18d8: 输入规范化（PyO3 暴露）
// ============================================

/// Python 侧调用 canonicalize_source
///
/// 将文件规范化（BOM 剥离 + 编码检测 + CRLF→LF），返回 dict：
///   {
///     "canonical_bytes": bytes,      # 规范化后的字节
///     "content_hash": str,           # sha256(canonical_bytes)
///     "canonical_total": int,       # canonical_bytes 长度
///     "raw_total": int,             # 原始字节长度（含 BOM）
///     "metadata": {
///       "raw_hash": str,            # sha256(raw_bytes)
///       "source_encoding": str,     # "utf-8" / "utf-16-le" / "latin-1" 等
///       "bom_kind": str,            # "utf-8" / "utf-16-le" / "utf-16-be" / "none"
///       "newline_style": str,       # "crlf" / "lf" / "cr" / "none"
///     }
///   }
#[pyfunction]
fn canonicalize_source_py<'py>(py: Python<'py>, abs_path: &str) -> PyResult<Bound<'py, PyAny>> {
    let result = canonicalize::canonicalize_source(abs_path)
        .map_err(|e| pyo3::exceptions::PyIOError::new_err(e.to_string()))?;

    let dict = PyDict::new(py);

    // canonical_bytes → Python bytes
    let canonical_bytes = PyBytes::new(py, &result.canonical_bytes);
    dict.set_item("canonical_bytes", canonical_bytes)?;

    dict.set_item("content_hash", result.content_hash.clone())?;
    dict.set_item("canonical_total", result.canonical_total)?;
    dict.set_item("raw_total", result.raw_total)?;

    // metadata 嵌套 dict
    let metadata_dict = PyDict::new(py);
    metadata_dict.set_item("raw_hash", result.metadata.raw_hash.clone())?;
    metadata_dict.set_item("source_encoding", result.metadata.source_encoding.clone())?;
    metadata_dict.set_item("bom_kind", result.metadata.bom_kind.clone())?;
    metadata_dict.set_item("newline_style", result.metadata.newline_style.clone())?;
    dict.set_item("metadata", metadata_dict)?;

    Ok(dict.into_any().into_bound())
}

// ============================================
// 模块注册
// ============================================

#[pymodule]
fn callwarden_core(m: &Bound<'_, PyModule>) -> PyResult<()> {
    // P29: parse 热路径
    m.add_function(wrap_pyfunction!(batch_parse_c_files, m)?)?;
    m.add_function(wrap_pyfunction!(parse_c_file, m)?)?;
    m.add_function(wrap_pyfunction!(core_version, m)?)?;
    // P30: 流式回传 — Rust 侧持有结果，Python 按需读取
    m.add_class::<ParseResultPool>()?;
    m.add_function(wrap_pyfunction!(batch_parse_c_files_pool, m)?)?;
    // L6: 真正流式回传 — rayon + crossbeam-channel，按完成顺序返回
    m.add_class::<ParseResultStream>()?;
    m.add_function(wrap_pyfunction!(batch_parse_c_files_stream, m)?)?;
    // F11 方案 A: Rust 端并行构建 CSR → 一次性 dump
    m.add_function(wrap_pyfunction!(build_graph_from_c_files, m)?)?;
    // P31: 多语言 parser（config 驱动框架，支持 11 种语言）
    m.add_function(wrap_pyfunction!(multi_lang::parse_file_lang, m)?)?;
    m.add_function(wrap_pyfunction!(multi_lang::parse_canonical_bytes_py, m)?)?;
    m.add_function(wrap_pyfunction!(multi_lang::batch_parse_files_lang, m)?)?;
    m.add_function(wrap_pyfunction!(
        multi_lang::batch_parse_files_lang_pool,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(multi_lang::supported_languages, m)?)?;
    // P1-F Step 1: Parse 失败状态定义（设计 §5.3）
    m.add_function(wrap_pyfunction!(multi_lang::parse_status_from_fields, m)?)?;
    m.add_function(wrap_pyfunction!(
        multi_lang::parse_diagnostics_from_fields,
        m
    )?)?;
    // P28: 批量余弦相似度（保留）
    m.add_function(wrap_pyfunction!(batch_cosine_similarity, m)?)?;
    // B-PoC: 图存储 + 查询下沉（CSR 邻接表 + 内存索引 + rusqlite 加载）
    m.add_class::<graph::GraphStore>()?;
    m.add_class::<graph::CallersBatch>()?; // P10: get_callers 懒转换批量结果
    m.add_class::<graph::SymbolSearchBatch>()?; // P11: search_symbols 懒转换批量结果
    m.add_class::<graph::BlastRadiusBatch>()?; // Phase 6-1: blast_radius 懒转换批量结果
    // Phase 6-2: MinHash/LSH clone detection 核心
    m.add_function(wrap_pyfunction!(clone_detection::py_minhash_signature, m)?)?;
    m.add_function(wrap_pyfunction!(clone_detection::py_lsh_buckets, m)?)?;
    m.add_function(wrap_pyfunction!(clone_detection::py_batch_minhash_signatures, m)?)?;
    m.add_function(wrap_pyfunction!(clone_detection::py_lsh_candidate_pairs, m)?)?;
    m.add_function(wrap_pyfunction!(clone_detection::clone_detection_params, m)?)?;
    m.add_function(wrap_pyfunction!(clone_detection::py_detect_clones_core, m)?)?;
    // Phase 6-1 P2/P3: cross_layer_impact + defect_correlation Rust 短路
    m.add_function(wrap_pyfunction!(impact::py_cross_layer_impact, m)?)?;
    m.add_function(wrap_pyfunction!(impact::py_defect_correlation, m)?)?;
    // Phase 6-3 P1: 向量加载 + TopK 排序 Rust 短路
    m.add_function(wrap_pyfunction!(vector_topk::py_load_embeddings_from_blobs, m)?)?;
    m.add_function(wrap_pyfunction!(vector_topk::py_vector_topk, m)?)?;
                                                // Phase 4: GraphSnapshot + ArcSwap 原子发布
    m.add_class::<snapshot::PySnapshotManager>()?;
    m.add_class::<snapshot::PySnapshotCache>()?;
    // Phase 5: File Watcher (notify crate)
    m.add_class::<watcher::PyFileWatcher>()?;
    // Phase 5.1: DebouncedFileWatcher (debounce + batch coalescing)
    m.add_class::<watcher::PyDebouncedFileWatcher>()?;
    // Phase 5.2: HashDiffStore (content hash diff for false-positive filtering)
    m.add_class::<hash_diff::PyHashDiffStore>()?;
    // Phase 5.3: Parse Delta / Resolve Delta
    m.add_class::<delta::PyParseDelta>()?;
    m.add_class::<delta::PyResolveDelta>()?;
    m.add_class::<delta::PyDeltaComputer>()?;
    // Phase 5.4: Affected Frontier
    m.add_class::<frontier::PyAffectedFrontier>()?;
    m.add_function(wrap_pyfunction!(frontier::compute_frontier, m)?)?;
    m.add_function(wrap_pyfunction!(frontier::compute_frontier_with_budget, m)?)?;
    // Phase 5.5: Local Metrics Update (depth/cycle/impact)
    m.add_class::<metrics::PyDepthChange>()?;
    m.add_class::<metrics::PyCycleChange>()?;
    m.add_class::<metrics::PyImpactChange>()?;
    m.add_class::<metrics::PyLocalMetricsUpdate>()?;
    m.add_function(wrap_pyfunction!(metrics::compute_local_update, m)?)?;
    // Phase 6.1: Toolchain Fingerprint
    m.add_function(wrap_pyfunction!(toolchain::detect_compiler_type_py, m)?)?;
    m.add_function(wrap_pyfunction!(
        toolchain::compute_toolchain_fingerprint_py,
        m
    )?)?;
    // T-1783751519227-18d8: 输入规范化入口（BOM 剥离 + 编码检测 + CRLF→LF）
    m.add_function(wrap_pyfunction!(canonicalize_source_py, m)?)?;
    // Phase 1-1/2: SQLite schema_version 查询与 Rust schema migration
    m.add_function(wrap_pyfunction!(sqlite_query::sqlite_query_schema_version, m)?)?;
    m.add_function(wrap_pyfunction!(sqlite_query::sqlite_migrate_schema, m)?)?;
    // Phase 1-2: CAS 只读查询 API（compute_cas_key_v1 纯函数 + lookup/get_state/count_files/get_file_generation 只读查询）
    m.add_function(wrap_pyfunction!(cas_query::compute_cas_key_v1, m)?)?;
    m.add_function(wrap_pyfunction!(cas_query::compute_symbol_content_hash, m)?)?;
    m.add_function(wrap_pyfunction!(cas_query::cas_global_lookup, m)?)?;
    m.add_function(wrap_pyfunction!(cas_query::cas_global_get_state, m)?)?;
    m.add_function(wrap_pyfunction!(cas_query::cas_global_count_files, m)?)?;
    m.add_function(wrap_pyfunction!(cas_query::cas_local_get_file_generation, m)?)?;
    // Phase 1-3: workspace manifest 只读查询 API
    m.add_function(wrap_pyfunction!(manifest_query::manifest_get, m)?)?;
    m.add_function(wrap_pyfunction!(manifest_query::manifest_list, m)?)?;
    m.add_function(wrap_pyfunction!(manifest_query::manifest_count, m)?)?;
    m.add_function(wrap_pyfunction!(manifest_query::snapshot_get_files, m)?)?;
    m.add_function(wrap_pyfunction!(manifest_query::manifest_verify_raw_hash, m)?)?;
    // Phase 1-4: Replicator 只读查询 API
    m.add_function(wrap_pyfunction!(replicator_query::replicator_get_pending_count, m)?)?;
    // Phase 2-1: CAS→CodeGraph Merge PyO3 暴露层（cas_merge_to_codegraph + cas_merge_init_schema）
    m.add_function(wrap_pyfunction!(cas_merge_query::cas_merge_to_codegraph, m)?)?;
    m.add_function(wrap_pyfunction!(cas_merge_query::cas_merge_init_schema, m)?)?;
    // Phase 2-2: 批量 symbols 写入 PyO3 暴露层（batch_save_symbols）
    m.add_function(wrap_pyfunction!(batch_build_query::batch_save_symbols, m)?)?;
    // Phase 2-3: 调用边 resolve + 批量写入 PyO3 暴露层（batch_resolve_and_save_calls）
    m.add_function(wrap_pyfunction!(batch_calls_query::batch_resolve_and_save_calls, m)?)?;
    // Phase 2-4: 批量文件历史版本写入
    m.add_function(wrap_pyfunction!(batch_file_versions_query::batch_save_file_versions, m)?)?;
    // Phase 2-6-3: 批量文件注册
    m.add_function(wrap_pyfunction!(batch_register_query::batch_register_files, m)?)?;
    // Phase 3-4-1: StagingLog PyO3 暴露层（9 个 API）
    m.add_function(wrap_pyfunction!(staging_log_query::staging_log_append, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_query::staging_log_read, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_query::staging_log_read_pending, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_query::staging_log_mark_applied_batch, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_query::staging_log_mark_failed, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_query::staging_log_truncate, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_query::staging_log_compact_applied, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_query::staging_log_stats, m)?)?;
    m.add_function(wrap_pyfunction!(staging_log_query::staging_log_next_lsn, m)?)?;
    // Phase 3-4-2: ParseRetryLog PyO3 暴露层（9 个 API）
    m.add_function(wrap_pyfunction!(parse_retry_log_query::parse_retry_log_append, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_query::parse_retry_log_read, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_query::parse_retry_log_read_pending, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_query::parse_retry_log_read_retryable, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_query::parse_retry_log_mark_applied, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_query::parse_retry_log_mark_exhausted, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_query::parse_retry_log_increment_retry, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_query::parse_retry_log_compact, m)?)?;
    m.add_function(wrap_pyfunction!(parse_retry_log_query::parse_retry_log_next_lsn, m)?)?;
    // Phase 2-6-1: 增量构建 PyO3 暴露层（compute_and_apply_symbol_diff + load_file_result_from_db）
    m.add_function(wrap_pyfunction!(incremental_build_query::compute_and_apply_symbol_diff, m)?)?;
    m.add_function(wrap_pyfunction!(incremental_build_query::load_file_result_from_db, m)?)?;
    // Phase 4-1: UDS framing/SO_PEERCRED/RPC dispatch PyO3 暴露层（14 个 API）
    m.add_function(wrap_pyfunction!(daemon_query::protocol_constants, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::protocol_encode_payload, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::protocol_decode_payload, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::protocol_build_frame, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::protocol_parse_header, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::protocol_validate_message_size, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::protocol_parse_response, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::protocol_make_ok_response, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::protocol_make_error_response, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::peercred_is_available, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::peercred_info, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::dispatch_list_methods, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::dispatch_list_error_codes, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::dispatch_is_admin_method, m)?)?;
    // Phase 4-2: UID/workspace ACL、路径安全与资源预算（10 个 PyO3 API）
    m.add_function(wrap_pyfunction!(daemon_query::validate_owned_path, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::check_path_within_workspace, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::is_admin_uid, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::current_daemon_uid_py, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::check_workspace_owner, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::budget_create, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::budget_preset, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::budget_tracker_new, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::budget_tracker_visit_node, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::budget_tracker_truncate_results, m)?)?;

    // Phase 4-3: health_check_all PyO3 暴露（1 个 PyO3 API）
    // 契约：docs/design/phase4-3-metrics-health-audit-contract.md §3.1
    m.add_function(wrap_pyfunction!(daemon_query::health_check_all, m)?)?;

    // Phase 4-3 P1: metrics 纯计算（2 个 PyO3 API）
    // 契约：docs/design/phase4-3-metrics-health-audit-contract.md §3.2
    m.add_function(wrap_pyfunction!(daemon_query::metrics_percentile, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::metrics_format_labels, m)?)?;

    // Phase 4-3 P2: audit 纯计算（2 个 PyO3 API）
    // 契约：docs/design/phase4-3-metrics-health-audit-contract.md §3.3
    m.add_function(wrap_pyfunction!(daemon_query::audit_canonical_json, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::audit_compute_signature, m)?)?;

    // Phase 4-3 P3: backup 纯计算（2 个 PyO3 API）
    // 契约：docs/design/phase4-3-metrics-health-audit-contract.md §3.4
    m.add_function(wrap_pyfunction!(daemon_query::backup_compute_file_sha256, m)?)?;
    m.add_function(wrap_pyfunction!(daemon_query::backup_compute_meta_checksum, m)?)?;

    // Phase 5-1: CLI 配置加载 + 只读命令识别 PyO3 暴露（6 个 API）
    // 契约：docs/design/phase5-1-cli-config-contract.md §3.1 + §3.3
    m.add_function(wrap_pyfunction!(cli::config::platform_paths_detect, m)?)?;
    m.add_function(wrap_pyfunction!(cli::config::load_config_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::config::config_explain_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::config::check_role_supported_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::readonly::is_readonly_command_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::readonly::is_readonly_args_py, m)?)?;

    // Phase 5-1 B: 路由决策 PyO3 暴露（5 个 API）
    // 契约：docs/design/phase5-1b-router-contract.md §3
    m.add_function(wrap_pyfunction!(cli::router::get_daemon_mode_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::router::is_daemon_required_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::router::is_daemon_available_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::router::daemon_socket_path_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::router::route_command_py, m)?)?;

    // Phase 5-3: 兼容输出层 PyO3 暴露（13 个 API）
    // 契约：docs/design/phase5-3-output-layer-contract.md §3
    m.add_function(wrap_pyfunction!(cli::output::should_use_color_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::output::should_use_color_auto_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::output::colorize_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::output::cprint_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::output::success_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::output::error_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::output::warning_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::output::info_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::output::dim_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::output::bold_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::output::format_duration_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::output::format_size_py, m)?)?;
    m.add_function(wrap_pyfunction!(cli::output::json_dumps_pretty_py, m)?)?;

    // Phase 5-1 C: stats 子命令业务逻辑 PyO3 暴露（1 个 API）
    // 契约：docs/design/phase5-1c-stats-vertical-slice-contract.md §3
    m.add_function(wrap_pyfunction!(cli::stats::stats_command_run_py, m)?)?;

    // Phase 5-2 Slice 1: Daemon RPC Client PyO3 暴露（2 个跨平台 + 1 个 Unix-only API）
    // 跨平台：build_request_py / parse_rpc_response_py（Windows 可测）
    // Unix-only：daemon_client_call_py（UDS 客户端，仅 Linux/macOS）
    // 契约：docs/design/phase5-2-slice1-daemon-client-contract.md §3
    m.add_function(wrap_pyfunction!(daemon::client::build_request_py, m)?)?;
    m.add_function(wrap_pyfunction!(daemon::client::parse_rpc_response_py, m)?)?;
    #[cfg(unix)]
    {
        m.add_function(wrap_pyfunction!(daemon::client::daemon_client_call_py, m)?)?;
    }

    // Phase 5-2 Slice 2: query RPC 参数构建 PyO3 暴露（1 个跨平台 API）
    // 对齐 Python cli/daemon_commands.py:run_daemon_command 的 query 分支
    m.add_function(wrap_pyfunction!(daemon::client::build_query_request_py, m)?)?;

    // Phase 5-2 Slice 3: 简单 RPC 命令参数构建 PyO3 暴露（1 个跨平台 API）
    // 对齐 Python cli/daemon_commands.py:run_daemon_command 的 list/status/health/schema-version 分支
    m.add_function(wrap_pyfunction!(daemon::client::build_simple_request_py, m)?)?;

    // Phase 5-2 Slice 5: 剩余 RPC 命令参数构建 PyO3 暴露（1 个跨平台 API）
    // 对齐 Python cli/daemon_commands.py:run_daemon_command 的 register/backup/restore/gc/snapshot/mount 分支
    m.add_function(wrap_pyfunction!(daemon::client::build_rpc_request_py, m)?)?;

    // Phase 5-2 Slice 4: snapshot.publish 参数构建 PyO3 暴露（1 个跨平台 API）
    // 对齐 Python UnixDaemonRpcClient.publish_snapshot 的参数构建部分
    // FD 打开和 SCM_RIGHTS 传递是 Unix-only 副作用，不暴露给 Python
    m.add_function(wrap_pyfunction!(daemon::client::build_publish_params_py, m)?)?;

    // Phase 5-2 Slice 6: agent session 参数构建 PyO3 暴露（2 个跨平台 API）
    // 对齐 Python server/agent_protocol.py 的 connect/refresh 参数构建
    m.add_function(wrap_pyfunction!(daemon::client::build_connect_params_py, m)?)?;
    m.add_function(wrap_pyfunction!(daemon::client::build_refresh_params_py, m)?)?;
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    /// 辅助函数：构造 SymbolInfo（仅填必要字段，byte range 用于推导父子关系）
    fn make_symbol(
        name: &str,
        qualified: &str,
        byte_start: u32,
        byte_end: u32,
    ) -> SymbolInfo {
        SymbolInfo {
            name: name.to_string(),
            qualified_name: qualified.to_string(),
            kind: "fn".to_string(),
            start_line: 1,
            end_line: 1,
            module_path: "test".to_string(),
            symbol_hash: "h".to_string(),
            depth: -1,
            has_comment: false,
            visibility: "public".to_string(),
            content: String::new(),
            signature: String::new(),
            local_id: 0,
            lexical_parent_local_id: None,
            byte_start,
            byte_end,
        }
    }

    /// 辅助函数：构造 RawCall（caller_qualified 决定是否解析到调用者）
    fn make_call(caller_qualified: &str, byte_start: u32) -> RawCall {
        RawCall {
            callee_name: "callee".to_string(),
            callee_module: String::new(),
            caller_name: caller_qualified.to_string(),
            caller_qualified: caller_qualified.to_string(),
            call_line: 1,
            is_cross_file: false,
            caller_local_id: None,
            ordinal: 0,
            byte_start,
            byte_end: byte_start + 1,
        }
    }

    /// R14-P0-2 回归测试：NULL ABI 语义验证
    ///
    /// 企业设计 enterprise-phase1-phase3-detail.md:1074-1077 要求：
    /// - `lexical_parent_local_id` 用 None（NULL）表示顶层，不用 0
    /// - `caller_local_id` 用 None（NULL）表示顶层裸调用，不用 0
    ///
    /// 复审 §P0-2 指出原实现用 u32 + 0 哨兵，无法区分"顶层"与
    /// "synthetic module symbol local_id=0"。本测试验证 Option<u32> 实现：
    /// 1. 顶层符号 lexical_parent_local_id == None
    /// 2. 嵌套符号 lexical_parent_local_id == Some(父 local_id)
    /// 3. 已解析调用 caller_local_id == Some(调用者 local_id)
    /// 4. 未解析调用（caller_qualified 不匹配任何符号）caller_local_id == None
    #[test]
    fn test_r14_null_abi_for_parent_and_caller() {
        // 构造 2 个符号：outer (byte 0-100) 包含 inner (byte 10-50)
        // outer 是顶层 → lexical_parent_local_id == None
        // inner 嵌套在 outer 中 → lexical_parent_local_id == Some(outer.local_id)
        let mut symbols = vec![
            make_symbol("outer", "mod.outer", 0, 100),
            make_symbol("inner", "mod.outer.inner", 10, 50),
        ];
        // 构造 2 个调用：
        // call1: caller_qualified="mod.outer" → 解析到 outer，caller_local_id=Some(1)
        // call2: caller_qualified="mod.unknown" → 未解析，caller_local_id=None
        let mut calls = vec![
            make_call("mod.outer", 20),
            make_call("mod.unknown", 70),
        ];

        assign_local_ids(&mut symbols, &mut calls);

        // 验证 local_id（1-based，按 byte_start 排序）
        // outer (byte_start=0) → local_id=1
        // inner (byte_start=10) → local_id=2
        assert_eq!(symbols[0].local_id, 1, "outer.local_id 应为 1（1-based）");
        assert_eq!(symbols[1].local_id, 2, "inner.local_id 应为 2");

        // 验证 lexical_parent_local_id（NULL ABI）
        assert_eq!(
            symbols[0].lexical_parent_local_id,
            None,
            "R14-P0-2: outer 是顶层，lexical_parent_local_id 应为 None（NULL），不是 0"
        );
        assert_eq!(
            symbols[1].lexical_parent_local_id,
            Some(1),
            "R14-P0-2: inner 嵌套在 outer 中，lexical_parent_local_id 应为 Some(1)"
        );

        // 验证 caller_local_id（NULL ABI）
        // calls 按 caller_local_id 排序：None 排在最前，Some(1) 排在后
        // 排序后：call2 (None) → ordinal=0, call1 (Some(1)) → ordinal=0
        let call_with_none = calls
            .iter()
            .find(|c| c.caller_local_id.is_none())
            .expect("应有一个 caller_local_id=None 的调用（未解析）");
        assert_eq!(
            call_with_none.caller_qualified, "mod.unknown",
            "未解析调用的 caller_qualified 应为 mod.unknown"
        );

        let call_with_some = calls
            .iter()
            .find(|c| c.caller_local_id == Some(1))
            .expect("应有一个 caller_local_id=Some(1) 的调用（解析到 outer）");
        assert_eq!(
            call_with_some.caller_qualified, "mod.outer",
            "已解析调用的 caller_qualified 应为 mod.outer"
        );
    }

    /// R14-P0-2 回归测试：所有符号都在顶层时，lexical_parent_local_id 全为 None
    #[test]
    fn test_r14_all_top_level_symbols_have_none_parent() {
        // 两个不重叠的符号都是顶层
        let mut symbols = vec![
            make_symbol("foo", "mod.foo", 0, 10),
            make_symbol("bar", "mod.bar", 20, 30),
        ];
        let mut calls: Vec<RawCall> = vec![];

        assign_local_ids(&mut symbols, &mut calls);

        for s in &symbols {
            assert_eq!(
                s.lexical_parent_local_id,
                None,
                "R14-P0-2: 顶层符号 {} 的 lexical_parent_local_id 应为 None",
                s.name
            );
        }
    }

    /// R14-P0-2 回归测试：所有调用都未解析时，caller_local_id 全为 None
    #[test]
    fn test_r14_all_unresolved_calls_have_none_caller() {
        let mut symbols = vec![make_symbol("foo", "mod.foo", 0, 10)];
        // caller_qualified 不匹配任何符号
        let mut calls = vec![
            make_call("mod.unknown1", 5),
            make_call("mod.unknown2", 6),
        ];

        assign_local_ids(&mut symbols, &mut calls);

        for c in &calls {
            assert_eq!(
                c.caller_local_id,
                None,
                "R14-P0-2: 未解析调用 {} 的 caller_local_id 应为 None",
                c.caller_qualified
            );
        }
    }
}
