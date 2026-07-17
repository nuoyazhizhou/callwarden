//! SO_PEERCRED——获取连接对端进程凭证（UID/GID/PID）。
//!
//! 仅 Linux/Unix 可用。规范：daemon-ipc-security.md §3.1
//!
//! 注意：本模块使用 libc（getsockopt/SO_PEERCRED）。libc 已在 Cargo.toml 中。

use std::os::unix::io::AsRawFd;
use std::os::unix::net::UnixStream;

/// 对端进程凭证（来自 SO_PEERCRED，内核保证不可伪造）
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PeerCred {
    pub uid: u32,
    pub gid: u32,
    pub pid: i32,
}

/// 从已连接的 UDS 获取对端进程的凭证。
///
/// Linux 上使用 SO_PEERCRED（getsockopt），内核保证不可伪造。
/// 对应 Python daemon_server.py:get_peer_credentials
pub fn get_peer_cred(stream: &UnixStream) -> std::io::Result<PeerCred> {
    let fd = stream.as_raw_fd();
    let mut cred = libc::ucred {
        pid: 0,
        uid: 0,
        gid: 0,
    };
    let mut cred_len = std::mem::size_of::<libc::ucred>() as libc::socklen_t;

    let ret = unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut cred as *mut _ as *mut _,
            &mut cred_len as *mut _,
        )
    };

    if ret < 0 {
        return Err(std::io::Error::last_os_error());
    }

    Ok(PeerCred {
        uid: cred.uid,
        gid: cred.gid,
        pid: cred.pid,
    })
}

/// 获取当前进程的 UID（用于 server 启动时记录 daemon owner）
pub fn current_uid() -> u32 {
    // SAFETY: getuid 永远安全
    unsafe { libc::getuid() }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::os::unix::net::UnixStream;

    /// 通过 socketpair 验证 get_peer_cred 能返回本进程的 uid
    #[test]
    fn test_get_peer_cred_returns_local_uid() {
        let (a, _b) = UnixStream::pair().unwrap();
        let cred = get_peer_cred(&a).unwrap();
        // 回环连接的 peer uid 应该等于当前进程 uid
        assert_eq!(cred.uid, current_uid());
        // pid 应该是当前进程的 pid
        assert_eq!(cred.pid, std::process::id() as i32);
    }

    /// 验证 PeerCred 是 Copy + Clone
    #[test]
    fn test_peer_cred_copy() {
        let cred1 = PeerCred {
            uid: 1000,
            gid: 1000,
            pid: 12345,
        };
        let cred2 = cred1;
        assert_eq!(cred1, cred2);
    }

    /// 验证 current_uid 与 unsafe libc::getuid 一致
    #[test]
    fn test_current_uid_matches_libc() {
        let uid = current_uid();
        let libc_uid = unsafe { libc::getuid() };
        assert_eq!(uid, libc_uid);
    }
}
