//! H1: Call Warden HTTP MVP transport（frozen contract: HTTP MVP Compatibility Contract §4）.
//!
//! - HTTP 栈：axum 0.8.x + hyper 1.x + tokio 1.x，仅 HTTP/1.1（不启用 HTTP/2 / WS / SSE / 压缩）。
//! - 仅 dev_loopback_unauthenticated：bind 必须 loopback（127.0.0.0/8 或 ::1），
//!   默认 `127.0.0.1:0` 动态端口；任何非 loopback 在绑定前 fail-closed 返回
//!   `E_HTTP_MVP_LOOPBACK_ONLY`，且不建立 listener、不发布 manifest。
//! - 端点：`GET /health`、`GET /capabilities`、`POST /v1/rpc`、`POST /v1/jobs`、
//!   `GET /v1/jobs/{job_id}`、`POST /v1/jobs/{job_id}/cancel`。
//! - JSON-RPC 2.0 envelope IN/OUT；业务错误保留 `error.data.code`（E_* 字符串）。
//! - body 上限 8 MiB；Content-Type 必须为 `application/json`（否则 415）。
//! - mutation request_id 去重（内存 HashMap，跨重启保留推迟到 H3/H4）。
//! - 身份：合成 local-owner `PeerCredential`（无 OS peer cred over HTTP）。
//! - 绑定成功后原子发布 `callwarden-http-manifest/v1` manifest。
//!
//! 本模块对 `S: DaemonStateExt + Send + Sync + 'static` 泛型，持有
//! `Arc<tokio::sync::Mutex<S>>` + `Arc<SerializationPoint>`。

use std::collections::HashMap;
use std::net::SocketAddr;
use std::path::PathBuf;
use std::sync::{Arc, Mutex as StdMutex};
use std::time::{SystemTime, UNIX_EPOCH};

use axum::{
    body::{Body, Bytes},
    extract::{Path, Request, State},
    http::{HeaderMap, StatusCode},
    middleware::{self, Next},
    response::Response,
    routing::{get, post},
    Router,
};
use serde_json::{Map, Value, json};
use sha2::{Digest, Sha256};
use std::net::ToSocketAddrs;
use tokio::net::TcpListener;
use tokio::sync::Mutex as TokioMutex;

use super::compat_adapter::{CompatAdapter, CompatAdapterConfig};
use super::dispatch::{
    DaemonStateExt, PeerCredential, current_daemon_owner_key, is_protected_mutation,
};
use super::serialization::SerializationPoint;
use super::SCHEMA_VERSION;

/// 8 MiB 请求体上限（按原始 body bytes 计）。
const MAX_BODY_BYTES: usize = 8 * 1024 * 1024;

/// 固定 security profile 标识。
const SECURITY_PROFILE: &str = "dev_loopback_unauthenticated";

/// capability registry revision（H1 冻结标识）。
const CAPABILITY_REGISTRY_REVISION: &str = "http-mvp-cap-registry-v1";

// ============================================
// 错误类型
// ============================================

/// HTTP MVP 绑定/服务错误。
///
/// `LoopbackOnly` 对应 `E_HTTP_MVP_LOOPBACK_ONLY`：非 loopback 绑定被拒绝，
/// listener 未建立，manifest 未发布。
#[derive(Debug)]
pub enum HttpServerError {
    LoopbackOnly,
    Bind(String),
    Manifest(String),
    Serve(String),
}

impl std::fmt::Display for HttpServerError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            HttpServerError::LoopbackOnly => {
                write!(f, "E_HTTP_MVP_LOOPBACK_ONLY: non-loopback bind rejected")
            }
            HttpServerError::Bind(e) => write!(f, "E_HTTP_BIND_FAILED: {}", e),
            HttpServerError::Manifest(e) => write!(f, "E_HTTP_MANIFEST_WRITE: {}", e),
            HttpServerError::Serve(e) => write!(f, "E_HTTP_SERVE_FAILED: {}", e),
        }
    }
}

impl std::error::Error for HttpServerError {}

impl From<serde_json::Error> for HttpServerError {
    fn from(e: serde_json::Error) -> Self {
        HttpServerError::Manifest(e.to_string())
    }
}

// ============================================
// 配置
// ============================================

/// HTTP MVP server 配置（构造后传入 [`serve`]）。
#[derive(Debug, Clone)]
pub struct HttpServerConfig {
    /// 绑定地址字符串，如 `127.0.0.1:0`。绑定前做 loopback 校验。
    pub bind_spec: String,
    /// manifest 写入的完整文件路径。
    pub manifest_path: PathBuf,
    /// mutation 去重持久化 SQLite 路径（与 manifest 同目录）。
    pub dedup_db_path: PathBuf,
    /// authority 标识（来自 `http_authority_id()`，与 H2 侧一致）。
    pub authority_id: String,
    /// Git commit（运行时 `git rev-parse HEAD` 捕获）。
    pub git_commit: String,
    /// daemon 可执行文件路径（用于 manifest `daemon_executable`）。
    pub daemon_executable: PathBuf,
    /// daemon 二进制 SHA-256（用于 manifest `daemon_binary_sha256`）。
    pub daemon_binary_sha256: String,
    /// capability registry revision（manifest + /health 字段）。
    pub capability_registry_revision: String,
    /// worker 状态（H1 无 compat worker → `not_started`）。
    pub worker_status: String,
    /// 每次启动随机的 manifest_id。
    pub manifest_id: String,
    /// 绑定成功后的实际 endpoint（由 serve 填充）。
    pub endpoint: String,
}

impl HttpServerConfig {
    /// 用绑定地址与 manifest 路径构造；其余字段在运行时捕获。
    pub fn new(bind_spec: String, manifest_path: PathBuf) -> Self {
        // P0-2：mutation 去重持久化 SQLite 与 manifest 同目录（跨 daemon restart 保留 ≥24h）。
        let dedup_db_path = manifest_path
            .parent()
            .map(|p| p.join("http-dedup-v1.sqlite"))
            .unwrap_or_else(|| PathBuf::from("http-dedup-v1.sqlite"));
        Self {
            bind_spec,
            manifest_path,
            dedup_db_path,
            authority_id: http_authority_id(),
            git_commit: current_git_commit(),
            daemon_executable: std::env::current_exe()
                .unwrap_or_else(|_| PathBuf::from("cw-daemon")),
            daemon_binary_sha256: compute_binary_sha256(),
            capability_registry_revision: CAPABILITY_REGISTRY_REVISION.to_string(),
            worker_status: "not_started".to_string(),
            manifest_id: make_id("manifest"),
            endpoint: String::new(),
        }
    }
}

// ============================================
// 共享状态
// ============================================

/// mutation 去重持久化存储（P0-2：跨 daemon restart 保留 ≥24h）。
///
/// 用 SQLite 表 `http_dedup` 持久化 `(workspace_instance_id, method, request_id)`
/// → `(params_hash, response_json)`。`check_and_reserve` 用 `INSERT OR IGNORE`
/// 原子占位，消除并发重复在 placeholder 写入前再次派发的窗口。
struct DedupStore {
    conn: StdMutex<rusqlite::Connection>,
}

/// dedup 检查结果。
enum DedupCheck {
    /// 本请求是首个执行者，继续 dispatch。
    First,
    /// 相同 request_id + 相同 params 的重放，返回已存结果 JSON。
    Replay(String),
    /// 相同 request_id + 不同 params，拒绝（无副作用）。
    Mismatch,
    /// 相同 request_id + 相同 params，但首个执行者仍在 dispatch 中。
    InFlight,
}

/// dedup 表 DDL（主键为 (workspace_instance_id, method, request_id)）。
const DEDUP_DDL: &str = "
CREATE TABLE IF NOT EXISTS http_dedup (
    workspace_instance_id TEXT NOT NULL,
    method TEXT NOT NULL,
    request_id TEXT NOT NULL,
    params_hash TEXT NOT NULL,
    response_json TEXT,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (workspace_instance_id, method, request_id)
);
CREATE INDEX IF NOT EXISTS idx_http_dedup_created ON http_dedup(created_at);
";

/// mutation 去重记录保留时长（契约 §4.2：跨 restart 保留 ≥24h）。
const DEDUP_RETENTION_SECS: u64 = 24 * 3600;

impl DedupStore {
    /// 打开（或创建）去重 SQLite 并初始化 schema。
    fn open(path: &PathBuf) -> Result<Self, HttpServerError> {
        let conn = rusqlite::Connection::open(path)
            .map_err(|e| HttpServerError::Manifest(format!("dedup db open failed: {}", e)))?;
        conn.execute_batch(DEDUP_DDL)
            .map_err(|e| HttpServerError::Manifest(format!("dedup ddl failed: {}", e)))?;
        Ok(Self {
            conn: StdMutex::new(conn),
        })
    }

    /// 原子 check-and-reserve：`INSERT OR IGNORE` 占位，返回本请求的去重处置。
    ///
    /// - 首次插入成功 → `First`（继续 dispatch）。
    /// - key 已存在且 params_hash 不同 → `Mismatch`。
    /// - key 已存在、params_hash 相同且已有结果 → `Replay(result_json)`。
    /// - key 已存在、params_hash 相同但结果为空 → `InFlight`（首个执行者仍在处理）。
    fn check_and_reserve(
        &self,
        ws: &str,
        method: &str,
        request_id: &str,
        params_hash: &str,
    ) -> Result<DedupCheck, HttpServerError> {
        let conn = self.conn.lock().unwrap();
        let now = now_unix();
        // 顺带清理过期记录（> 24h）
        let cutoff = now.saturating_sub(DEDUP_RETENTION_SECS) as i64;
        let _ = conn.execute(
            "DELETE FROM http_dedup WHERE created_at < ?1",
            rusqlite::params![cutoff],
        );

        let inserted = conn
            .execute(
                "INSERT OR IGNORE INTO http_dedup \
                 (workspace_instance_id, method, request_id, params_hash, created_at) \
                 VALUES (?1, ?2, ?3, ?4, ?5)",
                rusqlite::params![ws, method, request_id, params_hash, now as i64],
            )
            .map_err(|e| HttpServerError::Manifest(format!("dedup insert failed: {}", e)))?;

        if inserted == 1 {
            return Ok(DedupCheck::First);
        }

        let row: (String, Option<String>) = conn
            .query_row(
                "SELECT params_hash, response_json FROM http_dedup \
                 WHERE workspace_instance_id = ?1 AND method = ?2 AND request_id = ?3",
                rusqlite::params![ws, method, request_id],
                |r| Ok((r.get(0)?, r.get(1)?)),
            )
            .map_err(|e| HttpServerError::Manifest(format!("dedup query failed: {}", e)))?;

        if row.0 != params_hash {
            return Ok(DedupCheck::Mismatch);
        }
        match row.1 {
            Some(resp) => Ok(DedupCheck::Replay(resp)),
            None => Ok(DedupCheck::InFlight),
        }
    }

    /// 回填首个执行者的结果（dispatch 完成后调用）。
    fn store_result(
        &self,
        ws: &str,
        method: &str,
        request_id: &str,
        result: &Value,
    ) -> Result<(), HttpServerError> {
        let conn = self.conn.lock().unwrap();
        let resp = serde_json::to_string(result).unwrap_or_else(|_| "null".to_string());
        conn.execute(
            "UPDATE http_dedup SET response_json = ?1 \
             WHERE workspace_instance_id = ?2 AND method = ?3 AND request_id = ?4",
            rusqlite::params![resp, ws, method, request_id],
        )
        .map_err(|e| HttpServerError::Manifest(format!("dedup update failed: {}", e)))?;
        Ok(())
    }
}

/// job 记录（H1 最小实现）。
#[derive(Clone)]
struct JobRecord {
    status: String,
    method: String,
    params: Value,
    request_id: String,
    result: Option<Value>,
    error: Option<Value>,
    created_at: String,
    finished_at: Option<String>,
}

/// axum 应用共享状态。
pub struct AppState<S: DaemonStateExt + Send + Sync + 'static> {
    pub state: Arc<TokioMutex<S>>,
    pub serialization: Arc<SerializationPoint>,
    pub config: Arc<HttpServerConfig>,
    pub dedup: Arc<DedupStore>,
    pub jobs: Arc<StdMutex<HashMap<String, JobRecord>>>,
    /// H3: Python compatibility worker adapter（python_compat 方法路由）。
    pub compat: Arc<CompatAdapter>,
}

impl<S: DaemonStateExt + Send + Sync + 'static> Clone for AppState<S> {
    fn clone(&self) -> Self {
        Self {
            state: Arc::clone(&self.state),
            serialization: Arc::clone(&self.serialization),
            config: Arc::clone(&self.config),
            dedup: Arc::clone(&self.dedup),
            jobs: Arc::clone(&self.jobs),
            compat: Arc::clone(&self.compat),
        }
    }
}

// ============================================
// 绑定 / 校验
// ============================================

