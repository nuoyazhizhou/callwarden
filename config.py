"""
config.py
=========

代码知识图谱配置：路径常量、工具函数、多语言配置。
"""

import hashlib
import os
import tempfile
from typing import Dict, List, Optional, Tuple

# 路径常量
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGES_DIR = os.path.dirname(SCRIPT_DIR)  # scripts/
SRC_DIR = os.path.join(PACKAGES_DIR, "..", "src")

# 数据库根目录：用户主目录下的 .code_graph/
USER_HOME = os.path.expanduser("~")
CODE_GRAPH_DIR = os.path.join(USER_HOME, ".code_graph")

# 向后兼容的默认 DB_PATH（推荐使用 get_project_db_path 按项目隔离）
DB_PATH = os.path.join(CODE_GRAPH_DIR, "code_graph.db")


def get_project_db_path(project_root: str) -> str:
    """根据项目根路径生成项目级数据库路径（一个项目一个 SQLite 数据库）

    路径格式: $HOME/.code_graph/16位hash/code_graph.db

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
    project_dir = os.path.join(CODE_GRAPH_DIR, path_hash)
    os.makedirs(project_dir, exist_ok=True)
    return os.path.join(project_dir, "code_graph.db")


def ensure_code_graph_dir() -> str:
    """确保数据库根目录存在，返回目录路径"""
    os.makedirs(CODE_GRAPH_DIR, exist_ok=True)
    return CODE_GRAPH_DIR


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
    """标准化路径：统一使用正斜杠，消除跨平台差异"""
    return path.replace("\\", "/")


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
