"""db_risk.py

风险评分模型与边置信度四档机制（纯函数，无 DB 依赖）。

设计来源：testcode 多仓分析的可借鉴项——callwarden 的 review_readiness
此前只有"影响符号总数"单一阈值（>20 high / >5 medium / else low），
既不分边的可靠度，也不区分"影响深度/跨模块/测试缺口/安全面/调用压力/
变更热度"这些真正决定审查成本的维度。本模块把这两层补上：

1. 边置信度四档（EDGE_CONFIDENCE）：解析器对每条调用边的确定程度。
   - EXTRACTED：callee_id > 0 且调用方/被调方同文件（同一 file_instance）；
   - INFERRED：callee_id > 0 但跨文件（依赖跨文件符号解析）；
   - AMBIGUOUS：callee_id = 0 但有 callee_qualified（同名多候选未收敛）；
   - UNRESOLVED：callee_id = 0 且只有裸名（只能按名字猜测）。
2. 风险评分（六信号加权求和）：深度触达、跨模块扩散、测试缺口、
   安全命名面、调用压力、变更热度，各自封顶（cap）防止单信号支配。

全部为纯函数：输入已从 DB 取出的标量/序列，输出可直接 JSON 序列化的
dict，便于单元测试与跨模块（MCP/CLI）复用。
"""

from __future__ import annotations

import re
from typing import Dict, Iterable, List

# 边置信度四档权重（越大越确定；审查建议据此分级）。
EDGE_CONFIDENCE: Dict[str, float] = {
    "EXTRACTED": 1.0,
    "INFERRED": 0.8,
    "AMBIGUOUS": 0.4,
    "UNRESOLVED": 0.2,
}

# 置信度 → 审查结论分档：≥0.8 判定"会坏"，≥0.4 判定"可能受影响"。
CONFIDENCE_WILL_BREAK = 0.8
CONFIDENCE_LIKELY_AFFECTED = 0.4

# 安全敏感命名（auth/password/token/secret/key/crypto/login 等）。
# 命中即视为变更需要额外人工审查（认证/密钥/加密面）。
_SECURITY_NAME_RE = re.compile(
    r"(auth|credential|password|passwd|token|secret|api[_-]?key|crypto|"
    r"encrypt|decrypt|signature|session|login|csrf|jwt|oauth)",
    re.IGNORECASE,
)

# 单信号权重与上限：0.05/单位，cap 防止单一信号支配总分。
SIGNAL_STEP = 0.05
SIGNAL_CAPS: Dict[str, float] = {
    "depth_reach": 0.25,      # 每层影响 0.05
    "cross_module": 0.15,     # 每个跨模块受影响符号 0.05
    "test_gap": 0.30,         # 每个缺少测试覆盖的 public 符号 0.05
    "security_surface": 0.20,  # 命中安全命名即 0.20（flat，不累加）
    "caller_pressure": 0.10,  # 每 20 个直接调用方 0.05
    "churn": 0.15,            # 每 10 次提交 0.05
}

# 裸名边匹配的噪声护栏：过短/过通用的名字不参与 UNRESOLVED 降级，
# 否则任意 `get`/`run` 调用都会把同名符号压到最低档。
_GENERIC_NAMES = frozenset({
    "get", "set", "run", "main", "new", "init", "call", "read", "write",
    "open", "close", "load", "save", "add", "put", "exec", "test",
})
_MIN_RAW_NAME_LEN = 4


def classify_edge(callee_id: int, same_file: bool, callee_qualified: str) -> str:
    """把一条 calls 边归类到四档置信度（见模块 docstring）。

    Args:
        callee_id: calls.callee_id（>0 表示已解析到 symbols.id）。
        same_file: 调用方与被调方是否同一 file_instance（仅 callee_id>0 有意义）。
        callee_qualified: calls.callee_qualified（解析前的限定名，可能为空）。

    Returns:
        EDGE_CONFIDENCE 的键之一。
    """
    if callee_id and callee_id > 0:
        return "EXTRACTED" if same_file else "INFERRED"
    if (callee_qualified or "").strip():
        return "AMBIGUOUS"
    return "UNRESOLVED"


def is_noisy_raw_name(name: str) -> bool:
    """裸名边是否因过短/过通用而不参与 UNRESOLVED 降级（噪声护栏）。"""
    norm = (name or "").strip()
    if len(norm) < _MIN_RAW_NAME_LEN:
        return True
    return norm.lower() in _GENERIC_NAMES


def edge_confidence_weight(tier: str) -> float:
    """分档名 → 权重；未知分档按最低档处理（fail-closed）。"""
    return EDGE_CONFIDENCE.get(tier, EDGE_CONFIDENCE["UNRESOLVED"])


def is_security_named(name: str, qualified_name: str = "") -> bool:
    """符号名（或限定名）是否命中安全敏感命名。"""
    return bool(_SECURITY_NAME_RE.search(f"{name} {qualified_name}"))


def _capped(count: float, cap: float) -> float:
    """count 个权重单位（SIGNAL_STEP/单位）求和后封顶，保留 2 位小数。"""
    return round(min(cap, SIGNAL_STEP * max(0.0, float(count))), 2)


