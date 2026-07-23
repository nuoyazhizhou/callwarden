"""C# 和 Ruby Parser 端到端验证测试

验证 P0 扩展语言（C# / Ruby）的 tree-sitter 解析器是否正确工作：
- 语言检测（.cs / .rb 扩展名）
- create_parser 工厂分发
- C# 解析：命名空间、类、接口、方法、构造方法、属性、using、调用关系
- Ruby 解析：module、class、方法、require、调用关系
- install.py 一键安装器核心组件

运行方式:
    cd c:\\git_work\\callwarden\\scripts
    cw test test_csharp_ruby

依赖:
    pip install tree-sitter-c-sharp tree-sitter-ruby
"""
import os
import sys
import tempfile

# 确保能导入 callwarden 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ====================================================================
# 测试样本代码
# ====================================================================

C_SHARP_SAMPLE = '''using System;
using System.Collections.Generic;

namespace MyCompany.MyApp
{
    /// <summary>
    /// 用户服务类
    /// </summary>
    public class UserService
    {
        private readonly IUserRepository _repo;

        public UserService(IUserRepository repo)
        {
            _repo = repo;
        }

        public User GetUser(int id)
        {
            var user = _repo.FindById(id);
            if (user == null)
            {
                throw new Exception("User not found");
            }
            return user;
        }

        public string GetUserName(int id)
        {
            var user = GetUser(id);
            return user.Name;
        }
    }

    public interface IUserRepository
    {
        User FindById(int id);
    }

    public class User
    {
        public string Name { get; set; }
        public int Id { get; set; }
    }
}
'''

RUBY_SAMPLE = '''require "json"
require_relative "user"

module MyCompany
  module Auth
    # 用户认证服务
    class UserService
      def initialize(repo)
        @repo = repo
      end

      def find_user(id)
        user = @repo.find_by_id(id)
        raise "Not found" unless user
        user
      end

      def user_name(id)
        user = find_user(id)
        user.name
      end

      def self.create_default
        UserService.new(DefaultRepo.new)
      end
    end
  end
end
'''


# ====================================================================
# 测试用例
# ====================================================================

def test_language_detection():
    """测试 1：语言检测（.cs / .rb 扩展名）"""
    print("--- 测试 1：语言检测 ---")
    from callwarden.config import detect_language_from_path

    assert detect_language_from_path("test.cs") == "csharp", ".cs 应识别为 csharp"
    assert detect_language_from_path("test.rb") == "ruby", ".rb 应识别为 ruby"
    assert detect_language_from_path("Program.cs") == "csharp"
    assert detect_language_from_path("app.rb") == "ruby"
    # 大小写不敏感
    assert detect_language_from_path("Model.CS") == "csharp"
    assert detect_language_from_path("Gemfile.RB") == "ruby"
    print("PASS 测试 1：语言检测正确\n")


def test_create_parser_factory():
    """测试 2：create_parser 工厂分发"""
    print("--- 测试 2：create_parser 工厂 ---")
    from callwarden.parsers import CSharpParser, RubyParser, create_parser

    p_cs = create_parser("Program.cs")
    p_rb = create_parser("app.rb")
    assert p_cs is not None, "C# parser 不应为 None"
    assert p_rb is not None, "Ruby parser 不应为 None"
    assert isinstance(p_cs, CSharpParser), f"应为 CSharpParser，实际 {type(p_cs).__name__}"
    assert isinstance(p_rb, RubyParser), f"应为 RubyParser，实际 {type(p_rb).__name__}"

    # 验证缓存生效（同一扩展名返回同一实例）
    p_cs2 = create_parser("Other.cs")
    assert p_cs is p_cs2, "create_parser 应缓存解析器实例"
    print("PASS 测试 2：工厂分发正确\n")


