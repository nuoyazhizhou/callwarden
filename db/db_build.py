"""
db_build.py
===========

代码知识图谱构建 Mixin 类。

提供文件扫描、多语言解析、调用图构建、增量更新等功能。
"""

from __future__ import annotations

import os
import pickle
import re
import sys
import time
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set

from ..config import (
    norm_path, read_file_normalized, read_file_text,
    detect_language_from_path, get_supported_extensions, compute_content_hash,
    safe_walk,
)
from ..parsers import RustParser, ModuleResolver, CallResolver, create_parser
from ..cli.console import cprint, print_progress, clear_progress, Spinner, format_duration, print_build_summary
from ..i18n import t


# ============================================
# P15: ProcessPoolExecutor worker 函数（模块级，可 pickle）
# ============================================

# 每个 worker 进程的 parser 缓存（进程级，避免每文件创建）
_worker_parsers: Dict[str, Any] = {}


def _init_worker_parsers():
    """worker 进程初始化：惰性加载 parser（不预加载）。

    在 ProcessPoolExecutor 的 initializer 中调用，每个 worker 进程启动时
    执行一次。空初始化，parser 在 _parse_file_worker 中按需创建并缓存。

    P15 修正：不预加载所有 16 语言（每进程省 ~200MB），改为惰性加载。
    firmware 只有 C/C++，只需加载 2 个 parser，内存占用从 ~300MB 降到 ~80MB。
    """
    global _worker_parsers
    _worker_parsers = {}


# 每个 worker 进程预估内存上限（MB）
# P28 修正：原 150MB 只算 parser 加载（Python ~30MB + grammar ~30MB + 缓存 ~20MB），
# 完全未算 tree-sitter AST 解析峰值内存。实测 1MB .c 文件 parse 时 AST 可达 1-2GB，
# 即使过滤了 LVGL 资源，普通大文件（200KB .c）也产生 200-500MB AST 峰值。
# 真实预算 = parser 加载（~80MB）+ AST 峰值（大文件 500MB-1GB）→ 取 800MB 保守值
_WORKER_MEM_BUDGET_MB = 800
# 保留给宿主机/其他进程的内存下限（MB），避免把宿主机搞崩溃
# P28 修正：原 2GB 太激进，32GB 机器 OS+IDE+浏览器常态占用 10-15GB，留 4GB 缓冲更安全
_HOST_RESERVED_MEM_MB = 4096  # 4GB
# worker 数硬上限（即使资源充足也不超过，避免进程调度开销）
_MAX_WORKERS_CAP = 8
# worker 数硬下限
_MIN_WORKERS = 1

# 主进程持有 parse 结果的预估内存（每符号 KB）
# 含 symbol dict + call list + position info + module info
# 实测 1.5M 符号 ~ 10-14GB → 每符号约 7-9KB，取保守值 8KB
# 这是原算法完全缺失的维度：build_full_graph 把所有结果存到 file_results dict，
# 即使 worker=1 主进程也会爆，是 4 worker 模式崩溃的根因
_MAIN_PROCESS_PER_SYMBOL_KB = 8
# 每文件平均符号数（用于从 file_count 估算主进程内存）
_AVG_SYMBOLS_PER_FILE = 10

# 数据规模阈值：超过时主进程结果持有内存会爆炸，必须降低 worker 数
# 阈值推导（每符号 8KB × 每文件 10 符号 = 80KB/文件）：
#   10K 文件 → 主进程 ~800MB（可接受，最多 2 worker）
#   50K 文件 → 主进程 ~4GB（必须降到 1 worker）
#   200K 文件 → 主进程 ~16GB（必须分批构建，强制 1 worker 警告）
# 命名加 _SCALE_ 前缀避免与第三方库检测的 _LARGE_FILE_COUNT_THRESHOLD=5 撞名
_SCALE_LARGE_FILE_THRESHOLD = 10000
_SCALE_VERY_LARGE_FILE_THRESHOLD = 50000
_SCALE_HUGE_FILE_THRESHOLD = 200000


def _detect_optimal_workers(file_count: int = 0) -> int:
    """根据宿主机剩余资源动态计算最优 worker 数。

    P28 修复背景：原算法只考虑 worker 内存预算（150MB）和保留内存（2GB），
    导致 4 worker 模式下主进程内存从 9GB 涨到 14.2GB，把 32GB 宿主机搞崩。
    真实缺陷：
    1. worker 预算严重低估：未算 AST 峰值（大文件可达 1GB）
    2. 保留内存太小：32GB 机器留 2GB 不够 OS+IDE 占用
    3. 完全没算主进程结果持有内存：1.5M 符号需 10-14GB
    4. 没考虑数据规模因子：文件数越多主进程越爆

    P28 修复后综合考虑四个因素：
    1. CPU 核心数：worker 数不超过 CPU 核心数（留 1 核给主进程/系统）
    2. 剩余可用内存：每 worker 预估 800MB（含 AST 峰值），保留 4GB 给宿主机
    3. 数据规模因子（新增）：文件数多时主进程结果持有内存爆炸，必须降低 worker 数
       - >= 200K 文件：主进程结果 16GB+，必须分批构建（强制 1 worker）
       - >= 50K 文件：主进程结果 4GB+，最多 1 worker
       - >= 10K 文件：主进程结果 800MB+，最多 2 worker
    4. 硬上限 8：避免进程过多导致 IPC 开销和调度抖动

    内存检测优先用 psutil（跨平台），不可用时回退到平台原生 API
    （Windows: ctypes.windll.kernel32.GlobalMemoryStatusEx；
     Linux: /proc/meminfo）。

    Args:
        file_count: 本次待解析的文件数，用于估算主进程结果持有内存。
                    默认 0（不启用数据规模因子，向后兼容）。
                    调用方应传 len(to_parse) 启用规模感知。

    Returns:
        推荐 worker 数（1-8）
    """
    cpu_count = os.cpu_count() or 4

    # CPU 维度：留 1 核给主进程和系统
    cpu_based = max(_MIN_WORKERS, cpu_count - 1)

    # 数据规模因子（P28 新增）：文件数过多时主进程结果持有内存爆炸
    # 这是原算法完全缺失的维度，是 4 worker 模式崩溃的根因
    if file_count >= _SCALE_HUGE_FILE_THRESHOLD:
        # 200K+ 文件：主进程结果 16GB+，必须分批构建
        # 强制 1 worker（调用方应改用 build_directory 分批）
        scale_cap = 1
    elif file_count >= _SCALE_VERY_LARGE_FILE_THRESHOLD:
        # 50K+ 文件：主进程结果 4GB+，最多 1 worker
        scale_cap = 1
    elif file_count >= _SCALE_LARGE_FILE_THRESHOLD:
        # 10K+ 文件：主进程结果 800MB+，最多 2 worker
        scale_cap = 2
    else:
        scale_cap = _MAX_WORKERS_CAP

    # 内存维度：检测可用内存
    mem_based = _MAX_WORKERS_CAP  # 默认假设内存充足
    available_mb = _get_available_memory_mb()
    if available_mb is not None:
        # 减去保留量后，按每 worker 预算计算
        usable_mb = available_mb - _HOST_RESERVED_MEM_MB
        if usable_mb <= 0:
            mem_based = _MIN_WORKERS
        else:
            mem_based = max(_MIN_WORKERS, int(usable_mb / _WORKER_MEM_BUDGET_MB))

    # 取 CPU、内存、数据规模三者的较小值，再限制在硬上限内
    workers = min(cpu_based, mem_based, _MAX_WORKERS_CAP, scale_cap)
    return max(_MIN_WORKERS, workers)


def _get_available_memory_mb() -> Optional[float]:
    """获取系统可用物理内存（MB），跨平台。

    优先用 psutil（跨平台），不可用时回退到平台原生 API。

    Returns:
        可用内存 MB 数，无法检测时返回 None（由调用方决定回退策略）
    """
    # 优先 psutil
    try:
        import psutil
        return psutil.virtual_memory().available / (1024 * 1024)
    except ImportError:
        pass

    # Windows 原生：GlobalMemoryStatusEx
    if sys.platform == "win32":
        try:
            import ctypes
            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            return stat.ullAvailPhys / (1024 * 1024)
        except Exception:
            pass

    # Linux 原生：/proc/meminfo
    if sys.platform.startswith("linux"):
        try:
            with open("/proc/meminfo", "r") as f:
                for line in f:
                    if line.startswith("MemAvailable:"):
                        # 格式: MemAvailable:  12345678 kB
                        return int(line.split()[1]) / 1024
        except Exception:
            pass

    # macOS：sysctl hw.memsize 只给总量，无可用量，回退到 None
    return None


def _get_or_create_parser(lang: str, rel_path: str):
    """获取或创建 parser（带进程级缓存）。

    真懒加载：按语言直接 import 具体模块，避免经过 parsers/__init__.py
    聚合入口全量加载 16 个 grammar。

    问题背景：parsers/__init__.py 顶层 import 了所有 parser 模块，
    每个 parser 模块又顶层 import 对应 tree-sitter grammar。
    所以 `from ..parsers import CParser` 会连带加载 tree_sitter_c
    以及其他 15 个 grammar，每个约 15-30MB，总 200-400MB。

    改为 `from ..parsers.c_parser import CParser` 后，只加载 tree_sitter_c，
    firmware 项目（C/C++）每个 worker 从 ~300MB 降到 ~80MB。
    """
    global _worker_parsers
    if lang in _worker_parsers:
        return _worker_parsers[lang]

    p = None
    if lang == "rust":
        from ..parsers.rust import RustParser
        p = RustParser()
    elif lang == "typescript":
        from ..parsers.typescript import TypeScriptParser
        p = TypeScriptParser(dialect="tsx" if rel_path.endswith(".tsx") else "typescript")
    elif lang == "javascript":
        from ..parsers.typescript import TypeScriptParser
        p = TypeScriptParser(dialect="jsx" if rel_path.endswith(".jsx") else "javascript")
    elif lang == "python":
        from ..parsers.python_parser import PythonParser
        p = PythonParser()
    elif lang == "kotlin":
        from ..parsers.kotlin_parser import KotlinParser
        p = KotlinParser()
    elif lang == "go":
        from ..parsers.go_parser import GoParser
        p = GoParser()
    elif lang == "java":
        from ..parsers.java_parser import JavaParser
        p = JavaParser()
    elif lang == "c":
        from ..parsers.c_parser import CParser
        p = CParser()
    elif lang == "cpp":
        from ..parsers.c_parser import CppParser
        p = CppParser()
    elif lang == "csharp":
        from ..parsers.csharp_parser import CSharpParser
        p = CSharpParser()
    elif lang == "ruby":
        from ..parsers.ruby_parser import RubyParser
        p = RubyParser()
    elif lang == "php":
        from ..parsers.php_parser import PhpParser
        p = PhpParser()
    elif lang == "swift":
        from ..parsers.swift_parser import SwiftParser
        p = SwiftParser()
    elif lang == "scala":
        from ..parsers.scala_parser import ScalaParser
        p = ScalaParser()
    elif lang == "hcl":
        from ..parsers.hcl_parser import HclParser
        p = HclParser()
    elif lang == "elixir":
        from ..parsers.elixir_parser import ElixirParser
        p = ElixirParser()

    if p is not None:
        _worker_parsers[lang] = p
    return p


# ============================================
# 资源文件检测（基于内容特征，非文件大小）
# ============================================

# LVGL 生成的字体/图片资源 C 文件的特征宏定义
_LVGL_RESOURCE_MARKERS = (
    "LV_ATTRIBUTE_IMG_",
    "LV_ATTRIBUTE_LARGE_CONST",
)

# 十六进制字面量正则（0x00-0xFF）
_HEX_LITERAL_RE = re.compile(r'0x[0-9a-fA-F]{2}')

# 检测时读取的文件头部字节数（足够检测特征，又不读太多）
_RESOURCE_HEAD_BYTES = 8192

# 十六进制字面量密度阈值：平均每行超过此数量判定为资源文件
_HEX_DENSITY_THRESHOLD = 8
# 最少行数要求（避免短文件误判）
_HEX_MIN_LINES = 10


def _is_resource_file(abs_path: str):
    """检测文件是否为资源文件（字体/图片数据的 C 数组，非业务代码）。

    基于内容特征检测，而非文件大小判断。

    用户要求：判断代码内容是否为"奇怪格式"，而非简单按大小判断。
    资源文件（如 LVGL 生成的字体/图片 C 文件）有两个明显内容特征：

    1. LVGL 资源文件宏定义（LV_ATTRIBUTE_IMG_ / LV_ATTRIBUTE_LARGE_CONST）
       —— LVGL 图片转换工具生成的 C 文件头部必有这些宏
    2. 十六进制字节数组密度（前 8KB 平均每行 > 8 个 0x.. 字面量）
       —— 二进制数据用 C 数组表示时的典型模式
       （如 GIF 头 0x47, 0x49, 0x46, 0x38, 0x39, 0x61 ...）

    tree-sitter parse 这类文件时 AST 内存爆炸（8.9MB 文件 → 7GB+ AST），
    且这些文件无业务代码语义，应跳过。

    Args:
        abs_path: 文件绝对路径

    Returns:
        (is_resource, reason):
            is_resource: True 表示是资源文件应跳过
            reason: 跳过原因（用于日志），如 "lvgl_resource" / "hex_array"，
                   非资源文件为 None
    """
    try:
        with open(abs_path, 'rb') as f:
            head = f.read(_RESOURCE_HEAD_BYTES)
    except OSError:
        return False, None

    head_str = head.decode('utf-8', errors='replace')

    # 特征 1: LVGL 资源文件宏定义（强信号）
    # LVGL 生成的资源 C 文件头部必有这些宏定义
    if any(m in head_str for m in _LVGL_RESOURCE_MARKERS):
        return True, "lvgl_resource"

    # 特征 2: 十六进制字节数组密度检测
    # 资源文件特征：大量 0x.. 字面量密集出现（二进制数据的 C 数组表示）
    hex_count = len(_HEX_LITERAL_RE.findall(head_str))
    lines = head_str.count('\n') + 1
    if lines >= _HEX_MIN_LINES and hex_count / lines > _HEX_DENSITY_THRESHOLD:
        return True, "hex_array"

    return False, None


# ============================================
# P21: 第三方库目录自动检测（基于内容特征，非硬编码规则）
# ============================================

# 已知第三方库目录名模式（这些目录几乎 100% 是第三方库存放目录）
_THIRD_PARTY_DIR_NAMES = frozenset({
    "node_modules", "vendor", "third_party", "thirdparty", "3rdparty",
    "bower_components", "jspm_packages", "web_modules",
    # Java/JVM
    ".m2", ".gradle", "ivy",
    # 其他
    "deps", "deps_packages",
})

# 第三方库目录的"可疑"目录名（需要配合内容检测才判定）
_SUSPICIOUS_DIR_NAMES = frozenset({
    "static", "libs", "lib", "external", "externals",
    "assets", "resources", "vendor_src",
})

# 大文件阈值：> 500KB 的源码文件通常是打包后的第三方库（如 echarts.js 2.8MB）
# 业务代码文件通常 < 500KB（即使是大型 C 文件，一般也不超过 500KB）
_LARGE_FILE_THRESHOLD = 500 * 1024  # 500KB
# 目录内大文件数量阈值：超过此数量判定为第三方库目录
_LARGE_FILE_COUNT_THRESHOLD = 5
# minified 文件特征
_MINIFIED_FILE_MARK = ".min."

