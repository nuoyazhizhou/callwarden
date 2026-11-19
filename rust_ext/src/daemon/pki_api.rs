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
/// 服务端证书自动续期阈值（天）：剩余有效期 ≤ 该值时 `ensure_server_cert`
/// 启动路径自动重签（§9.34 阶段 A1）。过期自然落入该判定。
pub const SERVER_CERT_RENEW_THRESHOLD_DAYS: i64 = 30;
/// 客户端叶证书有效期（90 天）。
const CLIENT_VALIDITY_SECS: i64 = 90 * 24 * 3600;
/// 引导 token TTL（ADR §4.2：600s）。
pub const ENROLL_TOKEN_TTL_SECS: u64 = 600;

/// §9.35 TLS 热加载哨兵文件名（位于 PKI 根目录下）。
///
/// 「磁盘即协议」先例（rotate-ca 的目录 rename + window.json 同款）：
/// `cw pki rotate-server` / `rotate-ca prepare/commit` 收尾写入
/// `pki/tls_reload.request`，daemon watcher 检测到后热加载并删除。
/// daemon 未运行时哨兵保留，下次启动首轮自愈（幂等）。
pub const TLS_RELOAD_REQUEST_FILE: &str = "tls_reload.request";

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

    /// 跨模块复用的 io 包装（§9.34 C rotate-ca CLI 需要）。
    pub fn io_pub(ctx: &str, e: std::io::Error) -> Self {
        Self::io(ctx, e)
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
    pem_decode_all(text, label).into_iter().next()
}

