# -*- coding: utf-8 -*-
"""Smart CI 检查集选择（db_impact.smart_ci_plan）测试。

覆盖验收口径：
- 给定合成变更能稳定选出检查集（同一输入两次调用结果完全一致）；
- 无受影响符号时返回空检查集（checks / test_selection 均为空）；
- 复用 test_impact_selection 选出测试、复用 guardrail 规则产出检查项。
"""

from __future__ import annotations

from callwarden.db.db import CodeGraphDB


def _insert_file(db: CodeGraphDB, ws_id: int, rel_path: str, content: str) -> int:
    """写入磁盘文件并登记 file_instances（abs_path 指向真实文件）。"""
    abs_path = db.workspace_root.rstrip("/\\") + "/" + rel_path
    import os

    os.makedirs(os.path.dirname(abs_path), exist_ok=True)
    with open(abs_path, "w", encoding="utf-8") as f:
        f.write(content)
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
        (ws_id, rel_path, abs_path, rel_path.rsplit(".", 1)[0].replace("/", ".")),
    )
    return int(cur.lastrowid)


def _insert_symbol(
    db: CodeGraphDB,
    file_instance_id: int,
    symbol_hash: str,
    name: str,
    qualified_name: str,
    start_line: int,
    end_line: int,
) -> int:
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
        ) VALUES (?, ?, ?, 'fn', 'public', ?, ?, ?, ?)
        """,
        (file_instance_id, symbol_hash, name, start_line, end_line, qualified_name,
         qualified_name.rsplit(".", 1)[0] if "." in qualified_name else qualified_name),
    )
    return int(cur.lastrowid)


def _insert_call(
    db: CodeGraphDB,
    caller_id: int,
    caller_name: str,
    callee_id: int,
    callee_qualified: str,
    line: int,
) -> None:
    db.conn.execute(
        """
        INSERT INTO calls (
            caller_id, caller_name, caller_module, callee_name,
            callee_qualified, callee_id, call_line
        ) VALUES (?, ?, 'mod', ?, ?, ?, ?)
        """,
        (caller_id, caller_name, callee_qualified.rsplit(".", 1)[-1],
         callee_qualified, callee_id, line),
    )


def _build_fixture(db: CodeGraphDB, ws_id: int) -> None:
    """源函数 + 测试调用方 + 触发 guardrail 的高风险文件。"""
    src_file = _insert_file(
        db, ws_id, "pkg_src.py",
        "def source():\n    return 1\n",
    )
    test_file = _insert_file(
        db, ws_id, "tests/test_src.py",
        "def test_source():\n    assert source() == 1\n",
    )
    _insert_file(
        db, ws_id, "risky.py",
        "# BREAKING CHANGE: drop legacy parameter\nvalue = 1\n",
    )

    source_id = _insert_symbol(
        db, src_file, "h-source", "source", "pkg_src.source", 1, 2,
    )
    test_id = _insert_symbol(
        db, test_file, "h-test", "test_source", "tests.test_src.test_source", 1, 2,
    )
    _insert_call(db, test_id, "test_source", source_id, "pkg_src.source", 2)
    db.conn.commit()


def _make_db(tmp_path) -> CodeGraphDB:
    db = CodeGraphDB(str(tmp_path / "smart_ci.db"), workspace_root=str(tmp_path))
    ws_id = db.register_workspace("ws", str(tmp_path), "")
    _build_fixture(db, ws_id)
    db.set_active_workspace(ws_id)
    # 强制走 Python SQL BFS（禁用 Rust GraphStore 短路），保证结果与环境无关
    db._get_graph_store = lambda: None
    return db


def test_smart_ci_selects_minimal_checks_reproducibly(tmp_path):
    db = _make_db(tmp_path)
    try:
        plan = db.smart_ci_plan(changed_files=["pkg_src.py", "risky.py"])

        # 影响面：源函数 + 其调用方（测试函数）都进入影响集
        assert [s["qualified_name"] for s in plan["impacted_symbols"]] == [
            "pkg_src.source",
            "tests.test_src.test_source",
        ]
        # 复用 test_impact_selection：测试函数被选中，并记录覆盖的受影响符号
        assert plan["test_selection"] == [{
            "qualified_name": "tests.test_src.test_source",
            "name": "test_source",
            "file_path": "tests/test_src.py",
            "module_path": "tests.test_src",
            "start_line": 1,
            "covers": ["pkg_src.source"],
        }]

        # 最小充分检查集：测试 + 语法 + guardrail（其余命中为 incident 规则）
        assert [c["check_id"] for c in plan["checks"]] == [
            "guardrail:GR-builtin-api-1:risky.py",
            "guardrail:GR-builtin-inc-1:risky.py",
            "guardrail:GR-builtin-inc-2:risky.py",
            "guardrail:GR-builtin-inc-3:risky.py",
            "syntax:pkg_src.py",
            "syntax:risky.py",
            "test:tests/test_src.py",
        ]
        by_id = {c["check_id"]: c for c in plan["checks"]}
        guard = by_id["guardrail:GR-builtin-api-1:risky.py"]
        assert guard["kind"] == "guardrail"
        assert guard["severity"] == "block"
        assert guard["command"] == "cw guardrail scan --file risky.py"
        syntax = by_id["syntax:pkg_src.py"]
        assert syntax["command"] == "python -m py_compile pkg_src.py"
        test_check = by_id["test:tests/test_src.py"]
        assert test_check["command"] == "pytest -q tests/test_src.py"

        assert plan["selection"]["test_count"] == 1
        assert plan["selection"]["rule_ids"] == [
            "GR-builtin-api-1",
            "GR-builtin-inc-1",
            "GR-builtin-inc-2",
            "GR-builtin-inc-3",
        ]

        # 同一变更反复调用 → 输出完全一致
        again = db.smart_ci_plan(changed_files=["risky.py", "pkg_src.py"])
        assert again == plan
    finally:
        db.close()


def test_smart_ci_empty_when_no_impacted_symbols(tmp_path):
    db = _make_db(tmp_path)
    try:
        plan = db.smart_ci_plan(changed_files=["ghost_file_xyz.py"])
        assert plan["impacted_symbols"] == []
        assert plan["test_selection"] == []
        assert plan["checks"] == []
        assert plan["summary"] == "无受影响符号，检查集为空"
    finally:
        db.close()


def test_smart_ci_accepts_diff_text(tmp_path):
    db = _make_db(tmp_path)
    try:
        diff_text = (
            "--- a/pkg_src.py\n"
            "+++ b/pkg_src.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def source():\n"
            "-    return 1\n"
            "+    return 2\n"
        )
        plan = db.smart_ci_plan(diff_text=diff_text)
        assert plan["changed_files"] == ["pkg_src.py"]
        assert [s["qualified_name"] for s in plan["seed_symbols"]] == [
            "pkg_src.source",
        ]
        assert [t["qualified_name"] for t in plan["test_selection"]] == [
            "tests.test_src.test_source",
        ]
        assert "test:tests/test_src.py" in [c["check_id"] for c in plan["checks"]]
    finally:
        db.close()


def _insert_agent_rule(
    db: CodeGraphDB,
    rule_id: str,
    title: str,
    rule_text: str,
    scope: dict,
    severity: str,
    evidence: dict,
) -> None:
    import json

    db.conn.execute(
        """
        INSERT INTO agent_rules (
            id, title, rule_text, scope_json, severity, status,
            evidence_json, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, 'active', ?, 0, 0)
        """,
        (rule_id, title, rule_text, json.dumps(scope), severity,
         json.dumps(evidence)),
    )


def _insert_guardrail_rule(
    db: CodeGraphDB,
    rule_id: str,
    pattern: str,
    description: str,
    severity: str = "block",
) -> None:
    db.conn.execute(
        """
        INSERT INTO guardrail_rules (
            rule_id, category, severity, pattern, action, description,
            is_builtin, created_at
        ) VALUES (?, 'custom', ?, ?, 'block', ?, 0, 1)
        """,
        (rule_id, severity, pattern, description),
    )


def test_verify_design_hits_constraints_with_design_refs(tmp_path):
    db = _make_db(tmp_path)
    try:
        _insert_agent_rule(
            db, "AR-design-1", "写操作必须走事务封装",
            "见 docs/design/tx-design.md#L12：写操作必须走事务封装",
            {"file_patterns": ["pkg_src.py"]}, "error",
            {"doc": "docs/design/tx-design.md"},
        )
        _insert_guardrail_rule(
            db, "GR-test-pattern", r"BREAKING CHANGE",
            "兼容性条款见 docs/design/api-design.md",
        )
        db.conn.commit()

        result = db.verify_design(changed_files=["pkg_src.py", "risky.py"])

        # agent_rules：仅命中 file_patterns=pkg_src.py 的符号上下文
        agent_hits = [v for v in result["violations"] if v["source"] == "agent_rule"]
        assert len(agent_hits) == 1
        assert agent_hits[0]["rule_id"] == "AR-design-1"
        assert agent_hits[0]["qualified_name"] == "pkg_src.source"
        assert agent_hits[0]["file_path"] == "pkg_src.py"
        assert agent_hits[0]["rule_ref"] == "rule:AR-design-1"
        assert agent_hits[0]["design_refs"] == [
            "docs/design/tx-design.md",
            "docs/design/tx-design.md#L12",
        ]

        # guardrail_rules：pattern 命中变更文件 risky.py，给出条文引用
        guard_hits = [
            v for v in result["violations"] if v["source"] == "guardrail_rule"
        ]
        assert len(guard_hits) == 1
        assert guard_hits[0]["rule_id"] == "GR-test-pattern"
        assert guard_hits[0]["file_path"] == "risky.py"
        assert guard_hits[0]["lines"] == [1]
        assert guard_hits[0]["rule_ref"] == "guardrail:GR-test-pattern"
        assert guard_hits[0]["design_refs"] == ["docs/design/api-design.md"]

        assert result["design_refs"] == [
            "docs/design/api-design.md",
            "docs/design/tx-design.md",
            "docs/design/tx-design.md#L12",
            "guardrail:GR-test-pattern",
            "rule:AR-design-1",
        ]
        assert result["summary"] == "命中设计约束 2 项"
    finally:
        db.close()


def test_verify_design_empty_when_no_constraints_hit(tmp_path):
    db = _make_db(tmp_path)
    try:
        result = db.verify_design(changed_files=["pkg_src.py"])
        assert result["violations"] == []
        assert result["design_refs"] == []
        assert result["summary"] == "未命中设计约束"

        # 无受影响符号 → 空结果且不报错
        empty = db.verify_design(changed_files=["ghost_file_xyz.py"])
        assert empty["violations"] == []
        assert empty["design_refs"] == []
        assert empty["summary"] == "无受影响符号，未命中设计约束"
    finally:
        db.close()

