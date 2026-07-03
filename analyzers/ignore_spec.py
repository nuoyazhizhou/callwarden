"""
ignore_spec.py
==============

.gitignore / .callwardenignore 规则解析器（支持完整 gitignore 语法）。

语法支持：
- 空行 / # 开头：注释，跳过
- ! 前缀：取反（白名单，取消前面的忽略）
- / 前缀：锚定根目录（如 /build 只匹配根目录的 build/）
- / 后缀：只匹配目录（如 build/ 不匹配文件 build）
- 中间 /：完整路径匹配（如 a/b/c）
- 无 /：匹配任意层级的文件名/目录名（如 *.pyc 匹配任意深度的 *.pyc）
- * 通配符：匹配任意字符（不含 /）
- ** 通配符：匹配任意目录层级（含 0 层）
- 转义：\\# \\! 等转义特殊字符

设计要点：
- 规则按出现顺序应用，后出现的规则可以覆盖先出现的（! 取反）
- 支持多个 .gitignore 文件（不同目录下的 .gitignore 作用范围不同）
- 默认规则（hardcoded ignores）作为"基线"先应用，用户规则可覆盖
"""

from __future__ import annotations

import os
import re
from typing import List, Tuple, Optional


class IgnoreRule:
    """单条忽略规则（已编译为正则）

    Attributes:
        pattern: 原始模式字符串（如 "*.pyc" 或 "/build/"）
        negation: 是否取反（! 前缀）
        dir_only: 是否只匹配目录（/ 后缀）
        anchored: 是否锚定根目录（/ 前缀或含 /）
        regex: 编译后的正则表达式
        source: 规则来源（如 ".gitignore" / ".callwardenignore" / "default"）
    """

    __slots__ = ("pattern", "negation", "dir_only", "anchored", "regex", "source")

    def __init__(self, pattern: str, negation: bool, dir_only: bool,
                 anchored: bool, regex: re.Pattern, source: str):
        self.pattern = pattern
        self.negation = negation
        self.dir_only = dir_only
        self.anchored = anchored
        self.regex = regex
        self.source = source

    def __repr__(self) -> str:
        sign = "!" if self.negation else ""
        suffix = "/" if self.dir_only else ""
        prefix = "/" if self.anchored else ""
        return f"IgnoreRule({self.source}:{prefix}{sign}{self.pattern}{suffix})"


def _translate_to_regex(pattern: str, anchored: bool) -> re.Pattern:
    """把 gitignore 模式翻译为正则表达式

    Args:
        pattern: 已去除 !、/ 前缀和 / 后缀的纯净模式
        anchored: 是否锚定根目录

    Returns:
        编译后的正则表达式（匹配完整相对路径）
    """
    # 处理 ** 通配符（必须先于 * 处理）
    # **/ 匹配任意目录层级（含 0 层），/** 匹配任意路径后缀
    # 独立的 ** 等价于 .*（匹配任意字符含 /）

    i = 0
    n = len(pattern)
    regex_parts = []

    if anchored:
        # 锚定根目录：从路径开头开始匹配
        regex_parts.append("^")
    else:
        # 不锚定：允许从任意目录开始（用 (?:^|/) 匹配路径分隔符或开头）
        regex_parts.append("(?:^|/)")

    while i < n:
        c = pattern[i]

        if c == "*":
            # 检查是否是 ** 通配符
            if i + 1 < n and pattern[i + 1] == "*":
                # ** 通配符
                # **/ 匹配任意目录层级（含 0 层），等价于 (?:.*/)? 
                # 独立 ** 匹配任意字符含 /
                if i + 2 < n and pattern[i + 2] == "/":
                    # **/ 的情况：匹配任意目录层级
                    regex_parts.append("(?:.*/)?")
                    i += 3
                    continue
                else:
                    # 独立 ** 的情况：匹配任意字符（含 /）
                    regex_parts.append(".*")
                    i += 2
                    continue
            else:
                # 单 * 通配符：匹配任意字符（不含 /）
                regex_parts.append("[^/]*")
                i += 1
                continue

        elif c == "?":
            # ? 匹配单个字符（不含 /）
            regex_parts.append("[^/]")
            i += 1

        elif c == "/":
            # 路径分隔符
            regex_parts.append("/")
            i += 1

        elif c == "\\":
            # 转义字符：下一个字符按字面值处理
            if i + 1 < n:
                regex_parts.append(re.escape(pattern[i + 1]))
                i += 2
            else:
                # 行尾的反斜杠按字面值处理
                regex_parts.append(re.escape("\\"))
                i += 1

        else:
            # 普通字符：转义后追加
            regex_parts.append(re.escape(c))
            i += 1

    # 匹配路径或路径前缀（目录后缀已在外部处理）
    # 例如模式 "build" 应匹配 "build" 和 "build/anything"
    regex_parts.append("(?:$|/)")

    pattern_str = "".join(regex_parts)
    return re.compile(pattern_str)


