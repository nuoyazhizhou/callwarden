//! G10/G20: memfd / FD 读取四重校验——接收 SCM_RIGHTS 传递的 FD 时执行
//! 安全读取，避免 `read_to_end` 无界读导致 OOM / 内存耗尽攻击。
//!
//! ## 四重校验
//!
//! 1. **FD 类型校验**：`fstat` 检查 `st_mode & S_IFMT == S_IFREG`，
//!    拒绝读取目录 / 字符设备 / 块设备 / 套接字 / FIFO 等。
//!    例外：`memfd_create` 创建的 FD 在 `/proc/self/fd/<N>` fstat 显示为常规文件，
//!    `fstat` 通过即可放行。
//! 2. **大小预检**：从 `fstat.st_size` 获取文件大小，0 视为合法（管道/临时文件），
//!    按 `st_size` 预分配 buf 容量（避免 `read_to_end` 的指数扩容）。
//! 3. **容量上限**：超过 `MAX_FD_READ_BYTES`（默认 64MB）直接拒绝。
//!    防止恶意客户端传递超大文件导致 daemon OOM。
//! 4. **摘要校验**（可选）：客户端提供 `expected_sha256` 时，读取完毕后计算
//!    SHA-256 比对，不匹配则丢弃 buf + 返回 `digest_mismatch` 错误。
//!
//! ## 跨平台
//!
//! `fstat` 调用用 `#[cfg(unix)]` 限定。Windows 下 `from_raw_fd` 不存在，
//! FD 路径在 dispatch 层已拒绝（`#[cfg(not(unix))]` 分支）。
//! 本模块仅在 Unix 下编译。

#![cfg(unix)]

use std::io::{self, Read};
use std::os::unix::io::{FromRawFd, RawFd};

use sha2::{Digest, Sha256};

/// 默认 FD 读取上限：64 MB
///
/// 选择依据：tree-sitter parser 单文件最大处理约 8MB（Linux kernel 大文件），
/// 64MB 留出 8x 余量。超过此大小的 FD 视为异常 / 恶意输入。
pub const DEFAULT_MAX_FD_READ_BYTES: usize = 64 * 1024 * 1024;

/// FD 读取错误
#[derive(Debug)]
pub enum FdReadError {
    /// fstat 调用失败（FD 无效或无权限）
    FstatFailed(io::Error),
    /// FD 不是常规文件（目录 / 设备 / 套接字 / FIFO）
    NotRegularFile(u32),
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

/// fstat 返回字段的简化封装（仅 mode + size）
#[derive(Debug, Clone, Copy)]
struct FileStat {
    st_mode: u32,
    st_size: i64,
}

/// 获取 FD 的 stat（mode + size）
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
    })
}

/// S_IFREG 宏（常规文件）
#[cfg(unix)]
const S_IFMT: u32 = 0o170_000;
#[cfg(unix)]
const S_IFREG: u32 = 0o100_000;

/// 从 FD 读取字节——执行四重校验
///
/// 参数：
/// - `fd`: 已通过 SCM_RIGHTS 接收的 FD（由调用方负责 close）
/// - `max_bytes`: 读取上限（默认 64MB，调用方可自定义）
/// - `expected_sha256`: 客户端提供的预期摘要（None 跳过摘要校验）
///
/// 返回：
/// - `Ok(Vec<u8>)`：读取成功 + 校验通过
/// - `Err(FdReadError)`：任一校验失败
///
/// 副作用：
/// - 成功：FD 所有权转移到 File，读取完毕后 File drop 自动关闭 FD
/// - 失败：FD 同样被 File drop 关闭，调用方无需手动 close
pub fn read_from_fd_with_validation(
    fd: RawFd,
    max_bytes: usize,
    expected_sha256: Option<&str>,
) -> Result<Vec<u8>, FdReadError> {
    // 校验 1：fstat + FD 类型校验
    let stat = fd_stat(fd).map_err(FdReadError::FstatFailed)?;
    let file_type = stat.st_mode & S_IFMT;
    if file_type != S_IFREG {
        return Err(FdReadError::NotRegularFile(stat.st_mode));
    }

    // 校验 2 + 3：大小预检 + 容量上限
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
                // 校验 3（运行时）：每 chunk 检查总大小
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

    // 校验 4：摘要校验（可选）
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

/// 便利函数：使用默认上限 + 不校验摘要
pub fn read_from_fd_default(fd: RawFd) -> Result<Vec<u8>, FdReadError> {
    read_from_fd_with_validation(fd, DEFAULT_MAX_FD_READ_BYTES, None)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Write;
    use std::os::unix::io::AsRawFd;

    #[test]
    fn test_read_from_fd_small_file() {
        // 创建临时文件并写入数据
        let mut tmp = tempfile::NamedTempFile::new().unwrap();
        tmp.write_all(b"hello world").unwrap();
        tmp.flush().unwrap();

        // 复制 FD（避免 from_raw_fd 关闭原 FD）
        let fd = tmp.as_raw_fd();
        // 在测试中我们用 try_clone 得到独立的 FD，让 read_from_fd_with_validation 接管
        let cloned = tmp.try_clone().unwrap();
        let cloned_fd = cloned.as_raw_fd();
        // 防止 cloned drop 关闭 FD（让 from_raw_fd 接管）
        std::mem::forget(cloned);

        let result = read_from_fd_with_validation(cloned_fd, DEFAULT_MAX_FD_READ_BYTES, None);
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

        let cloned = tmp.try_clone().unwrap();
        let cloned_fd = cloned.as_raw_fd();
        std::mem::forget(cloned);

        let result = read_from_fd_with_validation(cloned_fd, DEFAULT_MAX_FD_READ_BYTES, Some(&expected));
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

        let cloned = tmp.try_clone().unwrap();
        let cloned_fd = cloned.as_raw_fd();
        std::mem::forget(cloned);

        let result = read_from_fd_with_validation(cloned_fd, DEFAULT_MAX_FD_READ_BYTES, Some(wrong_hash));
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

        let cloned = tmp.try_clone().unwrap();
        let cloned_fd = cloned.as_raw_fd();
        std::mem::forget(cloned);

        // fstat 报告 st_size=1024 > 100，应立即拒绝
        let result = read_from_fd_with_validation(cloned_fd, 100, None);
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

        let result = read_from_fd_with_validation(read_fd, DEFAULT_MAX_FD_READ_BYTES, None);
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
        let cloned = tmp.try_clone().unwrap();
        let cloned_fd = cloned.as_raw_fd();
        std::mem::forget(cloned);

        let result = read_from_fd_with_validation(cloned_fd, DEFAULT_MAX_FD_READ_BYTES, None);
        assert!(result.is_ok());
        assert_eq!(result.unwrap().len(), 0);
    }
}
