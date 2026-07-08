"""P9 C/C++ parser 显式栈遍历 + 默认 ignore 规则测试。

覆盖 `parsers/c_parser.py` 中 `_extract_raw_calls` 从递归 DFS 改为显式栈后的：
- C parser 基础调用提取（顶层函数、函数内调用、嵌套块中的调用）
- C++ parser 命名空间作用域 + 类方法作用域
- 深嵌套代码不触发 RecursionError（P9 核心目标）
- 默认 ignore 规则覆盖 thirdParty/ / third_party/ / vendor/（P9 配套）
"""
import os
import sys
import tempfile

import pytest

from callwarden.parsers.c_parser import CParser, CppParser
from callwarden.db.db import CodeGraphDB


# ============================================
# C 源码样例
# ============================================

C_BASIC = r"""
/* 基础调用关系：a 调用 b，b 调用 c */
void a(void) {
    b();
}

void b(void) {
    if (1) {
        c();
    }
}

void c(void) {
    /* 叶子函数 */
}
"""

C_DEEP_NESTING_TEMPLATE = """
void top(void) {{
{body}
}}
"""

# 构造 600 层 if 嵌套，叶子层调用 leaf()
# 600 层在 Python 默认 recursionlimit=1000 下会逼近递归深度，
# 配合测试中临时降低 recursionlimit 可验证显式栈不爆栈
_DEEP_BODY = "".join("    if (1) {\n" for _ in range(600))
_DEEP_BODY += "        leaf();\n"
_DEEP_BODY += "".join("    }\n" for _ in range(600))
C_DEEP_NESTING = C_DEEP_NESTING_TEMPLATE.format(body=_DEEP_BODY)


# ============================================
# C++ 源码样例
# ============================================

CPP_NAMESPACE = r"""
namespace app {

void free_func(void) {
    helper();
}

namespace inner {
    void helper(void) {
        deep();
    }
    void deep(void) {}
}

}
"""

CPP_CLASS = r"""
class Service {
public:
    void start() {
        init();
        run();
    }
    void init() {}
    void run() {
        stop();
    }
    void stop() {}
};
"""

CPP_NESTED = r"""
namespace outer {

class Widget {
public:
    void render() {
        paint();
    }
    void paint() {}
};

}
"""


# ============================================
# 辅助函数
# ============================================

def _parse_source(parser, source: str, module_path: str = "mod"):
    """直接解析源码字符串，返回 raw_calls（未经 should_filter 过滤的原始提取结果）"""
    src_bytes = source.encode("utf-8")
    tree = parser.parser.parse(src_bytes)
    return parser._extract_raw_calls(tree.root_node, src_bytes, module_path)


def _write_and_parse_file(parser, root, filename, content):
    """写临时文件并走 parse_file 公共接口（含 should_filter 过滤）"""
    path = os.path.join(root, filename)
    with open(path, "w", encoding="utf-8") as f:
        f.write(content)
    return parser.parse_file(path, module_path=filename)


# ============================================
# C parser 测试
# ============================================

def test_c_parser_extracts_basic_calls():
    """C parser 显式栈应正确提取函数调用关系。"""
    parser = CParser()
    calls = _parse_source(parser, C_BASIC, "module")

    # a 调用 b
    call_a = [c for c in calls if c.get("caller_name") == "a"]
    assert len(call_a) == 1
    assert call_a[0]["callee_name"] == "b"

    # b 调用 c（嵌套在 if 块中）
    call_b = [c for c in calls if c.get("caller_name") == "b"]
    assert len(call_b) == 1
    assert call_b[0]["callee_name"] == "c"

    # c 是叶子，无调用
    call_c = [c for c in calls if c.get("caller_name") == "c"]
    assert len(call_c) == 0


def test_c_parser_caller_qualified_includes_module():
    """caller_qualified 应包含 module_path 前缀。"""
    parser = CParser()
    calls = _parse_source(parser, C_BASIC, "myapp::core")

    call_a = [c for c in calls if c.get("caller_name") == "a"][0]
    assert call_a["caller_qualified"] == "myapp::core.a"


def test_c_parser_deep_nesting_no_recursion_error():
    """P9 核心目标：深嵌套代码不应触发 RecursionError。

    构造 600 层 if 嵌套，临时把 sys.recursionlimit 降到 100，
    递归遍历必然爆栈，显式栈遍历应正常返回。
    """
    parser = CParser()
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(100)
    try:
        # 显式栈遍历不应抛 RecursionError
        calls = _parse_source(parser, C_DEEP_NESTING, "deep")
        # 顶层函数 top 调用 leaf，应被提取
        call_top = [c for c in calls if c.get("caller_name") == "top"]
        assert len(call_top) == 1
        assert call_top[0]["callee_name"] == "leaf"
    finally:
        sys.setrecursionlimit(old_limit)


