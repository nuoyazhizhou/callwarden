//! PHP 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）
//!
//! P0-C Step 2: 修复 PHP 契约：
//! - 新增 property_declaration 规则：提取 `private $value;` 为 property 符号
//!   （NameStrategy::PhpProperty 3 层嵌套：property_element → variable_name → name）
//! - method_declaration + name="__construct" → kind="constructor"（通过 kind_from_name）
//! - visibility 从 visibility_modifier 子节点提取（private/protected/public）
//! - trait/interface 规则已在 Step 0 迁移，本步骤验证

use crate::multi_lang::{CallRule, LangConfig, NameStrategy, SymbolRule};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
    LangConfig {
        lang_id: "php",
        language: Language::from(tree_sitter_php::LANGUAGE_PHP),
        symbol_rules: vec![
            // PHP 方法名在 formal_parameters 之前
            // body 是 compound_statement（{ ... }），不是 declaration_list
            // （declaration_list 是 class/interface/trait 的成员列表）
            // P0-C Step 2: __construct 通过 kind_from_name 映射为 "constructor"
            SymbolRule::new(
                "method_declaration",
                NameStrategy::PositionBefore {
                    terminator: "formal_parameters",
                    name_kind: "name",
                },
                "method",
                Some("compound_statement"),
                true,
            )
            .with_kind_from_name(vec![("__construct", "constructor")]),
            // PHP 独立函数 function foo() { ... }
            SymbolRule::new(
                "function_definition",
                NameStrategy::PositionBefore {
                    terminator: "formal_parameters",
                    name_kind: "name",
                },
                "fn",
                Some("compound_statement"),
                true,
            ),
            // P0-C Step 2: PHP property 声明（private $value; / public $name = ...;）
            // property_declaration → property_element → variable_name → name
            // body=None：property 无函数体，不递归提取调用
            SymbolRule::new(
                "property_declaration",
                NameStrategy::PhpProperty,
                "property",
                None,
                false,
            ),
            SymbolRule::new(
                "class_declaration",
                NameStrategy::ChildByType(vec!["name"]),
                "class",
                Some("declaration_list"),
                false,
            ),
            SymbolRule::new(
                "interface_declaration",
                NameStrategy::ChildByType(vec!["name"]),
                "interface",
                Some("declaration_list"),
                false,
            ),
            SymbolRule::new(
                "trait_declaration",
                NameStrategy::ChildByType(vec!["name"]),
                "trait",
                Some("declaration_list"),
                false,
            ),
        ],
        call_rules: vec![
            // PHP 4 种调用表达式，按 tree-sitter-php 0.23 node-types.json 定义使用正确 field：
            //   function_call_expression: field "function" → name/qualified_name
            //   member_call_expression:     field "name"    → name ($obj->method())
            //   nullsafe_member_call_expression: field "name" → name ($obj?->method())
            //   scoped_call_expression:     field "name"    → name (Class::method())
            CallRule {
                kind: "function_call_expression",
                callee_field: Some("function"),
            },
            CallRule {
                kind: "member_call_expression",
                callee_field: Some("name"),
            },
            CallRule {
                kind: "nullsafe_member_call_expression",
                callee_field: Some("name"),
            },
            CallRule {
                kind: "scoped_call_expression",
                callee_field: Some("name"),
            },
        ],
        import_kinds: vec!["namespace_use_declaration"],
        import_directives: vec![],
        reference_rules: vec![],
        skip_kinds: vec![],
    }
}
