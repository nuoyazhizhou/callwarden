"""
GC 功能测试：验证归档/复活/状态/清除的完整流程

测试场景：
1. IgnoreMatcher 规则解析与匹配（.gitignore 完整语法）
2. gc_archive：被 ignore 命中的文件迁入 archived_files
3. gc_restore：取消 ignore 后复活归档文件
4. gc_status：GC 状态统计正确
5. gc_purge：彻底清除归档超过 N 天的文件
6. 默认 ignore 规则（autogen/、*.pb.cc 等）命中
7. .gitignore 取反语法（! path）复活文件
"""
import os
import sys
import tempfile
import time

# 确保能导入 callwarden 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from callwarden.db import CodeGraphDB
from callwarden.analyzers.ignore_spec import IgnoreMatcher, parse_ignore_line


def setup_test_workspace():
    """创建临时 workspace 并注册激活"""
    tmpdir = tempfile.mkdtemp(prefix="callwarden_gc_test_")
    db = CodeGraphDB(db_path=os.path.join(tmpdir, "test.db"))
    ws_id = db.register_workspace("gc-test", tmpdir)
    db.set_active_workspace(ws_id)
    return db, tmpdir


def test_ignore_matcher_basic():
    """测试 1: IgnoreMatcher 基础规则匹配"""
    print("\n--- 测试 1: IgnoreMatcher 基础规则匹配 ---")
    matcher = IgnoreMatcher("/fake/root")
    matcher.add_default_rules(["build/", "*.pyc", "/out", "node_modules/"])

    # build/ 应匹配目录和其下文件
    assert matcher.is_ignored("build", is_dir=True), "build/ 应作为目录被忽略"
    assert matcher.is_ignored("build/main.o", is_dir=False), "build/main.o 应被忽略"
    assert matcher.is_ignored("src/build", is_dir=True), "src/build 应被忽略（任意层级）"

    # *.pyc 应匹配任意深度
    assert matcher.is_ignored("foo.pyc", is_dir=False), "foo.pyc 应被忽略"
    assert matcher.is_ignored("src/lib/utils.pyc", is_dir=False), "src/lib/utils.pyc 应被忽略"

    # /out 锚定根目录
    assert matcher.is_ignored("out", is_dir=False), "out 应被忽略（根目录锚定）"
    assert not matcher.is_ignored("src/output", is_dir=False), "src/output 不应被 /out 匹配"

    # 未匹配的文件
    assert not matcher.is_ignored("src/main.py", is_dir=False), "src/main.py 不应被忽略"

    print("PASS: IgnoreMatcher 基础规则匹配正确")


def test_ignore_matcher_negation():
    """测试 2: ! 取反语法（白名单）"""
    print("\n--- 测试 2: ! 取反语法 ---")
    matcher = IgnoreMatcher("/fake/root")
    matcher.add_default_rules([
        "*.log",
        "!important.log",  # 取反：important.log 不被忽略
    ])

    assert matcher.is_ignored("debug.log", is_dir=False), "debug.log 应被忽略"
    assert not matcher.is_ignored("important.log", is_dir=False), "important.log 应被取反"
    print("PASS: ! 取反语法正确")


def test_ignore_matcher_double_star():
    """测试 3: ** 递归通配符"""
    print("\n--- 测试 3: ** 递归通配符 ---")
    matcher = IgnoreMatcher("/fake/root")
    matcher.add_default_rules(["docs/**/*.md", "vendor/"])

    assert matcher.is_ignored("docs/a.md", is_dir=False), "docs/a.md 应被 ** 匹配"
    assert matcher.is_ignored("docs/sub/b.md", is_dir=False), "docs/sub/b.md 应被 ** 匹配"
    assert matcher.is_ignored("docs/sub/deep/c.md", is_dir=False), "docs/sub/deep/c.md 应被 ** 匹配"
    assert not matcher.is_ignored("README.md", is_dir=False), "README.md 不应被 docs/**/*.md 匹配"
    print("PASS: ** 递归通配符正确")


