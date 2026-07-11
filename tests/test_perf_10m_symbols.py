"""千万级符号性能测试

目标：验证 Call Warden 在 1000 万符号规模下的关键操作性能。

合成策略：
- 生成 N 个 Python 文件，每个文件 M 个函数
- 5000 文件 × 2000 函数 = 1000 万符号
- 每个文件内函数链式调用（fn_0 → fn_1 → ... → fn_M）
- 跨文件链式调用（file_0::fn_0 → file_1::fn_0 → ...）

测量指标：
1. build_full_graph 总耗时（parse + resolve + depth）
2. search_symbols 延迟
3. get_callers / get_callees 延迟
4. get_stats 延迟
5. 内存占用（RSS）

使用方式：
    # 小规模验证（1000 符号）
    python tests/test_perf_10m_symbols.py

    # 千万级测试（需环境变量触发）
    RUN_PERF_10M=1 python -m pytest tests/test_perf_10m_symbols.py -v -s

    # 自定义规模
    PERF_NUM_FILES=100 PERF_FUNCS_PER_FILE=500 RUN_PERF_10M=1 python -m pytest tests/test_perf_10m_symbols.py::TestPerf10MSymbols::test_10m_symbols_perf -v -s
"""
import os
import sys
import time
import shutil
import tempfile
import json

import pytest

# 确保能导入 callwarden
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from callwarden.db.db import CodeGraphDB


# ============================================
# 合成代码生成器
# ============================================

def generate_synthetic_repo(root: str, num_files: int = 5000, funcs_per_file: int = 2000):
    """生成合成 Python 代码库。

    Args:
        root: 目标根目录
        num_files: 文件数量
        funcs_per_file: 每个文件的函数数量

    总符号数 = num_files × funcs_per_file
    默认: 5000 × 2000 = 10,000,000（一千万）
    """
    os.makedirs(root, exist_ok=True)

    for file_idx in range(num_files):
        filepath = os.path.join(root, f"mod_{file_idx:06d}.py")
        lines = []

        for fn_idx in range(funcs_per_file):
            fn_name = f"fn_{file_idx}_{fn_idx:06d}"

            if fn_idx + 1 < funcs_per_file:
                # 文件内链式调用：fn_i 调用 fn_{i+1}
                callee = f"fn_{file_idx}_{fn_idx + 1:06d}"
                lines.append(f"def {fn_name}(x):")
                lines.append(f"    return {callee}(x + 1)")
                lines.append("")
            elif file_idx + 1 < num_files:
                # 最后一个函数调用下一个文件的第一个函数（跨文件链）
                callee = f"fn_{file_idx + 1:06d}_000000"
                lines.append(f"def {fn_name}(x):")
                lines.append(f"    return {callee}(x + 1)")
                lines.append("")
            else:
                # 最后一个文件的最后一个函数
                lines.append(f"def {fn_name}(x):")
                lines.append(f"    return x")
                lines.append("")

        with open(filepath, "w", encoding="utf-8") as f:
            f.write("\n".join(lines))

        if file_idx % 500 == 0 and file_idx > 0:
            print(f"  生成进度: {file_idx}/{num_files} 文件", flush=True)

    total_symbols = num_files * funcs_per_file
    print(f"  合成完成: {num_files} 文件, {total_symbols:,} 符号", flush=True)
    return total_symbols


# ============================================
# 规模配置
# ============================================

# 默认配置：千万级（5000 × 2000 = 10M）
PERF_NUM_FILES = int(os.environ.get("PERF_NUM_FILES", "5000"))
PERF_FUNCS_PER_FILE = int(os.environ.get("PERF_FUNCS_PER_FILE", "2000"))
PERF_TARGET_SYMBOLS = PERF_NUM_FILES * PERF_FUNCS_PER_FILE

# 小规模快速测试配置（用于 CI 验证脚本正确性）
SMALL_NUM_FILES = int(os.environ.get("SMALL_NUM_FILES", "10"))
SMALL_FUNCS_PER_FILE = int(os.environ.get("SMALL_FUNCS_PER_FILE", "100"))


# ============================================
# 辅助函数
# ============================================


def _format_bytes(n):
    """格式化字节数。"""
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _get_rss_mb():
    """获取当前进程 RSS（MB）。"""
    try:
        import psutil
        return psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
    except ImportError:
        return 0


def _setup_db(repo_path: str) -> CodeGraphDB:
    """在指定路径创建 CodeGraphDB 并注册 workspace。"""
    db_path = os.path.join(repo_path, "callwarden.db")
    db = CodeGraphDB(db_path, workspace_root=repo_path)
    ws_id = db.register_workspace("perf-test", repo_path, "性能测试工作区")
    db.set_active_workspace(ws_id)
    return db


# ============================================
# 性能测试
# ============================================


