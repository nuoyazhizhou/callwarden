//! G10/G20: memfd / FD 读取校验——接收 SCM_RIGHTS 传递的 FD 时执行
//! 安全读取，避免 `read_to_end` 无界读导致 OOM / 内存耗尽攻击。
//!
//! ## 六重校验（P1-3 复审整改 2026-07-21）
//!
//! 1. **FD 类型校验**：`fstat` 检查 `st_mode & S_IFMT == S_IFREG`，
//!    拒绝读取目录 / 字符设备 / 块设备 / 套接字 / FIFO 等。
//!    例外：`memfd_create` 创建的 FD 在 `/proc/self/fd/<N>` fstat 显示为常规文件，
//!    `fstat` 通过即可放行。
//! 2. **owner UID 校验**（P1-3 新增）：`fstat.st_uid` 必须等于 `peer_uid`，
//!    防止跨 UID 攻击（peer 传递不属于自己 UID 的 FD，绕过文件系统 ACL）。
//!    `peer_uid == 0`（root）跳过此校验（root 受 SO_PEERCRED 信任）。
//! 3. **seals 校验**（P1-3 新增，仅 Linux memfd）：`F_GET_SEALS` 必须包含
//!    `F_SEAL_SEAL | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE`。
//!    非 memfd FD（F_GET_SEALS 返回 EINVAL/ENOTTY）跳过此项，视为普通文件。
//!    规范：daemon-ipc-security.md §3.2 + server/ipc_transport.py:validate_memfd_fd。
//! 4. **大小预检**：从 `fstat.st_size` 获取文件大小，0 视为合法（管道/临时文件），
//!    按 `st_size` 预分配 buf 容量（避免 `read_to_end` 的指数扩容）。
//! 5. **容量上限**：超过 `MAX_FD_READ_BYTES`（默认 64MB）直接拒绝。
//!    防止恶意客户端传递超大文件导致 daemon OOM。
//! 6. **摘要校验**（可选）：客户端提供 `expected_sha256` 时，读取完毕后计算
//!    SHA-256 比对，不匹配则丢弃 buf + 返回 `digest_mismatch` 错误。
//!
//! ## 跨平台
//!
//! `fstat` 调用用 `#[cfg(unix)]` 限定。Windows 下 `from_raw_fd` 不存在，
//! FD 路径在 dispatch 层已拒绝（`#[cfg(not(unix))]` 分支）。
//! 本模块仅在 Unix 下编译。
//! `F_GET_SEALS` 仅 Linux 可用，macOS/BSD 跳过 seals 校验。

#![cfg(unix)]

use std::io::{self, Read};
use std::os::unix::io::{FromRawFd, RawFd};

use sha2::{Digest, Sha256};

/// 默认 FD 读取上限：64 MB
///
/// 选择依据：tree-sitter parser 单文件最大处理约 8MB（Linux kernel 大文件），
/// 64MB 留出 8x 余量。超过此大小的 FD 视为异常 / 恶意输入。
pub const DEFAULT_MAX_FD_READ_BYTES: usize = 64 * 1024 * 1024;

// ============================================
// P1-3（2026-07-21）：Linux memfd seals 常量
// ============================================
//
// 规范：Linux <uapi/linux/fcntl.h> + server/ipc_transport.py
// libc crate 在 Linux 上提供 F_GET_SEALS / F_SEAL_* 常量，但版本兼容性不一，
// 这里显式定义避免依赖 libc 版本。
//
// F_GET_SEALS = (F_LINUX_SPECIFIC_BASE + 3) = 1024 + 3 = 1034
// 仅 Linux 3.17+ 支持；macOS/BSD 无此系统调用。

#[cfg(target_os = "linux")]
const F_GET_SEALS: libc::c_int = 1034;

#[cfg(target_os = "linux")]
const F_SEAL_SEAL: libc::c_int = 0x0001;
#[cfg(target_os = "linux")]
const F_SEAL_SHRINK: libc::c_int = 0x0002;
#[cfg(target_os = "linux")]
const F_SEAL_GROW: libc::c_int = 0x0004;
#[cfg(target_os = "linux")]
const F_SEAL_WRITE: libc::c_int = 0x0008;

/// memfd 必须包含的 seals 集合（与 Python validate_memfd_fd 一致）
#[cfg(target_os = "linux")]
const REQUIRED_SEALS: libc::c_int = F_SEAL_SEAL | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE;

