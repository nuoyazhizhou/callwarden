//! 引导端点 `POST /v1/enroll`（ADR-002 §4.2）。
//!
//! 这是唯一允许“无客户端证书”的写面：仅服务端认证 TLS + 有效单次 token，
//! 不接受任何业务方法。签发成功后 token 原子消费，之后所有 `/v1/rpc` 必须 mTLS。
//!
//! 失败语义（ADR §4.2 / §15.3）：
//! - token 类失败（不存在/过期/已用/主体不匹配）对外统一 `E_ENROLL_TOKEN_INVALID`，
//!   不回显细节，避免 oracle。
//! - CSR 非法 → `E_ENROLL_CSR_INVALID`。

use axum::{
    body::Bytes,
    extract::{Extension, State},
    http::StatusCode,
    response::Response,
};
use serde_json::{json, Value};

use super::pki_api;
use super::{json_response, AppState, AuthPrincipal};
use crate::daemon::dispatch::DaemonStateExt;

/// POST /v1/enroll 处理：`{ "token": "<id>.<secret>", "csr_pem": "...", "principal_id"?, "workspace_id"? }`
pub async fn enroll_handler<S: DaemonStateExt + Send + Sync + 'static>(
    State(app): State<AppState<S>>,
    body: Bytes,
) -> Response {
    let parsed: Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(_) => {
            return json_response(
                StatusCode::BAD_REQUEST,
                error_body("E_ENROLL_CSR_INVALID", "请求体不是合法 JSON").to_string(),
            )
        }
    };
    let token = parsed.get("token").and_then(|v| v.as_str()).unwrap_or("");
    let csr_pem = parsed.get("csr_pem").and_then(|v| v.as_str()).unwrap_or("");
    if token.is_empty() || csr_pem.is_empty() {
        return json_response(
            StatusCode::BAD_REQUEST,
            error_body("E_ENROLL_CSR_INVALID", "缺少 token 或 csr_pem").to_string(),
        );
    }

    // CA 必须已存在（daemon 启动期 ensure_ca 保证）；此处只读不生成。
    let ca = match pki_api::load_ca(&app.config.pki_dir) {
        Ok(Some(ca)) => ca,
        _ => {
            return json_response(
                StatusCode::SERVICE_UNAVAILABLE,
                error_body("E_PKI_CA_LOAD", "daemon CA 未就绪").to_string(),
            )
        }
    };

    let rec = match pki_api::consume_enroll_token(&ca, token) {
        Ok(r) => r,
        Err(_) => {
            // ADR §4.2：token 失败统一 E_ENROLL_TOKEN_INVALID，不区分过期/已用。
            return json_response(
                StatusCode::FORBIDDEN,
                error_body("E_ENROLL_TOKEN_INVALID", "token 无效").to_string(),
            )
        }
    };

    // 可选绑定校验：请求声明的 principal/workspace 必须与 token 绑定一致。
    for (field, expected) in [
        ("principal_id", rec.principal_id.as_str()),
        ("workspace_id", rec.workspace_id.as_str()),
    ] {
        if let Some(v) = parsed.get(field).and_then(|v| v.as_str()) {
            if !v.is_empty() && v != expected {
                return json_response(
                    StatusCode::FORBIDDEN,
                    error_body("E_ENROLL_TOKEN_INVALID", "token 无效").to_string(),
                );
            }
        }
    }

    match pki_api::issue_client_certificate(&ca, &rec.principal_id, csr_pem) {
        Ok(issued) => {
            // ADR-002 §5.2：签发即登记（磁盘 grants + DB 台账），否则新 principal
            // 在下一个连接上会因“未知 principal”被拒（fail-closed）。
            if let Err(e) = app.principal_registry.record_issued_certificate(
                &issued.principal_id,
                &issued.serial,
                &issued.cert_sha256,
                &rec.workspace_id,
            ) {
                eprintln!("[cw_daemon][pki] 签发登记失败（fail-closed）: {}", e);
                return json_response(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    error_body("E_PKI_IO", "证书已签发但登记失败").to_string(),
                );
            }
            let body = json!({
                "principal_id": issued.principal_id,
                "cert_pem": issued.cert_pem,
                "ca_pem": issued.ca_pem,
                "serial": issued.serial,
                "not_after": issued.not_after,
                "cert_sha256": issued.cert_sha256,
            });
            json_response(StatusCode::OK, body.to_string())
        }
        Err(e) => json_response(
            StatusCode::BAD_REQUEST,
            error_body(e.code, &e.message).to_string(),
        ),
    }
}

fn error_body(code: &str, message: &str) -> Value {
    json!({ "error": { "code": code, "message": message } })
}

/// POST /v1/enroll/renew（ADR-002 §4.3）：
/// 已持有效客户端证书者换发新证书；`{ "csr_pem": "..." }`。
///
/// 旧证书不在此处吊销——按 §4.3 的 overlap 窗口语义，吊销由 owner 显式
/// `cw pki revoke --serial` 执行（默认保留到过期）。
pub async fn renew_handler<S: DaemonStateExt + Send + Sync + 'static>(
    State(app): State<AppState<S>>,
    Extension(auth): Extension<AuthPrincipal>,
    body: Bytes,
) -> Response {
    let principal_id = auth.0.principal_id.as_str().to_string();
    if principal_id.is_empty() {
        return json_response(
            StatusCode::FORBIDDEN,
            error_body("E_MTLS_CLIENT_CERT_MISSING", "client certificate (mTLS) required")
                .to_string(),
        );
    }
    let parsed: Value = match serde_json::from_slice(&body) {
        Ok(v) => v,
        Err(_) => {
            return json_response(
                StatusCode::BAD_REQUEST,
                error_body("E_ENROLL_CSR_INVALID", "请求体不是合法 JSON").to_string(),
            )
        }
    };
    let csr_pem = parsed.get("csr_pem").and_then(|v| v.as_str()).unwrap_or("");
    if csr_pem.is_empty() {
        return json_response(
            StatusCode::BAD_REQUEST,
            error_body("E_ENROLL_CSR_INVALID", "缺少 csr_pem").to_string(),
        );
    }

    let ca = match pki_api::load_ca(&app.config.pki_dir) {
        Ok(Some(ca)) => ca,
        _ => {
            return json_response(
                StatusCode::SERVICE_UNAVAILABLE,
                error_body("E_PKI_CA_LOAD", "daemon CA 未就绪").to_string(),
            )
        }
    };
    match pki_api::issue_client_certificate(&ca, &principal_id, csr_pem) {
        Ok(issued) => {
            // 不新增 workspace grant（workspace_id 传空表示“保持既有授权集合”）。
            if let Err(e) = app.principal_registry.record_issued_certificate(
                &issued.principal_id,
                &issued.serial,
                &issued.cert_sha256,
                "",
            ) {
                eprintln!("[cw_daemon][pki] renew 登记失败（fail-closed）: {}", e);
                return json_response(
                    StatusCode::INTERNAL_SERVER_ERROR,
                    error_body("E_PKI_IO", "证书已签发但登记失败").to_string(),
                );
            }
            let body = json!({
                "principal_id": issued.principal_id,
                "cert_pem": issued.cert_pem,
                "ca_pem": issued.ca_pem,
                "serial": issued.serial,
                "not_after": issued.not_after,
                "cert_sha256": issued.cert_sha256,
            });
            json_response(StatusCode::OK, body.to_string())
        }
        Err(e) => json_response(
            StatusCode::BAD_REQUEST,
            error_body(e.code, &e.message).to_string(),
        ),
    }
}
