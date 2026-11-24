"""rationale 入库与回读单测（任务 T-1795451980839-5932a434 step1）。

覆盖：
- 符号级 rationale 写入 symbol_rationale 表并按 file_version + qualified_name 回读
- get_symbol 返回 rationale 字段
- 无注释文件不产生 rationale 行，查询返回空列表（入库行为不变）
- 旧库缺表时查询 fail-soft 降级为空列表
"""

import hashlib

from callwarden.db.db import CodeGraphDB


def _make_db(tmp_path):
    db = CodeGraphDB(db_path=str(tmp_path / "graph.db"), workspace_root=str(tmp_path))
    # 强制 Python 写路径，避免依赖 Rust batch_save_symbols 的入参契约
    db.is_feature_rolled_back = lambda name: True
    return db


def _symbol(qualified_name, start_line, end_line, rationale=None):
    name = qualified_name.split(".")[-1]
    content = f"def {name}():\n    pass\n"
    sym = {
        "content_hash": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "name": name,
        "kind": "function",
        "visibility": "public",
        "start_line": start_line,
        "end_line": end_line,
        "start_col": 0,
        "end_col": 0,
        "signature": f"def {name}()",
        "has_comment": False,
        "comment_content": "",
        "content": content,
        "module_path": "mod",
        "qualified_name": qualified_name,
    }
    if rationale is not None:
        sym["rationale"] = rationale
    return sym


def _save_version(db, tmp_path, symbols):
    path = tmp_path / "mod.py"
    source = "def a():\n    pass\n\n\ndef b():\n    pass\n"
    path.write_text(source, encoding="utf-8")
    file_instance_id = db._register_file_db(str(path), "mod")
    result = {
        "abs_path": str(path),
        "rel_path": "mod.py",
        "module_path": "mod",
        "content_hash": hashlib.sha256(source.encode("utf-8")).hexdigest(),
        "total_lines": source.count("\n"),
        "language": "python",
        "symbols": symbols,
        "inline_modules": [],
    }
    file_version_id = db._save_file_version(file_instance_id, result)
    db._save_symbols_for_version(file_version_id, file_instance_id, result)
    db.conn.commit()
    return file_version_id


def test_rationale_persisted_and_returned(tmp_path):
    db = _make_db(tmp_path)
    rationale = [
        {"line": 1, "prefix": "WHY", "text": "必须保留旧路径"},
        {"line": 2, "prefix": "TODO", "text": "补缓存"},
    ]
    _save_version(db, tmp_path, [
        _symbol("mod.a", 1, 2, rationale=rationale),
        _symbol("mod.b", 5, 6),
    ])

    rows = db.conn.execute(
        "SELECT qualified_name, prefix, text FROM symbol_rationale ORDER BY line"
    ).fetchall()
    assert [(r["qualified_name"], r["prefix"], r["text"]) for r in rows] == [
        ("mod.a", "WHY", "必须保留旧路径"),
        ("mod.a", "TODO", "补缓存"),
    ]

    got = db.get_symbol("mod.a")
    assert got is not None
    assert got["rationale"] == rationale

    plain = db.get_symbol("mod.b")
    assert plain is not None
    assert plain["rationale"] == []


def test_no_rationale_no_rows(tmp_path):
    """无注释文件：rationale 表零行，get_symbol 仍返回空列表字段。"""
    db = _make_db(tmp_path)
    _save_version(db, tmp_path, [_symbol("mod.a", 1, 2)])

    assert db.conn.execute("SELECT COUNT(*) FROM symbol_rationale").fetchone()[0] == 0
    got = db.get_symbol("mod.a")
    assert got is not None
    assert got["rationale"] == []


def test_missing_table_degrades_to_empty(tmp_path):
    """旧库没有 symbol_rationale 扩展表时，查询 fail-soft 返回空列表。"""
    db = _make_db(tmp_path)
    db.conn.execute("DROP TABLE symbol_rationale")
    db.conn.commit()
    assert db._get_rationale_rows("symbol_hash = ?", ("abc",)) == []
