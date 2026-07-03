"""
call_filter.py
==============

调用过滤模块：识别并过滤标准库、外部依赖等非项目内部调用。

设计目标：
1. 减少无效调用关系存储，降低数据库大小
2. 提升调用解析精度，过滤掉明显的外部/标准库调用
3. 支持多语言：Rust/TypeScript/JavaScript/Python/Kotlin 等
"""

from __future__ import annotations

import re
from typing import Set


RUST_STD_PREFIXES: Set[str] = {
    "std::", "core::", "alloc::", "std::prelude::",
    "std::io::", "std::fmt::", "std::collections::",
    "std::option::Option::", "std::result::Result::",
    "std::vec::Vec::", "std::string::String::",
    "std::str::", "std::fs::", "std::path::",
    "std::thread::", "std::sync::", "std::time::",
}

RUST_STD_MACROS: Set[str] = {
    "println", "print", "eprintln", "eprint",
    "format", "format_args", "vec", "assert", "assert_eq", "assert_ne",
    "debug_assert", "debug_assert_eq", "debug_assert_ne",
    "panic", "unreachable", "unimplemented", "todo",
    "try", "await", "dbg", "write", "writeln",
    "include_str", "include_bytes", "include",
    "env", "option_env", "concat", "stringify", "cfg",
    "column", "file", "line", "module_path",
}

RUST_COMMON_DERIVE: Set[str] = {
    "Debug", "Clone", "Copy", "PartialEq", "Eq",
    "Hash", "PartialOrd", "Ord", "Default",
    "Serialize", "Deserialize", "Display", "From",
    "Into", "TryFrom", "TryInto", "Deref", "DerefMut",
    "AsRef", "AsMut", "FromStr",
}

RUST_COMMON_EXTERNAL_CRATES: Set[str] = {
    "tokio::", "serde::", "serde_json::", "anyhow::",
    "thiserror::", "clap::", "reqwest::", "hyper::",
    "tracing::", "log::", "env_logger::", "rand::",
    "chrono::", "uuid::", "base64::", "regex::",
    "once_cell::", "lazy_static::", "parking_lot::",
    "crossbeam::", "rayon::", "itertools::",
    "nom::", "syn::", "quote::", "proc_macro2::",
    "axum::", "actix::", "rocket::", "warp::",
    "sqlx::", "diesel::", "rusqlite::",
    "bytes::", "futures::", "pin_project::",
    "mio::", "rustls::", "webpki::",
    "tree_sitter::", "ignore::", "walkdir::",
    "toml::", "yaml::", "ron::", "bincode::",
}

TS_JS_GLOBALS: Set[str] = {
    "console", "Math", "JSON", "Object", "Array", "String",
    "Number", "Boolean", "Date", "RegExp", "Error", "TypeError",
    "RangeError", "ReferenceError", "SyntaxError", "URIError",
    "Promise", "Map", "Set", "WeakMap", "WeakSet", "Symbol",
    "Proxy", "Reflect", "Int8Array", "Uint8Array", "Uint8ClampedArray",
    "Int16Array", "Uint16Array", "Int32Array", "Uint32Array",
    "Float32Array", "Float64Array", "BigInt64Array", "BigUint64Array",
    "BigInt", "DataView", "ArrayBuffer", "SharedArrayBuffer",
    "Atomics", "globalThis", "window", "document", "navigator",
    "self", "global", "process", "Buffer", "setTimeout", "setInterval",
    "clearTimeout", "clearInterval", "setImmediate", "clearImmediate",
    "queueMicrotask", "requestAnimationFrame", "cancelAnimationFrame",
    "fetch", "Headers", "Request", "Response", "FormData", "Blob",
    "File", "FileReader", "URL", "URLSearchParams", "TextEncoder",
    "TextDecoder", "AbortController", "AbortSignal",
    "alert", "confirm", "prompt",
    "require", "module", "exports", "__dirname", "__filename",
    "describe", "it", "test", "expect", "beforeEach", "afterEach",
    "beforeAll", "afterAll", "jest",
    "React", "useState", "useEffect", "useContext", "useReducer",
    "useCallback", "useMemo", "useRef", "useImperativeHandle",
    "useLayoutEffect",
}

