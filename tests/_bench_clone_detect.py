#!/usr/bin/env python3
"""P1 性能验证：直接跑 android clone detect，测量当前 LSH 实现的性能。

用法：python -u tests/_bench_clone_detect.py
"""
import os
import sys
import time

# 添加项目根目录到 path
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)


def find_android_db():
    """查找 android 仓库的 callwarden 数据库"""
    perf_db = os.path.join(_project_root, "tests", "_perf_db", "android", "callwarden.db")
    if os.path.exists(perf_db):
        return perf_db
    return None


def main():
    # 延迟导入，避免 import 期间无法看到错误
    print("P1 Clone Detect 性能验证", flush=True)
    print(f"Python: {sys.version.split()[0]}", flush=True)

    from callwarden.db.db import CodeGraphDB

    db_path = find_android_db()
    if not db_path:
        print("未找到 android 数据库", flush=True)
        return

    db_size_mb = os.path.getsize(db_path) / (1024 * 1024)
    print(f"数据库: {db_path} ({db_size_mb:.1f} MB)", flush=True)

    android_root = r"C:\git_work\callwarden\testcode\android"

    # 打开 DB（CodeGraphDB 不支持 readonly 参数，直接打开）
    print("打开 DB...", flush=True)
    t0 = time.time()
    db = CodeGraphDB(db_path=db_path, workspace_root=android_root)
    print(f"  打开耗时: {time.time() - t0:.2f}s", flush=True)

    # 先 wal_checkpoint，确保读到最新数据（immutable 只读连接需此操作）
    try:
        db.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception as e:
        print(f"  wal_checkpoint: {e}", flush=True)

    # 统计符号数
    cur = db.conn.execute(
        "SELECT COUNT(*) FROM symbols WHERE kind IN ('fn','function','method','test_fn')"
    )
    symbol_count = cur.fetchone()[0]
    print(f"待扫描符号数: {symbol_count}", flush=True)

    # 清空旧 clone_pairs，避免 UPSERT 时旧数据拖慢索引查找
    cur = db.conn.execute("SELECT COUNT(*) FROM clone_pairs")
    old_pairs = cur.fetchone()[0]
    print(f"已有 clone_pairs: {old_pairs}", flush=True)
    if old_pairs > 0:
        db.conn.execute("DELETE FROM clone_pairs")
        db.conn.commit()
        print("已清空旧 clone_pairs", flush=True)

    print("开始 clone detect...", flush=True)
    t1 = time.time()
    result = db.detect_clones(min_lines=5, similarity_threshold=0.8)
    elapsed = time.time() - t1

    print(f"\n=== P1 Clone Detect 性能结果 ===", flush=True)
    print(f"耗时: {elapsed:.3f}s", flush=True)
    print(f"总对数: {result['total_pairs']}", flush=True)
    print(f"  Type-1: {result['type1_pairs']}", flush=True)
    print(f"  Type-2: {result['type2_pairs']}", flush=True)
    print(f"  Type-3: {result['type3_pairs']}", flush=True)
    print(f"扫描符号: {result['scanned_symbols']}", flush=True)
    print(f"跳过符号: {result['skipped_symbols']}", flush=True)

    # 对比优化前
    old_elapsed = 42.263
    if elapsed < old_elapsed:
        speedup = old_elapsed / elapsed
        print(f"\n✓ 优化前 {old_elapsed}s → 优化后 {elapsed:.3f}s ({speedup:.1f}x 加速)", flush=True)
    else:
        print(f"\n⚠ 优化后 {elapsed:.3f}s 比优化前 {old_elapsed}s 更慢，需检查", flush=True)

    db.close()


if __name__ == "__main__":
    main()
