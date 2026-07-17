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

mod canonicalize;
// R7: daemon/snapshot 模块需对 cw_daemon binary 可见（bin 与 lib 在同一 crate，但
// 默认 mod 是私有的。改为 pub mod 让 binary 入口能 use callwarden_core::daemon::*
pub mod daemon;
mod delta;
mod diff;
mod frontier;
mod graph;
mod hash_diff;
mod metrics;
mod multi_lang;
// R7: cw_daemon 需要 SnapshotCache 类型（daemon/snapshot_state.rs 中使用）
pub mod snapshot;
mod toolchain;
mod watcher;

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
    pub error: Option<String>,
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
                    error: Some(format!("read error: {}", e)),
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
                error: Some("set_language failed".to_string()),
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
                    error: Some("parse returned None".to_string()),
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
            error: None,
        }
    }
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
        signature: String::new(),
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
        signature: String::new(),
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
        signature: String::new(),
    })
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
            d.into_any().into_bound()
        })
        .collect();
    dict.set_item("raw_calls", calls)?;

    dict.set_item("imports", r.imports.clone())?;

    if let Some(err) = &r.error {
        dict.set_item("error", err.clone())?;
    }

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
    // P31: 多语言 parser（config 驱动框架，支持 11 种语言）
    m.add_function(wrap_pyfunction!(multi_lang::parse_file_lang, m)?)?;
    m.add_function(wrap_pyfunction!(multi_lang::parse_canonical_bytes_py, m)?)?;
    m.add_function(wrap_pyfunction!(multi_lang::batch_parse_files_lang, m)?)?;
    m.add_function(wrap_pyfunction!(
        multi_lang::batch_parse_files_lang_pool,
        m
    )?)?;
    m.add_function(wrap_pyfunction!(multi_lang::supported_languages, m)?)?;
    // P28: 批量余弦相似度（保留）
    m.add_function(wrap_pyfunction!(batch_cosine_similarity, m)?)?;
    // B-PoC: 图存储 + 查询下沉（CSR 邻接表 + 内存索引 + rusqlite 加载）
    m.add_class::<graph::GraphStore>()?;
    m.add_class::<graph::CallersBatch>()?; // P10: get_callers 懒转换批量结果
    m.add_class::<graph::SymbolSearchBatch>()?; // P11: search_symbols 懒转换批量结果
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
    Ok(())
}
