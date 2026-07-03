"""
压力测试：验证 CodeGraphDB 在 10w+ 符号规模下的性能

目的：
  - 验证 10 万符号 + 20 万调用关系规模下，数据库的查询和写入性能
  - 确保 search_symbols / get_call_chain_up / get_call_chain_down 在 10w 符号下响应时间 < 5 秒
  - 确保批量插入 1 万符号 < 30 秒
  - 确保数据库文件 < 500MB
  - 所有数据通过 SQL 批量插入生成，不经过解析器

运行方式:
    cd c:\\git_work\\callwarden\\scripts
    cw test test_stress
"""
import os
import sys
import tempfile
import time
import random

# 确保能导入 code_graph 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from code_graph.db import CodeGraphDB

# ============================================
# 测试规模常量
# ============================================
NUM_FILES = 1000                # 文件实例数
SYMBOLS_PER_FILE = 100          # 每文件符号数 → 总计 10 万符号
NUM_CALLS = 200000              # 调用关系数 → 20 万条
BATCH_INSERT_SYMBOLS = 10000    # 批量插入测试的符号数 → 1 万

# ============================================
# 性能断言阈值（宽松，确保 CI 能过）
# ============================================
MAX_QUERY_TIME = 5.0            # 查询响应时间上限（秒）
MAX_BATCH_INSERT_TIME = 30.0    # 批量插入 1w 符号时间上限（秒）
MAX_DB_SIZE_MB = 500            # 数据库文件大小上限（MB）


def setup_database():
    """创建临时数据库，注册并激活工作区，补充必要的性能索引

    Returns:
        (db, db_path, tmpdir, ws_id) 元组
    """
    tmpdir = tempfile.mkdtemp(prefix="code_graph_stress_")
    db_path = os.path.join(tmpdir, "stress_test.db")
    db = CodeGraphDB(db_path)

    # 注册并激活工作区
    ws_id = db.register_workspace("stress-test", tmpdir)
    db.set_active_workspace(ws_id)

    # 补充 call_versions.callee_qualified 索引（原 schema 缺失，提升 get_call_chain_up 性能）
    db.conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_call_versions_callee ON call_versions(callee_qualified)"
    )
    db.conn.commit()

    print(f"临时数据库: {db_path}")
    print(f"工作区 ID: {ws_id}")
    return db, db_path, tmpdir, ws_id


def test_generate_symbols(db, ws_id):
    """测试 a: 生成 10 万个符号（symbol_contents + symbols + file_symbol_versions）

    说明：
      - 用户要求插入 symbol_contents + symbols 表
      - 同时插入 file_symbol_versions / file_instances / file_versions / file_contents，
        因为 search_symbols 方法实际通过这些表联查
    """
    print("--- 测试 a: 生成 10 万符号 ---")
    start = time.time()
    now = time.time()

    # 1. 批量插入 file_contents（每个文件一个内容哈希）
    fc_data = [(f"fc_{i}", "python", 100, now) for i in range(NUM_FILES)]
    db.conn.executemany(
        "INSERT INTO file_contents (content_hash, language, total_lines, first_seen_at) VALUES (?, ?, ?, ?)",
        fc_data,
    )

    # 2. 批量插入 file_instances（workspace_id 关联工作区）
    fi_data = [
        (
            ws_id,
            f"src/mod_{i}.py",
            f"/tmp/src/mod_{i}.py",
            f"fc_{i}",
            now,
            100,
            now,
            "parsed",
            f"mod_{i}",
        )
        for i in range(NUM_FILES)
    ]
    db.conn.executemany(
        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        fi_data,
    )

    # 3. 批量插入 file_versions（每个文件实例一个当前版本，ID 从 1 开始递增）
    fv_data = [
        (i + 1, 1, f"fc_{i}", now, 100, now, 1, 0)
        for i in range(NUM_FILES)
    ]
    db.conn.executemany(
        "INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime, total_lines, parsed_at, is_current, is_deleted) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        fv_data,
    )

    # 4. 批量生成并插入 symbol_contents + file_symbol_versions + symbols
    sc_data = []
    fsv_data = []
    sym_data = []
    for i in range(NUM_FILES):
        file_version_id = i + 1
        file_instance_id = i + 1
        for j in range(SYMBOLS_PER_FILE):
            content_hash = f"sh_{i}_{j}"
            name = f"func_{j}"
            qualified_name = f"mod_{i}::func_{j}"
            # symbol_contents：符号内容去重表
            sc_data.append((content_hash, name, "fn", f"def {name}(): pass", f"def {name}()", qualified_name))
            # file_symbol_versions：文件版本与符号的关联（search_symbols 查询用）
            fsv_data.append((file_version_id, content_hash, qualified_name, j + 1, j + 1, f"mod_{i}", 0, 0))
            # symbols：当前快照表
            sym_data.append((file_instance_id, content_hash, name, "fn", j + 1, j + 1, f"mod_{i}", qualified_name))

    db.conn.executemany(
        "INSERT INTO symbol_contents (content_hash, name, kind, content, signature, qualified_name) VALUES (?, ?, ?, ?, ?, ?)",
        sc_data,
    )
    db.conn.executemany(
        "INSERT INTO file_symbol_versions (file_version_id, symbol_hash, qualified_name, start_line, end_line, module_path, depth, is_deleted) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        fsv_data,
    )
    db.conn.executemany(
        "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, start_line, end_line, module_path, qualified_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        sym_data,
    )
    db.conn.commit()

    elapsed = time.time() - start

    # 验证符号数
    count = db.conn.execute("SELECT COUNT(*) as c FROM symbols").fetchone()["c"]
    sc_count = db.conn.execute("SELECT COUNT(*) as c FROM symbol_contents").fetchone()["c"]
    fsv_count = db.conn.execute("SELECT COUNT(*) as c FROM file_symbol_versions").fetchone()["c"]
    print(f"生成符号: symbols={count}, symbol_contents={sc_count}, file_symbol_versions={fsv_count}")
    print(f"耗时: {elapsed:.2f}s")
    assert count == NUM_FILES * SYMBOLS_PER_FILE, f"symbols 数不匹配: {count}"
    assert sc_count == NUM_FILES * SYMBOLS_PER_FILE, f"symbol_contents 数不匹配: {sc_count}"
    assert fsv_count == NUM_FILES * SYMBOLS_PER_FILE, f"file_symbol_versions 数不匹配: {fsv_count}"
    print(f"PASS 测试 a: 10 万符号生成完成（{elapsed:.2f}s）\n")


