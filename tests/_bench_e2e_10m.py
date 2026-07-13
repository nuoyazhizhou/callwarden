"""
千万级端到端压测：正式代码 P12 优化验证

使用正式 schema.py 的 SCHEMA_TABLES_SQL + SCHEMA_INDEXES_SQL，
模拟正式代码的入库流程：建表（无索引）→ 入库（分段 commit）→ 建索引 → WAL checkpoint

与 _bench_insert_strategies.py 的区别：
- 使用正式 schema（51 表 + 132 索引 + 3 触发器），而非精简版（4 表 + 6 索引）
- 使用正式 PRAGMA 设置（cache_size=-64000 等），而非优化版（-256000）
- 验证 SCHEMA_INDEXES_SQL 在千万级数据下的建索引耗时

用法：
  python tests/_bench_e2e_10m.py                       # 默认 10M 符号
  python tests/_bench_e2e_10m.py --symbols 2000000     # 2M 符号
  python tests/_bench_e2e_10m.py --symbols 5000000     # 5M 符号
"""
import os
import sys
import time
import sqlite3
import random
import shutil
import json
import argparse
from pathlib import Path

# 直接加载 schema.py 模块（避免 db/__init__.py 的相对导入链）
import importlib.util
_schema_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "schema.py")
_spec = importlib.util.spec_from_file_location("callwarden_schema", _schema_path)
_schema_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_schema_mod)
SCHEMA_TABLES_SQL = _schema_mod.SCHEMA_TABLES_SQL
SCHEMA_INDEXES_SQL = _schema_mod.SCHEMA_INDEXES_SQL


# ============================================
# 数据生成（内存）
# ============================================

