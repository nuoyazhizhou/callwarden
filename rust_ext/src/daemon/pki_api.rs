//! 本地 PKI（ADR-002 §4、§15.4）：内置 CA、服务端叶证书、客户端签发、引导 token。
//!
//! 单轨 HTTPS + mTLS 的证书材料全部落在 `CW_PKI_DIR`（默认 `<manifest 目录>/pki`）：
//!
//! ```text
//! pki/
//! ├── ca/{ca.crt,ca.key,ca.meta.json}
//! ├── server/{server.crt,server.key}
//! ├── identities/<principal_id>/{client.crt,meta.json}
//! └── bootstrap/<token_id>.json
//! ```
//!
//! 不变量：
//! - 唯一信任根是 `ca/ca.crt`；服务端与客户端叶证书都由它签发（不自签叶证书）。
//! - 授权主语是 SAN URI `spiffe://callwarden.local/principal/<principal_id>`，
//!   不读 CN、不读 OS uid/SID（ADR §5.1）。
//! - 服务端证书 SAN 必须覆盖实际绑定地址，否则启动期 fail-closed
//!   `E_TLS_SAN_MISMATCH`。
//! - 引导 token 单次、短 TTL；失败对外统一 `E_ENROLL_TOKEN_INVALID`（避免 oracle）。
//!
//! 算法：ECDSA P-256（rcgen 默认）+ ring，TLS 1.2+（rustls）。

use std::io::Write;
use std::net::{IpAddr, SocketAddr};
use std::path::{Path, PathBuf};
use std::sync::Arc;

use base64::Engine;
use rcgen::{
    BasicConstraints, CertificateParams, CertificateSigningRequestParams, DnType,
    ExtendedKeyUsagePurpose, IsCa, Issuer, KeyPair, KeyUsagePurpose, SanType,
};
use rustls::pki_types::{CertificateDer, PrivateKeyDer, PrivatePkcs8KeyDer};
use rustls::server::WebPkiClientVerifier;
use rustls::{RootCertStore, ServerConfig};
use sha2::{Digest, Sha256};
use x509_parser::prelude::*;

/// 授权主语 URI 前缀（ADR §4.1）。
pub const PRINCIPAL_URI_PREFIX: &str = "spiffe://callwarden.local/principal/";
/// CA 自签有效期（10 年，按 365 天/年折算）。
const CA_VALIDITY_SECS: i64 = 3650 * 24 * 3600;
/// 服务端叶证书有效期（1 年）。
const SERVER_VALIDITY_SECS: i64 = 365 * 24 * 3600;
/// 客户端叶证书有效期（90 天）。
const CLIENT_VALIDITY_SECS: i64 = 90 * 24 * 3600;
/// 引导 token TTL（ADR §4.2：600s）。
pub const ENROLL_TOKEN_TTL_SECS: u64 = 600;

// ============================================
// 错误
// ============================================

/// PKI 错误：`code` 是稳定错误码字符串（E_*），供 HTTP/CLI 直接透出。
#[derive(Debug, Clone)]
pub struct PkiError {
    pub code: &'static str,
    pub message: String,
}

impl PkiError {
    pub fn new(code: &'static str, message: impl Into<String>) -> Self {
        Self {
            code,
            message: message.into(),
        }
    }

    fn io(ctx: &str, e: std::io::Error) -> Self {
        Self::new("E_PKI_IO", format!("{}: {}", ctx, e))
    }

    fn rcgen(ctx: &str, e: rcgen::Error) -> Self {
        Self::new("E_PKI_GENERATE", format!("{}: {}", ctx, e))
    }
}

impl std::fmt::Display for PkiError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{}: {}", self.code, self.message)
    }
}

impl std::error::Error for PkiError {}

// ============================================
// PEM / 文件工具
// ============================================

/// 生成 PEM 文本（64 列折行，LF 换行）。
pub fn pem_encode(label: &str, der: &[u8]) -> String {
    let body = base64::engine::general_purpose::STANDARD.encode(der);
    let mut out = String::with_capacity(body.len() + 64);
    out.push_str(&format!("-----BEGIN {}-----\n", label));
    for chunk in body.as_bytes().chunks(64) {
        out.push_str(&String::from_utf8_lossy(chunk));
        out.push('\n');
    }
    out.push_str(&format!("-----END {}-----\n", label));
    out
}