/// 解析 PEM 文本中**全部**同名标签块（§9.34 C：manifest `ca_cert_pem` 在
/// rotate-ca 窗口期为 新 CA + 旧 CA 的 PEM bundle，消费端须逐块装载）。
pub fn pem_decode_all(text: &str, label: &str) -> Vec<Vec<u8>> {
    let begin = format!("-----BEGIN {}-----", label);
    let end = format!("-----END {}-----", label);
    let mut out = Vec::new();
    let mut rest = text;
    while let Some(pos) = rest.find(&begin) {
        let start = pos + begin.len();
        let Some(stop_rel) = rest[start..].find(&end) else {
            break;
        };
        let stop = start + stop_rel;
        let body: String =
            rest[start..stop].chars().filter(|c| !c.is_whitespace()).collect();
        if let Ok(der) = base64::engine::general_purpose::STANDARD.decode(&body) {
            out.push(der);
        }
        rest = &rest[stop + end.len()..];
    }
    out
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

/// 跨模块复用的进程内 PKI 写入锁（§9.34 C rotate-ca prepare/commit 需要）。
pub fn pki_write_lock() -> std::sync::MutexGuard<'static, ()> {
    pki_write_guard()
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

pub fn sha256_hex(bytes: &[u8]) -> String {
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

/// 本地 CA（证书公开，私钥只在磁盘 0600 文件或外部文件里，按需读入）。
#[derive(Debug, Clone)]
pub struct LocalCa {
    pub root: PathBuf,
    /// CA 来源（§9.36）：内置 `ca/` 或外部 env 文件。
    pub source: CaSource,
    /// CA 私钥路径：内置模式为 `ca/ca.key`；外部模式为 `CW_CA_KEY` 指向的
    /// 外部文件（daemon 只读，绝不改写）。
    pub key_path: PathBuf,
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
        let key_pem = read_text(&self.key_path)?;
        let key = KeyPair::from_pem(&key_pem).map_err(|e| PkiError::rcgen("ca key", e))?;
        Issuer::from_ca_cert_der(&self.cert_der, key).map_err(|e| PkiError::rcgen("ca issuer", e))
    }
}

/// CA 来源（§9.36）。
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum CaSource {
    /// 内置自签 CA（`ca/` 目录，daemon 全权管理）。
    Internal,
    /// 外部导入 CA（`CW_CA_CERT`/`CW_CA_KEY` 指向的外部文件，daemon 只读）。
    External,
}

/// 外部 CA 配置解析（§9.36）：纯逻辑——trim、空串视为未设置、
/// **只设其一 → fail-closed**（半配置是典型误操作，静默回退内置 CA 会造成
/// "签发 CA 与预期不符"）。env 读取与校验分离，便于免 env 测试。
fn external_ca_from_values(
    cert: Option<std::ffi::OsString>,
    key: Option<std::ffi::OsString>,
) -> Result<Option<(PathBuf, PathBuf)>, PkiError> {
    let cert = cert.as_deref().map(|s| s.to_string_lossy().to_string());
    let key = key.as_deref().map(|s| s.to_string_lossy().to_string());
    let cert = cert.as_deref().map(str::trim).filter(|s| !s.is_empty());
    let key = key.as_deref().map(str::trim).filter(|s| !s.is_empty());
    match (cert, key) {
        (Some(c), Some(k)) => Ok(Some((PathBuf::from(c), PathBuf::from(k)))),
        (None, None) => Ok(None),
        (Some(_), None) => Err(PkiError::new(
            "E_PKI_CA_LOAD",
            "CW_CA_CERT 与 CW_CA_KEY 必须同时设置（外部 CA 模式）；当前只设置了 CW_CA_CERT",
        )),
        (None, Some(_)) => Err(PkiError::new(
            "E_PKI_CA_LOAD",
            "CW_CA_CERT 与 CW_CA_KEY 必须同时设置（外部 CA 模式）；当前只设置了 CW_CA_KEY",
        )),
    }
}

thread_local! {
    /// 测试构建专用：注入外部 CA 配置（`Some(None)` 强制内置、`Some(Some(paths))`
    /// 强制外部）。线程局部 → 测试并行互不污染，且与宿主机真实 env 解耦。
    static EXTERNAL_CA_TEST_OVERRIDE: std::cell::RefCell<Option<Option<(PathBuf, PathBuf)>>> =
        const { std::cell::RefCell::new(None) };
}

/// 测试专用：注入外部 CA 配置，Drop 时自动清除（panic 安全）。
#[cfg(test)]
pub(crate) struct ExternalCaOverrideGuard;

#[cfg(test)]
impl ExternalCaOverrideGuard {
    pub(crate) fn inject(injected: Option<(PathBuf, PathBuf)>) -> Self {
        EXTERNAL_CA_TEST_OVERRIDE.with(|c| *c.borrow_mut() = Some(injected));
        Self
    }
}

#[cfg(test)]
impl Drop for ExternalCaOverrideGuard {
    fn drop(&mut self) {
        EXTERNAL_CA_TEST_OVERRIDE.with(|c| *c.borrow_mut() = None);
    }
}

/// 当前是否启用外部 CA 模式（测试构建走线程局部注入，生产构建读 env）。
pub fn external_ca_paths() -> Result<Option<(PathBuf, PathBuf)>, PkiError> {
    #[cfg(test)]
    {
        if let Some(injected) =
            EXTERNAL_CA_TEST_OVERRIDE.with(|c| c.borrow().clone())
        {
            return Ok(injected);
        }
        // 测试构建未注入 → 视为未配置（内置 CA），保证测试与宿主机 env 解耦。
        return Ok(None);
    }
    #[cfg(not(test))]
    {
        external_ca_from_values(
            std::env::var_os("CW_CA_CERT"),
            std::env::var_os("CW_CA_KEY"),
        )
    }
}

/// 证书 Subject Key Identifier（CA 身份比对基准，§9.36 ensure_server_cert
/// issuer 归属检查复用；cli/pki.rs rotate-ca status 同源）。
pub fn cert_subject_key_id(der: &[u8]) -> Option<Vec<u8>> {
    let (_, cert) = x509_parser::parse_x509_certificate(der).ok()?;
    for ext in cert.extensions() {
        if let x509_parser::extensions::ParsedExtension::SubjectKeyIdentifier(kid) =
            ext.parsed_extension()
        {
            return Some(kid.0.to_vec());
        }
    }
    None
}

/// 证书 Authority Key Identifier（签发方身份比对基准）。
pub fn cert_authority_key_id(der: &[u8]) -> Option<Vec<u8>> {
    let (_, cert) = x509_parser::parse_x509_certificate(der).ok()?;
    for ext in cert.extensions() {
        if let x509_parser::extensions::ParsedExtension::AuthorityKeyIdentifier(aki) =
            ext.parsed_extension()
        {
            return aki.key_identifier.as_ref().map(|k| k.0.to_vec());
        }
    }
    None
}

/// 证书 SPKI 公钥原始字节（外部 CA 的 cert/key 匹配校验用）。
fn spki_public_key_bytes(der: &[u8]) -> Option<Vec<u8>> {
    let (_, cert) = x509_parser::parse_x509_certificate(der).ok()?;
    Some(cert.public_key().subject_public_key.data.to_vec())
}

/// 校验外部 CA 证书可用于签发（§9.36 fail-closed）：
/// 单张 CERTIFICATE 块、BasicConstraints CA=true、KeyUsage 含 keyCertSign、
/// 在有效期内。任一不满足 → `E_PKI_CA_LOAD`。
fn validate_external_ca_cert(der: &[u8], source: &Path) -> Result<(), PkiError> {
    let (_, cert) = x509_parser::parse_x509_certificate(der).map_err(|e| {
        PkiError::new(
            "E_PKI_CA_LOAD",
            format!("外部 CA {} 解析失败: {}", source.display(), e),
        )
    })?;
    let is_ca = cert
        .basic_constraints()
        .ok()
        .flatten()
        .map(|bc| bc.value.ca)
        .unwrap_or(false);
    if !is_ca {
        return Err(PkiError::new(
            "E_PKI_CA_LOAD",
            format!("外部 CA {} 不是 CA 证书（BasicConstraints CA=false）", source.display()),
        ));
    }
    let key_cert_sign = cert
        .key_usage()
        .ok()
        .flatten()
        .map(|ku| ku.value.key_cert_sign())
        .unwrap_or(false);
    if !key_cert_sign {
        return Err(PkiError::new(
            "E_PKI_CA_LOAD",
            format!("外部 CA {} 缺少 keyCertSign 用法，不能签发证书", source.display()),
        ));
    }
    let now = now_unix();
    if cert.validity().not_before.timestamp() > now {
        return Err(PkiError::new(
            "E_PKI_CA_LOAD",
            format!("外部 CA {} 尚未生效（not_before 在未来）", source.display()),
        ));
    }
    if cert.validity().not_after.timestamp() < now {
        return Err(PkiError::new(
            "E_PKI_CA_LOAD",
            format!("外部 CA {} 已过期（not_after {}）", source.display(), cert.validity().not_after),
        ));
    }
    Ok(())
}

/// 读取已有 CA（不存在返回 Ok(None)）。
///
/// §9.36 外部 CA 模式（`CW_CA_CERT`+`CW_CA_KEY` 同时设置）：env 优先——
/// 外部文件即唯一签发 CA，`ca/` 目录被完全忽略；外部文件缺失/非法 →
/// `E_PKI_CA_LOAD` fail-closed，**绝不自签兜底**（签发 CA 与预期不符比不可用
/// 更危险）。外部模式下的磁盘共存检查：
/// - `ca-previous/` 存在 → 拒绝（先 `rotate-ca --action commit` 收敛内置窗口）；
/// - `ca/ca.crt` 存在且指纹 ≠ 外部证书 → 拒绝（内置→外部的模式切换须显式
///   迁移：收敛窗口后移除/归档内置 `ca/`，客户端重新 enroll）。
pub fn load_ca(root: &Path) -> Result<Option<LocalCa>, PkiError> {
    load_ca_with_external(root, external_ca_paths()?)
}

/// `load_ca` 的参数化核心（外部 CA 路径由调用方注入；测试用它避免进程级
/// env 污染，生产路径由 `load_ca` 读 env 后传入）。
fn load_ca_with_external(
    root: &Path,
    external: Option<(PathBuf, PathBuf)>,
) -> Result<Option<LocalCa>, PkiError> {
    if let Some((cert_path, key_path)) = external {
        if root.join("ca-previous").join("ca.crt").exists() {
            return Err(PkiError::new(
                "E_PKI_CA_LOAD",
                "外部 CA 模式与 rotate-ca 双根窗口不兼容：先 `cw pki rotate-ca --action commit` 收敛内置 CA 窗口",
            ));
        }
        let cert_pem = read_text(&cert_path).map_err(|e| {
            PkiError::new("E_PKI_CA_LOAD", format!("外部 CA 证书不可读: {}", e))
        })?;
        let blocks = pem_decode_all(&cert_pem, "CERTIFICATE");
        if blocks.len() != 1 {
            return Err(PkiError::new(
                "E_PKI_CA_LOAD",
                format!(
                    "外部 CA {} 须包含且仅包含 1 张 CERTIFICATE 块（实际 {} 张）",
                    cert_path.display(),
                    blocks.len()
                ),
            ));
        }
        let der = blocks.into_iter().next().expect("blocks 非空");
        validate_external_ca_cert(&der, &cert_path)?;
        // cert/key 匹配校验：私钥公钥必须等于证书 SPKI（误配对是典型运维错误，
        // 不在加载期拦截就会在首次签发时才以费解的签名错误暴露）。
        let key_pem = read_text(&key_path).map_err(|e| {
            PkiError::new("E_PKI_CA_LOAD", format!("外部 CA 私钥不可读: {}", e))
        })?;
        let key = KeyPair::from_pem(&key_pem)
            .map_err(|e| PkiError::new("E_PKI_CA_LOAD", format!("外部 CA 私钥 {} 解析失败: {}", key_path.display(), e)))?;
        let spki = spki_public_key_bytes(&der).ok_or_else(|| {
            PkiError::new("E_PKI_CA_LOAD", format!("外部 CA {} SPKI 解析失败", cert_path.display()))
        })?;
        if key.public_key_raw() != spki.as_slice() {
            return Err(PkiError::new(
                "E_PKI_CA_LOAD",
                format!(
                    "外部 CA 私钥 {} 与证书 {} 不匹配（公钥不一致）",
                    key_path.display(),
                    cert_path.display()
                ),
            ));
        }
        // 磁盘内置 CA 共存检查：指纹相同（如曾导出复制）→ 兼容放行；
        // 不同 → 显式迁移，拒绝静默切换签发 CA。
        let internal_crt = root.join("ca").join("ca.crt");
        if internal_crt.exists() {
            let internal_pem = read_text(&internal_crt)?;
            if let Some(internal_der) = pem_decode(&internal_pem, "CERTIFICATE") {
                if sha256_hex(&internal_der) != sha256_hex(&der) {
                    return Err(PkiError::new(
                        "E_PKI_CA_LOAD",
                        format!(
                            "外部 CA {} 与磁盘内置 CA {} 指纹不一致：切换签发 CA 须显式迁移（收敛 rotate-ca 窗口后移除/归档内置 ca/ 目录，客户端重新 enroll）",
                            cert_path.display(),
                            internal_crt.display()
                        ),
                    ));
                }
            }
        }
        return Ok(Some(LocalCa {
            root: root.to_path_buf(),
            source: CaSource::External,
            key_path,
            cert_pem,
            cert_der: CertificateDer::from(der.clone()),
            fingerprint_sha256: sha256_hex(&der),
        }));
    }

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
        source: CaSource::Internal,
        key_path: root.join("ca").join("ca.key"),
        cert_pem,
        cert_der: CertificateDer::from(der.clone()),
        fingerprint_sha256: sha256_hex(&der),
    }))
}

