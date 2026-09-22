//! cw-daemon single-instance 守卫（P0：防止重复启动导致 lease/authority 分裂）
//!
//! ## 问题（2026-09-23 审计结论）
//! daemon 启动序列（`cw_daemon.rs` unix `serve`/windows `serve`）**没有**任何
//! 跨进程互斥：无 PID 文件、无 named mutex、无端口占用检查。导致：
//! 1. UDS：`server.rs prepare_socket_path` 直接 `remove_file` 旧 socket 再 bind，
//!    第二个 daemon 抢走 socket 路径，第一个沦为孤儿（仍持已 unlink 的 fd，
//!    新客户端全连到 #2）。
//! 2. HTTP：动态端口 `127.0.0.1:0` 永不冲突，manifest 原子发布到**同一**
//!    authority-scoped 固定路径，#2 覆盖 #1 的 manifest → last-one-wins。
//! 3. lease 的并发锁是进程内 `std::Mutex`，两个 daemon 打开同一个权威任务库，
//!    各自独立 `daemon_generation`，都认为自己是权威 → 租约/permit 分裂。
//!
//! ## 方案
//! authority-scoped 跨进程文件锁（fs2 `lock_exclusive`，与 `cas.rs` GC 锁同范式）：
//! - 锁文件 = manifest 同目录的 `daemon-instance.<authority>.lock`
//! - daemon 启动早期（HTTP 预绑定之前、schema 打开之前）获取独占锁
//! - 拿到锁 → 写入自己的 PID + 启动时间 → 返回 guard（drop 释放）
//! - 拿不到 → 读锁文件 PID 判断存活：存活则 `E_DAEMON_ALREADY_RUNNING`
//!   fail-closed 拒绝启动；已死则清理重试（崩溃残留）
//!
//! fs2 flock 在进程退出时自动释放（Windows LockFileEx / Unix flock 同语义），
//! 崩溃不残留锁；残留的只是锁文件内的 PID 文本，下次启动覆盖写。
//!
//! ## Windows 关键实测结论（2026-09-22，回归测试 `locked_file_read_platform_contract`）
//! Windows `LockFileEx` 排他锁**不只是阻止写入**——它同时阻止他进程**读取**
//! 锁定范围，读取得到 `os error 33`（ERROR_LOCK_VIOLATION，「另一个进程已锁定
//! 文件的一部分」）。因此：
//! - 在 daemon B 持锁期间，daemon A 的 `read_lock_pid` 必然失败 → PID 不可读；
//! - **不能**把"PID 不可读"当作"锁是异常残留"（旧逻辑会误导用户手动删锁文件，
//!   而删除持活锁的文件正是 authority 分裂的入口）；
//! - 正确语义：`try_lock_exclusive` 失败 = 有活进程持锁（flock 随进程退出自动
//!   释放，这是权威信号），PID 记录仅作 best-effort 展示；读不到时报
//!   `AlreadyRunning { pid: 0 }`（Display 标注 PID 未知）。
//!
//! 与 Python 侧 `daemon_mutex.py DaemonMutex` 的关系：Python 互斥体只管
//! 自动唤起去重（`daemon_autostart.py`），**手动 `cw-daemon serve` 不受约束**；
//! 本模块在 daemon 进程内拦截所有启动路径，是 Python 侧的超集。

use std::fs::{File, OpenOptions};
use std::io::{self, Read, Write};
use std::path::{Path, PathBuf};
use std::time::{SystemTime, UNIX_EPOCH};

use fs2::FileExt;

use super::http_server::{http_authority_id, http_manifest_dir};

/// single-instance 锁错误。
#[derive(Debug)]
pub enum SingleInstanceError {
    /// 另一个 daemon 正在运行（PID 存活校验通过）
    ///
    /// `pid == 0` 表示 PID 不可读（未知）：Windows 下排他锁会阻止他进程读取
    /// 锁文件内容（`os error 33` ERROR_LOCK_VIOLATION，见下方模块文档），
    /// 此时 `try_lock` 失败已足以证明有活 daemon，PID 仅为展示用。
    AlreadyRunning {
        pid: u32,
        endpoint: Option<String>,
        lock_path: PathBuf,
    },
    /// 锁文件 IO 错误（父目录不可建等）
    Io(io::Error),
}

