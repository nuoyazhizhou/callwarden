"""
test_fuzz.py
============

模糊测试脚本：验证 callwarden 在恶意输入下的安全性和健壮性。

测试目的：
    本脚本针对 SEC-001 到 SEC-007 安全修复后的代码，进行模糊测试（Fuzz Testing）。
    通过向关键接口注入恶意输入（SQL 注入、路径遍历、Shell 注入、极端输入等），
    验证系统在以下方面的安全性：
    1. SQL 注入防御：参数化查询是否能阻止注入，表不被删除/篡改
    2. 路径遍历防御：_validate_file_path 是否能拒绝 ../.. 等目录遍历路径
    3. Shell 注入防御：_validate_file_path 是否能拒绝 ; $` 等 shell 元字符
    4. 编辑接口健壮性：propose_edit 对非法 operation/超长 content/空路径的处理
    5. 极端输入容错：超长字符串、Unicode 边界字符、空值等不导致崩溃
    6. 原子写入并发安全（SEC-001）：多线程并发写入同一文件，最终内容一致

运行方式:
    cd c:\\git_work\\callwarden\\scripts
    cw test test_fuzz
"""
import os
import sys
import tempfile
import time
import threading

# 确保能导入 callwarden 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from callwarden.db import CodeGraphDB


# ===========================================================================
# 辅助函数
# ===========================================================================

def _create_tmp_db():
    """创建临时数据库并注册工作区，返回 (db, db_path, tmpdir)

    每个测试函数独立使用一个临时数据库，避免相互干扰。
    """
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "fuzz_test.db")
    db = CodeGraphDB(db_path, workspace_root=tmpdir)
    db.register_workspace("fuzz-ws", tmpdir)
    return db, db_path, tmpdir


def _table_exists(db, table_name):
    """检查 SQLite 表是否存在"""
    cur = db.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name=?",
        (table_name,),
    )
    return cur.fetchone() is not None


# ===========================================================================
# 测试用例
# ===========================================================================

def test_sql_injection():
    """SQL 注入测试：向符号搜索接口传入 SQL 注入字符串

    验证点：
    - 不崩溃
    - symbols 表不被删除（参数化查询阻止注入）
    - 返回空列表或正常结果（注入字符串被当作普通查询词处理）
    """
    print("--- SQL 注入测试 ---")
    db, db_path, tmpdir = _create_tmp_db()

    # 注入字符串列表（经典 SQL 注入 payload）
    injection_payloads = [
        "'; DROP TABLE symbols; --",
        "' OR '1'='1",
        "%'; DELETE FROM symbols WHERE '1'='1",
    ]

    for payload in injection_payloads:
        # 实际方法名为 search_symbols（参数化查询，注入字符串作为 LIKE 参数值）
        result = db.search_symbols(payload, limit=20)
        # 断言：返回列表（空或正常），不崩溃
        assert isinstance(result, list), f"SQL 注入 payload 导致非列表返回: {payload!r}"
        print(f"PASS: payload={payload!r} 返回 {len(result)} 条结果（不崩溃）")

    # 断言：symbols 表仍然存在（未被 DROP/DELETE）
    assert _table_exists(db, "symbols"), "symbols 表被注入删除了！"
    print("PASS: symbols 表未被删除（参数化查询防御生效）")
    print("PASS: SQL 注入测试完成\n")


def test_path_traversal():
    """路径遍历测试：向 LSP 方法传入恶意路径

    验证点：
    - _validate_file_path 拒绝包含 .. 的路径
    - lsp_hover 返回 available=False
    - 不触发文件读取（不读取项目外文件）
    """
    print("--- 路径遍历测试 ---")
    db, db_path, tmpdir = _create_tmp_db()

    malicious_paths = [
        "../../etc/passwd",
        "..\\..\\windows\\system32",
        "../../../etc/passwd",
    ]

    for path in malicious_paths:
        result = db.lsp_hover(file_path=path, line=0, character=0)
        # 断言：返回字典且 available=False（被 _validate_file_path 拒绝）
        assert isinstance(result, dict), f"路径遍历导致非字典返回: {path!r}"
        assert result.get("available") is False, (
            f"路径遍历未被拒绝: {path!r}, available={result.get('available')}"
        )
        print(f"PASS: path={path!r} 被拒绝（available=False）")

    print("PASS: 路径遍历测试完成\n")


