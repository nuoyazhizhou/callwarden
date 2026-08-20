"""H2 manifest 发现 / 校验单元测试（frozen contract §4.1）。

使用内存 dict 与临时 manifest 文件，不启动任何真实 daemon，不打开 SQLite。
覆盖：loopback 校验、manifest hash / security_profile / authority / 协议交集 /
stale-PID、endpoint 发现优先级、缺失 manifest fail-closed。
"""

import json

import pytest

from callwarden.config import (
    HTTP_MANIFEST_SCHEMA_VERSION,
    HTTP_MVP_TRANSPORT_PROFILE,
    HTTP_PROTOCOL_VERSION,
    E_HTTP_MVP_LOOPBACK_ONLY,
    E_HTTP_MANIFEST_MISSING,
    E_HTTP_MANIFEST_STALE,
    E_HTTP_MANIFEST_HASH_MISMATCH,
    E_PROTOCOL_VERSION_UNSUPPORTED,
    compute_http_manifest_hash,
    get_http_authority_id,
    is_loopback_host,
)
from callwarden.server import daemon_autostart as da
from callwarden.server.daemon_protocol import DaemonRemoteError


# ----------------------------------------------------------------------
# 构造 helper
# ----------------------------------------------------------------------

AUTHORITY = "unit-test-authority"


def build_manifest_dict(endpoint="http://127.0.0.1:0", authority_id=AUTHORITY, **overrides):
    """构造一个字段完整、hash 正确的 manifest dict。"""
    m = {
        "manifest_version": HTTP_MANIFEST_SCHEMA_VERSION,
        "manifest_id": "m-1",
        "authority_id": authority_id,
        "endpoint": endpoint,
        "pid": None,
        "process_start_time": 0,
        "daemon_executable": "",
        "daemon_binary_sha256": "deadbeef",
        "protocol_version": "1",
        "supported_protocol_versions": ["1"],
        "security_profile": HTTP_MVP_TRANSPORT_PROFILE,
        "security_profile_required": HTTP_MVP_TRANSPORT_PROFILE,
        "git_commit": "abc",
        "schema_version": 50,
        "started_at": "2026-08-14T00:00:00Z",
        "capability_registry_revision": 1,
        "worker_status": "ready",
    }
    m.update(overrides)
    m["manifest_hash"] = compute_http_manifest_hash(m)
    return m


# ----------------------------------------------------------------------
# loopback 校验
# ----------------------------------------------------------------------

def test_loopback_host_recognizes_loopback():
    assert is_loopback_host("127.0.0.1") is True
    assert is_loopback_host("127.0.0.5") is True
    assert is_loopback_host("::1") is True
    assert is_loopback_host("localhost") is True


def test_loopback_host_rejects_non_loopback():
    assert is_loopback_host("0.0.0.0") is False
    assert is_loopback_host("10.0.0.1") is False
    assert is_loopback_host("192.168.1.5") is False
    assert is_loopback_host("example.com") is False


def test_validate_endpoint_loopback_ok():
    assert da.validate_http_endpoint_loopback("http://127.0.0.1:8123") == "http://127.0.0.1:8123"


@pytest.mark.parametrize("endpoint", [
    "http://192.168.1.5:80",
    "http://10.0.0.1:80",
    "http://example.com:80",
    "https://127.0.0.1:80",
    "ftp://127.0.0.1:21",
])
def test_validate_endpoint_loopback_rejects(endpoint):
    with pytest.raises(DaemonRemoteError) as exc:
        da.validate_http_endpoint_loopback(endpoint)
    assert exc.value.code == E_HTTP_MVP_LOOPBACK_ONLY


# ----------------------------------------------------------------------
# manifest 字段校验
# ----------------------------------------------------------------------

def test_validate_manifest_ok():
    da.validate_http_manifest(build_manifest_dict(), AUTHORITY)


def test_validate_manifest_hash_mismatch():
    m = build_manifest_dict()
    m["manifest_hash"] = "tampered"
    with pytest.raises(DaemonRemoteError) as exc:
        da.validate_http_manifest(m, AUTHORITY)
    assert exc.value.code == E_HTTP_MANIFEST_HASH_MISMATCH


def test_validate_manifest_schema_version():
    m = build_manifest_dict(manifest_version="other/v9")
    with pytest.raises(DaemonRemoteError) as exc:
        da.validate_http_manifest(m, AUTHORITY)
    assert exc.value.code == E_HTTP_MANIFEST_STALE


def test_validate_manifest_security_profile():
    m = build_manifest_dict(security_profile="enterprise")
    with pytest.raises(DaemonRemoteError) as exc:
        da.validate_http_manifest(m, AUTHORITY)
    assert exc.value.code == E_HTTP_MANIFEST_STALE


def test_validate_manifest_authority():
    m = build_manifest_dict(authority_id="A")
    with pytest.raises(DaemonRemoteError) as exc:
        da.validate_http_manifest(m, "B")
    assert exc.value.code == E_HTTP_MANIFEST_STALE


def test_validate_manifest_protocol_no_intersection():
    m = build_manifest_dict(supported_protocol_versions=["2", "3"])
    with pytest.raises(DaemonRemoteError) as exc:
        da.validate_http_manifest(m, AUTHORITY)
    assert exc.value.code == E_PROTOCOL_VERSION_UNSUPPORTED


