"""A14 增量扫描修复验证测试（2026-07-20 二轮评审）

验证点：
1. Schema：semgrep_findings.scan_id 字段 + 索引 + SCHEMA_VERSION=40
2. analyzers/issues.py: save_semgrep_findings 支持 scan_type / files_scanned / stale_file_ids
3. analyzers/issues.py: scan_semgrep_incremental 方法存在且签名正确
4. cli/main.py: semgrep scan 子命令支持 --incremental / --base / --head
5. server/mcp_server.py: scan_semgrep_incremental MCP 工具注册
6. cicd/pr_check.py: 优先调用 scan_semgrep_incremental
7. 行为逻辑：增量扫描清理旧 findings + scan_id 关联
8. _feature_matrix.md: A14 状态改为 ✅ 已修复
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest


# ============================================
# 1. Schema 层验证
# ============================================

class TestA14SchemaMigration:
    """验证 schema v40 迁移：semgrep_findings 加 scan_id 字段 + 索引"""

    def test_schema_version_bumped_to_40(self):
        """SCHEMA_VERSION 应为 40（A14 迁移后）"""
        from callwarden.db.schema import SCHEMA_VERSION
        assert SCHEMA_VERSION == 40, f"SCHEMA_VERSION 应为 40，实际 {SCHEMA_VERSION}"

    def test_semgrep_findings_has_scan_id_column_in_schema_sql(self):
        """SCHEMA_SQL 中 semgrep_findings 表应包含 scan_id 列定义"""
        from callwarden.db.schema import SCHEMA_SQL
        # 查找 semgrep_findings 表定义段
        assert "semgrep_findings" in SCHEMA_SQL
        assert "scan_id" in SCHEMA_SQL, "semgrep_findings 表应包含 scan_id 列"

    def test_semgrep_findings_scan_id_index_in_schema_sql(self):
        """SCHEMA_SQL 应包含 idx_semgrep_scan_id 索引"""
        from callwarden.db.schema import SCHEMA_INDEXES_SQL, SCHEMA_SQL
        # 索引可能在 SCHEMA_SQL 或 SCHEMA_INDEXES_SQL 中
        combined = SCHEMA_SQL + SCHEMA_INDEXES_SQL
        assert "idx_semgrep_scan_id" in combined, "应有 idx_semgrep_scan_id 索引"

    def test_migration_v39_to_v40_function_exists(self):
        """_migrate_v39_to_v40 函数应存在"""
        from callwarden.db.db_base import _migrate_v39_to_v40
        assert callable(_migrate_v39_to_v40)

    def test_migration_registry_includes_v40(self):
        """迁移注册表应包含 version=40 的条目"""
        from callwarden.db.db_base import CodeGraphBase
        # _get_migrations 是静态/类方法
        db = CodeGraphBase.__new__(CodeGraphBase)
        migrations = db._get_migrations()
        assert 40 in migrations, "迁移注册表应包含 v40 条目"
        assert "func" in migrations[40]
        assert "description" in migrations[40]

    def test_migration_v40_is_idempotent(self, tmp_path):
        """v40 迁移应幂等：重复执行不报错"""
        import sqlite3
        from callwarden.db.db_base import _migrate_v39_to_v40

        db_path = str(tmp_path / "test_v40.db")
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        # 模拟 v39 既有库的 semgrep_findings 表（无 scan_id 列）
        conn.execute("""
            CREATE TABLE semgrep_findings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_instance_id INTEGER NOT NULL,
                content_hash TEXT DEFAULT '',
                rule_id TEXT NOT NULL,
                rule_name TEXT DEFAULT '',
                message TEXT DEFAULT '',
                severity TEXT DEFAULT 'INFO',
                confidence TEXT DEFAULT 'UNKNOWN',
                language TEXT DEFAULT '',
                start_line INTEGER DEFAULT 0,
                end_line INTEGER DEFAULT 0,
                snippet TEXT DEFAULT '',
                fix TEXT DEFAULT '',
                symbol_id INTEGER DEFAULT 0,
                symbol_qualified TEXT DEFAULT '',
                scanned_at REAL DEFAULT 0,
                UNIQUE(content_hash, rule_id, start_line)
            )
        """)
        conn.execute("INSERT INTO semgrep_findings (file_instance_id, content_hash, rule_id) VALUES (1, 'abc', 'rule1')")
        conn.commit()

        # 第一次迁移
        _migrate_v39_to_v40(conn)
        # 验证 scan_id 列已添加
        cur = conn.execute("PRAGMA table_info(semgrep_findings)")
        columns = {row["name"] for row in cur.fetchall()}
        assert "scan_id" in columns

        # 第二次迁移（幂等性）
        _migrate_v39_to_v40(conn)
        # 验证数据未丢失
        cur = conn.execute("SELECT COUNT(*) as c FROM semgrep_findings")
        assert cur.fetchone()["c"] == 1
        conn.close()


# ============================================
# 2. save_semgrep_findings 参数验证
# ============================================

class TestA14SaveSemgrepFindingsSignature:
    """验证 save_semgrep_findings 接受新参数"""

    def test_save_semgrep_findings_accepts_scan_type(self):
        """save_semgrep_findings 应接受 scan_type 参数"""
        import inspect
        from callwarden.analyzers.issues import IssueAnalyzerMixin
        sig = inspect.signature(IssueAnalyzerMixin.save_semgrep_findings)
        assert "scan_type" in sig.parameters, "save_semgrep_findings 应接受 scan_type 参数"
        assert sig.parameters["scan_type"].default == "full", "scan_type 默认应为 'full'"

    def test_save_semgrep_findings_accepts_files_scanned(self):
        """save_semgrep_findings 应接受 files_scanned 参数"""
        import inspect
        from callwarden.analyzers.issues import IssueAnalyzerMixin
        sig = inspect.signature(IssueAnalyzerMixin.save_semgrep_findings)
        assert "files_scanned" in sig.parameters
        assert sig.parameters["files_scanned"].default == 0

    def test_save_semgrep_findings_accepts_stale_file_ids(self):
        """save_semgrep_findings 应接受 stale_file_ids 参数"""
        import inspect
        from callwarden.analyzers.issues import IssueAnalyzerMixin
        sig = inspect.signature(IssueAnalyzerMixin.save_semgrep_findings)
        assert "stale_file_ids" in sig.parameters
        assert sig.parameters["stale_file_ids"].default is None


# ============================================
# 3. scan_semgrep_incremental 方法验证
# ============================================

class TestA14ScanIncrementalMethod:
    """验证 scan_semgrep_incremental 方法存在且签名正确"""

    def test_scan_semgrep_incremental_exists(self):
        """scan_semgrep_incremental 方法应存在"""
        from callwarden.analyzers.issues import IssueAnalyzerMixin
        assert hasattr(IssueAnalyzerMixin, "scan_semgrep_incremental")

    def test_scan_semgrep_incremental_signature(self):
        """scan_semgrep_incremental 应有正确的参数签名"""
        import inspect
        from callwarden.analyzers.issues import IssueAnalyzerMixin
        sig = inspect.signature(IssueAnalyzerMixin.scan_semgrep_incremental)
        params = sig.parameters
        assert "base_branch" in params
        assert "head" in params
        assert "config" in params
        assert "languages" in params
        assert "timeout" in params
        assert params["base_branch"].default == "main"
        assert params["head"].default == "HEAD"
        assert params["config"].default == "p/default"

    def test_scan_semgrep_incremental_docstring_mentions_a14(self):
        """scan_semgrep_incremental docstring 应提到 A14 修复"""
        from callwarden.analyzers.issues import IssueAnalyzerMixin
        doc = IssueAnalyzerMixin.scan_semgrep_incremental.__doc__ or ""
        assert "A14" in doc
        assert "incremental" in doc.lower()


# ============================================
# 4. CLI 子命令验证
# ============================================

class TestA14CLICommand:
    """验证 cw semgrep scan --incremental 子命令注册"""

    def test_cli_semgrep_scan_supports_incremental_flag(self):
        """CLI semgrep scan 子命令应支持 --incremental flag"""
        # 通过模拟 argparse 解析验证
        from callwarden.cli.main import _handle_semgrep
        # 不能直接调用 _handle_semgrep（需要 db），但可以读取源码验证参数注册
        import inspect
        src = inspect.getsource(_handle_semgrep)
        assert "--incremental" in src
        assert "scan_semgrep_incremental" in src

    def test_cli_semgrep_scan_supports_base_branch_arg(self):
        """CLI semgrep scan 子命令应支持 --base 参数"""
        from callwarden.cli.main import _handle_semgrep
        import inspect
        src = inspect.getsource(_handle_semgrep)
        assert "--base" in src
        assert "base_branch" in src

    def test_cli_semgrep_scan_supports_head_arg(self):
        """CLI semgrep scan 子命令应支持 --head 参数"""
        from callwarden.cli.main import _handle_semgrep
        import inspect
        src = inspect.getsource(_handle_semgrep)
        assert "--head" in src

    def test_cli_semgrep_incremental_calls_db_method(self):
        """CLI --incremental 分支应调用 db.scan_semgrep_incremental"""
        from callwarden.cli.main import _handle_semgrep
        import inspect
        src = inspect.getsource(_handle_semgrep)
        # 验证分支逻辑：先检查 opts.incremental，再调用 scan_semgrep_incremental
        assert "if opts.incremental" in src
        assert "db.scan_semgrep_incremental" in src


# ============================================
# 5. MCP 工具验证
# ============================================

class TestA14MCPTool:
    """验证 scan_semgrep_incremental MCP 工具注册"""

    def test_mcp_tool_scan_semgrep_incremental_registered(self):
        """server/mcp_server.py 应注册 scan_semgrep_incremental MCP 工具"""
        # 读取源码验证（避免实际启动 MCP server）
        with open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "server", "mcp_server.py"),
            "r", encoding="utf-8"
        ) as f:
            src = f.read()
        # 应有 @mcp.tool() 装饰的 scan_semgrep_incremental 函数
        assert "def scan_semgrep_incremental" in src
        # 函数前应有 @mcp.tool() 装饰器（查找紧邻的装饰器）
        idx = src.find("def scan_semgrep_incremental")
        # 往前找最近的 @mcp.tool()
        before = src[:idx]
        assert "@mcp.tool()" in before[-200:]

    def test_mcp_tool_scan_semgrep_incremental_docstring_mentions_a14(self):
        """scan_semgrep_incremental MCP 工具 docstring 应提到 A14 修复"""
        with open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "server", "mcp_server.py"),
            "r", encoding="utf-8"
        ) as f:
            src = f.read()
        idx = src.find("def scan_semgrep_incremental")
        # 截取函数 docstring 段
        doc_start = src.find('"""', idx)
        doc_end = src.find('"""', doc_start + 3)
        doc = src[doc_start:doc_end]
        assert "A14" in doc


