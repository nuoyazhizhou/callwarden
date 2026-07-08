"""
config.py
=========

代码知识图谱配置：路径常量、工具函数、多语言配置。
"""

import codecs
import hashlib
import os
import tempfile
from typing import Dict, List, Optional, Tuple

# 路径常量
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGES_DIR = os.path.dirname(SCRIPT_DIR)  # scripts/
SRC_DIR = os.path.join(PACKAGES_DIR, "..", "src")

# 数据库根目录：用户主目录下的 .callwarden/
USER_HOME = os.path.expanduser("~")
CALLWARDEN_DIR = os.path.join(USER_HOME, ".callwarden")

# 向后兼容的默认 DB_PATH（推荐使用 get_project_db_path 按项目隔离）
DB_PATH = os.path.join(CALLWARDEN_DIR, "callwarden.db")


def get_project_db_path(project_root: str) -> str:
    """根据项目根路径生成项目级数据库路径（一个项目一个 SQLite 数据库）

    路径格式: $HOME/.callwarden/16位hash/callwarden.db

    16 位 hash 是项目根路径绝对路径的 SHA-256 前 16 位，确保不同项目的数据库隔离。
    这样每个数据库只包含一个项目的数据，体积小、查询快、互不干扰。

    Args:
        project_root: 项目根目录路径

    Returns:
        项目级数据库绝对路径
    """
    abs_root = os.path.abspath(project_root)
    # 标准化路径（统一正斜杠，消除跨平台差异）
    norm_root = norm_path(abs_root)
    # 计算项目路径的 SHA-256 前 16 位作为目录名
    path_hash = hashlib.sha256(norm_root.encode("utf-8")).hexdigest()[:16]
    project_dir = os.path.join(CALLWARDEN_DIR, path_hash)
    os.makedirs(project_dir, exist_ok=True)
    return os.path.join(project_dir, "callwarden.db")


def ensure_callwarden_dir() -> str:
    """确保数据库根目录存在，返回目录路径"""
    os.makedirs(CALLWARDEN_DIR, exist_ok=True)
    return CALLWARDEN_DIR


def atomic_write_file(file_path: str, content: str, encoding: str = "utf-8") -> None:
    """原子写入文件：先写临时文件再 rename，避免半写入状态

    SEC-001 安全修复：所有文件写入必须走此函数，确保数据完整性。

    流程：
    1. 确保目标目录存在
    2. 在同目录创建临时文件（保证同一文件系统，rename 原子）
    3. 写入内容并 flush/fsync 确保落盘
    4. os.replace 原子替换目标文件（Windows/Linux 均支持）

    Args:
        file_path: 目标文件绝对路径
        content: 要写入的内容
        encoding: 文件编码（默认 utf-8）

    Raises:
        OSError: 文件写入失败
    """
    # 确保目录存在
    dir_path = os.path.dirname(file_path)
    if dir_path and not os.path.exists(dir_path):
        os.makedirs(dir_path, exist_ok=True)

    # 在同目录创建临时文件（保证同一文件系统，rename 才原子）
    dir_for_tmp = dir_path if dir_path else "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp_write_",
        suffix=".bak",
        dir=dir_for_tmp,
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # 某些文件系统不支持 fsync，忽略错误
                pass
        # 原子替换（Windows/Linux 均支持 os.replace）
        os.replace(tmp_path, file_path)
    except Exception:
        # 写入失败时清理临时文件
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def detect_project_root(start_path: str) -> Optional[str]:
    """从 start_path 向上查找项目根目录（工作区根）

    检测优先级：
    1. .repo/ 目录（repo 管理的多仓库项目，根目录优先级最高）
    2. .git/ 目录（独立 git 仓库）
    3. 项目配置文件（Cargo.toml / package.json / pom.xml / go.mod / CMakeLists.txt / setup.py）
    4. 找不到任何标记 → 返回 None

    Args:
        start_path: 起始路径（文件或目录）

    Returns:
        项目根目录绝对路径，未找到返回 None
    """
    start_path = os.path.abspath(start_path)
    if os.path.isfile(start_path):
        current = os.path.dirname(start_path)
    else:
        current = start_path

    root_markers_dir = [".repo", ".git"]
    root_markers_file = [
        "Cargo.toml", "package.json", "pom.xml", "go.mod",
        "CMakeLists.txt", "setup.py", "pyproject.toml",
        "build.gradle", "build.gradle.kts",
    ]

    while True:
        for marker in root_markers_dir:
            if os.path.isdir(os.path.join(current, marker)):
                return norm_path(current)

        for marker in root_markers_file:
            if os.path.isfile(os.path.join(current, marker)):
                return norm_path(current)

        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    return None


