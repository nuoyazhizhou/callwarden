//! `cw pki` 子命令（ADR-002 §4 / §4.3 / §6）。
//!
//! daemon owner 的本地 PKI 生命周期管理：
//!
//! - `init`：确保 CA + 服务端叶证书存在（幂等；已存在且 SAN 覆盖则原样复用）。
//! - `token issue`：签发单次引导 token（磁盘 `bootstrap/<id>.json` 权威，DB 台账
//!   `pki_enrollment_tokens` best-effort），返回 `<token_id>.<secret>`。
//! - `enroll`：本地生成密钥对 + CSR，经**仅服务端认证** TLS `POST /v1/enroll`
//!   换取客户端证书；私钥永不离开本机。
//! - `renew`：持有效客户端证书经 mTLS `POST /v1/enroll/renew` 换发新证书
//!   （旧证书保留到过期/显式 `revoke`，即 ADR §4.3 的 overlap 语义）。
//! - `revoke`：把 serial 写入权威 DB（`pki_revocations` + `pki_issued_certs.status`）
//!   并刷新 `revocations.json` 缓存；幂等（重复吊销同一 serial 成功且不重复插行）。
//! - `rotate-server`：同一 CA 下强制重签服务端证书（daemon 重启后生效）。
//!
//! 传输约束：本模块只使用 `rustls`（与 daemon 同一 TLS 后端、同一 pinned CA），
//! 不引入任何明文 HTTP 分支；服务器证书主机名校验由 rustls `ServerName` 承担
//! （用 IP 连接时对应 IP SAN，用 DNS 名连接时对应 DNS SAN）。

use std::io::{Read, Write};
use std::net::{IpAddr, SocketAddr, TcpStream, ToSocketAddrs};
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use rcgen::{CertificateParams, DistinguishedName, KeyPair};
use serde_json::{json, Value};

use crate::daemon::http_server::http_manifest_dir;
use crate::daemon::http_server::pki_api::{self, LocalCa, PkiError};

/// 客户端连接超时（PKI 引导为交互式命令，失败要快）。
const PKI_HTTP_TIMEOUT: Duration = Duration::from_secs(30);

/// `CW_PKI_DIR` 优先，否则 `~/.callwarden/pki`（与 daemon 侧 `pki_api::pki_root` 同源）。
pub fn resolve_pki_root() -> PathBuf {
    if let Some(dir) = std::env::var_os("CW_PKI_DIR") {
        if !dir.is_empty() {
            return PathBuf::from(dir);
        }
    }
    http_manifest_dir().join("pki")
}

/// 权威任务库路径（`CW_DAEMON_TASK_DB` 优先，否则 `~/.callwarden/callwarden.db`）。
pub fn resolve_db_path() -> PathBuf {
    if let Some(p) = std::env::var_os("CW_DAEMON_TASK_DB") {
        if !p.is_empty() {
            return PathBuf::from(p);
        }
    }
    http_manifest_dir().join("callwarden.db")
}

// ============================================
// init
// ============================================

/// 本机 owner 的固定 principal（`kind=service`，workspace 白名单不设限）。
pub const OWNER_PRINCIPAL: &str = "pr_owner";

/// 确保 CA + 服务端证书 + 本地 owner 身份（幂等）。
/// 返回 `(ca_pem, ca_fingerprint, server_sha256, san)`。
pub fn init(root: &Path, bind: SocketAddr) -> Result<Value, PkiError> {
    let ca = pki_api::ensure_ca(root)?;
    let server = pki_api::ensure_server_cert(&ca, &bind)?;
    ensure_owner_identity(&ca, root)?;
    Ok(json!({
        "pki_dir": root.to_string_lossy(),
        "ca_fingerprint_sha256": ca.fingerprint_sha256,
        "ca_cert_pem": ca.cert_pem,
        "server_cert_sha256": server.sha256,
        "server_not_after": server.not_after,
        "san": server.san,
        "owner_principal": OWNER_PRINCIPAL,
        "owner_identity_dir": root.join("identities").join(OWNER_PRINCIPAL).to_string_lossy(),
    }))
}

/// 幂等签发本机 owner 客户端身份（ADR §5.2 owner 迁移的最小实现）。
///
/// 已存在 `client.crt` + `client.key` 时不做任何事——重复 `init` 不得轮换
/// owner 证书（否则已配置的客户端会突然失去身份）。
fn ensure_owner_identity(ca: &LocalCa, root: &Path) -> Result<(), PkiError> {
    let dir = root.join("identities").join(OWNER_PRINCIPAL);
    if dir.join("client.crt").is_file() && dir.join("client.key").is_file() {
        return Ok(());
    }
    let (_cert_pem, key_pem) = pki_api::issue_local_client_identity(ca, OWNER_PRINCIPAL)?;
    write_private(&dir.join("client.key"), &key_pem)?;
    Ok(())
}

// ============================================
// token issue
// ============================================

