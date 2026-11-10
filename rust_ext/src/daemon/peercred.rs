//! principal 身份与 daemon owner 本地身份工具。
//!
//! ADR-002「单轨 HTTPS + mTLS」S4-02：对端身份不再由操作系统 Peer_Credential
//! （`SO_PEERCRED` / `LOCAL_PEERCRED` / `ImpersonateNamedPipeClient`）派生，
//! 唯一授权主语是 mTLS 客户端证书解析出的 [`PrincipalIdentity::principal_id`]。
//!
//! 本模块保留：
//! - [`FixedStr`]：给 `PeerCredential` 提供 `Copy` 语义的定长字符串；
//! - [`PrincipalIdentity`]：HTTP handler 的授权上下文；
//! - Windows 下 [`get_current_user_sid`]：daemon owner 本地引导身份（workspace
//!   owner 校验的服务端视角），不参与远程 RPC 授权。
//!
//! UDS / 命名管道对端凭证解析（`PeerIdentity` / `PeerCred` / `get_peer_cred` /
//! `get_peer_sid_from_pipe` / `sid_to_peer_identity` / `current_uid`）已随传输层删除。

// ============================================
// principal 身份（ADR-002 §5.1）
// ============================================

/// 定长、可 `Copy` 的字符串。
///
/// `PeerCredential` 在 dispatch/handler 层被大量按值传递（历史契约依赖 `Copy`），
/// 因此 principal 字段不能引入 `String`/`Vec`——一旦失去 `Copy`，所有
/// `DaemonStateExt` handler 签名与 peer 复用点都会连锁失效。这里用固定缓冲
/// 承载：超长（`None`）由调用方按 fail-closed 处理。
#[derive(Clone, Copy, PartialEq, Eq)]
pub struct FixedStr<const N: usize> {
    len: u16,
    buf: [u8; N],
}

impl<const N: usize> Default for FixedStr<N> {
    fn default() -> Self {
        Self {
            len: 0,
            buf: [0u8; N],
        }
    }
}

impl<const N: usize> FixedStr<N> {
    pub const fn empty() -> Self {
        Self {
            len: 0,
            buf: [0u8; N],
        }
    }

    /// 写入字符串；超出容量返回 `None`（调用方必须 fail-closed）。
    pub fn from_str(s: &str) -> Option<Self> {
        let bytes = s.as_bytes();
        if bytes.len() > N || bytes.len() > u16::MAX as usize {
            return None;
        }
        let mut buf = [0u8; N];
        buf[..bytes.len()].copy_from_slice(bytes);
        Some(Self {
            len: bytes.len() as u16,
            buf,
        })
    }

    pub fn as_str(&self) -> &str {
        std::str::from_utf8(&self.buf[..self.len as usize]).unwrap_or("")
    }

    pub fn is_empty(&self) -> bool {
        self.len == 0
    }
}

impl<const N: usize> std::fmt::Debug for FixedStr<N> {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        write!(f, "{:?}", self.as_str())
    }
}

/// mTLS 连接解析出的 principal 身份（ADR-002 §5）。
///
/// 授权主语只有 `principal_id`（证书 SAN URI）；`cert_serial`/`cert_sha256`
/// 仅用于吊销与审计定位，`kind` 决定 workspace 默认范围（`service` = 全部）。
#[derive(Clone, Copy, PartialEq, Eq, Default, Debug)]
pub struct PrincipalIdentity {
    /// SAN URI 中的 principal_id（`pr_<32 hex>`）。
    pub principal_id: FixedStr<48>,
    /// 叶证书 serial（吊销比对）。
    pub cert_serial: FixedStr<64>,
    /// 叶证书 SHA-256（审计/排查）。
    pub cert_sha256: FixedStr<64>,
    /// `service` / `agent` / `human`（DB `pki_principals.kind` 投影）。
    pub kind: FixedStr<16>,
    /// 逗号分隔的 workspace_id 白名单；`service` 为空表示“全部”。
    pub allowed_workspace_ids: FixedStr<512>,
    /// 白名单超出容量：fail-closed，视为无任何 workspace 授权。
    pub scopes_overflow: bool,
}