def get_default_workspace_name(root_path: str) -> str:
    """根据根目录路径生成默认工作区名称

    Args:
        root_path: 项目根目录

    Returns:
        工作区名称（取目录名）
    """
    return os.path.basename(os.path.normpath(root_path))


# 子项目识别用的清单文件 → 语言映射（与 detect_project_root 的 root_markers_file 保持一致 + 扩展）
PROJECT_MANIFESTS: Dict[str, str] = {
    "go.mod": "go",
    "Cargo.toml": "rust",
    "package.json": "javascript",
    "pom.xml": "java",
    "pyproject.toml": "python",
    "setup.py": "python",
    "Gemfile": "ruby",
    "composer.json": "php",
    "mix.exs": "elixir",
    "Package.swift": "swift",
    "build.gradle": "java",
    "build.gradle.kts": "kotlin",
    "CMakeLists.txt": "cmake",
}

# 子项目扫描时跳过的目录（第三方库 / VCS / 构建产物）
_SUBPROJECT_SKIP_DIRS = frozenset({
    ".git", "node_modules", "target", "vendor", ".venv", "venv",
    "dist", "build", ".gradle", "__pycache__", ".m2", ".cache",
    ".repo", ".next", "out", "bin", "obj",
})


def scan_subprojects(root_dir: str, max_depth: int = 5) -> List[Dict[str, str]]:
    """向下扫描目录，识别所有子项目根（基于清单文件）

    与 detect_project_root（向上找）互补：本函数向下递归发现子项目。
    用于处理 "一个目录下有多个独立开源项目" 的场景（如 testcode/repos/）。

    算法：
    1. os.walk 递归遍历目录树
    2. 跳过第三方库/VCS/构建产物目录（性能优化）
    3. 当一个目录包含清单文件（go.mod/Cargo.toml/package.json 等）时，
       标记为项目根，记录项目名、语言、清单文件
    4. 识别到项目根后继续扫描子目录（支持 monorepo 嵌套子项目）

    Args:
        root_dir: 扫描根目录
        max_depth: 最大递归深度（相对 root_dir 的目录层数），默认 5

    Returns:
        项目列表，每项含 root/rel_path/name/lang/manifest 字段
    """
    root_dir = os.path.abspath(root_dir)
    projects: List[Dict[str, str]] = []

    # P23.5: onerror 回调，跳过不可访问的目录（如文件锁、权限不足、路径过长）
    def _scan_onerror(err):
        pass

    for root, dirs, files in os.walk(root_dir, onerror=_scan_onerror):
        # 跳过第三方/VCS/构建目录
        dirs[:] = [d for d in dirs if d not in _SUBPROJECT_SKIP_DIRS and not d.startswith(".")]

        # 深度限制
        rel = os.path.relpath(root, root_dir)
        depth = 0 if rel == "." else rel.count(os.sep)
        if depth > max_depth:
            dirs[:] = []
            continue

        # 检查是否是项目根（含清单文件）
        for manifest, lang in PROJECT_MANIFESTS.items():
            if manifest in files:
                rel_path = norm_path(rel) if rel != "." else ""
                projects.append({
                    "root": root,
                    "rel_path": rel_path,
                    "name": os.path.basename(root) if rel != "." else os.path.basename(root_dir),
                    "lang": lang,
                    "manifest": manifest,
                })
                break  # 一个目录只取第一个匹配的清单文件

    return projects