/// 解析 PEM 文本中指定 label 的 DER（失败返回 None）。
pub fn pem_decode(text: &str, label: &str) -> Option<Vec<u8>> {
    let begin = format!("-----BEGIN {}-----", label);
    let end = format!("-----END {}-----", label);
    let start = text.find(&begin)? + begin.len();
    let stop = text.find(&end)?;
    if stop <= start {
        return None;
    }
    let body: String = text[start..stop].chars().filter(|c| !c.is_whitespace()).collect();
    base64::engine::general_purpose::STANDARD.decode(body).ok()
}

/// PKI 根目录：`CW_PKI_DIR` 优先，否则与 manifest 同目录下的 `pki/`
/// （生产即 `~/.callwarden/pki`，测试落 tempdir）。
pub fn pki_root(manifest_path: &Path) -> PathBuf {
    if let Some(dir) = std::env::var_os("CW_PKI_DIR") {
        if !dir.is_empty() {
            return PathBuf::from(dir);
        }
    }
    manifest_path
        .parent()
        .map(|p| p.join("pki"))
        .unwrap_or_else(|| PathBuf::from("pki"))
}

/// 进程内 PKI 写入锁：保证 ca/server 证书的“生成 + 原子发布”对同进程并发调用
/// 是串行的（测试多线程并发 `bind_http` 时会同时命中缺失分支）。跨进程由
/// rename 原子性 + 每进程独立 PKI 目录约束。
static PKI_WRITE_LOCK: std::sync::Mutex<()> = std::sync::Mutex::new(());

fn pki_write_guard() -> std::sync::MutexGuard<'static, ()> {
    PKI_WRITE_LOCK.lock().unwrap_or_else(|e| e.into_inner())
}

fn ensure_dir(p: &Path) -> Result<(), PkiError> {
    std::fs::create_dir_all(p).map_err(|e| PkiError::io("create_dir", e))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(p, std::fs::Permissions::from_mode(0o700));
    }
    Ok(())
}

/// 原子写文件（唯一 tmp + rename）；`private=true` 时 Unix 置 0600。
fn write_file(path: &Path, contents: &str, private: bool) -> Result<(), PkiError> {
    if let Some(parent) = path.parent() {
        ensure_dir(parent)?;
    }
    // tmp 名必须唯一：并发写同一文件时固定 `.tmp` 会被两边 rename 抢走/丢失。
    static TMP_SEQ: std::sync::atomic::AtomicU64 = std::sync::atomic::AtomicU64::new(0);
    let seq = TMP_SEQ.fetch_add(1, std::sync::atomic::Ordering::Relaxed);
    let fname = path
        .file_name()
        .map(|n| n.to_string_lossy().to_string())
        .unwrap_or_else(|| "pki".to_string());
    let tmp = path.with_file_name(format!("{}.{}.{}.tmp", fname, std::process::id(), seq));
    {
        let mut f = std::fs::File::create(&tmp).map_err(|e| PkiError::io("create tmp", e))?;
        f.write_all(contents.as_bytes())
            .map_err(|e| PkiError::io("write tmp", e))?;
        f.sync_all().map_err(|e| PkiError::io("sync tmp", e))?;
    }
    #[cfg(unix)]
    if private {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600));
    }
    #[cfg(not(unix))]
    let _ = private;
    std::fs::rename(&tmp, path).map_err(|e| PkiError::io("rename", e))?;
    Ok(())
}

fn read_text(path: &Path) -> Result<String, PkiError> {
    std::fs::read_to_string(path).map_err(|e| PkiError::io(&format!("read {}", path.display()), e))
}

fn sha256_hex(bytes: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(bytes);
    format!("{:x}", h.finalize())
}

fn now_unix() -> i64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs() as i64)
        .unwrap_or(0)
}

/// Unix 秒 → RFC3339 UTC（`1970-01-01T00:00:00Z` 形态，无外部时钟依赖）。
pub fn rfc3339_from_unix(secs: i64) -> String {
    let days = secs / 86400;
    let rem = secs % 86400;
    let (h, m, s) = (rem / 3600, (rem % 3600) / 60, rem % 60);
    // Howard Hinnant civil_from_days
    let z = days + 719468;
    let era = if z >= 0 { z } else { z - 146096 } / 146097;
    let doe = z - era * 146097;
    let yoe = (doe - doe / 1460 + doe / 36524 - doe / 146096) / 365;
    let y = yoe + era * 400;
    let doy = doe - (365 * yoe + yoe / 4 - yoe / 100);
    let mp = (5 * doy + 2) / 153;
    let d = doy - (153 * mp + 2) / 5 + 1;
    let mth = if mp < 10 { mp + 3 } else { mp - 9 };
    let yr = if mth <= 2 { y + 1 } else { y };
    format!(
        "{:04}-{:02}-{:02}T{:02}:{:02}:{:02}Z",
        yr, mth, d, h, m, s
    )
}

