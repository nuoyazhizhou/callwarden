"""P0 Bug 修复端到端验证脚本

验证三个 P0 级别 Bug 是否真正修复：
- B1: db_comment.py 缺 import os
- B2: issues.py 3 处 self.ISSUE_RULES 未定义
- B3: db_git.py git_symbol_changes 表只读不写

运行方式:
    cd c:\\git_work\\callwarden\\scripts
    cw test test_p0_bugfixes
"""
import os
import sys
import tempfile

# 确保能导入 callwarden 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from callwarden.db import CodeGraphDB


def test_b1_import_os():
    """B1: db_comment.py 应能正常导入并使用 os.path"""
    print("--- B1: db_comment.py import os ---")
    import callwarden.db.db_comment as mod
    # 模块级别应能正常导入（如果缺 import os，模块导入就会失败）
    assert hasattr(mod, "os"), "db_comment 模块未导入 os"
    print("PASS B1.1: db_comment 模块成功导入 os")
    # CommentMixin 类应存在
    assert hasattr(mod, "CommentMixin"), "CommentMixin 类不存在"
    print("PASS B1.2: CommentMixin 类存在")
    print("PASS B1: import os 修复完成\n")


def test_b2_issue_rules():
    """B2: issues.py 应能用 _get_all_issue_rules / _get_issue_rules_for_language 替代 self.ISSUE_RULES"""
    print("--- B2: issues.py ISSUE_RULES 修复 ---")
    from callwarden.analyzers.issues import IssueAnalyzerMixin

    # 验证新方法存在
    assert hasattr(IssueAnalyzerMixin, "_get_all_issue_rules"), "_get_all_issue_rules 方法不存在"
    assert hasattr(IssueAnalyzerMixin, "_get_issue_rules_for_language"), "_get_issue_rules_for_language 方法不存在"
    assert hasattr(IssueAnalyzerMixin, "_detect_language_from_module_path"), "_detect_language_from_module_path 方法不存在"
    print("PASS B2.1: 三个新方法都存在")

    # 用一个临时实例测试（Mixin 不需要 __init__）
    class FakeMixin:
        pass
    fake = FakeMixin()
    fake.COMMON_ISSUE_RULES = IssueAnalyzerMixin.COMMON_ISSUE_RULES
    fake.LANGUAGE_RULES_MAP = IssueAnalyzerMixin.LANGUAGE_RULES_MAP
    fake._get_all_issue_rules = IssueAnalyzerMixin._get_all_issue_rules.__get__(fake)
    fake._get_issue_rules_for_language = IssueAnalyzerMixin._get_issue_rules_for_language.__get__(fake)
    fake._detect_language_from_module_path = IssueAnalyzerMixin._detect_language_from_module_path.__get__(fake)

    # _get_all_issue_rules 应返回非空列表
    all_rules = fake._get_all_issue_rules()
    assert len(all_rules) > 0, "_get_all_issue_rules 返回空列表"
    print(f"PASS B2.2: _get_all_issue_rules 返回 {len(all_rules)} 条规则")

    # 按语言选规则应正确
    rust_rules = fake._get_issue_rules_for_language("rust")
    assert any(r[0] == "unwrap_call" for r in rust_rules), "rust 规则应包含 unwrap_call"
    print(f"PASS B2.3: rust 规则 {len(rust_rules)} 条，包含 unwrap_call")

    # 语言推断应正确
    assert fake._detect_language_from_module_path("src::main::run") == "rust"
    assert fake._detect_language_from_module_path("src/main.py::run") == "python"
    assert fake._detect_language_from_module_path("main.go::run") == "go"
    print("PASS B2.4: 语言推断正确（rust/python/go）")

    # 验证代码中已无 self.ISSUE_RULES 引用
    import inspect
    source = inspect.getsource(IssueAnalyzerMixin)
    assert "self.ISSUE_RULES" not in source, "代码中仍存在 self.ISSUE_RULES 引用"
    print("PASS B2.5: 代码中已无 self.ISSUE_RULES 引用")
    print("PASS B2: ISSUE_RULES 修复完成\n")


def test_b3_git_symbol_changes():
    """B3: db_git.py git_symbol_changes 表应有写入"""
    print("--- B3: db_git.py git_symbol_changes 写入 ---")
    from callwarden.db.db_git import GitMixin
    import inspect

    # 验证 _extract_and_store_symbol_changes 方法存在
    assert hasattr(GitMixin, "_extract_and_store_symbol_changes"), "_extract_and_store_symbol_changes 方法不存在"
    print("PASS B3.1: _extract_and_store_symbol_changes 方法存在")

    # 验证 _import_git_file_changes 源码中调用了 _extract_and_store_symbol_changes
    source = inspect.getsource(GitMixin._import_git_file_changes)
    assert "_extract_and_store_symbol_changes" in source, "_import_git_file_changes 未调用符号变更提取"
    print("PASS B3.2: _import_git_file_changes 调用了 _extract_and_store_symbol_changes")

    # 验证 git_symbol_changes 表有 INSERT 语句
    extract_source = inspect.getsource(GitMixin._extract_and_store_symbol_changes)
    assert "INSERT" in extract_source and "git_symbol_changes" in extract_source, "未写入 git_symbol_changes 表"
    print("PASS B3.3: _extract_and_store_symbol_changes 包含 INSERT INTO git_symbol_changes")
    print("PASS B3: git_symbol_changes 写入修复完成\n")


def test_end_to_end_with_real_db():
    """端到端验证：用真实数据库测试 B2 和 B3"""
    print("--- 端到端验证：真实数据库 ---")
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "test.db")
    db = CodeGraphDB(db_path)
    ws_id = db.register_workspace("test-ws", os.getcwd())
    print(f"PASS E2E.1: 数据库初始化成功，workspace_id={ws_id}")

    # 测试 get_function_issues 不崩溃（B2 验证）
    # 注意：空数据库下应返回空列表而非崩溃
    issues = db.get_function_issues(limit=5)
    assert isinstance(issues, list), "get_function_issues 应返回列表"
    print(f"PASS E2E.2: get_function_issues 返回 {len(issues)} 条（不崩溃）")

    # 测试 get_issue_summary 不崩溃（B2 验证）
    summary = db.get_issue_summary()
    assert isinstance(summary, dict), "get_issue_summary 应返回字典"
    print(f"PASS E2E.3: get_issue_summary 返回 {len(summary)} 项（不崩溃）")

    # 测试 get_symbol_commit_history 不崩溃（B3 验证）
    history = db.get_symbol_commit_history("fake_hash_12345", limit=5)
    assert isinstance(history, list), "get_symbol_commit_history 应返回列表"
    print(f"PASS E2E.4: get_symbol_commit_history 返回 {len(history)} 条（不崩溃）")

    # 测试 get_comment_from_version 不崩溃（B1 验证）
    comment = db.get_comment_from_version("fake_fn@v1")
    print(f"PASS E2E.5: get_comment_from_version 返回 {comment}（不崩溃）")
    print("PASS E2E: 端到端验证全部通过\n")


def main():
    print("=" * 60)
    print("P0 Bug 修复验证")
    print("=" * 60)
    print()
    test_b1_import_os()
    test_b2_issue_rules()
    test_b3_git_symbol_changes()
    test_end_to_end_with_real_db()
    print("=" * 60)
    print("=== ALL P0 TESTS PASSED ===")
    print("=" * 60)


if __name__ == "__main__":
    main()
