//! P31: 多语言 parser 框架
//!
//! 配置驱动的 tree-sitter parser，统一 walk 逻辑提取符号 + 调用 + import。
//! 支持 15 种语言（C 保留专用 parser；其余 15 语言全 Rust 化：
//! python/rust/go/java/ts/js/ruby/php/scala/csharp/cpp/kotlin/swift/elixir/hcl）。
//!
//! 设计原则：
//! - 每语言一份 LangConfig，配置节点 kind 映射和名称提取策略
//! - 统一 walk_node 递归，同时提取符号、调用关系、import
//! - 名称提取策略：ChildByType / FieldName / PositionBefore / ChildByTypeNested /
//!   ImplTraitForType / CallArgName / HclLabels（后两个为 Elixir/HCL 专用）
//! - 调用关系按当前函数上下文标注 caller
//!
//! P0-C Step 0: 各语言配置函数已拆分到 languages/ 目录下的按语言模块。
//! 本文件保留通用框架代码（类型定义、walker、PyO3 接口）。

use tree_sitter::{Language, Node, Parser};
use std::sync::Arc;
use rayon::prelude::*;
use serde::{Deserialize, Serialize};
use crate::{
    ParseResult, SymbolInfo, RawCall, RawReference, ParseResultPool,
    find_child, node_text, make_qualified, blake_hash, parse_result_to_pydict,
};

// ============================================
// 名称提取策略
// ============================================

