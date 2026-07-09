"""生成大规模符号测试源码（100K / 1M / 10M 符号级别）

为千万级符号性能验证（roadmap_phase2_plan.md §千万级符号性能验证）生成
模拟 Python 源码，用于测试 refresh + 查询性能随符号量增长的趋势。

设计：
- 每文件 100 个函数（func_0..func_99）= 100 个符号
- func_0 是入口，调用 func_1/func_50/跨模块函数
- func_i 调用 func_(i+1) 形成调用链（测 call-chain 性能）
- 每 10 个函数有一个跨模块调用（测跨文件解析）
- 文件组织：mod_NNNN/unit_MMMM.py（模块化结构）

规模档位：
  100k = 1,000 文件（10 模块 × 100 文件）= 10 万符号
  1m   = 10,000 文件（100 模块 × 100 文件）= 100 万符号
  10m  = 100,000 文件（200 模块 × 500 文件）= 1000 万符号

用法：
  python tests/_gen_symbols.py --target 100k --out tests/_gen/100k
  python tests/_gen_symbols.py --target 1m --out tests/_gen/1m
  python tests/_gen_symbols.py --target 10m --out tests/_gen/10m
"""
from __future__ import annotations
import argparse
import os
import sys
import time

# 每文件符号数（函数数）
SYMBOLS_PER_FILE = 100

# 规模档位：目标符号数 → (模块数, 每模块文件数)
TARGETS = {
    "100k": (100_000, 10, 100),    # 10 模块 × 100 文件 = 1000 文件
    "1m":   (1_000_000, 100, 100),  # 100 模块 × 100 文件 = 10000 文件
    "10m":  (10_000_000, 200, 500), # 200 模块 × 500 文件 = 100000 文件
}


def gen_file_lines(module_name: str, file_idx: int, cross_modules: list) -> list:
    """生成单个 Python 文件的内容（行列表）。

    每文件 100 个函数，含：
    - 同文件调用链 func_i → func_(i+1)
    - func_0 调用 func_1/func_50（入口点，测 call-chain）
    - 每 10 个函数跨模块调用（测跨文件解析）
    """
    lines = [
        f'"""{module_name}.unit_{file_idx:04d} - auto-generated for perf testing"""',
        '',
    ]
    # 跨模块 import（每文件 import 2 个其他模块的 func_0）
    cross_imports = cross_modules[:2] if cross_modules else []
    for ci, cm in enumerate(cross_imports):
        lines.append(f'from {cm}.unit_{file_idx % 100:04d} import func_0 as ext_fn_{ci}')
    if cross_imports:
        lines.append('')
    lines.append('')

    for i in range(SYMBOLS_PER_FILE):
        lines.append(f'def func_{i}():')
        if i == 0:
            # 入口：调用同文件 func_1 + func_50 + 第一个跨模块函数
            lines.append(f'    return func_1() + func_50()' +
                         (f' + ext_fn_0()' if cross_imports else ''))
        elif i == SYMBOLS_PER_FILE - 1:
            # 末尾函数：叶子节点，返回常量
            lines.append(f'    return {i}')
        elif i % 10 == 5 and cross_imports:
            # 每 10 个函数一个跨模块调用（测跨文件解析）
            ci = (i // 10) % len(cross_imports)
            lines.append(f'    return ext_fn_{ci}() + func_{i + 1}()')
        else:
            # 同文件调用链
            lines.append(f'    return func_{i + 1}() + {i}')
        lines.append('')

    return lines


def gen_project(root: str, target_symbols: int, num_modules: int,
                files_per_module: int) -> dict:
    """生成整个项目的源码文件。

    Returns:
        统计信息 dict（文件数、预估符号数、耗时）
    """
    t0 = time.perf_counter()
    os.makedirs(root, exist_ok=True)

    module_names = [f'mod_{i:04d}' for i in range(num_modules)]
    total_files = 0

    for mi, mname in enumerate(module_names):
        mdir = os.path.join(root, mname)
        os.makedirs(mdir, exist_ok=True)
        # __init__.py 让 import 能工作
        init_path = os.path.join(mdir, '__init__.py')
        if not os.path.exists(init_path):
            with open(init_path, 'w', encoding='utf-8') as f:
                f.write('')

        # 跨模块：选 5 个其他模块
        cross = [mn for j, mn in enumerate(module_names) if j != mi][:5]

        for fi in range(files_per_module):
            fname = f'unit_{fi:04d}.py'
            fpath = os.path.join(mdir, fname)
            lines = gen_file_lines(mname, fi, cross)
            # 一次性写入（比多次 write 快）
            with open(fpath, 'w', encoding='utf-8') as f:
                f.write('\n'.join(lines))
            total_files += 1

        if (mi + 1) % 20 == 0 or mi == 0:
            elapsed = time.perf_counter() - t0
            pct = (mi + 1) / num_modules * 100
            sys.stdout.write(
                f'\r  生成中: module {mi+1}/{num_modules} '
                f'({pct:.0f}%) files={total_files} {elapsed:.1f}s'
            )
            sys.stdout.flush()

    elapsed = time.perf_counter() - t0
    print()  # 换行

    estimated_symbols = total_files * SYMBOLS_PER_FILE
    return {
        'total_files': total_files,
        'estimated_symbols': estimated_symbols,
        'modules': num_modules,
        'elapsed_sec': round(elapsed, 2),
    }


def main():
    parser = argparse.ArgumentParser(description='生成大规模符号测试源码')
    parser.add_argument(
        '--target', choices=list(TARGETS.keys()), required=True,
        help='目标符号规模档位',
    )
    parser.add_argument(
        '--out', required=True,
        help='输出目录路径',
    )
    parser.add_argument(
        '--clean', action='store_true',
        help='生成前清空输出目录',
    )
    args = parser.parse_args()

    target_symbols, num_modules, files_per_module = TARGETS[args.target]
    total_expected = num_modules * files_per_module

    print(f'目标规模: {args.target} ({target_symbols:,} 符号)')
    print(f'结构: {num_modules} 模块 × {files_per_module} 文件 = {total_expected:,} 文件')
    print(f'输出目录: {args.out}')
    print(f'每文件符号数: {SYMBOLS_PER_FILE}')
    print()

    if args.clean and os.path.exists(args.out):
        import shutil
        print(f'清空输出目录...')
        shutil.rmtree(args.out, ignore_errors=True)

    stats = gen_project(args.out, target_symbols, num_modules, files_per_module)

    print()
    print('=' * 50)
    print(f'生成完成:')
    print(f'  文件数: {stats["total_files"]:,}')
    print(f'  预估符号数: {stats["estimated_symbols"]:,}')
    print(f'  模块数: {stats["modules"]:,}')
    print(f'  耗时: {stats["elapsed_sec"]:.1f}s')
    print(f'  目录: {args.out}')
    print('=' * 50)
    print()
    print(f'下一步: python tests/_perf_scale.py --root {args.out} --label {args.target}')


if __name__ == '__main__':
    main()