/// 签发单次引导 token。返回 `(token, principal_id, workspace_id)`。
///
/// 幂等性说明：每次调用都会产生**新** token（这是 token 的语义，不是缺陷）；
/// 重复调用不会破坏既有 CA/证书，也不会覆盖已消费的 token 文件。
pub fn token_issue(
    root: &Path,
    principal: Option<&str>,
    workspace: &str,
) -> Result<Value, PkiError> {
    let ca = pki_api::ensure_ca(root)?;
    let principal_id = match principal {
        Some(p) if !p.is_empty() => p.to_string(),
        _ => pki_api::new_principal_id()?,
    };
    let (token, rec) = pki_api::issue_enroll_token(&ca, &principal_id, workspace)?;

    // DB 台账 best-effort：磁盘 token 文件是权威，DB 行仅用于审计/查询。
    if let Ok(conn) = rusqlite::Connection::open(resolve_db_path()) {
        let _ = ensure_pki_tables(&conn);
        let token_id = rec.token_id.clone();
        let principal_id_row = rec.principal_id.clone();
        let workspace_id_row = rec.workspace_id.clone();
        let issued_at_row = rec.issued_at;
        let expires_at_row = rec.expires_at;
        let _ = conn.execute(
            "INSERT OR REPLACE INTO pki_enrollment_tokens \
             (token_id, secret_hash, principal_id, workspace_ids_json, issued_at, \
              expires_at, status, consumed_at, issued_by) \
             VALUES (?1, ?2, ?3, ?4, ?5, ?6, 'pending', 0, 'cw pki token issue')",
            rusqlite::params![
                token_id.clone(),
                sha256_hex(&token.split_once('.').map(|(_, s)| s.to_string()).unwrap_or_default()),
                principal_id_row.clone(),
                serde_json::to_string(&vec![workspace_id_row.clone()]).unwrap_or_else(|_| "[]".into()),
                issued_at_row as f64,
                expires_at_row as f64,
            ],
        );
        // S1-19：引导 token 签发进入 audit_chain（best-effort，不阻断引导）。
        let _ = crate::cli::task::sign_audit_chain(
            &conn,
            "pki_enrollment_tokens",
            &token_id,
            &json!({
                "event": "pki_token_issued",
                "token_id": token_id,
                "principal_id": principal_id_row,
                "workspace_id": workspace_id_row,
                "issued_at": issued_at_row,
                "expires_at": expires_at_row,
                "issued_by": "cw pki token issue",
            }),
        );
    }

    Ok(json!({
        "token": token,
        "token_id": rec.token_id,
        "principal_id": rec.principal_id,
        "workspace_id": rec.workspace_id,
        "expires_at": rec.expires_at,
    }))
}

// ============================================
// enroll / renew
// ============================================

/// 生成密钥对 + CSR（PEM）。私钥由调用方保存，永不发送给 daemon。
fn generate_key_and_csr() -> Result<(String, String), PkiError> {
    let key = KeyPair::generate().map_err(|e| PkiError::new("E_PKI_GENERATE", format!("keygen: {}", e)))?;
    let mut params = CertificateParams::new(Vec::<String>::new())
        .map_err(|e| PkiError::new("E_PKI_GENERATE", format!("csr params: {}", e)))?;
    params.distinguished_name = DistinguishedName::new();
    let csr = params
        .serialize_request(&key)
        .map_err(|e| PkiError::new("E_ENROLL_CSR_INVALID", format!("CSR 生成失败: {}", e)))?;
    let key_pem = pki_api::pem_encode("PRIVATE KEY", &key.serialize_der());
    Ok((key_pem, csr.pem().map_err(|e| PkiError::new("E_ENROLL_CSR_INVALID", format!("CSR PEM: {}", e)))?))
}

