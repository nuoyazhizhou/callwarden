//! TypeScript 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）
//!
//! P0-C Step 1: 修复 TypeScript 契约：
//! - function_declaration sym_kind: "fn" → "function"（对齐 golden kind 区分）
//! - method_definition + name="constructor": kind → "constructor"（通过 kind_from_name）
//! - 新增 new_expression 调用规则：提取 `new User(...)` 构造调用（callee=User）
//! - TSX grammar 已启用（LANGUAGE_TSX），支持 JSX/TSX 语法

use crate::multi_lang::{LangConfig, SymbolRule, CallRule, NameStrategy};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
    LangConfig {
        lang_id: "typescript",
        // TSX grammar 是 TypeScript 的超集，同时支持 .ts 和 .tsx 文件
        language: Language::from(tree_sitter_typescript::LANGUAGE_TSX),
        symbol_rules: vec![
            // P0-C Step 1: top-level function kind 从 "fn" 改为 "function"（对齐 golden）
            SymbolRule::new(
                "function_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "function", Some("statement_block"), true,
            ),
            // P0-C Step 1: method_definition + name="constructor" → kind="constructor"
            // 普通方法仍为 "method"，仅 constructor 名称命中 kind_from_name 映射
            SymbolRule::new(
                "method_definition",
                NameStrategy::ChildByType(vec!["property_identifier"]),
                "method", Some("statement_block"), true,
            ).with_kind_from_name(vec![("constructor", "constructor")]),
            SymbolRule::new(
                "class_declaration",
                NameStrategy::ChildByType(vec!["type_identifier"]),
                "class", Some("class_body"), false,
            ),
            SymbolRule::new(
                "interface_declaration",
                NameStrategy::ChildByType(vec!["type_identifier"]),
                "interface", None, false,
            ),
        ],
        call_rules: vec![
            CallRule { kind: "call_expression", callee_field: Some("function") },
            // P0-C Step 1: new_expression 提取构造调用（new User('Alice') → callee=User）
            // tree-sitter-typescript: new_expression 的 "constructor" 字段指向构造类名
            CallRule { kind: "new_expression", callee_field: Some("constructor") },
        ],
        import_kinds: vec!["import_statement"],
        skip_kinds: vec![],
    }
}
