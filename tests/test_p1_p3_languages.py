"""P1-P3 扩展语言 Parser 端到端验证测试

验证 5 种扩展语言的 tree-sitter 解析器是否正确工作：
- PHP：类、接口、方法、属性、namespace/use、调用关系
- Swift：类、结构体、协议、方法、init、import、调用关系
- Scala：类、Trait、Object、方法、package/import、调用关系
- HCL：resource/provider/variable/output 块、引用关系
- Elixir：defmodule、def、defp、alias/import、调用关系

运行方式:
    cd c:\\git_work\\callwarden\\scripts
    cw test test_p1_p3_languages

依赖:
    pip install tree-sitter-php tree-sitter-swift tree-sitter-scala \\
                tree-sitter-hcl tree-sitter-elixir
"""
import os
import sys
import tempfile

# 确保能导入 callwarden 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ====================================================================
# 测试样本代码
# ====================================================================

PHP_SAMPLE = '''<?php
namespace MyCompany\\Auth;

use MyApp\\Models\\User;

class UserService
{
    private $repo;

    public function __construct($repo)
    {
        $this->repo = $repo;
    }

    public function getUser(int $id): ?User
    {
        $user = $this->repo->findById($id);
        return $user;
    }

    public function getUserName(int $id): string
    {
        $user = $this->getUser($id);
        return $user->getName();
    }
}

interface UserRepositoryInterface
{
    public function findById(int $id);
}
'''

SWIFT_SAMPLE = '''import Foundation

class UserService {
    private let repo: UserRepository

    init(repo: UserRepository) {
        self.repo = repo
    }

    func getUser(id: Int) -> User? {
        let user = repo.findById(id: id)
        return user
    }

    func getUserName(id: Int) -> String {
        let user = getUser(id: id)
        return user.name
    }
}

protocol UserRepository {
    func findById(id: Int) -> User?
}

struct User {
    var name: String
    var id: Int
}
'''

SCALA_SAMPLE = '''package com.mycompany.auth

import com.mycompany.models.User

class UserService(repo: UserRepository) {
  def getUser(id: Int): Option[User] = {
    val user = repo.findById(id)
    user
  }

  def getUserName(id: Int): String = {
    val user = getUser(id)
    user.name
  }
}

trait UserRepository {
  def findById(id: Int): Option[User]
}

object UserService {
  def createDefault(): UserService = new UserService(DefaultRepo)
}
'''

HCL_SAMPLE = '''# Terraform 配置示例
terraform {
  required_version = ">= 1.0"
}

provider "aws" {
  region = "us-west-2"
}

resource "aws_instance" "web" {
  ami           = "ami-12345"
  instance_type = "t2.micro"

  tags = {
    Name = "WebServer"
  }
}

variable "instance_count" {
  default = 1
  type    = number
}

output "instance_ip" {
  value = aws_instance.web.public_ip
}
'''

ELIXIR_SAMPLE = '''defmodule MyCompany.Auth do
  defmodule UserService do
    def get_user(id) do
      user = Repo.find_by_id(id)
      user
    end

    def get_user_name(id) do
      user = get_user(id)
      user.name
    end
  end

  defmodule UserRepository do
    def find_by_id(id) do
      nil
    end
  end
end
'''


# ====================================================================
# 测试用例
# ====================================================================

def test_language_detection():
    """测试 1：语言检测（5 种新扩展名）"""
    print("--- 测试 1：语言检测 ---")
    from callwarden.config import detect_language_from_path

    assert detect_language_from_path("test.php") == "php", ".php 应识别为 php"
    assert detect_language_from_path("test.swift") == "swift", ".swift 应识别为 swift"
    assert detect_language_from_path("test.scala") == "scala", ".scala 应识别为 scala"
    assert detect_language_from_path("test.sc") == "scala", ".sc 应识别为 scala"
    assert detect_language_from_path("test.tf") == "hcl", ".tf 应识别为 hcl"
    assert detect_language_from_path("test.hcl") == "hcl", ".hcl 应识别为 hcl"
    assert detect_language_from_path("test.ex") == "elixir", ".ex 应识别为 elixir"
    assert detect_language_from_path("test.exs") == "elixir", ".exs 应识别为 elixir"
    print("PASS 测试 1：语言检测正确\n")