# 检测得分阈值：总分 >= 此值才判定为第三方库目录
_THIRD_PARTY_SCORE_THRESHOLD = 50

# 内容检测最大深度：只对深度 <= 2 的目录做内容检测（性能优化）
_CONTENT_SCAN_MAX_DEPTH = 2

# 源码文件扩展名集合（用于过滤大文件统计，忽略 .a/.so 等二进制文件）
# 避免误判：firmware 的 libbin/ 目录有 26 个 .a/.so 文件（4MB+），但这些不是源码
_SOURCE_FILE_EXTS = frozenset({
    ".c", ".cpp", ".cc", ".cxx", ".h", ".hpp", ".hxx",  # C/C++
    ".java", ".kt", ".scala",  # JVM
    ".py", ".rb", ".php",  # 脚本
    ".js", ".jsx", ".ts", ".tsx",  # JS/TS
    ".go", ".rs", ".swift",  # 现代
    ".cs", ".m", ".mm",  # C#/ObjC
    ".ex", ".exs",  # Elixir
    ".hcl", ".tf",  # HCL/Terraform
})


def _detect_third_party_dir(abs_dir_path: str, rel_dir_path: str) -> tuple:
    """检测目录是否为第三方库目录（基于内容特征）

    算法：基于多信号评分，总分 >= 50 才判定为第三方库目录。

    信号：
    1. 大文件密度（> 5 个 > 100KB 的文件）：+50 分
       第三方库通常被打包成大文件（如 echarts.js 2.8MB）
    2. minified 文件（.min.js/.min.css）：+30 分
       第三方库通常提供 minified 版本
    3. 目录名匹配已知模式（node_modules/vendor 等）：+100 分（直接判定）
    4. 目录名匹配可疑模式（static/libs 等）：+20 分（辅助信号）

    性能优化：
    - 目录名匹配已知模式直接判定，不做内容检测
    - 深度 > 2 的目录只做目录名检测，不做内容检测
    - 内容检测只统计文件大小，不读文件内容

    Args:
        abs_dir_path: 目录绝对路径
        rel_dir_path: 目录相对路径（用 / 分隔，用于计算深度）

    Returns:
        (is_third_party, reason):
            is_third_party: True 表示是第三方库目录应跳过
            reason: 跳过原因（用于日志），如 "known_dir" / "large_files" / "minified"
    """
    dir_name = os.path.basename(abs_dir_path).lower()

    # 信号 3: 已知第三方库目录名（直接判定，100 分）
    if dir_name in _THIRD_PARTY_DIR_NAMES:
        return True, f"known_dir:{dir_name}"

    # 计算目录深度（相对路径的 / 数量）
    depth = rel_dir_path.count("/") if rel_dir_path else 0

    # 深度 > 2 的目录只做目录名检测（性能优化）
    if depth > _CONTENT_SCAN_MAX_DEPTH:
        return False, None

    # 内容检测：统计大文件和 minified 文件
    try:
        large_files = 0
        has_minified = False
        for f in os.listdir(abs_dir_path):
            if f.startswith("."):
                continue
            # 信号 2: minified 文件
            if _MINIFIED_FILE_MARK in f.lower():
                has_minified = True
                continue  # minified 文件通常不大，不需要再 stat
            # 只统计源码文件的大文件（忽略 .a/.so/.o 等二进制文件）
            ext = os.path.splitext(f)[1].lower()
            if ext not in _SOURCE_FILE_EXTS:
                continue
            f_path = os.path.join(abs_dir_path, f)
            try:
                if os.path.isfile(f_path) and os.path.getsize(f_path) > _LARGE_FILE_THRESHOLD:
                    large_files += 1
            except OSError:
                pass

        # 信号 1: 大文件密度
        if large_files >= _LARGE_FILE_COUNT_THRESHOLD:
            return True, f"large_files:{large_files}"

        # 信号 2: minified 文件
        if has_minified:
            return True, "minified"

        # 信号 4: 可疑目录名 + 辅助信号（大文件 >= 1 或 minified 存在）
        if dir_name in _SUSPICIOUS_DIR_NAMES:
            if large_files >= 1 or has_minified:
                return True, f"suspicious_dir:{dir_name}"
    except OSError:
        pass

    return False, None


def _can_use_rust_parse() -> bool:
    """P29/P30: 检测 Rust 扩展是否可用且支持 parse。

    P30 起优先使用流式 pool API（batch_parse_c_files_pool + ParseResultPool），
    结果存 Rust 侧 Vec，Python 按需 get_at 读取单个 dict，避免一次性生成 N 个
    Python dict 的转换峰值。不可用时回退到 P29 的 batch_parse_c_files。

    Returns:
        True 表示 callwarden_core 的 parse 接口可用
    """
    try:
        # P30: 优先检测流式 pool API
        from callwarden_core import batch_parse_c_files_pool  # noqa: F401
        return True
    except ImportError:
        pass
    try:
        from callwarden_core import batch_parse_c_files  # noqa: F401
        return True
    except ImportError:
        return False