def test_csharp_parser():
    """测试 3：C# 解析（命名空间、类、接口、方法、构造方法、属性、using、调用）"""
    print("--- 测试 3：C# 解析 ---")
    from callwarden.parsers import CSharpParser

    with tempfile.NamedTemporaryFile(mode="w", suffix=".cs", delete=False, encoding="utf-8") as f:
        f.write(C_SHARP_SAMPLE)
        cs_path = f.name

    try:
        parser = CSharpParser()
        result = parser.parse_file(cs_path)

        # 基本字段
        assert result["language"] == "csharp", f"language 应为 csharp，实际 {result['language']}"
        assert result["total_lines"] > 0, "total_lines 应大于 0"
        print(f"  total_lines: {result['total_lines']}")
        print(f"  symbols:     {len(result['symbols'])}")
        print(f"  imports:     {len(result['imports'])}")
        print(f"  raw_calls:   {len(result['raw_calls'])}")

        # 符号验证
        symbols = result["symbols"]
        symbol_names = [s["name"] for s in symbols]

        # 按 (name, kind) 建索引，避免同名符号（如 class UserService 和 constructor UserService）互相覆盖
        def find_sym(name, kind):
            for s in symbols:
                if s["name"] == name and s["kind"] == kind:
                    return s
            return None

        def assert_sym(name, kind, qualified=None):
            s = find_sym(name, kind)
            assert s is not None, f"应找到 {kind} {name}"
            if qualified:
                assert s["qualified_name"] == qualified, \
                    f"{kind} {name} qualified_name 应为 {qualified}，实际 {s['qualified_name']}"

        # 应找到的符号
        assert "UserService" in symbol_names, "应找到 UserService 类"
        assert "IUserRepository" in symbol_names, "应找到 IUserRepository 接口"
        assert "User" in symbol_names, "应找到 User 类"
        assert "GetUser" in symbol_names, "应找到 GetUser 方法"
        assert "GetUserName" in symbol_names, "应找到 GetUserName 方法"
        assert "FindById" in symbol_names, "应找到 FindById 方法"
        assert "Name" in symbol_names, "应找到 Name 属性"
        assert "Id" in symbol_names, "应找到 Id 属性"

        # kind 与 qualified_name 联合验证
        assert_sym("UserService", "class", "MyCompany.MyApp.UserService")
        assert_sym("UserService", "constructor", "MyCompany.MyApp.UserService.UserService")
        assert_sym("IUserRepository", "interface", "MyCompany.MyApp.IUserRepository")
        assert_sym("User", "class", "MyCompany.MyApp.User")
        assert_sym("GetUser", "method", "MyCompany.MyApp.UserService.GetUser")
        assert_sym("GetUserName", "method", "MyCompany.MyApp.UserService.GetUserName")
        assert_sym("FindById", "method", "MyCompany.MyApp.IUserRepository.FindById")
        assert_sym("Name", "property", "MyCompany.MyApp.User.Name")
        assert_sym("Id", "property", "MyCompany.MyApp.User.Id")

        # 构造方法验证
        constructors = [s for s in symbols if s["kind"] == "constructor"]
        assert len(constructors) == 1, f"应有 1 个构造方法，实际 {len(constructors)}"

        # import 验证（2 个 using）
        imports = result["imports"]
        assert len(imports) == 2, f"应有 2 个 using，实际 {len(imports)}"
        assert imports[0]["module"] == "System"
        assert imports[1]["module"] == "System.Collections.Generic"

        # 调用关系验证
        raw_calls = result["raw_calls"]
        assert len(raw_calls) >= 2, f"应有至少 2 个调用，实际 {len(raw_calls)}"
        # GetUserName 内部调用了 GetUser
        callee_names = [c["callee_name"] for c in raw_calls]
        assert "GetUser" in callee_names, "应找到对 GetUser 的调用"

        print(f"  symbol names: {symbol_names}")
        print("PASS 测试 3：C# 解析正确\n")
    finally:
        os.unlink(cs_path)


def test_ruby_parser():
    """测试 4：Ruby 解析（module、class、方法、require、调用）"""
    print("--- 测试 4：Ruby 解析 ---")
    from callwarden.parsers import RubyParser

    with tempfile.NamedTemporaryFile(mode="w", suffix=".rb", delete=False, encoding="utf-8") as f:
        f.write(RUBY_SAMPLE)
        rb_path = f.name

    try:
        parser = RubyParser()
        result = parser.parse_file(rb_path)

        # 基本字段
        assert result["language"] == "ruby", f"language 应为 ruby，实际 {result['language']}"
        assert result["total_lines"] > 0, "total_lines 应大于 0"
        print(f"  total_lines: {result['total_lines']}")
        print(f"  symbols:     {len(result['symbols'])}")
        print(f"  imports:     {len(result['imports'])}")
        print(f"  raw_calls:   {len(result['raw_calls'])}")

        # 符号验证
        symbols = result["symbols"]
        symbol_names = [s["name"] for s in symbols]
        symbol_kinds = {s["name"]: s["kind"] for s in symbols}

        # 应找到的符号
        assert "MyCompany" in symbol_names, "应找到 MyCompany 模块"
        assert "Auth" in symbol_names, "应找到 Auth 模块"
        assert "UserService" in symbol_names, "应找到 UserService 类"
        assert "find_user" in symbol_names, "应找到 find_user 方法"
        assert "user_name" in symbol_names, "应找到 user_name 方法"
        assert "create_default" in symbol_names, "应找到 create_default 类方法"

        # kind 验证
        assert symbol_kinds["MyCompany"] == "module", \
            f"MyCompany 应为 module，实际 {symbol_kinds['MyCompany']}"
        assert symbol_kinds["Auth"] == "module", \
            f"Auth 应为 module，实际 {symbol_kinds['Auth']}"
        assert symbol_kinds["UserService"] == "class", \
            f"UserService 应为 class，实际 {symbol_kinds['UserService']}"
        assert symbol_kinds["find_user"] == "method", \
            f"find_user 应为 method，实际 {symbol_kinds['find_user']}"
        assert symbol_kinds["user_name"] == "method", \
            f"user_name 应为 method，实际 {symbol_kinds['user_name']}"

        # qualified_name 验证（嵌套模块/类应正确拼接）
        symbol_quals = {s["name"]: s["qualified_name"] for s in symbols}
        assert "MyCompany.MyCompany" != symbol_quals.get("MyCompany", ""), \
            "MyCompany qualified_name 不应重复"
        # UserService 应在 Auth 模块下
        us_quals = [s for s in symbols if s["name"] == "UserService"][0]["qualified_name"]
        assert "Auth" in us_quals, f"UserService qualified_name 应包含 Auth：{us_quals}"
        assert "MyCompany" in us_quals, f"UserService qualified_name 应包含 MyCompany：{us_quals}"

        # import 验证（require / require_relative）
        imports = result["imports"]
        assert len(imports) == 2, f"应有 2 个 require，实际 {len(imports)}"
        modules = [imp["module"] for imp in imports]
        assert "json" in modules, "应 require json"
        assert "user" in modules, "应 require_relative user"

        # 调用关系验证
        raw_calls = result["raw_calls"]
        assert len(raw_calls) >= 2, f"应有至少 2 个调用，实际 {len(raw_calls)}"
        # user_name 内部调用了 find_user
        callee_names = [c["callee_name"] for c in raw_calls]
        assert "find_user" in callee_names, "应找到对 find_user 的调用"

        print(f"  symbol names: {symbol_names}")
        print("PASS 测试 4：Ruby 解析正确\n")
    finally:
        os.unlink(rb_path)


