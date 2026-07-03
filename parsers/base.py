"""
base.py
=======

代码解析器基类：定义所有语言解析器的统一接口。

各语言解析器需继承 BaseParser 并实现 parse_file 方法，
返回统一格式的解析结果，供上层 db.py 和调用链分析使用。
"""

from __future__ import annotations

from typing import Any, Dict, List

from tree_sitter import Language, Parser

from ..config import read_file_normalized
from .call_filter import should_filter_call


class BaseParser:
    """代码解析器基类

    所有语言解析器的统一接口，定义：
    - 统一的 parse_file 方法签名
    - 统一的返回格式
    - 通用的工具方法（节点文本提取、注释检测等）
    """

    language_id: str = ""  # 语言标识，如 "rust", "typescript"
    language_module = None  # tree-sitter 语言模块

    def __init__(self):
        if self.language_module is None:
            raise NotImplementedError("子类必须设置 language_module")
        self.language = Language(self.language_module.language())
        self.parser = Parser(self.language)

    # --------------------------------------------------------------------
    # 公共接口
    # --------------------------------------------------------------------

    def parse_file(self, file_path: str, module_path: str = "") -> Dict[str, Any]:
        """解析单个源文件

        Args:
            file_path: 文件绝对路径
            module_path: 模块路径（如 "crate::core"）

        Returns:
            统一格式的解析结果字典：
            {
                "total_lines": int,
                "content_hash": str,
                "symbols": [...],          # 符号列表
                "imports": [...],          # import/use 语句
                "raw_calls": [...],        # 原始调用关系
                "has_module_comment": bool,
                "language": str,
            }
        """
        source_str, content_hash = read_file_normalized(file_path)
        source = source_str.encode("utf-8")

        tree = self.parser.parse(source)
        root = tree.root_node

        symbols = self._extract_symbols(root, source, source_str, module_path)
        imports = self._extract_imports(root, source)
        raw_calls = self._extract_raw_calls(root, source, module_path)
        
        # 统一过滤标准库/外部依赖/全局调用
        raw_calls = [
            c for c in raw_calls
            if not should_filter_call(self.language_id, c.get("callee_name", ""))
        ]
        
        total_lines = source_str.count("\n") + 1
        has_module_comment = self._has_module_comment(root, source)

        return {
            "total_lines": total_lines,
            "content_hash": content_hash,
            "symbols": symbols,
            "imports": imports,
            "raw_calls": raw_calls,
            "has_module_comment": has_module_comment,
            "language": self.language_id,
        }

    # --------------------------------------------------------------------
    # 子类需实现的方法
    # --------------------------------------------------------------------

    def _extract_symbols(self, root, source: bytes, source_str: str,
                         module_path: str) -> List[Dict[str, Any]]:
        """提取符号列表（函数、类、结构体等）"""
        raise NotImplementedError

    def _extract_imports(self, root, source: bytes) -> List[Dict[str, Any]]:
        """提取 import/use 语句"""
        raise NotImplementedError

    def _extract_raw_calls(self, root, source: bytes,
                           module_path: str) -> List[Dict[str, Any]]:
        """提取原始调用关系"""
        raise NotImplementedError

    def _has_module_comment(self, root, source: bytes) -> bool:
        """检测是否有模块级注释/文件级注释"""
        raise NotImplementedError

    # --------------------------------------------------------------------
    # 通用工具方法
    # --------------------------------------------------------------------

    def _node_text(self, node, source: bytes) -> str:
        """获取节点的文本内容"""
        return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace")

    def _node_text_str(self, node, source_str: str) -> str:
        """从字符串获取节点文本"""
        return source_str[node.start_byte:node.end_byte]

    def _find_prev_sibling_comment(self, node, source: bytes) -> str:
        """查找节点前面的注释（向前遍历兄弟节点）"""
        comment_parts = []
        prev = node.prev_named_sibling
        while prev and prev.type in ("comment", "line_comment", "block_comment"):
            text = self._node_text(prev, source).strip()
            comment_parts.insert(0, text)
            prev = prev.prev_named_sibling
        return "\n".join(comment_parts) if comment_parts else ""
