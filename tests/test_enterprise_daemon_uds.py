"""Enterprise daemon 最小纵向切片验收。"""

import json
import os
import socket
import sqlite3
import struct
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path

import pytest

from callwarden.server.daemon_client import (
    DaemonClient,
    DaemonUnavailableError,
    UnixDaemonRpcClient,
)
from callwarden.server.daemon_protocol import (
    DaemonRemoteError,
    ProtocolError,
    parse_response,
    recv_message,
    recv_message_with_fds,
    send_message,
    send_message_with_fds,
)
from callwarden.server.daemon_server import (
    DaemonRpcError,
    EnterpriseDaemonServer,
    EnterpriseDaemonService,
)
from callwarden.server.snapshot_manager import SnapshotManagerService


callwarden_core = pytest.importorskip("callwarden_core")


def test_framed_json_roundtrip_over_stream_socket():
    left, right = socket.socketpair()
    try:
        request = {"id": 17, "method": "ping", "params": {"text": "中文"}}
        send_message(left, request, max_bytes=1024)
        assert recv_message(right, max_bytes=1024) == request
    finally:
        left.close()
        right.close()


def test_protocol_rejects_oversized_frame_before_payload_read():
    left, right = socket.socketpair()
    try:
        left.sendall(struct.pack("!I", 2048))
        with pytest.raises(ProtocolError, match="非法消息长度"):
            recv_message(right, max_bytes=1024)
    finally:
        left.close()
        right.close()


def test_protocol_rejects_malformed_json():
    left, right = socket.socketpair()
    try:
        payload = b"{not-json}"
        left.sendall(struct.pack("!I", len(payload)) + payload)
        with pytest.raises(ProtocolError, match="JSON 解码失败"):
            recv_message(right, max_bytes=1024)
    finally:
        left.close()
        right.close()


def test_structured_remote_error_preserves_code():
    with pytest.raises(DaemonRemoteError) as captured:
        parse_response({
            "id": 9,
            "ok": False,
            "error": {"code": "workspace_forbidden", "message": "denied"},
        })
    assert captured.value.code == "workspace_forbidden"


def test_enterprise_mode_never_silently_falls_back(monkeypatch, tmp_path):
    missing_socket = str(tmp_path / "missing.sock")
    monkeypatch.setattr(
        "callwarden.server.daemon_client.get_daemon_mode", lambda: "enterprise"
    )
    client = DaemonClient(socket_path=missing_socket)
    with pytest.raises(DaemonUnavailableError, match="enterprise 模式要求 daemon"):
        client.get_stats(db_path=str(tmp_path / "callwarden.db"))
    assert client.sql_fallbacks == 0


def test_auto_mode_falls_back_when_socket_is_absent(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "callwarden.server.daemon_client.get_daemon_mode", lambda: "auto"
    )
    client = DaemonClient(socket_path=str(tmp_path / "missing.sock"))
    monkeypatch.setattr(
        client, "_sql_fallback_get_stats", lambda: {"source": "sql"}
    )
    assert client.get_stats(db_path=None) == {"source": "sql"}
    assert client.sql_fallbacks == 1


@pytest.mark.skipif(
    not hasattr(socket.socket, "sendmsg") or not hasattr(socket, "SCM_RIGHTS"),
    reason="当前平台不支持 SCM_RIGHTS",
)
def test_scm_rights_transfers_read_only_fd(tmp_path):
    source = tmp_path / "snapshot.db"
    source.write_bytes(b"sqlite-snapshot")
    fd = os.open(source, os.O_RDONLY)
    left, right = socket.socketpair()
    received = []
    try:
        send_message_with_fds(
            left, {"id": 3, "method": "snapshot.publish"}, [fd])
        message, received = recv_message_with_fds(right)
        assert message["id"] == 3
        assert len(received) == 1
        assert os.read(received[0], 6) == b"sqlite"
    finally:
        os.close(fd)
        for received_fd in received:
            os.close(received_fd)
        left.close()
        right.close()


