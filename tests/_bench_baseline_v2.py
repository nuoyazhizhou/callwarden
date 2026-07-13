"""
修正后的压测基准体系 v2

修复 v1 的 6 个方法论问题：
1. 流式生成并入库（不保留全部合成数据，避免内存干扰）
2. 串行运行（不并行，避免磁盘/内存/缓存干扰）
3. A/B 对比（eager-index vs deferred-index）
4. 分开报告 storage_build_time 和 end_to_end_time
5. 完整环境指标（CPU/内存/磁盘/SQLite版本/峰值RSS/峰值WAL/磁盘读写量）
6. 每个索引单独计时（找出真正昂贵的索引）

用法：
  python tests/_bench_baseline_v2.py --symbols 1000000 --runs 3
  python tests/_bench_baseline_v2.py --symbols 2000000 --runs 3 --mode deferred
  python tests/_bench_baseline_v2.py --symbols 1000000 --runs 1 --mode both  # A/B 对比
"""
import os
import sys
import time
import sqlite3
import random
import shutil
import json
import argparse
import threading
import platform
from pathlib import Path
from typing import Iterator, List, Tuple

# 直接加载 schema.py 模块（避免 db/__init__.py 的相对导入链）
import importlib.util
_schema_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "db", "schema.py")
_spec = importlib.util.spec_from_file_location("callwarden_schema", _schema_path)
_schema_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_schema_mod)
SCHEMA_TABLES_SQL = _schema_mod.SCHEMA_TABLES_SQL
SCHEMA_INDEXES_SQL = _schema_mod.SCHEMA_INDEXES_SQL
SCHEMA_SQL_FULL = _schema_mod.SCHEMA_SQL  # 完整 schema（建表+建索引一起，用于 eager 模式）


# ============================================
# 环境信息采集
# ============================================

def collect_env_info() -> dict:
    """采集测试环境信息"""
    info = {
        "sqlite_version": sqlite3.sqlite_version,
        "python_version": platform.python_version(),
        "os": platform.platform(),
        "cpu": platform.processor(),
        "cpu_cores": os.cpu_count(),
    }
    try:
        import psutil
        mem = psutil.virtual_memory()
        info["ram_total_gb"] = mem.total // 1024 // 1024 // 1024
        info["ram_available_mb"] = mem.available // 1024 // 1024
        d = psutil.disk_usage(os.getcwd())
        info["disk_total_gb"] = d.total // 1024 // 1024 // 1024
        info["disk_free_gb"] = d.free // 1024 // 1024 // 1024
    except ImportError:
        pass
    return info


class ResourceMonitor:
    """后台资源监控：峰值 RSS、峰值 WAL 大小"""

    def __init__(self, db_path: str, interval: float = 0.5):
        self.db_path = db_path
        self.interval = interval
        self._running = False
        self._thread = None
        self.peak_rss_mb = 0
        self.peak_wal_mb = 0
        self.samples = []

    def _monitor_loop(self):
        try:
            import psutil
            proc = psutil.Process()
        except ImportError:
            return
        while self._running:
            try:
                rss = proc.memory_info().rss / 1024 / 1024
                wal_path = self.db_path + "-wal"
                wal_size = os.path.getsize(wal_path) / 1024 / 1024 if os.path.exists(wal_path) else 0
                self.peak_rss_mb = max(self.peak_rss_mb, rss)
                self.peak_wal_mb = max(self.peak_wal_mb, wal_size)
                self.samples.append({"t": time.time(), "rss_mb": rss, "wal_mb": wal_size})
            except Exception:
                pass
            time.sleep(self.interval)

    def start(self):
        self._running = True
        self._thread = threading.Thread(target=self._monitor_loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=2)


# ============================================
# 流式数据生成器
# ============================================

def gen_files_stream(n_files: int, batch_size: int = 1000) -> Iterator[List[tuple]]:
    """流式生成文件数据，每次 yield 一个 batch"""
    batch = []
    for i in range(1, n_files + 1):
        dir_idx = i // 20
        rel_path = f"src/module_{dir_idx}/file_{i}.py"
        batch.append((
            i, 1, rel_path, f"/{rel_path}", f"hash_{i:08x}",
            float(i), 100, 0.0, "active", f"module_{dir_idx}",
        ))
        if len(batch) >= batch_size:
            yield batch
            batch = []
    if batch:
        yield batch


