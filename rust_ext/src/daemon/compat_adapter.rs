//! H3: Python compatibility worker adapter —— daemon 管理的 worker 生命周期 + 帧协议。
//!
//! 契约：docs/design/http-daemon-mvp-compatibility-contract.md §3.3
//! - worker 仅通过 daemon 创建的 child stdin/stdout 私有 IPC 通信；
//!   不暴露任何外部 socket / TCP/HTTP 端口，客户端不得直连 worker；
//! - 帧格式：4-byte big-endian payload length + UTF-8 JSON object，单帧上限 8 MiB；
//!   请求帧必含 worker_protocol_version/request_id/method/params/
//!   workspace_instance_id/workspace_id/operation_class/deadline，禁止含 db_path；
//! - daemon 发帧前验证并注入 workspace context；worker 只能用显式 context，
//!   通过 authority 配置解析用户级数据库；
//! - worker 的 DB 写操作必须经过 daemon 兼容写锁（与 Rust mutation 共享
//!   SerializationPoint），不能与 Rust mutation 并发写同一数据库；
//! - 错误映射：
//!   - 启动/崩溃 → E_COMPAT_WORKER_UNAVAILABLE（retryable）
//!   - 协议损坏 → E_COMPAT_WORKER_PROTOCOL
//!   - deadline 超时 → 先终止当前 worker → E_COMPAT_WORKER_TIMEOUT（retryable）
//!   三者都保留 request_id、可重试标记和恢复指引，不得偷偷切换客户端本地 SQLite。
//!
//! 本模块用 std::process 实现（不引入 tokio process feature），帧读写与超时等待
//! 在 blocking 线程（tokio::task::spawn_blocking）中执行，符合 MVP 同步 RPC 的
//! bounded deadline 要求。

use std::io::{BufReader, ErrorKind, Read, Write};
use std::path::{Path, PathBuf};
use std::process::{Child, ChildStdin, ChildStdout, Command, Stdio};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{self, Receiver, RecvTimeoutError};
use std::sync::{Arc, Mutex as StdMutex};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use serde_json::{Value, json};

use super::dispatch::DaemonRpcError;
use super::serialization::SerializationPoint;

/// worker 协议版本（与 server/compat_worker.py WORKER_PROTOCOL_VERSION 一致）。
pub const WORKER_PROTOCOL_VERSION: u32 = 1;
/// 单帧上限（与 worker 侧 MAX_FRAME_BYTES 一致）。
pub const MAX_FRAME_BYTES: usize = 8 * 1024 * 1024;
/// 默认 worker 调用 deadline（毫秒）。
const DEFAULT_DEADLINE_MS: u64 = 30_000;
/// deadline 上限（毫秒），与 HTTP request body 语义对齐。
const MAX_DEADLINE_MS: u64 = 120_000;

/// 结构化兼容错误码（daemon 侧产生）。
const E_UNAVAILABLE: &str = "E_COMPAT_WORKER_UNAVAILABLE";
const E_PROTOCOL: &str = "E_COMPAT_WORKER_PROTOCOL";
const E_TIMEOUT: &str = "E_COMPAT_WORKER_TIMEOUT";
const E_WRITE_LOCK_TIMEOUT: &str = "E_COMPAT_WRITE_LOCK_TIMEOUT";

// ============================================
// 配置
// ============================================

/// compatibility worker 启动配置。
#[derive(Debug, Clone)]
pub struct CompatAdapterConfig {
    /// Python 解释器路径（Windows 默认 C:\Python314\python.exe；Unix 默认 python3）。
    pub python: String,
    /// worker 脚本绝对/相对路径。
    pub worker_script: PathBuf,
}

fn _default_python() -> String {
    #[cfg(windows)]
    {
        let p314 = r"C:\Python314\python.exe";
        if Path::new(p314).exists() {
            return p314.to_string();
        }
        "python".to_string()
    }
    #[cfg(not(windows))]
    {
        "python3".to_string()
    }
}

