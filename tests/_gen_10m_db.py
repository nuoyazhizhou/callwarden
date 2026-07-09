"""直接生成 10M DB（symbols + calls，绕过 refresh）

为千万级符号性能验证准备 10M 规模的 DB。直接 INSERT，不走 refresh，
避免 refresh 的 O(M×K) 调用关系解析瓶颈（1M 卡 22+ 分钟，10M 完全不可行）。

设计：
- 200 模块 × 500 文件 = 100,000 文件实例
- 每文件 100 个函数 = 10,000,000 符号
- 每文件 111 个 calls = 11,100,000 调用关系
- 用 CodeGraphDB 初始化空 DB（自动建 schema）
- 用 executemany 批量 INSERT

用法：
  python tests/_gen_10m_db.py
"""
from __future__ import annotations
import os
import sys
import time
import sqlite3

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from callwarden.db.db import CodeGraphDB

GEN_DB_DIR = os.path.join(_PKG_ROOT, 'tests', '_gen_db')
LABEL = '10m'
NUM_MODULES = 200
FILES_PER_MODULE = 500
SYMBOLS_PER_FILE = 100


def gen_symbols_batch(module_start: int, module_end: int) -> tuple:
    """生成指定模块范围内的 symbols + file_instances 数据。

    Returns:
        (file_instances_rows, symbols_rows, file_id_to_module_path)
    """
    file_instances = []
    symbols = []
    file_id_to_path = {}

    # file_instance_id 从 1 开始（自增）
    fid = module_start * FILES_PER_MODULE + 1
    for mi in range(module_start, module_end):
        mname = f'mod_{mi:04d}'
        for fi in range(FILES_PER_MODULE):
            rel_path = f'{mname}/unit_{fi:04d}.py'
            module_path = f'{mname}.unit_{fi:04d}'
            file_instances.append((1, rel_path, 'python', 'current'))  # workspace_id=1
            file_id_to_path[fid] = module_path
            # 100 个函数符号
            for i in range(SYMBOLS_PER_FILE):
                qname = f'{module_path}.func_{i}'
                symbols.append((fid, f'func_{i}', 'fn', qname, module_path,
                                i * 4 + 1, i * 4 + 3, f'hash_{fid}_{i}', 0, 0))
            fid += 1

    return file_instances, symbols, file_id_to_path


