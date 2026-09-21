"""
callwarden
==========

Call Warden：面向 AI Agent 的代码知识图谱工具，基于 tree-sitter 的多语言代码分析、版本管理、缺陷检测。

核心类：
- RustParser：Rust 源码解析器
- ModuleResolver：Rust 模块系统解析器
- CallResolver：调用关系解析器
- FileWatcher：文件监控器

注：``CodeGraphDB`` 已降级为**仅测试支持**的遗留类（db/ 退休验收③），
不再由顶层包导出；业务逻辑全部下沉 Rust daemon，生产路径经 daemon RPC 访问。
"""

from .config import PROJECT_ROOT, norm_path, norm_newlines, compute_content_hash, read_file_normalized

__version__ = "0.3.23"
__all__ = ["PROJECT_ROOT", "norm_path", "norm_newlines", "compute_content_hash", "read_file_normalized"]