/// `cw pki enroll --token <id>.<secret> [--endpoint https://host:port] [--ca ca.crt] [--out dir]`
///
/// 仅服务端认证 TLS（客户端此时尚无证书）。成功后把 `client.crt`/`client.key`/
/// `ca.crt` 写入 `--out`（默认 `<pki>/identities/<principal_id>/`）。
pub fn enroll(
    root: &Path,
    token: &str,
    endpoint: Option<&str>,
    ca_path: Option<&Path>,
    out_dir: Option<&Path>,
) -> Result<Value, PkiError> {
    let ca_pem = match ca_path {
        Some(p) => std::fs::read_to_string(p)
            .map_err(|e| PkiError::new("E_MTLS_UNKNOWN_CA", format!("读取 CA 失败: {}", e)))?,
        None => {
            let ca = pki_api::load_ca(root)?
                .ok_or_else(|| PkiError::new("E_MTLS_UNKNOWN_CA", "本机 PKI 中不存在 CA（先 cw pki init 或 --ca 指定）"))?;
            ca.cert_pem.clone()
        }
    };
    let endpoint = match endpoint {
        Some(e) => e.to_string(),
        None => discover_endpoint(Some(root))?,
    };
    let (key_pem, csr_pem) = generate_key_and_csr()?;
    let body = json!({ "token": token, "csr_pem": csr_pem });
    let (status, text) = tls_post(&endpoint, "/v1/enroll", &ca_pem, None, &body)?;
    let parsed: Value = serde_json::from_str(&text).map_err(|_| {
        PkiError::new("E_TLS_HANDSHAKE_FAILED", format!("enroll 响应非 JSON（HTTP {}）", status))
    })?;
    if status != 200 {
        return Err(error_from_body(&parsed, "E_ENROLL_TOKEN_INVALID"));
    }

    let principal_id = parsed
        .get("principal_id")
        .and_then(|v| v.as_str())
        .ok_or_else(|| PkiError::new("E_ENROLL_CSR_INVALID", "enroll 响应缺少 principal_id"))?
        .to_string();
    let cert_pem = parsed
        .get("cert_pem")
        .and_then(|v| v.as_str())
        .ok_or_else(|| PkiError::new("E_ENROLL_CSR_INVALID", "enroll 响应缺少 cert_pem"))?
        .to_string();
    let ca_pem_out = parsed
        .get("ca_pem")
        .and_then(|v| v.as_str())
        .unwrap_or(ca_pem.as_str())
        .to_string();

    let dir = out_dir
        .map(|p| p.to_path_buf())
        .unwrap_or_else(|| root.join("identities").join(&principal_id));
    write_private(&dir.join("client.key"), &key_pem)?;
    write_private(&dir.join("client.crt"), &cert_pem)?;
    write_private(&dir.join("ca.crt"), &ca_pem_out)?;
    let meta = json!({
        "principal_id": principal_id,
        "kind": "agent",
        "serial": parsed.get("serial").cloned().unwrap_or(Value::Null),
        "not_after": parsed.get("not_after").cloned().unwrap_or(Value::Null),
        "cert_sha256": parsed.get("cert_sha256").cloned().unwrap_or(Value::Null),
    });
    write_private(&dir.join("meta.json"), &serde_json::to_string_pretty(&meta).unwrap_or_default())?;

    Ok(json!({
        "principal_id": principal_id,
        "identity_dir": dir.to_string_lossy(),
        "serial": parsed.get("serial").cloned().unwrap_or(Value::Null),
        "not_after": parsed.get("not_after").cloned().unwrap_or(Value::Null),
    }))
}

/// `cw pki renew [--principal <id>] [--endpoint ...] [--identity-dir <dir>]`
///
/// 持有效客户端证书经 mTLS 换发新证书（旧证书保留 = overlap 窗口）。
pub fn renew(
    root: &Path,
    principal: Option<&str>,
    endpoint: Option<&str>,
    identity_dir: Option<&Path>,
) -> Result<Value, PkiError> {
    let dir = match identity_dir {
        Some(p) => p.to_path_buf(),
        None => {
            let id = principal.map(|s| s.to_string()).unwrap_or_else(default_principal);
            root.join("identities").join(id)
        }
    };
    let cert_pem = read_required(&dir.join("client.crt"), "E_MTLS_CLIENT_CERT_MISSING")?;
    let key_pem = read_required(&dir.join("client.key"), "E_MTLS_CLIENT_CERT_MISSING")?;
    let ca_pem = match dir.join("ca.crt") {
        p if p.exists() => std::fs::read_to_string(&p).map_err(|e| PkiError::new("E_MTLS_UNKNOWN_CA", e.to_string()))?,
        _ => pki_api::load_ca(root)?
            .ok_or_else(|| PkiError::new("E_MTLS_UNKNOWN_CA", "找不到 CA 证书"))?
            .cert_pem,
    };
    let endpoint = match endpoint {
        Some(e) => e.to_string(),
        None => discover_endpoint(Some(root))?,
    };
    let (key_pem_new, csr_pem) = generate_key_and_csr()?;
    let body = json!({ "csr_pem": csr_pem });
    let (status, text) = tls_post(
        &endpoint,
        "/v1/enroll/renew",
        &ca_pem,
        Some((cert_pem.as_str(), key_pem.as_str())),
        &body,
    )?;
    let parsed: Value = serde_json::from_str(&text)
        .map_err(|_| PkiError::new("E_TLS_HANDSHAKE_FAILED", format!("renew 响应非 JSON（HTTP {}）", status)))?;
    if status != 200 {
        return Err(error_from_body(&parsed, "E_MTLS_CLIENT_CERT_REJECTED"));
    }
    let new_cert = parsed
        .get("cert_pem")
        .and_then(|v| v.as_str())
        .ok_or_else(|| PkiError::new("E_ENROLL_CSR_INVALID", "renew 响应缺少 cert_pem"))?
        .to_string();
    write_private(&dir.join("client.key"), &key_pem_new)?;
    write_private(&dir.join("client.crt"), &new_cert)?;
    Ok(json!({
        "identity_dir": dir.to_string_lossy(),
        "serial": parsed.get("serial").cloned().unwrap_or(Value::Null),
        "not_after": parsed.get("not_after").cloned().unwrap_or(Value::Null),
    }))
}

