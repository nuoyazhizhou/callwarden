//! 获取连接对端进程凭证并派生 Peer_Identity —— 三平台实现。
//!
//! ## 平台机制
//! - Linux: SO_PEERCRED + ucred（内核保证不可伪造，含 uid/gid/pid）
//! - macOS: LOCAL_PEERCRED + xucred（内核保证不可伪造，含 uid/gid，**无 pid**）
//! - Windows: ImpersonateNamedPipeClient + GetTokenInformation（内核保证不可伪造，取对端 SID）
//!
//! ## 安全模型（Req 14.5, 14.8, 14.9）
//! - **Req 14.5**：Peer_Identity 必须且只能从操作系统 Peer_Credential 派生；
//!   客户端自报的 agent 名、session 名与请求体身份字段一律不参与授权判定，只作审计元数据。
//! - **Req 14.8**：macOS 的 Peer_Identity 仅由 UID + GID 构成；pid 不属于 Peer_Identity，
//!   因为 LOCAL_PEERCRED 不提供 pid。缺 pid 不得退化为无身份或拒绝全部请求。
//! - **Req 14.9**：Windows 的 Peer_Identity 由命名管道对端访问令牌 SID 构成；
//!   workspace 路径校验比较 peer SID 与注册 owner SID，不匹配则拒绝。
//!
//! ## pid 的语义
//! pid 在所有平台上仅为**审计元数据**，不参与授权判定：
//! - Linux: SO_PEERCRED 提供 pid，记录用于审计日志，但 owner_key() 只用 uid
//! - macOS: LOCAL_PEERCRED 不提供 pid，值为 0，不影响身份有效性
//! - Windows: GetNamedPipeClientProcessId 提供 pid，记录用于审计日志，但 owner_key() 只用 SID
//!
//! 规范：daemon-ipc-security.md §3.1

// ============================================
// 跨平台：PeerIdentity 授权身份抽象
// ============================================

/// 对端授权身份（仅含授权判定所需字段，不含审计元数据）。
///
/// Req 14.5：身份必须且只能从 OS Peer_Credential 派生。
/// Req 14.8：macOS 不含 pid。
/// Req 14.9：Windows 用 SID。
#[derive(Debug, Clone, PartialEq, Eq)]
pub enum PeerIdentity {
    /// Unix 平台授权身份：UID + GID（pid 仅审计，不在此处）
    Unix { uid: u32, gid: u32 },
    /// Windows 平台授权身份：对端令牌 SID（pid 仅审计，不在此处）
    Windows { sid: String },
}

impl PeerIdentity {
    /// 返回用于 ACL 比较的 owner key。
    ///
    /// - Unix: UID 字符串（如 "1000"）
    /// - Windows: SID 字符串（如 "S-1-5-21-..."）
    ///
    /// 此值是 workspace owner 校验的唯一比较对象（Req 14.9 对等 Unix UID ACL）。
    pub fn owner_key(&self) -> String {
        match self {
            PeerIdentity::Unix { uid, .. } => uid.to_string(),
            PeerIdentity::Windows { sid } => sid.clone(),
        }
    }

    /// 判断身份是否有效（非空/非零）。
    ///
    /// Req 14.8：macOS 缺 pid 不得退化为无身份——只要 uid 有效，身份即有效。
    pub fn is_valid(&self) -> bool {
        match self {
            // uid=0 是 root，仍然有效
            PeerIdentity::Unix { .. } => true,
            PeerIdentity::Windows { sid } => !sid.is_empty(),
        }
    }
}

// ============================================
// Unix 平台：PeerCred + get_peer_cred
// ============================================

#[cfg(unix)]
use std::os::unix::io::AsRawFd;
#[cfg(unix)]
use std::os::unix::net::UnixStream;

/// 对端进程凭证（来自 SO_PEERCRED / LOCAL_PEERCRED，内核保证不可伪造）。
///
/// 注意：pid 仅为审计元数据，不参与授权判定（Req 14.5）。
/// macOS 上 pid 恒为 0（LOCAL_PEERCRED 不提供），不影响身份有效性（Req 14.8）。
#[cfg(unix)]
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct PeerCred {
    pub uid: u32,
    pub gid: u32,
    /// 审计元数据，不参与授权。macOS 上恒为 0（Req 14.8）。
    pub pid: i32,
}