def _python_multiprocess_parse(to_parse, mp_workers, file_results,
                                failed_files, parse_total,
                                skipped_ref=None, failed_ref=None):
    """P29: Python 原多进程 parse 路径（提取为函数，供 Rust fallback 复用）。

    Args:
        to_parse: 待 parse 文件列表 [(idx, rel_path, abs_path, lang, module_path, file_instance_id), ...]
        mp_workers: worker 数
        file_results: 结果 dict（in-place 更新）
        failed_files: 失败文件列表（in-place 更新）
        parse_total: 总文件数（进度显示用）
        skipped_ref: [skipped_count] 引用（in-place 更新）
        failed_ref: [failed_count] 引用（in-place 更新）
    """
    skipped = skipped_ref[0] if skipped_ref else 0
    failed = failed_ref[0] if failed_ref else 0

    cprint(t("cli.messages.db_build_parallel_parse", workers=mp_workers, count=len(to_parse)), "dim")
    from concurrent.futures import ProcessPoolExecutor, as_completed
    mp_args = [(rel_path, abs_path, lang, module_path, file_instance_id)
               for _, rel_path, abs_path, lang, module_path, file_instance_id in to_parse]
    chunksize = max(1, len(mp_args) // (mp_workers * 4))
    cprint(f"  (P15 multiprocess: {mp_workers} workers, chunksize={chunksize})", "dim")

    try:
        with ProcessPoolExecutor(
            max_workers=mp_workers,
            initializer=_init_worker_parsers,
        ) as pool:
            done_count = 0
            for status, rel_path, payload in pool.map(
                _parse_file_worker, mp_args, chunksize=chunksize
            ):
                if status == "ok":
                    file_results[rel_path] = payload
                elif status == "fail":
                    failed += 1
                    failed_files.append((rel_path, payload))
                elif status == "skip_resource":
                    skipped += 1
                    cprint(t("cli.messages.db_build_skip_resource", path=rel_path, reason=payload), "yellow")
                else:
                    skipped += 1
                done_count += 1
                print_progress(done_count, parse_total,
                               t("cli.messages.db_build_parse_progress_lang", path=rel_path, lang=""))
    except (pickle.PickleError, BrokenPipeError, OSError) as e:
        cprint(f"  (P15 fallback to ThreadPool: {e})", "yellow")
        # 降级为 ThreadPoolExecutor
        max_workers = min(8, max(1, mp_workers))
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            done_count = 0
            for status, rel_path, payload in pool.map(_parse_file_worker, mp_args):
                if status == "ok":
                    file_results[rel_path] = payload
                elif status == "fail":
                    failed += 1
                    failed_files.append((rel_path, payload))
                elif status == "skip_resource":
                    skipped += 1
                    cprint(t("cli.messages.db_build_skip_resource", path=rel_path, reason=payload), "yellow")
                else:
                    skipped += 1
                done_count += 1
                print_progress(done_count, parse_total,
                               t("cli.messages.db_build_parse_progress_lang", path=rel_path, lang=""))

    # 回写引用
    if skipped_ref is not None:
        skipped_ref[0] = skipped
    if failed_ref is not None:
        failed_ref[0] = failed


def _parse_file_worker(args):
    """多进程 worker：解析单个源文件（模块级函数，可 pickle）。

    在 worker 进程中执行，绕开 GIL 实现真正的并行 parse。
    使用进程级 _worker_parsers 缓存，避免每文件创建 parser。

    Args:
        args: 元组 (rel_path, abs_path, lang, module_path, file_instance_id)
    Returns:
        元组 (status, rel_path, payload)
            - status: "ok" / "fail" / "skip" / "skip_resource"
            - payload: 成功时为解析结果 dict，失败时为错误字符串，
              skip_resource 时为原因字符串，其他跳过为 None
    """
    rel_path, abs_path, lang, module_path, file_instance_id = args
    try:
        # 资源文件检测：基于内容特征跳过字体/图片数据的 C 数组
        # 不按文件大小判断，而是检测 LVGL 宏定义和十六进制字节数组密度
        is_res, reason = _is_resource_file(abs_path)
        if is_res:
            return ("skip_resource", rel_path, reason)

        parser = _get_or_create_parser(lang, rel_path)

        if not parser:
            return ("skip", rel_path, None)

        result = parser.parse_file(abs_path, module_path)
        result["abs_path"] = abs_path
        result["file_instance_id"] = file_instance_id
        result["module_path"] = module_path
        result["rel_path"] = rel_path
        result.setdefault("inline_modules", [])
        return ("ok", rel_path, result)
    except Exception as e:
        return ("fail", rel_path, str(e))


class BuildMixin:
    """构建功能 Mixin

    通过 self.conn 访问数据库连接，提供文件解析和调用图构建功能。
    """

    def build(self):
        """完整构建知识图谱"""
        print(t("cli.messages.db_build_step1_parse_modules"))
        self.module_resolver.resolve_all(self.parser)
        print(t("cli.messages.db_build_modules_found", count=len(self.module_resolver.module_to_file)))

        crate_name = self._detect_crate_name()
        print(t("cli.messages.db_build_crate_name", name=crate_name))

        print(t("cli.messages.db_build_step2_parse_files"))
        file_results = {}
        failed_files = []
        total = len(self.module_resolver.module_to_file)
        for i, (mod_path, rel_path) in enumerate(self.module_resolver.module_to_file.items(), 1):
            abs_path = os.path.join(self.workspace_root, rel_path)
            file_instance_id = self._register_file_db(abs_path, mod_path)
            try:
                result = self.parser.parse_file(abs_path, mod_path)
                result["abs_path"] = abs_path
                result["file_instance_id"] = file_instance_id
                result["module_path"] = mod_path
                result["rel_path"] = norm_path(rel_path)
                result.setdefault("inline_modules", [])
                file_results[norm_path(rel_path)] = result
                print(t("cli.messages.db_build_parse_progress", i=i, total=total, path=rel_path))
            except Exception as e:
                failed_files.append((rel_path, str(e)))
                print(t("cli.messages.db_build_parse_fail", i=i, total=total, path=rel_path, error=e))

        print(t("cli.messages.db_build_parsed_count", count=len(file_results)), end="")
        if failed_files:
            print(t("cli.messages.db_build_failed_count", count=len(failed_files)))
        else:
            print()

        print(t("cli.messages.db_build_step3_versions"))
        for rel_path, result in file_results.items():
            file_version_id = self._save_file_version(result["file_instance_id"], result)
            result["file_version_id"] = file_version_id
            self._save_symbols_for_version(file_version_id, result["file_instance_id"], result)

        print(t("cli.messages.db_build_step4_calls"))
        self.build_call_graph(file_results, crate_name)

        print(t("cli.messages.db_build_step5_depth"))
        self._build_depth()

        print(t("cli.messages.db_build_step6_update_depth"))
        self._update_symbol_version_depths()

        self.conn.commit()
        # B-P7b: 失效 GraphStore 缓存（完整构建已写入 symbols/calls）
        self._invalidate_graph_store()

        if failed_files:
            print(t("cli.messages.db_build_done_failures", count=len(failed_files)))
        else:
            print(t("cli.messages.db_build_done"))


    def build_full_graph(self, force: bool = False):
        """构建完整知识图谱（自动检测所有支持的语言）

        Args:
            force: 是否强制重新解析所有文件（忽略增量）
        """
        # 扫描所有支持的文件，统一走多语言构建流程（支持增量）
        files = self._scan_supported_files()
        self._build_multi_lang(files, force=force)


    def build_directory(self, dir_path: str):
        """构建指定目录的知识图谱

        Args:
            dir_path: 相对项目根目录的路径（如 "src/cli"）或绝对路径
        """
        # 标准化路径
        abs_dir = os.path.abspath(dir_path)
        if not os.path.isabs(dir_path):
            abs_dir = os.path.join(self.workspace_root, dir_path)

        if not os.path.isdir(abs_dir):
            print(t("cli.messages.db_build_dir_not_exist", path=abs_dir))
            return

        # 扫描该目录下的源文件
        supported_extensions = set(get_supported_extensions())
        skip_dirs = {".git", "node_modules", "target", "dist", "build", ".next", "__pycache__"}
        files = []
        for root, dirs, filenames in safe_walk(abs_dir):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
            for filename in filenames:
                ext = os.path.splitext(filename)[1].lower()
                if ext in supported_extensions:
                    abs_path = os.path.join(root, filename)
                    rel_path = norm_path(os.path.relpath(abs_path, self.workspace_root))
                    files.append(rel_path)

        files.sort()
        if not files:
            print(t("cli.messages.db_build_no_source_files", path=dir_path))
            return

        rust_files = [f for f in files if f.endswith(".rs")]
        other_files = [f for f in files if not f.endswith(".rs")]

        print(t("cli.messages.db_build_build_dir", path=dir_path, count=len(files)))
        if other_files or len(rust_files) != len(files):
            self._build_multi_lang(files)
        else:
            # 纯 Rust 目录，也走多语言流程以保持一致
            self._build_multi_lang(files)


    def _load_ignore_patterns(self) -> List[str]:
        """加载 .callwardenignore 规则

        规则格式（类似 .gitignore）：
        - 每行一个模式
        - # 开头是注释
        - 支持通配符：* 匹配任意字符，** 匹配任意目录
        - 目录名后加 / 只匹配目录
        - 以 / 开头匹配根目录
        """
        import fnmatch

        patterns = []
        ignore_file = os.path.join(self.workspace_root, ".callwardenignore")

        # 默认忽略规则（硬编码基线，覆盖 VCS/包管理/构建输出/预构建/autogen）
        # 这些规则对 repo manifest/AOSP/嵌入式项目尤其关键——
        # autogen 代码和预构建产物动辄几十万文件，不排除会让 DB 爆掉
        default_ignores = [
            # === VCS / 包管理 / Python 虚拟环境 ===
            ".git/", "node_modules/", ".next/",
            "__pycache__/", ".venv/", "venv/", "env/", ".tox/", "*.egg-info/",
            # === 构建输出目录（AOSP/嵌入式/Make/Cargo）===
            # 这些目录包含编译中间产物和 autogen 源码，体积巨大且无分析价值
            "target/", "dist/", "build/", "out/", "output/", "outputs/",
            "obj/", "bin/", "rootfs/", "staging/", "sysroot/", "ccache/",
            # === 预构建 / 二进制 / 工具链 ===
            # prebuilt/prebuilts/blob 是厂商二进制，toolchain/ndk/jdk 是工具链
            "prebuilt/", "prebuilts/", "blob/", "toolchain/", "toolchains/",
            "ndk/", "jdk/",
            # === 第三方依赖源码 ===
            # thirdParty/third_party/vendor 是常见第三方库存放目录
            # 包含大量外部代码，非项目业务逻辑，分析价值低且会显著增加 DB 体积
            "thirdParty/", "third_party/", "vendor/",
            # === autogen 代码目录（核心痛点）===
            # 这些目录存放 protobuf/grpc/Qt moc 等自动生成的源码
            # 扩展名是 .c/.cpp/.py，会被扫描器拾取，但内容无人工维护语义
            "autogen/", "auto_gen/", "generated/", "gen/", "generated_src/",
            "proto_gen/", "protobuf_gen/", "grpc_gen/", "moc/",
            # === autogen 文件名模式（protobuf / Qt / Python 字节码）===
            "*.pb.cc", "*.pb.h", "*.pb.go",           # protobuf C++/Go 生成
            "*_pb2.py", "*_pb2.pyi", "*_pb2_grpc.py",  # protobuf Python 生成
            "*.grpc.cc", "*.grpc.h",                   # grpc C++ 生成
            "moc_*.cpp", "ui_*.h", "qrc_*.cpp",        # Qt moc/ui 资源生成
            "*.pyc", "*.pyo",                           # Python 字节码
            # === repo 工具元数据（AOSP repo manifest 项目）===
            ".repo/",
        ]

        if os.path.exists(ignore_file):
            try:
                text = read_file_text(ignore_file)
                for line in text.splitlines():
                    line = line.strip()
                    if line and not line.startswith("#"):
                        patterns.append(line)
            except Exception:
                pass

        # 合并默认规则和用户规则
        all_patterns = default_ignores + patterns
        return all_patterns


    def _should_ignore(self, rel_path: str, is_dir: bool, patterns: List[str]) -> bool:
        """判断路径是否应该被忽略

        Args:
            rel_path: 相对路径（使用 / 分隔符）
            is_dir: 是否是目录
            patterns: 忽略规则列表
        """
        import fnmatch

        path_parts = rel_path.split("/")

        for pattern in patterns:
            orig_pattern = pattern
            p = pattern.rstrip("/")
            match_dir_only = pattern.endswith("/")

            # 以 / 开头：只匹配根目录
            if p.startswith("/"):
                p = p[1:]
                if fnmatch.fnmatch(rel_path, p):
                    return True
                if is_dir and fnmatch.fnmatch(rel_path + "/", p + "/"):
                    return True
                continue

            # 包含 /：匹配完整路径
            if "/" in p:
                if fnmatch.fnmatch(rel_path, p):
                    return True
                continue

            # 不包含 /：匹配任意层级的文件名/目录名
            if match_dir_only:
                if is_dir and path_parts[-1] == p:
                    return True
            else:
                if fnmatch.fnmatch(path_parts[-1], p):
                    return True
                # 也检查是否匹配完整路径的任意后缀
                for i in range(len(path_parts)):
                    subpath = "/".join(path_parts[i:])
                    if fnmatch.fnmatch(subpath, p):
                        return True

        return False


    def _scan_supported_files(self) -> List[str]:
        """扫描项目中所有支持的源文件（尊重 .callwardenignore + P21 自动检测）"""
        supported_extensions = set(get_supported_extensions())
        files = []
        ignore_patterns = self._load_ignore_patterns()
        # P21: 自动检测到的第三方库目录（用于日志输出）
        auto_ignored: List[tuple] = []

        for root, dirs, filenames in safe_walk(self.workspace_root):
            # 过滤目录：原地修改 dirs 以跳过
            rel_root = norm_path(os.path.relpath(root, self.workspace_root))
            if rel_root == ".":
                rel_root = ""

            # 过滤要跳过的目录
            dirs_to_keep = []
            for d in dirs:
                if d.startswith(".") and d not in (".codegraph",):
                    continue
                d_rel = (rel_root + "/" + d) if rel_root else d
                if self._should_ignore(d_rel, True, ignore_patterns):
                    continue
                # P21: 自动检测第三方库目录（基于内容特征，非硬编码规则）
                abs_d = os.path.join(root, d)
                is_tp, reason = _detect_third_party_dir(abs_d, d_rel)
                if is_tp:
                    auto_ignored.append((d_rel, reason))
                    continue
                dirs_to_keep.append(d)
            dirs[:] = dirs_to_keep

            for filename in filenames:
                ext = os.path.splitext(filename)[1].lower()
                if ext in supported_extensions:
                    abs_path = os.path.join(root, filename)
                    rel_path = norm_path(os.path.relpath(abs_path, self.workspace_root))
                    if not self._should_ignore(rel_path, False, ignore_patterns):
                        files.append(rel_path)

        # P21: 打印自动检测到的第三方库目录
        if auto_ignored:
            cprint(
                t(
                    "cli.messages.auto_ignore_detected",
                    count=len(auto_ignored),
                    dirs=", ".join(f"{d}({r})" for d, r in auto_ignored[:10]),
                    default=f"  P21 自动检测: 跳过 {len(auto_ignored)} 个第三方库目录",
                ),
                "dim",
            )

        return sorted(files)


    def _detect_repo_manifest(self) -> int:
        """检测 repo manifest 并注册子仓库为 workspace

        AOSP/嵌入式项目用 `repo` 工具管理多仓库，根目录有 `.repo/` 元数据。
        本方法解析 manifest.xml，把每个 <project> 注册为独立 workspace 记录，
        让 list_workspaces / 跨仓库分析能识别子仓库边界。

        注册的 workspace：
        - name: manifest 的 name 属性（如 "firmware/middleware"）
        - root_path: workspace_root + "/" + path 属性
        - description: "repo subproject: <remote>/<name>"

        Returns:
            注册的子仓库数量（已存在的跳过）
        """
        import xml.etree.ElementTree as ET

        repo_dir = os.path.join(self.workspace_root, ".repo")
        if not os.path.isdir(repo_dir):
            return 0

        # 定位 manifest.xml：优先 .repo/manifests/<default>.xml，回退到根目录
        manifest_path = None
        manifests_dir = os.path.join(repo_dir, "manifests")
        if os.path.isdir(manifests_dir):
            # 找 default.xml 或任意 .xml
            for candidate in ("default.xml", "manifest.xml"):
                p = os.path.join(manifests_dir, candidate)
                if os.path.isfile(p):
                    manifest_path = p
                    break
            if not manifest_path:
                # 找目录里第一个 .xml
                for fn in os.listdir(manifests_dir):
                    if fn.endswith(".xml"):
                        manifest_path = os.path.join(manifests_dir, fn)
                        break
        if not manifest_path:
            # 回退：根目录的 manifest.xml
            root_manifest = os.path.join(self.workspace_root, "manifest.xml")
            if os.path.isfile(root_manifest):
                manifest_path = root_manifest

        if not manifest_path:
            return 0

        try:
            tree = ET.parse(manifest_path)
            root_elem = tree.getroot()
        except Exception:
            return 0

        registered = 0
        for proj in root_elem.findall("project"):
            name = proj.get("name", "")
            path = proj.get("path", name)
            remote = proj.get("remote", "")
            if not name:
                continue
            # 子仓库的绝对路径
            sub_abs = os.path.join(self.workspace_root, path)
            if not os.path.isdir(sub_abs):
                continue
            # 注册为 workspace（register_workspace 内部已做去重）
            desc = f"repo subproject: {remote}/{name}" if remote else f"repo subproject: {name}"
            self.register_workspace(name, sub_abs, description=desc)
            registered += 1

        return registered


    def _build_multi_lang(self, files: List[str], force: bool = False):
        """多语言通用构建流程（支持并行解析）

        Args:
            files: 相对路径列表
            force: 是否强制重新解析所有文件（忽略增量）
        """
        t_start = time.time()
        total = len(files)

        # 检测 repo manifest，注册子仓库为 workspace（让跨仓库分析能识别边界）
        subrepo_count = self._detect_repo_manifest()
        if subrepo_count > 0:
            cprint(t("cli.messages.db_build_repo_manifest", count=subrepo_count), "dim")

        cprint(t("cli.messages.db_build_step1_5_scan", count=total), "cyan", bold=True)

        cprint(t("cli.messages.db_build_step2_5_parse"), "cyan", bold=True)
        file_results = {}
        skipped = 0
        unchanged = 0
        failed = 0
        failed_files = []  # P23.5: 提前初始化，register 循环中也可能追加

        to_parse = []
        parsed_new = 0  # P11: 初始化为 0，用于 GC 条件化判断

        # P20: 收集项目中实际出现的语言集合
        # 用于 stdlib_import 时只导入项目实际使用语言的 stdlib 符号，
        # 避免给 Python 项目导入 Java/C/Rust 等无关语言的 stdlib（无效数据 + 耗时）
        project_langs: Set[str] = set()

        # P10: 细拆 register 阶段计时（逐文件 SQL: _register_file_db + _get_file_version）
        t_register_start = time.perf_counter()
        for i, rel_path in enumerate(files, 1):
            abs_path = os.path.join(self.workspace_root, rel_path)
            lang = detect_language_from_path(rel_path)
            parser = create_parser(rel_path)

            if not parser:
                skipped += 1
                continue

            # P20: 记录项目实际使用的语言
            if lang:
                project_langs.add(lang)

            # P23.5/P23.6: 捕获文件系统异常（WinError 1920 文件锁 / WinError 3 路径过长）
            # 跳过不可访问的文件，不中断整个构建
            try:
                module_path = self._infer_module_path_generic(rel_path, lang)
                file_instance_id = self._register_file_db(abs_path, module_path)

                if not force:
                    current_mtime = os.path.getmtime(abs_path)
                    latest_fv = self._get_file_version(file_instance_id)
                    if latest_fv and abs(latest_fv["mtime"] - current_mtime) < 0.001:
                        old_result = self._load_file_result_from_db(file_instance_id, latest_fv["id"], rel_path, abs_path, module_path)
                        if old_result:
                            file_results[rel_path] = old_result
                            unchanged += 1
                            continue
            except OSError as e:
                # 文件不可访问：跳过并记录，不中断构建
                failed += 1
                failed_files.append((rel_path, f"OSError: {e}"))
                cprint(f"  ⚠ 跳过不可访问文件: {rel_path} ({e})", "yellow")
                continue

            to_parse.append((i, rel_path, abs_path, lang, module_path, file_instance_id))
        t_register = time.perf_counter() - t_register_start

        if unchanged > 0 and not to_parse:
            cprint(t("cli.messages.db_build_all_unchanged", count=unchanged), "green")
            duration = time.time() - t_start
            cprint()
            cprint(t("cli.messages.db_build_summary_title"), "cyan", bold=True)
            cprint()
            cprint(t("cli.messages.db_build_summary_files"), "bold")
            cprint(t("cli.messages.db_build_summary_unchanged", count=unchanged), "dim")
            cprint()
            cprint(t("cli.messages.db_build_summary_duration", duration=format_duration(duration)), "yellow")
            cprint(t("cli.messages.db_build_summary_done"), "green")
            cprint()
            # P10: even in all-unchanged path, show register timing
            cprint("── 阶段耗时分解 ──────────────────────", "cyan", bold=True)
            cprint(f"  register (逐文件SQL)    : {t_register:8.2f}s  (注册/mtime/version 查询)", "dim")
            cprint(f"  total                   : {duration:8.2f}s", "cyan", bold=True)
            cprint("──────────────────────────────────────", "cyan")
            # P13: early return 路径也暴露 _stage_timings（供 perf test 做基线对比）
            self._stage_timings = {
                "register": t_register,
                "parse": 0,
                "symbol_write": 0,
                "stdlib_import": 0,
                "call_resolve_write": 0,
                "depth": 0,
                "fts_rebuild": 0,
                "commit": 0,
                "gc_archive": 0,
                "total": duration,
                "files_total": unchanged + skipped,
                "files_parsed": 0,
                "files_unchanged": unchanged,
                # P23.7: 暴露 skipped/failed 计数
                "files_skipped": skipped,
                "files_failed": 0,
            }
            return

        t_parse_start = time.perf_counter()
        if to_parse:
            parse_total = len(to_parse)

            # P15.3: 按语言预排序，让同语言文件聚集
            # ProcessPoolExecutor 的 chunksize 分块后，每个 worker 尽量只处理一种语言，
            # 减少 worker 内多语言切换导致的 parser 创建开销（每个 parser ~30MB）
            # 排序稳定（相同 lang 保持原 idx 顺序），不影响结果正确性
            to_parse.sort(key=lambda x: (x[3], x[0]))  # (lang, idx)

            # P15: 文件数超过阈值时用 ProcessPoolExecutor（绕开 GIL 真正并行 parse）
            # tree-sitter Parser.parse() 不释放 GIL，ThreadPoolExecutor 实际只有 2x 加速
            # ProcessPoolExecutor 用独立进程，每个进程有自己的 GIL，可真正 N 核并行
            #
            # 真懒加载后每进程内存约 80-150MB（仅加载实际用到的语言 grammar）
            # worker 数由 _detect_optimal_workers() 根据宿主机剩余资源动态决定
            MP_THRESHOLD = 50  # 文件数 >= 50 才用多进程（避免进程创建开销）
            use_multiprocess = len(to_parse) >= MP_THRESHOLD

            if use_multiprocess:
                # 多进程路径：动态检测宿主机资源决定 worker 数
                # 真懒加载后每进程内存约 80-150MB，按可用内存动态分配 worker
                env_workers = os.environ.get("CW_MP_WORKERS")
                if env_workers:
                    # 环境变量覆盖：用户手动指定
                    mp_workers = max(1, min(8, int(env_workers)))
                else:
                    # 动态检测：根据 CPU 核心数、剩余内存、数据规模计算
                    # P28：传 file_count 启用数据规模因子，避免主进程结果持有内存爆炸
                    mp_workers = _detect_optimal_workers(len(to_parse))

                # P29: Rust batch_parse_c_files 接入点（仅 C 语言）
                # Rust 路径：rayon 线程并行 + grammar 共享 + 零拷贝，避免多进程内存爆炸
                # Python fallback：Rust 扩展不可用或非 C 语言时走原 ProcessPoolExecutor
                c_files_to_parse = [x for x in to_parse if x[3] == "c"]
                use_rust = (len(c_files_to_parse) >= MP_THRESHOLD
                            and _can_use_rust_parse()
                            and not os.environ.get("CW_DISABLE_RUST_PARSE"))
                if use_rust:
                    cprint(t("cli.messages.db_build_parallel_parse",
                             workers=mp_workers, count=len(c_files_to_parse)), "dim")
                    cprint(f"  (P30 rust pool: {len(c_files_to_parse)} files, "
                           f"threads={mp_workers})", "dim")
                    try:
                        # 资源文件预过滤（Rust 侧不做，避免读大文件）
                        c_files_filtered = []
                        for rel_path, abs_path, lang, module_path, file_instance_id in c_files_to_parse:
                            is_res, reason = _is_resource_file(abs_path)
                            if is_res:
                                skipped += 1
                                failed_files.append((rel_path, f"skip_resource:{reason}"))
                                cprint(t("cli.messages.db_build_skip_resource",
                                        path=rel_path, reason=reason), "yellow")
                                continue
                            c_files_filtered.append((abs_path, module_path, rel_path, file_instance_id))
                        # Rust 批量 parse（rayon 并行，grammar 共享）
                        rust_args = [(abs_path, module_path)
                                     for abs_path, module_path, _, _ in c_files_filtered]
                        # P30: 优先使用流式 pool API（结果存 Rust 侧，Python 按需 get_at 读取）
                        # 避免 batch_parse_c_files 一次性生成 N 个 Python dict 的转换峰值
                        pool = None
                        try:
                            from callwarden_core import batch_parse_c_files_pool
                            pool = batch_parse_c_files_pool(rust_args, num_threads=mp_workers)
                        except ImportError:
                            pass
                        if pool is not None:
                            # P30 流式：逐个 get_at 转 dict，写入 file_results
                            for i, (abs_path, module_path, rel_path, file_instance_id) in enumerate(c_files_filtered):
                                r = pool.get_at(i)
                                if r.get("error"):
                                    failed += 1
                                    failed_files.append((rel_path, r["error"]))
                                    continue
                                r["abs_path"] = abs_path
                                r["file_instance_id"] = file_instance_id
                                r["module_path"] = module_path
                                r["rel_path"] = rel_path
                                r.setdefault("inline_modules", [])
                                file_results[rel_path] = r
                                done_count = len(file_results)
                                print_progress(done_count, parse_total,
                                               t("cli.messages.db_build_parse_progress_lang",
                                                 path=rel_path, lang="c"))
                        else:
                            # P29 fallback：一次性返回 Python list
                            from callwarden_core import batch_parse_c_files
                            rust_results = batch_parse_c_files(rust_args, num_threads=mp_workers)
                            for (abs_path, module_path, rel_path, file_instance_id), r in zip(c_files_filtered, rust_results):
                                if r.get("error"):
                                    failed += 1
                                    failed_files.append((rel_path, r["error"]))
                                    continue
                                r["abs_path"] = abs_path
                                r["file_instance_id"] = file_instance_id
                                r["module_path"] = module_path
                                r["rel_path"] = rel_path
                                r.setdefault("inline_modules", [])
                                file_results[rel_path] = r
                                done_count = len(file_results)
                                print_progress(done_count, parse_total,
                                               t("cli.messages.db_build_parse_progress_lang",
                                                 path=rel_path, lang="c"))
                        # 非 C 语言文件走原 Python 多进程（如果有的话）
                        non_c_files = [x for x in to_parse if x[3] != "c"]
                        if non_c_files:
                            _python_multiprocess_parse(non_c_files, mp_workers, file_results,
                                                       failed_files, parse_total,
                                                       skipped_ref=[skipped], failed_ref=[failed])
                    except Exception as e:
                        cprint(f"  (P30 rust fallback to multiprocess: {e})", "yellow")
                        use_rust = False
                if not use_rust:
                    _python_multiprocess_parse(to_parse, mp_workers, file_results,
                                               failed_files, parse_total,
                                               skipped_ref=[skipped], failed_ref=[failed])
                    # use_multiprocess 标记保持 True，因为已尝试多进程

            if not use_multiprocess:
                # 原多线程路径（小文件量或 fallback）
                # 线程比进程轻量，内存占用远低于进程，但 GIL 限制实际并行度
                # 仍按动态资源检测决定线程数（线程数可放宽，每个线程内存开销小）
                # P28：仍传 file_count，因主进程结果持有内存是真实瓶颈（与线程/进程无关）
                max_workers = _detect_optimal_workers(len(to_parse))
                # 线程比进程轻量，可适当多开（线程内存开销小，GIL 才是真瓶颈）
                max_workers = min(8, max(1, max_workers))
                cprint(t("cli.messages.db_build_parallel_parse", workers=max_workers, count=len(to_parse)), "dim")
                print_lock = threading.Lock()
                done_count = [0]

                def _parse_one(args):
                    """多线程工作函数：解析单个源文件并返回结果元组"""
                    _, rel_path, abs_path, lang, module_path, file_instance_id = args
                    try:
                        # 资源文件检测：基于内容特征跳过字体/图片数据的 C 数组
                        is_res, reason = _is_resource_file(abs_path)
                        if is_res:
                            with print_lock:
                                done_count[0] += 1
                                print_progress(done_count[0], parse_total, t("cli.messages.db_build_parse_progress_lang", path=rel_path, lang=lang))
                            return ("skip_resource", rel_path, reason)
                        # 复用模块级 _get_or_create_parser：真懒加载，按语言直接 import
                        p = _get_or_create_parser(lang, rel_path)
                        if not p:
                            with print_lock:
                                done_count[0] += 1
                            return ("skip", rel_path, None)
                        result = p.parse_file(abs_path, module_path)
                        result["abs_path"] = abs_path
                        result["file_instance_id"] = file_instance_id
                        result["module_path"] = module_path
                        result["rel_path"] = rel_path
                        result.setdefault("inline_modules", [])
                        with print_lock:
                            done_count[0] += 1
                            print_progress(done_count[0], parse_total, t("cli.messages.db_build_parse_progress_lang", path=rel_path, lang=lang))
                        return ("ok", rel_path, result)
                    except Exception as e:
                        with print_lock:
                            done_count[0] += 1
                        return ("fail", rel_path, str(e))

                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    for status, rel_path, payload in pool.map(_parse_one, to_parse):
                        if status == "ok":
                            file_results[rel_path] = payload
                        elif status == "fail":
                            failed += 1
                            failed_files.append((rel_path, payload))
                        elif status == "skip_resource":
                            skipped += 1
                            cprint(t("cli.messages.db_build_skip_resource", path=rel_path, reason=payload), "yellow")
                        else:
                            skipped += 1

            clear_progress()
            parsed_new = len(file_results) - unchanged
            if failed == 0:
                cprint(t("cli.messages.db_build_parse_ok", parsed=parsed_new, unchanged=unchanged, skipped=skipped), "green")
            else:
                cprint(t("cli.messages.db_build_parse_warn", parsed=parsed_new, unchanged=unchanged, skipped=skipped, failed=failed), "yellow")
                for rel_path, err in failed_files:
                    cprint(t("cli.messages.db_build_parse_fail_item", path=rel_path, error=err), "red")

        t_parse = time.perf_counter() - t_parse_start

        spinner = Spinner(t("cli.messages.db_build_step3_5_versions"))
        spinner.start()
        # P8 优化：full build 期间禁用 FTS 触发器，避免每个 symbol INSERT/UPDATE
        # 都同步维护 trigram FTS 索引（写放大）。批量写完后一次性 rebuild。
        fts_was_disabled = self._disable_fts_triggers()
        t_versions_start = time.perf_counter()
        version_count = 0
        for rel_path, result in file_results.items():
            if result.get("_from_db"):
                self._restore_symbol_snapshots(result["file_instance_id"], result)
                continue
            file_version_id = self._save_file_version(result["file_instance_id"], result)
            result["file_version_id"] = file_version_id
            self._save_symbols_for_version(file_version_id, result["file_instance_id"], result)
            version_count += 1
        spinner.stop(t("cli.messages.db_build_versions_written", count=version_count))
        t_versions = time.perf_counter() - t_versions_start

        spinner = Spinner(t("cli.messages.db_build_step4_5_calls"))
        spinner.start()
        # 导入项目实际使用语言的标准库符号
        # P20: 只导入项目实际检测到的语言，避免给 Python 项目导入 Java/C/Rust 等无关 stdlib
        # 这一步在构建调用图之前完成，确保后续 callee 匹配能命中标准库符号
        t_stdlib_start = time.perf_counter()
        self.import_all_stdlib_symbols(languages=list(project_langs) if project_langs else None)
        self.import_project_dependencies()
        t_stdlib = time.perf_counter() - t_stdlib_start

        t_call_resolve_start = time.perf_counter()
        self._build_call_graph_multi_lang(file_results)
        t_call_resolve = time.perf_counter() - t_call_resolve_start

        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute("SELECT COUNT(*) as c FROM calls c JOIN symbols s ON c.caller_id = s.id JOIN file_instances fi ON s.file_instance_id = fi.id WHERE fi.workspace_id = ?", (ws_id,))
        total_calls = cur.fetchone()["c"]
        cur = self.conn.execute("SELECT COUNT(*) as c FROM calls c JOIN symbols s ON c.caller_id = s.id JOIN file_instances fi ON s.file_instance_id = fi.id WHERE fi.workspace_id = ? AND c.callee_id IS NOT NULL", (ws_id,))
        resolved_calls = cur.fetchone()["c"]
        spinner.stop(t("cli.messages.db_build_calls_count", total=total_calls, resolved=resolved_calls))

        spinner = Spinner(t("cli.messages.db_build_step5_5_depth"))
        spinner.start()
        t_depth_start = time.perf_counter()
        self._build_depth()
        self._update_symbol_version_depths()
        t_depth = time.perf_counter() - t_depth_start
        spinner.stop(t("cli.messages.db_build_depth_done"))

        # P8: 批量写完后一次性重建 FTS 索引 + 重新启用触发器
        t_fts_start = time.perf_counter()
        if fts_was_disabled:
            self._rebuild_and_enable_fts()
        t_fts = time.perf_counter() - t_fts_start

        t_commit_start = time.perf_counter()
        self.conn.commit()
        t_commit = time.perf_counter() - t_commit_start
        # B-P7b: 失效 GraphStore 缓存（批量构建已写入 symbols/calls）
        self._invalidate_graph_store()

        # 步骤 6/6: GC 归档（类 Java Young GC，扫描 pending 文件命中 ignore 的迁入 archived_files）
        # 注意：在 commit 之后执行，避免归档事务与构建事务冲突
        # P11: 条件化 GC —— 无新解析文件时跳过（避免 os.walk 全仓遍历构建 matcher）
        # parsed_new 是实际解析（非 unchanged）的文件数；为 0 说明全部走增量，不需要 GC
        t_gc_start = time.perf_counter()
        try:
            if parsed_new > 0:
                gc_result = self.gc_archive(force=False)
                if gc_result["archived"] > 0:
                    cprint(t("cli.messages.db_build_step6_gc", count=gc_result['archived']), "yellow")
                    for reason, count in gc_result["reasons"].items():
                        cprint(t("cli.messages.db_build_gc_reason", reason=reason, count=count), "dim")
            # P11: 无新解析文件时跳过 GC，matcher 不构建、os.walk 不执行
        except Exception as e:
            # GC 失败不阻塞构建
            cprint(t("cli.messages.db_build_gc_fail", error=e), "yellow")
        t_gc = time.perf_counter() - t_gc_start

        duration = time.time() - t_start

        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
        """, (ws_id,))
        total_symbols = cur.fetchone()["cnt"]

        parsed_new = len(file_results) - unchanged
        print_build_summary(parsed_new, unchanged, skipped, failed, total_symbols, total_calls, resolved_calls, duration)

        # 阶段计时汇总（用于定位性能瓶颈）
        # P10: 细拆 scan+parse+register → register（逐文件 SQL） + parse（tree-sitter）
        t_other = duration - (t_register + t_parse + t_versions + t_stdlib + t_call_resolve + t_depth + t_fts + t_commit + t_gc)
        cprint()
        cprint("── 阶段耗时分解 ──────────────────────", "cyan", bold=True)
        cprint(f"  register (逐文件SQL)    : {t_register:8.2f}s  (注册/mtime/version 查询)", "dim")
        cprint(f"  parse (tree-sitter)     : {t_parse:8.2f}s  (多线程源码解析)", "dim")
        cprint(f"  symbol write            : {t_versions:8.2f}s  (写入符号/版本)", "dim")
        cprint(f"  stdlib import           : {t_stdlib:8.2f}s  (标准库符号导入)", "dim")
        cprint(f"  call resolve + write    : {t_call_resolve:8.2f}s  (调用关系解析+写入)", "yellow")
        cprint(f"  depth                   : {t_depth:8.2f}s  (拓扑深度计算)", "dim")
        cprint(f"  fts rebuild             : {t_fts:8.2f}s  (FTS5 索引重建)", "dim")
        cprint(f"  commit                  : {t_commit:8.2f}s  (事务提交)", "dim")
        cprint(f"  gc archive              : {t_gc:8.2f}s  (GC 归档)", "dim")
        if t_other > 0.01:
            cprint(f"  other                   : {t_other:8.2f}s  (其他开销)", "dim")
        cprint(f"  total                   : {duration:8.2f}s", "cyan", bold=True)
        cprint("──────────────────────────────────────", "cyan")

        # P13: 暴露阶段耗时数据到实例，供 perf test 读取做回归基线对比
        self._stage_timings = {
            "register": t_register,
            "parse": t_parse,
            "symbol_write": t_versions,
            "stdlib_import": t_stdlib,
            "call_resolve_write": t_call_resolve,
            "depth": t_depth,
            "fts_rebuild": t_fts,
            "commit": t_commit,
            "gc_archive": t_gc,
            "total": duration,
            "files_total": total,
            "files_parsed": parsed_new,
            "files_unchanged": unchanged,
            # P23.7: 暴露 skipped/failed 计数，避免基准脚本误算
            # (files_total - files_parsed - files_unchanged 把 skipped 算成 failed)
            "files_skipped": skipped,
            "files_failed": failed,
        }
    

    # ---- P8: FTS5 触发器延后重建 ----

    def _disable_fts_triggers(self) -> bool:
        """禁用 symbols_fts 的 3 个同步触发器，返回是否成功禁用。

        在 full build 期间禁用触发器，避免每个 symbol INSERT/UPDATE 都同步维护
        trigram FTS 索引（写放大）。批量写完后调用 _rebuild_and_enable_fts() 重建。

        Returns:
            True 表示触发了禁用（需要后续重建），False 表示 FTS 表不存在或已禁用
        """
        try:
            # 检查 symbols_fts 表是否存在
            cur = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='symbols_fts'"
            )
            if not cur.fetchone():
                return False
            # DROP 3 个触发器（IF EXISTS 确保幂等）
            self.conn.execute("DROP TRIGGER IF EXISTS symbols_fts_ai")
            self.conn.execute("DROP TRIGGER IF EXISTS symbols_fts_ad")
            self.conn.execute("DROP TRIGGER IF EXISTS symbols_fts_au")
            return True
        except Exception:
            return False

    def _rebuild_and_enable_fts(self):
        """一次性重建 FTS5 索引并重新启用同步触发器。

        在 _disable_fts_triggers() 之后调用，确保 FTS 索引与 symbols 表一致。
        """
        try:
            # 重建 FTS 索引（从 symbols 表重新构建全文索引）
            self.conn.execute("INSERT INTO symbols_fts(symbols_fts) VALUES('rebuild')")
            # 重新创建同步触发器
            self.conn.execute("""
                CREATE TRIGGER IF NOT EXISTS symbols_fts_ai AFTER INSERT ON symbols BEGIN
                    INSERT INTO symbols_fts(rowid, name, qualified_name)
                    VALUES (new.id, new.name, new.qualified_name);
                END
            """)
            self.conn.execute("""
                CREATE TRIGGER IF NOT EXISTS symbols_fts_ad AFTER DELETE ON symbols BEGIN
                    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name)
                    VALUES ('delete', old.id, old.name, old.qualified_name);
                END
            """)
            self.conn.execute("""
                CREATE TRIGGER IF NOT EXISTS symbols_fts_au AFTER UPDATE ON symbols BEGIN
                    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name)
                    VALUES ('delete', old.id, old.name, old.qualified_name);
                    INSERT INTO symbols_fts(rowid, name, qualified_name)
                    VALUES (new.id, new.name, new.qualified_name);
                END
            """)
        except Exception:
            pass

    def rebuild_fts_index(self) -> dict:
        """P29：独立重建 FTS5 索引（公开方法，供 CLI `cw fts rebuild` 调用）

        场景：refresh 中断后 symbols_fts 为空，search 返回 0 结果。
        此方法从 symbols 表全量重建 FTS5 索引，并确保同步触发器存在。

        Returns:
            dict: {
                "success": bool,
                "symbols_count": int,   # symbols 表行数
                "fts_rows": int,        # 重建后 FTS5 索引行数
                "triggers_recreated": int,  # 重建的触发器数
                "elapsed": float,       # 耗时（秒）
                "error": str,           # 失败时的错误信息
            }
        """
        import time
        t0 = time.time()
        result = {
            "success": False,
            "symbols_count": 0,
            "fts_rows": 0,
            "triggers_recreated": 0,
            "elapsed": 0.0,
            "error": "",
        }
        try:
            # 检查 symbols_fts 表是否存在
            cur = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='symbols_fts'"
            )
            if not cur.fetchone():
                result["error"] = "symbols_fts 表不存在（数据库版本过低或未初始化）"
                result["elapsed"] = time.time() - t0
                return result

            # 统计 symbols 表行数
            cur = self.conn.execute("SELECT COUNT(*) FROM symbols")
            result["symbols_count"] = cur.fetchone()[0]

            # 重建 FTS5 索引
            self.conn.execute("INSERT INTO symbols_fts(symbols_fts) VALUES('rebuild')")

            # 统计重建后 FTS5 行数
            cur = self.conn.execute("SELECT COUNT(*) FROM symbols_fts")
            result["fts_rows"] = cur.fetchone()[0]

            # 重建触发器（确保增量维护生效）
            self.conn.execute("DROP TRIGGER IF EXISTS symbols_fts_ai")
            self.conn.execute("DROP TRIGGER IF EXISTS symbols_fts_ad")
            self.conn.execute("DROP TRIGGER IF EXISTS symbols_fts_au")
            self.conn.execute("""
                CREATE TRIGGER symbols_fts_ai AFTER INSERT ON symbols BEGIN
                    INSERT INTO symbols_fts(rowid, name, qualified_name)
                    VALUES (new.id, new.name, new.qualified_name);
                END
            """)
            self.conn.execute("""
                CREATE TRIGGER symbols_fts_ad AFTER DELETE ON symbols BEGIN
                    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name)
                    VALUES ('delete', old.id, old.name, old.qualified_name);
                END
            """)
            self.conn.execute("""
                CREATE TRIGGER symbols_fts_au AFTER UPDATE ON symbols BEGIN
                    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name)
                    VALUES ('delete', old.id, old.name, old.qualified_name);
                    INSERT INTO symbols_fts(rowid, name, qualified_name)
                    VALUES (new.id, new.name, new.qualified_name);
                END
            """)
            result["triggers_recreated"] = 3
            self.conn.commit()
            result["success"] = True
        except Exception as e:
            result["error"] = str(e)
        result["elapsed"] = time.time() - t0
        return result

    def get_fts_status(self) -> dict:
        """P29：查询 FTS5 索引状态（供 CLI `cw fts status` 调用）

        Returns:
            dict: {
                "exists": bool,        # symbols_fts 表是否存在
                "symbols_count": int,  # symbols 表行数
                "fts_rows": int,       # FTS5 索引行数
                "triggers": list,      # 已存在的同步触发器列表
                "consistent": bool,    # fts_rows == symbols_count
            }
        """
        result = {
            "exists": False,
            "symbols_count": 0,
            "fts_rows": 0,
            "triggers": [],
            "consistent": False,
        }
        try:
            cur = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='symbols_fts'"
            )
            if not cur.fetchone():
                return result
            result["exists"] = True

            cur = self.conn.execute("SELECT COUNT(*) FROM symbols")
            result["symbols_count"] = cur.fetchone()[0]

            cur = self.conn.execute("SELECT COUNT(*) FROM symbols_fts")
            result["fts_rows"] = cur.fetchone()[0]

            cur = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'symbols_fts_%'"
            )
            result["triggers"] = [row[0] for row in cur.fetchall()]

            result["consistent"] = (result["fts_rows"] == result["symbols_count"])
        except Exception:
            pass
        return result

    def _load_file_result_from_db(self, file_instance_id: int, file_version_id: int,
        rel_path: str, abs_path: str, module_path: str) -> Optional[Dict]:
        """从数据库加载已解析的文件结果（增量构建用）"""
        try:
            # 加载文件版本的基本信息
            cur = self.conn.execute(
                "SELECT content_hash, total_lines FROM file_versions WHERE id = ?",
                (file_version_id,)
            )
            fv_row = cur.fetchone()
            if not fv_row:
                return None

            # 加载该版本的所有符号（含 symbol_contents 详情）
            cur = self.conn.execute("""
                SELECT sv.id, sv.symbol_hash, sv.qualified_name, sv.start_line, sv.end_line,
                       sv.module_path, sv.depth, sv.is_deleted,
                       sc.name, sc.kind, sc.content, sc.signature, sc.has_comment,
                       sc.comment_content as doc_comment
                FROM file_symbol_versions sv
                JOIN symbol_contents sc ON sv.symbol_hash = sc.content_hash
                WHERE sv.file_version_id = ? AND sv.is_deleted = 0
            """, (file_version_id,))
            symbols = []
            for row in cur:
                sym = dict(row)
                # 从DB加载的符号没有 raw_calls（调用点信息），需要重新解析文件才能获取
                # 但对于符号索引来说已经足够
                sym["calls"] = []  # 调用点信息不在DB中，增量时其他文件的调用点来自call_versions
                sym.setdefault("issues", [])
                symbols.append(sym)

            # 从快照表加载调用关系（通过 symbols 表关联 file_instance_id）
            cur = self.conn.execute("""
                SELECT c.caller_name, c.caller_module, c.callee_name, c.callee_module,
                       c.callee_qualified, c.callee_file, c.callee_id, c.call_line, c.is_cross_file
                FROM calls c
                JOIN symbols s ON c.caller_id = s.id
                WHERE s.file_instance_id = ?
            """, (file_instance_id,))
            raw_calls = [dict(row) for row in cur]

            return {
                "abs_path": abs_path,
                "rel_path": rel_path,
                "module_path": module_path,
                "file_instance_id": file_instance_id,
                "file_version_id": file_version_id,
                "symbols": symbols,
                "raw_calls": raw_calls,
                "imports": [],  # imports 不存储，但不影响符号索引
                "content_hash": fv_row["content_hash"],
                "total_lines": fv_row["total_lines"],
                "inline_modules": [],
                "_from_db": True,  # 标记来自DB（跳过写入）
            }
        except Exception as e:
            return None


    def _restore_symbol_snapshots(self, file_instance_id: int, result: Dict):
        """从DB加载的结果恢复符号快照到 symbols 表（增量构建用）

        如果快照已存在则跳过，否则从 symbol_contents 和 file_symbol_versions 恢复。
        """
        # 检查是否已有快照
        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM symbols WHERE file_instance_id = ?",
            (file_instance_id,)
        )
        if cur.fetchone()["cnt"] > 0:
            return  # 已有快照，不需要恢复

        # 从 file_symbol_versions + symbol_contents 恢复
        for sym in result.get("symbols", []):
            qname = sym.get("qualified_name", "")
            name = sym.get("name", "")
            kind = sym.get("kind", "fn")
            start_line = sym.get("start_line", 0)
            end_line = sym.get("end_line", 0)
            module_path = sym.get("module_path", result.get("module_path", ""))
            content_hash = sym.get("symbol_hash", "")
            depth = sym.get("depth", -1)
            has_comment = sym.get("has_comment", 0)

            self.conn.execute("""
                INSERT OR IGNORE INTO symbols
                (file_instance_id, name, kind, qualified_name, module_path,
                 start_line, end_line, symbol_hash, depth, has_comment)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (file_instance_id, name, kind, qname, module_path,
                  start_line, end_line, content_hash, depth, has_comment))


    def _build_call_graph_multi_lang(self, file_results: Dict[str, Dict[str, Any]]):
        """多语言调用关系构建（多级解析策略）

        解析策略（按优先级）：
        1. 精确匹配：callee_module.callee_name 完全匹配 qualified_name
        2. import 解析：通过文件的 import 列表将 callee_module 映射到实际模块路径
        3. 简名唯一匹配：callee_name 在全局符号表中唯一存在
        4. 简名同文件匹配：callee_name 在当前文件中存在

        性能优化（P0/P3）：
        - 后缀反向索引：策略 2/4 的后缀匹配从 O(M*N) 优化为 O(M*K)
        - external_symbols 批量加载：策略 5 从 M 次 DB 查询优化为 1 次批量加载
        """
        total_calls = 0
        resolved_count = 0

        # ---- 第一阶段：构建符号索引 + 后缀反向索引 ----
        # qualified_name -> {file, symbol}
        all_symbols_map: Dict[str, Dict] = {}
        # 简名 -> [qualified_name, ...]（用于 fallback 匹配）
        name_index: Dict[str, List[str]] = defaultdict(list)
        # 每个文件的符号简名集合（用于同文件匹配存在性判断）
        file_symbols: Dict[str, Set[str]] = defaultdict(set)
        # P27 优化：每个文件的 简名→qualified_name 映射（用于策略3/4的 O(1) 查找）
        # 替代原策略3多候选 O(K) 遍历：1M 规模下 K=10000，O(M×K)=10^10 次操作
        # 内存开销：与 file_symbols 同量级（每符号存一个 qname 引用），20 万符号约 ~10MB
        file_local_qname: Dict[str, Dict[str, str]] = defaultdict(dict)
        # P0 优化：后缀反向索引
        # 后缀（含前导点，如 ".module.name"）-> [qualified_name, ...]
        # 用于策略 2/4 的后缀匹配，从 O(N) 遍历优化为 O(K) 索引查找
        # 内存开销：平均每 qname 4 段 → 4 个索引项，20 万符号约 80 万项/40MB
        suffix_index: Dict[str, List[str]] = defaultdict(list)

        for rel_path, result in file_results.items():
            for sym in result.get("symbols", []):
                qname = sym.get("qualified_name", "")
                if not qname:
                    continue
                all_symbols_map[qname] = {"file": rel_path, "symbol": sym}
                # 简名 = qualified_name 的最后一段（支持 . 和 :: 分隔符）
                norm_qname = qname.replace("::", ".")
                parts = norm_qname.rsplit(".", 1)
                simple_name = parts[-1] if parts else qname
                name_index[simple_name].append(qname)
                file_symbols[rel_path].add(simple_name)
                # P27：构建 file-local qname 映射（简名→qname）
                # 注意：同文件内同名符号（如多类同名方法）后写覆盖前写，
                # 但这符合策略3"优先当前文件"的语义，且多候选时原本也只取第一个
                file_local_qname[rel_path][simple_name] = qname
                # P0：构建后缀索引（含前导点，与原 endswith 语义一致）
                # 例如 "com.foo.Bar.method" → 索引 ".method", ".Bar.method", ".foo.Bar.method", ".com.foo.Bar.method"
                norm_parts = norm_qname.split(".")
                for i in range(len(norm_parts)):
                    suffix = "." + ".".join(norm_parts[i:])
                    suffix_index[suffix].append(qname)

        # ---- P3+P7 合并：批量加载 external_symbols 到内存 + 构建 qname_id_map ----
        # 策略 5 原来对每个未解析调用执行 DB 查询，改为内存查找
        # P7: 同时构建 qname_id_map（避免第二次全表扫描 external_symbols）
        # 优化：只加载解析需要的 4 列（id, symbol_name, qualified_name, package_name），
        #       跳过 package_version/signature/docstring（397k 行 × 3 列 → 节省 ~50% 内存和时间）
        ext_by_qname: Dict[str, Dict] = {}
        ext_by_name: Dict[str, List[Dict]] = defaultdict(list)
        qname_id_map: Dict[str, int] = {}
        try:
            cur = self.conn.execute(
                "SELECT id, symbol_name, qualified_name, package_name FROM external_symbols"
            )
            for row in cur:
                d = {"id": row[0], "symbol_name": row[1],
                     "qualified_name": row[2], "package_name": row[3]}
                qn = d.get("qualified_name", "")
                if qn:
                    ext_by_qname[qn] = d
                    qname_id_map[qn] = -d["id"]  # P7: 外部符号用负 id
                ext_by_name[d.get("symbol_name", "")].append(d)
        except Exception:
            # external_symbols 表可能不存在（旧版本 DB）
            pass

        # ---- P7 优化：一次性构建 qname_id_map（项目符号）+ file_sym_id_map ----
        # 替代 _write_calls_db 内每文件全表扫描 symbols
        file_sym_id_map: Dict[int, Dict[str, int]] = defaultdict(dict)
        cur = self.conn.execute("SELECT id, name, qualified_name, file_instance_id FROM symbols")
        for row in cur:
            qn = row["qualified_name"]
            if qn:
                qname_id_map[qn] = row["id"]
            file_sym_id_map[row["file_instance_id"]][row["name"]] = row["id"]

        # ---- 第二阶段：构建 import 索引 ----
        # file_path -> {alias/module_name: full_module_path}
        file_imports: Dict[str, Dict[str, str]] = {}
        for rel_path, result in file_results.items():
            imports = result.get("imports", [])
            import_map = {}
            for imp in imports:
                module = imp.get("module", "")
                if not module:
                    continue
                # Go: "github.com/user/project/pkg" → 包名 "pkg"
                # Java: "com.tokenslim.sdk.TokenSlimClient" → 类名 "TokenSlimClient"
                # C/C++: "stdio.h" → "stdio"
                if "/" in module:
                    parts = module.rstrip("/").split("/")
                    alias = parts[-1]
                elif "." in module:
                    parts = module.split(".")
                    alias = parts[-1]
                else:
                    alias = module.replace(".h", "").replace(".hpp", "")
                import_map[alias] = module
            file_imports[rel_path] = import_map

        # ---- 第三阶段：解析调用关系 ----
        # P7 优化：批量收集 calls/call_versions 记录，循环后 executemany 一次性写入
        calls_to_insert: List[tuple] = []          # calls 表的 INSERT 元组
        call_versions_to_insert: List[tuple] = []  # call_versions 表的 INSERT 元组
        changed_file_instance_ids: List[int] = []  # 需要删除旧 calls 的文件实例

        for rel_path, result in file_results.items():
            # 如果结果来自DB，calls 已在 calls 表中，无需 DELETE+重写（消除写放大）
            if result.get("_from_db"):
                existing_calls = result.get("raw_calls", [])
                total_calls += len(existing_calls)
                resolved_count += sum(1 for c in existing_calls if c.get("callee_qualified"))
                continue

            raw_calls = result.get("raw_calls", [])
            calls = []
            current_imports = file_imports.get(rel_path, {})

            for raw in raw_calls:
                callee_qname = ""
                callee_file = ""
                callee_id = 0
                is_cross = 0

                callee_name = raw.get("callee_name", "")
                callee_module = raw.get("callee_module", "")

                if not callee_name:
                    calls.append(self._make_call_entry(raw, "", "", 0, 0))
                    continue

                # 策略 1：精确匹配 module.name
                if callee_module:
                    test_qname = f"{callee_module}.{callee_name}"
                    if test_qname in all_symbols_map:
                        callee_qname = test_qname
                        callee_file = all_symbols_map[test_qname]["file"]
                        callee_id = all_symbols_map[test_qname]["symbol"].get("id", 0)
                        if callee_file != rel_path:
                            is_cross = 1

                # 策略 2：通过 import 映射 module 后匹配
                if not callee_qname and callee_module:
                    # callee_module 可能是 import 别名，尝试映射到实际模块路径
                    if callee_module in current_imports:
                        # 尝试用 import 的完整路径的末段作为模块名
                        full_mod = current_imports[callee_module]
                        # 构建可能的 qualified_name
                        mod_parts = full_mod.replace("/", ".").split(".")
                        # 尝试最后一段 + 函数名
                        for i in range(len(mod_parts)):
                            test_mod = ".".join(mod_parts[i:])
                            test_qname = f"{test_mod}.{callee_name}"
                            if test_qname in all_symbols_map:
                                callee_qname = test_qname
                                callee_file = all_symbols_map[test_qname]["file"]
                                callee_id = all_symbols_map[test_qname]["symbol"].get("id", 0)
                                if callee_file != rel_path:
                                    is_cross = 1
                                break

                    # P0 优化：后缀索引替代全表遍历（O(N) → O(K)，K 为后缀候选数）
                    if not callee_qname:
                        suffix = f".{callee_module}.{callee_name}"
                        for qname in suffix_index.get(suffix, []):
                            callee_qname = qname
                            callee_file = all_symbols_map[qname]["file"]
                            callee_id = all_symbols_map[qname]["symbol"].get("id", 0)
                            if callee_file != rel_path:
                                is_cross = 1
                            break

                # 策略 3：简名唯一匹配（即使 callee_module 为空也尝试）
                if not callee_qname and callee_name in name_index:
                    candidates = name_index[callee_name]
                    if len(candidates) == 1:
                        callee_qname = candidates[0]
                        callee_file = all_symbols_map[callee_qname]["file"]
                        callee_id = all_symbols_map[callee_qname]["symbol"].get("id", 0)
                        if callee_file != rel_path:
                            is_cross = 1
                    elif len(candidates) > 1:
                        # P27 优化：file-local qname dict 查找，O(1) 替代 O(K) 遍历
                        # 1M 规模下 K=10000，原 O(M×K)=10^10 次操作，现降为 O(M)
                        local_qname = file_local_qname.get(rel_path, {}).get(callee_name)
                        if local_qname:
                            callee_qname = local_qname
                            callee_file = rel_path
                            callee_id = all_symbols_map[local_qname]["symbol"].get("id", 0)
                        # 如果当前文件没有，且 callee_module 匹配某个候选的父级
                        # 此时仍需 O(K) 遍历，但此分支触发率极低（本地无同名符号才进入）
                        if not callee_qname and callee_module:
                            for qname in candidates:
                                # 支持 :: 和 . 分隔符
                                norm_qname = qname.replace("::", ".")
                                parts = norm_qname.rsplit(".", 1)
                                if len(parts) > 1 and parts[0].endswith(callee_module):
                                    callee_qname = qname
                                    callee_file = all_symbols_map[qname]["file"]
                                    callee_id = all_symbols_map[qname]["symbol"].get("id", 0)
                                    if callee_file != rel_path:
                                        is_cross = 1
                                    break

                # 策略 4：同文件简名匹配（P27 优化：file_local_qname dict O(1) 查找）
                # 原 P0 用 suffix_index O(K) 遍历筛选当前文件，现直接 dict 取值
                if not callee_qname and callee_name in file_symbols.get(rel_path, set()):
                    local_qname = file_local_qname.get(rel_path, {}).get(callee_name)
                    if local_qname:
                        callee_qname = local_qname
                        callee_file = rel_path
                        callee_id = all_symbols_map[local_qname]["symbol"].get("id", 0)

                # 策略 5：查找外部符号表（P3 优化：内存查找替代逐条 DB 查询）
                if not callee_qname:
                    if callee_module:
                        test_qname = f"{callee_module}.{callee_name}"
                        ext_sym = ext_by_qname.get(test_qname)
                        if ext_sym:
                            callee_qname = test_qname
                            callee_file = f"external://{ext_sym['package_name']}"
                            callee_id = -ext_sym["id"]
                            is_cross = 1
                    else:
                        ext_syms = ext_by_name.get(callee_name, [])
                        if len(ext_syms) == 1:
                            ext_sym = ext_syms[0]
                            callee_qname = ext_sym["qualified_name"]
                            callee_file = f"external://{ext_sym['package_name']}"
                            callee_id = -ext_sym["id"]
                            is_cross = 1

                if callee_qname:
                    resolved_count += 1

                calls.append(self._make_call_entry(
                    raw, callee_qname, callee_file, callee_id, is_cross
                ))

            # P7: 收集到批量列表（不再逐条 INSERT）
            fi_id = result["file_instance_id"]
            changed_file_instance_ids.append(fi_id)
            sym_id_map_fi = file_sym_id_map.get(fi_id, {})

            # 收集 calls 表 INSERT 元组（caller_id 多级 fallback 与 _write_calls_db 一致）
            for call in calls:
                caller_id = 0
                caller_qname = call.get("caller_qualified", "")
                caller_name_raw = call.get("caller_name", "")
                if caller_qname and caller_qname in qname_id_map:
                    caller_id = qname_id_map[caller_qname]
                elif caller_name_raw and caller_name_raw in qname_id_map:
                    caller_id = qname_id_map[caller_name_raw]
                elif caller_name_raw:
                    simple_name = caller_name_raw
                    for sep in ("::", ".", "#"):
                        if sep in simple_name:
                            simple_name = simple_name.rsplit(sep, 1)[-1]
                    caller_id = sym_id_map_fi.get(simple_name, 0)
                if caller_id == 0:
                    continue
                callee_q = call.get("callee_qualified", "")
                callee_id_resolved = qname_id_map.get(callee_q, 0) if callee_q else 0
                calls_to_insert.append((
                    caller_id, caller_name_raw, call.get("caller_module", ""),
                    call["callee_name"], call.get("callee_module", ""),
                    callee_q, call.get("callee_file", ""),
                    callee_id_resolved, call.get("call_line", 0),
                    call.get("is_cross_file", 0),
                ))

            # 收集 call_versions 表 INSERT 元组
            fn_hash_map = {}
            for sym in result["symbols"]:
                if sym["kind"] in ("fn", "test_fn"):
                    fn_hash_map[sym["qualified_name"]] = sym["content_hash"]
            for inline_mod in result.get("inline_modules", []):
                for sym in inline_mod["symbols"]:
                    if sym["kind"] in ("fn", "test_fn"):
                        fn_hash_map[sym["qualified_name"]] = sym["content_hash"]
            mod_path = result.get("module_path", "")
            fv_id = result["file_version_id"]
            for call in calls:
                caller_qualified = call.get("caller_qualified", "")
                if not caller_qualified:
                    if call.get("caller_name"):
                        caller_qualified = f"{mod_path}::{call['caller_name']}"
                caller_hash = fn_hash_map.get(caller_qualified, "")
                call_versions_to_insert.append((
                    fv_id, caller_qualified, caller_hash,
                    call["callee_name"], call["callee_module"], call["callee_qualified"],
                    call["callee_file"], call["call_line"], call["is_cross_file"],
                ))
            total_calls += len(calls)

        # ---- P7: 批量写入 calls + call_versions ----
        # 批量删除已变更文件的旧 calls（分批 IN 子句，避免 SQLite 999 参数限制）
        if changed_file_instance_ids:
            BATCH = 500
            for i in range(0, len(changed_file_instance_ids), BATCH):
                chunk = changed_file_instance_ids[i:i + BATCH]
                placeholders = ",".join("?" * len(chunk))
                self.conn.execute(
                    f"DELETE FROM calls WHERE caller_id IN ("
                    f"SELECT id FROM symbols WHERE file_instance_id IN ({placeholders}))",
                    chunk,
                )
        if calls_to_insert:
            self.conn.executemany(
                """INSERT INTO calls
                   (caller_id, caller_name, caller_module, callee_name,
                    callee_module, callee_qualified, callee_file, callee_id,
                    call_line, is_cross_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                calls_to_insert,
            )
        if call_versions_to_insert:
            self.conn.executemany(
                """INSERT INTO call_versions
                   (file_version_id, caller_qualified, caller_hash, callee_name,
                    callee_module, callee_qualified, callee_file, call_line, is_cross_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                call_versions_to_insert,
            )

        print(t("cli.messages.db_build_calls_summary", total=total_calls, resolved=resolved_count, percent=resolved_count * 100 // total_calls if total_calls else 0))


    def _make_call_entry(self, raw: Dict, callee_qname: str, callee_file: str,
                         callee_id: int, is_cross: int) -> Dict:
        """构造调用关系记录"""
        return {
            "caller_name": raw.get("caller_name", ""),
            "caller_qualified": raw.get("caller_qualified", ""),
            "caller_module": raw.get("caller_module", ""),
            "callee_name": raw.get("callee_name", ""),
            "callee_module": raw.get("callee_module", ""),
            "callee_qualified": callee_qname,
            "callee_file": callee_file,
            "callee_id": callee_id,
            "call_line": raw.get("call_line", 0),
            "is_cross_file": is_cross,
        }


    def _infer_module_path_generic(self, rel_path: str, lang: str) -> str:
        """通用模块路径推断

        Args:
            rel_path: 相对路径
            lang: 语言

        Returns:
            模块路径字符串
        """
        path = rel_path.replace("\\", "/")

        if lang == "rust":
            return self._infer_module_path(rel_path)

        # 去掉扩展名
        for ext in get_supported_extensions():
            if path.endswith(ext):
                path = path[: -len(ext)]
                break

        # 去掉常见的 src/ 前缀
        for prefix in ("src/", "lib/", "app/", "main/"):
            if path.startswith(prefix):
                path = path[len(prefix):]
                break

        # 去掉 index/__init__ 等入口文件名
        basename = os.path.basename(path)
        if basename in ("index", "__init__", "mod"):
            dirname = os.path.dirname(path)
            if dirname:
                path = dirname
            else:
                path = "(root)"

        # 用点分隔
        return path.replace("/", ".")


    def build_call_graph(self, file_results: Dict[str, Dict[str, Any]], crate_name: str = ""):
        """构建调用关系图

        Args:
            file_results: 文件解析结果字典
            crate_name: crate 名称
        """
        if not crate_name:
            crate_name = self._detect_crate_name()

        self.call_resolver.crate_name = crate_name
        self.call_resolver.lib_crate_alias = "lib"
        self.call_resolver.load_all_symbols(file_results)
        total_calls = 0
        for rel_path, result in file_results.items():
            calls = self._resolve_file_calls(rel_path, result)
            self._write_calls_db(result["file_instance_id"], calls)
            self._save_calls_for_version(result["file_version_id"], calls, result)
            total_calls += len(calls)

        print(t("cli.messages.db_build_calls_simple", total=total_calls))


    def _detect_crate_name(self) -> str:
        """从 Cargo.toml 检测 crate 名称"""
        cargo_toml = os.path.join(self.workspace_root, "Cargo.toml")
        if os.path.exists(cargo_toml):
            try:
                import re
                content = read_file_text(cargo_toml)
                m = re.search(r'name\s*=\s*"([^"]+)"', content)
                if m:
                    return m.group(1)
            except Exception:
                pass
        return "tokenslim"


    def _register_file_db(self, abs_path: str, module_path: str) -> int:
        """注册文件到数据库（使用 file_instances 表）

        Args:
            abs_path: 绝对路径
            module_path: 模块路径

        Returns:
            file_instance_id
        """
        ws_id = self._get_active_workspace_id()
        rel_path = norm_path(os.path.relpath(abs_path, self.workspace_root))
        mtime = os.path.getmtime(abs_path)

        cur = self.conn.execute(
            "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = ?",
            (ws_id, rel_path),
        )
        row = cur.fetchone()

        if row:
            self.conn.execute(
                "UPDATE file_instances SET mtime = ?, module_path = ?, status = 'pending' WHERE id = ?",
                (mtime, module_path, row["id"]),
            )
            return row["id"]
        else:
            cur = self.conn.execute(
                """INSERT INTO file_instances
                   (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
                   VALUES (?, ?, ?, '', ?, 0, 0, 'pending', ?)""",
                (ws_id, rel_path, norm_path(abs_path), mtime, module_path),
            )
            return cur.lastrowid


    def _get_file_version(self, file_instance_id: int) -> Optional[sqlite3.Row]:
        """获取文件的最新版本"""
        cur = self.conn.execute(
            "SELECT * FROM file_versions WHERE file_instance_id = ? ORDER BY version_num DESC LIMIT 1",
            (file_instance_id,),
        )
        return cur.fetchone()


    def _save_file_version(self, file_instance_id: int, result: Dict[str, Any]) -> int:
        """创建新的文件版本，如果内容没变则返回现有版本号"""
        content_hash = result["content_hash"]
        mtime = os.path.getmtime(result["abs_path"]) if "abs_path" in result else 0
        total_lines = result["total_lines"]
        parsed_at = time.time()
        language = detect_language_from_path(result.get("rel_path", ""))

        # 确保 file_contents 中有记录
        self.conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) VALUES (?, ?, ?, ?)",
            (content_hash, language, total_lines, parsed_at),
        )

        latest = self._get_file_version(file_instance_id)
        if latest and latest["content_hash"] == content_hash:
            # 更新 mtime
            self.conn.execute(
                "UPDATE file_versions SET mtime = ? WHERE id = ?",
                (mtime, latest["id"]),
            )
            # 更新 file_instances 的 current_content_hash 和 last_parsed
            self.conn.execute(
                "UPDATE file_instances SET current_content_hash = ?, last_parsed = ?, total_lines = ?, mtime = ? WHERE id = ?",
                (content_hash, parsed_at, total_lines, mtime, file_instance_id),
            )
            # 更新 ast_cache 元数据（即使内容未变，记录 parsed_at 用于跨进程缓存判断）
            self._update_ast_cache(latest["id"], result, content_hash, parsed_at)
            return latest["id"]

        # 计算符号 diff（与上一版本比较）
        prev_version_id = latest["id"] if latest else None

        if latest:
            self.conn.execute(
                "UPDATE file_versions SET is_current = 0 WHERE id = ?",
                (latest["id"],),
            )
            version_num = latest["version_num"] + 1
        else:
            version_num = 1

        cur = self.conn.execute(
            """INSERT INTO file_versions
               (file_instance_id, version_num, content_hash, mtime, total_lines, parsed_at, is_current, is_deleted)
               VALUES (?, ?, ?, ?, ?, ?, 1, 0)""",
            (file_instance_id, version_num, content_hash, mtime, total_lines, parsed_at),
        )
        new_version_id = cur.lastrowid

        # 更新 file_instances 的 current_content_hash 和 last_parsed
        self.conn.execute(
            "UPDATE file_instances SET current_content_hash = ?, last_parsed = ?, total_lines = ?, mtime = ? WHERE id = ?",
            (content_hash, parsed_at, total_lines, mtime, file_instance_id),
        )

        # 写入 ast_cache 元数据（v28 新增：AST 增量解析元信息）
        self._update_ast_cache(new_version_id, result, content_hash, parsed_at)

        # 如果有前一版本，计算符号 diff 并设置删除标记
        if prev_version_id:
            self._compute_and_apply_symbol_diff(prev_version_id, new_version_id)

        return new_version_id

    def _update_ast_cache(
        self,
        file_version_id: int,
        result: Dict[str, Any],
        content_hash: str,
        parsed_at: float,
    ) -> None:
        """更新 file_versions.ast_cache 字段（v28 新增）

        存储格式：JSON 编码的元数据字节流
        {
            "content_hash": str,         # 上次解析的 content_hash
            "parsed_at": float,          # 上次解析时间
            "incremental": bool,         # 是否走了增量解析路径
            "changed_ranges_count": int, # 变更区间数量（0 表示全量解析）
            "language": str,             # 语言标识
        }

        tree-sitter Tree 对象无法跨进程序列化，此处只存元数据。
        实际的 Tree 对象缓存在 BaseParser._tree_cache（进程内）。
        """
        import json
        metadata = {
            "content_hash": content_hash,
            "parsed_at": parsed_at,
            "incremental": result.get("incremental", False),
            "changed_ranges_count": len(result.get("changed_ranges", [])),
            "language": result.get("language", ""),
        }
        try:
            self.conn.execute(
                "UPDATE file_versions SET ast_cache = ? WHERE id = ?",
                (json.dumps(metadata).encode("utf-8"), file_version_id),
            )
        except sqlite3.OperationalError:
            # ast_cache 字段不存在（v27 库未迁移到 v28），降级为无操作
            pass

    def _read_ast_cache(self, file_instance_id: int) -> Optional[Dict[str, Any]]:
        """读取 file_versions.ast_cache 元数据（v28 新增）

        用于跨进程判断上次解析状态：
        - 若 ast_cache.content_hash 与当前文件 content_hash 相同，可跳过解析
        - 若 ast_cache.incremental 为 True，可复用增量解析状态

        Returns:
            元数据字典或 None（无缓存或字段不存在）
        """
        import json
        cur = self.conn.execute(
            "SELECT ast_cache FROM file_versions WHERE file_instance_id = ? AND is_current = 1 LIMIT 1",
            (file_instance_id,),
        )
        row = cur.fetchone()
        if not row or not row["ast_cache"]:
            return None
        try:
            return json.loads(row["ast_cache"].decode("utf-8") if isinstance(row["ast_cache"], bytes) else row["ast_cache"])
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None


    def _compute_symbol_diff(self, prev_version_id: int, curr_version_id: int) -> Dict:
        """计算两个版本之间的符号差异

        Args:
            prev_version_id: 上一版本 ID
            curr_version_id: 当前版本 ID

        Returns:
            {"added": [...], "removed": [...], "modified": [...]}
        """
        # 获取上一版本的符号
        cur = self.conn.execute(
            "SELECT symbol_hash, qualified_name, start_line, end_line FROM file_symbol_versions WHERE file_version_id = ?",
            (prev_version_id,),
        )
        prev_symbols = {row["qualified_name"]: dict(row) for row in cur}

        # 获取当前版本的符号
        cur = self.conn.execute(
            "SELECT symbol_hash, qualified_name, start_line, end_line FROM file_symbol_versions WHERE file_version_id = ?",
            (curr_version_id,),
        )
        curr_symbols = {row["qualified_name"]: dict(row) for row in cur}

        added = []
        removed = []
        modified = []

        all_names = set(prev_symbols.keys()) | set(curr_symbols.keys())

        for name in all_names:
            prev = prev_symbols.get(name)
            curr = curr_symbols.get(name)

            if prev and not curr:
                removed.append(prev)
            elif curr and not prev:
                added.append(curr)
            elif prev and curr and prev["symbol_hash"] != curr["symbol_hash"]:
                modified.append(curr)

        return {
            "added": added,
            "removed": removed,
            "modified": modified,
        }


    def _compute_and_apply_symbol_diff(self, prev_version_id: int, curr_version_id: int):
        """计算符号 diff 并应用删除标记

        为当前版本中不存在的符号（相对于上一版本）设置 is_deleted=1
        """
        # 获取上一版本的符号
        cur = self.conn.execute(
            "SELECT symbol_hash, qualified_name FROM file_symbol_versions WHERE file_version_id = ?",
            (prev_version_id,),
        )
        prev_symbols = {row["qualified_name"]: row["symbol_hash"] for row in cur}

        # 获取当前版本的符号
        cur = self.conn.execute(
            "SELECT id, symbol_hash, qualified_name FROM file_symbol_versions WHERE file_version_id = ?",
            (curr_version_id,),
        )
        curr_symbols = {row["qualified_name"]: row["symbol_hash"] for row in cur}
        curr_ids = {row["qualified_name"]: row["id"] for row in cur}

        # 找出删除的符号（上一版本有，当前版本没有）
        removed_names = set(prev_symbols.keys()) - set(curr_symbols.keys())

        # 对于删除的符号，在当前版本中插入标记为 is_deleted=1 的记录
        for name in removed_names:
            symbol_hash = prev_symbols[name]
            # 获取上一版本中的位置信息
            cur = self.conn.execute(
                "SELECT start_line, end_line, module_path, depth FROM file_symbol_versions WHERE file_version_id = ? AND qualified_name = ?",
                (prev_version_id, name),
            )
            prev_row = cur.fetchone()
            if prev_row:
                self.conn.execute(
                    """INSERT INTO file_symbol_versions
                       (file_version_id, symbol_hash, qualified_name, start_line, end_line, module_path, depth, is_deleted)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                    (curr_version_id, symbol_hash, name,
                     prev_row["start_line"], prev_row["end_line"],
                     prev_row["module_path"], prev_row["depth"]),
                )


    def _save_symbols_for_version(self, file_version_id: int, file_instance_id: int, result: Dict[str, Any]):
        """为文件版本创建所有符号版本（批量写入优化，相同 hash 只存一次）

        性能优化（原 N×3 次 SQL → 现 5 次 SQL）：
        1. 批量 INSERT OR IGNORE symbol_contents（content_hash 是 PK，自动去重）
        2. 批量 SELECT 已存在的 symbols（按 qualified_name IN (...) 一次查询）
        3. 批量 UPDATE 已存在的 symbols（executemany）
        4. 批量 INSERT 新增的 symbols（executemany）
        5. 批量 INSERT file_symbol_versions（executemany）

        整个操作在外层 build() 的事务中执行，无需显式 BEGIN/COMMIT。
        """
        all_symbols = list(result["symbols"])
        for inline_mod in result.get("inline_modules", []):
            all_symbols.extend(inline_mod["symbols"])

        if not all_symbols:
            return

        # 1. 补算 content_hash（多语言 parser 可能没计算，现场补算）
        for sym in all_symbols:
            if "content_hash" not in sym:
                sym["content_hash"] = compute_content_hash(sym.get("content", ""))

        # 2. 批量 INSERT OR IGNORE symbol_contents（content_hash 是 PK，自动去重）
        self.conn.executemany(
            """INSERT OR IGNORE INTO symbol_contents
               (content_hash, name, kind, content, signature, has_comment, comment_content, qualified_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(s["content_hash"], s["name"], s["kind"], s["content"],
              s["signature"], s["has_comment"], s.get("comment_content", ""),
              s["qualified_name"]) for s in all_symbols],
        )

        # 3. 批量查询已存在的 symbols（按 qualified_name 一次查询，避免 N 次 SELECT）
        qualified_names = [s["qualified_name"] for s in all_symbols]
        # 去重 qualified_name 避免重复占位符（同一文件可能有同名符号，罕见但安全处理）
        unique_qnames = list(set(qualified_names))
        placeholders = ",".join("?" * len(unique_qnames))
        cur = self.conn.execute(
            f"SELECT id, qualified_name FROM symbols WHERE qualified_name IN ({placeholders})",
            unique_qnames,
        )
        existing_map = {row["qualified_name"]: row["id"] for row in cur.fetchall()}

        # 4. 分离已存在（UPDATE）和新增（INSERT）的符号
        to_update = []
        to_insert = []
        for sym in all_symbols:
            qname = sym["qualified_name"]
            if qname in existing_map:
                to_update.append((
                    file_instance_id, sym["content_hash"], sym["name"], sym["kind"], sym["visibility"],
                    sym["start_line"], sym["end_line"], sym["start_col"], sym["end_col"],
                    sym["signature"], sym["has_comment"], sym["module_path"],
                    existing_map[qname],
                ))
            else:
                to_insert.append((
                    file_instance_id, sym["content_hash"], sym["name"], sym["kind"], sym["visibility"],
                    sym["start_line"], sym["end_line"],
                    sym["start_col"], sym["end_col"], sym["signature"], sym["has_comment"],
                    sym["module_path"], qname,
                ))

        # 5. 批量 UPDATE 已存在的 symbols
        if to_update:
            self.conn.executemany(
                """UPDATE symbols SET
                   file_instance_id = ?, symbol_hash = ?, name = ?, kind = ?, visibility = ?,
                   start_line = ?, end_line = ?, start_col = ?, end_col = ?,
                   signature = ?, has_comment = ?, module_path = ?
                   WHERE id = ?""",
                to_update,
            )

        # 6. 批量 UPSERT 新增的 symbols（ON CONFLICT 防止重复行）
        if to_insert:
            self.conn.executemany(
                """INSERT INTO symbols
                   (file_instance_id, symbol_hash, name, kind, visibility, start_line, end_line,
                    start_col, end_col, signature, has_comment, comment_status,
                    module_path, qualified_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                   ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET
                    symbol_hash = excluded.symbol_hash,
                    kind = excluded.kind,
                    visibility = excluded.visibility,
                    end_line = excluded.end_line,
                    start_col = excluded.start_col,
                    end_col = excluded.end_col,
                    signature = excluded.signature,
                    has_comment = excluded.has_comment,
                    module_path = excluded.module_path,
                    qualified_name = excluded.qualified_name""",
                to_insert,
            )

        # 7. 批量 INSERT file_symbol_versions
        self.conn.executemany(
            """INSERT INTO file_symbol_versions
               (file_version_id, symbol_hash, qualified_name, start_line, end_line, module_path, depth, is_deleted)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            [(file_version_id, s["content_hash"], s["qualified_name"],
              s["start_line"], s["end_line"], s["module_path"], -1) for s in all_symbols],
        )


    def _ensure_symbol_content(self, sym: Dict[str, Any]):
        """确保符号内容已存储（相同 hash 只存一次）

        优化：用 INSERT OR IGNORE 替代 SELECT-then-INSERT，省掉一次查询。
        symbol_contents 表的 content_hash 是 PRIMARY KEY，自动去重。
        """
        self.conn.execute(
            """INSERT OR IGNORE INTO symbol_contents
               (content_hash, name, kind, content, signature, has_comment, comment_content, qualified_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (sym["content_hash"], sym["name"], sym["kind"], sym["content"],
             sym["signature"], sym["has_comment"], sym.get("comment_content", ""),
             sym["qualified_name"]),
        )


    def _get_or_create_symbol(self, file_instance_id: int, sym: Dict[str, Any]) -> int:
        """获取或创建符号（通过 qualified_name 匹配）"""
        cur = self.conn.execute(
            "SELECT id FROM symbols WHERE qualified_name = ?",
            (sym["qualified_name"],),
        )
        row = cur.fetchone()
        if row:
            self.conn.execute(
                """UPDATE symbols SET 
                   file_instance_id = ?, symbol_hash = ?, name = ?, kind = ?, visibility = ?,
                   start_line = ?, end_line = ?, start_col = ?, end_col = ?,
                   signature = ?, has_comment = ?, module_path = ?
                   WHERE id = ?""",
                (file_instance_id, sym["content_hash"], sym["name"], sym["kind"], sym["visibility"],
                 sym["start_line"], sym["end_line"], sym["start_col"], sym["end_col"],
                 sym["signature"], sym["has_comment"], sym["module_path"], row["id"]),
            )
            return row["id"]
        else:
            cur = self.conn.execute(
                """INSERT INTO symbols
                   (file_instance_id, symbol_hash, name, kind, visibility, start_line, end_line,
                    start_col, end_col, signature, has_comment, comment_status,
                    module_path, qualified_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                   ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET
                    symbol_hash = excluded.symbol_hash,
                    kind = excluded.kind,
                    visibility = excluded.visibility,
                    end_line = excluded.end_line,
                    start_col = excluded.start_col,
                    end_col = excluded.end_col,
                    signature = excluded.signature,
                    has_comment = excluded.has_comment,
                    module_path = excluded.module_path,
                    qualified_name = excluded.qualified_name
                   RETURNING id""",
                (file_instance_id, sym["content_hash"], sym["name"], sym["kind"], sym["visibility"],
                 sym["start_line"], sym["end_line"],
                 sym["start_col"], sym["end_col"], sym["signature"], sym["has_comment"],
                 sym["module_path"], sym["qualified_name"]),
            )
            return cur.fetchone()[0]


    def _insert_symbol(self, file_instance_id: int, sym: Dict[str, Any]):
        """向 symbols 表插入单个符号记录

        若符号 dict 中没有 content_hash，则根据 content 字段自动计算。
        comment_status 初始化为 'pending'，后续由注释恢复流程更新。

        Args:
            file_instance_id: 所属文件实例 ID
            sym: 符号信息字典，需包含 name/kind/visibility/start_line/end_line/
                 start_col/end_col/signature/has_comment/module_path/qualified_name
        """
        if "content_hash" not in sym:
            sym["content_hash"] = compute_content_hash(sym.get("content", ""))
        self.conn.execute(
            """INSERT INTO symbols
               (file_instance_id, symbol_hash, name, kind, visibility, start_line, end_line,
                start_col, end_col, signature, has_comment, comment_status,
                module_path, qualified_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
               ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET
                symbol_hash = excluded.symbol_hash,
                kind = excluded.kind,
                visibility = excluded.visibility,
                end_line = excluded.end_line,
                start_col = excluded.start_col,
                end_col = excluded.end_col,
                signature = excluded.signature,
                has_comment = excluded.has_comment,
                module_path = excluded.module_path,
                qualified_name = excluded.qualified_name""",
            (file_instance_id, sym["content_hash"], sym["name"], sym["kind"], sym["visibility"],
             sym["start_line"], sym["end_line"],
             sym["start_col"], sym["end_col"], sym["signature"], sym["has_comment"],
             sym["module_path"], sym["qualified_name"]),
        )


    def _resolve_file_calls(self, rel_path: str, result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """解析单个文件的所有调用"""
        resolved_calls = []
        module_path = result["module_path"]

        for raw_call in result["raw_calls"]:
            callee_info = self.call_resolver.resolve_call(
                rel_path,
                module_path,
                raw_call["callee_name"],
                raw_call.get("callee_module", raw_call.get("callee_path", "")),
                raw_call.get("callee_is_qualified", False),
            )

            resolved_call = {
                "caller_name": raw_call["caller_name"],
                "caller_module": module_path,
                "callee_name": raw_call["callee_name"],
                "callee_module": callee_info["module_path"] if callee_info else "",
                "callee_qualified": callee_info["qualified_name"] if callee_info else "",
                "callee_file": callee_info["file"] if callee_info else "",
                "callee_id": callee_info.get("id", 0) if callee_info else 0,
                "call_line": raw_call["call_line"],
                "is_cross_file": 1 if callee_info and callee_info["file"] != rel_path else 0,
            }
            resolved_calls.append(resolved_call)

        return resolved_calls


    def _write_calls_db(self, file_instance_id: int, calls: List[Dict[str, Any]]):
        """将文件的调用关系写入数据库，先删除旧快照再写入新关系

        Args:
            file_instance_id: 文件实例 ID
            calls: 调用关系字典列表，每项含 caller_qualified / callee_qualified 等字段
        """
        # 先删除该文件已有的调用快照
        self.conn.execute(
            "DELETE FROM calls WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id = ?)",
            (file_instance_id,)
        )

        # 同文件内 name -> id（最后一级 fallback，无法区分同名方法）
        sym_id_map = {}
        cur = self.conn.execute(
            "SELECT id, name FROM symbols WHERE file_instance_id = ?", (file_instance_id,)
        )
        for row in cur:
            sym_id_map[row["name"]] = row["id"]

        # 全局 qualified_name -> id（精确匹配，能区分同名方法）
        qname_id_map = {}
        cur = self.conn.execute("SELECT id, qualified_name FROM symbols")
        for row in cur:
            qname_id_map[row["qualified_name"]] = row["id"]

        # 外部符号 qualified_name -> -id（负值表示外部符号）
        cur = self.conn.execute("SELECT id, qualified_name FROM external_symbols")
        for row in cur:
            qname_id_map[row["qualified_name"]] = -row["id"]

        for call in calls:
            # 多级 fallback 策略匹配 caller_id：
            # 1. caller_qualified 精确匹配 qname_id_map（最优，带类名）
            # 2. caller_name 直接匹配 qname_id_map（无类语言：C/Go/HCL 等）
            # 3. 提取 caller_name 最后一段（简名）匹配同文件 sym_id_map（兜底）
            caller_id = 0
            caller_qname = call.get("caller_qualified", "")
            caller_name_raw = call.get("caller_name", "")

            if caller_qname and caller_qname in qname_id_map:
                caller_id = qname_id_map[caller_qname]
            elif caller_name_raw and caller_name_raw in qname_id_map:
                caller_id = qname_id_map[caller_name_raw]
            elif caller_name_raw:
                # 提取简名（最后一段），兼容不同分隔符
                simple_name = caller_name_raw
                for sep in ("::", ".", "#"):
                    if sep in simple_name:
                        simple_name = simple_name.rsplit(sep, 1)[-1]
                caller_id = sym_id_map.get(simple_name, 0)

            if caller_id == 0:
                continue

            callee_id = qname_id_map.get(call["callee_qualified"], 0) if call.get("callee_qualified") else 0

            self.conn.execute(
                """INSERT INTO calls
                   (caller_id, caller_name, caller_module, callee_name,
                    callee_module, callee_qualified, callee_file, callee_id,
                    call_line, is_cross_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (caller_id, caller_name_raw, call.get("caller_module", ""),
                 call["callee_name"], call.get("callee_module", ""),
                 call.get("callee_qualified", ""), call.get("callee_file", ""),
                 callee_id, call.get("call_line", 0),
                 call.get("is_cross_file", 0)),
            )


    def _save_calls_for_version(self, file_version_id: int, calls: List[Dict[str, Any]], result: Dict[str, Any]):
        """为文件版本创建调用关系版本"""
        fn_hash_map = {}
        for sym in result["symbols"]:
            if sym["kind"] in ("fn", "test_fn"):
                fn_hash_map[sym["qualified_name"]] = sym["content_hash"]
        for inline_mod in result.get("inline_modules", []):
            for sym in inline_mod["symbols"]:
                if sym["kind"] in ("fn", "test_fn"):
                    fn_hash_map[sym["qualified_name"]] = sym["content_hash"]

        for call in calls:
            # 直接用 parser 输出的 caller_qualified（与 symbols.qualified_name 一致）
            # fallback：旧 parser 没有 caller_qualified 字段时拼装 module_path::caller_name
            caller_qualified = call.get("caller_qualified", "")
            if not caller_qualified:
                if call.get("caller_name"):
                    caller_qualified = f"{result['module_path']}::{call['caller_name']}"
                else:
                    caller_qualified = ""
            caller_hash = fn_hash_map.get(caller_qualified, "")

            self.conn.execute(
                """INSERT INTO call_versions
                   (file_version_id, caller_qualified, caller_hash, callee_name,
                    callee_module, callee_qualified, callee_file, call_line, is_cross_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (file_version_id, caller_qualified, caller_hash,
                 call["callee_name"], call["callee_module"], call["callee_qualified"],
                 call["callee_file"], call["call_line"], call["is_cross_file"]),
            )


    def _update_symbol_version_depths(self):
        """更新当前文件-符号关联的深度"""
        cur = self.conn.execute(
            """SELECT fsv.id, s.depth 
               FROM file_symbol_versions fsv
               JOIN symbols s ON fsv.qualified_name = s.qualified_name
               JOIN file_versions fv ON fsv.file_version_id = fv.id
               WHERE fv.is_current = 1 AND fsv.is_deleted = 0"""
        )
        updates = []
        for row in cur:
            updates.append((row["depth"], row["id"]))

        self.conn.executemany(
            "UPDATE file_symbol_versions SET depth = ? WHERE id = ?",
            updates,
        )


    def _build_depth(self):
        """计算每个函数的拓扑深度"""
        cur = self.conn.execute(
            "SELECT id, qualified_name FROM symbols WHERE kind IN ('fn', 'test_fn')"
        )
        all_fns = {row["id"]: row["qualified_name"] for row in cur}

        call_graph = defaultdict(list)
        cur = self.conn.execute(
            "SELECT caller_id, callee_id FROM calls WHERE callee_id > 0"
        )
        for row in cur:
            call_graph[row["caller_id"]].append(row["callee_id"])

        depth_cache = {}

        def compute_depth(fn_id: int, visited: Set[int]) -> int:
            """递归计算函数的调用深度，带缓存与环检测"""
            if fn_id in depth_cache:
                return depth_cache[fn_id]

            if fn_id in visited:
                return 0

            visited.add(fn_id)

            callees = call_graph.get(fn_id, [])
            if not callees:
                depth = 0
            else:
                max_callee_depth = 0
                for callee_id in callees:
                    d = compute_depth(callee_id, visited)
                    if d > max_callee_depth:
                        max_callee_depth = d
                depth = max_callee_depth + 1

            visited.remove(fn_id)
            depth_cache[fn_id] = depth
            return depth

        for fn_id in all_fns:
            compute_depth(fn_id, set())

        # P5 优化：executemany 批量更新替代逐个 UPDATE
        updates = [(depth, fn_id) for fn_id, depth in depth_cache.items()]
        self.conn.executemany(
            "UPDATE symbols SET depth = ? WHERE id = ?",
            updates,
        )

        self.conn.commit()

    # --------------------------------------------------------------------
    # 增量刷新
    # --------------------------------------------------------------------


    def refresh_file(self, file_path: str):
        """刷新单个文件（增量更新），支持多语言"""
        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path):
            return

        lang = detect_language_from_path(abs_path)
        if not lang:
            return

        rel_path = norm_path(os.path.relpath(abs_path, self.workspace_root))
        print(t("cli.messages.db_build_refresh", path=rel_path, lang=lang))

        self._refresh_file_internal(abs_path, rel_path, lang)
        # B-P7b: 失效 GraphStore 缓存（symbols/calls 已更新，下次查询重新加载）
        self._invalidate_graph_store()


    def _refresh_file_internal(self, abs_path: str, rel_path: str, lang: str = "rust"):
        """内部刷新逻辑（支持多语言）"""
        if lang == "rust":
            self._refresh_file_rust(abs_path, rel_path)
        else:
            self._refresh_file_generic(abs_path, rel_path, lang)


    def _refresh_file_rust(self, abs_path: str, rel_path: str):
        """Rust 文件刷新逻辑（增量方式）"""
        if not self.module_resolver.module_to_file:
            self.module_resolver.resolve_all(self.parser)

        module_path = self.module_resolver.get_file_module(rel_path)
        if not module_path:
            module_path = self._infer_module_path(rel_path)

        file_instance_id = self._register_file_db(abs_path, module_path)

        result = self.parser.parse_file(abs_path, module_path)
        result["abs_path"] = abs_path
        result["file_instance_id"] = file_instance_id
        result["module_path"] = module_path
        result["rel_path"] = rel_path
        result.setdefault("inline_modules", [])

        latest_fv = self._get_file_version(file_instance_id)
        if latest_fv and latest_fv["content_hash"] == result["content_hash"]:
            self.conn.execute(
                "UPDATE file_versions SET mtime = ? WHERE id = ?",
                (os.path.getmtime(abs_path), latest_fv["id"]),
            )
            self.conn.commit()
            return

        new_fv_id = self._save_file_version(file_instance_id, result)
        result["file_version_id"] = new_fv_id
        self._save_symbols_for_version(new_fv_id, file_instance_id, result)

        # 增量方式：从DB加载其他文件结果 + 新解析的当前文件
        all_file_results = self._collect_all_current_file_results()
        all_file_results[rel_path] = result

        # 只重算当前文件的调用关系（使用完整的符号索引）
        self._build_call_graph_multi_lang({rel_path: result} | {
            k: v for k, v in all_file_results.items() if k != rel_path
        })

        # 清理旧版本的调用关系
        if latest_fv:
            self.conn.execute(
                "DELETE FROM call_versions WHERE file_version_id = ?",
                (latest_fv["id"],),
            )

        self._build_depth()
        self._update_symbol_version_depths()
        self.conn.commit()


    def _refresh_file_generic(self, abs_path: str, rel_path: str, lang: str):
        """通用语言文件刷新逻辑（增量方式）"""
        from ..parsers import create_parser

        parser = create_parser(rel_path)
        if not parser:
            return

        module_path = self._infer_module_path_generic(rel_path, lang)
        file_instance_id = self._register_file_db(abs_path, module_path)

        try:
            result = parser.parse_file(abs_path, module_path)
        except Exception as e:
            print(t("cli.messages.db_build_refresh_fail", path=rel_path, error=e))
            return

        result["abs_path"] = abs_path
        result["file_instance_id"] = file_instance_id
        result["module_path"] = module_path
        result["rel_path"] = rel_path
        result.setdefault("inline_modules", [])

        latest_fv = self._get_file_version(file_instance_id)
        if latest_fv and latest_fv["content_hash"] == result.get("content_hash", ""):
            self.conn.execute(
                "UPDATE file_versions SET mtime = ? WHERE id = ?",
                (os.path.getmtime(abs_path), latest_fv["id"]),
            )
            self.conn.commit()
            return

        new_fv_id = self._save_file_version(file_instance_id, result)
        result["file_version_id"] = new_fv_id
        self._save_symbols_for_version(new_fv_id, file_instance_id, result)

        # 增量方式：从DB加载其他文件结果
        all_file_results = self._collect_all_current_file_results()
        all_file_results[rel_path] = result

        # 重算调用关系（符号索引来自DB+新文件，只写入变化文件的调用）
        self._build_call_graph_multi_lang(all_file_results)

        # 清理旧版本的调用关系
        if latest_fv:
            self.conn.execute(
                "DELETE FROM call_versions WHERE file_version_id = ?",
                (latest_fv["id"],),
            )

        self._build_depth()
        self._update_symbol_version_depths()
        self.conn.commit()


    def remove_file(self, file_path: str):
        """删除文件（清理数据库中的相关记录，保留版本历史）

        注意：保留 file_versions / file_symbol_versions / call_versions 历史，
        只标记 file_instances 表中的状态为 deleted，并清除 symbols / calls 当前快照。
        """
        ws_id = self._get_active_workspace_id()
        rel_path = norm_path(file_path)

        cur = self.conn.execute(
            "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = ?",
            (ws_id, rel_path),
        )
        row = cur.fetchone()
        if not row:
            return

        file_instance_id = row["id"]

        # 清除当前快照（保留历史版本）
        self.conn.execute("DELETE FROM calls WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id = ?)", (file_instance_id,))
        self.conn.execute("DELETE FROM symbols WHERE file_instance_id = ?", (file_instance_id,))

        # 标记文件为已删除
        self.conn.execute(
            "UPDATE file_instances SET status = 'deleted', mtime = ? WHERE id = ?",
            (time.time(), file_instance_id),
        )

        # 标记最新版本为已删除
        cur = self.conn.execute(
            "SELECT id FROM file_versions WHERE file_instance_id = ? AND is_current = 1",
            (file_instance_id,),
        )
        latest = cur.fetchone()
        if latest:
            self.conn.execute(
                "UPDATE file_versions SET is_deleted = 1 WHERE id = ?",
                (latest["id"],),
            )

        self.conn.commit()
        # B-P7b: 失效 GraphStore 缓存（symbols/calls 已删除）
        self._invalidate_graph_store()
        print(t("cli.messages.db_build_marked_deleted", path=rel_path, count=self._count_file_versions(file_instance_id)))


    def _count_file_versions(self, file_instance_id: int) -> int:
        """统计文件的历史版本数"""
        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM file_versions WHERE file_instance_id = ?",
            (file_instance_id,),
        )
        return cur.fetchone()["cnt"]


    def _infer_module_path(self, rel_path: str) -> str:
        """从文件路径推断模块路径（简化版）"""
        path = rel_path.replace("\\", "/")
        if path.startswith("src/"):
            path = path[4:]
        if path.endswith(".rs"):
            path = path[:-3]
        if path.endswith("/mod"):
            path = path[:-4]
        if path == "lib":
            return "lib"
        if path == "main":
            return "main"
        return "lib::" + path.replace("/", "::")


    def _collect_all_current_file_results(self) -> Dict[str, Dict[str, Any]]:
        """收集所有当前版本的文件解析结果（用于调用关系解析）

        从数据库加载已解析的结果，不重新解析文件。
        """
        results = {}

        cur = self.conn.execute("""
            SELECT fv.id as fv_id, fi.id as fi_id, fi.rel_path, fi.abs_path, fi.module_path
            FROM file_versions fv
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fv.is_current = 1 AND fv.is_deleted = 0
              AND fi.workspace_id = ?
        """, (self._get_active_workspace_id(),))

        for row in cur:
            rel_path = row["rel_path"]
            abs_path = row["abs_path"]
            module_path = row["module_path"]
            fi_id = row["fi_id"]
            fv_id = row["fv_id"]

            result = self._load_file_result_from_db(fi_id, fv_id, rel_path, abs_path, module_path)
            if result:
                results[rel_path] = result

        return results

    # --------------------------------------------------------------------
    # 查询接口
    # --------------------------------------------------------------------