def _create_graph_db(db_path: Path) -> None:
    conn = sqlite3.connect(str(db_path))
    conn.executescript("""
        CREATE TABLE file_instances (
            id INTEGER PRIMARY KEY,
            rel_path TEXT,
            status TEXT DEFAULT 'active',
            workspace_id INTEGER DEFAULT 1
        );
        INSERT INTO file_instances VALUES (1, 'src/main.py', 'active', 1);
        CREATE TABLE symbols (
            id INTEGER PRIMARY KEY,
            file_instance_id INTEGER,
            kind TEXT,
            name TEXT,
            qualified_name TEXT,
            module_path TEXT,
            start_line INTEGER,
            end_line INTEGER,
            depth INTEGER
        );
        INSERT INTO symbols VALUES
            (1, 1, 'fn', 'main', 'app.main', 'app', 1, 8, 0),
            (2, 1, 'fn', 'helper', 'app.helper', 'app', 10, 14, 0);
        CREATE TABLE calls (
            caller_id INTEGER,
            callee_id INTEGER,
            callee_name TEXT,
            call_line INTEGER,
            is_cross_file INTEGER
        );
        INSERT INTO calls VALUES (1, 2, 'helper', 4, 0);
    """)
    conn.commit()
    conn.close()


def _short_socket_path() -> str:
    return os.path.join(
        tempfile.gettempdir(), f"cw-{os.getpid()}-{uuid.uuid4().hex[:8]}.sock"
    )


@pytest.fixture
def daemon_service(tmp_path):
    snapshot_service = SnapshotManagerService(max_workspaces=8)
    return EnterpriseDaemonService(
        registry_db=str(tmp_path / "registry.db"),
        snapshot_service=snapshot_service,
    )


@pytest.fixture
def running_daemon(daemon_service):
    if not hasattr(socket, "AF_UNIX"):
        pytest.skip("当前 Python 平台不支持 Unix domain socket")
    socket_path = _short_socket_path()
    service = daemon_service
    server = EnterpriseDaemonServer(socket_path, service, socket_mode=0o666)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    assert server.ready.wait(timeout=5), "daemon UDS 未在 5 秒内就绪"
    try:
        yield UnixDaemonRpcClient(socket_path, timeout=5), service
    finally:
        server.shutdown()
        thread.join(timeout=5)
        assert not thread.is_alive(), "daemon 未在 5 秒内停止"


def test_real_uds_register_publish_and_query(running_daemon, tmp_path):
    client, _service = running_daemon
    db_path = tmp_path / "callwarden.db"
    _create_graph_db(db_path)

    ping = client.call("ping")
    expected_uid = os.getuid() if hasattr(os, "getuid") else 0
    assert ping["peer_uid"] == expected_uid

    workspace = client.call("workspace.register", {
        "client_view_root": str(tmp_path),
        "owner_uid": expected_uid + 1000,
    })
    assert workspace["owner_uid"] == expected_uid
    workspace_id = workspace["workspace_instance_id"]

    published = client.publish_snapshot(workspace_id, str(db_path), "uds-e2e")
    assert published["generation"] == 1
    assert published["call_count"] == 1

    stats = client.call("query.stats", {"workspace_instance_id": workspace_id})
    assert stats["edge_count"] == 1
    symbol = client.call("query.symbol", {
        "workspace_instance_id": workspace_id,
        "qualified_name": "app.helper",
    })
    assert symbol["name"] == "helper"
    found = client.call("query.search", {
        "workspace_instance_id": workspace_id,
        "query": "help",
    })
    assert [item["qualified_name"] for item in found] == ["app.helper"]
    callers = client.call("query.callers", {
        "workspace_instance_id": workspace_id,
        "callee_name": "helper",
        "qualified_name": "app.helper",
    })
    assert callers[0]["caller_qualified"] == "app.main"


def test_high_level_daemon_client_routes_to_uds(
    running_daemon, tmp_path, monkeypatch
):
    low_level, _service = running_daemon
    db_path = tmp_path / "callwarden.db"
    _create_graph_db(db_path)
    monkeypatch.setattr(
        "callwarden.server.daemon_client.get_daemon_mode", lambda: "enterprise"
    )
    client = DaemonClient(socket_path=low_level.socket_path)
    client.configure_workspace(str(tmp_path))

    symbol = client.get_symbol("app.helper", db_path=str(db_path))
    assert symbol["name"] == "helper"
    assert client.daemon_hits == 1
    assert client.sql_fallbacks == 0


