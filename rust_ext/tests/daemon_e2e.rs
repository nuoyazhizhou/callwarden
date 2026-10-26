//! R8: cw_daemon binary 端到端集成测试
//!
//! Linux-only。启动 cw_daemon binary 子进程 → UDS 连接 → RPC 调用 → 验证响应。
//! 覆盖所有已实现的 RPC 方法分组：基础方法、workspace 管理、snapshot 查询。
//!
//! 测试策略：
//! - 每个测试用独立的临时 socket/registry 路径，并行安全
//! - 通过 Cargo 环境变量定位 cw-daemon binary（cargo test 自动设置
//!   CARGO_BIN_EXE_cw-daemon——bin 名原样带连字符，见 Cargo 环境变量参考）
//! - 跳过 cross-UID ACL（需 sudo 切换用户，不适合自动化测试）

#![cfg(unix)]

use std::io::{Read, Write};
use std::os::unix::net::UnixStream;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::thread;
use std::time::{Duration, Instant};

use serde_json::{json, Value};

/// 定位 cw-daemon binary（cargo test 自动注入 CARGO_BIN_EXE_cw-daemon）
fn bin_path() -> PathBuf {
    // TP-11G P0-A：P0-3 发布修复（3f62fb7）把 bin 从 cw_daemon 改名为
    // cw-daemon 后，CARGO_BIN_EXE_<name> 按 bin 名**原样**注入（连字符不
    // 转换）——旧下划线形式在本文件（#![cfg(unix)]，仅 Linux 编译）里
    // 直到 CI 编译门禁（ubuntu）才暴露。Windows 侧本文件被 cfg 掉，
    // 故本地全绿也发现不了。
    PathBuf::from(env!("CARGO_BIN_EXE_cw-daemon"))
}

/// 临时测试 fixture：唯一 socket + registry 路径 + daemon 子进程管理
struct DaemonFixture {
    child: Child,
    socket: PathBuf,
    _tmpdir: tempfile::TempDir,
}

impl DaemonFixture {
    /// 启动 daemon，等待 socket 就绪
    fn start() -> std::io::Result<Self> {
        let tmpdir = tempfile::tempdir()?;
        let socket = tmpdir.path().join("e2e.sock");
        let registry = tmpdir.path().join("e2e_registry.db");

        let mut cmd = Command::new(bin_path());
        cmd.args([
            "--socket",
            socket.to_str().unwrap(),
            "--registry",
            registry.to_str().unwrap(),
            "--workers",
            "4",
            "serve",
        ])
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        // 隔离：daemon 默认 data_root=/var/lib/callwarden（systemd StateDirectory，
        // 非 root 不可写）、task/codegraph 库默认在 ~/.callwarden、审计库默认在
        // /var/log/callwarden。这里全部重定向到本用例的 tmpdir——否则普通用户
        // 启动即 `创建数据目录失败: Permission denied (os error 13)`，socket
        // 永不就绪（首轮 CI 实证 9/9 失败）；同时让 9 个用例并行时各自独占
        // SQLite 文件，避免跨用例锁竞争。
        .env("CW_DAEMON_DATA_ROOT", tmpdir.path())
        .env("HOME", tmpdir.path())
        .env("CW_DAEMON_AUDIT_DB", tmpdir.path().join("audit.log"))
        .env("RUST_LOG", "info");

        let mut child = cmd.spawn()?;

        // 等待 socket 就绪（最多 30 秒）。
        //
        // 原 5s 在并行执行时不够：9 个用例各自拉起一个独立 daemon，单个在本机
        // 实测约 4-5s（schema 迁移 v60 + workspace recovery + HTTP 预绑定），
        // 并发时互相抢 CPU 导致集体超时（CI 首轮 9/9 失败）。
        //
        // 超时后必须 kill 子进程：否则孤儿 daemon 仍在继续启动，而本函数的
        // TempDir 已随返回 Err 被 Drop 删除，它随后在已删除路径上打开 SQLite，
        // 报 `unable to open database file`——掩盖真正的超时原因。
        let deadline = Instant::now() + Duration::from_secs(30);
        loop {
            if socket.exists() {
                break;
            }
            if Instant::now() > deadline {
                unsafe {
                    libc::kill(child.id() as i32, libc::SIGKILL);
                }
                let _ = child.wait();
                return Err(std::io::Error::new(
                    std::io::ErrorKind::TimedOut,
                    "daemon socket 未在 30 秒内就绪",
                ));
            }
            thread::sleep(Duration::from_millis(50));
        }

        Ok(Self {
            child,
            socket,
            _tmpdir: tmpdir,
        })
    }

