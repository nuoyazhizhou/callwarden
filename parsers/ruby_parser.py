"""
ruby_parser.py
==============

基于 tree-sitter 的 Ruby 源码解析器。

提取模块、类、方法等符号，以及 require 语句和调用关系。
语法节点参考 tree-sitter-ruby 官方 grammar。

安装：pip install tree-sitter-ruby
"""

from __future__ import annotations

from typing import Any, Dict, List

import tree_sitter_ruby as tsruby

from .base import BaseParser


class RubyParser(BaseParser):
    """Ruby 源码解析器"""

    language_id = "ruby"
    language_module = tsruby

    # ------------------------------------------------------------------
    # 符号提取
    # ------------------------------------------------------------------

    def _extract_symbols(self, root, source: bytes, source_str: str,
                         module_path: str) -> List[Dict[str, Any]]:
        """提取符号：模块、类、方法"""
        symbols: List[Dict[str, Any]] = []
        self._walk_symbols(root, source, source_str, module_path, symbols, "")
        return symbols

    def _walk_symbols(self, node, source: bytes, source_str: str,
                      module_path: str, symbols: List[Dict[str, Any]],
                      parent_qualified: str):
        """递归遍历提取符号"""
        for child in node.named_children:
            kind = child.type

            if kind == "module":
                sym = self._parse_module(child, source, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)
                    # 递归进入模块体
                    body = self._find_child_by_type(child, "body_statement")
                    if body:
                        self._walk_symbols(body, source, source_str, module_path,
                                           symbols, sym["qualified_name"])

            elif kind == "class":
                sym = self._parse_class(child, source, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)
                    body = self._find_child_by_type(child, "body_statement")
                    if body:
                        self._walk_symbols(body, source, source_str, module_path,
                                           symbols, sym["qualified_name"])

            elif kind == "method":
                sym = self._parse_method(child, source, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "singleton_method":
                sym = self._parse_singleton_method(child, source, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "body_statement":
                self._walk_symbols(child, source, source_str, module_path,
                                   symbols, parent_qualified)

            elif kind == "begin":
                # begin/end 块可能包含方法定义
                self._walk_symbols(child, source, source_str, module_path,
                                   symbols, parent_qualified)

    def _parse_module(self, node, source: bytes,
                      module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析 module 声明"""
        # Ruby module 名可能是 constant 或 scope_resolution（如 ::Foo）或 qualified_ref
        name_node = self._find_child_by_type(node, "constant") \
                    or self._find_child_by_type(node, "scope_resolution")
        if not name_node:
            return None

        name = self._node_text(name_node, source)
        if parent_qualified:
            qualified = f"{parent_qualified}::{name}"
        elif module_path:
            qualified = f"{module_path}::{name}"
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
            "signature": f"module {name}",
            "has_comment": 1 if bool(comment) else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_class(self, node, source: bytes,
                     module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析 class 声明"""
        name_node = self._find_child_by_type(node, "constant") \
                    or self._find_child_by_type(node, "scope_resolution")
        if not name_node:
            return None

        name = self._node_text(name_node, source)
        if parent_qualified:
            qualified = f"{parent_qualified}::{name}"
        elif module_path:
            qualified = f"{module_path}::{name}"
        else:
            qualified = name

        comment = self._find_prev_comment(node, source)
        content = self._node_text(node, source)

        # 检测父类（superclass）
        super_node = self._find_child_by_type(node, "superclass")
        parent_class = ""
        if super_node:
            parent_const = self._find_child_by_type(super_node, "constant")
            if parent_const:
                parent_class = self._node_text(parent_const, source)

        sig = f"class {name}"
        if parent_class:
            sig += f" < {parent_class}"

        return {
            "name": name,
            "kind": "class",
            "visibility": "public",
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": sig,
            "has_comment": 1 if bool(comment) else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_method(self, node, source: bytes,
                      module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析方法声明"""
        name_node = self._find_child_by_type(node, "identifier")
        if not name_node:
            return None

        name = self._node_text(name_node, source)
        if parent_qualified:
            qualified = f"{parent_qualified}#{name}"  # Ruby 用 # 分隔实例方法
        elif module_path:
            qualified = f"{module_path}#{name}"
        else:
            qualified = name

        comment = self._find_prev_comment(node, source)
        content = self._node_text(node, source)
        params = self._extract_method_params(node, source)

        return {
            "name": name,
            "kind": "method",
            "visibility": self._detect_visibility(node, source, name),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"def {name}{params}",
            "has_comment": 1 if bool(comment) else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_singleton_method(self, node, source: bytes,
                                module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析类方法声明（def self.method）"""
        name_node = self._find_child_by_type(node, "identifier")
        if not name_node:
            return None

        name = self._node_text(name_node, source)
        # 类方法用 . 分隔（区别于实例方法的 #）
        if parent_qualified:
            qualified = f"{parent_qualified}.{name}"
        elif module_path:
            qualified = f"{module_path}.{name}"
        else:
            qualified = name

        comment = self._find_prev_comment(node, source)
        content = self._node_text(node, source)
        params = self._extract_method_params(node, source)

        return {
            "name": name,
            "kind": "singleton_method",
            "visibility": "public",
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"def self.{name}{params}",
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
        """提取 require / require_relative 语句"""
        imports: List[Dict[str, Any]] = []

        def walk(node):
            for child in node.named_children:
                if child.type == "call" and child.named_child_count > 0:
                    # 检查是否是 require 或 require_relative 调用
                    first = child.named_children[0]
                    if first.type == "identifier":
                        fn_name = self._node_text(first, source)
                        if fn_name in ("require", "require_relative", "load"):
                            arg = self._find_child_by_type(child, "argument_list")
                            if arg:
                                str_node = self._find_child_by_type(arg, "string")
                                if str_node:
                                    raw = self._node_text(str_node, source)
                                    # 去掉引号
                                    module_name = raw.strip("\"'")
                                    imports.append({
                                        "module": module_name,
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
        """提取原始调用关系"""
        calls: List[Dict[str, Any]] = []

        def walk(node, current_fn: str = "", current_qualified: str = "", current_scope: str = ""):
            for child in node.named_children:
                if child.type in ("module", "class"):
                    name_node = self._find_child_by_type(child, "constant") \
                                or self._find_child_by_type(child, "scope_resolution")
                    if name_node:
                        name = self._node_text(name_node, source)
                        if current_scope:
                            new_scope = f"{current_scope}::{name}"
                        elif module_path:
                            new_scope = f"{module_path}::{name}"
                        else:
                            new_scope = name
                        walk(child, current_fn, current_qualified, new_scope)
                    else:
                        walk(child, current_fn, current_qualified, current_scope)
                elif child.type in ("method", "singleton_method"):
                    name_node = self._find_child_by_type(child, "identifier")
                    fn_name = self._node_text(name_node, source) if name_node else ""
                    sep = "#" if child.type == "method" else "."
                    if current_scope:
                        new_qualified = f"{current_scope}{sep}{fn_name}"
                    elif module_path:
                        new_qualified = f"{module_path}{sep}{fn_name}"
                    else:
                        new_qualified = fn_name
                    walk(child, fn_name, new_qualified, current_scope)
                elif child.type == "call":
                    call_info = self._parse_call(child, source)
                    if call_info and current_fn:
                        call_info["caller_name"] = current_fn
                        call_info["caller_qualified"] = current_qualified
                        call_info["caller_module"] = module_path
                        calls.append(call_info)
                    walk(child, current_fn, current_qualified, current_scope)
                else:
                    walk(child, current_fn, current_qualified, current_scope)

        walk(root)
        return calls

    def _parse_call(self, node, source: bytes) -> Dict[str, Any]:
        """解析方法调用"""
        callee_name = ""
        callee_module = ""

        # call 节点的结构：receiver.method(args) 或 method(args)
        receiver = node.child_by_field_name("receiver")
        method_node = node.child_by_field_name("method")

        if method_node:
            callee_name = self._node_text(method_node, source)

        if receiver:
            callee_module = self._node_text(receiver, source)

        if not callee_name:
            # 回退：尝试找 identifier
            ident = self._find_child_by_type(node, "identifier")
            if ident:
                callee_name = self._node_text(ident, source)

        return {
            "callee_name": callee_name,
            "callee_module": callee_module,
            "call_line": node.start_point[0] + 1,
        }

    # ------------------------------------------------------------------
    # 模块级注释
    # ------------------------------------------------------------------

    def _has_module_comment(self, root, source: bytes) -> bool:
        """检测是否有文件级注释（# 或 =begin/=end 块注释）"""
        if not root.children:
            return False
        for child in root.children:
            if child.type == "comment":
                text = self._node_text(child, source).strip()
                # =begin/=end 是 Ruby 的块注释
                if text.startswith("=begin") or text.startswith("#!") is False and len(text) > 30:
                    return True
            elif child.type in ("module", "class"):
                break
        return False

    # ------------------------------------------------------------------
    # 工具方法
    # ------------------------------------------------------------------

    def _find_child_by_type(self, node, type_name: str):
        """按类型查找子节点"""
        for child in node.named_children:
            if child.type == type_name:
                return child
        return None

    def _find_prev_comment(self, node, source: bytes) -> str:
        """查找节点前的注释（# 行注释或 =begin/=end 块注释）"""
        comment_parts = []
        prev = node.prev_named_sibling
        while prev and prev.type == "comment":
            text = self._node_text(prev, source).strip()
            comment_parts.insert(0, text)
            prev = prev.prev_named_sibling
        return "\n".join(comment_parts) if comment_parts else ""

    def _detect_visibility(self, node, source: bytes, name: str) -> str:
        """检测方法可见性

        Ruby 方法默认 public，但以 _ 开头通常是 private 约定。
        真正的 private 通过 private 方法调用声明，难以静态分析，这里用命名约定。
        """
        if name.startswith("_"):
            return "private"
        return "public"

    def _extract_method_params(self, node, source: bytes) -> str:
        """提取方法参数列表"""
        # method 节点可能直接带 method_parameters 或在 body_statement 中
        params_node = self._find_child_by_type(node, "method_parameters")
        if params_node:
            return self._node_text(params_node, source)
        # singleton_method 可能用 bare_parameters
        params_node = self._find_child_by_type(node, "bare_parameters")
        if params_node:
            return self._node_text(params_node, source)
        return "()"
