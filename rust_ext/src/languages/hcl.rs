//! HCL 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）
//!
//! P0-D Step 0+1: 实现 HCL attribute traversal 引用语义。
//! HCL 的"调用关系"是 attribute 表达式中的引用，如：
//!   value = aws_instance.web.private_ip
//! 通过 ReferenceRule + walk_node 的 reference 提取路径处理，
//! 同时产出 references（语义化资源地址）和 raw_calls（完整 traversal 文本）。

use crate::multi_lang::{LangConfig, NameStrategy, ReferenceRule, SymbolRule};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
    // HCL AST 特殊性：所有顶层块统一为 kind="block"，块类型由首个 identifier 子节点
    // 文本决定（resource/provider/variable/output/module/data/locals/terraform）。
    // 用 SymbolRule.kind_from_child_text 按 identifier 文本映射 sym_kind。
    // name 用 HclLabels 收集 string_lit 标签拼接（resource/data 用 type.name 风格）。
    LangConfig {
        lang_id: "hcl",
        language: Language::from(tree_sitter_hcl::LANGUAGE),
        symbol_rules: vec![SymbolRule {
            kind: "block",
            name: NameStrategy::HclLabels { fallback: "block" },
            // sym_kind 是兜底值；实际由 kind_from_child_text 覆盖
            sym_kind: "block",
            // HCL block 的 body 子节点用于递归提取 attribute 中的引用（调用关系）
            body: Some("body"),
            // P0-D: is_fn=true 让 walk_node 把 block 名设为 current_fn，
            // 这样 attribute 中的引用能正确归属到包含它的 block。
            // HCL 无 CallRule，is_fn 仅用于设置调用/引用上下文，不触发 call 提取。
            is_fn: true,
            dynamic_kind: vec![],
            call_keyword: None,
            kind_from_child_text: vec![
                ("resource", "resource"),
                ("provider", "provider"),
                ("variable", "variable"),
                ("output", "output"),
                ("module", "module"),
                ("data", "data"),
                ("locals", "locals"),
                ("terraform", "terraform"),
            ],
            kind_from_name: vec![],
            require_parent_kind: None,
            constructor_if_name_matches_parent: false,
        }],
        // HCL 无传统函数调用；引用通过 reference_rules 提取
        call_rules: vec![],
        // HCL 无 import 概念
        import_kinds: vec![],
        // P0-D: 无 Elixir 风格的 import 指令
        import_directives: vec![],
        // P0-D: HCL attribute traversal 引用提取规则
        // attribute 节点 → expression 子节点 → variable_expr + get_attr 链
        reference_rules: vec![ReferenceRule {
            attribute_kind: "attribute",
            expression_kind: "expression",
            variable_kind: "variable_expr",
            get_attr_kind: "get_attr",
        }],
        skip_kinds: vec![],
    }
}