def generate_data(target_symbols: int, seed: int = 42):
    """生成合成数据到内存 list，返回 (files, symbols, calls)"""
    rng = random.Random(seed)
    n_files = max(1, target_symbols // 5)
    n_fns = target_symbols * 7 // 10
    n_classes = target_symbols * 2 // 10
    n_structs = target_symbols - n_fns - n_classes

    # 1. workspaces（1 条）
    # 2. file_instances（n_files 条）
    files = []
    for i in range(1, n_files + 1):
        dir_idx = i // 20
        rel_path = f"src/module_{dir_idx}/file_{i}.py"
        files.append((
            i,                      # id
            1,                      # workspace_id
            rel_path,               # rel_path
            f"/{rel_path}",         # abs_path
            f"hash_{i:08x}",       # current_content_hash
            float(i),               # mtime
            100,                    # total_lines
            0.0,                    # last_parsed
            "active",               # status
            f"module_{dir_idx}",    # module_path
        ))

    # 3. symbols（正式 schema 列：id, file_instance_id, symbol_hash, name, kind,
    #    visibility, start_line, end_line, start_col, end_col, signature,
    #    has_comment, comment_status, module_path, qualified_name, depth）
    symbols = []
    fn_ids = []
    sym_names = []
    sym_id = 0
    for i in range(n_fns):
        sym_id += 1
        fid = (i % n_files) + 1
        name = f"func_{i}"
        qname = f"module_{i // 100}.func_{i}"
        symbols.append((
            sym_id, fid, f"sh_{sym_id:08x}", name, "fn", "public",
            (sym_id % 10000) + 1, (sym_id % 10000) + 10, 0, 0,
            "def func(): pass", 1, "ok", f"module_{i // 100}", qname, -1,
        ))
        fn_ids.append(sym_id)
        sym_names.append((sym_id, name))

    for i in range(n_classes):
        sym_id += 1
        fid = ((n_fns + i) % n_files) + 1
        name = f"Class_{i}"
        qname = f"module_{i // 100}.Class_{i}"
        symbols.append((
            sym_id, fid, f"sh_{sym_id:08x}", name, "class", "public",
            (sym_id % 10000) + 1, (sym_id % 10000) + 50, 0, 0,
            "", 1, "ok", f"module_{i // 100}", qname, 0,
        ))
        sym_names.append((sym_id, name))

    for i in range(n_structs):
        sym_id += 1
        fid = ((n_fns + n_classes + i) % n_files) + 1
        name = f"Struct_{i}"
        qname = f"module_{i // 100}.Struct_{i}"
        symbols.append((
            sym_id, fid, f"sh_{sym_id:08x}", name, "struct", "public",
            (sym_id % 10000) + 1, (sym_id % 10000) + 20, 0, 0,
            "", 0, "pending", f"module_{i // 100}", qname, -1,
        ))

    # 4. calls（正式 schema 列：id, caller_id, caller_name, caller_module,
    #    callee_name, callee_module, callee_qualified, callee_file,
    #    callee_id, call_line, is_cross_file）
    calls = []
    call_id = 0
    for caller_id in fn_ids:
        n_callees = rng.randint(5, 15)
        for _ in range(n_callees):
            call_id += 1
            if rng.random() < 0.3:
                callee_name = f"ext_func_{rng.randint(0, 99999)}"
                callee_id = 0
                callee_qname = ""
            else:
                callee_idx = rng.randint(0, len(sym_names) - 1)
                callee_id, callee_name = sym_names[callee_idx]
                callee_qname = f"module_{callee_idx // 100}.{callee_name}"
            caller_name = f"func_{caller_id - 1}"
            caller_module = f"module_{(caller_id - 1) // 100}"
            is_cross = 1 if rng.random() < 0.4 else 0
            call_line = rng.randint(1, 100)
            calls.append((
                call_id, caller_id, caller_name, caller_module,
                callee_name, caller_module, callee_qname, "",
                callee_id, call_line, is_cross,
            ))

    return files, symbols, calls


# ============================================
# 端到端测试：正式 schema + 正式 PRAGMA + A_ckpt 策略
# ============================================

def run_e2e(db_path: str, target_symbols: int, commit_every: int = 10):
    """端到端测试：使用正式 schema.py 的 SQL + 正式 PRAGMA 设置"""
    if os.path.exists(db_path):
        os.remove(db_path)
    # 清理 WAL/SHM 残留
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)

    # 正式 PRAGMA 设置（与 db_base.py CodeGraphDB.__init__ 一致）
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-64000")       # 正式：64MB
    conn.execute("PRAGMA temp_store=MEMORY")
    conn.execute("PRAGMA mmap_size=268435456")     # 256MB
    conn.execute("PRAGMA locking_mode=NORMAL")
    conn.execute("PRAGMA foreign_keys=OFF")

    # ---- 阶段 0：生成数据 ----
    t0 = time.perf_counter()
    files, symbols, calls = generate_data(target_symbols)
    t_gen = time.perf_counter() - t0
    print(f"  数据生成：{len(files)} 文件, {len(symbols)} 符号, {len(calls)} 调用边 ({t_gen:.1f}s)")

    # ---- 阶段 1：建表（SCHEMA_TABLES_SQL，无索引/触发器）----
    t0 = time.perf_counter()
    conn.executescript(SCHEMA_TABLES_SQL)
    # 插入 workspace
    conn.execute(
        "INSERT INTO workspaces (id, name, root_path, created_at, is_active, description) "
        "VALUES (1, 'synthetic', '/synthetic', ?, 1, '压测工作区')",
        (time.time(),)
    )
    conn.commit()
    t_schema = time.perf_counter() - t0

    # 统计建表后的索引数（应该只有 PRIMARY KEY / UNIQUE 自动索引）
    cur = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
    idx_before = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'")
    trigger_before = cur.fetchone()[0]

    # ---- 阶段 2：入库（分段 commit，A_ckpt 策略）----
    n_commits = 0
    t0 = time.perf_counter()

    # 2a. 文件
    conn.executemany(
        "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path, current_content_hash, "
        "mtime, total_lines, last_parsed, status, module_path) VALUES (?,?,?,?,?,?,?,?,?,?)",
        files
    )
    conn.commit()
    n_commits += 1

    # 2b. 符号（每 commit_every 批 commit 一次）
    BATCH = 50000
    sym_batches = list(range(0, len(symbols), BATCH))
    sym_insert_sql = (
        "INSERT INTO symbols (id, file_instance_id, symbol_hash, name, kind, visibility, "
        "start_line, end_line, start_col, end_col, signature, has_comment, comment_status, "
        "module_path, qualified_name, depth) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    for i, start in enumerate(sym_batches):
        conn.executemany(sym_insert_sql, symbols[start:start + BATCH])
        if (i + 1) % commit_every == 0:
            conn.commit()
            n_commits += 1
    conn.commit()
    n_commits += 1

    # 2c. 调用边（每 commit_every 批 commit 一次）
    BATCH_CALL = 100000
    call_batches = list(range(0, len(calls), BATCH_CALL))
    call_insert_sql = (
        "INSERT INTO calls (id, caller_id, caller_name, caller_module, callee_name, "
        "callee_module, callee_qualified, callee_file, callee_id, call_line, is_cross_file) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)"
    )
    for i, start in enumerate(call_batches):
        conn.executemany(call_insert_sql, calls[start:start + BATCH_CALL])
        if (i + 1) % commit_every == 0:
            conn.commit()
            n_commits += 1
    conn.commit()
    n_commits += 1
    t_insert = time.perf_counter() - t0

    # 验证入库行数
    sym_count = conn.execute("SELECT COUNT(*) FROM symbols").fetchone()[0]
    call_count = conn.execute("SELECT COUNT(*) FROM calls").fetchone()[0]

    # ---- 阶段 3：建索引 + 触发器（SCHEMA_INDEXES_SQL）----
    t0 = time.perf_counter()
    conn.executescript(SCHEMA_INDEXES_SQL)
    conn.commit()
    t_index = time.perf_counter() - t0

    # 统计建索引后的数量
    cur = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='index' AND sql IS NOT NULL")
    idx_after = cur.fetchone()[0]
    cur = conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='trigger'")
    trigger_after = cur.fetchone()[0]

    # ---- 阶段 4：WAL checkpoint（TRUNCATE）----
    t0 = time.perf_counter()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()
    t_checkpoint = time.perf_counter() - t0

    # ---- 统计 ----
    db_size = os.path.getsize(db_path) / 1024 / 1024
    wal_path = db_path + "-wal"
    wal_size = os.path.getsize(wal_path) / 1024 / 1024 if os.path.exists(wal_path) else 0
    shm_path = db_path + "-shm"
    shm_size = os.path.getsize(shm_path) / 1024 / 1024 if os.path.exists(shm_path) else 0

    # ---- 正确性验证：索引可用性 ----
    t0 = time.perf_counter()
    # 查询 1：通过 qualified_name 索引查找
    row = conn.execute(
        "SELECT * FROM symbols WHERE qualified_name = ? LIMIT 1",
        (f"module_0.func_0",)
    ).fetchone()
    assert row is not None, "索引验证失败：无法通过 qualified_name 查找符号"

    # 查询 2：通过 calls.callee_name 索引查找
    callee_count = conn.execute(
        "SELECT COUNT(*) FROM calls WHERE callee_name = ?", ("ext_func_0",)
    ).fetchone()[0]

    # 查询 3：JOIN 查询（calls → symbols）
    join_result = conn.execute(
        "SELECT s.name, COUNT(*) as n FROM calls c "
        "JOIN symbols s ON c.caller_id = s.id "
        "GROUP BY s.id ORDER BY n DESC LIMIT 1"
    ).fetchone()
    assert join_result is not None, "索引验证失败：JOIN 查询无结果"
    t_verify = time.perf_counter() - t0

    total = t_schema + t_insert + t_index + t_checkpoint
    conn.close()

    return {
        "target_symbols": target_symbols,
        "actual_symbols": sym_count,
        "actual_calls": call_count,
        "n_files": len(files),
        "timing": {
            "gen_s": round(t_gen, 2),
            "schema_s": round(t_schema, 2),
            "insert_s": round(t_insert, 2),
            "index_s": round(t_index, 2),
            "checkpoint_s": round(t_checkpoint, 2),
            "verify_s": round(t_verify, 4),
            "total_s": round(total, 2),
        },
        "db_size_mb": round(db_size, 1),
        "wal_size_mb": round(wal_size, 1),
        "shm_size_mb": round(shm_size, 1),
        "indexes_before": idx_before,
        "indexes_after": idx_after,
        "triggers_before": trigger_before,
        "triggers_after": trigger_after,
        "n_commits": n_commits,
    }


# ============================================
# 主函数
# ============================================

def main():
    parser = argparse.ArgumentParser(description="千万级端到端压测：正式代码 P12 优化验证")
    parser.add_argument("--symbols", type=int, default=10000000, help="目标符号数（默认 10M）")
    parser.add_argument("--db", type=str, default="", help="数据库路径（默认临时目录）")
    parser.add_argument("--commit-every", type=int, default=10, help="每 N 批 commit 一次（默认 10）")
    args = parser.parse_args()

    if args.db:
        db_path = args.db
    else:
        db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               f"_e2e_{args.symbols // 1000000}m.db")

    print(f"\n{'='*60}")
    print(f"千万级端到端压测：正式代码 P12 优化验证")
    print(f"{'='*60}")
    print(f"  目标符号数：{args.symbols:,}")
    print(f"  数据库路径：{db_path}")
    print(f"  commit 频率：每 {args.commit_every} 批")
    print(f"  schema：正式 SCHEMA_TABLES_SQL + SCHEMA_INDEXES_SQL")
    print(f"  PRAGMA：cache_size=-64000 (64MB), mmap=256MB, WAL, synch=NORMAL")
    print()

    result = run_e2e(db_path, args.symbols, args.commit_every)

    print(f"\n{'='*60}")
    print(f"压测结果")
    print(f"{'='*60}")
    t = result["timing"]
    print(f"  数据生成      : {t['gen_s']:8.2f}s")
    print(f"  建表(无索引)  : {t['schema_s']:8.2f}s")
    print(f"  入库(分段commit): {t['insert_s']:8.2f}s  ({result['n_commits']} commits)")
    print(f"  建索引+触发器 : {t['index_s']:8.2f}s  (P12: 延迟建索引)")
    print(f"  WAL checkpoint: {t['checkpoint_s']:8.2f}s")
    print(f"  正确性验证    : {t['verify_s']:8.4f}s")
    print(f"  ─────────────────────────────")
    print(f"  总耗时        : {t['total_s']:8.2f}s")
    print()
    print(f"  数据量：")
    print(f"    文件     : {result['n_files']:,}")
    print(f"    符号     : {result['actual_symbols']:,}")
    print(f"    调用边   : {result['actual_calls']:,}")
    print(f"  存储：")
    print(f"    DB 大小  : {result['db_size_mb']:,.1f} MB")
    print(f"    WAL 大小 : {result['wal_size_mb']:,.1f} MB")
    print(f"    SHM 大小 : {result['shm_size_mb']:,.1f} MB")
    print(f"  索引/触发器：")
    print(f"    建索引前 : {result['indexes_before']} indexes, {result['triggers_before']} triggers")
    print(f"    建索引后 : {result['indexes_after']} indexes, {result['triggers_after']} triggers")
    print()

    # 每M入库耗时
    per_m_insert = t['insert_s'] / (result['actual_symbols'] / 1_000_000)
    per_m_index = t['index_s'] / (result['actual_symbols'] / 1_000_000)
    print(f"  每M入库耗时 : {per_m_insert:.2f}s/M")
    print(f"  每M建索引   : {per_m_index:.2f}s/M")
    print()

    # 保存报告
    report_path = db_path.replace(".db", "_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)
    print(f"  报告已保存：{report_path}")


if __name__ == "__main__":
    main()