def test_create_parser_factory():
    """测试 2：create_parser 工厂分发"""
    print("--- 测试 2：create_parser 工厂 ---")
    from callwarden.parsers import (
        PhpParser, SwiftParser, ScalaParser, HclParser, ElixirParser,
        create_parser,
    )

    p_php = create_parser("test.php")
    p_swift = create_parser("test.swift")
    p_scala = create_parser("test.scala")
    p_hcl = create_parser("test.tf")
    p_elixir = create_parser("test.ex")

    assert isinstance(p_php, PhpParser)
    assert isinstance(p_swift, SwiftParser)
    assert isinstance(p_scala, ScalaParser)
    assert isinstance(p_hcl, HclParser)
    assert isinstance(p_elixir, ElixirParser)
    print("PASS 测试 2：工厂分发正确\n")


def test_php_parser():
    """测试 3：PHP 解析"""
    print("--- 测试 3：PHP 解析 ---")
    from callwarden.parsers import PhpParser

    with tempfile.NamedTemporaryFile(mode="w", suffix=".php", delete=False, encoding="utf-8") as f:
        f.write(PHP_SAMPLE)
        path = f.name

    try:
        parser = PhpParser()
        result = parser.parse_file(path)

        assert result["language"] == "php"
        symbols = result["symbols"]
        names = [s["name"] for s in symbols]

        def find_sym(name, kind):
            for s in symbols:
                if s["name"] == name and s["kind"] == kind:
                    return s
            return None

        # 验证符号
        assert find_sym("UserService", "class"), f"应找到 UserService class，symbols={names}"
        assert find_sym("UserRepositoryInterface", "interface"), \
            f"应找到 UserRepositoryInterface interface，symbols={names}"
        assert find_sym("__construct", "method"), "应找到 __construct 方法"
        assert find_sym("getUser", "method"), "应找到 getUser 方法"
        assert find_sym("getUserName", "method"), "应找到 getUserName 方法"
        assert find_sym("findById", "method"), "应找到 findById 方法"
        assert find_sym("repo", "property"), "应找到 repo 属性"

        # 验证 qualified_name
        us = find_sym("UserService", "class")
        assert "Auth.UserService" in us["qualified_name"], \
            f"UserService qualified_name 应包含 Auth：{us['qualified_name']}"

        # 验证 import（use 语句）
        assert len(result["imports"]) >= 1, f"应有至少 1 个 use，实际 {len(result['imports'])}"

        print(f"  symbols: {len(symbols)}, imports: {len(result['imports'])}, calls: {len(result['raw_calls'])}")
        print("PASS 测试 3：PHP 解析正确\n")
    finally:
        os.unlink(path)


def test_swift_parser():
    """测试 4：Swift 解析"""
    print("--- 测试 4：Swift 解析 ---")
    from callwarden.parsers import SwiftParser

    with tempfile.NamedTemporaryFile(mode="w", suffix=".swift", delete=False, encoding="utf-8") as f:
        f.write(SWIFT_SAMPLE)
        path = f.name

    try:
        parser = SwiftParser()
        result = parser.parse_file(path)

        assert result["language"] == "swift"
        symbols = result["symbols"]
        names = [s["name"] for s in symbols]

        def find_sym(name, kind):
            for s in symbols:
                if s["name"] == name and s["kind"] == kind:
                    return s
            return None

        # 验证符号
        assert find_sym("UserService", "class"), f"应找到 UserService class，symbols={names}"
        assert find_sym("UserRepository", "protocol"), \
            f"应找到 UserRepository protocol，symbols={names}"
        # Swift struct 可能被解析为 class 或 struct，这里两种都接受
        user_sym = find_sym("User", "struct") or find_sym("User", "class")
        assert user_sym, f"应找到 User struct/class，symbols={names}"
        assert find_sym("init", "constructor"), "应找到 init 构造方法"
        assert find_sym("getUser", "function"), "应找到 getUser 函数"
        assert find_sym("getUserName", "function"), "应找到 getUserName 函数"
        assert find_sym("findById", "function"), "应找到 findById 函数"

        # 验证 import
        assert len(result["imports"]) >= 1, f"应有至少 1 个 import，实际 {len(result['imports'])}"
        assert result["imports"][0]["module"] == "Foundation"

        print(f"  symbols: {len(symbols)}, imports: {len(result['imports'])}, calls: {len(result['raw_calls'])}")
        print("PASS 测试 4：Swift 解析正确\n")
    finally:
        os.unlink(path)


