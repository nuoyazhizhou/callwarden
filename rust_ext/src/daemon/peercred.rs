//! SO_PEERCRED——获取连接对端进程凭证（UID/GID/PID）。
//! 仅 Linux 可用。
//! 注意：本模块使用 libc（getsockopt/SO_PEERCRED），需在 Cargo.toml 添加 `libc = "0.2"` 依赖后才能在 Linux 上编译。

use std::os::unix::io::AsRawFd;
use std::os::unix::net::UnixStream;

pub struct PeerCred {
    pub uid: u32,
    pub gid: u32,
    pub pid: i32,
}

pub fn get_peer_cred(stream: &UnixStream) -> std::io::Result<PeerCred> {
    let fd = stream.as_raw_fd();
    let mut cred = libc::ucred { pid: 0, uid: 0, gid: 0 };
    let cred_len = std::mem::size_of::<libc::ucred>() as u32;
    
    let ret = unsafe {
        libc::getsockopt(
            fd,
            libc::SOL_SOCKET,
            libc::SO_PEERCRED,
            &mut cred as *mut _ as *mut _,
            &cred_len as *const _ as *mut _,
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

pub fn handle_connection(stream: &UnixStream) -> std::io::Result<()> {
    let cred = get_peer_cred(stream)?;
    eprintln!("[cw_daemon] connection from uid={} gid={} pid={}", cred.uid, cred.gid, cred.pid);
    
    // TODO: 读取 JSON-RPC 请求 + 路由到 handler
    // 当前骨架：echo back
    use std::io::{Read, Write};
    let mut buf = [0u8; 1024];
    let n = stream.as_raw_fd();
    let _ = n;
    
    Ok(())
}
