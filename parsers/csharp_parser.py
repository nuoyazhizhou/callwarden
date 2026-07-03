"""
csharp_parser.py
================

基于 tree-sitter 的 C# 源码解析器。

提取类、接口、结构体、枚举、方法、属性等符号，以及 using 语句和调用关系。
语法节点参考 tree-sitter-c-sharp 官方 grammar。

安装：pip install tree-sitter-c-sharp
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import tree_sitter_c_sharp as tscsharp

from .base import BaseParser


class CSharpParser(BaseParser):
    """C# 源码解析器"""

    language_id = "csharp"
    language_module = tscsharp

    # ------------------------------------------------------------------
    # 符号提取
    # ------------------------------------------------------------------

    def _extract_symbols(self, root, source: bytes, source_str: str,
                         module_path: str) -> List[Dict[str, Any]]:
        """提取符号：类、接口、结构体、枚举、方法、属性"""
        symbols: List[Dict[str, Any]] = []
        namespace = self._extract_namespace(root, source)
        effective_module = namespace or module_path
        # 从根节点开始递归遍历，处理命名空间、类、接口等顶层声明
        self._walk_declarations(root, source, source_str, effective_module,
                                symbols, "")
        return symbols

    def _extract_namespace(self, root, source: bytes) -> str:
        """提取命名空间名"""
        for child in root.named_children:
            if child.type == "namespace_declaration":
                name_node = self._find_child_by_type(child, "qualified_name")
                if name_node:
                    return self._node_text(name_node, source)
                ident = self._find_child_by_type(child, "identifier")
                if ident:
                    return self._node_text(ident, source)
        return ""

    def _walk_declarations(self, node, source: bytes, source_str: str,
                           module_path: str, symbols: List[Dict[str, Any]],
                           parent_qualified: str):
        """递归遍历声明节点

        处理命名空间、类、接口、结构体、枚举、方法、构造方法、属性。
        遇到 namespace_declaration 时递归进入其内部；
        遇到 class/interface/struct/enum 时添加符号并递归进入 declaration_list；
        遇到 declaration_list 时遍历其内成员。
        """
        for child in node.named_children:
            kind = child.type

            if kind == "namespace_declaration":
                # 递归进入命名空间内部（命名空间本身不产生符号）
                self._walk_declarations(child, source, source_str, module_path,
                                        symbols, parent_qualified)

            elif kind in ("class_declaration", "interface_declaration",
                          "struct_declaration", "enum_declaration"):
                # 处理类型声明：添加符号并递归进入类体
                type_kind = kind.replace("_declaration", "")
                self._process_type_declaration(child, source, source_str,
                                               type_kind, module_path,
                                               symbols, parent_qualified)

            elif kind == "method_declaration":
                sym = self._parse_method(child, source, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "constructor_declaration":
                sym = self._parse_constructor(child, source, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "property_declaration":
                sym = self._parse_property(child, source, module_path, parent_qualified)
                if sym:
                    symbols.append(sym)

            elif kind == "declaration_list":
                # 命名空间/类体的声明列表，递归遍历其内部成员
                self._walk_declarations(child, source, source_str, module_path,
                                        symbols, parent_qualified)

    def _process_type_declaration(self, node, source: bytes, source_str: str,
                                  kind: str, module_path: str,
                                  symbols: List[Dict[str, Any]],
                                  parent_qualified: str):
        """处理类型声明（class/interface/struct/enum）

        添加类型符号，并递归遍历其 declaration_list 内的成员（方法、属性、嵌套类型）。
        """
        sym = self._parse_class_like(node, source, kind, module_path, parent_qualified)
        if sym:
            symbols.append(sym)
            body = self._find_child_by_type(node, "declaration_list")
            if body:
                # 用类型自身的 qualified_name 作为子成员的 parent_qualified
                self._walk_declarations(body, source, source_str, module_path,
                                        symbols, sym["qualified_name"])

    def _parse_class_like(self, node, source: bytes, kind: str,
                          module_path: str, parent_qualified: str) -> Optional[Dict[str, Any]]:
        """解析类/接口/结构体/枚举声明

        类型名是 class/interface/struct/enum 关键字后的第一个 identifier。
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

        方法名是 parameter_list 紧前的 identifier（返回类型可能是 predefined_type
        或 identifier，不能简单取第一个 identifier）。
        """
        name_node = self._find_name_before(node, source, "parameter_list")
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
        signature = self._extract_method_signature(node, source, name)

        return {
            "name": name,
            "kind": "method",
            "visibility": self._detect_visibility(node, source),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": signature,
            "has_comment": 1 if bool(comment) else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_constructor(self, node, source: bytes,
                           module_path: str, parent_qualified: str) -> Optional[Dict[str, Any]]:
        """解析构造方法声明

        构造方法名是 parameter_list 紧前的 identifier（即类名）。
        """
        name_node = self._find_name_before(node, source, "parameter_list")
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
        params_node = self._find_child_by_type(node, "parameter_list")
        params = self._node_text(params_node, source) if params_node else "()"

        return {
            "name": name,
            "kind": "constructor",
            "visibility": self._detect_visibility(node, source),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"{name}{params}",
            "has_comment": 1 if bool(comment) else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified,
            "content": content,
        }

    def _parse_property(self, node, source: bytes,
                        module_path: str, parent_qualified: str) -> Optional[Dict[str, Any]]:
        """解析属性声明

        属性名是 accessor_list 紧前的 identifier。
        """
        name_node = self._find_name_before(node, source, "accessor_list")
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
            "kind": "property",
            "visibility": self._detect_visibility(node, source),
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f"property {name}",
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
        """提取 using 语句"""
        imports: List[Dict[str, Any]] = []

        def walk(node):
            for child in node.named_children:
                if child.type == "using_directive":
                    name_node = self._find_child_by_type(child, "qualified_name") \
                                or self._find_child_by_type(child, "identifier")
                    module_name = self._node_text(name_node, source) if name_node else ""
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

        遍历方法/构造方法体内所有 invocation_expression，记录调用者与被调用者。
        """
        calls: List[Dict[str, Any]] = []
        namespace = self._extract_namespace(root, source)
        effective_module = namespace or module_path

        def walk(node, current_fn: str = "", current_qualified: str = ""):
            for child in node.named_children:
                if child.type in ("class_declaration", "struct_declaration",
                                  "interface_declaration", "enum_declaration"):
                    name_node = self._find_child_by_type(child, "identifier")
                    type_name = self._node_text(name_node, source) if name_node else ""
                    if type_name:
                        if current_qualified:
                            next_qualified = f"{current_qualified}.{type_name}"
                        else:
                            next_qualified = f"{effective_module}.{type_name}" if effective_module else type_name
                    else:
                        next_qualified = current_qualified
                    walk(child, current_fn, next_qualified)
                elif child.type in ("method_declaration", "constructor_declaration"):
                    name_node = self._find_name_before(child, source, "parameter_list")
                    fn_name = self._node_text(name_node, source) if name_node else ""
                    if fn_name:
                        if current_qualified:
                            fn_qualified = f"{current_qualified}.{fn_name}"
                        else:
                            fn_qualified = f"{effective_module}.{fn_name}" if effective_module else fn_name
                    else:
                        fn_qualified = current_qualified
                    walk(child, fn_name, fn_qualified)
                elif child.type == "invocation_expression":
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
        """解析方法调用

        invocation_expression 形如 `obj.Method(args)` 或 `Method(args)`。
        - 含 member_access_expression 时：callee_name=方法名，callee_module=接收者
        - 否则：callee_name=直接函数名
        """
        callee_name = ""
        callee_module = ""

        member_access = self._find_child_by_type(node, "member_access_expression")
        if member_access:
            # 形如 obj.Method() 或 Class.Method()
            name_node = self._find_child_by_type(member_access, "identifier")
            if name_node:
                callee_name = self._node_text(name_node, source)
            # member_access_expression 的第一个 identifier 是接收者
            scope_node = self._find_child_by_type(member_access, "identifier")
            if scope_node:
                callee_module = self._node_text(scope_node, source)
        else:
            # 形如 Method()
            name_node = self._find_child_by_type(node, "identifier")
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
        """检测是否有文件级注释（XML 文档注释 /// 或 /** */）"""
        if not root.children:
            return False
        for child in root.children:
            if child.type in ("comment", "block_comment", "line_comment"):
                text = self._node_text(child, source).strip()
                if text.startswith("///") or text.startswith("/**"):
                    return True
            elif child.type == "using_directive":
                continue
            elif child.type == "namespace_declaration":
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
        """查找终止节点（parameter_list / accessor_list）紧前的 identifier

        C# grammar 不使用 field name，因此方法/属性名需要按位置定位：
        方法名 = parameter_list 紧前的 identifier
        属性名 = accessor_list 紧前的 identifier
        """
        children = node.named_children
        for i, child in enumerate(children):
            if child.type == terminator_type:
                if i > 0 and children[i - 1].type == "identifier":
                    return children[i - 1]
                break
        return None

    def _find_prev_comment(self, node, source: bytes) -> str:
        """查找节点前的注释（XML 文档注释 /// 或 /* */）"""
        comment_parts = []
        prev = node.prev_named_sibling
        while prev and prev.type in ("comment", "block_comment", "line_comment"):
            text = self._node_text(prev, source).strip()
            comment_parts.insert(0, text)
            prev = prev.prev_named_sibling
        return "\n".join(comment_parts) if comment_parts else ""

    def _detect_visibility(self, node, source: bytes) -> str:
        """检测可见性修饰符（C# 默认 private）

        tree-sitter-c-sharp 使用单数 `modifier` 节点（可重复），
        一个声明可能有多个 modifier（如 `public static`）。
        """
        modifiers_text = []
        for child in node.named_children:
            if child.type == "modifier":
                modifiers_text.append(self._node_text(child, source))
        text = " ".join(modifiers_text)
        if "public" in text:
            return "public"
        elif "protected" in text:
            return "protected"
        elif "internal" in text:
            return "internal"
        elif "private" in text:
            return "private"
        return "private"  # C# 默认 private

    def _extract_method_signature(self, node, source: bytes, name: str) -> str:
        """提取方法签名

        返回类型可能是 predefined_type（string/int/...）或 identifier（自定义类型），
        需要遍历 modifier 之后、parameter_list 之前的所有节点拼接为返回类型。
        """
        params_node = self._find_child_by_type(node, "parameter_list")
        params = self._node_text(params_node, source) if params_node else "()"

        # 收集 modifier 之后、parameter_list 之前的非 modifier 节点作为返回类型
        ret_parts = []
        seen_modifier_end = False
        for child in node.named_children:
            if child.type == "modifier":
                continue
            if child.type == "parameter_list":
                break
            # 跳过方法名 identifier（最后一个 identifier）
            # 此时它后面的下一个节点就是 parameter_list，由外层判断
            ret_parts.append(self._node_text(child, source))

        # 移除最后一个 identifier（方法名本身）
        if ret_parts and ret_parts[-1] == name:
            ret_parts.pop()

        ret_type = " ".join(ret_parts) if ret_parts else "void"
        return f"{ret_type} {name}{params}"