def parse_ignore_line(line: str, source: str = ".gitignore") -> Optional[IgnoreRule]:
    """解析单行 .gitignore 规则

    Args:
        line: 单行文本（含前后空白）
        source: 规则来源文件名

    Returns:
        IgnoreRule 实例，空行/注释返回 None
    """
    # 去除行尾换行符（已 strip 过，但保险）
    line = line.rstrip("\r\n")

    # 去除行内尾部空格（gitignore 不忽略尾部空格前的内容，但空行不算规则）
    stripped = line.strip()

    # 空行或注释：跳过
    if not stripped or stripped.startswith("#"):
        return None

    # 处理转义的 # 和 ! （\\# \\!）
    if stripped.startswith("\\#"):
        stripped = stripped[1:]
    elif stripped.startswith("\\!"):
        stripped = stripped[1:]

    # 取反标志
    negation = False
    if stripped.startswith("!"):
        negation = True
        stripped = stripped[1:]
        # 取反后内容为空，跳过
        if not stripped:
            return None

    # 锚定根目录标志
    anchored = False
    if stripped.startswith("/"):
        anchored = True
        stripped = stripped[1:]
        # 仅 / 是无效规则
        if not stripped:
            return None

    # 只匹配目录标志
    dir_only = False
    if stripped.endswith("/"):
        dir_only = True
        stripped = stripped.rstrip("/")
        if not stripped:
            return None

    # 含 / 但不是结尾的 /：完整路径匹配，自动锚定根目录
    if "/" in stripped:
        anchored = True

    # 编译为正则
    regex = _translate_to_regex(stripped, anchored)

    return IgnoreRule(
        pattern=stripped,
        negation=negation,
        dir_only=dir_only,
        anchored=anchored,
        regex=regex,
        source=source,
    )


def load_ignore_file(file_path: str, source: str = "") -> List[IgnoreRule]:
    """加载单个 ignore 文件的所有规则

    Args:
        file_path: .gitignore / .callwardenignore 文件路径
        source: 规则来源标识（为空则用文件名）

    Returns:
        规则列表（按文件中的出现顺序）
    """
    if not source:
        source = os.path.basename(file_path)

    if not os.path.isfile(file_path):
        return []

    rules: List[IgnoreRule] = []
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                rule = parse_ignore_line(line, source)
                if rule:
                    rules.append(rule)
    except Exception:
        pass

    return rules


