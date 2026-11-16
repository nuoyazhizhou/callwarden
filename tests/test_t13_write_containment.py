"""
test_t13_write_containment.py
=============================

T-13（GAP-SEC-01 同族）越权写收敛回归测试。

T-5 修复（2e7b2d6）只收敛了 propose_edit；本文件覆盖其余 4 个 agent 可达
的 db 写入口，统一验证「越界路径一律失败且不落盘、合法路径行为不回归」：

1. export_module_graph output_file（READ_ONLY 类写副作用，db_query.py）
2. rule_sync_agents_md target_path（db_agent_rules.py）
3. rule_insert_agents_md_block target_path（db_agent_rules.py）
4. restore_comment / restore_all_comments（DB rel_path 投毒防御纵深，
   db_comment.py）

运行方式:
    pytest tests/test_t13_write_containment.py -v
"""
import os
import sys
import tempfile

import pytest

# 确保能导入 callwarden 包
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from callwarden.db import CodeGraphDB


# ===========================================================================
# 辅助函数
# ===========================================================================

def _create_tmp_db():
    """创建临时数据库并注册工作区，返回 (db, tmpdir)"""
    tmpdir = tempfile.mkdtemp()
    db_path = os.path.join(tmpdir, "t13_test.db")
    db = CodeGraphDB(db_path, workspace_root=tmpdir)
    db.register_workspace("t13-ws", tmpdir)
    return db, tmpdir


# ===========================================================================
# 入口 1：export_module_graph output_file
# ===========================================================================

def test_export_module_graph_rejects_relative_traversal():
    """`..` 相对穿越必须 raise ValueError 且不落盘"""
    db, tmpdir = _create_tmp_db()
    escape_path = os.path.abspath(os.path.join(tmpdir, "..", "escape_export.mmd"))

    with pytest.raises(ValueError, match="workspace"):
        db.export_module_graph(format="mermaid", output_file="../escape_export.mmd")

    assert not os.path.exists(escape_path), f"越界写入实际落盘: {escape_path}"


def test_export_module_graph_rejects_absolute_escape():
    """workspace 外绝对路径必须 raise ValueError 且不落盘"""
    db, tmpdir = _create_tmp_db()
    outside_dir = tempfile.mkdtemp()
    escape_path = os.path.join(outside_dir, "escape_abs.mmd")

    with pytest.raises(ValueError, match="workspace"):
        db.export_module_graph(format="mermaid", output_file=escape_path)

    assert not os.path.exists(escape_path), f"越界写入实际落盘: {escape_path}"


def test_export_module_graph_allows_in_workspace():
    """合法 workspace 内路径不回归：正常落盘并返回路径"""
    db, tmpdir = _create_tmp_db()
    target = os.path.join(tmpdir, "graph.mmd")

    result = db.export_module_graph(format="mermaid", output_file=target)

    assert result == target
    assert os.path.isfile(target), "workspace 内合法导出未落盘"
    with open(target, "r", encoding="utf-8") as f:
        assert f.read().startswith("flowchart TD")


# ===========================================================================
# 入口 2：rule_sync_agents_md target_path
# ===========================================================================

def test_rule_sync_agents_md_rejects_absolute_escape():
    """workspace 外绝对路径必须 success=False 且原文件不被触碰"""
    db, tmpdir = _create_tmp_db()
    outside_dir = tempfile.mkdtemp()
    escape_path = os.path.join(outside_dir, "AGENTS.md")
    with open(escape_path, "w", encoding="utf-8") as f:
        f.write("victim content")

    result = db.rule_sync_agents_md(target_path=escape_path, dry_run=False)

    assert result["success"] is False
    assert "workspace" in result["error"]
    with open(escape_path, "r", encoding="utf-8") as f:
        assert f.read() == "victim content", "越界路径原文件被改写"


def test_rule_sync_agents_md_rejects_relative_traversal():
    """`..` 相对穿越必须 success=False 且不落盘"""
    db, tmpdir = _create_tmp_db()
    escape_path = os.path.abspath(os.path.join(tmpdir, "..", "escape_sync.md"))

    result = db.rule_sync_agents_md(target_path="../escape_sync.md", dry_run=False)

    assert result["success"] is False
    assert "workspace" in result["error"]
    assert not os.path.exists(escape_path), f"越界写入实际落盘: {escape_path}"


def test_rule_sync_agents_md_allows_in_workspace():
    """合法 workspace 内 AGENTS.md 不回归：正常写入标记区"""
    db, tmpdir = _create_tmp_db()
    agents_md = os.path.join(tmpdir, "AGENTS.md")
    marker_block = (
        f"{db._MARKER_START}\n{db._MARKER_BLOCK_HINT}\n{db._MARKER_END}\n"
    )
    with open(agents_md, "w", encoding="utf-8") as f:
        f.write("# Project\n" + marker_block)

    result = db.rule_sync_agents_md(target_path="AGENTS.md", dry_run=False)

    assert result["success"] is True, f"合法写入被误拒: {result.get('error')}"
    with open(agents_md, "r", encoding="utf-8") as f:
        content = f.read()
    assert db._MARKER_START in content
    assert "# Project" in content, "标记区外人工维护内容被破坏"