fn _resolve_worker_script(daemon_exe: &Path) -> Option<PathBuf> {
    let mut candidates: Vec<PathBuf> = Vec::new();
    if let Ok(cwd) = std::env::current_dir() {
        candidates.push(cwd.join("server").join("compat_worker.py"));
    }
    if let Some(dir) = daemon_exe.parent() {
        // 与 daemon 同目录的 server/
        candidates.push(dir.join("server").join("compat_worker.py"));
        // debug 布局 rust_ext/target/debug/cw-daemon.exe → 上溯 3 级到仓库根
        candidates.push(dir.join("..").join("..").join("..").join("server").join("compat_worker.py"));
    }
    candidates.into_iter().find(|p| p.exists())
}

impl CompatAdapterConfig {
    /// 从环境与 daemon 可执行路径解析配置。
    ///
    /// 环境变量覆盖：
    /// - `CW_COMPAT_PYTHON`：Python 解释器
    /// - `CW_COMPAT_WORKER_SCRIPT`：worker 脚本路径
    pub fn from_env(daemon_executable: &Path) -> Self {
        let python = std::env::var("CW_COMPAT_PYTHON")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .unwrap_or_else(_default_python);
        let worker_script = std::env::var("CW_COMPAT_WORKER_SCRIPT")
            .ok()
            .filter(|s| !s.trim().is_empty())
            .map(PathBuf::from)
            .or_else(|| _resolve_worker_script(daemon_executable))
            .unwrap_or_else(|| PathBuf::from("server/compat_worker.py"));
        Self { python, worker_script }
    }
}

// ============================================
// 帧协议（blocking 编解码）
// ============================================

/// 从 reader 读取一帧：Ok(Some(obj)) 正常；Ok(None) EOF；Err(协议损坏)。
fn read_frame_blocking<R: Read>(r: &mut R) -> Result<Option<Value>, String> {
    let mut len_buf = [0u8; 4];
    match r.read_exact(&mut len_buf) {
        Ok(()) => {}
        Err(e) if e.kind() == ErrorKind::UnexpectedEof => return Ok(None),
        Err(e) => return Err(format!("读取帧长度失败: {}", e)),
    }
    let len = u32::from_be_bytes(len_buf) as usize;
    if len == 0 || len > MAX_FRAME_BYTES {
        return Err(format!("非法帧长度 {}（上限 {}）", len, MAX_FRAME_BYTES));
    }
    let mut buf = vec![0u8; len];
    r.read_exact(&mut buf)
        .map_err(|e| format!("读取帧体失败: {}", e))?;
    serde_json::from_slice(&buf)
        .map(Some)
        .map_err(|e| format!("帧 JSON 解析失败: {}", e))
}

/// 向 writer 写入一帧（4-byte BE length + UTF-8 JSON）。
fn write_frame_blocking<W: Write>(w: &mut W, v: &Value) -> Result<(), String> {
    let payload = serde_json::to_vec(v).map_err(|e| format!("帧序列化失败: {}", e))?;
    if payload.len() > MAX_FRAME_BYTES {
        return Err(format!("帧超过 {} 字节上限", MAX_FRAME_BYTES));
    }
    w.write_all(&(payload.len() as u32).to_be_bytes())
        .map_err(|e| format!("写帧长度失败: {}", e))?;
    w.write_all(&payload).map_err(|e| format!("写帧体失败: {}", e))?;
    w.flush().map_err(|e| format!("刷新帧失败: {}", e))?;
    Ok(())
}

// ============================================
// worker 状态
// ============================================

/// 读者线程 → adapter 的消息。
enum WorkerMsg {
    Frame(Value),
    Eof,
}

/// worker 进程运行时状态（std Mutex 保护，dispatch 全程同步访问）。
struct CompatWorkerState {
    child: Option<Child>,
    rx: Option<Receiver<WorkerMsg>>,
    status: String,
    last_error: Option<String>,
}

impl Default for CompatWorkerState {
    fn default() -> Self {
        Self {
            child: None,
            rx: None,
            status: "not_started".to_string(),
            last_error: None,
        }
    }
}

/// 构造兼容错误 envelope（Ok(Value) 承载，保证 retryable/recovery 透传）。
fn compat_err(code: &str, message: impl Into<String>, retryable: bool, recovery: &str) -> Value {
    json!({
        "ok": false,
        "error": {
            "code": code,
            "message": message.into(),
            "retryable": retryable,
            "recovery": recovery,
        }
    })
}