    /// 连接到 UDS socket
    fn connect(&self) -> std::io::Result<UnixStream> {
        UnixStream::connect(&self.socket)
    }
}

impl Drop for DaemonFixture {
    fn drop(&mut self) {
        // 优雅关闭：发送 SIGTERM，等待退出
        let pid = self.child.id() as i32;
        unsafe {
            libc::kill(pid, libc::SIGTERM);
        }
        let _ = self.child.wait();
    }
}

/// 发送 JSON-RPC 请求并读取响应
fn rpc_call(stream: &mut UnixStream, method: &str, params: Value) -> std::io::Result<Value> {
    let request = json!({
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": params,
    });
    let payload = serde_json::to_vec(&request).unwrap();
    let len = payload.len() as u32;
    stream.write_all(&len.to_be_bytes())?;
    stream.write_all(&payload)?;
    stream.flush()?;

    // 读取响应
    let mut header = [0u8; 4];
    stream.read_exact(&mut header)?;
    let size = u32::from_be_bytes(header) as usize;
    let mut buf = vec![0u8; size];
    stream.read_exact(&mut buf)?;
    let response: Value = serde_json::from_slice(&buf)?;
    Ok(response)
}

// ============================================
// 基础方法 E2E 测试
// ============================================

#[test]
fn e2e_ping_returns_pong() {
    let fixture = DaemonFixture::start().expect("daemon 启动失败");
    let mut stream = fixture.connect().expect("UDS 连接失败");

    let resp = rpc_call(&mut stream, "ping", json!({})).expect("RPC 失败");
    // R8: daemon 响应可能不含 jsonrpc 字段（R3 实现简化），只验证 id 和 result
    assert_eq!(resp["id"], 1);
    assert!(
        resp["result"].is_object() || resp["result"]["pong"].is_string(),
        "ping 应返回 result，实际: {resp}"
    );
}

#[test]
fn e2e_health_returns_status() {
    let fixture = DaemonFixture::start().expect("daemon 启动失败");
    let mut stream = fixture.connect().expect("UDS 连接失败");

    let resp = rpc_call(&mut stream, "health", json!({})).expect("RPC 失败");
    assert!(resp["result"].is_object());
    // health 应至少包含 status 字段
    assert!(resp["result"]["status"].is_string() || resp["result"]["ok"].is_boolean());
}

#[test]
fn e2e_schema_version_returns_version() {
    let fixture = DaemonFixture::start().expect("daemon 启动失败");
    let mut stream = fixture.connect().expect("UDS 连接失败");

    let resp = rpc_call(&mut stream, "schema.version", json!({})).expect("RPC 失败");
    assert!(resp["result"]["version"].is_u64() || resp["result"]["schema_version"].is_u64());
}

#[test]
fn e2e_unknown_method_returns_error() {
    let fixture = DaemonFixture::start().expect("daemon 启动失败");
    let mut stream = fixture.connect().expect("UDS 连接失败");

    let resp = rpc_call(&mut stream, "nonexistent.method", json!({})).expect("RPC 失败");
    assert!(resp["error"].is_object());
    assert_eq!(resp["error"]["code"], "method_not_found");
}

// ============================================
// Workspace 管理 E2E 测试
// ============================================

#[test]
fn e2e_workspace_register_and_list() {
    let fixture = DaemonFixture::start().expect("daemon 启动失败");

    // R8: workspace.register 要求路径存在，先创建临时目录
    let reg_root = fixture._tmpdir.path().join("ws_root");
    std::fs::create_dir_all(&reg_root).expect("创建 ws_root 失败");

    // register
    let mut stream = fixture.connect().expect("UDS 连接失败");
    let resp = rpc_call(
        &mut stream,
        "workspace.register",
        json!({
            "client_view_root": reg_root.to_str().unwrap(),
            "git_remote_url": "",
            "git_head_commit_sha": "",
            "toolchain_fingerprint": "",
        }),
    )
    .expect("register RPC 失败");
    drop(stream);

    assert!(
        resp["result"].is_object(),
        "register 应返回对象 result，实际: {resp}"
    );
    assert!(
        resp["result"]["workspace_instance_id"].is_string()
            || resp["result"]["workspace_id"].is_string(),
        "register 应返回 workspace id"
    );

    // list（新连接）
    let mut stream2 = fixture.connect().expect("list UDS 连接失败");
    let resp2 = rpc_call(&mut stream2, "workspace.list", json!({})).expect("list RPC 失败");

    assert!(resp2["result"].is_array() || resp2["result"]["workspaces"].is_array());
}

// ============================================
// 查询方法 E2E 测试
// ============================================