#[cfg(unix)]
impl PeerCred {
    /// 从 PeerCred 派生授权身份（剥离 pid 审计字段）。
    ///
    /// Req 14.8：macOS 缺 pid 不得退化为无身份——只要 uid/gid 存在即有效。
    pub fn to_peer_identity(&self) -> PeerIdentity {
        PeerIdentity::Unix {
            uid: self.uid,
            gid: self.gid,
        }
    }

    /// 返回用于 ACL 比较的 owner key（UID 字符串）。
    ///
    /// 等价 `to_peer_identity().owner_key()`，快捷路径避免分配。
    pub fn owner_key(&self) -> String {
        self.uid.to_string()
    }
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
        pid: cred.pid, // 审计元数据（Req 14.5：不参与授权）
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
        // Req 14.8：macOS LOCAL_PEERCRED 不提供 pid。
        // 缺 pid 不得退化为无身份或拒绝全部请求——uid/gid 有效即身份有效。
        pid: 0,
    })
}

/// 获取当前进程的 UID（用于 server 启动时记录 daemon owner）
#[cfg(unix)]
pub fn current_uid() -> u32 {
    // SAFETY: getuid 永远安全
    unsafe { libc::getuid() }
}

// ============================================
// Windows 平台：命名管道对端 SID 派生
// ============================================

/// Windows：从已连接的命名管道句柄派生对端 SID。
///
/// 机制（Req 14.5, 14.9）：
/// 1. ImpersonateNamedPipeClient —— 让当前线程模拟对端安全上下文
/// 2. OpenThreadToken —— 获取模拟令牌（即对端令牌）
/// 3. GetTokenInformation(TokenUser) —— 从令牌提取 SID
/// 4. RevertToSelf —— 恢复服务端身份
///
/// 返回的 SID 由内核在连接上下文中给出，客户端无法覆写（等价 Unix SO_PEERCRED）。
/// pid 通过 GetNamedPipeClientProcessId 单独获取，仅作审计元数据。
#[cfg(windows)]
pub fn get_peer_sid_from_pipe(pipe_handle: windows_sys::Win32::Foundation::HANDLE) -> std::io::Result<String> {
    use std::ffi::c_void;
    use std::ptr;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::Security::{
        GetTokenInformation, RevertToSelf, TokenUser, TOKEN_QUERY,
    };
    use windows_sys::Win32::System::Pipes::ImpersonateNamedPipeClient;
    use windows_sys::Win32::System::Threading::{GetCurrentThread, OpenThreadToken};

    unsafe {
        // 模拟客户端安全上下文
        if ImpersonateNamedPipeClient(pipe_handle) == 0 {
            return Err(std::io::Error::new(
                std::io::ErrorKind::Other,
                format!(
                    "ImpersonateNamedPipeClient 失败 (error {})",
                    windows_sys::Win32::Foundation::GetLastError()
                ),
            ));
        }

        // 获取当前线程令牌（即对端令牌）
        let mut token: HANDLE = ptr::null_mut();
        if OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, 0, &mut token) == 0 {
            RevertToSelf();
            return Err(std::io::Error::last_os_error());
        }

        // 获取令牌用户 SID
        let mut needed: u32 = 0;
        GetTokenInformation(token, TokenUser, ptr::null_mut(), 0, &mut needed);
        if needed == 0 {
            CloseHandle(token);
            RevertToSelf();
            return Err(std::io::Error::last_os_error());
        }

        let mut buf = vec![0u8; needed as usize];
        if GetTokenInformation(
            token,
            TokenUser,
            buf.as_mut_ptr() as *mut c_void,
            needed,
            &mut needed,
        ) == 0
        {
            CloseHandle(token);
            RevertToSelf();
            return Err(std::io::Error::last_os_error());
        }
        CloseHandle(token);
        RevertToSelf();

        // TOKEN_USER.User.Sid 是第一个字段
        let sid_ptr = *(buf.as_ptr() as *const *mut c_void);
        sid_to_string(sid_ptr)
    }
}

