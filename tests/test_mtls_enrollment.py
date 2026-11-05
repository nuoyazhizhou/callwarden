r"""S1 引导链验收（ADR-002 §4.2/§4.3）：token 单次/过期、错 CA、无证书、
过期证书、吊销、签发台账可校验。

覆盖 backlog §38.1：S1-02 / S1-03 / S1-06 / S1-07 / S1-11 / S1-12 / S1-19。
夹具与隔离 daemon/PKI 工具复用 `test_mtls_control_plane.py`。
"""

import datetime
import json
import os
import sqlite3
import ssl
import sys
import time

import pytest

import test_mtls_control_plane as cp

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="mTLS 引导链需要 Windows + loopback HTTPS daemon",
)

requires_binaries = pytest.mark.skipif(
    cp._DAEMON_BIN is None,
    reason="未找到 cw-daemon 构建（需先 cargo build --bin cw-daemon）",
)


@pytest.fixture
def daemon(tmp_path):
    """隔离 daemon（已 pki init 且 /health 就绪），yield (manifest, data_root)。"""
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
        yield manifest, data_root, proc
    finally:
        cp.terminate(proc)


def issue_token(data_root, principal, workspace="1"):
    rc, out, err = cp.run_cw_pki(
        data_root, "token", "issue", "--principal", principal, "--workspace", workspace
    )
    assert rc == 0, f"token issue 失败: {err[-1000:]}"
    return json.loads(out)


def enroll(data_root, token, endpoint, ca_path=None):
    args = ["enroll", "--token", token, "--endpoint", endpoint]
    if ca_path:
        args += ["--ca", ca_path]
    return cp.run_cw_pki(data_root, *args)


def client_for(manifest, data_root, principal):
    from callwarden.server.daemon_client import HttpDaemonRpcClient
    cert, key = cp.identity_paths(data_root, principal)
    os.environ_backup = None  # noqa: F841 - 保持调用方环境不变
    return HttpDaemonRpcClient(
        endpoint=manifest["endpoint"],
        verify_health=False,
        timeout=10.0,
    ), cert, key


def mtls_get(endpoint, path, data_root, principal):
    cert, key = cp.identity_paths(data_root, principal)
    ctx = cp.build_ssl_context(data_root, cert, key)
    return cp.https_get(endpoint, path, ctx)


def mtls_rpc(endpoint, data_root, principal, payload):
    cert, key = cp.identity_paths(data_root, principal)
    ctx = cp.build_ssl_context(data_root, cert, key)
    if "protocol_version" not in payload:
        payload["protocol_version"] = "1"
    return cp.https_post(endpoint, "/v1/rpc", ctx, payload)


def rpc_error_code(status, body):
    if status != 200:
        return cp.error_code_of(body)
    data = json.loads(body)
    err = data.get("error")
    return err.get("data", {}).get("code") or err.get("code") if isinstance(err, dict) else None


# ----------------------------------------------------------------------
# S1-02 / S1-03：无客户端证书 / 错 CA
# ----------------------------------------------------------------------

class TestClientAuthNegative:

    @requires_binaries
    def test_s1_02_rpc_without_client_cert_is_403(self, daemon):
        manifest, data_root, _proc = daemon
        ctx = cp.build_ssl_context(data_root)  # 无客户端证书
        status, body = cp.https_post(
            manifest["endpoint"], "/v1/rpc", ctx,
            {"jsonrpc": "2.0", "id": "s1-02", "method": "ping", "params": {}},
        )
        assert status == 403, body
        assert cp.error_code_of(body) == "E_MTLS_CLIENT_CERT_MISSING"

    def test_s1_02_python_client_fails_closed_without_certificate(self, monkeypatch, tmp_path):
        from callwarden.server.daemon_client import (
            DaemonUnavailableError,
            HttpDaemonRpcClient,
        )
        from callwarden.config import E_MTLS_CLIENT_CERT_MISSING

        empty_pki = str(tmp_path / "empty_pki")
        os.makedirs(empty_pki, exist_ok=True)
        for key in ("CW_CLIENT_CERT", "CW_CLIENT_KEY", "CW_CA_CERT", "CW_PRINCIPAL_ID"):
            monkeypatch.delenv(key, raising=False)
        monkeypatch.setenv("CW_PKI_DIR", empty_pki)

        client = HttpDaemonRpcClient(
            endpoint="https://127.0.0.1:7633", verify_health=False
        )
        with pytest.raises(DaemonUnavailableError) as exc:
            client.call("ping", {})
        assert exc.value.code == E_MTLS_CLIENT_CERT_MISSING

    @requires_binaries
    def test_s1_03_enroll_with_wrong_ca_is_rejected(self, daemon, tmp_path):
        manifest, data_root, _proc = daemon
        wrong_ca = _make_self_signed_ca(tmp_path / "wrong-ca")
        token = issue_token(data_root, "pr_wrong_ca")["token"]
        rc, out, err = enroll(
            data_root, token, manifest["endpoint"], ca_path=str(wrong_ca)
        )
        assert rc != 0, "错 CA 引导必须失败（fail-closed）"
        assert cp.error_code_of(err) in {
            "E_MTLS_UNKNOWN_CA", "E_TLS_HANDSHAKE_FAILED",
        }, err[-800:]


