"""直接 INSERT 大规模 calls 数据（绕过 refresh 的 O(M×K) 瓶颈）

为 1M / 10M 规模的 DB 直接写入 calls 表数据，用于查询性能测试。

设计：
- 1M DB 已有 100 万 symbols（refresh 写完 symbols 后卡在 call_resolve_write）
- 此脚本读取 symbols 表，按生成模式构造 calls 数据
- 每文件 100 个函数，调用关系与 _gen_symbols.py 一致：
  - func_0 → func_1, func_50, ext_fn_0（跨模块）
  - func_i → func_(i+1)（同文件调用链）
  - 每 10 个函数一个跨模块调用

用法：
  python tests/_gen_calls_direct.py --label 1m
  python tests/_gen_calls_direct.py --label 10m
"""
from __future__ import annotations
import argparse
import os
import sys
import time
import sqlite3

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

GEN_DB_DIR = os.path.join(_PKG_ROOT, 'tests', '_gen_db')


def load_symbols_by_file(db_path: str) -> dict:
    """加载所有 symbols，按 file_instance_id 分组。

    Returns:
        {file_instance_id: [(symbol_id, qualified_name, name), ...]}
    """
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT id, name, qualified_name, file_instance_id FROM symbols ORDER BY file_instance_id, id"
    )
    by_file = {}
    for row in cur:
        fid = row['file_instance_id']
        if fid not in by_file:
            by_file[fid] = []
        by_file[fid].append((row['id'], row['qualified_name'], row['name']))
    conn.close()
    return by_file