TS_JS_COMMON_MODULES: Set[str] = {
    "react", "react-dom", "react-dom/client", "react-router", "react-router-dom",
    "vue", "@vue/runtime-core", "@vue/reactivity",
    "axios", "lodash", "underscore", "moment", "dayjs", "date-fns",
    "express", "koa", "hapi", "fastify", "nestjs", "@nestjs/common",
    "webpack", "vite", "rollup", "esbuild", "parcel",
    "typescript", "ts-node", "ts-jest", "babel", "@babel/core",
    "jest", "mocha", "chai", "sinon", "vitest",
    "eslint", "prettier", "eslint-plugin-react",
    "fs", "path", "os", "http", "https", "url", "querystring",
    "crypto", "stream", "buffer", "util", "events", "child_process",
    "worker_threads", "perf_hooks", "async_hooks", "zlib",
    "electron", "@electron/remote",
}

KOTLIN_STD_PREFIXES: Set[str] = {
    "kotlin.", "kotlinx.", "java.", "javax.", "android.",
    "androidx.", "com.google.", "org.json.",
}

KOTLIN_STD_CLASSES: Set[str] = {
    "String", "Int", "Long", "Short", "Byte", "Float", "Double",
    "Boolean", "Char", "Unit", "Nothing", "Any",
    "List", "MutableList", "Set", "MutableSet", "Map", "MutableMap",
    "Array", "ArrayList", "HashMap", "HashSet", "LinkedHashMap", "LinkedHashSet",
    "Pair", "Triple", "Comparable", "Comparator",
    "println", "print", "readLine", "readln",
    "run", "let", "also", "apply", "with", "takeIf", "takeUnless",
    "repeat", "lazy", "error", "TODO", "check", "checkNotNull",
    "require", "requireNotNull", "assert",
}

GO_STD_PREFIXES: Set[str] = {
    "fmt.", "os.", "io.", "ioutil.", "net.", "net/http.",
    "encoding.", "encoding/json.", "encoding/base64.",
    "strings.", "strconv.", "bytes.", "bufio.", "path.",
    "path/filepath.", "sort.", "sync.", "time.", "context.",
    "log.", "errors.", "math.", "math/rand.", "crypto.",
    "crypto/tls.", "crypto/sha256.", "crypto/md5.",
    "regexp.", "reflect.", "runtime.", "runtime/debug.",
    "unicode.", "unicode/utf8.", "flag.", "testing.",
    "database/sql.", "html.", "html/template.", "text/template.",
}

GO_BUILTIN: Set[str] = {
    "append", "copy", "delete", "len", "cap", "make", "new",
    "complex", "real", "imag", "close", "panic", "recover",
    "print", "println", "error", "bool", "string",
    "int", "int8", "int16", "int32", "int64",
    "uint", "uint8", "uint16", "uint32", "uint64", "uintptr",
    "byte", "rune", "float32", "float64", "complex64", "complex128",
    "nil", "true", "false",
    "map", "chan", "select", "go", "defer", "fallthrough",
}

PYTHON_STD_MODULES: Set[str] = {
    "os", "sys", "io", "re", "json", "time", "datetime", "math",
    "random", "string", "collections", "itertools", "functools",
    "pathlib", "subprocess", "threading", "multiprocessing",
    "logging", "argparse", "configparser", "csv", "hashlib",
    "base64", "uuid", "copy", "typing", "abc", "dataclasses",
    "enum", "struct", "socket", "http", "http.client", "urllib",
    "urllib.request", "urllib.parse", "email", "html", "xml",
    "sqlite3", "pickle", "shutil", "glob", "tempfile", "textwrap",
    "warnings", "traceback", "inspect", "ast", "tokenize",
    "importlib", "pkgutil", "platform", "weakref", "gc",
    "contextlib", "asyncio", "concurrent", "concurrent.futures",
    "queue", "signal", "select", "selectors", "ssl",
}