# 多语言配置：统一管理各语言的扩展名、注释符号、入口文件规则
LANGUAGE_CONFIG: Dict[str, Dict] = {
    "rust": {
        "extensions": [".rs"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "///",
        "entry_files": ["lib.rs", "main.rs", "mod.rs"],
        "module_keyword": "mod",
        "import_keyword": "use",
    },
    "typescript": {
        "extensions": [".ts", ".tsx"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/*",
        "entry_files": ["index.ts", "index.tsx", "main.ts", "main.tsx"],
        "module_keyword": "",
        "import_keyword": "import",
    },
    "javascript": {
        "extensions": [".js", ".jsx", ".mjs", ".cjs"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/*",
        "entry_files": ["index.js", "index.jsx", "main.js", "main.jsx"],
        "module_keyword": "",
        "import_keyword": "import",
    },
    "python": {
        "extensions": [".py"],
        "comment_line": "#",
        "comment_block_start": '"""',
        "comment_block_end": '"""',
        "doc_comment_line": '"""',
        "entry_files": ["__init__.py", "main.py", "__main__.py"],
        "module_keyword": "",
        "import_keyword": "import",
    },
    "kotlin": {
        "extensions": [".kt", ".kts"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/*",
        "entry_files": ["Main.kt"],
        "module_keyword": "package",
        "import_keyword": "import",
    },
    "go": {
        "extensions": [".go"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "//",
        "entry_files": ["main.go"],
        "module_keyword": "package",
        "import_keyword": "import",
    },
    "java": {
        "extensions": [".java"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/*",
        "entry_files": ["Main.java"],
        "module_keyword": "package",
        "import_keyword": "import",
    },
    "c": {
        "extensions": [".c", ".h"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/*",
        "entry_files": ["main.c"],
        "module_keyword": "",
        "import_keyword": "#include",
    },
    "cpp": {
        "extensions": [".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/*",
        "entry_files": ["main.cpp", "main.cc"],
        "module_keyword": "namespace",
        "import_keyword": "#include",
    },
    "csharp": {
        "extensions": [".cs"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "///",
        "entry_files": ["Program.cs", "Startup.cs"],
        "module_keyword": "namespace",
        "import_keyword": "using",
    },
    "ruby": {
        "extensions": [".rb"],
        "comment_line": "#",
        "comment_block_start": "=begin",
        "comment_block_end": "=end",
        "doc_comment_line": "#",
        "entry_files": ["main.rb", "app.rb", "Gemfile"],
        "module_keyword": "module",
        "import_keyword": "require",
    },
    "php": {
        "extensions": [".php"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/",
        "entry_files": ["index.php", "composer.json"],
        "module_keyword": "namespace",
        "import_keyword": "use",
    },
    "swift": {
        "extensions": [".swift"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "///",
        "entry_files": ["main.swift", "Package.swift"],
        "module_keyword": "class",
        "import_keyword": "import",
    },
    "scala": {
        "extensions": [".scala", ".sc"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/",
        "entry_files": ["Main.scala", "build.sbt"],
        "module_keyword": "package",
        "import_keyword": "import",
    },
    "hcl": {
        "extensions": [".tf", ".hcl"],
        "comment_line": "#",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "#",
        "entry_files": ["main.tf", "variables.tf", "outputs.tf"],
        "module_keyword": "resource",
        "import_keyword": "module",
    },
    "elixir": {
        "extensions": [".ex", ".exs"],
        "comment_line": "#",
        "comment_block_start": "=begin",
        "comment_block_end": "=end",
        "doc_comment_line": "@moduledoc",
        "entry_files": ["mix.exs", "application.ex"],
        "module_keyword": "defmodule",
        "import_keyword": "alias",
    },
}


def detect_language_from_path(file_path: str) -> str:
    """根据文件路径检测语言

    Args:
        file_path: 文件路径

    Returns:
        语言名称（如 "rust", "typescript"），未识别返回 ""
    """
    _, ext = os.path.splitext(file_path)
    ext_lower = ext.lower()
    for lang, config in LANGUAGE_CONFIG.items():
        if ext_lower in config["extensions"]:
            return lang
    return ""


def get_supported_extensions() -> List[str]:
    """获取所有支持的文件扩展名"""
    exts = []
    for config in LANGUAGE_CONFIG.values():
        exts.extend(config["extensions"])
    return exts


def norm_path(path: str) -> str:
    """标准化路径：统一正斜杠 + 去末尾斜杠 + Windows 盘符小写

    消除跨平台和大小写差异，确保同一路径的不同写法产生相同的 hash。
    - 反斜杠 → 正斜杠
    - 去掉末尾斜杠（根目录 "/" 除外）
    - Windows 盘符统一小写（C:\\ 和 c:\\ 等价）
    """
    if not path:
        return path
    # 反斜杠 → 正斜杠
    normalized = path.replace("\\", "/")
    # 去掉末尾斜杠（但保留根目录 "/"）
    if len(normalized) > 1 and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    # Windows 盘符统一小写（如 C:/ → c:/）
    if len(normalized) >= 2 and normalized[1] == ":" and normalized[0].isalpha():
        normalized = normalized[0].lower() + normalized[1:]
    return normalized


def norm_newlines(text: str) -> str:
    """标准化换行符：CRLF/CR -> LF"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def compute_content_hash(content: str | bytes) -> str:
    """计算内容 hash（标准化换行符后）"""
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8", errors="replace")
        except Exception:
            text = content.decode("latin-1", errors="replace")
    else:
        text = content
    normalized = norm_newlines(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def read_file_normalized(file_path: str) -> Tuple[str, str]:
    """读取文件并标准化换行符，返回 (标准化后的内容, hash)"""
    with open(file_path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    normalized = norm_newlines(text)
    content_hash = compute_content_hash(normalized)
    return normalized, content_hash


def read_file_text(file_path: str, errors: str = "replace") -> str:
    """安全读取文本文件（UTF-8 优先，失败降级 latin-1）

    P23.4: 项目依赖文件（requirements.txt / package.json / Cargo.toml 等）
    可能使用非 UTF-8 编码（如 UTF-16 BOM、GB2312）。直接 open(encoding="utf-8")
    会抛 UnicodeDecodeError 导致整个项目构建失败。本函数自动降级，确保任何
    编码的文件都能读出文本（无效字节用 ? 替换）。

    Args:
        file_path: 文件绝对路径
        errors: 解码错误处理策略（默认 "replace"，用 ? 替换无效字节）

    Returns:
        文件文本内容（换行符已标准化为 LF）
    """
    with open(file_path, "rb") as f:
        raw = f.read()
    # 尝试 UTF-8（含 BOM 自动剥离）
    if raw.startswith(codecs.BOM_UTF8):
        text = raw[3:].decode("utf-8", errors=errors)
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # UTF-16 BOM 检测
            if raw.startswith(codecs.BOM_UTF16_LE):
                text = raw[2:].decode("utf-16-le", errors=errors)
            elif raw.startswith(codecs.BOM_UTF16_BE):
                text = raw[2:].decode("utf-16-be", errors=errors)
            else:
                # 降级 latin-1：逐字节映射，不会失败
                text = raw.decode("latin-1", errors=errors)
    return norm_newlines(text)


def to_long_path(path: str) -> str:
    """Windows 长路径支持：添加 \\\\?\\ 前缀绕过 MAX_PATH 260 限制

    P23.6: Java Maven 深层目录路径可能超过 260 字符
    (src/main/java/com/xxx/xxx/...)，Windows 默认不支持。
    添加 \\\\?\\ 前缀后，Windows API 支持最长 32767 字符路径。

    仅在 Windows 且路径长度可能超限时添加。\\?\ 前缀要求：
    - 绝对路径
    - 不含 .. 或 .
    - 反斜杠分隔

    Args:
        path: 原始路径

    Returns:
        可能添加了 \\\\?\\ 前缀的路径
    """
    if os.name != "nt":
        return path
    if not path:
        return path
    # 已有 \\?\ 前缀，不重复添加
    if path.startswith("\\\\?\\"):
        return path
    # 路径未超 260，不需要长路径前缀
    if len(path) < 250:
        return path
    # 必须是绝对路径
    if not os.path.isabs(path):
        return path
    # 标准化为绝对路径，解析 .. 和 .
    abs_path = os.path.abspath(path)
    # 转为反斜杠分隔
    abs_path = abs_path.replace("/", "\\")
    # 添加 \\?\ 前缀
    if abs_path[1] == ":" and abs_path[0].isalpha():
        return "\\\\?\\" + abs_path
    # UNC 路径 \\\\server\\share -> \\\\?\\UNC\\server\\share
    if abs_path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_path[2:]
    return abs_path


def safe_walk(root_dir: str, max_depth: int = -1, **kwargs):
    """带错误处理的 os.walk：跳过不可访问的目录/文件

    P23.5: Windows 下可能遇到文件锁定（WinError 1920）、权限不足、
    路径过长（WinError 3）等错误，os.walk 默认会中断遍历。
    本函数通过 onerror 回调捕获错误并继续遍历。

    Args:
        root_dir: 遍历根目录
        max_depth: 最大递归深度（-1 表示无限制）
        **kwargs: 传递给 os.walk 的额外参数

    Yields:
        (dirpath, dirnames, filenames) 三元组
    """
    _walk_errors = []

    def _onerror(err):
        _walk_errors.append(err)

    root_dir = to_long_path(root_dir)

    for root, dirs, files in os.walk(root_dir, onerror=_onerror, **kwargs):
        # 深度限制
        if max_depth >= 0:
            rel = os.path.relpath(root, root_dir)
            depth = 0 if rel == "." else rel.count(os.sep)
            if depth > max_depth:
                dirs[:] = []
                continue
        yield root, dirs, files
