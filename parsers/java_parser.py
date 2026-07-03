"""
java_parser.py
==============

基于 tree-sitter 的 Java 源码解析器。

提取类、接口、枚举、方法等符号，以及 import 语句和调用关系。
"""

from __future__ import annotations

from typing import Any, Dict, List

import tree_sitter_java as tsjava

from .base import BaseParser


class JavaParser(BaseParser):
    """Java 源码解析器"""

    language_id = "java"
    language_module = tsjava

    # --------------------------------------------------------------------
    # 符号提取
    # --------------------------------------------------------------------

    def _extract_symbols(self, root, source: bytes, source_str: str,
                         module_path: str) -> List[Dict[str, Any]]:
        """提取符号：类、接口、枚举、方法"""
        symbols = []
        pkg_name = self._extract_package(root, source)
        self._walk_symbols(root, source, source_str, pkg_name or module_path, symbols, "")
        return symbols

    def _extract_package(self, root, source: bytes) -> str:
        """提取包名"""
        for child in root.named_children:
            if child.type == "package_declaration":
                scopes = self._find_child_by_type(child, "scoped_identifier")
                if scopes:
                    return self._node_text(scopes, source)
                ident = self._find_child_by_type(child, "identifier")
                if ident:
                    return self._node_text(ident, source)
        return ""

    def _walk_symbols(self, node, source: bytes, source_str: str,
                      module_path: str, symbols: List[Dict[str, Any]],
                      parent_qualified: str):
        """递归遍历提取符号"""
        for child in node.named_children:
            kind = child.type

            if kind == "class_declaration":
                sym = self._parse_class(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)
                    body = self._find_child_by_type(child, "class_body")
                    if body:
                        self._walk_class_body(body, source, source_str, module_path,
                                            symbols, sym["qualified_name"])

            elif kind == "interface_declaration":
                sym = self._parse_interface(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)
                    body = self._find_child_by_type(child, "interface_body")
                    if body:
                        self._walk_class_body(body, source, source_str, module_path,
                                            symbols, sym["qualified_name"])

            elif kind == "enum_declaration":
                sym = self._parse_enum(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "method_declaration":
                sym = self._parse_method(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "constructor_declaration":
                sym = self._parse_constructor(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "class_body" or kind == "interface_body" or kind == "enum_body":
                self._walk_class_body(child, source, source_str, module_path,
                                    symbols, parent_qualified)

    def _walk_class_body(self, node, source: bytes, source_str: str,
                         module_path: str, symbols: List[Dict[str, Any]],
                         parent_qualified: str):
        """遍历类体/接口体提取方法和嵌套类"""
        for child in node.named_children:
            if child.type in ("class_declaration", "interface_declaration", "enum_declaration"):
                self._walk_symbols(child, source, source_str, module_path,
                                 symbols, parent_qualified)
            elif child.type == "method_declaration":
                sym = self._parse_method(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)
            elif child.type == "constructor_declaration":
                sym = self._parse_constructor(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

    def _parse_class(self, node, source: bytes, source_str: str,
                     module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析类声明"""
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

        return {
            "name": name,
            "kind": "class",
            "visibility": self._detect_visibility(node, source),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"class {name}",
            "has_comment": 1 if has_comment else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_interface(self, node, source: bytes, source_str: str,
                         module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析接口声明"""
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

        return {
            "name": name,
            "kind": "interface",
            "visibility": self._detect_visibility(node, source),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"interface {name}",
            "has_comment": 1 if has_comment else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_enum(self, node, source: bytes, source_str: str,
                    module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析枚举声明"""
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

        return {
            "name": name,
            "kind": "enum",
            "visibility": self._detect_visibility(node, source),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"enum {name}",
            "has_comment": 1 if has_comment else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_method(self, node, source: bytes, source_str: str,
                      module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析方法声明"""
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
        signature = self._extract_signature(node, source, name)

        return {
            "name": name,
            "kind": "method",
            "visibility": self._detect_visibility(node, source),
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

    def _parse_constructor(self, node, source: bytes, source_str: str,
                           module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析构造方法声明"""
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
        signature = self._extract_constructor_signature(node, source, name)

        return {
            "name": name,
            "kind": "constructor",
            "visibility": self._detect_visibility(node, source),
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

    # --------------------------------------------------------------------
    # import 提取
    # --------------------------------------------------------------------

    def _extract_imports(self, root, source: bytes) -> List[Dict[str, Any]]:
        """提取 import 语句"""
        imports = []

        def walk(node):
            for child in node.named_children:
                if child.type == "import_declaration":
                    imp = self._parse_import(child, source)
                    if imp:
                        imports.append(imp)
                walk(child)

        walk(root)
        return imports

    def _parse_import(self, node, source: bytes) -> Dict[str, Any]:
        """解析单个 import 语句"""
        scoped = self._find_child_by_type(node, "scoped_identifier")
        if scoped:
            module_name = self._node_text(scoped, source)
        else:
            ident = self._find_child_by_type(node, "identifier")
            module_name = self._node_text(ident, source) if ident else ""

        return {
            "module": module_name,
            "imported": [],
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

        def walk(node, current_fn: str = ""):
            for child in node.named_children:
                if child.type in ("method_declaration", "constructor_declaration"):
                    name_node = self._find_child_by_type(child, "identifier")
                    fn_name = self._node_text(name_node, source) if name_node else ""
                    qual = f"{pkg_name}.{fn_name}" if pkg_name else fn_name
                    walk(child, qual)
                elif child.type == "method_invocation":
                    call_info = self._parse_call(child, source)
                    if call_info and current_fn:
                        call_info["caller_name"] = current_fn
                        call_info["caller_module"] = pkg_name
                        calls.append(call_info)
                    walk(child, current_fn)
                else:
                    walk(child, current_fn)

        walk(root)
        return calls

    def _parse_call(self, node, source: bytes) -> Dict[str, Any]:
        """解析方法调用"""
        callee_name = ""
        callee_module = ""

        name_node = self._find_child_by_type(node, "identifier")
        if name_node:
            callee_name = self._node_text(name_node, source)

        scope = node.child_by_field_name("scope")
        if scope:
            callee_module = self._node_text(scope, source)

        return {
            "callee_name": callee_name,
            "callee_module": callee_module,
            "call_line": node.start_point[0] + 1,
        }

    # --------------------------------------------------------------------
    # 模块级注释
    # --------------------------------------------------------------------

    def _has_module_comment(self, root, source: bytes) -> bool:
        """检测是否有文件级注释（Javadoc 风格）"""
        if not root.children:
            return False
        for child in root.children:
            if child.type in ("comment", "block_comment", "line_comment"):
                text = self._node_text(child, source).strip()
                if text.startswith("/**") or text.startswith("/*") and len(text) > 50:
                    return True
            elif child.type == "package_declaration":
                break
            elif child.type == "import_declaration":
                continue
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
        while prev and prev.type in ("comment", "block_comment", "line_comment"):
            text = self._node_text(prev, source).strip()
            comment_parts.insert(0, text)
            prev = prev.prev_named_sibling
        return "\n".join(comment_parts) if comment_parts else ""

    def _detect_visibility(self, node, source: bytes) -> str:
        """检测可见性修饰符"""
        modifiers = self._find_child_by_type(node, "modifiers")
        if modifiers:
            text = self._node_text(modifiers, source)
            if "private" in text:
                return "private"
            elif "protected" in text:
                return "protected"
            elif "public" in text:
                return "public"
        return "default"  # Java 默认包级可见

    def _extract_signature(self, node, source: bytes, name: str) -> str:
        """提取方法签名"""
        params_node = self._find_child_by_type(node, "formal_parameters")
        type_node = self._find_child_by_type(node, "type_parameters")

        params = self._node_text(params_node, source) if params_node else "()"
        type_params = self._node_text(type_node, source) if type_node else ""

        return f"{type_params} {name}{params}".strip()

    def _extract_constructor_signature(self, node, source: bytes, name: str) -> str:
        """提取构造方法签名"""
        params_node = self._find_child_by_type(node, "formal_parameters")
        params = self._node_text(params_node, source) if params_node else "()"
        return f"{name}{params}"