/// FD 读取错误
#[derive(Debug)]
pub enum FdReadError {
    /// fstat 调用失败（FD 无效或无权限）
    FstatFailed(io::Error),
    /// FD 不是常规文件（目录 / 设备 / 套接字 / FIFO）
    NotRegularFile(u32),
    /// owner UID 不匹配（P1-3 新增）
    /// peer 传递不属于自己 UID 的 FD，绕过文件系统 ACL
    OwnerMismatch { fd_uid: u32, peer_uid: u32 },
    /// memfd seals 校验失败（P1-3 新增，仅 Linux）
    /// seals 缺失或不足，daemon 可能被 lseek+write 篡改内容
    SealsInsufficient {
        actual: libc::c_int,
        required: libc::c_int,
    },
    /// 文件大小超过上限
    SizeExceedsLimit { size: u64, limit: u64 },
    /// 读取失败（I/O 错误）
    ReadFailed(io::Error),
    /// 摘要不匹配（客户端提供的 expected_sha256 与实际不符）
    DigestMismatch { expected: String, actual: String },
}

impl std::fmt::Display for FdReadError {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            FdReadError::FstatFailed(e) => write!(f, "fstat 失败: {}", e),
            FdReadError::NotRegularFile(mode) => {
                write!(f, "FD 不是常规文件（st_mode=0o{:o}）", mode)
            }
            FdReadError::OwnerMismatch { fd_uid, peer_uid } => {
                write!(
                    f,
                    "FD owner UID 不匹配：fd_uid={}，peer_uid={}（跨 UID 攻击）",
                    fd_uid, peer_uid
                )
            }
            FdReadError::SealsInsufficient { actual, required } => {
                write!(
                    f,
                    "memfd seals 不足：actual=0o{:o}，required=0o{:o}",
                    actual, required
                )
            }
            FdReadError::SizeExceedsLimit { size, limit } => {
                write!(f, "文件大小 {} 超过上限 {}", size, limit)
            }
            FdReadError::ReadFailed(e) => write!(f, "读取失败: {}", e),
            FdReadError::DigestMismatch { expected, actual } => {
                write!(f, "摘要不匹配：expected={}, actual={}", expected, actual)
            }
        }
    }
}

impl std::error::Error for FdReadError {}

/// fstat 返回字段的简化封装（mode + size + uid）
#[derive(Debug, Clone, Copy)]
struct FileStat {
    st_mode: u32,
    st_size: i64,
    /// P1-3 新增：文件 owner UID，用于与 peer_uid 比对
    st_uid: u32,
}

/// 获取 FD 的 stat（mode + size + uid）
///
/// 直接调用 libc::fstat，跨平台（Linux/macOS/BSD 均支持）。
fn fd_stat(fd: RawFd) -> io::Result<FileStat> {
    let mut st: libc::stat = unsafe { std::mem::zeroed() };
    let ret = unsafe { libc::fstat(fd, &mut st) };
    if ret != 0 {
        return Err(io::Error::last_os_error());
    }
    Ok(FileStat {
        st_mode: st.st_mode as u32,
        st_size: st.st_size as i64,
        st_uid: st.st_uid,
    })
}

/// S_IFREG 宏（常规文件）
#[cfg(unix)]
const S_IFMT: u32 = 0o170_000;
#[cfg(unix)]
const S_IFREG: u32 = 0o100_000;

/// P1-3 新增：检测 FD 是否为 memfd 并校验 seals（仅 Linux）
///
/// 规范：server/ipc_transport.py:is_memfd + validate_memfd_fd
///
/// 实现策略：
/// - Linux：调用 fcntl(fd, F_GET_SEALS)，成功返回 seals bitmask
///   - 非 memfd FD 返回 EINVAL（视为普通文件，跳过校验）
///   - memfd 但 seals 不足返回 SealsInsufficient
/// - 非 Linux：跳过（macOS/BSD 无 memfd_create）
///
/// 返回值：
/// - Ok(true)：memfd 且 seals 通过
/// - Ok(false)：非 memfd（普通文件），跳过 seals 校验
/// - Err：memfd 但 seals 不足
#[cfg(target_os = "linux")]
fn verify_memfd_seals(fd: RawFd) -> Result<bool, FdReadError> {
    let ret = unsafe { libc::fcntl(fd, F_GET_SEALS) };
    if ret < 0 {
        let err = io::Error::last_os_error();
        // EINVAL/ENOTTY: 非 memfd（普通文件），跳过 seals 校验
        // EINVAL 在 Linux 上表示 FD 不支持 seals（普通文件）
        if err.raw_os_error() == Some(libc::EINVAL) || err.raw_os_error() == Some(libc::ENOTTY) {
            return Ok(false);
        }
        // 其他错误（EBADF 等）视为校验失败
        return Err(FdReadError::FstatFailed(err));
    }
    let actual = ret as libc::c_int;
    if (actual & REQUIRED_SEALS) != REQUIRED_SEALS {
        return Err(FdReadError::SealsInsufficient {
            actual,
            required: REQUIRED_SEALS,
        });
    }
    Ok(true)
}