// ============================================
// revoke / rotate-server
// ============================================

/// 吊销证书 serial（幂等）。DB 为权威，`revocations.json` 为缓存。
pub fn revoke(
    root: &Path,
    db_path: Option<&Path>,
    serial: &str,
    reason: &str,
    actor: &str,
) -> Result<Value, PkiError> {
    if serial.trim().is_empty() {
        return Err(PkiError::new("E_PKI_REVOKE", "serial 不能为空"));
    }
    let serial = serial.trim().to_ascii_uppercase();
    let db = db_path.map(|p| p.to_path_buf()).unwrap_or_else(resolve_db_path);
    let conn = rusqlite::Connection::open(&db)
        .map_err(|e| PkiError::new("E_PKI_DB_MISSING", format!("打开权威库 {} 失败: {}", db.display(), e)))?;
    ensure_pki_tables(&conn)
        .map_err(|e| PkiError::new("E_PKI_REVOKE", format!("初始化 PKI 台账失败: {}", e)))?;
    let now = unix_now_f64();
    let principal_id: String = conn
        .query_row(
            "SELECT principal_id FROM pki_issued_certs WHERE cert_serial = ?1",
            rusqlite::params![serial],
            |r| r.get(0),
        )
        .or_else(|_| {
            conn.query_row(
                "SELECT principal_id FROM pki_revocations WHERE cert_serial = ?1",
                rusqlite::params![serial],
                |r| r.get(0),
            )
        })
        .unwrap_or_default();
    let inserted = conn
        .execute(
            "INSERT OR IGNORE INTO pki_revocations \
             (cert_serial, principal_id, revoked_at, reason, actor_principal_id, audit_record_id) \
             VALUES (?1, ?2, ?3, ?4, ?5, '')",
            rusqlite::params![serial, principal_id, now, reason, actor],
        )
        .map_err(|e| PkiError::new("E_PKI_REVOKE", format!("写 pki_revocations 失败: {}", e)))?;
    let _ = conn.execute(
        "UPDATE pki_issued_certs SET status = 'revoked', revoked_at = ?2 \
         WHERE cert_serial = ?1 AND status = 'active'",
        rusqlite::params![serial, now],
    );
    // S1-19：首次吊销进入 audit_chain（幂等重复调用不重复记链），
    // 并把 audit 行号回填 `pki_revocations.audit_record_id` 方便校验。
    if inserted > 0 {
        let audit = crate::cli::task::sign_audit_chain(
            &conn,
            "pki_revocations",
            &serial,
            &json!({
                "event": "pki_cert_revoked",
                "cert_serial": serial,
                "principal_id": principal_id,
                "revoked_at": now,
                "reason": reason,
                "actor_principal_id": actor,
            }),
        );
        if audit.is_ok() {
            let _ = conn.execute(
                "UPDATE pki_revocations SET audit_record_id = ?2 WHERE cert_serial = ?1",
                rusqlite::params![serial, conn.last_insert_rowid()],
            );
        }
    }

    // 缓存文件（ADR §4.1 `revocations.json`）；DB 仍是权威。
    let cache = root.join("revocations.json");
    let mut serials: Vec<String> = std::fs::read_to_string(&cache)
        .ok()
        .and_then(|raw| serde_json::from_str::<Value>(&raw).ok())
        .and_then(|v| match v.get("serials") {
            Some(Value::Array(a)) => Some(a.iter().filter_map(|x| x.as_str().map(|s| s.to_string())).collect()),
            _ => None,
        })
        .unwrap_or_default();
    if !serials.iter().any(|s| s == &serial) {
        serials.push(serial.clone());
        serials.sort();
    }
    write_private(
        &cache,
        &serde_json::to_string_pretty(&json!({ "serials": serials })).unwrap_or_default(),
    )?;

    Ok(json!({ "serial": serial, "principal_id": principal_id, "revoked_at": now }))
}

/// 同一 CA 下重签服务端叶证书（先删除旧文件再 `ensure_server_cert` 生成）。
pub fn rotate_server(root: &Path, bind: SocketAddr) -> Result<Value, PkiError> {
    let ca = pki_api::ensure_ca(root)?;
    let dir = root.join("server");
    for name in ["server.crt", "server.key"] {
        let p = dir.join(name);
        if p.exists() {
            std::fs::remove_file(&p)
                .map_err(|e| PkiError::new("E_PKI_IO", format!("删除 {} 失败: {}", p.display(), e)))?;
        }
    }
    let server = pki_api::ensure_server_cert(&ca, &bind)?;
    // S1-19：服务端证书轮换进入 audit_chain（best-effort）。
    if let Ok(conn) = rusqlite::Connection::open(resolve_db_path()) {
        let _ = ensure_pki_tables(&conn);
        let _ = crate::cli::task::sign_audit_chain(
            &conn,
            "pki_server_certs",
            &server.sha256,
            &json!({
                "event": "pki_server_cert_rotated",
                "server_cert_sha256": server.sha256,
                "server_not_after": server.not_after,
                "san": server.san,
                "bind": bind.to_string(),
            }),
        );
        let _ = conn.execute(
            "INSERT OR REPLACE INTO pki_issued_certs \
             (cert_serial, principal_id, fingerprint_sha256, issued_at, not_before, \
              not_after, status, revoked_at, issued_by, source, subject_san) \
             VALUES (?1, 'pr_daemon_server', ?2, ?3, ?3, ?4, 'active', 0, 'pki rotate-server', 'rotate', ?5)",
            rusqlite::params![
                server.sha256,
                server.sha256,
                unix_now_f64(),
                server.not_after,
                server.san.join(","),
            ],
        );
    }
    Ok(json!({
        "server_cert_sha256": server.sha256,
        "server_not_after": server.not_after,
        "san": server.san,
        "restart_required": true,
    }))
}

