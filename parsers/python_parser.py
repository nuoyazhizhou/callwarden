"""
python_parser.py
================

基于 tree-sitter 的 Python 源码解析器。

提取函数、类、方法等符号，以及 import 语句和调用关系。
"""

from __future__ import annotations

from typing import Any, Dict, List

import tree_sitter_python as tspython
from tree_sitter import Language, Parser

from .base import BaseParser


class PythonParser(BaseParser):
    """Python 源码解析器"""

    language_id = "python"
    language_module = tspython

    # --------------------------------------------------------------------
    # 符号提取
    # --------------------------------------------------------------------

    def _extract_symbols(self, root, source: bytes, source_str: str,
                         module_path: str) -> List[Dict[str, Any]]:
        """提取符号：函数、类、方法"""
        symbols = []
        self._walk_symbols(root, source, source_str, module_path, symbols, "")
        return symbols

    def _walk_symbols(self, node, source: bytes, source_str: str,
                      module_path: str, symbols: List[Dict[str, Any]],
                      parent_qualified: str):
        """递归遍历提取符号"""
        for child in node.named_children:
            kind = child.type

            if kind == "function_definition":
                sym = self._parse_function(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)
                    # 递归提取函数内的嵌套函数
                    body = self._find_child_by_type(child, "block")
                    if body:
                        self._walk_symbols(body, source, source_str, module_path,
                                           symbols, sym["qualified_name"])

            elif kind == "class_definition":
                sym = self._parse_class(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)
                    # 递归提取类内方法
                    body = self._find_child_by_type(child, "block")
                    if body:
                        self._walk_symbols(body, source, source_str, module_path,
                                           symbols, sym["qualified_name"])

            elif kind == "block":
                self._walk_symbols(child, source, source_str, module_path, symbols, parent_qualified)

            elif kind == "decorated_definition":
                # 装饰器包裹的定义，递归处理内部的 function/class
                inner = self._find_child_by_type(child, "function_definition")
                if inner:
                    sym = self._parse_function(inner, source, source_str, module_path, parent_qualified)
                    if sym:
                        symbols.append(sym)
                inner = self._find_child_by_type(child, "class_definition")
                if inner:
                    sym = self._parse_class(inner, source, source_str, module_path, parent_qualified)
                    if sym:
                        symbols.append(sym)

    def _parse_function(self, node, source: bytes, source_str: str,
                        module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析函数定义"""
        name_node = self._find_child_by_type(node, "identifier")
        if not name_node:
            return None

        name = self._node_text(name_node, source)
        if parent_qualified:
            qualified = f"{parent_qualified}.{name}"
        elif module_path:
            qualified = f"{module_path}.{name}"
        else:
            qualified = name

        docstring = self._get_docstring(node, source)
        has_comment = bool(docstring)
        content = self._node_text(node, source)
        signature = self._extract_signature(node, source)

        return {
            "name": name,
            "kind": "fn",
            "visibility": "private" if name.startswith("_") else "public",
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": signature,
            "has_comment": 1 if has_comment else 0,
            "comment_content": docstring,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_class(self, node, source: bytes, source_str: str,
                     module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析类定义"""
        name_node = self._find_child_by_type(node, "identifier")
        if not name_node:
            return None

        name = self._node_text(name_node, source)
        if parent_qualified:
            qualified = f"{parent_qualified}.{name}"
        elif module_path:
            qualified = f"{module_path}.{name}"
        else:
            qualified = name

        docstring = self._get_docstring(node, source)
        has_comment = bool(docstring)
        content = self._node_text(node, source)

        return {
            "name": name,
            "kind": "class",
            "visibility": "private" if name.startswith("_") else "public",
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"class {name}",
            "has_comment": 1 if has_comment else 0,
            "comment_content": docstring,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _get_docstring(self, node, source: bytes) -> str:
        """获取函数/类的文档字符串"""
        body = self._find_child_by_type(node, "block")
        if not body or not body.named_children:
            return ""

        first = body.named_children[0]
        if first.type == "expression_statement":
            str_node = self._find_child_by_type(first, "string")
            if str_node:
                text = self._node_text(str_node, source)
                # 去掉三引号
                if text.startswith('"""') or text.startswith("'''"):
                    text = text[3:-3] if len(text) > 6 else text[3:]
                elif text.startswith('"') or text.startswith("'"):
                    text = text[1:-1] if len(text) > 2 else text[1:]
                return text.strip()
        return ""

    # --------------------------------------------------------------------
    # import 提取
    # --------------------------------------------------------------------

    def _extract_imports(self, root, source: bytes) -> List[Dict[str, Any]]:
        """提取 import 语句"""
        imports = []

        def walk(node):
            for child in node.named_children:
                if child.type == "import_statement":
                    imp = self._parse_import_statement(child, source)
                    if imp:
                        imports.append(imp)
                elif child.type == "import_from_statement":
                    imp = self._parse_from_import(child, source)
                    if imp:
                        imports.append(imp)
                walk(child)

        walk(root)
        return imports

    def _parse_import_statement(self, node, source: bytes) -> Dict[str, Any]:
        """解析 import xxx 语句"""
        modules = []
        for child in node.named_children:
            if child.type == "dotted_name":
                modules.append(self._node_text(child, source))

        return {
            "module": modules[0] if modules else "",
            "imported": [],
            "line": node.start_point[0] + 1,
        }

    def _parse_from_import(self, node, source: bytes) -> Dict[str, Any]:
        """解析 from xxx import yyy 语句"""
        module_name = ""
        imported = []

        for child in node.named_children:
            if child.type == "dotted_name":
                module_name = self._node_text(child, source)
            elif child.type == "import_from_clause":
                # 跳过 '(' ')' 等，提取标识符
                for sub in child.named_children:
                    if sub.type == "dotted_name":
                        imported.append(self._node_text(sub, source))

        return {
            "module": module_name,
            "imported": imported,
            "line": node.start_point[0] + 1,
        }

    # --------------------------------------------------------------------
    # 调用关系提取
    # --------------------------------------------------------------------

    def _extract_raw_calls(self, root, source: bytes,
                           module_path: str) -> List[Dict[str, Any]]:
        """提取原始调用关系"""
        calls = []

        def walk(node, current_fn: str = ""):
            for child in node.named_children:
                if child.type == "function_definition":
                    name_node = self._find_child_by_type(child, "identifier")
                    fn_name = self._node_text(name_node, source) if name_node else ""
                    if module_path:
                        qual = f"{module_path}.{fn_name}"
                    else:
                        qual = fn_name
                    walk(child, qual)
                elif child.type == "call":
                    call_info = self._parse_call(child, source)
                    if call_info and current_fn:
                        call_info["caller_name"] = current_fn
                        call_info["caller_module"] = module_path
                        calls.append(call_info)
                    walk(child, current_fn)
                else:
                    walk(child, current_fn)

        walk(root)
        return calls

    def _parse_call(self, node, source: bytes) -> Dict[str, Any]:
        """解析调用表达式"""
        func_node = node.child_by_field_name("function")
        if not func_node:
            return None

        callee_name = self._node_text(func_node, source)
        callee_module = ""

        # 处理属性调用（如 obj.method()）
        if func_node.type == "attribute":
            attr_name = self._find_child_by_type(func_node, "identifier")
            obj_node = self._find_child_by_type(func_node, "identifier")
            if attr_name:
                callee_name = self._node_text(attr_name, source)
                # 第一个 identifier 是对象
                for child in func_node.named_children:
                    if child.type == "identifier":
                        callee_module = self._node_text(child, source)
                        break

        return {
            "callee_name": callee_name,
            "callee_module": callee_module,
            "call_line": node.start_point[0] + 1,
        }

    # --------------------------------------------------------------------
    # 模块级注释
    # --------------------------------------------------------------------

    def _has_module_comment(self, root, source: bytes) -> bool:
        """检测是否有模块级文档字符串"""
        if not root.named_children:
            return False
        first = root.named_children[0]
        if first.type == "expression_statement":
            str_node = self._find_child_by_type(first, "string")
            if str_node:
                return True
        return False

    # --------------------------------------------------------------------
    # 工具方法
    # --------------------------------------------------------------------

    def _find_child_by_type(self, node, type_name: str):
        """按类型查找子节点"""
        for child in node.named_children:
            if child.type == type_name:
                return child
        return None

    def _extract_signature(self, node, source: bytes) -> str:
        """提取函数签名"""
        name_node = self._find_child_by_type(node, "identifier")
        params_node = self._find_child_by_type(node, "parameters")
        return_type = self._find_child_by_type(node, "type")

        name = self._node_text(name_node, source) if name_node else ""
        params = self._node_text(params_node, source) if params_node else "()"
        ret = f" -> {self._node_text(return_type, source)}" if return_type else ""
        return f"def {name}{params}{ret}"