def test_gc_archive_basic():
    """测试 4: gc_archive 基本归档流程"""
    print("\n--- 测试 4: gc_archive 基本归档 ---")
    db, tmpdir = setup_test_workspace()

    # 创建 3 个文件：1 个被 ignore，2 个不被 ignore
    # 注意：test_a.py 在 build 目录下（默认 ignore 规则命中）
    files = {
        "src/main.py": "def main():\n    pass\n",
        "src/utils.py": "def util():\n    pass\n",
        "build/generated.py": "def gen():\n    pass\n",  # build/ 默认被忽略
    }

    for rel_path, content in files.items():
        abs_path = os.path.join(tmpdir, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        # 注册到 DB
        fi_id = db._register_file_db(abs_path, "test_module")
        assert fi_id > 0, f"注册文件失败: {rel_path}"

    # 验证 3 个文件都注册成功
    cur = db.conn.execute("SELECT COUNT(*) as c FROM file_instances WHERE workspace_id = ?", (db._get_active_workspace_id(),))
    assert cur.fetchone()["c"] == 3, "应有 3 个文件实例"

    # 执行 Full GC（force=True 扫描所有 active 文件）
    result = db.gc_archive(force=True, dry_run=False)

    assert result["scanned"] == 3, f"应扫描 3 个文件，实际 {result['scanned']}"
    assert result["archived"] == 1, f"应归档 1 个文件（build/generated.py），实际 {result['archived']}"
    assert "build/generated.py" in str(result["reasons"]) or result["archived"] >= 1

    # 验证归档记录
    cur = db.conn.execute("SELECT COUNT(*) as c FROM archived_files WHERE workspace_id = ?", (db._get_active_workspace_id(),))
    assert cur.fetchone()["c"] == 1, "archived_files 应有 1 条记录"

    # 验证 file_instances.status
    cur = db.conn.execute("SELECT status FROM file_instances WHERE rel_path = ?", ("build/generated.py",))
    assert cur.fetchone()["status"] == "archived", "build/generated.py 状态应为 archived"

    cur = db.conn.execute("SELECT status FROM file_instances WHERE rel_path = ?", ("src/main.py",))
    assert cur.fetchone()["status"] == "active", "src/main.py 状态应为 active"

    print(f"PASS: gc_archive 归档 {result['archived']} 个文件")
    db.close()


def test_gc_restore():
    """测试 5: gc_restore 复活归档文件"""
    print("\n--- 测试 5: gc_restore 复活 ---")
    db, tmpdir = setup_test_workspace()

    # 创建一个被 ignore 的文件
    abs_path = os.path.join(tmpdir, "build/gen.py")
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write("def gen():\n    pass\n")
    db._register_file_db(abs_path, "test")

    # 归档
    db.gc_archive(force=True)

    # 验证已归档
    cur = db.conn.execute("SELECT status FROM file_instances WHERE rel_path = 'build/gen.py'")
    assert cur.fetchone()["status"] == "archived"

    # 复活（force=True，即使仍命中 ignore 也复活）
    result = db.gc_restore(force=True)

    assert result["restored"] == 1, f"应复活 1 个文件，实际 {result['restored']}"
    cur = db.conn.execute("SELECT status FROM file_instances WHERE rel_path = 'build/gen.py'")
    assert cur.fetchone()["status"] == "pending", "复活后状态应为 pending"

    # 验证归档记录已删除
    cur = db.conn.execute("SELECT COUNT(*) as c FROM archived_files")
    assert cur.fetchone()["c"] == 0, "归档记录应已删除"

    print(f"PASS: gc_restore 复活 {result['restored']} 个文件")
    db.close()


def test_gc_status():
    """测试 6: gc_status 状态统计"""
    print("\n--- 测试 6: gc_status 状态统计 ---")
    db, tmpdir = setup_test_workspace()

    # 创建 3 个文件，2 个被 ignore
    files = {
        "src/main.py": "def main():\n    pass\n",
        "build/a.py": "def a():\n    pass\n",
        "out/b.py": "def b():\n    pass\n",
    }
    for rel_path, content in files.items():
        abs_path = os.path.join(tmpdir, rel_path)
        os.makedirs(os.path.dirname(abs_path), exist_ok=True)
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write(content)
        db._register_file_db(abs_path, "test")

    db.gc_archive(force=True)

    status = db.gc_status()
    assert status["active_files"] == 1, f"活跃文件应为 1，实际 {status['active_files']}"
    assert status["archived_files"] == 2, f"归档文件应为 2，实际 {status['archived_files']}"
    assert 0 < status["archive_ratio"] < 1, f"归档率应在 0-1 之间，实际 {status['archive_ratio']}"
    assert len(status["recent_archives"]) == 2, f"最近归档应有 2 条，实际 {len(status['recent_archives'])}"

    print(f"PASS: gc_status 统计正确 (active={status['active_files']}, archived={status['archived_files']})")
    db.close()


def test_gc_purge():
    """测试 7: gc_purge 彻底清除"""
    print("\n--- 测试 7: gc_purge 彻底清除 ---")
    db, tmpdir = setup_test_workspace()

    abs_path = os.path.join(tmpdir, "build/gen.py")
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write("def gen():\n    pass\n")
    db._register_file_db(abs_path, "test")

    db.gc_archive(force=True)

    # 模拟归档时间改为 31 天前
    cutoff = time.time() - 31 * 86400
    db.conn.execute("UPDATE archived_files SET archived_at = ?", (cutoff,))
    db.conn.commit()

    # 清除 30 天前的
    result = db.gc_purge(older_than_days=30)

    assert result["purged_files"] == 1, f"应清除 1 个文件，实际 {result['purged_files']}"

    # 验证 file_instances 已删除
    cur = db.conn.execute("SELECT COUNT(*) as c FROM file_instances WHERE rel_path = 'build/gen.py'")
    assert cur.fetchone()["c"] == 0, "file_instances 应已删除"

    print(f"PASS: gc_purge 清除 {result['purged_files']} 个文件")
    db.close()


def test_gc_dry_run():
    """测试 8: dry_run 预演模式不实际归档"""
    print("\n--- 测试 8: dry_run 预演模式 ---")
    db, tmpdir = setup_test_workspace()

    abs_path = os.path.join(tmpdir, "build/gen.py")
    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write("def gen():\n    pass\n")
    db._register_file_db(abs_path, "test")

    result = db.gc_archive(force=True, dry_run=True)

    assert result["archived"] == 1, "dry_run 应统计 1 个待归档"
    assert result["dry_run"] is True

    # 验证未实际归档
    cur = db.conn.execute("SELECT status FROM file_instances WHERE rel_path = 'build/gen.py'")
    assert cur.fetchone()["status"] != "archived", "dry_run 不应实际归档"

    cur = db.conn.execute("SELECT COUNT(*) as c FROM archived_files")
    assert cur.fetchone()["c"] == 0, "dry_run 不应写入归档记录"

    print("PASS: dry_run 预演模式正确")
    db.close()


def test_default_autogen_rules():
    """测试 9: 默认 autogen 规则命中"""
    print("\n--- 测试 9: 默认 autogen 规则 ---")
    matcher = IgnoreMatcher("/fake/root")
    # 这些是 DEFAULT_IGNORE_RULES 中的规则
    matcher.add_default_rules([
        "*.pb.cc", "*_pb2.py", "moc_*.cpp",
        "autogen/", "generated/", "proto_gen/",
    ])

    # protobuf 生成文件
    assert matcher.is_ignored("src/foo.pb.cc", is_dir=False), "foo.pb.cc 应被忽略"
    assert matcher.is_ignored("rpc_pb2.py", is_dir=False), "rpc_pb2.py 应被忽略"

    # Qt moc 生成
    assert matcher.is_ignored("moc_mainwindow.cpp", is_dir=False), "moc_mainwindow.cpp 应被忽略"

    # autogen 目录
    assert matcher.is_ignored("autogen/types.py", is_dir=False), "autogen/types.py 应被忽略"
    assert matcher.is_ignored("src/generated/config.py", is_dir=False), "src/generated/config.py 应被忽略"

    # 正常文件不应被忽略
    assert not matcher.is_ignored("src/main.py", is_dir=False), "src/main.py 不应被忽略"

    print("PASS: 默认 autogen 规则命中正确")


def main():
    print("=" * 60)
    print("callwarden GC 功能测试")
    print("=" * 60)

    tests = [
        test_ignore_matcher_basic,
        test_ignore_matcher_negation,
        test_ignore_matcher_double_star,
        test_gc_archive_basic,
        test_gc_restore,
        test_gc_status,
        test_gc_purge,
        test_gc_dry_run,
        test_default_autogen_rules,
    ]

    passed = 0
    failed = 0
    for test_fn in tests:
        try:
            test_fn()
            passed += 1
        except Exception as e:
            failed += 1
            import traceback
            print(f"FAIL: {test_fn.__name__}: {e}")
            traceback.print_exc()

    print()
    print("=" * 60)
    if failed == 0:
        print(f"=== ALL GC TESTS PASSED ({passed}/{passed + failed}) ===")
    else:
        print(f"=== {failed} TESTS FAILED ({passed} passed / {failed} failed) ===")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