/// 将 SID 指针转换为 SDDL 字符串格式（S-1-5-21-...）。
#[cfg(windows)]
unsafe fn sid_to_string(sid: *mut std::ffi::c_void) -> std::io::Result<String> {
    use std::ptr;

    let sid_bytes = sid as *const u8;
    let revision = *sid_bytes;
    let sub_count = *sid_bytes.add(1) as usize;

    // IdentifierAuthority（6 字节，大端）
    let ia_ptr = sid_bytes.add(2);
    let ia_value = ((*(ia_ptr) as u64) << 40)
        | ((*(ia_ptr.add(1)) as u64) << 32)
        | ((*(ia_ptr.add(2)) as u64) << 24)
        | ((*(ia_ptr.add(3)) as u64) << 16)
        | ((*(ia_ptr.add(4)) as u64) << 8)
        | (*(ia_ptr.add(5)) as u64);

    let mut result = format!("S-{}-{}", revision, ia_value);

    // SubAuthority（每个 4 字节，小端）
    let sa_ptr = sid_bytes.add(8) as *const u32;
    for i in 0..sub_count {
        let sa = ptr::read_unaligned(sa_ptr.add(i));
        result.push_str(&format!("-{}", sa));
    }

    Ok(result)
}

/// Windows：从 Peer SID 派生授权身份。
///
/// Req 14.9：workspace 路径校验比较 peer SID 与注册 owner SID。
#[cfg(windows)]
pub fn sid_to_peer_identity(sid: String) -> PeerIdentity {
    PeerIdentity::Windows { sid }
}