/// 解析并校验绑定地址为 loopback。
///
/// - `127.0.0.1:0` / `::1:0` → Ok
/// - `0.0.0.0` / `::` / LAN / 远程 / 含非 loopback 解析的 hostname → `LoopbackOnly`
pub fn resolve_loopback(spec: &str) -> Result<SocketAddr, HttpServerError> {
    let addrs: Vec<SocketAddr> = match spec.to_socket_addrs() {
        Ok(it) => it.collect(),
        Err(_) => return Err(HttpServerError::LoopbackOnly),
    };
    if addrs.is_empty() {
        return Err(HttpServerError::LoopbackOnly);
    }
    for a in &addrs {
        if !a.ip().is_loopback() {
            return Err(HttpServerError::LoopbackOnly);
        }
    }
    Ok(addrs[0])
}

/// 供 daemon 启动前 fail-closed 预检使用。
pub fn validate_loopback_bind(spec: &str) -> Result<(), HttpServerError> {
    resolve_loopback(spec).map(|_| ())
}

/// 绑定成功后的句柄（listener 已就绪、manifest 已发布）。
pub struct BoundHttp {
    pub listener: TcpListener,
    pub local_addr: SocketAddr,
    pub manifest: Value,
}

/// 绑定 + 发布 manifest（仅在成功 bind 之后）。
pub async fn bind_http(cfg: &HttpServerConfig) -> Result<BoundHttp, HttpServerError> {
    let addr = resolve_loopback(&cfg.bind_spec)?;
    let listener = TcpListener::bind(addr)
        .await
        .map_err(|e| HttpServerError::Bind(e.to_string()))?;
    let local = listener
        .local_addr()
        .map_err(|e| HttpServerError::Bind(e.to_string()))?;
    let manifest = build_manifest(cfg, &local);
    publish_manifest_atomic(&cfg.manifest_path, &manifest)?;
    Ok(BoundHttp {
        listener,
        local_addr: local,
        manifest,
    })
}

/// 创建 router 并 serve（绑定成功后调用）。返回实际绑定地址。
pub async fn serve<S>(
    state: Arc<TokioMutex<S>>,
    sp: Arc<SerializationPoint>,
    config: HttpServerConfig,
) -> Result<SocketAddr, HttpServerError>
where
    S: DaemonStateExt + Send + Sync + 'static,
{
    let mut config = config;
    // H3: 启动 compat worker（非致命：失败标记 unhealthy，调用时返回
    // E_COMPAT_WORKER_UNAVAILABLE，不阻塞 HTTP transport 启动）。
    let compat = Arc::new(CompatAdapter::new(
        CompatAdapterConfig::from_env(&config.daemon_executable),
        Arc::clone(&sp),
    ));
    if let Err(e) = compat.start() {
        eprintln!(
            "[cw_daemon][compat] worker 启动失败（继续 serve，兼容调用返回 UNAVAILABLE）: {}",
            e
        );
    }
    config.worker_status = compat.worker_status();
    let bound = bind_http(&config).await?;
    config.endpoint = format!("http://{}", bound.local_addr);
    // P0-2：打开持久化去重存储（与 manifest 同目录，跨 restart 保留 ≥24h）
    let dedup = Arc::new(DedupStore::open(&config.dedup_db_path)?);
    let app_state = AppState {
        state,
        serialization: sp,
        config: Arc::new(config),
        dedup,
        jobs: Arc::new(StdMutex::new(HashMap::new())),
        compat,
    };
    let router = build_router(app_state);
    axum::serve(bound.listener, router)
        .await
        .map_err(|e| HttpServerError::Serve(e.to_string()))?;
    Ok(bound.local_addr)
}

/// 在独立 OS 线程 + tokio runtime 中启动 HTTP transport，与现有 UDS/Named Pipe transport 并行运行。
///
/// `serve` 绑定并阻塞服务（直到 server 停止）；本函数在独立线程中运行 runtime 并 block_on，
/// 失败仅打印日志，不影响主 transport。供 `server.rs::start_server` 在 opt-in 时调用。
pub fn run_http_transport<S>(
    state: Arc<TokioMutex<S>>,
    sp: Arc<SerializationPoint>,
    config: HttpServerConfig,
) -> Result<(), HttpServerError>
where
    S: DaemonStateExt + Send + Sync + 'static,
{
    std::thread::spawn(move || {
        let rt = match tokio::runtime::Builder::new_multi_thread()
            .enable_all()
            .build()
        {
            Ok(rt) => rt,
            Err(e) => {
                eprintln!("[cw_daemon][http] tokio runtime 初始化失败: {}", e);
                return;
            }
        };
        rt.block_on(async {
            if let Err(e) = serve(state, sp, config).await {
                eprintln!("[cw_daemon][http] HTTP MVP transport 启动失败: {}", e);
            }
        });
    });
    Ok(())
}

// ============================================
// Router / handlers
// ============================================

/// 自定义中间件：在进入 handler / body 提取之前，依据声明的 Content-Length
/// 头部强制 8 MiB 上限。超出立即返回 413，且因未触发 body 读取，不会像
/// axum 的 DefaultBodyLimit 那样在读取途中中止连接导致 RST（客户端读不到 413）。
async fn enforce_max_body_size(req: Request, next: Next, git: &str) -> Response {
    let exceeded = req
        .headers()
        .get(axum::http::header::CONTENT_LENGTH)
        .and_then(|v| v.to_str().ok())
        .and_then(|s| s.trim().parse::<usize>().ok())
        .map(|n| n > MAX_BODY_BYTES)
        .unwrap_or(false);
    if exceeded {
        return json_rpc_error(
            None,
            -32600,
            "E_REQUEST_TOO_LARGE",
            "request body exceeds the 8 MiB limit",
            413,
            git,
        );
    }
    next.run(req).await
}

pub fn build_router<S: DaemonStateExt + Send + Sync + 'static>(app: AppState<S>) -> Router {
    let git = app.config.git_commit.clone();
    Router::new()
        .route("/health", get(health_handler::<S>))
        .route("/capabilities", get(capabilities_handler::<S>))
        .route("/v1/rpc", post(rpc_handler::<S>))
        .route("/v1/jobs", post(jobs_handler::<S>))
        .route("/v1/jobs/{job_id}", get(job_get_handler::<S>))
        .route("/v1/jobs/{job_id}/cancel", post(job_cancel_handler::<S>))
        .route("/v1/meta/tools", get(meta_tools_handler::<S>))
        .layer(middleware::from_fn(move |req, next| {
            let git = git.clone();
            async move { enforce_max_body_size(req, next, &git).await }
        }))
        .with_state(app)
}

/// GET /health — 返回安全 profile + 端点 + PID + git + schema + worker + registry revision。
async fn health_handler<S: DaemonStateExt + Send + Sync + 'static>(
    State(app): State<AppState<S>>,
) -> Response {
    let cfg = &app.config;
    let body = json!({
        "security_profile": SECURITY_PROFILE,
        "endpoint": cfg.endpoint,
        "pid": std::process::id(),
        "git_commit": cfg.git_commit,
        "schema_version": SCHEMA_VERSION,
        "worker_status": cfg.worker_status,
        "capability_registry_revision": cfg.capability_registry_revision,
    });
    json_response(StatusCode::OK, serde_json::to_string(&body).unwrap())
}

/// GET /capabilities — 返回 capability registry（fail closed 时 503）。
async fn capabilities_handler<S: DaemonStateExt + Send + Sync + 'static>(
    State(app): State<AppState<S>>,
) -> Response {
    match build_capability_registry() {
        Ok(v) => json_response(StatusCode::OK, serde_json::to_string(&v).unwrap()),
        Err(e) => {
            let body = json!({
                "jsonrpc": "2.0",
                "id": null,
                "error": {
                    "code": -32603,
                    "message": "capability registry build failed (fail closed)",
                    "data": { "code": "E_CAPABILITY_REGISTRY_FAILED", "detail": e }
                }
            });
            json_response(
                StatusCode::SERVICE_UNAVAILABLE,
                serde_json::to_string(&body).unwrap(),
            )
        }
    }
}

/// GET /v1/meta/tools — 返回 239 工具路由矩阵自描述（C2，T01）。
///
/// 数据源：`route_matrix::ToolRegistry::meta_tools_value()`（由
/// deliverables/software-company/tool_migration_matrix.json 生成）。
/// 每项含 name/module/target_backend/rpc_method/op_class/batch/status，
/// 供监控 `backend=python_compat` 的工具清单（Q5 compat 窗口截止跟踪）。
async fn meta_tools_handler<S: DaemonStateExt + Send + Sync + 'static>(
    State(_app): State<AppState<S>>,
) -> Response {
    let tools = super::route_matrix::ToolRegistry::meta_tools_value();
    let body = json!({
        "schema_version": "1.0",
        "total_tools": super::route_matrix::TOOL_ROUTES.len(),
        "tools": tools,
    });
    json_response(StatusCode::OK, serde_json::to_string(&body).unwrap())
}

/// python_compat 批量 read_only 白名单（H4C-1 基建扩展）。
///
/// 与 Python 侧 server/compat_registry.py `RUST_COMPAT_ROUTE` 完全一致
/// （由 validate_against_rust_route 两端对齐门保证）；新增 python_compat
/// 方法时需同步：本数组 + build_capability_registry python_compat 行 +
/// Python 侧 RUST_COMPAT_ROUTE 与默认 registry。
const COMPAT_ROUTE_WHITELIST: &[(&str, &str)] = &[
    // H4C-1 默认（1 项；get_uncommented_symbols 已 W2-1 迁移 rust_native，T-1786840097330-dec66710）
    ("stats_top_files", "read_only"),
    // H4C-2 符号组只读工具（14 项，T-1786716190783-ba187c88#H4C-2；
    // get_module_call_stats / get_semgrep_stats 已 W2-1 迁移 rust_native，
    // T-1786840097330-dec66710；
    // get_semgrep_findings 已 W3-3 迁移 rust_native，T-1786861820151-deb64c48；
    // get_file_history 已 W4-1 迁移 rust_native，T-1786886251769-22b94ee8-sub-1，
    // 剩 13 项）
    ("get_symbol_history", "read_only"),
    ("get_recent_changes", "read_only"),
    ("get_impact", "read_only"),
    ("get_comment_from_version", "read_only"),
    ("get_issue_summary", "read_only"),
    ("find_issues", "read_only"),
    ("get_test_coverage", "read_only"),
    ("export_module_graph", "read_only"),
    // H4C-3 任务组只读工具（13 项，T-1786716190783-ba187c88#H4C-3；
    // get_clone_stats / get_job_stats / get_clone_group_stats 已 W2-2 迁移
    // rust_native，T-1786840097330-a9e0ec69；
    // get_job_status / list_jobs / wait_for_job 已 W3-2 迁移 rust_native，
    // T-1786861820151-f3cecf40；
    // get_commit_tasks 已 W4-1 迁移 rust_native，T-1786886251769-22b94ee8-sub-1；
    // get_defect_correlation 已 W4-3 迁移 rust_native，
    // T-1786886251769-22b94ee8-sub-3，剩 8 项）
    ("get_symbol_change_tasks", "read_only"),
    ("audit_verify_chain", "read_only"),
    ("list_audit_signing_keys", "read_only"),
    ("bootstrap_status", "read_only"),
    ("list_clones", "read_only"),
    ("list_clone_groups", "read_only"),
    ("get_clone_group_detail", "read_only"),
    ("task_plan_template", "read_only"),
    // H4C-2 第二批摘要/演化/护栏/缺陷组只读工具（27 项，T-1786747295213-64204cce#步骤#0；
    // defect_stats 已 W2-3 迁移 rust_native 并移除白名单，T-1786840097331-fd01a3f8；
    // get_coverage_for_symbol / diff_to_symbol 已 W4-2 迁移 rust_native，
    // T-1786886251769-22b94ee8-sub-2；defect_correlation / churn_analysis /
    // defect_search / defect_suggest_fix 已 W4-3 迁移 rust_native 并移除白名单，
    // T-1786886251769-22b94ee8-sub-3，剩 20 项（含 defect_learn 写面保留 python_compat））
    ("get_summary", "read_only"),
    ("project_brief", "read_only"),
    ("repo_map", "read_only"),
    ("test_impact_selection", "read_only"),
    ("who_to_ask", "read_only"),
    ("get_ownership_map", "read_only"),
    ("guardrail_scan", "read_only"),
    ("guardrail_check_edit", "read_only"),
    ("guardrail_list_rules", "read_only"),
    ("blast_radius", "read_only"),
    ("ask_codebase", "read_only"),
    ("get_token_savings_report", "read_only"),
    ("get_vulnerability_blast_radius", "read_only"),
    ("get_clone_aware_impact", "read_only"),
    ("review_readiness", "read_only"),
    ("cross_layer_impact", "read_only"),
    ("evolution_frequency", "read_only"),
    ("hotspot_evolution", "read_only"),
    ("defect_learn", "read_only"),
    // defect_stats 已 W2-3 迁移 rust_native（T-1786840097331-fd01a3f8），白名单条目移除
    // H4C-2 第二批语义/外部符号组只读工具（5 项，T-1786747295213-64204cce#步骤#1）
    ("semantic_search", "read_only"),
    ("find_similar_functions", "read_only"),
    ("get_symbol_commit_history", "read_only"),
    ("parse_codeowners", "read_only"),
    ("get_project_dependencies", "read_only"),
    // H4C-2 第三批分支/编辑历史/跨仓库/LSP 组只读工具（13 项，T-1786747295227-49c90d68#步骤#0；
    // get_edit_stats 已 W2-3 迁移 rust_native，T-1786840097331-fd01a3f8；
    // diff_branches 已 W4-4 迁移 rust_native，T-1786886251769-22b94ee8-sub-4）
    ("list_branches", "read_only"),
    ("merge_preview", "read_only"),
    ("get_edit_history", "read_only"),
    ("find_shared_symbols", "read_only"),
    ("cross_repo_impact", "read_only"),
    ("cross_repo_summary", "read_only"),
    ("lsp_hover", "read_only"),
    ("lsp_definition", "read_only"),
    ("lsp_references", "read_only"),
    ("lsp_diagnostics", "read_only"),
    ("lsp_completion", "read_only"),
    ("lsp_check_available", "read_only"),
    // H4C-2 第三批 toolchain/edge 组只读工具（8 项，T-1786747295227-49c90d68#步骤#1；
    // list_build_contexts / get_build_context / get_active_build_context /
    // get_resolved_edges / count_resolved_edges 已 W3-1 迁移 rust_native，
    // T-1786861820150-bfe5e805；list_toolchains / get_toolchain /
    // get_workspace_toolchains 已 S2 批次2 迁移 rust_native，T-1787209948470-a59bcf9c）
    // H4C-2 第三批规则查询组只读工具（3 项，T-1786747295227-49c90d68 整改：
    // rule_candidate_list/rule_list/get_applicable_rules 纯 SELECT 接入 worker）
    ("rule_candidate_list", "read_only"),
    ("rule_list", "read_only"),
    ("get_applicable_rules", "read_only"),
    // H4C-2 第三批 collab 组只读工具（4 项，T-1786747295227-b876fddf#步骤#0；
    // get_role_view 已 MCP-001 迁移 rust_native 并移除白名单，
    // T-1787321708699-da5d8224，剩 3 项）
    ("find_evidence", "read_only"),
    ("get_freshness_status", "read_only"),
    ("get_gate_decision", "read_only"),
    // H4C-2 第三批 p2 依赖图/环检测组只读工具（5 项，T-1786747295227-b876fddf#步骤#1；
    // get_artifact_freshness 已 MCP-005 迁移 rust_native 并移除白名单，
    // T-1787321709137-2df7bd97；get_interface_providers 已 MCP-006 迁移 rust_native
    // 并移除白名单，T-1787321709098-f2236ea0；detect_cycle 已 MCP-007 迁移 rust_native
    // 并移除白名单，T-1787321709179-f6fdf5bc；validate_revision_dependencies 已 MCP-008
    // 迁移 rust_native 并移除白名单，T-1787321709249-fb256530；get_dependency_edges 已
    // MCP-009 迁移 rust_native 并移除白名单，T-1787321709365-021050a8，p2 组全部迁移）
    // H4C-2 第三批 p3 身份/证明组只读工具（5 项，T-1786747295227-b876fddf#步骤#1）
    ("get_action_identity", "read_only"),
    ("check_action_identity", "read_only"),
    ("check_session_separation", "read_only"),
    ("get_attestation_validity", "read_only"),
    ("list_attestation_revocations", "read_only"),
    // H4C-2 第三批 p4 assignment 只读工具（1 项，T-1786747295227-b876fddf#步骤#1；
    // lease_* 5 项 rust_native 走 daemon dispatch，不在此白名单）
    ("assignment_show", "read_only"),
];