# ============================================
# 6. cicd/pr_check.py 调用验证
# ============================================

class TestA14PRCheckIntegration:
    """验证 cicd/pr_check.py 优先调用 scan_semgrep_incremental"""

    def test_pr_check_uses_incremental_scan(self):
        """pr_check.py 应优先调用 scan_semgrep_incremental"""
        with open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "cicd", "pr_check.py"),
            "r", encoding="utf-8"
        ) as f:
            src = f.read()
        assert "scan_semgrep_incremental" in src
        assert "incremental_fn" in src or "getattr(self.db, \"scan_semgrep_incremental\"" in src

    def test_pr_check_has_fallback_to_legacy(self):
        """pr_check.py 应有 fallback 到 run_semgrep_and_save 的逻辑"""
        with open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "cicd", "pr_check.py"),
            "r", encoding="utf-8"
        ) as f:
            src = f.read()
        # Fallback 分支
        assert "run_semgrep_and_save" in src
        assert "fallback" in src.lower() or "elif" in src

    def test_pr_check_mentions_a14_in_comment(self):
        """pr_check.py 应有 A14 修复注释"""
        with open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "cicd", "pr_check.py"),
            "r", encoding="utf-8"
        ) as f:
            src = f.read()
        assert "A14" in src


# ============================================
# 7. 行为逻辑验证（单元测试）
# ============================================

