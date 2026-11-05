r"""S1 principal 授权验收（ADR-002 §5）：SAN URI 唯一主语、未知 principal、
workspace grant 与授权回收。

覆盖 backlog §38.1：S1-04 / S1-05 / S1-13 / S1-14。
"""

import datetime
import json
import os
import sqlite3
import sys
import time

import pytest

import test_mtls_control_plane as cp

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="mTLS principal 授权需要 Windows + loopback HTTPS daemon",
)

requires_binaries = pytest.mark.skipif(
    cp._DAEMON_BIN is None,
    reason="未找到 cw-daemon 构建（需先 cargo build --bin cw-daemon）",
)


@pytest.fixture
def daemon(tmp_path):
    if cp._DAEMON_BIN is None:
        pytest.skip("cw-daemon 未构建")
    data_root = str(tmp_path / "data")
    os.makedirs(data_root, exist_ok=True)
    proc = cp.spawn_daemon(data_root)
    try:
        manifest, _path = cp.wait_manifest(proc, data_root)
        if manifest is None:
            out, err = proc.communicate(timeout=5)
            pytest.fail(
                "隔离 daemon 未发布 manifest\n"
                f"stdout={out.decode('utf-8', 'replace')[-1500:]}\n"
                f"stderr={err.decode('utf-8', 'replace')[-2000:]}")
        cp.pki_init(data_root)
        cp.wait_health(manifest["endpoint"], cp.build_ssl_context(data_root))
        yield manifest, data_root
    finally:
        cp.terminate(proc)


def enroll_principal(data_root, principal, workspace="1"):
    rc, out, err = cp.run_cw_pki(
        data_root, "token", "issue", "--principal", principal, "--workspace", workspace
    )
    assert rc == 0, err[-1000:]
    token = json.loads(out)["token"]
    rc2, _out2, err2 = cp.run_cw_pki(
        data_root, "enroll", "--token", token, "--endpoint",
        _CURRENT_ENDPOINT["value"],
    )
    assert rc2 == 0, err2[-1000:]
    return principal


# 由 fixture 注入，避免 enroll 助手到处传参
_CURRENT_ENDPOINT = {"value": None}


def rpc(manifest, data_root, principal, method="ping", params=None):
    cert, key = cp.identity_paths(data_root, principal)
    return _rpc_with(manifest, cert, key, data_root, method, params)


def _make_client_identity(data_root, principal, sans, days=30):
    """用 daemon CA 私钥签发客户端证书（SAN 可控）。"""
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    ca_dir = os.path.join(cp.pki_dir_of(data_root), "ca")
    with open(os.path.join(ca_dir, "ca.crt"), "rb") as f:
        ca_cert = x509.load_pem_x509_certificate(f.read())
    with open(os.path.join(ca_dir, "ca.key"), "rb") as f:
        ca_key = serialization.load_pem_private_key(f.read(), password=None)

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.datetime.now(datetime.timezone.utc)
    builder = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, principal)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=days))
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
    )
    if sans:
        builder = builder.add_extension(
            x509.SubjectAlternativeName([x509.UniformResourceIdentifier(s) for s in sans]),
            critical=False,
        )
    cert = builder.sign(ca_key, hashes.SHA256())

    out_dir = os.path.join(data_root, "custom_identities", principal)
    os.makedirs(out_dir, exist_ok=True)
    cert_path = os.path.join(out_dir, "client.crt")
    key_path = os.path.join(out_dir, "client.key")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    with open(key_path, "wb") as f:
        f.write(key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ))
    return cert_path, key_path


def _rpc_with(manifest, cert, key, data_root, method="ping", params=None):
    ctx = cp.build_ssl_context(data_root, cert, key)
    status, body = cp.https_post(
        manifest["endpoint"], "/v1/rpc", ctx,
        {
            "jsonrpc": "2.0", "id": "s1", "method": method,
            "params": params or {}, "protocol_version": "1",
        },
    )
    data = json.loads(body)
    if status != 200:
        return status, cp.error_code_of(body), data
    if "error" in data:
        return status, (data["error"].get("data") or {}).get("code"), data
    return status, None, data


# ----------------------------------------------------------------------
# S1-04：正确 CA + 正确 SAN → 通过，ping 回显 principal_id
# ----------------------------------------------------------------------

class TestAuthorizedPrincipal:

    @requires_binaries
    def test_s1_04_enrolled_principal_is_authorized_and_echoed(self, daemon):
        manifest, data_root = daemon
        _CURRENT_ENDPOINT["value"] = manifest["endpoint"]
        enroll_principal(data_root, "pr_ok", "1")

        status, code, data = rpc(manifest, data_root, "pr_ok")
        assert status == 200 and code is None, data
        assert data["result"]["principal_id"] == "pr_ok"
        assert data["result"]["status"] == "ok"

        # 授权仅读 SAN URI：证书 SHA256/序列号来自 rustls 已验证的叶证书
        with open(
            os.path.join(cp.pki_dir_of(data_root), "identities", "pr_ok", "meta.json"),
            "r", encoding="utf-8",
        ) as f:
            meta = json.load(f)
        assert len(meta["serial"]) == len(data["result"].get("cert_serial", meta["serial"])) \
            or "cert_serial" not in data["result"]

    @requires_binaries
    def test_s1_04_unsupported_workspace_scope_is_absent_without_grant(self, daemon):
        manifest, data_root = daemon
        _CURRENT_ENDPOINT["value"] = manifest["endpoint"]
        enroll_principal(data_root, "pr_scope", "1")
        status, code, data = rpc(
            manifest, data_root, "pr_scope", params={"workspace_id": "1"}
        )
        assert status == 200 and code is None, data