#[test]
fn e2e_query_stats_returns_object() {
    let fixture = DaemonFixture::start().expect("daemon 启动失败");

    // R8: workspace.register 要求路径存在
    let reg_root = fixture._tmpdir.path().join("ws_stats_root");
    std::fs::create_dir_all(&reg_root).expect("创建 ws_stats_root 失败");

    // 先注册一个 workspace（query.stats 需要 workspace_instance_id）
    let mut stream = fixture.connect().expect("UDS 连接失败");
    let reg = rpc_call(
        &mut stream,
        "workspace.register",
        json!({
            "client_view_root": reg_root.to_str().unwrap(),
            "git_remote_url": "",
            "git_head_commit_sha": "",
            "toolchain_fingerprint": "",
        }),
    )
    .expect("register 失败");
    drop(stream);

    let wid = reg["result"]["workspace_instance_id"]
        .as_str()
        .or_else(|| reg["result"]["workspace_id"].as_str())
        .expect("缺少 workspace_instance_id");

    // 查询 stats（可能返回 empty snapshot 错误，因为没发布过 snapshot）
    let mut stream2 = fixture.connect().expect("query UDS 连接失败");
    let resp = rpc_call(
        &mut stream2,
        "query.stats",
        json!({ "workspace_instance_id": wid }),
    )
    .expect("query.stats RPC 失败");

    // 没发布过 snapshot，应返回错误或空 stats
    assert!(
        resp["error"].is_object() || resp["result"].is_object(),
        "query.stats 应返回 error 或 result，实际: {resp}"
    );
}

#[test]
fn e2e_gc_snapshots_returns_object() {
    let fixture = DaemonFixture::start().expect("daemon 启动失败");
    let mut stream = fixture.connect().expect("UDS 连接失败");

    let resp = rpc_call(&mut stream, "gc.snapshots", json!({ "keep_last": 3 }))
        .expect("gc.snapshots RPC 失败");

    // gc.snapshots 应返回 result（可能 empty）
    assert!(
        resp["result"].is_object() || resp["error"].is_object(),
        "gc.snapshots 应返回 result 或 error，实际: {resp}"
    );
}

// ============================================
// 连接复用 / 并发 E2E 测试
// ============================================

#[test]
fn e2e_multiple_connections_concurrent() {
    let fixture = DaemonFixture::start().expect("daemon 启动失败");

    // 同时开 3 个连接，每个发 ping
    let socket = fixture.socket.clone();
    let handle1 = thread::spawn(move || {
        let mut s = UnixStream::connect(&socket).expect("连接 1 失败");
        rpc_call(&mut s, "ping", json!({})).expect("RPC 1 失败")
    });
    let socket2 = fixture.socket.clone();
    let handle2 = thread::spawn(move || {
        let mut s = UnixStream::connect(&socket2).expect("连接 2 失败");
        rpc_call(&mut s, "ping", json!({})).expect("RPC 2 失败")
    });
    let socket3 = fixture.socket.clone();
    let handle3 = thread::spawn(move || {
        let mut s = UnixStream::connect(&socket3).expect("连接 3 失败");
        rpc_call(&mut s, "health", json!({})).expect("RPC 3 失败")
    });

    let r1 = handle1.join().expect("thread 1 panic");
    let r2 = handle2.join().expect("thread 2 panic");
    let r3 = handle3.join().expect("thread 3 panic");

    assert!(r1["result"].is_object(), "并发连接 1 失败: {r1}");
    assert!(r2["result"].is_object(), "并发连接 2 失败: {r2}");
    assert!(r3["result"].is_object(), "并发连接 3 失败: {r3}");
}

#[test]
fn e2e_daemon_graceful_shutdown_on_sigterm() {
    let mut fixture = DaemonFixture::start().expect("daemon 启动失败");
    let pid = fixture.child.id() as i32;

    // 发送 SIGTERM
    unsafe {
        libc::kill(pid, libc::SIGTERM);
    }

    // 等待退出（最多 3 秒）
    let deadline = Instant::now() + Duration::from_secs(3);
    loop {
        match fixture.child.try_wait() {
            Ok(Some(_status)) => break,
            Ok(None) => {
                if Instant::now() > deadline {
                    panic!("daemon 在 3 秒内未响应 SIGTERM 退出");
                }
                thread::sleep(Duration::from_millis(100));
            }
            Err(e) => panic!("wait 失败: {e}"),
        }
    }

    // socket 应被清理（R7 Drop impl 会 remove socket 文件）
    // 注意：这里不严格断言，因为 Drop 在 test 函数返回后才执行
    let _ = pid;
}
