r"""task.supersede P0-H CLI 层测试（T-1787277487109-758e56d0）。

覆盖（prove-cli-validation-migration-and-no-fallback）：
1. v58→v59 迁移：基础 5 列表 → 无损补齐 v59 列/索引（历史行保留、幂等可重跑、
   fail-closed 缺列抛错）；
2. CLI 严格 role：--role 仅接受 adjudicator（argparse choices）；
3. CLI 证据前置校验：缺 --evidence-path / --evidence-hash fail-fast；
4. daemon 不可用 fail-closed：enterprise 模式 + 死 endpoint → 上抛
   DaemonUnavailableError，绝不回退本地 SQLite（route policy 显式断言）；
5. 显式路由策略：task.supersede / task.superseded_by 均为 forbidden fallback。

前置：Windows（CLI/daemon 相关用例）；迁移用例纯 Python 无需 daemon。
"""

import os
import sqlite3
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PY_EXE = sys.executable
_CW_PY = os.path.join(_REPO_ROOT, "cw.py")


def _run_cli(args, env_extra=None):
    env = dict(os.environ)
    env.pop("CW_AGENT_SESSION_ID", None)
    env.setdefault("PYTHONPATH", _REPO_ROOT)
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [_PY_EXE, _CW_PY] + args,
        env=env, cwd=_REPO_ROOT, capture_output=True,
        text=True, encoding="utf-8", errors="replace", timeout=60,
    )


def _all_out(proc) -> str:
    return (proc.stdout or "") + "\n" + (proc.stderr or "")


# ============================================
# 1) v58→v59 无损迁移（纯 Python，无 daemon）
# ============================================

class TestSchemaMigrationV58ToV59:
    """v58→v59 迁移：基础 supersede 表（ensure_supersede_schema 5 列形态）补齐 v59 列。"""

    def _base_db(self):
        from callwarden.db.db_base import _migrate_v58_to_v59  # noqa: F401
        con = sqlite3.connect(":memory:")
        con.executescript("""
        CREATE TABLE task_supersede_relations (
            superseded_task_id TEXT NOT NULL,
            superseding_task_id TEXT NOT NULL,
            reason TEXT DEFAULT '',
            actor TEXT NOT NULL,
            created_at REAL NOT NULL,
            PRIMARY KEY (superseded_task_id, superseding_task_id)
        );
        CREATE INDEX idx_task_supersede_relations_superseding
            ON task_supersede_relations(superseding_task_id);
        CREATE TABLE task_supersede_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            superseded_task_id TEXT NOT NULL,
            superseding_task_id TEXT NOT NULL,
            reason TEXT DEFAULT '',
            actor TEXT NOT NULL,
            monotonic_seq INTEGER NOT NULL,
            authoritative_timestamp REAL NOT NULL
        );
        INSERT INTO task_supersede_relations
            (superseded_task_id, superseding_task_id, reason, actor, created_at)
        VALUES ('OLD','NEW','multi-edge acceptance','implementer-workbuddy-v1', 1787239000.0);
        """)
        return con

    def test_migration_adds_v59_columns_and_preserves_legacy_row(self):
        from callwarden.db.db_base import _migrate_v58_to_v59
        con = self._base_db()
        _migrate_v58_to_v59(con)
        rel_cols = {r[1] for r in con.execute("PRAGMA table_info(task_supersede_relations)")}
        ev_cols = {r[1] for r in con.execute("PRAGMA table_info(task_supersede_events)")}
        for c in ("workspace_id", "supersedence_id", "reason_code",
                  "actor_agent_id", "actor_session_id", "actor_model_id", "actor_role",
                  "request_id", "lease_id", "fencing_counter",
                  "evidence_path", "evidence_hash", "authoritative_timestamp"):
            assert c in rel_cols, f"relations 缺列 {c}"
            assert c in ev_cols, f"events 缺列 {c}"
        # 历史行保留（无损；workspace_id 默认 0 = legacy）
        row = con.execute(
            "SELECT superseded_task_id, superseding_task_id, workspace_id, actor "
            "FROM task_supersede_relations"
        ).fetchone()
        assert row == ("OLD", "NEW", 0, "implementer-workbuddy-v1")
        # 索引齐备
        idx = {r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND name LIKE 'idx_task_supersede%'"
        )}
        assert {"idx_task_supersede_relations_workspace",
                "idx_task_supersede_relations_supersedence",
                "idx_task_supersede_events_workspace"} <= idx
        con.close()

    def test_migration_idempotent(self):
        from callwarden.db.db_base import _migrate_v58_to_v59
        con = self._base_db()
        _migrate_v58_to_v59(con)
        _migrate_v58_to_v59(con)  # 二次执行不抛错、不重复加列
        rel_cols = [r[1] for r in con.execute("PRAGMA table_info(task_supersede_relations)")]
        assert rel_cols.count("workspace_id") == 1
        con.close()

    def test_migration_fails_closed_on_missing_index(self):
        from callwarden.db.db_base import _verify_supersede_v59_schema
        con = sqlite3.connect(":memory:")
        # v59 列齐备但缺 idx_task_supersede_relations_workspace → 校验 fail-closed
        con.executescript("""
        CREATE TABLE task_supersede_relations (
            superseded_task_id TEXT NOT NULL,
            superseding_task_id TEXT NOT NULL,
            reason TEXT DEFAULT '',
            actor TEXT NOT NULL,
            created_at REAL NOT NULL,
            workspace_id INTEGER NOT NULL DEFAULT 0,
            supersedence_id TEXT NOT NULL DEFAULT '',
            reason_code TEXT NOT NULL DEFAULT 'governance_supersede',
            actor_agent_id TEXT NOT NULL DEFAULT '',
            actor_session_id TEXT NOT NULL DEFAULT '',
            actor_model_id TEXT NOT NULL DEFAULT '',
            actor_role TEXT NOT NULL DEFAULT '',
            request_id TEXT NOT NULL DEFAULT '',
            lease_id TEXT NOT NULL DEFAULT '',
            fencing_counter INTEGER NOT NULL DEFAULT -1,
            evidence_path TEXT NOT NULL DEFAULT '',
            evidence_hash TEXT NOT NULL DEFAULT '',
            authoritative_timestamp REAL NOT NULL DEFAULT 0,
            PRIMARY KEY (superseded_task_id, superseding_task_id)
        );
        CREATE INDEX idx_task_supersede_relations_superseding
            ON task_supersede_relations(superseding_task_id);
        CREATE TABLE task_supersede_events (
            event_id INTEGER PRIMARY KEY AUTOINCREMENT,
            superseded_task_id TEXT NOT NULL,
            superseding_task_id TEXT NOT NULL,
            reason TEXT DEFAULT '',
            actor TEXT NOT NULL,
            monotonic_seq INTEGER NOT NULL,
            authoritative_timestamp REAL NOT NULL,
            workspace_id INTEGER NOT NULL DEFAULT 0,
            supersedence_id TEXT NOT NULL DEFAULT '',
            reason_code TEXT NOT NULL DEFAULT 'governance_supersede',
            actor_agent_id TEXT NOT NULL DEFAULT '',
            actor_session_id TEXT NOT NULL DEFAULT '',
            actor_model_id TEXT NOT NULL DEFAULT '',
            actor_role TEXT NOT NULL DEFAULT '',
            request_id TEXT NOT NULL DEFAULT '',
            lease_id TEXT NOT NULL DEFAULT '',
            fencing_counter INTEGER NOT NULL DEFAULT -1,
            evidence_path TEXT NOT NULL DEFAULT '',
            evidence_hash TEXT NOT NULL DEFAULT ''
        );
        """)
        with pytest.raises(RuntimeError, match="索引缺失"):
            _verify_supersede_v59_schema(con)
        con.close()


