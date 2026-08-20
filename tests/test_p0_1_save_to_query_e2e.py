"""P0-1 watcher save-to-query 数据链闭合 E2E 测试（2026-07-21 整改）。

复审报告 §3 P0-1 / §8.1 第 1 条：建立真实
`agent start → register/connect → refresh → apply manifest/query DB → publish → query(min_generation)`
E2E；任一步失败不得 mark staging applied。

测试覆盖：
- Step 1: dispatch 用 int(workspace["workspace_id"]) 而非 int("hash_string")，
  避免原 ValueError 导致 refresh 从未执行
- Step 2: db_cas_merge.merge_cas_to_codegraph 把 CAS 解析结果 merge 到主 CodeGraph DB
  的 file_instances / symbols / calls 表
- Step 3: daemon_handle_refresh CAS committed 后调用 merge + upsert_manifest，
  失败抛异常让上层不追加 staging entry
- Step 4: 完整 E2E——register → connect → file.refresh → CodeGraph DB 查询到新符号

规范：
- AGENTS.md 规则 2：CodeGraph DB 用户级单库（测试用 tmp_path 隔离）
- 复审报告 §8.1 第 1 条：任一步失败不得 mark staging applied
- db/schema.py：workspaces / file_instances / symbols / calls / file_contents 表定义
- db/db_cas.py：cas_symbols / cas_raw_calls / cas_file_cache 表定义
- db/db_cas_merge.py：merge_cas_to_codegraph 实现
- server/replicator.py:daemon_handle_refresh：refresh 主流程
- server/daemon_server.py:dispatch：workspace.register / connect / file.refresh
"""

import os
import sqlite3
import tempfile

import pytest

from callwarden.db.schema import SCHEMA_SQL
from callwarden.db.db_cas import (
    CAS_SCHEMA_DDL,
    CAS_INDEX_SQL,
    FILE_GENERATIONS_DDL,
    init_cas_schema,
)
from callwarden.db.db_daemon import init_daemon_schema, register_workspace
from callwarden.db.db_workspace_manifest import init_manifest_schema
from callwarden.server.replicator import init_session_schema, daemon_handle_refresh


# ============================================
# Step 1: dispatch int(workspace_id) bug 修复
# ============================================


class TestStep1DispatchIntBug:
    """验证 dispatch 用数字主键 workspace_id（而非 hash 字符串转 int）。

    Bug：原 `int(workspace_id)` 把 16 位 hash 字符串转 int 必抛 ValueError，
    导致 workspace.file.refresh 从未成功执行过。
    """

    def test_dispatch_uses_numeric_workspace_id_from_row(self):
        """dispatch 应从 workspace row 取数字主键 workspace_id。

        验证 workspace.file.refresh 和 workspace.connect 两个关键调用点
        使用 int(workspace["workspace_id"])（数字主键）而非 int(workspace_id)
        （hash 字符串转 int）。

        _owned_workspace_by_id 接收的就是数字主键参数（int workspace_id），
        其内部 int(workspace_id) 是冗余防御，不在本测试断言范围。
        """
        source = open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "server", "daemon_server.py"),
            encoding="utf-8",
        ).read()

        # 验证 file.refresh 调用点用数字主键
        refresh_idx = source.find('if method == "workspace.file.refresh"')
        assert refresh_idx >= 0, "找不到 workspace.file.refresh 分支"
        # 截取 file.refresh 分支体（到下一个 if method ==）
        refresh_body_end = source.find('if method == ', refresh_idx + 30)
        if refresh_body_end < 0:
            refresh_body_end = len(source)
        refresh_body = source[refresh_idx:refresh_body_end]
        assert 'int(workspace["workspace_id"])' in refresh_body, (
            "P0-1 Step 1: workspace.file.refresh 分支应使用 "
            "int(workspace['workspace_id'])（数字主键）"
        )

        # 验证 workspace.connect 调用点用数字主键
        connect_idx = source.find('if method == "workspace.connect"')
        assert connect_idx >= 0, "找不到 workspace.connect 分支"
        connect_body_end = source.find('if method == ', connect_idx + 30)
        if connect_body_end < 0:
            connect_body_end = len(source)
        connect_body = source[connect_idx:connect_body_end]
        assert 'int(workspace["workspace_id"])' in connect_body, (
            "P0-1 Step 1: workspace.connect 分支应使用 "
            "int(workspace['workspace_id'])（数字主键）"
        )


# ============================================
# Step 2: merge_cas_to_codegraph 单元测试
# ============================================


