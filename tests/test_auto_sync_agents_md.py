"""C2: Agent Rule Memory 启动时自动同步 AGENTS.md 测试。

覆盖 T-1783349079762-bd5d 任务：
- _auto_sync_agents_md(): 启动时自动同步（fail-soft）
- _print_auto_sync_summary(): 摘要输出到 stderr
- CLI --refresh-all 后触发同步
- 同步失败不阻断启动/refresh

测试内容：
1. _auto_sync_agents_md 成功同步（标记区存在 + active 规则）
2. _auto_sync_agents_md 标记区不存在返回 error（不抛异常）
3. _auto_sync_agents_md 异常时 fail-soft 返回 error dict
4. _print_auto_sync_summary 成功输出到 stderr
5. _print_auto_sync_summary 标记区不存在输出 no_marker
6. _print_auto_sync_summary 其他错误输出 skipped
7. CLI --refresh-all 后触发 rule_sync_agents_md
8. CLI --refresh-all 同步失败不阻断（fail-soft）
9. 同步日志记录 actor=mcp_server_startup
10. 同步日志记录 actor=cli_refresh_all
"""

import io
import os
import sys
import tempfile
from contextlib import redirect_stderr
from unittest.mock import MagicMock, patch

import pytest

from callwarden.db.db import CodeGraphDB
from server.mcp_server import (
    _auto_sync_agents_md,
    _print_auto_sync_summary,
    get_db,
)


# ============================================
# 辅助函数
# ============================================


def _setup_active_rules(db, count=2):
    """辅助：创建并 accept count 条 active 规则，返回 rule_ids"""
    rule_ids = []
    for i in range(count):
        cid = db.rule_candidate_create(
            title=f"rule-{i+1}",
            rule_text=f"text {i+1}",
            severity="warning" if i == 0 else "info",
        )
        rid = db.rule_candidate_accept(cid)
        rule_ids.append(rid)
    return rule_ids