PYTHON_BUILTINS: Set[str] = {
    "print", "len", "range", "str", "int", "float", "bool",
    "list", "dict", "set", "tuple", "bytes", "bytearray",
    "type", "isinstance", "issubclass", "super", "id", "hash",
    "repr", "chr", "ord", "hex", "oct", "bin", "format",
    "max", "min", "sum", "sorted", "reversed", "enumerate",
    "zip", "map", "filter", "any", "all", "abs", "round",
    "divmod", "pow", "complex", "open", "input", "next",
    "iter", "property", "classmethod", "staticmethod",
    "Exception", "ValueError", "TypeError", "KeyError",
    "IndexError", "AttributeError", "RuntimeError", "StopIteration",
    "NotImplementedError", "AssertionError", "OSError", "IOError",
    "FileNotFoundError", "PermissionError", "FileExistsError",
    "NotImplemented", "None", "True", "False",
    "__import__", "__name__", "__file__", "__doc__",
    "self", "cls",
}

CSHARP_STD_PREFIXES: Set[str] = {
    "System.", "System.Collections.", "System.Collections.Generic.",
    "System.IO.", "System.Linq.", "System.Text.", "System.Text.Json.",
    "System.Threading.", "System.Threading.Tasks.", "System.Net.",
    "System.Net.Http.", "System.Diagnostics.", "System.Reflection.",
    "System.Runtime.", "System.Security.", "System.Text.RegularExpressions.",
    "System.Xml.", "System.Data.", "System.Configuration.",
    "Microsoft.", "Microsoft.AspNetCore.", "Microsoft.Extensions.",
    "Microsoft.Win32.",
}

CSHARP_KEYWORDS: Set[str] = {
    "Console.WriteLine", "Console.Write", "Console.ReadLine", "Console.Read",
    "Debug.WriteLine", "Trace.WriteLine",
    "ToString", "Equals", "GetHashCode", "GetType",
    "List", "Dictionary", "HashSet", "Queue", "Stack",
    "Enumerable.Select", "Enumerable.Where", "Enumerable.SelectMany",
    "Task.Run", "Task.WhenAll", "Task.WhenAny", "Task.Delay",
    "await", "async", "var", "new", "this", "base",
    "null", "true", "false", "void", "return", "throw",
}

C_STD_FUNCTIONS: Set[str] = {
    "printf", "fprintf", "sprintf", "snprintf", "scanf", "fscanf", "sscanf",
    "puts", "fputs", "gets", "fgets", "getchar", "putchar", "getc", "putc", "fgetc", "fputc",
    "malloc", "calloc", "realloc", "free", "memcpy", "memmove", "memset", "memcmp", "memchr",
    "strlen", "strcpy", "strncpy", "strcat", "strncat", "strcmp", "strncmp", "strchr", "strrchr",
    "strstr", "strtok", "strdup", "strndup",
    "open", "close", "read", "write", "lseek", "creat", "unlink", "link", "stat", "fstat",
    "fopen", "fclose", "fread", "fwrite", "fflush", "fseek", "ftell", "rewind", "feof", "ferror",
    "exit", "abort", "atexit", "system",
    "abs", "labs", "atof", "atoi", "atol", "strtol", "strtod", "strtoul",
    "isalpha", "isdigit", "isalnum", "isspace", "isupper", "islower", "toupper", "tolower",
    "time", "ctime", "localtime", "gmtime", "mktime", "difftime",
    "qsort", "bsearch", "rand", "srand",
}

CPP_STD_PREFIXES: Set[str] = {
    "std::", "std::chrono::", "std::filesystem::", "std::ranges::",
    "boost::",
}

CPP_STD_CLASSES: Set[str] = {
    "cout", "cin", "cerr", "clog",
    "string", "vector", "map", "unordered_map", "set", "unordered_set",
    "list", "deque", "queue", "stack", "priority_queue",
    "unique_ptr", "shared_ptr", "weak_ptr", "make_unique", "make_shared",
    "move", "forward", "forward_as_tuple", "tie",
    "endl", "flush", "hex", "dec", "oct", "setw", "setfill",
}


def should_filter_rust_call(call_name: str) -> bool:
    """判断 Rust 调用是否应该被过滤（仅过滤明确的标准库/宏/语法结构）"""
    if not call_name:
        return True
    if call_name in RUST_STD_MACROS:
        return True
    if call_name in RUST_COMMON_DERIVE:
        return True
    for prefix in RUST_STD_PREFIXES:
        if call_name.startswith(prefix):
            return True
    for prefix in RUST_COMMON_EXTERNAL_CRATES:
        if call_name.startswith(prefix):
            return True
    if call_name in {"Ok", "Err", "Some", "None", "Box", "Rc", "Arc", "Cell", "RefCell",
                     "Pin", "Vec", "String", "BTreeMap", "BTreeSet", "HashMap", "HashSet",
                     "LinkedList", "VecDeque", "BinaryHeap"}:
        return True
    if call_name.endswith("!"):
        return True
    return False