/// 证书 SAN 列表（`DNS:/IP:/URI:` 前缀形式；解析失败返回空）。
pub fn certificate_sans(der: &[u8]) -> Vec<String> {
    let Ok((_, cert)) = x509_parser::parse_x509_certificate(der) else {
        return Vec::new();
    };
    let Ok(Some(ext)) = cert.subject_alternative_name() else {
        return Vec::new();
    };
    ext.value
        .general_names
        .iter()
        .filter_map(|g| match g {
            GeneralName::DNSName(d) => Some(format!("DNS:{}", d)),
            GeneralName::URI(u) => Some(format!("URI:{}", u)),
            GeneralName::IPAddress(b) => {
                let ip = match b.len() {
                    4 => IpAddr::from([b[0], b[1], b[2], b[3]]),
                    16 => {
                        let mut arr = [0u8; 16];
                        arr.copy_from_slice(b);
                        IpAddr::from(arr)
                    }
                    _ => return None,
                };
                Some(format!("IP:{}", ip))
            }
            _ => None,
        })
        .collect()
}

/// 从叶证书 SAN URI 提取 `principal_id`（唯一授权主语）。
pub fn principal_id_from_cert(der: &[u8]) -> Option<String> {
    certificate_sans(der)
        .into_iter()
        .find_map(|s| s.strip_prefix("URI:").and_then(|u| {
            u.strip_prefix(PRINCIPAL_URI_PREFIX)
                .map(|p| p.to_string())
        }))
        .filter(|p| !p.is_empty())
}

/// 证书 SHA-256 指纹（小写 hex）。
pub fn cert_fingerprint(der: &[u8]) -> String {
    sha256_hex(der)
}

/// 证书 serial（大写 hex，与 X.509 显示习惯一致）。
pub fn cert_serial_hex(der: &[u8]) -> String {
    match x509_parser::parse_x509_certificate(der) {
        Ok((_, cert)) => hex::encode_upper(cert.tbs_certificate.raw_serial()),
        Err(_) => String::new(),
    }
}

/// 证书 notAfter（RFC3339 UTC）。
fn cert_not_after_rfc3339(der: &[u8]) -> String {
    match x509_parser::parse_x509_certificate(der) {
        Ok((_, cert)) => rfc3339_from_unix(cert.validity().not_after.timestamp()),
        Err(_) => String::new(),
    }
}

// ============================================
// CA
// ============================================

/// 本地 CA（证书公开，私钥只在磁盘 0600 文件里，按需读入）。
#[derive(Debug, Clone)]
pub struct LocalCa {
    pub root: PathBuf,
    pub cert_pem: String,
    pub cert_der: CertificateDer<'static>,
    pub fingerprint_sha256: String,
}

impl LocalCa {
    fn ca_dir(&self) -> PathBuf {
        self.root.join("ca")
    }

    /// 读入 CA 私钥并重建 `Issuer`（DN / key usage / key id method 从 CA 证书反解）。
    pub fn issuer(&self) -> Result<Issuer<'static, KeyPair>, PkiError> {
        let key_path = self.ca_dir().join("ca.key");
        let key_pem = read_text(&key_path)?;
        let key = KeyPair::from_pem(&key_pem).map_err(|e| PkiError::rcgen("ca key", e))?;
        Issuer::from_ca_cert_der(&self.cert_der, key).map_err(|e| PkiError::rcgen("ca issuer", e))
    }
}

/// 读取已有 CA（不存在返回 Ok(None)）。
pub fn load_ca(root: &Path) -> Result<Option<LocalCa>, PkiError> {
    let crt = root.join("ca").join("ca.crt");
    if !crt.exists() {
        return Ok(None);
    }
    let cert_pem = read_text(&crt)?;
    let Some(der) = pem_decode(&cert_pem, "CERTIFICATE") else {
        return Err(PkiError::new(
            "E_PKI_CA_LOAD",
            format!("{} 不是合法 PEM 证书", crt.display()),
        ));
    };
    Ok(Some(LocalCa {
        root: root.to_path_buf(),
        cert_pem,
        cert_der: CertificateDer::from(der.clone()),
        fingerprint_sha256: sha256_hex(&der),
    }))
}

/// 确保 CA 存在（缺失则生成自签 CA：ECDSA P-256，10 年）。
pub fn ensure_ca(root: &Path) -> Result<LocalCa, PkiError> {
    let _guard = pki_write_guard();
    ensure_ca_locked(root)
}