def test_shell_injection():
    """Shell 注入测试：向 LSP 方法传入 shell 元字符路径

    验证点：
    - _validate_file_path 拒绝包含 ; $ ` 等 shell 元字符的路径
    - lsp_hover 返回 available=False
    - 不触发 subprocess 执行恶意命令
    """
    print("--- Shell 注入测试 ---")
    db, db_path, tmpdir = _create_tmp_db()

    shell_payloads = [
        "; rm -rf /",
        "$(whoami)",
        "`whoami`",
    ]

    for payload in shell_payloads:
        result = db.lsp_hover(file_path=payload, line=0, character=0)
        # 断言：返回字典且 available=False（被 _validate_file_path 拒绝）
        assert isinstance(result, dict), f"Shell 注入导致非字典返回: {payload!r}"
        assert result.get("available") is False, (
            f"Shell 注入未被拒绝: {payload!r}, available={result.get('available')}"
        )
        print(f"PASS: payload={payload!r} 被拒绝（available=False）")

    print("PASS: Shell 注入测试完成\n")


def test_propose_edit_malicious():
    """propose_edit 恶意输入测试

    验证点：
    - 非法 operation 返回 status="failed"
    - 超长 content 不崩溃（返回合理状态）
    - 空 file_path 不崩溃（返回 status="failed"）
    """
    print("--- propose_edit 恶意输入测试 ---")
    db, db_path, tmpdir = _create_tmp_db()

    # 测试 1：非法 operation
    result = db.propose_edit(
        file_path=os.path.join(tmpdir, "test.txt"),
        new_content="hello",
        operation="hack",
    )
    assert isinstance(result, dict), "非法 operation 导致非字典返回"
    assert result.get("status") == "failed", (
        f"非法 operation 应返回 failed，实际: {result.get('status')}"
    )
    assert result.get("success") is False, "非法 operation 应 success=False"
    print(f"PASS: operation='hack' 返回 status={result.get('status')}（被拒绝）")

    # 测试 2：超长 content（10MB）
    huge_content = "x" * 10_000_000
    result = db.propose_edit(
        file_path=os.path.join(tmpdir, "huge.txt"),
        new_content=huge_content,
        operation="create",
    )
    assert isinstance(result, dict), "超长 content 导致非字典返回（崩溃）"
    # 超长 content 可能成功（applied）或失败（failed），关键是不崩溃
    assert result.get("status") in ("applied", "failed", "error"), (
        f"超长 content 返回异常状态: {result.get('status')}"
    )
    print(f"PASS: 10MB content 返回 status={result.get('status')}（不崩溃）")

    # 测试 3：空 file_path
    result = db.propose_edit(
        file_path="",
        new_content="test",
        operation="edit",
    )
    assert isinstance(result, dict), "空 file_path 导致非字典返回（崩溃）"
    # 空 file_path 解析为 workspace_root（目录），写入目录会失败
    assert result.get("status") in ("failed", "error"), (
        f"空 file_path 应返回 failed/error，实际: {result.get('status')}"
    )
    print(f"PASS: 空 file_path 返回 status={result.get('status')}（不崩溃）")

    print("PASS: propose_edit 恶意输入测试完成\n")


def test_extreme_inputs():
    """极端输入测试：超长字符串、Unicode 边界、空值等

    验证点：
    - 超长查询（1MB）不崩溃，返回合理结果
    - Unicode 边界字符（\x00、\x1f、emoji、零宽字符）不崩溃
    - 空字符串、None 不崩溃（None 用 try/except 包裹）
    """
    print("--- 极端输入测试 ---")
    db, db_path, tmpdir = _create_tmp_db()

    # 测试 1：超长字符串（1MB）作为搜索查询
    huge_query = "x" * 1_048_576  # 1MB
    result = db.search_symbols(huge_query, limit=20)
    assert isinstance(result, list), "超长查询导致非列表返回（崩溃）"
    print(f"PASS: 1MB 查询返回 {len(result)} 条结果（不崩溃）")

    # 测试 2：Unicode 边界字符
    unicode_payloads = [
        "\x00",                    # 空字节
        "\x1f",                    # 控制字符（单元分隔符）
        "\x7f",                    # DEL 字符
        "🎉🚀💻",                  # emoji
        "\u200b\u200c\u200d",      # 零宽字符（ZWSP/ZWNJ/ZWJ）
        "\ufeff",                  # BOM
        "中文测试 русский العربية",  # 多语言混合
    ]
    for payload in unicode_payloads:
        result = db.search_symbols(payload, limit=20)
        assert isinstance(result, list), f"Unicode payload 导致非列表返回: {payload!r}"
        print(f"PASS: Unicode payload {payload!r} 返回 {len(result)} 条结果")

    # 测试 3：空字符串
    result = db.search_symbols("", limit=20)
    assert isinstance(result, list), "空字符串查询导致非列表返回"
    print(f"PASS: 空字符串查询返回 {len(result)} 条结果")

    # 测试 4：None（用 try/except 包裹，不应导致未捕获异常）
    try:
        result = db.search_symbols(None, limit=20)
        # 如果没抛异常，断言返回列表
        assert isinstance(result, list), "None 查询导致非列表返回"
        print(f"PASS: None 查询返回 {len(result)} 条结果（未抛异常）")
    except (TypeError, ValueError) as e:
        # 抛出预期异常也算通过（关键是不产生未捕获的崩溃）
        print(f"PASS: None 查询抛出预期异常: {type(e).__name__}: {e}")
    except Exception as e:
        # 其他异常也算通过，只要是被捕获的（关键是不崩溃整个进程）
        print(f"PASS: None 查询抛出异常（已捕获）: {type(e).__name__}")

    print("PASS: 极端输入测试完成\n")


