r"""W1-3（T-1786808777379-c87171e7）snapshot 管理 HTTP daemon 通道单测。

背景：项目已默认 HTTP transport（CW_DAEMON_TRANSPORT=http，H6）。W1 批次
最后一个子任务：给 HttpDaemonRpcClient 补齐 snapshot 三个便捷方法
（snapshot_stats / snapshot_list_workspaces / snapshot_evict），对齐 W1-1
workspace_status 与 W1-2 workspace_register 系列的注入模式。

桥接设计（零 Rust 改动，Rust handler 契约见 snapshot_state.rs L1054-1190）：
- `HttpDaemonRpcClient.snapshot_stats(db_path)`：经 `_ensure_remote_snapshot`
  注册 workspace + 按需 snapshot.publish，注入权威 workspace_instance_id 后
  call("snapshot.stats")；db_path=None 时不发布（Rust 返回 snapshot_not_ready，
  fail-closed，不静默回退本地 SQL）。
- `HttpDaemonRpcClient.snapshot_list_workspaces()`：无参 call，peer UID 过滤
  （P0-2 整改：非 admin 只能看到自己的 workspace）。
- `HttpDaemonRpcClient.snapshot_evict(workspace_instance_id)`：直接 call，
  owned ACL 校验后驱逐 snapshot cache。

Rust 侧语义（只读确认，禁止改动）：
- snapshot.stats：require_str_param(workspace_instance_id) + owned_workspace
  ACL（owner_uid 匹配 + 非 archived，越权/不存在 → workspace_not_found）；
  未发布 → snapshot_not_ready。返回 workspace_instance_id/generation/
  symbol_count/call_count/file_count/build_duration_ms/last_error/
  source_db_path/history_len。
- snapshot.list_workspaces：无参数；admin（peer.uid==0 或 daemon uid）看全部，
  非 admin 经 registry owner_uid 交集过滤，只返回自己的 workspace。
- snapshot.evict：require_str_param(workspace_instance_id) + owned_workspace
  ACL；返回 {"evicted": bool, "workspace_instance_id"}，幂等（不在 cache →
  evicted=false，但 workspace 未注册仍被 ACL 拒绝）。

覆盖矩阵（对齐统一验收标准 6 问）：
- HTTP 注入：snapshot_stats 自动 register（+ 按需 publish）后注入权威
  instance_id；无 db_path 跳过 publish；缓存复用不重复 register；register
  响应缺 instance_id → DaemonUnavailableError（fail-closed）
- 无参契约：snapshot_list_workspaces 不注入、不 register（Rust 不 require）
- 越界参数：evict/stats 缺 instance_id → invalid_params（Rust 强制 require）；
  不存在 instance_id → workspace_not_found（owned ACL）
- snapshot_not_ready 语义：注册但未发布 → snapshot_not_ready（fail-closed，
  不静默回退本地 SQL）；evict 后再次 stats → snapshot_not_ready（cache 已驱逐）
- 跨 workspace 隔离：list_workspaces 结果按 peer UID 过滤；evict 只能驱逐
  自己的 workspace
- Python fallback 边界：HTTP 模式无 SQL 路径（HttpDaemonRpcClient 零 SQL，
  fail-closed）；legacy UnixDaemonRpcClient.call 连接失败抛
  DaemonUnavailableError，publish_snapshot 不静默回退；legacy DaemonClient
  侧无 snapshot.stats/list_workspaces/evict 调用点（grep 确认仅 publish_snapshot）
- 进程级 round-trip：真实 daemon register→publish→stats 命中→list 可见→
  evict 驱逐→幂等/拒绝态验证

前置条件（进程级部分，与 test_workspace_write_rpc_http.py 一致）：
1. Windows 平台
2. 已构建 `cw-daemon.exe`（cargo build --release --no-default-features
   --manifest-path rust_ext/Cargo.toml --bin cw-daemon）
3. 默认 HTTP endpoint（authority-scoped manifest）未被生产 daemon 占用
   （占用则设计性 skip，避免污染生产 registry 与覆盖 ~/.callwarden 权威
   manifest）
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time

import pytest

from callwarden.server.daemon_client import DaemonUnavailableError
from callwarden.server.daemon_protocol import DaemonRemoteError

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DAEMON_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "release", "cw-daemon.exe")

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="进程级 snapshot round-trip 需要 Windows + loopback HTTP daemon",
)

requires_binaries = pytest.mark.skipif(
    not os.path.exists(_DAEMON_BIN),
    reason="cw-daemon.exe 未构建（需先 cargo build --bin cw-daemon）",
)


# ----------------------------------------------------------------------
# HTTP 便捷方法注入 harness（禁真实 daemon，仅 mock call）
# ----------------------------------------------------------------------

class _SnapshotHarness:
    """snapshot 三便捷方法注入 harness（对齐 test_workspace_write_rpc_http.py
    _WriteClientHarness 模式）。"""

    @staticmethod
    def _make_client(monkeypatch, register_ok=True):
        from callwarden.server.daemon_client import HttpDaemonRpcClient
        client = HttpDaemonRpcClient.__new__(HttpDaemonRpcClient)
        client._remote_workspace_id = None
        client._remote_snapshot_ready = False
        client._project_root = None
        calls = []

        def fake_call(method, params, request_id=None):
            calls.append((method, params))
            if method == "workspace.register":
                if not register_ok:
                    return {"workspace_id": 1}  # 缺少 workspace_instance_id
                return {"workspace_id": 1, "workspace_instance_id": "inst-s"}
            if method == "snapshot.publish":
                return {"ok": True, "snapshot_id": "snap-s"}
            if method == "snapshot.stats":
                return {
                    "workspace_instance_id": params.get("workspace_instance_id"),
                    "generation": 3,
                    "symbol_count": 2,
                    "call_count": 1,
                    "file_count": 1,
                    "build_duration_ms": 10,
                    "last_error": None,
                    "source_db_path": "",
                    "history_len": 1,
                }
            if method == "snapshot.list_workspaces":
                return [{
                    "workspace_instance_id": "inst-s",
                    "generation": 3,
                    "history_len": 1,
                    "symbol_count": 2,
                    "call_count": 1,
                    "file_count": 1,
                }]
            if method == "snapshot.evict":
                return {
                    "evicted": params.get("workspace_instance_id") == "inst-s",
                    "workspace_instance_id": params.get("workspace_instance_id"),
                }
            raise AssertionError(f"意外 method: {method}")

        monkeypatch.setattr(client, "call", fake_call)
        return client, calls


class TestHttpSnapshotConvenienceMethods:
    """W1-3：HttpDaemonRpcClient snapshot 三便捷方法自动注入权威
    workspace_instance_id（Rust handle_snapshot_stats/evict 强制 require，
    缺注入返回 invalid_params；list_workspaces 无参）。"""

    def test_snapshot_stats_registers_publishes_and_injects(self, monkeypatch):
        client, calls = _SnapshotHarness._make_client(monkeypatch)
        db_path = os.path.join(os.getcwd(), "snap.db")

        result = client.snapshot_stats(db_path=db_path)

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "snapshot.publish", "snapshot.stats"], \
            f"调用序应为 register→publish→stats，实际 {methods}"
        # publish：注入权威 instance_id + 透传 db_path（abspath 规范化）
        assert calls[1][1]["workspace_instance_id"] == "inst-s"
        assert calls[1][1]["db_path"] == os.path.abspath(db_path)
        # stats：注入权威 instance_id —— 本任务核心断言
        assert calls[2][1] == {"workspace_instance_id": "inst-s"}
        assert result["symbol_count"] == 2

    def test_snapshot_stats_without_db_path_skips_publish(self, monkeypatch):
        """db_path=None 时不发布（Rust 返回 snapshot_not_ready，fail-closed）。"""
        client, calls = _SnapshotHarness._make_client(monkeypatch)

        result = client.snapshot_stats()

        methods = [m for m, _ in calls]
        assert methods == ["workspace.register", "snapshot.stats"], \
            f"无 db_path 不得 publish，实际 {methods}"
        assert calls[1][1] == {"workspace_instance_id": "inst-s"}

    def test_snapshot_stats_reuses_without_repeat_register(self, monkeypatch):
        """重复调用不重复 register / publish（缓存复用）。"""
        client, calls = _SnapshotHarness._make_client(monkeypatch)
        db_path = os.path.join(os.getcwd(), "snap.db")

        client.snapshot_stats(db_path=db_path)
        client.snapshot_stats(db_path=db_path)

        methods = [m for m, _ in calls]
        assert methods.count("workspace.register") == 1, "缓存命中后不得重复 register"
        assert methods.count("snapshot.publish") == 1, "缓存命中后不得重复 publish"
        assert calls[-1][1] == {"workspace_instance_id": "inst-s"}

    def test_snapshot_stats_register_missing_instance_id_raises(self, monkeypatch):
        """fail-closed：register 响应缺 workspace_instance_id → DaemonUnavailableError。"""
        client, _calls = _SnapshotHarness._make_client(monkeypatch, register_ok=False)
        with pytest.raises(DaemonUnavailableError):
            client.snapshot_stats()

    def test_snapshot_list_workspaces_no_params(self, monkeypatch):
        """list_workspaces 无参、不 register、不注入（Rust 不 require 参数）。"""
        client, calls = _SnapshotHarness._make_client(monkeypatch)

        result = client.snapshot_list_workspaces()

        assert calls == [("snapshot.list_workspaces", {})], \
            f"list_workspaces 应为无参调用，实际 {calls}"
        assert isinstance(result, list) and result[0]["workspace_instance_id"] == "inst-s"

    def test_snapshot_evict_injects_instance_id(self, monkeypatch):
        """evict 直接透传调用方提供的 instance_id（Rust 强制 require）。"""
        client, calls = _SnapshotHarness._make_client(monkeypatch)

        result = client.snapshot_evict("inst-x")

        assert calls == [("snapshot.evict", {"workspace_instance_id": "inst-x"})]
        assert result == {"evicted": False, "workspace_instance_id": "inst-x"}


# ----------------------------------------------------------------------
# 进程级 round-trip（设计性 skip：生产 daemon 占用默认 HTTP 端口时跳过）
# ----------------------------------------------------------------------

def _http_manifest_occupied() -> bool:
    """判断权威 HTTP manifest 是否"占用"（有效或 stale 均视为占用）。

    对齐 test_workspace_write_rpc_http.py：生产 daemon（transport=http）运行中时
    manifest 有效 → True（skip）；manifest 存在但 stale → 保守视为占用。
    仅当 E_HTTP_MANIFEST_MISSING（manifest 完全不存在）才返回 False。
    """
    from callwarden.config import get_http_authority_id
    from callwarden.server.daemon_autostart import resolve_http_endpoint_and_manifest
    from callwarden.server.daemon_client import HttpDaemonRpcClient
    try:
        endpoint, _manifest = resolve_http_endpoint_and_manifest(
            authority_id=get_http_authority_id()
        )
    except DaemonRemoteError as exc:
        if getattr(exc, "code", "") == "E_HTTP_MANIFEST_MISSING":
            return False
        return True
    client = HttpDaemonRpcClient.__new__(HttpDaemonRpcClient)
    client._resolved_endpoint = endpoint
    try:
        resp = client.call("ping")
        return not (isinstance(resp, dict) and resp.get("status") == "ok")
    except Exception:
        return True


def _spawn_isolated_daemon(bin_path, data_root, http_bind):
    """启动隔离 daemon（临时 task DB / registry / 管道），启用 HTTP transport。"""
    env = os.environ.copy()
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_SOCKET"] = os.path.join(data_root, "pipe")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    proc = subprocess.Popen(
        [bin_path, "--http-bind=" + http_bind],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    return proc


def _wait_isolated_manifest(proc, timeout=15.0):
    from callwarden.config import get_http_authority_id
    authority = get_http_authority_id()
    safe = authority.replace("/", "_").replace("\\", "_").replace(":", "_")
    manifest_path = os.path.join(
        os.path.expanduser("~"), ".callwarden",
        f"http-daemon.{safe}.manifest.json",
    )
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None
        if os.path.isfile(manifest_path):
            try:
                with open(manifest_path, "r", encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                time.sleep(0.2)
                continue
            if m.get("pid") == proc.pid:
                return m
        time.sleep(0.2)
    return None


def _terminate(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def _make_minimal_db(path, workspace_id=1) -> str:
    """构造可被 daemon GraphStore 构建的最小 callwarden 库。

    对齐 test_phase4_query_budget.py minimal_db fixture：
    file_instances / symbols / calls 三表 + 少量行。

    P0-2 整改后 GraphStore build 的 SQL 强制 `AND workspace_id = <id>`
    （graph.rs _load_from_sqlite_mode），file_instances 必须带 workspace_id
    列且行值等于 publish 时 registry 的数值 workspace_id（ROWID），否则
    过滤查不到任何行 → 空快照。abs_path/mtime 为真实 schema 的 NOT NULL
    列，一并对齐（w1_3_http_verify.py _make_minimal_db 同款契约）。
    """
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute("""CREATE TABLE file_instances (
        id INTEGER PRIMARY KEY,
        workspace_id INTEGER NOT NULL,
        rel_path TEXT NOT NULL,
        abs_path TEXT NOT NULL,
        mtime REAL NOT NULL,
        status TEXT DEFAULT 'active')""")
    cur.execute("INSERT INTO file_instances VALUES (1, ?, 'src/main.py', '/abs/src/main.py', 0.0, 'active')",
                (workspace_id,))
    cur.execute("""CREATE TABLE symbols (
        id INTEGER PRIMARY KEY, file_instance_id INTEGER, kind TEXT,
        name TEXT, qualified_name TEXT, module_path TEXT,
        start_line INTEGER, end_line INTEGER, depth INTEGER)""")
    cur.execute("""INSERT INTO symbols VALUES
        (1, 1, 'fn', 'main', 'main', '', 1, 10, 0),
        (2, 1, 'fn', 'init', 'main.init', '', 11, 20, 1)""")
    cur.execute("""CREATE TABLE calls (
        caller_id INTEGER, callee_id INTEGER, callee_name TEXT,
        call_line INTEGER, is_cross_file INTEGER)""")
    cur.execute("INSERT INTO calls VALUES (1, 2, 'init', 5, 0)")
    conn.commit()
    conn.close()
    return str(path)


class TestRealDaemonSnapshotRoundTrip:
    """真实 daemon 进程级 snapshot round-trip（设计性 skip，对齐 W1-1/W1-2）。

    a) stats 缺参 → invalid_params（Rust 强制 require）
    b) stats/evict 不存在 instance_id → workspace_not_found（owned ACL）
    c) 注册未发布 → stats → snapshot_not_ready（fail-closed，不静默回退）
    d) 便捷方法 snapshot_stats(db_path) 全链路：register→publish→stats 命中
    e) list_workspaces 可见刚发布的 workspace（按 peer UID 过滤）
    f) evict → evicted=true；重复 evict → evicted=false（幂等）
    g) evict 缺参 → invalid_params
    """

    @pytest.fixture
    def real_daemon_client(self, tmp_path):
        if _http_manifest_occupied():
            pytest.skip(
                "权威 HTTP manifest 被占用（生产 daemon 运行中或残留 stale "
                "manifest）；为避免污染生产 registry（daemon_workspaces 表）"
                "与覆盖 ~/.callwarden 权威 manifest，进程级 round-trip 设计性 "
                "skip（对齐 test_workspace_write_rpc_http.py skip 模式）"
            )
        bin_path = _DAEMON_BIN
        if not os.path.exists(bin_path):
            pytest.skip("cw-daemon.exe 未构建（需先 cargo build --bin cw-daemon）")
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        proc = _spawn_isolated_daemon(bin_path, data_root, "127.0.0.1:0")
        try:
            manifest = _wait_isolated_manifest(proc)
            if manifest is None:
                pytest.fail("隔离 daemon 未在超时内发布 manifest")
            from callwarden.server.daemon_client import HttpDaemonRpcClient
            client = HttpDaemonRpcClient(
                endpoint=manifest["endpoint"],
                verify_health=False,
                timeout=5.0,
            )
            yield client
        finally:
            _terminate(proc)

    @requires_binaries
    def test_stats_missing_instance_invalid_params(self, real_daemon_client):
        """a) 缺 workspace_instance_id 的 snapshot.stats → invalid_params。"""
        with pytest.raises(DaemonRemoteError) as exc:
            real_daemon_client.call("snapshot.stats", {})
        assert exc.value.code == "invalid_params"

    @requires_binaries
    def test_evict_missing_instance_invalid_params(self, real_daemon_client):
        """g) 缺 workspace_instance_id 的 snapshot.evict → invalid_params。"""
        with pytest.raises(DaemonRemoteError) as exc:
            real_daemon_client.call("snapshot.evict", {})
        assert exc.value.code == "invalid_params"

    @requires_binaries
    def test_stats_unknown_instance_workspace_not_found(self, real_daemon_client):
        """b) 不存在 instance_id 的 snapshot.stats → workspace_not_found。"""
        with pytest.raises(DaemonRemoteError) as exc:
            real_daemon_client.call(
                "snapshot.stats", {"workspace_instance_id": "deadbeefdeadbeef01"}
            )
        assert exc.value.code == "workspace_not_found"

    @requires_binaries
    def test_evict_unknown_instance_workspace_not_found(self, real_daemon_client):
        """b) 不存在 instance_id 的 snapshot.evict → workspace_not_found。"""
        with pytest.raises(DaemonRemoteError) as exc:
            real_daemon_client.call(
                "snapshot.evict", {"workspace_instance_id": "deadbeefdeadbeef01"}
            )
        assert exc.value.code == "workspace_not_found"

    @requires_binaries
    def test_stats_before_publish_snapshot_not_ready(self, real_daemon_client, tmp_path):
        """c) 注册但未发布 → snapshot.stats → snapshot_not_ready（fail-closed，
        不静默回退本地 SQL）。"""
        root = tempfile.mkdtemp(prefix="cw_w13_ws_")
        try:
            reg = real_daemon_client.workspace_register(root)
            instance_id = reg["workspace_instance_id"]
            with pytest.raises(DaemonRemoteError) as exc:
                real_daemon_client.call(
                    "snapshot.stats", {"workspace_instance_id": instance_id}
                )
            assert exc.value.code == "snapshot_not_ready"
        finally:
            shutil.rmtree(root, ignore_errors=True)

    @requires_binaries
    def test_snapshot_stats_full_roundtrip(self, real_daemon_client, tmp_path):
        """d) 便捷方法 snapshot_stats(db_path) 全链路命中（register→publish→
        stats 注入权威 instance_id）；e) list_workspaces 可见；f) evict 驱逐 +
        幂等。"""
        root = tempfile.mkdtemp(prefix="cw_w13_ws_")
        try:
            # 先 configure 到临时 root（对齐便捷方法内部 _ensure_remote_snapshot
            # 的注册目标：默认进程 cwd，不切会导致 publish 的 workspace_id 过滤
            # 与 minimal_db 行不匹配），再经 workspace_status 完成首次 register
            # 并取 daemon registry 当前数值 workspace_id（ROWID）。
            #
            # 注意（w1_3_rowid_rotation.py 实证）：daemon_workspaces.workspace_id
            # 是 INTEGER PRIMARY KEY AUTOINCREMENT（ROWID），register_workspace
            # 用 INSERT OR REPLACE，每次重复 register ROWID 递增；发布时
            # handle_snapshot_publish 从 registry 取"当前" ROWID 做 GraphStore
            # SQL 过滤。因此必须先让 register 完成（此处置位
            # _remote_workspace_id），再取 ROWID 建 minimal_db，最后
            # snapshot_stats(db_path) 不再 register，publish 的 ROWID 与
            # minimal_db 行一致（此前直接取 workspace_register 返回的 ROWID
            # 会因便捷方法内部二次 register 轮转而失配 → 空快照）。
            real_daemon_client.configure_workspace(root)
            st = real_daemon_client.workspace_status()
            instance_id = st["workspace_instance_id"]
            ws_id_num = int(st["workspace_id"])
            db_path = _make_minimal_db(str(tmp_path / "w13_min.db"), ws_id_num)

            # 便捷方法内部 publish + 注入 instance_id → 命中
            stats = real_daemon_client.snapshot_stats(db_path=db_path)
            assert stats["workspace_instance_id"] == instance_id
            # symbol_count=3：GraphStore by_id 数组含 id=0 占位符，最小库 2 个
            # 符号实际返回 3（w1_3_repro_graphstore.py 本地实测；empty-list 时
            # file_count=1 为伪命中，不看 file_count）
            assert stats["symbol_count"] == 3, f"最小库应 3（2 符号+占位符），实际 {stats}"
            assert stats["generation"] >= 1

            # e) list_workspaces 可见刚发布的 workspace（peer UID 过滤）
            entries = real_daemon_client.snapshot_list_workspaces()
            ids = [e.get("workspace_instance_id") for e in entries
                   if isinstance(e, dict)]
            assert instance_id in ids, f"list_workspaces 应含刚发布 workspace，实际 {ids}"

            # f) evict → evicted=true；重复 evict → evicted=false（幂等）
            ev1 = real_daemon_client.snapshot_evict(instance_id)
            assert ev1 == {"evicted": True, "workspace_instance_id": instance_id}
            ev2 = real_daemon_client.snapshot_evict(instance_id)
            assert ev2["evicted"] is False, "重复驱逐应幂等返回 evicted=false"

            # 驱逐后 stats → snapshot_not_ready（cache 已清）
            with pytest.raises(DaemonRemoteError) as exc:
                real_daemon_client.call(
                    "snapshot.stats", {"workspace_instance_id": instance_id}
                )
            assert exc.value.code == "snapshot_not_ready"
        finally:
            shutil.rmtree(root, ignore_errors=True)
