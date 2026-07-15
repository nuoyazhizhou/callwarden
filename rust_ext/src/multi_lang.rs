//! P31: 多语言 parser 框架
//!
//! 配置驱动的 tree-sitter parser，统一 walk 逻辑提取符号 + 调用 + import。
//! 支持 13 种语言（C 保留专用 parser；Kotlin/Swift 已补齐；
//! Elixir/HCL 因 AST 结构特殊保持 Python fallback）。
//!
//! 设计原则：
//! - 每语言一份 LangConfig，配置节点 kind 映射和名称提取策略
//! - 统一 walk_node 递归，同时提取符号、调用关系、import
//! - 三种名称提取策略：ChildByType / FieldName / PositionBefore / ChildByTypeNested
//! - 调用关系按当前函数上下文标注 caller

use tree_sitter::{Language, Node, Parser};
use std::sync::Arc;
use rayon::prelude::*;
use crate::{
    ParseResult, SymbolInfo, RawCall, ParseResultPool,
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
}

impl SymbolRule {
    /// 快速构造（无动态 kind）
    const fn new(
        kind: &'static str,
        name: NameStrategy,
        sym_kind: &'static str,
        body: Option<&'static str>,
        is_fn: bool,
    ) -> Self {
        Self { kind, name, sym_kind, body, is_fn, dynamic_kind: vec![] }
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

/// 语言配置
pub struct LangConfig {
    pub lang_id: &'static str,
    pub language: Language,
    pub symbol_rules: Vec<SymbolRule>,
    pub call_rules: Vec<CallRule>,
    pub import_kinds: Vec<&'static str>,
    /// 跳过的节点 kind：既不提取符号也不递归子节点
    /// 用于 Rust 的 mod_item（Python 不提取到 symbols，放到 inline_modules）
    pub skip_kinds: Vec<&'static str>,
}

impl LangConfig {
    /// 按 language_id 获取配置
    pub fn get(lang_id: &str) -> Option<Self> {
        let config = match lang_id {
            "python" => python_config(),
            "rust" => rust_config(),
            "go" => go_config(),
            "java" => java_config(),
            "typescript" => typescript_config(),
            "javascript" => javascript_config(),
            "ruby" => ruby_config(),
            "php" => php_config(),
            "scala" => scala_config(),
            "csharp" => csharp_config(),
            "cpp" => cpp_config(),
            "kotlin" => kotlin_config(),
            "swift" => swift_config(),
            _ => return None,
        };
        Some(config)
    }

    /// 获取支持的语言列表
    pub fn supported_languages() -> Vec<&'static str> {
        vec![
            "python", "rust", "go", "java", "typescript", "javascript",
            "ruby", "php", "scala", "csharp", "cpp",
            "kotlin", "swift",
        ]
    }
}

// ============================================
// 各语言配置
// ============================================

fn python_config() -> LangConfig {
    LangConfig {
        lang_id: "python",
        language: Language::from(tree_sitter_python::LANGUAGE),
        symbol_rules: vec![
            SymbolRule::new(
                "function_definition",
                NameStrategy::ChildByType(vec!["identifier"]),
                "fn", Some("block"), true,
            ),
            SymbolRule::new(
                "class_definition",
                NameStrategy::ChildByType(vec!["identifier"]),
                "class", Some("block"), false,
            ),
        ],
        call_rules: vec![CallRule { kind: "call", callee_field: Some("function") }],
        import_kinds: vec!["import_statement", "import_from_statement"],
        skip_kinds: vec![],
    }
}

fn rust_config() -> LangConfig {
    LangConfig {
        lang_id: "rust",
        language: Language::from(tree_sitter_rust::LANGUAGE),
        symbol_rules: vec![
            SymbolRule::new(
                "function_item",
                NameStrategy::FieldName("name"),
                "fn", Some("block"), true,
            ),
            SymbolRule::new(
                "struct_item",
                NameStrategy::FieldName("name"),
                "struct", None, false,
            ),
            SymbolRule::new(
                "enum_item",
                NameStrategy::FieldName("name"),
                "enum", None, false,
            ),
            SymbolRule::new(
                "trait_item",
                NameStrategy::FieldName("name"),
                "trait", None, false,
            ),
            // impl 块：不递归进 body（对齐 Python rust_parser._parse_impl 行为）
            // name 格式 "Trait for Type" 或 "Type"
            SymbolRule::new(
                "impl_item",
                NameStrategy::ImplTraitForType { trait_field: "trait", type_field: "type" },
                "impl", None, false,
            ),
            // const/static/macro（对齐 Python rust_parser 的符号种类）
            SymbolRule::new(
                "const_item",
                NameStrategy::FieldName("name"),
                "const", None, false,
            ),
            SymbolRule::new(
                "static_item",
                NameStrategy::FieldName("name"),
                "static", None, false,
            ),
            SymbolRule::new(
                "macro_definition",
                NameStrategy::ChildByType(vec!["identifier"]),
                "macro_rules", None, false,
            ),
        ],
        call_rules: vec![CallRule { kind: "call_expression", callee_field: Some("function") }],
        import_kinds: vec!["use_declaration"],
        skip_kinds: vec!["mod_item"],
    }
}

fn go_config() -> LangConfig {
    LangConfig {
        lang_id: "go",
        language: Language::from(tree_sitter_go::LANGUAGE),
        symbol_rules: vec![
            SymbolRule::new(
                "function_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "fn", Some("block"), true,
            ),
            SymbolRule::new(
                "method_declaration",
                NameStrategy::ChildByType(vec!["field_identifier"]),
                "method", Some("block"), true,
            ),
            // Go 的 type_spec 需要动态 kind（struct_type/interface_type）
            SymbolRule {
                kind: "type_spec",
                name: NameStrategy::ChildByType(vec!["type_identifier"]),
                sym_kind: "type",
                body: None,
                is_fn: false,
                dynamic_kind: vec![
                    ("struct_type", "struct"),
                    ("interface_type", "interface"),
                ],
            },
        ],
        call_rules: vec![CallRule { kind: "call_expression", callee_field: Some("function") }],
        import_kinds: vec!["import_spec"],
        skip_kinds: vec![],
    }
}

fn java_config() -> LangConfig {
    LangConfig {
        lang_id: "java",
        language: Language::from(tree_sitter_java::LANGUAGE),
        symbol_rules: vec![
            SymbolRule::new(
                "method_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "method", Some("block"), true,
            ),
            SymbolRule::new(
                "constructor_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "constructor", Some("block"), true,
            ),
            SymbolRule::new(
                "class_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "class", Some("class_body"), false,
            ),
            SymbolRule::new(
                "interface_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "interface", Some("interface_body"), false,
            ),
            SymbolRule::new(
                "enum_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "enum", Some("enum_body"), false,
            ),
        ],
        call_rules: vec![CallRule { kind: "method_invocation", callee_field: None }],
        import_kinds: vec!["import_declaration"],
        skip_kinds: vec![],
    }
}

fn typescript_config() -> LangConfig {
    LangConfig {
        lang_id: "typescript",
        language: Language::from(tree_sitter_typescript::LANGUAGE_TSX),
        symbol_rules: vec![
            SymbolRule::new(
                "function_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "fn", Some("statement_block"), true,
            ),
            SymbolRule::new(
                "method_definition",
                NameStrategy::ChildByType(vec!["property_identifier"]),
                "method", Some("statement_block"), true,
            ),
            SymbolRule::new(
                "class_declaration",
                NameStrategy::ChildByType(vec!["type_identifier"]),
                "class", Some("class_body"), false,
            ),
            SymbolRule::new(
                "interface_declaration",
                NameStrategy::ChildByType(vec!["type_identifier"]),
                "interface", None, false,
            ),
        ],
        call_rules: vec![CallRule { kind: "call_expression", callee_field: Some("function") }],
        import_kinds: vec!["import_statement"],
        skip_kinds: vec![],
    }
}

fn javascript_config() -> LangConfig {
    LangConfig {
        lang_id: "javascript",
        // JavaScript 使用 TypeScript grammar 解析（TS 是 JS 的超集）
        language: Language::from(tree_sitter_typescript::LANGUAGE_TYPESCRIPT),
        symbol_rules: vec![
            SymbolRule::new(
                "function_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "fn", Some("statement_block"), true,
            ),
            SymbolRule::new(
                "method_definition",
                NameStrategy::ChildByType(vec!["property_identifier"]),
                "method", Some("statement_block"), true,
            ),
            SymbolRule::new(
                "class_declaration",
                NameStrategy::ChildByType(vec!["type_identifier"]),
                "class", Some("class_body"), false,
            ),
        ],
        call_rules: vec![CallRule { kind: "call_expression", callee_field: Some("function") }],
        import_kinds: vec!["import_statement"],
        skip_kinds: vec![],
    }
}

fn ruby_config() -> LangConfig {
    LangConfig {
        lang_id: "ruby",
        language: Language::from(tree_sitter_ruby::LANGUAGE),
        symbol_rules: vec![
            SymbolRule::new(
                "method",
                NameStrategy::ChildByType(vec!["identifier"]),
                "fn", Some("body_statement"), true,
            ),
            SymbolRule::new(
                "singleton_method",
                NameStrategy::ChildByType(vec!["identifier"]),
                "method", Some("body_statement"), true,
            ),
            SymbolRule::new(
                "class",
                NameStrategy::ChildByType(vec!["constant", "scope_resolution"]),
                "class", Some("body_statement"), false,
            ),
            SymbolRule::new(
                "module",
                NameStrategy::ChildByType(vec!["constant", "scope_resolution"]),
                "module", Some("body_statement"), false,
            ),
        ],
        call_rules: vec![CallRule { kind: "call", callee_field: Some("method") }],
        // Ruby 的 require 是 call 节点，不走 import 逻辑
        import_kinds: vec![],
        skip_kinds: vec![],
    }
}

fn php_config() -> LangConfig {
    LangConfig {
        lang_id: "php",
        language: Language::from(tree_sitter_php::LANGUAGE_PHP),
        symbol_rules: vec![
            // PHP 方法名在 formal_parameters 之前
            SymbolRule::new(
                "method_declaration",
                NameStrategy::PositionBefore {
                    terminator: "formal_parameters",
                    name_kind: "name",
                },
                "method", Some("declaration_list"), true,
            ),
            SymbolRule::new(
                "class_declaration",
                NameStrategy::ChildByType(vec!["name"]),
                "class", Some("declaration_list"), false,
            ),
            SymbolRule::new(
                "interface_declaration",
                NameStrategy::ChildByType(vec!["name"]),
                "interface", Some("declaration_list"), false,
            ),
            SymbolRule::new(
                "trait_declaration",
                NameStrategy::ChildByType(vec!["name"]),
                "trait", Some("declaration_list"), false,
            ),
        ],
        call_rules: vec![
            CallRule { kind: "function_call_expression", callee_field: None },
            CallRule { kind: "member_call_expression", callee_field: None },
        ],
        import_kinds: vec!["namespace_use_declaration"],
        skip_kinds: vec![],
    }
}

fn scala_config() -> LangConfig {
    LangConfig {
        lang_id: "scala",
        language: Language::from(tree_sitter_scala::LANGUAGE),
        symbol_rules: vec![
            SymbolRule::new(
                "function_definition",
                NameStrategy::ChildByType(vec!["identifier"]),
                "fn", Some("block"), true,
            ),
            SymbolRule::new(
                "function_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "fn", None, false,  // 抽象方法无 body
            ),
            SymbolRule::new(
                "class_definition",
                NameStrategy::ChildByType(vec!["identifier"]),
                "class", Some("template_body"), false,
            ),
            SymbolRule::new(
                "trait_definition",
                NameStrategy::ChildByType(vec!["identifier"]),
                "trait", Some("template_body"), false,
            ),
            SymbolRule::new(
                "object_definition",
                NameStrategy::ChildByType(vec!["identifier"]),
                "object", Some("template_body"), false,
            ),
        ],
        call_rules: vec![CallRule { kind: "call_expression", callee_field: None }],
        import_kinds: vec!["import_declaration"],
        skip_kinds: vec![],
    }
}

fn csharp_config() -> LangConfig {
    LangConfig {
        lang_id: "csharp",
        language: Language::from(tree_sitter_c_sharp::LANGUAGE),
        symbol_rules: vec![
            // C# 方法名在 parameter_list 之前
            SymbolRule::new(
                "method_declaration",
                NameStrategy::PositionBefore {
                    terminator: "parameter_list",
                    name_kind: "identifier",
                },
                "method", Some("block"), true,
            ),
            SymbolRule::new(
                "constructor_declaration",
                NameStrategy::PositionBefore {
                    terminator: "parameter_list",
                    name_kind: "identifier",
                },
                "constructor", Some("block"), true,
            ),
            SymbolRule::new(
                "class_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "class", Some("declaration_list"), false,
            ),
            SymbolRule::new(
                "struct_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "struct", Some("declaration_list"), false,
            ),
            SymbolRule::new(
                "interface_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "interface", Some("declaration_list"), false,
            ),
            SymbolRule::new(
                "enum_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "enum", Some("declaration_list"), false,
            ),
        ],
        call_rules: vec![CallRule { kind: "invocation_expression", callee_field: None }],
        import_kinds: vec!["using_directive"],
        skip_kinds: vec![],
    }
}

fn cpp_config() -> LangConfig {
    LangConfig {
        lang_id: "cpp",
        language: Language::from(tree_sitter_cpp::LANGUAGE),
        symbol_rules: vec![
            // C++ 函数名在 function_declarator 内（类似 C）
            SymbolRule::new(
                "function_definition",
                NameStrategy::ChildByTypeNested {
                    intermediate: "function_declarator",
                    name_kinds: vec!["identifier", "field_identifier", "qualified_identifier"],
                },
                "fn", Some("compound_statement"), true,
            ),
            SymbolRule::new(
                "class_specifier",
                NameStrategy::ChildByType(vec!["type_identifier"]),
                "class", Some("field_declaration_list"), false,
            ),
            SymbolRule::new(
                "struct_specifier",
                NameStrategy::ChildByType(vec!["type_identifier"]),
                "struct", Some("field_declaration_list"), false,
            ),
            SymbolRule::new(
                "enum_specifier",
                NameStrategy::ChildByType(vec!["type_identifier"]),
                "enum", None, false,
            ),
            SymbolRule::new(
                "namespace_definition",
                NameStrategy::ChildByType(vec!["namespace_identifier", "identifier"]),
                "namespace", Some("declaration_list"), false,
            ),
        ],
        call_rules: vec![CallRule { kind: "call_expression", callee_field: Some("function") }],
        import_kinds: vec!["preproc_include"],
        skip_kinds: vec![],
    }
}

fn kotlin_config() -> LangConfig {
    LangConfig {
        lang_id: "kotlin",
        language: Language::from(tree_sitter_kotlin_ng::LANGUAGE),
        symbol_rules: vec![
            // Kotlin 函数声明：function_declaration → identifier + function_body
            SymbolRule::new(
                "function_declaration",
                NameStrategy::ChildByType(vec!["simple_identifier", "identifier"]),
                "fn", Some("function_body"), true,
            ),
            // Kotlin 类声明
            SymbolRule::new(
                "class_declaration",
                NameStrategy::ChildByType(vec!["type_identifier", "identifier"]),
                "class", Some("class_body"), false,
            ),
            // Kotlin object 声明（单例对象）
            SymbolRule::new(
                "object_declaration",
                NameStrategy::ChildByType(vec!["type_identifier", "identifier"]),
                "object", Some("class_body"), false,
            ),
            // Kotlin 接口声明
            SymbolRule::new(
                "interface_declaration",
                NameStrategy::ChildByType(vec!["type_identifier", "identifier"]),
                "interface", Some("class_body"), false,
            ),
        ],
        // Kotlin 调用：call_expression 无 callee field，从 identifier 提取
        call_rules: vec![CallRule { kind: "call_expression", callee_field: None }],
        import_kinds: vec!["import"],
        skip_kinds: vec![],
    }
}

fn swift_config() -> LangConfig {
    LangConfig {
        lang_id: "swift",
        language: Language::from(tree_sitter_swift::LANGUAGE),
        symbol_rules: vec![
            // Swift 函数声明
            SymbolRule::new(
                "function_declaration",
                NameStrategy::ChildByType(vec!["simple_identifier", "identifier"]),
                "fn", Some("function_body"), true,
            ),
            // Swift init 声明（构造函数）
            SymbolRule::new(
                "init_declaration",
                NameStrategy::ChildByType(vec!["simple_identifier", "identifier"]),
                "constructor", Some("function_body"), true,
            ),
            // Swift 协议内的方法声明（无方法体）
            SymbolRule::new(
                "protocol_function_declaration",
                NameStrategy::ChildByType(vec!["simple_identifier", "identifier"]),
                "fn", None, true,
            ),
            // Swift 类型声明：tree-sitter-swift 0.7.x 把 class/struct/enum/actor
            // 统一为 class_declaration（用 declaration_kind 字段区分）。
            // Rust multilang 框架暂不支持字段值映射，统一标记为 "class"。
            // name 通过 "name" field 提取（比 ChildByType 更可靠）。
            SymbolRule::new(
                "class_declaration",
                NameStrategy::FieldName("name"),
                "class", Some("class_body"), false,
            ),
            // Swift protocol
            SymbolRule::new(
                "protocol_declaration",
                NameStrategy::FieldName("name"),
                "protocol", Some("protocol_body"), false,
            ),
            // Swift typealias（类型别名）
            SymbolRule::new(
                "typealias_declaration",
                NameStrategy::FieldName("name"),
                "typealias", None, false,
            ),
        ],
        // Swift 调用：call_expression 有 "function" field
        call_rules: vec![CallRule { kind: "call_expression", callee_field: Some("function") }],
        import_kinds: vec!["import_declaration"],
        skip_kinds: vec![],
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

    /// parse 单个文件，提取符号 + 调用 + import
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

        let root = tree.root_node();
        walk_node(
            &root, &source, &self.config, module_path, "",
            "", "",
            &mut symbols, &mut calls, &mut imports,
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

        let root = tree.root_node();
        walk_node(
            &root, canonical_bytes, &self.config, module_path, "",
            "", "",
            &mut symbols, &mut calls, &mut imports,
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
        error: Some(err.to_string()),
    }
}

// ============================================
// 统一 walk 逻辑
// ============================================

/// 递归遍历 AST，同时提取符号、调用关系、import
///
/// Walker 逻辑：
/// 1. 匹配符号规则 → 提取符号 → 递归进 body（设置新的调用上下文）
/// 2. 匹配调用规则 → 提取调用 → 递归子节点（调用可能嵌套）
/// 3. 匹配 import kind → 提取 import → 不递归
/// 4. 默认 → 递归子节点（保持当前调用上下文）
///
/// 调用上下文：current_fn / current_qualified 跟踪当前所在函数，
/// 只有在函数体内（current_fn 非空）才记录调用关系。
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
) {
    let mut cursor = node.walk();
    for child in node.named_children(&mut cursor) {
        let kind = child.kind();

        // 0. 跳过指定 kind（不提取符号、不递归）
        if config.skip_kinds.contains(&kind) {
            continue;
        }

        // 1. 检查符号规则
        if let Some(rule) = config.symbol_rules.iter().find(|r| r.kind == kind) {
            if let Some(name) = extract_name(&child, source, &rule.name) {
                // 动态 kind 处理（Go 的 type_spec → struct/interface）
                let actual_kind = if !rule.dynamic_kind.is_empty() {
                    rule.dynamic_kind.iter()
                        .find_map(|(child_kind, sym_kind)| {
                            if find_child(&child, child_kind).is_some() {
                                Some(*sym_kind)
                            } else {
                                None
                            }
                        })
                        .unwrap_or(rule.sym_kind)
                } else {
                    rule.sym_kind
                };

                let qualified = make_qualified(module_path, parent_qualified, &name);
                let sym = make_symbol(&child, source, module_path, &name, &qualified, actual_kind);
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
                            symbols, calls, imports,
                        );
                    }
                }
            }
            continue;
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
                symbols, calls, imports,
            );
            continue;
        }

        // 3. 检查 import
        if config.import_kinds.contains(&kind) {
            let imp = node_text(&child, source).to_string();
            imports.push(clean_import(&imp));
            continue;
        }

        // 4. 默认：递归子节点
        walk_node(
            &child, source, config, module_path, parent_qualified,
            current_fn, current_qualified,
            symbols, calls, imports,
        );
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
            .or_else(|| find_child(node, "simple_identifier"))?,
    };
    Some(node_text(&callee, source).to_string())
}

/// 将 callee 文本拆分为 (callee_name, callee_module)
///
/// 处理成员调用：
/// - `foo.bar()` → (name="bar", module="foo")
/// - `Foo::bar()` → (name="bar", module="Foo")
/// - `obj->method()` → (name="method", module="obj")
/// - `func()` → (name="func", module="")
fn split_callee(text: &str) -> (String, String) {
    // 优先匹配 :: 和 ->（C++/Rust 风格）
    if let Some(pos) = text.rfind("::") {
        return (text[pos + 2..].to_string(), text[..pos].to_string());
    }
    if let Some(pos) = text.rfind("->") {
        return (text[pos + 2..].to_string(), text[..pos].to_string());
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
        visibility: "public".to_string(),
        content,
        signature: String::new(),
    }
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
