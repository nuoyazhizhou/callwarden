r"""S1 控制面验收（ADR-002 单轨 HTTPS + mTLS）。

覆盖 backlog §38.1：
- S1-01 默认 TLS 监听（manifest v2 / security_profile / endpoint / bind_scope）
- S1-08 非 loopback 无授权 → E_HTTP_BIND_REMOTE_NOT_ALLOWED（无 listener/manifest）
- S1-09 非 loopback + allow_remote → 启动成功 + bind_scope=remote
- S1-10 无明文端口（http:// 一律失败；源码无明文 endpoint 分支）
- S1-15 cw-bridge 从构建目标与源码删除
- S1-16 旧 profile env（CW_DAEMON_HTTP_PROFILE=dev_loopback_unauthenticated）启动失败
- S1-17 /health 无客户端证书可读且不泄露 principal/grant
- S1-18 /capabilities 强制 mTLS（无证书拒绝、持证 200）
- S1-20 peer_uid 收口（授权路径不读 peer_uid/current_uid）

本文件同时导出隔离 daemon / PKI / TLS 夹具，供
`test_mtls_enrollment.py` / `test_principal_authorization.py` 复用。
"""

import datetime
import glob
import http.client
import json
import os
import ssl
import subprocess
import sys
import time

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

try:
    from _w3_harness import find_daemon_binary as _find_daemon_binary
    _DAEMON_BIN = _find_daemon_binary()
except Exception:  # pragma: no cover - harness 不可导入时回退
    _DAEMON_BIN = None
if _DAEMON_BIN is None:  # pragma: no cover
    _cands = [p for p in (
        os.path.join(_REPO_ROOT, "rust_ext", "target", d, "cw-daemon.exe")
        for d in ("release", "debug")) if os.path.exists(p)]
    _DAEMON_BIN = max(_cands, key=os.path.getmtime) if _cands else None

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="daemon TLS 控制面需要 Windows + loopback HTTPS daemon",
)

requires_binaries = pytest.mark.skipif(
    _DAEMON_BIN is None,
    reason="未找到 cw-daemon 构建（需先 cargo build --bin cw-daemon）",
)


# ----------------------------------------------------------------------
# 隔离 daemon / PKI 夹具（USERPROFILE 重定向 + 独立命名管道）
# ----------------------------------------------------------------------

def daemon_home(data_root):
    home = os.path.join(data_root, "userhome")
    os.makedirs(os.path.join(home, ".callwarden"), exist_ok=True)
    return home


def daemon_env(data_root, extra=None):
    """隔离 daemon 环境：manifest/单实例锁/PKI/DB 全部落在 data_root。"""
    home = daemon_home(data_root)
    env = os.environ.copy()
    env["USERPROFILE"] = home
    env["HOME"] = home
    env["CW_DAEMON_DATA_ROOT"] = data_root
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_CODEGRAPH_DB_TEMPLATE"] = os.path.join(
        home, ".callwarden", "callwarden.db"
    )
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    import hashlib
    iso_tag = hashlib.sha256(data_root.encode("utf-8")).hexdigest()[:12]
    env["CW_DAEMON_SOCKET"] = rf"\\.\pipe\callwarden-mtls-iso-{iso_tag}"
    env.pop("CW_DAEMON_HTTP_PROFILE", None)
    env.pop("CW_DAEMON_HTTP_ALLOW_REMOTE", None)
    if extra:
        env.update({k: str(v) for k, v in extra.items()})
    return env


