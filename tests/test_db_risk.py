"""风险评分模型 + 边置信度四档机制测试。

A. 纯函数（db/db_risk.py）：分档、封顶、同源加法、置信度分档、汇总。
B. 集成（CodeGraphDB.review_readiness_report）：用合成调用图验证
   resolved（同文件/跨文件）与 raw（qualified/裸名）四档都进入报告，
   且旧字段（risk_level / must_test / impact_scope ...）保持向后兼容。
"""

from __future__ import annotations

import pytest

from callwarden.db.db import CodeGraphDB
from callwarden.db.db_risk import (
    EDGE_CONFIDENCE,
    classify_confidence,
    classify_edge,
    collect_risk_signals,
    compute_risk_score,
    edge_confidence_weight,
    is_noisy_raw_name,
    is_security_named,
    summarize_edges,
)


# ----------------------------------------------------------------------
# A. 纯函数
# ----------------------------------------------------------------------


def test_classify_edge_four_tiers():
    assert classify_edge(12, True, "mod.target") == "EXTRACTED"
    assert classify_edge(12, False, "mod.target") == "INFERRED"
    assert classify_edge(0, False, "mod.target") == "AMBIGUOUS"
    assert classify_edge(0, False, "") == "UNRESOLVED"
    # 已解析边不受 qualified 存在与否影响
    assert classify_edge(3, False, "") == "INFERRED"


def test_edge_confidence_weights_and_unknown_tier():
    assert EDGE_CONFIDENCE == {
        "EXTRACTED": 1.0,
        "INFERRED": 0.8,
        "AMBIGUOUS": 0.4,
        "UNRESOLVED": 0.2,
    }
    for tier, weight in EDGE_CONFIDENCE.items():
        assert edge_confidence_weight(tier) == weight
    # 未知分档 fail-closed 到最低档
    assert edge_confidence_weight("WHATEVER") == 0.2


def test_signals_are_capped_per_dimension():
    signals = collect_risk_signals(
        max_depth=10,                       # 0.05*10 → cap 0.25
        impacted_modules=[f"m{i}" for i in range(10)],
        source_module="src",
        must_test_count=10,                 # 0.05*10 → cap 0.30
        test_symbol_count=0,
        security_named_count=3,             # flat 0.20（不累加）
        direct_caller_count=100,            # 100/20*0.05=0.25 → cap 0.10
        churn_commits=100,                  # 100/10*0.05=0.5 → cap 0.15
    )
    assert signals == {
        "depth_reach": 0.25,
        "cross_module": 0.15,
        "test_gap": 0.30,
        "security_surface": 0.20,
        "caller_pressure": 0.10,
        "churn": 0.15,
    }
    assert compute_risk_score(signals) == 1.15


def test_cross_module_excludes_source_module_and_test_gap_floors_at_zero():
    signals = collect_risk_signals(
        max_depth=0,
        impacted_modules=["src", "src", ""],
        source_module="src",
        must_test_count=2,
        test_symbol_count=5,  # 测试覆盖超过必测项 → 缺口为 0
        security_named_count=0,
        direct_caller_count=0,
        churn_commits=0,
    )
    assert signals["cross_module"] == 0.0
    assert signals["test_gap"] == 0.0
    assert signals["security_surface"] == 0.0
    assert compute_risk_score(signals) == 0.0


def test_risk_score_is_sum_of_signals():
    signals = {"depth_reach": 0.10, "cross_module": 0.05, "churn": 0.05}
    assert compute_risk_score(signals) == 0.20


@pytest.mark.parametrize(
    ("weight", "expected"),
    [
        (1.0, "WILL_BREAK"),
        (0.8, "WILL_BREAK"),
        (0.79, "LIKELY_AFFECTED"),
        (0.4, "LIKELY_AFFECTED"),
        (0.2, "UNAFFECTED"),
        (0.0, "UNAFFECTED"),
    ],
)
def test_classify_confidence_boundaries(weight, expected):
    assert classify_confidence(weight) == expected


def test_summarize_edges_histogram_and_ordering():
    block = summarize_edges({"a.fast": 1.0, "b.mid": 0.8, "c.leaf": 0.2})
    assert block["min_confidence"] == 0.2
    assert block["classification"] == "UNAFFECTED"
    assert block["histogram"] == {
        "WILL_BREAK": 2,
        "LIKELY_AFFECTED": 0,
        "UNAFFECTED": 1,
    }
    # 按置信度升序：最不可靠的排前面
    assert [row["qualified_name"] for row in block["per_symbol"]] == [
        "c.leaf", "b.mid", "a.fast",
    ]
    assert block["per_symbol"][0]["tier"] == "UNRESOLVED"
    assert block["per_symbol"][2]["tier"] == "EXTRACTED"


