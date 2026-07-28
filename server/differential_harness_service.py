"""DifferentialHarnessService —— 生产代码访问差分 harness 的查询服务（Phase 0 子任务 3 Step 4）

设计文档：docs/design/differential-harness-contract.md
真相源：docs/design/differential-harness-contract.md + rust_ext/src/differential_baseline.rs

本服务为生产代码（CLI/MCP/daemon）提供差分 harness 的运行时查询能力：
    - 性能基线目标查询
    - 回归阈值查询
    - Phase 0 完成门查询
    - 基线 JSON 路径查询

设计原则：
    - 只读：本服务只读取常量和文件，不修改
    - 无状态：所有方法都是纯函数
    - 无锁：不访问数据库，无 SQLite 锁冲突
    - Python 端镜像 Rust differential_baseline 模块的常量
    - 后续 Phase 1+ 可通过 PyO3 直接调用 Rust 模块，本服务作为过渡

错误语义：
    - 所有查询返回明确值，不抛异常
    - 文件不存在时返回 None 或空结果
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

# ============================================
# 性能基线常量（镜像 rust_ext/src/differential_baseline.rs）
# ============================================

# 契约 §4.1 性能指标目标
PARSE_P50_TARGET_MS = 100.0
PARSE_P95_TARGET_MS = 200.0
GRAPHSTORE_LOAD_P50_TARGET_MS = 5000.0
GRAPHSTORE_LOAD_P95_TARGET_MS = 10000.0
GET_CALLERS_P50_TARGET_MS = 1.0
GET_CALLERS_P95_TARGET_MS = 5.0
WATCHER_UPDATE_P95_TARGET_MS = 3000.0

# 契约 §4.3 回归阈值
PERF_P50_REGRESSION_THRESHOLD = 1.5
PERF_P95_REGRESSION_THRESHOLD = 2.0
RSS_REGRESSION_THRESHOLD = 1.5
BINARY_SIZE_REGRESSION_THRESHOLD = 1.2

# 契约 §3.3 Phase 0 完成门
PHASE0_GATE_TYPESCRIPT = "tests_expose_typescript_gap"
PHASE0_GATE_PHP = "tests_expose_php_gap"
PHASE0_GATE_SCALA = "tests_expose_scala_gap"
PHASE0_GATE_HCL = "tests_expose_hcl_gap"


@dataclass(frozen=True)
class PerfTarget:
    """性能指标目标。"""
    name: str
    target_ms: float
    description: str


# 所有性能目标（9 个指标）
_PERF_TARGETS: dict[str, PerfTarget] = {
    "parse_p50_ms": PerfTarget("parse_p50_ms", PARSE_P50_TARGET_MS, "单文件 parse P50"),
    "parse_p95_ms": PerfTarget("parse_p95_ms", PARSE_P95_TARGET_MS, "单文件 parse P95"),
    "graphstore_load_p50_ms": PerfTarget(
        "graphstore_load_p50_ms", GRAPHSTORE_LOAD_P50_TARGET_MS,
        "GraphStore 加载 1M 符号 P50"
    ),
    "graphstore_load_p95_ms": PerfTarget(
        "graphstore_load_p95_ms", GRAPHSTORE_LOAD_P95_TARGET_MS,
        "GraphStore 加载 1M 符号 P95"
    ),
    "get_callers_p50_ms": PerfTarget(
        "get_callers_p50_ms", GET_CALLERS_P50_TARGET_MS,
        "GraphStore get_callers P50"
    ),
    "get_callers_p95_ms": PerfTarget(
        "get_callers_p95_ms", GET_CALLERS_P95_TARGET_MS,
        "GraphStore get_callers P95"
    ),
    "watcher_update_p95_ms": PerfTarget(
        "watcher_update_p95_ms", WATCHER_UPDATE_P95_TARGET_MS,
        "watcher 单文件更新 P95"
    ),
    "build_full_graph_p50_ms": PerfTarget(
        "build_full_graph_p50_ms", 0.0, "1M 符号全量构建 P50（待测）"
    ),
    "build_full_graph_p95_ms": PerfTarget(
        "build_full_graph_p95_ms", 0.0, "1M 符号全量构建 P95（待测）"
    ),
}


# 所有回归阈值
_REGRESSION_THRESHOLDS: dict[str, float] = {
    "perf_p50": PERF_P50_REGRESSION_THRESHOLD,
    "perf_p95": PERF_P95_REGRESSION_THRESHOLD,
    "rss": RSS_REGRESSION_THRESHOLD,
    "binary_size": BINARY_SIZE_REGRESSION_THRESHOLD,
}


# Phase 0 完成门列表
_PHASE0_GATES: list[str] = [
    PHASE0_GATE_TYPESCRIPT,
    PHASE0_GATE_PHP,
    PHASE0_GATE_SCALA,
    PHASE0_GATE_HCL,
]


class DifferentialHarnessService:
    """差分 harness 查询服务（只读/无状态/无锁）。

    用法：
        service = DifferentialHarnessService()
        target = service.get_perf_target("parse_p50_ms")
        threshold = service.get_regression_threshold("perf_p50")
        baseline = service.load_baseline()
    """

    @staticmethod
    def get_perf_target(metric: str) -> Optional[PerfTarget]:
        """获取性能指标目标。"""
        return _PERF_TARGETS.get(metric)

    @staticmethod
    def list_perf_targets() -> list[str]:
        """列出所有性能指标名。"""
        return list(_PERF_TARGETS.keys())

    @staticmethod
    def get_regression_threshold(category: str) -> Optional[float]:
        """获取回归阈值。

        Args:
            category: "perf_p50" / "perf_p95" / "rss" / "binary_size"
        """
        return _REGRESSION_THRESHOLDS.get(category)

    @staticmethod
    def list_regression_thresholds() -> dict[str, float]:
        """返回所有回归阈值。"""
        return dict(_REGRESSION_THRESHOLDS)

    @staticmethod
    def list_phase0_gates() -> list[str]:
        """返回 Phase 0 完成门列表。"""
        return list(_PHASE0_GATES)

    @staticmethod
    def detect_regression(
        metric: str,
        baseline_value: float,
        current_value: float,
        threshold: float,
    ) -> dict:
        """检测性能回归。

        Returns:
            {
                "metric": str,
                "baseline_value": float,
                "current_value": float,
                "ratio": float,
                "threshold": float,
                "is_regression": bool,
            }
        """
        if baseline_value > 0:
            ratio = current_value / baseline_value
        elif current_value > 0:
            ratio = float("inf")
        else:
            ratio = 1.0
        return {
            "metric": metric,
            "baseline_value": baseline_value,
            "current_value": current_value,
            "ratio": ratio,
            "threshold": threshold,
            "is_regression": ratio > threshold,
        }

    @staticmethod
    def get_baseline_path() -> Path:
        """返回 baseline.json 默认路径。"""
        return Path(__file__).resolve().parent.parent / "tests" / "parser_contract" / "baseline.json"

    @staticmethod
    def load_baseline() -> Optional[dict]:
        """加载 baseline.json（不存在返回 None）。"""
        path = DifferentialHarnessService.get_baseline_path()
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    @staticmethod
    def verify_baseline_commit(current_sha: str) -> dict:
        """验证 baseline.json 的 commit_sha 与当前一致。

        Returns:
            {
                "baseline_exists": bool,
                "baseline_commit": str | None,
                "current_commit": str,
                "is_consistent": bool,
            }
        """
        baseline = DifferentialHarnessService.load_baseline()
        if baseline is None:
            return {
                "baseline_exists": False,
                "baseline_commit": None,
                "current_commit": current_sha,
                "is_consistent": False,
            }
        baseline_commit = baseline.get("commit_sha", "")
        return {
            "baseline_exists": True,
            "baseline_commit": baseline_commit,
            "current_commit": current_sha,
            "is_consistent": baseline_commit == current_sha,
        }

    @staticmethod
    def check_phase0_gates(baseline: Optional[dict] = None) -> dict:
        """检查 Phase 0 完成门是否正确暴露缺口。

        gate=True 表示 baseline 正确检测到缺口（不是缺口已修复）。

        Returns:
            {
                "<gate_name>": bool,  # True = 暴露了缺口
                ...
                "all_gates_exposed": bool,
            }
        """
        if baseline is None:
            baseline = DifferentialHarnessService.load_baseline()
        if baseline is None:
            return {
                gate: False for gate in _PHASE0_GATES
            } | {"all_gates_exposed": False, "baseline_exists": False}

        gates = baseline.get("phase0_completion_gates", {})
        result = {}
        all_exposed = True
        for gate in _PHASE0_GATES:
            exposed = gates.get(gate, False)
            result[gate] = exposed
            if not exposed:
                all_exposed = False
        result["all_gates_exposed"] = all_exposed
        result["baseline_exists"] = True
        return result

    @staticmethod
    def get_language_capability(lang: str, baseline: Optional[dict] = None) -> Optional[dict]:
        """获取指定语言的能力快照。"""
        if baseline is None:
            baseline = DifferentialHarnessService.load_baseline()
        if baseline is None:
            return None
        return baseline.get("language_capability", {}).get(lang)

    @staticmethod
    def list_known_gaps() -> list[str]:
        """列出所有已知差异类型。"""
        return [
            "symbol_diff",       # 符号差异
            "call_diff",         # 调用差异
            "signature_gap",     # signature 缺口
            "visibility_gap",    # visibility 缺口
            "references_gap",    # references 缺口
        ]
