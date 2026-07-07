"""agent_rule_sync_log 表清理策略测试（C6）

验证：
1. db.cleanup_sync_log dry_run（默认）不删除，返回预估值
2. db.cleanup_sync_log dry_run=False 真正执行删除
3. total <= keep_latest 时仅按时间过滤
4. total > keep_latest 时双重过滤（时间 + 保留阈值）
5. 异常封装为 fail-soft（success=False + error）
6. 返回字段完整（success/dry_run/deleted_count/remaining_count/total_before/older_than_days/keep_latest）
7. 空表也能正常处理
8. CLI `cw rule cleanup-sync-log` 默认 dry-run，--apply 执行
9. CLI `--help` 不初始化数据库
10. MCP 工具 cleanup_agent_rule_sync_log 已注册
11. i18n key（zh_CN / en_US）存在且占位符齐全
"""
import json
import os
import sys
import tempfile
import time

import pytest

from callwarden.db.db import CodeGraphDB


# ----------------------------------------------------------------------
# 辅助
# ----------------------------------------------------------------------

def _db_with_workspace():
    """构造临时工作区数据库（触发完整 schema 初始化）。"""
    root = tempfile.mkdtemp()
    db = CodeGraphDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


def _insert_sync_log(db, count, base_ts=None, step=1.0):
    """向 agent_rule_sync_log 插入 count 条记录，created_at 从 base_ts 起按 step 递增。

    返回最大 created_at（用于断言）。
    """
    if base_ts is None:
        base_ts = time.time() - count * step
    for i in range(count):
        db.conn.execute(
            "INSERT INTO agent_rule_sync_log (id, target_path, created_at) VALUES (?, ?, ?)",
            (f"ARSL-test-{i}-{base_ts}", "AGENTS.md", base_ts + i * step),
        )
    db.conn.commit()
    return base_ts + (count - 1) * step


# ----------------------------------------------------------------------
# DB 层：dry_run
# ----------------------------------------------------------------------

def test_cleanup_default_dry_run_does_not_delete():
    """默认 dry_run=True 不删除记录。"""
    db, _ = _db_with_workspace()
    try:
        _insert_sync_log(db, 5)
        before = db.conn.execute("SELECT COUNT(*) FROM agent_rule_sync_log").fetchone()[0]
        assert before == 5

        result = db.cleanup_sync_log()  # 默认 dry_run=True
        after = db.conn.execute("SELECT COUNT(*) FROM agent_rule_sync_log").fetchone()[0]

        assert result["success"] is True
        assert result["dry_run"] is True
        # dry-run 不应删除任何记录
        assert after == 5
    finally:
        db.close()


def test_cleanup_dry_run_returns_projected_count():
    """dry_run 时 deleted_count 为预估值（SELECT COUNT），remaining 不变。"""
    db, _ = _db_with_workspace()
    try:
        # 插入 10 条 100 天前的记录
        old_ts = time.time() - 100 * 86400
        _insert_sync_log(db, 10, base_ts=old_ts)

        result = db.cleanup_sync_log(older_than_days=90, keep_latest=100, dry_run=True)

        assert result["success"] is True
        assert result["dry_run"] is True
        assert result["deleted_count"] == 10  # 预估删除 10 条
        assert result["remaining_count"] == 10  # 实际未删除
        assert result["total_before"] == 10
    finally:
        db.close()


# ----------------------------------------------------------------------
# DB 层：apply
# ----------------------------------------------------------------------

def test_cleanup_apply_actually_deletes():
    """dry_run=False 真正删除超期记录。"""
    db, _ = _db_with_workspace()
    try:
        old_ts = time.time() - 100 * 86400
        _insert_sync_log(db, 10, base_ts=old_ts)

        result = db.cleanup_sync_log(older_than_days=90, keep_latest=100, dry_run=False)

        assert result["success"] is True
        assert result["dry_run"] is False
        assert result["deleted_count"] == 10
        assert result["remaining_count"] == 0
        assert result["total_before"] == 10
    finally:
        db.close()


def test_cleanup_apply_keeps_recent_records():
    """apply 时保留 recent 记录（时间未超期）。"""
    db, _ = _db_with_workspace()
    try:
        # 5 条 100 天前 + 3 条 1 天前
        old_ts = time.time() - 100 * 86400
        _insert_sync_log(db, 5, base_ts=old_ts)
        recent_ts = time.time() - 1 * 86400
        _insert_sync_log(db, 3, base_ts=recent_ts)

        result = db.cleanup_sync_log(older_than_days=90, keep_latest=100, dry_run=False)

        assert result["success"] is True
        assert result["deleted_count"] == 5  # 删除 5 条旧记录
        assert result["remaining_count"] == 3  # 保留 3 条新记录
        assert result["total_before"] == 8
    finally:
        db.close()


