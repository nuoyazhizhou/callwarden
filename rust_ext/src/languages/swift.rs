//! Swift 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）
//!
//! P0-C Step 5: 修复 Swift kind 对齐 golden：
//! - function_declaration/protocol_function_declaration: "fn" → "method"（对齐 golden）

use crate::multi_lang::{CallRule, LangConfig, NameStrategy, SymbolRule};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
    LangConfig {
        lang_id: "swift",
        language: Language::from(tree_sitter_swift::LANGUAGE),
        symbol_rules: vec![
            // P0-C Step 5: Swift 函数声明 — kind="method"（对齐 golden）
            SymbolRule::new(
                "function_declaration",
                NameStrategy::ChildByType(vec!["simple_identifier", "identifier"]),
                "method",
                Some("function_body"),
                true,
            ),
            // Swift init 声明（构造函数）
            SymbolRule::new(
                "init_declaration",
                NameStrategy::ChildByType(vec!["simple_identifier", "identifier"]),
                "constructor",
                Some("function_body"),
                true,
            ),
            // P0-C Step 5: 协议内的方法声明 — kind="method"（对齐 golden）
            SymbolRule::new(
                "protocol_function_declaration",
                NameStrategy::ChildByType(vec!["simple_identifier", "identifier"]),
                "method",
                None,
                true,
            ),
            // Swift 类型声明：tree-sitter-swift 0.7.x 把 class/struct/enum/actor
            // 统一为 class_declaration（用 declaration_kind 字段区分）。
            // Rust multilang 框架暂不支持字段值映射，统一标记为 "class"。
            // name 通过 "name" field 提取（比 ChildByType 更可靠）。
            SymbolRule::new(
                "class_declaration",
                NameStrategy::FieldName("name"),
                "class",
                Some("class_body"),
                false,
            ),
            // Swift protocol
            SymbolRule::new(
                "protocol_declaration",
                NameStrategy::FieldName("name"),
                "protocol",
                Some("protocol_body"),
                false,
            ),
            // Swift typealias（类型别名）
            SymbolRule::new(
                "typealias_declaration",
                NameStrategy::FieldName("name"),
                "typealias",
                None,
                false,
            ),
        ],
        // Swift 调用：call_expression 有 "function" field
        call_rules: vec![CallRule {
            kind: "call_expression",
            callee_field: Some("function"),
        }],
        import_kinds: vec!["import_declaration"],
        import_directives: vec![],
        reference_rules: vec![],
        skip_kinds: vec![],
    }
}