/// 旧 CA 信任根（§9.34 C rotate-ca 双 CA 窗口）。
///
/// 只承载验签材料：**不得用于签发**（签发永远走 `ca/` 当前 CA——布局保证，
/// `PreviousCa` 不暴露 `issuer()`）。`ca-previous/` 由 `cw pki rotate-ca
/// prepare` 创建、`--commit` 移除。
#[derive(Debug, Clone)]
pub struct PreviousCa {
    pub cert_pem: String,
    pub cert_der: CertificateDer<'static>,
    pub fingerprint_sha256: String,
}

/// 读取 rotate-ca 窗口中的旧 CA（`ca-previous/ca.crt`）；无窗口返回 Ok(None)。
pub fn load_previous_ca(root: &Path) -> Result<Option<PreviousCa>, PkiError> {
    let crt = root.join("ca-previous").join("ca.crt");
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
    Ok(Some(PreviousCa {
        fingerprint_sha256: sha256_hex(&der),
        cert_der: CertificateDer::from(der),
        cert_pem,
    }))
}

/// 确保 CA 存在（缺失则生成自签 CA：ECDSA P-256，10 年）。
pub fn ensure_ca(root: &Path) -> Result<LocalCa, PkiError> {
    let _guard = pki_write_guard();
    ensure_ca_locked(root)
}

