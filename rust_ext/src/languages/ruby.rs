//! Ruby 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）
//!
//! P0-C Step 5: 修复 Ruby kind 对齐 golden：
//! - 类内 method: kind="method"，initialize → "constructor"（kind_from_name）
//! - 顶层 method: kind="function"（对齐 golden 期望）
//! - singleton_method: kind="method"（保持）

use crate::multi_lang::{LangConfig, SymbolRule, CallRule, NameStrategy};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
    LangConfig {
        lang_id: "ruby",
        language: Language::from(tree_sitter_ruby::LANGUAGE),
        symbol_rules: vec![
            // P0-C Step 5: 类内方法 — kind="method"，initialize 映射为 "constructor"
            // require_parent_kind="body_statement" 匹配 class/module 内的方法
            SymbolRule::new(
                "method",
                NameStrategy::ChildByType(vec!["identifier"]),
                "method", Some("body_statement"), true,
            )
            .with_require_parent_kind("body_statement")
            .with_kind_from_name(vec![("initialize", "constructor")]),
            // P0-C Step 5: 顶层方法 — kind="function"（对齐 golden 期望）
            SymbolRule::new(
                "method",
                NameStrategy::ChildByType(vec!["identifier"]),
                "function", Some("body_statement"), true,
            ),
            SymbolRule::new(
                "singleton_method",
                NameStrategy::ChildByType(vec!["identifier"]),
                "method", Some("body_statement"), true,
            ),
            SymbolRule::new(
                "class",
                NameStrategy::ChildByType(vec!["constant", "scope_resolution"]),
                "class", Some("body_statement"), false,
            ),
            SymbolRule::new(
                "module",
                NameStrategy::ChildByType(vec!["constant", "scope_resolution"]),
                "module", Some("body_statement"), false,
            ),
        ],
        call_rules: vec![CallRule { kind: "call", callee_field: Some("method") }],
        // Ruby 的 require 是 call 节点，不走 import 逻辑
        import_kinds: vec![],
        import_directives: vec![],
        reference_rules: vec![],
        skip_kinds: vec![],
    }
}
