//! Daemon server —— 单轨 HTTPS + mTLS 传输入口。
//!
//! ADR-002「单轨 HTTPS + mTLS」S4-01：daemon RPC 的唯一出口是
//! [`super::http_server`] 提供的 HTTPS + mTLS listener。
//!
//! 本模块只保留 HTTP over TLS accept loop 的启动器：
//! - `spawn_http_transport`：自行 bind 后启动；
//! - `spawn_http_transport_prebound`：接管已预绑定的 listener（CR10）。
//!
//! UDS / Windows 命名管道端点、按平台派生 socket path / pipe name 的逻辑、
//! 以及 `SO_PEERCRED` / `ImpersonateNamedPipeClient` 对端凭证路径已全部移除。
//! 对端身份只来自 mTLS 客户端证书（principal_id），见 `http_server::AuthPrincipal`。

use std::sync::Arc;
use std::thread;

use tokio::sync::Mutex as TokioMutex;

use super::dispatch::DaemonStateExt;
use super::http_server::HttpServerConfig;
use super::serialization::SerializationPoint;

/// 创建 daemon 唯一的串行化点（Req 14.6 / P0-1）。
///
/// 所有 mutation（HTTP RPC handler）共享同一实例，避免并发写。
pub fn new_serialization_point() -> Arc<SerializationPoint> {
    Arc::new(SerializationPoint::with_default_timeout())
}

/// 在独立线程的 tokio runtime 中启动 HTTP(S) listener。
///
/// - `state`: 共享的 daemon state，包装为 `Arc<TokioMutex<S>>`。
/// - `sp`: daemon 唯一串行化点。
/// - `config`: HTTP 配置（`bind_spec` / `manifest_path` 等）。
///
/// 返回守护线程的 `JoinHandle`，调用方无需 join（随主线程退出而结束）。
pub fn spawn_http_transport<S>(
    state: Arc<TokioMutex<S>>,
    sp: Arc<SerializationPoint>,
    config: HttpServerConfig,
) -> thread::JoinHandle<()>
where
    S: DaemonStateExt + Send + Sync + 'static,
{
    thread::Builder::new()
        .name("cw-http-mvp".to_string())
        .spawn(move || {
            let rt = match tokio::runtime::Builder::new_multi_thread()
                .worker_threads(2)
                .enable_all()
                .build()
            {
                Ok(rt) => rt,
                Err(e) => {
                    eprintln!("[cw_daemon] [ERROR] HTTP tokio runtime init failed: {}", e);
                    return;
                }
            };
            rt.block_on(async {
                match super::http_server::serve(state, sp, config).await {
                    Ok(addr) => eprintln!("[cw_daemon] [INFO] HTTP listener bound at {}", addr),
                    Err(e) => eprintln!("[cw_daemon] [ERROR] HTTP bind failed: {:?}", e),
                }
            });
        })
        .expect("spawn cw-http-mvp thread")
}

/// CR10（client_convergence_codereview_20260909 §八）：使用已预绑定的 listener
/// 启动 HTTP(S) accept loop。
///
/// manifest 已在 daemon 主流程早期经 `bind_http` 同步发布（新 PID/endpoint），
/// 本函数只接管 accept 循环——重启动步骤（recovery / snapshot 恢复 /
/// TaskCollabStore）不再推迟 manifest 可见性。
pub fn spawn_http_transport_prebound<S>(
    bound: super::http_server::BoundHttp,
    state: Arc<TokioMutex<S>>,
    sp: Arc<SerializationPoint>,
    config: HttpServerConfig,
) -> thread::JoinHandle<()>
where
    S: DaemonStateExt + Send + Sync + 'static,
{
    thread::Builder::new()
        .name("cw-http-mvp".to_string())
        .spawn(move || {
            let rt = match tokio::runtime::Builder::new_multi_thread()
                .worker_threads(2)
                .enable_all()
                .build()
            {
                Ok(rt) => rt,
                Err(e) => {
                    eprintln!("[cw_daemon] [ERROR] HTTP tokio runtime init failed: {}", e);
                    return;
                }
            };
            rt.block_on(async {
                match super::http_server::serve_prebound(bound, state, sp, config).await {
                    Ok(addr) => eprintln!("[cw_daemon] [INFO] HTTP listener bound at {}", addr),
                    Err(e) => eprintln!("[cw_daemon] [ERROR] HTTP serve failed: {:?}", e),
                }
            });
        })
        .expect("spawn cw-http-mvp thread")
}