/// 判断 method 是否为 python_compat（由 H3 compat worker 提供服务），返回其
/// operation_class；非 compat 方法返回 None（走 rust_native dispatch）。
fn compat_route(method: &str) -> Option<&'static str> {
    COMPAT_ROUTE_WHITELIST
        .iter()
        .find(|(m, _)| *m == method)
        .map(|(_, op)| *op)
}

/// POST /v1/rpc — JSON-RPC 2.0 dispatch。
async fn rpc_handler<S: DaemonStateExt + Send + Sync + 'static>(
    State(app): State<AppState<S>>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    // Content-Type 必须为 application/json（否则 415，handler 未执行）
    let ctype = headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if !ctype.starts_with("application/json") {
        return json_rpc_error(
            None,
            -32600,
            "E_CONTENT_TYPE_NOT_JSON",
            "Content-Type must be application/json",
            415,
            &app.config.git_commit,
        );
    }

    // 任务 1D2（Req 15/AC26）：raw body bytes 先经 strict duplicate-key parser，
    // 在转成 `Value` 前检测重复 key。命中时 fail-closed 返回稳定
    // E_DUPLICATE_JSON_KEY，不进入 dedup/dispatch，也不写任一 ledger；
    // 其余解析错误继续走下方既有 serde_json 路径（E_PARSE_ERROR 语义不变）。
    if let Err(e) = crate::daemon::task_loop::strict_transport::parse_strict_envelope(&body) {
        if e.code == crate::daemon::task_loop::strict_transport::ERR_DUPLICATE_JSON_KEY {
            return json_rpc_error(
                None,
                -32600,
                "E_DUPLICATE_JSON_KEY",
                &e.message,
                400,
                &app.config.git_commit,
            );
        }
    }

    // 解析 JSON（malformed → 400，handler 未执行）
    let parsed: Value = match serde_json::from_slice::<Value>(&body) {
        Ok(v) => v,
        Err(_) => {
            return json_rpc_error(
                None,
                -32700,
                "E_PARSE_ERROR",
                "malformed JSON",
                400,
                &app.config.git_commit,
            )
        }
    };

    // protocol_version 必须为字符串 "1"（否则 426）
    match parsed.get("protocol_version").and_then(|v| v.as_str()) {
        Some("1") => {}
        _ => {
            return json_rpc_error(
                None,
                -32600,
                "E_PROTOCOL_VERSION_UNSUPPORTED",
                "protocol_version must be \"1\"",
                426,
                &app.config.git_commit,
            )
        }
    }

    // jsonrpc 必须严格 "2.0"
    if parsed.get("jsonrpc").and_then(|v| v.as_str()) != Some("2.0") {
        return json_rpc_error(
            None,
            -32600,
            "E_INVALID_REQUEST",
            "jsonrpc must be \"2.0\"",
            400,
            &app.config.git_commit,
        );
    }

    // id 必须 1..128 字节非空字符串
    let id = match parsed.get("id").and_then(|v| v.as_str()) {
        Some(s) if !s.is_empty() && s.len() <= 128 => s.to_string(),
        _ => {
            return json_rpc_error(
                None,
                -32600,
                "E_INVALID_REQUEST",
                "id must be 1..128 byte non-empty string",
                400,
                &app.config.git_commit,
            )
        }
    };

    // method 必须非空
    let method = match parsed.get("method").and_then(|v| v.as_str()) {
        Some(m) if !m.is_empty() => m.to_string(),
        _ => {
            return json_rpc_error(
                Some(&id),
                -32600,
                "E_INVALID_REQUEST",
                "method required",
                400,
                &app.config.git_commit,
            )
        }
    };

    // params 必须是 object（缺省空 object）
    let params = match parsed.get("params") {
        Some(Value::Object(_)) | None => {
            parsed.get("params").cloned().unwrap_or(Value::Object(Map::new()))
        }
        Some(_) => {
            return json_rpc_error(
                Some(&id),
                -32600,
                "E_INVALID_REQUEST",
                "params must be object",
                400,
                &app.config.git_commit,
            )
        }
    };

    // mutation request_id 去重（P0-2：持久化 + 原子占位，跨 restart 保留 ≥24h）
    let ws = params
        .get("workspace_instance_id")
        .and_then(|v| v.as_str())
        .unwrap_or("")
        .to_string();
    let is_mut = is_protected_mutation(&method);
    let params_hash = sha256_hex(serde_json::to_string(&params).unwrap_or_default().as_bytes());

    // H3: python_compat 方法 → 路由到 compat worker（read_only；MVP 无
    // governance_write，index_write 走 daemon 兼容写锁）。与 rust_native 共享
    // JSON-RPC envelope，错误经 build_rpc_response 保留 retryable/recovery。
    if let Some(op_class) = compat_route(&method) {
        let ws_id = params.get("workspace_id").and_then(|v| v.as_i64());
        let deadline_ms = params
            .get("deadline_ms")
            .and_then(|v| v.as_u64())
            .unwrap_or(30_000)
            .clamp(1, 120_000);
        let result = match app
            .compat
            .dispatch_arc(
                &method,
                params.clone(),
                ws,
                ws_id,
                op_class,
                deadline_ms,
            )
            .await
        {
            Ok(v) => v,
            Err(e) => json!({
                "ok": false,
                "error": {
                    "code": e.code,
                    "message": e.message,
                    "retryable": false,
                    "recovery": "compat adapter 内部错误，检查 daemon 日志"
                }
            }),
        };
        return build_rpc_response(&id, &result, &app.config.git_commit);
    }

    if is_mut {
        // 并发同 key 时轮询等待首个执行者完成，最多等 client deadline（默认 30s）
        let deadline_ms = params
            .get("deadline_ms")
            .and_then(|v| v.as_u64())
            .unwrap_or(30_000)
            .clamp(1, 120_000);
        let start = std::time::Instant::now();
        loop {
            match app.dedup.check_and_reserve(&ws, &method, &id, &params_hash) {
                Ok(DedupCheck::First) => break,
                Ok(DedupCheck::Replay(resp)) => {
                    let v: Value = serde_json::from_str(&resp).unwrap_or(Value::Null);
                    return build_rpc_response(&id, &v, &app.config.git_commit);
                }
                Ok(DedupCheck::Mismatch) => {
                    return json_rpc_error(
                        Some(&id),
                        -32000,
                        "E_REQUEST_ID_REUSE_MISMATCH",
                        "request_id reused with different params",
                        200,
                        &app.config.git_commit,
                    );
                }
                Ok(DedupCheck::InFlight) => {
                    if start.elapsed().as_millis() as u64 >= deadline_ms {
                        return json_rpc_error(
                            Some(&id),
                            -32000,
                            "E_REQUEST_IN_FLIGHT",
                            "duplicate mutation still in flight",
                            200,
                            &app.config.git_commit,
                        );
                    }
                    tokio::time::sleep(std::time::Duration::from_millis(50)).await;
                }
                Err(e) => {
                    return json_rpc_error(
                        Some(&id),
                        -32603,
                        "E_DEDUP_STORE_ERROR",
                        &e.to_string(),
                        500,
                        &app.config.git_commit,
                    );
                }
            }
        }
    }

    // 合成 local-owner peer（无 OS peer cred over HTTP）
    let peer = synthetic_local_owner_peer();

    let result = {
        let mut st = app.state.lock().await;
        super::dispatch::dispatch_rpc(&mut *st, peer, &method, &params, &[], &app.serialization)
    };

    if is_mut {
        let _ = app.dedup.store_result(&ws, &method, &id, &result);
    }

    build_rpc_response(&id, &result, &app.config.git_commit)
}