class TestPerf10MSymbols:
    """千万级符号性能测试"""

    @pytest.fixture
    def small_repo(self, tmp_path):
        """小规模合成代码库（用于快速验证）。"""
        repo_dir = tmp_path / "small_repo"
        repo_dir_str = str(repo_dir)

        total = generate_synthetic_repo(repo_dir_str, SMALL_NUM_FILES, SMALL_FUNCS_PER_FILE)
        print(f"\n小规模测试: {SMALL_NUM_FILES} 文件 × {SMALL_FUNCS_PER_FILE} 函数 = {total:,} 符号")

        yield repo_dir_str

        shutil.rmtree(repo_dir_str, ignore_errors=True)

    def test_small_scale_validation(self, small_repo):
        """小规模验证：确保脚本正确运行"""
        print(f"\n=== 小规模验证 ===")
        print(f"仓库路径: {small_repo}")

        db = _setup_db(small_repo)
        try:
            # 1. build_full_graph
            print("\n--- build_full_graph ---")
            rss_before = _get_rss_mb()
            t0 = time.time()
            db.build_full_graph()
            build_elapsed = time.time() - t0
            rss_after = _get_rss_mb()
            print(f"耗时: {build_elapsed:.2f}s, RSS: {rss_before:.0f}→{rss_after:.0f} MB")

            # 2. get_stats
            print("\n--- get_stats ---")
            t0 = time.time()
            stats = db.get_stats()
            stats_elapsed = time.time() - t0
            print(f"耗时: {stats_elapsed:.3f}s")
            print(f"  统计: {json.dumps(stats, ensure_ascii=False, default=str)[:200]}")

            # 3. search_symbols
            print("\n--- search_symbols ---")
            t0 = time.time()
            results = db.search_symbols("fn_0_000005")
            search_elapsed = time.time() - t0
            print(f"耗时: {search_elapsed:.3f}s, 结果数: {len(results) if results else 0}")

            # 4. get_callers
            print("\n--- get_callers ---")
            t0 = time.time()
            callers = db.get_callers("fn_0_000006")
            callers_elapsed = time.time() - t0
            print(f"耗时: {callers_elapsed:.3f}s, 结果数: {len(callers) if callers else 0}")

            # 5. get_callees
            print("\n--- get_callees ---")
            t0 = time.time()
            callees = db.get_callees("fn_0_000005")
            callees_elapsed = time.time() - t0
            print(f"耗时: {callees_elapsed:.3f}s, 结果数: {len(callees) if callees else 0}")

            print("\n=== 小规模验证通过 ===")
        finally:
            db.close()

    @pytest.mark.slow
    @pytest.mark.skipif(
        os.environ.get("RUN_PERF_10M") != "1",
        reason="千万级性能测试需要 RUN_PERF_10M=1 环境变量触发"
    )
    def test_10m_symbols_perf(self, tmp_path):
        """千万级符号性能测试（需 RUN_PERF_10M=1 触发）

        测试项：
        1. build_full_graph 总耗时
        2. search_symbols 延迟
        3. get_callers 延迟
        4. get_callees 延迟
        5. get_stats 延迟
        """
        results = {
            "config": {
                "num_files": PERF_NUM_FILES,
                "funcs_per_file": PERF_FUNCS_PER_FILE,
                "target_symbols": PERF_TARGET_SYMBOLS,
            },
            "metrics": {},
        }

        repo_dir = tmp_path / "perf_repo"
        repo = str(repo_dir)

        print(f"\n{'='*60}")
        print(f"千万级符号性能测试")
        print(f"目标: {PERF_TARGET_SYMBOLS:,} 符号 ({PERF_NUM_FILES} 文件 × {PERF_FUNCS_PER_FILE} 函数/文件)")
        print(f"{'='*60}")

        # ========== 0. 生成合成代码 ==========
        print(f"\n--- 0. 生成合成代码 ---")
        gen_start = time.time()
        generate_synthetic_repo(repo, PERF_NUM_FILES, PERF_FUNCS_PER_FILE)
        gen_elapsed = time.time() - gen_start
        print(f"生成耗时: {gen_elapsed:.1f}s")

        # 检查磁盘占用
        total_size = 0
        for f in os.listdir(repo):
            fp = os.path.join(repo, f)
            if os.path.isfile(fp):
                total_size += os.path.getsize(fp)
        print(f"磁盘占用: {_format_bytes(total_size)}")

        # ========== 1. build_full_graph ==========
        print(f"\n--- 1. build_full_graph ---")
        rss_before = _get_rss_mb()
        print(f"起始 RSS: {rss_before:.0f} MB")

        db = _setup_db(repo)
        t0 = time.time()
        db.build_full_graph()
        build_elapsed = time.time() - t0
        rss_after = _get_rss_mb()

        print(f"耗时: {build_elapsed:.1f}s ({build_elapsed/60:.1f}min)")
        print(f"RSS 变化: {rss_before:.0f} → {rss_after:.0f} MB")

        results["metrics"]["build_full_graph"] = {
            "elapsed_s": round(build_elapsed, 2),
            "rss_before_mb": round(rss_before, 0),
            "rss_after_mb": round(rss_after, 0),
        }

        # ========== 2. get_stats ==========
        print(f"\n--- 2. get_stats ---")
        t0 = time.time()
        stats = db.get_stats()
        stats_elapsed = time.time() - t0
        print(f"耗时: {stats_elapsed:.3f}s")
        if stats:
            for k, v in stats.items():
                if isinstance(v, (int, float, str)):
                    print(f"  {k}: {v}")

        results["metrics"]["get_stats"] = {
            "elapsed_s": round(stats_elapsed, 3),
        }

        # ========== 3. search_symbols ==========
        print(f"\n--- 3. search_symbols ---")
        search_target = f"fn_{PERF_NUM_FILES // 2}_{PERF_FUNCS_PER_FILE // 2:06d}"
        t0 = time.time()
        search_results = db.search_symbols(search_target)
        search_elapsed = time.time() - t0
        result_count = len(search_results) if search_results else 0
        print(f"耗时: {search_elapsed:.3f}s (搜索: {search_target}, 结果数: {result_count})")

        results["metrics"]["search_symbols"] = {
            "elapsed_s": round(search_elapsed, 3),
            "target": search_target,
            "result_count": result_count,
        }

        # ========== 4. get_callers ==========
        print(f"\n--- 4. get_callers ---")
        callers_target = f"fn_0_000002"
        t0 = time.time()
        callers = db.get_callers(callers_target)
        callers_elapsed = time.time() - t0
        caller_count = len(callers) if callers else 0
        print(f"耗时: {callers_elapsed:.3f}s (查询 callers: {callers_target}, 结果数: {caller_count})")

        results["metrics"]["get_callers"] = {
            "elapsed_s": round(callers_elapsed, 3),
            "target": callers_target,
            "result_count": caller_count,
        }

        # ========== 5. get_callees ==========
        print(f"\n--- 5. get_callees ---")
        callees_target = f"fn_0_000001"
        t0 = time.time()
        callees = db.get_callees(callees_target)
        callees_elapsed = time.time() - t0
        callee_count = len(callees) if callees else 0
        print(f"耗时: {callees_elapsed:.3f}s (查询 callees: {callees_target}, 结果数: {callee_count})")

        results["metrics"]["get_callees"] = {
            "elapsed_s": round(callees_elapsed, 3),
            "target": callees_target,
            "result_count": callee_count,
        }

        # ========== 6. 最终 RSS ==========
        rss_final = _get_rss_mb()
        results["metrics"]["final_rss_mb"] = round(rss_final, 0)
        print(f"\n最终 RSS: {rss_final:.0f} MB")

        db.close()

        # ========== 输出 JSON 报告 ==========
        report_path = os.path.join(os.path.dirname(__file__), "_perf_10m_report.json")
        with open(report_path, "w", encoding="utf-8") as f:
            json.dump(results, f, indent=2, ensure_ascii=False)
        print(f"\n报告已写入: {report_path}")

        # ========== 性能基准断言 ==========
        print(f"\n{'='*60}")
        print("性能基准验证")
        print(f"{'='*60}")

        # build_full_graph 应在合理时间内完成
        if PERF_TARGET_SYMBOLS >= 10_000_000:
            # 千万符号：允许 30 分钟
            assert build_elapsed < 1800, f"build_full_graph 耗时 {build_elapsed}s 超过 1800s 阈值"
        elif PERF_TARGET_SYMBOLS >= 1_000_000:
            # 百万+符号：允许 10 分钟
            assert build_elapsed < 600, f"build_full_graph 耗时 {build_elapsed}s 超过 600s 阈值"

        # 查询操作应在 5 秒内完成
        assert stats_elapsed < 5.0, f"get_stats 耗时 {stats_elapsed}s 超过 5s 阈值"
        assert search_elapsed < 5.0, f"search_symbols 耗时 {search_elapsed}s 超过 5s 阈值"
        assert callers_elapsed < 5.0, f"get_callers 耗时 {callers_elapsed}s 超过 5s 阈值"
        assert callees_elapsed < 5.0, f"get_callees 耗时 {callees_elapsed}s 超过 5s 阈值"

        print("\n✓ 所有性能基准通过")


if __name__ == "__main__":
    """直接运行：python tests/test_perf_10m_symbols.py"""
    print("="*60)
    print("小规模验证（10 文件 × 100 函数 = 1000 符号）")
    print("="*60)

    tmpdir = tempfile.mkdtemp()
    try:
        repo = os.path.join(tmpdir, "small_repo")
        generate_synthetic_repo(repo, 10, 100)

        db = _setup_db(repo)
        try:
            print("\n--- build_full_graph ---")
            t0 = time.time()
            db.build_full_graph()
            elapsed = time.time() - t0
            print(f"耗时: {elapsed:.2f}s")

            print("\n--- get_stats ---")
            t0 = time.time()
            stats = db.get_stats()
            elapsed = time.time() - t0
            print(f"耗时: {elapsed:.3f}s")
            if stats:
                for k, v in stats.items():
                    if isinstance(v, (int, float, str)):
                        print(f"  {k}: {v}")

            print("\n--- search ---")
            t0 = time.time()
            results = db.search_symbols("fn_0_000005")
            elapsed = time.time() - t0
            print(f"耗时: {elapsed:.3f}s, 结果数: {len(results) if results else 0}")

            print("\n✓ 小规模验证通过")
        finally:
            db.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)
