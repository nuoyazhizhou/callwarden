//! 获取连接对端进程凭证（UID/GID/PID）—— 跨平台实现。
//!
//! Linux: SO_PEERCRED + ucred（内核保证不可伪造）
//! macOS: LOCAL_PEERCRED + xucred（无 pid）
//!
//! 规范：daemon-ipc-security.md §3.1

use std::os::unix::io::AsRawFd;
use std::os::unix::net::UnixStream;

/// 对端进程凭证（来自 SO_PEERCRED / LOCAL_PEERCRED，内核保证不可伪造）
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PeerCred {
    pub uid: u32,
    pub gid: u32,
    pub pid: i32,
}

// Linux: 使用 SO_PEERCRED + ucred
#[cfg(target_os = "linux")]
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

// macOS: 使用 LOCAL_PEERCRED + xucred（无 pid 字段）
#[cfg(target_os = "macos")]
pub fn get_peer_cred(stream: &UnixStream) -> std::io::Result<PeerCred> {
    let fd = stream.as_raw_fd();
    let mut cred: libc::xucred = unsafe { std::mem::zeroed() };
    let mut cred_len = std::mem::size_of::<libc::xucred>() as libc::socklen_t;

    let ret = unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_LOCAL,
            libc::LOCAL_PEERCRED,
            &mut cred as *mut _ as *mut _,
            &mut cred_len as *mut _,
        )
    };

    if ret < 0 {
        return Err(std::io::Error::last_os_error());
    }

    Ok(PeerCred {
        uid: cred.cr_uid,
        // xucred.cr_groups[0] 是主组 GID
        gid: if cred.cr_ngroups > 0 {
            cred.cr_groups[0]
        } else {
            0
        },
        pid: 0, // macOS xucred 没有 pid 字段
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
    #[cfg(target_os = "linux")]
    #[test]
    fn test_get_peer_cred_returns_local_uid() {
        let (a, _b) = UnixStream::pair().unwrap();
        let cred = get_peer_cred(&a).unwrap();
        // 回环连接的 peer uid 应该等于当前进程 uid
        assert_eq!(cred.uid, current_uid());
        // pid 应该是当前进程的 pid
        assert_eq!(cred.pid, std::process::id() as i32);
    }

    /// macOS: 验证 get_peer_cred 能返回 uid（无 pid 验证）
    #[cfg(target_os = "macos")]
    #[test]
    fn test_get_peer_cred_returns_local_uid() {
        let (a, _b) = UnixStream::pair().unwrap();
        let cred = get_peer_cred(&a).unwrap();
        assert_eq!(cred.uid, current_uid());
        // macOS xucred 没有 pid，值为 0
        assert_eq!(cred.pid, 0);
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
