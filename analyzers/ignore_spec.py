"""
ignore_spec.py
==============

.gitignore / .callwardenignore 规则解析器（支持完整 gitignore 语法）。

A15 修复（2026-07-20 二轮评审）：
- 主路径：接入 pathspec 库（``pathspec.PathSpec.from_lines('gitignore', ...)``
  + ``GitIgnoreBasicPattern``），获得完整 gitignore 语义支持：
  * 字符类 ``[abc]`` / ``[a-z]`` / ``[!abc]``（之前自研实现不支持）
  * 尾随空格语义保留（除非行末 ``\\`` 转义，之前 ``strip()`` 丢失）
  * 目录剪枝后的 negation 恢复（pathspec 内部完整处理 last-match-wins）
  * 复杂 ``**`` 与 ``/`` 组合场景
- Fallback：pathspec 不可用时降级到自研实现（保留向后兼容）
- 公开 API 完全兼容：``IgnoreRule`` / ``parse_ignore_line`` / ``load_ignore_file``
  / ``IgnoreMatcher.add_default_rules`` / ``IgnoreMatcher.load_workspace_ignores``
  / ``IgnoreMatcher.is_ignored`` / ``IgnoreMatcher.filter_files``

语法支持（pathspec 主路径）：
- 空行 / # 开头：注释，跳过
- ! 前缀：取反（白名单，取消前面的忽略）
- / 前缀：锚定根目录（如 /build 只匹配根目录的 build/）
- / 后缀：只匹配目录（如 build/ 不匹配文件 build）
- 中间 /：完整路径匹配（如 a/b/c）
- 无 /：匹配任意层级的文件名/目录名（如 *.pyc 匹配任意深度的 *.pyc）
- * 通配符：匹配任意字符（不含 /）
- ** 通配符：匹配任意目录层级（含 0 层）
- ? 通配符：匹配单个字符（不含 /）
- 字符类 [abc] / [a-z] / [!abc]：字符集合（自研实现不支持，仅 pathspec 支持）
- 转义：\\# \\! 等转义特殊字符

设计要点：
- 规则按出现顺序应用，后出现的规则可以覆盖先出现的（! 取反）
- 支持多个 .gitignore 文件（不同目录下的 .gitignore 作用范围不同）
- 默认规则（hardcoded ignores）作为"基线"先应用，用户规则可覆盖
"""

from __future__ import annotations

import os
import re
from typing import List, Optional

try:
    import pathspec
    _HAS_PATHSPEC = True
except ImportError:
    _HAS_PATHSPEC = False


# ============================================
# 元数据载体（向后兼容；实际匹配由 pathspec 完成）
# ============================================

class IgnoreRule:
    """单条忽略规则的元数据（用于 source 追溯，实际匹配由 pathspec 完成）

    Attributes:
        pattern: 原始模式字符串（如 "*.pyc" 或 "/build/"）
        negation: 是否取反（! 前缀）
        dir_only: 是否只匹配目录（/ 后缀）
        anchored: 是否锚定根目录（/ 前缀或含 /）
        regex: 编译后的正则表达式（仅 fallback 自研路径使用；pathspec 路径下为 None）
        source: 规则来源（如 ".gitignore" / ".callwardenignore" / "default"）
    """

    __slots__ = ("pattern", "negation", "dir_only", "anchored", "regex", "source")

    def __init__(self, pattern: str, negation: bool, dir_only: bool,
                 anchored: bool, regex: Optional[re.Pattern], source: str):
        """初始化忽略规则

        Args:
            pattern: 原始模式字符串（如 "*.pyc" 或 "/build/"）
            negation: 是否取反（! 前缀）
            dir_only: 是否只匹配目录（/ 后缀）
            anchored: 是否锚定根目录（/ 前缀或含 /）
            regex: 编译后的正则表达式（仅 fallback 路径使用，pathspec 路径下可为 None）
            source: 规则来源（如 ".gitignore" / ".callwardenignore" / "default"）
        """
        self.pattern = pattern
        self.negation = negation
        self.dir_only = dir_only
        self.anchored = anchored
        self.regex = regex
        self.source = source

    def __repr__(self) -> str:
        """返回规则的可读字符串表示，包含来源与前缀标志

        Returns:
            形如 "IgnoreRule(source:/!pattern/)" 的字符串
        """
        sign = "!" if self.negation else ""
        suffix = "/" if self.dir_only else ""
        prefix = "/" if self.anchored else ""
        return f"IgnoreRule({self.source}:{prefix}{sign}{self.pattern}{suffix})"


# ============================================
# 自研 regex 翻译（fallback 路径，pathspec 不可用时使用）
# 保留以避免环境兼容性问题
# ============================================

