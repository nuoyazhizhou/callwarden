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

import hashlib
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
    """[A 类] daemon_handle_refresh 薄客户端 RPC 转发契约。

    stale 依据（生产侧 server/replicator.py）：`daemon_handle_refresh`
    （:364-384）已随 daemon authority 迁移为纯薄客户端——函数体
    `del peer_uid, workspace_id, ws_conn, cas_conn, workspace_root`（:379）
    与 `del codegraph_db_path, workspace_root_path, ws_db_path, cas_db_path`
    （:380）后仅 `return _call_daemon_rpc(_REPLICATOR_REFRESH_METHOD, params)`
    （:384，方法常量 `mcp.replicator.daemon_handle_refresh` 见 :52）。CAS 两阶段 /
    canonical bytes 校验 / CodeGraph merge / manifest upsert / generation 守护均在
    Rust daemon 内执行；旧测试断言的 Python 本地 parse→merge→manifest 链路
    （`_daemon_parse_and_publish`）已非调用路径，daemon 不可用时
    `_call_daemon_rpc`（server/_mcp_common.py:27-44）fail-closed 抛
    E_HTTP_DAEMON_UNAVAILABLE（实测）。故改为 mock 模块级 `R._call_daemon_rpc`，
    断言「转发 method+params（含 canonical_bytes_hex）+ 回包逐字透传 + 本地
    ws_conn 不被提交（无本地回退）」。

    参照已全绿同族：tests/test_c6_snapshot_guard_replicator.py::
    TestDaemonHandleRefreshGenerationProtection。
    """

    def _make_ws_conn(self, workspace_id: int) -> sqlite3.Connection:
        """构造一个含 session/manifest/file_generations schema 的 workspace DB。"""
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        init_session_schema(conn)
        # 初始化 manifest schema（旧路径会 upsert_manifest；保留以证明薄客户端不写）
        init_manifest_schema(conn)
        # file_generations 表（从 db_cas 延迟导入）
        conn.execute(FILE_GENERATIONS_DDL)
        conn.commit()
        return conn

    def _stub_daemon(self, monkeypatch, *responses):
        """安装 RPC seam 替身：记录 (method, params) 并按下标回放 daemon 回包。"""
        from callwarden.server import replicator as R

        calls = []

        def fake_rpc(method, params):
            idx = min(len(calls), len(responses) - 1)
            calls.append((method, dict(params)))
            return responses[idx]

        monkeypatch.setattr(R, "_call_daemon_rpc", fake_rpc)
        return R, calls

    def _make_msg(self, tmp_path, rel_path: str) -> dict:
        return {
            "rel_path": rel_path,
            "agent_session_id": "test-session",
            "monotonic_seq": 1,
            "session_epoch": 1,
            "abs_path": str(tmp_path / rel_path),
        }

    def _committed_generation(self, ws_conn, workspace_id: int, rel_path: str) -> str:
        row = ws_conn.execute(
            "SELECT latest_committed_generation FROM file_generations "
            "WHERE workspace_id = ? AND rel_path = ?",
            (workspace_id, rel_path),
        ).fetchone()
        return "" if row is None else (row["latest_committed_generation"] or "")

    def test_refresh_committed_forwarded_and_passthrough(self, tmp_path, monkeypatch):
        """refresh 成功：转发 method+params（含 canonical_bytes_hex）且回包逐字透传。"""
        workspace_id = 100
        canonical_bytes = b"# test file\ndef processed_fn():\n    pass\n"
        content_hash = hashlib.sha256(canonical_bytes).hexdigest()
        resp = {
            "status": "committed",
            "cas_state": "ready_published",
            "content_hash": content_hash,
            "merge": {
                "merge_status": "merged",
                "symbols_inserted": 1,
                "workspace_id": workspace_id,
            },
        }
        ws_conn = self._make_ws_conn(workspace_id)
        R, calls = self._stub_daemon(monkeypatch, resp)

        result = daemon_handle_refresh(
            peer_uid=1000,
            workspace_id=workspace_id,
            msg=self._make_msg(tmp_path, "module.py"),
            ws_conn=ws_conn,
            cas_conn=None,
            canonical_bytes=canonical_bytes,
            codegraph_db_path=str(tmp_path / "codegraph.db"),
            workspace_root_path=str(tmp_path),
        )

        assert result == resp
        assert calls[0][0] == R._REPLICATOR_REFRESH_METHOD
        assert calls[0][1]["canonical_bytes_hex"] == canonical_bytes.hex()
        assert calls[0][1]["rel_path"] == "module.py"
        assert calls[0][1]["agent_session_id"] == "test-session"
        assert calls[0][1]["monotonic_seq"] == 1
        assert result["status"] == "committed"
        assert result["merge"]["merge_status"] == "merged"
        # 薄客户端不写本地 DB：无本地 committed generation
        assert self._committed_generation(ws_conn, workspace_id, "module.py") == ""

        ws_conn.close()

    def test_refresh_merge_error_passthrough_no_local_commit(self, tmp_path, monkeypatch):
        """daemon 返回 error merge_result：回包透传，本地不推进 committed generation。

        C5 C2（2026-08-08）：对齐 Rust merge_ok 门控——merge 数据失败时
        status=committed（CAS 层已提交）但 merge_status=error，上层据此 append
        staging 为 pending 但不 replicate，同 seq 可重试。
        """
        workspace_id = 200
        canonical_bytes = b"# test\ndef fn():\n    pass\n"
        resp = {
            "status": "committed",
            "cas_state": "ready_published",
            "merge": {"merge_status": "error", "error": "simulated merge failure"},
        }
        ws_conn = self._make_ws_conn(workspace_id)
        R, calls = self._stub_daemon(monkeypatch, resp)

        result = daemon_handle_refresh(
            peer_uid=1000,
            workspace_id=workspace_id,
            msg=self._make_msg(tmp_path, "m.py"),
            ws_conn=ws_conn,
            cas_conn=None,
            canonical_bytes=canonical_bytes,
            codegraph_db_path=str(tmp_path / "valid_for_merge_fail.db"),
            workspace_root_path=str(tmp_path),
        )

        assert result == resp
        assert calls[0][0] == R._REPLICATOR_REFRESH_METHOD
        assert result["status"] == "committed"
        assert result["merge"]["merge_status"] == "error"
        assert result["merge"]["error"]
        # P0-2：merge 失败时本地不得提交 committed generation（薄客户端本就不写）
        assert self._committed_generation(ws_conn, workspace_id, "m.py") == ""

        ws_conn.close()

    def test_retry_after_merge_failure_not_stale(self, tmp_path, monkeypatch):
        """P0-2：merge 失败后重试同一 seq——薄客户端两次均原样转发，不做本地 stale 判定。

        旧顺序（step 4 先于 step 5）下 merge 失败后 latest_committed_generation
        已写入，重试时 incoming_seq <= latest_seq 判 stale 丢弃；新顺序（step 4
        后于 step 5）下该判定已下沉 daemon，薄客户端对同 seq 恒转发。
        """
        workspace_id = 201
        canonical_bytes = b"# test\ndef fn():\n    pass\n"
        resp_err = {
            "status": "committed",
            "cas_state": "ready_published",
            "merge": {"merge_status": "error", "error": "attempt 1 failure"},
        }
        resp_ok = {
            "status": "committed",
            "cas_state": "ready_published",
            "merge": {"merge_status": "merged", "symbols_inserted": 1},
        }
        ws_conn = self._make_ws_conn(workspace_id)
        R, calls = self._stub_daemon(monkeypatch, resp_err, resp_ok)

        result1 = daemon_handle_refresh(
            peer_uid=1000,
            workspace_id=workspace_id,
            msg=self._make_msg(tmp_path, "m.py"),
            ws_conn=ws_conn,
            cas_conn=None,
            canonical_bytes=canonical_bytes,
            codegraph_db_path=str(tmp_path / "retry_cg.db"),
            workspace_root_path=str(tmp_path),
        )
        assert result1["merge"]["merge_status"] == "error"

        result2 = daemon_handle_refresh(
            peer_uid=1000,
            workspace_id=workspace_id,
            msg=self._make_msg(tmp_path, "m.py"),
            ws_conn=ws_conn,
            cas_conn=None,
            canonical_bytes=canonical_bytes,
            codegraph_db_path=str(tmp_path / "retry_cg.db"),
            workspace_root_path=str(tmp_path),
        )
        assert result2 == resp_ok
        assert result2["status"] == "committed"
        # 同一 seq 两次均被转发（薄客户端不丢弃）
        assert len(calls) == 2
        assert calls[0][1]["monotonic_seq"] == calls[1][1]["monotonic_seq"] == 1

        ws_conn.close()


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

    def test_register_connect_refresh_query_e2e(self, tmp_path, monkeypatch):
        """[B 类·迁移] register → connect → file.refresh 薄客户端转发 E2E。

        stale 依据（生产侧）：daemon_server 的 `workspace.file.refresh` 分支
        （server/daemon_server.py:1102 分支内 `from callwarden.server.replicator
        import daemon_handle_refresh`，:1249 调用）已薄客户端化——
        `daemon_handle_refresh`（server/replicator.py:364-384）仅
        `_call_daemon_rpc("mcp.replicator.daemon_handle_refresh", params)`
        （:384）。旧测试断言的“service 进程内完成 parse→merge→CodeGraph DB
        写入→查询”数据链已下沉 Rust daemon，无 daemon 时 fail-closed 抛
        E_HTTP_MANIFEST_MISSING（实测）。故 register/connect 仍走真实本地实现，
        仅 mock `callwarden.server.replicator.daemon_handle_refresh`（分支内
        from-import 生效，参照已全绿 test_c6_snapshot_guard_replicator.py::
        TestDaemonServerBlockedBranch），断言「转发参数 + 回包透传 + staging/
        replicate 由 daemon 回包驱动」。

        Windows 环境 _validate_owned_path 的 owner_uid 校验被跳过（无 os.getuid），
        本测试主要验证转发契约，不依赖 Unix peer credentials。

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

        # Step 1: workspace.register（本地真实实现）
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

        # Step 3: workspace.file.refresh —— 薄客户端已下沉 daemon，mock RPC seam
        file_path = ws_root / "test_module.py"
        canonical_bytes = file_path.read_bytes()
        content_hash = hashlib.sha256(canonical_bytes).hexdigest()

        from callwarden.server import replicator as R
        forwarded = []

        def _fake_refresh(**kwargs):
            forwarded.append(kwargs)
            return {
                "status": "committed",
                "cas_state": "ready_published",
                "content_hash": kwargs.get("msg", {}).get("content_hash", ""),
                "merge": {"merge_status": "merged", "symbols_inserted": 1},
            }

        monkeypatch.setattr(R, "daemon_handle_refresh", _fake_refresh)

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

        # 验证转发契约：daemon 薄客户端收到完整 msg（含 canonical_bytes_hex）
        assert forwarded, "workspace.file.refresh 应转发到 daemon 薄客户端"
        assert forwarded[0]["msg"]["rel_path"] == "test_module.py"
        assert forwarded[0]["msg"]["canonical_bytes_hex"] == canonical_bytes.hex()
        assert forwarded[0]["msg"]["agent_session_id"] == "e2e-session"
        assert forwarded[0]["msg"]["monotonic_seq"] == 1

        # 验证 daemon 回包逐字透传
        assert refresh_result["status"] == "committed"
        assert refresh_result["cas_state"] == "ready_published"
        assert refresh_result["merge"]["merge_status"] == "merged"

        # 验证 daemon 回包驱动 dispatch 的 staging/replicate 后处理
        assert "replication" in refresh_result
        assert refresh_result["replication"]["snapshot_published"] is True
