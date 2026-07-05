"""
go_parser.py
============

基于 tree-sitter 的 Go 源码解析器。

提取函数、方法、结构体、接口等符号，以及 import 语句和调用关系。
"""

from __future__ import annotations

from typing import Any, Dict, List

import tree_sitter_go as tsgo

from .base import BaseParser


class GoParser(BaseParser):
    """Go 源码解析器"""

    language_id = "go"
    language_module = tsgo

    # --------------------------------------------------------------------
    # 符号提取
    # --------------------------------------------------------------------

    def _extract_symbols(self, root, source: bytes, source_str: str,
                         module_path: str) -> List[Dict[str, Any]]:
        """提取符号：函数、方法、结构体、接口"""
        symbols = []
        pkg_name = self._extract_package(root, source)
        self._walk_symbols(root, source, source_str, pkg_name or module_path, symbols, "")
        return symbols

    def _extract_package(self, root, source: bytes) -> str:
        """提取包名"""
        for child in root.named_children:
            if child.type == "package_clause":
                pkg_name = self._find_child_by_type(child, "package_identifier")
                if pkg_name:
                    return self._node_text(pkg_name, source)
        return ""

    def _walk_symbols(self, node, source: bytes, source_str: str,
                      module_path: str, symbols: List[Dict[str, Any]],
                      parent_qualified: str):
        """递归遍历提取符号"""
        for child in node.named_children:
            kind = child.type

            if kind == "function_declaration":
                sym = self._parse_function(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "method_declaration":
                sym = self._parse_method(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "type_declaration":
                self._walk_type_declaration(child, source, source_str, module_path,
                                           symbols, parent_qualified)

            elif kind == "struct_type":
                pass

            elif kind == "interface_type":
                pass

            elif kind == "method_spec":
                pass

    def _walk_type_declaration(self, node, source: bytes, source_str: str,
                                module_path: str, symbols: List[Dict[str, Any]],
                                parent_qualified: str):
        """遍历 type 声明（可能包含多个 type_spec"""
        for child in node.named_children:
            if child.type == "type_spec":
                name_node = self._find_child_by_type(child, "type_identifier")
                type_node = None
                for c in child.named_children:
                    if c.type in ("struct_type", "interface_type"):
                        type_node = c
                        break
                    elif c.type == "type_identifier" and c != name_node:
                        type_node = c
                        break

                if name_node and type_node:
                    name = self._node_text(name_node, source)
                    if type_node.type == "struct_type":
                        sym = self._parse_struct(child, source, source_str, module_path, parent_qualified, name)
                        if sym:
                            symbols.append(sym)
                            self._walk_struct_fields(type_node, source, source_str, module_path,
                                                     symbols, sym["qualified_name"])
                    elif type_node.type == "interface_type":
                        sym = self._parse_interface(child, source, source_str, module_path, parent_qualified, name)
                        if sym:
                            symbols.append(sym)

    def _walk_struct_fields(self, node, source: bytes, source_str: str,
                            module_path: str, symbols: List[Dict[str, Any]],
                            parent_qualified: str):
        """遍历结构体字段和方法"""
        for child in node.named_children:
            if child.type == "field_declaration_list":
                self._walk_struct_fields(child, source, source_str, module_path,
                                        symbols, parent_qualified)
            elif child.type == "method_declaration":
                sym = self._parse_method(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)
            elif child.type == "function_declaration":
                sym = self._parse_function(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

    def _parse_function(self, node, source: bytes, source_str: str,
                        module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析函数声明"""
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

        comment = self._find_prev_comment(node, source)
        has_comment = bool(comment)
        content = self._node_text(node, source)
        signature = self._extract_signature(node, source)

        return {
            "name": name,
            "kind": "fn",
            "visibility": self._detect_visibility(name),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": signature,
            "has_comment": 1 if has_comment else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_method(self, node, source: bytes, source_str: str,
                      module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析方法声明（带接收者）"""
        name_node = self._find_child_by_type(node, "field_identifier")
        if not name_node:
            return None

        name = self._node_text(name_node, source)

        receiver_type = self._extract_receiver_type(node, source)
        if receiver_type:
            if parent_qualified:
                qualified = f"{parent_qualified}.{name}"
            elif module_path:
                qualified = f"{module_path}.{receiver_type}.{name}"
            else:
                qualified = f"{receiver_type}.{name}"
        else:
            if parent_qualified:
                qualified = f"{parent_qualified}.{name}"
            elif module_path:
                qualified = f"{module_path}.{name}"
            else:
                qualified = name

        comment = self._find_prev_comment(node, source)
        has_comment = bool(comment)
        content = self._node_text(node, source)
        signature = self._extract_method_signature(node, source)

        return {
            "name": name,
            "kind": "method",
            "visibility": self._detect_visibility(name),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": signature,
            "has_comment": 1 if has_comment else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_struct(self, node, source: bytes, source_str: str,
                      module_path: str, parent_qualified: str,
                      name: str) -> Dict[str, Any]:
        """解析结构体声明"""
        if parent_qualified:
            qualified = f"{parent_qualified}.{name}"
        elif module_path:
            qualified = f"{module_path}.{name}"
        else:
            qualified = name

        comment = self._find_prev_comment(node, source)
        has_comment = bool(comment)
        content = self._node_text(node, source)

        return {
            "name": name,
            "kind": "struct",
            "visibility": self._detect_visibility(name),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"type {name} struct",
            "has_comment": 1 if has_comment else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_interface(self, node, source: bytes, source_str: str,
                         module_path: str, parent_qualified: str,
                         name: str) -> Dict[str, Any]:
        """解析接口声明"""
        if parent_qualified:
            qualified = f"{parent_qualified}.{name}"
        elif module_path:
            qualified = f"{module_path}.{name}"
        else:
            qualified = name

        comment = self._find_prev_comment(node, source)
        has_comment = bool(comment)
        content = self._node_text(node, source)

        return {
            "name": name,
            "kind": "interface",
            "visibility": self._detect_visibility(name),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"type {name} interface",
            "has_comment": 1 if has_comment else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    # --------------------------------------------------------------------
    # import 提取
    # --------------------------------------------------------------------

    def _extract_imports(self, root, source: bytes) -> List[Dict[str, Any]]:
        """提取 import 语句"""
        imports = []

        def walk(node):
            """递归遍历 AST，收集所有 import_declaration 与 import_spec 节点。

            Args:
                node: 当前遍历的 tree-sitter 节点。
            """
            for child in node.named_children:
                if child.type == "import_declaration":
                    imp = self._parse_import(child, source)
                    if imp:
                        imports.append(imp)
                elif child.type == "import_spec":
                    imp = self._parse_import_spec(child, source)
                    if imp:
                        imports.append(imp)
                walk(child)

        walk(root)
        return imports

    def _parse_import(self, node, source: bytes) -> List[Dict[str, Any]]:
        """解析 import 声明（可能包含多个 import_spec）"""
        results = []
        for child in node.named_children:
            if child.type == "import_spec":
                imp = self._parse_import_spec(child, source)
                if imp:
                    results.append(imp)
            elif child.type == "import_spec_list":
                for spec in child.named_children:
                    if spec.type == "import_spec":
                        imp = self._parse_import_spec(spec, source)
                        if imp:
                            results.append(imp)
        return results[0] if len(results) == 1 else None

    def _parse_import_spec(self, node, source: bytes) -> Dict[str, Any]:
        """解析单个 import spec"""
        path_node = self._find_child_by_type(node, "interpreted_string_literal")
        if not path_node:
            path_node = self._find_child_by_type(node, "raw_string_literal")

        module_name = self._node_text(path_node, source).strip('"').strip("'") if path_node else ""

        imported = []
        if module_name:
            if "/" in module_name:
                imported = [module_name.split("/")[-1]]
            else:
                imported = [module_name]

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
        pkg_name = self._extract_package(root, source)

        def walk(node, current_fn: str = "", current_qualified: str = ""):
            """递归遍历 AST，识别函数/方法定义并收集 call_expression 调用关系。

            Args:
                node: 当前遍历的 tree-sitter 节点。
                current_fn: 当前所在函数/方法名，用于标注调用者。
                current_qualified: 当前所在符号的完整限定名（含接收者类型），用于精确匹配。
            """
            for child in node.named_children:
                if child.type == "function_declaration":
                    name_node = self._find_child_by_type(child, "identifier")
                    fn_name = self._node_text(name_node, source) if name_node else ""
                    qual = f"{pkg_name}.{fn_name}" if pkg_name else fn_name
                    walk(child, fn_name, qual)
                elif child.type == "method_declaration":
                    name_node = self._find_child_by_type(child, "field_identifier")
                    fn_name = self._node_text(name_node, source) if name_node else ""
                    receiver_type = self._extract_receiver_type(child, source)
                    if receiver_type:
                        qual = f"{pkg_name}.{receiver_type}.{fn_name}" if pkg_name else f"{receiver_type}.{fn_name}"
                    else:
                        qual = f"{pkg_name}.{fn_name}" if pkg_name else fn_name
                    walk(child, fn_name, qual)
                elif child.type == "call_expression":
                    call_info = self._parse_call(child, source)
                    if call_info and current_fn:
                        call_info["caller_name"] = current_fn
                        call_info["caller_qualified"] = current_qualified
                        call_info["caller_module"] = pkg_name
                        calls.append(call_info)
                    walk(child, current_fn, current_qualified)
                else:
                    walk(child, current_fn, current_qualified)

        walk(root)
        return calls

    def _parse_call(self, node, source: bytes) -> Dict[str, Any]:
        """解析调用表达式"""
        callee = node.child_by_field_name("function")
        if not callee:
            if node.children:
                callee = node.children[0]
            else:
                return None

        callee_name = ""
        callee_module = ""

        if callee.type == "selector_expression":
            field = self._find_child_by_type(callee, "field_identifier")
            operand = self._find_child_by_type(callee, "identifier")
            callee_name = self._node_text(field, source) if field else ""
            callee_module = self._node_text(operand, source) if operand else ""
        elif callee.type == "identifier":
            callee_name = self._node_text(callee, source)
        else:
            callee_name = self._node_text(callee, source)

        return {
            "callee_name": callee_name,
            "callee_module": callee_module,
            "call_line": node.start_point[0] + 1,
        }

    # --------------------------------------------------------------------
    # 模块级注释
    # --------------------------------------------------------------------

    def _has_module_comment(self, root, source: bytes) -> bool:
        """检测是否有包级注释"""
        if not root.children:
            return False
        for child in root.children:
            if child.type in ("comment", "line_comment", "block_comment"):
                text = self._node_text(child, source).strip()
                if text.startswith("// Package") or "package" in text.lower() or len(text) > 50:
                    return True
            elif child.type == "package_clause":
                break
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

    def _find_prev_comment(self, node, source: bytes) -> str:
        """查找节点前的注释"""
        comment_parts = []
        prev = node.prev_named_sibling
        while prev and prev.type in ("comment", "line_comment", "block_comment"):
            text = self._node_text(prev, source).strip()
            comment_parts.insert(0, text)
            prev = prev.prev_named_sibling
        return "\n".join(comment_parts) if comment_parts else ""

    def _detect_visibility(self, name: str) -> str:
        """检测可见性（Go 中首字母大写 = 导出）"""
        if name and name[0].isupper():
            return "public"
        return "private"

    def _extract_receiver_type(self, node, source: bytes) -> str:
        """提取方法接收者类型"""
        receiver = self._find_child_by_type(node, "receiver")
        if not receiver:
            return ""
        # 接收者参数列表中找类型
        for child in receiver.named_children:
            if child.type == "parameter_list":
                for param in child.named_children:
                    if param.type == "parameter_declaration":
                        type_node = None
                        for c in param.named_children:
                            if c.type not in ("identifier", "variadic_parameter"):
                                type_node = c
                                break
                        if type_node:
                            type_text = self._node_text(type_node, source)
                            if type_text.startswith("*"):
                                type_text = type_text[1:]
                            return type_text
        return ""

    def _extract_signature(self, node, source: bytes) -> str:
        """提取函数签名"""
        name_node = self._find_child_by_type(node, "identifier")
        params_node = self._find_child_by_type(node, "parameters")
        result_node = self._find_child_by_type(node, "result")

        name = self._node_text(name_node, source) if name_node else ""
        params = self._node_text(params_node, source) if params_node else "()"
        result = ""
        if result_node:
            result = f" {self._node_text(result_node, source)}"

        return f"func {name}{params}{result}"

    def _extract_method_signature(self, node, source: bytes) -> str:
        """提取方法签名"""
        receiver = self._find_child_by_type(node, "receiver")
        name_node = self._find_child_by_type(node, "field_identifier")
        params_node = self._find_child_by_type(node, "parameters")
        result_node = self._find_child_by_type(node, "result")

        recv = self._node_text(receiver, source) if receiver else ""
        name = self._node_text(name_node, source) if name_node else ""
        params = self._node_text(params_node, source) if params_node else "()"
        result = ""
        if result_node:
            result = f" {self._node_text(result_node, source)}"

        return f"func {recv} {name}{params}{result}"
