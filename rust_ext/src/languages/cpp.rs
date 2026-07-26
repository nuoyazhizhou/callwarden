//! C++ 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）
//!
//! P0-C Step 4: 修复 C++ 契约：
//! - 区分类内方法（method）与自由函数（function）：通过 require_parent_kind 实现
//! - 构造函数检测：name == 类名时 kind="constructor"（constructor_if_name_matches_parent）
//! - namespace 已支持（Step 0）
//! - macro/template 投影：当前样本无 macro/template，保持现有行为

use crate::multi_lang::{LangConfig, SymbolRule, CallRule, NameStrategy};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
    LangConfig {
        lang_id: "cpp",
        language: Language::from(tree_sitter_cpp::LANGUAGE),
        symbol_rules: vec![
            // P0-C Step 4: 类内函数 — kind="method"，构造函数检测
            // require_parent_kind="field_declaration_list" 确保只匹配类/结构体内的函数
            // constructor_if_name_matches_parent: name == 类名时覆盖为 "constructor"
            SymbolRule::new(
                "function_definition",
                NameStrategy::ChildByTypeNested {
                    intermediate: "function_declarator",
                    name_kinds: vec!["identifier", "field_identifier", "qualified_identifier"],
                },
                "method", Some("compound_statement"), true,
            )
            .with_require_parent_kind("field_declaration_list")
            .with_constructor_detection(),
            // P0-C Step 4: 自由函数 — kind="function"
            // 无 require_parent_kind 限制（匹配非类内上下文的 function_definition）
            // 注意：此规则在类内规则之后，find() 会先匹配类内规则
            SymbolRule::new(
                "function_definition",
                NameStrategy::ChildByTypeNested {
                    intermediate: "function_declarator",
                    name_kinds: vec!["identifier", "field_identifier", "qualified_identifier"],
                },
                "function", Some("compound_statement"), true,
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
        import_directives: vec![],
        reference_rules: vec![],
        skip_kinds: vec![],
    }
}
