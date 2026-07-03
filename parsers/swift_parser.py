"""
swift_parser.py
===============

基于 tree-sitter 的 Swift 源码解析器。

提取类、结构体、协议、枚举、方法、属性等符号，以及 import 语句和调用关系。
语法节点参考 tree-sitter-swift 官方 grammar。

安装：pip install tree-sitter-swift
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import tree_sitter_swift as tsswift

from .base import BaseParser


class SwiftParser(BaseParser):
    """Swift 源码解析器"""

    language_id = "swift"
    language_module = tsswift

    # ------------------------------------------------------------------
    # 符号提取
    # ------------------------------------------------------------------

    def _extract_symbols(self, root, source: bytes, source_str: str,
                         module_path: str) -> List[Dict[str, Any]]:
        """提取符号：类、结构体、协议、枚举、方法、属性"""
        symbols: List[Dict[str, Any]] = []
        self._walk_declarations(root, source, source_str, module_path,
                                symbols, "")
        return symbols

    def _walk_declarations(self, node, source: bytes, source_str: str,
                           module_path: str, symbols: List[Dict[str, Any]],
                           parent_qualified: str):
        """递归遍历声明节点"""
        for child in node.named_children:
            kind = child.type

            if kind in ("class_declaration", "struct_declaration",
                        "protocol_declaration", "enum_declaration",
                        "actor_declaration"):
                type_kind = kind.replace("_declaration", "")
                self._process_type_declaration(child, source, source_str,
                                               type_kind, module_path,
                                               symbols, parent_qualified)

            elif kind == "function_declaration":
                sym = self._parse_function(child, source, module_path, parent_qualified, "function")
                if sym:
                    symbols.append(sym)

            elif kind == "init_declaration":
                sym = self._parse_init(child, source, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "protocol_function_declaration":
                # 协议内的方法声明（无方法体）
                sym = self._parse_function(child, source, module_path, parent_qualified, "function")
                if sym:
                    symbols.append(sym)

            elif kind in ("class_body", "struct_body", "protocol_body",
                          "enum_body", "actor_body"):
                self._walk_declarations(child, source, source_str, module_path,
                                        symbols, parent_qualified)

    def _process_type_declaration(self, node, source: bytes, source_str: str,
                                  kind: str, module_path: str,
                                  symbols: List[Dict[str, Any]],
                                  parent_qualified: str):
        """处理类型声明"""
        sym = self._parse_type_decl(node, source, kind, module_path, parent_qualified)
        if sym:
            symbols.append(sym)
            # 找到对应的 body 节点递归
            body_kind = f"{kind}_body"
            body = self._find_child_by_type(node, body_kind)
            if body:
                self._walk_declarations(body, source, source_str, module_path,
                                        symbols, sym["qualified_name"])

    def _parse_type_decl(self, node, source: bytes, kind: str,
                         module_path: str, parent_qualified: str) -> Optional[Dict[str, Any]]:
        """解析类/结构体/协议/枚举声明

        类型名是 type_identifier 节点的文本。
        """
        name_node = self._find_child_by_type(node, "type_identifier")
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
        content = self._node_text(node, source)

        return {
            "name": name,
            "kind": kind,
            "visibility": self._detect_visibility(node, source),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"{kind} {name}",
            "has_comment": 1 if bool(comment) else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_function(self, node, source: bytes,
                        module_path: str, parent_qualified: str,
                        kind: str = "function") -> Optional[Dict[str, Any]]:
        """解析函数声明

        函数名是 simple_identifier 节点（func 关键字后的第一个 simple_identifier）。
        """
        name_node = self._find_child_by_type(node, "simple_identifier")
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
        content = self._node_text(node, source)

        return {
            "name": name,
            "kind": kind,
            "visibility": self._detect_visibility(node, source),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"func {name}",
            "has_comment": 1 if bool(comment) else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_init(self, node, source: bytes,
                    module_path: str, parent_qualified: str) -> Optional[Dict[str, Any]]:
        """解析 init 声明（构造方法）"""
        name = "init"
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
            "kind": "constructor",
            "visibility": self._detect_visibility(node, source),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": "init",
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
        """提取 import 语句"""
        imports: List[Dict[str, Any]] = []

        def walk(node):
            for child in node.named_children:
                if child.type == "import_declaration":
                    # import_declaration 内有 identifier
                    ident = self._find_child_by_type(child, "identifier")
                    module_name = self._node_text(ident, source) if ident else ""
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
        """提取原始调用关系

        遍历方法体内所有 call_expression。
        """
        calls: List[Dict[str, Any]] = []

        def walk(node, current_fn: str = "", current_qualified: str = ""):
            for child in node.named_children:
                if child.type == "function_declaration":
                    name_node = self._find_child_by_type(child, "simple_identifier")
                    fn_name = self._node_text(name_node, source) if name_node else ""
                    if current_qualified:
                        qual = f"{current_qualified}.{fn_name}"
                    elif module_path:
                        qual = f"{module_path}.{fn_name}"
                    else:
                        qual = fn_name
                    walk(child, fn_name, qual)
                elif child.type == "init_declaration":
                    fn_name = "init"
                    if current_qualified:
                        qual = f"{current_qualified}.{fn_name}"
                    elif module_path:
                        qual = f"{module_path}.{fn_name}"
                    else:
                        qual = fn_name
                    walk(child, fn_name, qual)
                elif child.type in ("class_declaration", "struct_declaration",
                                    "protocol_declaration", "enum_declaration",
                                    "actor_declaration"):
                    name_node = self._find_child_by_type(child, "type_identifier")
                    type_name = self._node_text(name_node, source) if name_node else ""
                    if type_name:
                        if current_qualified:
                            type_qual = f"{current_qualified}.{type_name}"
                        elif module_path:
                            type_qual = f"{module_path}.{type_name}"
                        else:
                            type_qual = type_name
                        walk(child, current_fn, type_qual)
                    else:
                        walk(child, current_fn, current_qualified)
                elif child.type == "call_expression":
                    call_info = self._parse_call(child, source)
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

    def _parse_call(self, node, source: bytes) -> Dict[str, Any]:
        """解析调用表达式

        Swift 的 call_expression 形如 func(args) 或 obj.method(args)。
        - 含 navigation_expression 时：callee_name=方法名，callee_module=接收者
        - 否则：callee_name=直接函数名
        """
        callee_name = ""
        callee_module = ""

        nav = self._find_child_by_type(node, "navigation_expression")
        if nav:
            # 形如 obj.method()
            ident = self._find_child_by_type(nav, "simple_identifier")
            if ident:
                callee_name = self._node_text(ident, source)
            callee_module = self._node_text(nav, source).split(".")[0]
        else:
            # 形如 func()
            ident = self._find_child_by_type(node, "simple_identifier")
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
        """检测是否有文件级注释（/// 或 /* */）"""
        for child in root.children:
            if child.type in ("comment", "line_comment", "block_comment"):
                text = self._node_text(child, source).strip()
                if text.startswith("///") or text.startswith("/*"):
                    return True
            elif child.type == "import_declaration":
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

    def _detect_visibility(self, node, source: bytes) -> str:
        """检测可见性修饰符

        Swift 用 modifiers 节点包裹 visibility_modifier（public/private/fileprivate/internal 等）。
        默认 internal（同模块可见），这里简化为 public/private。
        """
        modifiers = self._find_child_by_type(node, "modifiers")
        if modifiers:
            text = self._node_text(modifiers, source)
            if "public" in text or "open" in text:
                return "public"
            elif "private" in text or "fileprivate" in text:
                return "private"
            elif "internal" in text:
                return "internal"
        return "internal"  # Swift 默认 internal
