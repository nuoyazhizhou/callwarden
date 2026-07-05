"""
php_parser.py
=============

基于 tree-sitter 的 PHP 源码解析器。

提取类、接口、Trait、方法、属性等符号，以及 namespace / use 语句和调用关系。
语法节点参考 tree-sitter-php 官方 grammar。

安装：pip install tree-sitter-php
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import tree_sitter_php as tsphp
from tree_sitter import Language, Parser

from .base import BaseParser


class PhpParser(BaseParser):
    """PHP 源码解析器

    基于 tree-sitter-php 解析 PHP 源码，提取类、接口、Trait、方法、属性等符号，
    以及 namespace / use 语句和调用关系。

    主要属性：
        language_id: 语言标识，固定为 "php"。
        language_module: tree-sitter-php 模块，提供 language_php 入口。
        language: tree-sitter Language 实例，用于初始化 parser。
        parser: tree-sitter Parser 实例，负责 PHP 源码解析。
    """

    language_id = "php"
    # tree-sitter-php 使用 language_php() 而非 language()
    language_module = tsphp

    def __init__(self):
        """初始化 PHP 解析器。

        tree-sitter-php 的语言入口是 language_php() 而非 language()，
        因此需要在此处单独构造 Language 与 Parser 实例。

        Raises:
            Exception: 当 tree-sitter-php 模块加载失败时抛出。
        """
        # tree-sitter-php 的入口是 language_php，不是 language
        self.language = Language(tsphp.language_php())
        self.parser = Parser(self.language)

    # ------------------------------------------------------------------
    # 符号提取
    # ------------------------------------------------------------------

    def _extract_symbols(self, root, source: bytes, source_str: str,
                         module_path: str) -> List[Dict[str, Any]]:
        """提取符号：类、接口、Trait、方法、属性"""
        symbols: List[Dict[str, Any]] = []
        namespace = self._extract_namespace(root, source)
        effective_module = namespace or module_path
        self._walk_declarations(root, source, source_str, effective_module,
                                symbols, "")
        return symbols

    def _extract_namespace(self, root, source: bytes) -> str:
        """提取命名空间名"""
        for child in root.named_children:
            if child.type == "namespace_definition":
                name_node = self._find_child_by_type(child, "namespace_name")
                if name_node:
                    return self._node_text(name_node, source).replace("\\", ".")
        return ""

    def _walk_declarations(self, node, source: bytes, source_str: str,
                           module_path: str, symbols: List[Dict[str, Any]],
                           parent_qualified: str):
        """递归遍历声明节点

        处理 namespace / class / interface / trait / method / property。
        """
        for child in node.named_children:
            kind = child.type

            if kind == "namespace_definition":
                # 递归进入命名空间体（命名空间本身不产生符号）
                self._walk_declarations(child, source, source_str, module_path,
                                        symbols, parent_qualified)

            elif kind in ("class_declaration", "interface_declaration",
                          "trait_declaration"):
                type_kind = kind.replace("_declaration", "")
                self._process_type_declaration(child, source, source_str,
                                               type_kind, module_path,
                                               symbols, parent_qualified)

            elif kind == "method_declaration":
                sym = self._parse_method(child, source, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "property_declaration":
                sym = self._parse_property(child, source, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "declaration_list":
                # 类/接口/Trait 体，递归遍历成员
                self._walk_declarations(child, source, source_str, module_path,
                                        symbols, parent_qualified)

    def _process_type_declaration(self, node, source: bytes, source_str: str,
                                  kind: str, module_path: str,
                                  symbols: List[Dict[str, Any]],
                                  parent_qualified: str):
        """处理类型声明（class/interface/trait）"""
        sym = self._parse_class_like(node, source, kind, module_path, parent_qualified)
        if sym:
            symbols.append(sym)
            body = self._find_child_by_type(node, "declaration_list")
            if body:
                self._walk_declarations(body, source, source_str, module_path,
                                        symbols, sym["qualified_name"])

    def _parse_class_like(self, node, source: bytes, kind: str,
                          module_path: str, parent_qualified: str) -> Optional[Dict[str, Any]]:
        """解析类/接口/Trait 声明

        类型名是 class/interface/trait 关键字后的第一个 name 节点。
        """
        name_node = self._find_child_by_type(node, "name")
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

    def _parse_method(self, node, source: bytes,
                      module_path: str, parent_qualified: str) -> Optional[Dict[str, Any]]:
        """解析方法声明

        方法名是 formal_parameters 紧前的 name 节点。
        """
        name_node = self._find_name_before(node, source, "formal_parameters")
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
        params_node = self._find_child_by_type(node, "formal_parameters")
        params = self._node_text(params_node, source) if params_node else "()"

        return {
            "name": name,
            "kind": "method",
            "visibility": self._detect_visibility(node, source),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"function {name}{params}",
            "has_comment": 1 if bool(comment) else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_property(self, node, source: bytes,
                        module_path: str, parent_qualified: str) -> Optional[Dict[str, Any]]:
        """解析属性声明

        property_declaration 内含 property_element -> variable_name -> name。
        """
        var_node = self._find_child_by_type(node, "variable_name")
        if not var_node:
            # property_element 包裹的情况
            prop_elem = self._find_child_by_type(node, "property_element")
            if prop_elem:
                var_node = self._find_child_by_type(prop_elem, "variable_name")
        if not var_node:
            return None

        name_node = self._find_child_by_type(var_node, "name")
        if not name_node:
            return None

        name = self._node_text(name_node, source)
        # PHP 属性名带 $ 前缀，移除以保持符号名整洁
        name = name.lstrip("$")

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
            "kind": "property",
            "visibility": self._detect_visibility(node, source),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"property ${name}",
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
        """提取 use 语句"""
        imports: List[Dict[str, Any]] = []

        def walk(node):
            """递归遍历 AST，收集所有 namespace_use_declaration 中的 use 语句。

            Args:
                node: 当前遍历的 tree-sitter 节点。
            """
            for child in node.named_children:
                if child.type == "namespace_use_declaration":
                    # namespace_use_declaration 内有多个 namespace_use_clause
                    for clause in child.named_children:
                        if clause.type == "namespace_use_clause":
                            qn = self._find_child_by_type(clause, "qualified_name")
                            if qn:
                                module_name = self._node_text(qn, source)
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

        遍历方法体内所有 member_call_expression / function_call_expression。
        """
        calls: List[Dict[str, Any]] = []
        namespace = self._extract_namespace(root, source)
        effective_module = namespace or module_path

        def walk(node, current_fn: str = "", current_qualified: str = ""):
            """递归遍历 AST，识别类/接口/Trait 与方法定义并收集调用关系。

            Args:
                node: 当前遍历的 tree-sitter 节点。
                current_fn: 当前所在方法名，用于标注调用者。
                current_qualified: 当前所在符号的完整限定名，用于精确匹配。
            """
            for child in node.named_children:
                if child.type in ("class_declaration", "interface_declaration",
                                  "trait_declaration"):
                    name_node = self._find_child_by_type(child, "name")
                    class_name = self._node_text(name_node, source) if name_node else ""
                    if class_name:
                        class_qualified = f"{current_qualified}.{class_name}" if current_qualified else (
                            f"{effective_module}.{class_name}" if effective_module else class_name
                        )
                        body = self._find_child_by_type(child, "declaration_list")
                        if body:
                            walk(body, "", class_qualified)
                    walk(child, current_fn, current_qualified)
                elif child.type == "method_declaration":
                    name_node = self._find_name_before(child, source, "formal_parameters")
                    fn_name = self._node_text(name_node, source) if name_node else ""
                    method_qualified = f"{current_qualified}.{fn_name}" if current_qualified else (
                        f"{effective_module}.{fn_name}" if effective_module else fn_name
                    )
                    walk(child, fn_name, method_qualified)
                elif child.type == "member_call_expression":
                    call_info = self._parse_member_call(child, source)
                    if call_info and current_fn:
                        call_info["caller_name"] = current_fn
                        call_info["caller_qualified"] = current_qualified
                        call_info["caller_module"] = effective_module
                        calls.append(call_info)
                    walk(child, current_fn, current_qualified)
                elif child.type == "function_call_expression":
                    call_info = self._parse_function_call(child, source)
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

    def _parse_member_call(self, node, source: bytes) -> Dict[str, Any]:
        """解析成员调用 $obj->method()"""
        callee_name = ""
        callee_module = ""

        # member_call_expression = member_access_expression + name + arguments
        # name 节点是被调用的方法名
        name_node = self._find_child_by_type(node, "name")
        if name_node:
            callee_name = self._node_text(name_node, source)

        # member_access_expression 是接收者（如 $this、$obj）
        member_access = self._find_child_by_type(node, "member_access_expression")
        if member_access:
            var_node = self._find_child_by_type(member_access, "variable_name")
            if var_node:
                callee_module = self._node_text(var_node, source)

        return {
            "callee_name": callee_name,
            "callee_module": callee_module,
            "call_line": node.start_point[0] + 1,
        }

    def _parse_function_call(self, node, source: bytes) -> Dict[str, Any]:
        """解析函数调用 func() 或 Namespace\\func()"""
        callee_name = ""
        callee_module = ""

        # 检查是否有 qualified_name（如 \App\foo 或 Namespace\func）
        qname_node = self._find_child_by_type(node, "qualified_name")
        if qname_node:
            full_text = self._node_text(qname_node, source)
            # 去掉前导反斜杠
            full_text = full_text.lstrip("\\")
            parts = full_text.split("\\")
            if len(parts) > 1:
                callee_name = parts[-1]
                callee_module = "\\".join(parts[:-1])
            else:
                callee_name = parts[0]
        else:
            # function_call_expression = name + arguments
            name_node = self._find_child_by_type(node, "name")
            if name_node:
                callee_name = self._node_text(name_node, source)

        return {
            "callee_name": callee_name,
            "callee_module": callee_module,
            "call_line": node.start_point[0] + 1,
        }

    # ------------------------------------------------------------------
    # 模块级注释
    # ------------------------------------------------------------------

    def _has_module_comment(self, root, source: bytes) -> bool:
        """检测是否有文件级注释（PHPDoc /** */ 或 // #）"""
        for child in root.children:
            if child.type in ("comment", "block_comment", "line_comment"):
                text = self._node_text(child, source).strip()
                if text.startswith("/**") or text.startswith("//") or text.startswith("#"):
                    return True
            elif child.type == "php_tag":
                continue
            elif child.type == "namespace_definition":
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

    def _find_name_before(self, node, source: bytes, terminator_type: str):
        """查找终止节点（formal_parameters）紧前的 name 节点"""
        children = node.named_children
        for i, child in enumerate(children):
            if child.type == terminator_type:
                if i > 0 and children[i - 1].type == "name":
                    return children[i - 1]
                break
        return None

    def _find_prev_comment(self, node, source: bytes) -> str:
        """查找节点前的注释（PHPDoc 或行注释）"""
        comment_parts = []
        prev = node.prev_named_sibling
        while prev and prev.type in ("comment", "block_comment", "line_comment"):
            text = self._node_text(prev, source).strip()
            comment_parts.insert(0, text)
            prev = prev.prev_named_sibling
        return "\n".join(comment_parts) if comment_parts else ""

    def _detect_visibility(self, node, source: bytes) -> str:
        """检测可见性修饰符

        PHP 用 visibility_modifier 节点包裹 public/protected/private。
        """
        vis_mod = self._find_child_by_type(node, "visibility_modifier")
        if vis_mod:
            text = self._node_text(vis_mod, source)
            if "public" in text:
                return "public"
            elif "protected" in text:
                return "protected"
            elif "private" in text:
                return "private"
        return "public"  # PHP 默认 public