/// 非 Linux 平台：跳过 seals 校验
#[cfg(not(target_os = "linux"))]
fn verify_memfd_seals(_fd: RawFd) -> Result<bool, FdReadError> {
    Ok(false)
}

/// 从 FD 读取字节——执行六重校验
///
/// 参数：
/// - `fd`: 已通过 SCM_RIGHTS 接收的 FD（由调用方负责 close）
/// - `max_bytes`: 读取上限（默认 64MB，调用方可自定义）
/// - `expected_sha256`: 客户端提供的预期摘要（None 跳过摘要校验）
/// - `peer_uid`: 发送方进程 UID（来自 SO_PEERCRED），用于 owner 校验
///   `peer_uid == 0`（root）跳过 owner 校验
///
/// 返回：
/// - `Ok(Vec<u8>)`：读取成功 + 校验通过
/// - `Err(FdReadError)`：任一校验失败
///
/// 副作用：
/// - 成功：FD 所有权转移到 File，读取完毕后 File drop 自动关闭 FD
/// - 失败：FD 同样被 File drop 关闭，调用方无需手动 close
///
/// 校验顺序（fail-fast）：
/// 1. fstat + FD 类型校验
/// 2. owner UID 校验（st_uid == peer_uid，root 跳过）
/// 3. seals 校验（仅 Linux memfd，非 memfd 跳过）
/// 4. 大小预检 + 容量上限
/// 5. 读取（chunked + 运行时容量上限）
/// 6. 摘要校验（可选）
pub fn read_from_fd_with_validation(
    fd: RawFd,
    max_bytes: usize,
    expected_sha256: Option<&str>,
    peer_uid: u32,
) -> Result<Vec<u8>, FdReadError> {
    // 校验 1：fstat + FD 类型校验
    let stat = fd_stat(fd).map_err(FdReadError::FstatFailed)?;
    let file_type = stat.st_mode & S_IFMT;
    if file_type != S_IFREG {
        return Err(FdReadError::NotRegularFile(stat.st_mode));
    }

    // 校验 2（P1-3）：owner UID 校验
    // peer_uid == 0（root）跳过此校验，root 受 SO_PEERCRED 信任
    // 防止 peer 传递不属于自己 UID 的 FD（如其他用户的 /tmp 文件）
    if peer_uid != 0 && stat.st_uid != peer_uid {
        return Err(FdReadError::OwnerMismatch {
            fd_uid: stat.st_uid,
            peer_uid,
        });
    }

    // 校验 3（P1-3）：memfd seals 校验（仅 Linux）
    // 非 memfd FD（普通文件）跳过此项
    // memfd FD 必须包含 F_SEAL_SEAL | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE
    verify_memfd_seals(fd)?;

    // 校验 4 + 5：大小预检 + 容量上限
    // st_size 为 0 时（临时文件 / memfd），仍尝试读取，但用 read_to_end 兜底
    let file_size = stat.st_size.max(0) as u64;
    if file_size > max_bytes as u64 {
        return Err(FdReadError::SizeExceedsLimit {
            size: file_size,
            limit: max_bytes as u64,
        });
    }

    // 接管 FD 所有权：from_raw_fd 后 File drop 会自动 close(fd)
    let mut file = unsafe { std::fs::File::from_raw_fd(fd) };

    // 预分配 buf：避免 read_to_end 的指数扩容
    // st_size 为 0 时预分配 4KB，让 read_to_end 自然增长（受 max_bytes 限制）
    let initial_cap = if file_size > 0 {
        file_size as usize
    } else {
        4 * 1024
    };
    let mut buf = Vec::with_capacity(initial_cap);

    // 读取：手动控制读取量，防止 st_size=0 但实际数据超大（管道攻击）
    // 安全策略：每读取一个 chunk 检查总大小是否超限
    let mut chunk = [0u8; 64 * 1024]; // 64KB chunk
    let mut hasher = if expected_sha256.is_some() {
        Some(Sha256::new())
    } else {
        None
    };

    loop {
        match file.read(&mut chunk) {
            Ok(0) => break, // EOF
            Ok(n) => {
                buf.extend_from_slice(&chunk[..n]);
                if let Some(ref mut h) = hasher {
                    h.update(&chunk[..n]);
                }
                // 校验 5（运行时）：每 chunk 检查总大小
                if buf.len() > max_bytes {
                    return Err(FdReadError::SizeExceedsLimit {
                        size: buf.len() as u64,
                        limit: max_bytes as u64,
                    });
                }
            }
            Err(e) if e.kind() == io::ErrorKind::Interrupted => continue,
            Err(e) => return Err(FdReadError::ReadFailed(e)),
        }
    }

    // 校验 6：摘要校验（可选）
    if let Some(expected) = expected_sha256 {
        let hasher = hasher.expect("hasher should be initialized when expected_sha256 is Some");
        let digest = hasher.finalize();
        let actual = hex::encode(digest);
        if actual != expected {
            return Err(FdReadError::DigestMismatch {
                expected: expected.to_string(),
                actual,
            });
        }
    }

    Ok(buf)
}

