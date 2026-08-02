//! C# 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）

use crate::multi_lang::{CallRule, LangConfig, NameStrategy, SymbolRule};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
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
                "method",
                Some("block"),
                true,
            ),
            SymbolRule::new(
                "constructor_declaration",
                NameStrategy::PositionBefore {
                    terminator: "parameter_list",
                    name_kind: "identifier",
                },
                "constructor",
                Some("block"),
                true,
            ),
            SymbolRule::new(
                "class_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "class",
                Some("declaration_list"),
                false,
            ),
            SymbolRule::new(
                "struct_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "struct",
                Some("declaration_list"),
                false,
            ),
            SymbolRule::new(
                "interface_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "interface",
                Some("declaration_list"),
                false,
            ),
            SymbolRule::new(
                "enum_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "enum",
                Some("declaration_list"),
                false,
            ),
            // R15-P0-3: C# field 提取（golden 期望 value 字段）
            // AST: field_declaration → variable_declaration → variable_declarator → identifier
            SymbolRule::new(
                "field_declaration",
                NameStrategy::ChildByTypeNested {
                    intermediate: "variable_declaration",
                    name_kinds: vec!["variable_declarator"],
                },
                "field",
                None,
                false,
            ),
        ],
        call_rules: vec![CallRule {
            kind: "invocation_expression",
            callee_field: None,
        }],
        import_kinds: vec!["using_directive"],
        import_directives: vec![],
        reference_rules: vec![],
        skip_kinds: vec![],
    }
}