// ============================================
// TLS HTTP 客户端（rustls，仅 HTTPS）
// ============================================

/// `POST path` over HTTPS。`client` 为 `Some((cert_pem, key_pem))` 时启用 mTLS。
fn tls_post(
    endpoint: &str,
    path: &str,
    ca_pem: &str,
    client: Option<(&str, &str)>,
    body: &Value,
) -> Result<(u16, String), PkiError> {
    let (host, port) = parse_endpoint(endpoint)?;
    let payload = body.to_string();
    let request = format!(
        "POST {} HTTP/1.1\r\nHost: {}\r\nContent-Type: application/json\r\n\
         Content-Length: {}\r\nConnection: close\r\n\r\n{}",
        path, host, payload.len(), payload
    );

    let config = match client {
        Some((cert, key)) => pki_api::client_tls_config(ca_pem, cert, key)?,
        None => server_auth_only_config(ca_pem)?,
    };
    let server_name = server_name_for(&host)?;
    let addr = resolve_addr(&host, port)?;
    let stream = TcpStream::connect_timeout(&addr, PKI_HTTP_TIMEOUT)
        .map_err(|e| PkiError::new("E_TLS_HANDSHAKE_FAILED", format!("连接 {} 失败: {}", endpoint, e)))?;
    let _ = stream.set_read_timeout(Some(PKI_HTTP_TIMEOUT));
    let _ = stream.set_write_timeout(Some(PKI_HTTP_TIMEOUT));

    let conn = rustls::ClientConnection::new(config, server_name)
        .map_err(|e| PkiError::new("E_TLS_HANDSHAKE_FAILED", format!("TLS 初始化失败: {}", e)))?;
    let mut tls = rustls::StreamOwned::new(conn, stream);
    tls.write_all(request.as_bytes())
        .map_err(|e| PkiError::new("E_TLS_HANDSHAKE_FAILED", format!("TLS 握手/写入失败: {}", e)))?;

    let mut buf = Vec::new();
    let mut chunk = [0u8; 4096];
    loop {
        match tls.read(&mut chunk) {
            Ok(0) => break,
            Ok(n) => buf.extend_from_slice(&chunk[..n]),
            Err(ref e) if e.kind() == std::io::ErrorKind::WouldBlock => continue,
            Err(e) => {
                return Err(PkiError::new(
                    "E_TLS_HANDSHAKE_FAILED",
                    format!("读取响应失败: {}", e),
                ))
            }
        }
    }
    parse_http_response(&buf)
}

/// 仅服务端认证的客户端配置（enroll 引导阶段尚无客户端证书）。
fn server_auth_only_config(ca_pem: &str) -> Result<Arc<rustls::ClientConfig>, PkiError> {
    let ca_der = pki_api::pem_decode(ca_pem, "CERTIFICATE")
        .ok_or_else(|| PkiError::new("E_MTLS_UNKNOWN_CA", "CA PEM 解析失败"))?;
    let provider = Arc::new(rustls::crypto::ring::default_provider());
    let mut roots = rustls::RootCertStore::empty();
    roots
        .add(rustls::pki_types::CertificateDer::from(ca_der))
        .map_err(|e| PkiError::new("E_MTLS_UNKNOWN_CA", format!("信任根装载失败: {}", e)))?;
    let cfg = rustls::ClientConfig::builder_with_provider(provider)
        .with_safe_default_protocol_versions()
        .map_err(|e| PkiError::new("E_TLS_CONFIG", format!("protocol versions: {}", e)))?
        .with_root_certificates(roots)
        .with_no_client_auth();
    Ok(Arc::new(cfg))
}

fn server_name_for(host: &str) -> Result<rustls::pki_types::ServerName<'static>, PkiError> {
    if let Ok(ip) = host.parse::<IpAddr>() {
        return Ok(rustls::pki_types::ServerName::IpAddress(ip.into()));
    }
    rustls::pki_types::ServerName::try_from(host.to_string())
        .map_err(|_| PkiError::new("E_TLS_SAN_MISMATCH", format!("非法主机名: {}", host)))
}

