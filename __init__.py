"""
code_graph
==========

代码知识图谱工具包：基于 tree-sitter 的多语言代码分析、版本管理、缺陷检测。

核心类：
- CodeGraphDB：代码知识图谱数据库（主入口）
- RustParser：Rust 源码解析器
- ModuleResolver：Rust 模块系统解析器
- CallResolver：调用关系解析器
- FileWatcher：文件监控器
"""

from .db import CodeGraphDB
from .config import DB_PATH, PROJECT_ROOT, norm_path, norm_newlines, compute_content_hash, read_file_normalized

__version__ = "0.2.0"
__all__ = ["CodeGraphDB", "DB_PATH", "PROJECT_ROOT", "norm_path", "norm_newlines", "compute_content_hash", "read_file_normalized"]