class TestStep2CasMerge:
    """验证 db_cas_merge.merge_cas_to_codegraph 单元功能。"""

    def _make_cas_db(self, cas_key: str, content_hash: str,
                     symbols: list, raw_calls: list) -> sqlite3.Connection:
        """构造一个含给定 cas_key 数据的 CAS DB。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_cas_schema(conn)
        # cas_file_cache
        conn.execute(
            "INSERT OR REPLACE INTO cas_file_cache "
            "(cas_key, content_hash, language, file_size, total_lines, "
            "parser_version, callwarden_version, extraction_config_version, "
            "abi_version, input_abi_version, state, parsed_at) "
            "VALUES (?, ?, 'python', ?, ?, '0.1.0', '0.2.0', 'v1', 'v1', 'v1', 'ready', 0)",
            (cas_key, content_hash, 100, 5),
        )
        # cas_symbols
        for i, sym in enumerate(symbols):
            conn.execute(
                "INSERT OR REPLACE INTO cas_symbols "
                "(cas_key, local_symbol_id, symbol_content_hash, name, "
                "local_qualified_name, kind, start_line, end_line, start_col, end_col, "
                "visibility, signature, has_comment, depth) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cas_key, i, sym["hash"], sym["name"], sym["qname"],
                 sym["kind"], sym["start_line"], sym["end_line"], 0, 0,
                 "private", "", 0, -1),
            )
        # cas_raw_calls
        for call in raw_calls:
            conn.execute(
                "INSERT OR REPLACE INTO cas_raw_calls "
                "(cas_key, caller_local_id, caller_name, callee_name, call_line, call_ordinal) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (cas_key, call["caller_local_id"], call["caller_name"],
                 call["callee_name"], call["call_line"]),
            )
        conn.commit()
        return conn

    def _make_codegraph_db(self) -> sqlite3.Connection:
        """构造一个空的 CodeGraph DB。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.executescript(SCHEMA_SQL)
        conn.commit()
        return conn

    def test_merge_inserts_symbols_and_calls(self):
        """merge 后 CodeGraph DB 中应有新文件符号和调用关系。"""
        from callwarden.db.db_cas_merge import merge_cas_to_codegraph

        cas_key = "test_cas_key_v1"
        content_hash = "abc123hash"
        symbols = [
            {"name": "foo", "qname": "module.foo", "kind": "function",
             "hash": "symhash1", "start_line": 1, "end_line": 5},
            {"name": "bar", "qname": "module.bar", "kind": "function",
             "hash": "symhash2", "start_line": 7, "end_line": 10},
        ]
        raw_calls = [
            {"caller_local_id": 0, "caller_name": "foo",
             "callee_name": "bar", "call_line": 3},
        ]
        cas_conn = self._make_cas_db(cas_key, content_hash, symbols, raw_calls)
        cg_conn = self._make_codegraph_db()

        result = merge_cas_to_codegraph(
            cas_conn=cas_conn,
            codegraph_conn=cg_conn,
            cas_key=cas_key,
            workspace_id=42,
            rel_path="module.py",
            abs_path="/test/module.py",
            content_hash=content_hash,
            language="python",
            workspace_root_path="/test",
        )

        assert result["merge_status"] == "merged"
        assert result["symbols_inserted"] == 2
        assert result["calls_inserted"] == 1
        assert result["workspace_id"] == 42

        # 验证 workspaces 表
        ws_row = cg_conn.execute(
            "SELECT * FROM workspaces WHERE id = 42"
        ).fetchone()
        assert ws_row is not None
        assert ws_row["name"] == "daemon_ws_42"

        # 验证 file_instances 表
        fi_row = cg_conn.execute(
            "SELECT * FROM file_instances WHERE workspace_id = 42 AND rel_path = ?",
            ("module.py",),
        ).fetchone()
        assert fi_row is not None
        assert fi_row["current_content_hash"] == content_hash
        assert fi_row["status"] == "parsed"
        assert fi_row["module_path"] == "module"

        # 验证 symbols 表（两条）
        sym_rows = cg_conn.execute(
            "SELECT * FROM symbols WHERE file_instance_id = ?",
            (fi_row["id"],),
        ).fetchall()
        assert len(sym_rows) == 2
        sym_names = {r["name"] for r in sym_rows}
        assert sym_names == {"foo", "bar"}

        # 验证 calls 表（一条）
        call_rows = cg_conn.execute(
            "SELECT * FROM calls WHERE caller_id IN "
            "(SELECT id FROM symbols WHERE file_instance_id = ?)",
            (fi_row["id"],),
        ).fetchall()
        assert len(call_rows) == 1
        assert call_rows[0]["caller_name"] == "foo"
        assert call_rows[0]["callee_name"] == "bar"
        assert call_rows[0]["call_line"] == 3

        cas_conn.close()
        cg_conn.close()

    def test_merge_replaces_existing_symbols(self):
        """同文件二次 merge 应替换旧 symbols（而非追加）。"""
        from callwarden.db.db_cas_merge import merge_cas_to_codegraph

        cas_key = "cas_v2"
        content_hash = "hash_v2"
        cas_conn = self._make_cas_db(cas_key, content_hash,
                                     [{"name": "new_fn", "qname": "m.new_fn", "kind": "function",
                                       "hash": "newhash", "start_line": 1, "end_line": 2}], [])
        cg_conn = self._make_codegraph_db()

        # 第一次 merge（含 2 个符号）
        merge_cas_to_codegraph(
            cas_conn=cas_conn, codegraph_conn=cg_conn, cas_key=cas_key,
            workspace_id=1, rel_path="m.py", abs_path="/m.py",
            content_hash=content_hash, language="python", workspace_root_path="/",
        )

        # 第二次 merge（替换为 1 个符号）
        result = merge_cas_to_codegraph(
            cas_conn=cas_conn, codegraph_conn=cg_conn, cas_key=cas_key,
            workspace_id=1, rel_path="m.py", abs_path="/m.py",
            content_hash=content_hash, language="python", workspace_root_path="/",
        )

        # 验证：file_instances 还是同一行（UPSERT）
        fi_rows = cg_conn.execute(
            "SELECT * FROM file_instances WHERE workspace_id = 1 AND rel_path = ?",
            ("m.py",),
        ).fetchall()
        assert len(fi_rows) == 1

        # 验证：symbols 只剩第二次 merge 的 1 个（不是 3 个）
        sym_rows = cg_conn.execute(
            "SELECT * FROM symbols WHERE file_instance_id = ?",
            (fi_rows[0]["id"],),
        ).fetchall()
        assert len(sym_rows) == 1
        assert sym_rows[0]["name"] == "new_fn"

        cas_conn.close()
        cg_conn.close()

    def test_merge_handles_missing_cas_key(self):
        """cas_key 不存在时返回 cas_miss 状态，不报错。"""
        from callwarden.db.db_cas_merge import merge_cas_to_codegraph

        cas_conn = self._make_cas_db("existing_key", "hash", [], [])
        cg_conn = self._make_codegraph_db()

        result = merge_cas_to_codegraph(
            cas_conn=cas_conn, codegraph_conn=cg_conn,
            cas_key="nonexistent_key",
            workspace_id=1, rel_path="x.py", abs_path="/x.py",
            content_hash="x", language="python", workspace_root_path="/",
        )

        assert result["merge_status"] == "cas_miss"
        assert result["symbols_inserted"] == 0

        cas_conn.close()
        cg_conn.close()