class TestA14IncrementalBehavior:
    """验证增量扫描行为逻辑：清理旧 findings + scan_id 关联"""

    def test_save_semgrep_findings_writes_scan_type_incremental(self, tmp_path):
        """save_semgrep_findings(scan_type='incremental') 应写入 semgrep_scans.scan_type='incremental'"""
        from callwarden.db import CodeGraphDB
        db_path = str(tmp_path / "test_a14_behavior.db")
        db = CodeGraphDB(db_path=db_path, workspace_root=str(tmp_path))
        ws_id = db.register_workspace("test", str(tmp_path))
        db.set_active_workspace(ws_id)

        # 调用 save_semgrep_findings(scan_type='incremental')
        count = db.save_semgrep_findings(
            [],  # 空 findings
            scan_config="p/default",
            scan_type="incremental",
            files_scanned=5,
        )
        assert count == 0

        # 验证 semgrep_scans 记录
        cur = db.conn.execute("SELECT scan_type, files_scanned FROM semgrep_scans ORDER BY id DESC LIMIT 1")
        row = cur.fetchone()
        assert row["scan_type"] == "incremental"
        assert row["files_scanned"] == 5
        db.close()

    def test_save_semgrep_findings_writes_scan_id_to_findings(self, tmp_path):
        """save_semgrep_findings 应把 scan_id 写入每条 finding"""
        from callwarden.db import CodeGraphDB
        db_path = str(tmp_path / "test_a14_scan_id.db")
        db = CodeGraphDB(db_path=db_path, workspace_root=str(tmp_path))
        ws_id = db.register_workspace("test", str(tmp_path))
        db.set_active_workspace(ws_id)

        # 注册一个文件
        abs_path = os.path.join(str(tmp_path), "test.py")
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write("import os\n")
        fi_id = db._register_file_db(abs_path, "test")

        # 构造一条 finding（path 使用 rel_path，匹配 file_map 的 key）
        findings = [{
            "path": "test.py",  # rel_path
            "rule_id": "test-rule",
            "rule_name": "Test Rule",
            "message": "test message",
            "severity": "WARNING",
            "confidence": "HIGH",
            "language": "python",
            "start_line": 1,
            "end_line": 1,
            "snippet": "import os",
            "fix": "",
        }]

        count = db.save_semgrep_findings(findings, scan_config="p/default", scan_type="full")
        assert count == 1

        # 验证 finding 的 scan_id 与 semgrep_scans.id 一致
        cur = db.conn.execute("SELECT scan_id FROM semgrep_findings WHERE rule_id='test-rule'")
        finding_scan_id = cur.fetchone()["scan_id"]
        cur = db.conn.execute("SELECT id FROM semgrep_scans ORDER BY id DESC LIMIT 1")
        scan_id = cur.fetchone()["id"]
        assert finding_scan_id == scan_id, f"finding.scan_id={finding_scan_id} != scan.id={scan_id}"
        db.close()

    def test_incremental_scan_cleans_stale_findings(self, tmp_path):
        """增量扫描应清理变更文件的旧 findings"""
        from callwarden.db import CodeGraphDB
        db_path = str(tmp_path / "test_a14_stale.db")
        db = CodeGraphDB(db_path=db_path, workspace_root=str(tmp_path))
        ws_id = db.register_workspace("test", str(tmp_path))
        db.set_active_workspace(ws_id)

        # 注册两个文件
        for fname in ["a.py", "b.py"]:
            abs_path = os.path.join(str(tmp_path), fname)
            with open(abs_path, "w", encoding="utf-8") as f:
                f.write("import os\n")
            db._register_file_db(abs_path, "test")

        # 第一次全量扫描：插入两条 findings（path 用 rel_path）
        findings_a = [{
            "path": "a.py",
            "rule_id": "rule-a", "rule_name": "A", "message": "msg-a",
            "severity": "WARNING", "confidence": "HIGH", "language": "python",
            "start_line": 1, "end_line": 1, "snippet": "", "fix": "",
        }]
        findings_b = [{
            "path": "b.py",
            "rule_id": "rule-b", "rule_name": "B", "message": "msg-b",
            "severity": "WARNING", "confidence": "HIGH", "language": "python",
            "start_line": 1, "end_line": 1, "snippet": "", "fix": "",
        }]
        db.save_semgrep_findings(findings_a + findings_b, scan_config="p/default", scan_type="full")

        # 验证有 2 条 findings
        cur = db.conn.execute("SELECT COUNT(*) as c FROM semgrep_findings")
        assert cur.fetchone()["c"] == 2

        # 获取 a.py 的 file_instance_id
        cur = db.conn.execute("SELECT id FROM file_instances WHERE rel_path='a.py'")
        fi_id_a = cur.fetchone()["id"]

        # 增量扫描：清理 a.py 的旧 findings，但 b.py 保留
        db.save_semgrep_findings(
            [],  # 不插入新 finding
            scan_config="p/default",
            scan_type="incremental",
            stale_file_ids=[fi_id_a],
        )

        # 验证 a.py 的 finding 被清理，b.py 保留
        cur = db.conn.execute("SELECT COUNT(*) as c FROM semgrep_findings WHERE rule_id='rule-a'")
        assert cur.fetchone()["c"] == 0, "a.py 的旧 finding 应被清理"
        cur = db.conn.execute("SELECT COUNT(*) as c FROM semgrep_findings WHERE rule_id='rule-b'")
        assert cur.fetchone()["c"] == 1, "b.py 的 finding 应保留"
        db.close()

    def test_full_scan_does_not_clean_stale(self, tmp_path):
        """全量扫描（scan_type='full'）不应触发 stale_file_ids 清理"""
        from callwarden.db import CodeGraphDB
        db_path = str(tmp_path / "test_a14_full.db")
        db = CodeGraphDB(db_path=db_path, workspace_root=str(tmp_path))
        ws_id = db.register_workspace("test", str(tmp_path))
        db.set_active_workspace(ws_id)

        # 注册文件
        abs_path = os.path.join(str(tmp_path), "a.py")
        with open(abs_path, "w", encoding="utf-8") as f:
            f.write("import os\n")
        db._register_file_db(abs_path, "test")

        # 插入一条 finding（path 用 rel_path）
        findings = [{
            "path": "a.py",
            "rule_id": "rule-a", "rule_name": "A", "message": "msg",
            "severity": "WARNING", "confidence": "HIGH", "language": "python",
            "start_line": 1, "end_line": 1, "snippet": "", "fix": "",
        }]
        db.save_semgrep_findings(findings, scan_config="p/default", scan_type="full")

        # 再次全量扫描，传 stale_file_ids（应不生效）
        cur = db.conn.execute("SELECT id FROM file_instances WHERE rel_path='a.py'")
        fi_id = cur.fetchone()["id"]
        db.save_semgrep_findings(
            [], scan_config="p/default",
            scan_type="full", stale_file_ids=[fi_id],
        )

        # 验证 finding 保留
        cur = db.conn.execute("SELECT COUNT(*) as c FROM semgrep_findings WHERE rule_id='rule-a'")
        assert cur.fetchone()["c"] == 1, "全量扫描不应清理 stale_file_ids"
        db.close()