def collect_risk_signals(
    *,
    max_depth: int,
    impacted_modules: Iterable[str],
    source_module: str,
    must_test_count: int,
    test_symbol_count: int,
    security_named_count: int,
    direct_caller_count: int,
    churn_commits: int,
) -> Dict[str, float]:
    """汇总六个风险信号并各自封顶。

    Args:
        max_depth: 影响树实际达到的最大层深（源符号为第 0 层）。
        impacted_modules: 受影响符号的 module_path 序列（不含源符号）。
        source_module: 源符号的 module_path。
        must_test_count: 受影响 public 函数数（必测项）。
        test_symbol_count: 影响树内测试符号数（已覆盖的必测项）。
        security_named_count: 命中安全命名的受影响符号数。
        direct_caller_count: 第 1 层直接调用方数量。
        churn_commits: 源文件近期提交次数（git log 计数）。

    Returns:
        {"depth_reach": x, "cross_module": x, "test_gap": x,
         "security_surface": x, "caller_pressure": x, "churn": x}
    """
    modules = {m for m in impacted_modules if m}
    src_mod = source_module or ""
    cross_module_count = len({m for m in modules if m != src_mod})
    test_gap_count = max(0, int(must_test_count) - int(test_symbol_count))
    return {
        "depth_reach": _capped(max(0, int(max_depth)), SIGNAL_CAPS["depth_reach"]),
        "cross_module": _capped(cross_module_count, SIGNAL_CAPS["cross_module"]),
        "test_gap": _capped(test_gap_count, SIGNAL_CAPS["test_gap"]),
        "security_surface": (
            SIGNAL_CAPS["security_surface"] if security_named_count > 0 else 0.0
        ),
        "caller_pressure": _capped(
            int(direct_caller_count) / 20.0, SIGNAL_CAPS["caller_pressure"]
        ),
        "churn": _capped(int(churn_commits) / 10.0, SIGNAL_CAPS["churn"]),
    }


def compute_risk_score(signals: Dict[str, float]) -> float:
    """风险总分 = 各信号之和（信号已封顶，同源加法，便于解释）。"""
    return round(sum(float(v) for v in signals.values()), 2)


def classify_confidence(weight: float) -> str:
    """单符号影响置信度 → 审查结论分档。"""
    if weight >= CONFIDENCE_WILL_BREAK:
        return "WILL_BREAK"
    if weight >= CONFIDENCE_LIKELY_AFFECTED:
        return "LIKELY_AFFECTED"
    return "UNAFFECTED"


def _candidate_row(
    qualified_name: str, weight: float, metadata: Dict[str, Dict[str, object]]
) -> Dict[str, object]:
    """构造单条候选记录（含消歧所需的定位元数据）。

    target_uid 与 qualified_name 同值（任务口径把限定名作为候选的唯一标识）。
    file_path / kind / call_line 缺省为空串/0，保证字段齐全、可稳定序列化。
    """
    info = metadata.get(qualified_name) or {}
    call_line = int(info.get("call_line") or 0)
    return {
        "qualified_name": qualified_name,
        "target_uid": qualified_name,
        "confidence": round(float(weight), 2),
        "classification": classify_confidence(weight),
        "tier": _tier_for_weight(weight),
        "file_path": info.get("file_path") or "",
        "kind": info.get("kind") or "",
        "call_line": call_line,
    }


def _rank_key(row: Dict[str, object]) -> tuple:
    """ranked 排序键：置信度降序 → 调用点行号升序（0 视为未知排后）→ 限定名。

    末位限定名兜底保证同分同行的候选顺序稳定可复现（不依赖 dict 插入序）。
    """
    line = int(row.get("call_line") or 0)
    return (
        -float(row["confidence"]),
        line if line > 0 else float("inf"),
        str(row["qualified_name"]),
    )


def summarize_edges(
    per_symbol_weights: Dict[str, float],
    metadata: Dict[str, Dict[str, object]] | None = None,
) -> Dict[str, object]:
    """把逐符号权重汇总为报告用的 edge_confidence 块。

    Args:
        per_symbol_weights: {qualified_name: weight}（weight 为入边最低置信度）。
        metadata: 可选 {qualified_name: {"file_path", "kind", "call_line"}}，
            用于给每个候选补充定位元数据；缺省时字段以空串/0 填充。

    Returns:
        {"min_confidence": 最低权重,
         "classification": 全局分档（按最低置信度，保守口径）,
         "histogram": {WILL_BREAK: n, LIKELY_AFFECTED: n, UNAFFECTED: n},
         "per_symbol": [候选记录]（按 confidence 升序，最多 20 条）,
         "ranked": [候选记录]（按 confidence 降序 + call_line 升序，最多 20 条）}
        候选记录字段：qualified_name / target_uid / confidence /
        classification / tier / file_path / kind / call_line。
    """
    meta = metadata or {}
    histogram: Dict[str, int] = {
        "WILL_BREAK": 0,
        "LIKELY_AFFECTED": 0,
        "UNAFFECTED": 0,
    }
    rows: List[Dict[str, object]] = []
    for qn, weight in per_symbol_weights.items():
        row = _candidate_row(qn, weight, meta)
        histogram[row["classification"]] += 1
        rows.append(row)
    rows.sort(key=lambda r: (r["confidence"], r["qualified_name"]))
    ranked = sorted(rows, key=_rank_key)
    min_conf = min(per_symbol_weights.values()) if per_symbol_weights else 1.0
    return {
        "min_confidence": round(float(min_conf), 2),
        "classification": classify_confidence(min_conf),
        "histogram": histogram,
        "per_symbol": rows[:20],
        "ranked": ranked[:20],
    }


def _tier_for_weight(weight: float) -> str:
    """权重 → 最接近的分档名（用于报告可读性；不引入额外常量）。"""
    best, best_diff = "UNRESOLVED", None
    for tier, tier_weight in EDGE_CONFIDENCE.items():
        diff = abs(tier_weight - float(weight))
        if best_diff is None or diff < best_diff:
            best, best_diff = tier, diff
    return best