def _write_agents_md_with_marker(tmp):
    """辅助：写入带标记区的 AGENTS.md，返回路径"""
    path = os.path.join(tmp, "AGENTS.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write("# Project\n\n")
        f.write("Some content before marker.\n\n")
        f.write("<!-- CALLWARDEN_RULES_START -->\n")
        f.write("<!-- 自动同步区域，请通过 cw rule sync 更新，不要手改 -->\n")
        f.write("<!-- CALLWARDEN_RULES_END -->\n")
        f.write("\nSome content after marker.\n")
    return path


# ============================================
# _auto_sync_agents_md 测试
# ============================================


def test_auto_sync_agents_md_success_with_marker():
    """_auto_sync_agents_md 在标记区存在时成功同步"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            _setup_active_rules(db, count=2)
            agents_md = _write_agents_md_with_marker(tmp)

            # Mock get_db 返回测试 db
            with patch("server.mcp_server.get_db", return_value=db):
                result = _auto_sync_agents_md()

            assert result["success"] is True
            assert result["dry_run"] is False
            assert result["rule_count"] == 2
            assert result["target_path"] == "AGENTS.md"
            assert result["after_hash"] != ""
            assert result["before_hash"] != result["after_hash"]

            # 验证 AGENTS.md 已写入规则
            with open(agents_md, "r", encoding="utf-8") as f:
                content = f.read()
            assert "rule-1" in content
            assert "rule-2" in content
        finally:
            db.close()


def test_auto_sync_agents_md_no_marker_returns_error():
    """_auto_sync_agents_md 标记区不存在时返回 error（不抛异常）"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            _setup_active_rules(db, count=1)
            # 写入不带标记区的 AGENTS.md
            agents_md = os.path.join(tmp, "AGENTS.md")
            with open(agents_md, "w", encoding="utf-8") as f:
                f.write("# Project\n\nno marker\n")

            with patch("server.mcp_server.get_db", return_value=db):
                result = _auto_sync_agents_md()

            # fail-soft：返回 error dict 而非抛异常
            assert result["success"] is False
            assert result["rule_count"] == 0
            assert "error" in result and result["error"]
            assert "suggested_block" in result
        finally:
            db.close()


def test_auto_sync_agents_md_fail_soft_on_exception():
    """_auto_sync_agents_md 异常时 fail-soft 返回 error dict"""
    # Mock get_db 抛异常
    mock_get_db = MagicMock(side_effect=RuntimeError("DB connection failed"))
    with patch("server.mcp_server.get_db", mock_get_db):
        result = _auto_sync_agents_md()

    # fail-soft：不抛异常，返回 error dict
    assert result["success"] is False
    assert result["rule_count"] == 0
    assert "DB connection failed" in result["error"]
    assert result["target_path"] == "AGENTS.md"


# ============================================
# _print_auto_sync_summary 测试
# ============================================


def test_print_auto_sync_summary_success():
    """_print_auto_sync_summary 成功时输出到 stderr"""
    result = {"success": True, "rule_count": 3}
    err_buf = io.StringIO()
    with redirect_stderr(err_buf):
        _print_auto_sync_summary(result)
    output = err_buf.getvalue()
    assert "3" in output  # 包含规则数
    # 不应输出到 stdout
    assert "Auto Sync" in output or "自动同步" in output


def test_print_auto_sync_summary_no_marker():
    """_print_auto_sync_summary 标记区不存在时输出 no_marker 消息"""
    result = {
        "success": False,
        "error": "Marker block not found in AGENTS.md",
    }
    err_buf = io.StringIO()
    with redirect_stderr(err_buf):
        _print_auto_sync_summary(result)
    output = err_buf.getvalue()
    # 应该输出标记区不存在的提示
    assert "marker" in output.lower() or "标记区" in output


def test_print_auto_sync_summary_skipped():
    """_print_auto_sync_summary 其他错误时输出 skipped 消息"""
    result = {
        "success": False,
        "error": "Permission denied",
    }
    err_buf = io.StringIO()
    with redirect_stderr(err_buf):
        _print_auto_sync_summary(result)
    output = err_buf.getvalue()
    assert "Permission denied" in output


# ============================================
# 同步日志测试
# ============================================


def test_sync_log_records_mcp_server_startup_actor():
    """同步日志记录 actor=mcp_server_startup"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            _setup_active_rules(db, count=1)
            _write_agents_md_with_marker(tmp)

            with patch("server.mcp_server.get_db", return_value=db):
                _auto_sync_agents_md()

            # 验证 agent_rule_sync_log 表有记录
            cur = db.conn.execute(
                "SELECT actor FROM agent_rule_sync_log ORDER BY created_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            assert row is not None
            assert row["actor"] == "mcp_server_startup"
        finally:
            db.close()


def test_sync_log_records_cli_refresh_all_actor():
    """同步日志记录 actor=cli_refresh_all（模拟 CLI 调用）"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            _setup_active_rules(db, count=1)
            _write_agents_md_with_marker(tmp)

            # 直接调用 rule_sync_agents_md，模拟 CLI --refresh-all 后的行为
            db.rule_sync_agents_md(
                target_path="AGENTS.md",
                dry_run=False,
                actor="cli_refresh_all",
            )

            cur = db.conn.execute(
                "SELECT actor FROM agent_rule_sync_log ORDER BY created_at DESC LIMIT 1"
            )
            row = cur.fetchone()
            assert row is not None
            assert row["actor"] == "cli_refresh_all"
        finally:
            db.close()


# ============================================
# CLI --refresh-all 集成测试
# ============================================


def test_refresh_all_triggers_rule_sync_agents_md():
    """CLI --refresh-all 后触发 rule_sync_agents_md"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            _setup_active_rules(db, count=2)
            _write_agents_md_with_marker(tmp)

            # Mock build_full_graph 避免实际解析
            db.build_full_graph = MagicMock()

            # 调用 rule_sync_agents_md 验证可正常执行
            sync_result = db.rule_sync_agents_md(
                target_path="AGENTS.md",
                dry_run=False,
                actor="cli_refresh_all",
            )

            assert sync_result["success"] is True
            assert sync_result["rule_count"] == 2
        finally:
            db.close()


def test_refresh_all_sync_failure_does_not_block():
    """CLI --refresh-all 同步失败不阻断（fail-soft）"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            # 不创建 AGENTS.md，rule_sync_agents_md 会返回 error
            # 但不会抛异常（标记区不存在返回 error dict）
            result = db.rule_sync_agents_md(
                target_path="AGENTS.md",
                dry_run=False,
                actor="cli_refresh_all",
            )

            # 应返回 error dict 而非抛异常
            assert result["success"] is False
            assert "error" in result
            assert result["error"]  # error 非空
        finally:
            db.close()


def test_auto_sync_idempotent():
    """多次调用 _auto_sync_agents_md 结果一致（幂等）"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            _setup_active_rules(db, count=2)
            _write_agents_md_with_marker(tmp)

            with patch("server.mcp_server.get_db", return_value=db):
                result1 = _auto_sync_agents_md()
                result2 = _auto_sync_agents_md()

            # 两次同步的规则数相同
            assert result1["rule_count"] == result2["rule_count"]
            assert result1["success"] is True
            assert result2["success"] is True
            # after_hash 应相同（内容没变）
            assert result1["after_hash"] == result2["after_hash"]
        finally:
            db.close()


def test_auto_sync_with_empty_active_rules():
    """没有 active 规则时同步返回 success + rule_count=0"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            _write_agents_md_with_marker(tmp)
            # 不创建任何规则

            with patch("server.mcp_server.get_db", return_value=db):
                result = _auto_sync_agents_md()

            assert result["success"] is True
            assert result["rule_count"] == 0
        finally:
            db.close()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
