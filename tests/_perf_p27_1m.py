"""P27 优化后 1M refresh 实测脚本。

验证 file_local_qname 优化是否消除了 call_resolve_write 的 O(M×K) 瓶颈。
对比：优化前 1M refresh 卡死 22+ 分钟，优化后预期大幅下降。

用法：
  python tests/_perf_p27_1m.py
"""
from __future__ import annotations
import os
import sys
import time
import shutil

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from callwarden.db.db import CodeGraphDB

GEN_ROOT = os.path.join(_PKG_ROOT, 'tests', '_gen', '1m')
DB_DIR = os.path.join(_PKG_ROOT, 'tests', '_gen_db', '1m_p27')
DB_PATH = os.path.join(DB_DIR, 'callwarden.db')


def main():
    print(f'P27 优化后 1M refresh 实测')
    print(f'  源码: {GEN_ROOT}')
    print(f'  DB:   {DB_PATH}')
    print()

    # 清理旧 DB
    if os.path.exists(DB_DIR):
        shutil.rmtree(DB_DIR)
    os.makedirs(DB_DIR, exist_ok=True)

    # 创建 DB + 注册工作区
    t0 = time.perf_counter()
    db = CodeGraphDB(db_path=DB_PATH, workspace_root=GEN_ROOT)
    ws_id = db.register_workspace('1m_p27', GEN_ROOT, 'P27 1M 测试')
    db.set_active_workspace(ws_id)
    print(f'[setup] DB 初始化: {time.perf_counter() - t0:.1f}s')

    # 全量 refresh
    print(f'[refresh] 开始全量构建...')
    t1 = time.perf_counter()
    db.build_full_graph()
    elapsed = time.perf_counter() - t1
    print(f'[refresh] 完成: {elapsed:.1f}s')
    print()

    # 统计
    stats = db.get_stats()
    print(f'[stats] symbols: {stats.get("total_symbols", 0):,}')
    print(f'[stats] calls:   {stats.get("total_calls", 0):,}')
    print(f'[stats] files:   {stats.get("total_files", 0):,}')

    db_size = os.path.getsize(DB_PATH) / (1024 * 1024)
    print(f'[stats] DB size: {db_size:.2f} MB')

    db.close()

    print()
    print('=' * 60)
    print(f'结论: P27 优化后 1M refresh = {elapsed:.1f}s')
    print(f'      优化前: 卡死 22+ 分钟（未完成）')
    print('=' * 60)


if __name__ == '__main__':
    main()
