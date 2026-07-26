//! Python 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）

use crate::multi_lang::{LangConfig, SymbolRule, CallRule, NameStrategy};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
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
        import_directives: vec![],
        reference_rules: vec![],
        skip_kinds: vec![],
    }
}