def gen_symbols_stream(target_symbols: int, n_files: int, batch_size: int = 50000, seed: int = 42) -> Iterator[List[tuple]]:
    """流式生成符号数据，每次 yield 一个 batch

    关键：生成顺序就是插入顺序，按 sym_id 递增（与正式代码一致）
    """
    rng = random.Random(seed)
    n_fns = target_symbols * 7 // 10
    n_classes = target_symbols * 2 // 10
    n_structs = target_symbols - n_fns - n_classes

    batch = []
    sym_id = 0

    # 函数
    for i in range(n_fns):
        sym_id += 1
        fid = (i % n_files) + 1
        name = f"func_{i}"
        qname = f"module_{i // 100}.func_{i}"
        batch.append((
            sym_id, fid, f"sh_{sym_id:08x}", name, "fn", "public",
            (sym_id % 10000) + 1, (sym_id % 10000) + 10, 0, 0,
            "def func(): pass", 1, "ok", f"module_{i // 100}", qname, -1,
        ))
        if len(batch) >= batch_size:
            yield batch
            batch = []

    # 类
    for i in range(n_classes):
        sym_id += 1
        fid = ((n_fns + i) % n_files) + 1
        name = f"Class_{i}"
        qname = f"module_{i // 100}.Class_{i}"
        batch.append((
            sym_id, fid, f"sh_{sym_id:08x}", name, "class", "public",
            (sym_id % 10000) + 1, (sym_id % 10000) + 50, 0, 0,
            "", 1, "ok", f"module_{i // 100}", qname, 0,
        ))
        if len(batch) >= batch_size:
            yield batch
            batch = []

    # 结构体
    for i in range(n_structs):
        sym_id += 1
        fid = ((n_fns + n_classes + i) % n_files) + 1
        name = f"Struct_{i}"
        qname = f"module_{i // 100}.Struct_{i}"
        batch.append((
            sym_id, fid, f"sh_{sym_id:08x}", name, "struct", "public",
            (sym_id % 10000) + 1, (sym_id % 10000) + 20, 0, 0,
            "", 0, "pending", f"module_{i // 100}", qname, -1,
        ))
        if len(batch) >= batch_size:
            yield batch
            batch = []

    if batch:
        yield batch


def gen_calls_stream(target_symbols: int, n_files: int, batch_size: int = 100000, seed: int = 42) -> Iterator[List[tuple]]:
    """流式生成调用边，每次 yield 一个 batch

    关键：需要知道所有 fn_ids 才能生成 calls。
    为了真正流式，我们用一个确定性公式生成 callee，而不是预先生成 sym_names 列表。

    sym_id 分布：
    - fn: 1 ~ n_fns
    - class: n_fns+1 ~ n_fns+n_classes
    - struct: n_fns+n_classes+1 ~ target_symbols

    callee 70% 概率是函数（1~n_fns），30% 概率是外部函数（callee_id=0）
    """
    rng = random.Random(seed)
    n_fns = target_symbols * 7 // 10

    batch = []
    call_id = 0
    for caller_id in range(1, n_fns + 1):
        n_callees = rng.randint(5, 15)
        for _ in range(n_callees):
            call_id += 1
            if rng.random() < 0.3:
                # 外部函数
                callee_name = f"ext_func_{rng.randint(0, 99999)}"
                callee_id = 0
                callee_qname = ""
            else:
                # 内部函数（callee_id 在 1~n_fns 范围内）
                callee_id = rng.randint(1, n_fns)
                callee_name = f"func_{callee_id - 1}"
                callee_qname = f"module_{(callee_id - 1) // 100}.{callee_name}"
            caller_name = f"func_{caller_id - 1}"
            caller_module = f"module_{(caller_id - 1) // 100}"
            is_cross = 1 if rng.random() < 0.4 else 0
            call_line = rng.randint(1, 100)
            batch.append((
                call_id, caller_id, caller_name, caller_module,
                callee_name, caller_module, callee_qname, "",
                callee_id, call_line, is_cross,
            ))
            if len(batch) >= batch_size:
                yield batch
                batch = []
    if batch:
        yield batch