fn parse_endpoint(endpoint: &str) -> Result<(String, u16), PkiError> {
    let raw = endpoint.strip_prefix("https://").ok_or_else(|| {
        PkiError::new("E_TLS_REQUIRED", format!("端点必须是 https://（收到 {}）", endpoint))
    })?;
    let raw = raw.trim_end_matches('/');
    let (host, port) = match raw.rsplit_once(':') {
        Some((h, p)) => (h.trim_matches(['[', ']']).to_string(), p.parse::<u16>().map_err(|_| {
            PkiError::new("E_TLS_REQUIRED", format!("非法端口: {}", endpoint))
        })?),
        None => (raw.to_string(), 443),
    };
    if host.is_empty() {
        return Err(PkiError::new("E_TLS_REQUIRED", "端点缺少主机名"));
    }
    Ok((host, port))
}

fn resolve_addr(host: &str, port: u16) -> Result<SocketAddr, PkiError> {
    if let Ok(ip) = host.parse::<IpAddr>() {
        return Ok(SocketAddr::new(ip, port));
    }
    (host, port)
        .to_socket_addrs()
        .map_err(|e| PkiError::new("E_TLS_HANDSHAKE_FAILED", format!("解析 {}:{} 失败: {}", host, port, e)))?
        .next()
        .ok_or_else(|| PkiError::new("E_TLS_HANDSHAKE_FAILED", format!("{}:{} 无可用地址", host, port)))
}

fn parse_http_response(buf: &[u8]) -> Result<(u16, String), PkiError> {
    let split = buf
        .windows(4)
        .position(|w| w == b"\r\n\r\n")
        .ok_or_else(|| PkiError::new("E_TLS_HANDSHAKE_FAILED", "响应缺少 HTTP 头"))?;
    let head = String::from_utf8_lossy(&buf[..split]).to_string();
    let status = head
        .lines()
        .next()
        .and_then(|l| l.split_whitespace().nth(1))
        .and_then(|s| s.parse::<u16>().ok())
        .ok_or_else(|| PkiError::new("E_TLS_HANDSHAKE_FAILED", "响应状态行非法"))?;
    Ok((status, String::from_utf8_lossy(&buf[split + 4..]).to_string()))
}

fn error_from_body(body: &Value, fallback: &'static str) -> PkiError {
    let code = body
        .get("error")
        .and_then(|e| e.get("code"))
        .and_then(|c| c.as_str())
        .unwrap_or(fallback);
    let message = body
        .get("error")
        .and_then(|e| e.get("message"))
        .and_then(|m| m.as_str())
        .unwrap_or(code);
    // 错误码来自协议常量，转静态字符串避免泄漏任意输入。
    let static_code: &'static str = match code {
        "E_ENROLL_TOKEN_INVALID" => "E_ENROLL_TOKEN_INVALID",
        "E_ENROLL_CSR_INVALID" => "E_ENROLL_CSR_INVALID",
        "E_MTLS_CLIENT_CERT_MISSING" => "E_MTLS_CLIENT_CERT_MISSING",
        "E_MTLS_CLIENT_CERT_REJECTED" => "E_MTLS_CLIENT_CERT_REJECTED",
        "E_PRINCIPAL_UNKNOWN" => "E_PRINCIPAL_UNKNOWN",
        "E_PRINCIPAL_REVOKED" => "E_PRINCIPAL_REVOKED",
        "E_PKI_CA_LOAD" => "E_PKI_CA_LOAD",
        "E_PKI_IO" => "E_PKI_IO",
        _ => fallback,
    };
    PkiError::new(static_code, message.to_string())
}

// ============================================
// 杂项
// ============================================

/// 从 manifest 发现端点（`~/.callwarden/http-daemon.*.manifest.json` 中最新一份）。
fn discover_endpoint(root: Option<&Path>) -> Result<String, PkiError> {
    let dir = root
        .and_then(|r| r.parent().map(|p| p.to_path_buf()))
        .unwrap_or_else(http_manifest_dir);
    let mut candidates: Vec<PathBuf> = match std::fs::read_dir(&dir) {
        Ok(rd) => rd
            .flatten()
            .map(|e| e.path())
            .filter(|p| {
                p.file_name()
                    .map(|n| n.to_string_lossy().starts_with("http-daemon.") && n.to_string_lossy().ends_with(".manifest.json"))
                    .unwrap_or(false)
            })
            .collect(),
        Err(_) => Vec::new(),
    };
    candidates.sort();
    for path in candidates.into_iter().rev() {
        let Ok(raw) = std::fs::read_to_string(&path) else { continue };
        let Ok(v) = serde_json::from_str::<Value>(&raw) else { continue };
        if v.get("schema").and_then(|s| s.as_str()) != Some("callwarden-http-manifest/v2") {
            continue;
        }
        if let Some(ep) = v.get("endpoint").and_then(|s| s.as_str()) {
            return Ok(ep.to_string());
        }
    }
    Err(PkiError::new("E_HTTP_MANIFEST_MISSING", "未发现 v2 manifest，请用 --endpoint 指定"))
}