# ----------------------------------------------------------------------
# DB 层：keep_latest 边界
# ----------------------------------------------------------------------

def test_cleanup_keep_latest_boundary_only_time_filter():
    """total <= keep_latest 时仅按时间过滤（不裁剪保留阈值）。"""
    db, _ = _db_with_workspace()
    try:
        # 插入 5 条 100 天前的记录（total=5 <= keep_latest=10）
        old_ts = time.time() - 100 * 86400
        _insert_sync_log(db, 5, base_ts=old_ts)

        result = db.cleanup_sync_log(older_than_days=90, keep_latest=10, dry_run=True)

        assert result["success"] is True
        # 5 条都超期，全部进入候选
        assert result["deleted_count"] == 5
    finally:
        db.close()


def test_cleanup_dual_filter_when_total_gt_keep_latest():
    """total > keep_latest 时双重过滤：时间超期 AND 不在最近 keep_latest 内。"""
    db, _ = _db_with_workspace()
    try:
        # 插入 20 条 100 天前的旧记录
        old_ts = time.time() - 100 * 86400
        _insert_sync_log(db, 20, base_ts=old_ts)
        # 插入 5 条 1 天前的新记录
        recent_ts = time.time() - 1 * 86400
        _insert_sync_log(db, 5, base_ts=recent_ts)
        # total = 25, keep_latest = 10 → 保留最近 10 条（5 新 + 5 旧中较新的）

        result = db.cleanup_sync_log(older_than_days=90, keep_latest=10, dry_run=True)

        assert result["success"] is True
        assert result["total_before"] == 25
        # 25 条都超期（created_at < cutoff_ts），但保留最近 10 条
        # 最近 10 条：5 条新 + 5 条旧（旧的 20 条中 created_at 最大的 5 条）
        # 所以预估删除 = 25 - 10 = 15
        assert result["deleted_count"] == 15
    finally:
        db.close()


def test_cleanup_dual_filter_apply_deletes_correct_count():
    """total > keep_latest 时 apply 实际删除数与预估值一致。"""
    db, _ = _db_with_workspace()
    try:
        old_ts = time.time() - 100 * 86400
        _insert_sync_log(db, 20, base_ts=old_ts)

        # dry-run 预估
        dry = db.cleanup_sync_log(older_than_days=90, keep_latest=5, dry_run=True)
        assert dry["deleted_count"] == 15  # 20 - 5

        # apply 执行
        applied = db.cleanup_sync_log(older_than_days=90, keep_latest=5, dry_run=False)
        assert applied["deleted_count"] == 15
        assert applied["remaining_count"] == 5
    finally:
        db.close()


# ----------------------------------------------------------------------
# DB 层：异常 / 边界
# ----------------------------------------------------------------------

def test_cleanup_exception_is_fail_soft():
    """db.conn.execute 抛异常时封装为 success=False，不外抛。"""
    db, _ = _db_with_workspace()
    try:
        # 替换整个 conn 为 mock（sqlite3.Connection.execute 实例属性只读，无法直接 patch）
        class _BoomConn:
            def execute(self, *args, **kwargs):
                raise RuntimeError("simulated db error")

            def close(self):
                pass

        db.conn = _BoomConn()
        result = db.cleanup_sync_log()

        assert result["success"] is False
        assert "simulated db error" in result["error"]
        assert result["deleted_count"] == 0
        assert result["remaining_count"] == -1
    finally:
        db.close()


def test_cleanup_empty_table():
    """空表也能正常处理。"""
    db, _ = _db_with_workspace()
    try:
        result = db.cleanup_sync_log(older_than_days=90, keep_latest=100, dry_run=True)

        assert result["success"] is True
        assert result["total_before"] == 0
        assert result["deleted_count"] == 0
        assert result["remaining_count"] == 0
    finally:
        db.close()


def test_cleanup_returns_required_fields():
    """返回 dict 包含所有必需字段。"""
    db, _ = _db_with_workspace()
    try:
        result = db.cleanup_sync_log()
        required = {
            "success", "dry_run", "deleted_count", "remaining_count",
            "total_before", "older_than_days", "keep_latest",
        }
        assert required.issubset(result.keys())
    finally:
        db.close()


def test_cleanup_echoes_params():
    """返回值回显 older_than_days / keep_latest 参数。"""
    db, _ = _db_with_workspace()
    try:
        result = db.cleanup_sync_log(older_than_days=30, keep_latest=50)
        assert result["older_than_days"] == 30
        assert result["keep_latest"] == 50
    finally:
        db.close()