/// 便利函数：使用默认上限 + 不校验摘要 + 跳过 owner 校验（root 语义）
///
/// **注意**：仅用于 daemon 内部读取（peer_uid=0），生产路径应显式传 peer_uid
pub fn read_from_fd_default(fd: RawFd) -> Result<Vec<u8>, FdReadError> {
    read_from_fd_with_validation(fd, DEFAULT_MAX_FD_READ_BYTES, None, 0)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::os::unix::io::AsRawFd;

    /// 构造一个 peer_uid = current_uid() 的 peer（用于通过 owner 校验）
    fn current_uid() -> u32 {
        // SAFETY: getuid 永远安全
        unsafe { libc::getuid() }
    }

    /// 临时文件 FD 复制工具（修复既有 try_clone 不可用问题）
    ///
    /// tempfile 3.27 的 NamedTempFile 没有 `try_clone()` 方法，但测试需要独立 FD
    /// 交给 `from_raw_fd` 接管。用 `libc::dup` 复制 FD 替代 `try_clone`。
    fn dup_tempfile_fd(tmp: &tempfile::NamedTempFile) -> RawFd {
        let fd = tmp.as_raw_fd();
        let new_fd = unsafe { libc::dup(fd) };
        assert!(new_fd >= 0, "dup failed: {}", io::Error::last_os_error());
        new_fd
    }

    #[test]
    fn test_read_from_fd_small_file() {
        // 创建临时文件并写入数据
        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        tmp.write_all(b"hello world").unwrap();
        tmp.flush().unwrap();

        // 用 dup 复制 FD（避免 from_raw_fd 关闭原 FD）
        let cloned_fd = dup_tempfile_fd(&tmp);

        let result =
            read_from_fd_with_validation(cloned_fd, DEFAULT_MAX_FD_READ_BYTES, None, current_uid());
        assert!(result.is_ok(), "read failed: {:?}", result.err());
        let buf = result.unwrap();
        assert_eq!(buf, b"hello world");
    }

    #[test]
    fn test_read_from_fd_with_digest_verification() {
        let data = b"sha256 test payload";
        let mut hasher = Sha256::new();
        hasher.update(data);
        let expected = hex::encode(hasher.finalize());

        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        tmp.write_all(data).unwrap();
        tmp.flush().unwrap();

        let cloned_fd = dup_tempfile_fd(&tmp);

        let result = read_from_fd_with_validation(
            cloned_fd,
            DEFAULT_MAX_FD_READ_BYTES,
            Some(&expected),
            current_uid(),
        );
        assert!(result.is_ok());
        assert_eq!(result.unwrap(), data);
    }

    #[test]
    fn test_read_from_fd_digest_mismatch() {
        let data = b"actual content";
        let wrong_hash = "0000000000000000000000000000000000000000000000000000000000000000";

        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        tmp.write_all(data).unwrap();
        tmp.flush().unwrap();

        let cloned_fd = dup_tempfile_fd(&tmp);

        let result = read_from_fd_with_validation(
            cloned_fd,
            DEFAULT_MAX_FD_READ_BYTES,
            Some(wrong_hash),
            current_uid(),
        );
        match result {
            Err(FdReadError::DigestMismatch { expected, actual }) => {
                assert_eq!(expected, wrong_hash);
                assert_ne!(actual, wrong_hash);
            }
            other => panic!("expected DigestMismatch, got {:?}", other),
        }
    }

    #[test]
    fn test_read_from_fd_size_exceeds_limit() {
        // 创建一个 1KB 文件，但 limit 设为 100 字节
        let data = vec![b'x'; 1024];
        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        tmp.write_all(&data).unwrap();
        tmp.flush().unwrap();

        let cloned_fd = dup_tempfile_fd(&tmp);

        // fstat 报告 st_size=1024 > 100，应立即拒绝
        let result = read_from_fd_with_validation(cloned_fd, 100, None, current_uid());
        match result {
            Err(FdReadError::SizeExceedsLimit { size, limit }) => {
                assert_eq!(size, 1024);
                assert_eq!(limit, 100);
            }
            other => panic!("expected SizeExceedsLimit, got {:?}", other),
        }
    }

    #[test]
    fn test_read_from_fd_not_regular_file() {
        // 创建管道：FD 不是常规文件
        let (read_fd, write_fd) = {
            let mut fds = [0i32; 2];
            let ret = unsafe { libc::pipe(fds.as_mut_ptr()) };
            assert_eq!(ret, 0, "pipe creation failed");
            (fds[0], fds[1])
        };

        // 关闭写端（避免读端阻塞）
        unsafe { libc::close(write_fd) };

        let result =
            read_from_fd_with_validation(read_fd, DEFAULT_MAX_FD_READ_BYTES, None, current_uid());
        match result {
            Err(FdReadError::NotRegularFile(_)) => {}
            other => {
                // 某些平台 pipe fstat 可能返回 S_IFIFO，符合预期
                let _ = unsafe { libc::close(read_fd) };
                panic!("expected NotRegularFile, got {:?}", other);
            }
        }
    }

    #[test]
    fn test_read_from_fd_empty_file() {
        let tmp = tempfile::NamedTempFile::new().unwrap();
        let cloned_fd = dup_tempfile_fd(&tmp);

        let result =
            read_from_fd_with_validation(cloned_fd, DEFAULT_MAX_FD_READ_BYTES, None, current_uid());
        assert!(result.is_ok());
        assert_eq!(result.unwrap().len(), 0);
    }

    /// P1-3 新增：owner UID 校验
    /// peer_uid != st_uid 应拒绝（防止跨 UID 攻击）
    #[test]
    fn test_read_from_fd_owner_mismatch_rejected() {
        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        tmp.write_all(b"owned by current user").unwrap();
        tmp.flush().unwrap();

        let cloned_fd = dup_tempfile_fd(&tmp);

        // 使用一个不可能的 peer_uid（与 current_uid 不同）
        // 注意：若当前进程是 root（uid=0），owner 校验会被跳过
        let fake_peer_uid = if current_uid() == 0 {
            // root 进程：owner 校验跳过，本测试无法验证
            // 改为传 1（非 root），但 st_uid 仍是 root（0），应触发 OwnerMismatch
            1
        } else {
            current_uid() + 1
        };

        let result =
            read_from_fd_with_validation(cloned_fd, DEFAULT_MAX_FD_READ_BYTES, None, fake_peer_uid);
        match result {
            Err(FdReadError::OwnerMismatch { fd_uid, peer_uid }) => {
                assert_eq!(peer_uid, fake_peer_uid);
                assert_ne!(fd_uid, fake_peer_uid);
            }
            other => panic!("expected OwnerMismatch, got {:?}", other),
        }
    }

    /// P1-3 新增：peer_uid == 0（root）跳过 owner 校验
    #[test]
    fn test_read_from_fd_root_peer_skips_owner_check() {
        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        tmp.write_all(b"root readable").unwrap();
        tmp.flush().unwrap();

        let cloned_fd = dup_tempfile_fd(&tmp);

        // peer_uid=0 表示 root，应跳过 owner 校验
        let result = read_from_fd_with_validation(cloned_fd, DEFAULT_MAX_FD_READ_BYTES, None, 0);
        assert!(
            result.is_ok(),
            "root peer should bypass owner check: {:?}",
            result.err()
        );
        assert_eq!(result.unwrap(), b"root readable");
    }

    /// P1-3 新增：memfd seals 校验（仅 Linux）
    /// 真实 memfd_create 创建的 FD 应通过 seals 校验
    #[cfg(target_os = "linux")]
    #[test]
    fn test_verify_memfd_seals_real_memfd() {
        // 使用 syscall 创建 memfd（避免依赖 libc crate 的 memfd_create 绑定）
        // memfd_create 系统调用号：x86_64 = 319, aarch64 = 279
        // 这里用 libc::syscall 调用
        let fd = unsafe {
            let ret = libc::syscall(
                libc::SYS_memfd_create,
                b"cw_test\0".as_ptr() as *const libc::c_char,
                libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
            );
            if ret < 0 {
                // 某些容器/沙箱可能不支持 memfd_create，跳过测试
                eprintln!("memfd_create not supported, skipping test");
                return;
            }
            ret as RawFd
        };

        // 写入数据
        {
            let mut file = unsafe { std::fs::File::from_raw_fd(fd) };
            file.write_all(b"test memfd content").unwrap();
            // 添加完整 seals
            let ret = unsafe {
                libc::fcntl(
                    fd,
                    libc::F_ADD_SEALS,
                    F_SEAL_SEAL | F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE,
                )
            };
            assert!(
                ret >= 0,
                "F_ADD_SEALS failed: {}",
                io::Error::last_os_error()
            );
            std::mem::forget(file); // 不让 file drop 关闭 fd
        }

        // 校验 seals
        let result = verify_memfd_seals(fd);
        assert!(result.is_ok(), "seals check failed: {:?}", result.err());
        assert_eq!(result.unwrap(), true); // 是 memfd 且 seals 通过

        // 用 read_from_fd_with_validation 完整校验
        let result =
            read_from_fd_with_validation(fd, DEFAULT_MAX_FD_READ_BYTES, None, current_uid());
        assert!(result.is_ok(), "read memfd failed: {:?}", result.err());
        assert_eq!(result.unwrap(), b"test memfd content");

        // fd 已被 read_from_fd_with_validation 关闭
    }

    /// P1-3 新增：普通文件（非 memfd）跳过 seals 校验
    #[cfg(target_os = "linux")]
    #[test]
    fn test_verify_memfd_seals_regular_file_skipped() {
        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        tmp.write_all(b"regular file").unwrap();
        tmp.flush().unwrap();

        let cloned_fd = dup_tempfile_fd(&tmp);

        // 普通文件 F_GET_SEALS 返回 EINVAL，verify_memfd_seals 应返回 Ok(false)
        let result = verify_memfd_seals(cloned_fd);
        assert!(
            result.is_ok(),
            "regular file should skip seals: {:?}",
            result.err()
        );
        assert_eq!(result.unwrap(), false);

        // 关闭 cloned_fd
        unsafe { libc::close(cloned_fd) };
    }

    /// P1-3 新增：memfd seals 不足应拒绝
    #[cfg(target_os = "linux")]
    #[test]
    fn test_verify_memfd_seals_insufficient() {
        let fd = unsafe {
            let ret = libc::syscall(
                libc::SYS_memfd_create,
                b"cw_test_insuff\0".as_ptr() as *const libc::c_char,
                libc::MFD_CLOEXEC | libc::MFD_ALLOW_SEALING,
            );
            if ret < 0 {
                eprintln!("memfd_create not supported, skipping test");
                return;
            }
            ret as RawFd
        };

        // 写入数据
        {
            let file = unsafe { std::fs::File::from_raw_fd(fd) };
            file.write_all(b"insufficient seals").unwrap();
            std::mem::forget(file);
        }

        // 只加 F_SEAL_SEAL（缺少 SHRINK/GROW/WRITE）
        let ret = unsafe { libc::fcntl(fd, libc::F_ADD_SEALS, F_SEAL_SEAL) };
        assert!(ret >= 0, "F_ADD_SEALS F_SEAL_SEAL failed");

        // 校验 seals 应失败
        let result = verify_memfd_seals(fd);
        match result {
            Err(FdReadError::SealsInsufficient { actual, required }) => {
                assert_eq!(required, REQUIRED_SEALS);
                assert_ne!(actual & required, required);
            }
            other => panic!("expected SealsInsufficient, got {:?}", other),
        }

        // 关闭 fd
        unsafe { libc::close(fd) };
    }
}
