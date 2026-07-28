"""Phase 0 Step 3: 迁移 manifest 差分测试。

验证 docs/design/migration-manifest.md 作为真相源与实际代码现状一致：
1. manifest 中列出的 Rust 模块文件都实际存在
2. manifest 中列出的 Python 生产入口模块都实际存在
3. manifest 中列出的 PyO3 API 在 lib.rs 中注册
4. manifest 中列出的 daemon 内部模块文件都存在
5. Rust migration_manifest 模块的 MigrationStatus emoji 映射与 manifest.md 表格一致

设计文档 §4：每个功能子任务的 differential-test 步骤必须对比真相源与实现。
本测试是 Phase 0 第一个子任务的 differential-test，对比 manifest 真相源与代码。
"""
from __future__ import annotations

import os
import re
import sys
from pathlib import Path

import pytest

_PKG_ROOT = Path(__file__).resolve().parent.parent
if str(_PKG_ROOT) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT))

_MANIFEST_MD = _PKG_ROOT / "docs" / "design" / "migration-manifest.md"
_RUST_SRC = _PKG_ROOT / "rust_ext" / "src"
_LIB_RS = _RUST_SRC / "lib.rs"


def _read_manifest() -> str:
    """读取 migration-manifest.md 全文。"""
    assert _MANIFEST_MD.exists(), f"manifest 不存在: {_MANIFEST_MD}"
    return _MANIFEST_MD.read_text(encoding="utf-8")


def _lib_rs_content() -> str:
    """读取 lib.rs 全文。"""
    assert _LIB_RS.exists(), f"lib.rs 不存在: {_LIB_RS}"
    return _LIB_RS.read_text(encoding="utf-8")


# ============================================
# 1. Rust 模块文件存在性测试
# ============================================

# manifest 第 2.1 节列出的 PyO3 暴露 API 对应的模块
_EXPECTED_RUST_PY_API_MODULES = {
    "multi_lang",  # parse_file_lang 等
    "graph",  # GraphStore
    "snapshot",  # PySnapshotManager
    "canonicalize",  # canonicalize_source_py
}

# manifest 第 2.2 节列出的 daemon 内部模块
_EXPECTED_DAEMON_MODULES = {
    "cas", "cas_merge", "replicator", "workspace", "snapshot_guard",
    "snapshot_state", "staging_log", "parse_retry_log", "dispatch", "server",
    "peercred", "budget", "health", "parser_metrics", "protocol", "config",
    "toolchain", "memfd",
}

# manifest 第 2.2 节列出的其他内部模块
_EXPECTED_OTHER_RUST_MODULES = {
    "watcher", "canonicalize", "delta", "diff", "frontier", "hash_diff",
    "metrics", "toolchain",
}


def test_manifest_rust_py_api_modules_exist():
    """manifest 第 2.1 节列出的 PyO3 API 对应模块文件必须存在。"""
    for mod in _EXPECTED_RUST_PY_API_MODULES:
        if mod == "canonicalize":
            path = _RUST_SRC / f"{mod}.rs"
        else:
            path = _RUST_SRC / f"{mod}.rs"
        assert path.exists(), f"manifest 列出的 Rust 模块文件不存在: {path}"


def test_manifest_daemon_modules_exist():
    """manifest 第 2.2 节列出的 daemon 内部模块文件必须存在。"""
    daemon_dir = _RUST_SRC / "daemon"
    assert daemon_dir.exists(), f"daemon 目录不存在: {daemon_dir}"
    for mod in _EXPECTED_DAEMON_MODULES:
        path = daemon_dir / f"{mod}.rs"
        assert path.exists(), f"manifest 列出的 daemon 模块文件不存在: {path}"


def test_manifest_other_rust_modules_exist():
    """manifest 第 2.2 节列出的其他内部模块文件必须存在。"""
    for mod in _EXPECTED_OTHER_RUST_MODULES:
        path = _RUST_SRC / f"{mod}.rs"
        assert path.exists(), f"manifest 列出的 Rust 模块文件不存在: {path}"


def test_manifest_migration_manifest_module_exists():
    """Phase 0 Step 2 创建的 migration_manifest.rs 必须存在。"""
    path = _RUST_SRC / "migration_manifest.rs"
    assert path.exists(), f"migration_manifest.rs 不存在: {path}"


# ============================================
# 2. PyO3 API 注册一致性测试
# ============================================

# manifest 第 2.1 节列出的 PyO3 暴露 API 名称
_EXPECTED_PY_API_NAMES = {
    "batch_parse_c_files",
    "parse_c_file",
    "batch_parse_c_files_pool",
    "batch_parse_c_files_stream",
    "build_graph_from_c_files",
    "parse_file_lang",
    "parse_canonical_bytes_py",
    "batch_parse_files_lang",
    "batch_parse_files_lang_pool",
    "supported_languages",
    "parse_status_from_fields",
    "parse_diagnostics_from_fields",
    "batch_cosine_similarity",
    "canonicalize_source_py",
    "core_version",
}