# ----------------------------------------------------------------------
# CLI 层
# ----------------------------------------------------------------------

def test_cli_cleanup_dispatched_dry_run():
    """_handle_rule 能分发到 cleanup-sync-log action（默认 dry-run）。"""
    from callwarden.cli.main import _handle_rule

    class _MockDB:
        def __init__(self):
            self.calls = []

        def cleanup_sync_log(self, older_than_days=90, keep_latest=100, dry_run=True):
            self.calls.append(("cleanup_sync_log", older_than_days, keep_latest, dry_run))
            return {
                "success": True,
                "dry_run": dry_run,
                "deleted_count": 3,
                "remaining_count": 2,
                "total_before": 5,
                "older_than_days": older_than_days,
                "keep_latest": keep_latest,
            }

    mock_db = _MockDB()
    _handle_rule(["cleanup-sync-log"], mock_db)
    assert mock_db.calls[-1][0] == "cleanup_sync_log"
    assert mock_db.calls[-1][3] is True  # dry_run=True（默认）


def test_cli_cleanup_dispatched_apply():
    """--apply 触发 dry_run=False。"""
    from callwarden.cli.main import _handle_rule

    class _MockDB:
        def __init__(self):
            self.calls = []

        def cleanup_sync_log(self, older_than_days=90, keep_latest=100, dry_run=True):
            self.calls.append(("cleanup_sync_log", older_than_days, keep_latest, dry_run))
            return {
                "success": True,
                "dry_run": dry_run,
                "deleted_count": 3,
                "remaining_count": 2,
                "total_before": 5,
                "older_than_days": older_than_days,
                "keep_latest": keep_latest,
            }

    mock_db = _MockDB()
    _handle_rule(["cleanup-sync-log", "--apply"], mock_db)
    assert mock_db.calls[-1][3] is False  # dry_run=False


def test_cli_cleanup_custom_params():
    """--older-than / --keep-latest 透传到 db 方法。"""
    from callwarden.cli.main import _handle_rule

    class _MockDB:
        def __init__(self):
            self.calls = []

        def cleanup_sync_log(self, older_than_days=90, keep_latest=100, dry_run=True):
            self.calls.append((older_than_days, keep_latest, dry_run))
            return {
                "success": True, "dry_run": dry_run, "deleted_count": 0,
                "remaining_count": 0, "total_before": 0,
                "older_than_days": older_than_days, "keep_latest": keep_latest,
            }

    mock_db = _MockDB()
    _handle_rule(
        ["cleanup-sync-log", "--older-than", "30", "--keep-latest", "50", "--apply"],
        mock_db,
    )
    assert mock_db.calls[-1] == (30, 50, False)


def test_cli_cleanup_help_no_db():
    """cw rule cleanup-sync-log --help 不应触发数据库初始化。"""
    from unittest import mock
    from callwarden.cli import main as cli_main
    from callwarden.db.db import CodeGraphDB

    old_argv = sys.argv
    sys.argv = ["cw", "rule", "cleanup-sync-log", "--help"]
    try:
        db_init_called = {"count": 0}

        def fake_init(self, *args, **kwargs):
            db_init_called["count"] += 1
            raise RuntimeError("db should not be initialized for --help")

        with mock.patch.object(CodeGraphDB, "__init__", fake_init):
            with mock.patch.object(cli_main, "CodeGraphDB", CodeGraphDB):
                try:
                    cli_main._run_subcommand_mode()
                except SystemExit as e:
                    # --help 触发 argparse 退出码 0
                    assert e.code == 0
        assert db_init_called["count"] == 0
    finally:
        sys.argv = old_argv


def test_cli_cleanup_end_to_end_dry_run(capsys):
    """CLI 端到端：dry-run 输出标题 + summary + hint。"""
    from callwarden.cli.main import _handle_rule

    class _MockDB:
        def cleanup_sync_log(self, older_than_days=90, keep_latest=100, dry_run=True):
            return {
                "success": True, "dry_run": True, "deleted_count": 4,
                "remaining_count": 6, "total_before": 10,
                "older_than_days": older_than_days, "keep_latest": keep_latest,
            }

    _handle_rule(["cleanup-sync-log"], _MockDB())
    out = capsys.readouterr().out
    # dry-run 标题、summary、hint 都应出现
    assert "Dry-Run" in out or "预览" in out
    assert "10" in out  # total_before
    assert "4" in out   # deleted
    assert "--apply" in out  # hint


# ----------------------------------------------------------------------
# MCP 层
# ----------------------------------------------------------------------