fn ensure_ca_locked(root: &Path) -> Result<LocalCa, PkiError> {
    if let Some(ca) = load_ca(root)? {
        return Ok(ca);
    }
    ensure_dir(&root.join("ca"))?;

    let mut params = CertificateParams::new(Vec::<String>::new())
        .map_err(|e| PkiError::rcgen("ca params", e))?;
    params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    params.distinguished_name = rcgen::DistinguishedName::new();
    params
        .distinguished_name
        .push(DnType::OrganizationName, "CallWarden Local CA");
    params
        .distinguished_name
        .push(DnType::CommonName, "CallWarden Local CA");
    params.key_usages = vec![
        KeyUsagePurpose::KeyCertSign,
        KeyUsagePurpose::CrlSign,
        KeyUsagePurpose::DigitalSignature,
    ];
    let now = now_unix();
    set_validity(&mut params, now - 3600, now + CA_VALIDITY_SECS);

    let key = KeyPair::generate().map_err(|e| PkiError::rcgen("ca keygen", e))?;
    let cert = params
        .self_signed(&key)
        .map_err(|e| PkiError::rcgen("ca self-signed", e))?;
    let cert_der = cert.der().clone();
    let cert_pem = pem_encode("CERTIFICATE", cert_der.as_ref());
    let key_der = key.serialize_der();
    let key_pem = pem_encode("PRIVATE KEY", &key_der);
    let fingerprint = sha256_hex(cert_der.as_ref());

    write_file(&root.join("ca").join("ca.key"), &key_pem, true)?;
    write_file(&root.join("ca").join("ca.crt"), &cert_pem, false)?;
    let meta = serde_json::json!({
        "subject": "O=CallWarden Local CA",
        "serial": cert_serial_hex(cert_der.as_ref()),
        "not_after": cert_not_after_rfc3339(cert_der.as_ref()),
        "fingerprint_sha256": fingerprint,
    });
    write_file(
        &root.join("ca").join("ca.meta.json"),
        &serde_json::to_string_pretty(&meta).unwrap_or_default(),
        true,
    )?;

    Ok(LocalCa {
        root: root.to_path_buf(),
        cert_pem,
        cert_der,
        fingerprint_sha256: fingerprint,
    })
}

/// Unix 秒 → (年, 月, 日)。有效期粒度到天，避免引入额外时间 crate。
fn civil_date(secs: i64) -> (i32, u8, u8) {
    let s = rfc3339_from_unix(secs);
    (
        s[0..4].parse().unwrap_or(1970),
        s[5..7].parse().unwrap_or(1),
        s[8..10].parse().unwrap_or(1),
    )
}

/// 设置证书有效期（`from`/`to` 为 Unix 秒，粒度到天）。
fn set_validity(params: &mut CertificateParams, from: i64, to: i64) {
    let (y, m, d) = civil_date(from);
    params.not_before = rcgen::date_time_ymd(y, m, d);
    let (y, m, d) = civil_date(to);
    params.not_after = rcgen::date_time_ymd(y, m, d);
}

// ============================================
// 服务端叶证书
// ============================================

/// 服务端叶证书材料 + 冻结进 manifest 的公开信息。
#[derive(Debug, Clone)]
pub struct ServerCertificate {
    pub cert_pem: String,
    pub key_pem: String,
    pub cert_der: CertificateDer<'static>,
    pub sha256: String,
    pub not_after: String,
    pub san: Vec<String>,
}

/// 实际绑定地址要求 SAN 至少覆盖的集合（ADR §4.1 / §15.4）。
pub fn required_server_sans(bind: &SocketAddr) -> Vec<String> {
    let mut v = vec![
        "DNS:localhost".to_string(),
        "IP:127.0.0.1".to_string(),
        "IP:::1".to_string(),
        format!("IP:{}", bind.ip()),
    ];
    if let Some(h) = host_name() {
        v.push(format!("DNS:{}", h));
    }
    v.sort();
    v.dedup();
    v
}

fn host_name() -> Option<String> {
    std::env::var("COMPUTERNAME")
        .ok()
        .or_else(|| std::env::var("HOSTNAME").ok())
        .filter(|s| !s.is_empty() && !s.contains(' '))
}

/// 确保服务端叶证书存在且 SAN 覆盖绑定地址；不覆盖 → `E_TLS_SAN_MISMATCH`。
pub fn ensure_server_cert(
    ca: &LocalCa,
    bind: &SocketAddr,
) -> Result<ServerCertificate, PkiError> {
    let _guard = pki_write_guard();
    ensure_server_cert_locked(ca, bind)
}