/// Windows：获取当前进程用户的 SID 字符串。
///
/// 命名管道 SDDL（Req 14.18）保证只有 owner 能连接 daemon，
/// 因此当前用户 SID == 对端 peer SID。workspace.rs 的 validate_owned_path
/// 使用此函数作为 peer SID 来源。
#[cfg(windows)]
pub fn get_current_user_sid() -> std::io::Result<String> {
    use std::ffi::c_void;
    use std::ptr;
    use windows_sys::Win32::Foundation::{CloseHandle, HANDLE};
    use windows_sys::Win32::Security::{GetTokenInformation, TokenUser, TOKEN_QUERY};
    use windows_sys::Win32::System::Threading::{GetCurrentProcess, OpenProcessToken};

    unsafe {
        let mut token: HANDLE = ptr::null_mut();
        if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
            return Err(std::io::Error::last_os_error());
        }

        // 第一次调用获取所需缓冲区大小
        let mut needed: u32 = 0;
        GetTokenInformation(token, TokenUser, ptr::null_mut(), 0, &mut needed);
        if needed == 0 {
            CloseHandle(token);
            return Err(std::io::Error::last_os_error());
        }

        // 分配缓冲区并获取 TOKEN_USER
        let mut buf = vec![0u8; needed as usize];
        if GetTokenInformation(
            token,
            TokenUser,
            buf.as_mut_ptr() as *mut c_void,
            needed,
            &mut needed,
        ) == 0
        {
            CloseHandle(token);
            return Err(std::io::Error::last_os_error());
        }
        CloseHandle(token);

        // TOKEN_USER.User.Sid 是第一个字段
        let sid_ptr = *(buf.as_ptr() as *const *mut c_void);
        sid_to_string(sid_ptr)
    }
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;

    // --- 跨平台测试 ---

    #[test]
    fn test_peer_identity_owner_key_unix() {
        let id = PeerIdentity::Unix { uid: 1000, gid: 1000 };
        assert_eq!(id.owner_key(), "1000");
    }

    #[test]
    fn test_peer_identity_owner_key_windows() {
        let id = PeerIdentity::Windows {
            sid: "S-1-5-21-123-456-789".to_string(),
        };
        assert_eq!(id.owner_key(), "S-1-5-21-123-456-789");
    }

    #[test]
    fn test_peer_identity_valid_unix() {
        // uid=0 (root) 仍然有效
        let root = PeerIdentity::Unix { uid: 0, gid: 0 };
        assert!(root.is_valid());

        // 普通用户有效
        let user = PeerIdentity::Unix { uid: 1000, gid: 1000 };
        assert!(user.is_valid());
    }

    #[test]
    fn test_peer_identity_valid_windows() {
        let valid = PeerIdentity::Windows {
            sid: "S-1-5-21-123".to_string(),
        };
        assert!(valid.is_valid());

        // 空 SID 无效
        let invalid = PeerIdentity::Windows { sid: String::new() };
        assert!(!invalid.is_valid());
    }

    #[test]
    fn test_peer_identity_equality() {
        let a = PeerIdentity::Unix { uid: 1000, gid: 1000 };
        let b = PeerIdentity::Unix { uid: 1000, gid: 1000 };
        assert_eq!(a, b);

        // 不同 uid 不相等
        let c = PeerIdentity::Unix { uid: 1001, gid: 1000 };
        assert_ne!(a, c);
    }

    /// Req 14.8：macOS 缺 pid 不得退化为无身份。
    /// 验证 PeerIdentity::Unix 不含 pid 字段——身份有效性不依赖 pid。
    #[test]
    fn test_peer_identity_excludes_pid() {
        // PeerIdentity::Unix 只有 uid + gid，没有 pid 字段
        // 这保证了 macOS（pid=0）和 Linux（pid 有值）派生出相同的身份结构
        let id = PeerIdentity::Unix { uid: 501, gid: 20 };
        assert!(id.is_valid());
        assert_eq!(id.owner_key(), "501");
    }

    // --- Unix 平台测试 ---

    #[cfg(unix)]
    mod unix_tests {
        use super::super::*;
        use std::os::unix::net::UnixStream;

        #[test]
        fn test_peer_cred_to_peer_identity() {
            let cred = PeerCred {
                uid: 1000,
                gid: 1000,
                pid: 12345, // 审计字段，不应出现在 PeerIdentity 中
            };
            let identity = cred.to_peer_identity();
            assert_eq!(identity, PeerIdentity::Unix { uid: 1000, gid: 1000 });
            // owner_key 只用 uid
            assert_eq!(cred.owner_key(), "1000");
        }

        #[test]
        fn test_peer_cred_owner_key() {
            let cred = PeerCred { uid: 0, gid: 0, pid: 1 };
            assert_eq!(cred.owner_key(), "0");
        }

        /// macOS: pid=0 不影响 owner_key 和身份有效性
        #[test]
        fn test_macos_no_pid_does_not_degrade() {
            // 模拟 macOS 返回值（pid=0）
            let cred = PeerCred { uid: 501, gid: 20, pid: 0 };
            let identity = cred.to_peer_identity();
            assert!(identity.is_valid());
            assert_eq!(identity.owner_key(), "501");
        }

        /// 通过 socketpair 验证 get_peer_cred 能返回本进程的 uid
        #[cfg(target_os = "linux")]
        #[test]
        fn test_get_peer_cred_returns_local_uid() {
            let (a, _b) = UnixStream::pair().unwrap();
            let cred = get_peer_cred(&a).unwrap();
            assert_eq!(cred.uid, current_uid());
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

    // --- Windows 平台测试 ---

    #[cfg(windows)]
    mod windows_tests {
        use super::super::*;

        #[test]
        fn test_sid_to_peer_identity() {
            let id = sid_to_peer_identity("S-1-5-21-1000-2000-3000".to_string());
            assert_eq!(id.owner_key(), "S-1-5-21-1000-2000-3000");
            assert!(id.is_valid());
        }

        #[test]
        fn test_sid_to_string_format() {
            // 构造一个最小 SID 结构：Revision=1, SubCount=1, IA=5 (NT Authority), SA=21
            // SID 布局：[Revision:1][SubCount:1][IA:6 bytes][SA:4 bytes each]
            let sid_bytes: [u8; 12] = [
                1,    // Revision
                1,    // SubAuthorityCount
                0, 0, 0, 0, 0, 5, // IdentifierAuthority (big-endian: 5 = NT Authority)
                21, 0, 0, 0, // SubAuthority[0] = 21 (little-endian)
            ];
            let sid_str = unsafe { sid_to_string(sid_bytes.as_ptr() as *mut std::ffi::c_void) };
            assert!(sid_str.is_ok());
            assert_eq!(sid_str.unwrap(), "S-1-5-21");
        }
    }
}
