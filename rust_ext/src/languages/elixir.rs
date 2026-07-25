//! Elixir 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）
//!
//! P0-D 将在本文件扩展 Elixir 引用语义。本步骤仅做等价迁移。

use crate::multi_lang::{LangConfig, SymbolRule, CallRule, NameStrategy};
use tree_sitter::Language;

pub(crate) fn config() -> LangConfig {
    // Elixir AST 特殊性：所有声明都是 call 节点（同 kind="call"），
    // 需按首个 identifier 文本区分 defmodule/def/defp/defmacro/defmacrop/defguard/defguardp。
    // 用 SymbolRule.call_keyword 字段过滤，name 用 CallArgName 从 arguments 提取。
    LangConfig {
        lang_id: "elixir",
        language: Language::from(tree_sitter_elixir::LANGUAGE),
        symbol_rules: vec![
            // defmodule Foo.Bar do ... end → kind="module"，name 从 arguments 内 alias 取
            SymbolRule {
                kind: "call",
                name: NameStrategy::CallArgName {
                    container: "arguments",
                    child_kind: "alias",
                    name_kind: "alias",
                },
                sym_kind: "module",
                body: Some("do_block"),
                is_fn: false,
                dynamic_kind: vec![],
                call_keyword: Some("defmodule"),
                kind_from_child_text: vec![],
                kind_from_name: vec![],
                require_parent_kind: None,
                constructor_if_name_matches_parent: false,
            },
            // def foo(args) do ... end → kind="function"，is_fn=true（设置调用上下文）
            // name 从 arguments 内首个 call 节点的 identifier 提取
            SymbolRule {
                kind: "call",
                name: NameStrategy::CallArgName {
                    container: "arguments",
                    child_kind: "call",
                    name_kind: "identifier",
                },
                sym_kind: "function",
                body: Some("do_block"),
                is_fn: true,
                dynamic_kind: vec![],
                call_keyword: Some("def"),
                kind_from_child_text: vec![],
                kind_from_name: vec![],
                require_parent_kind: None,
                constructor_if_name_matches_parent: false,
            },
            // defp foo(args) do ... end → kind="function"（私有）
            SymbolRule {
                kind: "call",
                name: NameStrategy::CallArgName {
                    container: "arguments",
                    child_kind: "call",
                    name_kind: "identifier",
                },
                sym_kind: "function",
                body: Some("do_block"),
                is_fn: true,
                dynamic_kind: vec![],
                call_keyword: Some("defp"),
                kind_from_child_text: vec![],
                kind_from_name: vec![],
                require_parent_kind: None,
                constructor_if_name_matches_parent: false,
            },
            // defmacro name(args) do ... end → kind="macro"
            SymbolRule {
                kind: "call",
                name: NameStrategy::CallArgName {
                    container: "arguments",
                    child_kind: "call",
                    name_kind: "identifier",
                },
                sym_kind: "macro",
                body: Some("do_block"),
                is_fn: true,
                dynamic_kind: vec![],
                call_keyword: Some("defmacro"),
                kind_from_child_text: vec![],
                kind_from_name: vec![],
                require_parent_kind: None,
                constructor_if_name_matches_parent: false,
            },
            // defmacrop name(args) do ... end → kind="macro"（私有）
            SymbolRule {
                kind: "call",
                name: NameStrategy::CallArgName {
                    container: "arguments",
                    child_kind: "call",
                    name_kind: "identifier",
                },
                sym_kind: "macro",
                body: Some("do_block"),
                is_fn: true,
                dynamic_kind: vec![],
                call_keyword: Some("defmacrop"),
                kind_from_child_text: vec![],
                kind_from_name: vec![],
                require_parent_kind: None,
                constructor_if_name_matches_parent: false,
            },
            // defguard name(args) do ... end → kind="guard"
            SymbolRule {
                kind: "call",
                name: NameStrategy::CallArgName {
                    container: "arguments",
                    child_kind: "call",
                    name_kind: "identifier",
                },
                sym_kind: "guard",
                body: Some("do_block"),
                is_fn: true,
                dynamic_kind: vec![],
                call_keyword: Some("defguard"),
                kind_from_child_text: vec![],
                kind_from_name: vec![],
                require_parent_kind: None,
                constructor_if_name_matches_parent: false,
            },
            // defguardp name(args) do ... end → kind="guard"（私有）
            SymbolRule {
                kind: "call",
                name: NameStrategy::CallArgName {
                    container: "arguments",
                    child_kind: "call",
                    name_kind: "identifier",
                },
                sym_kind: "guard",
                body: Some("do_block"),
                is_fn: true,
                dynamic_kind: vec![],
                call_keyword: Some("defguardp"),
                kind_from_child_text: vec![],
                kind_from_name: vec![],
                require_parent_kind: None,
                constructor_if_name_matches_parent: false,
            },
        ],
        // Elixir 普通 call 调用：identifier 是函数名，callee_field=None
        // 注意：def/defp 等 call_keyword 匹配的 SymbolRule 会先命中走符号路径，
        // 其他普通 call（如 IO.puts、Enum.map）会走此调用规则路径
        call_rules: vec![CallRule { kind: "call", callee_field: None }],
        // Elixir 的 alias/import/use/require 也是 call 节点，不走 import 路径
        // （Python parser 的 _extract_imports 专门处理，这里留空，由 Python 端补充）
        import_kinds: vec![],
        skip_kinds: vec![],
    }
}