fn ensure_server_cert_locked(
    ca: &LocalCa,
    bind: &SocketAddr,
) -> Result<ServerCertificate, PkiError> {
    let dir = ca.root.join("server");
    let crt_path = dir.join("server.crt");
    let key_path = dir.join("server.key");
    let required = required_server_sans(bind);

    if crt_path.exists() && key_path.exists() {
        let cert_pem = read_text(&crt_path)?;
        let key_pem = read_text(&key_path)?;
        let der = pem_decode(&cert_pem, "CERTIFICATE").ok_or_else(|| {
            PkiError::new("E_PKI_CA_LOAD", "server.crt 不是合法 PEM 证书")
        })?;
        let san = certificate_sans(&der);
        let missing: Vec<&String> = required.iter().filter(|r| !san.contains(r)).collect();
        if !missing.is_empty() {
            return Err(PkiError::new(
                "E_TLS_SAN_MISMATCH",
                format!(
                    "server.crt SAN 未覆盖绑定地址，缺失 {:?}（现有 {:?}）",
                    missing, san
                ),
            ));
        }
        let cert_der = CertificateDer::from(der);
        return Ok(ServerCertificate {
            cert_pem,
            key_pem,
            sha256: sha256_hex(cert_der.as_ref()),
            not_after: cert_not_after_rfc3339(cert_der.as_ref()),
            san,
            cert_der,
        });
    }

    ensure_dir(&dir)?;
    let mut params = CertificateParams::new(Vec::<String>::new())
        .map_err(|e| PkiError::rcgen("server params", e))?;
    params.distinguished_name = rcgen::DistinguishedName::new();
    params
        .distinguished_name
        .push(DnType::OrganizationName, "CallWarden Daemon");
    params
        .distinguished_name
        .push(DnType::CommonName, "CallWarden Daemon");
    params.is_ca = IsCa::NoCa;
    params.key_usages = vec![
        KeyUsagePurpose::DigitalSignature,
        KeyUsagePurpose::KeyEncipherment,
    ];
    params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ServerAuth];
    params.subject_alt_names = required.iter().filter_map(|s| san_type_from_string(s)).collect();
    let now = now_unix();
    set_validity(&mut params, now - 3600, now + SERVER_VALIDITY_SECS);

    let key = KeyPair::generate().map_err(|e| PkiError::rcgen("server keygen", e))?;
    let issuer = ca.issuer()?;
    let cert = params
        .signed_by(&key, &issuer)
        .map_err(|e| PkiError::rcgen("server signed_by", e))?;
    let cert_der = cert.der().clone();
    let cert_pem = pem_encode("CERTIFICATE", cert_der.as_ref());
    let key_pem = pem_encode("PRIVATE KEY", &key.serialize_der());

    write_file(&key_path, &key_pem, true)?;
    write_file(&crt_path, &cert_pem, false)?;

    let san = certificate_sans(cert_der.as_ref());
    Ok(ServerCertificate {
        cert_pem,
        key_pem,
        sha256: sha256_hex(cert_der.as_ref()),
        not_after: cert_not_after_rfc3339(cert_der.as_ref()),
        san,
        cert_der,
    })
}

fn san_type_from_string(s: &str) -> Option<SanType> {
    if let Some(ip) = s.strip_prefix("IP:") {
        ip.parse::<IpAddr>().ok().map(SanType::IpAddress)
    } else if let Some(dns) = s.strip_prefix("DNS:") {
        Some(SanType::DnsName(dns.try_into().ok()?))
    } else if let Some(uri) = s.strip_prefix("URI:") {
        Some(SanType::URI(uri.try_into().ok()?))
    } else {
        None
    }
}

