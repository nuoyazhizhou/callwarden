"""
入库策略对比测试

对比三种 SQLite 入库策略在 200 万符号规模下的性能：
  A: 单线程入库 + 最后建索引（基线：先插入数据，索引最后建）
  B: 分片并行入库 + ATTACH 合并（多进程写独立 DB，最后合并）
  C: 内存批量 + 单次大写入（全部数据收集到内存，一次性 executemany）

每个方案测量 5 个阶段：
  1. 建表（无索引）
  2. 生成数据（Python 层）
  3. 入库（INSERT）
  4. 建索引（CREATE INDEX）
  5. 总耗时

用法：
  python tests/_bench_insert_strategies.py
"""
import os
import sys
import time
import sqlite3
import random
import shutil
import tempfile
import json
import multiprocessing as mp
from pathlib import Path


# ============================================
# 公共：数据生成（内存）
# ============================================

def generate_data(target_symbols: int, seed: int = 42):
    """生成合成数据到内存 list，返回 (files, symbols, calls)"""
    rng = random.Random(seed)
    n_files = max(1, target_symbols // 5)
    n_fns = target_symbols * 7 // 10
    n_classes = target_symbols * 2 // 10
    n_structs = target_symbols - n_fns - n_classes

    # 1. 文件
    files = []
    for i in range(1, n_files + 1):
        dir_idx = i // 20
        rel_path = f"src/module_{dir_idx}/file_{i}.py"
        files.append((i, 1, rel_path, f"/{rel_path}", "abc123", 100, "python", "active"))

    # 2. 符号
    symbols = []
    fn_ids = []
    sym_names = []
    sym_id = 0
    for i in range(n_fns):
        sym_id += 1
        fid = (i % n_files) + 1
        name = f"func_{i}"
        qname = f"module_{i // 100}.func_{i}"
        symbols.append((sym_id, fid, name, qname, "fn", "", 1, 10, -1, "", 0, "", "", ""))
        fn_ids.append(sym_id)
        sym_names.append((sym_id, name))
    for i in range(n_classes):
        sym_id += 1
        fid = ((n_fns + i) % n_files) + 1
        name = f"Class_{i}"
        qname = f"module_{i // 100}.Class_{i}"
        symbols.append((sym_id, fid, name, qname, "class", "", 1, 50, -1, "", 0, "", "", ""))
        sym_names.append((sym_id, name))
    for i in range(n_structs):
        sym_id += 1
        fid = ((n_fns + n_classes + i) % n_files) + 1
        name = f"Struct_{i}"
        qname = f"module_{i // 100}.Struct_{i}"
        symbols.append((sym_id, fid, name, qname, "struct", "", 1, 20, -1, "", 0, "", "", ""))

    # 3. 调用边
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
            caller_qname = f"module_{(caller_id-1) // 100}.func_{caller_id - 1}"
            caller_fid = ((caller_id - 1) % n_files) + 1
            is_cross = 1 if rng.random() < 0.4 else 0
            call_line = rng.randint(1, 100)
            calls.append((call_id, caller_id, caller_name, caller_qname,
                          callee_name, callee_qname, "", callee_id, call_line, is_cross, caller_fid))

    return files, symbols, calls


# ============================================
# 公共：建表 SQL（无索引）
# ============================================

SCHEMA_NO_INDEX = """
CREATE TABLE workspaces (
    id INTEGER PRIMARY KEY,
    root_path TEXT,
    name TEXT,
    created_at TEXT DEFAULT (datetime('now')),
    is_active INTEGER DEFAULT 0
);
CREATE TABLE file_instances (
    id INTEGER PRIMARY KEY,
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    abs_path TEXT,
    content_hash TEXT,
    total_lines INTEGER DEFAULT 0,
    language TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now'))
);
CREATE TABLE symbols (
    id INTEGER PRIMARY KEY,
    file_instance_id INTEGER NOT NULL,
    name TEXT NOT NULL,
    qualified_name TEXT NOT NULL,
    kind TEXT NOT NULL,
    module_path TEXT DEFAULT '',
    start_line INTEGER DEFAULT 0,
    end_line INTEGER DEFAULT 0,
    depth INTEGER DEFAULT -1,
    symbol_hash TEXT DEFAULT '',
    has_comment INTEGER DEFAULT 0,
    visibility TEXT DEFAULT '',
    content TEXT DEFAULT '',
    signature TEXT DEFAULT ''
);
CREATE TABLE calls (
    id INTEGER PRIMARY KEY,
    caller_id INTEGER NOT NULL,
    caller_name TEXT DEFAULT '',
    caller_qualified TEXT DEFAULT '',
    callee_name TEXT NOT NULL,
    callee_qualified TEXT DEFAULT '',
    callee_module TEXT DEFAULT '',
    callee_id INTEGER DEFAULT 0,
    call_line INTEGER DEFAULT 0,
    is_cross_file INTEGER DEFAULT 0,
    file_instance_id INTEGER DEFAULT 0
);
"""

INDEX_SQL = """
CREATE INDEX idx_symbols_qname ON symbols(qualified_name);
CREATE INDEX idx_symbols_name ON symbols(name);
CREATE INDEX idx_symbols_file ON symbols(file_instance_id);
CREATE INDEX idx_calls_caller ON calls(caller_id);
CREATE INDEX idx_calls_callee_id ON calls(callee_id);
CREATE INDEX idx_calls_callee_name ON calls(callee_name);
"""


# ============================================
# 方案 A：单线程入库 + 最后建索引
# ============================================

def strategy_a(db_path: str, files, symbols, calls):
    """方案 A：建表（无索引）→ 单线程批量入库 → 最后建索引"""
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-256000")  # 256MB cache

    t0 = time.perf_counter()
    # 1. 建表（无索引）
    conn.executescript(SCHEMA_NO_INDEX)
    conn.execute("INSERT INTO workspaces (id, root_path, name, is_active) VALUES (1, '/synthetic', 'synthetic', 1)")
    t_schema = time.perf_counter() - t0

    t0 = time.perf_counter()
    # 2. 入库：文件
    conn.executemany(
        "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path, content_hash, total_lines, language, status) VALUES (?,?,?,?,?,?,?,?)",
        files
    )
    # 3. 入库：符号（分批 50000）
    BATCH = 50000
    for start in range(0, len(symbols), BATCH):
        conn.executemany(
            "INSERT INTO symbols (id, file_instance_id, name, qualified_name, kind, module_path, start_line, end_line, depth, symbol_hash, has_comment, visibility, content, signature) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            symbols[start:start + BATCH]
        )
    # 4. 入库：调用边（分批 100000）
    BATCH_CALL = 100000
    for start in range(0, len(calls), BATCH_CALL):
        conn.executemany(
            "INSERT INTO calls (id, caller_id, caller_name, caller_qualified, callee_name, callee_qualified, callee_module, callee_id, call_line, is_cross_file, file_instance_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            calls[start:start + BATCH_CALL]
        )
    conn.commit()
    t_insert = time.perf_counter() - t0

    t0 = time.perf_counter()
    # 5. 最后建索引
    conn.executescript(INDEX_SQL)
    conn.commit()
    t_index = time.perf_counter() - t0

    db_size = os.path.getsize(db_path) / 1024 / 1024
    conn.close()
    return {
        "schema_s": round(t_schema, 2),
        "insert_s": round(t_insert, 2),
        "index_s": round(t_index, 2),
        "total_s": round(t_schema + t_insert + t_index, 2),
        "db_size_mb": round(db_size, 1),
    }


# ============================================
# 方案 A+checkpoint：A 基础 + 每 10 批 commit 一次（容错版）
# ============================================

def strategy_a_checkpoint(db_path: str, files, symbols, calls, commit_every: int = 10):
    """方案 A+checkpoint：A 基础 + 每 N 批 commit 一次（容错版）

    - 挂掉时只丢最后 N 批（N*50000 符号 / N*100000 调用边）
    - 每次 commit 触发 1 次 fsync（~10ms），200万/5万=40 批 → 4 次 commit = 40ms
    """
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-256000")

    t0 = time.perf_counter()
    conn.executescript(SCHEMA_NO_INDEX)
    conn.execute("INSERT INTO workspaces (id, root_path, name, is_active) VALUES (1, '/synthetic', 'synthetic', 1)")
    t_schema = time.perf_counter() - t0

    n_commits = 0
    t0 = time.perf_counter()
    # 1. 文件（量小，一次 commit）
    conn.executemany(
        "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path, content_hash, total_lines, language, status) VALUES (?,?,?,?,?,?,?,?)",
        files
    )
    conn.commit()
    n_commits += 1

    # 2. 符号：每 commit_every 批 commit 一次
    BATCH = 50000
    sym_batches = list(range(0, len(symbols), BATCH))
    for i, start in enumerate(sym_batches):
        conn.executemany(
            "INSERT INTO symbols (id, file_instance_id, name, qualified_name, kind, module_path, start_line, end_line, depth, symbol_hash, has_comment, visibility, content, signature) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            symbols[start:start + BATCH]
        )
        if (i + 1) % commit_every == 0:
            conn.commit()
            n_commits += 1
    conn.commit()
    n_commits += 1

    # 3. 调用边：每 commit_every 批 commit 一次
    BATCH_CALL = 100000
    call_batches = list(range(0, len(calls), BATCH_CALL))
    for i, start in enumerate(call_batches):
        conn.executemany(
            "INSERT INTO calls (id, caller_id, caller_name, caller_qualified, callee_name, callee_qualified, callee_module, callee_id, call_line, is_cross_file, file_instance_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            calls[start:start + BATCH_CALL]
        )
        if (i + 1) % commit_every == 0:
            conn.commit()
            n_commits += 1
    conn.commit()
    n_commits += 1
    t_insert = time.perf_counter() - t0

    t0 = time.perf_counter()
    conn.executescript(INDEX_SQL)
    conn.commit()
    t_index = time.perf_counter() - t0

    # 缺陷修复：显式 checkpoint + truncate WAL，避免 WAL 残留几 GB
    t0 = time.perf_counter()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()
    t_checkpoint = time.perf_counter() - t0

    db_size = os.path.getsize(db_path) / 1024 / 1024
    # WAL 文件大小（如果存在）
    wal_path = db_path + "-wal"
    wal_size = os.path.getsize(wal_path) / 1024 / 1024 if os.path.exists(wal_path) else 0
    conn.close()
    return {
        "schema_s": round(t_schema, 2),
        "insert_s": round(t_insert, 2),
        "index_s": round(t_index, 2),
        "checkpoint_s": round(t_checkpoint, 2),
        "total_s": round(t_schema + t_insert + t_index + t_checkpoint, 2),
        "db_size_mb": round(db_size, 1),
        "wal_size_mb": round(wal_size, 1),
        "n_commits": n_commits,
    }


# ============================================
# 方案 B：分片并行入库 + ATTACH 合并
# ============================================

def _worker_b(args):
    """worker：写入分片到独立 DB"""
    shard_idx, shard_dir, files_shard, symbols_shard, calls_shard = args
    db_path = os.path.join(shard_dir, f"shard_{shard_idx}.db")
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA cache_size=-128000")  # 128MB cache/worker

    # 建表（无索引，无 workspaces 表，分片不需要）
    conn.executescript(SCHEMA_NO_INDEX)

    # 入库
    if files_shard:
        conn.executemany(
            "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path, content_hash, total_lines, language, status) VALUES (?,?,?,?,?,?,?,?)",
            files_shard
        )
    if symbols_shard:
        BATCH = 50000
        for start in range(0, len(symbols_shard), BATCH):
            conn.executemany(
                "INSERT INTO symbols (id, file_instance_id, name, qualified_name, kind, module_path, start_line, end_line, depth, symbol_hash, has_comment, visibility, content, signature) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                symbols_shard[start:start + BATCH]
            )
    if calls_shard:
        BATCH_CALL = 100000
        for start in range(0, len(calls_shard), BATCH_CALL):
            conn.executemany(
                "INSERT INTO calls (id, caller_id, caller_name, caller_qualified, callee_name, callee_qualified, callee_module, callee_id, call_line, is_cross_file, file_instance_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                calls_shard[start:start + BATCH_CALL]
            )
    conn.commit()
    conn.close()
    return shard_idx, db_path


def strategy_b(db_path: str, files, symbols, calls, n_workers: int = 4):
    """方案 B：N 个 worker 并行写独立 DB → ATTACH 合并 → 最后建索引"""
    if os.path.exists(db_path):
        os.remove(db_path)
    shard_dir = tempfile.mkdtemp(prefix="cw_shards_")

    try:
        # 1. 分片
        t0 = time.perf_counter()
        sym_shards = [[] for _ in range(n_workers)]
        for i, s in enumerate(symbols):
            sym_shards[i % n_workers].append(s)
        call_shards = [[] for _ in range(n_workers)]
        for i, c in enumerate(calls):
            call_shards[i % n_workers].append(c)
        file_shards = [[] for _ in range(n_workers)]
        for i, f in enumerate(files):
            file_shards[i % n_workers].append(f)

        worker_args = []
        for i in range(n_workers):
            worker_args.append((i, shard_dir, file_shards[i], sym_shards[i], call_shards[i]))
        t_split = time.perf_counter() - t0

        # 2. 并行入库到独立 DB
        t0 = time.perf_counter()
        with mp.Pool(n_workers) as pool:
            results = pool.map(_worker_b, worker_args)
        t_parallel = time.perf_counter() - t0

        # 3. ATTACH 合并到主 DB
        t0 = time.perf_counter()
        conn = sqlite3.connect(db_path, timeout=60)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("PRAGMA cache_size=-256000")
        conn.executescript(SCHEMA_NO_INDEX)
        conn.execute("INSERT INTO workspaces (id, root_path, name, is_active) VALUES (1, '/synthetic', 'synthetic', 1)")

        for shard_idx, shard_db in sorted(results):
            conn.execute(f"ATTACH DATABASE '{shard_db}' AS s{shard_idx}")
            conn.execute(f"INSERT INTO file_instances SELECT * FROM s{shard_idx}.file_instances")
            conn.execute(f"INSERT INTO symbols SELECT * FROM s{shard_idx}.symbols")
            conn.execute(f"INSERT INTO calls SELECT * FROM s{shard_idx}.calls")
            conn.commit()  # 每个 shard 立即 commit 释放写锁
            conn.execute(f"DETACH DATABASE s{shard_idx}")
        t_merge = time.perf_counter() - t0

        # 4. 最后建索引
        t0 = time.perf_counter()
        conn.executescript(INDEX_SQL)
        conn.commit()
        t_index = time.perf_counter() - t0

        db_size = os.path.getsize(db_path) / 1024 / 1024
        conn.close()
        return {
            "split_s": round(t_split, 2),
            "parallel_s": round(t_parallel, 2),
            "merge_s": round(t_merge, 2),
            "index_s": round(t_index, 2),
            "total_s": round(t_split + t_parallel + t_merge + t_index, 2),
            "db_size_mb": round(db_size, 1),
            "n_workers": n_workers,
        }
    finally:
        shutil.rmtree(shard_dir, ignore_errors=True)


# ============================================
# 方案 C：内存批量 + 单次大写入
# ============================================

def strategy_c(db_path: str, files, symbols, calls):
    """方案 C：全部数据已在内存 list → 单次 executemany（不分批）→ 最后建索引"""
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA cache_size=-512000")  # 512MB cache，更大的批量

    t0 = time.perf_counter()
    conn.executescript(SCHEMA_NO_INDEX)
    conn.execute("INSERT INTO workspaces (id, root_path, name, is_active) VALUES (1, '/synthetic', 'synthetic', 1)")
    t_schema = time.perf_counter() - t0

    t0 = time.perf_counter()
    # 单次 executemany，不分批
    conn.executemany(
        "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path, content_hash, total_lines, language, status) VALUES (?,?,?,?,?,?,?,?)",
        files
    )
    conn.executemany(
        "INSERT INTO symbols (id, file_instance_id, name, qualified_name, kind, module_path, start_line, end_line, depth, symbol_hash, has_comment, visibility, content, signature) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        symbols
    )
    conn.executemany(
        "INSERT INTO calls (id, caller_id, caller_name, caller_qualified, callee_name, callee_qualified, callee_module, callee_id, call_line, is_cross_file, file_instance_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
        calls
    )
    conn.commit()
    t_insert = time.perf_counter() - t0

    t0 = time.perf_counter()
    conn.executescript(INDEX_SQL)
    conn.commit()
    t_index = time.perf_counter() - t0

    db_size = os.path.getsize(db_path) / 1024 / 1024
    conn.close()
    return {
        "schema_s": round(t_schema, 2),
        "insert_s": round(t_insert, 2),
        "index_s": round(t_index, 2),
        "total_s": round(t_schema + t_insert + t_index, 2),
        "db_size_mb": round(db_size, 1),
    }


# ============================================
# 基线：原压测脚本方式（先建索引 + 分批 commit）
# ============================================

def strategy_baseline(db_path: str, files, symbols, calls):
    """基线：建表+建索引 → 分批入库+每批 commit（原压测脚本方式）"""
    if os.path.exists(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")

    t0 = time.perf_counter()
    conn.executescript(SCHEMA_NO_INDEX + INDEX_SQL)  # 表+索引一起建
    conn.execute("INSERT INTO workspaces (id, root_path, name, is_active) VALUES (1, '/synthetic', 'synthetic', 1)")
    t_schema = time.perf_counter() - t0

    t0 = time.perf_counter()
    conn.executemany(
        "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path, content_hash, total_lines, language, status) VALUES (?,?,?,?,?,?,?,?)",
        files
    )
    BATCH = 50000
    for start in range(0, len(symbols), BATCH):
        conn.executemany(
            "INSERT INTO symbols (id, file_instance_id, name, qualified_name, kind, module_path, start_line, end_line, depth, symbol_hash, has_comment, visibility, content, signature) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            symbols[start:start + BATCH]
        )
        conn.commit()  # 每批 commit（基线的低效点）
    BATCH_CALL = 100000
    for start in range(0, len(calls), BATCH_CALL):
        conn.executemany(
            "INSERT INTO calls (id, caller_id, caller_name, caller_qualified, callee_name, callee_qualified, callee_module, callee_id, call_line, is_cross_file, file_instance_id) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            calls[start:start + BATCH_CALL]
        )
        conn.commit()  # 每批 commit
    conn.commit()
    t_insert = time.perf_counter() - t0

    db_size = os.path.getsize(db_path) / 1024 / 1024
    conn.close()
    return {
        "schema_s": round(t_schema, 2),
        "insert_s": round(t_insert, 2),
        "index_s": 0.0,  # 索引已在 schema 阶段建好
        "total_s": round(t_schema + t_insert, 2),
        "db_size_mb": round(db_size, 1),
    }


# ============================================
# 主流程
# ============================================

def main():
    target_symbols = int(sys.argv[1]) if len(sys.argv) > 1 else 2_000_000
    # --only baseline,A,B_4w,B_8w,C 指定只跑某些策略
    only_arg = None
    for i, a in enumerate(sys.argv):
        if a == "--only" and i + 1 < len(sys.argv):
            only_arg = sys.argv[i + 1]
    only_set = set(only_arg.split(",")) if only_arg else None

    print(f"=" * 70)
    print(f"  入库策略对比测试 ({target_symbols:,} 符号)")
    print(f"=" * 70)

    tmp_dir = tempfile.mkdtemp(prefix="cw_strategies_")
    try:
        # 1. 生成数据（共享）
        print(f"\n[0/5] 生成合成数据到内存...", flush=True)
        t0 = time.perf_counter()
        files, symbols, calls = generate_data(target_symbols)
        t_gen = time.perf_counter() - t0
        print(f"  生成耗时: {t_gen:.2f}s", flush=True)
        print(f"  文件: {len(files):,} | 符号: {len(symbols):,} | 调用边: {len(calls):,}", flush=True)

        results = {}

        # 2. 基线（原方式）
        if only_set is None or "baseline" in only_set:
            print(f"\n[1/6] 基线（先建索引 + 每批 commit）...", flush=True)
            t0 = time.perf_counter()
            results["baseline"] = strategy_baseline(
                os.path.join(tmp_dir, "baseline.db"), files, symbols, calls
            )
            print(f"  完成: {time.perf_counter() - t0:.2f}s", flush=True)
            print(f"  {results['baseline']}", flush=True)

        # 3. 方案 A
        if only_set is None or "A" in only_set:
            print(f"\n[2/6] 方案 A（单线程 + 最后建索引）...", flush=True)
            t0 = time.perf_counter()
            results["A"] = strategy_a(
                os.path.join(tmp_dir, "strategy_a.db"), files, symbols, calls
            )
            print(f"  完成: {time.perf_counter() - t0:.2f}s", flush=True)
            print(f"  {results['A']}", flush=True)

        # 3.5 方案 A+checkpoint（容错版）
        if only_set is None or "A_ckpt" in only_set:
            print(f"\n[2.5/6] 方案 A+checkpoint（单线程 + 每10批commit + 最后建索引）...", flush=True)
            t0 = time.perf_counter()
            results["A_ckpt"] = strategy_a_checkpoint(
                os.path.join(tmp_dir, "strategy_a_ckpt.db"), files, symbols, calls
            )
            print(f"  完成: {time.perf_counter() - t0:.2f}s", flush=True)
            print(f"  {results['A_ckpt']}", flush=True)

        # 4. 方案 B（4 workers）
        if only_set is None or "B_4w" in only_set:
            print(f"\n[3/5] 方案 B（4 workers 并行 + ATTACH 合并）...", flush=True)
            t0 = time.perf_counter()
            results["B_4w"] = strategy_b(
                os.path.join(tmp_dir, "strategy_b_4w.db"), files, symbols, calls, n_workers=4
            )
            print(f"  完成: {time.perf_counter() - t0:.2f}s", flush=True)
            print(f"  {results['B_4w']}", flush=True)

        # 5. 方案 B（8 workers）
        if only_set is None or "B_8w" in only_set:
            print(f"\n[4/5] 方案 B（8 workers 并行 + ATTACH 合并）...", flush=True)
            t0 = time.perf_counter()
            results["B_8w"] = strategy_b(
                os.path.join(tmp_dir, "strategy_b_8w.db"), files, symbols, calls, n_workers=8
            )
            print(f"  完成: {time.perf_counter() - t0:.2f}s", flush=True)
            print(f"  {results['B_8w']}", flush=True)

        # 6. 方案 C
        if only_set is None or "C" in only_set:
            print(f"\n[5/5] 方案 C（内存批量 + 单次大写入 + 最后建索引）...", flush=True)
            t0 = time.perf_counter()
            results["C"] = strategy_c(
                os.path.join(tmp_dir, "strategy_c.db"), files, symbols, calls
            )
            print(f"  完成: {time.perf_counter() - t0:.2f}s", flush=True)
            print(f"  {results['C']}", flush=True)

        # 汇总
        print(f"\n{'=' * 70}")
        print(f"  汇总对比 ({target_symbols:,} 符号)")
        print(f"{'=' * 70}")
        print(f"{'方案':<20} {'总耗时(s)':<12} {'入库(s)':<12} {'建索引(s)':<12} {'DB(MB)':<10}")
        print("-" * 70)
        for name, r in results.items():
            insert_s = r.get("insert_s", r.get("parallel_s", 0))
            index_s = r.get("index_s", 0)
            print(f"{name:<20} {r['total_s']:<12.2f} {insert_s:<12.2f} {index_s:<12.2f} {r['db_size_mb']:<10.1f}")

        # 保存报告
        report_path = "tests/_bench_insert_strategies_report.json"
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump({
                "target_symbols": target_symbols,
                "data_gen_s": round(t_gen, 2),
                "strategies": results,
            }, f, indent=2, ensure_ascii=False)
        print(f"\n报告已保存: {report_path}")

    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)


if __name__ == "__main__":
    mp.freeze_support()
    main()