impl PrincipalIdentity {
    pub fn is_authenticated(&self) -> bool {
        !self.principal_id.is_empty()
    }

    pub fn is_service(&self) -> bool {
        self.kind.as_str() == "service"
    }

    /// workspace 授权判定（ADR-002 §5.4：无“全局”授权，逐 workspace grant）。
    ///
    /// - 未认证 → false；
    /// - 白名单溢出 → false（fail-closed）；
    /// - `service` → true（daemon owner 本地引导身份）；
    /// - 其余 principal → 必须命中显式 grant，空列表即无权限。
    pub fn workspace_allowed(&self, workspace_id: &str) -> bool {
        if !self.is_authenticated() {
            return false;
        }
        if self.scopes_overflow {
            return false;
        }
        if self.is_service() {
            return true;
        }
        if self.allowed_workspace_ids.is_empty() {
            return false;
        }
        self.allowed_workspace_ids
            .as_str()
            .split(',')
            .any(|id| !id.is_empty() && id == workspace_id)
    }
}

// ============================================
// Windows：daemon owner 本地身份
// ============================================

/// Windows：获取当前进程用户的 SID 字符串。
///
/// 单轨下该 SID 只作为 daemon owner 的本地引导身份（workspace owner 校验的
/// 服务端视角）；远程 RPC 授权一律使用 mTLS principal，不读取对端 SID。
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

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_fixed_str_roundtrip_and_overflow() {
        let v = FixedStr::<48>::from_str("pr_0123456789abcdef").unwrap();
        assert_eq!(v.as_str(), "pr_0123456789abcdef");
        assert!(!v.is_empty());
        assert!(FixedStr::<48>::from_str(&"x".repeat(49)).is_none());
        // 空串合法（= 未认证）
        let empty = FixedStr::<48>::from_str("").unwrap();
        assert!(empty.is_empty());
    }

    #[test]
    fn test_principal_identity_service_is_unrestricted() {
        let ident = PrincipalIdentity {
            principal_id: FixedStr::<48>::from_str("pr_test").unwrap(),
            kind: FixedStr::<16>::from_str("service").unwrap(),
            ..Default::default()
        };
        assert!(ident.is_authenticated());
        assert!(ident.workspace_allowed("1"));
        assert!(ident.workspace_allowed("42"));
    }

    #[test]
    fn test_principal_identity_agent_requires_explicit_grant() {
        let ident = PrincipalIdentity {
            principal_id: FixedStr::<48>::from_str("pr_test").unwrap(),
            kind: FixedStr::<16>::from_str("agent").unwrap(),
            allowed_workspace_ids: FixedStr::<512>::from_str("1,7").unwrap(),
            ..Default::default()
        };
        assert!(ident.workspace_allowed("1"));
        assert!(ident.workspace_allowed("7"));
        assert!(!ident.workspace_allowed("2"));

        // 无 grant 的 agent → 全部拒绝（fail-closed）
        let bare = PrincipalIdentity {
            principal_id: FixedStr::<48>::from_str("pr_test").unwrap(),
            kind: FixedStr::<16>::from_str("agent").unwrap(),
            ..Default::default()
        };
        assert!(!bare.workspace_allowed("1"));
    }

    #[test]
    fn test_principal_identity_overflow_and_unauthenticated_fail_closed() {
        let overflow = PrincipalIdentity {
            principal_id: FixedStr::<48>::from_str("pr_test").unwrap(),
            kind: FixedStr::<16>::from_str("agent").unwrap(),
            allowed_workspace_ids: FixedStr::<512>::from_str("1").unwrap(),
            scopes_overflow: true,
            ..Default::default()
        };
        assert!(!overflow.workspace_allowed("1"));

        assert!(!PrincipalIdentity::default().workspace_allowed("1"));
    }

    #[cfg(windows)]
    mod windows_tests {
        use super::super::*;

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