/// 已持有 `pki_write_lock` 的调用方（§9.34 rotate-ca prepare）使用此变体，
/// 避免 `ensure_ca` 重入同一把非重入锁导致同线程死锁。
///
/// §9.36 外部 CA 模式：`load_ca` 的 env 分支要么成功要么 fail-closed，
/// 永不走到自签分支——外部 CA 模式下 daemon 不得生成任何内置 CA（否则
/// "导入外部 CA"会悄悄退化为内外并存）。
pub fn ensure_ca_locked(root: &Path) -> Result<LocalCa, PkiError> {
    if let Some(ca) = load_ca(root)? {
        return Ok(ca);
    }
    ensure_dir(&root.join("ca"))?;

    let mut params = CertificateParams::new(Vec::<String>::new())
        .map_err(|e| PkiError::rcgen("ca params", e))?;
    params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
    params.key_usages = vec![
        KeyUsagePurpose::KeyCertSign,
        KeyUsagePurpose::CrlSign,
        KeyUsagePurpose::DigitalSignature,
    ];
    let now = now_unix();
    set_validity(&mut params, now - 3600, now + CA_VALIDITY_SECS);

    // 先生成密钥再定 DN：CN 携带公钥指纹前缀，保证不同代 CA 的 subject DN
    // 互不相同。rotate-ca 窗口期 manifest 会同时装载 新+旧 两张 CA 证书，
    // 若两者同 DN，OpenSSL（Python `cw` CLI）按 DN 选候选信任根且不回溯，
    // 会把新 CA 签名的证书交给旧 CA 公钥验证 → certificate signature failure。
    // webpki/rustls 无此问题，但信任根 DN 必须对全部消费端可区分（§9.34）。
    let key = KeyPair::generate().map_err(|e| PkiError::rcgen("ca keygen", e))?;
    let key_fp_prefix: String = {
        let digest = Sha256::digest(key.public_key_raw());
        digest.iter().take(4).map(|b| format!("{:02x}", b)).collect()
    };
    let ca_common_name = format!("CallWarden Local CA {}", key_fp_prefix);
    params.distinguished_name = rcgen::DistinguishedName::new();
    params
        .distinguished_name
        .push(DnType::OrganizationName, "CallWarden Local CA");
    params
        .distinguished_name
        .push(DnType::CommonName, ca_common_name.clone());

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
        "subject": format!("O=CallWarden Local CA, CN={}", ca_common_name),
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
        source: CaSource::Internal,
        key_path: root.join("ca").join("ca.key"),
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

/// 确保服务端叶证书存在、SAN 覆盖绑定地址且未临期；SAN 不覆盖 →
/// `E_TLS_SAN_MISMATCH`；文件存在但剩余有效期 ≤
/// [`SERVER_CERT_RENEW_THRESHOLD_DAYS`]（含已过期）→ 自动重签（§9.34 A1）。
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
        let cert_der = CertificateDer::from(der.clone());
        // §9.36 issuer 归属检查：服务端证书必须由当前 CA 签发（AKI == CA SKI）。
        // CA 未含 SKI 时无法判定 → 跳过（避免无法判定导致每次 reload 重签churn）；
        // 叶证书缺 AKI（含解析失败）→ 视为不归属，重签一次后新证书带 AKI 即稳定。
        // 这支撑外部 CA 文件被用户整体替换后的热加载自愈：换 CA → reload →
        // 旧 server 证书 AKI 不归属新 CA → 自动以新 CA 重签。
        let ca_ski = cert_subject_key_id(ca.cert_der.as_ref());
        if ca_ski.is_some() {
            let server_aki = cert_authority_key_id(&der);
            if server_aki.as_ref() != ca_ski.as_ref() {
                eprintln!(
                    "[cw_daemon][pki] server cert not issued by current CA (AKI mismatch), reissuing"
                );
                return ensure_server_cert_reissue(ca, bind, &required);
            }
        }
        if !server_cert_needs_renewal(&der, now_unix(), SERVER_CERT_RENEW_THRESHOLD_DAYS) {
            return Ok(ServerCertificate {
                cert_pem,
                key_pem,
                sha256: sha256_hex(cert_der.as_ref()),
                not_after: cert_not_after_rfc3339(cert_der.as_ref()),
                san,
                cert_der,
            });
        }
        // §9.34 A1：临期/已过期 → 不复用，落入下方重签路径（覆盖 server.crt/key，
        // SAN 以 required 为准）。重签失败按 PkiError 正常上抛（fail-closed，
        // 不静默沿用临期证书）。
        eprintln!(
            "[cw_daemon][pki] server cert expiring/expired (<= {} days left), reissuing",
            SERVER_CERT_RENEW_THRESHOLD_DAYS
        );
    }

    ensure_server_cert_reissue(ca, bind, &required)
}

/// 强制重签服务端叶证书（覆盖 `server/`，SAN 以 `required` 为准）。
/// §9.36：临期重签、AKI 不归属重签共用此路径。
fn ensure_server_cert_reissue(
    ca: &LocalCa,
    bind: &SocketAddr,
    required: &[String],
) -> Result<ServerCertificate, PkiError> {
    let dir = ca.root.join("server");
    let crt_path = dir.join("server.crt");
    let key_path = dir.join("server.key");

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
    // §9.36：写 AKI 扩展（签发方 SKI），供 issuer 归属检查判定服务端证书
    // 是否由当前 CA 签发——外部 CA 文件被用户替换后 reload 自愈的依据。
    // §9.36 前签发的服务端证书无 AKI，归属检查按"无法判定归属"重签一次，
    // 新证书带 AKI 后即幂等稳定。
    params.use_authority_key_identifier_extension = true;
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
    build_tls_config_with_roots(ca, None, server)
}