def gen_calls_for_file(file_id: int, module_path: str,
                       file_local_name_to_id: dict) -> list:
    """为单文件生成 calls 数据。

    与 _gen_symbols.py 的 gen_file_lines 一致：
    - func_0 → func_1 + func_50 + ext_fn_0（跨模块）
    - func_i → func_(i+1)（i < 99）
    - 每 10 个函数一个跨模块调用（i % 10 == 5）

    Args:
        file_id: 文件实例 ID
        module_path: 模块路径（如 "mod_0000.unit_0000"）
        file_local_name_to_id: {func_name: symbol_id} 本文件符号映射
    """
    calls = []
    for i in range(100):
        caller_name = f'func_{i}'
        if caller_name not in file_local_name_to_id:
            continue
        caller_id = file_local_name_to_id[caller_name]

        if i == 0:
            # func_0 → func_1 + func_50 + ext_fn_0
            for callee_name, line in [('func_1', 8), ('func_50', 8), ('ext_fn_0', 8)]:
                callee = file_local_name_to_id.get(callee_name)
                if callee:
                    cqname = f'{module_path}.{callee_name}'
                    calls.append((caller_id, caller_name, module_path,
                                  callee_name, '', cqname, '', callee, line, 0))
                else:
                    # ext_fn_0 跨模块未解析
                    calls.append((caller_id, caller_name, module_path,
                                  callee_name, '', '', '', 0, line, 1))
        elif i == 99:
            pass  # 叶子
        elif i % 10 == 5:
            # 跨模块 + 同文件
            callee_name = f'func_{i + 1}'
            callee = file_local_name_to_id.get(callee_name)
            if callee:
                cqname = f'{module_path}.{callee_name}'
                calls.append((caller_id, caller_name, module_path,
                              callee_name, '', cqname, '', callee, 23, 0))
            ext_idx = (i // 10) % 2
            calls.append((caller_id, caller_name, module_path,
                          f'ext_fn_{ext_idx}', '', '', '', 0, 23, 1))
        else:
            callee_name = f'func_{i + 1}'
            callee = file_local_name_to_id.get(callee_name)
            if callee:
                cqname = f'{module_path}.{callee_name}'
                calls.append((caller_id, caller_name, module_path,
                              callee_name, '', cqname, '', callee, 8 + i * 3, 0))
    return calls


def main():
    db_dir = os.path.join(GEN_DB_DIR, LABEL)
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, 'callwarden.db')

    # 如果存在旧 DB，先删除
    import shutil
    if os.path.exists(db_path):
        os.remove(db_path)

    total_files = NUM_MODULES * FILES_PER_MODULE
    total_symbols = total_files * SYMBOLS_PER_FILE
    estimated_calls = total_files * 111  # 平均每文件 111 calls

    print(f'目标: {LABEL} ({total_symbols:,} 符号, {total_files:,} 文件, ~{estimated_calls:,} calls)')
    print(f'DB: {db_path}')
    print()

    # 1. 用 CodeGraphDB 初始化 schema
    print(f'[1/5] 初始化 DB schema...')
    t0 = time.perf_counter()
    db = CodeGraphDB(db_path=db_path, workspace_root=f'tests/_gen/{LABEL}')
    ws_id = db.register_workspace(LABEL, f'tests/_gen/{LABEL}')
    db.set_active_workspace(ws_id)
    db.close()
    print(f'  schema 初始化完成 ({time.perf_counter() - t0:.1f}s)')

    # 2. 直接批量 INSERT file_instances + symbols
    print(f'[2/5] 批量 INSERT file_instances + symbols...')
    t0 = time.perf_counter()
    conn = sqlite3.connect(db_path)
    conn.execute('BEGIN')

    # 关闭触发器（FTS5 同步触发器，避免每条 INSERT 都维护索引）
    try:
        conn.execute('DROP TRIGGER IF EXISTS symbols_ai_fts')
        conn.execute('DROP TRIGGER IF EXISTS symbols_ad_fts')
        conn.execute('DROP TRIGGER IF EXISTS symbols_au_fts')
    except Exception:
        pass

    # workspace_id = 1
    BATCH = 10000
    total_sym_inserted = 0
    total_files_inserted = 0

    # file_instances 实际列（db_base.py schema）：
    # id, workspace_id, rel_path, abs_path, current_content_hash, mtime,
    # total_lines, last_parsed, status, module_path
    #
    # symbols 实际列：
    # id, file_instance_id, symbol_hash, name, kind, visibility,
    # start_line, end_line, start_col, end_col, signature,
    # has_comment, comment_status, module_path, qualified_name, depth
    file_id = 1
    now = time.time()
    for mi in range(NUM_MODULES):
        mname = f'mod_{mi:04d}'
        file_rows = []
        sym_rows = []
        for fi in range(FILES_PER_MODULE):
            rel_path = f'{mname}/unit_{fi:04d}.py'
            module_path = f'{mname}.unit_{fi:04d}'
            abs_path = f'tests/_gen/10m/{rel_path}'
            # (id, workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
            file_rows.append((file_id, 1, rel_path, abs_path, '', now, 200, now, 'current', module_path))
            for i in range(SYMBOLS_PER_FILE):
                qname = f'{module_path}.func_{i}'
                # (id, file_instance_id, symbol_hash, name, kind, visibility,
                #  start_line, end_line, start_col, end_col, signature,
                #  has_comment, comment_status, module_path, qualified_name, depth)
                sym_rows.append((None, file_id, f'hash_{file_id}_{i}', f'func_{i}', 'fn', 'private',
                                 i * 4 + 1, i * 4 + 3, 0, 0, '',
                                 0, 'pending', module_path, qname, 0))
            file_id += 1

        conn.executemany(
            "INSERT OR IGNORE INTO file_instances (id, workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path) VALUES (?,?,?,?,?,?,?,?,?,?)",
            file_rows,
        )
        conn.executemany(
            """INSERT INTO symbols (id, file_instance_id, symbol_hash, name, kind, visibility,
               start_line, end_line, start_col, end_col, signature,
               has_comment, comment_status, module_path, qualified_name, depth)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            sym_rows,
        )
        total_sym_inserted += len(sym_rows)
        total_files_inserted += len(file_rows)

        if (mi + 1) % 20 == 0 or mi == 0:
            elapsed = time.perf_counter() - t0
            pct = (mi + 1) / NUM_MODULES * 100
            print(f'  module {mi+1}/{NUM_MODULES} ({pct:.0f}%) files={total_files_inserted:,} sym={total_sym_inserted:,} {elapsed:.0f}s')

    conn.execute('COMMIT')
    print(f'  完成: {total_files_inserted:,} 文件, {total_sym_inserted:,} 符号 ({time.perf_counter() - t0:.1f}s)')

    # 3. 构建 file_local name -> id 映射（用于生成 calls）
    print(f'[3/5] 构建 file-local 符号映射...')
    t0 = time.perf_counter()
    conn.row_factory = sqlite3.Row
    cur = conn.execute(
        "SELECT id, name, file_instance_id FROM symbols ORDER BY file_instance_id, id"
    )
    file_to_syms = {}
    for row in cur:
        fid = row['file_instance_id']
        if fid not in file_to_syms:
            file_to_syms[fid] = {}
        file_to_syms[fid][row['name']] = row['id']
    print(f'  加载 {len(file_to_syms):,} 文件的符号映射 ({time.perf_counter() - t0:.1f}s)')

    # 4. 生成 + INSERT calls
    print(f'[4/5] 生成 + 批量 INSERT calls...')
    t0 = time.perf_counter()
    conn.execute('BEGIN')
    sql = """INSERT INTO calls
             (caller_id, caller_name, caller_module, callee_name,
              callee_module, callee_qualified, callee_file, callee_id,
              call_line, is_cross_file)
             VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""

    batch = []
    total_calls = 0
    for fid, name_to_id in file_to_syms.items():
        # 推断 module_path：从 name_to_id 中任取一个 qname 解析
        # 这里用 fid 反查 rel_path
        cur2 = conn.execute("SELECT rel_path FROM file_instances WHERE id = ?", (fid,))
        rel_path = cur2.fetchone()[0]
        module_path = rel_path.replace('/', '.').replace('.py', '')

        calls = gen_calls_for_file(fid, module_path, name_to_id)
        batch.extend(calls)
        if len(batch) >= BATCH:
            conn.executemany(sql, batch)
            total_calls += len(batch)
            if (total_calls // BATCH) % 20 == 0:
                elapsed = time.perf_counter() - t0
                pct = total_calls / estimated_calls * 100
                print(f'  calls={total_calls:,}/{estimated_calls:,} ({pct:.0f}%) {elapsed:.0f}s')
            batch = []

    if batch:
        conn.executemany(sql, batch)
        total_calls += len(batch)

    conn.execute('COMMIT')
    print(f'  完成: {total_calls:,} calls ({time.perf_counter() - t0:.1f}s)')

    # 5. 创建索引 + VACUUM
    print(f'[5/5] 创建索引 + VACUUM...')
    t0 = time.perf_counter()
    conn.execute('CREATE INDEX IF NOT EXISTS idx_calls_caller ON calls(caller_id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_calls_callee ON calls(callee_name)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_symbols_name ON symbols(name)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_symbols_qname ON symbols(qualified_name)')
    conn.execute('VACUUM')
    db_size = os.path.getsize(db_path) / (1024 * 1024)
    print(f'  DB size: {db_size:.2f} MB ({time.perf_counter() - t0:.1f}s)')

    conn.close()

    print()
    print('=' * 60)
    print(f'10M DB 生成完成:')
    print(f'  files: {total_files_inserted:,}')
    print(f'  symbols: {total_sym_inserted:,}')
    print(f'  calls: {total_calls:,}')
    print(f'  DB size: {db_size:.2f} MB')
    print('=' * 60)
    print()
    print(f'下一步: python tests/_perf_scale.py --root tests/_gen/10m --label 10m --skip-refresh')


if __name__ == '__main__':
    main()
