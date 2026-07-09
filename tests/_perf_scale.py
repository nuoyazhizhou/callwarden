"""大规模符号性能测试脚本（千万级符号性能验证）

在 _gen_symbols.py 生成的模拟源码上测试 refresh + 核心查询性能，
验证 Call Warden 在 100K / 1M / 10M 符号级别下的可扩展性。

测量项（对应 roadmap_phase2_plan.md §千万级符号性能验证）：
1. refresh-all 耗时（随符号量增长的趋势）
2. 核心查询性能：search / get_callers / get_callees / topo / clone_detect
3. SQLite DB 文件大小
4. 进程内存占用（RSS）

用法：
  python tests/_perf_scale.py --root tests/_gen/100k --label 100k
  python tests/_perf_scale.py --root tests/_gen/1m --label 1m
  python tests/_perf_scale.py --root tests/_gen/10m --label 10m --skip-refresh
  python tests/_perf_scale.py --root tests/_gen/100k --label 100k --clone

注意：不推送任何代码到远端，不修改仓库代码。
DB 存储在 tests/_gen_db/<label>/callwarden.db。
"""
from __future__ import annotations
import argparse
import json
import os
import sys
import time

# 确保能导入 callwarden 包
_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from callwarden.db.db import CodeGraphDB

# DB 存储目录（与 _perf_test.py 一致，放 tests/ 下绕过沙箱限制）
GEN_DB_DIR = os.path.join(_PKG_ROOT, 'tests', '_gen_db')

# 报告输出路径
REPORT_PATH = os.path.join(_PKG_ROOT, 'tests', '_perf_scale_report.json')


def get_db_size_mb(db_path: str) -> float:
    """获取 DB 文件大小（MB）"""
    try:
        return os.path.getsize(db_path) / (1024 * 1024)
    except OSError:
        return 0.0


def get_rss_mb() -> float:
    """获取当前进程 RSS 内存（MB）"""
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / (1024 * 1024)
    except ImportError:
        # Windows 无 psutil 时用 resource（仅 Unix）或返回 0
        try:
            import resource
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024
        except ImportError:
            return 0.0


def timed(fn, *args, **kwargs):
    """执行函数并计时，返回 (result, elapsed_sec)"""
    start = time.perf_counter()
    result = fn(*args, **kwargs)
    elapsed = time.perf_counter() - start
    return result, elapsed


