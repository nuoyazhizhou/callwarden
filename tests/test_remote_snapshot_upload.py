r"""S2（ADR-002）snapshot.upload 客户端半侧单测（S2-03/04/06/07）。

契约：`docs/design/mtls-remote-plane.md` §16.2/§16.3（已冻结）；
验收行：`docs/design/cli-mcp-matrix-backlog.md` §38.2。

daemon 半侧（upload slots、sha256/raw_size 校验、db_path 拒绝、bind_scope
注入）由 `rust_ext/src/daemon/snapshot_state.rs` 的 `s2_upload_tests`
（cargo test）覆盖；本文件覆盖客户端半侧：

- S2-03 build_graph 客户端本机构建 → snapshot.upload 发布：上传字节为
  合法 SQLite（magic header）、落 tmp 后可查询 file_instances/symbols/calls
  三张权威表、结果含 ok 与 exec_origin="client"。
- S2-04 remote profile 下 db_path 拒绝（客户端半）：`_ensure_remote_snapshot`
  remote 分支以 `client_view_root="."` 注册（daemon canonicalize 到其 cwd），
  不携带 db_path、不发 snapshot.publish——即使调用方传入 db_path；
  loopback 分支不受影响（回归守护）。
- S2-06 上传校验失败原样传播：daemon 返回 E_SNAPSHOT_UPLOAD_HASH_MISMATCH
  时 upload_snapshot / build_graph 工具层面均抛 DaemonRemoteError，不静默
  成功、不重试、不吞码。
- S2-07 分块契约：inline 形态（mode/content_encoding/payload_b64 解出
  gzip 流、sha256+raw_size 双校验、max_body_bytes=64MiB 口径）；chunk 形态
  （total_chunks≥2、upload_id 一致、逐块 chunk_sha256、b64 重组==gzip 流、
  commit 携带 hash 契约）；SNAPSHOT_UPLOAD_MAX_CHUNKS 超限 →
  E_PAYLOAD_TOO_LARGE 且**不发送任何 chunk RPC**（无截断入库）。

桩边界：方法级 upload_snapshot 测试用 `object.__new__` 构造
HttpDaemonRpcClient 并钉死 `call`（避免 endpoint 发现）；工具级 build_graph
测试 monkeypatch `tools_workspace._remote_client`（同
test_remote_read_plane.py 的 harness 风格）。
"""

import base64
import gzip
import hashlib
import os
import sqlite3

import pytest

from callwarden.server.daemon_protocol import DaemonRemoteError


# ----------------------------------------------------------------------
# S2-03：build_graph 客户端本机构建 → snapshot.upload 发布
# ----------------------------------------------------------------------

class _UploadStub:
    """remote profile 桩 client：记录 workspace 注册与上传，禁止普通 RPC。"""

    def __init__(self, root, ws_id="inst-s2-up-1"):
        self._project_root = root
        self._remote_workspace_id = ws_id
        self.calls = []        # 普通 RPC（build_graph 远端路径不应触发）
        self.registers = []
        self.uploads = []
        self.upload_error = None

    def is_remote_profile(self):
        return True

    def bind_scope(self):
        return "remote"

    def call(self, method, params=None, **kwargs):
        self.calls.append((method, params or {}))
        raise AssertionError(
            f"远端只读面 build_graph 不应发起 {method} RPC（S2-03：客户端本机构建）"
        )

    def ensure_remote_workspace(self):
        self.registers.append(True)
        return self._remote_workspace_id

    def upload_snapshot(self, workspace_instance_id, raw_bytes, **kwargs):
        self.uploads.append((workspace_instance_id, raw_bytes, kwargs))
        if self.upload_error is not None:
            raise self.upload_error
        return {"ok": True, "snapshot_id": "snap-s2-up-1"}


class _CaptureMCP:
    def __init__(self):
        self._tools = {}

    def tool(self):
        def decorator(fn):
            self._tools[fn.__name__] = fn
            return fn
        return decorator


@pytest.fixture
def remote_build_tools(tmp_path, monkeypatch):
    """构造 remote profile 小工作树 + 上传桩，返回 (stub, tools, root)。"""
    from callwarden.server.tools import tools_workspace

    (tmp_path / "alpha.py").write_text(
        "def alpha():\n    return helper(1)\n", encoding="utf-8"
    )
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "beta.py").write_text(
        "def beta(a, b):\n    return a + b\n", encoding="utf-8"
    )

    stub = _UploadStub(str(tmp_path))
    monkeypatch.setattr(tools_workspace, "_remote_client", lambda: stub)
    mcp = _CaptureMCP()
    tools_workspace.register(mcp)
    yield stub, mcp._tools, tmp_path


