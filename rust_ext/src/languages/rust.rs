//! Rust 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）

use crate::multi_lang::{LangConfig, SymbolRule, CallRule, NameStrategy};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
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
        import_directives: vec![],
        reference_rules: vec![],
        skip_kinds: vec!["mod_item"],
    }
}