fn default_principal() -> String {
    std::env::var("CW_PRINCIPAL_ID").unwrap_or_else(|_| "owner".to_string())
}

fn read_required(path: &Path, code: &'static str) -> Result<String, PkiError> {
    std::fs::read_to_string(path).map_err(|e| {
        PkiError::new(code, format!("读取 {} 失败: {}", path.display(), e))
    })
}

fn write_private(path: &Path, contents: &str) -> Result<(), PkiError> {
    if let Some(parent) = path.parent() {
        std::fs::create_dir_all(parent)
            .map_err(|e| PkiError::new("E_PKI_IO", format!("mkdir {}: {}", parent.display(), e)))?;
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            let _ = std::fs::set_permissions(parent, std::fs::Permissions::from_mode(0o700));
        }
    }
    let tmp = path.with_extension(format!("{}.tmp", std::process::id()));
    std::fs::write(&tmp, contents)
        .map_err(|e| PkiError::new("E_PKI_IO", format!("写 {} 失败: {}", tmp.display(), e)))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let _ = std::fs::set_permissions(&tmp, std::fs::Permissions::from_mode(0o600));
    }
    std::fs::rename(&tmp, path)
        .map_err(|e| PkiError::new("E_PKI_IO", format!("rename {} 失败: {}", path.display(), e)))?;
    Ok(())
}

fn sha256_hex(text: &str) -> String {
    use sha2::{Digest, Sha256};
    let mut h = Sha256::new();
    h.update(text.as_bytes());
    format!("{:x}", h.finalize())
}

fn unix_now_f64() -> f64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs_f64())
        .unwrap_or(0.0)
}

// ============================================
// clap 命令树 + 入口
// ============================================

/// `cw pki` 动作（与 `cw pki <action>` 对齐）。
#[derive(Clone, clap::Subcommand, Debug)]
pub enum PkiCommand {
    /// 确保 CA + 服务端叶证书存在（幂等）
    Init {
        /// 服务端证书必须覆盖的绑定地址（默认 127.0.0.1:7633）
        #[arg(long, default_value = "127.0.0.1:7633")]
        bind: String,
    },
    /// 单次引导 token 管理
    Token {
        #[command(subcommand)]
        action: PkiTokenCommand,
    },
    /// 用引导 token 换取客户端证书（仅服务端认证 TLS）
    Enroll {
        #[arg(long)]
        token: String,
        /// 显式端点（缺省从 v2 manifest 发现）
        #[arg(long)]
        endpoint: Option<String>,
        /// 显式 CA 证书（跨机引导时使用）
        #[arg(long)]
        ca: Option<PathBuf>,
        /// 身份输出目录（默认 `<pki>/identities/<principal_id>/`）
        #[arg(long)]
        out: Option<PathBuf>,
    },
    /// 持有效客户端证书换发新证书（mTLS）
    Renew {
        #[arg(long)]
        principal: Option<String>,
        #[arg(long)]
        endpoint: Option<String>,
        #[arg(long)]
        identity_dir: Option<PathBuf>,
    },
    /// 吊销证书 serial（幂等）
    Revoke {
        #[arg(long)]
        serial: String,
        #[arg(long, default_value = "owner_requested")]
        reason: String,
        /// 权威任务库路径（缺省 `~/.callwarden/callwarden.db`）
        #[arg(long)]
        db: Option<PathBuf>,
    },
    /// 同一 CA 下重签服务端证书（daemon 重启生效）
    RotateServer {
        #[arg(long, default_value = "127.0.0.1:7633")]
        bind: String,
    },
}

#[derive(Clone, clap::Subcommand, Debug)]
pub enum PkiTokenCommand {
    /// 签发单次 token（`--principal` 缺省自动分配）
    Issue {
        #[arg(long)]
        principal: Option<String>,
        #[arg(long, default_value = "")]
        workspace: String,
    },
}

