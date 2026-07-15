"""Kotlin 和 Go Parser 端到端验证测试

H13 补齐：覆盖 16 种语言中最后缺专门测试的 Kotlin + Go。

验证内容：
- 语言检测（.kt / .go 扩展名）
- create_parser 工厂分发
- Kotlin 解析：package、class、function、import、调用关系
- Go 解析：package、struct、interface、method、function、import、调用关系
- db_build 工厂分支集成

运行方式:
    cd c:\\git_work\\callwarden
    cw test test_kotlin_go

依赖:
    pip install tree-sitter-kotlin tree-sitter-go
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ====================================================================
# 测试样本代码
# ====================================================================

KOTLIN_SAMPLE = '''package com.example.app

import kotlin.collections.List
import java.io.File

class UserService {
    fun findUser(id: Int): String {
        val name = getName(id)
        return name
    }

    fun getName(id: Int): String {
        return "user_$id"
    }
}

fun main() {
    val svc = UserService()
    println(svc.findUser(1))
}
'''

GO_SAMPLE = '''package main

import (
    "fmt"
    "strings"
)

type User struct {
    Name string
    Id   int
}

type UserRepository interface {
    FindById(id int) User
}

func (repo *defaultRepo) FindById(id int) User {
    return User{Name: "test", Id: id}
}

func GetUser(id int) string {
    repo := &defaultRepo{}
    user := repo.FindById(id)
    return user.Name
}

func main() {
    fmt.Println(GetUser(1))
    strings.ToLower("X")
}
'''


# ====================================================================
# 测试用例
# ====================================================================

def test_language_detection():
    """测试 1：语言检测（.kt / .go 扩展名）"""
    from callwarden.config import detect_language_from_path

    assert detect_language_from_path("test.kt") == "kotlin", ".kt 应识别为 kotlin"
    assert detect_language_from_path("test.go") == "go", ".go 应识别为 go"
    assert detect_language_from_path("Main.kt") == "kotlin"
    assert detect_language_from_path("main.go") == "go"
    # 大小写不敏感
    assert detect_language_from_path("Model.KT") == "kotlin"
    assert detect_language_from_path("App.GO") == "go"


def test_create_parser_factory():
    """测试 2：create_parser 工厂分发"""
    from callwarden.parsers import KotlinParser, GoParser, create_parser

    p_kt = create_parser("Main.kt")
    p_go = create_parser("main.go")
    assert p_kt is not None, "Kotlin parser 不应为 None"
    assert p_go is not None, "Go parser 不应为 None"
    assert isinstance(p_kt, KotlinParser), f"应为 KotlinParser，实际 {type(p_kt).__name__}"
    assert isinstance(p_go, GoParser), f"应为 GoParser，实际 {type(p_go).__name__}"

    # 验证缓存生效（同一扩展名返回同一实例）
    p_kt2 = create_parser("Other.kt")
    assert p_kt is p_kt2, "create_parser 应缓存解析器实例"


def test_kotlin_parser():
    """测试 3：Kotlin 解析（package、class、function、import、调用）"""
    from callwarden.parsers import KotlinParser

    with tempfile.NamedTemporaryFile(mode="w", suffix=".kt", delete=False, encoding="utf-8") as f:
        f.write(KOTLIN_SAMPLE)
        kt_path = f.name

    try:
        parser = KotlinParser()
        result = parser.parse_file(kt_path)

        # 基本字段
        assert result["language"] == "kotlin", f"language 应为 kotlin，实际 {result['language']}"
        assert result["total_lines"] > 0, "total_lines 应大于 0"

        # 符号验证
        symbols = result["symbols"]
        symbol_names = [s["name"] for s in symbols]
        symbol_kinds = {s["name"]: s["kind"] for s in symbols}

        # 应找到的符号
        assert "UserService" in symbol_names, "应找到 UserService 类"
        assert "findUser" in symbol_names, "应找到 findUser 函数"
        assert "getName" in symbol_names, "应找到 getName 函数"
        assert "main" in symbol_names, "应找到 main 函数"

        # kind 验证
        assert symbol_kinds["UserService"] == "class", \
            f"UserService 应为 class，实际 {symbol_kinds['UserService']}"
        assert symbol_kinds["findUser"] == "fn", \
            f"findUser 应为 fn，实际 {symbol_kinds['findUser']}"
        assert symbol_kinds["main"] == "fn", \
            f"main 应为 fn，实际 {symbol_kinds['main']}"

        # qualified_name 验证（含 package 前缀）
        symbol_quals = {s["name"]: s["qualified_name"] for s in symbols}
        assert symbol_quals["UserService"] == "com.example.app.UserService", \
            f"UserService qn 应含 package，实际 {symbol_quals['UserService']}"
        assert symbol_quals["main"] == "com.example.app.main", \
            f"main qn 应含 package，实际 {symbol_quals['main']}"
        # 类内方法 qn 应含类名
        assert "UserService.findUser" in symbol_quals["findUser"], \
            f"findUser qn 应含 UserService，实际 {symbol_quals['findUser']}"

        # import 验证（2 个 import）
        imports = result["imports"]
        assert len(imports) == 2, f"应有 2 个 import，实际 {len(imports)}"
        modules = [imp["module"] for imp in imports]
        assert "kotlin.collections.List" in modules, "应 import kotlin.collections.List"
        assert "java.io.File" in modules, "应 import java.io.File"

        # 调用关系验证
        raw_calls = result["raw_calls"]
        assert len(raw_calls) >= 2, f"应有至少 2 个调用，实际 {len(raw_calls)}"
        callee_names = [c["callee_name"] for c in raw_calls]
        # findUser 内部调用了 getName
        assert "getName" in callee_names, "应找到对 getName 的调用"
        # main 内部调用了 findUser
        assert "findUser" in callee_names, "应找到对 findUser 的调用"
    finally:
        os.unlink(kt_path)


def test_go_parser():
    """测试 4：Go 解析（package、struct、interface、method、function、import、调用）"""
    from callwarden.parsers import GoParser

    with tempfile.NamedTemporaryFile(mode="w", suffix=".go", delete=False, encoding="utf-8") as f:
        f.write(GO_SAMPLE)
        go_path = f.name

    try:
        parser = GoParser()
        result = parser.parse_file(go_path)

        # 基本字段
        assert result["language"] == "go", f"language 应为 go，实际 {result['language']}"
        assert result["total_lines"] > 0, "total_lines 应大于 0"

        # 符号验证
        symbols = result["symbols"]
        symbol_names = [s["name"] for s in symbols]
        symbol_kinds = {s["name"]: s["kind"] for s in symbols}

        # 应找到的符号
        assert "User" in symbol_names, "应找到 User 结构体"
        assert "UserRepository" in symbol_names, "应找到 UserRepository 接口"
        assert "FindById" in symbol_names, "应找到 FindById 方法"
        assert "GetUser" in symbol_names, "应找到 GetUser 函数"
        assert "main" in symbol_names, "应找到 main 函数"

        # kind 验证
        assert symbol_kinds["User"] == "struct", \
            f"User 应为 struct，实际 {symbol_kinds['User']}"
        assert symbol_kinds["UserRepository"] == "interface", \
            f"UserRepository 应为 interface，实际 {symbol_kinds['UserRepository']}"
        assert symbol_kinds["FindById"] == "method", \
            f"FindById 应为 method，实际 {symbol_kinds['FindById']}"
        assert symbol_kinds["GetUser"] == "fn", \
            f"GetUser 应为 fn，实际 {symbol_kinds['GetUser']}"
        assert symbol_kinds["main"] == "fn", \
            f"main 应为 fn，实际 {symbol_kinds['main']}"

        # qualified_name 验证（含 package 前缀）
        symbol_quals = {s["name"]: s["qualified_name"] for s in symbols}
        assert symbol_quals["User"] == "main.User", \
            f"User qn 应含 package，实际 {symbol_quals['User']}"
        assert symbol_quals["UserRepository"] == "main.UserRepository", \
            f"UserRepository qn 应含 package，实际 {symbol_quals['UserRepository']}"
        assert symbol_quals["main"] == "main.main", \
            f"main qn 应含 package，实际 {symbol_quals['main']}"

        # import 验证（2 个 import）
        imports = result["imports"]
        assert len(imports) == 2, f"应有 2 个 import，实际 {len(imports)}"
        modules = [imp["module"] for imp in imports]
        assert "fmt" in modules, "应 import fmt"
        assert "strings" in modules, "应 import strings"

        # 调用关系验证
        raw_calls = result["raw_calls"]
        assert len(raw_calls) >= 2, f"应有至少 2 个调用，实际 {len(raw_calls)}"
        callee_names = [c["callee_name"] for c in raw_calls]
        # GetUser 内部调用了 FindById
        assert "FindById" in callee_names, "应找到对 FindById 的调用"
        # main 内部调用了 GetUser
        assert "GetUser" in callee_names, "应找到对 GetUser 的调用"
    finally:
        os.unlink(go_path)


def test_db_build_integration():
    """测试 5：db_build.py 工厂分支集成验证"""
    import inspect
    from callwarden.db import db_build

    source = inspect.getsource(db_build)
    assert "KotlinParser" in source, "db_build 应引用 KotlinParser"
    assert "GoParser" in source, "db_build 应引用 GoParser"
    assert '"kotlin"' in source or "'kotlin'" in source, "db_build 应有 kotlin 分支"
    assert '"go"' in source or "'go'" in source, "db_build 应有 go 分支"


# ====================================================================
# 主入口
# ====================================================================

def main():
    print("=" * 60)
    print("Kotlin 和 Go Parser 端到端验证测试")
    print("=" * 60)
    print()
    test_language_detection()
    print("PASS test_language_detection")
    test_create_parser_factory()
    print("PASS test_create_parser_factory")
    test_kotlin_parser()
    print("PASS test_kotlin_parser")
    test_go_parser()
    print("PASS test_go_parser")
    test_db_build_integration()
    print("PASS test_db_build_integration")
    print("=" * 60)
    print("=== ALL KOTLIN_GO TESTS PASSED ===")
    print("=" * 60)


if __name__ == "__main__":
    main()
