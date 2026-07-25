//! Java 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）

use crate::multi_lang::{LangConfig, SymbolRule, CallRule, NameStrategy};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
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