def spawn_daemon(data_root, bind="127.0.0.1:0", extra_env=None):
    return subprocess.Popen(
        [_DAEMON_BIN, "--http-bind=" + bind],
        env=daemon_env(data_root, extra_env),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def manifest_file(data_root):
    home = os.path.join(data_root, "userhome", ".callwarden")
    found = sorted(glob.glob(os.path.join(home, "http-daemon.*.manifest.json")))
    return found[-1] if found else None


def wait_manifest(proc, data_root, timeout=30.0):
    """等待隔离 daemon 发布自己的 manifest（pid 必须匹配）。"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return None, None
        path = manifest_file(data_root)
        if path:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    m = json.load(f)
            except (OSError, ValueError):
                time.sleep(0.2)
                continue
            if m.get("pid") == proc.pid:
                return m, path
        time.sleep(0.2)
    return None, None


def terminate(proc):
    try:
        proc.terminate()
        proc.wait(timeout=5)
    except Exception:
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass


def pki_dir_of(data_root):
    return os.path.join(data_root, "userhome", ".callwarden", "pki")


def pki_env(data_root):
    env = daemon_env(data_root)
    env["CW_PKI_DIR"] = pki_dir_of(data_root)
    return env


def run_cw_pki(data_root, *args, timeout=90):
    """执行 `cw-daemon pki <args>`；返回 (rc, stdout, stderr)。"""
    proc = subprocess.run(
        [_DAEMON_BIN, "pki"] + list(args),
        env=pki_env(data_root),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    return proc.returncode, proc.stdout, proc.stderr


def pki_init(data_root):
    rc, out, err = run_cw_pki(data_root, "init")
    assert rc == 0, f"pki init 失败: {err[-1500:]}"
    return pki_dir_of(data_root)


def parse_json_text(text):
    try:
        return json.loads(text)
    except ValueError:
        return None


def error_code_of(text):
    """从 CLI stderr / HTTP body JSON 中提取 `error.code`。"""
    data = parse_json_text(text)
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            return err.get("code")
    return None


def ca_pem_of(data_root):
    with open(os.path.join(pki_dir_of(data_root), "ca", "ca.crt"), "r", encoding="utf-8") as f:
        return f.read()


def identity_paths(data_root, principal="pr_owner"):
    base = os.path.join(pki_dir_of(data_root), "identities", principal)
    return (
        os.path.join(base, "client.crt"),
        os.path.join(base, "client.key"),
    )


def make_fake_tls_material(out_dir, host="127.0.0.1"):
    """生成测试用 TLS 材料（自签 CA + 服务端叶证书 + 客户端证书）。

    返回 dict：`ca_pem` / `ca_fingerprint_sha256` / `server_cert_sha256` /
    `server_cert` / `server_key` / `client_cert` / `client_key` / `ca_cert`，
    供假 daemon（TLSServer）与 `HttpDaemonRpcClient` 双向认证使用。
    """
    pytest.importorskip("cryptography")
    import hashlib

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

    os.makedirs(out_dir, exist_ok=True)
    now = datetime.datetime.now(datetime.timezone.utc)

    def _write(name, data):
        path = os.path.join(out_dir, name)
        if isinstance(data, bytes):
            with open(path, "wb") as f:
                f.write(data)
        else:
            with open(path, "w", encoding="utf-8") as f:
                f.write(data)
        return path

    ca_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "callwarden-test-ca")])
    ca_cert = (
        x509.CertificateBuilder()
        .subject_name(ca_name).issuer_name(ca_name)
        .public_key(ca_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - datetime.timedelta(hours=1))
        .not_valid_after(now + datetime.timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(ca_key, hashes.SHA256())
    )
    ca_pem = ca_cert.public_bytes(serialization.Encoding.PEM).decode("ascii")
    ca_cert_path = _write("ca.crt", ca_pem)

    def _leaf(common_name, sans, eku):
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = (
            x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)]))
            .issuer_name(ca_cert.subject)
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - datetime.timedelta(hours=1))
            .not_valid_after(now + datetime.timedelta(days=90))
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName(sans), critical=False)
            .add_extension(x509.ExtendedKeyUsage([eku]), critical=False)
            .sign(ca_key, hashes.SHA256())
        )
        der = cert.public_bytes(serialization.Encoding.DER)
        key_pem = key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
        return (
            cert.public_bytes(serialization.Encoding.PEM),
            key_pem,
            hashlib.sha256(der).hexdigest(),
        )

    server_pem, server_key_pem, server_sha = _leaf(
        "callwarden-test-daemon",
        [x509.IPAddress(__import__("ipaddress").IPv4Address(host))],
        ExtendedKeyUsageOID.SERVER_AUTH,
    )
    client_pem, client_key_pem, _ = _leaf(
        "pr_owner",
        [x509.UniformResourceIdentifier(
            "spiffe://callwarden.local/principal/pr_owner"
        )],
        ExtendedKeyUsageOID.CLIENT_AUTH,
    )

    return {
        "ca_pem": ca_pem,
        "ca_fingerprint_sha256": hashlib.sha256(
            ssl.PEM_cert_to_DER_cert(ca_pem)
        ).hexdigest(),
        "server_cert_sha256": server_sha,
        "ca_cert": ca_cert_path,
        "server_cert": _write("server.crt", server_pem),
        "server_key": _write("server.key", server_key_pem),
        "client_cert": _write("client.crt", client_pem),
        "client_key": _write("client.key", client_key_pem),
    }


def build_ssl_context(data_root, client_cert=None, client_key=None):
    """仅信任隔离 CA 的 SSLContext；给定 cert/key 时启用 mTLS。"""
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = True
    ctx.verify_mode = ssl.CERT_REQUIRED
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.load_verify_locations(cadata=ca_pem_of(data_root))
    if client_cert and client_key:
        ctx.load_cert_chain(certfile=client_cert, keyfile=client_key)
    return ctx


def https_get(endpoint, path, ctx):
    """GET endpoint+path；返回 (status, body_text)。TLS 失败抛异常。"""
    host_port = endpoint.split("://", 1)[1]
    host, _, port = host_port.rpartition(":")
    conn = http.client.HTTPSConnection(host, int(port), timeout=10, context=ctx)
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        body = resp.read().decode("utf-8", "replace")
        return resp.status, body
    finally:
        conn.close()


def https_post(endpoint, path, ctx, payload, timeout=30):
    host_port = endpoint.split("://", 1)[1]
    host, _, port = host_port.rpartition(":")
    conn = http.client.HTTPSConnection(host, int(port), timeout=timeout, context=ctx)
    try:
        conn.request(
            "POST", path, body=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        return resp.status, resp.read().decode("utf-8", "replace")
    finally:
        conn.close()


def wait_health(endpoint, ctx, timeout=120.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            status, body = https_get(endpoint, "/health", ctx)
            if status == 200:
                return json.loads(body)
            last = (status, body)
        except Exception as exc:  # 服务端尚未进入 accept/serve 阶段
            last = exc
        time.sleep(0.5)
    pytest.fail(f"隔离 daemon /health 未在 {timeout}s 内就绪: {last}")


@pytest.fixture
def live_daemon(tmp_path, monkeypatch):
    """起隔离 daemon（已 pki init），yield (manifest, manifest_path, data_root)。"""
    if _DAEMON_BIN is None:
        pytest.skip("cw-daemon 未构建")
    data_root = str(tmp_path / "data")
    os.makedirs(data_root, exist_ok=True)
    proc = spawn_daemon(data_root)
    try:
        manifest, mpath = wait_manifest(proc, data_root)
        if manifest is None:
            out, err = proc.communicate(timeout=5)
            pytest.fail(
                "隔离 daemon 未发布 manifest\n"
                f"stdout={out.decode('utf-8', 'replace')[-1500:]}\n"
                f"stderr={err.decode('utf-8', 'replace')[-2000:]}")
        pki_init(data_root)
        ctx = build_ssl_context(data_root)
        wait_health(manifest["endpoint"], ctx)
        yield manifest, mpath, data_root
    finally:
        terminate(proc)


# ----------------------------------------------------------------------
# S1-01 / S1-17 / S1-18：默认 TLS listener 与探活面
# ----------------------------------------------------------------------

class TestDefaultTlsListener:

    @requires_binaries
    def test_s1_01_manifest_declares_https_mtls_loopback(self, live_daemon):
        manifest, _path, _root = live_daemon
        assert manifest["schema"] == "callwarden-http-manifest/v2"
        assert manifest["security_profile"] == "https_mtls_v1"
        assert manifest["auth"] == "mtls_client_cert_required"
        assert manifest["endpoint"].startswith("https://")
        assert manifest["bind_scope"] == "loopback"
        tls = manifest["tls"]
        assert tls["min_version"] == "1.2"
        assert isinstance(tls["ca_cert_pem"], str) and "BEGIN CERTIFICATE" in tls["ca_cert_pem"]
        assert len(tls["ca_fingerprint_sha256"]) == 64
        assert "IP:127.0.0.1" in tls["san"]
        # manifest 绝不携带私钥/principal/grant
        raw = json.dumps(manifest)
        assert "PRIVATE KEY" not in raw
        assert "principal" not in raw
        assert "grants" not in raw

    @requires_binaries
    def test_s1_17_health_without_client_cert_is_public_and_minimal(self, live_daemon):
        manifest, _path, data_root = live_daemon
        ctx = build_ssl_context(data_root)  # 无客户端证书
        status, body = https_get(manifest["endpoint"], "/health", ctx)
        assert status == 200
        health = json.loads(body)
        assert health["security_profile"] == "https_mtls_v1"
        assert health["pid"] == manifest["pid"]
        assert set(health) <= {
            "security_profile", "endpoint", "pid", "git_commit",
            "schema_version", "worker_status", "capability_registry_revision",
            "manifest_id",
        }, f"/health 泄露了额外字段: {sorted(health)}"
        assert "principal" not in health
        assert "grants" not in health

    @requires_binaries
    def test_s1_18_capabilities_requires_client_cert(self, live_daemon):
        manifest, _path, data_root = live_daemon
        ctx = build_ssl_context(data_root)  # 无客户端证书
        status, body = https_get(manifest["endpoint"], "/capabilities", ctx)
        assert status == 403, body
        assert error_code_of(body) == "E_MTLS_CLIENT_CERT_MISSING"

        cert, key = identity_paths(data_root)
        ctx_mtls = build_ssl_context(data_root, cert, key)
        status2, body2 = https_get(manifest["endpoint"], "/capabilities", ctx_mtls)
        assert status2 == 200, body2


# ----------------------------------------------------------------------
# S1-10：无明文端口
# ----------------------------------------------------------------------

class TestNoPlaintext:

    @requires_binaries
    def test_s1_10_plaintext_request_to_tls_port_fails(self, live_daemon):
        manifest, _path, _root = live_daemon
        host_port = manifest["endpoint"].split("://", 1)[1]
        host, _, port = host_port.rpartition(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=5)
        with pytest.raises(Exception):
            conn.request("GET", "/health")
            conn.getresponse()
        conn.close()

    def test_s1_10_python_client_rejects_plaintext_endpoint(self):
        from callwarden.server import daemon_autostart as da
        from callwarden.config import E_TLS_REQUIRED
        from callwarden.server.daemon_protocol import DaemonRemoteError

        with pytest.raises(DaemonRemoteError) as exc:
            da.validate_http_endpoint_loopback("http://127.0.0.1:7633")
        assert exc.value.code == E_TLS_REQUIRED

    def test_s1_10_source_has_no_plaintext_endpoint_branch(self):
        src_path = os.path.join(_REPO_ROOT, "rust_ext", "src", "daemon", "http_server.rs")
        with open(src_path, "r", encoding="utf-8") as f:
            src = f.read()
        assert 'format!("https://{}", local)' in src
        assert 'format!("http://{}", local)' not in src


# ----------------------------------------------------------------------
# S1-16 / S1-08 / S1-09：绑定与 profile 门禁
# ----------------------------------------------------------------------

class TestBindAndProfileGates:

    @requires_binaries
    def test_s1_16_legacy_profile_env_fails_startup(self, tmp_path):
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        proc = spawn_daemon(
            data_root,
            extra_env={"CW_DAEMON_HTTP_PROFILE": "dev_loopback_unauthenticated"},
        )
        try:
            _out, err = proc.communicate(timeout=30)
        finally:
            terminate(proc)
        assert proc.returncode == 1, f"旧 profile 必须启动失败，rc={proc.returncode}"
        stderr = err.decode("utf-8", "replace")
        assert "E_HTTP_PROFILE_INVALID" in stderr, stderr[-2000:]
        assert manifest_file(data_root) is None, "旧 profile 不得发布 manifest"

    @requires_binaries
    def test_s1_08_nonloopback_without_allow_remote_fails_closed(self, tmp_path):
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        proc = spawn_daemon(data_root, bind="0.0.0.0:0")
        try:
            _out, err = proc.communicate(timeout=30)
        finally:
            terminate(proc)
        assert proc.returncode == 1, f"非 loopback 无授权必须启动失败，rc={proc.returncode}"
        stderr = err.decode("utf-8", "replace")
        assert "E_HTTP_BIND_REMOTE_NOT_ALLOWED" in stderr, stderr[-2000:]
        assert manifest_file(data_root) is None, "拒绝时不得发布 manifest"

    @requires_binaries
    def test_s1_09_nonloopback_with_allow_remote_starts(self, tmp_path, monkeypatch):
        data_root = str(tmp_path / "data")
        os.makedirs(data_root, exist_ok=True)
        proc = spawn_daemon(
            data_root, bind="0.0.0.0:0",
            extra_env={"CW_DAEMON_HTTP_ALLOW_REMOTE": "1"},
        )
        try:
            manifest, mpath = wait_manifest(proc, data_root)
            if manifest is None:
                out, err = proc.communicate(timeout=5)
                pytest.fail(
                    "allow_remote 启动未发布 manifest\n"
                    f"stdout={out.decode('utf-8', 'replace')[-1500:]}\n"
                    f"stderr={err.decode('utf-8', 'replace')[-2000:]}")
            assert manifest["bind_scope"] == "remote"
            assert manifest["endpoint"].startswith("https://0.0.0.0:")
            assert "IP:0.0.0.0" in manifest["tls"]["san"]
            pki_init(data_root)
            ctx = build_ssl_context(data_root)
            cert, key = identity_paths(data_root)
            ctx_mtls = build_ssl_context(data_root, cert, key)
            # 服务端 SAN 含 IP:127.0.0.1，故本机经 127.0.0.1 复用同一监听做 mTLS 验证
            port = manifest["endpoint"].rsplit(":", 1)[1]
            wait_health(f"https://127.0.0.1:{port}", ctx)
            status, body = https_get(f"https://127.0.0.1:{port}", "/capabilities", ctx_mtls)
            assert status == 200, body
        finally:
            terminate(proc)


# ----------------------------------------------------------------------
# S1-15：cw-bridge 删除
# ----------------------------------------------------------------------

class TestBridgeRemoval:

    def test_s1_15_no_bridge_bin_target_or_source(self):
        cargo_path = os.path.join(_REPO_ROOT, "rust_ext", "Cargo.toml")
        with open(cargo_path, "r", encoding="utf-8") as f:
            cargo = f.read()
        assert "cw-bridge" not in cargo
        assert "cw_bridge.rs" not in cargo
        assert not os.path.exists(
            os.path.join(_REPO_ROOT, "rust_ext", "src", "bin", "cw_bridge.rs")
        )

    @requires_binaries
    def test_s1_15_no_bridge_artifact_after_build(self):
        built = glob.glob(os.path.join(_REPO_ROOT, "rust_ext", "target", "*", "cw-bridge.exe"))
        assert built == [], f"构建产物仍含 cw-bridge: {built}"


# ----------------------------------------------------------------------
# S1-20：peer_uid 收口（授权路径不读 peer_uid/current_uid）
# ----------------------------------------------------------------------

class TestPeerUidContainment:

    def _read(self, rel):
        with open(os.path.join(_REPO_ROOT, rel), "r", encoding="utf-8") as f:
            return f.read()

    @staticmethod
    def _code_only(src):
        """去掉 `//` / `//!` 注释行，只看可执行代码（注释里允许说明 peer_uid 不参与授权）。"""
        return "\n".join(
            line for line in src.splitlines() if not line.strip().startswith("//")
        )

    def test_s1_20_principal_authorization_does_not_read_peer_uid(self):
        src = self._code_only(
            self._read(os.path.join("rust_ext", "src", "daemon", "auth_principal.rs"))
        )
        assert "peer_uid" not in src
        assert "current_uid" not in src

    def test_s1_20_http_authorize_middleware_uses_san_only(self):
        src = self._read(os.path.join("rust_ext", "src", "daemon", "http_server.rs"))
        start = src.index("async fn authorize_principal")
        end = src.index("pub fn build_router", start)
        region = self._code_only(src[start:end])
        assert "peer_uid" not in region
        assert "current_uid" not in region
        assert "principal_id" in region