def test_summarize_edges_caps_per_symbol_at_20():
    block = summarize_edges({f"mod.fn{i:02d}": 1.0 for i in range(30)})
    assert len(block["per_symbol"]) == 20
    assert sum(block["histogram"].values()) == 30


def test_summarize_edges_candidate_metadata_complete():
    """每个候选都带 qualified_name/target_uid/file_path/kind/call_line。"""
    metadata = {
        "a.fast": {"file_path": "a.py", "kind": "fn", "call_line": 30},
        "b.mid": {"file_path": "b.py", "kind": "method", "call_line": 20},
        "c.leaf": {"file_path": "c.py", "kind": "fn", "call_line": 10},
    }
    block = summarize_edges({"a.fast": 1.0, "b.mid": 0.8, "c.leaf": 0.2}, metadata)
    for row in block["per_symbol"] + block["ranked"]:
        for field in ("qualified_name", "target_uid", "file_path", "kind", "call_line"):
            assert field in row, f"候选缺少字段 {field}"
        assert row["target_uid"] == row["qualified_name"]
    by_name = {row["qualified_name"]: row for row in block["ranked"]}
    assert by_name["a.fast"]["file_path"] == "a.py"
    assert by_name["b.mid"]["kind"] == "method"
    assert by_name["c.leaf"]["call_line"] == 10


def test_summarize_edges_ranked_order_stable():
    """ranked：置信度降序 → 调用行号升序 → 限定名兜底，重复调用结果一致。"""
    metadata = {
        "x.a": {"call_line": 20},
        "x.b": {"call_line": 10},
        "x.c": {"call_line": 30},
    }
    weights = {"x.a": 0.8, "x.b": 0.8, "x.c": 0.8, "x.high": 1.0, "x.low": 0.2}
    metadata["x.high"] = {"call_line": 40}
    metadata["x.low"] = {"call_line": 50}
    first = summarize_edges(weights, metadata)["ranked"]
    second = summarize_edges(weights, metadata)["ranked"]
    order = [row["qualified_name"] for row in first]
    assert order == [
        "x.high",   # 1.0
        "x.b", "x.a", "x.c",  # 0.8，按行号 10/20/30
        "x.low",    # 0.2
    ]
    assert [row["qualified_name"] for row in second] == order


def test_summarize_edges_ranked_unknown_call_line_last_in_tier():
    """同置信度下未知调用行号（0）排在已知行号之后。"""
    metadata = {"x.unknown": {"call_line": 0}, "x.known": {"call_line": 7}}
    ranked = summarize_edges({"x.unknown": 0.4, "x.known": 0.4}, metadata)["ranked"]
    assert [row["qualified_name"] for row in ranked] == ["x.known", "x.unknown"]


def test_summarize_edges_min_confidence_filters_candidates():
    """min_confidence=0.4 过滤掉 UNAFFECTED 候选，全局统计不变。"""
    weights = {"a.fast": 1.0, "b.mid": 0.8, "c.edge": 0.4, "d.low": 0.2}
    metadata = {name: {"call_line": idx + 1} for idx, name in enumerate(weights)}
    full = summarize_edges(weights, metadata)
    assert len(full["ranked"]) == 4

    filtered = summarize_edges(weights, metadata, 0.4)
    assert [row["qualified_name"] for row in filtered["ranked"]] == [
        "a.fast", "b.mid", "c.edge",
    ]
    assert [row["qualified_name"] for row in filtered["per_symbol"]] == [
        "c.edge", "b.mid", "a.fast",
    ]
    assert all(row["confidence"] >= 0.4 for row in filtered["ranked"])
    # 全量统计字段不受过滤影响
    assert filtered["histogram"] == full["histogram"]
    assert filtered["min_confidence"] == full["min_confidence"]
    # 缺省 None 与不传参一致
    assert summarize_edges(weights, metadata, None) == full