/// 名称提取策略 — 不同语言的 AST 中符号名称位置不同
#[derive(Clone, Debug)]
pub enum NameStrategy {
    /// 遍历 named_children 找首个匹配 kind 的子节点（可多个候选 kind）
    /// 适用于：Python/Go/Java/TS/Ruby/Scala/C#/Kotlin/Swift 等
    ChildByType(Vec<&'static str>),

    /// 通过 child_by_field_name 提取
    /// 适用于：Rust（field "name"/"type"）
    FieldName(&'static str),

    /// 位置定位：在 terminator 前找 name_kind 的 identifier
    /// 适用于：C# 方法名在 parameter_list 之前、PHP 方法名在 formal_parameters 之前
    PositionBefore {
        terminator: &'static str,
        name_kind: &'static str,
    },

    /// 嵌套查找：先找 intermediate 节点，再在其中找 name_kinds
    /// 适用于：C/C++ function_definition → function_declarator → identifier
    ChildByTypeNested {
        intermediate: &'static str,
        name_kinds: Vec<&'static str>,
    },

    /// Rust impl 块专用：trait_field + type_field 拼接为 "Trait for Type" 或 "Type"
    /// 对齐 Python rust_parser._parse_impl 的 name 格式
    ImplTraitForType {
        trait_field: &'static str,
        type_field: &'static str,
    },

    /// Elixir 专用：从 arguments 内的特定结构提取名称
    /// 流程：在 container（如 "arguments"）内找首个 child_kind 节点，
    /// 再提取该节点内（或自身）首个 name_kind 子节点的文本。
    /// - defmodule：container="arguments", child_kind="alias", name_kind="alias"（取自身）
    /// - def/defp：container="arguments", child_kind="call", name_kind="identifier"
    CallArgName {
        container: &'static str,
        child_kind: &'static str,
        name_kind: &'static str,
    },

    /// HCL 专用：收集所有 string_lit 子节点的 template_literal 文本作为标签
    /// - 2+ 标签：name = "labels[0].labels[1]"（resource/data 风格，如 aws_instance.web）
    /// - 1 标签：name = "labels[0]"
    /// - 0 标签：name = fallback（块类型文本）
    HclLabels {
        fallback: &'static str,
    },

    /// PHP property 专用：3 层嵌套提取
    /// property_declaration → property_element → variable_name → name
    /// 返回 name 节点文本（已去掉 $ 前缀，因为 $ 是 variable_name 的匿名子节点）
    /// P0-C Step 2: 新增，支持 PHP property 符号提取
    PhpProperty,
}

// ============================================
// 规则定义
// ============================================

/// 符号提取规则
#[derive(Clone, Debug)]
pub struct SymbolRule {
    /// AST 节点 kind（如 "function_definition", "class_declaration"）
    pub kind: &'static str,
    /// 名称提取策略
    pub name: NameStrategy,
    /// 符号 kind 字段值（如 "fn", "class", "struct"）
    pub sym_kind: &'static str,
    /// 体节点 kind（如 "block", "class_body"），用于递归提取嵌套符号和调用
    pub body: Option<&'static str>,
    /// 是否为函数（true = 设置调用上下文）
    pub is_fn: bool,
    /// 动态 kind：遍历子节点，第一个匹配的 (child_kind, sym_kind) 决定符号 kind
    /// 用于 Go 的 type_spec → struct_type/interface_type
    pub dynamic_kind: Vec<(&'static str, &'static str)>,
    /// 调用关键字过滤：当 kind 匹配时，要求首个 identifier 子节点文本等于此值才命中
    /// 用于 Elixir：所有声明都是 call 节点，需按 identifier 文本区分 defmodule/def/defp/...
    /// None 时不做文本过滤（默认行为，不影响已接入的 13 种语言）
    pub call_keyword: Option<&'static str>,
    /// 按子节点文本映射 sym_kind：遍历 named_children，首个 identifier 子节点的文本
    /// 匹配此映射则决定 sym_kind。用于 HCL：block 节点统一为 kind="block"，
    /// 块类型（resource/provider/variable/...）由首个 identifier 文本决定。
    /// 空时不做文本映射（默认行为，不影响已接入的 13 种语言）
    pub kind_from_child_text: Vec<(&'static str, &'static str)>,
    /// 按提取到的符号名映射 sym_kind：当 extract_name 返回的 name 匹配此映射时，
    /// 用映射值覆盖 sym_kind。用于 TS/JS：method_definition 的 name="constructor"
    /// 时 sym_kind 应为 "constructor" 而非 "method"。
    /// 空时不做名称映射（默认行为）。
    /// P0-C Step 1: 新增字段，支持 TS/JS constructor kind 区分
    pub kind_from_name: Vec<(&'static str, &'static str)>,
    /// 要求父节点 kind 匹配此值才命中规则。None 时不限制父节点（默认行为）。
    /// 用于 C++：function_definition 在 field_declaration_list 内为 method，
    /// 在 declaration_list 或文件作用域为 function。
    /// P0-C Step 4: 新增字段，支持 C++ method/function 区分
    pub require_parent_kind: Option<&'static str>,
    /// 当 true 且提取到的 name 等于 parent_qualified 的最后一段（即类名）时，
    /// 将 sym_kind 覆盖为 "constructor"。用于 C++ 构造函数检测（名称与类名相同）。
    /// P0-C Step 4: 新增字段，支持 C++ constructor 检测
    pub constructor_if_name_matches_parent: bool,
}

impl SymbolRule {
    /// 快速构造（无动态 kind / call_keyword / kind_from_child_text / kind_from_name）
    // P0-C Step 0: 改为 pub(crate) 以便 languages 子模块复用
    pub(crate) const fn new(
        kind: &'static str,
        name: NameStrategy,
        sym_kind: &'static str,
        body: Option<&'static str>,
        is_fn: bool,
    ) -> Self {
        Self {
            kind, name, sym_kind, body, is_fn,
            dynamic_kind: vec![],
            call_keyword: None,
            kind_from_child_text: vec![],
            kind_from_name: vec![],
            require_parent_kind: None,
            constructor_if_name_matches_parent: false,
        }
    }

    /// P0-C Step 1: 链式设置 kind_from_name（用于 TS/JS constructor 区分）
    /// 返回 Self 以便在配置函数中链式调用：
    ///   SymbolRule::new(...).with_kind_from_name(vec![("constructor", "constructor")])
    pub(crate) fn with_kind_from_name(mut self, mapping: Vec<(&'static str, &'static str)>) -> Self {
        self.kind_from_name = mapping;
        self
    }

    /// P0-C Step 4: 链式设置 require_parent_kind（用于 C++ method/function 区分）
    pub(crate) fn with_require_parent_kind(mut self, parent_kind: &'static str) -> Self {
        self.require_parent_kind = Some(parent_kind);
        self
    }

    /// P0-C Step 4: 链式设置 constructor_if_name_matches_parent（用于 C++ constructor 检测）
    pub(crate) fn with_constructor_detection(mut self) -> Self {
        self.constructor_if_name_matches_parent = true;
        self
    }
}

/// 调用提取规则
#[derive(Clone, Debug)]
pub struct CallRule {
    /// 调用节点 kind（如 "call_expression", "call", "method_invocation"）
    pub kind: &'static str,
    /// callee 字段名（如 "function", "callee", "method"）
    /// None 时用 find_child(node, "identifier") 提取
    pub callee_field: Option<&'static str>,
}

/// P0-D: 引用提取规则（HCL attribute traversal）
/// 用于声明式语言中属性表达式内的跨块引用，如 HCL 的：
///   value = aws_instance.web.private_ip
/// expression 子节点包含 variable_expr + get_attr 链，需提取为引用关系。
#[derive(Clone, Debug)]
pub struct ReferenceRule {
    /// attribute 节点 kind（如 HCL 的 "attribute"）
    pub attribute_kind: &'static str,
    /// expression 子节点 kind（如 HCL 的 "expression"）
    pub expression_kind: &'static str,
    /// variable 节点 kind（如 HCL 的 "variable_expr"）
    pub variable_kind: &'static str,
    /// get_attr 节点 kind（如 HCL 的 "get_attr"）
    pub get_attr_kind: &'static str,
}

/// P0-D: import 指令规则（Elixir alias/import/use/require）
/// Elixir 中这 4 个关键字都是 call 节点，但语义上是 import 指令，
/// 不应作为普通 call 处理，需提取为 import。
#[derive(Clone, Debug)]
pub struct ImportDirective {
    /// call 节点首个 identifier 文本（如 "alias", "import", "use", "require"）
    pub keyword: &'static str,
    /// arguments 子节点 kind（Elixir 固定为 "arguments"）
    pub arguments_kind: &'static str,
    /// alias 子节点 kind（Elixir 固定为 "alias"）
    pub alias_kind: &'static str,
}

/// 语言配置
pub struct LangConfig {
    pub lang_id: &'static str,
    pub language: Language,
    pub symbol_rules: Vec<SymbolRule>,
    pub call_rules: Vec<CallRule>,
    pub import_kinds: Vec<&'static str>,
    /// P0-D: import 指令规则（Elixir alias/import/use/require）
    /// 匹配 call 节点首个 identifier 文本，提取为 import 而非普通 call
    pub import_directives: Vec<ImportDirective>,
    /// P0-D: 引用提取规则（HCL attribute traversal）
    /// 匹配 attribute 节点，提取 expression 中的 variable_expr + get_attr 链
    pub reference_rules: Vec<ReferenceRule>,
    /// 跳过的节点 kind：既不提取符号也不递归子节点
    /// 用于 Rust 的 mod_item（Python 不提取到 symbols，放到 inline_modules）
    pub skip_kinds: Vec<&'static str>,
}

impl LangConfig {
    /// 按 language_id 获取配置
    /// P0-C Step 0: 实现委托给 languages 模块（按语言拆分到 languages/{lang}.rs）
    pub fn get(lang_id: &str) -> Option<Self> {
        crate::languages::get_config(lang_id)
    }

    /// 获取支持的语言列表
    /// P0-D Step 3: HCL 已加入 Rust supported_languages。
    /// HCL 的"调用关系"是 attribute 中的引用（如 `value = aws_instance.web.public_ip`），
    /// 通过 ReferenceRule + walk_node 的 reference 提取路径处理，
    /// 不再依赖 Python parser 的 _extract_refs_from_expression。
    pub fn supported_languages() -> Vec<&'static str> {
        vec![
            "python", "rust", "go", "java", "typescript", "javascript",
            "ruby", "php", "scala", "csharp", "cpp",
            "kotlin", "swift",
            "elixir",
            "hcl",
        ]
    }
}

// ============================================
// 通用 parser
// ============================================

/// 配置驱动的多语言 parser
pub struct GenericParser {
    config: Arc<LangConfig>,
}

impl GenericParser {
    pub fn new(config: Arc<LangConfig>) -> Self {
        Self { config }
    }

