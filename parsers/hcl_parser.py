"""
hcl_parser.py
=============

基于 tree-sitter 的 Terraform/HCL 源码解析器。

HCL（HashiCorp Configuration Language）是声明式配置语言，
不像通用编程语言那样有函数/类层级。本解析器将顶层 block 视为符号：
- resource 块 → 符号 kind="resource"
- provider 块 → 符号 kind="provider"
- variable 块 → 符号 kind="variable"
- output 块 → 符号 kind="output"
- module 块 → 符号 kind="module"
- data 块 → 符号 kind="data"
- locals 块 → 符号 kind="locals"
- terraform 块 → 符号 kind="terraform"

调用关系按 attribute 中的引用解析（如 aws_instance.web.public_ip）。

安装：pip install tree-sitter-hcl
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import tree_sitter_hcl as tshcl

from .base import BaseParser


def label_str(label: str) -> str:
    """格式化 HCL 标签为带引号的字符串"""
    return f'"{label}"'


class HclParser(BaseParser):
    """Terraform/HCL 源码解析器"""

    language_id = "hcl"
    language_module = tshcl

    # 块类型 → 符号 kind 映射
    BLOCK_TYPES = {
        "resource": "resource",
        "provider": "provider",
        "variable": "variable",
        "output": "output",
        "module": "module",
        "data": "data",
        "locals": "locals",
        "terraform": "terraform",
    }

    # ------------------------------------------------------------------
    # 符号提取
    # ------------------------------------------------------------------

    def _extract_symbols(self, root, source: bytes, source_str: str,
                         module_path: str) -> List[Dict[str, Any]]:
        """提取符号：顶层 block（resource/provider/variable/output/module 等）"""
        symbols: List[Dict[str, Any]] = []

        # 找到 config_file 下的 body
        body = self._find_child_by_type(root, "body")
        if not body:
            return symbols

        for child in body.named_children:
            if child.type == "block":
                sym = self._parse_block(child, source, module_path)
                if sym:
                    symbols.append(sym)

        return symbols

    def _parse_block(self, node, source: bytes,
                     module_path: str) -> Optional[Dict[str, Any]]:
        """解析一个 HCL block

        block 结构：identifier [string_lit...] block_start body block_end
        第一个 identifier 是块类型（resource/provider/...），
        后续 string_lit 是块名标签（如 "aws_instance" "web"）。
        """
        children = node.named_children
        if not children:
            return None

        # 第一个 identifier 是块类型
        type_node = children[0]
        if type_node.type != "identifier":
            return None

        block_type = self._node_text(type_node, source)
        kind = self.BLOCK_TYPES.get(block_type)
        if not kind:
            # 未知块类型，跳过
            return None

        # 收集后续 string_lit 作为块名标签
        labels = []
        for child in children[1:]:
            if child.type == "string_lit":
                # string_lit 内有 template_literal
                template = self._find_child_by_type(child, "template_literal")
                if template:
                    labels.append(self._node_text(template, source))
            else:
                break

        # 符号名：resource 类型用 type.name（如 aws_instance.web），
        # 其他类型用第一个 label
        if block_type == "resource" and len(labels) >= 2:
            name = f"{labels[0]}.{labels[1]}"
            qualified_name = name
        elif block_type == "data" and len(labels) >= 2:
            name = f"{labels[0]}.{labels[1]}"
            qualified_name = name
        elif labels:
            name = labels[0]
            qualified_name = name
        else:
            name = block_type
            qualified_name = block_type

        # 检测块前注释（HCL 用 # 或 // 行注释、/* */ 块注释）
        comment = self._find_prev_comment(node, source)
        content = self._node_text(node, source)

        return {
            "name": name,
            "kind": kind,
            "visibility": "public",  # HCL 无可见性概念，统一 public
            "start_line": node.start_point[0] + 1,
            "end_line": node.end_point[0] + 1,
            "start_col": node.start_point[1],
            "end_col": node.end_point[1],
            "signature": f'{block_type} {" ".join(label_str(l) for l in labels)}',
            "has_comment": 1 if bool(comment) else 0,
            "comment_content": comment,
            "module_path": module_path,
            "qualified_name": qualified_name,
            "content": content,
        }

    # ------------------------------------------------------------------
    # import 提取
    # ------------------------------------------------------------------

    def _extract_imports(self, root, source: bytes) -> List[Dict[str, Any]]:
        """HCL 无 import 概念，返回空列表"""
        return []

    # ------------------------------------------------------------------
    # 调用关系提取
    # ------------------------------------------------------------------

    def _extract_raw_calls(self, root, source: bytes,
                           module_path: str) -> List[Dict[str, Any]]:
        """提取引用关系

        HCL 的"调用"是属性值中对其他资源的引用，
        如 value = aws_instance.web.public_ip 引用了 aws_instance.web 块。
        """
        calls: List[Dict[str, Any]] = []

        body = self._find_child_by_type(root, "body")
        if not body:
            return calls

        def walk(node, current_block: str = "", current_qualified: str = ""):
            """递归遍历 AST，识别 block 节点并收集 attribute 表达式中的跨块引用。

            Args:
                node: 当前遍历的 tree-sitter 节点。
                current_block: 当前所在 block 的简名，用于标注引用者。
                current_qualified: 当前所在 block 的完整限定名，用于精确匹配。
            """
            for child in node.named_children:
                if child.type == "block":
                    # 进入 block 时记录当前块名
                    block_sym = self._parse_block(child, source, module_path)
                    if block_sym:
                        walk(child, block_sym["qualified_name"], block_sym["qualified_name"])
                        continue
                    walk(child, current_block, current_qualified)
                elif child.type == "attribute":
                    # attribute = identifier expression
                    # 提取 expression 中的引用
                    expr = self._find_child_by_type(child, "expression")
                    if expr:
                        refs = self._extract_refs_from_expression(expr, source)
                        for ref in refs:
                            if current_block:
                                calls.append({
                                    "caller_name": current_block,
                                    "caller_qualified": current_qualified,
                                    "caller_module": module_path,
                                    "callee_name": ref,
                                    "callee_module": "",
                                    "call_line": child.start_point[0] + 1,
                                })
                    walk(child, current_block, current_qualified)
                else:
                    walk(child, current_block, current_qualified)

        walk(body)
        return calls

    def _extract_refs_from_expression(self, expr, source: bytes) -> List[str]:
        """从表达式中提取引用

        HCL 引用形如 aws_instance.web.public_ip，
        由 variable_expr + get_attr 链组成。
        """
        refs: List[str] = []

        def walk_expr(node):
            """递归遍历表达式节点，收集形如 a.b.c 的跨块引用。

            Args:
                node: 当前遍历的 tree-sitter 表达式节点。
            """
            for child in node.named_children:
                if child.type == "variable_expr":
                    # variable_expr 包含 identifier
                    ident = self._find_child_by_type(child, "identifier")
                    if ident:
                        # 后续 get_attr 链组成完整引用
                        ref = self._node_text(child, source)
                        # 检查兄弟节点中的 get_attr
                        sibling = child.next_named_sibling
                        while sibling and sibling.type == "get_attr":
                            attr_ident = self._find_child_by_type(sibling, "identifier")
                            if attr_ident:
                                ref += "." + self._node_text(attr_ident, source)
                            sibling = sibling.next_named_sibling
                        # 只保留至少含一个点的引用（即跨块引用）
                        if "." in ref:
                            refs.append(ref)
                walk_expr(child)

        walk_expr(expr)
        return refs

    # ------------------------------------------------------------------
    # 模块级注释
    # ------------------------------------------------------------------

    def _has_module_comment(self, root, source: bytes) -> bool:
        """检测是否有文件级注释（# 或 /* */）"""
        for child in root.children:
            if child.type in ("comment", "line_comment", "block_comment"):
                return True
            elif child.type == "config_file":
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