# ============================================
# 拆分 SCHEMA_INDEXES_SQL 为单条语句（用于逐索引计时）
# ============================================

def split_index_statements() -> List[Tuple[str, str]]:
    """拆分 SCHEMA_INDEXES_SQL 为 (name, sql) 列表

    返回 [(语句类型+目标, SQL), ...]
    例如 [("INDEX idx_symbols_file", "CREATE INDEX IF NOT EXISTS idx_symbols_file ON ..."), ...]
    """
    import re

    statements = []
    buf = []
    in_trigger = False

    for line in SCHEMA_INDEXES_SQL.split('\n'):
        buf.append(line)
        stripped = line.strip()

        if stripped.upper().startswith('CREATE TRIGGER'):
            in_trigger = True

        if in_trigger:
            if stripped.upper() == 'END;':
                stmt = '\n'.join(buf).strip()
                if stmt:
                    # 提取触发器名：CREATE TRIGGER [IF NOT EXISTS] <name>
                    m = re.match(r'CREATE\s+TRIGGER\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)', stmt, re.IGNORECASE)
                    name = f"TRIGGER {m.group(1)}" if m else "TRIGGER"
                    statements.append((name, stmt))
                buf = []
                in_trigger = False
        else:
            if stripped.endswith(';'):
                stmt = '\n'.join(buf).strip()
                if stmt:
                    # 提取索引名：CREATE [UNIQUE] INDEX [IF NOT EXISTS] <name>
                    m = re.match(
                        r'CREATE\s+(?:UNIQUE\s+)?INDEX\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)',
                        stmt, re.IGNORECASE
                    )
                    if m:
                        is_unique = 'UNIQUE' in stmt.upper()[:30]
                        prefix = "UNIQUE INDEX" if is_unique else "INDEX"
                        name = f"{prefix} {m.group(1)}"
                    else:
                        name = "UNKNOWN"
                    statements.append((name, stmt))
                buf = []

    if buf:
        stmt = '\n'.join(buf).strip()
        if stmt:
            statements.append(("TAIL", stmt))

    return statements


# ============================================
# 基准测试：单次运行
# ============================================