def test_generate_calls(db, ws_id):
    """测试 b: 生成 20 万条调用关系（calls + call_versions）

    说明：
      - 用户要求插入 calls 表
      - 同时插入 call_versions，因为 get_call_chain_up/down 方法实际查询该表
      - 调用关系包含模块内链式调用 + 随机跨模块调用
    """
    print("--- 测试 b: 生成 20 万调用关系 ---")
    start = time.time()

    calls_data = []
    cv_data = []
    random.seed(42)  # 固定随机种子，确保可复现

    # 1. 模块内链式调用: func_j → func_{j+1}（每个模块 99 条，共 99,000 条）
    for i in range(NUM_FILES):
        file_version_id = i + 1
        for j in range(SYMBOLS_PER_FILE - 1):
            caller_qn = f"mod_{i}::func_{j}"
            callee_qn = f"mod_{i}::func_{j + 1}"
            caller_id = i * SYMBOLS_PER_FILE + j + 1  # 预测的 symbol ID（AUTOINCREMENT 从 1 开始）
            # calls 表（当前快照）
            calls_data.append((caller_id, caller_qn, f"mod_{i}", f"func_{j + 1}", f"mod_{i}", callee_qn, f"src/mod_{i}.py", 0, j + 1, 0))
            # call_versions 表（版本化调用关系，查询方法用）
            cv_data.append((file_version_id, caller_qn, f"sh_{i}_{j}", f"func_{j + 1}", f"mod_{i}", callee_qn, f"src/mod_{i}.py", j + 1, 0))

    # 2. 随机跨模块调用，补足到 20 万条
    remaining = NUM_CALLS - len(calls_data)
    for _ in range(remaining):
        ci = random.randint(0, NUM_FILES - 1)
        cj = random.randint(0, SYMBOLS_PER_FILE - 1)
        mi = random.randint(0, NUM_FILES - 1)
        mj = random.randint(0, SYMBOLS_PER_FILE - 1)
        caller_qn = f"mod_{ci}::func_{cj}"
        callee_qn = f"mod_{mi}::func_{mj}"
        caller_id = ci * SYMBOLS_PER_FILE + cj + 1
        is_cross = 1 if ci != mi else 0
        calls_data.append((caller_id, caller_qn, f"mod_{ci}", f"func_{mj}", f"mod_{mi}", callee_qn, f"src/mod_{mi}.py", 0, cj + 1, is_cross))
        cv_data.append((ci + 1, caller_qn, f"sh_{ci}_{cj}", f"func_{mj}", f"mod_{mi}", callee_qn, f"src/mod_{mi}.py", cj + 1, is_cross))

    db.conn.executemany(
        "INSERT INTO calls (caller_id, caller_name, caller_module, callee_name, callee_module, callee_qualified, callee_file, callee_id, call_line, is_cross_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        calls_data,
    )
    db.conn.executemany(
        "INSERT INTO call_versions (file_version_id, caller_qualified, caller_hash, callee_name, callee_module, callee_qualified, callee_file, call_line, is_cross_file) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        cv_data,
    )
    db.conn.commit()

    elapsed = time.time() - start

    # 验证调用关系数
    count = db.conn.execute("SELECT COUNT(*) as c FROM calls").fetchone()["c"]
    cv_count = db.conn.execute("SELECT COUNT(*) as c FROM call_versions").fetchone()["c"]
    print(f"生成调用关系: calls={count}, call_versions={cv_count}")
    print(f"耗时: {elapsed:.2f}s")
    assert count == NUM_CALLS, f"calls 数不匹配: {count} != {NUM_CALLS}"
    assert cv_count == NUM_CALLS, f"call_versions 数不匹配: {cv_count} != {NUM_CALLS}"
    print(f"PASS 测试 b: 20 万调用关系生成完成（{elapsed:.2f}s）\n")


