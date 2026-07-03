"""
typescript.py
=============

基于 tree-sitter 的 TypeScript/JavaScript 源码解析器。

支持 TypeScript (.ts/.tsx) 和 JavaScript (.js/.jsx)，
提取函数、类、接口、方法等符号，以及 import 语句和调用关系。
"""

from __future__ import annotations

from typing import Any, Dict, List

import tree_sitter_typescript as tstypescript
from tree_sitter import Language, Parser

from .base import BaseParser
from ..config import read_file_normalized


class TypeScriptParser(BaseParser):
    """TypeScript/JavaScript 源码解析器"""

    language_id = "typescript"

    def __init__(self, dialect: str = "typescript"):
        """
        Args:
            dialect: 方言，"typescript" / "tsx" / "javascript" / "jsx"
        """
        if dialect in ("tsx", "jsx"):
            self.language_module = type('_mod', (), {'language': staticmethod(tstypescript.language_tsx)})()
        else:
            self.language_module = type('_mod', (), {'language': staticmethod(tstypescript.language_typescript)})()
        if dialect in ("javascript", "jsx"):
            self.language_id = "javascript"
        super().__init__()

    # --------------------------------------------------------------------
    # 符号提取
    # --------------------------------------------------------------------

    def _extract_symbols(self, root, source: bytes, source_str: str,
                         module_path: str) -> List[Dict[str, Any]]:
        """提取符号：函数、类、接口、方法"""
        symbols = []
        self._walk_symbols(root, source, source_str, module_path, symbols, "")
        return symbols

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
                    # 递归提取类内的方法
                    class_body = self._find_child_by_type(child, "class_body")
                    if class_body:
                        qual = sym["qualified_name"]
                        self._walk_class_body(class_body, source, source_str, module_path, symbols, qual)

            elif kind == "interface_declaration":
                sym = self._parse_interface(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "export_statement":
                # export 语句包裹的声明，递归处理
                self._walk_symbols(child, source, source_str, module_path, symbols, parent_qualified)

            # 跳过函数体内部以避免重复提取
            if kind not in ("statement_block", "class_body"):
                self._walk_symbols(child, source, source_str, module_path, symbols, parent_qualified)

    def _walk_class_body(self, node, source: bytes, source_str: str,
                         module_path: str, symbols: List[Dict[str, Any]],
                         parent_qualified: str):
        """遍历类体提取方法"""
        for child in node.named_children:
            kind = child.type
            if kind == "method_definition":
                sym = self._parse_method(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)
            elif kind == "class_declaration":
                # 嵌套类
                self._walk_symbols(child, source, source_str, module_path, symbols, parent_qualified)

    def _parse_function(self, node, source: bytes, source_str: str,
                        module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析函数声明"""
        name_node = self._find_child_by_type(node, "identifier")
        if not name_node:
            return None

        name = self._node_text(name_node, source)
        qualified = f"{parent_qualified}.{name}" if parent_qualified else (
            f"{module_path}.{name}" if module_path else name
        )

        comment = self._find_prev_comment(node, source)
        has_comment = bool(comment)

        body_node = self._find_child_by_type(node, "statement_block")
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
        name_node = self._find_child_by_type(node, "type_identifier")
        if not name_node:
            return None

        name = self._node_text(name_node, source)
        qualified = f"{parent_qualified}.{name}" if parent_qualified else (
            f"{module_path}.{name}" if module_path else name
        )

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
        name_node = self._find_child_by_type(node, "type_identifier")
        if not name_node:
            return None

        name = self._node_text(name_node, source)
        qualified = f"{module_path}.{name}" if module_path else name

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

    def _parse_method(self, node, source: bytes, source_str: str,
                      module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析类方法"""
        name_node = self._find_child_by_type(node, "property_identifier")
        if not name_node:
            return None

        name = self._node_text(name_node, source)
        qualified = f"{parent_qualified}.{name}" if parent_qualified else name

        comment = self._find_prev_comment(node, source)
        has_comment = bool(comment)
        content = self._node_text(node, source)
        signature = self._extract_method_signature(node, source)

        return {
            "name": name,
            "kind": "method",
            "visibility": self._detect_method_visibility(node, source),
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
                if child.type == "import_statement":
                    imp = self._parse_import(child, source)
                    if imp:
                        imports.append(imp)
                walk(child)

        walk(root)
        return imports

    def _parse_import(self, node, source: bytes) -> Dict[str, Any]:
        """解析单个 import 语句"""
        source_node = self._find_child_by_type(node, "string")
        module_name = self._node_text(source_node, source).strip("'\"") if source_node else ""

        # 提取导入的符号
        imported = []
        import_clause = self._find_child_by_type(node, "import_clause")
        if import_clause:
            named_imports = self._find_child_by_type(import_clause, "named_imports")
            if named_imports:
                for spec in named_imports.named_children:
                    if spec.type == "import_specifier":
                        ident = self._find_child_by_type(spec, "identifier")
                        if ident:
                            imported.append(self._node_text(ident, source))

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
        """提取原始调用关系

        与 _walk_symbols 保持一致的类处理逻辑：
        - function_declaration: qualified = module_path.name
        - class_declaration: 递归进类体，current_qualified 传递类名
        - method_definition: qualified = current_qualified.name

        输出字段：
        - caller_name: 简名（如 foo），与 symbols.name 对应
        - caller_qualified: 完整限定名（如 module.Class.foo），
          与 symbols.qualified_name 对应，用于精确匹配
        """
        calls = []

        def make_qualified(name: str, parent_qualified: str) -> str:
            if parent_qualified:
                return f"{parent_qualified}.{name}"
            if module_path:
                return f"{module_path}.{name}"
            return name

        def walk(node, current_fn: str = "", current_qualified: str = ""):
            for child in node.named_children:
                if child.type == "function_declaration":
                    name_node = self._find_child_by_type(child, "identifier")
                    fn_name = self._node_text(name_node, source) if name_node else ""
                    qual = make_qualified(fn_name, current_qualified)
                    walk(child, fn_name, qual)
                elif child.type == "class_declaration":
                    name_node = self._find_child_by_type(child, "type_identifier")
                    cls_name = self._node_text(name_node, source) if name_node else ""
                    qual = make_qualified(cls_name, current_qualified)
                    walk(child, cls_name, qual)
                elif child.type == "method_definition":
                    name_node = self._find_child_by_type(child, "property_identifier")
                    fn_name = self._node_text(name_node, source) if name_node else ""
                    qual = make_qualified(fn_name, current_qualified)
                    walk(child, fn_name, qual)
                elif child.type == "call_expression":
                    call_info = self._parse_call_expression(child, source)
                    if call_info and current_fn:
                        call_info["caller_name"] = current_fn
                        call_info["caller_qualified"] = current_qualified
                        call_info["caller_module"] = module_path
                        calls.append(call_info)
                    walk(child, current_fn, current_qualified)
                else:
                    walk(child, current_fn, current_qualified)

        walk(root)
        return calls

    def _parse_call_expression(self, node, source: bytes) -> Dict[str, Any]:
        """解析调用表达式"""
        func_node = node.child_by_field_name("function")
        if not func_node:
            return None

        callee_name = self._node_text(func_node, source)
        callee_module = ""

        # 处理 member_expression（如 obj.method()）
        if func_node.type == "member_expression":
            prop = self._find_child_by_type(func_node, "property_identifier")
            obj = self._find_child_by_type(func_node, "identifier")
            if prop:
                callee_name = self._node_text(prop, source)
            if obj:
                callee_module = self._node_text(obj, source)

        return {
            "callee_name": callee_name,
            "callee_module": callee_module,
            "call_line": node.start_point[0] + 1,
        }

    # --------------------------------------------------------------------
    # 模块级注释
    # --------------------------------------------------------------------

    def _has_module_comment(self, root, source: bytes) -> bool:
        """检测是否有文件级注释（第一个子节点是注释）"""
        if not root.children:
            return False
        first = root.children[0]
        if first.type == "comment":
            text = self._node_text(first, source).strip()
            # JSDoc 注释或多行注释视为模块级注释
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
        while prev and prev.type == "comment":
            text = self._node_text(prev, source).strip()
            comment_parts.insert(0, text)
            prev = prev.prev_named_sibling

        # 如果在 export 语句里，检查 export 的前一个兄弟
        if not comment_parts and node.parent and node.parent.type == "export_statement":
            prev = node.parent.prev_named_sibling
            while prev and prev.type == "comment":
                text = self._node_text(prev, source).strip()
                comment_parts.insert(0, text)
                prev = prev.prev_named_sibling

        return "\n".join(comment_parts) if comment_parts else ""

    def _detect_visibility(self, node, source: bytes) -> str:
        """检测可见性"""
        if node.parent and node.parent.type == "export_statement":
            return "public"
        return "private"

    def _detect_method_visibility(self, node, source: bytes) -> str:
        """检测方法可见性"""
        for child in node.named_children:
            if child.type == "accessibility_modifier":
                text = self._node_text(child, source)
                return text  # public/private/protected
        return "public"  # TypeScript 方法默认 public

    def _extract_signature(self, node, source: bytes) -> str:
        """提取函数签名"""
        name_node = self._find_child_by_type(node, "identifier")
        params_node = self._find_child_by_type(node, "formal_parameters")
        return_type = self._find_child_by_type(node, "type_annotation")

        name = self._node_text(name_node, source) if name_node else ""
        params = self._node_text(params_node, source) if params_node else "()"
        ret = self._node_text(return_type, source) if return_type else ""
        return f"function {name}{params}{ret}"

    def _extract_method_signature(self, node, source: bytes) -> str:
        """提取方法签名"""
        name_node = self._find_child_by_type(node, "property_identifier")
        params_node = self._find_child_by_type(node, "formal_parameters")
        return_type = self._find_child_by_type(node, "type_annotation")

        name = self._node_text(name_node, source) if name_node else ""
        params = self._node_text(params_node, source) if params_node else "()"
        ret = self._node_text(return_type, source) if return_type else ""
        return f"{name}{params}{ret}"