class IgnoreMatcher:
    """忽略规则匹配器

    合并多个来源的规则，按顺序应用：
    1. 默认硬编码规则（VCS/构建输出/autogen）
    2. workspace 根目录的 .gitignore
    3. workspace 根目录的 .callwardenignore
    4. 各子目录的 .gitignore（按路径深度应用）

    规则应用顺序：后出现的规则覆盖先出现的（! 取反）。
    """

    def __init__(self, workspace_root: str):
        self.workspace_root = os.path.abspath(workspace_root)
        # 全局规则（根目录的 .gitignore + .callwardenignore + 默认规则）
        self.global_rules: List[IgnoreRule] = []
        # 子目录规则：{目录相对路径: [规则列表]}
        # 当文件在子目录下时，需要应用该目录及所有祖先目录的 .gitignore
        self.dir_rules: dict[str, List[IgnoreRule]] = {}

    def add_default_rules(self, rules: List[str]) -> None:
        """添加默认硬编码规则（如 .git/, node_modules/ 等）

        Args:
            rules: 规则字符串列表（如 [".git/", "node_modules/", "*.pyc"]）
        """
        for r in rules:
            rule = parse_ignore_line(r, source="default")
            if rule:
                self.global_rules.append(rule)

    def load_workspace_ignores(self) -> None:
        """加载 workspace 根目录的 .gitignore 和 .callwardenignore

        同时递归扫描所有子目录的 .gitignore，建立 dir_rules 索引。
        子目录 .gitignore 的作用范围仅限于该目录及其子目录。
        """
        # 根目录规则
        root_gitignore = os.path.join(self.workspace_root, ".gitignore")
        root_callwardenignore = os.path.join(self.workspace_root, ".callwardenignore")

        self.global_rules.extend(load_ignore_file(root_gitignore, ".gitignore"))
        self.global_rules.extend(load_ignore_file(root_callwardenignore, ".callwardenignore"))

        # 递归扫描子目录的 .gitignore
        # 跳过明显的非源码目录以加速扫描
        skip_dirs = {
            ".git", ".repo", "node_modules", "target", "dist", "build",
            ".next", "__pycache__", ".venv", "venv", "env", ".tox",
            "out", "output", "obj", "bin", "rootfs", "staging", "sysroot",
            "ccache", "prebuilt", "prebuilts", "blob", "toolchain",
        }

        for root, dirs, files in os.walk(self.workspace_root):
            # 原地修改 dirs 跳过非源码目录
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]

            if ".gitignore" not in files:
                continue

            # 计算相对目录路径
            abs_dir = os.path.abspath(root)
            if abs_dir == self.workspace_root:
                continue  # 根目录已处理

            rel_dir = os.path.relpath(abs_dir, self.workspace_root).replace("\\", "/")
            gitignore_path = os.path.join(abs_dir, ".gitignore")
            rules = load_ignore_file(gitignore_path, f".gitignore:{rel_dir}")
            if rules:
                self.dir_rules[rel_dir] = rules

    def is_ignored(self, rel_path: str, is_dir: bool = False) -> bool:
        """判断相对路径是否被忽略

        应用规则顺序（后者覆盖前者）：
        1. 默认硬编码规则
        2. 根目录 .gitignore + .callwardenignore
        3. 路径所属目录及祖先目录的 .gitignore

        Args:
            rel_path: 相对 workspace_root 的路径（用 / 分隔符）
            is_dir: 是否是目录

        Returns:
            True 表示应忽略
        """
        # 标准化路径（统一 / 分隔符，去除前导 ./）
        rel_path = rel_path.replace("\\", "/").lstrip("./")
        if not rel_path:
            return False

        # 应用全局规则（默认 + 根目录）
        ignored = False
        for rule in self.global_rules:
            # dir_only 规则只对目录生效
            if rule.dir_only and not is_dir:
                # 但如果文件路径以 dir_only 模式为前缀，也应被忽略
                # 例如规则 "build/" 应该让 "build/main.o" 也被忽略
                # 检查：去掉末尾 / 后的正则是否匹配路径前缀
                # 由于 _translate_to_regex 已加 (?:$|/)，会匹配 "build/" 前缀
                if rule.regex.search(rel_path):
                    ignored = not rule.negation
                continue

            if rule.regex.search(rel_path):
                ignored = not rule.negation

        # 应用子目录 .gitignore 规则（按路径深度从浅到深）
        # 找到所有祖先目录的 .gitignore
        path_parts = rel_path.split("/")
        for i in range(1, len(path_parts)):
            ancestor_dir = "/".join(path_parts[:i])
            if ancestor_dir not in self.dir_rules:
                continue

            for rule in self.dir_rules[ancestor_dir]:
                # 子目录规则中的路径是相对该子目录的
                # 但我们存储时是相对 workspace_root 的，所以直接匹配完整路径
                # 这里需要把规则模式重新匹配相对子目录的路径
                # 简化处理：直接匹配完整路径（已锚定到子目录前缀）
                if rule.dir_only and not is_dir:
                    if rule.regex.search(rel_path):
                        ignored = not rule.negation
                    continue

                if rule.regex.search(rel_path):
                    ignored = not rule.negation

        return ignored

    def filter_files(self, rel_paths: List[str]) -> List[str]:
        """批量过滤，返回未被忽略的文件列表"""
        return [p for p in rel_paths if not self.is_ignored(p, is_dir=False)]