    /// parse 单个文件，提取符号 + 调用 + import + 引用
    pub fn parse_file(&self, abs_path: &str, module_path: &str) -> ParseResult {
        let source = match std::fs::read(abs_path) {
            Ok(s) => s,
            Err(e) => {
                return error_result(abs_path, module_path, self.config.lang_id,
                                    &format!("read error: {}", e));
            }
        };

        let mut parser = Parser::new();
        if parser.set_language(&self.config.language).is_err() {
            return error_result(abs_path, module_path, self.config.lang_id,
                                "set_language failed");
        }

        let tree = match parser.parse(&source, None) {
            Some(t) => t,
            None => {
                return error_result(abs_path, module_path, self.config.lang_id,
                                    "parse returned None");
            }
        };

        let content_hash = format!("{:x}", blake_hash(&source));
        let total_lines = source.iter().filter(|&&b| b == b'\n').count() as u32 + 1;

        let mut symbols = Vec::new();
        let mut calls = Vec::new();
        let mut imports = Vec::new();
        let mut references = Vec::new();

        let root = tree.root_node();
        walk_node(
            &root, &source, &self.config, module_path, "",
            "", "",
            &mut symbols, &mut calls, &mut imports, &mut references,
        );

        ParseResult {
            rel_path: String::new(),
            abs_path: abs_path.to_string(),
            module_path: module_path.to_string(),
            content_hash,
            total_lines,
            language: self.config.lang_id.to_string(),
            symbols,
            calls,
            imports,
            references,
            error: None,
        }
    }

    /// Parse canonical bytes（已 BOM 剥离 + CRLF 归一化 + UTF-8 编码）
    ///
    /// T-1783751519227-18d8: 供 delta.rs 调用，避免 parse_file 直接读文件绕过
    /// canonicalize_source。content_hash 由调用方（canonicalize 阶段）提供，
    /// 不再从原始字节计算，确保 hash 基于规范化后的内容。
    pub fn parse_canonical_bytes(
        &self,
        canonical_bytes: &[u8],
        abs_path: &str,
        module_path: &str,
        content_hash: &str,
    ) -> ParseResult {
        let mut parser = Parser::new();
        if parser.set_language(&self.config.language).is_err() {
            return error_result(abs_path, module_path, self.config.lang_id,
                                "set_language failed");
        }

        let tree = match parser.parse(canonical_bytes, None) {
            Some(t) => t,
            None => {
                return error_result(abs_path, module_path, self.config.lang_id,
                                    "parse returned None");
            }
        };

        // 使用 canonical bytes 的行数（CRLF 已归一化为 LF）
        let total_lines = canonical_bytes.iter().filter(|&&b| b == b'\n').count() as u32 + 1;

        let mut symbols = Vec::new();
        let mut calls = Vec::new();
        let mut imports = Vec::new();
        let mut references = Vec::new();

        let root = tree.root_node();
        walk_node(
            &root, canonical_bytes, &self.config, module_path, "",
            "", "",
            &mut symbols, &mut calls, &mut imports, &mut references,
        );

        ParseResult {
            rel_path: String::new(),
            abs_path: abs_path.to_string(),
            module_path: module_path.to_string(),
            content_hash: content_hash.to_string(),
            total_lines,
            language: self.config.lang_id.to_string(),
            symbols,
            calls,
            imports,
            references,
            error: None,
        }
    }
}

/// 构造错误结果
fn error_result(abs_path: &str, module_path: &str, lang_id: &str, err: &str) -> ParseResult {
    ParseResult {
        rel_path: String::new(),
        abs_path: abs_path.to_string(),
        module_path: module_path.to_string(),
        content_hash: String::new(),
        total_lines: 0,
        language: lang_id.to_string(),
        symbols: Vec::new(),
        calls: Vec::new(),
        imports: Vec::new(),
        references: Vec::new(),
        error: Some(err.to_string()),
    }
}

// ============================================
// 统一 walk 逻辑
// ============================================

/// 递归遍历 AST，同时提取符号、调用关系、import、引用
///
/// Walker 逻辑：
/// 1. 匹配符号规则 → 提取符号 → 递归进 body（设置新的调用上下文）
/// 2. P0-D: 匹配 import 指令（Elixir alias/import/use/require）→ 提取 import → 不递归
/// 3. 匹配调用规则 → 提取调用 → 递归子节点（调用可能嵌套）
/// 4. 匹配 import kind → 提取 import → 不递归
/// 5. P0-D: 匹配引用规则（HCL attribute traversal）→ 提取引用 + raw_calls → 递归子节点
/// 6. 默认 → 递归子节点（保持当前调用上下文）
///
/// 调用上下文：current_fn / current_qualified 跟踪当前所在函数或 block，
/// 只有在函数体内（current_fn 非空）才记录调用关系和引用。
fn walk_node(
    node: &Node,
    source: &[u8],
    config: &LangConfig,
    module_path: &str,
    parent_qualified: &str,
    current_fn: &str,
    current_qualified: &str,
    symbols: &mut Vec<SymbolInfo>,
    calls: &mut Vec<RawCall>,
    imports: &mut Vec<String>,
    references: &mut Vec<RawReference>,
) {
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        let kind = child.kind();

        // 0. 跳过指定 kind（不提取符号、不递归）
        if config.skip_kinds.contains(&kind) {
            continue;
        }

        // 1. 检查符号规则（kind + call_keyword + require_parent_kind 都匹配）
        //    Elixir 的 defmodule/def/defp 等都是 call 节点，需按首个 identifier 文本过滤
        //    P0-C Step 4: require_parent_kind 用于 C++ 区分类内方法 vs 自由函数
        let parent_kind = node.kind();
        let rule_match = config.symbol_rules.iter().find(|r| {
            if r.kind != kind { return false; }
            // P0-C Step 4: 父节点 kind 必须匹配（若配置了 require_parent_kind）
            if let Some(req_pk) = r.require_parent_kind {
                if parent_kind != req_pk { return false; }
            }
            if let Some(kw) = r.call_keyword {
                // Elixir：要求首个 identifier 子节点文本等于 kw
                return find_child(&child, "identifier")
                    .map(|n| node_text(&n, source) == kw)
                    .unwrap_or(false);
            }
            true
        });
        if let Some(rule) = rule_match {
            // 计算 actual_kind：优先 kind_from_child_text（HCL），其次 dynamic_kind（Go），兜底 sym_kind
            // kind_from_child_text 配置但未匹配时返回 None（HCL：未知块类型，跳过符号提取）
            let actual_kind_opt: Option<&'static str> = if !rule.kind_from_child_text.is_empty() {
                find_child(&child, "identifier").and_then(|n| {
                    let text = node_text(&n, source);
                    rule.kind_from_child_text.iter()
                        .find_map(|(txt, kind)| if text == *txt { Some(*kind) } else { None })
                })
                // 注意：不设 unwrap_or(rule.sym_kind)，匹配失败返回 None
            } else if !rule.dynamic_kind.is_empty() {
                // Go：按子节点 kind 映射，匹配失败兜底 sym_kind
                Some(rule.dynamic_kind.iter()
                    .find_map(|(child_kind, sym_kind)| {
                        if find_child(&child, child_kind).is_some() {
                            Some(*sym_kind)
                        } else {
                            None
                        }
                    })
                    .unwrap_or(rule.sym_kind))
            } else {
                Some(rule.sym_kind)
            };

            if let Some(actual_kind) = actual_kind_opt {
                if let Some(name) = extract_name(&child, source, &rule.name) {
                    // P0-C Step 1: kind_from_name 映射（TS/JS constructor 区分）
                    // 提取到 name 后，若 name 命中 kind_from_name 映射，覆盖 actual_kind
                    let final_kind = if !rule.kind_from_name.is_empty() {
                        rule.kind_from_name.iter()
                            .find_map(|(n, k)| if *n == name.as_str() { Some(*k) } else { None })
                            .unwrap_or(actual_kind)
                    } else {
                        actual_kind
                    };
                    // P0-C Step 4: constructor_if_name_matches_parent（C++ 构造函数检测）
                    // 当配置了此标志且 name 等于 parent_qualified 的最后一段（类名）时，
                    // 覆盖 kind 为 "constructor"
                    let final_kind = if rule.constructor_if_name_matches_parent && !parent_qualified.is_empty() {
                        let parent_class = parent_qualified.rsplit('.').next().unwrap_or("");
                        if name.as_str() == parent_class { "constructor" } else { final_kind }
                    } else {
                        final_kind
                    };
                    let qualified = make_qualified(module_path, parent_qualified, &name);
                    let sym = make_symbol(&child, source, module_path, &name, &qualified, final_kind);
                    symbols.push(sym);

                    // 设置新的调用上下文
                    let (new_fn, new_qual) = if rule.is_fn {
                        (name.as_str(), qualified.as_str())
                    } else {
                        (current_fn, current_qualified)
                    };

                    // 递归进 body
                    if let Some(body_kind) = rule.body {
                        if let Some(body) = find_child(&child, body_kind) {
                            walk_node(
                                &body, source, config, module_path, &qualified,
                                new_fn, new_qual,
                                symbols, calls, imports, references,
                            );
                        }
                    }
                }
            } else {
                // kind_from_child_text 未匹配（HCL：未知块类型），仍递归子节点提取嵌套
                walk_node(
                    &child, source, config, module_path, parent_qualified,
                    current_fn, current_qualified,
                    symbols, calls, imports, references,
                );
            }
            continue;
        }