# ============================================
# Step 3: daemon_handle_refresh 集成测试
# ============================================


class TestStep3DaemonHandleRefreshIntegration:
    """验证 daemon_handle_refresh CAS committed 后调用 merge + upsert_manifest。

    复审报告 §8.1 第 1 条：任一步失败不得 mark staging applied。
    """

    def _make_ws_conn(self, workspace_id: int) -> sqlite3.Connection:
        """构造一个含 active session 的 workspace DB。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_session_schema(conn)
        # 初始化 manifest schema（refresh 会 upsert_manifest）
        init_manifest_schema(conn)
        # file_generations 表（从 db_cas 延迟导入）
        conn.execute(FILE_GENERATIONS_DDL)
        # 插入 active session
        conn.execute(
            "INSERT INTO workspace_active_session "
            "(workspace_id, active_session_id, active_session_epoch) "
            "VALUES (?, ?, ?)",
            (workspace_id, "test-session", 1),
        )
        conn.commit()
        return conn

    def _make_cas_db_with_content(self, cas_key: str,
                                  content_hash: str, symbols: list,
                                  raw_calls: list,
                                  db_path: str = "") -> sqlite3.Connection:
        """构造含内容的 CAS DB（db_path 为空时用 :memory:）。

        C4：daemon refresh 的 merge 已切 Rust facade 短连接，CAS 必须落文件
        才能被真实 Rust 路径打开，否则 cas_db_path 探测为空导致 merge 走
        拒绝分支。
        """
        conn = sqlite3.connect(db_path or ":memory:")
        conn.row_factory = sqlite3.Row
        init_cas_schema(conn)
        conn.execute(
            "INSERT OR REPLACE INTO cas_file_cache "
            "(cas_key, content_hash, language, file_size, total_lines, "
            "parser_version, callwarden_version, extraction_config_version, "
            "abi_version, input_abi_version, state, parsed_at) "
            "VALUES (?, ?, 'python', ?, ?, '0.1.0', '0.2.0', 'v1', 'v1', 'v1', 'ready', 0)",
            (cas_key, content_hash, 100, 5),
        )
        for i, sym in enumerate(symbols):
            conn.execute(
                "INSERT OR REPLACE INTO cas_symbols "
                "(cas_key, local_symbol_id, symbol_content_hash, name, "
                "local_qualified_name, kind, start_line, end_line, start_col, end_col, "
                "visibility, signature, has_comment, depth) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (cas_key, i, sym["hash"], sym["name"], sym["qname"],
                 sym["kind"], sym["start_line"], sym["end_line"], 0, 0,
                 "private", "", 0, -1),
            )
        for call in raw_calls:
            conn.execute(
                "INSERT OR REPLACE INTO cas_raw_calls "
                "(cas_key, caller_local_id, caller_name, callee_name, call_line, call_ordinal) "
                "VALUES (?, ?, ?, ?, ?, 0)",
                (cas_key, call["caller_local_id"], call["caller_name"],
                 call["callee_name"], call["call_line"]),
            )
        conn.commit()
        return conn

    def test_refresh_merges_to_codegraph_db(self, tmp_path):
        """refresh 成功后 CodeGraph DB 中应有新文件符号。"""
        from callwarden.db.schema import SCHEMA_SQL

        workspace_id = 100
        cas_key = "refresh_cas_v1"
        content_hash = "refresh_hash_v1"
        symbols = [
            {"name": "processed_fn", "qname": "module.processed_fn",
             "kind": "function", "hash": "processed_hash",
             "start_line": 1, "end_line": 5},
        ]
        raw_calls = []

        ws_conn = self._make_ws_conn(workspace_id)
        cas_conn = self._make_cas_db_with_content(
            cas_key, content_hash, symbols, raw_calls,
            db_path=str(tmp_path / "cas.db"),
        )
        # CodeGraph DB 文件路径（tmp_path 隔离）
        cg_db_path = str(tmp_path / "test_codegraph.db")
        cg_conn = sqlite3.connect(cg_db_path)
        cg_conn.executescript(SCHEMA_SQL)
        cg_conn.commit()
        cg_conn.close()

        # 准备 canonical_bytes（用于 _daemon_parse_and_publish）
        canonical_bytes = b"# test file\ndef processed_fn():\n    pass\n"

        # 需要 mock parse_canonical_bytes_py 以返回预定 cas_key 对应的解析结果
        import callwarden.server.replicator as repl_mod
        # 保存原模块对象（不是函数对象），finally 中恢复原模块对象
        # 修复 sys.modules 污染：原实现用 original_parse.__module__（字符串）恢复，
        # 导致 sys.modules['callwarden_core'] 被替换为字符串，后续 import 失败
        import sys
        original_module = sys.modules.get("callwarden_core")
        try:
            from callwarden_core import parse_canonical_bytes_py as _orig_parse  # noqa: F401
        except ImportError:
            pass

        # Mock canonicalize_source_py / parse_canonical_bytes_py 返回预定结果
        mock_module = type(sys)("callwarden_core_mock")
        mock_module.canonpath = None

        def mock_canonicalize_source_py(abs_path):
            return {"canonical_bytes": canonical_bytes,
                    "content_hash": content_hash}
        def mock_parse_canonical_bytes_py(canonical_bytes_, module_path,
                                          language, content_hash_):
            return {
                "symbols": [
                    {"name": "processed_fn", "qualified_name": "module.processed_fn",
                     "kind": "function", "start_line": 1, "end_line": 5,
                     "start_col": 0, "end_col": 0, "start_byte": 0, "end_byte": 0,
                     "visibility": "private", "signature": "", "has_comment": False,
                     "depth": -1, "symbol_hash": "processed_hash"},
                ],
                "raw_calls": [],
                "module_path": "module",
                "content_hash": content_hash_,
            }
        mock_module.canonicalize_source_py = mock_canonicalize_source_py
        mock_module.parse_canonical_bytes_py = mock_parse_canonical_bytes_py
        # C4：merge 走真实 Rust facade（CAS 已落文件），避免 mock 破坏 P0-1
        # 数据链闭合——真实 merge 才能让 CodeGraph DB 断言成立。
        try:
            from callwarden_core import cas_merge_to_codegraph as _real_merge
            mock_module.cas_merge_to_codegraph = _real_merge
        except ImportError:
            pass
        sys.modules["callwarden_core"] = mock_module

        # 直接调用 daemon_handle_refresh
        msg = {
            "rel_path": "module.py",
            "agent_session_id": "test-session",
            "monotonic_seq": 1,
            "session_epoch": 1,
            "abs_path": str(tmp_path / "module.py"),
        }

        try:
            result = daemon_handle_refresh(
                peer_uid=1000,
                workspace_id=workspace_id,
                msg=msg,
                ws_conn=ws_conn,
                cas_conn=cas_conn,
                canonical_bytes=canonical_bytes,
                codegraph_db_path=cg_db_path,
                workspace_root_path=str(tmp_path),
            )
        finally:
            # 恢复 sys.modules（修复污染：恢复原模块对象，而非字符串或删除）
            if original_module is not None:
                sys.modules["callwarden_core"] = original_module
            else:
                sys.modules.pop("callwarden_core", None)

        # 验证 refresh 主流程成功
        assert result["status"] == "committed"
        assert result["cas_state"] == "ready_published"

        # 验证 P0-1 merge 结果
        assert "merge" in result
        assert result["merge"]["merge_status"] == "merged"
        assert result["merge"]["symbols_inserted"] == 1
        assert result["merge"]["workspace_id"] == workspace_id

        # content_hash 在 _daemon_parse_and_publish 路径中由 canonical_bytes 的
        # 真实 sha256 决定（不信任客户端传入的 hash），故断言实际 hash
        import hashlib as _hl
        expected_content_hash = _hl.sha256(canonical_bytes).hexdigest()

        # 验证 CodeGraph DB 中确实有新文件符号
        cg_conn2 = sqlite3.connect(cg_db_path)
        cg_conn2.row_factory = sqlite3.Row
        fi_row = cg_conn2.execute(
            "SELECT * FROM file_instances WHERE workspace_id = ? AND rel_path = ?",
            (workspace_id, "module.py"),
        ).fetchone()
        assert fi_row is not None, "P0-1: file_instances 表应有新文件行"

        sym_rows = cg_conn2.execute(
            "SELECT * FROM symbols WHERE file_instance_id = ?",
            (fi_row["id"],),
        ).fetchall()
        assert len(sym_rows) == 1, "P0-1: symbols 表应有 1 个新符号"
        assert sym_rows[0]["name"] == "processed_fn"

        # 验证 workspace_manifests 表已被接入
        manifest_row = ws_conn.execute(
            "SELECT * FROM workspace_manifests WHERE workspace_id = ? AND rel_path = ?",
            (workspace_id, "module.py"),
        ).fetchone()
        assert manifest_row is not None, "P0-1: workspace_manifests 应有记录"
        assert manifest_row["content_hash"] == expected_content_hash
        assert manifest_row["is_dirty"] == 1

        ws_conn.close()
        cas_conn.close()
        cg_conn2.close()

    def test_refresh_failure_does_not_mark_applied(self, tmp_path):
        """refresh 失败时不应追加 staging entry（§8.1 第 1 条）。

        C5 C2（2026-08-08）：对齐 Rust merge_ok 门控——Rust 可用但 merge
        数据失败时不抛异常，返回 error merge_result（status=committed 表示
        CAS 层已提交），latest_committed_generation 不推进，同 seq 可重试。
        上层据此 append staging 为 pending 但不 replicate。
        """
        workspace_id = 200
        cas_key = "fail_cas_v1"
        content_hash = "fail_hash_v1"
        symbols = [
            {"name": "fn", "qname": "m.fn", "kind": "function",
             "hash": "fn_hash", "start_line": 1, "end_line": 2},
        ]

        ws_conn = self._make_ws_conn(workspace_id)
        cas_conn = self._make_cas_db_with_content(
            cas_key, content_hash, symbols, [],
            db_path=str(tmp_path / "cas_fail.db"),
        )
        # CodeGraph DB 路径不存在（构造失败场景）
        # 但 sqlite3.connect 会自动创建空 DB，所以需要 mock cas_merge_to_codegraph 抛异常
        import sys
        # 保存原模块对象，finally 中恢复（修复 sys.modules 污染）
        original_module = sys.modules.get("callwarden_core")
        mock_module = type(sys)("callwarden_core_fail")
        canonical_bytes = b"# test\ndef fn():\n    pass\n"
        mock_module.canonicalize_source_py = lambda abs_path: {
            "canonical_bytes": canonical_bytes,
            "content_hash": content_hash,
        }
        mock_module.parse_canonical_bytes_py = lambda b, m, l, h: {
            "symbols": [{"name": "fn", "qualified_name": "m.fn",
                         "kind": "function", "start_line": 1, "end_line": 2,
                         "start_col": 0, "end_col": 0, "start_byte": 0, "end_byte": 0,
                         "visibility": "private", "signature": "",
                         "has_comment": False, "depth": -1,
                         "symbol_hash": "fn_hash"}],
            "raw_calls": [], "module_path": "m", "content_hash": h,
        }
        # C4：mock Rust facade merge 抛异常，模拟生产 merge 失败
        def _fail_merge(**kwargs):
            raise RuntimeError("simulated merge failure")
        mock_module.cas_merge_to_codegraph = _fail_merge
        sys.modules["callwarden_core"] = mock_module

        msg = {
            "rel_path": "m.py",
            "agent_session_id": "test-session",
            "monotonic_seq": 1,
            "session_epoch": 1,
            "abs_path": str(tmp_path / "m.py"),
        }

        # codegraph_db_path 用 tmp_path 下的有效路径（sqlite3.connect 会自动创建）
        # 先初始化 CodeGraph DB schema，让 schema 检测通过
        # merge_cas_to_codegraph mock 抛异常以模拟 merge 失败
        from callwarden.db.schema import SCHEMA_SQL as _SCHEMA_SQL
        cg_db_path_valid = str(tmp_path / "valid_for_merge_fail.db")
        cg_init_conn = sqlite3.connect(cg_db_path_valid)
        cg_init_conn.executescript(_SCHEMA_SQL)
        cg_init_conn.commit()
        cg_init_conn.close()

        try:
            # C5 C2：merge 数据失败不再抛异常，返回 error merge_result
            result = daemon_handle_refresh(
                peer_uid=1000,
                workspace_id=workspace_id,
                msg=msg,
                ws_conn=ws_conn,
                cas_conn=cas_conn,
                canonical_bytes=canonical_bytes,
                codegraph_db_path=cg_db_path_valid,
                workspace_root_path=str(tmp_path),
            )
            assert result["status"] == "committed", (
                f"CAS 层已提交，status 应为 committed，实际: {result}"
            )
            merge_info = result.get("merge") or {}
            assert merge_info.get("merge_status") == "error", (
                f"merge 失败应返回 merge_status=error，实际: {merge_info}"
            )
            assert merge_info.get("error"), (
                f"error merge_result 应携带 error 详情，实际: {merge_info}"
            )

            # P0-2 整改（2026-07-22）：step 4（committed_generation）移到 step 5（merge）之后，
            # merge 失败时 latest_committed_generation 不应被提交。
            # 旧顺序（step 4 先于 step 5）下 merge 失败后 committed_generation 已写入，
            # 重试同一 seq 会判 stale 丢弃。新顺序下可安全重试。
            row = ws_conn.execute(
                "SELECT latest_committed_generation FROM file_generations "
                "WHERE workspace_id = ? AND rel_path = ?",
                (workspace_id, "m.py"),
            ).fetchone()
            assert row is not None, "file_generations 应有记录（step 2 seen 已执行）"
            assert row["latest_committed_generation"] == "", (
                "merge 失败后 latest_committed_generation 不应被提交（P0-2 顺序调整）"
            )
        finally:
            # 恢复原模块对象（修复污染：原实现直接 pop 导致后续测试重新 import 失败）
            if original_module is not None:
                sys.modules["callwarden_core"] = original_module
            else:
                sys.modules.pop("callwarden_core", None)
            ws_conn.close()
            cas_conn.close()

    def test_retry_after_merge_failure_not_stale(self, tmp_path):
        """P0-2 整改（2026-07-22）：merge 失败后重试同一 seq 不判 stale。

        旧顺序（step 4 先于 step 5）下 merge 失败后 latest_committed_generation
        已写入，重试时 incoming_seq <= latest_seq 判 stale 丢弃。
        新顺序（step 4 后于 step 5）下 merge 失败后 latest_committed_generation
        未提交，重试可成功完成。
        """
        workspace_id = 201
        cas_key = "retry_cas_v1"
        content_hash = "retry_hash_v1"
        symbols = [
            {"name": "fn", "qname": "m.fn", "kind": "function",
             "hash": "fn_hash", "start_line": 1, "end_line": 2},
        ]

        ws_conn = self._make_ws_conn(workspace_id)
        cas_conn = self._make_cas_db_with_content(
            cas_key, content_hash, symbols, [],
            db_path=str(tmp_path / "cas_retry.db"),
        )
        canonical_bytes = b"# test\ndef fn():\n    pass\n"

        # Mock callwarden_core（canonicalize + parse）
        import sys
        # 保存原模块对象，finally 中恢复（修复 sys.modules 污染）
        original_module = sys.modules.get("callwarden_core")
        mock_module = type(sys)("callwarden_core_retry")
        mock_module.canonicalize_source_py = lambda abs_path: {
            "canonical_bytes": canonical_bytes,
            "content_hash": content_hash,
        }
        mock_module.parse_canonical_bytes_py = lambda b, m, l, h: {
            "symbols": [{"name": "fn", "qualified_name": "m.fn",
                         "kind": "function", "start_line": 1, "end_line": 2,
                         "start_col": 0, "end_col": 0, "start_byte": 0, "end_byte": 0,
                         "visibility": "private", "signature": "",
                         "has_comment": False, "depth": -1,
                         "symbol_hash": "fn_hash"}],
            "raw_calls": [], "module_path": "m", "content_hash": h,
        }
        # C4：第一次 merge 失败（模拟 Rust facade 异常），第二次转发真实 Rust
        # facade 完成 merge——验证 merge 失败后同 seq 重试不判 stale（P0-2）。
        from callwarden_core import cas_merge_to_codegraph as _real_merge
        merge_fail = {"active": True}

        def _flaky_merge(**kwargs):
            if merge_fail["active"]:
                raise RuntimeError("simulated merge failure (attempt 1)")
            return _real_merge(**kwargs)

        mock_module.cas_merge_to_codegraph = _flaky_merge
        sys.modules["callwarden_core"] = mock_module

        # 初始化 CodeGraph DB schema
        from callwarden.db.schema import SCHEMA_SQL as _SCHEMA_SQL
        cg_db_path = str(tmp_path / "retry_cg.db")
        cg_init_conn = sqlite3.connect(cg_db_path)
        cg_init_conn.executescript(_SCHEMA_SQL)
        cg_init_conn.commit()
        cg_init_conn.close()

        msg = {
            "rel_path": "m.py",
            "agent_session_id": "test-session",
            "monotonic_seq": 1,
            "session_epoch": 1,
            "abs_path": str(tmp_path / "m.py"),
        }

        # 第一次：merge 失败（Rust facade 抛异常 → C5 C2 返回 error merge_result）
        try:
            # 第一次：merge 失败，返回 error merge_result（不抛异常）
            result1 = daemon_handle_refresh(
                peer_uid=1000,
                workspace_id=workspace_id,
                msg=msg,
                ws_conn=ws_conn,
                cas_conn=cas_conn,
                canonical_bytes=canonical_bytes,
                codegraph_db_path=cg_db_path,
                workspace_root_path=str(tmp_path),
            )
            assert result1.get("merge", {}).get("merge_status") == "error", (
                f"第一次 merge 失败应返回 merge_status=error，实际: {result1}"
            )

            # 验证 latest_committed_generation 未提交
            row = ws_conn.execute(
                "SELECT latest_committed_generation FROM file_generations "
                "WHERE workspace_id = ? AND rel_path = ?",
                (workspace_id, "m.py"),
            ).fetchone()
            assert row is not None
            assert row["latest_committed_generation"] == ""

            # 恢复 merge（第二次走真实 Rust facade）
            merge_fail["active"] = False

            # 第二次：同一 seq 重试，应成功（不判 stale）
            result = daemon_handle_refresh(
                peer_uid=1000,
                workspace_id=workspace_id,
                msg=msg,
                ws_conn=ws_conn,
                cas_conn=cas_conn,
                canonical_bytes=canonical_bytes,
                codegraph_db_path=cg_db_path,
                workspace_root_path=str(tmp_path),
            )
            assert result["status"] == "committed", (
                f"merge 失败后重试同一 seq 应成功，实际: {result}"
            )
        finally:
            # 恢复原模块对象（修复污染：原实现直接 pop 导致后续测试重新 import 失败）
            if original_module is not None:
                sys.modules["callwarden_core"] = original_module
            else:
                sys.modules.pop("callwarden_core", None)
            ws_conn.close()
            cas_conn.close()


# ============================================
# Step 4: 完整 E2E——register → connect → refresh → query
# ============================================


class TestStep4FullE2E:
    """完整 save-to-query E2E：register → connect → refresh → CodeGraph DB 查询。

    复审报告 §8.1 第 1 条：建立真实
    `agent start → register/connect → refresh → apply manifest/query DB → publish → query(min_generation)`
    E2E。

    Windows 兼容性（2026-07-29）：原 skipif(not hasattr(os, "getuid")) 限制过严。
    测试用 peer={"uid": 0}（root），_validate_owned_path 在 Windows 无 getuid 时
    跳过 owner_uid 校验，不影响数据链闭合验证。E2E 核心是 P0-1 数据链，
    不依赖 Unix peer credentials。

    现改为只在 callwarden_core 不可导入时跳过（daemon_server.py 间接依赖）。
    """

    def test_register_connect_refresh_query_e2e(self, tmp_path):
        """完整链路：daemon dispatch register / connect / file.refresh → CodeGraph DB 有符号。

        Windows 环境下 _validate_owned_path 的 owner_uid 校验被跳过（无 os.getuid），
        本测试主要验证 P0-1 数据链闭合，不依赖 Unix peer credentials。

        若 callwarden_core 包损坏（site-packages 中的 __init__.py 循环引用），
        daemon_server.py 无法导入，本测试 skip（环境问题，非代码问题）。
        """
        # 检查 callwarden_core 是否可正常导入（非 ImportError 且非 NameError）
        try:
            import callwarden_core  # noqa: F401
            cc_ok = True
        except (ImportError, NameError):
            cc_ok = False
        if not cc_ok:
            pytest.skip(
                "callwarden_core 包损坏（site-packages __init__.py 循环引用），"
                "daemon_server.py 无法导入。环境问题，非代码问题。"
            )

        from callwarden.server.daemon_server import EnterpriseDaemonService
        from callwarden.server.snapshot_manager import SnapshotManagerService
        from unittest.mock import MagicMock
        import hashlib

        # 构造 daemon service
        registry_db = str(tmp_path / "registry.db")
        snapshot_svc = MagicMock(spec=SnapshotManagerService)
        # publish_snapshot 必须返回含 generation 的 dict，否则 repl_result.generation 为 MagicMock
        snapshot_svc.publish_snapshot.return_value = {"generation": 1}
        service = EnterpriseDaemonService(
            registry_db=registry_db,
            snapshot_service=snapshot_svc,
            data_root=str(tmp_path / "enterprise"),
            start_background_tasks=False,
        )

        # Mock _config.resolve_codegraph_db_path 返回 tmp_path 下的 CodeGraph DB
        cg_db_path = str(tmp_path / "codegraph.db")
        # 初始化 CodeGraph DB schema
        cg_conn = sqlite3.connect(cg_db_path)
        cg_conn.executescript(SCHEMA_SQL)
        cg_conn.commit()
        cg_conn.close()

        service._config.resolve_codegraph_db_path = lambda workspace_id: cg_db_path

        # 准备测试目录
        ws_root = tmp_path / "ws_root"
        ws_root.mkdir()
        (ws_root / "test_module.py").write_text(
            "# test\ndef my_func():\n    pass\n",
            encoding="utf-8",
        )

        # Step 1: workspace.register
        peer = {"uid": 0}  # root，避免 owner 校验问题
        reg_result = service.dispatch(peer, "workspace.register", {
            "client_view_root": str(ws_root),
            "git_remote_url": "",
            "git_head_commit_sha": "",
        })
        ws_instance_id = reg_result["workspace_instance_id"]
        ws_numeric_id = reg_result["workspace_id"]
        assert ws_instance_id, "workspace.register 应返回 workspace_instance_id"
        assert ws_numeric_id > 0, "workspace.register 应返回数字 workspace_id"

        # Step 2: workspace.connect
        connect_result = service.dispatch(peer, "workspace.connect", {
            "workspace_instance_id": ws_instance_id,
            "agent_session_id": "e2e-session",
        })
        assert connect_result["session_epoch"] >= 1

        # Step 3: workspace.file.refresh
        # 准备 canonical_bytes（绕过 abs_path 读取）
        file_path = ws_root / "test_module.py"
        file_bytes = file_path.read_bytes()
        canonical_bytes = file_bytes  # 简化：直接用原 bytes
        content_hash = hashlib.sha256(canonical_bytes).hexdigest()

        # Mock callwarden_core（避免依赖 Rust 扩展）
        import sys
        # 保存原模块对象，finally 中恢复（修复 sys.modules 污染）
        original_module = sys.modules.get("callwarden_core")
        mock_module = type(sys)("callwarden_core_e2e")
        mock_module.canonicalize_source_py = lambda abs_path: {
            "canonical_bytes": canonical_bytes,
            "content_hash": content_hash,
        }
        mock_module.parse_canonical_bytes_py = lambda b, m, l, h: {
            "symbols": [
                {"name": "my_func", "qualified_name": "test_module.my_func",
                 "kind": "function", "start_line": 2, "end_line": 3,
                 "start_col": 0, "end_col": 0, "start_byte": 0, "end_byte": 0,
                 "visibility": "private", "signature": "",
                 "has_comment": False, "depth": -1,
                 "symbol_hash": "my_func_hash_v1"},
            ],
            "raw_calls": [],
            "module_path": "test_module",
            "content_hash": h,
        }
        # C4：merge 走真实 Rust facade（_get_workspace_resources 已把 CAS 落文件，
        # cas_db_path 有效），否则 CodeGraph DB 的 file_instances/symbols 断言无法闭合。
        try:
            from callwarden_core import cas_merge_to_codegraph as _real_merge
            mock_module.cas_merge_to_codegraph = _real_merge
        except ImportError:
            pass
        sys.modules["callwarden_core"] = mock_module

        try:
            refresh_result = service.dispatch(peer, "workspace.file.refresh", {
                "workspace_instance_id": ws_instance_id,
                "agent_session_id": "e2e-session",
                "session_epoch": connect_result["session_epoch"],
                "monotonic_seq": 1,
                "rel_path": "test_module.py",
                "canonical_bytes_hex": canonical_bytes.hex(),
                "content_hash": content_hash,
                "language": "python",
            })
        finally:
            # 恢复原模块对象（修复污染：原实现直接 pop 导致后续测试重新 import 失败）
            if original_module is not None:
                sys.modules["callwarden_core"] = original_module
            else:
                sys.modules.pop("callwarden_core", None)

        # 验证 refresh 成功
        assert refresh_result["status"] == "committed"
        assert refresh_result.get("cas_state") in (
            "ready_published", "ready_cache_hit")

        # 验证 P0-1 merge：CodeGraph DB 中应有新文件符号
        cg_conn2 = sqlite3.connect(cg_db_path)
        cg_conn2.row_factory = sqlite3.Row
        fi_row = cg_conn2.execute(
            "SELECT * FROM file_instances WHERE workspace_id = ? AND rel_path = ?",
            (ws_numeric_id, "test_module.py"),
        ).fetchone()
        assert fi_row is not None, "P0-1 E2E: file_instances 表应有新文件行"
        assert fi_row["current_content_hash"] == content_hash

        sym_rows = cg_conn2.execute(
            "SELECT * FROM symbols WHERE file_instance_id = ?",
            (fi_row["id"],),
        ).fetchall()
        assert len(sym_rows) >= 1, "P0-1 E2E: symbols 表应有新符号"
        sym_names = {r["name"] for r in sym_rows}
        assert "my_func" in sym_names, (
            f"P0-1 E2E: symbols 应含 'my_func'，实际 {sym_names}"
        )

        # 验证 generation 递增（min_generation 可查询）
        ws_resources = service._get_workspace_resources(ws_instance_id)
        gen_row = ws_resources["ws_conn"].execute(
            "SELECT latest_committed_generation FROM file_generations "
            "WHERE workspace_id = ? AND rel_path = ?",
            (ws_numeric_id, "test_module.py"),
        ).fetchone()
        assert gen_row is not None, "file_generations 应有 committed generation 记录"
        assert gen_row["latest_committed_generation"], "latest_committed_generation 非空"

        cg_conn2.close()
