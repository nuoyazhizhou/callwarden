"""
base.py
=======

代码解析器基类：定义所有语言解析器的统一接口。

各语言解析器需继承 BaseParser 并实现 parse_file 方法，
返回统一格式的解析结果，供上层 db.py 和调用链分析使用。

支持 AST 级增量解析（v28 起）：基于 tree-sitter 增量解析能力，
仅重新解析变更区域，大文件性能可提升 10 倍。
"""

from __future__ import annotations

import difflib
from typing import Any, Dict, List, Optional, Tuple

# R2-P0-3: tree_sitter 延迟导入 — local wheel / frozen bundle 不安装
# parser-reference extra（tree-sitter 核心 + 16 grammar）时，import callwarden
# 不应在模块加载阶段失败。Language/Parser 仅在 BaseParser.__init__ 实例化时
# 需要（生产路径走 RustParserFacade，不实例化 Python parser）。
# 将导入推迟到 __init__ 内，让 base.py 可在无 tree_sitter 环境下安全 import。

from ..config import read_file_normalized
from .call_filter import should_filter_call


class BaseParser:
    """代码解析器基类

    所有语言解析器的统一接口，定义：
    - 统一的 parse_file 方法签名
    - 统一的返回格式
    - 通用的工具方法（节点文本提取、注释检测等）
    - AST 级增量解析：维护 in-memory tree cache，文件未变化时跳过解析

    tree-sitter Tree 对象与 parser 进程绑定，无法跨进程序列化。
    因此 AST 增量缓存分为两层：
    - 内存层（self._tree_cache）：Tree 对象，进程内复用，零拷贝
    - 持久层（db.file_versions.ast_cache）：存储 content_hash + 元数据，
      用于跨进程判断是否需要全量解析（如内容未变则直接复用解析结果）
    """

    language_id: str = ""  # 语言标识，如 "rust", "typescript"
    language_module = None  # tree-sitter 语言模块

    def __init__(self):
        """初始化解析器，基于子类提供的 language_module 构造 Language 与 Parser。

        子类需在类属性中设置 language_module（tree-sitter 语言模块），
        本方法通过其 language() 入口构造 Language 实例并初始化 Parser。

        Raises:
            NotImplementedError: 当子类未设置 language_module 时抛出。
        """
        if self.language_module is None:
            raise NotImplementedError("子类必须设置 language_module")
        # R2-P0-3: tree_sitter 延迟导入 — 仅在实例化 Python parser 时加载
        from tree_sitter import Language, Parser

        self.language = Language(self.language_module.language())
        self.parser = Parser(self.language)
        # AST 增量缓存：{file_path: (Tree, content_hash)}
        # - key: 文件绝对路径
        # - value: (tree-sitter Tree, 上次解析的 content_hash)
        # 当 file_path 再次解析且 content_hash 未变时，直接复用 Tree 跳过解析
        # 注意：使用 _ensure_tree_cache() 访问，兼容子类未调用 super().__init__() 的情况
        self._tree_cache: Dict[str, Tuple[Any, str]] = {}

    # --------------------------------------------------------------------
    # 公共接口
    # --------------------------------------------------------------------

    def _ensure_tree_cache(self) -> Dict[str, Tuple[Any, str]]:
        """获取 tree 缓存字典，若未初始化则初始化

        兼容子类（如 PhpParser）重写 __init__ 但未调用 super().__init__() 的情况。
        """
        cache = getattr(self, "_tree_cache", None)
        if cache is None:
            cache = {}
            self._tree_cache = cache
        return cache

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
                "incremental": bool,      # 是否走了增量解析路径
                "changed_ranges": list,   # 增量解析的变更区间（全量为空）
            }
        """
        source_str, content_hash = read_file_normalized(file_path)
        source = source_str.encode("utf-8")

        # AST 增量解析：尝试复用缓存 tree
        tree_cache = self._ensure_tree_cache()
        cached = tree_cache.get(file_path)
        incremental = False
        changed_ranges: List[Dict[str, Any]] = []

        if cached is not None:
            old_tree, old_hash = cached
            if old_hash == content_hash:
                # 内容完全未变化：直接复用 tree，零解析开销
                tree = old_tree
            else:
                # 内容有变化：使用增量解析
                # 1. 计算编辑区间（基于行级 diff）
                old_source = old_tree.root_node.text if hasattr(old_tree.root_node, "text") else None
                if old_source is None:
                    # 老版本 tree-sitter 没有 .text 属性，降级为全量
                    tree = self.parser.parse(source)
                else:
                    try:
                        old_source_bytes = old_source if isinstance(old_source, bytes) else old_source.encode("utf-8")
                        edits = self._compute_edits(old_source_bytes, source)
                        tree, changed_ranges = self.parse_incremental(file_path, old_tree, edits)
                        incremental = True
                    except Exception:
                        # 增量解析失败，降级为全量解析
                        tree = self.parser.parse(source)
        else:
            # 无缓存：全量解析
            tree = self.parser.parse(source)

        # 更新缓存
        tree_cache[file_path] = (tree, content_hash)

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
            "incremental": incremental,
            "changed_ranges": changed_ranges,
        }

    def parse_incremental(
        self,
        file_path: str,
        old_tree: Any,
        edits: List[Dict[str, int]],
    ) -> Tuple[Any, List[Dict[str, Any]]]:
        """增量解析：基于旧 tree + 编辑信息解析新内容

        使用 tree-sitter 增量解析 API：
        1. 对 old_tree 调用 tree.edit()，告知编辑区间
        2. parser.parse(new_source, old_tree) 复用旧 tree 仅解析变更区域
        3. tree.changed_ranges(new_tree) 计算实际变化的节点区间

        Args:
            file_path: 文件路径（用于错误信息，不参与解析）
            old_tree: 上一次解析的 tree-sitter Tree
            edits: 编辑信息列表，每个元素：
                {
                    "start_byte": int, "old_end_byte": int, "new_end_byte": int,
                    "start_row": int, "start_col": int,
                    "old_end_row": int, "old_end_col": int,
                    "new_end_row": int, "new_end_col": int,
                }
                区间语义：[start_byte, old_end_byte) 是旧内容的删除区间，
                [start_byte, new_end_byte) 是新内容的插入区间。

        Returns:
            (new_tree, changed_ranges):
                new_tree: 新的 tree-sitter Tree（已更新缓存）
                changed_ranges: 变化的区间列表，每个元素：
                    {
                        "start_row": int, "start_col": int,
                        "end_row": int, "end_col": int,
                        "start_byte": int, "end_byte": int,
                    }

        Raises:
            ValueError: 当 edits 为空时
        """
        if not edits:
            raise ValueError("edits 不能为空，无编辑信息应直接复用 old_tree")

        # 1. 对 old_tree 应用编辑信息（更新 tree 的内部状态以匹配编辑后的内容）
        for edit in edits:
            old_tree.edit(
                start_byte=edit["start_byte"],
                old_end_byte=edit["old_end_byte"],
                new_end_byte=edit["new_end_byte"],
                start_row=edit["start_row"],
                start_col=edit["start_col"],
                old_end_row=edit["old_end_row"],
                old_end_col=edit["old_end_col"],
                new_end_row=edit["new_end_row"],
                new_end_col=edit["new_end_col"],
            )

        # 2. 读取新内容并增量解析
        source_str, _ = read_file_normalized(file_path)
        source = source_str.encode("utf-8")
        new_tree = self.parser.parse(source, old_tree)

        # 3. 计算实际变化的节点区间
        changed_ranges: List[Dict[str, Any]] = []
        try:
            ranges = new_tree.changed_ranges(old_tree)
            for r in ranges:
                changed_ranges.append({
                    "start_row": r.start_point[0],
                    "start_col": r.start_point[1],
                    "end_row": r.end_point[0],
                    "end_col": r.end_point[1],
                    "start_byte": r.start_byte,
                    "end_byte": r.end_byte,
                })
        except Exception:
            # 某些 tree-sitter 版本不支持 changed_ranges，降级为空列表
            pass

        return new_tree, changed_ranges

    # --------------------------------------------------------------------
    # AST 缓存管理
    # --------------------------------------------------------------------

    def get_cached_tree(self, file_path: str) -> Optional[Tuple[Any, str]]:
        """获取文件路径对应的缓存 tree

        Returns:
            (Tree, content_hash) 或 None（未缓存）
        """
        return self._ensure_tree_cache().get(file_path)

    def invalidate_tree_cache(self, file_path: str) -> None:
        """失效指定文件的 tree 缓存（文件被删除或路径变化时调用）"""
        self._ensure_tree_cache().pop(file_path, None)

    def clear_tree_cache(self) -> None:
        """清空所有 tree 缓存（进程退出或全量刷新时调用）"""
        self._ensure_tree_cache().clear()

    def _compute_edits(
        self,
        old_source: bytes,
        new_source: bytes,
    ) -> List[Dict[str, int]]:
        """基于行级 diff 计算编辑区间（供 parse_incremental 使用）

        使用 difflib.SequenceMatcher 对行进行匹配，找出增加/删除/替换的行块，
        转换为 tree-sitter 增量解析所需的字节级编辑区间。

        Args:
            old_source: 旧文件内容（bytes）
            new_source: 新文件内容（bytes）

        Returns:
            编辑信息列表，按 start_byte 升序排列
        """
        old_lines = old_source.split(b"\n")
        new_lines = new_source.split(b"\n")

        matcher = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
        edits: List[Dict[str, int]] = []

        for tag, i1, i2, j1, j2 in matcher.get_opcodes():
            if tag == "equal":
                continue

            # 计算旧内容区间 [start_byte, old_end_byte)
            start_byte = sum(len(l) + 1 for l in old_lines[:i1])  # +1 for \n
            old_end_byte = sum(len(l) + 1 for l in old_lines[:i2])

            # 计算新内容区间 [start_byte, new_end_byte)
            new_end_byte = sum(len(l) + 1 for l in new_lines[:j2])

            # 行列信息（基于 0-indexed）
            start_row = i1
            start_col = 0
            old_end_row = i2 - 1 if i2 > i1 else i1
            old_end_col = len(old_lines[i2 - 1]) if i2 > i1 and i2 - 1 < len(old_lines) else 0
            new_end_row = j2 - 1 if j2 > j1 else j1
            new_end_col = len(new_lines[j2 - 1]) if j2 > j1 and j2 - 1 < len(new_lines) else 0

            edits.append({
                "start_byte": start_byte,
                "old_end_byte": old_end_byte,
                "new_end_byte": new_end_byte,
                "start_row": start_row,
                "start_col": start_col,
                "old_end_row": old_end_row,
                "old_end_col": old_end_col,
                "new_end_row": new_end_row,
                "new_end_col": new_end_col,
            })

        return edits

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