def test_summarize_edges_per_symbol_carries_metadata():
    """per_symbol 每条候选同样带收窄字段，且保持升序口径。"""
    metadata = {
        "m.b": {"file_path": "b.py", "kind": "method", "call_line": 8},
        "m.a": {"file_path": "a.py", "kind": "fn", "call_line": 5},
    }
    block = summarize_edges({"m.a": 0.8, "m.b": 0.2}, metadata)
    assert [row["qualified_name"] for row in block["per_symbol"]] == ["m.b", "m.a"]
    for row in block["per_symbol"]:
        for field in ("qualified_name", "target_uid", "file_path", "kind", "call_line"):
            assert field in row, f"per_symbol 缺少字段 {field}"
        assert row["target_uid"] == row["qualified_name"]
    by_name = {row["qualified_name"]: row for row in block["per_symbol"]}
    assert (by_name["m.a"]["file_path"], by_name["m.a"]["kind"], by_name["m.a"]["call_line"]) == (
        "a.py", "fn", 5,
    )




def test_security_named_detection():
    assert is_security_named("authenticate_user", "mod.authenticate_user")
    assert is_security_named("rotate_api_key")
    assert is_security_named("LoginHandler")
    assert not is_security_named("render_page", "mod.render_page")


def test_noisy_raw_name_guard():
    assert is_noisy_raw_name("get")
    assert is_noisy_raw_name("ab")
    assert not is_noisy_raw_name("resolve_target")


# ----------------------------------------------------------------------
# B. 集成（review_readiness_report）
# ----------------------------------------------------------------------

def _insert_file(
    db: CodeGraphDB, workspace_id: int, rel_path: str, module_path: str
) -> int:
    # file_instances.current_content_hash 有 FK 指向 file_contents（默认 ''）
    db.conn.execute(
        "INSERT OR IGNORE INTO file_contents "
        "(content_hash, language, total_lines, first_seen_at) VALUES ('', '', 0, 0)"
    )
    cur = db.conn.execute(
        """
        INSERT INTO file_instances (
            workspace_id, rel_path, abs_path, mtime, status, module_path
        ) VALUES (?, ?, ?, 0, 'active', ?)
        """,
        (workspace_id, rel_path, f"/{rel_path}", module_path),
    )
    return int(cur.lastrowid)


def _insert_symbol(
    db: CodeGraphDB,
    file_instance_id: int,
    symbol_hash: str,
    name: str,
    qualified_name: str,
    module_path: str,
) -> int:
    # symbols.symbol_hash 有 FK 指向 symbol_contents.content_hash
    db.conn.execute(
        "INSERT OR IGNORE INTO symbol_contents "
        "(content_hash, name, kind, content, qualified_name) VALUES (?, ?, 'fn', '', ?)",
        (symbol_hash, name, qualified_name),
    )
    cur = db.conn.execute(
        """
        INSERT INTO symbols (
            file_instance_id, symbol_hash, name, kind, visibility,
            start_line, end_line, qualified_name, module_path
        ) VALUES (?, ?, ?, 'fn', 'public', 1, 2, ?, ?)
        """,
        (file_instance_id, symbol_hash, name, qualified_name, module_path),
    )
    return int(cur.lastrowid)


def _insert_call(
    db: CodeGraphDB,
    caller_id: int,
    caller_name: str,
    callee_name: str,
    callee_qualified: str,
    callee_id: int,
    line: int,
) -> None:
    db.conn.execute(
        """
        INSERT INTO calls (
            caller_id, caller_name, caller_module, callee_name,
            callee_qualified, callee_id, call_line
        ) VALUES (?, ?, 'mod', ?, ?, ?, ?)
        """,
        (caller_id, caller_name, callee_name, callee_qualified, callee_id, line),
    )


def _build_risk_fixture(db: CodeGraphDB, ws_id: int) -> None:
    """源 → (跨文件调用方 ×2 + 同文件调用方) → 二层 leaf，外加 raw 候选边。"""
    src_file = _insert_file(db, ws_id, "src.py", "pkg_src")
    mid_file = _insert_file(db, ws_id, "mid.py", "pkg_mid")
    auth_file = _insert_file(db, ws_id, "auth.py", "pkg_auth")

    source = _insert_symbol(db, src_file, "h-source", "source", "pkg_src.source", "pkg_src")
    local = _insert_symbol(db, src_file, "h-local", "local_helper", "pkg_src.local_helper", "pkg_src")
    mid = _insert_symbol(db, mid_file, "h-mid", "mid", "pkg_mid.mid", "pkg_mid")
    leaf = _insert_symbol(db, mid_file, "h-leaf", "leaf", "pkg_mid.leaf", "pkg_mid")
    auth = _insert_symbol(
        db, auth_file, "h-auth", "authenticate_user", "pkg_auth.authenticate_user", "pkg_auth"
    )
    orphan = _insert_symbol(db, auth_file, "h-orphan", "orphan", "pkg_auth.orphan", "pkg_auth")

    # 已解析边：caller → source（同文件 = EXTRACTED，跨文件 = INFERRED）
    _insert_call(db, local, "local_helper", "source", "pkg_src.source", source, 10)
    _insert_call(db, mid, "mid", "source", "pkg_src.source", source, 11)
    _insert_call(db, auth, "authenticate_user", "source", "pkg_src.source", source, 12)
    # 二层：leaf → mid（同文件）
    _insert_call(db, leaf, "leaf", "mid", "pkg_mid.mid", mid, 13)

    # raw 边：leaf 另有一条未解析的 qualified 边指向 mid（AMBIGUOUS）
    _insert_call(db, leaf, "leaf", "mid", "pkg_mid.mid", 0, 14)
    # raw 边：orphan 只有裸名 source（UNRESOLVED，且不在影响树内 → 候选）
    _insert_call(db, orphan, "orphan", "source", "", 0, 15)