def _translate_to_regex(pattern: str, anchored: bool) -> re.Pattern:
    """把 gitignore 模式翻译为正则表达式（fallback 自研实现，不支持字符类）

    Args:
        pattern: 已去除 !、/ 前缀和 / 后缀的纯净模式
        anchored: 是否锚定根目录

    Returns:
        编译后的正则表达式（匹配完整相对路径）
    """
    i = 0
    n = len(pattern)
    regex_parts = []

    if anchored:
        regex_parts.append("^")
    else:
        regex_parts.append("(?:^|/)")

    while i < n:
        c = pattern[i]

        if c == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                if i + 2 < n and pattern[i + 2] == "/":
                    regex_parts.append("(?:.*/)?")
                    i += 3
                    continue
                else:
                    regex_parts.append(".*")
                    i += 2
                    continue
            else:
                regex_parts.append("[^/]*")
                i += 1
                continue

        elif c == "?":
            regex_parts.append("[^/]")
            i += 1

        elif c == "/":
            regex_parts.append("/")
            i += 1

        elif c == "\\":
            if i + 1 < n:
                regex_parts.append(re.escape(pattern[i + 1]))
                i += 2
            else:
                regex_parts.append(re.escape("\\"))
                i += 1

        else:
            regex_parts.append(re.escape(c))
            i += 1

    regex_parts.append("(?:$|/)")
    pattern_str = "".join(regex_parts)
    return re.compile(pattern_str)


def parse_ignore_line(line: str, source: str = ".gitignore") -> Optional[IgnoreRule]:
    """解析单行 .gitignore 规则（用于元数据生成）

    A15 修复（2026-07-20）：保留尾随空格语义（除非行末 ``\\`` 转义）。
    pathspec 主路径下，本函数返回的 IgnoreRule 仅用于 source 追溯，
    实际匹配由 pathspec 完成（pathspec 自己解析模式字符串）。

    Args:
        line: 单行文本（含前后空白）
        source: 规则来源文件名

    Returns:
        IgnoreRule 实例，空行/注释返回 None
    """
    # 去除行尾换行符（保留尾随空格语义）
    line = line.rstrip("\r\n")

    # A15 修复：检查行末是否以反斜杠转义（保留尾随空格）
    # 不以 \ 结尾时，去除尾随空格（gitignore 默认语义）
    if line.endswith("\\"):
        # 行末 \ 表示保留尾随空格，去除 \ 本身
        line = line[:-1]
    else:
        line = line.rstrip()

    stripped = line.strip()

    if not stripped or stripped.startswith("#"):
        return None

    if stripped.startswith("\\#"):
        stripped = stripped[1:]
    elif stripped.startswith("\\!"):
        stripped = stripped[1:]

    negation = False
    if stripped.startswith("!"):
        negation = True
        stripped = stripped[1:]
        if not stripped:
            return None

    anchored = False
    if stripped.startswith("/"):
        anchored = True
        stripped = stripped[1:]
        if not stripped:
            return None

    dir_only = False
    if stripped.endswith("/"):
        dir_only = True
        stripped = stripped.rstrip("/")
        if not stripped:
            return None

    if "/" in stripped:
        anchored = True

    # regex 仅在 fallback 自研路径下使用；pathspec 路径下不使用
    regex = _translate_to_regex(stripped, anchored) if not _HAS_PATHSPEC else None

    return IgnoreRule(
        pattern=stripped,
        negation=negation,
        dir_only=dir_only,
        anchored=anchored,
        regex=regex,
        source=source,
    )


