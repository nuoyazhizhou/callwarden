"""
elixir_parser.py
================

基于 tree-sitter 的 Elixir 源码解析器。

提取 module、函数等符号，以及 alias / import / use 语句和调用关系。
语法节点参考 tree-sitter-elixir 官方 grammar。

Elixir 的语法特殊：
- defmodule ... do ... end 是 call 节点（不是专用的 class_declaration）
- def/defp 也是 call 节点
- 需要根据 call 的 identifier（defmodule/def/defp/defmacro 等）来识别声明

安装：pip install tree-sitter-elixir
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import tree_sitter_elixir as tselixir

from .base import BaseParser


class ElixirParser(BaseParser):
    """Elixir 源码解析器"""

    language_id = "elixir"
    language_module = tselixir

    # 调用关键字 → 符号 kind 映射
    DEF_KEYWORDS = {
        "def": "function",
        "defp": "function",
        "defmacro": "macro",
        "defmacrop": "macro",
        "defguard": "guard",
        "defguardp": "guard",
    }

    # ------------------------------------------------------------------
    # 符号提取
    # ------------------------------------------------------------------

    def _extract_symbols(self, root, source: bytes, source_str: str,
                         module_path: str) -> List[Dict[str, Any]]:
        """提取符号：module、函数、宏"""
        symbols: List[Dict[str, Any]] = []
        self._walk(root, source, source_str, module_path, symbols, "")
        return symbols

    def _walk(self, node, source: bytes, source_str: str,
              module_path: str, symbols: List[Dict[str, Any]],
              parent_qualified: str):
        """递归遍历 call 节点，识别 defmodule / def / defp 等声明"""
        for child in node.named_children:
            if child.type == "call":
                sym = self._parse_call_as_symbol(child, source, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)
                    # 如果是 defmodule，递归进入 do_block 提取内部声明
                    if sym["kind"] == "module":
                        do_block = self._find_child_by_type(child, "do_block")
                        if do_block:
                            self._walk(do_block, source, source_str, module_path,
                                       symbols, sym["qualified_name"])
                else:
                    # 非 defmodule/def，但内部可能含声明（如 use 宏内部）
                    self._walk(child, source, source_str, module_path,
                               symbols, parent_qualified)
            elif child.type == "do_block":
                self._walk(child, source, source_str, module_path,
                           symbols, parent_qualified)

    def _parse_call_as_symbol(self, node, source: bytes,
                              module_path: str,
                              parent_qualified: str) -> Optional[Dict[str, Any]]:
        """解析 call 节点为符号

        call 节点结构：identifier + arguments + do_block
        - identifier 为 defmodule：arguments[0] 是 alias（模块名）
        - identifier 为 def/defp：arguments[0] 是 call（函数头，含函数名）
        """
        ident_node = self._find_child_by_type(node, "identifier")
        if not ident_node:
            return None

        keyword = self._node_text(ident_node, source)

        if keyword == "defmodule":
            return self._parse_defmodule(node, source, module_path, parent_qualified)
        elif keyword in self.DEF_KEYWORDS:
            return self._parse_def(node, source, keyword, module_path, parent_qualified)
        return None

    def _parse_defmodule(self, node, source: bytes,
                         module_path: str,
                         parent_qualified: str) -> Optional[Dict[str, Any]]:
        """解析 defmodule 声明

        arguments 内第一个 alias 节点是模块名（如 MyCompany.Auth）。
        """
        args = self._find_child_by_type(node, "arguments")
        if not args:
            return None

        alias_node = self._find_child_by_type(args, "alias")
        if not alias_node:
            return None

        name = self._node_text(alias_node, source)
        if parent_qualified:
            qualified = f"{parent_qualified}.{name}"
        elif module_path:
            qualified = f"{module_path}.{name}"
        else:
            qualified = name

        comment = self._find_prev_comment(node, source)
        content = self._node_text(node, source)

        return {
            "name": name,
            "kind": "module",
            "visibility": "public",
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"defmodule {name}",
            "has_comment": 1 if bool(comment) else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_def(self, node, source: bytes, keyword: str,
                   module_path: str,
                   parent_qualified: str) -> Optional[Dict[str, Any]]:
        """解析 def/defp/defmacro 等函数声明

        arguments[0] 是 call 节点，其 identifier 是函数名。
        """
        args = self._find_child_by_type(node, "arguments")
        if not args:
            return None

        # arguments 内第一个 call 节点包含函数名和参数
        func_call = self._find_child_by_type(args, "call")
        if not func_call:
            return None

        func_ident = self._find_child_by_type(func_call, "identifier")
        if not func_ident:
            return None

        name = self._node_text(func_ident, source)
        if parent_qualified:
            qualified = f"{parent_qualified}.{name}"
        elif module_path:
            qualified = f"{module_path}.{name}"
        else:
            qualified = name

        kind = self.DEF_KEYWORDS[keyword]
        visibility = "private" if keyword.endswith("p") else "public"

        comment = self._find_prev_comment(node, source)
        content = self._node_text(node, source)

        return {
            "name": name,
            "kind": kind,
            "visibility": visibility,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"{keyword} {name}",
            "has_comment": 1 if bool(comment) else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    # ------------------------------------------------------------------
    # import 提取
    # ------------------------------------------------------------------

    def _extract_imports(self, root, source: bytes) -> List[Dict[str, Any]]:
        """提取 alias / import / use / require 语句"""
        imports: List[Dict[str, Any]] = []

        def walk(node):
            """递归遍历 AST，收集 alias / import / use / require 调用形式的模块引用。

            Args:
                node: 当前遍历的 tree-sitter 节点。
            """
            for child in node.named_children:
                if child.type == "call":
                    ident = self._find_child_by_type(child, "identifier")
                    if ident and self._node_text(ident, source) in ("alias", "import", "use", "require"):
                        args = self._find_child_by_type(child, "arguments")
                        if args:
                            alias_node = self._find_child_by_type(args, "alias")
                            if alias_node:
                                imports.append({
                                    "module": self._node_text(alias_node, source),
                                    "imported": [],
                                    "line": child.start_point[0] + 1,
                                })
                walk(child)

        walk(root)
        return imports

    # ------------------------------------------------------------------
    # 调用关系提取
    # ------------------------------------------------------------------

    def _extract_raw_calls(self, root, source: bytes,
                           module_path: str) -> List[Dict[str, Any]]:
        """提取原始调用关系

        Elixir 中函数定义（def/defp）是 call 节点，其 arguments 内含函数头 call。
        普通调用也是 call 节点。需要区分：
        - def/defp/defmacro：函数定义，进入其 do_block 提取调用，并更新 current_fn
        - defmodule：模块定义，进入其 do_block 但不更新 current_fn
        - 其他 call：普通函数调用
        - 其他节点类型（binary_operator 等）：递归遍历找内部嵌套的 call
        """
        calls: List[Dict[str, Any]] = []

        # 构建 import_map（从 alias/use 提取的模块别名映射）
        import_map: Dict[str, str] = {}
        for imp in self._extract_imports(root, source):
            mod = imp.get("module", "")
            if mod:
                # alias Foo.Bar -> 别名是最后一段 Bar
                parts = mod.split(".")
                alias_name = parts[-1]
                import_map[alias_name] = mod

        def walk(node, current_fn: str = "", current_qualified: str = ""):
            """递归遍历 AST，区分 def/defp/defmodule 等定义节点并收集普通 call 调用关系。

            Args:
                node: 当前遍历的 tree-sitter 节点。
                current_fn: 当前所在函数名，用于标注调用者。
                current_qualified: 当前所在符号的完整限定名（含模块前缀），用于精确匹配。
            """
            for child in node.named_children:
                if child.type == "call":
                    ident = self._find_child_by_type(child, "identifier")
                    if ident:
                        keyword = self._node_text(ident, source)
                        if keyword in self.DEF_KEYWORDS:
                            # 函数定义：记录函数名后递归进入 do_block
                            args = self._find_child_by_type(child, "arguments")
                            if args:
                                func_call = self._find_child_by_type(args, "call")
                                if func_call:
                                    func_ident = self._find_child_by_type(func_call, "identifier")
                                    if func_ident:
                                        fn_name = self._node_text(func_ident, source)
                                        if current_qualified:
                                            qual = f"{current_qualified}.{fn_name}"
                                        elif module_path:
                                            qual = f"{module_path}.{fn_name}"
                                        else:
                                            qual = fn_name
                                        do_block = self._find_child_by_type(child, "do_block")
                                        if do_block:
                                            walk(do_block, fn_name, qual)
                                        continue
                            # 未能提取函数名的 def，仍递归处理
                            walk(child, current_fn, current_qualified)
                            continue
                        elif keyword == "defmodule":
                            # 模块定义：递归进入 do_block 但不更新 current_fn
                            # 更新 current_qualified（模块名前缀）
                            args = self._find_child_by_type(child, "arguments")
                            module_name = ""
                            if args:
                                alias_node = self._find_child_by_type(args, "alias")
                                if alias_node:
                                    module_name = self._node_text(alias_node, source)
                            new_qualified = current_qualified
                            if module_name:
                                if current_qualified:
                                    new_qualified = f"{current_qualified}.{module_name}"
                                elif module_path:
                                    new_qualified = f"{module_path}.{module_name}"
                                else:
                                    new_qualified = module_name
                            do_block = self._find_child_by_type(child, "do_block")
                            if do_block:
                                walk(do_block, current_fn, new_qualified)
                            continue
                        else:
                            # 普通函数调用（非 def/defp/defmodule）
                            if current_fn:
                                # 推断 callee_module
                                callee_mod = ""
                                # 检查是否有 alias 前缀（如 Logger.info 中的 Logger）
                                if "." in keyword:
                                    parts = keyword.split(".")
                                    first_part = parts[0]
                                    if first_part in import_map:
                                        callee_mod = import_map[first_part]
                                    else:
                                        callee_mod = first_part
                                    # callee_name 取最后一段
                                    keyword_name = parts[-1]
                                else:
                                    keyword_name = keyword
                                    # 检查是否是 alias 引入的模块
                                    if keyword in import_map:
                                        callee_mod = import_map[keyword]

                                calls.append({
                                    "caller_name": current_fn,
                                    "caller_module": module_path,
                                    "caller_qualified": current_qualified,
                                    "callee_name": keyword_name,
                                    "callee_module": callee_mod,
                                    "call_line": child.start_point[0] + 1,
                                })
                            # 递归处理 call 内部可能嵌套的子 call（如 arguments 内的 call）
                            walk(child, current_fn, current_qualified)
                            continue
                    # 没有 identifier 的 call，递归处理
                    walk(child, current_fn, current_qualified)
                else:
                    # 所有其他节点类型（do_block/stab_clause/binary_operator 等）
                    # 递归遍历以发现内部嵌套的 call
                    walk(child, current_fn, current_qualified)

        walk(root)
        return calls

    # ------------------------------------------------------------------
    # 模块级注释
    # ------------------------------------------------------------------

    def _has_module_comment(self, root, source: bytes) -> bool:
        """检测是否有文件级注释（# 或 @moduledoc）"""
        for child in root.children:
            if child.type in ("comment", "line_comment", "block_comment"):
                text = self._node_text(child, source).strip()
                if text.startswith("#"):
                    return True
            elif child.type == "source":
                continue
            else:
                break
        return False

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _find_child_by_type(self, node, type_name: str):
        """按类型查找第一个命名子节点"""
        for child in node.named_children:
            if child.type == type_name:
                return child
        return None

    def _find_prev_comment(self, node, source: bytes) -> str:
        """查找节点前的注释"""
        comment_parts = []
        prev = node.prev_named_sibling
        while prev and prev.type in ("comment", "line_comment", "block_comment"):
            text = self._node_text(prev, source).strip()
            comment_parts.insert(0, text)
            prev = prev.prev_named_sibling
        return "\n".join(comment_parts) if comment_parts else ""