/// 双信任根版本（§9.34 C rotate-ca 窗口期）：`previous` 非空时旧 CA 同入
/// `RootCertStore`——窗口内旧 CA 签发的客户端证书继续可用，签发一律走
/// 当前 CA。`max_conn_age` 保证窗口收敛后旧连接自然退出。
pub fn build_tls_config_with_roots(
    ca: &LocalCa,
    previous: Option<&PreviousCa>,
    server: &ServerCertificate,
) -> Result<Arc<ServerConfig>, PkiError> {
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let mut roots = RootCertStore::empty();
    roots.add(ca.cert_der.clone()).map_err(|e| {
        PkiError::new("E_PKI_CA_LOAD", format!("信任根装载失败: {}", e))
    })?;
    if let Some(prev) = previous {
        roots.add(prev.cert_der.clone()).map_err(|e| {
            PkiError::new("E_PKI_CA_LOAD", format!("旧 CA 信任根装载失败: {}", e))
        })?;
    }
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

/// §9.35 热加载：从盘上材料无条件重建 TLS 服务端配置（幂等，无副作用除非
/// 重签实际发生）。
///
/// 流程：`load_ca`（缺失/非法 → `E_PKI_CA_LOAD` fail-closed）→
/// `load_previous_ca`（rotate-ca 双 CA 窗口，无则 None）→
/// `ensure_server_cert`（幂等：缺失/过期/临期 ≤30 天自动重签——rotate-ca
/// prepare 删除 `server/` 后由此以新 CA 重签）→
/// `build_tls_config_with_roots`（当前 CA + 可选旧 CA 双信任根）。
///
/// 返回 `(ServerConfig, LocalCa, Option<PreviousCa>, ServerCertificate)`，
/// 供调用方（http_server::reload_tls_now）重发布 manifest。
pub fn load_tls_config_from_disk(
    root: &Path,
    bind: &SocketAddr,
) -> Result<(Arc<ServerConfig>, LocalCa, Option<PreviousCa>, ServerCertificate), PkiError> {
    let ca = load_ca(root)?.ok_or_else(|| {
        PkiError::new("E_PKI_CA_LOAD", format!("PKI 根目录无 CA: {}", root.display()))
    })?;
    let previous = load_previous_ca(root)?;
    let server = ensure_server_cert(&ca, bind)?;
    let tls = build_tls_config_with_roots(&ca, previous.as_ref(), &server)?;
    Ok((tls, ca, previous, server))
}

/// 生成一个 TLS 客户端配置（信任 pin 的 CA + 携带客户端证书）。
///
/// daemon 侧测试与本地工具复用；生产 Python 客户端由 `ssl.SSLContext` 承担。
/// `ca_pem` 支持多证书 PEM bundle（§9.34 C rotate-ca 窗口期双信任根）。
pub fn client_tls_config(
    ca_pem: &str,
    client_cert_pem: &str,
    client_key_pem: &str,
) -> Result<Arc<rustls::ClientConfig>, PkiError> {
    let ca_ders = pem_decode_all(ca_pem, "CERTIFICATE");
    if ca_ders.is_empty() {
        return Err(PkiError::new("E_PKI_CA_LOAD", "CA PEM 解析失败"));
    }
    let cert_der = pem_decode(client_cert_pem, "CERTIFICATE")
        .ok_or_else(|| PkiError::new("E_PKI_CA_LOAD", "client.crt 解析失败"))?;
    let key_der = pem_decode(client_key_pem, "PRIVATE KEY")
        .ok_or_else(|| PkiError::new("E_PKI_CA_LOAD", "client.key 解析失败"))?;

    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let mut roots = RootCertStore::empty();
    for der in ca_ders {
        roots
            .add(CertificateDer::from(der))
            .map_err(|e| PkiError::new("E_PKI_CA_LOAD", format!("信任根装载失败: {}", e)))?;
    }
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
    // §9.34 rotate-ca：写 AKI 扩展（签发方 SKI），供 `rotate-ca status` 按
    // AKI vs 当前 CA SKI 判定待重签清单。rcgen 默认不写（use_... 默认 false）。
    params.use_authority_key_identifier_extension = true;
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
    // §9.34 rotate-ca：AKI 扩展（同 issue_client_certificate）。
    params.use_authority_key_identifier_extension = true;
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

// ============================================
// 证书到期监控（§9.31）
// ============================================

/// 服务端叶证书剩余有效天数（向下取整；证书缺失 / 解析失败 → `None`）。
///
/// `/health` `cert_expires_in_days` 字段的数据源：读 `<pki_root>/server/server.crt`
/// （与 [`ensure_server_cert_locked`] 的权威路径一致），解析 notAfter 与当前
/// unix 时间差。只读、不 panic、不阻塞 /health（任何失败返回 `None`，
/// HTTP 层以 `-1` 作为缺省哨兵透出）。
pub fn server_cert_expires_in_days(pki_root: &Path) -> Option<i64> {
    let crt_path = pki_root.join("server").join("server.crt");
    let cert_pem = std::fs::read_to_string(crt_path).ok()?;
    let der = pem_decode(&cert_pem, "CERTIFICATE")?;
    let (_, cert) = x509_parser::parse_x509_certificate(&der).ok()?;
    let not_after = cert.validity().not_after.timestamp();
    Some((not_after - now_unix()).div_euclid(86_400))
}

/// 服务端证书是否临期/已过期（§9.34 A1 启动自动重签判定）。
///
/// `days_left <= threshold_days`（含过期，days_left 为负）→ true；
/// 解析失败 → true（fail-closed：宁可重签也不信任不可读的证书）。
pub fn server_cert_needs_renewal(der: &[u8], now_unix_secs: i64, threshold_days: i64) -> bool {
    let Ok((_, cert)) = x509_parser::parse_x509_certificate(der) else {
        return true;
    };
    let days_left = (cert.validity().not_after.timestamp() - now_unix_secs).div_euclid(86_400);
    days_left <= threshold_days
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_server_cert_expires_in_days_valid() {
        let root = tempfile::tempdir().unwrap();
        let ca = ensure_ca(root.path()).unwrap();
        let bind: SocketAddr = "127.0.0.1:8456".parse().unwrap();
        ensure_server_cert(&ca, &bind).unwrap();
        let days = server_cert_expires_in_days(root.path()).unwrap();
        assert!(days > 0, " freshly issued server cert must have days > 0, got {}", days);
        assert!(
            days <= SERVER_VALIDITY_SECS / 86_400,
            "days {} must not exceed 1y validity",
            days
        );
    }

    #[test]
    fn test_server_cert_missing_returns_none() {
        let root = tempfile::tempdir().unwrap();
        assert!(server_cert_expires_in_days(root.path()).is_none());
    }

    #[test]
    fn test_server_cert_garbage_pem_returns_none() {
        let root = tempfile::tempdir().unwrap();
        let dir = root.path().join("server");
        std::fs::create_dir_all(&dir).unwrap();
        std::fs::write(dir.join("server.crt"), "not a pem").unwrap();
        assert!(server_cert_expires_in_days(root.path()).is_none());
    }

    // ============================================
    // §9.34 A1：服务端证书临期自动重签
    // ============================================

    /// 用给定剩余秒数签发一张服务端 SAN 证书（测试专用：直接写 server/ 目录
    /// 模拟磁盘上已存在的临期证书）。
    fn write_server_cert_with_days_left(
        root: &Path,
        ca: &LocalCa,
        bind: &SocketAddr,
        days_left_secs: i64,
    ) -> String {
        let dir = root.join("server");
        std::fs::create_dir_all(&dir).unwrap();
        let mut params = CertificateParams::new(Vec::<String>::new()).unwrap();
        params.distinguished_name = rcgen::DistinguishedName::new();
        params.is_ca = IsCa::NoCa;
        params.subject_alt_names = required_server_sans(bind)
            .iter()
            .filter_map(|s| san_type_from_string(s))
            .collect();
        let now = now_unix();
        set_validity(&mut params, now - 3600, now + days_left_secs);
        let key = KeyPair::generate().unwrap();
        let cert = params.signed_by(&key, &ca.issuer().unwrap()).unwrap();
        let cert_pem = pem_encode("CERTIFICATE", cert.der());
        std::fs::write(dir.join("server.crt"), &cert_pem).unwrap();
        std::fs::write(dir.join("server.key"), "dummy-key").unwrap();
        cert_pem
    }

    #[test]
    fn test_server_cert_needs_renewal_boundaries() {
        let ca_root = tempfile::tempdir().unwrap();
        let ca = ensure_ca(ca_root.path()).unwrap();
        let bind: SocketAddr = "127.0.0.1:8457".parse().unwrap();
        let now = now_unix();
        let make = |days_left: i64| {
            let mut params = CertificateParams::new(Vec::<String>::new()).unwrap();
            set_validity(&mut params, now - 3600, now + days_left * 86_400);
            let key = KeyPair::generate().unwrap();
            let cert = params.signed_by(&key, &ca.issuer().unwrap()).unwrap();
            cert.der().to_vec()
        };
        // 边界（set_validity 粒度到天，not_after 截断到 civil 日零点，
        // 故阈值外侧用 32 天保证 days_left ≥ 31）：32 天 → false；
        // 30/29/0/过期 → true。
        assert!(!server_cert_needs_renewal(&make(32), now, SERVER_CERT_RENEW_THRESHOLD_DAYS));
        assert!(server_cert_needs_renewal(&make(30), now, SERVER_CERT_RENEW_THRESHOLD_DAYS));
        assert!(server_cert_needs_renewal(&make(29), now, SERVER_CERT_RENEW_THRESHOLD_DAYS));
        assert!(server_cert_needs_renewal(&make(0), now, SERVER_CERT_RENEW_THRESHOLD_DAYS));
        assert!(server_cert_needs_renewal(&make(-5), now, SERVER_CERT_RENEW_THRESHOLD_DAYS));
        // 解析失败 → fail-closed 重签。
        assert!(server_cert_needs_renewal(b"garbage", now, SERVER_CERT_RENEW_THRESHOLD_DAYS));
    }

    #[test]
    fn test_ensure_server_cert_reissues_when_expiring() {
        let root = tempfile::tempdir().unwrap();
        let ca = ensure_ca(root.path()).unwrap();
        let bind: SocketAddr = "127.0.0.1:8458".parse().unwrap();
        let old_pem = write_server_cert_with_days_left(root.path(), &ca, &bind, 10 * 86_400);

        ensure_server_cert(&ca, &bind).unwrap();

        let days = server_cert_expires_in_days(root.path()).unwrap();
        assert!(
            days > 340,
            "expiring cert must be reissued to ~1y validity, got {} days",
            days
        );
        let new_pem =
            std::fs::read_to_string(root.path().join("server").join("server.crt")).unwrap();
        assert_ne!(old_pem, new_pem, "server.crt must be overwritten on renewal");
    }

    #[test]
    fn test_ensure_server_cert_reissues_when_expired() {
        let root = tempfile::tempdir().unwrap();
        let ca = ensure_ca(root.path()).unwrap();
        let bind: SocketAddr = "127.0.0.1:8459".parse().unwrap();
        write_server_cert_with_days_left(root.path(), &ca, &bind, -86_400);

        ensure_server_cert(&ca, &bind).unwrap();
        assert!(server_cert_expires_in_days(root.path()).unwrap() > 340);
    }

    #[test]
    fn test_ensure_server_cert_fresh_cert_idempotent() {
        let root = tempfile::tempdir().unwrap();
        let ca = ensure_ca(root.path()).unwrap();
        let bind: SocketAddr = "127.0.0.1:8460".parse().unwrap();

        let first = ensure_server_cert(&ca, &bind).unwrap();
        let second = ensure_server_cert(&ca, &bind).unwrap();

        assert_eq!(
            first.sha256, second.sha256,
            "fresh cert must be reused (no reissue) across calls"
        );
        assert!(server_cert_expires_in_days(root.path()).unwrap() > 340);
    }

    // ============================================
    // §9.35：TLS 热加载（load_tls_config_from_disk）
    // ============================================

    #[test]
    fn test_load_tls_config_from_disk_idempotent() {
        let root = tempfile::tempdir().unwrap();
        ensure_ca(root.path()).unwrap();
        let bind: SocketAddr = "127.0.0.1:8461".parse().unwrap();

        let (cfg1, ca1, prev1, server1) = load_tls_config_from_disk(root.path(), &bind).unwrap();
        let (cfg2, ca2, prev2, server2) = load_tls_config_from_disk(root.path(), &bind).unwrap();

        assert_eq!(server1.sha256, server2.sha256, "idempotent: no reissue");
        assert_eq!(ca1.fingerprint_sha256, ca2.fingerprint_sha256);
        assert!(prev1.is_none() && prev2.is_none(), "no rotate-ca window");
    }

    #[test]
    fn test_load_tls_config_from_disk_no_ca_fails_closed() {
        let root = tempfile::tempdir().unwrap();
        let bind: SocketAddr = "127.0.0.1:8462".parse().unwrap();
        let err = load_tls_config_from_disk(root.path(), &bind).unwrap_err();
        assert_eq!(err.code, "E_PKI_CA_LOAD", "missing CA must fail closed");
    }

    #[test]
    fn test_load_tls_config_from_disk_after_rotate_ca_prepare() {
        // 模拟 rotate-ca prepare 的磁盘效果：旧 CA rename 到 ca-previous/
        // + 新 CA 落位 + server/ 删除。reload 必须以新 CA 重签服务端证书，
        // 且 previous 指纹 = 旧 CA 指纹（双信任根窗口语义）。
        let root = tempfile::tempdir().unwrap();
        let old_ca = ensure_ca(root.path()).unwrap();
        let old_fp = old_ca.fingerprint_sha256.clone();
        let bind: SocketAddr = "127.0.0.1:8463".parse().unwrap();
        ensure_server_cert(&old_ca, &bind).unwrap();

        std::fs::rename(root.path().join("ca"), root.path().join("ca-previous")).unwrap();
        ensure_ca(root.path()).unwrap();
        std::fs::remove_dir_all(root.path().join("server")).unwrap();

        let (_cfg, new_ca, prev, server) = load_tls_config_from_disk(root.path(), &bind).unwrap();
        assert_ne!(
            new_ca.fingerprint_sha256, old_fp,
            "current CA must be the newly created one"
        );
        let prev = prev.expect("rotate-ca prepare leaves ca-previous window");
        assert_eq!(
            prev.fingerprint_sha256, old_fp,
            "previous trust root must be the pre-rotate CA"
        );
        // 新服务端证书必须由新 CA 签发：issuer DN == 新 CA subject DN，
        // 且 != 旧 CA subject DN（§9.34 同 DN 缺陷修复后的代际区分不变量）。
        let old_der = pem_decode(&old_ca.cert_pem, "CERTIFICATE").unwrap();
        let new_der = pem_decode(&new_ca.cert_pem, "CERTIFICATE").unwrap();
        let server_der = pem_decode(&server.cert_pem, "CERTIFICATE").unwrap();
        let (_, old_ca_parsed) = x509_parser::parse_x509_certificate(&old_der).unwrap();
        let (_, new_ca_parsed) = x509_parser::parse_x509_certificate(&new_der).unwrap();
        let (_, server_parsed) = x509_parser::parse_x509_certificate(&server_der).unwrap();
        let old_subject = old_ca_parsed.subject().to_string();
        let new_subject = new_ca_parsed.subject().to_string();
        let server_issuer = server_parsed.issuer().to_string();
        assert_ne!(
            old_subject, new_subject,
            "new CA DN must differ from old CA DN"
        );
        assert_eq!(
            server_issuer, new_subject,
            "reissued server cert must be issued by the new CA"
        );
    }

    // ============================================
    // §9.36：外部 CA 导入（CW_CA_CERT + CW_CA_KEY）
    // ============================================

    /// 在指定目录生成一个独立自签 CA（cert/key PEM 文件 + 指纹）。
    fn make_standalone_ca(dir: &Path, cn: &str) -> (PathBuf, PathBuf, String) {
        let mut params = CertificateParams::new(Vec::<String>::new()).unwrap();
        params.distinguished_name = rcgen::DistinguishedName::new();
        params
            .distinguished_name
            .push(DnType::OrganizationName, "CallWarden Test");
        params.distinguished_name.push(DnType::CommonName, cn);
        params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        params.key_usages = vec![KeyUsagePurpose::KeyCertSign, KeyUsagePurpose::CrlSign];
        let now = now_unix();
        set_validity(&mut params, now - 3600, now + CA_VALIDITY_SECS);
        let key = KeyPair::generate().unwrap();
        let cert = params.self_signed(&key).unwrap();
        let cert_der = cert.der().clone();
        let fp = sha256_hex(cert_der.as_ref());
        let cert_path = dir.join(format!("{}.crt", cn));
        let key_path = dir.join(format!("{}.key", cn));
        std::fs::write(&cert_path, pem_encode("CERTIFICATE", cert_der.as_ref())).unwrap();
        std::fs::write(&key_path, pem_encode("PRIVATE KEY", &key.serialize_der())).unwrap();
        (cert_path, key_path, fp)
    }

    #[test]
    fn test_external_ca_loads_and_issues() {
        let ext_dir = tempfile::tempdir().unwrap();
        let (cert_path, key_path, fp) = make_standalone_ca(ext_dir.path(), "External CA A");
        let root = tempfile::tempdir().unwrap();
        let ca = load_ca_with_external(root.path(), Some((cert_path, key_path)))
            .unwrap()
            .unwrap();
        assert_eq!(ca.source, CaSource::External);
        assert_eq!(ca.fingerprint_sha256, fp);
        assert!(
            !root.path().join("ca").exists(),
            "外部 CA 模式不得创建/生成内置 ca/ 目录"
        );
        // 签发路径可用：以外部 CA 签发客户端身份，SAN principal 正确。
        let (cert_pem, _key_pem) = issue_local_client_identity(&ca, "pr_ext_test").unwrap();
        let der = pem_decode(&cert_pem, "CERTIFICATE").unwrap();
        assert_eq!(
            principal_id_from_cert(&der).as_deref(),
            Some("pr_ext_test")
        );
    }

    #[test]
    fn test_external_ca_missing_file_fail_closed() {
        let root = tempfile::tempdir().unwrap();
        let err = load_ca_with_external(
            root.path(),
            Some((
                PathBuf::from("Z:/definitely/missing/ca.crt"),
                PathBuf::from("Z:/definitely/missing/ca.key"),
            )),
        )
        .unwrap_err();
        assert_eq!(err.code, "E_PKI_CA_LOAD");
        assert!(
            !root.path().join("ca").exists(),
            "外部 CA 缺失时绝不自签兜底（不得生成 ca/）"
        );
    }

    #[test]
    fn test_external_ca_key_mismatch_fail_closed() {
        let ext_dir = tempfile::tempdir().unwrap();
        let (cert1, _, _) = make_standalone_ca(ext_dir.path(), "CA M1");
        let (_, key2, _) = make_standalone_ca(ext_dir.path(), "CA M2");
        let root = tempfile::tempdir().unwrap();
        let err =
            load_ca_with_external(root.path(), Some((cert1, key2))).unwrap_err();
        assert_eq!(err.code, "E_PKI_CA_LOAD", "cert/key 配对必须加载期拦截");
    }

    #[test]
    fn test_external_ca_rejects_non_ca_cert() {
        let ext_dir = tempfile::tempdir().unwrap();
        let (ca_cert, ca_key, _) = make_standalone_ca(ext_dir.path(), "CA Leaf Test");
        let scratch = tempfile::tempdir().unwrap();
        let ca = load_ca_with_external(scratch.path(), Some((ca_cert, ca_key.clone())))
            .unwrap()
            .unwrap();
        // 用 CA 签一张叶子证书（NoCa），再拿它冒充外部 CA。
        let mut params = CertificateParams::new(Vec::<String>::new()).unwrap();
        params.is_ca = IsCa::NoCa;
        let now = now_unix();
        set_validity(&mut params, now - 3600, now + 86_400);
        let key = KeyPair::generate().unwrap();
        let leaf = params.signed_by(&key, &ca.issuer().unwrap()).unwrap();
        let leaf_path = ext_dir.path().join("leaf.crt");
        std::fs::write(&leaf_path, pem_encode("CERTIFICATE", leaf.der())).unwrap();
        let root = tempfile::tempdir().unwrap();
        let err =
            load_ca_with_external(root.path(), Some((leaf_path, ca_key))).unwrap_err();
        assert_eq!(err.code, "E_PKI_CA_LOAD", "非 CA 证书不能当外部 CA");
    }

    #[test]
    fn test_external_ca_rejects_expired_ca() {
        let ext_dir = tempfile::tempdir().unwrap();
        let mut params = CertificateParams::new(Vec::<String>::new()).unwrap();
        params.distinguished_name = rcgen::DistinguishedName::new();
        params
            .distinguished_name
            .push(DnType::CommonName, "Expired CA");
        params.is_ca = IsCa::Ca(BasicConstraints::Unconstrained);
        params.key_usages = vec![KeyUsagePurpose::KeyCertSign];
        let now = now_unix();
        set_validity(&mut params, now - 10 * 86_400, now - 86_400);
        let key = KeyPair::generate().unwrap();
        let cert = params.self_signed(&key).unwrap();
        let cert_path = ext_dir.path().join("expired.crt");
        let key_path = ext_dir.path().join("expired.key");
        std::fs::write(&cert_path, pem_encode("CERTIFICATE", cert.der())).unwrap();
        std::fs::write(&key_path, pem_encode("PRIVATE KEY", &key.serialize_der())).unwrap();
        let root = tempfile::tempdir().unwrap();
        let err =
            load_ca_with_external(root.path(), Some((cert_path, key_path))).unwrap_err();
        assert_eq!(err.code, "E_PKI_CA_LOAD", "过期外部 CA 必须 fail-closed");
    }

    #[test]
    fn test_external_ca_conflicts_with_internal_ca() {
        let root = tempfile::tempdir().unwrap();
        let internal = ensure_ca(root.path()).unwrap();
        let ext_dir = tempfile::tempdir().unwrap();
        let (cert, key, _) = make_standalone_ca(ext_dir.path(), "External CA B");
        let err = load_ca_with_external(root.path(), Some((cert, key))).unwrap_err();
        assert_eq!(
            err.code, "E_PKI_CA_LOAD",
            "外部 CA 与磁盘内置 CA 指纹不一致必须拒绝静默切换"
        );
        // 指纹相同（外部文件 = 内置 CA 的副本）→ 兼容放行。
        let ext2 = tempfile::tempdir().unwrap();
        std::fs::write(ext2.path().join("copy.crt"), &internal.cert_pem).unwrap();
        std::fs::copy(
            root.path().join("ca").join("ca.key"),
            ext2.path().join("copy.key"),
        )
        .unwrap();
        let ca = load_ca_with_external(
            root.path(),
            Some((ext2.path().join("copy.crt"), ext2.path().join("copy.key"))),
        )
        .unwrap()
        .unwrap();
        assert_eq!(ca.source, CaSource::External);
        assert_eq!(ca.fingerprint_sha256, internal.fingerprint_sha256);
    }

    #[test]
    fn test_external_ca_rotation_window_conflict() {
        // 内置 rotate-ca 双根窗口未收敛时进入外部 CA 模式 → 拒绝。
        let root = tempfile::tempdir().unwrap();
        ensure_ca(root.path()).unwrap();
        std::fs::rename(root.path().join("ca"), root.path().join("ca-previous")).unwrap();
        ensure_ca(root.path()).unwrap();
        let ext_dir = tempfile::tempdir().unwrap();
        let (cert, key, _) = make_standalone_ca(ext_dir.path(), "External CA C");
        let err = load_ca_with_external(root.path(), Some((cert, key))).unwrap_err();
        assert_eq!(err.code, "E_PKI_CA_LOAD");
    }

    #[test]
    fn test_external_ca_env_semantics() {
        // env 解析纯逻辑：都未设置 → 内置；只设其一 → fail-closed。
        use std::ffi::OsString;
        assert!(
            external_ca_from_values(None, None).unwrap().is_none(),
            "两个 env 都未设置 → 内置 CA 模式"
        );
        let err = external_ca_from_values(Some(OsString::from("a.crt")), None).unwrap_err();
        assert_eq!(err.code, "E_PKI_CA_LOAD", "只设 CW_CA_CERT → fail-closed");
        let err = external_ca_from_values(None, Some(OsString::from("a.key"))).unwrap_err();
        assert_eq!(err.code, "E_PKI_CA_LOAD", "只设 CW_CA_KEY → fail-closed");
        // 空串与空白等同未设置。
        let err = external_ca_from_values(Some(OsString::from("")), Some(OsString::from("k")))
            .unwrap_err();
        assert_eq!(err.code, "E_PKI_CA_LOAD", "空 CW_CA_CERT 视为未设置 → 只设其一");
        let (c, k) =
            external_ca_from_values(Some(OsString::from(" a.crt ")), Some(OsString::from("k")))
                .unwrap()
                .unwrap();
        assert_eq!(c, PathBuf::from("a.crt"), "trim 前后空白");
        assert_eq!(k, PathBuf::from("k"));
    }

    #[test]
    fn test_external_ca_ensure_ca_never_self_signs() {
        // 注入"指向不存在文件"的外部 CA 配置 → ensure_ca 必须 fail-closed，
        // 且绝不自签生成内置 ca/（生产构建下由 CW_CA_CERT/CW_CA_KEY env 触发）。
        let _guard = ExternalCaOverrideGuard::inject(Some((
            PathBuf::from("Z:/definitely/missing/ca.crt"),
            PathBuf::from("Z:/definitely/missing/ca.key"),
        )));
        let root = tempfile::tempdir().unwrap();
        let result = ensure_ca(root.path());
        assert!(result.is_err(), "外部 CA 文件缺失 → ensure_ca fail-closed");
        assert!(
            !root.path().join("ca").exists(),
            "外部 CA 模式下 ensure_ca 绝不自签生成内置 CA"
        );
    }

    #[test]
    fn test_server_cert_reissued_when_ca_swapped() {
        // §9.36 issuer 归属检查：CA1 签发 server 证书后换成 CA2，
        // ensure_server_cert 必须识别 AKI 不归属并重签（外部 CA 文件被用户
        // 替换后的热加载自愈依赖此检查）。
        let root = tempfile::tempdir().unwrap();
        let ca1 = ensure_ca(root.path()).unwrap();
        let bind: SocketAddr = "127.0.0.1:8464".parse().unwrap();
        let old = ensure_server_cert(&ca1, &bind).unwrap();

        std::fs::remove_dir_all(root.path().join("ca")).unwrap();
        let ca2 = ensure_ca(root.path()).unwrap();
        let fresh = ensure_server_cert(&ca2, &bind).unwrap();
        assert_ne!(old.sha256, fresh.sha256, "换 CA 后 server 证书必须重签");
        let server_der = pem_decode(&fresh.cert_pem, "CERTIFICATE").unwrap();
        assert_eq!(
            cert_authority_key_id(&server_der),
            cert_subject_key_id(ca2.cert_der.as_ref()),
            "重签证书的 AKI 必须归属当前 CA"
        );
        // 再跑一次：AKI 已归属 → 幂等不重签。
        let again = ensure_server_cert(&ca2, &bind).unwrap();
        assert_eq!(fresh.sha256, again.sha256, "issuer 归属时不得重签（churn 防护）");
    }
}