def test_validate_manifest_protocol_missing_list():
    m = build_manifest_dict(supported_protocol_versions=[])
    with pytest.raises(DaemonRemoteError) as exc:
        da.validate_http_manifest(m, AUTHORITY)
    assert exc.value.code == E_PROTOCOL_VERSION_UNSUPPORTED


def test_validate_manifest_stale_pid(monkeypatch):
    monkeypatch.setattr(da, "_pid_alive", lambda pid: False)
    m = build_manifest_dict(pid=123456)
    with pytest.raises(DaemonRemoteError) as exc:
        da.validate_http_manifest(m, AUTHORITY)
    assert exc.value.code == E_HTTP_MANIFEST_STALE


def test_validate_manifest_endpoint_must_be_loopback():
    m = build_manifest_dict(endpoint="http://192.168.1.5:80")
    with pytest.raises(DaemonRemoteError) as exc:
        da.validate_http_manifest(m, AUTHORITY)
    assert exc.value.code == E_HTTP_MVP_LOOPBACK_ONLY


# ----------------------------------------------------------------------
# 发现优先级与 fail-closed
# ----------------------------------------------------------------------

def test_resolve_explicit_endpoint_skips_manifest(monkeypatch):
    monkeypatch.setenv("CW_DAEMON_HTTP_ENDPOINT", "http://127.0.0.1:8123")
    monkeypatch.setattr(
        "callwarden.config.get_default_http_manifest_path",
        lambda authority_id: "/nonexistent/http.manifest.json",
    )
    endpoint, manifest = da.resolve_http_endpoint_and_manifest(authority_id=AUTHORITY)
    assert endpoint == "http://127.0.0.1:8123"
    assert manifest is None


def test_resolve_explicit_endpoint_nonloopback_fails(monkeypatch):
    monkeypatch.setenv("CW_DAEMON_HTTP_ENDPOINT", "http://10.0.0.1:80")
    monkeypatch.setattr(
        "callwarden.config.get_default_http_manifest_path",
        lambda authority_id: "/nonexistent/http.manifest.json",
    )
    with pytest.raises(DaemonRemoteError) as exc:
        da.resolve_http_endpoint_and_manifest(authority_id=AUTHORITY)
    assert exc.value.code == E_HTTP_MVP_LOOPBACK_ONLY


def test_resolve_missing_manifest_fails_closed(monkeypatch):
    monkeypatch.setattr(
        "callwarden.config.get_default_http_manifest_path",
        lambda authority_id: "/nonexistent/http.manifest.json",
    )
    with pytest.raises(DaemonRemoteError) as exc:
        da.resolve_http_endpoint_and_manifest(
            explicit_endpoint=None,
            manifest_path="/nonexistent/explicit.manifest.json",
            authority_id=AUTHORITY,
        )
    assert exc.value.code == E_HTTP_MANIFEST_MISSING


def test_resolve_valid_manifest_file(tmp_path, monkeypatch):
    manifest = build_manifest_dict(endpoint="http://127.0.0.1:8888")
    mpath = tmp_path / "http.manifest.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        "callwarden.config.get_default_http_manifest_path",
        lambda authority_id: "/nonexistent/http.manifest.json",
    )
    endpoint, loaded = da.resolve_http_endpoint_and_manifest(
        manifest_path=str(mpath), authority_id=AUTHORITY
    )
    assert endpoint == "http://127.0.0.1:8888"
    assert loaded["manifest_id"] == "m-1"


def test_resolve_precedence_explicit_over_manifest(tmp_path, monkeypatch):
    manifest = build_manifest_dict(endpoint="http://127.0.0.1:8888")
    mpath = tmp_path / "http.manifest.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setenv("CW_DAEMON_HTTP_ENDPOINT", "http://127.0.0.1:9999")
    monkeypatch.setattr(
        "callwarden.config.get_default_http_manifest_path",
        lambda authority_id: "/nonexistent/http.manifest.json",
    )
    endpoint, _ = da.resolve_http_endpoint_and_manifest(
        manifest_path=str(mpath), authority_id=AUTHORITY
    )
    # 显式 endpoint 优先级高于 manifest 中的 endpoint
    assert endpoint == "http://127.0.0.1:9999"


def test_resolve_stale_manifest_file_fails(tmp_path, monkeypatch):
    manifest = build_manifest_dict()
    manifest["manifest_hash"] = "tampered"
    mpath = tmp_path / "http.manifest.json"
    mpath.write_text(json.dumps(manifest), encoding="utf-8")
    monkeypatch.setattr(
        "callwarden.config.get_default_http_manifest_path",
        lambda authority_id: "/nonexistent/http.manifest.json",
    )
    with pytest.raises(DaemonRemoteError) as exc:
        da.resolve_http_endpoint_and_manifest(
            manifest_path=str(mpath), authority_id=AUTHORITY
        )
    assert exc.value.code == E_HTTP_MANIFEST_HASH_MISMATCH


def test_read_manifest_missing_file(monkeypatch, tmp_path):
    missing = tmp_path / "nope.json"
    with pytest.raises(DaemonRemoteError) as exc:
        da.read_http_manifest(str(missing))
    assert exc.value.code == E_HTTP_MANIFEST_MISSING