/// 确保 PKI 台账表存在（与 `db/db_base.py::ensure_pki_schema` 逐列一致）。
///
/// `cw pki` 是**引导链第一步**，可能运行在 Python DB 层从未开过库的新环境
/// （隔离 daemon / 新机器）。此时 `pki_revocations`/`pki_issued_certs`/
/// `pki_enrollment_tokens` 尚不存在，直接写会 `no such table`。用
/// `CREATE TABLE IF NOT EXISTS` 幂等补齐，不改变既有 schema 版本。
fn ensure_pki_tables(conn: &rusqlite::Connection) -> rusqlite::Result<()> {
    conn.execute_batch(
        "
        CREATE TABLE IF NOT EXISTS pki_issued_certs (
            cert_serial TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL,
            fingerprint_sha256 TEXT NOT NULL,
            issued_at REAL NOT NULL,
            not_before REAL NOT NULL,
            not_after REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','revoked')),
            revoked_at REAL NOT NULL DEFAULT 0,
            issued_by TEXT NOT NULL DEFAULT '',
            source TEXT NOT NULL DEFAULT 'enroll'
                CHECK(source IN ('enroll','renew','rotate')),
            subject_san TEXT NOT NULL DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS pki_revocations (
            cert_serial TEXT PRIMARY KEY,
            principal_id TEXT NOT NULL,
            revoked_at REAL NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            actor_principal_id TEXT NOT NULL DEFAULT '',
            audit_record_id TEXT NOT NULL DEFAULT ''
        );
        CREATE INDEX IF NOT EXISTS idx_pki_revocations_principal
        ON pki_revocations(principal_id, revoked_at);
        CREATE TABLE IF NOT EXISTS pki_enrollment_tokens (
            token_id TEXT PRIMARY KEY,
            secret_hash TEXT NOT NULL,
            principal_id TEXT NOT NULL,
            workspace_ids_json TEXT NOT NULL DEFAULT '[]',
            issued_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending'
                CHECK(status IN ('pending','consumed','expired','revoked')),
            consumed_at REAL NOT NULL DEFAULT 0,
            issued_by TEXT NOT NULL DEFAULT ''
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_pki_enrollment_tokens_secret
        ON pki_enrollment_tokens(secret_hash);
        CREATE INDEX IF NOT EXISTS idx_pki_enrollment_tokens_principal
        ON pki_enrollment_tokens(principal_id, status);
        CREATE INDEX IF NOT EXISTS idx_pki_enrollment_tokens_expires
        ON pki_enrollment_tokens(expires_at);
        CREATE TABLE IF NOT EXISTS audit_chain (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            table_name TEXT NOT NULL,
            record_id TEXT NOT NULL,
            operation TEXT NOT NULL DEFAULT 'insert',
            payload_hash TEXT NOT NULL,
            prev_signature TEXT DEFAULT '',
            record_signature TEXT NOT NULL,
            signing_key_id TEXT DEFAULT 'local',
            signed_at REAL NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_audit_chain_table_record
        ON audit_chain(table_name, record_id);
        ",
    )
}

/// 执行 `cw pki <cmd>`；成功输出 JSON 到 stdout，失败输出错误 JSON 到 stderr。
pub fn run(cmd: PkiCommand) -> i32 {
    let root = resolve_pki_root();
    let actor = std::env::var("CW_PRINCIPAL_ID").unwrap_or_else(|_| "owner".to_string());
    let result = match cmd {
        PkiCommand::Init { bind } => parse_bind(&bind).and_then(|b| init(&root, b)),
        PkiCommand::Token { action } => match action {
            PkiTokenCommand::Issue {
                principal,
                workspace,
            } => token_issue(&root, principal.as_deref(), &workspace),
        },
        PkiCommand::Enroll {
            token,
            endpoint,
            ca,
            out,
        } => enroll(
            &root,
            &token,
            endpoint.as_deref(),
            ca.as_deref(),
            out.as_deref(),
        ),
        PkiCommand::Renew {
            principal,
            endpoint,
            identity_dir,
        } => renew(
            &root,
            principal.as_deref(),
            endpoint.as_deref(),
            identity_dir.as_deref(),
        ),
        PkiCommand::Revoke {
            serial,
            reason,
            db,
        } => revoke(&root, db.as_deref(), &serial, &reason, actor.as_str()),
        PkiCommand::RotateServer { bind } => {
            parse_bind(&bind).and_then(|b| rotate_server(&root, b))
        }
    };
    match result {
        Ok(v) => {
            println!("{}", serde_json::to_string_pretty(&v).unwrap_or_default());
            0
        }
        Err(e) => {
            eprintln!("{}", json!({ "error": { "code": e.code, "message": e.message } }));
            1
        }
    }
}

fn parse_bind(bind: &str) -> Result<SocketAddr, PkiError> {
    bind.parse::<SocketAddr>()
        .map_err(|_| PkiError::new("E_PKI_GENERATE", format!("非法 bind 地址: {}", bind)))
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parse_endpoint_requires_https() {
        assert!(parse_endpoint("http://127.0.0.1:1").is_err());
        assert_eq!(parse_endpoint("https://127.0.0.1:7633").unwrap(), ("127.0.0.1".into(), 7633));
    }

    #[test]
    fn parse_http_response_splits_head_and_body() {
        let raw = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}";
        let (status, body) = parse_http_response(raw).unwrap();
        assert_eq!(status, 200);
        assert_eq!(body, "{}");
    }

    #[test]
    fn init_is_idempotent() {
        let tmp = std::env::temp_dir().join(format!("cw-pki-init-{}", std::process::id()));
        let _ = std::fs::remove_dir_all(&tmp);
        let bind: SocketAddr = "127.0.0.1:7633".parse().unwrap();
        let first = init(&tmp, bind).unwrap();
        let second = init(&tmp, bind).unwrap();
        assert_eq!(
            first["ca_fingerprint_sha256"], second["ca_fingerprint_sha256"],
            "重复 init 不得换 CA"
        );
        let _ = std::fs::remove_dir_all(&tmp);
    }
}
