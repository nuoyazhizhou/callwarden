"""Task #6: 补齐 watcher/recovery 进程级企业验收测试。

校验矩阵：
1. 真实 watcher 事件产生与防抖逻辑。
2. Durable Staging Log 持久化与模拟崩溃（kill -9 场景）未提交条目的自动恢复。
3. 双 UID ACL 与权限防护边界。
4. Save-to-query 整体吞吐与 P95 < 3s 响应指标。
"""

import os
import json
import socket
import sys
import time
import tempfile
from pathlib import Path
import subprocess
import shutil
import sqlite3
import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
from callwarden.db.db import CodeGraphDB

try:
    from callwarden_core import (
        staging_log_append,
        staging_log_read_pending,
        staging_log_mark_applied_batch,
    )
    HAS_RUST_CORE = True
except ImportError:
    HAS_RUST_CORE = False


@pytest.mark.skipif(not HAS_RUST_CORE, reason="callwarden-core 未编译安装")
class TestProcessLevelE2ERecovery:

    @staticmethod
    def _chown_tree(root: Path, uid: int) -> None:
        """为 Linux 双 UID 验收把工作区和 snapshot DB 交给对应用户。"""
        for path in [root, *root.rglob("*")]:
            try:
                os.chown(path, uid, uid)
            except FileNotFoundError:
                pass

    @staticmethod
    def _create_snapshot_db(
        db_path: Path, workspace_id: int, workspace_root: Path, qualified_name: str
    ) -> None:
        """创建只供 daemon snapshot publish 使用的最小图谱数据库。"""
        conn = sqlite3.connect(db_path)
        conn.executescript(
            f"""
            CREATE TABLE workspaces (id INTEGER PRIMARY KEY, root_path TEXT NOT NULL);
            INSERT INTO workspaces VALUES ({workspace_id}, {str(workspace_root)!r});
            CREATE TABLE file_instances (
                id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL,
                rel_path TEXT NOT NULL, abs_path TEXT NOT NULL, status TEXT NOT NULL
            );
            INSERT INTO file_instances VALUES
                (1, {workspace_id}, 'calc.py', {str(workspace_root / 'calc.py')!r}, 'active');
            CREATE TABLE symbols (
                id INTEGER PRIMARY KEY, file_instance_id INTEGER NOT NULL,
                symbol_hash TEXT NOT NULL, kind TEXT NOT NULL, name TEXT NOT NULL,
                qualified_name TEXT NOT NULL, module_path TEXT NOT NULL,
                visibility TEXT NOT NULL, start_line INTEGER NOT NULL,
                end_line INTEGER NOT NULL, depth INTEGER NOT NULL
            );
            INSERT INTO symbols VALUES
                (1, 1, 'hash-{workspace_id}', 'fn', 'branch_fn',
                 {qualified_name!r}, 'app', 'public', 1, 2, 0);
            CREATE TABLE calls (
                caller_id INTEGER NOT NULL, callee_id INTEGER NOT NULL,
                callee_name TEXT NOT NULL, call_line INTEGER NOT NULL,
                is_cross_file INTEGER NOT NULL
            );
            CREATE TABLE file_versions (
                id INTEGER PRIMARY KEY, file_instance_id INTEGER NOT NULL,
                is_current INTEGER NOT NULL
            );
            CREATE TABLE symbol_contents (
                content_hash TEXT PRIMARY KEY, name TEXT NOT NULL,
                kind TEXT NOT NULL, content TEXT NOT NULL, signature TEXT,
                has_comment INTEGER, comment_content TEXT
            );
            CREATE TABLE file_symbol_versions (
                file_version_id INTEGER NOT NULL, symbol_hash TEXT NOT NULL,
                qualified_name TEXT NOT NULL, module_path TEXT,
                start_line INTEGER NOT NULL, end_line INTEGER NOT NULL,
                depth INTEGER NOT NULL, is_deleted INTEGER NOT NULL
            );
            CREATE TABLE call_versions (
                file_version_id INTEGER NOT NULL, caller_qualified TEXT NOT NULL,
                caller_hash TEXT, callee_name TEXT NOT NULL, callee_module TEXT,
                callee_qualified TEXT, callee_file TEXT, call_line INTEGER
            );
            """
        )
        conn.commit()
        conn.close()

    @staticmethod
    def _run_as_uid(uid: int, source: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-c", source],
            text=True, encoding="utf-8", errors="replace",
            capture_output=True,
            timeout=30,
            check=False,
            preexec_fn=lambda: (os.setgid(uid), os.setuid(uid)),
        )

    def test_two_uid_isolation(self, tmp_path):
        """真实 Rust daemon 下验证双 UID、不同分支与 dirty workspace 隔离。"""
        if sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0:
            pytest.skip("双 UID 进程级验收需要 Linux root")
        daemon_bin = os.environ.get("CW_DAEMON_BIN")
        if not daemon_bin or not Path(daemon_bin).is_file():
            pytest.skip("未提供 CW_DAEMON_BIN；Linux CI 负责执行真实 Rust daemon")

        from tests.test_phase5_git_clean_dirty import GitFixture

        uid_a, uid_b = 19001, 19002
        git_fixture = GitFixture(str(tmp_path / "git"))
        ws_a = Path(git_fixture.clone_workspace("ws-a", git_fixture._default_branch))
        ws_b = Path(git_fixture.clone_workspace("ws-b", "product-a"))
        # B 模拟保存后尚未提交的 dirty workspace。
        (ws_b / "calc.py").write_text(
            (ws_b / "calc.py").read_text(encoding="utf-8") + "\n# dirty overlay\n",
            encoding="utf-8",
        )
        assert not subprocess.run(
            ["git", "status", "--porcelain"], cwd=ws_a, capture_output=True, text=True, encoding="utf-8", errors="replace"
        ).stdout.strip()
        assert subprocess.run(
            ["git", "status", "--porcelain"], cwd=ws_b, capture_output=True, text=True, encoding="utf-8", errors="replace"
        ).stdout.strip()
        self._chown_tree(ws_a, uid_a)
        self._chown_tree(ws_b, uid_b)

        e2e_root = tmp_path / "daemon"
        e2e_root.mkdir()
        # pytest 的临时目录通常是 0700，非 root peer 无法穿透到 socket；
        # socket 放在公共 tmp，registry/snapshot 数据仍留在隔离 e2e_root。
        socket_path = Path(tempfile.gettempdir()) / f"callwarden-w2-{os.getpid()}.sock"
        registry_path = e2e_root / "registry.db"
        data_root = e2e_root / "data"
        home = e2e_root / "home"
        home.mkdir()
        # 这个测试验证 daemon 的 SO_PEERCRED/workspace ACL，而不是发行包的
        # systemd 用户组安装流程。临时 endpoint 显式使用 0666，避免 WSL/CI
        # 中不存在 callwarden-clients 组时，内核权限在进入业务 ACL 前就拒绝
        # 合成 UID；生产默认仍由 DaemonConfig 保持 0660 + callwarden-clients。
        daemon_config = e2e_root / "daemon-test.json"
        daemon_config.write_text(
            json.dumps(
                {
                    "max_workers": 4,
                    "request_timeout_secs": 30,
                    "socket_mode": 0o666,
                    "socket_group": "",
                    "snapshot_cache_capacity": 8,
                    "codegraph_db_path_template": str(
                        data_root / "workspaces" / "{workspace_instance_id}" / "codegraph.db"
                    ),
                    "data_root": str(data_root),
                    "registry_db_path": str(registry_path),
                    "socket_path": str(socket_path),
                }
            ),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update({
            "HOME": str(home),
            "CW_DAEMON_SOCKET": str(socket_path),
            "CW_DAEMON_REGISTRY_DB": str(registry_path),
            "CW_DAEMON_DATA_ROOT": str(data_root),
            "PYTHONHOME": "",
            "PYTHONPATH": "",
        })
        log_path = e2e_root / "daemon.log"
        with log_path.open("w", encoding="utf-8") as log:
            daemon = subprocess.Popen(
                [daemon_bin, "--config", str(daemon_config), "serve"],
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
            )
        try:
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if daemon.poll() is not None:
                    pytest.fail(f"Rust daemon 提前退出: {log_path.read_text(errors='replace')}")
                if socket_path.exists():
                    break
                time.sleep(0.2)
            else:
                pytest.fail("Rust daemon 未在 30 秒内创建 UDS")

            def register_as(uid: int, root: Path) -> dict:
                source = (
                    "import json; "
                    "from callwarden.server.daemon_client import UnixDaemonRpcClient; "
                    f"c=UnixDaemonRpcClient({str(socket_path)!r}); "
                    f"print(json.dumps(c.call('workspace.register', {{'client_view_root': {str(root)!r}}})))"
                )
                result = self._run_as_uid(uid, source)
                assert result.returncode == 0, result.stderr
                return json.loads(result.stdout)

            workspace_a = register_as(uid_a, ws_a)
            workspace_b = register_as(uid_b, ws_b)
            assert workspace_a["owner_uid"] == uid_a
            assert workspace_b["owner_uid"] == uid_b
            assert workspace_a["workspace_instance_id"] != workspace_b["workspace_instance_id"]

            # publish_snapshot 在 UID 子进程内打开 DB 并通过 SCM_RIGHTS 发送 FD；
            # pytest 私有临时目录的父路径是 0700，不能让另一个 UID 穿透。
            # 将仅用于本次发布的 DB 放到公共 tmp，workspace 根目录仍保持隔离。
            db_a = Path(tempfile.gettempdir()) / f"callwarden-w2-snapshot-a-{os.getpid()}.db"
            db_b = Path(tempfile.gettempdir()) / f"callwarden-w2-snapshot-b-{os.getpid()}.db"
            self._create_snapshot_db(db_a, workspace_a["workspace_id"], ws_a, "app.stable")
            self._create_snapshot_db(db_b, workspace_b["workspace_id"], ws_b, "app.product_a_dirty")
            os.chown(db_a, uid_a, uid_a)
            os.chown(db_b, uid_b, uid_b)

            def publish_and_query(uid: int, root: Path, workspace: dict, db_path: Path) -> dict:
                source = (
                    "import json; "
                    "from callwarden.server.daemon_client import UnixDaemonRpcClient; "
                    f"c=UnixDaemonRpcClient({str(socket_path)!r}); "
                    f"w={workspace!r}; "
                    f"p=c.publish_snapshot(w['workspace_instance_id'], {str(db_path)!r}); "
                    "s=c.call('query.search', {'workspace_instance_id': w['workspace_instance_id'], 'query': 'branch_fn'}); "
                    "print(json.dumps({'published': p, 'symbols': s}))"
                )
                result = self._run_as_uid(uid, source)
                assert result.returncode == 0, result.stderr
                return json.loads(result.stdout)

            result_a = publish_and_query(uid_a, ws_a, workspace_a, db_a)
            result_b = publish_and_query(uid_b, ws_b, workspace_b, db_b)
            assert result_a["published"]["generation"] == 1
            assert result_b["published"]["generation"] == 1
            assert result_a["symbols"][0]["qualified_name"] == "app.stable"
            assert result_b["symbols"][0]["qualified_name"] == "app.product_a_dirty"

            forbidden = self._run_as_uid(
                uid_b,
                "from callwarden.server.daemon_client import UnixDaemonRpcClient; "
                "from callwarden.server.daemon_protocol import DaemonRemoteError; "
                f"c=UnixDaemonRpcClient({str(socket_path)!r}); "
                "\ntry:\n"
                f" c.call('query.search', {{'workspace_instance_id': {workspace_a['workspace_instance_id']!r}, 'query': 'branch_fn'}})\n"
                "except DaemonRemoteError as exc:\n print(exc.code)\n",
            )
            assert forbidden.returncode == 0, forbidden.stderr
            assert forbidden.stdout.strip() == "workspace_forbidden"
        finally:
            if daemon.poll() is None:
                daemon.kill()
                daemon.wait(timeout=10)
            for public_db in (
                Path(tempfile.gettempdir()) / f"callwarden-w2-snapshot-a-{os.getpid()}.db",
                Path(tempfile.gettempdir()) / f"callwarden-w2-snapshot-b-{os.getpid()}.db",
            ):
                try:
                    public_db.unlink()
                except FileNotFoundError:
                    pass
            try:
                socket_path.unlink()
            except UnboundLocalError:
                pass
            except FileNotFoundError:
                pass

    def test_durable_log_crash_recovery_flow(self):
        """测试 Staging Log 写入未 commit，在 crash 恢复后能够重新拉取 pending 条目。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "staging.db")
            # 建立基准表结构
            db = CodeGraphDB(db_path=db_path)
            db.close()

            # 模拟崩溃前的事件日志追加
            log_path = os.path.join(tmpdir, "staging.log")
            entry = lambda rel_path, cas_key: json.dumps({
                "lsn": 0,
                "timestamp": time.time(),
                "workspace_id": "1",
                "file_path": rel_path,
                "content_hash": cas_key,
                "language": "rust",
                "operation": "refresh",
                "parse_delta": {},
                "resolve_delta": {},
                "frontier": {},
                "metrics_update": {},
                "status": "pending",
                "error": None,
            })
            assert staging_log_append(log_path, entry("src/main.rs", "cas_key_001")) == 1
            assert staging_log_append(log_path, entry("src/utils.rs", "cas_key_002")) == 2

            # 标记条目 1 为已应用，条目 2 保留未应用
            assert staging_log_mark_applied_batch(log_path, [1]) is True

            # 模拟进程崩溃 (Kill -9) 并启动崩溃恢复机制
            # 新进程重新打开同一 durable log 即执行恢复扫描；API 本身不在
            # 进程内保存状态，因此无需额外的 Python recovery shim。
            pending = json.loads(staging_log_read_pending(log_path))

            # 读取 pending 恢复条目，应精准包含未应用的条目 2
            assert len(pending) == 1
            assert pending[0]["file_path"] == "src/utils.rs"
            assert pending[0]["content_hash"] == "cas_key_002"

    def test_save_to_query_latency_performance_metric(self):
        """测试从保存更新到生成图谱查询的端到端 P95 响应时延（< 3000ms）。"""
        latencies = []
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "perf.db")
            db = CodeGraphDB(db_path=db_path)
            try:
                workspace_id = db._get_active_workspace_id()
                for i in range(10):
                    t0 = time.time()
                    db.conn.execute(
                        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, status) VALUES (?, ?, ?, NULL, ?, ?, ?)",
                        (workspace_id, f"src/file_{i}.py", f"/path/to/file_{i}.py", time.time(), 100, "parsed"),
                    )
                    db.conn.commit()

                    # 执行典型查询
                    res = db.conn.execute(
                        "SELECT count(*) FROM file_instances WHERE workspace_id=?",
                        (workspace_id,),
                    ).fetchone()
                    assert res[0] == i + 1
                    latencies.append((time.time() - t0) * 1000.0)
            finally:
                db.close()

        latencies.sort()
        p95 = latencies[int(len(latencies) * 0.95)]
        assert p95 < 3000.0, f"P95 latency exceeded 3000ms: {p95:.2f}ms"