def test_scala_parser():
    """测试 5：Scala 解析"""
    print("--- 测试 5：Scala 解析 ---")
    from callwarden.parsers import ScalaParser

    with tempfile.NamedTemporaryFile(mode="w", suffix=".scala", delete=False, encoding="utf-8") as f:
        f.write(SCALA_SAMPLE)
        path = f.name

    try:
        parser = ScalaParser()
        result = parser.parse_file(path)

        assert result["language"] == "scala"
        symbols = result["symbols"]
        names = [s["name"] for s in symbols]

        def find_sym(name, kind):
            for s in symbols:
                if s["name"] == name and s["kind"] == kind:
                    return s
            return None

        # 验证符号
        assert find_sym("UserService", "class"), f"应找到 UserService class，symbols={names}"
        assert find_sym("UserRepository", "trait"), f"应找到 UserRepository trait，symbols={names}"
        assert find_sym("UserService", "object"), f"应找到 UserService object，symbols={names}"
        assert find_sym("getUser", "method"), "应找到 getUser 方法"
        assert find_sym("getUserName", "method"), "应找到 getUserName 方法"
        assert find_sym("findById", "method"), "应找到 findById 方法"
        assert find_sym("createDefault", "method"), "应找到 createDefault 方法"

        # 验证 qualified_name 包含包名
        us = find_sym("UserService", "class")
        assert "auth.UserService" in us["qualified_name"], \
            f"UserService qualified_name 应包含 auth：{us['qualified_name']}"

        # 验证 import
        assert len(result["imports"]) >= 1, f"应有至少 1 个 import，实际 {len(result['imports'])}"

        print(f"  symbols: {len(symbols)}, imports: {len(result['imports'])}, calls: {len(result['raw_calls'])}")
        print("PASS 测试 5：Scala 解析正确\n")
    finally:
        os.unlink(path)


def test_hcl_parser():
    """测试 6：HCL/Terraform 解析"""
    print("--- 测试 6：HCL/Terraform 解析 ---")
    from callwarden.parsers import HclParser

    with tempfile.NamedTemporaryFile(mode="w", suffix=".tf", delete=False, encoding="utf-8") as f:
        f.write(HCL_SAMPLE)
        path = f.name

    try:
        parser = HclParser()
        result = parser.parse_file(path)

        assert result["language"] == "hcl"
        symbols = result["symbols"]
        names = [(s["name"], s["kind"]) for s in symbols]

        def find_sym(name, kind):
            for s in symbols:
                if s["name"] == name and s["kind"] == kind:
                    return s
            return None

        # 验证块符号
        assert find_sym("aws_instance.web", "resource"), \
            f"应找到 aws_instance.web resource，symbols={names}"
        assert find_sym("aws", "provider"), "应找到 aws provider"
        assert find_sym("instance_count", "variable"), "应找到 instance_count variable"
        assert find_sym("instance_ip", "output"), "应找到 instance_ip output"
        assert find_sym("terraform", "terraform"), "应找到 terraform 块"

        # HCL 没有 import
        assert len(result["imports"]) == 0

        # 验证调用关系（output 引用 aws_instance.web.public_ip）
        calls = result["raw_calls"]
        assert len(calls) >= 1, f"应有至少 1 个引用，实际 {len(calls)}"

        print(f"  symbols: {len(symbols)}, imports: {len(result['imports'])}, calls: {len(calls)}")
        print("PASS 测试 6：HCL/Terraform 解析正确\n")
    finally:
        os.unlink(path)