def run_single(
    db_path: str,
    target_symbols: int,
    mode: str = "deferred",
    commit_every: int = 10,
    cache_size_kb: int = 65536,
    mmap_size: int = 268435456,
    page_size: int = 4096,
    temp_store: str = "MEMORY",
    per_index_timing: bool = False,
) -> dict:
    """单次基准测试

    Args:
        mode: "deferred"（建表无索引→入库→建索引）或 "eager"（建表时建索引→入库）
        per_index_timing: 是否逐索引计时（仅 deferred 模式有效）
    """
    # 清理
    if os.path.exists(db_path):
        os.remove(db_path)
    for suffix in ("-wal", "-shm"):
        p = db_path + suffix
        if os.path.exists(p):
            os.remove(p)

    n_files = max(1, target_symbols // 5)

    # 启动资源监控
    monitor = ResourceMonitor(db_path, interval=0.5)
    monitor.start()

    # PRAGMA 设置
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA busy_timeout=5000")
    # page_size 必须在创建任何表之前设置
    if page_size != 4096:
        conn.execute(f"PRAGMA page_size={page_size}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute(f"PRAGMA cache_size=-{cache_size_kb // 1024}")  # 转为 MB
    conn.execute(f"PRAGMA temp_store={temp_store}")
    conn.execute(f"PRAGMA mmap_size={mmap_size}")
    conn.execute("PRAGMA locking_mode=NORMAL")
    conn.execute("PRAGMA foreign_keys=OFF")

    result = {
        "mode": mode,
        "target_symbols": target_symbols,
        "n_files": n_files,
        "cache_size_mb": cache_size_kb // 1024,
        "mmap_size_mb": mmap_size // 1024 // 1024,
        "page_size": page_size,
        "temp_store": temp_store,
    }

    # ---- 阶段 0：建表 ----
    t0 = time.perf_counter()
    if mode == "deferred":
        # 只建表（无索引/触发器）
        conn.executescript(SCHEMA_TABLES_SQL)
        conn.execute(
            "INSERT INTO workspaces (id, name, root_path, created_at, is_active, description) "
            "VALUES (1, 'synthetic', '/synthetic', ?, 1, '压测工作区')",
            (time.time(),)
        )
        conn.commit()
    else:  # eager
        # 建表 + 建索引一起（完整 schema）
        conn.executescript(SCHEMA_SQL_FULL)
        conn.execute(
            "INSERT INTO workspaces (id, name, root_path, created_at, is_active, description) "
            "VALUES (1, 'synthetic', '/synthetic', ?, 1, '压测工作区')",
            (time.time(),)
        )
        conn.commit()
    t_schema = time.perf_counter() - t0

    # ---- 阶段 1：入库（流式）----
    n_commits = 0
    sym_count = 0
    call_count = 0
    t0 = time.perf_counter()

    # 1a. 文件（流式）
    file_insert_sql = (
        "INSERT INTO file_instances (id, workspace_id, rel_path, abs_path, current_content_hash, "
        "mtime, total_lines, last_parsed, status, module_path) VALUES (?,?,?,?,?,?,?,?,?,?)"
    )
    for batch in gen_files_stream(n_files):
        conn.executemany(file_insert_sql, batch)
    conn.commit()
    n_commits += 1

    # 2b. 符号（流式，每 commit_every 批 commit）
    sym_insert_sql = (
        "INSERT INTO symbols (id, file_instance_id, symbol_hash, name, kind, visibility, "
        "start_line, end_line, start_col, end_col, signature, has_comment, comment_status, "
        "module_path, qualified_name, depth) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
    )
    for i, batch in enumerate(gen_symbols_stream(target_symbols, n_files)):
        conn.executemany(sym_insert_sql, batch)
        sym_count += len(batch)
        if (i + 1) % commit_every == 0:
            conn.commit()
            n_commits += 1
    conn.commit()
    n_commits += 1

    # 2c. 调用边（流式，每 commit_every 批 commit）
    call_insert_sql = (
        "INSERT INTO calls (id, caller_id, caller_name, caller_module, callee_name, "
        "callee_module, callee_qualified, callee_file, callee_id, call_line, is_cross_file) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?)"
    )
    for i, batch in enumerate(gen_calls_stream(target_symbols, n_files)):
        conn.executemany(call_insert_sql, batch)
        call_count += len(batch)
        if (i + 1) % commit_every == 0:
            conn.commit()
            n_commits += 1
    conn.commit()
    n_commits += 1
    t_insert = time.perf_counter() - t0

    # ---- 阶段 2：建索引（仅 deferred 模式）----
    index_timings = []
    t0 = time.perf_counter()
    if mode == "deferred":
        if per_index_timing:
            # 逐索引计时
            for name, stmt in split_index_statements():
                t_idx_start = time.perf_counter()
                try:
                    conn.execute(stmt)
                    conn.commit()
                except Exception as e:
                    index_timings.append({"name": name, "time_s": -1, "error": str(e)})
                    continue
                t_idx = time.perf_counter() - t_idx_start
                index_timings.append({"name": name, "time_s": round(t_idx, 4)})
        else:
            # 一次性建索引
            conn.executescript(SCHEMA_INDEXES_SQL)
            conn.commit()
    t_index = time.perf_counter() - t0

    # ---- 阶段 3：WAL checkpoint ----
    t0 = time.perf_counter()
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()
    t_checkpoint = time.perf_counter() - t0

    # ---- 阶段 4：正确性验证 ----
    t0 = time.perf_counter()
    row = conn.execute(
        "SELECT * FROM symbols WHERE qualified_name = ? LIMIT 1",
        (f"module_0.func_0",)
    ).fetchone()
    assert row is not None, "验证失败：qualified_name 查询无结果"

    callee_count = conn.execute(
        "SELECT COUNT(*) FROM calls WHERE callee_name = ?", ("ext_func_0",)
    ).fetchone()[0]

    join_result = conn.execute(
        "SELECT s.name, COUNT(*) as n FROM calls c "
        "JOIN symbols s ON c.caller_id = s.id "
        "GROUP BY s.id ORDER BY n DESC LIMIT 1"
    ).fetchone()
    assert join_result is not None, "验证失败：JOIN 查询无结果"
    t_verify = time.perf_counter() - t0

    # ---- 查询性能采样（冷/热缓存）----
    # 冷缓存：关闭并重新打开连接
    conn.close()

    t0 = time.perf_counter()
    conn2 = sqlite3.connect(db_path)
    conn2.execute("PRAGMA cache_size=-65536")
    # 冷缓存查询（首次查询）
    cold_q1 = time.perf_counter() - t0
    t0 = time.perf_counter()
    _ = conn2.execute("SELECT * FROM symbols WHERE qualified_name = ? LIMIT 1", ("module_0.func_0",)).fetchone()
    cold_q2 = time.perf_counter() - t0
    t0 = time.perf_counter()
    _ = conn2.execute("SELECT COUNT(*) FROM calls WHERE callee_name = ?", ("ext_func_0",)).fetchone()[0]
    cold_q3 = time.perf_counter() - t0
    # 热缓存查询（第二次查询）
    t0 = time.perf_counter()
    _ = conn2.execute("SELECT * FROM symbols WHERE qualified_name = ? LIMIT 1", ("module_0.func_0",)).fetchone()
    hot_q1 = time.perf_counter() - t0
    t0 = time.perf_counter()
    _ = conn2.execute("SELECT COUNT(*) FROM calls WHERE callee_name = ?", ("ext_func_0",)).fetchone()[0]
    hot_q2 = time.perf_counter() - t0
    conn2.close()

    # 停止监控
    monitor.stop()

    # 统计文件大小
    db_size = os.path.getsize(db_path) / 1024 / 1024
    wal_path = db_path + "-wal"
    wal_size = os.path.getsize(wal_path) / 1024 / 1024 if os.path.exists(wal_path) else 0

    # storage_build_time = schema + insert + index + checkpoint
    storage_build_time = t_schema + t_insert + t_index + t_checkpoint
    # end_to_end_time = storage_build_time + verify + query
    end_to_end_time = storage_build_time + t_verify + cold_q1 + cold_q2 + cold_q3

    result.update({
        "timing": {
            "schema_s": round(t_schema, 4),
            "insert_s": round(t_insert, 4),
            "index_s": round(t_index, 4),
            "checkpoint_s": round(t_checkpoint, 4),
            "verify_s": round(t_verify, 4),
            "storage_build_s": round(storage_build_time, 4),
            "end_to_end_s": round(end_to_end_time, 4),
        },
        "query_latency": {
            "cold_qualified_name_ms": round(cold_q2 * 1000, 2),
            "cold_callee_name_ms": round(cold_q3 * 1000, 2),
            "hot_qualified_name_ms": round(hot_q1 * 1000, 2),
            "hot_callee_name_ms": round(hot_q2 * 1000, 2),
        },
        "data": {
            "symbols": sym_count,
            "calls": call_count,
            "files": n_files,
            "commits": n_commits,
        },
        "storage": {
            "db_mb": round(db_size, 1),
            "wal_mb": round(wal_size, 1),
            "peak_wal_mb": round(monitor.peak_wal_mb, 1),
        },
        "memory": {
            "peak_rss_mb": round(monitor.peak_rss_mb, 1),
        },
        "index_timings": index_timings if per_index_timing else None,
    })

    return result


# ============================================
# 主函数：多次运行取中位数
# ============================================

def median(values: list) -> float:
    """计算中位数"""
    s = sorted(values)
    n = len(s)
    if n == 0:
        return 0
    if n % 2 == 1:
        return s[n // 2]
    return (s[n // 2 - 1] + s[n // 2]) / 2


def main():
    parser = argparse.ArgumentParser(description="修正后的压测基准体系 v2")
    parser.add_argument("--symbols", type=int, default=1000000, help="目标符号数（默认 1M）")
    parser.add_argument("--runs", type=int, default=3, help="每组运行次数（默认 3，取中位数）")
    parser.add_argument("--mode", choices=["deferred", "eager", "both"], default="both",
                        help="索引模式：deferred / eager / both（默认 both）")
    parser.add_argument("--commit-every", type=int, default=10, help="每 N 批 commit 一次")
    parser.add_argument("--cache-size", type=int, default=65536, help="cache_size KB（默认 65536=64MB）")
    parser.add_argument("--mmap-size", type=int, default=268435456, help="mmap_size 字节（默认 256MB）")
    parser.add_argument("--page-size", type=int, default=4096, help="page_size 字节（默认 4096）")
    parser.add_argument("--temp-store", choices=["MEMORY", "FILE"], default="MEMORY", help="temp_store")
    parser.add_argument("--per-index-timing", action="store_true", help="逐索引计时（仅 deferred）")
    parser.add_argument("--db-dir", type=str, default="", help="数据库目录（默认临时目录）")
    args = parser.parse_args()

    env = collect_env_info()
    print(f"\n{'='*70}")
    print(f"压测基准体系 v2")
    print(f"{'='*70}")
    print(f"环境信息：")
    print(f"  SQLite: {env['sqlite_version']}, Python: {env['python_version']}")
    print(f"  OS: {env['os']}")
    print(f"  CPU: {env['cpu']} ({env['cpu_cores']} cores)")
    if 'ram_total_gb' in env:
        print(f"  RAM: {env['ram_total_gb']}GB total, {env['ram_available_mb']}MB available")
        print(f"  Disk: {env['disk_total_gb']}GB total, {env['disk_free_gb']}GB free")
    print(f"\n参数：")
    print(f"  目标符号数：{args.symbols:,}")
    print(f"  运行次数：{args.runs}（取中位数）")
    print(f"  模式：{args.mode}")
    print(f"  cache_size: {args.cache_size // 1024}MB, mmap: {args.mmap_size // 1024 // 1024}MB")
    print(f"  page_size: {args.page_size}, temp_store: {args.temp_store}")
    print(f"  commit_every: {args.commit_every}")
    print()

    db_dir = args.db_dir or os.path.dirname(os.path.abspath(__file__))
    modes = ["deferred", "eager"] if args.mode == "both" else [args.mode]
    all_results = {}

    for mode in modes:
        print(f"\n{'─'*70}")
        print(f"模式：{mode}")
        print(f"{'─'*70}")
        runs = []
        for run_idx in range(args.runs):
            db_path = os.path.join(db_dir, f"_bench_v2_{mode}_run{run_idx}.db")
            print(f"\n  Run {run_idx + 1}/{args.runs}...")
            # 只在最后一次运行做逐索引计时
            per_index = args.per_index_timing and (run_idx == args.runs - 1) and (mode == "deferred")
            result = run_single(
                db_path, args.symbols, mode=mode,
                commit_every=args.commit_every,
                cache_size_kb=args.cache_size,
                mmap_size=args.mmap_size,
                page_size=args.page_size,
                temp_store=args.temp_store,
                per_index_timing=per_index,
            )
            t = result["timing"]
            print(f"    schema={t['schema_s']:.2f}s, insert={t['insert_s']:.2f}s, "
                  f"index={t['index_s']:.2f}s, checkpoint={t['checkpoint_s']:.4f}s")
            print(f"    storage_build={t['storage_build_s']:.2f}s, end_to_end={t['end_to_end_s']:.2f}s")
            print(f"    db={result['storage']['db_mb']:.1f}MB, peak_wal={result['storage']['peak_wal_mb']:.1f}MB, "
                  f"peak_rss={result['memory']['peak_rss_mb']:.1f}MB")
            runs.append(result)
            # 清理（保留最后一个用于查询）
            if run_idx < args.runs - 1:
                if os.path.exists(db_path):
                    os.remove(db_path)
                for suffix in ("-wal", "-shm"):
                    p = db_path + suffix
                    if os.path.exists(p):
                        os.remove(p)

        # 计算中位数
        medians = {
            "schema_s": median([r["timing"]["schema_s"] for r in runs]),
            "insert_s": median([r["timing"]["insert_s"] for r in runs]),
            "index_s": median([r["timing"]["index_s"] for r in runs]),
            "checkpoint_s": median([r["timing"]["checkpoint_s"] for r in runs]),
            "storage_build_s": median([r["timing"]["storage_build_s"] for r in runs]),
            "end_to_end_s": median([r["timing"]["end_to_end_s"] for r in runs]),
            "peak_rss_mb": median([r["memory"]["peak_rss_mb"] for r in runs]),
            "peak_wal_mb": median([r["storage"]["peak_wal_mb"] for r in runs]),
            "db_mb": median([r["storage"]["db_mb"] for r in runs]),
        }
        # 查询中位数（最后一次运行的）
        last = runs[-1]
        medians["query_latency"] = last["query_latency"]
        medians["index_timings"] = last.get("index_timings")
        all_results[mode] = {
            "runs": runs,
            "median": medians,
        }

    # 打印汇总
    print(f"\n\n{'='*70}")
    print(f"汇总（中位数，{args.runs} 次运行）")
    print(f"{'='*70}")
    print(f"{'指标':<25} {'deferred':>15} {'eager':>15} {'比值(d/e)':>10}")
    print(f"{'─'*70}")
    for key in ["schema_s", "insert_s", "index_s", "checkpoint_s", "storage_build_s", "end_to_end_s"]:
        d = all_results.get("deferred", {}).get("median", {}).get(key, "-")
        e = all_results.get("eager", {}).get("median", {}).get(key, "-")
        if isinstance(d, float) and isinstance(e, float) and e > 0:
            ratio = f"{d/e:.2f}x"
        else:
            ratio = "-"
        d_str = f"{d:.2f}s" if isinstance(d, float) else str(d)
        e_str = f"{e:.2f}s" if isinstance(e, float) else str(e)
        print(f"  {key:<23} {d_str:>15} {e_str:>15} {ratio:>10}")
    for key in ["peak_rss_mb", "peak_wal_mb", "db_mb"]:
        d = all_results.get("deferred", {}).get("median", {}).get(key, "-")
        e = all_results.get("eager", {}).get("median", {}).get(key, "-")
        d_str = f"{d:.1f}MB" if isinstance(d, float) else str(d)
        e_str = f"{e:.1f}MB" if isinstance(e, float) else str(e)
        print(f"  {key:<23} {d_str:>15} {e_str:>15}")

    # 查询延迟
    print(f"\n查询延迟（最后一次运行）：")
    print(f"  {'指标':<25} {'deferred':>15} {'eager':>15}")
    ql_d = all_results.get("deferred", {}).get("median", {}).get("query_latency", {})
    ql_e = all_results.get("eager", {}).get("median", {}).get("query_latency", {})
    for key in ["cold_qualified_name_ms", "cold_callee_name_ms", "hot_qualified_name_ms", "hot_callee_name_ms"]:
        d = ql_d.get(key, "-")
        e = ql_e.get(key, "-")
        d_str = f"{d:.2f}ms" if isinstance(d, float) else str(d)
        e_str = f"{e:.2f}ms" if isinstance(e, float) else str(e)
        print(f"  {key:<23} {d_str:>15} {e_str:>15}")

    # 逐索引耗时
    idx_timings = all_results.get("deferred", {}).get("median", {}).get("index_timings")
    if idx_timings:
        print(f"\n逐索引耗时（仅最后一次 deferred 运行）：")
        sorted_timings = sorted([t for t in idx_timings if t["time_s"] > 0], key=lambda x: -x["time_s"])
        total_idx_time = sum(t["time_s"] for t in sorted_timings)
        cum = 0
        print(f"  {'索引名':<45} {'耗时':>10} {'占比':>8} {'累计':>8}")
        print(f"  {'─'*75}")
        for t in sorted_timings[:20]:  # Top 20
            pct = t["time_s"] / total_idx_time * 100 if total_idx_time > 0 else 0
            cum += pct
            print(f"  {t['name']:<45} {t['time_s']:>8.2f}s {pct:>6.1f}% {cum:>6.1f}%")
        print(f"  {'─'*75}")
        print(f"  {'总计':<45} {total_idx_time:>8.2f}s {100.0:>6.1f}%")

    # 保存报告
    report = {
        "env": env,
        "params": {
            "symbols": args.symbols,
            "runs": args.runs,
            "mode": args.mode,
            "commit_every": args.commit_every,
            "cache_size_mb": args.cache_size // 1024,
            "mmap_size_mb": args.mmap_size // 1024 // 1024,
            "page_size": args.page_size,
            "temp_store": args.temp_store,
        },
        "results": all_results,
    }
    report_path = os.path.join(db_dir, f"_bench_v2_{args.symbols // 1000000}m_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(report, f, indent=2, ensure_ascii=False)
    print(f"\n报告已保存：{report_path}")


if __name__ == "__main__":
    main()