def run_test(root: str, label: str, skip_refresh: bool = False,
             skip_clone: bool = True) -> dict:
    """在指定规模的数据上跑 refresh + 查询性能测试。

    Args:
        root: 生成的源码根目录
        label: 规模标签（100k/1m/10m）
        skip_refresh: 跳过 refresh（用已有 DB）
        skip_clone: 跳过 clone detect（默认跳过，因为它耗时最长）
    """
    db_dir = os.path.join(GEN_DB_DIR, label)
    os.makedirs(db_dir, exist_ok=True)
    db_path = os.path.join(db_dir, 'callwarden.db')

    result = {
        'label': label,
        'root': root,
        'db_path': db_path,
        'steps': [],
    }

    print(f'\n{"=" * 60}')
    print(f'规模: {label} | 源码: {root}')
    print(f'DB: {db_path}')
    print(f'{"=" * 60}')

    # 1. 打开 DB + 注册 workspace
    print(f'[1/7] 打开 DB + 注册 workspace...')
    rss_before = get_rss_mb()
    db_size_before = get_db_size_mb(db_path)
    db = CodeGraphDB(db_path=db_path, workspace_root=root)

    ws_id, reg_elapsed = timed(db.register_workspace, label, root)
    db.set_active_workspace(ws_id)
    print(f'  workspace_id={ws_id} ({reg_elapsed:.2f}s)')
    print(f'  DB 初始: {db_size_before:.2f} MB | RSS: {rss_before:.1f} MB')
    result['workspace_id'] = ws_id
    result['rss_before_mb'] = rss_before
    result['db_size_before_mb'] = db_size_before

    # 2. refresh-all（核心性能指标）
    if not skip_refresh:
        print(f'[2/7] refresh-all（构建代码图谱）...')
        try:
            _, elapsed = timed(db.build_full_graph, force=False)
            db_size_after = get_db_size_mb(db_path)
            rss_after_refresh = get_rss_mb()
            print(f'  refresh 完成: {elapsed:.1f}s ({elapsed / 60:.1f}min)')
            print(f'  DB: {db_size_after:.2f} MB (增量 {db_size_after - db_size_before:+.2f} MB)')
            print(f'  RSS: {rss_after_refresh:.1f} MB')
            result['refresh'] = {
                'elapsed_sec': elapsed,
                'elapsed_min': elapsed / 60,
                'db_size_after_mb': db_size_after,
                'db_delta_mb': db_size_after - db_size_before,
                'rss_after_mb': rss_after_refresh,
            }
            # 捕获阶段耗时
            stage_timings = getattr(db, '_stage_timings', None)
            if stage_timings:
                result['refresh']['stage_timings'] = stage_timings
                print(f'  阶段耗时:')
                for k, v in stage_timings.items():
                    if isinstance(v, float):
                        print(f'    {k:25s}: {v:.2f}s')
                    else:
                        print(f'    {k:25s}: {v}')
        except Exception as e:
            print(f'  refresh 失败: {e}')
            result['refresh'] = {'elapsed_sec': 0, 'error': str(e)[:500]}
            db.close()
            return result
    else:
        print(f'[2/7] 跳过 refresh（使用已有 DB）')
        result['refresh'] = {'skipped': True}

    # 3. 统计信息
    print(f'[3/7] 获取统计信息...')
    try:
        stats, elapsed = timed(db.get_stats)
        symbols = stats.get('total_symbols', 0)
        files = stats.get('total_files', 0)
        calls = stats.get('total_calls', 0)
        print(f'  stats ({elapsed:.3f}s): 符号={symbols:,}, 文件={files:,}, 调用={calls:,}')
        result['stats'] = {
            'elapsed': elapsed, 'symbols': symbols,
            'files': files, 'calls': calls, 'raw': stats,
        }
    except Exception as e:
        print(f'  stats 失败: {e}')
        result['stats'] = {'error': str(e)[:300]}
        symbols = 0
        files = 0

    # 4. 核心查询性能测试
    print(f'[4/7] 核心查询性能测试...')
    queries = {}

    # 4a. search（FTS5 模糊搜索）— 测索引查询
    try:
        search_results, elapsed = timed(db.search_symbols, 'func_0', limit=50)
        print(f'  search "func_0": {elapsed:.3f}s, {len(search_results)} 结果')
        queries['search'] = {
            'term': 'func_0', 'elapsed': elapsed,
            'result_count': len(search_results),
        }
    except Exception as e:
        print(f'  search 失败: {e}')
        queries['search'] = {'error': str(e)[:300]}
        search_results = []

    # 注意：get_callers/get_callees API 接受短名（name 字段）而非 qualified_name。
    # 选择 func_1 作为查询目标：它既有调用者（func_0）也有被调用者（func_2），
    # 且跨所有 1000 个文件都存在，能真实反映大规模符号下的查询性能。
    target_name = 'func_1'
    print(f'  查询目标符号（短名）: {target_name}')

    # 4b. get_callers（谁调用某符号）— 测反向索引
    try:
        callers, elapsed = timed(db.get_callers, target_name)
        print(f'  get_callers "{target_name}": {elapsed:.3f}s, {len(callers)} 调用者')
        queries['get_callers'] = {
            'target': target_name, 'elapsed': elapsed,
            'result_count': len(callers),
        }
    except Exception as e:
        print(f'  get_callers 失败: {e}')
        queries['get_callers'] = {'error': str(e)[:300]}

    # 4c. get_callees（某符号调用谁）— 测正向索引
    try:
        callees, elapsed = timed(db.get_callees, target_name)
        print(f'  get_callees "{target_name}": {elapsed:.3f}s, {len(callees)} 被调用者')
        queries['get_callees'] = {
            'target': target_name, 'elapsed': elapsed,
            'result_count': len(callees),
        }
    except Exception as e:
        print(f'  get_callees 失败: {e}')
        queries['get_callees'] = {'error': str(e)[:300]}

    # 4d. get_topological_order（拓扑排序）— 测全图遍历
    try:
        topo, elapsed = timed(db.get_topological_order, limit=5000)
        print(f'  topo: {elapsed:.3f}s, {len(topo)} 节点')
        queries['topo'] = {'elapsed': elapsed, 'count': len(topo)}
    except Exception as e:
        print(f'  topo 失败: {e}')
        queries['topo'] = {'error': str(e)[:300]}

    result['queries'] = queries

    # 5. clone detect（可选，默认跳过）
    if skip_clone:
        print(f'[5/7] clone detect（跳过，使用 --clone 开启）')
        queries['clone_detect'] = {'skipped': True}
    else:
        print(f'[5/7] clone detect（克隆检测）...')
        try:
            clones, elapsed = timed(db.detect_clones,
                                      min_lines=5, similarity_threshold=0.8)
            total_pairs = clones.get('total_pairs', 0)
            scanned = clones.get('scanned_symbols', 0)
            print(f'  clone detect: {elapsed:.3f}s, {total_pairs} 对, 扫描 {scanned} 符号')
            queries['clone_detect'] = {
                'elapsed': elapsed, 'total_pairs': total_pairs,
                'scanned_symbols': scanned,
            }
        except Exception as e:
            print(f'  clone detect 失败: {e}')
            queries['clone_detect'] = {'error': str(e)[:300]}

    # 6. DB VACUUM 后大小（测实际存储占用）
    print(f'[6/7] DB 大小统计...')
    rss_final = get_rss_mb()
    db_size_final = get_db_size_mb(db_path)
    print(f'  DB: {db_size_final:.2f} MB | RSS: {rss_final:.1f} MB')
    result['final'] = {
        'db_size_mb': db_size_final, 'rss_mb': rss_final,
    }

    # 7. 关闭 DB
    print(f'[7/7] 关闭 DB')
    db.close()

    result['queries'] = queries
    return result


