//! Go 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）

use crate::multi_lang::{LangConfig, SymbolRule, CallRule, NameStrategy};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
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
                call_keyword: None,
                kind_from_child_text: vec![],
                kind_from_name: vec![],
                require_parent_kind: None,
                constructor_if_name_matches_parent: false,
            },
        ],
        call_rules: vec![CallRule { kind: "call_expression", callee_field: Some("function") }],
        import_kinds: vec!["import_spec"],
        import_directives: vec![],
        reference_rules: vec![],
        skip_kinds: vec![],
    }
}