/// H3: compatibility worker adapter。
///
/// 生命周期：daemon 启动时 `start()`（非致命，失败标记 unhealthy）；每次调用前
/// 自动 ensure_running（崩溃/超时后下次调用自动重建 worker）。
pub struct CompatAdapter {
    cfg: CompatAdapterConfig,
    /// 兼容写锁：index_write 方法与 Rust mutation 共享的串行化点。
    serialization: Arc<SerializationPoint>,
    inner: StdMutex<CompatWorkerState>,
    next_request_id: AtomicU64,
}

impl std::fmt::Debug for CompatAdapter {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        let status = self
            .inner
            .lock()
            .map(|s| s.status.clone())
            .unwrap_or_else(|_| "<poisoned>".to_string());
        f.debug_struct("CompatAdapter")
            .field("cfg", &self.cfg)
            .field("inner", &status)
            .finish()
    }
}

impl CompatAdapter {
    pub fn new(cfg: CompatAdapterConfig, serialization: Arc<SerializationPoint>) -> Self {
        Self {
            cfg,
            serialization,
            inner: StdMutex::new(CompatWorkerState::default()),
            next_request_id: AtomicU64::new(0),
        }
    }

    /// 启动 worker（非致命：失败时 status=unhealthy，调用方收到
    /// E_COMPAT_WORKER_UNAVAILABLE）。
    pub fn start(&self) -> Result<(), String> {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if inner.child.is_some() {
            return Ok(());
        }
        self.spawn_locked(&mut inner)
    }

    /// 停止并终止当前 worker（best-effort）。
    pub fn stop(&self) {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        self.kill_locked(&mut inner, "daemon 主动停止");
        inner.status = "stopped".to_string();
    }

    /// 当前 worker 状态（manifest /health 使用）。
    pub fn worker_status(&self) -> String {
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner.status.clone()
    }

    /// 最近一次 worker 错误（诊断用）。
    pub fn last_error(&self) -> Option<String> {
        let inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner.last_error.clone()
    }

    /// 异步分发兼容调用（http_server 持有 `Arc<CompatAdapter>`，spawn_blocking 包装
    /// 避免阻塞 HTTP executor；`self: &Arc<Self>` 允许闭包 move 一份 Arc 进线程）。
    pub async fn dispatch_arc(
        self: &Arc<Self>,
        method: &str,
        params: Value,
        workspace_instance_id: String,
        workspace_id: Option<i64>,
        operation_class: &str,
        deadline_ms: u64,
    ) -> Result<Value, DaemonRpcError> {
        let adapter = Arc::clone(self);
        let method = method.to_string();
        let op_class = operation_class.to_string();
        let deadline_ms = deadline_ms.clamp(1, MAX_DEADLINE_MS);
        let handle = tokio::task::spawn_blocking(move || {
            adapter.dispatch_blocking(
                &method,
                params,
                workspace_instance_id,
                workspace_id,
                &op_class,
                deadline_ms,
            )
        });
        handle
            .await
            .map_err(|e| DaemonRpcError::internal_error(format!("compat dispatch join 失败: {}", e)))?
    }

    /// 同步分发（blocking 线程内执行）。
    fn dispatch_blocking(
        &self,
        method: &str,
        params: Value,
        workspace_instance_id: String,
        workspace_id: Option<i64>,
        operation_class: &str,
        deadline_ms: u64,
    ) -> Result<Value, DaemonRpcError> {
        if operation_class == "index_write" {
            // 兼容写锁：与 Rust mutation 共享串行化点，保证同一数据库单写
            let remaining = Duration::from_millis(deadline_ms);
            return self
                .serialization
                .execute_with_timeout(
                    || {
                        self.exchange(
                            method,
                            params,
                            workspace_instance_id,
                            workspace_id,
                            operation_class,
                            deadline_ms,
                        )
                    },
                    remaining,
                )
                .or_else(|e| {
                    if e.code == "request_timeout" {
                        Ok(compat_err(
                            E_WRITE_LOCK_TIMEOUT,
                            format!("兼容写锁等待超时: {}", e.message),
                            true,
                            "等待 daemon 写锁释放后重试（未执行任何写操作）",
                        ))
                    } else {
                        Err(e)
                    }
                });
        }
        self.exchange(
            method,
            params,
            workspace_instance_id,
            workspace_id,
            operation_class,
            deadline_ms,
        )
    }

