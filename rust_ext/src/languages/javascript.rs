//! JavaScript 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）
//!
//! P0-C Step 1: 修复 JavaScript 契约（与 TypeScript 同步）：
//! - function_declaration sym_kind: "fn" → "function"（对齐 golden kind 区分）
//! - method_definition + name="constructor": kind → "constructor"（通过 kind_from_name）
//! - 新增 new_expression 调用规则：提取 `new User(...)` 构造调用（callee=User）

use crate::multi_lang::{CallRule, LangConfig, NameStrategy, SymbolRule};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
    LangConfig {
        lang_id: "javascript",
        // JavaScript 使用 TypeScript grammar 解析（TS 是 JS 的超集）
        language: Language::from(tree_sitter_typescript::LANGUAGE_TYPESCRIPT),
        symbol_rules: vec![
            // P0-C Step 1: top-level function kind 从 "fn" 改为 "function"（对齐 golden）
            SymbolRule::new(
                "function_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "function",
                Some("statement_block"),
                true,
            ),
            // P0-C Step 1: method_definition + name="constructor" → kind="constructor"
            SymbolRule::new(
                "method_definition",
                NameStrategy::ChildByType(vec!["property_identifier"]),
                "method",
                Some("statement_block"),
                true,
            )
            .with_kind_from_name(vec![("constructor", "constructor")]),
            SymbolRule::new(
                "class_declaration",
                NameStrategy::ChildByType(vec!["type_identifier"]),
                "class",
                Some("class_body"),
                false,
            ),
        ],
        call_rules: vec![
            CallRule {
                kind: "call_expression",
                callee_field: Some("function"),
            },
            // P0-C Step 1: new_expression 提取构造调用（new User('Bob') → callee=User）
            CallRule {
                kind: "new_expression",
                callee_field: Some("constructor"),
            },
        ],
        import_kinds: vec!["import_statement"],
        import_directives: vec![],
        reference_rules: vec![],
        skip_kinds: vec![],
    }
}