# ============================================
# 2) CLI 严格 role / 证据前置 / no-fallback
# ============================================

pytestmark_windows = pytest.mark.skipif(
    sys.platform != "win32", reason="CLI/daemon 路由用例需 Windows"
)


class TestCliValidation:
    @pytest.mark.skipif(sys.platform != "win32", reason="Windows")
    def test_role_choices_strict_adjudicator(self):
        # --role implementer 在 argparse choices 层被拒（P0-H 严格限定）
        proc = _run_cli(["task", "supersede", "OLD", "NEW",
                         "--agent-id", "a", "--session-id", "s",
                         "--model-id", "m", "--role", "implementer",
                         "--evidence-path", "ev.json", "--evidence-hash", "h"])
        assert proc.returncode != 0
        assert "invalid choice" in _all_out(proc) or "argument --role" in _all_out(proc)

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows")
    def test_evidence_required_fail_fast(self):
        # 缺 --evidence-path/--evidence-hash：CLI 前置 fail-fast（不发 daemon 请求）
        proc = _run_cli(["task", "supersede", "OLD", "NEW",
                         "--agent-id", "a", "--session-id", "s",
                         "--model-id", "m", "--role", "adjudicator"])
        out = _all_out(proc)
        assert "evidence" in out.lower(), f"未提示证据缺失:\n{out}"

    @pytest.mark.skipif(sys.platform != "win32", reason="Windows")
    def test_daemon_unavailable_fail_closed_no_local_fallback(self, monkeypatch):
        # enterprise 模式 + daemon 连接失败：task.supersede 必须上抛
        # DaemonUnavailableError，且**绝不调用** local fallback（P0-H route policy）。
        import callwarden.server.daemon_client as dc

        monkeypatch.setenv("CW_DAEMON_MODE", "enterprise")
        monkeypatch.setenv("CW_DAEMON_AUTOSTART_WINDOW", "0")

        class _DeadClient:
            """模拟 daemon 不可达（连接层失败）。"""
            def call(self, *a, **k):
                raise dc.DaemonUnavailableError("connection refused (simulated)")

        monkeypatch.setattr(dc, "_get_rpc_client_for_route", lambda: _DeadClient())
        fallback_called = []

        def _fallback():
            fallback_called.append(True)
            return {"local_fallback": True}

        with pytest.raises(dc.DaemonUnavailableError):
            dc.route_task_write(
                "task.supersede",
                {"superseded_id": "OLD", "superseding_id": "NEW", "workspace_id": 1},
                _fallback,
            )
        assert not fallback_called, "daemon 不可用时不得回退本地 SQLite（fail-closed）"


class TestRoutePolicy:
    """P0-H 显式路由策略（daemon_client 单测级，无需 daemon）。"""

    def test_supersede_policy_forbidden_fallback(self):
        from callwarden.server.daemon_client import (
            TASK_SUPERSEDE_ROUTE_POLICY,
            get_supersede_route_policy,
        )
        assert get_supersede_route_policy("task.supersede")["fallback"] == "forbidden"
        assert get_supersede_route_policy("task.superseded_by")["fallback"] == "forbidden"
        assert "task.supersede" in TASK_SUPERSEDE_ROUTE_POLICY

    def test_assert_supersede_no_local_fallback(self):
        from callwarden.server.daemon_client import (
            DaemonUnavailableError,
            assert_supersede_no_local_fallback,
        )
        with pytest.raises(DaemonUnavailableError):
            assert_supersede_no_local_fallback("task.supersede", "enterprise")
        # local 模式不拦截（fallback 由调用方决定；CLI 的 supersede fallback 本身即 raise）
        assert_supersede_no_local_fallback("task.supersede", "local")
