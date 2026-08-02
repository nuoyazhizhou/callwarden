//! Scala 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）
//!
//! P0-C Step 3: 修复 Scala 契约：
//! - function_definition/declaration sym_kind: "fn" → "method"（对齐 golden）
//! - call_expression 提取 calc.add(10) 对象方法调用（通过 field_expression fallback）
//! - 新增 instance_expression 调用规则：提取 `new Calculator` 构造调用
//! - Scala 3 语法：tree-sitter-scala 0.26 已支持 Scala 3 语法（given/using/extension 等）

use crate::multi_lang::{CallRule, LangConfig, NameStrategy, SymbolRule};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
    LangConfig {
        lang_id: "scala",
        language: Language::from(tree_sitter_scala::LANGUAGE),
        symbol_rules: vec![
            // P0-C Step 3: Scala 方法 kind 从 "fn" 改为 "method"（对齐 golden）
            SymbolRule::new(
                "function_definition",
                NameStrategy::ChildByType(vec!["identifier"]),
                "method",
                Some("block"),
                true,
            ),
            // P0-C Step 3: 抽象方法同样改为 "method"
            SymbolRule::new(
                "function_declaration",
                NameStrategy::ChildByType(vec!["identifier"]),
                "method",
                None,
                false,
            ),
            SymbolRule::new(
                "class_definition",
                NameStrategy::ChildByType(vec!["identifier"]),
                "class",
                Some("template_body"),
                false,
            ),
            SymbolRule::new(
                "trait_definition",
                NameStrategy::ChildByType(vec!["identifier"]),
                "trait",
                Some("template_body"),
                false,
            ),
            SymbolRule::new(
                "object_definition",
                NameStrategy::ChildByType(vec!["identifier"]),
                "object",
                Some("template_body"),
                false,
            ),
        ],
        call_rules: vec![
            // call_expression: 覆盖 println(calc) 和 calc.add(10)
            // callee_field=None 时 extract_callee 按顺序查找:
            //   identifier（println）→ field_expression（calc.add）→ type_identifier
            CallRule {
                kind: "call_expression",
                callee_field: None,
            },
            // P0-C Step 3: instance_expression 提取 new Calculator 构造调用
            // extract_callee 查找 type_identifier 子节点 → "Calculator"
            CallRule {
                kind: "instance_expression",
                callee_field: None,
            },
        ],
        import_kinds: vec!["import_declaration"],
        import_directives: vec![],
        reference_rules: vec![],
        skip_kinds: vec![],
    }
}