def test_daemon_cli_management_flow_uses_uds(running_daemon, tmp_path, capsys):
    from callwarden.cli.daemon_commands import run_daemon_command

    client, _service = running_daemon
    socket_args = ["--socket", client.socket_path]

    assert run_daemon_command(["--socket", client.socket_path, "ping"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"

    assert run_daemon_command(socket_args + ["register", str(tmp_path)]) == 0
    workspace = json.loads(capsys.readouterr().out)
    workspace_id = workspace["workspace_instance_id"]

    assert run_daemon_command(socket_args + ["list"]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert [item["workspace_instance_id"] for item in listed] == [workspace_id]

    assert run_daemon_command(socket_args + ["status", workspace_id]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["snapshot"] is None

    db_path = tmp_path / "callwarden.db"
    _create_graph_db(db_path)
    assert run_daemon_command(
        socket_args + ["publish", workspace_id, str(db_path)]
    ) == 0
    published = json.loads(capsys.readouterr().out)
    assert published["generation"] == 1

    assert run_daemon_command(
        socket_args + ["query", workspace_id, "symbol", "app.helper"]
    ) == 0
    symbol = json.loads(capsys.readouterr().out)
    assert symbol["name"] == "helper"


def test_service_rejects_cross_uid_workspace_access(daemon_service, tmp_path):
    owner_uid = os.getuid() if hasattr(os, "getuid") else 0
    owner_peer = {"pid": os.getpid(), "uid": owner_uid, "gid": owner_uid}
    workspace = daemon_service.dispatch(owner_peer, "workspace.register", {
        "client_view_root": str(tmp_path),
        "owner_uid": owner_uid + 1000,
    })
    owner_uid = int(workspace["owner_uid"])
    other_peer = {"pid": 99999, "uid": owner_uid + 1, "gid": owner_uid + 1}

    assert daemon_service.dispatch(other_peer, "workspace.list", {}) == []
    with pytest.raises(DaemonRpcError, match="workspace 不属于当前 UID"):
        daemon_service.dispatch(other_peer, "workspace.status", {
            "workspace_instance_id": workspace["workspace_instance_id"],
        })


def test_service_rejects_cross_uid_query_isolation(daemon_service, tmp_path):
    """验收：用户 A 的所有 workspace 级操作对用户 B 完全不可见。

    补全 T-1783952125417-8255 Step #4：不仅 workspace.status 被阻断，
    workspace.connect、workspace.file.refresh、workspace.recover、
    snapshot.publish、workspace.refresh 和全部 query.* 方法都必须拒绝
    跨 UID 请求。这是"完整查询隔离"的核心要求。
    """
    owner_uid = os.getuid() if hasattr(os, "getuid") else 0
    owner_peer = {"pid": os.getpid(), "uid": owner_uid, "gid": owner_uid}
    workspace = daemon_service.dispatch(owner_peer, "workspace.register", {
        "client_view_root": str(tmp_path),
    })
    ws_id = workspace["workspace_instance_id"]
    other_peer = {"pid": 99999, "uid": owner_uid + 1, "gid": owner_uid + 1}

    # 所有 workspace 级方法都必须拒绝跨 UID
    blocked_methods = [
        ("workspace.status", {"workspace_instance_id": ws_id}),
        ("workspace.connect", {
            "workspace_instance_id": ws_id,
            "agent_session_id": "test-session",
        }),
        ("workspace.file.refresh", {
            "workspace_instance_id": ws_id,
            "rel_path": "test.py",
            "agent_session_id": "test-session",
            "session_epoch": 1,
            "monotonic_seq": 1,
        }),
        ("workspace.recover", {"workspace_instance_id": ws_id}),
        ("snapshot.publish", {
            "workspace_instance_id": ws_id,
            "db_path": "/tmp/nonexistent.db",
        }),
        ("workspace.refresh", {
            "workspace_instance_id": ws_id,
            "db_path": "/tmp/nonexistent.db",
        }),
    ]
    for method, params in blocked_methods:
        with pytest.raises(DaemonRpcError, match="workspace 不属于当前 UID"):
            daemon_service.dispatch(other_peer, method, params)

    # query.* 方法需要先 publish snapshot，但跨 UID 在 _owned_workspace 就被拦截
    query_methods = [
        ("query.stats", {"workspace_instance_id": ws_id}),
        ("query.symbol", {
         "workspace_instance_id": ws_id, "qualified_name": "foo"}),
        ("query.search", {"workspace_instance_id": ws_id, "query": "foo"}),
        ("query.callers", {
         "workspace_instance_id": ws_id, "callee_name": "foo"}),
        ("query.callees", {
         "workspace_instance_id": ws_id, "caller_name": "foo"}),
    ]
    for method, params in query_methods:
        with pytest.raises(DaemonRpcError, match="workspace 不属于当前 UID"):
            daemon_service.dispatch(other_peer, method, params)


def test_service_routes_publish_and_query_without_socket(daemon_service, tmp_path):
    uid = os.getuid() if hasattr(os, "getuid") else 0
    peer = {"pid": os.getpid(), "uid": uid, "gid": uid}
    db_path = tmp_path / "callwarden.db"
    _create_graph_db(db_path)
    workspace = daemon_service.dispatch(peer, "workspace.register", {
        "client_view_root": str(tmp_path),
    })
    workspace_id = workspace["workspace_instance_id"]
    published = daemon_service.dispatch(peer, "snapshot.publish", {
        "workspace_instance_id": workspace_id,
        "db_path": str(db_path),
    })
    assert published["generation"] == 1
    result = daemon_service.dispatch(peer, "query.symbol", {
        "workspace_instance_id": workspace_id,
        "qualified_name": "app.helper",
    })
    assert result["name"] == "helper"


@pytest.mark.skipif(
    sys.platform != "linux" or not hasattr(os, "geteuid") or os.geteuid() != 0,
    reason="真实双 UID 验收需要 Linux root 才能 setuid",
)
def test_linux_two_real_uids_are_isolated(running_daemon, tmp_path):
    client, _service = running_daemon
    socket_path = client.socket_path
    uid_a, uid_b = 19001, 19002
    root_a = tmp_path / "uid-a"
    root_b = tmp_path / "uid-b"
    root_a.mkdir()
    root_b.mkdir()
    db_a = root_a / "callwarden.db"
    db_b = root_b / "callwarden.db"
    _create_graph_db(db_a)
    _create_graph_db(db_b)
    for traversed_dir in (tmp_path.parent.parent, tmp_path.parent, tmp_path):
        os.chmod(traversed_dir, 0o755)
    os.chown(tmp_path, 0, 0)
    os.chown(root_a, uid_a, uid_a)
    os.chown(root_b, uid_b, uid_b)
    os.chown(db_a, uid_a, uid_a)
    os.chown(db_b, uid_b, uid_b)

    def run_as(uid: int, source: str) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, "-c", source],
            cwd=str(Path(__file__).resolve().parents[1]),
            text=True,
            capture_output=True,
            timeout=15,
            check=False,
            preexec_fn=lambda: (os.setgid(uid), os.setuid(uid)),
        )

    def publish_code(root: Path, db_path: Path) -> str:
        return (
            "import json; from callwarden.server.daemon_client import UnixDaemonRpcClient; "
            f"c=UnixDaemonRpcClient({socket_path!r}); "
            f"w=c.call('workspace.register', {{'client_view_root': {str(root)!r}}}); "
            f"p=c.publish_snapshot(w['workspace_instance_id'], {str(db_path)!r}); "
            "s=c.call('query.symbol', {'workspace_instance_id': w['workspace_instance_id'], "
            "'qualified_name': 'app.helper'}); "
            "print(json.dumps({'workspace': w, 'published': p, 'symbol': s}))"
        )

    registered = run_as(uid_a, publish_code(root_a, db_a))
    assert registered.returncode == 0, registered.stderr
    payload_a = json.loads(registered.stdout)
    workspace = payload_a["workspace"]
    assert workspace["owner_uid"] == uid_a
    assert payload_a["published"]["generation"] == 1
    assert payload_a["symbol"]["name"] == "helper"

    published_b = run_as(uid_b, publish_code(root_b, db_b))
    assert published_b.returncode == 0, published_b.stderr
    payload_b = json.loads(published_b.stdout)
    assert payload_b["workspace"]["owner_uid"] == uid_b
    assert payload_b["symbol"]["name"] == "helper"

    forbidden_code = (
        "from callwarden.server.daemon_client import UnixDaemonRpcClient; "
        "from callwarden.server.daemon_protocol import DaemonRemoteError; "
        f"c=UnixDaemonRpcClient({socket_path!r}); "
        "\ntry:\n"
        f" c.call('workspace.status', {{'workspace_instance_id': {workspace['workspace_instance_id']!r}}})\n"
        "except DaemonRemoteError as exc:\n print(exc.code)\n"
    )
    forbidden = run_as(uid_b, forbidden_code)
    assert forbidden.returncode == 0, forbidden.stderr
    assert forbidden.stdout.strip() == "workspace_forbidden"
