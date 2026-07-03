"""
scala_parser.py
===============

基于 tree-sitter 的 Scala 源码解析器。

提取类、Trait、Object、方法等符号，以及 package / import 语句和调用关系。
语法节点参考 tree-sitter-scala 官方 grammar。

安装：pip install tree-sitter-scala
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import tree_sitter_scala as tsscala

from .base import BaseParser


class ScalaParser(BaseParser):
    """Scala 源码解析器"""

    language_id = "scala"
    language_module = tsscala

    # ------------------------------------------------------------------
    # 符号提取
    # ------------------------------------------------------------------

    def _extract_symbols(self, root, source: bytes, source_str: str,
                         module_path: str) -> List[Dict[str, Any]]:
        """提取符号：类、Trait、Object、方法"""
        symbols: List[Dict[str, Any]] = []
        package = self._extract_package(root, source)
        effective_module = package or module_path
        self._walk_declarations(root, source, source_str, effective_module,
                                symbols, "")
        return symbols

    def _extract_package(self, root, source: bytes) -> str:
        """提取包名"""
        for child in root.named_children:
            if child.type == "package_clause":
                pkg_id = self._find_child_by_type(child, "package_identifier")
                if pkg_id:
                    return self._node_text(pkg_id, source)
        return ""

    def _walk_declarations(self, node, source: bytes, source_str: str,
                           module_path: str, symbols: List[Dict[str, Any]],
                           parent_qualified: str):
        """递归遍历声明节点"""
        for child in node.named_children:
            kind = child.type

            if kind == "package_clause":
                # 递归进入包体
                self._walk_declarations(child, source, source_str, module_path,
                                        symbols, parent_qualified)

            elif kind in ("class_definition", "trait_definition",
                          "object_definition"):
                type_kind = kind.replace("_definition", "")
                self._process_type_declaration(child, source, source_str,
                                               type_kind, module_path,
                                               symbols, parent_qualified)

            elif kind == "function_definition":
                sym = self._parse_function(child, source, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "function_declaration":
                # 抽象方法声明（无方法体）
                sym = self._parse_function(child, source, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "template_body":
                # 类/Trait/Object 体，递归遍历成员
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
            body = self._find_child_by_type(node, "template_body")
            if body:
                self._walk_declarations(body, source, source_str, module_path,
                                        symbols, sym["qualified_name"])

    def _parse_type_decl(self, node, source: bytes, kind: str,
                         module_path: str, parent_qualified: str) -> Optional[Dict[str, Any]]:
        """解析类/Trait/Object 声明

        类型名是 type_definition 后的第一个 identifier 节点。
        """
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
        content = self._node_text(node, source)

        return {
            "name": name,
            "kind": kind,
            "visibility": "public",  # Scala 默认 public
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
                        module_path: str, parent_qualified: str) -> Optional[Dict[str, Any]]:
        """解析方法声明

        方法名是第一个 identifier 节点。
        """
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
        content = self._node_text(node, source)
        params_node = self._find_child_by_type(node, "parameters")
        params = self._node_text(params_node, source) if params_node else "()"

        return {
            "name": name,
            "kind": "method",
            "visibility": "public",
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

    # ------------------------------------------------------------------
    # import 提取
    # ------------------------------------------------------------------

    def _extract_imports(self, root, source: bytes) -> List[Dict[str, Any]]:
        """提取 import 语句"""
        imports: List[Dict[str, Any]] = []

        def walk(node):
            for child in node.named_children:
                if child.type == "import_declaration":
                    # import_declaration 内有多个 identifier 节点
                    idents = [c for c in child.named_children if c.type == "identifier"]
                    if idents:
                        # 第一个 identifier 是包名起始，最后一个可能是导入的类名
                        module_name = ".".join(self._node_text(i, source) for i in idents)
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
        package = self._extract_package(root, source)
        effective_module = package or module_path

        def walk(node, current_fn: str = "", current_qualified: str = ""):
            for child in node.named_children:
                if child.type in ("class_definition", "object_definition",
                                  "trait_definition"):
                    name_node = self._find_child_by_type(child, "identifier")
                    type_name = self._node_text(name_node, source) if name_node else ""
                    if type_name:
                        if current_qualified:
                            new_qualified = f"{current_qualified}.{type_name}"
                        elif effective_module:
                            new_qualified = f"{effective_module}.{type_name}"
                        else:
                            new_qualified = type_name
                        walk(child, current_fn, new_qualified)
                    else:
                        walk(child, current_fn, current_qualified)
                elif child.type == "function_definition":
                    name_node = self._find_child_by_type(child, "identifier")
                    fn_name = self._node_text(name_node, source) if name_node else ""
                    if fn_name:
                        if current_qualified:
                            new_qualified = f"{current_qualified}.{fn_name}"
                        elif effective_module:
                            new_qualified = f"{effective_module}.{fn_name}"
                        else:
                            new_qualified = fn_name
                        walk(child, fn_name, new_qualified)
                    else:
                        walk(child, current_fn, current_qualified)
                elif child.type == "call_expression":
                    call_info = self._parse_call(child, source)
                    if call_info and current_fn:
                        call_info["caller_name"] = current_fn
                        call_info["caller_qualified"] = current_qualified
                        call_info["caller_module"] = effective_module
                        calls.append(call_info)
                    walk(child, current_fn, current_qualified)
                else:
                    walk(child, current_fn, current_qualified)

        walk(root)
        return calls

    def _parse_call(self, node, source: bytes) -> Dict[str, Any]:
        """解析调用表达式

        Scala call_expression 形如 func(args) 或 obj.method(args)。
        - 含 field_expression 时：callee_name=方法名，callee_module=接收者
        - 否则：callee_name=直接函数名
        """
        callee_name = ""
        callee_module = ""

        field_expr = self._find_child_by_type(node, "field_expression")
        if field_expr:
            # 形如 obj.method(args)
            idents = [c for c in field_expr.named_children if c.type == "identifier"]
            if len(idents) >= 2:
                callee_module = self._node_text(idents[0], source)
                callee_name = self._node_text(idents[1], source)
        else:
            # 形如 func(args)
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
        """检测是否有文件级注释（Scaladoc /** */ 或 //）"""
        for child in root.children:
            if child.type in ("comment", "block_comment", "line_comment"):
                text = self._node_text(child, source).strip()
                if text.startswith("/**") or text.startswith("//"):
                    return True
            elif child.type == "package_clause":
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
        while prev and prev.type in ("comment", "block_comment", "line_comment"):
            text = self._node_text(prev, source).strip()
            comment_parts.insert(0, text)
            prev = prev.prev_named_sibling
        return "\n".join(comment_parts) if comment_parts else ""
