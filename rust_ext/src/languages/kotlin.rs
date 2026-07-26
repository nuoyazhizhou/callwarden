//! Kotlin 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）

use crate::multi_lang::{LangConfig, SymbolRule, CallRule, NameStrategy};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
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
        import_directives: vec![],
        reference_rules: vec![],
        skip_kinds: vec![],
    }
}