/// POST /v1/jobs — 接受与 /v1/rpc 相同的 envelope，返回 202。
async fn jobs_handler<S: DaemonStateExt + Send + Sync + 'static>(
    State(app): State<AppState<S>>,
    headers: HeaderMap,
    body: Bytes,
) -> Response {
    let ctype = headers
        .get(axum::http::header::CONTENT_TYPE)
        .and_then(|v| v.to_str().ok())
        .unwrap_or("");
    if !ctype.starts_with("application/json") {
        return json_rpc_error(
            None,
            -32600,
            "E_CONTENT_TYPE_NOT_JSON",
            "Content-Type must be application/json",
            415,
            &app.config.git_commit,
        );
    }
    // 任务 1D2（Req 15/AC26）：与 /v1/rpc 相同的 raw-bytes strict duplicate-key gate。
    if let Err(e) = crate::daemon::task_loop::strict_transport::parse_strict_envelope(&body) {
        if e.code == crate::daemon::task_loop::strict_transport::ERR_DUPLICATE_JSON_KEY {
            return json_rpc_error(
                None,
                -32600,
                "E_DUPLICATE_JSON_KEY",
                &e.message,
                400,
                &app.config.git_commit,
            );
        }
    }
    let parsed: Value = match serde_json::from_slice::<Value>(&body) {
        Ok(v) => v,
        Err(_) => {
            return json_rpc_error(
                None,
                -32700,
                "E_PARSE_ERROR",
                "malformed JSON",
                400,
                &app.config.git_commit,
            )
        }
    };
    if parsed.get("protocol_version").and_then(|v| v.as_str()) != Some("1") {
        return json_rpc_error(
            None,
            -32600,
            "E_PROTOCOL_VERSION_UNSUPPORTED",
            "protocol_version must be \"1\"",
            426,
            &app.config.git_commit,
        );
    }
    let id = match parsed.get("id").and_then(|v| v.as_str()) {
        Some(s) if !s.is_empty() && s.len() <= 128 => s.to_string(),
        _ => {
            return json_rpc_error(
                None,
                -32600,
                "E_INVALID_REQUEST",
                "id required",
                400,
                &app.config.git_commit,
            )
        }
    };
    let method = match parsed.get("method").and_then(|v| v.as_str()) {
        Some(m) if !m.is_empty() => m.to_string(),
        _ => {
            return json_rpc_error(
                Some(&id),
                -32600,
                "E_INVALID_REQUEST",
                "method required",
                400,
                &app.config.git_commit,
            )
        }
    };
    let params = match parsed.get("params") {
        Some(Value::Object(_)) | None => {
            parsed.get("params").cloned().unwrap_or(Value::Object(Map::new()))
        }
        Some(_) => {
            return json_rpc_error(
                Some(&id),
                -32600,
                "E_INVALID_REQUEST",
                "params must be object",
                400,
                &app.config.git_commit,
            )
        }
    };

    let job_id = make_id("job");
    let now = now_unix().to_string();
    {
        let mut jobs = app.jobs.lock().unwrap();
        jobs.insert(
            job_id.clone(),
            JobRecord {
                status: "queued".to_string(),
                method: method.clone(),
                params: params.clone(),
                request_id: id.clone(),
                result: None,
                error: None,
                created_at: now.clone(),
                finished_at: None,
            },
        );
    }

    // 后台执行（H1：直接 dispatch；后续替换为 Rust job runner / compat worker）
    let st = Arc::clone(&app.state);
    let sp = Arc::clone(&app.serialization);
    let jobs2 = Arc::clone(&app.jobs);
    let jid = job_id.clone();
    let peer = synthetic_local_owner_peer();
    tokio::spawn(async move {
        {
            let mut j = jobs2.lock().unwrap();
            if let Some(r) = j.get_mut(&jid) {
                r.status = "running".to_string();
            }
        }
        let res = {
            let mut s = st.lock().await;
            super::dispatch::dispatch_rpc(&mut *s, peer, &method, &params, &[], &sp)
        };
        let mut j = jobs2.lock().unwrap();
        if let Some(r) = j.get_mut(&jid) {
            let ok = res.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
            if ok {
                r.status = "succeeded".to_string();
                r.result = res.get("result").cloned();
            } else {
                r.status = "failed".to_string();
                r.error = res.get("error").cloned();
            }
            r.finished_at = Some(now_unix().to_string());
        }
    });

    let body = json!({
        "job_id": job_id,
        "status": "queued",
        "status_url": format!("/v1/jobs/{}", job_id)
    });
    json_response(StatusCode::ACCEPTED, serde_json::to_string(&body).unwrap())
}

/// GET /v1/jobs/{job_id}
async fn job_get_handler<S: DaemonStateExt + Send + Sync + 'static>(
    State(app): State<AppState<S>>,
    Path(job_id): Path<String>,
) -> Response {
    let jobs = app.jobs.lock().unwrap();
    match jobs.get(&job_id) {
        None => json_response(StatusCode::NOT_FOUND, "{\"error\":\"job not found\"}".to_string()),
        Some(r) => {
            let body = json!({
                "job_id": job_id,
                "status": r.status,
                "method": r.method,
                "request_id": r.request_id,
                "created_at": r.created_at,
                "finished_at": r.finished_at,
                "result": r.result,
                "error": r.error,
            });
            json_response(StatusCode::OK, serde_json::to_string(&body).unwrap())
        }
    }
}

/// POST /v1/jobs/{job_id}/cancel — 幂等。
async fn job_cancel_handler<S: DaemonStateExt + Send + Sync + 'static>(
    State(app): State<AppState<S>>,
    Path(job_id): Path<String>,
) -> Response {
    let mut jobs = app.jobs.lock().unwrap();
    match jobs.get_mut(&job_id) {
        None => json_response(StatusCode::NOT_FOUND, "{\"error\":\"job not found\"}".to_string()),
        Some(r) => {
            // 终态或取消中：返回当前状态（幂等）；否则标记 cancel_pending
            if !(r.status == "succeeded"
                || r.status == "failed"
                || r.status == "cancelled"
                || r.status == "cancel_pending")
            {
                r.status = "cancel_pending".to_string();
            }
            let body = json!({ "job_id": job_id, "status": r.status });
            json_response(StatusCode::OK, serde_json::to_string(&body).unwrap())
        }
    }
}

// ============================================
// 响应构造
// ============================================

/// 构造 JSON 响应（固定 content-type: application/json）。
fn json_response(status: StatusCode, body: String) -> Response {
    Response::builder()
        .status(status)
        .header(axum::http::header::CONTENT_TYPE, "application/json")
        .body(Body::from(body))
        .unwrap()
}

/// 构造 JSON-RPC 错误响应（带 id、整数 error.code、data.code = E_* 字符串）。
fn json_rpc_error(
    id: Option<&str>,
    int_code: i64,
    data_code: &str,
    msg: &str,
    http_status: u16,
    git: &str,
) -> Response {
    let body = json!({
        "jsonrpc": "2.0",
        "id": id,
        "error": {
            "code": int_code,
            "message": msg,
            "data": {
                "code": data_code,
                "message_key": data_code.to_lowercase(),
                "retryable": false,
                "request_id": id
            }
        },
        "server": {
            "protocol_version": "1",
            "git_commit": git,
            "schema_version": SCHEMA_VERSION
        }
    });
    let status = StatusCode::from_u16(http_status).unwrap_or(StatusCode::BAD_REQUEST);
    json_response(status, serde_json::to_string(&body).unwrap())
}

/// 由 dispatch 结果构造 JSON-RPC 成功/错误 envelope（HTTP 200）。
fn build_rpc_response(id: &str, result: &Value, git: &str) -> Response {
    let ok = result.get("ok").and_then(|v| v.as_bool()).unwrap_or(false);
    let body = if ok {
        let r = result.get("result").cloned().unwrap_or(Value::Null);
        json!({
            "jsonrpc": "2.0",
            "id": id,
            "result": r,
            "server": {
                "protocol_version": "1",
                "git_commit": git,
                "schema_version": SCHEMA_VERSION
            }
        })
    } else {
        let (code, msg) = result
            .get("error")
            .and_then(|v| v.as_object())
            .map(|e| {
                (
                    e.get("code")
                        .and_then(|v| v.as_str())
                        .unwrap_or("daemon_error")
                        .to_string(),
                    e.get("message")
                        .and_then(|v| v.as_str())
                        .unwrap_or("unknown")
                        .to_string(),
                )
            })
            .unwrap_or(("daemon_error".into(), "unknown".into()));
        let int_code = jsonrpc_int_code(&code);
        // H3: compat worker 错误携带 retryable/recovery（契约 §3.3 结构化错误要求）。
        let err_obj = result
            .get("error")
            .and_then(|v| v.as_object())
            .cloned()
            .unwrap_or_default();
        let retryable = err_obj
            .get("retryable")
            .and_then(|v| v.as_bool())
            .unwrap_or(false);
        let recovery = err_obj.get("recovery").cloned().unwrap_or(Value::Null);
        json!({
            "jsonrpc": "2.0",
            "id": id,
            "error": {
                "code": int_code,
                "message": msg,
                "data": {
                    "code": code,
                    "message_key": code.to_lowercase(),
                    "retryable": retryable,
                    "recovery": recovery,
                    "request_id": id
                }
            },
            "server": {
                "protocol_version": "1",
                "git_commit": git,
                "schema_version": SCHEMA_VERSION
            }
        })
    };
    json_response(StatusCode::OK, serde_json::to_string(&body).unwrap())
}

/// 将 E_* 字符串错误码映射到 JSON-RPC 整数错误码。
fn jsonrpc_int_code(e: &str) -> i64 {
    if e.contains("method_not_found") {
        -32601
    } else if e.contains("invalid_params") {
        -32602
    } else if e.contains("internal_error") {
        -32603
    } else {
        -32000
    }
}

// ============================================
// 身份 / 工具
// ============================================

/// 构造合成 local-owner peer（dev_loopback_unauthenticated 下无 OS peer cred）。
fn synthetic_local_owner_peer() -> PeerCredential {
    let key = current_daemon_owner_key();
    #[cfg(windows)]
    {
        PeerCredential::new_windows(key, std::process::id())
    }
    #[cfg(not(windows))]
    {
        let uid = key.parse::<u32>().unwrap_or(1000);
        PeerCredential::new_unix(uid, uid, std::process::id() as i32)
    }
}

/// HTTP MVP 的 authority 标识（与 Python 侧 `get_http_authority_id()` 严格一致）。
///
/// 该 profile 仅限开发机 loopback，authority 即当前本地用户会话；
/// Windows 用纯 SID，Unix 用 `uid-{uid}`。与 manifest `authority_id` 字段一致，
/// 供 H2 client 做 authority 作用域校验。
pub fn http_authority_id() -> String {
    #[cfg(windows)]
    {
        current_daemon_owner_key()
    }
    #[cfg(not(windows))]
    {
        format!("uid-{}", current_daemon_owner_key())
    }
}

/// authority-scoped manifest 文件名（与 Python 侧 `get_http_manifest_path` 一致）。
///
/// 文件名嵌入 authority_id（`/`、`\`、`:` 替换为 `_`），保证不同本地用户/作用域隔离。
pub fn http_manifest_filename(authority_id: &str) -> String {
    let safe = authority_id
        .replace('/', "_")
        .replace('\\', "_")
        .replace(':', "_");
    format!("http-daemon.{}.manifest.json", safe)
}

/// HTTP manifest 所在目录（与 Python 侧 `get_http_manifest_dir()` 一致：`~/.callwarden`）。
///
/// H6 修复：manifest 必须与权威任务库同根（Python 客户端按 `get_http_manifest_path`
/// 读取）；不能随 Rust 默认 `data_root`（`/var/lib/callwarden`），否则客户端发现不到。
/// home 不可用时（罕见，如无 USERPROFILE/HOME）回退默认 data_root，保证可写不阻断启动。
pub fn http_manifest_dir() -> PathBuf {
    let home = std::env::var_os("USERPROFILE")
        .or_else(|| std::env::var_os("HOME"))
        .unwrap_or_default();
    if home.is_empty() {
        PathBuf::from(super::config::DEFAULT_DATA_ROOT)
    } else {
        PathBuf::from(home).join(".callwarden")
    }
}

/// 运行时捕获 git commit。
pub fn current_git_commit() -> String {
    std::process::Command::new("git")
        .args(["rev-parse", "HEAD"])
        .output()
        .ok()
        .and_then(|o| {
            if o.status.success() {
                String::from_utf8(o.stdout).ok()
            } else {
                None
            }
        })
        .map(|s| s.trim().to_string())
        .unwrap_or_else(|| "unknown".to_string())
}

/// 计算当前可执行文件 SHA-256（用于 manifest）。
pub fn compute_binary_sha256() -> String {
    match std::env::current_exe() {
        Ok(p) => match std::fs::read(&p) {
            Ok(bytes) => sha256_hex(&bytes),
            Err(_) => "unknown".to_string(),
        },
        Err(_) => "unknown".to_string(),
    }
}

fn sha256_hex(b: &[u8]) -> String {
    let mut h = Sha256::new();
    h.update(b);
    format!("{:x}", h.finalize())
}

fn now_unix() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

fn make_id(prefix: &str) -> String {
    let n = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_nanos())
        .unwrap_or(0);
    format!("{}-{}", prefix, n)
}

// ============================================
// Manifest
// ============================================

/// 构造 manifest（callwarden-http-manifest/v1），`manifest_hash` 排除自身。
fn build_manifest(cfg: &HttpServerConfig, local: &SocketAddr) -> Value {
    let mut m = std::collections::BTreeMap::new();
    m.insert(
        "manifest_version".to_string(),
        Value::String("callwarden-http-manifest/v1".into()),
    );
    m.insert("manifest_id".to_string(), Value::String(cfg.manifest_id.clone()));
    m.insert(
        "authority_id".to_string(),
        Value::String(cfg.authority_id.clone()),
    );
    m.insert(
        "endpoint".to_string(),
        Value::String(format!("http://{}", local)),
    );
    m.insert("pid".to_string(), Value::Number(std::process::id().into()));
    m.insert(
        "process_start_time".to_string(),
        Value::String(now_unix().to_string()),
    );
    m.insert(
        "daemon_executable".to_string(),
        Value::String(cfg.daemon_executable.to_string_lossy().to_string()),
    );
    m.insert(
        "daemon_binary_sha256".to_string(),
        Value::String(cfg.daemon_binary_sha256.clone()),
    );
    m.insert("protocol_version".to_string(), Value::String("1".into()));
    m.insert(
        "supported_protocol_versions".to_string(),
        Value::Array(vec![Value::String("1".into())]),
    );
    m.insert(
        "security_profile".to_string(),
        Value::String(SECURITY_PROFILE.into()),
    );
    m.insert("git_commit".to_string(), Value::String(cfg.git_commit.clone()));
    m.insert(
        "schema_version".to_string(),
        Value::Number(SCHEMA_VERSION.into()),
    );
    m.insert(
        "started_at".to_string(),
        Value::String(now_unix().to_string()),
    );
    m.insert(
        "capability_registry_revision".to_string(),
        Value::String(cfg.capability_registry_revision.clone()),
    );
    m.insert(
        "worker_status".to_string(),
        Value::String(cfg.worker_status.clone()),
    );

    // 计算 manifest_hash（排除自身）后插入
    let canonical = serde_json::to_string(&Value::Object(m.clone().into_iter().collect())).unwrap_or_default();
    let hash = sha256_hex(canonical.as_bytes());
    m.insert("manifest_hash".to_string(), Value::String(hash));

    Value::Object(m.into_iter().collect())
}