        // 1.5. P0-D: 检查 import 指令（Elixir alias/import/use/require）
        //      这些是 call 节点但语义上是 import 指令，不作为普通 call 处理
        if !config.import_directives.is_empty() {
            if let Some(directive) = config.import_directives.iter().find(|d| {
                find_child(&child, "identifier")
                    .map(|n| node_text(&n, source) == d.keyword)
                    .unwrap_or(false)
            }) {
                if let Some(args) = find_child(&child, directive.arguments_kind) {
                    if let Some(alias_node) = find_child(&args, directive.alias_kind) {
                        imports.push(node_text(&alias_node, source).to_string());
                    }
                }
                continue;  // 不作为普通 call 处理，不递归
            }
        }

        // 2. 检查调用规则
        if let Some(call_rule) = config.call_rules.iter().find(|r| r.kind == kind) {
            if !current_fn.is_empty() {
                if let Some(callee_text) = extract_callee(&child, source, call_rule.callee_field) {
                    let (callee_name, callee_module) = split_callee(&callee_text);
                    calls.push(RawCall {
                        callee_name,
                        callee_module,
                        caller_name: current_fn.to_string(),
                        caller_qualified: current_qualified.to_string(),
                        call_line: child.start_position().row as u32 + 1,
                        is_cross_file: false,
                    });
                }
            }
            // 继续遍历子节点（调用可能嵌套）
            walk_node(
                &child, source, config, module_path, parent_qualified,
                current_fn, current_qualified,
                symbols, calls, imports, references,
            );
            continue;
        }

        // 3. 检查 import
        if config.import_kinds.contains(&kind) {
            let imp = node_text(&child, source).to_string();
            imports.push(clean_import(&imp));
            continue;
        }

        // 4. P0-D: 检查引用规则（HCL attribute traversal）
        //    attribute 节点内 expression 含 variable_expr + get_attr 链，
        //    提取为 references + raw_calls（向后兼容 Python parser 行为）
        if let Some(ref_rule) = config.reference_rules.iter().find(|r| r.attribute_kind == kind) {
            if let Some(expr) = find_child(&child, ref_rule.expression_kind) {
                extract_traversal_references(
                    &expr, source, ref_rule,
                    module_path, current_fn, current_qualified,
                    calls, references,
                );
            }
            // 递归子节点（attribute 内可能有嵌套结构）
            walk_node(
                &child, source, config, module_path, parent_qualified,
                current_fn, current_qualified,
                symbols, calls, imports, references,
            );
            continue;
        }

        // 5. 默认：递归子节点
        walk_node(
            &child, source, config, module_path, parent_qualified,
            current_fn, current_qualified,
            symbols, calls, imports, references,
        );
    }
}