impl std::fmt::Display for SingleInstanceError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            SingleInstanceError::AlreadyRunning { pid, endpoint, lock_path } => {
                // pid==0（未知）：Windows 排他锁挡住 PID 记录读取时的降级展示
                let pid_part = if *pid == 0 {
                    String::from("PID 未知：锁文件被排他锁占用无法读取 PID 记录")
                } else {
                    format!("pid={}", pid)
                };
                write!(
                    f,
                    "E_DAEMON_ALREADY_RUNNING: 另一个 cw-daemon ({}) 正在运行{}，\
                     拒绝重复启动以免 lease/authority 分裂（锁: {}）",
                    pid_part,
                    endpoint
                        .as_ref()
                        .map(|e| format!(" @ {}", e))
                        .unwrap_or_default(),
                    lock_path.display()
                )
            }
            SingleInstanceError::Io(e) => write!(f, "single-instance 锁 IO 错误: {}", e),
        }
    }
}

impl std::error::Error for SingleInstanceError {}

impl From<io::Error> for SingleInstanceError {
    fn from(e: io::Error) -> Self {
        SingleInstanceError::Io(e)
    }
}

/// 持有 single-instance 锁的 guard。drop 时释放（fs2 自动 unlock + 删除 PID 记录）。
///
/// **不可 Clone**：同一时刻只能有一个 daemon 持有。
pub struct InstanceLock {
    _file: File,
    lock_path: PathBuf,
}

impl Drop for InstanceLock {
    fn drop(&mut self) {
        // 顺序敏感（Windows）：必须先释放排他锁，再截断 PID 记录——
        // 否则 File::create 的截断写入会被自己持有的 LockFileEx 拒绝（os error 33）。
        // fs2 在 File drop 时也会自动释放 flock，显式 unlock 只是更清晰。
        let _ = FileExt::unlock(&self._file);
        // 清理 PID 文件内容，避免下次启动误判残留 daemon（锁本身已释放，
        // 但文件文本残留会让 `read_lock_pid` 返回旧 PID）。
        let _ = File::create(&self.lock_path);
    }
}

impl std::fmt::Debug for InstanceLock {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.debug_struct("InstanceLock")
            .field("lock_path", &self.lock_path)
            .finish()
    }
}

/// 锁文件内记录的 daemon 元信息（JSON，便于人肉排查）
#[derive(Debug, Clone, serde::Serialize, serde::Deserialize)]
struct LockRecord {
    pid: u32,
    started_at: u64,
    manifest_id: Option<String>,
    endpoint: Option<String>,
}

/// 计算锁文件路径：`<manifest_dir>/daemon-instance.<authority>.lock`
///
/// 与 manifest 同目录（`~/.callwarden`），同 authority 隔离，保证不同本地用户
/// 各自可以有独立 daemon（与 manifest authority scope 语义一致）。
pub fn instance_lock_path(authority_id: &str) -> PathBuf {
    let safe = authority_id
        .replace('/', "_")
        .replace('\\', "_")
        .replace(':', "_");
    http_manifest_dir().join(format!("daemon-instance.{}.lock", safe))
}

/// 读锁文件里的 PID（解析失败/文件缺失返回 None）
fn read_lock_pid(lock_path: &Path) -> Option<u32> {
    let mut text = String::new();
    File::open(lock_path).ok()?.read_to_string(&mut text).ok()?;
    if text.trim().is_empty() {
        return None;
    }
    // 优先 JSON 记录；兼容裸 PID 文本
    if let Ok(rec) = serde_json::from_str::<LockRecord>(&text) {
        return Some(rec.pid);
    }
    text.trim().parse::<u32>().ok()
}