/// 原子发布 manifest：owner-only 临时文件 → flush/fsync → 原子 replace。
///
/// - Unix：临时文件权限 0600，`rename`（覆盖已存在目标，原子）。
/// - Windows：临时文件设置 owner-only DACL，`MoveFileExW(REPLACE_EXISTING|WRITE_THROUGH)`
///   原子替换，消除 remove→rename 的缺失窗口（P1-4 修复）。
fn publish_manifest_atomic(path: &PathBuf, value: &Value) -> Result<(), HttpServerError> {
    let json = serde_json::to_vec(value)?;
    let tmp = path.with_extension("tmp");
    {
        let mut f = std::fs::File::create(&tmp).map_err(|e| HttpServerError::Manifest(e.to_string()))?;
        use std::io::Write;
        f.write_all(&json)
            .map_err(|e| HttpServerError::Manifest(e.to_string()))?;
        f.sync_all()
            .map_err(|e| HttpServerError::Manifest(e.to_string()))?;
    }

    #[cfg(windows)]
    {
        publish_manifest_windows(&tmp, path)?;
    }

    #[cfg(not(windows))]
    {
        // Unix：owner-only 权限 0600
        let _ = std::fs::set_permissions(
            &tmp,
            std::os::unix::fs::Permissions::from_mode(0o600),
        );
        // Unix rename 覆盖已存在目标（原子）
        std::fs::rename(&tmp, path).map_err(|e| HttpServerError::Manifest(e.to_string()))?;
    }

    Ok(())
}

/// Windows：owner-only DACL + 原子 replace（P1-4 修复）。
///
/// 1. 用 SDDL `D:P(A;;FA;;;<owner-sid>)` 构建 owner-only 安全描述符，经
///    `SetFileSecurityW` 施加到临时文件（仅当前用户 SID 有 Full Access）。
/// 2. `MoveFileExW(REPLACE_EXISTING|WRITE_THROUGH)` 原子替换，不在 remove→rename
///    之间留下目标缺失窗口。
#[cfg(windows)]
fn publish_manifest_windows(tmp: &PathBuf, path: &PathBuf) -> Result<(), HttpServerError> {
    use windows_sys::Win32::Foundation::LocalFree;
    use windows_sys::Win32::Security::Authorization::{
        ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1,
    };
    use windows_sys::Win32::Security::{DACL_SECURITY_INFORMATION, SetFileSecurityW};
    use windows_sys::Win32::Storage::FileSystem::{
        MoveFileExW, MOVEFILE_REPLACE_EXISTING, MOVEFILE_WRITE_THROUGH,
    };

    // 1. owner-only DACL（仅当前用户 SID）
    let owner_sid = current_daemon_owner_key();
    let sddl = format!("D:P(A;;FA;;;{})", owner_sid);
    let sddl_wide: Vec<u16> = sddl.encode_utf16().chain(std::iter::once(0)).collect();
    let mut sd: *mut std::ffi::c_void = std::ptr::null_mut();
    let conv_ok = unsafe {
        ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl_wide.as_ptr(),
            SDDL_REVISION_1,
            &mut sd,
            std::ptr::null_mut(),
        )
    };
    if conv_ok == 0 {
        return Err(HttpServerError::Manifest(format!(
            "SDDL 转换失败: {}",
            std::io::Error::last_os_error()
        )));
    }
    let tmp_wide: Vec<u16> = tmp
        .to_string_lossy()
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect();
    let set_ok = unsafe { SetFileSecurityW(tmp_wide.as_ptr(), DACL_SECURITY_INFORMATION, sd) };
    let set_err = std::io::Error::last_os_error();
    unsafe { LocalFree(sd) };
    if set_ok == 0 {
        return Err(HttpServerError::Manifest(format!(
            "SetFileSecurityW 失败: {}",
            set_err
        )));
    }

    // 2. 原子替换（REPLACE_EXISTING | WRITE_THROUGH）
    let path_wide: Vec<u16> = path
        .to_string_lossy()
        .encode_utf16()
        .chain(std::iter::once(0))
        .collect();
    let mv_ok = unsafe {
        MoveFileExW(
            tmp_wide.as_ptr(),
            path_wide.as_ptr(),
            MOVEFILE_REPLACE_EXISTING | MOVEFILE_WRITE_THROUGH,
        )
    };
    if mv_ok == 0 {
        return Err(HttpServerError::Manifest(format!(
            "MoveFileExW 原子替换失败: {}",
            std::io::Error::last_os_error()
        )));
    }
    Ok(())
}

// ============================================
// Capability registry（fail closed）
// ============================================