/// P0-D: 从 HCL expression 中提取 attribute traversal 引用
///
/// expression 内含 variable_expr + get_attr 链，如：
///   aws_instance.web.private_ip
///   variable_expr(get_attr(identifier='aws_instance'))
///     + get_attr(identifier='web')
///     + get_attr(identifier='private_ip')
///
/// 提取逻辑：
/// - 遍历 expression 的 named_children，找 variable_expr 起始的链
/// - 拼接 variable_expr.identifier + 后续 get_attr.identifier 形成完整 traversal
/// - 至少含 1 个 get_attr（即 >= 2 段）才记录为引用
/// - references.callee_name = 前 2 段（资源地址，如 aws_instance.web）
/// - raw_calls.callee_name = 完整 traversal（如 aws_instance.web.private_ip）
fn extract_traversal_references(
    expr: &Node,
    source: &[u8],
    ref_rule: &ReferenceRule,
    module_path: &str,
    current_fn: &str,
    current_qualified: &str,
    calls: &mut Vec<RawCall>,
    references: &mut Vec<RawReference>,
) {
    if current_fn.is_empty() {
        return;  // 不在 block 上下文中，不记录引用
    }

    let call_line = expr.start_position().row as u32 + 1;

    // 遍历 expression 的 named_children，找 variable_expr 起始的链
    let mut cursor = expr.walk();
    let children: Vec<Node> = expr.named_children(&mut cursor).collect();

    let mut i = 0;
    while i < children.len() {
        if children[i].kind() == ref_rule.variable_kind {
            // 找到 variable_expr，提取其 identifier 文本作为链起始
            let var_node = &children[i];
            let var_ident = match find_child(var_node, "identifier") {
                Some(n) => node_text(&n, source).to_string(),
                None => { i += 1; continue; }
            };

            // 收集后续连续的 get_attr
            let mut segments: Vec<String> = vec![var_ident.clone()];
            let mut j = i + 1;
            while j < children.len() && children[j].kind() == ref_rule.get_attr_kind {
                if let Some(attr_ident) = find_child(&children[j], "identifier") {
                    segments.push(node_text(&attr_ident, source).to_string());
                }
                j += 1;
            }

            // 至少含 1 个 get_attr（即 >= 2 段）才记录为引用
            if segments.len() >= 2 {
                let source_text = segments.join(".");
                // callee_name = 前 2 段（资源地址，如 aws_instance.web）
                // 若只有 2 段则与 source_text 相同
                let resource_address = if segments.len() >= 2 {
                    format!("{}.{}", segments[0], segments[1])
                } else {
                    segments[0].clone()
                };

                // references: 语义化引用（资源地址）
                references.push(RawReference {
                    caller_name: current_fn.to_string(),
                    callee_name: resource_address.clone(),
                    call_line,
                    reference_kind: "attribute_traversal".to_string(),
                    source_text: source_text.clone(),
                });

                // raw_calls: 完整 traversal 文本（向后兼容 Python parser 行为）
                calls.push(RawCall {
                    callee_name: source_text,
                    callee_module: module_path.to_string(),
                    caller_name: current_fn.to_string(),
                    caller_qualified: current_qualified.to_string(),
                    call_line,
                    is_cross_file: false,
                });
            }

            i = j;
        } else {
            i += 1;
        }
    }
}

// ============================================
// 名称提取
// ============================================

/// 按策略从 AST 节点提取符号名称
fn extract_name(node: &Node, source: &[u8], strategy: &NameStrategy) -> Option<String> {
    match strategy {
        NameStrategy::ChildByType(kinds) => {
            for kind in kinds {
                if let Some(child) = find_child(node, kind) {
                    return Some(node_text(&child, source).to_string());
                }
            }
            None
        }
        NameStrategy::FieldName(field) => {
            node.child_by_field_name(field)
                .map(|n| node_text(&n, source).to_string())
        }
        NameStrategy::PositionBefore { terminator, name_kind } => {
            // 在 named_children 中找到 terminator，记录之前的 name_kind 节点
            let mut cursor = node.walk();
            let mut name_node = None;
            for child in node.named_children(&mut cursor) {
                if child.kind() == *terminator {
                    break;
                }
                if child.kind() == *name_kind {
                    name_node = Some(child);
                }
            }
            name_node.map(|n| node_text(&n, source).to_string())
        }
        NameStrategy::ChildByTypeNested { intermediate, name_kinds } => {
            let intermediate_node = find_child(node, intermediate)?;
            for kind in name_kinds {
                if let Some(child) = find_child(&intermediate_node, kind) {
                    return Some(node_text(&child, source).to_string());
                }
            }
            None
        }
        NameStrategy::ImplTraitForType { trait_field, type_field } => {
            // Rust impl 块：有 trait 字段时 name = "Trait for Type"，否则 "Type"
            let type_name = node.child_by_field_name(type_field)
                .map(|n| node_text(&n, source).to_string())
                .unwrap_or_default();
            if let Some(trait_node) = node.child_by_field_name(trait_field) {
                let trait_name = node_text(&trait_node, source);
                Some(format!("{} for {}", trait_name, type_name))
            } else {
                Some(type_name)
            }
        }
        NameStrategy::CallArgName { container, child_kind, name_kind } => {
            // Elixir 专用：在 container（如 "arguments"）内找首个 child_kind 节点，
            // 再提取该节点内（或自身）首个 name_kind 子节点的文本
            let container_node = find_child(node, container)?;
            let mut cursor = container_node.walk();
            for inner in container_node.named_children(&mut cursor) {
                if inner.kind() != *child_kind { continue; }
                if *child_kind == *name_kind {
                    // defmodule：container="arguments", child_kind="alias", name_kind="alias"
                    // alias 节点本身就是名称，直接取其文本
                    return Some(node_text(&inner, source).to_string());
                }
                // def/defp：container="arguments", child_kind="call", name_kind="identifier"
                // 在 call 节点内找 identifier 子节点
                if let Some(name_child) = find_child(&inner, name_kind) {
                    return Some(node_text(&name_child, source).to_string());
                }
            }
            None
        }
        NameStrategy::HclLabels { fallback } => {
            // HCL 专用：收集所有 string_lit 子节点的 template_literal 文本作为标签
            // - 2+ 标签：name = "labels[0].labels[1]"（resource/data 风格，如 aws_instance.web）
            // - 1 标签：name = "labels[0]"
            // - 0 标签：name = fallback
            let mut labels: Vec<String> = Vec::new();
            let mut cursor = node.walk();
            for child in node.named_children(&mut cursor) {
                if child.kind() == "string_lit" {
                    // string_lit 内有 template_literal 子节点（或直接是字符串内容）
                    if let Some(tpl) = find_child(&child, "template_literal") {
                        labels.push(node_text(&tpl, source).to_string());
                    } else {
                        // 兜底：直接取 string_lit 整体文本并去引号
                        let raw = node_text(&child, source);
                        let stripped = raw.trim_matches('"').to_string();
                        labels.push(stripped);
                    }
                }
            }
            match labels.len() {
                0 => Some((*fallback).to_string()),
                1 => Some(labels[0].clone()),
                _ => Some(format!("{}.{}", labels[0], labels[1])),
            }
        }
        NameStrategy::PhpProperty => {
            // P0-C Step 2: PHP property_declaration → property_element → variable_name → name
            // $ 是 variable_name 的匿名子节点，name 是命名子节点（已无 $ 前缀）
            let prop_elem = find_child(node, "property_element")?;
            let var_name = find_child(&prop_elem, "variable_name")?;
            let name_node = find_child(&var_name, "name")?;
            Some(node_text(&name_node, source).to_string())
        }
    }
}