/// 用 CA 信任根 + 服务端叶证书构造 rustls `ServerConfig`。
///
/// 客户端证书验证器 `allow_unauthenticated()`：握手阶段允许无客户端证书
/// （`/health` 探活与 `/v1/enroll` 引导需要），其余路由在 HTTP 层要求
/// peer cert（缺失 → 403 `E_MTLS_CLIENT_CERT_MISSING`）。带证书但校验失败
/// （未知 CA / 过期）→ 握手直接失败（`E_TLS_HANDSHAKE_FAILED`）。
pub fn build_tls_config(
    ca: &LocalCa,
    server: &ServerCertificate,
) -> Result<Arc<ServerConfig>, PkiError> {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let mut roots = RootCertStore::empty();
    roots.add(ca.cert_der.clone()).map_err(|e| {
        PkiError::new("E_PKI_CA_LOAD", format!("信任根装载失败: {}", e))
    })?;
    let verifier = WebPkiClientVerifier::builder_with_provider(Arc::new(roots), provider.clone())
        .allow_unauthenticated()
        .build()
        .map_err(|e| PkiError::new("E_PKI_CA_LOAD", format!("client verifier: {}", e)))?;

    let key_der = pem_decode(&server.key_pem, "PRIVATE KEY").ok_or_else(|| {
        PkiError::new("E_PKI_CA_LOAD", "server.key 不是合法 PEM 私钥")
    })?;
    let key = PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(key_der));

    let mut cfg = ServerConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map_err(|e| PkiError::new("E_TLS_CONFIG", format!("protocol versions: {}", e)))?
        .with_client_cert_verifier(verifier)
        .with_single_cert(vec![server.cert_der.clone()], key)
        .map_err(|e| PkiError::new("E_TLS_CONFIG", format!("single cert: {}", e)))?;
    cfg.alpn_protocols = vec![b"http/1.1".to_vec()];
    Ok(Arc::new(cfg))
}

/// 生成一个 TLS 客户端配置（信任 pin 的 CA + 携带客户端证书）。
///
/// daemon 侧测试与本地工具复用；生产 Python 客户端由 `ssl.SSLContext` 承担。
pub fn client_tls_config(
    ca_pem: &str,
    client_cert_pem: &str,
    client_key_pem: &str,
) -> Result<Arc<rustls::ClientConfig>, PkiError> {
    let ca_der = pem_decode(ca_pem, "CERTIFICATE")
        .ok_or_else(|| PkiError::new("E_PKI_CA_LOAD", "CA PEM 解析失败"))?;
    let cert_der = pem_decode(client_cert_pem, "CERTIFICATE")
        .ok_or_else(|| PkiError::new("E_PKI_CA_LOAD", "client.crt 解析失败"))?;
    let key_der = pem_decode(client_key_pem, "PRIVATE KEY")
        .ok_or_else(|| PkiError::new("E_PKI_CA_LOAD", "client.key 解析失败"))?;

    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let mut roots = RootCertStore::empty();
    roots
        .add(CertificateDer::from(ca_der))
        .map_err(|e| PkiError::new("E_PKI_CA_LOAD", format!("信任根装载失败: {}", e)))?;
    let cfg = rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map_err(|e| PkiError::new("E_TLS_CONFIG", format!("protocol versions: {}", e)))?
        .with_root_certificates(roots)
        .with_client_auth_cert(
            vec![CertificateDer::from(cert_der)],
            PrivateKeyDer::Pkcs8(PrivatePkcs8KeyDer::from(key_der)),
        )
        .map_err(|e| PkiError::new("E_TLS_CONFIG", format!("client auth cert: {}", e)))?;
    Ok(Arc::new(cfg))
}

// ============================================
// 客户端身份签发
// ============================================

/// 客户端签发结果（`client.key` 永不落 daemon 磁盘：私钥留在客户端）。
#[derive(Debug, Clone)]
pub struct IssuedClientCertificate {
    pub principal_id: String,
    pub cert_pem: String,
    pub ca_pem: String,
    pub serial: String,
    pub not_after: String,
    pub cert_sha256: String,
}