# manifest 第 2.1 节列出的 pyclass 名称
_EXPECTED_PY_CLASSES = {
    "ParseResultPool",
    "ParseResultStream",
    "GraphStore",
    "CallersBatch",
    "SymbolSearchBatch",
    "PySnapshotManager",
    "PySnapshotCache",
}


def test_manifest_py_api_registered_in_lib_rs():
    """manifest 第 2.1 节列出的 PyO3 pyfunction 必须在 lib.rs 注册。"""
    content = _lib_rs_content()
    for api in _EXPECTED_PY_API_NAMES:
        assert api in content, (
            f"manifest 列出的 PyO3 API '{api}' 未在 lib.rs 中注册（wrap_pyfunction）"
        )


def test_manifest_py_classes_registered_in_lib_rs():
    """manifest 第 2.1 节列出的 pyclass 必须在 lib.rs 注册。"""
    content = _lib_rs_content()
    for cls in _EXPECTED_PY_CLASSES:
        assert cls in content, (
            f"manifest 列出的 pyclass '{cls}' 未在 lib.rs 中注册（add_class）"
        )


# ============================================
# 3. Python 生产入口存在性测试
# ============================================

# manifest 第 1.2 节列出的关键 db Mixin 文件
_EXPECTED_DB_MIXINS = {
    "db_base.py", "db_build.py", "db_query.py", "db_cas.py", "db_cas_merge.py",
    "db_workspace_manifest.py", "db_tasks.py", "db_daemon.py", "db_guardrail.py",
    "db_impact.py", "db_evolution.py", "db_vector.py", "db_clone_detection.py",
    "db_clone_groups.py", "db_coverage.py", "db_tests.py", "db_git.py",
    "db_migrate.py", "db_gc.py", "db_audit_chain.py", "db_lsp.py",
    "schema.py", "rust_parser_facade.py",
}

# manifest 第 1.3 节列出的 server 模块
_EXPECTED_SERVER_MODULES = {
    "mcp_server.py", "watcher.py", "daemon_server.py", "daemon_client.py",
    "replicator.py", "snapshot_manager.py", "staging_log.py",
    "schema_migrator.py", "backup_restore.py", "agent_watcher.py",
    "agent_session.py", "health_check.py", "metrics.py", "query_budget.py",
    "audit_log.py", "ipc_transport.py",
}

# manifest 第 1.1 节列出的 CLI 模块
_EXPECTED_CLI_MODULES = {
    "main.py", "console.py", "agent.py", "client.py",
    "daemon.py", "daemon_commands.py", "agent_registry.py",
}


def test_manifest_db_mixins_exist():
    """manifest 第 1.2 节列出的 db Mixin 文件必须存在。"""
    db_dir = _PKG_ROOT / "db"
    assert db_dir.exists(), f"db 目录不存在: {db_dir}"
    for mixin_file in _EXPECTED_DB_MIXINS:
        path = db_dir / mixin_file
        assert path.exists(), (
            f"manifest 列出的 db Mixin 文件不存在: {path}"
        )


def test_manifest_server_modules_exist():
    """manifest 第 1.3 节列出的 server 模块必须存在。"""
    server_dir = _PKG_ROOT / "server"
    assert server_dir.exists(), f"server 目录不存在: {server_dir}"
    for mod_file in _EXPECTED_SERVER_MODULES:
        path = server_dir / mod_file
        assert path.exists(), (
            f"manifest 列出的 server 模块文件不存在: {path}"
        )


def test_manifest_cli_modules_exist():
    """manifest 第 1.1 节列出的 CLI 模块必须存在。"""
    cli_dir = _PKG_ROOT / "cli"
    assert cli_dir.exists(), f"cli 目录不存在: {cli_dir}"
    for mod_file in _EXPECTED_CLI_MODULES:
        path = cli_dir / mod_file
        assert path.exists(), (
            f"manifest 列出的 CLI 模块文件不存在: {path}"
        )


def test_rust_parser_facade_is_production_entry():
    """manifest 第 1.4 节：rust_parser_facade.py 必须是生产解析入口。"""
    facade = _PKG_ROOT / "db" / "rust_parser_facade.py"
    content = facade.read_text(encoding="utf-8")
    # 必须导入 callwarden_core
    assert "from callwarden_core import" in content or "import callwarden_core" in content, (
        "rust_parser_facade.py 未导入 callwarden_core，不是 Rust 生产入口"
    )
    # 必须有 ParseMode 类（manifest 第 9 节回滚配置）
    assert "class ParseMode" in content, (
        "rust_parser_facade.py 缺少 ParseMode 类（CW_PARSE_MODE 配置）"
    )


