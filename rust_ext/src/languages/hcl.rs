//! HCL 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）
//!
//! P0-D 将在本文件扩展 HCL 引用语义。本步骤仅做等价迁移。
//!
//! 注意：HCL 不在 `LangConfig::supported_languages()` 列表中——HCL 的"调用关系"
//! 是 attribute 中的引用，不是传统函数调用，完整走 Python parser。但配置本身
//! 仍保留在此，供 Python 端按需取用符号。

use crate::multi_lang::{LangConfig, SymbolRule, NameStrategy};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
    // HCL AST 特殊性：所有顶层块统一为 kind="block"，块类型由首个 identifier 子节点
    // 文本决定（resource/provider/variable/output/module/data/locals/terraform）。
    // 用 SymbolRule.kind_from_child_text 按 identifier 文本映射 sym_kind。
    // name 用 HclLabels 收集 string_lit 标签拼接（resource/data 用 type.name 风格）。
    LangConfig {
        lang_id: "hcl",
        language: Language::from(tree_sitter_hcl::LANGUAGE),
        symbol_rules: vec![
            SymbolRule {
                kind: "block",
                name: NameStrategy::HclLabels { fallback: "block" },
                // sym_kind 是兜底值；实际由 kind_from_child_text 覆盖
                sym_kind: "block",
                // HCL block 的 body 子节点用于递归提取 attribute 中的引用（调用关系）
                body: Some("body"),
                is_fn: false,
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
            },
        ],
        // HCL 无传统函数调用；引用关系在 attribute 表达式中（如 value = aws_instance.web.public_ip）
        // 当前 walk_node 的 CallRule 按 kind 匹配，不适用于 attribute。
        // Python parser 的 _extract_refs_from_expression 专门处理，这里留空，
        // 由 Python 端补充提取（或后续扩展框架支持 attribute 引用）
        call_rules: vec![],
        // HCL 无 import 概念
        import_kinds: vec![],
        skip_kinds: vec![],
    }
}