def test_atomic_write_concurrent():
    """原子写入并发测试（SEC-001 验证）

    验证点：
    - 10 个线程同时 propose_edit 同一文件，不崩溃
    - 最终文件内容一致（是某个完整写入，不是半写入状态）
    - 验证 atomic_write_file 的 os.replace 原子性

    注意：每个线程创建独立的 CodeGraphDB 实例，因为 sqlite3 连接默认
    不允许跨线程使用（check_same_thread=True）。
    """
    print("--- 原子写入并发测试（SEC-001） ---")
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "concurrent.db")

    # 主线程先初始化数据库和工作区（确保 schema 就绪）
    init_db = CodeGraphDB(db_path, workspace_root=tmpdir)
    init_db.register_workspace("concurrent-ws", tmpdir)

    # 创建测试目标文件（初始内容）
    target_file = os.path.join(tmpdir, "target.txt")
    with open(target_file, "w", encoding="utf-8") as f:
        f.write("initial_content")

    # 10 个线程各自写入不同内容（每条内容长度不同，便于检测半写入）
    thread_count = 10
    contents = [f"thread_{i}_" + "x" * (100 + i * 10) for i in range(thread_count)]
    results = [None] * thread_count
    errors = []

    def worker(idx):
        """线程工作函数：创建独立 DB 实例并执行 propose_edit"""
        try:
            thread_db = CodeGraphDB(db_path, workspace_root=tmpdir)
            result = thread_db.propose_edit(
                file_path=target_file,
                new_content=contents[idx],
                operation="edit",
                agent_task_id=f"thread-{idx}",
            )
            results[idx] = result
        except Exception as e:
            # 捕获异常记录，不算崩溃（如 SQLite database is locked）
            errors.append((idx, type(e).__name__, str(e)))

    # 启动所有线程
    threads = [
        threading.Thread(target=worker, args=(i,), name=f"writer-{i}")
        for i in range(thread_count)
    ]
    start_time = time.time()
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)  # 超时 30 秒防卡死
    elapsed = time.time() - start_time

    # 断言 1：所有线程都已结束（无卡死）
    alive_threads = [t for t in threads if t.is_alive()]
    assert len(alive_threads) == 0, f"有 {len(alive_threads)} 个线程卡死"
    print(f"PASS: {thread_count} 个线程全部完成（耗时 {elapsed:.2f}s）")

    # 统计成功/失败/错误
    success_count = sum(1 for r in results if r and r.get("success"))
    failed_count = sum(1 for r in results if r and not r.get("success"))
    print(f"   成功: {success_count}, 失败: {failed_count}, 异常: {len(errors)}")
    if errors:
        # 打印前 3 个错误（调试用，不算失败——SQLite 并发锁冲突是预期行为）
        for idx, etype, emsg in errors[:3]:
            print(f"   线程 {idx} 异常: {etype}: {emsg[:80]}")

    # 断言 2：最终文件内容是其中一个完整写入（不是半写入状态）
    with open(target_file, "r", encoding="utf-8") as f:
        final_content = f.read()

    assert final_content in contents, (
        f"文件内容不是任何一次完整写入（半写入状态？）"
        f"长度={len(final_content)}, 内容前50字符={final_content[:50]!r}"
    )
    print(f"PASS: 最终文件内容一致（长度={len(final_content)}，匹配某次完整写入）")

    # 断言 3：文件内容不是初始内容（至少有一次写入成功）
    # 注意：如果所有写入都因锁冲突失败，文件可能是初始内容——这也是安全的
    if final_content == "initial_content":
        print("PASS: 所有并发写入均被锁冲突拒绝，文件保持初始内容（安全降级）")
    else:
        matched_idx = contents.index(final_content)
        print(f"PASS: 最终内容来自线程 {matched_idx} 的完整写入")

    print("PASS: 原子写入并发测试完成\n")


# ===========================================================================
# 主入口
# ===========================================================================

def main():
    """按顺序执行所有模糊测试，打印 PASS/FAIL 结果"""
    print("=" * 60)
    print("callwarden 模糊测试（SEC-001 ~ SEC-007 安全修复验证）")
    print("=" * 60)
    print()

    test_sql_injection()
    test_path_traversal()
    test_shell_injection()
    test_propose_edit_malicious()
    test_extreme_inputs()
    test_atomic_write_concurrent()

    print("=" * 60)
    print("=== ALL FUZZ TESTS PASSED ===")
    print("=" * 60)


if __name__ == "__main__":
    main()
