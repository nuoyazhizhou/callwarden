"""
parsers/__init__.py
===================

代码解析器模块：多语言源码解析、模块系统解析、调用关系解析。
"""

from typing import Optional

from ..config import detect_language_from_path
from .base import BaseParser
from .rust import RustParser
from .typescript import TypeScriptParser
from .python_parser import PythonParser
from .kotlin_parser import KotlinParser
from .go_parser import GoParser
from .java_parser import JavaParser
from .c_parser import CParser, CppParser
from .csharp_parser import CSharpParser
from .ruby_parser import RubyParser
from .php_parser import PhpParser
from .swift_parser import SwiftParser
from .scala_parser import ScalaParser
from .hcl_parser import HclParser
from .elixir_parser import ElixirParser
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


_parser_cache = {}


def create_parser(file_path: str) -> Optional[BaseParser]:
    """根据文件路径创建对应的语言解析器（带缓存）

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
        parser = RustParser()
    elif lang == "typescript":
        parser = TypeScriptParser(dialect="tsx" if dialect == "tsx" else "typescript")
    elif lang == "javascript":
        parser = TypeScriptParser(dialect="jsx" if dialect == "jsx" else "javascript")
    elif lang == "python":
        parser = PythonParser()
    elif lang == "kotlin":
        parser = KotlinParser()
    elif lang == "go":
        parser = GoParser()
    elif lang == "java":
        parser = JavaParser()
    elif lang == "c":
        parser = CParser()
    elif lang == "cpp":
        parser = CppParser()
    elif lang == "csharp":
        parser = CSharpParser()
    elif lang == "ruby":
        parser = RubyParser()
    elif lang == "php":
        parser = PhpParser()
    elif lang == "swift":
        parser = SwiftParser()
    elif lang == "scala":
        parser = ScalaParser()
    elif lang == "hcl":
        parser = HclParser()
    elif lang == "elixir":
        parser = ElixirParser()

    if parser:
        _parser_cache[cache_key] = parser

    return parser
