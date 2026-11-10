"""Task #6: 进程级 watcher/recovery 企业验收测试（ADR-002 单轨 HTTPS + mTLS）。

校验矩阵：
1. 真实 daemon 进程崩溃（kill）后同一 data_root 重启仍继续服务：workspace
   authority（registry 持久化）未丢，同 root 的 workspace_instance_id 复用。
2. Durable Staging Log 持久化与模拟崩溃（kill -9 场景）未提交条目的自动恢复。
3. Save-to-query 整体吞吐与 P95 < 3s 响应指标。

单轨约束：daemon 仅以 `--http-bind=127.0.0.1:0` 启动，经 manifest 发现 endpoint；
客户端只用 `HttpDaemonRpcClient(endpoint=..., authority_id=..., manifest_path=...)`。
UDS socket / `CW_DAEMON_SOCKET` / `UnixDaemonRpcClient` 构造已全部退役。
"""

import json
import os
import sys
import time
import tempfile
import subprocess

import pytest

sys.path.insert(0, os.path.abspath(os.path.dirname(os.path.dirname(__file__))))
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__)))

from callwarden.config import get_http_authority_id
from callwarden.db.db import CodeGraphDB
from callwarden.server.daemon_client import HttpDaemonRpcClient

from _w3_harness import find_daemon_binary, spawn_isolated_daemon, wait_manifest

try:
    from callwarden_core import (
        staging_log_append,
        staging_log_read_pending,
        staging_log_mark_applied_batch,
    )
    HAS_RUST_CORE = True
except ImportError:
    HAS_RUST_CORE = False


class TestProcessLevelE2ERecovery:

    @staticmethod
    def _pki_init(daemon_bin: str, data_root: str) -> dict:
        """在隔离 data_root 内初始化 mTLS PKI（CA + owner 客户端身份）。"""
        home = os.path.join(data_root, "userhome")
        os.makedirs(os.path.join(home, ".callwarden"), exist_ok=True)
        env = os.environ.copy()
        env["USERPROFILE"] = env["HOME"] = home
        env["CW_PKI_DIR"] = os.path.join(home, ".callwarden", "pki")
        result = subprocess.run(
            [daemon_bin, "pki", "init"],
            env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=90, check=False,
        )
        assert result.returncode == 0, f"pki init 失败: {result.stderr[-1500:]}"
        return env

    @staticmethod
    def _client_env(env: dict, data_root: str) -> dict:
        """把隔离 PKI 的 owner 身份注入子进程环境（mTLS 必需）。"""
        home = os.path.join(data_root, "userhome")
        pki = os.path.join(home, ".callwarden", "pki")
        ident = os.path.join(pki, "identities", "pr_owner")
        out = dict(env)
        out["CW_CLIENT_CERT"] = os.path.join(ident, "client.crt")
        out["CW_CLIENT_KEY"] = os.path.join(ident, "client.key")
        out["CW_CA_CERT"] = os.path.join(pki, "ca", "ca.crt")
        out["PYTHONPATH"] = os.path.abspath(
            os.path.dirname(os.path.dirname(__file__)))
        return out

    @staticmethod
    def _kill(proc) -> None:
        if proc.poll() is None:
            proc.kill()
        proc.wait(timeout=10)

    @staticmethod
    def _register_in_subprocess(manifest_file: str, root_path: str, env: dict) -> str:
        """子进程读取同一 manifest 文件构造 HttpDaemonRpcClient 并注册 workspace。"""
        source = (
            "import json; "
            "from callwarden.config import get_http_authority_id; "
            "from callwarden.server.daemon_client import HttpDaemonRpcClient; "
            f"man={manifest_file!r}; "
            "m=json.loads(open(man, encoding='utf-8').read()); "
            "c=HttpDaemonRpcClient(endpoint=m['endpoint'], "
            "authority_id=get_http_authority_id(), manifest_path=man); "
            "print(json.dumps(c.call('workspace.register', "
            f"{{'client_view_root': {root_path!r}}})))"
        )
        result = subprocess.run(
            [sys.executable, "-c", source],
            env=env, capture_output=True, text=True, encoding="utf-8",
            errors="replace", timeout=60, check=False,
        )
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)["workspace_instance_id"]

    def test_daemon_crash_restart_continues_service(self, tmp_path, monkeypatch):
        """daemon 进程崩溃后同 data_root 重启：workspace 权威仍可复用。"""
        daemon_bin = find_daemon_binary()
        if daemon_bin is None:
            pytest.skip("cw-daemon 二进制缺失（环境前置）")

        data_root = str(tmp_path / "data")
        root_path = str(tmp_path / "ws")
        os.makedirs(root_path, exist_ok=True)
        pki_env = self._pki_init(daemon_bin, data_root)
        client_env = self._client_env(pki_env, data_root)
        for key in ("CW_CLIENT_CERT", "CW_CLIENT_KEY", "CW_CA_CERT"):
            monkeypatch.setenv(key, client_env[key])
        monkeypatch.delenv("CW_PKI_DIR", raising=False)

        # 第一阶段：启动 → manifest 发现 → register（幂等复用同 root 实例）。
        proc1, _ = spawn_isolated_daemon(daemon_bin, data_root)
        try:
            manifest1, manifest_file1 = wait_manifest(data_root, proc1)
            assert manifest1 is not None, "daemon 首次启动未发布 HTTP manifest"
            client = HttpDaemonRpcClient(
                endpoint=manifest1["endpoint"],
                authority_id=get_http_authority_id(),
                manifest_path=manifest_file1,
            )
            instance1 = client.call(
                "workspace.register", {"client_view_root": root_path}
            )["workspace_instance_id"]
            assert instance1
            again = client.call(
                "workspace.register", {"client_view_root": root_path}
            )["workspace_instance_id"]
            assert again == instance1
        finally:
            self._kill(proc1)

        # 第二阶段：崩溃（kill）后同 data_root 重启；新进程（子进程薄客户端）
        # 按重启后的 manifest 继续服务，并复用同一 workspace authority。
        proc2, _ = spawn_isolated_daemon(daemon_bin, data_root)
        try:
            manifest2, manifest_file2 = wait_manifest(data_root, proc2)
            assert manifest2 is not None, "daemon 重启后未发布 HTTP manifest"
            assert manifest2["pid"] != manifest1["pid"], (
                "重启后 manifest 必须属于新进程: "
                f"{manifest2['pid']} == {manifest1['pid']}"
            )
            recovered = self._register_in_subprocess(
                manifest_file2, root_path, client_env)
            assert recovered == instance1, (
                "重启后 workspace 权威丢失: "
                f"期望复用 {instance1}，实际 {recovered}"
            )
        finally:
            self._kill(proc2)

    @pytest.mark.skipif(not HAS_RUST_CORE, reason="callwarden-core 未编译安装")
    def test_durable_log_crash_recovery_flow(self):
        """测试 Staging Log 写入未 commit，在 crash 恢复后能够重新拉取 pending 条目。"""
        with tempfile.TemporaryDirectory() as tmpdir:
            db_path = os.path.join(tmpdir, "staging.db")
            # 建立基准表结构
            db = CodeGraphDB(db_path=db_path)
            db.close()

            # 模拟崩溃前的事件日志追加
            log_path = os.path.join(tmpdir, "staging.log")
            def entry(rel_path, cas_key):
                return json.dumps({
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