def test_install_script():
    """测试 5：install.py 一键安装器核心组件"""
    print("--- 测试 5：install.py 安装器 ---")
    from callwarden.install import (
        CallWardenInstaller, PackageSpec,
        CORE_PACKAGES, SUPPORTED_LANGUAGE_PACKAGES,
        EXTENDED_LANGUAGE_PACKAGES, OPTIONAL_PACKAGES,
    )

    # 验证包定义完整性
    assert len(CORE_PACKAGES) >= 3, f"核心包应至少有 3 个，实际 {len(CORE_PACKAGES)}"
    assert len(SUPPORTED_LANGUAGE_PACKAGES) == 9, \
        f"已支持语言应有 9 个，实际 {len(SUPPORTED_LANGUAGE_PACKAGES)}"
    assert len(EXTENDED_LANGUAGE_PACKAGES) == 2, \
        f"P0 扩展语言应有 2 个，实际 {len(EXTENDED_LANGUAGE_PACKAGES)}"

    # 验证 C# 和 Ruby 在扩展语言中
    ext_langs = [p.language for p in EXTENDED_LANGUAGE_PACKAGES]
    assert "csharp" in ext_langs, "扩展语言应包含 csharp"
    assert "ruby" in ext_langs, "扩展语言应包含 ruby"

    # 验证 PackageSpec 字段
    for spec in CORE_PACKAGES + SUPPORTED_LANGUAGE_PACKAGES + EXTENDED_LANGUAGE_PACKAGES:
        assert spec.pip_name, f"{spec} 缺 pip_name"
        assert spec.import_name, f"{spec} 缺 import_name"
        assert spec.category in ("core", "language", "optional"), \
            f"{spec} category 非法：{spec.category}"
        assert spec.description, f"{spec} 缺 description"

    # 验证 installer 实例化
    installer = CallWardenInstaller()
    assert installer.result.total == 0
    assert installer.result.installed == 0

    # 验证 _is_package_installed 方法对已安装的包返回 True
    # tree-sitter 应该已安装（测试前置条件）
    ts_spec = PackageSpec("tree-sitter", "tree_sitter", "core", description="test")
    assert installer._is_package_installed(ts_spec) is True, \
        "tree-sitter 应已安装（测试前置条件）"

    # 验证对未安装的包返回 False（用一个不存在的模块名）
    fake_spec = PackageSpec("nonexistent-pkg-xyz", "nonexistent_module_xyz", "optional")
    assert installer._is_package_installed(fake_spec) is False, \
        "不存在的包应返回 False"

    print("PASS 测试 5：install.py 安装器正确\n")


def test_db_build_integration():
    """测试 6：db_build.py 工厂分支集成验证"""
    print("--- 测试 6：db_build 工厂集成 ---")
    # 验证 db_build._parse_one 函数中包含 C# / Ruby 分支
    import inspect
    from callwarden.db import db_build

    source = inspect.getsource(db_build)
    assert "CSharpParser" in source, "db_build 应引用 CSharpParser"
    assert "RubyParser" in source, "db_build 应引用 RubyParser"
    assert '"csharp"' in source or "'csharp'" in source, "db_build 应有 csharp 分支"
    assert '"ruby"' in source or "'ruby'" in source, "db_build 应有 ruby 分支"
    print("PASS 测试 6：db_build 工厂集成正确\n")


# ====================================================================
# 主入口
# ====================================================================

def main():
    print("=" * 60)
    print("C# 和 Ruby Parser 端到端验证测试")
    print("=" * 60)
    print()
    test_language_detection()
    test_create_parser_factory()
    test_csharp_parser()
    test_ruby_parser()
    test_install_script()
    test_db_build_integration()
    print("=" * 60)
    print("=== ALL CSHARP_RUBY TESTS PASSED ===")
    print("=" * 60)


if __name__ == "__main__":
    main()
