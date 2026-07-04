"""
kotlin_parser.py
================

基于 tree-sitter 的 Kotlin 源码解析器。

提取类、函数、方法等符号，以及 import 语句和调用关系。
"""

from __future__ import annotations

from typing import Any, Dict, List

import tree_sitter_kotlin as tskotlin
from tree_sitter import Language, Parser

from .base import BaseParser


class KotlinParser(BaseParser):
    """Kotlin 源码解析器"""

    language_id = "kotlin"
    language_module = tskotlin

    # --------------------------------------------------------------------
    # 符号提取
    # --------------------------------------------------------------------

    def _extract_symbols(self, root, source: bytes, source_str: str,
                         module_path: str) -> List[Dict[str, Any]]:
        """提取符号：类、函数、方法"""
        symbols = []
        pkg_name = self._extract_package(root, source)
        self._walk_symbols(root, source, source_str, pkg_name or module_path, symbols, "")
        return symbols

    def _extract_package(self, root, source: bytes) -> str:
        """提取包名"""
        for child in root.named_children:
            if child.type == "package_header":
                qual = self._find_child_by_type(child, "qualified_identifier")
                if qual:
                    return self._node_text(qual, source)
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

            elif kind == "class_declaration":
                sym = self._parse_class(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)
                    # 递归提取类内方法
                    class_body = self._find_child_by_type(child, "class_body")
                    if class_body:
                        self._walk_class_body(class_body, source, source_str, module_path,
                                              symbols, sym["qualified_name"])

            elif kind == "class_body":
                self._walk_class_body(child, source, source_str, module_path,
                                      symbols, parent_qualified)

    def _walk_class_body(self, node, source: bytes, source_str: str,
                         module_path: str, symbols: List[Dict[str, Any]],
                         parent_qualified: str):
        """遍历类体提取方法和属性"""
        for child in node.named_children:
            if child.type == "function_declaration":
                sym = self._parse_function(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)
            elif child.type == "property_declaration":
                # 属性也可以记录，但先不重点处理
                pass
            elif child.type == "class_declaration":
                # 嵌套类
                self._walk_symbols(child, source, source_str, module_path,
                                   symbols, parent_qualified)

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

    # --------------------------------------------------------------------
    # import 提取
    # --------------------------------------------------------------------

    def _extract_imports(self, root, source: bytes) -> List[Dict[str, Any]]:
        """提取 import 语句"""
        imports = []

        def walk(node):
            """递归遍历 AST，收集所有 import 节点。

            Args:
                node: 当前遍历的 tree-sitter 节点。
            """
            for child in node.named_children:
                if child.type == "import":
                    imp = self._parse_import(child, source)
                    if imp:
                        imports.append(imp)
                walk(child)

        walk(root)
        return imports

    def _parse_import(self, node, source: bytes) -> Dict[str, Any]:
        """解析单个 import 语句"""
        qual = self._find_child_by_type(node, "qualified_identifier")
        module_name = self._node_text(qual, source) if qual else ""

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

        def walk(node, current_fn: str = "", current_qualified: str = ""):
            """递归遍历 AST，识别函数/类定义并收集 call_expression 调用关系。

            Args:
                node: 当前遍历的 tree-sitter 节点。
                current_fn: 当前所在函数/类名，用于标注调用者。
                current_qualified: 当前所在符号的完整限定名，用于精确匹配。
            """
            for child in node.named_children:
                if child.type == "function_declaration":
                    name_node = self._find_child_by_type(child, "identifier")
                    fn_name = self._node_text(name_node, source) if name_node else ""
                    if current_qualified:
                        qual = f"{current_qualified}.{fn_name}"
                    elif pkg_name:
                        qual = f"{pkg_name}.{fn_name}"
                    else:
                        qual = fn_name
                    walk(child, fn_name, qual)
                elif child.type == "class_declaration":
                    name_node = self._find_child_by_type(child, "identifier")
                    class_name = self._node_text(name_node, source) if name_node else ""
                    if current_qualified:
                        qual = f"{current_qualified}.{class_name}"
                    elif pkg_name:
                        qual = f"{pkg_name}.{class_name}"
                    else:
                        qual = class_name
                    walk(child, class_name, qual)
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
        callee = node.child_by_field_name("callee")
        if not callee:
            # 尝试直接找第一个子节点
            if node.children:
                callee = node.children[0]
            else:
                return None

        callee_name = self._node_text(callee, source) if callee else ""
        callee_module = ""

        # 处理点调用（如 obj.method()）
        if callee.type == "navigation_expression":
            parts = []
            for child in callee.named_children:
                if child.type == "simple_identifier" or child.type == "identifier":
                    parts.append(self._node_text(child, source))
                elif child.type == "member_access_operator":
                    pass
            if len(parts) >= 2:
                callee_name = parts[-1]
                callee_module = parts[-2]

        return {
            "callee_name": callee_name,
            "callee_module": callee_module,
            "call_line": node.start_point[0] + 1,
        }

    # --------------------------------------------------------------------
    # 模块级注释
    # --------------------------------------------------------------------

    def _has_module_comment(self, root, source: bytes) -> bool:
        """检测是否有文件级注释（第一个命名子节点是注释）"""
        if not root.children:
            return False
        first = root.children[0]
        if first.type in ("comment", "block_comment"):
            text = self._node_text(first, source).strip()
            if text.startswith("/**") or "\n" in text:
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

    def _find_prev_comment(self, node, source: bytes) -> str:
        """查找节点前的注释"""
        comment_parts = []
        prev = node.prev_named_sibling
        while prev and prev.type in ("comment", "block_comment"):
            text = self._node_text(prev, source).strip()
            comment_parts.insert(0, text)
            prev = prev.prev_named_sibling
        return "\n".join(comment_parts) if comment_parts else ""

    def _detect_visibility(self, node, source: bytes) -> str:
        """检测可见性修饰符"""
        for child in node.named_children:
            if child.type == "visibility_modifiers":
                text = self._node_text(child, source)
                if "private" in text:
                    return "private"
                elif "internal" in text:
                    return "internal"
                elif "protected" in text:
                    return "protected"
        return "public"  # Kotlin 默认 public

    def _extract_signature(self, node, source: bytes) -> str:
        """提取函数签名"""
        name_node = self._find_child_by_type(node, "identifier")
        params_node = self._find_child_by_type(node, "function_value_parameters")
        return_type = None

        # 找返回类型（冒号后面的 type）
        for i, child in enumerate(node.children):
            if child.type == ":" and i + 1 < len(node.children):
                next_child = node.children[i + 1]
                if next_child.type in ("user_type", "nullable_type", "function_type"):
                    return_type = next_child
                    break

        name = self._node_text(name_node, source) if name_node else ""
        params = self._node_text(params_node, source) if params_node else "()"
        ret = ""
        if return_type:
            ret = f": {self._node_text(return_type, source)}"
        return f"fun {name}{params}{ret}"