def _make_self_signed_ca(path):
    """用 cryptography 生成一个与 daemon CA 无关的自签 CA 证书（PEM）。"""
    pytest.importorskip("cryptography")
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "wrong-ca")])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    out = path
    os.makedirs(out, exist_ok=True)
    cert_path = os.path.join(out, "ca.crt")
    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))
    return cert_path


def _make_expired_client_identity(data_root, principal, out_dir):
    """用 daemon CA 私钥签发一个已过期的客户端证书（SAN URI = principal）。"""
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
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, principal)]))
        .issuer_name(ca_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(days=3))
        .not_valid_after(now - datetime.timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([
                x509.UniformResourceIdentifier(
                    f"spiffe://callwarden.local/principal/{principal}"
                )
            ]),
            critical=False,
        )
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]), critical=False
        )
        .sign(ca_key, hashes.SHA256())
    )
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


# ----------------------------------------------------------------------
# S1-06：过期客户端证书
# ----------------------------------------------------------------------

class TestExpiredCertificate:

    @requires_binaries
    def test_s1_06_expired_client_cert_rejected_at_handshake(self, daemon, tmp_path):
        manifest, data_root, _proc = daemon
        cert, key = _make_expired_client_identity(
            data_root, "pr_expired", str(tmp_path / "expired_identity")
        )
        ctx = cp.build_ssl_context(data_root, cert, key)
        with pytest.raises((ssl.SSLError, OSError)):
            cp.https_get(manifest["endpoint"], "/health", ctx)


# ----------------------------------------------------------------------
# S1-11 / S1-12：token 单次与过期
# ----------------------------------------------------------------------

class TestEnrollToken:

    @requires_binaries
    def test_s1_11_token_is_single_use(self, daemon):
        manifest, data_root, _proc = daemon
        rec = issue_token(data_root, "pr_single")
        rc1, _out1, err1 = enroll(data_root, rec["token"], manifest["endpoint"])
        assert rc1 == 0, f"首次 enroll 应成功: {err1[-1000:]}"

        rc2, _out2, err2 = enroll(data_root, rec["token"], manifest["endpoint"])
        assert rc2 != 0, "同一 token 第二次 enroll 必须失败"
        code = cp.error_code_of(err2)
        assert code in {"E_ENROLL_TOKEN_INVALID", "E_ENROLL_TOKEN_USED"}, err2[-800:]
        # 单次语义：bootstrap 权威文件已被消费
        assert not os.path.exists(
            os.path.join(cp.pki_dir_of(data_root), "bootstrap", rec["token_id"] + ".json")
        )

    @requires_binaries
    def test_s1_12_expired_token_is_rejected(self, daemon):
        manifest, data_root, _proc = daemon
        rec = issue_token(data_root, "pr_expired_token")
        token_path = os.path.join(
            cp.pki_dir_of(data_root), "bootstrap", rec["token_id"] + ".json"
        )
        with open(token_path, "r", encoding="utf-8") as f:
            payload = json.load(f)
        payload["expires_at"] = int(time.time()) - 10
        with open(token_path, "w", encoding="utf-8") as f:
            json.dump(payload, f)

        rc, _out, err = enroll(data_root, rec["token"], manifest["endpoint"])
        assert rc != 0, "过期 token 必须失败"
        assert cp.error_code_of(err) in {
            "E_ENROLL_TOKEN_INVALID", "E_ENROLL_TOKEN_EXPIRED",
        }, err[-800:]


# ----------------------------------------------------------------------
# S1-07 / S1-19：吊销与签发台账
# ----------------------------------------------------------------------

def _meta_of(data_root, principal):
    with open(
        os.path.join(cp.pki_dir_of(data_root), "identities", principal, "meta.json"),
        "r", encoding="utf-8",
    ) as f:
        return json.load(f)