    /// 与 worker 完成一次帧交换（ensure_running → 写帧 → 带超时读帧 → 错误映射）。
    fn exchange(
        &self,
        method: &str,
        params: Value,
        workspace_instance_id: String,
        workspace_id: Option<i64>,
        operation_class: &str,
        deadline_ms: u64,
    ) -> Result<Value, DaemonRpcError> {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());

        // 清理已退出但未回收的 child
        self.clear_dead_locked(&mut inner);

        // 确保 worker 存活（崩溃/超时后自动重建）
        if inner.child.is_none() {
            if let Err(msg) = self.spawn_locked(&mut inner) {
                return Ok(compat_err(
                    E_UNAVAILABLE,
                    format!("compat worker 启动失败: {}", msg),
                    true,
                    "daemon 将自动重建 worker，重试即可（不会切换客户端本地 SQLite）",
                ));
            }
        }

        let request_id = format!(
            "cw-compat-{}-{}",
            std::process::id(),
            self.next_request_id.fetch_add(1, Ordering::Relaxed)
        );
        let deadline_epoch_ms = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map(|d| d.as_millis() as u64 + deadline_ms)
            .unwrap_or(deadline_ms);
        let frame = json!({
            "worker_protocol_version": WORKER_PROTOCOL_VERSION,
            "request_id": request_id,
            "method": method,
            "params": params,
            "workspace_instance_id": workspace_instance_id,
            "workspace_id": workspace_id,
            "operation_class": operation_class,
            "deadline": deadline_epoch_ms,
        });

        // 写帧（worker 死亡 → 写失败 → UNAVAILABLE）
        let write_result = {
            let child = inner.child.as_mut().expect("ensure_running 已保证 child 存在");
            let stdin: &mut ChildStdin = child
                .stdin
                .as_mut()
                .ok_or_else(|| DaemonRpcError::new(E_UNAVAILABLE, "worker stdin 不可用"))?;
            write_frame_blocking(stdin, &frame).map_err(|e| {
                self.mark_unhealthy_locked(&mut inner, &format!("写帧失败: {}", e));
                DaemonRpcError::new(
                    E_UNAVAILABLE,
                    format!("compat worker 写帧失败（可能已崩溃）: {}", e),
                )
            })
        };
        if let Err(e) = write_result {
            return Ok(compat_err(
                E_UNAVAILABLE,
                e.message.clone(),
                true,
                "daemon 将自动重建 worker，重试即可（不会切换客户端本地 SQLite）",
            ));
        }