def test_elixir_parser():
    """测试 7：Elixir 解析"""
    print("--- 测试 7：Elixir 解析 ---")
    from callwarden.parsers import ElixirParser

    with tempfile.NamedTemporaryFile(mode="w", suffix=".ex", delete=False, encoding="utf-8") as f:
        f.write(ELIXIR_SAMPLE)
        path = f.name

    try:
        parser = ElixirParser()
        result = parser.parse_file(path)

        assert result["language"] == "elixir"
        symbols = result["symbols"]
        names = [s["name"] for s in symbols]

        def find_sym(name, kind):
            for s in symbols:
                if s["name"] == name and s["kind"] == kind:
                    return s
            return None

        # 验证模块符号
        assert find_sym("MyCompany.Auth", "module"), \
            f"应找到 MyCompany.Auth module，symbols={names}"
        # 嵌套模块
        nested_modules = [s for s in symbols if s["kind"] == "module" and "UserService" in s["name"]]
        assert len(nested_modules) >= 1, f"应找到 UserService 嵌套模块，symbols={names}"

        # 验证函数符号
        assert find_sym("get_user", "function"), "应找到 get_user 函数"
        assert find_sym("get_user_name", "function"), "应找到 get_user_name 函数"
        assert find_sym("find_by_id", "function"), "应找到 find_by_id 函数"

        # 验证调用关系（get_user_name 调用 get_user）
        calls = result["raw_calls"]
        callee_names = [c["callee_name"] for c in calls]
        assert "get_user" in callee_names, f"应找到对 get_user 的调用，calls={callee_names}"

        print(f"  symbols: {len(symbols)}, imports: {len(result['imports'])}, calls: {len(calls)}")
        print("PASS 测试 7：Elixir 解析正确\n")
    finally:
        os.unlink(path)


def test_install_p1_p3_packages():
    """测试 8：install.py P1-P3 包定义完整性"""
    print("--- 测试 8：install.py P1-P3 包定义 ---")
    from callwarden.install import (
        P1_LANGUAGE_PACKAGES, P2_LANGUAGE_PACKAGES, P3_LANGUAGE_PACKAGES,
    )

    assert len(P1_LANGUAGE_PACKAGES) == 2, f"P1 应有 2 个包，实际 {len(P1_LANGUAGE_PACKAGES)}"
    assert len(P2_LANGUAGE_PACKAGES) == 2, f"P2 应有 2 个包，实际 {len(P2_LANGUAGE_PACKAGES)}"
    assert len(P3_LANGUAGE_PACKAGES) == 1, f"P3 应有 1 个包，实际 {len(P3_LANGUAGE_PACKAGES)}"

    p1_langs = [p.language for p in P1_LANGUAGE_PACKAGES]
    assert "php" in p1_langs and "swift" in p1_langs

    p2_langs = [p.language for p in P2_LANGUAGE_PACKAGES]
    assert "scala" in p2_langs and "hcl" in p2_langs

    p3_langs = [p.language for p in P3_LANGUAGE_PACKAGES]
    assert "elixir" in p3_langs

    print("PASS 测试 8：install.py P1-P3 包定义正确\n")


def test_db_build_integration():
    """测试 9：db_build.py 工厂分支集成"""
    print("--- 测试 9：db_build 工厂集成 ---")
    import inspect
    from callwarden.db import db_build

    source = inspect.getsource(db_build)
    for parser_name in ["PhpParser", "SwiftParser", "ScalaParser", "HclParser", "ElixirParser"]:
        assert parser_name in source, f"db_build 应引用 {parser_name}"
    for lang in ['"php"', '"swift"', '"scala"', '"hcl"', '"elixir"']:
        assert lang in source or lang.replace('"', "'") in source, f"db_build 应有 {lang} 分支"

    print("PASS 测试 9：db_build 工厂集成正确\n")


# ====================================================================
# 主入口
# ====================================================================

def main():
    print("=" * 60)
    print("P1-P3 扩展语言 Parser 端到端验证测试")
    print("=" * 60)
    print()
    test_language_detection()
    test_create_parser_factory()
    test_php_parser()
    test_swift_parser()
    test_scala_parser()
    test_hcl_parser()
    test_elixir_parser()
    test_install_p1_p3_packages()
    test_db_build_integration()
    print("=" * 60)
    print("=== ALL P1_P3 LANGUAGE TESTS PASSED ===")
    print("=" * 60)


if __name__ == "__main__":
    main()