/// 用 CA 给 CSR 签发客户端叶证书（SAN/EKU/有效期/DN 全部由 daemon 覆盖）。
///
/// CSR 只贡献公钥与签名校验结果——请求方不能自选 SAN、EKU 或有效期，
/// 避免“自签任意主体”的越权路径（ADR §5.1）。
pub fn issue_client_certificate(
    ca: &LocalCa,
    principal_id: &str,
    csr_pem: &str,
) -> Result<IssuedClientCertificate, PkiError> {
    let csr = CertificateSigningRequestParams::from_pem(csr_pem)
        .map_err(|e| PkiError::new("E_ENROLL_CSR_INVALID", format!("CSR 解析/验签失败: {}", e)))?;

    let mut params = csr.params;
    params.is_ca = IsCa::NoCa;
    params.distinguished_name = rcgen::DistinguishedName::new();
    params
        .distinguished_name
        .push(DnType::OrganizationName, "CallWarden Client");
    params
        .distinguished_name
        .push(DnType::CommonName, principal_id);
    params.subject_alt_names = vec![SanType::URI({
        format!("{}{}", PRINCIPAL_URI_PREFIX, principal_id)
            .try_into()
            .map_err(|_| PkiError::new("E_ENROLL_CSR_INVALID", "principal_id 含非法字符"))?
    })];
    params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ClientAuth];
    params.key_usages = vec![
        KeyUsagePurpose::DigitalSignature,
        KeyUsagePurpose::KeyEncipherment,
    ];
    let now = now_unix();
    set_validity(&mut params, now - 3600, now + CLIENT_VALIDITY_SECS);

    let issuer = ca.issuer()?;
    let cert = params
        .signed_by(&csr.public_key, &issuer)
        .map_err(|e| PkiError::rcgen("client signed_by", e))?;
    let der = cert.der().clone();
    let cert_pem = pem_encode("CERTIFICATE", der.as_ref());

    let id_dir = ca.root.join("identities").join(principal_id);
    ensure_dir(&id_dir)?;
    write_file(&id_dir.join("client.crt"), &cert_pem, false)?;
    let meta = serde_json::json!({
        "principal_id": principal_id,
        "kind": "agent",
        "serial": cert_serial_hex(der.as_ref()),
        "issued_at": rfc3339_from_unix(now),
        "not_after": cert_not_after_rfc3339(der.as_ref()),
        "cert_sha256": sha256_hex(der.as_ref()),
    });
    write_file(
        &id_dir.join("meta.json"),
        &serde_json::to_string_pretty(&meta).unwrap_or_default(),
        false,
    )?;

    Ok(IssuedClientCertificate {
        principal_id: principal_id.to_string(),
        cert_pem,
        ca_pem: ca.cert_pem.clone(),
        serial: cert_serial_hex(der.as_ref()),
        not_after: cert_not_after_rfc3339(der.as_ref()),
        cert_sha256: sha256_hex(der.as_ref()),
    })
}

/// 本地直发客户端身份（daemon 内置工具/测试用）：生成密钥对 + 经 CA 签发。
pub fn issue_local_client_identity(
    ca: &LocalCa,
    principal_id: &str,
) -> Result<(String, String), PkiError> {
    let key = KeyPair::generate().map_err(|e| PkiError::rcgen("client keygen", e))?;
    let key_pem = pem_encode("PRIVATE KEY", &key.serialize_der());

    let mut params = CertificateParams::new(Vec::<String>::new())
        .map_err(|e| PkiError::rcgen("client params", e))?;
    params.distinguished_name = rcgen::DistinguishedName::new();
    params
        .distinguished_name
        .push(DnType::OrganizationName, "CallWarden Client");
    params
        .distinguished_name
        .push(DnType::CommonName, principal_id);
    params.is_ca = IsCa::NoCa;
    params.key_usages = vec![
        KeyUsagePurpose::DigitalSignature,
        KeyUsagePurpose::KeyEncipherment,
    ];
    params.extended_key_usages = vec![ExtendedKeyUsagePurpose::ClientAuth];
    let uri: SanType = SanType::URI(
        format!("{}{}", PRINCIPAL_URI_PREFIX, principal_id)
            .try_into()
            .map_err(|_| PkiError::new("E_PKI_GENERATE", "principal_id 含非法字符"))?,
    );
    params.subject_alt_names = vec![uri];
    let now = now_unix();
    set_validity(&mut params, now - 3600, now + CLIENT_VALIDITY_SECS);

    let issuer = ca.issuer()?;
    let cert = params
        .signed_by(&key, &issuer)
        .map_err(|e| PkiError::rcgen("client signed_by", e))?;
    let der = cert.der().clone();
    let cert_pem = pem_encode("CERTIFICATE", der.as_ref());

    // 本地引导身份 = daemon owner（`kind=service`，workspace 不限）。
    // auth_principal 以 identities/<id>/meta.json 作为签发台账（ADR §5.1）。
    let id_dir = ca.root.join("identities").join(principal_id);
    ensure_dir(&id_dir)?;
    write_file(&id_dir.join("client.crt"), &cert_pem, false)?;
    let meta = serde_json::json!({
        "principal_id": principal_id,
        "kind": "service",
        "serial": cert_serial_hex(der.as_ref()),
        "issued_at": rfc3339_from_unix(now),
        "not_after": cert_not_after_rfc3339(der.as_ref()),
        "cert_sha256": sha256_hex(der.as_ref()),
    });
    write_file(
        &id_dir.join("meta.json"),
        &serde_json::to_string_pretty(&meta).unwrap_or_default(),
        false,
    )?;
    Ok((cert_pem, key_pem))
}

/// 生成新的 `principal_id`（`pr_` + 32 位小写 hex，ADR §4.1）。
pub fn new_principal_id() -> Result<String, PkiError> {
    let mut buf = [0u8; 16];
    fill_random(&mut buf)?;
    Ok(format!("pr_{}", hex::encode(buf)))
}