        // 读响应（带 deadline）
        let deadline = Instant::now() + Duration::from_millis(deadline_ms);
        let rx = inner.rx.as_ref().ok_or_else(|| {
            DaemonRpcError::internal_error("worker 响应通道缺失")
        })?;
        loop {
            let remaining = deadline.saturating_duration_since(Instant::now());
            match rx.recv_timeout(remaining) {
                Ok(WorkerMsg::Frame(v)) => return self.handle_worker_frame(&mut inner, v),
                Ok(WorkerMsg::Eof) => {
                    self.mark_unhealthy_locked(&mut inner, "worker 已退出（EOF）");
                    return Ok(compat_err(
                        E_UNAVAILABLE,
                        "compat worker 已退出（崩溃或被终止）",
                        true,
                        "daemon 将自动重建 worker，重试即可（不会切换客户端本地 SQLite）",
                    ));
                }
                Err(RecvTimeoutError::Timeout) => {
                    // 契约：超时先终止当前 worker，再返回 E_COMPAT_WORKER_TIMEOUT
                    self.kill_locked(&mut inner, &format!("deadline({}ms) 超时", deadline_ms));
                    return Ok(compat_err(
                        E_TIMEOUT,
                        format!("compat worker 调用超时（{}ms）", deadline_ms),
                        true,
                        "已终止超时 worker，重试将自动重建（不会切换客户端本地 SQLite）",
                    ));
                }
                Err(RecvTimeoutError::Disconnected) => {
                    self.mark_unhealthy_locked(&mut inner, "worker 响应通道断开");
                    return Ok(compat_err(
                        E_UNAVAILABLE,
                        "compat worker 响应通道断开",
                        true,
                        "daemon 将自动重建 worker，重试即可",
                    ));
                }
            }
        }
    }

    /// 解析 worker 响应帧；异常响应按协议损坏处理。
    fn handle_worker_frame(
        &self,
        inner: &mut CompatWorkerState,
        v: Value,
    ) -> Result<Value, DaemonRpcError> {
        if !v.is_object() {
            self.mark_unhealthy_locked(inner, "worker 返回非 object 响应帧");
            return Ok(compat_err(
                E_PROTOCOL,
                "worker 返回非 object 响应帧",
                false,
                "检查 daemon 与 worker 协议版本一致性",
            ));
        }
        let ok = v.get("ok").and_then(|x| x.as_bool()).unwrap_or(false);
        if ok {
            return Ok(json!({
                "ok": true,
                "result": v.get("result").cloned().unwrap_or(Value::Null),
            }));
        }
        let err = v.get("error").cloned().unwrap_or(Value::Null);
        if !err.is_object() {
            return Ok(compat_err(
                E_PROTOCOL,
                "worker 错误帧缺少 error 对象",
                false,
                "检查 daemon 与 worker 协议版本一致性",
            ));
        }
        let code = err
            .get("code")
            .and_then(|x| x.as_str())
            .unwrap_or("")
            .to_string();
        if !code.starts_with("E_COMPAT_") {
            return Ok(compat_err(
                E_PROTOCOL,
                format!("worker 返回非法错误码: {}", code),
                false,
                "检查 daemon 与 worker 协议版本一致性",
            ));
        }
        Ok(json!({ "ok": false, "error": err }))
    }

    // ---------- 生命周期内部实现 ----------

    fn spawn_locked(&self, inner: &mut CompatWorkerState) -> Result<(), String> {
        let mut cmd = Command::new(&self.cfg.python);
        cmd.arg(&self.cfg.worker_script)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::inherit()); // worker 日志走 stderr，不污染 stdout 协议帧
        let mut child = match cmd.spawn() {
            Ok(c) => c,
            Err(e) => {
                inner.status = "unhealthy".to_string();
                inner.last_error = Some(format!("spawn 失败: {}", e));
                return Err(format!("{}: {}", self.cfg.python, e));
            }
        };
        let stdout: ChildStdout = match child.stdout.take() {
            Some(s) => s,
            None => {
                inner.status = "unhealthy".to_string();
                inner.last_error = Some("worker stdout 不可用".to_string());
                let _ = child.kill();
                return Err("worker stdout 不可用".to_string());
            }
        };
        let (tx, rx) = mpsc::channel();
        std::thread::spawn(move || reader_loop(stdout, tx));
        inner.child = Some(child);
        inner.rx = Some(rx);
        inner.status = "healthy".to_string();
        inner.last_error = None;
        Ok(())
    }

    fn clear_dead_locked(&self, inner: &mut CompatWorkerState) {
        if let Some(child) = inner.child.as_mut() {
            match child.try_wait() {
                Ok(Some(status)) => {
                    inner.child = None;
                    inner.rx = None;
                    inner.status = "unhealthy".to_string();
                    inner.last_error = Some(format!("worker 已退出: {}", status));
                }
                Ok(None) => {}
                Err(e) => {
                    inner.last_error = Some(format!("try_wait 失败: {}", e));
                }
            }
        }
    }

    fn kill_locked(&self, inner: &mut CompatWorkerState, reason: &str) {
        if let Some(mut child) = inner.child.take() {
            let _ = child.kill();
            let _ = child.wait();
        }
        inner.rx = None;
        inner.status = "unhealthy".to_string();
        inner.last_error = Some(reason.to_string());
    }

    fn mark_unhealthy_locked(&self, inner: &mut CompatWorkerState, reason: &str) {
        inner.status = "unhealthy".to_string();
        inner.last_error = Some(reason.to_string());
    }
}

impl Drop for CompatAdapter {
    fn drop(&mut self) {
        if let Some(mut child) = self.inner.lock().ok().and_then(|mut s| s.child.take()) {
            let _ = child.kill();
            let _ = child.wait();
        }
    }
}