# ===========================================================================
# 入口 3：rule_insert_agents_md_block target_path
# ===========================================================================

def test_rule_insert_agents_md_block_rejects_absolute_escape():
    """workspace 外绝对路径必须 success=False 且不落盘"""
    db, tmpdir = _create_tmp_db()
    outside_dir = tempfile.mkdtemp()
    escape_path = os.path.join(outside_dir, "AGENTS.md")

    result = db.rule_insert_agents_md_block(target_path=escape_path)

    assert result["success"] is False
    assert "workspace" in result["message"]
    assert not os.path.exists(escape_path), f"越界写入实际落盘: {escape_path}"


def test_rule_insert_agents_md_block_rejects_relative_traversal():
    """`..` 相对穿越必须 success=False 且不落盘"""
    db, tmpdir = _create_tmp_db()
    escape_path = os.path.abspath(os.path.join(tmpdir, "..", "escape_insert.md"))

    result = db.rule_insert_agents_md_block(target_path="../escape_insert.md")

    assert result["success"] is False
    assert "workspace" in result["message"]
    assert not os.path.exists(escape_path), f"越界写入实际落盘: {escape_path}"


def test_rule_insert_agents_md_block_allows_in_workspace():
    """合法 workspace 内 AGENTS.md 不回归：正常插入标记块"""
    db, tmpdir = _create_tmp_db()
    agents_md = os.path.join(tmpdir, "AGENTS.md")
    with open(agents_md, "w", encoding="utf-8") as f:
        f.write("# Project\n")

    result = db.rule_insert_agents_md_block(target_path="AGENTS.md")

    assert result["success"] is True, f"合法写入被误拒: {result.get('message')}"
    with open(agents_md, "r", encoding="utf-8") as f:
        content = f.read()
    assert db._MARKER_START in content and db._MARKER_END in content


# ===========================================================================
# 入口 4：restore_comment / restore_all_comments（DB rel_path 投毒防御纵深）
# ===========================================================================

def _poisoned_info(file_path):
    """构造 get_comment_from_version 的投毒返回值（file_path 越出 workspace）"""
    return {
        "qualified_name": "mod::fn",
        "version_num": 1,
        "symbol_hash": "a" * 64,
        "file_path": file_path,
        "start_line": 1,
        "end_line": 2,
        "comment_content": "/// restored doc",
        "has_comment": 1,
        "full_content": "fn fn() {}",
    }


@pytest.mark.parametrize("poison_path", [
    "../poison_comment.rs",
    "C:\\absolutely\\outside\\poison_comment.rs",
])
def test_restore_comment_rejects_poisoned_rel_path(monkeypatch, poison_path):
    """restore_comment 遇投毒 rel_path 必须 success=False 且不落盘"""
    db, tmpdir = _create_tmp_db()
    monkeypatch.setattr(
        db, "get_comment_from_version", lambda spec: _poisoned_info(poison_path)
    )

    result = db.restore_comment("fn@v1", preview=False)

    assert result["success"] is False
    assert "workspace" in result["error"]
    escape_abs = os.path.normpath(os.path.join(tmpdir, poison_path))
    assert not os.path.exists(escape_abs), f"投毒路径实际落盘: {escape_abs}"


def test_restore_all_comments_skips_poisoned_rel_path(monkeypatch):
    """restore_all_comments 遇投毒 rel_path 必须记 errors 跳过且不落盘"""
    db, tmpdir = _create_tmp_db()

    class _FakeCursor:
        def fetchall(self):
            # (qualified_name, comment_content, file_path, parsed_at)
            return [("mod::fn", "/// doc", "../../poison_batch.rs", 1.0)]

    class _FakeConn:
        def execute(self, query, params):
            return _FakeCursor()

    monkeypatch.setattr(db, "_get_active_workspace_id", lambda: 1)
    monkeypatch.setattr(db, "conn", _FakeConn())

    result = db.restore_all_comments(preview=False)

    assert result["restored"] == 0
    assert result["skipped"] == 1
    assert any("workspace" in e for e in result["errors"])
    escape_abs = os.path.abspath(os.path.join(tmpdir, "..", "poison_batch.rs"))
    assert not os.path.exists(escape_abs), f"投毒路径实际落盘: {escape_abs}"


# ===========================================================================
# 主入口（与 test_fuzz.py 风格一致，支持直接运行）
# ===========================================================================

if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