class TestS203BuildGraphUpload:
    """S2-03：客户端本机构建 codegraph DB，产物经 snapshot.upload 发布。"""

    def test_s2_03_uploads_valid_sqlite_with_stats(self, remote_build_tools):
        stub, tools, root = remote_build_tools

        result = tools["build_graph"]()

        # §16.4 返回形态：daemon handler 统计口径（11 键）+ 上传结果 + exec_origin
        assert result["exec_origin"] == "client"
        assert result["ok"] is True
        for key in (
            "scanned", "inserted", "unchanged", "skipped_cached", "deleted",
            "archived", "skipped_unparsed", "symbols", "calls",
            "calls_resolved", "calls_ambiguous",
        ):
            assert key in result, f"build_graph 缺少统计键 {key}"
        assert result["scanned"] == 2               # 两个可索引 .py
        assert result["inserted"] == 2
        assert result["symbols"] >= 2               # alpha + beta 至少两符号

        # 上传链路：恰好一次 workspace 注册 + 一次 snapshot.upload
        assert stub.registers == [True]
        assert stub.calls == []                     # 零普通 RPC
        assert len(stub.uploads) == 1
        ws_id, raw, kwargs = stub.uploads[0]
        assert ws_id == "inst-s2-up-1"

        # 上传字节为合法 SQLite（daemon 侧将按 sha256/raw_size 校验并导入）
        assert raw[:16] == b"SQLite format 3\x00"
        db_file = root / "_s2_uploaded_probe.db"
        db_file.write_bytes(raw)
        conn = sqlite3.connect(db_file)
        try:
            counts = dict(conn.execute(
                "SELECT 'files', COUNT(*) FROM file_instances "
                "UNION ALL SELECT 'symbols', COUNT(*) FROM symbols "
                "UNION ALL SELECT 'calls', COUNT(*) FROM calls"
            ).fetchall())
        finally:
            conn.close()
        db_file.unlink()
        assert counts["files"] == 2
        assert counts["symbols"] >= 2
        # alpha() 内有 helper(1) 调用 → calls 表应有记录
        assert counts["calls"] >= 1


# ----------------------------------------------------------------------
# S2-04：remote profile 下 db_path 拒绝（客户端半）
# ----------------------------------------------------------------------

def _bare_client(monkeypatch, bind_scope_value, root):
    """object.__new__ 构造 HttpDaemonRpcClient（绕开 endpoint 发现），
    钉死 bind_scope 与 call，返回 (client, calls 记录列表)。"""
    from callwarden.server import daemon_client

    client = object.__new__(daemon_client.HttpDaemonRpcClient)
    client._remote_workspace_id = None
    client._remote_snapshot_id = None
    client._remote_snapshot_ready = False
    client._project_root = str(root)
    monkeypatch.setattr(client, "bind_scope", lambda: bind_scope_value)

    calls = []

    def _call(method, params=None, **kwargs):
        calls.append((method, params or {}, kwargs))
        if method == "workspace.register":
            return {"workspace_instance_id": "inst-s2-04-1"}
        return {"ok": True, "snapshot_id": "snap-s2-04-1"}

    monkeypatch.setattr(client, "call", _call)
    return client, calls


class TestS204RemoteDbPathForbidden:
    """S2-04：remote profile 快照入口唯一为 snapshot.upload，
    register 不带 db_path、不发 snapshot.publish（daemon 侧
    E_REMOTE_DB_PATH_FORBIDDEN 由 Rust s2_upload_tests 覆盖）。"""

    def test_s2_04_remote_register_uses_dot_without_publish(self, tmp_path, monkeypatch):
        client, calls = _bare_client(monkeypatch, "remote", tmp_path)

        # 即使调用方传入 db_path，remote 分支也不发布、不携带路径字段
        ws_id = client._ensure_remote_snapshot(str(tmp_path / "probe.db"))

        assert ws_id == "inst-s2-04-1"
        assert len(calls) == 1, f"remote 分支应只注册不发布，实际 {calls}"
        method, params, _kwargs = calls[0]
        assert method == "workspace.register"
        assert params["client_view_root"] == "."
        # 路径拒绝表口径：register 参数不得出现任何 db/绝对路径字段
        for key, value in params.items():
            assert key in (
                "client_view_root", "git_remote_url", "git_head_commit_sha"
            ), f"register 携带非契约字段 {key}"
            if key != "client_view_root":
                assert not os.path.isabs(str(value)) or key == "client_view_root"

    def test_s2_04_ensure_remote_workspace_returns_id(self, tmp_path, monkeypatch):
        client, calls = _bare_client(monkeypatch, "remote", tmp_path)

        assert client.ensure_remote_workspace() == "inst-s2-04-1"
        assert [c[0] for c in calls] == ["workspace.register"]

    def test_s2_04_loopback_still_publishes_db_path(self, tmp_path, monkeypatch):
        """回归守护：db_path 拒绝是 remote profile 特有口径，
        loopback 分支仍走 snapshot.publish（非误伤）。"""
        client, calls = _bare_client(monkeypatch, "loopback", tmp_path)

        db_path = tmp_path / "probe.db"
        db_path.write_bytes(b"SQLite format 3\x00")
        client._ensure_remote_snapshot(str(db_path))

        methods = [c[0] for c in calls]
        assert methods == ["workspace.register", "snapshot.publish"]
        register_params = calls[0][1]
        assert register_params["client_view_root"] == str(tmp_path)
        publish_params = calls[1][1]
        assert publish_params["db_path"] == os.path.abspath(str(db_path))


