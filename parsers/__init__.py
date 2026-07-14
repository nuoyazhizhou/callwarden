"""
parsers/__init__.py
===================

代码解析器模块：多语言源码解析、模块系统解析、调用关系解析。

L14 懒加载：各语言 Parser 类按需 import，避免启动时全量加载 16 种 grammar。
- create_parser() 创建实例时才 import 对应语言模块
- 外部 `from callwarden.parsers import RustParser` 通过模块级 __getattr__ 按需加载
- 基础类 BaseParser / ModuleResolver / CallResolver 仍顶层 import（不涉及 grammar）

背景：parsers/__init__.py 原顶层 import 全部 16 个 Parser 类，每个类又顶层
import 对应 tree-sitter grammar（每个 15-30MB，总计 200-400MB）。只要
db_base.py 执行 `from ..parsers import RustParser`，就会触发 __init__.py
全量加载所有 grammar。改为 __getattr__ 懒加载后，只加载实际用到的语言。
"""

from typing import Optional

from ..config import detect_language_from_path
from .base import BaseParser
from .module_resolver import ModuleResolver
from .call_resolver import CallResolver

__all__ = [
    "BaseParser",
    "RustParser",
    "TypeScriptParser",
    "PythonParser",
    "KotlinParser",
    "GoParser",
    "JavaParser",
    "CParser",
    "CppParser",
    "CSharpParser",
    "RubyParser",
    "PhpParser",
    "SwiftParser",
    "ScalaParser",
    "HclParser",
    "ElixirParser",
    "ModuleResolver",
    "CallResolver",
    "create_parser",
]


# 语言 Parser 类 → (相对模块路径, 类名) 映射，用于懒加载
# 模块路径相对于本包（parsers），如 ".rust" 对应 parsers.rust
_LAZY_PARSER_MAP = {
    "RustParser": (".rust", "RustParser"),
    "TypeScriptParser": (".typescript", "TypeScriptParser"),
    "PythonParser": (".python_parser", "PythonParser"),
    "KotlinParser": (".kotlin_parser", "KotlinParser"),
    "GoParser": (".go_parser", "GoParser"),
    "JavaParser": (".java_parser", "JavaParser"),
    "CParser": (".c_parser", "CParser"),
    "CppParser": (".c_parser", "CppParser"),
    "CSharpParser": (".csharp_parser", "CSharpParser"),
    "RubyParser": (".ruby_parser", "RubyParser"),
    "PhpParser": (".php_parser", "PhpParser"),
    "SwiftParser": (".swift_parser", "SwiftParser"),
    "ScalaParser": (".scala_parser", "ScalaParser"),
    "HclParser": (".hcl_parser", "HclParser"),
    "ElixirParser": (".elixir_parser", "ElixirParser"),
}


def __getattr__(name: str):
    """模块级懒加载钩子：按需 import 各语言 Parser 类

    当外部 `from callwarden.parsers import RustParser` 时，RustParser 不在
    模块命名空间，触发此函数按需加载对应模块，避免启动时全量加载 16 种
    tree-sitter grammar。加载后缓存到模块 globals()，后续访问不再触发。
    """
    if name in _LAZY_PARSER_MAP:
        import importlib

        module_path, class_name = _LAZY_PARSER_MAP[name]
        mod = importlib.import_module(module_path, __name__)
        cls = getattr(mod, class_name)
        # 缓存到模块命名空间，后续访问不再触发 __getattr__
        globals()[name] = cls
        return cls
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__():
    """支持 dir() 和 IDE 自动补全，返回所有可懒加载的属性名"""
    return sorted(set(list(globals().keys()) + list(_LAZY_PARSER_MAP.keys())))


_parser_cache = {}


def create_parser(file_path: str) -> Optional[BaseParser]:
    """根据文件路径创建对应的语言解析器（带缓存）

    L14 懒加载：按需 import 对应语言模块，不主动加载未使用的 grammar。

    Args:
        file_path: 文件路径

    Returns:
        对应的解析器实例，未支持的语言返回 None
    """
    lang = detect_language_from_path(file_path)
    if not lang:
        return None

    dialect = ""
    if lang in ("typescript", "javascript"):
        if file_path.endswith((".tsx", ".jsx")):
            dialect = "tsx" if lang == "typescript" else "jsx"
        else:
            dialect = "typescript" if lang == "typescript" else "javascript"

    cache_key = (lang, dialect) if dialect else lang
    if cache_key in _parser_cache:
        return _parser_cache[cache_key]

    parser = None
    if lang == "rust":
        from .rust import RustParser

        parser = RustParser()
    elif lang in ("typescript", "javascript"):
        from .typescript import TypeScriptParser

        parser = TypeScriptParser(dialect=dialect if dialect else lang)
    elif lang == "python":
        from .python_parser import PythonParser

        parser = PythonParser()
    elif lang == "kotlin":
        from .kotlin_parser import KotlinParser

        parser = KotlinParser()
    elif lang == "go":
        from .go_parser import GoParser

        parser = GoParser()
    elif lang == "java":
        from .java_parser import JavaParser

        parser = JavaParser()
    elif lang == "c":
        from .c_parser import CParser

        parser = CParser()
    elif lang == "cpp":
        from .c_parser import CppParser

        parser = CppParser()
    elif lang == "csharp":
        from .csharp_parser import CSharpParser

        parser = CSharpParser()
    elif lang == "ruby":
        from .ruby_parser import RubyParser

        parser = RubyParser()
    elif lang == "php":
        from .php_parser import PhpParser

        parser = PhpParser()
    elif lang == "swift":
        from .swift_parser import SwiftParser

        parser = SwiftParser()
    elif lang == "scala":
        from .scala_parser import ScalaParser

        parser = ScalaParser()
    elif lang == "hcl":
        from .hcl_parser import HclParser

        parser = HclParser()
    elif lang == "elixir":
        from .elixir_parser import ElixirParser

        parser = ElixirParser()

    if parser:
        _parser_cache[cache_key] = parser

    return parser