def test_query_performance(db):
    """测试 c: 查询性能（search_symbols / get_call_chain_up / get_call_chain_down）

    说明：
      - symbol_search 对应实际方法 search_symbols
      - 在 10w 符号 + 20w 调用关系下测试响应时间
    """
    print("--- 测试 c: 查询性能 ---")

    # 1. 测试 search_symbols（模糊搜索符号）
    start = time.time()
    results = db.search_symbols("func_50", limit=20)
    t_search = time.time() - start
    print(f"search_symbols('func_50'): {len(results)} 条结果，耗时 {t_search:.3f}s")
    assert len(results) > 0, "search_symbols 应返回结果"
    assert t_search < MAX_QUERY_TIME, f"search_symbols 超时: {t_search:.3f}s >= {MAX_QUERY_TIME}s"

    # 2. 测试 get_call_chain_down（向下追踪调用链）
    start = time.time()
    down = db.get_call_chain_down("mod_0::func_0", max_depth=5)
    t_down = time.time() - start
    print(f"get_call_chain_down('mod_0::func_0'): depth={down['max_depth_reached']}, downstream={down['total_downstream']}，耗时 {t_down:.3f}s")
    assert t_down < MAX_QUERY_TIME, f"get_call_chain_down 超时: {t_down:.3f}s >= {MAX_QUERY_TIME}s"

    # 3. 测试 get_call_chain_up（向上追踪调用链）
    start = time.time()
    up = db.get_call_chain_up("mod_0::func_50", max_depth=5)
    t_up = time.time() - start
    print(f"get_call_chain_up('mod_0::func_50'): depth={up['max_depth_reached']}, upstream={up['total_upstream']}，耗时 {t_up:.3f}s")
    assert t_up < MAX_QUERY_TIME, f"get_call_chain_up 超时: {t_up:.3f}s >= {MAX_QUERY_TIME}s"

    print(f"PASS 测试 c: 查询性能达标（search={t_search:.3f}s, down={t_down:.3f}s, up={t_up:.3f}s）\n")