# ============================================
# 8. _feature_matrix.md 状态验证
# ============================================

class TestA14FeatureMatrixStatus:
    """验证 _feature_matrix.md 中 A14 状态已更新"""

    @staticmethod
    def _read_a14_row():
        """读取 _feature_matrix.md 中的 A14 完整行"""
        matrix_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "_feature_matrix.md"
        )
        with open(matrix_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.startswith("| A14 |"):
                    return line.rstrip("\n")
        return ""

    def test_a14_status_updated_to_fixed(self):
        """_feature_matrix.md 中 A14 状态应为 ✅ 已修复"""
        a14_line = self._read_a14_row()
        assert a14_line, "_feature_matrix.md 应包含 A14 行"
        assert "✅" in a14_line, f"A14 状态应为 ✅ 已修复，实际: {a14_line}"

    def test_a14_status_mentions_incremental(self):
        """_feature_matrix.md 中 A14 说明应提到 incremental / 增量扫描"""
        a14_line = self._read_a14_row()
        assert a14_line, "_feature_matrix.md 应包含 A14 行"
        assert "incremental" in a14_line.lower() or "增量" in a14_line

    def test_a14_not_marked_as_not_implemented(self):
        """A14 不应标记为 ❌ 声明不成立"""
        a14_line = self._read_a14_row()
        assert a14_line, "_feature_matrix.md 应包含 A14 行"
        assert "❌" not in a14_line, "A14 不应标记为 ❌"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
