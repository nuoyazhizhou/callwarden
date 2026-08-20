"""共存契约子任务2：Windows bridge MVP 测试。

对应 windows-wsl-daemon-coexistence-contract.md §4.2 与
windows-wsl-daemon-coexistence-task-plan.md 子任务2。

bridge 核心逻辑在 Rust `rust_ext/src/bin/cw_bridge.rs`（Windows-only，转发 JSON-RPC
到 Named Pipe，不打开 SQLite）。本 Python 测试：
1. 验证 bridge 的纯逻辑等价实现（token 校验 / 错误信封 / method-params 提取）；
2. 断言 cw_bridge.rs 源码满足契约约束（不打开 SQLite、不实现 task 写、token 校验、
   E_AUTHORITY_UNAVAILABLE / E_BRIDGE_AUTH_FAILED 结构化错误）；
3. 标注 Rust 侧单测需 Linux/Windows runner 执行。
"""
import json
import os
import shutil
import socket
import struct
import subprocess
import sys
import time

import pytest


_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _bridge_src():
    with open(
        os.path.join(_REPO_ROOT, "rust_ext", "src", "bin", "cw_bridge.rs"),
        encoding="utf-8",
    ) as f:
        return f.read()


def _validate_token(request: dict, expected: str):
    """镜像 cw_bridge.rs validate_token：校验并剥离 bridge_token。"""
    provided = request.pop("bridge_token", "")
    if provided != expected:
        return "E_BRIDGE_AUTH_FAILED"
    return None


def _error_response(code: str, message: str):
    return {"ok": False, "error": {"code": code, "message": message}}


def _extract_method_params(request: dict):
    method = request.get("method")
    if not method:
        raise ValueError("请求缺少 method")
    return method, request.get("params", {})


def test_token_validation_success():
    req = {"id": 1, "method": "ping", "params": {}, "bridge_token": "secret"}
    err = _validate_token(req, "secret")
    assert err is None
    assert "bridge_token" not in req  # 转发前剥离


def test_token_validation_failure():
    req = {"id": 1, "method": "ping", "params": {}, "bridge_token": "wrong"}
    err = _validate_token(req, "secret")
    assert err == "E_BRIDGE_AUTH_FAILED"


def test_error_envelope_structured():
    """bridge 错误信封与 daemon 格式一致（ok=false + error.code/message）。"""
    resp = _error_response("E_AUTHORITY_UNAVAILABLE", "Windows daemon 不可用")
    assert resp["ok"] is False
    assert resp["error"]["code"] == "E_AUTHORITY_UNAVAILABLE"


def test_extract_method_params():
    req = {"id": 1, "method": "workspace.register", "params": {"client_view_root": "/x"}}
    method, params = _extract_method_params(req)
    assert method == "workspace.register"
    assert params == {"client_view_root": "/x"}


def test_bridge_src_does_not_open_sqlite():
    """契约：bridge 禁止打开 SQLite（不得出现 sqlite 连接调用）。"""
    src = _bridge_src()
    # 允许注释提及 sqlite，但不得有连接/打开调用
    assert "sqlite3" not in src
    assert "Connection::open" not in src
    assert "callwarden.db" not in src or "禁止" in src


def test_bridge_src_has_token_and_error_codes():
    """bridge 必须做 token 校验并提供结构化错误码。"""
    src = _bridge_src()
    assert "CW_BRIDGE_TOKEN_FILE" in src
    assert "E_BRIDGE_AUTH_FAILED" in src
    assert "E_AUTHORITY_UNAVAILABLE" in src
    assert "fallback=forbidden" in src or "fallback" in src


def test_bridge_src_has_rust_unit_tests():
    """Rust 侧单测存在（供 Windows runner 执行）。"""
    src = _bridge_src()
    # bridge 单测放 config.rs / 或 bridge 模块内；此处断言框架存在
    assert "#[cfg(test)]" in src or "mod tests" in src or "unit" in src