def test_review_readiness_integrates_risk_score_and_edge_confidence(tmp_path):
    db = CodeGraphDB(str(tmp_path / "risk.db"), workspace_root=str(tmp_path))
    try:
        ws_id = db.register_workspace("ws", str(tmp_path), "")
        _build_risk_fixture(db, ws_id)
        db.conn.commit()
        db.set_active_workspace(ws_id)

        # 强制走 Python SQL 路径（禁用 Rust GraphStore 短路）
        original_get_graph_store = db._get_graph_store
        db._get_graph_store = lambda: None
        try:
            report = db.review_readiness_report("h-source")
        finally:
            db._get_graph_store = original_get_graph_store

        # 旧字段向后兼容
        for key in (
            "impact_scope", "risk_level", "total_impacted", "must_test",
            "review_points", "by_layer",
        ):
            assert key in report, f"旧字段 {key} 丢失"
        assert report["risk_level"] == report["impact_scope"] == "low"
        assert report["total_impacted"] == 5  # source + 3 个一层 + leaf

        # 风险评分：信号 → 同源加法
        signals = report["risk_signals"]
        assert signals["depth_reach"] == 0.1        # 最大深度 2 层
        assert signals["cross_module"] == 0.1       # pkg_mid + pkg_auth
        assert signals["test_gap"] == 0.25          # 5 个 public 必测项（含源符号），0 个测试符号
        assert signals["security_surface"] == 0.2   # authenticate_user
        assert signals["churn"] == 0.0              # 非 git 仓库
        assert report["risk_score"] == compute_risk_score(signals)

        # 边置信度四档
        block = report["edge_confidence"]
        by_name = {row["qualified_name"]: row for row in block["per_symbol"]}
        assert by_name["pkg_src.local_helper"]["confidence"] == 1.0      # 同文件
        assert by_name["pkg_mid.mid"]["confidence"] == 0.8               # 跨文件
        assert by_name["pkg_auth.authenticate_user"]["confidence"] == 0.8
        assert by_name["pkg_mid.leaf"]["confidence"] == 0.4             # min(1.0, raw 0.4)
        assert by_name["pkg_auth.orphan"]["confidence"] == 0.2          # 裸名候选
        assert block["min_confidence"] == 0.2
        assert block["classification"] == "UNAFFECTED"
        assert block["histogram"] == {
            "WILL_BREAK": 3,
            "LIKELY_AFFECTED": 1,
            "UNAFFECTED": 1,
        }
        assert report["weighted_impacted"] == 3.2
    finally:
        db.close()


def test_review_readiness_degrades_gracefully_for_unknown_symbol(tmp_path):
    db = CodeGraphDB(str(tmp_path / "risk-empty.db"), workspace_root=str(tmp_path))
    try:
        ws_id = db.register_workspace("ws", str(tmp_path), "")
        db.set_active_workspace(ws_id)
        original_get_graph_store = db._get_graph_store
        db._get_graph_store = lambda: None
        try:
            report = db.review_readiness_report("h-missing")
        finally:
            db._get_graph_store = original_get_graph_store

        assert report["total_impacted"] == 0
        assert report["risk_score"] == 0.0
        assert report["risk_signals"] == {}
        assert report["edge_confidence"]["per_symbol"] == []
        assert report["edge_confidence"]["ranked"] == []
        assert report["weighted_impacted"] == 0.0
    finally:
        db.close()