# ----------------------------------------------------------------------
# S2-06：上传校验失败原样传播（不静默成功）
# ----------------------------------------------------------------------

class TestS206HashMismatchPropagation:
    """S2-06：daemon 侧 sha256/raw_size 校验失败（E_SNAPSHOT_UPLOAD_HASH_MISMATCH）
    时客户端必须原样抛出，不得吞码或伪 ok。"""

    def test_s2_06_inline_error_propagates(self, tmp_path, monkeypatch):
        from callwarden.server import daemon_client

        client = object.__new__(daemon_client.HttpDaemonRpcClient)
        calls = []

        def _call(method, params=None, **kwargs):
            calls.append((method, params or {}, kwargs))
            raise DaemonRemoteError(
                "E_SNAPSHOT_UPLOAD_HASH_MISMATCH",
                "sha256 校验失败：payload 与声明 hash 不符",
            )

        monkeypatch.setattr(client, "call", _call)

        with pytest.raises(DaemonRemoteError) as exc:
            client.upload_snapshot("inst-s2-06", b"SQLite format 3\x00" + b"x" * 64)
        assert exc.value.code == "E_SNAPSHOT_UPLOAD_HASH_MISMATCH"
        # 失败即终止：不重试、不降级
        assert len(calls) == 1

    def test_s2_06_commit_error_propagates_no_silent_ok(self, tmp_path, monkeypatch):
        from callwarden.server import daemon_client

        client = object.__new__(daemon_client.HttpDaemonRpcClient)
        calls = []
        committed = {"done": False}

        def _call(method, params=None, **kwargs):
            calls.append((method, params or {}, kwargs))
            if method == "snapshot.upload.commit":
                committed["done"] = True
                raise DaemonRemoteError(
                    "E_SNAPSHOT_UPLOAD_HASH_MISMATCH", "commit 阶段 hash 不符"
                )
            return {"ok": True}

        monkeypatch.setattr(client, "call", _call)
        # 强制 chunk 路径：chunk 全部成功，commit 校验失败
        monkeypatch.setattr(
            daemon_client, "SNAPSHOT_UPLOAD_INLINE_MAX_BYTES", 1
        )
        monkeypatch.setattr(
            daemon_client, "SNAPSHOT_UPLOAD_CHUNK_MAX_BYTES", 16
        )

        with pytest.raises(DaemonRemoteError) as exc:
            client.upload_snapshot("inst-s2-06", b"SQLite format 3\x00" + b"y" * 200)
        assert exc.value.code == "E_SNAPSHOT_UPLOAD_HASH_MISMATCH"
        assert committed["done"] is True

    def test_s2_06_build_graph_propagates_upload_error(self, remote_build_tools):
        """工具层口径：上传校验失败时 build_graph 抛 DaemonRemoteError，
        不返回伪成功结果。"""
        stub, tools, _root = remote_build_tools
        stub.upload_error = DaemonRemoteError(
            "E_SNAPSHOT_UPLOAD_HASH_MISMATCH", "daemon 侧校验失败"
        )

        with pytest.raises(DaemonRemoteError) as exc:
            tools["build_graph"]()
        assert exc.value.code == "E_SNAPSHOT_UPLOAD_HASH_MISMATCH"
        # 校验失败不应触发二次上传
        assert len(stub.uploads) == 1


# ----------------------------------------------------------------------
# S2-07：分块契约（inline / chunk / 上限）
# ----------------------------------------------------------------------

def _recorder_client(monkeypatch):
    """HttpDaemonRpcClient + call 记录桩，返回 (client, calls)。"""
    from callwarden.server import daemon_client

    client = object.__new__(daemon_client.HttpDaemonRpcClient)
    calls = []

    def _call(method, params=None, **kwargs):
        calls.append((method, params or {}, kwargs))
        return {"ok": True, "snapshot_id": "snap-s2-07"}

    monkeypatch.setattr(client, "call", _call)
    return client, calls