class TestRevocationAndLedger:

    @requires_binaries
    def test_s1_07_revoked_serial_is_rejected_on_new_connection(self, daemon):
        manifest, data_root, _proc = daemon
        rec = issue_token(data_root, "pr_revoke_me")
        rc, _out, err = enroll(data_root, rec["token"], manifest["endpoint"])
        assert rc == 0, err[-1000:]
        meta = _meta_of(data_root, "pr_revoke_me")
        serial = meta["serial"]

        # 首次：未吊销 → mTLS 调用成功（ping 回显 principal_id，见 S1-04）
        status, body = mtls_rpc(
            manifest["endpoint"], data_root, "pr_revoke_me",
            {"jsonrpc": "2.0", "id": "before", "method": "ping", "params": {}},
        )
        assert status == 200, body
        assert json.loads(body)["result"]["principal_id"] == "pr_revoke_me"

        rc2, _out2, err2 = cp.run_cw_pki(data_root, "revoke", "--serial", serial)
        assert rc2 == 0, err2[-1000:]
        assert cp.error_code_of(err2) is None

        time.sleep(1.5)  # DB data_version 轮询粒度 ≤1s
        status2, body2 = mtls_rpc(
            manifest["endpoint"], data_root, "pr_revoke_me",
            {"jsonrpc": "2.0", "id": "after", "method": "ping", "params": {}},
        )
        assert status2 == 403, body2
        assert cp.error_code_of(body2) in {
            "E_PRINCIPAL_REVOKED", "E_MTLS_CERT_REVOKED",
        }, body2

    @requires_binaries
    def test_s1_19_issuance_and_revocation_records_are_verifiable(self, daemon):
        manifest, data_root, _proc = daemon
        rec = issue_token(data_root, "pr_ledger")
        rc, _out, err = enroll(data_root, rec["token"], manifest["endpoint"])
        assert rc == 0, err[-1000:]
        meta = _meta_of(data_root, "pr_ledger")
        # 第二次签发（同表第二条链记录，用于校验 prev_signature 串联）
        rec2 = issue_token(data_root, "pr_ledger_2")
        rc_2, _out2, err_2 = enroll(data_root, rec2["token"], manifest["endpoint"])
        assert rc_2 == 0, err_2[-1000:]
        meta2 = _meta_of(data_root, "pr_ledger_2")

        db_path = os.path.join(data_root, "task.db")
        conn = sqlite3.connect(db_path)
        try:
            token_row = conn.execute(
                "SELECT token_id, principal_id, issued_at, expires_at, status "
                "FROM pki_enrollment_tokens WHERE token_id = ?",
                (rec["token_id"],),
            ).fetchone()
            assert token_row is not None, "token 台账缺失"
            assert token_row[1] == "pr_ledger"
            assert float(token_row[2]) > 0 and float(token_row[3]) > float(token_row[2])

            cert_row = conn.execute(
                "SELECT principal_id, status, issued_at FROM pki_issued_certs "
                "WHERE cert_serial = ?",
                (meta["serial"],),
            ).fetchone()
            assert cert_row is not None, "签发台账缺失"
            assert cert_row[0] == "pr_ledger"
            assert cert_row[1] == "active"

            # S1-19：签发必须有 audit_chain 记录且链头/链尾串联
            chain = conn.execute(
                "SELECT record_id, payload_hash, prev_signature, record_signature, signing_key_id "
                "FROM audit_chain WHERE table_name = 'pki_issued_certs' ORDER BY id ASC"
            ).fetchall()
            assert len(chain) == 2, f"签发 audit_chain 记录数应为 2，实际 {len(chain)}"
            first, second = chain
            assert first[1] and first[3], "payload_hash/record_signature 不得为空"
            assert first[2] == "", "链首 prev_signature 必须为空"
            assert second[2] == first[3], "第二条记录 prev_signature 必须等于上一条 record_signature"
            assert {first[0], second[0]} == {meta["serial"], meta2["serial"]}
            assert first[4] and second[4], "signing_key_id 不得为空"
        finally:
            conn.close()

        rc2, _out2, err2 = cp.run_cw_pki(data_root, "revoke", "--serial", meta["serial"])
        assert rc2 == 0, err2[-1000:]

        conn = sqlite3.connect(db_path)
        try:
            rev = conn.execute(
                "SELECT principal_id, revoked_at, reason, actor_principal_id, audit_record_id "
                "FROM pki_revocations WHERE cert_serial = ?",
                (meta["serial"],),
            ).fetchone()
            assert rev is not None, "吊销台账缺失"
            assert rev[0] == "pr_ledger"
            assert float(rev[1]) > 0
            assert rev[2], "吊销原因不得为空"
            assert rev[3], "吊销 actor 不得为空"
            assert rev[4], "audit_record_id 必须回填 audit_chain 行号"
            rev_chain = conn.execute(
                "SELECT record_id, payload_hash, prev_signature, record_signature "
                "FROM audit_chain WHERE table_name = 'pki_revocations' ORDER BY id ASC"
            ).fetchall()
            assert len(rev_chain) == 1, f"吊销 audit_chain 记录数应为 1，实际 {len(rev_chain)}"
            assert rev_chain[0][0] == meta["serial"]
            assert rev_chain[0][1] and rev_chain[0][3]
            status_row = conn.execute(
                "SELECT status FROM pki_issued_certs WHERE cert_serial = ?",
                (meta["serial"],),
            ).fetchone()
            assert status_row[0] == "revoked"
        finally:
            conn.close()