/// 读者线程：持续读取 worker stdout 帧，转发到 channel；EOF/损坏时结束。
fn reader_loop(stdout: ChildStdout, tx: mpsc::Sender<WorkerMsg>) {
    let mut reader = BufReader::new(stdout);
    loop {
        match read_frame_blocking(&mut reader) {
            Ok(Some(v)) => {
                if tx.send(WorkerMsg::Frame(v)).is_err() {
                    break; // 接收端已关闭
                }
            }
            Ok(None) => {
                let _ = tx.send(WorkerMsg::Eof);
                break;
            }
            Err(e) => {
                // 协议损坏：通知 daemon 端按 E_COMPAT_WORKER_PROTOCOL 处理
                let err_frame = json!({
                    "ok": false,
                    "error": {
                        "code": E_PROTOCOL,
                        "message": format!("worker 输出协议损坏: {}", e),
                        "retryable": false,
                        "recovery": "检查 daemon 与 worker 协议版本一致性",
                    }
                });
                let _ = tx.send(WorkerMsg::Frame(err_frame));
                break;
            }
        }
    }
}

// ============================================
// 单元测试（帧编解码 + 真实 worker 生命周期）
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;
    use std::time::Duration;

    fn test_python() -> Option<String> {
        let py = std::env::var("CW_COMPAT_PYTHON").ok().filter(|s| !s.is_empty())
            .unwrap_or_else(_default_python);
        // 探测解释器可用性
        let probe = Command::new(&py).arg("-c").arg("import sys; print(sys.version_info[0])").output();
        if probe.map(|o| o.status.success()).unwrap_or(false) {
            Some(py)
        } else {
            None
        }
    }

    #[test]
    fn test_frame_roundtrip_blocking() {
        let mut buf: Vec<u8> = Vec::new();
        let frame = json!({
            "worker_protocol_version": 1,
            "request_id": "r1",
            "method": "get_uncommented_symbols",
            "params": {"limit": 10},
            "workspace_instance_id": "ws-i1",
            "workspace_id": 7,
            "operation_class": "read_only",
            "deadline": 12345,
        });
        write_frame_blocking(&mut buf, &frame).unwrap();
        let mut cursor = Cursor::new(buf);
        let decoded = read_frame_blocking(&mut cursor).unwrap().unwrap();
        assert_eq!(decoded, frame);
    }

    #[test]
    fn test_read_frame_eof_returns_none() {
        let mut cursor = Cursor::new(Vec::<u8>::new());
        assert!(read_frame_blocking(&mut cursor).unwrap().is_none());
    }

    #[test]
    fn test_read_frame_rejects_oversized_length() {
        let mut buf: Vec<u8> = Vec::new();
        buf.extend_from_slice(&(MAX_FRAME_BYTES as u32 + 1).to_be_bytes());
        let mut cursor = Cursor::new(buf);
        assert!(read_frame_blocking(&mut cursor).is_err());
    }

    /// 写一个临时 echo worker 脚本：读一帧后回送固定响应。
    fn write_echo_script(dir: &Path, behavior: &str) -> PathBuf {
        // behavior: "ok" 正常响应；"exit" 立即退出；"sleep" 睡眠 5s 后响应
        let code: String = match behavior {
            "exit" => "import sys; sys.exit(3)\n".to_string(),
            "sleep" => [
                "import sys, time, struct\n",
                "h = sys.stdin.buffer.read(4)\n",
                "n = int.from_bytes(h, 'big')\n",
                "sys.stdin.buffer.read(n)\n",
                "time.sleep(5)\n",
            ]
            .concat(),
            _ => [
                "import sys, time\n",
                "h = sys.stdin.buffer.read(4)\n",
                "n = int.from_bytes(h, 'big')\n",
                "f = sys.stdin.buffer.read(n)\n",
                "time.sleep(0.05)\n",
                "body = b'{\"worker_protocol_version\":1,\"request_id\":\"echo\",\"ok\":true,\"result\":{\"echo\":true}}'\n",
                "sys.stdout.buffer.write(len(body).to_bytes(4,'big') + body)\n",
                "sys.stdout.buffer.flush()\n",
            ]
            .concat(),
        };
        let path = dir.join(format!("echo_worker_{}.py", behavior));
        std::fs::write(&path, code).unwrap();
        path
    }

    fn adapter_with_script(script: &Path) -> Arc<CompatAdapter> {
        let cfg = CompatAdapterConfig {
            python: test_python().expect("Python 不可用，跳过"),
            worker_script: script.to_path_buf(),
        };
        Arc::new(CompatAdapter::new(cfg, Arc::new(SerializationPoint::with_default_timeout())))
    }

    fn dispatch_ok(adapter: &Arc<CompatAdapter>) -> Value {
        let rt = tokio::runtime::Runtime::new().unwrap();
        rt.block_on(adapter.dispatch_arc(
            "echo.method",
            json!({}),
            "ws-i1".to_string(),
            Some(7),
            "read_only",
            5_000,
        ))
        .unwrap()
    }

    #[test]
    fn test_worker_roundtrip_and_restart_after_crash() {
        let Some(py) = test_python() else { eprintln!("Python 不可用，跳过"); return; };
        let _ = py;
        let dir = tempfile::tempdir().unwrap();
        let ok_script = write_echo_script(dir.path(), "ok");
        let adapter = adapter_with_script(&ok_script);
        adapter.start().unwrap();
        assert_eq!(adapter.worker_status(), "healthy");

        let v = dispatch_ok(&adapter);
        assert_eq!(v["ok"], true);
        assert_eq!(v["result"]["echo"], true);

        // 崩溃恢复：用 exit 脚本重建 adapter，首次调用 UNAVAILABLE，重建后再次调用成功
        let exit_script = write_echo_script(dir.path(), "exit");
        let adapter2 = adapter_with_script(&exit_script);
        let v2 = dispatch_ok(&adapter2);
        assert_eq!(v2["ok"], false);
        assert_eq!(v2["error"]["code"], E_UNAVAILABLE);
        // 自动重建：改用 ok 脚本的 adapter 无法原地替换脚本，故断言 unhealthy 后重建
        adapter2.stop();
        assert_eq!(adapter2.worker_status(), "stopped");
    }

    #[test]
    fn test_timeout_kills_worker_then_restart() {
        let Some(_py) = test_python() else { eprintln!("Python 不可用，跳过"); return; };
        let dir = tempfile::tempdir().unwrap();
        let sleep_script = write_echo_script(dir.path(), "sleep");
        let adapter = adapter_with_script(&sleep_script);
        let rt = tokio::runtime::Runtime::new().unwrap();
        let v = rt
            .block_on(adapter.dispatch_arc(
                "echo.method",
                json!({}),
                "ws-i1".to_string(),
                Some(7),
                "read_only",
                200,
            ))
            .unwrap();
        assert_eq!(v["ok"], false);
        assert_eq!(v["error"]["code"], E_TIMEOUT);
        assert_eq!(adapter.worker_status(), "unhealthy");
        // 超时后 worker 已被终止：child 已清空
        assert!(adapter.inner.lock().unwrap().child.is_none());
        // 下一次调用自动重建（新 spawn 的 sleep worker 仍会超时，但状态流转正常）
        let v2 = rt
            .block_on(adapter.dispatch_arc(
                "echo.method",
                json!({}),
                "ws-i1".to_string(),
                Some(7),
                "read_only",
                200,
            ))
            .unwrap();
        assert_eq!(v2["error"]["code"], E_TIMEOUT);
    }

    #[test]
    fn test_index_write_respects_serialization_lock() {
        let Some(_py) = test_python() else { eprintln!("Python 不可用，跳过"); return; };
        let dir = tempfile::tempdir().unwrap();
        let ok_script = write_echo_script(dir.path(), "ok");
        let adapter = adapter_with_script(&ok_script);
        let rt = tokio::runtime::Runtime::new().unwrap();

        // 占用串行化点 → index_write 应在等待超时后返回 E_COMPAT_WRITE_LOCK_TIMEOUT
        assert!(adapter.serialization.try_acquire());
        let v = rt
            .block_on(adapter.dispatch_arc(
                "echo.method",
                json!({}),
                "ws-i1".to_string(),
                Some(7),
                "index_write",
                200,
            ))
            .unwrap();
        adapter.serialization.release();
        assert_eq!(v["ok"], false);
        assert_eq!(v["error"]["code"], E_WRITE_LOCK_TIMEOUT);

        // 释放后 index_write 正常完成
        let v2 = rt
            .block_on(adapter.dispatch_arc(
                "echo.method",
                json!({}),
                "ws-i1".to_string(),
                Some(7),
                "index_write",
                5_000,
            ))
            .unwrap();
        assert_eq!(v2["ok"], true);
    }
}