def test_c_parser_parse_file_filters_stdlib():
    """走 parse_file 公共接口，标准库函数（如 printf）应被过滤。"""
    src = r"""
void run(void) {
    printf("hello");
    custom_helper();
}
"""
    with tempfile.TemporaryDirectory() as root:
        result = _write_and_parse_file(CParser(), root, "test.c", src)
        calls = result["raw_calls"]
        # printf 应被 should_filter_call 过滤
        callees = {c["callee_name"] for c in calls}
        assert "printf" not in callees
        # custom_helper 应保留
        assert "custom_helper" in callees


# ============================================
# C++ parser 测试
# ============================================

def test_cpp_parser_namespace_scope():
    """C++ namespace 内函数调用的 caller_qualified 应包含命名空间路径。"""
    parser = CppParser()
    calls = _parse_source(parser, CPP_NAMESPACE, "app")

    # app::free_func 调用 helper
    free_call = [c for c in calls if c.get("caller_name") == "free_func"]
    assert len(free_call) == 1
    assert free_call[0]["callee_name"] == "helper"
    # qualified 应含 module + scope
    assert "app" in free_call[0]["caller_qualified"]

    # inner::helper 调用 deep
    helper_call = [c for c in calls if c.get("caller_name") == "helper"]
    assert len(helper_call) == 1
    assert helper_call[0]["callee_name"] == "deep"
    # inner scope 应体现在 qualified_name 中
    assert "inner" in helper_call[0]["caller_qualified"]


def test_cpp_parser_class_method_scope():
    """C++ 类方法的调用关系应正确提取。"""
    parser = CppParser()
    calls = _parse_source(parser, CPP_CLASS, "svc")

    # Service::start 调用 init 和 run
    start_calls = [c for c in calls if c.get("caller_name") == "start"]
    callees_of_start = {c["callee_name"] for c in start_calls}
    assert "init" in callees_of_start
    assert "run" in callees_of_start

    # Service::run 调用 stop
    run_calls = [c for c in calls if c.get("caller_name") == "run"]
    assert len(run_calls) == 1
    assert run_calls[0]["callee_name"] == "stop"

    # 类名应体现在 qualified_name 中
    assert "Service" in start_calls[0]["caller_qualified"]


def test_cpp_parser_nested_namespace_class():
    """嵌套 namespace + class 的调用关系应正确提取。"""
    parser = CppParser()
    calls = _parse_source(parser, CPP_NESTED, "outer")

    render_calls = [c for c in calls if c.get("caller_name") == "render"]
    assert len(render_calls) == 1
    assert render_calls[0]["callee_name"] == "paint"
    # 命名空间 outer 和类名 Widget 都应体现在 qualified 中
    assert "outer" in render_calls[0]["caller_qualified"]
    assert "Widget" in render_calls[0]["caller_qualified"]


def test_cpp_parser_deep_nesting_no_recursion_error():
    """C++ parser 显式栈遍历同样不应爆栈。"""
    parser = CppParser()
    old_limit = sys.getrecursionlimit()
    sys.setrecursionlimit(100)
    try:
        calls = _parse_source(parser, C_DEEP_NESTING, "deep")
        call_top = [c for c in calls if c.get("caller_name") == "top"]
        assert len(call_top) == 1
        assert call_top[0]["callee_name"] == "leaf"
    finally:
        sys.setrecursionlimit(old_limit)


# ============================================
# 默认 ignore 规则测试
# ============================================

def test_default_ignore_thirdparty_dirs():
    """default_ignores 应包含 thirdParty/ / third_party/ / vendor/。"""
    db_root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(db_root, "cw.db"), workspace_root=db_root)
    try:
        patterns = db._load_ignore_patterns()
        assert "thirdParty/" in patterns
        assert "third_party/" in patterns
        assert "vendor/" in patterns
    finally:
        db.close()


def test_default_ignore_thirdparty_skipped_in_scan():
    """扫描时 thirdParty/third_party/vendor 目录应被跳过。"""
    root = tempfile.mkdtemp()
    # 在根目录创建第三方库目录
    for d in ("thirdParty", "third_party", "vendor"):
        sub = os.path.join(root, d)
        os.makedirs(sub, exist_ok=True)
        with open(os.path.join(sub, "lib.c"), "w") as f:
            f.write("void lib(void) {}\n")
    # 同时放一个项目内源文件
    with open(os.path.join(root, "main.c"), "w") as f:
        f.write("void main(void) {}\n")

    db = CodeGraphDB(os.path.join(root, "cw.db"), workspace_root=root)
    try:
        files = db._scan_supported_files()
        # 项目文件应被扫描到
        assert "main.c" in files
        # 第三方目录下的文件不应被扫描
        assert not any("thirdParty" in f for f in files)
        assert not any("third_party" in f for f in files)
        assert not any("vendor" in f for f in files)
    finally:
        db.close()