class TestS207ChunkContract:
    """S2-07：§16.2 三方法协议客户端组装口径（用小常量避免真 64MiB 开销）。"""

    def test_s2_07_inline_form_contract(self, monkeypatch):
        from callwarden.server import daemon_client

        client, calls = _recorder_client(monkeypatch)
        raw = b"SQLite format 3\x00" + b"k" * 512

        result = client.upload_snapshot(
            "inst-s2-07", raw,
            build_context_hash="bc-hash-1", snapshot_id="snap-pre-1",
        )

        assert result["ok"] is True
        assert len(calls) == 1
        method, params, kwargs = calls[0]
        assert method == "snapshot.upload"
        assert params["workspace_instance_id"] == "inst-s2-07"
        assert params["mode"] == "inline"
        assert params["content_encoding"] == "gzip"
        # hash 契约针对 gunzip 后的原始字节（sha256 + raw_size 双校验）；
        # gzip 流含 mtime 头，daemon 侧只按解压后字节校验，不做流级比对
        gz = base64.b64decode(params["payload_b64"])
        assert gzip.decompress(gz) == raw
        assert params["sha256"] == hashlib.sha256(raw).hexdigest()
        assert params["raw_size"] == len(raw)
        assert params["build_context_hash"] == "bc-hash-1"
        assert params["snapshot_id"] == "snap-pre-1"
        # 上传面 body 口径：64MiB（覆盖通用 8MiB）
        assert kwargs["max_body_bytes"] == daemon_client.SNAPSHOT_UPLOAD_BODY_MAX_BYTES

    def test_s2_07_chunk_form_contract(self, monkeypatch):
        from callwarden.server import daemon_client

        client, calls = _recorder_client(monkeypatch)
        # 小chunk 上限强制 total_chunks ≥ 2（gzip 流本身 >16 字节）
        monkeypatch.setattr(
            daemon_client, "SNAPSHOT_UPLOAD_INLINE_MAX_BYTES", 1
        )
        monkeypatch.setattr(
            daemon_client, "SNAPSHOT_UPLOAD_CHUNK_MAX_BYTES", 16
        )
        raw = b"SQLite format 3\x00" + b"m" * 512

        result = client.upload_snapshot("inst-s2-07", raw)

        assert result["ok"] is True
        chunk_calls = [c for c in calls if c[0] == "snapshot.upload.chunk"]
        commit_calls = [c for c in calls if c[0] == "snapshot.upload.commit"]
        assert len(chunk_calls) >= 2
        assert len(commit_calls) == 1

        total = chunk_calls[0][1]["total_chunks"]
        assert total == len(chunk_calls)
        upload_ids = {c[1]["upload_id"] for c in chunk_calls}
        assert len(upload_ids) == 1
        upload_id = upload_ids.pop()
        assert commit_calls[0][1]["upload_id"] == upload_id

        # chunk_index 连续从 0 开始；逐块 chunk_sha256；b64 重组 == gzip 流
        chunks = {c[1]["chunk_index"]: c for c in chunk_calls}
        assert sorted(chunks) == list(range(total))
        gz_stream = b""
        for idx in range(total):
            params = chunks[idx][1]
            chunk = base64.b64decode(params["chunk_b64"])
            assert len(chunk) <= 16
            assert params["chunk_sha256"] == hashlib.sha256(chunk).hexdigest()
            gz_stream += chunk
        assert gzip.decompress(gz_stream) == raw

        # commit 携带原始字节 hash 契约
        commit_params = commit_calls[0][1]
        assert commit_params["sha256"] == hashlib.sha256(raw).hexdigest()
        assert commit_params["raw_size"] == len(raw)
        # 每一次上传 RPC 都走 64MiB body 口径
        for _m, _p, kwargs in calls:
            assert kwargs["max_body_bytes"] == (
                daemon_client.SNAPSHOT_UPLOAD_BODY_MAX_BYTES
            )

    def test_s2_07_max_chunks_exceeded_fails_without_sending(self, monkeypatch):
        """超组装上限 → E_PAYLOAD_TOO_LARGE 且不发送任何 chunk RPC
        （无截断入库、无部分写入）。"""
        from callwarden.server import daemon_client

        client, calls = _recorder_client(monkeypatch)
        monkeypatch.setattr(
            daemon_client, "SNAPSHOT_UPLOAD_INLINE_MAX_BYTES", 1
        )
        monkeypatch.setattr(
            daemon_client, "SNAPSHOT_UPLOAD_CHUNK_MAX_BYTES", 1
        )
        monkeypatch.setattr(
            daemon_client, "SNAPSHOT_UPLOAD_MAX_CHUNKS", 2
        )
        raw = b"SQLite format 3\x00" + b"n" * 512

        with pytest.raises(DaemonRemoteError) as exc:
            client.upload_snapshot("inst-s2-07", raw)
        assert exc.value.code == "E_PAYLOAD_TOO_LARGE"
        # 关键：上限检查先于任何 chunk 发送——daemon 侧无任何 slot 写入
        assert calls == []