def test_mcp_tool_cleanup_agent_rule_sync_log_registered():
    """MCP 工具 cleanup_agent_rule_sync_log 已注册到 server。"""
    # 通过源码扫描确认注册（避免启动完整 MCP server）
    server_src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "server", "mcp_server.py",
    )
    with open(server_src, encoding="utf-8") as fh:
        content = fh.read()
    assert "def cleanup_agent_rule_sync_log(" in content
    assert "@mcp.tool()" in content


def test_mcp_tool_calls_db_method():
    """MCP 工具内部调用 db.cleanup_sync_log 并透传参数。"""
    server_src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "server", "mcp_server.py",
    )
    with open(server_src, encoding="utf-8") as fh:
        content = fh.read()
    # 确认工具调用 db.cleanup_sync_log 并传 dry_run 参数
    assert "db.cleanup_sync_log(" in content
    assert "dry_run=dry_run" in content


# ----------------------------------------------------------------------
# i18n 层
# ----------------------------------------------------------------------

I18N_CLI_MESSAGES_KEYS = [
    "rule_cleanup_sync_log_dry_run_title",
    "rule_cleanup_sync_log_apply_title",
    "rule_cleanup_sync_log_summary",
    "rule_cleanup_sync_log_failed",
    "rule_cleanup_sync_log_dry_run_hint",
]

I18N_ARGPARSE_KEYS = [
    "cli_rule_cleanup_sync_log_desc",
    "cli_rule_cleanup_sync_log_arg_older_than",
    "cli_rule_cleanup_sync_log_arg_keep_latest",
    "cli_rule_cleanup_sync_log_arg_apply",
]

PLACEHOLDER_CHECKS = [
    ("rule_cleanup_sync_log_dry_run_title", ["{older_than}", "{keep_latest}"]),
    ("rule_cleanup_sync_log_apply_title", ["{older_than}", "{keep_latest}"]),
    ("rule_cleanup_sync_log_summary", ["{total_before}", "{deleted}", "{remaining}"]),
    ("rule_cleanup_sync_log_failed", ["{error}"]),
]


def _load_i18n(lang):
    path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "i18n", f"{lang}.json",
    )
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


@pytest.mark.parametrize("key", I18N_CLI_MESSAGES_KEYS + I18N_ARGPARSE_KEYS)
def test_i18n_keys_exist_zh(key):
    data = _load_i18n("zh_CN")
    if key in I18N_CLI_MESSAGES_KEYS:
        assert key in data.get("cli", {}).get("messages", {}), f"missing zh cli.messages.{key}"
    else:
        assert key in data, f"missing zh {key}"


@pytest.mark.parametrize("key", I18N_CLI_MESSAGES_KEYS + I18N_ARGPARSE_KEYS)
def test_i18n_keys_exist_en(key):
    data = _load_i18n("en_US")
    if key in I18N_CLI_MESSAGES_KEYS:
        assert key in data.get("cli", {}).get("messages", {}), f"missing en cli.messages.{key}"
    else:
        assert key in data, f"missing en {key}"


@pytest.mark.parametrize("key,placeholders", PLACEHOLDER_CHECKS)
def test_i18n_placeholders_zh(key, placeholders):
    data = _load_i18n("zh_CN")
    val = data.get("cli", {}).get("messages", {}).get(key, "")
    for ph in placeholders:
        assert ph in val, f"zh {key} missing placeholder {ph}"


@pytest.mark.parametrize("key,placeholders", PLACEHOLDER_CHECKS)
def test_i18n_placeholders_en(key, placeholders):
    data = _load_i18n("en_US")
    val = data.get("cli", {}).get("messages", {}).get(key, "")
    for ph in placeholders:
        assert ph in val, f"en {key} missing placeholder {ph}"


def test_i18n_json_files_valid():
    """两个 i18n JSON 文件都能被解析。"""
    _load_i18n("zh_CN")
    _load_i18n("en_US")


# ----------------------------------------------------------------------
# 源码引用一致性
# ----------------------------------------------------------------------

def test_source_uses_i18n_keys():
    """cli/main.py 的 handler 引用新 i18n key，不再硬编码标题。"""
    cli_src = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
        "cli", "main.py",
    )
    with open(cli_src, encoding="utf-8") as fh:
        content = fh.read()
    assert "rule_cleanup_sync_log_dry_run_title" in content
    assert "rule_cleanup_sync_log_apply_title" in content
    assert "rule_cleanup_sync_log_summary" in content
    assert "rule_cleanup_sync_log_failed" in content
    assert "rule_cleanup_sync_log_dry_run_hint" in content


def test_python_syntax_ok():
    """三个修改的 .py 文件语法正确。"""
    import py_compile
    base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for rel in ["cli/main.py", "server/mcp_server.py", "db/db_agent_rules.py"]:
        py_compile.compile(os.path.join(base, rel), doraise=True)