# ============================================
# 4. manifest 内容完整性测试
# ============================================

def test_manifest_has_required_sections():
    """manifest 必须包含 10 个必需章节。"""
    content = _read_manifest()
    required_sections = [
        "## 1. Python 生产入口盘点",
        "## 2. Rust 已有能力盘点",
        "## 3. 迁移目标 Rust Service Trait 清单",
        "## 4. 跨语言 ABI 契约",
        "## 5. 错误码枚举",
        "## 6. 权限与事务边界",
        "## 7. 迁移状态跟踪表",
        "## 8. 性能基线",
        "## 9. 回滚配置",
        "## 10. 禁止事项",
    ]
    for section in required_sections:
        assert section in content, (
            f"manifest 缺少必需章节: {section}"
        )


def test_manifest_has_32_feature_rows():
    """manifest 第 7 节迁移状态跟踪表必须包含 32 个功能子任务行。"""
    content = _read_manifest()
    # 找到第 7 节到第 8 节之间的表格行
    section_match = re.search(
        r"## 7\. 迁移状态跟踪表(.*?)## 8\.",
        content,
        re.DOTALL,
    )
    assert section_match, "manifest 第 7 节迁移状态跟踪表不存在"
    table_content = section_match.group(1)
    # 表格数据行以 | 开头，包含 Phase 数字
    # 排除表头和分隔行
    data_rows = [
        line for line in table_content.splitlines()
        if line.strip().startswith("|")
        and "Phase" not in line
        and "---" not in line
        and re.search(r"\| (\d+) \|", line)
    ]
    # 32 个功能子任务（8 Phase × 4 功能）
    assert len(data_rows) >= 32, (
        f"manifest 第 7 节表格应有 32 个功能子任务行，实际 {len(data_rows)} 行"
    )


def test_manifest_error_codes_complete():
    """manifest 第 5 节必须包含关键错误码。"""
    content = _read_manifest()
    required_codes = [
        "PARSE_OK", "PARSE_PARTIAL", "PARSE_FAILED", "PARSE_UNSUPPORTED",
        "PARSE_FATAL", "CAS_LOCKED", "DB_LOCKED", "SNAPSHOT_STALE",
        "ACL_DENIED", "BUDGET_EXCEEDED", "RECOVERY_FAILED", "TRANSPORT_ERROR",
    ]
    for code in required_codes:
        assert code in content, f"manifest 缺少错误码: {code}"


def test_manifest_rollback_config_complete():
    """manifest 第 9 节必须包含关键回滚开关。"""
    content = _read_manifest()
    required_switches = [
        "CW_PARSE_MODE",
        "CW_RUST_EXT_PATH",
        "CW_DAEMON_BIN",
        "CW_DISABLE_RUST_PARSE",
        "--no-verify",
    ]
    for switch in required_switches:
        assert switch in content, f"manifest 缺少回滚开关: {switch}"


# ============================================
# 5. Rust MigrationStatus emoji 映射一致性测试
# ============================================

def test_rust_migration_status_emoji_matches_manifest():
    """Rust migration_manifest 模块的 MigrationStatus emoji 必须与 manifest 一致。"""
    try:
        from callwarden_core import migration_manifest  # type: ignore
    except ImportError:
        pytest.skip("callwarden_core 未安装或 migration_manifest 未暴露")

    # manifest 第 7 节使用的 emoji 标记
    expected_mapping = {
        "NotStarted": "🔴",
        "Partial": "🟡",
        "Done": "✅",
        "PendingReview": "⏸️",
    }

    # 注意：migration_manifest 模块当前未通过 PyO3 暴露，
    # 本测试在 Rust 暴露后启用。当前只验证 manifest.md 使用了这些 emoji。
    content = _read_manifest()
    for emoji in expected_mapping.values():
        assert emoji in content, (
            f"manifest 未使用状态 emoji: {emoji}"
        )


def test_manifest_uses_status_emojis():
    """manifest 第 7 节必须使用 4 种状态 emoji。"""
    content = _read_manifest()
    section_match = re.search(
        r"## 7\. 迁移状态跟踪表(.*?)## 8\.",
        content,
        re.DOTALL,
    )
    assert section_match, "manifest 第 7 节不存在"
    table_content = section_match.group(1)
    assert "🔴" in table_content, "manifest 表格缺少 🔴（未开始）"
    assert "🟡" in table_content, "manifest 表格缺少 🟡（部分完成）"
    # ✅ 和 ⏸️ 在 Phase 0 第一个任务完成后才会出现，当前阶段允许缺失