def should_filter_ts_js_call(call_name: str) -> bool:
    """判断 TypeScript/JavaScript 调用是否应该被过滤（全局对象/Node内置/常见库）"""
    if not call_name:
        return True
    base = call_name.split(".")[0] if "." in call_name else call_name
    if base in TS_JS_GLOBALS:
        return True
    if "." in call_name:
        if call_name.split(".")[0] in TS_JS_GLOBALS:
            return True
    if "/" not in call_name and call_name in TS_JS_COMMON_MODULES:
        return True
    if call_name.startswith("node:"):
        return True
    return False


def should_filter_kotlin_call(call_name: str) -> bool:
    """判断 Kotlin/Java 调用是否应该被过滤（标准库/JDK/Android）"""
    if not call_name:
        return True
    if call_name in KOTLIN_STD_CLASSES:
        return True
    for prefix in KOTLIN_STD_PREFIXES:
        if call_name.startswith(prefix):
            return True
    if "." in call_name:
        first_part = call_name.split(".")[0]
        if first_part[:1].islower() and first_part not in {"this", "it", "super"}:
            return False
    return False


def should_filter_go_call(call_name: str) -> bool:
    """判断 Go 调用是否应该被过滤（标准库/内置函数）"""
    if not call_name:
        return True
    if call_name in GO_BUILTIN:
        return True
    for prefix in GO_STD_PREFIXES:
        if call_name.startswith(prefix):
            return True
    if "." in call_name:
        first_part = call_name.split(".")[0]
        if first_part[:1].islower() and first_part not in {"err", "ok", "nil", "defer"}:
            return False
    return False


def should_filter_python_call(call_name: str) -> bool:
    """判断 Python 调用是否应该被过滤（标准库/内置函数）"""
    if not call_name:
        return True
    base = call_name.split(".")[0]
    if base in PYTHON_BUILTINS:
        return True
    if base in PYTHON_STD_MODULES:
        return True
    if call_name.startswith("self.") or call_name.startswith("cls."):
        return False
    return False


def should_filter_csharp_call(call_name: str) -> bool:
    """判断 C# 调用是否应该被过滤（.NET 标准库/关键字）"""
    if not call_name:
        return True
    for prefix in CSHARP_STD_PREFIXES:
        if call_name.startswith(prefix):
            return True
    if call_name in CSHARP_KEYWORDS:
        return True
    return False


def should_filter_c_call(call_name: str) -> bool:
    """判断 C 调用是否应该被过滤（C 标准库函数）"""
    if not call_name:
        return True
    if call_name in C_STD_FUNCTIONS:
        return True
    return False


def should_filter_cpp_call(call_name: str) -> bool:
    """判断 C++ 调用是否应该被过滤（C++ 标准库）"""
    if not call_name:
        return True
    if call_name in C_STD_FUNCTIONS:
        return True
    for prefix in CPP_STD_PREFIXES:
        if call_name.startswith(prefix):
            return True
    if call_name in CPP_STD_CLASSES:
        return True
    return False


def should_filter_call(language: str, call_name: str) -> bool:
    """
    判断指定语言的调用是否应该被过滤。

    返回 True 表示这是标准库/外部依赖/全局调用，应该跳过（不记录到调用图中）。
    返回 False 表示这可能是项目内部调用，应该尝试解析。
    """
    if not call_name or not call_name.strip():
        return True
    call_name = call_name.strip()

    if language == "rust":
        return should_filter_rust_call(call_name)
    elif language in ("typescript", "javascript"):
        return should_filter_ts_js_call(call_name)
    elif language in ("kotlin", "java"):
        return should_filter_kotlin_call(call_name)
    elif language == "go":
        return should_filter_go_call(call_name)
    elif language == "python":
        return should_filter_python_call(call_name)
    elif language == "c":
        return should_filter_c_call(call_name)
    elif language == "cpp":
        return should_filter_cpp_call(call_name)
    elif language == "csharp":
        return should_filter_csharp_call(call_name)
    return False
