"""
c_parser.py
===========

基于 tree-sitter 的 C/C++ 源码解析器。

提取函数、结构体、类、枚举等符号，以及 include 语句和调用关系。
支持 C 和 C++ 两种语言。
"""

from __future__ import annotations

from typing import Any, Dict, List

import tree_sitter_c as tsc
import tree_sitter_cpp as tscpp

from .base import BaseParser


class CParser(BaseParser):
    """C 源码解析器"""

    language_id = "c"
    language_module = tsc

    def __init__(self):
        super().__init__()

    # --------------------------------------------------------------------
    # 符号提取
    # --------------------------------------------------------------------

    def _extract_symbols(self, root, source: bytes, source_str: str,
                         module_path: str) -> List[Dict[str, Any]]:
        """提取符号：函数、结构体、枚举、联合体"""
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

            elif kind == "struct_specifier":
                sym = self._parse_struct(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)
                    body = self._find_child_by_type(child, "field_declaration_list")
                    if body:
                        self._walk_struct_body(body, source, source_str, module_path,
                                             symbols, sym["qualified_name"])

            elif kind == "enum_specifier":
                sym = self._parse_enum(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "union_specifier":
                sym = self._parse_union(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "type_definition":
                # typedef 声明，提取其中的 struct/enum/union
                struct_node = self._find_child_by_type(child, "struct_specifier")
                enum_node = self._find_child_by_type(child, "enum_specifier")
                union_node = self._find_child_by_type(child, "union_specifier")
                type_name_node = None
                # 最后一个 type_identifier 是 typedef 的名称
                type_idents = [c for c in child.named_children if c.type == "type_identifier"]
                if type_idents:
                    type_name_node = type_idents[-1]

                if struct_node and type_name_node:
                    name = self._node_text(type_name_node, source)
                    sym = self._parse_struct_with_name(
                        struct_node, source, source_str, module_path, parent_qualified, name
                    )
                    if sym:
                        symbols.append(sym)
                        body = self._find_child_by_type(struct_node, "field_declaration_list")
                        if body:
                            self._walk_struct_body(body, source, source_str, module_path,
                                                 symbols, sym["qualified_name"])
                elif enum_node and type_name_node:
                    name = self._node_text(type_name_node, source)
                    sym = self._parse_enum_with_name(
                        enum_node, source, source_str, module_path, parent_qualified, name
                    )
                    if sym:
                        symbols.append(sym)
                elif union_node and type_name_node:
                    name = self._node_text(type_name_node, source)
                    sym = self._parse_union_with_name(
                        union_node, source, source_str, module_path, parent_qualified, name
                    )
                    if sym:
                        symbols.append(sym)
                else:
                    # 其他 typedef 情况，递归处理
                    self._walk_symbols(child, source, source_str, module_path,
                                     symbols, parent_qualified)

            elif kind == "declaration_list" or kind == "field_declaration_list":
                self._walk_symbols(child, source, source_str, module_path,
                                  symbols, parent_qualified)

            elif kind == "function_declarator":
                pass

    def _walk_struct_body(self, node, source: bytes, source_str: str,
                          module_path: str, symbols: List[Dict[str, Any]],
                          parent_qualified: str):
        """遍历结构体字段"""
        for child in node.named_children:
            if child.type == "field_declaration":
                pass  # 字段暂不记录为符号
            elif child.type in ("struct_specifier", "enum_specifier", "union_specifier"):
                self._walk_symbols(child, source, source_str, module_path,
                                 symbols, parent_qualified)

    def _parse_function(self, node, source: bytes, source_str: str,
                        module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析函数定义"""
        declarator = self._find_child_by_type(node, "function_declarator")
        if not declarator:
            return None

        name_node = self._find_child_by_type(declarator, "identifier")
        if not name_node:
            name_node = self._find_child_by_type(declarator, "field_identifier")
        if not name_node:
            # 可能是带指针的声明符
            pointer_declarator = self._find_child_by_type(declarator, "pointer_declarator")
            if pointer_declarator:
                name_node = self._find_child_by_type(pointer_declarator, "identifier")
                if not name_node:
                    name_node = self._find_child_by_type(pointer_declarator, "field_identifier")
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
        signature = self._extract_function_signature(node, declarator, source, name)

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

    def _parse_struct_with_name(self, node, source: bytes, source_str: str,
                                 module_path: str, parent_qualified: str,
                                 name: str) -> Dict[str, Any]:
        """解析结构体（使用外部给定的名称，用于 typedef struct 的情况）"""
        if parent_qualified:
            qualified = f"{parent_qualified}.{name}"
        elif module_path:
            qualified = f"{module_path}.{name}"
        else:
            qualified = name

        comment = self._find_prev_comment(node.parent if node.parent else node, source)
        if not comment:
            comment = self._find_prev_comment(node, source)
        has_comment = bool(comment)
        content = self._node_text(node, source)

        return {
            "name": name,
            "kind": "struct",
            "visibility": self._detect_visibility(node, source),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"struct {name}",
            "has_comment": 1 if has_comment else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_enum_with_name(self, node, source: bytes, source_str: str,
                               module_path: str, parent_qualified: str,
                               name: str) -> Dict[str, Any]:
        """解析枚举（使用外部给定的名称）"""
        if parent_qualified:
            qualified = f"{parent_qualified}.{name}"
        elif module_path:
            qualified = f"{module_path}.{name}"
        else:
            qualified = name

        comment = self._find_prev_comment(node.parent if node.parent else node, source)
        if not comment:
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

    def _parse_union_with_name(self, node, source: bytes, source_str: str,
                                module_path: str, parent_qualified: str,
                                name: str) -> Dict[str, Any]:
        """解析联合体（使用外部给定的名称）"""
        if parent_qualified:
            qualified = f"{parent_qualified}.{name}"
        elif module_path:
            qualified = f"{module_path}.{name}"
        else:
            qualified = name

        comment = self._find_prev_comment(node.parent if node.parent else node, source)
        if not comment:
            comment = self._find_prev_comment(node, source)
        has_comment = bool(comment)
        content = self._node_text(node, source)

        return {
            "name": name,
            "kind": "union",
            "visibility": self._detect_visibility(node, source),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"union {name}",
            "has_comment": 1 if has_comment else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_struct(self, node, source: bytes, source_str: str,
                      module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析结构体"""
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
        has_comment = bool(comment)
        content = self._node_text(node, source)

        return {
            "name": name,
            "kind": "struct",
            "visibility": self._detect_visibility(node, source),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"struct {name}",
            "has_comment": 1 if has_comment else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_enum(self, node, source: bytes, source_str: str,
                    module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析枚举"""
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

    def _parse_union(self, node, source: bytes, source_str: str,
                     module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析联合体"""
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
        has_comment = bool(comment)
        content = self._node_text(node, source)

        return {
            "name": name,
            "kind": "union",
            "visibility": self._detect_visibility(node, source),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"union {name}",
            "has_comment": 1 if has_comment else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    # --------------------------------------------------------------------
    # include 提取
    # --------------------------------------------------------------------

    def _extract_imports(self, root, source: bytes) -> List[Dict[str, Any]]:
        """提取 #include 语句"""
        imports = []

        def walk(node):
            for child in node.named_children:
                if child.type == "preproc_include":
                    imp = self._parse_include(child, source)
                    if imp:
                        imports.append(imp)
                walk(child)

        walk(root)
        return imports

    def _parse_include(self, node, source: bytes) -> Dict[str, Any]:
        """解析单个 #include"""
        path_node = self._find_child_by_type(node, "system_lib_string")
        if not path_node:
            path_node = self._find_child_by_type(node, "string_literal")

        include_path = self._node_text(path_node, source).strip('"').strip("<>") if path_node else ""

        return {
            "module": include_path,
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

        def walk(node, current_fn: str = "", current_qualified: str = ""):
            for child in node.named_children:
                if child.type == "function_definition":
                    declarator = self._find_child_by_type(child, "function_declarator")
                    name_node = None
                    if declarator:
                        name_node = self._find_child_by_type(declarator, "identifier")
                        if not name_node:
                            name_node = self._find_child_by_type(declarator, "field_identifier")
                        if not name_node:
                            pointer_declarator = self._find_child_by_type(declarator, "pointer_declarator")
                            if pointer_declarator:
                                name_node = self._find_child_by_type(pointer_declarator, "identifier")
                                if not name_node:
                                    name_node = self._find_child_by_type(pointer_declarator, "field_identifier")
                    fn_name = self._node_text(name_node, source) if name_node else ""
                    qual = f"{module_path}.{fn_name}" if module_path else fn_name
                    walk(child, fn_name, qual)
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
        """解析调用表达式"""
        callee = node.child_by_field_name("function")
        if not callee:
            if node.children:
                callee = node.children[0]
            else:
                return None

        callee_name = ""
        callee_module = ""

        if callee.type == "field_expression":
            arg = self._find_child_by_type(callee, "identifier")
            callee_name = self._node_text(arg, source) if arg else ""
            operand = callee.child_by_field_name("operand")
            if operand:
                callee_module = self._node_text(operand, source)
        elif callee.type == "identifier":
            callee_name = self._node_text(callee, source)
        elif callee.type == "pointer_declarator":
            id_node = self._find_child_by_type(callee, "identifier")
            callee_name = self._node_text(id_node, source) if id_node else ""
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
        """检测是否有文件级注释"""
        if not root.children:
            return False
        for child in root.children:
            if child.type in ("comment", "block_comment", "line_comment"):
                text = self._node_text(child, source).strip()
                if len(text) > 50 or text.startswith("/*"):
                    return True
            elif child.type == "preproc_include":
                continue
            else:
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
        while prev and prev.type in ("comment", "block_comment", "line_comment"):
            text = self._node_text(prev, source).strip()
            comment_parts.insert(0, text)
            prev = prev.prev_named_sibling
        return "\n".join(comment_parts) if comment_parts else ""

    def _detect_visibility(self, node, source: bytes) -> str:
        """检测可见性（C 中默认都是 public）"""
        return "public"

    def _extract_function_signature(self, node, declarator, source: bytes, name: str) -> str:
        """提取函数签名"""
        params = self._find_child_by_type(declarator, "parameter_list")
        params_text = self._node_text(params, source) if params else "()"

        # 返回类型在 function_definition 的 declaration_specifiers 或 type 中
        return_type = ""
        for child in node.named_children:
            if child.type in ("declaration_specifiers", "type_specifier"):
                return_type = self._node_text(child, source)
                break

        return f"{return_type} {name}{params_text}".strip()


class CppParser(CParser):
    """C++ 源码解析器（继承 C 解析器，扩展 C++ 特性）"""

    language_id = "cpp"
    language_module = tscpp

    def __init__(self):
        BaseParser.__init__(self)

    def _extract_symbols(self, root, source: bytes, source_str: str,
                         module_path: str) -> List[Dict[str, Any]]:
        """提取符号：函数、类、结构体、枚举、命名空间"""
        symbols = []
        self._walk_symbols_cpp(root, source, source_str, module_path, symbols, "")
        return symbols

    def _walk_symbols_cpp(self, node, source: bytes, source_str: str,
                           module_path: str, symbols: List[Dict[str, Any]],
                           parent_qualified: str):
        """递归遍历提取 C++ 符号"""
        for child in node.named_children:
            kind = child.type

            if kind == "function_definition":
                sym = self._parse_function(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "struct_specifier":
                sym = self._parse_struct(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "class_specifier":
                sym = self._parse_class(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)
                    body = self._find_child_by_type(child, "field_declaration_list")
                    if body:
                        self._walk_class_body(body, source, source_str, module_path,
                                           symbols, sym["qualified_name"])

            elif kind == "enum_specifier":
                sym = self._parse_enum(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "namespace_definition":
                ns_name = self._extract_namespace_name(child, source)
                ns_qualified = f"{parent_qualified}.{ns_name}" if parent_qualified else ns_name
                ns_module = f"{module_path}.{ns_name}" if module_path else ns_name
                body = self._find_child_by_type(child, "declaration_list")
                if body:
                    self._walk_symbols_cpp(body, source, source_str, ns_module,
                                         symbols, ns_qualified)

            elif kind == "declaration_list" or kind == "field_declaration_list":
                self._walk_symbols_cpp(child, source, source_str, module_path,
                                     symbols, parent_qualified)

    def _walk_class_body(self, node, source: bytes, source_str: str,
                         module_path: str, symbols: List[Dict[str, Any]],
                         parent_qualified: str):
        """遍历类体提取方法和嵌套类"""
        for child in node.named_children:
            if child.type in ("access_specifier", "public", "private", "protected"):
                continue
            if child.type == "function_definition":
                sym = self._parse_method(child, source, source_str, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)
            elif child.type == "declaration":
                # 可能包含 function_declarator
                for c in child.named_children:
                    if c.type == "function_declarator":
                        pass  # 声明不处理
            elif child.type in ("struct_specifier", "class_specifier", "enum_specifier"):
                self._walk_symbols_cpp(child, source, source_str, module_path,
                                     symbols, parent_qualified)

    def _parse_class(self, node, source: bytes, source_str: str,
                     module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析类声明"""
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
        has_comment = bool(comment)
        content = self._node_text(node, source)

        return {
            "name": name,
            "kind": "class",
            "visibility": self._detect_visibility_cpp(node, source),
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

    def _parse_method(self, node, source: bytes, source_str: str,
                      module_path: str, parent_qualified: str) -> Dict[str, Any]:
        """解析类方法（C++ 特有）"""
        result = self._parse_function(node, source, source_str, module_path, parent_qualified)
        if result:
            result["kind"] = "method"
        return result

    def _extract_namespace_name(self, node, source: bytes) -> str:
        """提取命名空间名称"""
        name_node = self._find_child_by_type(node, "namespace_identifier")
        if name_node:
            return self._node_text(name_node, source)
        return ""

    def _extract_raw_calls(self, root, source: bytes,
                           module_path: str) -> List[Dict[str, Any]]:
        """提取原始调用关系（C++ 扩展版）"""
        calls = []

        def walk(node, current_fn: str = "", current_qualified: str = "",
                 current_scope: str = ""):
            for child in node.named_children:
                if child.type == "namespace_definition":
                    ns_name = self._extract_namespace_name(child, source)
                    new_scope = f"{current_scope}.{ns_name}" if current_scope else ns_name
                    walk(child, current_fn, current_qualified, new_scope)
                elif child.type in ("class_specifier", "struct_specifier"):
                    name_node = self._find_child_by_type(child, "type_identifier")
                    if name_node:
                        cls_name = self._node_text(name_node, source)
                        new_scope = f"{current_scope}.{cls_name}" if current_scope else cls_name
                        body = self._find_child_by_type(child, "field_declaration_list")
                        if body:
                            walk(body, current_fn, current_qualified, new_scope)
                elif child.type == "function_definition":
                    declarator = self._find_child_by_type(child, "function_declarator")
                    name_node = None
                    if declarator:
                        name_node = self._find_child_by_type(declarator, "identifier")
                        if not name_node:
                            name_node = self._find_child_by_type(declarator, "field_identifier")
                        if not name_node:
                            pointer_declarator = self._find_child_by_type(declarator, "pointer_declarator")
                            if pointer_declarator:
                                name_node = self._find_child_by_type(pointer_declarator, "identifier")
                                if not name_node:
                                    name_node = self._find_child_by_type(pointer_declarator, "field_identifier")
                    fn_name = self._node_text(name_node, source) if name_node else ""
                    if current_scope:
                        qual = f"{module_path}.{current_scope}.{fn_name}" if module_path else f"{current_scope}.{fn_name}"
                    else:
                        qual = f"{module_path}.{fn_name}" if module_path else fn_name
                    walk(child, fn_name, qual, current_scope)
                elif child.type == "call_expression":
                    call_info = self._parse_call_cpp(child, source)
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

    def _parse_call_cpp(self, node, source: bytes) -> Dict[str, Any]:
        """解析 C++ 调用表达式"""
        callee = node.child_by_field_name("function")
        if not callee:
            if node.children:
                callee = node.children[0]
            else:
                return None

        callee_name = ""
        callee_module = ""

        if callee.type == "field_expression":
            arg = self._find_child_by_type(callee, "identifier")
            callee_name = self._node_text(arg, source) if arg else ""
            operand = callee.child_by_field_name("operand")
            if operand:
                callee_module = self._node_text(operand, source)
        elif callee.type == "qualified_identifier":
            scope = self._find_child_by_type(callee, "namespace_identifier")
            name = self._find_child_by_type(callee, "identifier")
            callee_name = self._node_text(name, source) if name else ""
            callee_module = self._node_text(scope, source) if scope else ""
        elif callee.type == "identifier":
            callee_name = self._node_text(callee, source)
        else:
            callee_name = self._node_text(callee, source)

        return {
            "callee_name": callee_name,
            "callee_module": callee_module,
            "call_line": node.start_point[0] + 1,
        }

    def _detect_visibility_cpp(self, node, source: bytes) -> str:
        """检测 C++ 类的可见性"""
        return "public"
