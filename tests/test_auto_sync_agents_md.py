"""C2/T03：Agent Rule Memory 同步 AGENTS.md 测试。

T03 收敛后，Python MCP server 不再于启动时本地写 AGENTS.md（写入权威下沉
daemon RPC `rule.sync_agents_md`），`CodeGraphDB.rule_sync_agents_md`
仍是本地工作区 DB 提供的同步实现：写文件 + 记录 agent_rule_sync_log +
标记规则 synced_to_agents_md=1。

覆盖（同步失败不阻断调用方，fail-soft 返回 error dict）：
1. 标记区存在 + active 规则 → 成功写入，actor 记录 mcp_server_startup
2. 标记区不存在 → 返回 error dict（不抛异常，不静默改写全文）
3. 无 active 规则 → success + rule_count=0
4. 重复同步幂等（after_hash 稳定）
5. actor=cli_refresh_all（CLI --refresh-all 触发路径）写入 agent_rule_sync_log
"""

import os
import tempfile

from callwarden.db.db import CodeGraphDB


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


def _last_sync_log_actor(db):
    cur = db.conn.execute(
        "SELECT actor FROM agent_rule_sync_log ORDER BY created_at DESC LIMIT 1"
    )
    row = cur.fetchone()
    return None if row is None else row["actor"]


# ============================================
# rule_sync_agents_md 同步测试
# ============================================


def test_sync_success_with_marker_records_mcp_server_startup_actor():
    """标记区存在 + active 规则 → 成功写入，actor=mcp_server_startup"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            _setup_active_rules(db, count=2)
            _write_agents_md_with_marker(tmp)

            result = db.rule_sync_agents_md(
                target_path="AGENTS.md",
                dry_run=False,
                actor="mcp_server_startup",
            )

            assert result["success"] is True
            assert result["dry_run"] is False
            assert result["rule_count"] == 2
            assert result["target_path"] == "AGENTS.md"
            assert result["after_hash"] != ""
            assert result["before_hash"] != result["after_hash"]

            # 验证 AGENTS.md 已写入规则
            with open(os.path.join(tmp, "AGENTS.md"), "r", encoding="utf-8") as f:
                content = f.read()
            assert "rule-1" in content
            assert "rule-2" in content

            # 同步日志记录 actor=mcp_server_startup
            assert _last_sync_log_actor(db) == "mcp_server_startup"
        finally:
            db.close()


def test_sync_no_marker_returns_error():
    """标记区不存在 → 返回 error dict（不抛异常、不静默改写全文）"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            _setup_active_rules(db, count=1)
            # 写入不带标记区的 AGENTS.md
            with open(os.path.join(tmp, "AGENTS.md"), "w", encoding="utf-8") as f:
                f.write("# Project\n\nno marker\n")

            result = db.rule_sync_agents_md(
                target_path="AGENTS.md",
                dry_run=False,
                actor="mcp_server_startup",
            )

            assert result["success"] is False
            assert result["rule_count"] == 0
            assert "error" in result and result["error"]
            assert "suggested_block" in result
            # 不静默改写无标记区文件
            with open(os.path.join(tmp, "AGENTS.md"), "r", encoding="utf-8") as f:
                assert "no marker" in f.read()
        finally:
            db.close()


def test_sync_with_empty_active_rules_returns_success_zero():
    """没有 active 规则 → success + rule_count=0"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            _write_agents_md_with_marker(tmp)

            result = db.rule_sync_agents_md(
                target_path="AGENTS.md",
                dry_run=False,
                actor="mcp_server_startup",
            )

            assert result["success"] is True
            assert result["rule_count"] == 0
        finally:
            db.close()


def test_sync_idempotent():
    """多次同步结果一致（幂等）：after_hash 稳定"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            _setup_active_rules(db, count=2)
            _write_agents_md_with_marker(tmp)

            result1 = db.rule_sync_agents_md(
                target_path="AGENTS.md",
                dry_run=False,
                actor="mcp_server_startup",
            )
            result2 = db.rule_sync_agents_md(
                target_path="AGENTS.md",
                dry_run=False,
                actor="mcp_server_startup",
            )

            assert result1["success"] is True
            assert result2["success"] is True
            assert result1["rule_count"] == result2["rule_count"]
            # after_hash 应相同（内容没变）
            assert result1["after_hash"] == result2["after_hash"]
        finally:
            db.close()


def test_sync_records_cli_refresh_all_actor():
    """actor=cli_refresh_all（CLI --refresh-all 触发路径）写入 agent_rule_sync_log"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            _setup_active_rules(db, count=1)
            _write_agents_md_with_marker(tmp)

            db.rule_sync_agents_md(
                target_path="AGENTS.md",
                dry_run=False,
                actor="cli_refresh_all",
            )

            assert _last_sync_log_actor(db) == "cli_refresh_all"
        finally:
            db.close()


def test_sync_missing_marker_does_not_block_caller():
    """同步失败不阻断调用方（fail-soft）：标记区缺失返回 error dict 而非抛异常"""
    with tempfile.TemporaryDirectory() as tmp:
        db = CodeGraphDB(workspace_root=tmp)
        try:
            # 不创建 AGENTS.md → rule_sync_agents_md 返回 error dict
            result = db.rule_sync_agents_md(
                target_path="AGENTS.md",
                dry_run=False,
                actor="cli_refresh_all",
            )

            assert result["success"] is False
            assert "error" in result
            assert result["error"]  # error 非空
        finally:
            db.close()
