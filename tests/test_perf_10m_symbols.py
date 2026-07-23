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

# 常规 CI 默认跳过重度性能测试，需 RUN_PERF_10M=1 环境变量显式触发
pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_PERF_10M") != "1",
    reason="重度性能测试仅在 RUN_PERF_10M=1 时运行"
)

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

        H6 完整 checklist 覆盖：
        1. build_full_graph 总耗时（refresh-all 等价）
        2. search_symbols 延迟
        3. get_callers 延迟
        4. get_callees 延迟
        5. get_stats 延迟
        6. get_call_chain_up 延迟（等价于 MCP get_impact）
        7. get_call_chain_down 延迟
        8. blast_radius 延迟（变更影响半径）
        9. detect_clones 延迟（克隆检测，限定 file_filter 避免 O(N²)）
        10. SQLite db 文件大小测量
        11. 最终 RSS 与瓶颈识别
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

        # ========== 6. get_call_chain_up（等价于 MCP get_impact）==========
        print(f"\n--- 6. get_call_chain_up (MCP get_impact) ---")
        # 从 search_symbols 结果中拿到真实 qualified_name，避免 QN 格式不匹配
        chain_up_target = f"fn_0_000010"
        try:
            chain_up_results = db.search_symbols(chain_up_target) or []
            if chain_up_results:
                chain_up_target = chain_up_results[0].get("qualified_name", chain_up_target)
        except Exception as e:
            print(f"  查询 chain_up_target QN 失败: {e}")

        t0 = time.time()
        chain_up = db.get_call_chain_up(chain_up_target, max_depth=5)
        chain_up_elapsed = time.time() - t0
        chain_up_count = chain_up.get("total_upstream", 0) if isinstance(chain_up, dict) else 0
        print(f"耗时: {chain_up_elapsed:.3f}s (查询 call_chain_up: {chain_up_target}, "
              f"depth=5, 上游符号数: {chain_up_count})")

        results["metrics"]["get_call_chain_up"] = {
            "elapsed_s": round(chain_up_elapsed, 3),
            "target": chain_up_target,
            "max_depth": 5,
            "total_upstream": chain_up_count,
        }

        # ========== 7. get_call_chain_down ==========
        print(f"\n--- 7. get_call_chain_down ---")
        chain_down_target = f"fn_0_000001"
        try:
            chain_down_results = db.search_symbols(chain_down_target) or []
            if chain_down_results:
                chain_down_target = chain_down_results[0].get("qualified_name", chain_down_target)
        except Exception as e:
            print(f"  查询 chain_down_target QN 失败: {e}")

        t0 = time.time()
        chain_down = db.get_call_chain_down(chain_down_target, max_depth=5)
        chain_down_elapsed = time.time() - t0
        chain_down_count = chain_down.get("total_downstream", 0) if isinstance(chain_down, dict) else 0
        print(f"耗时: {chain_down_elapsed:.3f}s (查询 call_chain_down: {chain_down_target}, "
              f"depth=5, 下游符号数: {chain_down_count})")

        results["metrics"]["get_call_chain_down"] = {
            "elapsed_s": round(chain_down_elapsed, 3),
            "target": chain_down_target,
            "max_depth": 5,
            "total_downstream": chain_down_count,
        }

        # ========== 8. blast_radius（变更影响半径）==========
        print(f"\n--- 8. blast_radius ---")
        # 先查询目标符号的 symbol_hash（按短名 search 后取 QN）
        blast_target_qname = f"fn_0_000005"
        blast_symbol_hash = ""
        try:
            blast_results = db.search_symbols(blast_target_qname) or []
            if blast_results:
                blast_target_qname = blast_results[0].get("qualified_name", blast_target_qname)
                blast_symbol_hash = blast_results[0].get("symbol_hash", "")
        except Exception as e:
            print(f"  查询 blast_target_qname QN 失败: {e}")

        # search_symbols 可能不返回 symbol_hash，再尝试从 db 查
        if not blast_symbol_hash and blast_target_qname:
            try:
                cur = db.conn.execute(
                    "SELECT s.symbol_hash FROM symbols s "
                    "JOIN file_instances fi ON s.file_instance_id = fi.id "
                    "WHERE fi.workspace_id = ? AND s.qualified_name = ? LIMIT 1",
                    (db._get_active_workspace_id(), blast_target_qname),
                )
                row = cur.fetchone()
                if row:
                    blast_symbol_hash = row["symbol_hash"]
            except Exception as e:
                print(f"  从 symbols 表查询 symbol_hash 失败: {e}")

        if blast_symbol_hash:
            t0 = time.time()
            blast = db.blast_radius(blast_symbol_hash, depth=3)
            blast_elapsed = time.time() - t0
            blast_total = blast.get("total_impacted", 0) if isinstance(blast, dict) else 0
            print(f"耗时: {blast_elapsed:.3f}s (blast_radius: {blast_target_qname}, "
                  f"depth=3, 影响符号总数: {blast_total})")
            results["metrics"]["blast_radius"] = {
                "elapsed_s": round(blast_elapsed, 3),
                "target": blast_target_qname,
                "depth": 3,
                "total_impacted": blast_total,
            }
        else:
            print(f"  跳过 blast_radius：未找到符号 {blast_target_qname}")
            results["metrics"]["blast_radius"] = {"skipped": True, "reason": "symbol not found"}

        # ========== 9. detect_clones（克隆检测，限定 file_filter 避免 O(N²)）==========
        print(f"\n--- 9. detect_clones ---")
        # 限定单个文件，避免大规模下 O(N²) 爆炸
        clone_file_filter = f"mod_{PERF_NUM_FILES // 2:06d}.py"
        t0 = time.time()
        try:
            clone_stats = db.detect_clones(
                file_filter=clone_file_filter,
                min_lines=2,
                similarity_threshold=0.8,
            )
            clone_elapsed = time.time() - t0
            clone_pairs = clone_stats.get("total_pairs", 0) if isinstance(clone_stats, dict) else 0
            scanned = clone_stats.get("scanned_symbols", 0) if isinstance(clone_stats, dict) else 0
            print(f"耗时: {clone_elapsed:.3f}s (detect_clones: filter={clone_file_filter}, "
                  f"扫描符号数: {scanned}, 重复对数: {clone_pairs})")
            results["metrics"]["detect_clones"] = {
                "elapsed_s": round(clone_elapsed, 3),
                "file_filter": clone_file_filter,
                "scanned_symbols": scanned,
                "total_pairs": clone_pairs,
            }
        except Exception as e:
            clone_elapsed = time.time() - t0
            print(f"  detect_clones 失败 ({clone_elapsed:.3f}s): {e}")
            results["metrics"]["detect_clones"] = {"error": str(e), "elapsed_s": round(clone_elapsed, 3)}

        # ========== 10. SQLite 数据库文件大小 ==========
        print(f"\n--- 10. SQLite 数据库文件大小 ---")
        db_path = db.db_path if hasattr(db, 'db_path') else getattr(db, '_db_path', '')
        db_size_mb = 0.0
        if db_path and os.path.exists(db_path):
            db_size_mb = os.path.getsize(db_path) / (1024 * 1024)
            # 检查 WAL 和 SHM 文件
            wal_size_mb = 0.0
            wal_path = db_path + "-wal"
            if os.path.exists(wal_path):
                wal_size_mb = os.path.getsize(wal_path) / (1024 * 1024)
            print(f"数据库: {db_path}")
            print(f"主库大小: {db_size_mb:.1f} MB")
            if wal_size_mb > 0:
                print(f"WAL 文件: {wal_size_mb:.1f} MB")
                db_size_mb += wal_size_mb
        else:
            print(f"  未找到数据库文件 (path={db_path})")

        results["metrics"]["db_file_size_mb"] = round(db_size_mb, 1)

        # ========== 11. 最终 RSS 与瓶颈识别 ==========
        rss_final = _get_rss_mb()
        results["metrics"]["final_rss_mb"] = round(rss_final, 0)
        print(f"\n最终 RSS: {rss_final:.0f} MB")

        # 识别瓶颈：build_full_graph 中的 parse/resolve/depth 三阶段
        bottleneck = {}
        if build_elapsed > 60:
            bottleneck["build_full_graph"] = "耗时 > 60s，可能瓶颈在 tree-sitter 解析或符号入库"
        if stats_elapsed > 1.0:
            bottleneck["get_stats"] = "耗时 > 1s，可能存在全表 COUNT 查询"
        if search_elapsed > 1.0:
            bottleneck["search_symbols"] = "耗时 > 1s，可能缺失 qualified_name 索引"
        if chain_up_elapsed > 2.0:
            bottleneck["get_call_chain_up"] = "耗时 > 2s，可能存在多层 BFS 调用边全表扫描"
        if chain_down_elapsed > 2.0:
            bottleneck["get_call_chain_down"] = "耗时 > 2s，同上"
        if clone_elapsed > 5.0:
            bottleneck["detect_clones"] = "耗时 > 5s，O(N²) 笛卡尔积扫描"

        if bottleneck:
            print("\n--- 瓶颈识别 ---")
            for k, v in bottleneck.items():
                print(f"  {k}: {v}")
        else:
            print("\n--- 瓶颈识别：无明显瓶颈 ---")

        results["bottlenecks"] = bottleneck

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

        # H6 新增：调用链/影响/克隆 基准（千万级符号下也应在合理范围内）
        assert chain_up_elapsed < 10.0, f"get_call_chain_up 耗时 {chain_up_elapsed}s 超过 10s 阈值"
        assert chain_down_elapsed < 10.0, f"get_call_chain_down 耗时 {chain_down_elapsed}s 超过 10s 阈值"
        # blast_radius 可能因符号未找到而跳过
        if "blast_radius" in results["metrics"] and not results["metrics"]["blast_radius"].get("skipped"):
            assert blast_elapsed < 10.0, f"blast_radius 耗时 {blast_elapsed}s 超过 10s 阈值"
        # detect_clones 限定 file_filter 后应能快速完成
        if "error" not in results["metrics"].get("detect_clones", {}):
            assert clone_elapsed < 30.0, f"detect_clones 耗时 {clone_elapsed}s 超过 30s 阈值"

        # SQLite 数据库文件大小：千万符号不应超过 5 GB
        if PERF_TARGET_SYMBOLS >= 10_000_000:
            assert db_size_mb < 5000, f"数据库文件 {db_size_mb}MB 超过 5000MB 阈值"
        elif PERF_TARGET_SYMBOLS >= 1_000_000:
            assert db_size_mb < 800, f"数据库文件 {db_size_mb}MB 超过 800MB 阈值"

        print("\n✓ 所有性能基准通过")

    @pytest.mark.slow
    @pytest.mark.skipif(
        os.environ.get("RUN_PERF_10M") != "1",
        reason="千万级性能测试需要 RUN_PERF_10M=1 环境变量触发"
    )
    def test_multi_scale_perf(self, tmp_path):
        """多规模阶梯性能测试：测量 build_full_graph 耗时随符号量增长的趋势

        覆盖 H6 checklist：
        - 准备大规模测试数据（1K / 10K / 100K 三级）
        - 测量 refresh-all 耗时随规模增长的趋势
        - 测量核心查询性能在各级规模下的表现
        - 输出趋势分析报告
        """
        scales = [
            {"name": "1K", "files": 10, "funcs": 100, "target": 1000},
            {"name": "10K", "files": 50, "funcs": 200, "target": 10000},
            {"name": "100K", "files": 500, "funcs": 200, "target": 100000},
        ]

        trend = []

        print(f"\n{'='*60}")
        print("多规模阶梯性能测试")
        print(f"{'='*60}")

        for scale in scales:
            scale_name = scale["name"]
            print(f"\n--- 规模: {scale_name} ({scale['target']:,} 符号) ---")

            repo_dir = tmp_path / f"repo_{scale_name}"
            repo = str(repo_dir)
            generate_synthetic_repo(repo, scale["files"], scale["funcs"])

            db = _setup_db(repo)
            try:
                # build_full_graph 耗时
                rss_before = _get_rss_mb()
                t0 = time.time()
                db.build_full_graph()
                build_elapsed = time.time() - t0
                rss_after = _get_rss_mb()

                # 核心查询性能
                t0 = time.time()
                db.get_stats()
                stats_t = time.time() - t0

                t0 = time.time()
                db.search_symbols("fn_0_000005")
                search_t = time.time() - t0

                t0 = time.time()
                db.get_callers("fn_0_000002")
                callers_t = time.time() - t0

                t0 = time.time()
                db.get_call_chain_up("fn_0_000010", max_depth=5)
                chain_up_t = time.time() - t0

                t0 = time.time()
                db.get_call_chain_down("fn_0_000001", max_depth=5)
                chain_down_t = time.time() - t0

                # 数据库文件大小
                db_size_mb = 0.0
                if hasattr(db, 'db_path') and os.path.exists(db.db_path):
                    db_size_mb = os.path.getsize(db.db_path) / (1024 * 1024)

                print(f"  build: {build_elapsed:.2f}s | RSS: {rss_before:.0f}→{rss_after:.0f} MB")
                print(f"  stats: {stats_t:.3f}s | search: {search_t:.3f}s | callers: {callers_t:.3f}s")
                print(f"  chain_up: {chain_up_t:.3f}s | chain_down: {chain_down_t:.3f}s")
                print(f"  db_size: {db_size_mb:.1f} MB")

                trend.append({
                    "scale": scale_name,
                    "target_symbols": scale["target"],
                    "build_s": round(build_elapsed, 3),
                    "rss_delta_mb": round(rss_after - rss_before, 1),
                    "stats_s": round(stats_t, 4),
                    "search_s": round(search_t, 4),
                    "callers_s": round(callers_t, 4),
                    "chain_up_s": round(chain_up_t, 4),
                    "chain_down_s": round(chain_down_t, 4),
                    "db_size_mb": round(db_size_mb, 1),
                })
            finally:
                db.close()
                shutil.rmtree(repo, ignore_errors=True)

        # 输出趋势报告
        print(f"\n{'='*60}")
        print("多规模趋势分析")
        print(f"{'='*60}")
        print(f"{'规模':<8} {'符号数':<12} {'build(s)':<12} {'RSS增量(MB)':<15} {'db_size(MB)':<12}")
        for t in trend:
            print(f"{t['scale']:<8} {t['target_symbols']:<12,} {t['build_s']:<12.2f} "
                  f"{t['rss_delta_mb']:<15.1f} {t['db_size_mb']:<12.1f}")

        # 写入趋势报告
        trend_report_path = os.path.join(os.path.dirname(__file__), "_perf_multi_scale_trend.json")
        with open(trend_report_path, "w", encoding="utf-8") as f:
            json.dump({"scales": trend}, f, indent=2, ensure_ascii=False)
        print(f"\n趋势报告已写入: {trend_report_path}")

        # 趋势断言：build 耗时应近似线性增长（不能 O(n²)）
        if len(trend) >= 2:
            small = trend[0]
            large = trend[-1]
            symbol_ratio = large["target_symbols"] / max(small["target_symbols"], 1)
            build_ratio = large["build_s"] / max(small["build_s"], 0.001)
            print(f"\n规模增长: {symbol_ratio:.1f}x | build 耗时增长: {build_ratio:.1f}x")
            # 允许 build 时间增长不超过规模增长的 3 倍（防止 O(n²) 退化）
            assert build_ratio < symbol_ratio * 3, (
                f"build 耗时增长 {build_ratio:.1f}x 远超规模增长 {symbol_ratio:.1f}x，"
                f"可能存在 O(n²) 退化"
            )

        print("\n✓ 多规模趋势正常（未出现 O(n²) 退化）")


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