/// PID 存活探测（跨平台：Unix kill -0 / Windows OpenProcess）
pub(crate) fn pid_alive(pid: u32) -> bool {
    if pid == 0 {
        return false;
    }
    #[cfg(unix)]
    {
        // SAFETY: kill(pid, 0) 只做存在性检查，不发送信号
        let r = unsafe { libc::kill(pid as i32, 0) };
        if r == 0 {
            return true;
        }
        // ESRCH = 进程不存在；EPERM = 存在但属其他用户
        std::io::Error::last_os_error().raw_os_error() == Some(libc::EPERM)
    }
    #[cfg(windows)]
    {
        use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
        use windows_sys::Win32::System::Threading::{
            OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
        };
        // SAFETY: OpenProcess 句柄由本进程持有，CloseProcess 释放
        unsafe {
            let h: HANDLE = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
            if h.is_null() {
                // 句柄为空通常表示进程不存在（或权限不足视为存活，fail-closed）
                return std::io::Error::last_os_error().raw_os_error() == Some(5); // ERROR_ACCESS_DENIED
            }
            let _ = CloseHandle(h);
            true
        }
    }
}

/// 获取 single-instance 锁。
///
/// 调用时机：daemon 启动早期，HTTP 预绑定**之前**、schema/任务库打开**之前**——
/// 必须在任何可能被客户端发现的端点（manifest / socket / listener）建立之前，
/// 否则抢座半途中客户端已经连上来。
///
/// `manifest_id` / `endpoint` 用于锁文件记录，供人肉排查（可传入 None，
/// HTTP 预绑定后回写）。
pub fn acquire_instance_lock(
    authority_id: &str,
    manifest_id: Option<&str>,
    endpoint: Option<&str>,
) -> Result<InstanceLock, SingleInstanceError> {
    let lock_path = instance_lock_path(authority_id);
    if let Some(parent) = lock_path.parent() {
        if !parent.as_os_str().is_empty() {
            std::fs::create_dir_all(parent)?;
        }
    }

    let mut file = OpenOptions::new()
        .create(true)
        .read(true)
        .write(true)
        .truncate(false)
        .open(&lock_path)?;

    // 非阻塞尝试：拿到则继续，拿不到说明有活 daemon（flock 持有中）
    match FileExt::try_lock_exclusive(&file) {
        Ok(()) => {
            // 拿到锁：写入自己的记录（覆盖任何残留文本）
            let rec = LockRecord {
                pid: std::process::id(),
                started_at: SystemTime::now()
                    .duration_since(UNIX_EPOCH)
                    .map(|d| d.as_secs())
                    .unwrap_or(0),
                manifest_id: manifest_id.map(|s| s.to_string()),
                endpoint: endpoint.map(|s| s.to_string()),
            };
            let json = serde_json::to_vec(&rec).unwrap_or_else(|_| Vec::new());
            // 先 truncate 再写（用 seek+set_len 清空旧内容）
            use std::io::Seek;
            let _ = file.seek(std::io::SeekFrom::Start(0));
            let _ = file.set_len(0);
            let _ = file.write_all(&json);
            let _ = file.sync_all();
            Ok(InstanceLock { _file: file, lock_path })
        }
        Err(_) => {
            // 拿不到锁：**try_lock 失败本身就是"有活进程持锁"的权威信号**
            // （Windows LockFileEx / Unix flock 都在持有进程退出/崩溃时自动释放）。
            //
            // PID 记录读取降级为 best-effort：Windows 排他锁会阻止他进程读取
            // 锁范围（os error 33，见模块文档），读不到 PID 绝不代表没有 daemon。
            drop(file);
            match read_lock_pid(&lock_path) {
                Some(pid) if pid_alive(pid) => Err(SingleInstanceError::AlreadyRunning {
                    pid,
                    endpoint: endpoint.map(|s| s.to_string()),
                    lock_path: lock_path.clone(),
                }),
                Some(pid) => {
                    // 锁仍被持有但记录的 PID 已死：flock 语义下理论不该发生
                    // （NFS/外置盘等边界），fail-closed 报错请用户介入，不自动强抢。
                    Err(SingleInstanceError::Io(io::Error::new(
                        io::ErrorKind::WouldBlock,
                        format!(
                            "single-instance 锁被占用但 PID {} 不存活（异常残留，\
                             请手动删除 {} 后重试）",
                            pid,
                            lock_path.display()
                        ),
                    )))
                }
                None => {
                    // PID 不可读：几乎必然是 Windows 排他锁挡住了读取（os error 33），
                    // 即锁确被一个活 daemon 持有。报 AlreadyRunning（pid=0=未知），
                    // **绝不**当成残留让用户删锁文件——那会放第二个 daemon 进来。
                    Err(SingleInstanceError::AlreadyRunning {
                        pid: 0,
                        endpoint: endpoint.map(|s| s.to_string()),
                        lock_path: lock_path.clone(),
                    })
                }
            }
        }
    }
}