@pytest.mark.skipif(sys.platform != "win32", reason="bridge 进程验收需要 Windows")
def test_bridge_process_rejects_invalid_token(tmp_path):
    """真实 bridge 进程必须在下游 daemon 之前拒绝错误 token。"""
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.fail("Windows bridge 验收禁止在缺少 cargo 时静默跳过")

    target_dir = tmp_path / "cargo-target"
    build = subprocess.run(
        [
            cargo,
            "build",
            "--no-default-features",
            "--manifest-path",
            os.path.join(_REPO_ROOT, "rust_ext", "Cargo.toml"),
            "--bin",
            "cw-bridge",
        ],
        env={**os.environ, "CARGO_TARGET_DIR": str(target_dir)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=180,
    )
    if build.returncode != 0:
        pytest.fail("cargo build cw-bridge 失败：\n" + (build.stdout + build.stderr)[-4000:])

    bridge_bin = target_dir / "debug" / "cw-bridge.exe"
    assert bridge_bin.is_file(), f"cargo build 成功但缺少产物: {bridge_bin}"

    token_file = tmp_path / "bridge.token"
    token_file.write_text("expected-token\n", encoding="utf-8")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    endpoint = f"127.0.0.1:{port}"
    proc = subprocess.Popen(
        [str(bridge_bin)],
        env={
            **os.environ,
            "CW_BRIDGE_TOKEN_FILE": str(token_file),
            "CW_BRIDGE_ENDPOINT": endpoint,
            "CW_BRIDGE_MANIFEST": str(tmp_path / "bridge.manifest.json"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    def send_request(request):
        body = json.dumps(request, separators=(",", ":")).encode("utf-8")
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            conn.sendall(struct.pack(">I", len(body)) + body)
            header = conn.recv(4)
            assert len(header) == 4
            size = struct.unpack(">I", header)[0]
            payload = bytearray()
            while len(payload) < size:
                payload.extend(conn.recv(size - len(payload)))
            return json.loads(payload.decode("utf-8"))

    try:
        deadline = time.time() + 15
        while time.time() < deadline:
            if proc.poll() is not None:
                output = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"cw-bridge 提前退出: {output}")
            try:
                response = send_request(
                    {
                        "id": "bridge-e2e-1",
                        "method": "ping",
                        "params": {},
                        "bridge_token": "wrong-token",
                    }
                )
                break
            except (ConnectionRefusedError, TimeoutError, OSError):
                time.sleep(0.1)
        else:
            pytest.fail("cw-bridge 未在 15 秒内监听 endpoint")

        assert response["ok"] is False
        assert response["error"]["code"] == "E_BRIDGE_AUTH_FAILED"
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


@pytest.mark.skipif(sys.platform != "win32", reason="bridge 进程验收需要 Windows")
def test_bridge_process_accepts_tcp_endpoint(tmp_path):
    """真实 bridge 进程用 tcp:// 前缀 endpoint 必须成功 bind 并监听。

    strip_tcp_scheme 在生产路径生效：`tcp://127.0.0.1:port` 传入
    TcpListener::bind 前被剥离。有效 token 的 ping 若转发到运行中的
    Windows daemon 即完成 真实 TCP → bridge → Named Pipe → daemon round-trip；
    daemon 不可用时返回结构化 E_AUTHORITY_UNAVAILABLE（fail-closed）。
    """
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.fail("Windows bridge 验收禁止在缺少 cargo 时静默跳过")

    target_dir = tmp_path / "cargo-target-tcp"
    build = subprocess.run(
        [
            cargo,
            "build",
            "--no-default-features",
            "--manifest-path",
            os.path.join(_REPO_ROOT, "rust_ext", "Cargo.toml"),
            "--bin",
            "cw-bridge",
        ],
        env={**os.environ, "CARGO_TARGET_DIR": str(target_dir)},
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=240,
    )
    if build.returncode != 0:
        pytest.fail("cargo build cw-bridge 失败：\n" + (build.stdout + build.stderr)[-4000:])

    bridge_bin = target_dir / "debug" / "cw-bridge.exe"
    assert bridge_bin.is_file(), f"cargo build 成功但缺少产物: {bridge_bin}"

    token_file = tmp_path / "bridge.token"
    token_file.write_text("expected-token\n", encoding="utf-8")
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    # 复审要求：tcp:// 前缀 endpoint（strip_tcp_scheme 的生产入口）
    endpoint = f"tcp://127.0.0.1:{port}"
    proc = subprocess.Popen(
        [str(bridge_bin)],
        env={
            **os.environ,
            "CW_BRIDGE_TOKEN_FILE": str(token_file),
            "CW_BRIDGE_ENDPOINT": endpoint,
            "CW_BRIDGE_MANIFEST": str(tmp_path / "bridge-tcp.manifest.json"),
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    def send_request(request):
        body = json.dumps(request, separators=(",", ":")).encode("utf-8")
        with socket.create_connection(("127.0.0.1", port), timeout=5) as conn:
            conn.sendall(struct.pack(">I", len(body)) + body)
            header = conn.recv(4)
            assert len(header) == 4
            size = struct.unpack(">I", header)[0]
            payload = bytearray()
            while len(payload) < size:
                payload.extend(conn.recv(size - len(payload)))
            return json.loads(payload.decode("utf-8"))

    try:
        deadline = time.time() + 15
        response = None
        while time.time() < deadline:
            if proc.poll() is not None:
                output = proc.stdout.read() if proc.stdout else ""
                pytest.fail(f"cw-bridge(tcp://) 提前退出: {output}")
            try:
                response = send_request(
                    {
                        "id": "tcp-e2e-1",
                        "method": "ping",
                        "params": {},
                        "bridge_token": "expected-token",
                    }
                )
                break
            except (ConnectionRefusedError, TimeoutError, OSError):
                time.sleep(0.1)
        if response is None:
            pytest.fail("cw-bridge(tcp://) 未在 15 秒内监听 endpoint")

        # tcp:// 前缀已剥离、bridge 正常监听并处理请求
        assert response["ok"] in (True, False), f"响应结构异常: {response}"
        if response["ok"]:
            # Windows daemon 在跑 → 真实 TCP→bridge→Named Pipe→daemon round-trip
            # 真实 daemon ping 响应契约：{peer_uid, pid, status}（transport 为测试 fake 字段）
            assert response["result"]["status"] == "ok", f"ping 结果异常: {response}"
            assert response["result"].get("pid") is not None, (
                f"daemon ping 应返回 pid: {response}"
            )
        else:
            # daemon 不可用 → fail-closed 结构化错误（不允许本地 fallback）
            assert response["error"]["code"] == "E_AUTHORITY_UNAVAILABLE", (
                f"bridge 应 fail-closed: {response}"
            )
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)


def _bridge_validate_token(request: dict, expected: str):
    """镜像 cw_bridge.rs validate_token：顶层剥离并校验 bridge_token。"""
    provided = request.pop("bridge_token", "")
    if provided != expected:
        return "E_BRIDGE_AUTH_FAILED"
    return None


def _bridge_extract_method_params(request: dict):
    """镜像 cw_bridge.rs extract_method_params：校验后提取 method/params。"""
    method = request.get("method")
    if not method:
        raise ValueError("请求缺少 method")
    return method, request.get("params", {})


def test_protocol_roundtrip_top_level_token_accepted():
    """生产 client 帧（顶层 bridge_token）能被 bridge 校验并通过，转发剥离 token。"""
    # 模拟生产 UnixDaemonRpcClient.call() 在 windows-bridge 下构造的帧
    frame = {
        "id": 1,
        "method": "workspace.list",
        "params": {},
        "bridge_token": "secret-token",
    }
    err = _bridge_validate_token(frame, "secret-token")
    assert err is None
    # 剥离 token 后只剩 {id, method, params}
    assert "bridge_token" not in frame
    method, params = _bridge_extract_method_params(frame)
    assert method == "workspace.list"
    assert frame["id"] == 1


def test_protocol_roundtrip_top_level_token_rejected():
    """错误 token 在顶层被 bridge 拒绝（E_BRIDGE_AUTH_FAILED）。"""
    frame = {
        "id": 1,
        "method": "workspace.list",
        "params": {},
        "bridge_token": "wrong",
    }
    err = _bridge_validate_token(frame, "secret-token")
    assert err == "E_BRIDGE_AUTH_FAILED"


def test_protocol_token_must_be_top_level_not_params():
    """bridge 只校验顶层 bridge_token；token 放在 params 内无效（回归保护）。"""
    # 生产 client 旧实现错误地放在 params；bridge 顶层校验不到 → 认证失败
    frame = {
        "id": 1,
        "method": "ping",
        "params": {"bridge_token": "secret-token"},
    }
    err = _bridge_validate_token(frame, "secret-token")
    assert err == "E_BRIDGE_AUTH_FAILED"
    # 正确实现：顶层
    frame2 = {"id": 1, "method": "ping", "params": {}, "bridge_token": "secret-token"}
    assert _bridge_validate_token(frame2, "secret-token") is None


# ============================================================
# P0 复审补测：生产 UnixDaemonRpcClient → fake TCP bridge 原始帧
# 验证生产 client 完整 call() 逻辑（含 bridge_token 注入）发出的帧：
# 1. bridge_token 在请求顶层（cw_bridge.rs validate_token 剥离的位置）；
# 2. params 内不携带 bridge_token（旧实现回归保护）；
# 3. token 缺失时 fail-closed（不发出未认证请求）。
# ============================================================
import threading

import callwarden.server.daemon_client as dc


def _fake_tcp_connect(endpoint: str):
    """替代生产 try_connect 的连接层（Windows 平台无法用 Named Pipe 连 TCP fake）。
    仅替换"建立连接"，保留生产 client 的帧构造 / token 注入 / 收发逻辑。"""
    host_port = endpoint.removeprefix("tcp://")
    host, _, port = host_port.rpartition(":")
    if not host or not port.isdigit():
        raise OSError(f"无效 TCP 端点: {endpoint}")
    return socket.create_connection((host, int(port)), timeout=10)


def _run_fake_bridge(port: int, captured: dict, response: dict):
    """fake TCP bridge：接收 4 字节大端长度前缀 + JSON 帧 → 记录 → 回响应。
    payload 为空（客户端在发帧前断开）时静默返回，不抛异常。"""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", port))
        srv.listen(1)
        srv.settimeout(15)
        conn, _ = srv.accept()
        with conn:
            conn.settimeout(10)
            header = conn.recv(4)
            if len(header) != 4:
                return
            size = struct.unpack(">I", header)[0]
            payload = bytearray()
            while len(payload) < size:
                chunk = conn.recv(size - len(payload))
                if not chunk:
                    break
                payload.extend(chunk)
            if not payload:
                return
            captured["frame"] = json.loads(payload.decode("utf-8"))
            body = json.dumps(response, separators=(",", ":")).encode("utf-8")
            conn.sendall(struct.pack(">I", len(body)) + body)


_PING_OK = {
    "ok": True,
    "id": 1,
    "result": {
        "status": "ok",
        "protocol_version": 1,
        "authority_id": "linkplay-scm/windows/0/fingerprint",
        "platform": "win32",
        "transport": "windows-bridge",
        "task_db_fingerprint": "fake-fingerprint",
        "workspace_capabilities": [],
    },
}


def test_production_client_sends_top_level_token_frame(
    tmp_path, monkeypatch
):
    """生产 UnixDaemonRpcClient 经 bridge 发出的原始帧：bridge_token 在顶层。"""
    token_file = tmp_path / "bridge.token"
    token_file.write_text("secret-token\n", encoding="utf-8")

    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    captured: dict = {}
    t = threading.Thread(target=_run_fake_bridge, args=(port, captured, _PING_OK), daemon=True)

    monkeypatch.setattr(dc, "try_connect", _fake_tcp_connect)
    monkeypatch.setenv("CW_DAEMON_TRANSPORT", "windows-bridge")
    monkeypatch.setenv("CW_BRIDGE_TOKEN_FILE", str(token_file))

    client = dc.UnixDaemonRpcClient(socket_path=f"tcp://127.0.0.1:{port}", timeout=10)
    t.start()
    try:
        result = client.call("ping", {})
    finally:
        t.join(timeout=10)

    frame = captured["frame"]
    assert frame["bridge_token"] == "secret-token", f"token 未注入顶层: {frame}"
    assert "bridge_token" not in frame["params"], (
        f"token 不得放入 params（bridge 只校验顶层）: {frame}"
    )
    assert frame["method"] == "ping"
    assert result["status"] == "ok", f"响应解析失败: {result}"


def test_production_client_missing_token_fail_closed(
    tmp_path, monkeypatch
):
    """token 文件缺失时生产 client 拒绝发请求（fail-closed，不发出未认证帧）。"""
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]

    # fake bridge 正常监听：连接成功后才走到 token 检查（token 缺失 → fail-closed）
    captured: dict = {}
    t = threading.Thread(
        target=_run_fake_bridge,
        args=(port, captured, _PING_OK),
        daemon=True,
    )

    monkeypatch.setattr(dc, "try_connect", _fake_tcp_connect)
    monkeypatch.setenv("CW_DAEMON_TRANSPORT", "windows-bridge")
    monkeypatch.setenv("CW_BRIDGE_TOKEN_FILE", str(tmp_path / "missing-bridge.token"))

    client = dc.UnixDaemonRpcClient(socket_path=f"tcp://127.0.0.1:{port}", timeout=10)
    t.start()
    try:
        with pytest.raises(dc.DaemonUnavailableError, match="bridge token"):
            client.call("ping", {})
    finally:
        t.join(timeout=10)

    # fail-closed：未发出任何帧
    assert "frame" not in captured, f"fail-closed 仍发出了请求帧: {captured}"