// ============================================
// 调用关系提取
// ============================================

/// 提取调用表达式的 callee 文本
fn extract_callee(node: &Node, source: &[u8], field: Option<&str>) -> Option<String> {
    let callee = match field {
        Some(f) => node.child_by_field_name(f)?,
        None => find_child(node, "identifier")
            .or_else(|| find_child(node, "simple_identifier"))
            // P0-C Step 3: Scala field_expression（calc.add）作为 callee
            // field_expression 文本为 "calc.add"，由 split_callee 拆分为 (add, calc)
            .or_else(|| find_child(node, "field_expression"))
            // P0-C Step 3: Scala instance_expression（new Calculator）的 type_identifier
            .or_else(|| find_child(node, "type_identifier"))
            // P0-D Step 2: Elixir dot 节点（IO.puts）作为 callee
            // dot 文本为 "IO.puts"，由 split_callee 拆分为 (puts, IO)
            .or_else(|| find_child(node, "dot"))?,
    };
    Some(node_text(&callee, source).to_string())
}

/// 将 callee 文本拆分为 (callee_name, callee_module)
///
/// 处理成员调用：
/// - `foo.bar()` → (name="bar", module="foo")
/// - `Foo::bar()` → (name="bar", module="Foo")
/// - `obj->method()` → (name="method", module="obj")
/// - `Namespace\func()` → (name="func", module="Namespace")  PHP 命名空间
/// - `func()` → (name="func", module="")
fn split_callee(text: &str) -> (String, String) {
    // 优先匹配 :: 和 ->（C++/Rust 风格）
    if let Some(pos) = text.rfind("::") {
        return (text[pos + 2..].to_string(), text[..pos].to_string());
    }
    if let Some(pos) = text.rfind("->") {
        return (text[pos + 2..].to_string(), text[..pos].to_string());
    }
    // PHP 命名空间分隔符 \（在 . 之前匹配，避免被 . 截断）
    if let Some(pos) = text.rfind('\\') {
        return (text[pos + 1..].to_string(), text[..pos].to_string());
    }
    // 最后一个 . 分隔（Python/Java/TS/Go 风格）
    if let Some(pos) = text.rfind('.') {
        return (text[pos + 1..].to_string(), text[..pos].to_string());
    }
    (text.to_string(), String::new())
}

// ============================================
// 符号构造
// ============================================

/// 从 AST 节点构造 SymbolInfo
fn make_symbol(
    node: &Node,
    source: &[u8],
    module_path: &str,
    name: &str,
    qualified: &str,
    kind: &str,
) -> SymbolInfo {
    let content = node_text(node, source).to_string();
    SymbolInfo {
        name: name.to_string(),
        qualified_name: qualified.to_string(),
        kind: kind.to_string(),
        start_line: node.start_position().row as u32 + 1,
        end_line: node.end_position().row as u32 + 1,
        module_path: module_path.to_string(),
        symbol_hash: format!("{:x}", blake_hash(content.as_bytes())),
        depth: -1,
        has_comment: false,
        visibility: extract_visibility(node, source),
        content,
        signature: String::new(),
    }
}

/// 提取符号可见性
///
/// P0-C Step 2: PHP 用 visibility_modifier 节点包裹 public/protected/private。
/// 其他语言暂保持 "public" 默认（Phase 2.7 待补全其他语言的 visibility 提取）。
fn extract_visibility(node: &Node, source: &[u8]) -> String {
    if let Some(vis_mod) = find_child(node, "visibility_modifier") {
        let text = node_text(&vis_mod, source);
        if text.contains("private") {
            return "private".to_string();
        } else if text.contains("protected") {
            return "protected".to_string();
        } else if text.contains("public") {
            return "public".to_string();
        }
    }
    "public".to_string()
}

// ============================================
// import 文本清理
// ============================================

/// 清理 import 文本：去除关键字、引号、分号
fn clean_import(text: &str) -> String {
    text
        .trim_start_matches("import ")
        .trim_start_matches("use ")
        .trim_start_matches("from ")
        .trim_start_matches("using ")
        .trim_start_matches("require_relative ")
        .trim_start_matches("require ")
        .trim_start_matches("namespace ")
        .trim_matches(|c: char| {
            c == '"' || c == '\'' || c == ';' || c == '`' || c.is_whitespace()
        })
        .to_string()
}

// ============================================
// PyO3 接口
// ============================================

use pyo3::prelude::*;
use pyo3::types::PyAny;
use pyo3::Bound;
use pyo3::BoundObject; // P1-F: PyO3 0.29 需要 trait 导入才能用 into_bound()

/// 解析单个文件（多语言）
///
/// Python 调用：
///   from callwarden_core import parse_file_lang
///   result = parse_file_lang("/path/foo.py", "module.foo", "python")
#[pyfunction]
#[pyo3(signature = (abs_path, module_path, language))]
pub fn parse_file_lang<'py>(
    py: Python<'py>,
    abs_path: &str,
    module_path: &str,
    language: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let config = LangConfig::get(language)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err(
            format!("不支持的语言: {}，支持: {:?}", language, LangConfig::supported_languages())
        ))?;
    let parser = GenericParser::new(Arc::new(config));
    let result = parser.parse_file(abs_path, module_path);
    parse_result_to_pydict(py, &result)
}

/// 解析已规范化的 canonical bytes（不读文件）
///
/// Python 调用：
///   from callwarden_core import parse_canonical_bytes_py
///   result = parse_canonical_bytes_py(canonical_bytes, "module.foo", "python", content_hash)
///
/// 消除 TOCTOU：daemon 先 canonicalize + hash，再传同一份 bytes 给 parser，
/// CAS key 与 parse 来自同一份 canonical bytes（parse-input-abi.md §2）。
#[pyfunction]
#[pyo3(signature = (canonical_bytes, module_path, language, content_hash))]
pub fn parse_canonical_bytes_py<'py>(
    py: Python<'py>,
    canonical_bytes: &[u8],
    module_path: &str,
    language: &str,
    content_hash: &str,
) -> PyResult<Bound<'py, PyAny>> {
    let config = LangConfig::get(language)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err(
            format!("不支持的语言: {}，支持: {:?}", language, LangConfig::supported_languages())
        ))?;
    let parser = GenericParser::new(Arc::new(config));
    // abs_path 用 module_path 占位——canonical bytes 已从 daemon 侧验证
    let result = parser.parse_canonical_bytes(
        canonical_bytes, module_path, module_path, content_hash,
    );
    parse_result_to_pydict(py, &result)
}