/// 便捷入口：用当前 authority 获取锁（最常用，daemon 启动直接调）
pub fn acquire_default_instance_lock() -> Result<InstanceLock, SingleInstanceError> {
    acquire_instance_lock(&http_authority_id(), None, None)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn lock_path_is_authority_scoped() {
        let p = instance_lock_path("S-1-5-21-x");
        assert!(p.to_string_lossy().contains("daemon-instance.S-1-5-21-x.lock"));
        // 必须在 manifest 同目录（~/.callwarden）
        assert!(p.starts_with(http_manifest_dir()));
    }

    #[test]
    fn read_lock_pid_parses_json_and_plain() {
        // 裸 PID 文本兼容
        assert_eq!(read_lock_pid_json("12345"), Some(12345));
        // JSON 记录
        assert_eq!(
            read_lock_pid_json(r#"{"pid":999,"started_at":0,"manifest_id":null,"endpoint":null}"#),
            Some(999)
        );
        // 空文本
        assert_eq!(read_lock_pid_json(""), None);
        assert_eq!(read_lock_pid_json("   "), None);
    }

    /// 纯文本解析辅助（不碰文件系统）
    fn read_lock_pid_json(text: &str) -> Option<u32> {
        if text.trim().is_empty() {
            return None;
        }
        if let Ok(rec) = serde_json::from_str::<LockRecord>(text) {
            return Some(rec.pid);
        }
        text.trim().parse::<u32>().ok()
    }

    #[test]
    fn pid_alive_detects_self() {
        // 自己肯定存活
        assert!(pid_alive(std::process::id()));
        // PID 0 不存活
        assert!(!pid_alive(0));
    }

    #[test]
    fn double_acquire_rejects_second() {
        // 临时目录做真实 flock 互斥验证
        let dir = std::env::temp_dir().join("cw_single_instance_test");
        std::fs::create_dir_all(&dir).unwrap();
        let lock_path = dir.join("daemon-instance.test.lock");

        // 手工构造同路径锁：先拿一个
        let f1 = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&lock_path)
            .unwrap();
        FileExt::lock_exclusive(&f1).unwrap();

        // 第二个必须失败
        let f2 = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&lock_path)
            .unwrap();
        let r2 = FileExt::try_lock_exclusive(&f2);
        assert!(r2.is_err(), "第二个 exclusive lock 必须失败");

        // 释放第一个后第二个可以拿到
        FileExt::unlock(&f1).unwrap();
        assert!(FileExt::try_lock_exclusive(&f2).is_ok());

        // 清理临时锁文件
        drop(f1);
        drop(f2);
        let _ = std::fs::remove_file(&lock_path);
    }

    /// 平台契约回归：排他锁持锁期间，他进程/句柄读取锁范围的行为。
    ///
    /// 这是 `acquire_instance_lock` 失败分支设计所依赖的关键平台差异：
    /// - Windows：`LockFileEx` 排他锁**同时阻止读取** → `os error 33`
    ///   （ERROR_LOCK_VIOLATION）。所以拿不到锁时 `read_lock_pid` 读不到 PID，
    ///   不能据此判"异常残留"，只能判 AlreadyRunning（pid=0=未知）。
    /// - Unix：flock 是建议锁，**不阻止读写** → PID 可读取。
    #[test]
    fn locked_file_read_platform_contract() {
        let dir = std::env::temp_dir().join("cw_locked_read_contract");
        std::fs::create_dir_all(&dir).unwrap();
        let p = dir.join("contract.lock");
        let _ = std::fs::remove_file(&p);

        let f1 = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&p)
            .unwrap();
        FileExt::lock_exclusive(&f1).unwrap();

        let rec = LockRecord {
            pid: 12345,
            started_at: 0,
            manifest_id: None,
            endpoint: None,
        };
        use std::io::Write;
        (&f1).set_len(0).ok();
        assert!(
            (&f1).write_all(&serde_json::to_vec(&rec).unwrap()).is_ok(),
            "持锁者写入自己的锁范围必须成功"
        );

        // 持锁期间的读取（read_lock_pid 的精确路径）
        let mut t = String::new();
        let read_res = File::open(&p).and_then(|mut h| h.read_to_string(&mut t));

        #[cfg(windows)]
        {
            assert!(
                read_res.is_err(),
                "Windows 排他锁必须阻止他句柄读取（否则本测试前提失效）"
            );
            let code = read_res.err().and_then(|e| e.raw_os_error());
            assert_eq!(code, Some(33), "期望 ERROR_LOCK_VIOLATION(33)，实际: {:?}", code);
            assert!(t.is_empty());
        }

        #[cfg(unix)]
        {
            assert!(read_res.is_ok(), "Unix flock 是建议锁，读取必须成功");
            assert_eq!(read_lock_pid(&p), Some(12345));
        }

        // 解锁后双方都能读
        FileExt::unlock(&f1).unwrap();
        assert_eq!(read_lock_pid(&p), Some(12345));

        drop(f1);
        let _ = std::fs::remove_file(&p);
    }

    #[test]
    fn acquire_and_detect_running() {
        // 端到端：先占锁，再 acquire 必须报 AlreadyRunning
        // 直接用真实 authority 路径（~/.callwarden），测完清理
        let authority = "cw-single-instance-e2e-test";
        let lock_path = instance_lock_path(authority);
        // 起点幂等清理：上次失败可能残留（panic 时尾部清理没执行）
        let _ = std::fs::remove_file(&lock_path);

        // 先以本进程身份占住锁并写 PID
        let f1 = OpenOptions::new()
            .create(true)
            .read(true)
            .write(true)
            .open(&lock_path)
            .unwrap();
        FileExt::lock_exclusive(&f1).unwrap();
        let rec = LockRecord {
            pid: std::process::id(),
            started_at: 0,
            manifest_id: None,
            endpoint: None,
        };
        use std::io::Write;
        (&f1).set_len(0).ok();
        (&f1).write_all(&serde_json::to_vec(&rec).unwrap()).ok();

        // acquire_instance_lock 必须走 try_lock 失败 → 拒绝（AlreadyRunning）
        let r = acquire_instance_lock(authority, None, None);
        match &r {
            Err(SingleInstanceError::AlreadyRunning { pid, .. }) => {
                // Unix flock 是建议锁（advisory），不阻止读取 → PID 应可解析为本进程
                #[cfg(unix)]
                assert_eq!(*pid, std::process::id(), "Unix 下 PID 应可读取为本进程");

                // Windows LockFileEx 排他锁阻止他进程读取锁范围（os error 33，
                // 实测见 locked_file_read_platform_contract）→ PID 不可读（0=未知），
                // 但**必须**报 AlreadyRunning，不能误判为"异常残留"诱导用户删锁文件
                #[cfg(windows)]
                assert!(
                    *pid == 0 || *pid == std::process::id(),
                    "Windows 下应报 AlreadyRunning（PID 可读为本进程或 0=未知），实际 pid={}",
                    pid
                );
            }
            other => panic!(
                "占锁中再 acquire 必须报 AlreadyRunning，实际: {:?}",
                other.as_ref().map(|_| "").map_err(|e| e.to_string()).err()
            ),
        }

        drop(f1);
        let _ = std::fs::remove_file(&lock_path);
    }
}