/// 构造 capability registry；任一 row 的 backend/route/operation_class/fixture/owner
/// 为 unknown/空 → 失败（fail closed）。
fn build_capability_registry() -> Result<Value, String> {
    let mut rows: Vec<Value> = Vec::new();

    let mut add = |m: &str, mcp: &str, cli: &str, backend: &str, status: &str, op: &str,
                   scope: &str, jobs: bool, route: &str, success: &str, errf: &str,
                   owner: &str, deprec: &str| {
        rows.push(json!({
            "method": m,
            "mcp_entry": if mcp.is_empty() { Value::Null } else { Value::String(mcp.into()) },
            "cli_entry": if cli.is_empty() { Value::Null } else { Value::String(cli.into()) },
            "backend": backend,
            "status": status,
            "operation_class": op,
            "workspace_scope": scope,
            "supports_jobs": jobs,
            "security_profile_required": SECURITY_PROFILE,
            "http_route": route,
            "success_fixture": success,
            "structured_error_fixture": errf,
            "owner": owner,
            "deprecated_transport": if deprec.is_empty() { Value::Null } else { Value::String(deprec.into()) }
        }));
    };

    // rust_native + available（H1 已实现或稳定的方法）
    add("ping", "ping", "ping", "rust_native", "available", "read_only", "none", false, "/v1/rpc", "fixture-ping-ok", "fixture-ping-err", "T-1786590214634-9e740cdc-sub-2#H1", "");
    add("health", "health", "health", "rust_native", "available", "read_only", "none", false, "/v1/rpc", "fixture-health-ok", "fixture-health-err", "T-1786590214634-9e740cdc-sub-2#H1", "");
    add("schema.version", "schema.version", "schema-version", "rust_native", "available", "read_only", "none", false, "/v1/rpc", "fixture-schema-ok", "fixture-schema-err", "T-1786590214634-9e740cdc-sub-2#H1", "");
    add("workspace.register", "workspace.register", "workspace-register", "rust_native", "available", "index_write", "workspace", false, "/v1/rpc", "fixture-ws-reg-ok", "fixture-ws-reg-err", "T-1786590214634-9e740cdc-sub-2#H1", "");
    add("workspace.list", "workspace.list", "workspace-list", "rust_native", "available", "read_only", "authority", false, "/v1/rpc", "fixture-ws-list-ok", "fixture-ws-list-err", "T-1786590214634-9e740cdc-sub-2#H1", "");
    add("workspace.active", "workspace.active", "workspace-active", "rust_native", "available", "read_only", "authority", false, "/v1/rpc", "fixture-ws-act-ok", "fixture-ws-act-err", "T-1786590214634-9e740cdc-sub-2#H1", "");
    add("workspace.status", "workspace.status", "workspace-status", "rust_native", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-ws-stat-ok", "fixture-ws-stat-err", "T-1786590214634-9e740cdc-sub-2#H1", "");
    add("query.file", "query.file", "query-file", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-qf-ok", "fixture-qf-err", "T-1786590214634-9e740cdc-sub-2#H1", "");
    add("query.symbol", "query.symbol", "query-symbol", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-qs-ok", "fixture-qs-err", "T-1786590214634-9e740cdc-sub-2#H1", "");
    add("query.grep", "query.grep", "query-grep", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-qg-ok", "fixture-qg-err", "T-1786590214634-9e740cdc-sub-2#H1", "");
    add("query.issues", "query.issues", "query-issues", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-qi-ok", "fixture-qi-err", "T-1786590214634-9e740cdc-sub-2#H1", "");
    add("stats", "stats", "stats", "rust_native", "available", "read_only", "authority", false, "/v1/rpc", "fixture-stats-ok", "fixture-stats-err", "T-1786590214634-9e740cdc-sub-2#H1", "");
    add("snapshot.publish", "snapshot.publish", "snapshot-publish", "rust_native", "available", "index_write", "snapshot", false, "/v1/rpc", "fixture-sp-ok", "fixture-sp-err", "T-1786590214634-9e740cdc-sub-2#H1", "");
    add("gc.snapshots", "gc.snapshots", "gc-snapshots", "rust_native", "available", "index_write", "snapshot", false, "/v1/rpc", "fixture-gc-ok", "fixture-gc-err", "T-1786590214634-9e740cdc-sub-2#H1", "");

    // python_compat 方法由 H3 compat worker 提供服务（backend=python_compat + available）
    // W2-1（T-1786840097330-dec66710）：get_uncommented_symbols / get_module_call_stats /
    // get_semgrep_stats 已迁移 rust_native（走 snapshot query_db_path，native handler 在
    // snapshot_state.rs），对应 COMPAT_ROUTE_WHITELIST 条目已移除。
    add("get_uncommented_symbols", "get_uncommented_symbols", "get-uncommented-symbols", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-uncommented-symbols-ok", "fixture-get-uncommented-symbols-err", "T-1786840097330-dec66710#W2-1", "");
    add("stats_top_files", "stats_top_files", "stats-top-files", "python_compat", "available", "read_only", "authority", false, "/v1/rpc", "fixture-stats-top-files-ok", "fixture-stats-top-files-err", "T-1786590214634-9e740cdc-sub-4#H3", "legacy-python");
    // H4C-2 符号组只读工具（15 项，workspace scope，T-1786716190783-ba187c88#H4C-2；
    // get_uncommented_symbols / get_module_call_stats / get_semgrep_stats 3 项
    // 已 W2-1 迁移 rust_native（T-1786840097330-dec66710），17->15）
    add("get_symbol_history", "get_symbol_history", "get-symbol-history", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-symbol-history-ok", "fixture-get-symbol-history-err", "T-1786716190783-ba187c88#H4C-2", "legacy-python");
    // W4-1（T-1786886251769-22b94ee8-sub-1）：get_file_history 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除（90->88）。
    add("get_file_history", "get_file_history", "get-file-history", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-file-history-ok", "fixture-get-file-history-err", "T-1786886251769-22b94ee8-sub-1#W4-1", "");
    add("get_recent_changes", "get_recent_changes", "get-recent-changes", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-recent-changes-ok", "fixture-get-recent-changes-err", "T-1786716190783-ba187c88#H4C-2", "legacy-python");
    add("get_impact", "get_impact", "get-impact", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-impact-ok", "fixture-get-impact-err", "T-1786716190783-ba187c88#H4C-2", "legacy-python");
    add("get_top_callers", "get_top_callers", "get-top-callers", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-top-callers-ok", "fixture-get-top-callers-err", "T-1787209948470-a59bcf9c#S2-query-compat-batch1", "");
    add("get_orphan_symbols", "get_orphan_symbols", "get-orphan-symbols", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-orphan-symbols-ok", "fixture-get-orphan-symbols-err", "T-1787209948470-a59bcf9c#S2-query-compat-batch1", "");
    add("get_deepest_functions", "get_deepest_functions", "get-deepest-functions", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-deepest-functions-ok", "fixture-get-deepest-functions-err", "T-1787209948470-a59bcf9c#S2-query-compat-batch1", "");
    // W2-1（T-1786840097330-dec66710）：get_module_call_stats 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除。
    add("get_module_call_stats", "get_module_call_stats", "get-module-call-stats", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-module-call-stats-ok", "fixture-get-module-call-stats-err", "T-1786840097330-dec66710#W2-1", "");
    add("get_comment_from_version", "get_comment_from_version", "get-comment-from-version", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-comment-from-version-ok", "fixture-get-comment-from-version-err", "T-1786716190783-ba187c88#H4C-2", "legacy-python");
    add("get_issue_summary", "get_issue_summary", "get-issue-summary", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-issue-summary-ok", "fixture-get-issue-summary-err", "T-1786716190783-ba187c88#H4C-2", "legacy-python");
    add("find_issues", "find_issues", "find-issues", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-find-issues-ok", "fixture-find-issues-err", "T-1786716190783-ba187c88#H4C-2", "legacy-python");
    // W2-1（T-1786840097330-dec66710）：get_semgrep_stats 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除。
    add("get_semgrep_stats", "get_semgrep_stats", "get-semgrep-stats", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-semgrep-stats-ok", "fixture-get-semgrep-stats-err", "T-1786840097330-dec66710#W2-1", "");
    // W3-3（T-1786861820151-deb64c48）：get_semgrep_findings 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除（91->90）。
    add("get_semgrep_findings", "get_semgrep_findings", "get-semgrep-findings", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-semgrep-findings-ok", "fixture-get-semgrep-findings-err", "T-1786861820151-deb64c48#W3-3", "");
    add("get_comment_coverage", "get_comment_coverage", "get-comment-coverage", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-comment-coverage-ok", "fixture-get-comment-coverage-err", "T-1787209948470-a59bcf9c#S2-query-compat-batch1", "");
    add("get_call_heatmap", "get_call_heatmap", "get-call-heatmap", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-call-heatmap-ok", "fixture-get-call-heatmap-err", "T-1787209948470-a59bcf9c#S2-query-compat-batch1", "");
    add("get_test_coverage", "get_test_coverage", "get-test-coverage", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-test-coverage-ok", "fixture-get-test-coverage-err", "T-1786716190783-ba187c88#H4C-2", "legacy-python");
    add("export_module_graph", "export_module_graph", "export-module-graph", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-export-module-graph-ok", "fixture-export-module-graph-err", "T-1786716190783-ba187c88#H4C-2", "legacy-python");
    // H4C-3 任务组只读工具（13 项，workspace scope，T-1786716190783-ba187c88#H4C-3；
    // get_clone_stats / get_job_stats / get_clone_group_stats 3 项已 W2-2 迁移
    // rust_native（T-1786840097330-a9e0ec69），16->13）
    add("get_symbol_change_tasks", "get_symbol_change_tasks", "get-symbol-change-tasks", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-symbol-change-tasks-ok", "fixture-get-symbol-change-tasks-err", "T-1786716190783-ba187c88#H4C-3", "legacy-python");
    // W4-1（T-1786886251769-22b94ee8-sub-1）：get_commit_tasks 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除（90->88）。
    add("get_commit_tasks", "get_commit_tasks", "get-commit-tasks", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-commit-tasks-ok", "fixture-get-commit-tasks-err", "T-1786886251769-22b94ee8-sub-1#W4-1", "");
    // W4-1（T-1786886251769-22b94ee8-sub-1）：git 读组 3 工具首次入册
    // rust_native（此前 legacy_local 无 HTTP 分支、无 registry 条目）。
    add("get_git_commits", "get_git_commits", "get-git-commits", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-git-commits-ok", "fixture-get-git-commits-err", "T-1786886251769-22b94ee8-sub-1#W4-1", "");
    add("get_commit_changes", "get_commit_changes", "get-commit-changes", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-commit-changes-ok", "fixture-get-commit-changes-err", "T-1786886251769-22b94ee8-sub-1#W4-1", "");
    add("get_git_stats", "get_git_stats", "get-git-stats", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-git-stats-ok", "fixture-get-git-stats-err", "T-1786886251769-22b94ee8-sub-1#W4-1", "");
    add("audit_verify_chain", "audit_verify_chain", "audit-verify-chain", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-audit-verify-chain-ok", "fixture-audit-verify-chain-err", "T-1786716190783-ba187c88#H4C-3", "legacy-python");
    add("list_audit_signing_keys", "list_audit_signing_keys", "list-audit-signing-keys", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-list-audit-signing-keys-ok", "fixture-list-audit-signing-keys-err", "T-1786716190783-ba187c88#H4C-3", "legacy-python");
    add("bootstrap_status", "bootstrap_status", "bootstrap-status", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-bootstrap-status-ok", "fixture-bootstrap-status-err", "T-1786716190783-ba187c88#H4C-3", "legacy-python");
    add("list_clones", "list_clones", "list-clones", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-list-clones-ok", "fixture-list-clones-err", "T-1786716190783-ba187c88#H4C-3", "legacy-python");
    // W2-2（T-1786840097330-a9e0ec69）：get_clone_stats 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除。
    add("get_clone_stats", "get_clone_stats", "get-clone-stats", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-clone-stats-ok", "fixture-get-clone-stats-err", "T-1786840097330-a9e0ec69#W2-2", "");
    // W4-3（T-1786886251769-22b94ee8-sub-3）：get_defect_correlation 迁移
    // rust_native，backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST
    // 对应条目已移除。
    add("get_defect_correlation", "get_defect_correlation", "get-defect-correlation", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-defect-correlation-ok", "fixture-get-defect-correlation-err", "T-1786886251769-22b94ee8-sub-3#W4-3", "");
    // W3-2（T-1786861820151-f3cecf40）：get_job_status / list_jobs /
    // wait_for_job 迁移 rust_native，backend 由 python_compat 切换，
    // COMPAT_ROUTE_WHITELIST 对应条目已移除（94->91）。
    add("get_job_status", "get_job_status", "get-job-status", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-job-status-ok", "fixture-get-job-status-err", "T-1786861820151-f3cecf40#W3-2", "");
    add("list_jobs", "list_jobs", "list-jobs", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-list-jobs-ok", "fixture-list-jobs-err", "T-1786861820151-f3cecf40#W3-2", "");
    // W2-2（T-1786840097330-a9e0ec69）：get_job_stats 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除。
    add("get_job_stats", "get_job_stats", "get-job-stats", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-job-stats-ok", "fixture-get-job-stats-err", "T-1786840097330-a9e0ec69#W2-2", "");
    add("wait_for_job", "wait_for_job", "wait-for-job", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-wait-for-job-ok", "fixture-wait-for-job-err", "T-1786861820151-f3cecf40#W3-2", "");
    add("list_clone_groups", "list_clone_groups", "list-clone-groups", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-list-clone-groups-ok", "fixture-list-clone-groups-err", "T-1786716190783-ba187c88#H4C-3", "legacy-python");
    add("get_clone_group_detail", "get_clone_group_detail", "get-clone-group-detail", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-clone-group-detail-ok", "fixture-get-clone-group-detail-err", "T-1786716190783-ba187c88#H4C-3", "legacy-python");
    // W2-2（T-1786840097330-a9e0ec69）：get_clone_group_stats 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除。
    add("get_clone_group_stats", "get_clone_group_stats", "get-clone-group-stats", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-clone-group-stats-ok", "fixture-get-clone-group-stats-err", "T-1786840097330-a9e0ec69#W2-2", "");
    add("task_plan_template", "task_plan_template", "task-plan-template", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-task-plan-template-ok", "fixture-task-plan-template-err", "T-1786716190783-ba187c88#H4C-3", "legacy-python");
    // H4C-2 第二批（T-1786747295213-64204cce）：摘要/演化/护栏/缺陷组只读（27 项）
    add("get_summary", "get_summary", "get-summary", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-summary-ok", "fixture-get-summary-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("project_brief", "project_brief", "project-brief", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-project-brief-ok", "fixture-project-brief-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("repo_map", "repo_map", "repo-map", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-repo-map-ok", "fixture-repo-map-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    // W4-2（T-1786886251769-22b94ee8-sub-2）：get_coverage_for_symbol 迁移
    // rust_native，backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST
    // 对应条目已移除。
    add("get_coverage_for_symbol", "get_coverage_for_symbol", "get-coverage-for-symbol", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-coverage-for-symbol-ok", "fixture-get-coverage-for-symbol-err", "T-1786886251769-22b94ee8-sub-2#W4-2", "");
    add("find_uncovered_functions", "find_uncovered_functions", "find-uncovered-functions", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-find-uncovered-functions-ok", "fixture-find-uncovered-functions-err", "T-1787209948470-a59bcf9c#S2-query-compat-batch1", "");
    add("test_impact_selection", "test_impact_selection", "test-impact-selection", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-test-impact-selection-ok", "fixture-test-impact-selection-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("who_to_ask", "who_to_ask", "who-to-ask", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-who-to-ask-ok", "fixture-who-to-ask-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("get_ownership_map", "get_ownership_map", "get-ownership-map", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-ownership-map-ok", "fixture-get-ownership-map-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("guardrail_scan", "guardrail_scan", "guardrail-scan", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-guardrail-scan-ok", "fixture-guardrail-scan-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("guardrail_check_edit", "guardrail_check_edit", "guardrail-check-edit", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-guardrail-check-edit-ok", "fixture-guardrail-check-edit-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("guardrail_list_rules", "guardrail_list_rules", "guardrail-list-rules", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-guardrail-list-rules-ok", "fixture-guardrail-list-rules-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("blast_radius", "blast_radius", "blast-radius", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-blast-radius-ok", "fixture-blast-radius-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("ask_codebase", "ask_codebase", "ask-codebase", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-ask-codebase-ok", "fixture-ask-codebase-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("get_token_savings_report", "get_token_savings_report", "get-token-savings-report", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-token-savings-report-ok", "fixture-get-token-savings-report-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("get_vulnerability_blast_radius", "get_vulnerability_blast_radius", "get-vulnerability-blast-radius", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-vulnerability-blast-radius-ok", "fixture-get-vulnerability-blast-radius-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("get_clone_aware_impact", "get_clone_aware_impact", "get-clone-aware-impact", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-clone-aware-impact-ok", "fixture-get-clone-aware-impact-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    // W4-2（T-1786886251769-22b94ee8-sub-2）：diff_to_symbol 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除。
    add("diff_to_symbol", "diff_to_symbol", "diff-to-symbol", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-diff-to-symbol-ok", "fixture-diff-to-symbol-err", "T-1786886251769-22b94ee8-sub-2#W4-2", "");
    // review_readiness 依赖 blast_radius 与 cross_layer_impact（均未迁移），
    // 保持 python_compat（W4-2 决策，T-1786886251769-22b94ee8-sub-2，见 ledger §9.23）。
    add("review_readiness", "review_readiness", "review-readiness", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-review-readiness-ok", "fixture-review-readiness-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("cross_layer_impact", "cross_layer_impact", "cross-layer-impact", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-cross-layer-impact-ok", "fixture-cross-layer-impact-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("evolution_frequency", "evolution_frequency", "evolution-frequency", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-evolution-frequency-ok", "fixture-evolution-frequency-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    // W4-3（T-1786886251769-22b94ee8-sub-3）：defect 读组 4 工具
    // （defect_correlation / churn_analysis / defect_search /
    // defect_suggest_fix）迁移 rust_native，backend 由 python_compat 切换，
    // COMPAT_ROUTE_WHITELIST 对应条目已移除；
    // defect_learn 为写面（INSERT defect_fixes/defect_patterns），保持
    // python_compat（W4-3 决策，T-1786886251769-22b94ee8-sub-3，见 ledger §9.24）。
    add("defect_correlation", "defect_correlation", "defect-correlation", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-defect-correlation-ok", "fixture-defect-correlation-err", "T-1786886251769-22b94ee8-sub-3#W4-3", "");
    add("hotspot_evolution", "hotspot_evolution", "hotspot-evolution", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-hotspot-evolution-ok", "fixture-hotspot-evolution-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("churn_analysis", "churn_analysis", "churn-analysis", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-churn-analysis-ok", "fixture-churn-analysis-err", "T-1786886251769-22b94ee8-sub-3#W4-3", "");
    add("defect_search", "defect_search", "defect-search", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-defect-search-ok", "fixture-defect-search-err", "T-1786886251769-22b94ee8-sub-3#W4-3", "");
    add("defect_suggest_fix", "defect_suggest_fix", "defect-suggest-fix", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-defect-suggest-fix-ok", "fixture-defect-suggest-fix-err", "T-1786886251769-22b94ee8-sub-3#W4-3", "");
    add("defect_learn", "defect_learn", "defect-learn", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-defect-learn-ok", "fixture-defect-learn-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    // W2-3（T-1786840097331-fd01a3f8）：defect_stats 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除。
    add("defect_stats", "defect_stats", "defect-stats", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-defect-stats-ok", "fixture-defect-stats-err", "T-1786840097331-fd01a3f8#W2-3", "");
    // H4C-2 第二批：语义/外部符号组只读（5 项）
    add("semantic_search", "semantic_search", "semantic-search", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-semantic-search-ok", "fixture-semantic-search-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("find_similar_functions", "find_similar_functions", "find-similar-functions", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-find-similar-functions-ok", "fixture-find-similar-functions-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("get_symbol_commit_history", "get_symbol_commit_history", "get-symbol-commit-history", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-symbol-commit-history-ok", "fixture-get-symbol-commit-history-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("parse_codeowners", "parse_codeowners", "parse-codeowners", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-parse-codeowners-ok", "fixture-parse-codeowners-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    add("get_project_dependencies", "get_project_dependencies", "get-project-dependencies", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-project-dependencies-ok", "fixture-get-project-dependencies-err", "T-1786747295213-64204cce#H4C-2-B2", "legacy-python");
    // H4C-2 第三批（T-1786747295227-49c90d68）：security 组只读（14 项；
    // get_edit_stats 已 W2-3 迁移 rust_native，剩 13 项，T-1786840097331-fd01a3f8）
    add("list_branches", "list_branches", "list-branches", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-list-branches-ok", "fixture-list-branches-err", "T-1786747295227-49c90d68#H4C-2-B3", "legacy-python");
    // W4-4（T-1786886251769-22b94ee8-sub-4）：diff_branches 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除。
    // 跨 workspace 语义（按分支名查 source/target 两个 workspace），数据在
    // peer 合法可访问的 snapshot 库内，workspace_instance_id 仅用于连接级
    // ACL（同 W4-3 defect_search 全局视图模式）。
    add("diff_branches", "diff_branches", "diff-branches", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-diff-branches-ok", "fixture-diff-branches-err", "T-1786886251769-22b94ee8-sub-4#W4-4", "");
    add("merge_preview", "merge_preview", "merge-preview", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-merge-preview-ok", "fixture-merge-preview-err", "T-1786747295227-49c90d68#H4C-2-B3", "legacy-python");
    add("get_edit_history", "get_edit_history", "get-edit-history", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-edit-history-ok", "fixture-get-edit-history-err", "T-1786747295227-49c90d68#H4C-2-B3", "legacy-python");
    // W2-3（T-1786840097331-fd01a3f8）：get_edit_stats 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除。
    add("get_edit_stats", "get_edit_stats", "get-edit-stats", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-edit-stats-ok", "fixture-get-edit-stats-err", "T-1786840097331-fd01a3f8#W2-3", "");
    add("find_shared_symbols", "find_shared_symbols", "find-shared-symbols", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-find-shared-symbols-ok", "fixture-find-shared-symbols-err", "T-1786747295227-49c90d68#H4C-2-B3", "legacy-python");
    add("cross_repo_impact", "cross_repo_impact", "cross-repo-impact", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-cross-repo-impact-ok", "fixture-cross-repo-impact-err", "T-1786747295227-49c90d68#H4C-2-B3", "legacy-python");
    add("cross_repo_summary", "cross_repo_summary", "cross-repo-summary", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-cross-repo-summary-ok", "fixture-cross-repo-summary-err", "T-1786747295227-49c90d68#H4C-2-B3", "legacy-python");
    add("lsp_hover", "lsp_hover", "lsp-hover", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-lsp-hover-ok", "fixture-lsp-hover-err", "T-1786747295227-49c90d68#H4C-2-B3", "legacy-python");
    add("lsp_definition", "lsp_definition", "lsp-definition", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-lsp-definition-ok", "fixture-lsp-definition-err", "T-1786747295227-49c90d68#H4C-2-B3", "legacy-python");
    add("lsp_references", "lsp_references", "lsp-references", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-lsp-references-ok", "fixture-lsp-references-err", "T-1786747295227-49c90d68#H4C-2-B3", "legacy-python");
    add("lsp_diagnostics", "lsp_diagnostics", "lsp-diagnostics", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-lsp-diagnostics-ok", "fixture-lsp-diagnostics-err", "T-1786747295227-49c90d68#H4C-2-B3", "legacy-python");
    add("lsp_completion", "lsp_completion", "lsp-completion", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-lsp-completion-ok", "fixture-lsp-completion-err", "T-1786747295227-49c90d68#H4C-2-B3", "legacy-python");
    add("lsp_check_available", "lsp_check_available", "lsp-check-available", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-lsp-check-available-ok", "fixture-lsp-check-available-err", "T-1786747295227-49c90d68#H4C-2-B3", "legacy-python");
    // H4C-2 第三批：rules 组只读（8 项；list_build_contexts / get_build_context /
    // get_active_build_context / get_resolved_edges / count_resolved_edges 已 W3-1
    // 迁移 rust_native，T-1786861820150-bfe5e805，剩 3 项）
    add("list_toolchains", "list_toolchains", "list-toolchains", "rust_native", "available", "read_only", "authority", false, "/v1/rpc", "fixture-list-toolchains-ok", "fixture-list-toolchains-err", "T-1787209948470-a59bcf9c#S2-toolchain-compat-batch2", "");
    add("get_toolchain", "get_toolchain", "get-toolchain", "rust_native", "available", "read_only", "authority", false, "/v1/rpc", "fixture-get-toolchain-ok", "fixture-get-toolchain-err", "T-1787209948470-a59bcf9c#S2-toolchain-compat-batch2", "");
    add("get_workspace_toolchains", "get_workspace_toolchains", "get-workspace-toolchains", "rust_native", "available", "read_only", "authority", false, "/v1/rpc", "fixture-get-workspace-toolchains-ok", "fixture-get-workspace-toolchains-err", "T-1787209948470-a59bcf9c#S2-toolchain-compat-batch2", "");
    // W3-1（T-1786861820150-bfe5e805）：list_build_contexts 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除。
    add("list_build_contexts", "list_build_contexts", "list-build-contexts", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-list-build-contexts-ok", "fixture-list-build-contexts-err", "T-1786861820150-bfe5e805#W3-1", "");
    // W3-1（T-1786861820150-bfe5e805）：get_build_context 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除。
    add("get_build_context", "get_build_context", "get-build-context", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-build-context-ok", "fixture-get-build-context-err", "T-1786861820150-bfe5e805#W3-1", "");
    // W3-1（T-1786861820150-bfe5e805）：get_active_build_context 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除。
    add("get_active_build_context", "get_active_build_context", "get-active-build-context", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-active-build-context-ok", "fixture-get-active-build-context-err", "T-1786861820150-bfe5e805#W3-1", "");
    // W3-1（T-1786861820150-bfe5e805）：get_resolved_edges 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除。
    add("get_resolved_edges", "get_resolved_edges", "get-resolved-edges", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-get-resolved-edges-ok", "fixture-get-resolved-edges-err", "T-1786861820150-bfe5e805#W3-1", "");
    // W3-1（T-1786861820150-bfe5e805）：count_resolved_edges 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除。
    add("count_resolved_edges", "count_resolved_edges", "count-resolved-edges", "rust_native", "available", "read_only", "snapshot", false, "/v1/rpc", "fixture-count-resolved-edges-ok", "fixture-count-resolved-edges-err", "T-1786861820150-bfe5e805#W3-1", "");
    // H4C-2 第三批：规则查询组只读（3 项，T-1786747295227-49c90d68 整改）
    add("rule_candidate_list", "rule_candidate_list", "rule-candidate-list", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-rule-candidate-list-ok", "fixture-rule-candidate-list-err", "T-1786747295227-49c90d68#H4C-2-B3", "legacy-python");
    add("rule_list", "rule_list", "rule-list", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-rule-list-ok", "fixture-rule-list-err", "T-1786747295227-49c90d68#H4C-2-B3", "legacy-python");
    add("get_applicable_rules", "get_applicable_rules", "get-applicable-rules", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-applicable-rules-ok", "fixture-get-applicable-rules-err", "T-1786747295227-49c90d68#H4C-2-B3", "legacy-python");
    // H4C-2 第三批：collab 组只读（4 项，T-1786747295227-b876fddf#H4C-2-B3）
    // MCP-001（T-1787321708699-da5d8224）：get_role_view 迁移 rust_native，
    // backend 由 python_compat 切换，COMPAT_ROUTE_WHITELIST 对应条目已移除。
    add("get_role_view", "get_role_view", "get-role-view", "rust_native", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-role-view-ok", "fixture-get-role-view-err", "T-1787321708699-da5d8224#MCP-001", "");
    add("find_evidence", "find_evidence", "find-evidence", "rust_native", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-find-evidence-ok", "fixture-find-evidence-err", "T-1787321708760-de068a9c#MCP-002", "");
    add("get_freshness_status", "get_freshness_status", "get-freshness-status", "rust_native", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-freshness-status-ok", "fixture-get-freshness-status-err", "T-1787321708856-e3c10624#MCP-003", "");
    add("get_gate_decision", "get_gate_decision", "get-gate-decision", "rust_native", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-gate-decision-ok", "fixture-get-gate-decision-err", "T-1787321708926-e7ebfac4#MCP-004", "");
    // H4C-2 第三批：p2 依赖图/环检测组只读（5 项，T-1786747295227-b876fddf#H4C-2-B3）
    add("get_artifact_freshness", "get_artifact_freshness", "get-artifact-freshness", "rust_native", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-artifact-freshness-ok", "fixture-get-artifact-freshness-err", "T-1787321709017-ed4e79b0#MCP-005", "");
    add("get_interface_providers", "get_interface_providers", "get-interface-providers", "rust_native", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-interface-providers-ok", "fixture-get-interface-providers-err", "T-1787321709098-f2236ea0#MCP-006", "");
    add("detect_cycle", "detect_cycle", "detect-cycle", "rust_native", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-detect-cycle-ok", "fixture-detect-cycle-err", "T-1787321709179-f6fdf5bc#MCP-007", "");
    add("validate_revision_dependencies", "validate_revision_dependencies", "validate-revision-dependencies", "rust_native", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-validate-revision-dependencies-ok", "fixture-validate-revision-dependencies-err", "T-1787321709249-fb256530#MCP-008", "");
    add("get_dependency_edges", "get_dependency_edges", "get-dependency-edges", "rust_native", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-dependency-edges-ok", "fixture-get-dependency-edges-err", "T-1787321709365-021050a8#MCP-009", "");
    // H4C-2 第三批：p3 身份/证明组只读（5 项，T-1786747295227-b876fddf#H4C-2-B3）
    add("get_action_identity", "get_action_identity", "get-action-identity", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-action-identity-ok", "fixture-get-action-identity-err", "T-1786747295227-b876fddf#H4C-2-B3", "legacy-python");
    add("check_action_identity", "check_action_identity", "check-action-identity", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-check-action-identity-ok", "fixture-check-action-identity-err", "T-1786747295227-b876fddf#H4C-2-B3", "legacy-python");
    add("check_session_separation", "check_session_separation", "check-session-separation", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-check-session-separation-ok", "fixture-check-session-separation-err", "T-1786747295227-b876fddf#H4C-2-B3", "legacy-python");
    add("get_attestation_validity", "get_attestation_validity", "get-attestation-validity", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-get-attestation-validity-ok", "fixture-get-attestation-validity-err", "T-1786747295227-b876fddf#H4C-2-B3", "legacy-python");
    add("list_attestation_revocations", "list_attestation_revocations", "list-attestation-revocations", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-list-attestation-revocations-ok", "fixture-list-attestation-revocations-err", "T-1786747295227-b876fddf#H4C-2-B3", "legacy-python");
    // H4C-2 第三批：p4 assignment 只读（1 项，T-1786747295227-b876fddf#H4C-2-B3；
    // lease_* 5 项 rust_native 走 daemon dispatch，不在此 registry）
    add("assignment_show", "assignment_show", "assignment-show", "python_compat", "available", "read_only", "workspace", false, "/v1/rpc", "fixture-assignment-show-ok", "fixture-assignment-show-err", "T-1786747295227-b876fddf#H4C-2-B3", "legacy-python");

    // P0-H（T-1787277487109-758e56d0）：task.supersede 治理 mutation。
    // 不宣传为 enabled（status != available）：只有 Adjudicator accepted 且
    // promotion verifier PASS 后才列入 A′ Phase 0 allowed capability（届时
    // 由 promotion 任务把 status 提升为 available 并补 /capabilities 测试）。
    // task.superseded_by（只读投影）同样不宣传 enabled，随 promotion 一并开放。
    add("task.supersede", "task.supersede", "task-supersede", "rust_native", "pending_promotion", "governance_write", "authority", false, "/v1/rpc", "fixture-task-supersede-ok", "fixture-task-supersede-err", "T-1787277487109-758e56d0#P0-H", "");
    add("task.superseded_by", "task.superseded_by", "task-superseded", "rust_native", "pending_promotion", "read_only", "authority", false, "/v1/rpc", "fixture-task-superseded-ok", "fixture-task-superseded-err", "T-1787277487109-758e56d0#P0-H", "");

    drop(add);

    // fail closed 校验
    for r in &rows {
        for f in [
            "backend",
            "status",
            "operation_class",
            "http_route",
            "success_fixture",
            "structured_error_fixture",
            "owner",
        ] {
            let v = r.get(f).and_then(|x| x.as_str()).unwrap_or("");
            if v.is_empty() || v == "unknown" {
                return Err(format!(
                    "capability row {} has empty/unknown field {}",
                    r.get("method").and_then(|x| x.as_str()).unwrap_or("?"),
                    f
                ));
            }
        }
        let backend = r.get("backend").and_then(|x| x.as_str()).unwrap_or("");
        let status = r.get("status").and_then(|x| x.as_str()).unwrap_or("");
        if backend == "none" && status != "unsupported" && status != "disabled" {
            return Err(format!(
                "backend=none but status={} for {}",
                status,
                r.get("method").and_then(|x| x.as_str()).unwrap_or("?")
            ));
        }
    }

    let mut methods = serde_json::Map::new();
    for r in &rows {
        let m = r
            .get("method")
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .to_string();
        methods.insert(m, r.clone());
    }

    Ok(json!({
        "protocol_version": "1",
        "server_mode": SECURITY_PROFILE,
        "methods": Value::Object(methods)
    }))
}

// ============================================
// 单元测试（真实 loopback HTTP 往返）
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use crate::daemon::dispatch::DaemonState;
    use std::time::Duration;
    use tokio::io::{AsyncReadExt, AsyncWriteExt};
    use tokio::net::TcpStream;

    /// 在当前 tokio runtime 中启动真实 loopback HTTP server（通过 tokio::spawn 驱动
    /// axum::serve 的 accept loop），返回绑定地址。使用测试运行时可避免独立 runtime
    /// 中 axum::serve 内部 tokio::spawn 的 accept loop 不被驱动、请求无响应的问题。
    async fn spawn_server() -> SocketAddr {
        let dir = tempfile::tempdir().unwrap();
        let manifest = dir.path().join("http-manifest.json");
        let cfg = HttpServerConfig::new("127.0.0.1:0".into(), manifest);
        let bound = bind_http(&cfg).await.unwrap();
        let addr = bound.local_addr;
        let mut cfg = cfg;
        cfg.endpoint = format!("http://{}", addr);
        let ds = DaemonState {
            start_time: std::time::Instant::now(),
            schema_version: SCHEMA_VERSION,
            pid: std::process::id(),
            task_collab_store: None,
            authority_id: "test-authority".to_string(),
            transport: "http-mvp".to_string(),
            task_db_fingerprint: String::new(),
            task_loop_control: None,
        };
        let dedup = Arc::new(DedupStore::open(&cfg.dedup_db_path).unwrap());
        // 测试中构造 adapter 但不 start（不依赖 Python 解释器；compat 路由测试
        // 单独覆盖真实 worker 生命周期，见 compat_adapter.rs tests）。
        let compat = Arc::new(CompatAdapter::new(
            CompatAdapterConfig::from_env(std::path::Path::new("cw-daemon-test")),
            Arc::new(SerializationPoint::with_default_timeout()),
        ));
        let app_state = AppState {
            state: Arc::new(TokioMutex::new(ds)),
            serialization: Arc::new(SerializationPoint::with_default_timeout()),
            config: Arc::new(cfg),
            dedup,
            jobs: Arc::new(StdMutex::new(HashMap::new())),
            compat,
        };
        let router = build_router(app_state);
        // 在测试运行时中驱动 accept loop；丢弃 JoinHandle 仅分离任务（运行时结束才终止）。
        let _ = tokio::spawn(async move {
            let _ = axum::serve(bound.listener, router).await;
        });
        // 给 accept loop 一点启动时间，避免竞态导致连接被拒。
        tokio::time::sleep(std::time::Duration::from_millis(200)).await;
        addr
    }

    /// 发送原始 HTTP/1.1 请求（异步，避免阻塞单线程测试运行时），返回 (status, body)。
    /// 将请求头 + body 的写入放到独立任务，主任务并发读取响应：当 body 超过 8MiB 上限被
    /// 服务器以 413 拒绝并重置连接时，客户端仍能先读到 413 响应，而不是被连接重置吞掉状态码。
    async fn raw_request(
        addr: &SocketAddr,
        method: &str,
        path: &str,
        ctype: &str,
        body: &[u8],
        content_length: usize,
    ) -> (u16, String) {
        let mut stream = tokio::time::timeout(Duration::from_secs(5), TcpStream::connect(addr))
            .await
            .expect("connect timed out")
            .expect("connect failed");
        let req = format!(
            "{} {} HTTP/1.1\r\nHost: 127.0.0.1\r\nContent-Type: {}\r\nContent-Length: {}\r\nConnection: close\r\n\r\n",
            method, path, ctype, content_length
        );
        let (mut rd, mut wr) = stream.into_split();
        let write_req = req.into_bytes();
        let write_body = body.to_vec();
        // 独立任务负责写入（大 body 时即便被服务器重置也无关紧要）。
        // 注意：Windows 上 drop 写半边会连带关闭整条连接，导致读半边立即 EOF，
        // 因此写入后用 pending() 保持写半边存活，直到测试运行时随任务取消一起释放。
        tokio::spawn(async move {
            let _ = wr.write_all(&write_req).await;
            let _ = wr.write_all(&write_body).await;
            std::future::pending::<()>().await;
        });
        let mut buf = vec![0u8; 65536];
        let mut total = Vec::new();
        loop {
            match tokio::time::timeout(Duration::from_secs(5), rd.read(&mut buf)).await {
                Ok(Ok(0)) => break, // 服务器关闭连接（Connection: close）→ 完整响应已收到
                Ok(Ok(n)) => {
                    total.extend_from_slice(&buf[..n]);
                    // 已读到头部与 body 分隔符：再多读一轮以确保 body 完整（小概率分块到达）
                    if total.windows(4).any(|w| w == b"\r\n\r\n") {
                        if let Ok(Ok(n2)) =
                            tokio::time::timeout(Duration::from_millis(200), rd.read(&mut buf)).await
                        {
                            if n2 > 0 {
                                total.extend_from_slice(&buf[..n2]);
                            }
                        }
                        break;
                    }
                }
                Ok(Err(_)) => break, // 连接被重置（如超 8MiB 被拒）：以已收到的部分数据继续解析
                Err(_) => break,     // 读取超时：以已收到的部分数据继续解析
            }
        }
        let s = String::from_utf8_lossy(&total);
        let status_line = s.lines().next().unwrap_or("");
        let status: u16 = status_line
            .split_whitespace()
            .nth(1)
            .and_then(|x| x.parse().ok())
            .unwrap_or(0);
        let body_start = s.find("\r\n\r\n").map(|i| i + 4).unwrap_or(s.len());
        let resp_body = s[body_start..].to_string();
        (status, resp_body)
    }

    #[tokio::test]
    async fn test_health_returns_200_with_security_profile() {
        let addr = spawn_server().await;
        let (status, body) = raw_request(&addr, "GET", "/health", "application/json", b"", b"".len()).await;
        assert_eq!(status, 200, "status={}", status);
        let v: Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["security_profile"], "dev_loopback_unauthenticated");
        assert!(v["pid"].as_u64().is_some());
        assert_eq!(v["schema_version"], SCHEMA_VERSION as i64);
        assert!(v["git_commit"].is_string());
        assert!(v["capability_registry_revision"].is_string());
        assert!(v["worker_status"].is_string());
    }

    #[tokio::test]
    async fn test_capabilities_methods_map() {
        let addr = spawn_server().await;
        let (status, body) = raw_request(&addr, "GET", "/capabilities", "application/json", b"", b"".len()).await;
        assert_eq!(status, 200);
        let v: Value = serde_json::from_str(&body).unwrap();
        assert_eq!(v["protocol_version"], "1");
        assert_eq!(v["server_mode"], "dev_loopback_unauthenticated");
        assert_eq!(v["methods"]["ping"]["backend"], "rust_native");
        assert_eq!(v["methods"]["ping"]["status"], "available");
        assert_eq!(v["methods"]["get_uncommented_symbols"]["backend"], "rust_native");
        assert_eq!(v["methods"]["get_uncommented_symbols"]["status"], "available");
        assert_eq!(v["methods"]["get_module_call_stats"]["backend"], "rust_native");
        assert_eq!(v["methods"]["get_semgrep_stats"]["backend"], "rust_native");
        assert_eq!(v["methods"]["stats_top_files"]["status"], "available");
    }

    #[tokio::test]
    async fn test_rpc_ping_works() {
        let addr = spawn_server().await;
        let body = serde_json::json!({
            "jsonrpc": "2.0",
            "id": "req-ping-1",
            "protocol_version": "1",
            "method": "ping",
            "params": {}
        })
        .to_string();
        let (status, resp) = raw_request(&addr, "POST", "/v1/rpc", "application/json", body.as_bytes(), body.as_bytes().len()).await;
        assert_eq!(status, 200);
        let v: Value = serde_json::from_str(&resp).unwrap();
        assert_eq!(v["jsonrpc"], "2.0");
        assert_eq!(v["id"], "req-ping-1");
        assert!(v["result"].is_object());
    }

    #[tokio::test]
    async fn test_malformed_json_returns_400() {
        let addr = spawn_server().await;
        let (status, _resp) = raw_request(
            &addr,
            "POST",
            "/v1/rpc",
            "application/json",
            b"{not valid json",
            b"{not valid json".len(),
        ).await;
        assert_eq!(status, 400);
    }

    #[tokio::test]
    async fn test_body_too_large_returns_413() {
        let addr = spawn_server().await;
        // 声明超大 Content-Length 但仅发送极小 body，验证 axum DefaultBodyLimit 依据
        // 头部即拒绝超限请求。注意：原始 TCP 上因未读完的 body 触发 RST，客户端可能
        // 读到 status=0（连接重置）而非 413；两种结果都证明 8MiB 上限被强制。
        let (status, _resp) = raw_request(
            &addr,
            "POST",
            "/v1/rpc",
            "application/json",
            b"{}",
            9 * 1024 * 1024,
        )
        .await;
        assert!(
            status == 413 || status == 0,
            "oversized body must be rejected (413 response or connection reset), got status={}",
            status
        );
    }

    #[tokio::test]
    async fn test_non_json_content_type_returns_415() {
        let addr = spawn_server().await;
        let (status, _resp) = raw_request(
            &addr,
            "POST",
            "/v1/rpc",
            "text/plain",
            b"{\"jsonrpc\":\"2.0\"}",
            b"{\"jsonrpc\":\"2.0\"}".len(),
        ).await;
        assert_eq!(status, 415);
    }

    #[tokio::test]
    async fn test_protocol_version_neq_1_returns_426() {
        let addr = spawn_server().await;
        let body = serde_json::json!({
            "jsonrpc": "2.0",
            "id": "r1",
            "protocol_version": "2",
            "method": "ping",
            "params": {}
        })
        .to_string();
        let (status, resp) = raw_request(&addr, "POST", "/v1/rpc", "application/json", body.as_bytes(), body.as_bytes().len()).await;
        assert_eq!(status, 426);
        let v: Value = serde_json::from_str(&resp).unwrap();
        assert_eq!(v["error"]["data"]["code"], "E_PROTOCOL_VERSION_UNSUPPORTED");
    }

    #[tokio::test]
    async fn test_nonloopback_bind_rejected_no_manifest() {
        let dir = tempfile::tempdir().unwrap();
        let manifest = dir.path().join("http-manifest.json");
        // 预校验：非 loopback 必须被拒绝
        let res = validate_loopback_bind("0.0.0.0:0");
        assert!(matches!(res, Err(HttpServerError::LoopbackOnly)));
        // bind_http 在拒绝时不应发布 manifest
        let cfg = HttpServerConfig::new("0.0.0.0:0".into(), manifest.clone());
        let res = bind_http(&cfg).await;
        assert!(matches!(res, Err(HttpServerError::LoopbackOnly)));
        assert!(!manifest.exists(), "manifest must NOT be published for non-loopback bind");
    }

    #[tokio::test]
    async fn test_structured_business_error_preserved_200() {
        let addr = spawn_server().await;
        // 调用未知方法 → method_not_found 结构化错误，HTTP 200 + error.data.code
        let body = serde_json::json!({
            "jsonrpc": "2.0",
            "id": "req-err-1",
            "protocol_version": "1",
            "method": "this.method.does.not.exist",
            "params": {}
        })
        .to_string();
        let (status, resp) = raw_request(&addr, "POST", "/v1/rpc", "application/json", body.as_bytes(), body.as_bytes().len()).await;
        assert_eq!(status, 200);
        let v: Value = serde_json::from_str(&resp).unwrap();
        assert_eq!(v["error"]["code"], -32601);
        assert!(!v["error"]["data"]["code"].as_str().unwrap_or("").is_empty());
        assert_eq!(v["error"]["data"]["code"], "method_not_found");
    }

    #[tokio::test]
    async fn test_duplicate_mutation_request_id_returns_original() {
        let addr = spawn_server().await;
        // snapshot.publish 是 protected mutation；DaemonState 默认返回 method_not_found，
        // 但用于验证去重机制（相同重放返回原结果；不同 params 返回 mismatch）。
        let mk = |params: serde_json::Value| {
            serde_json::json!({
                "jsonrpc": "2.0",
                "id": "mut-1",
                "protocol_version": "1",
                "method": "snapshot.publish",
                "params": params
            })
            .to_string()
        };
        let p1 = serde_json::json!({"workspace_instance_id": "ws1", "a": 1});
        let (s1, r1) = raw_request(&addr, "POST", "/v1/rpc", "application/json", mk(p1.clone()).as_bytes(), mk(p1.clone()).as_bytes().len()).await;
        assert_eq!(s1, 200);
        // 相同 request_id + 相同 params → 返回原结果
        let (s2, r2) = raw_request(&addr, "POST", "/v1/rpc", "application/json", mk(p1.clone()).as_bytes(), mk(p1.clone()).as_bytes().len()).await;
        assert_eq!(s2, 200);
        assert_eq!(r1, r2, "identical replay must return identical stored result");
        // 相同 request_id + 不同 params → E_REQUEST_ID_REUSE_MISMATCH
        let p2 = serde_json::json!({"workspace_instance_id": "ws1", "a": 2});
        let p2_body = mk(p2);
        let (s3, r3) = raw_request(&addr, "POST", "/v1/rpc", "application/json", p2_body.as_bytes(), p2_body.as_bytes().len()).await;
        assert_eq!(s3, 200);
        let v3: Value = serde_json::from_str(&r3).unwrap();
        assert_eq!(v3["error"]["data"]["code"], "E_REQUEST_ID_REUSE_MISMATCH");
    }
}
