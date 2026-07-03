#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
rust.py
=======

基于 tree-sitter 的 Rust 源码解析器。

功能：
- 解析 Rust 源文件，提取函数、结构体、枚举、 trait 等符号
- 提取 use 语句和 mod 声明（用于跨文件调用解析）
- 提取内联模块及其内部符号
- 提取函数调用关系（原始调用，带路径信息）
- 统计行数、检查模块级注释
"""

from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Set, Tuple

import tree_sitter_rust as tsrust
from tree_sitter import Language, Parser

from ..config import read_file_normalized, norm_path, norm_newlines, compute_content_hash
from .call_filter import should_filter_call


class RustParser:
    """基于 tree-sitter 的 Rust 源码解析器"""
    
    def __init__(self):
        self.language = Language(tsrust.language())
        self.parser = Parser(self.language)
    
    # --------------------------------------------------------------------
    # 公共接口
    # --------------------------------------------------------------------
    
    def parse_file(self, file_path: str, module_path: str = "") -> Dict[str, Any]:
        """解析单个 Rust 文件"""
        source_str, content_hash = read_file_normalized(file_path)
        source = source_str.encode("utf-8")
        
        tree = self.parser.parse(source)
        root = tree.root_node
        
        # 提取符号
        symbols = self._extract_symbols(root, source, source_str, module_path)
        
        # 提取 use 语句（用于跨文件调用解析）
        use_stmts = self._extract_use_stmts(root, source)
        
        # 提取 mod 声明
        mod_decls = self._extract_mod_decls(root, source)
        
        # 提取内联模块及其符号
        inline_modules = self._extract_inline_modules(root, source, source_str, module_path)
        
        # 提取调用关系（原始调用，带路径信息）
        raw_calls = self._extract_raw_calls(root, source, module_path)
        
        # 统计行数
        total_lines = source_str.count("\n") + 1
        
        # 检查模块级注释
        has_module_comment = self._has_module_comment(root, source)
        
        return {
            "total_lines": total_lines,
            "content_hash": content_hash,
            "symbols": symbols,
            "use_stmts": use_stmts,
            "mod_decls": mod_decls,
            "inline_modules": inline_modules,
            "raw_calls": raw_calls,
            "has_module_comment": has_module_comment,
        }
    
    # --------------------------------------------------------------------
    # 符号提取
    # --------------------------------------------------------------------
    
    def _extract_symbols(self, root, source: bytes, source_str: str, module_path: str) -> List[Dict[str, Any]]:
        """提取所有可注释的符号（不含内联模块内的符号）"""
        symbols = []
        
        leading_attrs = []
        
        for node in root.children:
            kind = node.type
            
            if kind == "attribute_item":
                leading_attrs.append(node)
                continue
            
            attrs = leading_attrs
            leading_attrs = []
            
            if kind == "function_item":
                symbols.append(self._parse_function(node, source, source_str, module_path, attrs))
            elif kind == "struct_item":
                symbols.append(self._parse_struct(node, source, source_str, module_path))
            elif kind == "enum_item":
                symbols.append(self._parse_enum(node, source, source_str, module_path))
            elif kind == "trait_item":
                symbols.append(self._parse_trait(node, source, source_str, module_path))
            elif kind == "const_item":
                symbols.append(self._parse_const(node, source, source_str, module_path))
            elif kind == "static_item":
                symbols.append(self._parse_static(node, source, source_str, module_path))
            elif kind == "macro_definition":
                symbols.append(self._parse_macro(node, source, source_str, module_path))
            elif kind == "impl_item":
                symbols.append(self._parse_impl(node, source, source_str, module_path))
            else:
                leading_attrs = []
        
        return symbols
    
    def _parse_function(self, node, source: bytes, source_str: str, module_path: str, leading_attrs: List = None) -> Dict[str, Any]:
        """解析 Rust function_item 节点为符号字典

        提取函数名、可见性、签名、注释、内容 hash 等信息。
        若前导属性包含 #[test]，则标记为 test_fn 类型。

        Args:
            node: tree-sitter function_item 节点
            source: 源代码字节串
            source_str: 源代码字符串
            module_path: 所属模块路径
            leading_attrs: 前导属性节点列表（用于检测 #[test] 等）

        Returns:
            符号信息字典
        """
        name_node = node.child_by_field_name("name")
        name = self._get_text(name_node, source) if name_node else ""
        visibility = self._get_visibility(node, source)
        has_comment = self._has_doc_comment(node, source)
        content = self._get_symbol_content(node, source, source_str)
        comment_text = self._get_symbol_comment_text(node, source, source_str)
        content_hash = compute_content_hash(content)
        
        is_test = False
        if leading_attrs:
            for attr in leading_attrs:
                attr_text = self._get_text(attr, source)
                if "#[test]" in attr_text:
                    is_test = True
                    break
        
        signature = self._get_text(node, source)[:200]
        
        qualified = f"{module_path}::{name}" if module_path else name
        
        return {
            "name": name,
            "kind": "test_fn" if is_test else "fn",
            "visibility": visibility,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": signature,
            "has_comment": has_comment,
            "comment_content": comment_text,
            "content": content,
            "content_hash": content_hash,
            "module_path": module_path,
            "qualified_name": qualified,
        }
    
    def _parse_struct(self, node, source: bytes, source_str: str, module_path: str) -> Dict[str, Any]:
        """解析 Rust struct_item 节点为符号字典

        提取结构体名称、可见性、字段签名、注释等信息。

        Args:
            node: tree-sitter struct_item 节点
            source: 源代码字节串
            source_str: 源代码字符串
            module_path: 所属模块路径

        Returns:
            符号信息字典，kind 为 "struct"
        """
        name_node = node.child_by_field_name("name")
        name = self._get_text(name_node, source) if name_node else ""
        visibility = self._get_visibility(node, source)
        has_comment = self._has_doc_comment(node, source)
        content = self._get_symbol_content(node, source, source_str)
        comment_text = self._get_symbol_comment_text(node, source, source_str)
        content_hash = compute_content_hash(content)
        qualified = f"{module_path}::{name}" if module_path else name
        
        return {
            "name": name,
            "kind": "struct",
            "visibility": visibility,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": self._get_text(node, source)[:100],
            "has_comment": has_comment,
            "comment_content": comment_text,
            "content": content,
            "content_hash": content_hash,
            "module_path": module_path,
            "qualified_name": qualified,
        }
    
    def _parse_enum(self, node, source: bytes, source_str: str, module_path: str) -> Dict[str, Any]:
        """解析 Rust enum_item 节点为符号字典

        提取枚举名称、可见性、变体签名、注释等信息。

        Args:
            node: tree-sitter enum_item 节点
            source: 源代码字节串
            source_str: 源代码字符串
            module_path: 所属模块路径

        Returns:
            符号信息字典，kind 为 "enum"
        """
        name_node = node.child_by_field_name("name")
        name = self._get_text(name_node, source) if name_node else ""
        visibility = self._get_visibility(node, source)
        has_comment = self._has_doc_comment(node, source)
        content = self._get_symbol_content(node, source, source_str)
        comment_text = self._get_symbol_comment_text(node, source, source_str)
        content_hash = compute_content_hash(content)
        qualified = f"{module_path}::{name}" if module_path else name
        
        return {
            "name": name,
            "kind": "enum",
            "visibility": visibility,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": self._get_text(node, source)[:100],
            "has_comment": has_comment,
            "comment_content": comment_text,
            "content": content,
            "content_hash": content_hash,
            "module_path": module_path,
            "qualified_name": qualified,
        }
    
    def _parse_trait(self, node, source: bytes, source_str: str, module_path: str) -> Dict[str, Any]:
        """解析 Rust trait_item 节点为符号字典

        提取 trait 名称、可见性、方法签名、注释等信息。

        Args:
            node: tree-sitter trait_item 节点
            source: 源代码字节串
            source_str: 源代码字符串
            module_path: 所属模块路径

        Returns:
            符号信息字典，kind 为 "trait"
        """
        name_node = node.child_by_field_name("name")
        name = self._get_text(name_node, source) if name_node else ""
        visibility = self._get_visibility(node, source)
        has_comment = self._has_doc_comment(node, source)
        content = self._get_symbol_content(node, source, source_str)
        comment_text = self._get_symbol_comment_text(node, source, source_str)
        content_hash = compute_content_hash(content)
        qualified = f"{module_path}::{name}" if module_path else name
        
        return {
            "name": name,
            "kind": "trait",
            "visibility": visibility,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": self._get_text(node, source)[:100],
            "has_comment": has_comment,
            "comment_content": comment_text,
            "content": content,
            "content_hash": content_hash,
            "module_path": module_path,
            "qualified_name": qualified,
        }
    
    def _parse_const(self, node, source: bytes, source_str: str, module_path: str) -> Dict[str, Any]:
        """解析 Rust const_item 节点为符号字典

        提取常量名称、可见性、类型签名、注释等信息。

        Args:
            node: tree-sitter const_item 节点
            source: 源代码字节串
            source_str: 源代码字符串
            module_path: 所属模块路径

        Returns:
            符号信息字典，kind 为 "const"
        """
        name_node = node.child_by_field_name("name")
        name = self._get_text(name_node, source) if name_node else ""
        visibility = self._get_visibility(node, source)
        has_comment = self._has_doc_comment(node, source)
        content = self._get_symbol_content(node, source, source_str)
        comment_text = self._get_symbol_comment_text(node, source, source_str)
        content_hash = compute_content_hash(content)
        qualified = f"{module_path}::{name}" if module_path else name
        
        return {
            "name": name,
            "kind": "const",
            "visibility": visibility,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": self._get_text(node, source)[:50],
            "has_comment": has_comment,
            "comment_content": comment_text,
            "content": content,
            "content_hash": content_hash,
            "module_path": module_path,
            "qualified_name": qualified,
        }
    
    def _parse_static(self, node, source: bytes, source_str: str, module_path: str) -> Dict[str, Any]:
        name_node = node.child_by_field_name("name")
        name = self._get_text(name_node, source) if name_node else ""
        visibility = self._get_visibility(node, source)
        has_comment = self._has_doc_comment(node, source)
        content = self._get_symbol_content(node, source, source_str)
        comment_text = self._get_symbol_comment_text(node, source, source_str)
        content_hash = compute_content_hash(content)
        qualified = f"{module_path}::{name}" if module_path else name
        
        return {
            "name": name,
            "kind": "static",
            "visibility": visibility,
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": self._get_text(node, source)[:50],
            "has_comment": has_comment,
            "comment_content": comment_text,
            "content": content,
            "content_hash": content_hash,
            "module_path": module_path,
            "qualified_name": qualified,
        }
    
    def _parse_macro(self, node, source: bytes, source_str: str, module_path: str) -> Dict[str, Any]:
        name = ""
        for child in node.children:
            if child.type == "identifier":
                name = self._get_text(child, source)
                break
        
        has_comment = self._has_doc_comment(node, source)
        content = self._get_symbol_content(node, source, source_str)
        comment_text = self._get_symbol_comment_text(node, source, source_str)
        content_hash = compute_content_hash(content)
        qualified = f"{module_path}::{name}" if module_path else name
        
        return {
            "name": name,
            "kind": "macro_rules",
            "visibility": "private",
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": self._get_text(node, source)[:50],
            "has_comment": has_comment,
            "comment_content": comment_text,
            "content": content,
            "content_hash": content_hash,
            "module_path": module_path,
            "qualified_name": qualified,
        }
    
    def _parse_impl(self, node, source: bytes, source_str: str, module_path: str) -> Dict[str, Any]:
        type_node = node.child_by_field_name("type")
        name = self._get_text(type_node, source) if type_node else ""
        
        trait_node = node.child_by_field_name("trait")
        if trait_node:
            trait_name = self._get_text(trait_node, source)
            name = f"{trait_name} for {name}"
        
        has_comment = self._has_doc_comment(node, source)
        content = self._get_symbol_content(node, source, source_str)
        comment_text = self._get_symbol_comment_text(node, source, source_str)
        content_hash = compute_content_hash(content)
        qualified = f"{module_path}::impl {name}" if module_path else f"impl {name}"
        
        return {
            "name": name,
            "kind": "impl",
            "visibility": "private",
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": self._get_text(node, source)[:100],
            "has_comment": has_comment,
            "comment_content": comment_text,
            "content": content,
            "content_hash": content_hash,
            "module_path": module_path,
            "qualified_name": qualified,
        }
    
    # --------------------------------------------------------------------
    # use / mod 提取
    # --------------------------------------------------------------------
    
    def _extract_use_stmts(self, root, source: bytes) -> List[Dict[str, str]]:
        """提取 use 语句"""
        use_stmts = []
        
        for node in root.children:
            if node.type == "use_declaration":
                use_text = self._get_text(node, source)
                # 解析 use xxx::yyy as zzz;
                # 简化处理：提取完整路径和别名
                parts = self._parse_use_stmt(node, source)
                if parts:
                    use_stmts.append(parts)
        
        return use_stmts
    
    def _parse_use_stmt(self, node, source: bytes) -> Optional[Dict[str, str]]:
        """解析 use 语句，返回 {path, alias, is_pub}"""
        # use a::b::c as d;
        # use a::b::c;
        # use a::b::{c, d};
        # use a::b::*;
        
        # 检查是否是 pub use
        is_pub = False
        for child in node.children:
            if child.type == "visibility_modifier":
                vis_text = self._get_text(child, source)
                if "pub" in vis_text:
                    is_pub = True
                break
        
        # 找到路径节点
        path_node = None
        alias = ""
        
        for child in node.children:
            if child.type in ("scoped_identifier", "identifier", "use_tree", "use_list", 
                              "scoped_use_list", "use_wildcard"):
                path_node = child
            elif child.type == "as_alias":
                alias_node = child.child_by_field_name("name")
                if alias_node:
                    alias = self._get_text(alias_node, source)
        
        if not path_node:
            return None
        
        path_text = self._get_text(path_node, source)
        
        return {
            "path": path_text,
            "alias": alias,
            "is_pub": is_pub,
        }
    
    def _extract_mod_decls(self, root, source: bytes) -> List[Dict[str, str]]:
        """提取 mod 声明（mod xxx;）"""
        mod_decls = []
        
        for node in root.children:
            if node.type == "mod_item":
                # 检查是否是 mod xxx; 形式（无 body）
                has_body = any(child.type == "declaration_list" for child in node.children)
                if not has_body:
                    name_node = node.child_by_field_name("name")
                    name = self._get_text(name_node, source) if name_node else ""
                    visibility = self._get_visibility(node, source)
                    mod_decls.append({
                        "name": name,
                        "visibility": visibility,
                    })
        
        return mod_decls
    
    def _extract_inline_modules(self, root, source: bytes, source_str: str, parent_module: str) -> List[Dict[str, Any]]:
        """提取内联模块（mod xxx { ... }）及其内部符号"""
        modules = []
        
        for node in root.children:
            if node.type == "mod_item":
                body = None
                for child in node.children:
                    if child.type == "declaration_list":
                        body = child
                        break
                
                if body:
                    name_node = node.child_by_field_name("name")
                    name = self._get_text(name_node, source) if name_node else ""
                    visibility = self._get_visibility(node, source)
                    
                    mod_path = f"{parent_module}::{name}" if parent_module else name
                    
                    inner_symbols = []
                    for child in body.children:
                        if child.type == "function_item":
                            inner_symbols.append(self._parse_function(child, source, source_str, mod_path))
                        elif child.type == "struct_item":
                            inner_symbols.append(self._parse_struct(child, source, source_str, mod_path))
                        elif child.type == "enum_item":
                            inner_symbols.append(self._parse_enum(child, source, source_str, mod_path))
                        elif child.type == "trait_item":
                            inner_symbols.append(self._parse_trait(child, source, source_str, mod_path))
                        elif child.type == "const_item":
                            inner_symbols.append(self._parse_const(child, source, source_str, mod_path))
                        elif child.type == "impl_item":
                            inner_symbols.append(self._parse_impl(child, source, source_str, mod_path))
                    
                    modules.append({
                        "name": name,
                        "visibility": visibility,
                        "module_path": mod_path,
                        "symbols": inner_symbols,
                        "start_line": node.start_point[0] + 1,
                        "end_line": node.end_point[0] + 1,
                    })
        
        return modules
    
    # --------------------------------------------------------------------
    # 调用关系提取（原始调用，带路径信息）
    # --------------------------------------------------------------------
    
    def _extract_raw_calls(self, root, source: bytes, module_path: str) -> List[Dict[str, Any]]:
        """提取函数调用（原始形式，保留路径信息）"""
        calls = []
        
        # 收集所有函数定义
        fn_defs = {}  # name -> function_node
        for node in root.children:
            if node.type == "function_item":
                name_node = node.child_by_field_name("name")
                if name_node:
                    name = self._get_text(name_node, source)
                    fn_defs[name] = node
        
        # 对每个函数，提取其体内的调用
        for fn_name, fn_node in fn_defs.items():
            body = fn_node.child_by_field_name("body")
            if not body:
                continue
            
            for call_node in self._walk(body):
                if call_node.type == "call_expression":
                    func_node = call_node.child_by_field_name("function")
                    if not func_node:
                        continue
                    
                    # 解析被调用函数的路径
                    callee_info = self._parse_call_function(func_node, source)
                    if callee_info:
                        callee_name = callee_info["name"]
                        if not should_filter_call("rust", callee_name):
                            calls.append({
                                "caller_name": fn_name,
                                "caller_module": module_path,
                                "callee_name": callee_name,
                                "callee_path": callee_info["path"],
                                "callee_is_qualified": callee_info["is_qualified"],
                                "call_line": call_node.start_point[0] + 1,
                            })
        
        return calls
    
    def _parse_call_function(self, func_node, source: bytes) -> Optional[Dict[str, Any]]:
        """解析 call_expression 中的 function 部分"""
        if func_node.type == "identifier":
            return {
                "name": self._get_text(func_node, source),
                "path": "",
                "is_qualified": False,
            }
        elif func_node.type == "scoped_identifier":
            # a::b::c
            path = self._get_text(func_node, source)
            # 最后一个是名称
            parts = path.split("::")
            name = parts[-1]
            path_only = "::".join(parts[:-1])
            return {
                "name": name,
                "path": path_only,
                "is_qualified": True,
            }
        elif func_node.type == "field_expression":
            # obj.method() - 方法调用，暂不处理
            return None
        else:
            return None
    
    # --------------------------------------------------------------------
    # 辅助方法
    # --------------------------------------------------------------------
    
    def _walk(self, node):
        """深度优先遍历"""
        yield node
        for child in node.children:
            yield from self._walk(child)
    
    def _get_text(self, node, source: bytes) -> str:
        """获取节点文本"""
        if node is None:
            return ""
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")
    
    def _get_visibility(self, node, source: bytes) -> str:
        """获取可见性"""
        for child in node.children:
            if child.type == "visibility_modifier":
                text = self._get_text(child, source)
                if "crate" in text:
                    return "pub(crate)"
                else:
                    return "pub"
        return "private"
    
    def _get_symbol_content_range(self, node, source: bytes) -> tuple[int, int]:
        """获取符号的内容范围（从第一个前导注释/属性开始，到符号结束），返回 (start_line, end_line)"""
        start_line = node.start_point[0] + 1
        end_line = node.end_point[0] + 1
        
        prev = node.prev_sibling
        while prev and prev.type in ("line_comment", "block_comment", "attribute_item"):
            prev_start = prev.start_point[0] + 1
            if prev_start < start_line:
                start_line = prev_start
            prev = prev.prev_sibling
        
        return (start_line, end_line)
    
    def _get_symbol_content(self, node, source: bytes, source_str: str) -> str:
        """获取符号的完整内容（从注释开始到符号结束）"""
        start_line, end_line = self._get_symbol_content_range(node, source)
        lines = source_str.split("\n")
        return "\n".join(lines[start_line - 1:end_line])
    
    def _get_symbol_comment_text(self, node, source: bytes, source_str: str) -> str:
        """获取符号的完整注释文本（包括中间空行）"""
        # 从 content 的开头提取注释块（/// 或 /** 开头的行）
        content = self._get_symbol_content(node, source, source_str)
        lines = content.split("\n")
        
        comment_lines = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("///") or stripped.startswith("//!"):
                comment_lines.append(line)
            elif stripped.startswith("/**") or stripped.startswith("/*!"):
                comment_lines.append(line)
            elif stripped.startswith("*") and comment_lines:  # block comment 中间行
                comment_lines.append(line)
            elif stripped == "" and comment_lines:  # 注释中间的空行
                comment_lines.append(line)
            elif comment_lines and not stripped.startswith("pub") and not stripped.startswith("fn") and not stripped.startswith("struct") and not stripped.startswith("enum"):
                # 可能是注释的一部分（其他格式）
                continue
            else:
                # 非注释行，结束
                break
        
        # 去掉末尾的空行
        while comment_lines and comment_lines[-1].strip() == "":
            comment_lines.pop()
        
        return "\n".join(comment_lines)
    
    def _has_doc_comment(self, node, source: bytes) -> int:
        """检查是否有文档注释（/// 或 /** */）"""
        prev = node.prev_sibling
        while prev and prev.type in ("line_comment", "block_comment", "attribute_item"):
            if prev.type == "line_comment":
                text = self._get_text(prev, source)
                if text.startswith("///") or text.startswith("//!"):
                    return 1
            elif prev.type == "block_comment":
                text = self._get_text(prev, source)
                if text.startswith("/**") or text.startswith("/*!"):
                    return 1
            prev = prev.prev_sibling
        
        return 0
    
    def _is_test_fn(self, node, source: bytes) -> bool:
        """检查是否是 #[test] 函数"""
        for child in node.children:
            if child.type == "attribute_item":
                attr_text = self._get_text(child, source)
                if "#[test]" in attr_text:
                    return True
        return False
    
    def _has_module_comment(self, root, source: bytes) -> bool:
        """检查文件是否有模块级注释"""
        for child in root.children:
            if child.type == "line_comment":
                text = self._get_text(child, source)
                if text.startswith("//!"):
                    return True
            elif child.type == "block_comment":
                text = self._get_text(child, source)
                if text.startswith("/*!"):
                    return True
            else:
                # 遇到非注释就停止（模块注释必须在文件最开头）
                break
        return False
