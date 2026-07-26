//! Rust 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）

use crate::multi_lang::{LangConfig, SymbolRule, CallRule, NameStrategy};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
    LangConfig {
        lang_id: "rust",
        language: Language::from(tree_sitter_rust::LANGUAGE),
        symbol_rules: vec![
            // R15-P0-3: impl 块内方法优先匹配（require_parent_kind="declaration_list"）
            // 必须在 function_item 规则之前，因为 walk_node 用 find() 取首个匹配
            // impl/trait 体内 function_item 的 parent_kind="declaration_list" → method
            SymbolRule::new(
                "function_item",
                NameStrategy::FieldName("name"),
                "method", Some("block"), true,
            )
            .with_require_parent_kind("declaration_list"),
            // 顶层或 mod 内函数：parent_kind 不是 declaration_list → fn
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
            // R15-P0-3: trait 块递归进 declaration_list 提取方法签名
            SymbolRule::new(
                "trait_item",
                NameStrategy::FieldName("name"),
                "trait", Some("declaration_list"), false,
            ),
            // R15-P0-3: impl 块递归进 declaration_list 提取方法（golden 期望 new/distance）
            // name 格式 "Trait for Type" 或 "Type"
            SymbolRule::new(
                "impl_item",
                NameStrategy::ImplTraitForType { trait_field: "trait", type_field: "type" },
                "impl", Some("declaration_list"), false,
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