def test_batch_insert_performance(db, ws_id):
    """测试 d: 批量插入 1 万符号的性能

    说明：
      - 在已有 10w 符号的基础上，再批量插入 1w 符号
      - 使用 executemany 单事务提交，测量耗时
    """
    print("--- 测试 d: 批量插入 1 万符号 ---")
    now = time.time()
    file_idx = NUM_FILES  # 新文件索引从 1000 开始，避免与已有数据冲突

    # 创建新文件实例和版本（为批量插入准备载体）
    db.conn.execute(
        "INSERT INTO file_contents (content_hash, language, total_lines, first_seen_at) VALUES (?, ?, ?, ?)",
        (f"fc_{file_idx}", "python", BATCH_INSERT_SYMBOLS, now),
    )
    db.conn.execute(
        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (ws_id, "src/batch_test.py", "/tmp/src/batch_test.py", f"fc_{file_idx}", now, BATCH_INSERT_SYMBOLS, now, "parsed", "batch_mod"),
    )
    # 查询新创建的 file_instance_id
    fi_row = db.conn.execute(
        "SELECT id FROM file_instances WHERE rel_path = 'src/batch_test.py' AND workspace_id = ?",
        (ws_id,),
    ).fetchone()
    fi_id = fi_row["id"]

    db.conn.execute(
        "INSERT INTO file_versions (file_instance_id, version_num, content_hash, mtime, total_lines, parsed_at, is_current, is_deleted) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (fi_id, 1, f"fc_{file_idx}", now, BATCH_INSERT_SYMBOLS, now, 1, 0),
    )
    # 查询新创建的 file_version_id
    fv_row = db.conn.execute(
        "SELECT id FROM file_versions WHERE file_instance_id = ?",
        (fi_id,),
    ).fetchone()
    fv_id = fv_row["id"]
    db.conn.commit()

    # 计时开始：批量插入 1 万符号
    start = time.time()

    sc_data = []
    fsv_data = []
    sym_data = []
    for j in range(BATCH_INSERT_SYMBOLS):
        content_hash = f"sh_batch_{j}"
        name = f"batch_func_{j}"
        qualified_name = f"batch_mod::batch_func_{j}"
        sc_data.append((content_hash, name, "fn", f"def {name}(): pass", f"def {name}()", qualified_name))
        fsv_data.append((fv_id, content_hash, qualified_name, j + 1, j + 1, "batch_mod", 0, 0))
        sym_data.append((fi_id, content_hash, name, "fn", j + 1, j + 1, "batch_mod", qualified_name))

    db.conn.executemany(
        "INSERT INTO symbol_contents (content_hash, name, kind, content, signature, qualified_name) VALUES (?, ?, ?, ?, ?, ?)",
        sc_data,
    )
    db.conn.executemany(
        "INSERT INTO file_symbol_versions (file_version_id, symbol_hash, qualified_name, start_line, end_line, module_path, depth, is_deleted) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        fsv_data,
    )
    db.conn.executemany(
        "INSERT INTO symbols (file_instance_id, symbol_hash, name, kind, start_line, end_line, module_path, qualified_name) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        sym_data,
    )
    db.conn.commit()

    elapsed = time.time() - start

    # 验证新增符号数
    new_count = db.conn.execute(
        "SELECT COUNT(*) as c FROM symbols WHERE module_path = 'batch_mod'"
    ).fetchone()["c"]
    print(f"批量插入符号数: {new_count}，耗时: {elapsed:.2f}s")
    assert new_count == BATCH_INSERT_SYMBOLS, f"新增符号数不匹配: {new_count} != {BATCH_INSERT_SYMBOLS}"
    assert elapsed < MAX_BATCH_INSERT_TIME, f"批量插入超时: {elapsed:.2f}s >= {MAX_BATCH_INSERT_TIME}s"
    print(f"PASS 测试 d: 批量插入性能达标（{elapsed:.2f}s < {MAX_BATCH_INSERT_TIME}s）\n")


def test_database_size(db_path):
    """测试 e: 数据库文件大小"""
    print("--- 测试 e: 数据库文件大小 ---")
    size_bytes = os.path.getsize(db_path)
    size_mb = size_bytes / (1024 * 1024)
    print(f"数据库文件大小: {size_mb:.2f} MB（{size_bytes} bytes）")
    assert size_mb < MAX_DB_SIZE_MB, f"数据库过大: {size_mb:.2f} MB >= {MAX_DB_SIZE_MB} MB"
    print(f"PASS 测试 e: 数据库大小达标（{size_mb:.2f} MB < {MAX_DB_SIZE_MB} MB）\n")


def main():
    """主函数：按顺序执行所有压力测试，打印 PASS/FAIL 和耗时"""
    print("=" * 60)
    print("CodeGraphDB 压力测试：10w+ 符号规模性能验证")
    print("=" * 60)
    print()

    total_start = time.time()

    # 创建临时数据库
    db, db_path, tmpdir, ws_id = setup_database()
    print()

    try:
        # a) 生成 10 万符号
        test_generate_symbols(db, ws_id)

        # b) 生成 20 万调用关系
        test_generate_calls(db, ws_id)

        # c) 测试查询性能
        test_query_performance(db)

        # d) 测试批量插入性能
        test_batch_insert_performance(db, ws_id)

        # e) 测试数据库大小
        test_database_size(db_path)

    finally:
        db.conn.close()

    total_elapsed = time.time() - total_start
    print("=" * 60)
    print(f"全部测试通过！总耗时: {total_elapsed:.2f}s")
    print(f"临时数据库路径: {db_path}")
    print("=" * 60)


if __name__ == "__main__":
    main()