def load_ignore_file(file_path: str, source: str = "") -> List[IgnoreRule]:
    """加载单个 ignore 文件的所有规则（元数据）

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


def _load_ignore_lines(file_path: str) -> List[str]:
    """加载 ignore 文件的原始行（保留尾随空格，仅去除换行符）

    用于 pathspec.PathSpec.from_lines 输入。pathspec 内部完整 gitignore
    语义解析（识别 \\ 转义、尾随空格、字符类等）。

    Args:
        file_path: .gitignore / .callwardenignore 文件路径

    Returns:
        原始行列表（不含换行符，保留尾随空格）
    """
    if not os.path.isfile(file_path):
        return []
    try:
        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            # splitlines 会去除 \r\n 和 \n，但保留行内尾随空格
            return f.read().splitlines()
    except Exception:
        return []


# ============================================
# 主匹配器（pathspec 主路径 + fallback 自研）
# ============================================

class IgnoreMatcher:
    """忽略规则匹配器

    A15 修复（2026-07-20 二轮评审）：接入 pathspec 库作为主路径，获得
    完整 gitignore 语义支持。pathspec 不可用时降级到自研实现。

    合并多个来源的规则，按顺序应用：
    1. 默认硬编码规则（VCS/构建输出/autogen）
    2. workspace 根目录的 .gitignore
    3. workspace 根目录的 .callwardenignore
    4. 各子目录的 .gitignore（按路径深度应用）

    规则应用顺序：后出现的规则覆盖先出现的（! 取反）。
    """

    def __init__(self, workspace_root: str):
        """初始化忽略规则匹配器，设置工作区根目录

        Args:
            workspace_root: 工作区根目录路径（相对或绝对均可）
        """
        self.workspace_root = os.path.abspath(workspace_root)
        # 全局规则（根目录的 .gitignore + .callwardenignore + 默认规则）
        self.global_rules: List[IgnoreRule] = []
        # 子目录规则：{目录相对路径: [规则列表]}
        # 当文件在子目录下时，需要应用该目录及所有祖先目录的 .gitignore
        self.dir_rules: dict[str, List[IgnoreRule]] = {}

        # pathspec 主路径：全局 spec + 子目录 spec 字典
        self._global_spec = pathspec.PathSpec([]) if _HAS_PATHSPEC else None
        self._dir_specs: dict[str, "pathspec.PathSpec"] = {}

    def add_default_rules(self, rules: List[str]) -> None:
        """添加默认硬编码规则（如 .git/, node_modules/ 等）

        Args:
            rules: 规则字符串列表（如 [".git/", "node_modules/", "*.pyc"]）
        """
        if not rules:
            return
        # 元数据（用于 source 追溯）
        for r in rules:
            rule = parse_ignore_line(r, source="default")
            if rule:
                self.global_rules.append(rule)
        # pathspec 主路径
        if _HAS_PATHSPEC:
            new_spec = pathspec.PathSpec.from_lines('gitignore', rules)
            self._global_spec = self._global_spec + new_spec

    def load_workspace_ignores(self) -> None:
        """加载 workspace 根目录的 .gitignore 和 .callwardenignore

        同时递归扫描所有子目录的 .gitignore，建立 dir_rules 索引。
        子目录 .gitignore 的作用范围仅限于该目录及其子目录。
        """
        root_gitignore = os.path.join(self.workspace_root, ".gitignore")
        root_callwardenignore = os.path.join(self.workspace_root, ".callwardenignore")

        # 元数据
        self.global_rules.extend(load_ignore_file(root_gitignore, ".gitignore"))
        self.global_rules.extend(load_ignore_file(root_callwardenignore, ".callwardenignore"))
        # pathspec 主路径
        if _HAS_PATHSPEC:
            for path in (root_gitignore, root_callwardenignore):
                lines = _load_ignore_lines(path)
                if lines:
                    spec = pathspec.PathSpec.from_lines('gitignore', lines)
                    self._global_spec = self._global_spec + spec

        # 递归扫描子目录的 .gitignore
        skip_dirs = {
            ".git", ".repo", "node_modules", "target", "dist", "build",
            ".next", "__pycache__", ".venv", "venv", "env", ".tox",
            "out", "output", "obj", "bin", "rootfs", "staging", "sysroot",
            "ccache", "prebuilt", "prebuilts", "blob", "toolchain",
        }

        for root, dirs, files in os.walk(self.workspace_root):
            abs_dir = os.path.abspath(root)
            rel_dir = "" if abs_dir == self.workspace_root else (
                os.path.relpath(abs_dir, self.workspace_root).replace("\\", "/")
            )

            if rel_dir and ".gitignore" in files:
                gitignore_path = os.path.join(abs_dir, ".gitignore")
                # 元数据
                rules = load_ignore_file(gitignore_path, f".gitignore:{rel_dir}")
                if rules:
                    self.dir_rules[rel_dir] = rules
                # pathspec 主路径
                if _HAS_PATHSPEC:
                    lines = _load_ignore_lines(gitignore_path)
                    if lines:
                        self._dir_specs[rel_dir] = pathspec.PathSpec.from_lines(
                            'gitignore', lines
                        )

            kept_dirs = []
            for dirname in dirs:
                if dirname in skip_dirs or dirname.startswith("."):
                    continue
                child_rel = f"{rel_dir}/{dirname}" if rel_dir else dirname
                if self.is_ignored(child_rel, is_dir=True):
                    continue
                kept_dirs.append(dirname)
            dirs[:] = kept_dirs

    def is_ignored(self, rel_path: str, is_dir: bool = False) -> bool:
        """判断相对路径是否被忽略

        应用规则顺序（后者覆盖前者）：
        1. 默认硬编码规则
        2. 根目录 .gitignore + .callwardenignore
        3. 路径所属目录及祖先目录的 .gitignore

        A15 修复（2026-07-20）：
        - pathspec 主路径：用 ``PathSpec.match_file`` 完成完整 gitignore 语义
        - 子目录 spec 覆盖：迭代 spec.patterns 判断是否有匹配，区分"未匹配"
          与"被 negation 覆盖"
        - Fallback 自研路径：保留原逻辑（不完整，但向后兼容）

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

        # pathspec 主路径
        if _HAS_PATHSPEC:
            return self._is_ignored_pathspec(rel_path, is_dir)
        # fallback 自研路径
        return self._is_ignored_legacy(rel_path, is_dir)

    def _is_ignored_pathspec(self, rel_path: str, is_dir: bool) -> bool:
        """pathspec 主路径：完整 gitignore 语义匹配。

        处理子目录 .gitignore 作用范围：
        - 全局 spec 决定基线（默认 + 根 .gitignore + .callwardenignore）
        - 各祖先目录的 spec 覆盖（子目录规则覆盖父目录规则）
        - 区分 "未匹配" 与 "被 negation 覆盖"：
          pathspec.PathSpec.match_file 返回 False 可能是两种情况：
          (a) 未匹配任何规则 → 应保持父级结果
          (b) 最后匹配的规则是 negation → 应覆盖为 False
          通过迭代 spec.patterns 判断是否有匹配规则，决定是否覆盖
        - is_dir=True 时路径末尾加 ``/``：让 pathspec 正确匹配 dir_only 规则
          （pathspec 的 GitIgnoreBasicPattern 不带 is_dir 参数，靠路径
          末尾 ``/`` 区分目录/文件）
        """
        # is_dir=True 时路径末尾加 /，让 dir_only 规则（如 build/）能匹配
        match_path = rel_path + "/" if is_dir else rel_path

        # 全局 spec 决定基线
        ignored = self._global_spec.match_file(match_path)
        # 对非目录路径，也尝试无尾 / 的匹配（防止 dir_only 规则对文件漏判）
        if not is_dir and not ignored:
            # 检查是否有 dir_only 规则匹配该文件所在目录
            # 例如 build/ 应当让 build/foo 也被忽略
            # pathspec 内部已处理这种情况（GitIgnoreBasicPattern 会匹配
            # 父目录前缀），所以这里不需要额外逻辑
            pass

        # 子目录 spec 覆盖（从浅到深）
        path_parts = rel_path.split("/")
        for i in range(1, len(path_parts)):
            ancestor = "/".join(path_parts[:i])
            if ancestor not in self._dir_specs:
                continue
            spec = self._dir_specs[ancestor]
            scoped = "/".join(path_parts[i:])
            if not scoped:
                continue
            scoped_match = scoped + "/" if is_dir else scoped

            # 迭代 patterns 判断是否有匹配
            # GitIgnoreBasicPattern.include: True=normal / False=negation
            # GitIgnoreBasicPattern.match_file(path) → RegexMatchResult 或 None
            matched_any = False
            sub_ignored = False
            for pat in spec.patterns:
                match = pat.match_file(scoped_match)
                if match is not None:
                    matched_any = True
                    # include=True → 命中则忽略；include=False → 命中则取消忽略
                    sub_ignored = bool(pat.include)
            if matched_any:
                # 子目录规则匹配，覆盖父级结果
                ignored = sub_ignored

        return ignored

    def _is_ignored_legacy(self, rel_path: str, is_dir: bool) -> bool:
        """fallback 自研路径（pathspec 不可用时使用）

        保留原逻辑以维持环境兼容性。已知不完整：
        - 不支持字符类 [abc]/[a-z]/[!abc]
        - strip() 在 parse_ignore_line 中已修复，但目录剪枝后的 negation
          恢复仍是 last-match-wins，可能不符合 gitignore 完整语义
        """
        ignored = False
        for rule in self.global_rules:
            if rule.regex is None:
                continue
            if rule.dir_only and not is_dir:
                if rule.regex.search(rel_path):
                    ignored = not rule.negation
                continue
            if rule.regex.search(rel_path):
                ignored = not rule.negation

        path_parts = rel_path.split("/")
        for i in range(1, len(path_parts)):
            ancestor_dir = "/".join(path_parts[:i])
            if ancestor_dir not in self.dir_rules:
                continue
            scoped_path = "/".join(path_parts[i:])
            for rule in self.dir_rules[ancestor_dir]:
                if rule.regex is None:
                    continue
                if rule.dir_only and not is_dir:
                    if rule.regex.search(scoped_path):
                        ignored = not rule.negation
                    continue
                if rule.regex.search(scoped_path):
                    ignored = not rule.negation

        return ignored

    def filter_files(self, rel_paths: List[str]) -> List[str]:
        """批量过滤，返回未被忽略的文件列表"""
        return [p for p in rel_paths if not self.is_ignored(p, is_dir=False)]