def main():
    parser = argparse.ArgumentParser(description='大规模符号性能测试')
    parser.add_argument('--root', required=True, help='生成的源码根目录')
    parser.add_argument('--label', required=True, help='规模标签（100k/1m/10m）')
    parser.add_argument('--skip-refresh', action='store_true', help='跳过 refresh')
    parser.add_argument('--clone', action='store_true', help='执行 clone detect')
    parser.add_argument('--clean-db', action='store_true', help='测试前删除旧 DB')
    args = parser.parse_args()

    # 可选：删除旧 DB
    if args.clean_db:
        db_dir = os.path.join(GEN_DB_DIR, args.label)
        import shutil
        shutil.rmtree(db_dir, ignore_errors=True)

    result = run_test(args.root, args.label,
                      skip_refresh=args.skip_refresh,
                      skip_clone=not args.clone)

    # 追加到报告文件（支持多次运行累加）
    existing = []
    if os.path.exists(REPORT_PATH):
        try:
            with open(REPORT_PATH, 'r', encoding='utf-8') as f:
                existing = json.load(f)
        except (json.JSONDecodeError, ValueError):
            existing = []

    # 替换同 label 的旧结果
    existing = [r for r in existing if r.get('label') != args.label]
    existing.append(result)

    with open(REPORT_PATH, 'w', encoding='utf-8') as f:
        json.dump(existing, f, ensure_ascii=False, indent=2, default=str)

    # 打印汇总
    print(f'\n{"=" * 60}')
    print(f'性能测试汇总（{args.label}）')
    print(f'{"=" * 60}')
    refresh = result.get('refresh', {})
    stats = result.get('stats', {})
    queries = result.get('queries', {})
    print(f'{"指标":<20} {"值":<15}')
    print('-' * 40)
    print(f'{"refresh(s)":<20} {refresh.get("elapsed_sec", 0):<15.1f}')
    print(f'{"符号数":<20} {stats.get("symbols", 0):<15,}')
    print(f'{"文件数":<20} {stats.get("files", 0):<15,}')
    print(f'{"调用数":<20} {stats.get("calls", 0):<15,}')
    print(f'{"DB(MB)":<20} {result.get("final", {}).get("db_size_mb", 0):<15.2f}')
    print(f'{"RSS(MB)":<20} {result.get("final", {}).get("rss_mb", 0):<15.1f}')
    for qname in ('search', 'get_callers', 'get_callees', 'topo', 'clone_detect'):
        q = queries.get(qname, {})
        elapsed = q.get('elapsed', 0) if isinstance(q, dict) else 0
        if q.get('skipped'):
            print(f'{qname+"(s)":<20} {"skipped":<15}')
        else:
            print(f'{qname+"(s)":<20} {elapsed:<15.3f}')
    print(f'\n报告已保存: {REPORT_PATH}')


if __name__ == '__main__':
    main()