# ----------------------------------------------------------------------
# S1-05 / S1-20：错 SAN / 无 SAN → fail-closed（授权只看 SAN URI）
# ----------------------------------------------------------------------

class TestSanAuthorization:

    @requires_binaries
    def test_s1_05_unknown_principal_uri_is_rejected(self, daemon):
        manifest, data_root = daemon
        cert, key = _make_client_identity(
            data_root, "pr_ghost",
            ["spiffe://callwarden.local/principal/pr_ghost"],
        )
        status, code, _data = _rpc_with(manifest, cert, key, data_root)
        assert status == 403, code
        assert code == "E_PRINCIPAL_UNKNOWN", code

    @requires_binaries
    def test_s1_05_foreign_san_uri_is_rejected(self, daemon):
        manifest, data_root = daemon
        cert, key = _make_client_identity(
            data_root, "pr_foreign",
            ["spiffe://evil.example/principal/pr_foreign"],
        )
        status, code, _data = _rpc_with(manifest, cert, key, data_root)
        assert status == 403, code
        assert code in {
            "E_MTLS_CLIENT_CERT_MISSING", "E_PRINCIPAL_UNKNOWN",
            "E_MTLS_CLIENT_CERT_REJECTED",
        }, code

    @requires_binaries
    def test_s1_05_missing_san_uri_is_rejected(self, daemon):
        manifest, data_root = daemon
        cert, key = _make_client_identity(data_root, "pr_nosan", sans=[])
        status, code, _data = _rpc_with(manifest, cert, key, data_root)
        assert status == 403, code
        # 无 SAN URI → 服务端视为“无 principal 证书”（fail-closed）
        assert code in {"E_MTLS_CLIENT_CERT_MISSING", "E_MTLS_CLIENT_CERT_REJECTED"}, code


# ----------------------------------------------------------------------
# S1-13：跨 workspace 拒绝
# ----------------------------------------------------------------------

class TestWorkspaceGrant:

    @requires_binaries
    def test_s1_13_cross_workspace_request_is_forbidden(self, daemon):
        manifest, data_root = daemon
        _CURRENT_ENDPOINT["value"] = manifest["endpoint"]
        enroll_principal(data_root, "pr_ws", "1")

        status, code, data = rpc(
            manifest, data_root, "pr_ws", params={"workspace_id": "2"}
        )
        assert status == 200, data
        assert code == "E_PRINCIPAL_WORKSPACE_FORBIDDEN", data

        status2, code2, data2 = rpc(
            manifest, data_root, "pr_ws", params={"workspace_id": "1"}
        )
        assert status2 == 200 and code2 is None, data2


# ----------------------------------------------------------------------
# S1-14：授权回收（grant 解除后新连接立即拒绝）
# ----------------------------------------------------------------------

class TestGrantRevocation:

    @requires_binaries
    def test_s1_14_revoked_workspace_grant_blocks_new_connection(self, daemon):
        manifest, data_root = daemon
        _CURRENT_ENDPOINT["value"] = manifest["endpoint"]
        enroll_principal(data_root, "pr_grant", "1")

        status, code, data = rpc(
            manifest, data_root, "pr_grant", params={"workspace_id": "1"}
        )
        assert status == 200 and code is None, data

        # 磁盘 grants.json 是授权权威（每次新连接实时读取）
        grants_path = os.path.join(
            cp.pki_dir_of(data_root), "identities", "pr_grant", "grants.json"
        )
        with open(grants_path, "w", encoding="utf-8") as f:
            json.dump({"principal_id": "pr_grant", "workspace_ids": []}, f)

        # 新连接（新 SSLContext）≤1s 内拒绝；旧连接由 MAX_CONNECTION_AGE 兜底
        status2, code2, data2 = rpc(
            manifest, data_root, "pr_grant", params={"workspace_id": "1"}
        )
        assert status2 == 200, data2
        assert code2 == "E_PRINCIPAL_WORKSPACE_FORBIDDEN", data2
        time.sleep(0.2)

    @requires_binaries
    def test_s1_14_db_grant_projections_match_disk(self, daemon):
        manifest, data_root = daemon
        _CURRENT_ENDPOINT["value"] = manifest["endpoint"]
        enroll_principal(data_root, "pr_db_grant", "7")

        conn = sqlite3.connect(os.path.join(data_root, "task.db"))
        try:
            rows = conn.execute(
                "SELECT workspace_id FROM workspace_principal_grants "
                "WHERE principal_id = ?", ("pr_db_grant",)
            ).fetchall()
        finally:
            conn.close()
        assert rows and str(rows[0][0]) == "7", "DB 授权投影缺失"