/// 批量解析文件（多语言，rayon 并行）
///
/// Python 调用：
///   from callwarden_core import batch_parse_files_lang
///   results = batch_parse_files_lang([("/path/a.py", "mod.a"), ...], "python", num_threads=8)
#[pyfunction]
#[pyo3(signature = (files, language, num_threads=None))]
pub fn batch_parse_files_lang<'py>(
    py: Python<'py>,
    files: Vec<(String, String)>,
    language: &str,
    num_threads: Option<usize>,
) -> PyResult<Vec<Bound<'py, PyAny>>> {
    let config = LangConfig::get(language)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err(
            format!("不支持的语言: {}", language)
        ))?;

    if let Some(n) = num_threads {
        rayon::ThreadPoolBuilder::new()
            .num_threads(n)
            .build_global()
            .ok();
    }

    let config = Arc::new(config);
    let results: Vec<ParseResult> = files
        .par_iter()
        .map(|(abs_path, module_path)| {
            let parser = GenericParser::new(config.clone());
            parser.parse_file(abs_path, module_path)
        })
        .collect();

    let mut py_results = Vec::with_capacity(results.len());
    for r in results {
        py_results.push(parse_result_to_pydict(py, &r)?);
    }
    Ok(py_results)
}

/// 批量解析文件（多语言，流式回传 ParseResultPool）
///
/// Python 调用：
///   from callwarden_core import batch_parse_files_lang_pool
///   pool = batch_parse_files_lang_pool(files, "python", num_threads=8)
///   for i in range(pool.len()):
///       result = pool.get_at(i)
#[pyfunction]
#[pyo3(signature = (files, language, num_threads=None))]
pub fn batch_parse_files_lang_pool(
    files: Vec<(String, String)>,
    language: &str,
    num_threads: Option<usize>,
) -> PyResult<ParseResultPool> {
    let config = LangConfig::get(language)
        .ok_or_else(|| pyo3::exceptions::PyValueError::new_err(
            format!("不支持的语言: {}", language)
        ))?;

    if let Some(n) = num_threads {
        rayon::ThreadPoolBuilder::new()
            .num_threads(n)
            .build_global()
            .ok();
    }

    let config = Arc::new(config);
    let results: Vec<ParseResult> = files
        .par_iter()
        .map(|(abs_path, module_path)| {
            let parser = GenericParser::new(config.clone());
            parser.parse_file(abs_path, module_path)
        })
        .collect();

    Ok(ParseResultPool {
        results,
        iter_idx: std::sync::atomic::AtomicUsize::new(0),
    })
}

/// 获取支持的语言列表
#[pyfunction]
pub fn supported_languages() -> Vec<&'static str> {
    LangConfig::supported_languages()
}

// ============================================
// P1-F Step 1: Parse 失败状态定义（设计 §5.3）
// ============================================

/// Parse 结果状态（设计 §5.3 错误语义）
///
/// | 状态 | 行为 |
/// |------|------|
/// | `Ok` | 发布完整 ParseFact |
/// | `Partial` | 发布可用事实并持久化 diagnostics，不冒充完整成功 |
/// | `Unsupported` | 不发布空图谱，记录语言/构造并进入可观测失败 |
/// | `Failed` | 不替换上一代可查询 snapshot，记录失败并允许重试 |
/// | `Stale` | generation CAS 拒绝，不覆盖新状态 |
///
/// 设计原则：
/// - `parse_status_from_result` 只能推导出 `Ok` / `Partial` / `Failed`
/// - `Unsupported` 由调用方在 parse 之前判断语言是否支持时显式设置
/// - `Stale` 由 daemon CAS 层在 generation 拒绝时显式设置
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "snake_case")]
pub enum ParseStatus {
    Ok,
    Partial,
    Unsupported,
    Failed,
    Stale,
}

impl ParseStatus {
    pub fn as_str(&self) -> &'static str {
        match self {
            ParseStatus::Ok => "ok",
            ParseStatus::Partial => "partial",
            ParseStatus::Unsupported => "unsupported",
            ParseStatus::Failed => "failed",
            ParseStatus::Stale => "stale",
        }
    }

    /// 是否发布完整或部分 ParseFact
    ///
    /// 设计 §5.3：`Ok` / `Partial` 发布事实；`Unsupported` / `Failed` / `Stale`
    /// 不发布（避免空图谱覆盖上一代 snapshot）。
    pub fn publishes_fact(&self) -> bool {
        matches!(self, ParseStatus::Ok | ParseStatus::Partial)
    }

    /// 是否允许重试
    ///
    /// 设计 §5.3：`Failed` 允许重试（daemon 重启后重放）；
    /// `Stale` / `Unsupported` 不允许重试（永久拒绝）。
    pub fn allows_retry(&self) -> bool {
        matches!(self, ParseStatus::Failed)
    }

    /// 是否替换上一代 snapshot
    ///
    /// 设计 §5.3：只有 `Ok` / `Partial` 替换 snapshot；
    /// `Failed` 不替换（保留上一代可查询 snapshot）；
    /// `Stale` / `Unsupported` 不替换。
    pub fn replaces_snapshot(&self) -> bool {
        matches!(self, ParseStatus::Ok | ParseStatus::Partial)
    }
}