def gen_calls_for_file(symbols: list) -> list:
    """为单文件的 symbols 生成 calls 数据。

    与 _gen_symbols.py 的 gen_file_lines 一致：
    - func_0 调用 func_1 + func_50 + ext_fn_0（跨模块）
    - func_i 调用 func_(i+1)（i < 99）
    - 每 10 个函数一个跨模块调用（i % 10 == 5）

    Args:
        symbols: [(symbol_id, qualified_name, name), ...] 按 id 排序

    Returns:
        [(caller_id, caller_name, caller_module, callee_name, callee_module,
          callee_qualified, callee_file, callee_id, call_line, is_cross_file), ...]
    """
    # 构建 name -> (id, qname) 映射
    name_to_sym = {}
    for sid, qname, name in symbols:
        name_to_sym[name] = (sid, qname)

    calls = []
    # 解析 module_path from qname（如 "mod_0000.unit_0000.func_5" → "mod_0000.unit_0000"）
    if not symbols:
        return calls
    _, first_qname, _ = symbols[0]
    parts = first_qname.rsplit('.', 1)
    module_path = parts[0] if len(parts) > 1 else ''
    # 提取 mod_NNNN（跨模块调用的目标模块前缀）
    mod_parts = module_path.split('.')
    mod_name = mod_parts[0] if mod_parts else ''

    for i in range(100):
        caller_name = f'func_{i}'
        if caller_name not in name_to_sym:
            continue
        caller_id, caller_qname = name_to_sym[caller_name]

        if i == 0:
            # func_0 → func_1 + func_50 + ext_fn_0
            for callee_name, line in [('func_1', 8), ('func_50', 8), ('ext_fn_0', 8)]:
                callee = name_to_sym.get(callee_name)
                if callee:
                    cid, cqname = callee
                    calls.append((caller_id, caller_name, module_path,
                                  callee_name, '', cqname, '', cid, line, 0))
                else:
                    # ext_fn_0 跨模块，callee_id=0（未解析）
                    calls.append((caller_id, caller_name, module_path,
                                  callee_name, '', '', '', 0, line, 1))
        elif i == 99:
            # func_99 是叶子
            pass
        elif i % 10 == 5:
            # 跨模块调用 ext_fn_X + 同文件 func_(i+1)
            callee_name = f'func_{i + 1}'
            callee = name_to_sym.get(callee_name)
            if callee:
                cid, cqname = callee
                calls.append((caller_id, caller_name, module_path,
                              callee_name, '', cqname, '', cid, 23, 0))
            # ext_fn 跨模块
            ext_idx = (i // 10) % 2
            calls.append((caller_id, caller_name, module_path,
                          f'ext_fn_{ext_idx}', '', '', '', 0, 23, 1))
        else:
            # 同文件调用链 func_i → func_(i+1)
            callee_name = f'func_{i + 1}'
            callee = name_to_sym.get(callee_name)
            if callee:
                cid, cqname = callee
                calls.append((caller_id, caller_name, module_path,
                              callee_name, '', cqname, '', cid, 8 + i * 3, 0))
    return calls


def insert_calls_direct(db_path: str, batch_size: int = 5000) -> dict:
    """直接 INSERT calls 数据到 DB（绕过 refresh 瓶颈）。

    Args:
        db_path: DB 文件路径
        batch_size: 批量 INSERT 大小

    Returns:
        统计信息 dict
    """
    t0 = time.perf_counter()
    print(f'加载 symbols...')
    by_file = load_symbols_by_file(db_path)
    total_files = len(by_file)
    total_symbols = sum(len(s) for s in by_file.values())
    print(f'  files={total_files}, symbols={total_symbols}')

    print(f'生成 calls 数据...')
    all_calls = []
    for fid, symbols in by_file.items():
        calls = gen_calls_for_file(symbols)
        all_calls.extend(calls)
    print(f'  生成 {len(all_calls):,} 条 calls')

    print(f'清空 calls + call_versions 表...')
    conn = sqlite3.connect(db_path)
    conn.execute('DELETE FROM calls')
    conn.execute('DELETE FROM call_versions')
    conn.execute('COMMIT')
    conn.execute('BEGIN')

    print(f'批量 INSERT calls（batch_size={batch_size}）...')
    t_insert_start = time.perf_counter()
    sql = """INSERT INTO calls
             (caller_id, caller_name, caller_module, callee_name,
              callee_module, callee_qualified, callee_file, callee_id,
              call_line, is_cross_file)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
    for i in range(0, len(all_calls), batch_size):
        batch = all_calls[i:i + batch_size]
        conn.executemany(sql, batch)
        if (i // batch_size) % 20 == 0:
            pct = (i + len(batch)) / len(all_calls) * 100
            elapsed = time.perf_counter() - t_insert_start
            print(f'  [{i + len(batch):,}/{len(all_calls):,}] {pct:.0f}% {elapsed:.1f}s')

    # 创建索引（如果不存在）
    print(f'创建索引...')
    try:
        conn.execute('CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id)')
    except Exception as e:
        print(f'  idx_calls_caller: {e}')
    try:
        conn.execute('CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_name)')
    except Exception as e:
        print(f'  idx_calls_callee: {e}')

    conn.execute('COMMIT')

    # VACUUM 减小文件大小
    print(f'VACUUM...')
    conn.execute('VACUUM')

    db_size = os.path.getsize(db_path) / (1024 * 1024)
    print(f'  DB size: {db_size:.2f} MB')

    conn.close()

    elapsed = time.perf_counter() - t0
    return {
        'files': total_files,
        'symbols': total_symbols,
        'calls_inserted': len(all_calls),
        'elapsed_sec': round(elapsed, 2),
        'db_size_mb': round(db_size, 2),
    }


def main():
    parser = argparse.ArgumentParser(description='直接 INSERT 大规模 calls 数据')
    parser.add_argument('--label', required=True, help='规模标签（1m/10m）')
    parser.add_argument('--batch-size', type=int, default=5000)
    args = parser.parse_args()

    db_path = os.path.join(GEN_DB_DIR, args.label, 'callwarden.db')
    if not os.path.exists(db_path):
        print(f'ERROR: DB 不存在: {db_path}')
        sys.exit(1)

    print(f'目标 DB: {db_path}')
    print(f'当前 DB 大小: {os.path.getsize(db_path) / (1024 * 1024):.2f} MB')
    print()

    stats = insert_calls_direct(db_path, args.batch_size)

    print()
    print('=' * 50)
    print('完成:')
    print(f'  files: {stats["files"]:,}')
    print(f'  symbols: {stats["symbols"]:,}')
    print(f'  calls_inserted: {stats["calls_inserted"]:,}')
    print(f'  DB size: {stats["db_size_mb"]:.2f} MB')
    print(f'  elapsed: {stats["elapsed_sec"]:.1f}s')
    print('=' * 50)
    print()
    print(f'下一步: python tests/_perf_scale.py --root tests/_gen/{args.label} --label {args.label} --skip-refresh')


if __name__ == '__main__':
    main()
