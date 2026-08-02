//! Elixir 语言配置（从 multi_lang.rs 拆分，P0-C Step 0）
//!
//! P0-D Step 2: 扩展 Elixir 引用语义。
//! - 添加 import_directives 提取 alias/import/use/require 指令
//!   （这些是 call 节点但语义上是 import，不应作为普通 call 处理）
//! - call_rules 中已配置 dot 节点提取（extract_callee 支持 dot），
//!   IO.puts 等 dot 调用会被拆分为 (callee_name="puts", callee_module="IO")

use crate::multi_lang::{CallRule, ImportDirective, LangConfig, NameStrategy, SymbolRule};
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
        // P0-D Step 2: extract_callee 已支持 dot 节点（IO.puts），
        // split_callee 会将 "IO.puts" 拆分为 (callee_name="puts", callee_module="IO")
        call_rules: vec![CallRule {
            kind: "call",
            callee_field: None,
        }],
        // Elixir 的 alias/import/use/require 也是 call 节点，不走 import_kinds 路径
        // （由 import_directives 在 walk_node 中专门处理）
        import_kinds: vec![],
        // P0-D Step 2: Elixir import 指令规则
        // alias/import/use/require 都是 call 节点，首个 identifier 文本为关键字，
        // arguments 内的 alias 节点是目标模块名。
        // walk_node 会优先匹配 import_directives，提取为 import 而非普通 call。
        // 对齐 Python parser 的 _extract_imports 实现。
        import_directives: vec![
            ImportDirective {
                keyword: "alias",
                arguments_kind: "arguments",
                alias_kind: "alias",
            },
            ImportDirective {
                keyword: "import",
                arguments_kind: "arguments",
                alias_kind: "alias",
            },
            ImportDirective {
                keyword: "use",
                arguments_kind: "arguments",
                alias_kind: "alias",
            },
            ImportDirective {
                keyword: "require",
                arguments_kind: "arguments",
                alias_kind: "alias",
            },
        ],
        // Elixir 无 HCL 风格的 attribute traversal 引用
        reference_rules: vec![],
        skip_kinds: vec![],
    }
}