fn fill_random(buf: &mut [u8]) -> Result<(), PkiError> {
    use ring::rand::SecureRandom;
    let rng = ring::rand::SystemRandom::new();
    rng.fill(buf)
        .map_err(|_| PkiError::new("E_PKI_RNG", "CSPRNG 不可用"))
}

fn sha256_hex_str(s: &str) -> String {
    sha256_hex(s.as_bytes())
}

// ============================================
// 引导 token
// ============================================

/// 引导 token 记录（磁盘文件为权威，DB 台账推迟到 S1 step 3）。
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
pub struct EnrollTokenRecord {
    pub token_id: String,
    pub principal_id: String,
    pub workspace_id: String,
    pub issued_at: u64,
    pub expires_at: u64,
}

/// 发放单次引导 token，返回 `(<token_id>.<secret>, 记录)`。
pub fn issue_enroll_token(
    ca: &LocalCa,
    principal_id: &str,
    workspace_id: &str,
) -> Result<(String, EnrollTokenRecord), PkiError> {
    let mut id_buf = [0u8; 12];
    let mut secret_buf = [0u8; 32];
    fill_random(&mut id_buf)?;
    fill_random(&mut secret_buf)?;
    let token_id = hex::encode(id_buf);
    let secret = hex::encode(secret_buf);
    let now = now_unix() as u64;
    let rec = EnrollTokenRecord {
        token_id: token_id.clone(),
        principal_id: principal_id.to_string(),
        workspace_id: workspace_id.to_string(),
        issued_at: now,
        expires_at: now + ENROLL_TOKEN_TTL_SECS,
    };
    let payload = serde_json::json!({
        "token_id": rec.token_id,
        "secret_sha256": sha256_hex_str(&secret),
        "principal_id": rec.principal_id,
        "workspace_id": rec.workspace_id,
        "issued_at": rec.issued_at,
        "expires_at": rec.expires_at,
    });
    let dir = ca.root.join("bootstrap");
    ensure_dir(&dir)?;
    write_file(
        &dir.join(format!("{}.json", token_id)),
        &serde_json::to_string_pretty(&payload).unwrap_or_default(),
        true,
    )?;
    Ok((format!("{}.{}", token_id, secret), rec))
}

/// 原子消费 token：校验未过期 / secret 匹配后 rename 为 `.consumed.json`。
///
/// 并发消费由 rename 的原子性裁决（同一源文件只允许一次成功 rename）。
/// 对外（HTTP 层）所有失败统一 `E_ENROLL_TOKEN_INVALID`，不区分原因。
pub fn consume_enroll_token(ca: &LocalCa, token: &str) -> Result<EnrollTokenRecord, PkiError> {
    let invalid = || PkiError::new("E_ENROLL_TOKEN_INVALID", "token 无效");
    let (token_id, secret) = token.split_once('.').ok_or_else(invalid)?;
    if token_id.is_empty() || secret.is_empty() {
        return Err(invalid());
    }
    let dir = ca.root.join("bootstrap");
    let path = dir.join(format!("{}.json", token_id));
    let text = read_text(&path).map_err(|_| invalid())?;
    let v: serde_json::Value = serde_json::from_str(&text).map_err(|_| invalid())?;
    let expected = v
        .get("secret_sha256")
        .and_then(|s| s.as_str())
        .ok_or_else(invalid)?;
    if expected != sha256_hex_str(secret) {
        return Err(invalid());
    }
    let expires_at = v
        .get("expires_at")
        .and_then(|n| n.as_u64())
        .ok_or_else(invalid)?;
    if (now_unix() as u64) > expires_at {
        return Err(PkiError::new("E_ENROLL_TOKEN_EXPIRED", "token 已过期"));
    }

    // 原子消费：rename 成功者独占 token。
    let consumed = dir.join(format!("{}.consumed.json", token_id));
    std::fs::rename(&path, &consumed)
        .map_err(|_| PkiError::new("E_ENROLL_TOKEN_USED", "token 已被消费"))?;

    Ok(EnrollTokenRecord {
        token_id: token_id.to_string(),
        principal_id: v
            .get("principal_id")
            .and_then(|s| s.as_str())
            .unwrap_or_default()
            .to_string(),
        workspace_id: v
            .get("workspace_id")
            .and_then(|s| s.as_str())
            .unwrap_or_default()
            .to_string(),
        issued_at: v.get("issued_at").and_then(|n| n.as_u64()).unwrap_or(0),
        expires_at,
    })
}