impl std::fmt::Display for ParseStatus {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// Parse 诊断信息（设计 §5.2 输出契约 + §5.3 错误语义）
///
/// 每个语言至少覆盖：syntax error count / unsupported construct count /
/// partial parse marker / fatal parse error。本 struct 提供可序列化的
/// 诊断载体，供 daemon 持久化到 durable log（P1-F Step 3）。
#[derive(Debug, Clone, Default, Serialize, Deserialize)]
pub struct ParseDiagnostics {
    /// 状态字符串（"ok" / "partial" / "unsupported" / "failed" / "stale"）
    pub status: String,
    /// 语法错误数量（tree-sitter has_error 节点数）
    pub syntax_error_count: u32,
    /// 不支持的构造数量（lang rule 未覆盖的 AST 节点）
    pub unsupported_construct_count: u32,
    /// 致命解析错误（parse 返回 None / set_language 失败 / IO 错误）
    pub fatal_parse_error: Option<String>,
    /// 是否为部分解析（有 syntax error 或 unsupported construct，但仍发布了事实）
    pub partial_parse: bool,
    /// 兼容顶层 error 字段（与 ParseResult.error 对齐）
    pub error: Option<String>,
}

impl ParseDiagnostics {
    /// 从 ParseResult 推导诊断信息
    ///
    /// 注意：`Unsupported` 和 `Stale` 不能从 ParseResult 推导，
    /// 需调用方使用 `unsupported()` / `stale()` 显式构造。
    pub fn from_result(result: &ParseResult) -> Self {
        if let Some(err) = &result.error {
            return Self {
                status: ParseStatus::Failed.as_str().to_string(),
                syntax_error_count: 0,
                unsupported_construct_count: 0,
                fatal_parse_error: Some(err.clone()),
                partial_parse: false,
                error: Some(err.clone()),
            };
        }
        // 当前 ParseResult struct 不携带 syntax_error_count / unsupported_construct_count
        // 字段（定义在 lib.rs，未含这些字段）。tree-sitter 的语法错误信息需要从
        // tree.root_node().has_error() 提取，但当前 GenericParser::parse_file /
        // parse_canonical_bytes 未提取该信息到 ParseResult。
        //
        // P1-F Step 1 仅定义状态语义和推导骨架，syntax_error_count /
        // unsupported_construct_count 的实际值在后续 step 或 languages/ 模块补齐。
        // 当前默认 Ok 状态。
        Self {
            status: ParseStatus::Ok.as_str().to_string(),
            syntax_error_count: 0,
            unsupported_construct_count: 0,
            fatal_parse_error: None,
            partial_parse: false,
            error: None,
        }
    }

    /// 显式构造 `Unsupported` 状态（语言不支持时调用方使用）
    pub fn unsupported(language: &str) -> Self {
        let msg = format!("unsupported language: {}", language);
        Self {
            status: ParseStatus::Unsupported.as_str().to_string(),
            syntax_error_count: 0,
            unsupported_construct_count: 0,
            fatal_parse_error: Some(msg.clone()),
            partial_parse: false,
            error: Some(msg),
        }
    }

    /// 显式构造 `Stale` 状态（generation CAS 拒绝时调用方使用）
    pub fn stale(reason: &str) -> Self {
        Self {
            status: ParseStatus::Stale.as_str().to_string(),
            syntax_error_count: 0,
            unsupported_construct_count: 0,
            fatal_parse_error: Some(reason.to_string()),
            partial_parse: false,
            error: Some(reason.to_string()),
        }
    }

    /// 显式构造 `Failed` 状态（parse 异常时调用方使用）
    pub fn failed(reason: &str) -> Self {
        Self {
            status: ParseStatus::Failed.as_str().to_string(),
            syntax_error_count: 0,
            unsupported_construct_count: 0,
            fatal_parse_error: Some(reason.to_string()),
            partial_parse: false,
            error: Some(reason.to_string()),
        }
    }

    /// 转为 serde_json::Value（供 daemon durable log 序列化）
    pub fn to_json(&self) -> serde_json::Value {
        serde_json::to_value(self).unwrap_or_else(|_| {
            serde_json::json!({
                "status": "failed",
                "error": "diagnostics serialization failed",
            })
        })
    }
}

/// 从 ParseResult 推导 ParseStatus（不含 Unsupported/Stale）
///
/// 规则：
/// - `result.error.is_some()` → `Failed`
/// - 无 error，且（syntax_error_count > 0 或 unsupported_construct_count > 0）→ `Partial`
/// - 无 error，无 syntax/unsupported 问题 → `Ok`
///
/// 注意：当前 ParseResult 不携带 syntax_error_count / unsupported_construct_count，
/// 所以只能返回 `Ok` / `Failed`。`Partial` 状态需调用方补充诊断信息后判断。
pub fn parse_status_from_result(result: &ParseResult) -> ParseStatus {
    if result.error.is_some() {
        return ParseStatus::Failed;
    }
    // TODO: 后续 step 补充 syntax_error_count / unsupported_construct_count 检测
    ParseStatus::Ok
}

/// 从 ParseResult 推导 ParseDiagnostics
pub fn parse_diagnostics_from_result(result: &ParseResult) -> ParseDiagnostics {
    ParseDiagnostics::from_result(result)
}

/// PyO3 接口：从 result 字段推导权威 ParseStatus 字符串
///
/// Python 调用：
///   from callwarden_core import parse_status_from_fields
///   status = parse_status_from_fields(error, syntax_error_count, unsupported_construct_count)
///
/// 供 RustParserFacade.extract_diagnostics() 调用，确保 Python 与 Rust 状态判定一致。
#[pyfunction]
#[pyo3(signature = (error, syntax_error_count=0, unsupported_construct_count=0))]
pub fn parse_status_from_fields(
    error: Option<String>,
    syntax_error_count: u32,
    unsupported_construct_count: u32,
) -> String {
    if error.is_some() {
        return ParseStatus::Failed.as_str().to_string();
    }
    if syntax_error_count > 0 || unsupported_construct_count > 0 {
        return ParseStatus::Partial.as_str().to_string();
    }
    ParseStatus::Ok.as_str().to_string()
}

/// PyO3 接口：从 result 字段推导权威 ParseDiagnostics（dict 形式）
///
/// Python 调用：
///   from callwarden_core import parse_diagnostics_from_fields
///   diag = parse_diagnostics_from_fields(error, syntax_error_count, unsupported_construct_count)
#[pyfunction]
#[pyo3(signature = (error, syntax_error_count=0, unsupported_construct_count=0))]
pub fn parse_diagnostics_from_fields<'py>(
    py: Python<'py>,
    error: Option<String>,
    syntax_error_count: u32,
    unsupported_construct_count: u32,
) -> PyResult<Bound<'py, PyAny>> {
    let status = parse_status_from_fields(error.clone(), syntax_error_count, unsupported_construct_count);
    let fatal_parse_error = error.clone();
    let partial_parse = status == ParseStatus::Partial.as_str();

    let dict = pyo3::types::PyDict::new(py);
    dict.set_item("status", status)?;
    dict.set_item("syntax_error_count", syntax_error_count)?;
    dict.set_item("unsupported_construct_count", unsupported_construct_count)?;
    dict.set_item("fatal_parse_error", fatal_parse_error)?;
    dict.set_item("partial_parse", partial_parse)?;
    dict.set_item("error", error)?;
    Ok(dict.into_any().into_bound())
}