def test_review_readiness_ranked_candidates_have_metadata(tmp_path):
    """ranked 候选带 target_uid/file_path/kind/call_line，且按置信度+行号排序。"""
    db = CodeGraphDB(str(tmp_path / "risk-ranked.db"), workspace_root=str(tmp_path))
    try:
        ws_id = db.register_workspace("ws", str(tmp_path), "")
        _build_risk_fixture(db, ws_id)
        db.conn.commit()
        db.set_active_workspace(ws_id)

        original_get_graph_store = db._get_graph_store
        db._get_graph_store = lambda: None
        try:
            report = db.review_readiness_report("h-source")
        finally:
            db._get_graph_store = original_get_graph_store

        ranked = report["edge_confidence"]["ranked"]
        # 置信度降序：1.0 → 0.8（行 11、12）→ 0.4 → 0.2
        assert [row["qualified_name"] for row in ranked] == [
            "pkg_src.local_helper",
            "pkg_mid.mid",
            "pkg_auth.authenticate_user",
            "pkg_mid.leaf",
            "pkg_auth.orphan",
        ]
        by_name = {row["qualified_name"]: row for row in ranked}
        for row in ranked:
            for field in ("qualified_name", "target_uid", "file_path", "kind", "call_line"):
                assert field in row, f"候选缺少字段 {field}"
            assert row["target_uid"] == row["qualified_name"]

        local = by_name["pkg_src.local_helper"]
        assert (local["file_path"], local["kind"], local["call_line"]) == ("src.py", "fn", 10)
        mid = by_name["pkg_mid.mid"]
        assert (mid["file_path"], mid["call_line"]) == ("mid.py", 11)
        leaf = by_name["pkg_mid.leaf"]
        # leaf 有 resolved(行13,1.0) 与 raw(行14,0.4) 两条边，取最低权重边行号
        assert (leaf["file_path"], leaf["call_line"]) == ("mid.py", 14)
        orphan = by_name["pkg_auth.orphan"]
        assert (orphan["file_path"], orphan["call_line"]) == ("auth.py", 15)

        # 稳定可复现：再次生成顺序一致
        original_get_graph_store = db._get_graph_store
        db._get_graph_store = lambda: None
        try:
            again = db.review_readiness_report("h-source")
        finally:
            db._get_graph_store = original_get_graph_store
        assert [row["qualified_name"] for row in again["edge_confidence"]["ranked"]] == [
            row["qualified_name"] for row in ranked
        ]
    finally:
        db.close()


def test_review_readiness_min_confidence_filters_unaaffected(tmp_path):
    """min_confidence=0.4 过滤 UNAFFECTED 候选；None/不传参与原行为一致。"""
    db = CodeGraphDB(str(tmp_path / "risk-filter.db"), workspace_root=str(tmp_path))
    try:
        ws_id = db.register_workspace("ws", str(tmp_path), "")
        _build_risk_fixture(db, ws_id)
        db.conn.commit()
        db.set_active_workspace(ws_id)

        original_get_graph_store = db._get_graph_store
        db._get_graph_store = lambda: None
        try:
            base = db.review_readiness_report("h-source")
            same = db.review_readiness_report("h-source", min_confidence=None)
            filtered = db.review_readiness_report("h-source", min_confidence=0.4)
        finally:
            db._get_graph_store = original_get_graph_store

        # 不传参 / None → 行为完全一致（向后兼容）
        assert same == base
        assert "pkg_auth.orphan" in {
            row["qualified_name"] for row in base["edge_confidence"]["ranked"]
        }
        assert base["edge_confidence"]["min_confidence"] == 0.2

        # 0.4 → 过滤掉 0.2 的 UNAFFECTED 候选
        ranked = filtered["edge_confidence"]["ranked"]
        assert [row["qualified_name"] for row in ranked] == [
            "pkg_src.local_helper",
            "pkg_mid.mid",
            "pkg_auth.authenticate_user",
            "pkg_mid.leaf",
        ]
        assert all(row["confidence"] >= 0.4 for row in ranked)
        assert all(
            row["confidence"] >= 0.4
            for row in filtered["edge_confidence"]["per_symbol"]
        )
        # per_symbol 同样被收窄，且保持 confidence 升序 + 限定名兜底
        assert [
            row["qualified_name"] for row in filtered["edge_confidence"]["per_symbol"]
        ] == [
            "pkg_mid.leaf",
            "pkg_auth.authenticate_user",
            "pkg_mid.mid",
            "pkg_src.local_helper",
        ]
        # 旧字段不受过滤影响
        assert filtered["risk_level"] == base["risk_level"]
        assert filtered["must_test"] == base["must_test"]
        assert filtered["total_impacted"] == base["total_impacted"]
        # 全局直方图仍反映全量候选
        assert filtered["edge_confidence"]["histogram"] == base["edge_confidence"]["histogram"]
    finally:
        db.close()
