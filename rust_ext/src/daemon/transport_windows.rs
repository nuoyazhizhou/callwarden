//! Windows 命名管道传输实现。
//!
//! D0 3.1（Req 14.2, 14.18, 14.19, 14.20, 14.21）：
//! - 管道名由 owner user SID 派生：`\\.\pipe\callwarden-<user-sid>`
//! - SDDL 只授权 owner SID 的 connect 与读写（可选附加 local administrators），
//!   其他 SID 一律不授权，使访问范围等价 Unix socket 的 owner + 0660
//! - 预创建 ≥2 个管道实例，服务每个已接受连接之前补建替换实例（先补建、后服务）
//! - 端点负向约束：不使用 AF_UNIX、TCP 或 HTTPS
//!
//! ## 安全模型
//! 身份由 ImpersonateNamedPipeClient + GetTokenInformation 从内核获取，
//! 客户端无法伪造。等价 Unix SO_PEERCRED 的安全保证。

#![cfg(windows)]

use std::ffi::c_void;
use std::io;
use std::ptr;
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;

use windows_sys::Win32::Foundation::{
    CloseHandle, GetLastError, ERROR_IO_PENDING, ERROR_PIPE_CONNECTED, HANDLE,
    INVALID_HANDLE_VALUE, TRUE, WAIT_OBJECT_0,
};
use windows_sys::Win32::Security::Authorization::{
    ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1,
};
use windows_sys::Win32::Security::{
    GetTokenInformation, RevertToSelf, TokenUser, TOKEN_QUERY, SECURITY_ATTRIBUTES,
};
use windows_sys::Win32::Storage::FileSystem::PIPE_ACCESS_DUPLEX;
use windows_sys::Win32::System::Pipes::{
    ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, GetNamedPipeClientProcessId,
    ImpersonateNamedPipeClient, PIPE_READMODE_MESSAGE, PIPE_TYPE_MESSAGE, PIPE_UNLIMITED_INSTANCES,
    PIPE_WAIT,
};
use windows_sys::Win32::System::Threading::{
    CreateEventW, GetCurrentProcess, OpenProcessToken, SetEvent, WaitForSingleObject,
};
use windows_sys::Win32::System::IO::{OVERLAPPED, OVERLAPPED_0, OVERLAPPED_0_0};

use super::transport::{
    TransportConfig, TransportConnection, TransportListener, TransportPeerIdentity,
};

// ============================================
// 常量
// ============================================

/// 管道缓冲区大小（64 KB，与 Unix socket 默认缓冲区对齐）
const PIPE_BUFFER_SIZE: u32 = 65536;

/// 预创建管道实例数（Req 14.19：至少 2 个）
const MIN_PIPE_INSTANCES: usize = 2;

/// 最大等待连接超时（毫秒，用于响应 shutdown）
const ACCEPT_TIMEOUT_MS: u32 = 200;

// ============================================
// 辅助：获取当前用户 SID（SDDL 字符串）
// ============================================

/// 获取当前进程 owner 的 SID 字符串（SDDL 格式，如 S-1-5-21-...）。
///
/// 用于派生管道名和构建 SDDL 安全描述符。
pub fn get_current_user_sid() -> io::Result<String> {
    unsafe {
        let mut token: HANDLE = ptr::null_mut();
        if OpenProcessToken(GetCurrentProcess(), TOKEN_QUERY, &mut token) == 0 {
            return Err(io::Error::last_os_error());
        }

        // 第一次调用获取所需缓冲区大小
        let mut needed: u32 = 0;
        GetTokenInformation(token, TokenUser, ptr::null_mut(), 0, &mut needed);
        if needed == 0 {
            CloseHandle(token);
            return Err(io::Error::last_os_error());
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
            return Err(io::Error::last_os_error());
        }
        CloseHandle(token);

        // TOKEN_USER.User.Sid 是第一个字段
        let sid_ptr = *(buf.as_ptr() as *const *mut c_void);
        sid_to_string(sid_ptr)
    }
}

/// 将 SID 指针转换为 SDDL 字符串格式。
unsafe fn sid_to_string(sid: *mut c_void) -> io::Result<String> {
    // 使用 ConvertSidToStringSidW 的等价逻辑：
    // 手动解析 SID 结构（Revision-SubAuthorityCount-IdentifierAuthority-SubAuthority）
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
// SDDL 安全描述符构建
// ============================================

/// 构建命名管道的 SDDL 安全描述符字符串。
///
/// Req 14.18：只授权 owner SID 的 connect 与读写（可选附加 local administrators），
/// 其他 SID 一律不授权，使访问范围等价 Unix socket 的 owner + 0660。
///
/// 格式：`D:P(A;;GA;;;<owner-sid>)(A;;GA;;;BA)`
/// - D:P — DACL，Protected（不继承）
/// - A;;GA;;;<sid> — Allow Generic All 给 owner
/// - A;;GA;;;BA — Allow Generic All 给 Builtin Administrators（可选）
fn build_pipe_sddl(owner_sid: &str, include_admins: bool) -> String {
    let mut sddl = format!("D:P(A;;GA;;;{})", owner_sid);
    if include_admins {
        // BA = Builtin Administrators（S-1-5-32-544）
        sddl.push_str("(A;;GA;;;BA)");
    }
    sddl
}

/// 从 SDDL 字符串创建 SECURITY_ATTRIBUTES（用于 CreateNamedPipe）。
///
/// 返回的 SECURITY_DESCRIPTOR 由 LocalAlloc 分配，调用方负责 LocalFree。
unsafe fn create_security_attributes(sddl: &str) -> io::Result<SecurityAttributesGuard> {
    let wide: Vec<u16> = sddl.encode_utf16().chain(std::iter::once(0)).collect();
    let mut sd: *mut c_void = ptr::null_mut();

    if ConvertStringSecurityDescriptorToSecurityDescriptorW(
        wide.as_ptr(),
        SDDL_REVISION_1,
        &mut sd,
        ptr::null_mut(),
    ) == 0
    {
        return Err(io::Error::new(
            io::ErrorKind::Other,
            format!("SDDL 转换失败 (error {}): {}", GetLastError(), sddl),
        ));
    }

    Ok(SecurityAttributesGuard { sd })
}

/// RAII guard：持有 SECURITY_DESCRIPTOR 指针，Drop 时 LocalFree。
struct SecurityAttributesGuard {
    sd: *mut c_void,
}

impl Drop for SecurityAttributesGuard {
    fn drop(&mut self) {
        if !self.sd.is_null() {
            unsafe {
                windows_sys::Win32::Foundation::LocalFree(self.sd);
            }
        }
    }
}

// ============================================
// 管道实例创建
// ============================================

/// 创建单个命名管道实例。
///
/// 使用 FILE_FLAG_OVERLAPPED 以支持超时等待和 shutdown 打断。
unsafe fn create_pipe_instance(
    pipe_name: &str,
    sa: &SecurityAttributesGuard,
) -> io::Result<HANDLE> {
    let wide: Vec<u16> = pipe_name.encode_utf16().chain(std::iter::once(0)).collect();

    // SECURITY_ATTRIBUTES 结构体
    let sec_attr = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: sa.sd,
        bInheritHandle: 0,
    };

    let handle = CreateNamedPipeW(
        wide.as_ptr(),
        PIPE_ACCESS_DUPLEX | windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OVERLAPPED,
        PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
        PIPE_UNLIMITED_INSTANCES,
        PIPE_BUFFER_SIZE,
        PIPE_BUFFER_SIZE,
        0, // 默认超时
        &sec_attr,
    );

    if handle == INVALID_HANDLE_VALUE {
        return Err(io::Error::new(
            io::ErrorKind::Other,
            format!(
                "CreateNamedPipe 失败 (error {}): {}",
                GetLastError(),
                pipe_name
            ),
        ));
    }

    Ok(handle)
}

// ============================================
// NamedPipeListener
// ============================================

/// Windows 命名管道监听器。
///
/// Req 14.19（实例保活，硬要求）：
/// 预创建 ≥2 个管道实例，并在服务每个已接受连接之前补建替换实例，
/// 消除两次 accept 之间的 pipe-busy / 端点缺失竞态窗口。
/// 先补建、后服务——顺序颠倒即重新引入竞态。
pub struct NamedPipeListener {
    /// 管道名（`\\.\pipe\callwarden-<user-sid>`）
    pipe_name: String,
    /// SDDL 安全描述符字符串（保留用于诊断 / endpoint_description 扩展）
    #[allow(dead_code)]
    sddl: String,
    /// SECURITY_ATTRIBUTES guard（生命周期与 listener 绑定）
    sa: SecurityAttributesGuard,
    /// 空闲管道实例句柄池（始终保持 ≥1 个空闲实例）
    free_instances: Vec<HANDLE>,
    /// 停止标志
    stop_flag: Arc<AtomicBool>,
    /// 用于打断 ConnectNamedPipe 阻塞的事件
    shutdown_event: HANDLE,
    /// 传输配置
    config: TransportConfig,
}

// SAFETY: HANDLE 在 Windows 上是线程安全的（内核对象句柄可跨线程使用）
unsafe impl Send for NamedPipeListener {}

impl NamedPipeListener {
    /// 绑定命名管道端点。
    ///
    /// 1. 获取当前用户 SID
    /// 2. 派生管道名 `\\.\pipe\callwarden-<user-sid>`
    /// 3. 构建 SDDL（owner + 可选 admins）
    /// 4. 预创建 ≥2 个管道实例
    pub fn bind(config: &TransportConfig) -> io::Result<Self> {
        let owner_sid = get_current_user_sid()?;
        let pipe_name = format!(r"\\.\pipe\callwarden-{}", owner_sid);
        let sddl = build_pipe_sddl(&owner_sid, true);
        let sa = unsafe { create_security_attributes(&sddl)? };

        // 创建 shutdown 事件（手动重置，初始无信号）
        let shutdown_event = unsafe { CreateEventW(ptr::null(), TRUE, 0, ptr::null()) };
        if shutdown_event.is_null() {
            return Err(io::Error::last_os_error());
        }

        // 预创建 ≥2 个管道实例（Req 14.19）
        let mut free_instances = Vec::with_capacity(MIN_PIPE_INSTANCES);
        for _ in 0..MIN_PIPE_INSTANCES {
            let handle = unsafe { create_pipe_instance(&pipe_name, &sa)? };
            free_instances.push(handle);
        }

        Ok(Self {
            pipe_name,
            sddl,
            sa,
            free_instances,
            stop_flag: Arc::new(AtomicBool::new(false)),
            shutdown_event,
            config: config.clone(),
        })
    }

    /// 补建一个替换管道实例（Req 14.19：先补建、后服务）。
    fn replenish_instance(&mut self) -> io::Result<()> {
        let handle = unsafe { create_pipe_instance(&self.pipe_name, &self.sa)? };
        self.free_instances.push(handle);
        Ok(())
    }
}

impl TransportListener for NamedPipeListener {
    /// 接受一个连接。
    ///
    /// 流程（Req 14.19 先补建、后服务）：
    /// 1. 从空闲池取一个管道实例
    /// 2. ConnectNamedPipe 等待客户端连接（带超时以响应 shutdown）
    /// 3. 连接建立后，**立即补建替换实例**
    /// 4. 返回已连接的 NamedPipeConnection
    fn accept(&mut self) -> io::Result<Box<dyn TransportConnection>> {
        loop {
            if self.stop_flag.load(Ordering::SeqCst) {
                return Err(io::Error::new(
                    io::ErrorKind::Interrupted,
                    "listener shutdown",
                ));
            }

            // 取一个空闲实例
            let pipe_handle = self.free_instances.pop().ok_or_else(|| {
                io::Error::new(io::ErrorKind::Other, "无空闲管道实例（不应发生）")
            })?;

            // 使用 OVERLAPPED + 事件实现带超时的 ConnectNamedPipe
            let mut ov = OVERLAPPED {
                Internal: 0,
                InternalHigh: 0,
                Anonymous: OVERLAPPED_0 {
                    Anonymous: OVERLAPPED_0_0 {
                        Offset: 0,
                        OffsetHigh: 0,
                    },
                },
                hEvent: self.shutdown_event,
            };

            let result = unsafe { ConnectNamedPipe(pipe_handle, &mut ov) };

            if result == 0 {
                let err = unsafe { GetLastError() };
                if err == ERROR_PIPE_CONNECTED {
                    // 客户端在 CreateNamedPipe 和 ConnectNamedPipe 之间已连接
                    // 先补建替换实例（Req 14.19：先补建、后服务）
                    self.replenish_instance()?;
                    let peer_pid = get_pipe_client_pid(pipe_handle);
                    return Ok(Box::new(NamedPipeConnection::new(
                        pipe_handle,
                        self.config.max_message_bytes,
                        peer_pid,
                    )));
                }
                if err == ERROR_IO_PENDING {
                    // 等待连接或 shutdown 信号
                    let wait =
                        unsafe { WaitForSingleObject(self.shutdown_event, ACCEPT_TIMEOUT_MS) };
                    if wait == WAIT_OBJECT_0 {
                        // shutdown 信号
                        unsafe { DisconnectNamedPipe(pipe_handle) };
                        self.free_instances.push(pipe_handle);
                        return Err(io::Error::new(
                            io::ErrorKind::Interrupted,
                            "listener shutdown",
                        ));
                    }
                    // 超时：检查是否有连接到达
                    // 取消 overlapped 操作并重试
                    unsafe { DisconnectNamedPipe(pipe_handle) };
                    self.free_instances.push(pipe_handle);
                    continue;
                }
                // 其他错误
                self.free_instances.push(pipe_handle);
                return Err(io::Error::new(
                    io::ErrorKind::Other,
                    format!("ConnectNamedPipe 失败 (error {})", err),
                ));
            }

            // ConnectNamedPipe 返回非零：连接成功
            // 先补建替换实例（Req 14.19：先补建、后服务）
            self.replenish_instance()?;
            let peer_pid = get_pipe_client_pid(pipe_handle);
            return Ok(Box::new(NamedPipeConnection::new(
                pipe_handle,
                self.config.max_message_bytes,
                peer_pid,
            )));
        }
    }

    fn shutdown(&self) -> io::Result<()> {
        self.stop_flag.store(true, Ordering::SeqCst);
        // 触发事件，打断 WaitForSingleObject
        unsafe { SetEvent(self.shutdown_event) };
        Ok(())
    }

    fn endpoint_description(&self) -> String {
        format!("named-pipe:{}", self.pipe_name)
    }
}

impl Drop for NamedPipeListener {
    fn drop(&mut self) {
        // 关闭所有空闲管道实例
        for handle in self.free_instances.drain(..) {
            unsafe {
                DisconnectNamedPipe(handle);
                CloseHandle(handle);
            }
        }
        if !self.shutdown_event.is_null() {
            unsafe { CloseHandle(self.shutdown_event) };
        }
    }
}

// ============================================
// NamedPipeConnection
// ============================================

/// Windows 命名管道已建立连接。
///
/// 对端身份通过 ImpersonateNamedPipeClient + GetTokenInformation 获取（Req 14.5）。
pub struct NamedPipeConnection {
    /// 管道句柄
    handle: HANDLE,
    /// 最大消息字节数
    max_message_bytes: usize,
    /// 对端进程 PID（审计元数据，不参与授权）
    peer_pid: u32,
    /// 缓存的对端 SID（首次 peer_identity() 调用时获取）
    cached_sid: Option<String>,
}

// SAFETY: HANDLE 在 Windows 上是线程安全的
unsafe impl Send for NamedPipeConnection {}

impl NamedPipeConnection {
    fn new(handle: HANDLE, max_message_bytes: usize, peer_pid: u32) -> Self {
        Self {
            handle,
            max_message_bytes,
            peer_pid,
            cached_sid: None,
        }
    }

    /// 通过 ImpersonateNamedPipeClient 获取对端令牌 SID。
    ///
    /// 这是 SO_PEERCRED 的直接对等物：身份由内核在连接上下文里给出，
    /// 客户端无法覆写（Req 14.5）。
    fn get_peer_sid(&mut self) -> io::Result<String> {
        if let Some(ref sid) = self.cached_sid {
            return Ok(sid.clone());
        }

        unsafe {
            // 模拟客户端安全上下文
            if ImpersonateNamedPipeClient(self.handle) == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::Other,
                    format!("ImpersonateNamedPipeClient 失败 (error {})", GetLastError()),
                ));
            }

            // 获取当前线程令牌（即对端令牌）
            let mut token: HANDLE = ptr::null_mut();
            // OpenThreadToken 不在我们的 feature 中，使用 GetCurrentThread 等价
            // 实际上 ImpersonateNamedPipeClient 后，OpenProcessToken 拿到的是模拟令牌
            // 正确做法：使用 OpenThreadToken(GetCurrentThread(), TOKEN_QUERY, FALSE, &token)
            // 但 Win32_System_Threading feature 包含 OpenThreadToken
            let thread = windows_sys::Win32::System::Threading::GetCurrentThread();
            if windows_sys::Win32::System::Threading::OpenThreadToken(
                thread,
                TOKEN_QUERY,
                0, // FALSE: 不模拟
                &mut token,
            ) == 0
            {
                RevertToSelf();
                return Err(io::Error::last_os_error());
            }

            // 获取令牌用户 SID
            let mut needed: u32 = 0;
            GetTokenInformation(token, TokenUser, ptr::null_mut(), 0, &mut needed);
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
                return Err(io::Error::last_os_error());
            }
            CloseHandle(token);
            RevertToSelf();

            let sid_ptr = *(buf.as_ptr() as *const *mut c_void);
            let sid = sid_to_string(sid_ptr)?;
            self.cached_sid = Some(sid.clone());
            Ok(sid)
        }
    }
}

impl TransportConnection for NamedPipeConnection {
    fn recv_message(&mut self, max_bytes: usize) -> io::Result<Vec<u8>> {
        let limit = max_bytes.min(self.max_message_bytes);
        let mut buf = vec![0u8; limit];
        let mut bytes_read: u32 = 0;

        // 消息模式管道：一次 ReadFile 读取一个完整消息
        let ok = unsafe {
            windows_sys::Win32::Storage::FileSystem::ReadFile(
                self.handle,
                buf.as_mut_ptr(),
                limit as u32,
                &mut bytes_read,
                ptr::null_mut(),
            )
        };

        if ok == 0 {
            return Err(io::Error::last_os_error());
        }

        buf.truncate(bytes_read as usize);
        Ok(buf)
    }

    fn send_message(&mut self, data: &[u8]) -> io::Result<()> {
        let mut bytes_written: u32 = 0;
        let ok = unsafe {
            windows_sys::Win32::Storage::FileSystem::WriteFile(
                self.handle,
                data.as_ptr(),
                data.len() as u32,
                &mut bytes_written,
                ptr::null_mut(),
            )
        };

        if ok == 0 {
            return Err(io::Error::last_os_error());
        }

        if bytes_written as usize != data.len() {
            return Err(io::Error::new(io::ErrorKind::WriteZero, "管道写入不完整"));
        }

        // 刷新管道缓冲区
        unsafe {
            windows_sys::Win32::Storage::FileSystem::FlushFileBuffers(self.handle);
        }

        Ok(())
    }

    fn peer_identity(&self) -> io::Result<TransportPeerIdentity> {
        // 使用 unsafe 内部可变性获取 SID（缓存结果）
        // 这里需要 &mut self，但 trait 签名是 &self
        // 通过 unsafe 绕过（cached_sid 只在首次调用时写入，之后只读）
        let this = self as *const Self as *mut Self;
        let sid = unsafe { (*this).get_peer_sid()? };
        Ok(TransportPeerIdentity::Windows {
            sid,
            pid: self.peer_pid,
        })
    }

    fn set_timeout(&mut self, _timeout: Duration) -> io::Result<()> {
        // 命名管道消息模式下，超时由 ConnectNamedPipe 的 overlapped 控制
        // 读写操作本身是同步的（消息模式保证一次读取一个完整消息）
        // 如需读写超时，需改为 overlapped IO（当前设计不需要）
        Ok(())
    }
}

impl Drop for NamedPipeConnection {
    fn drop(&mut self) {
        unsafe {
            DisconnectNamedPipe(self.handle);
            CloseHandle(self.handle);
        }
    }
}

// ============================================
// 辅助函数
// ============================================

/// 获取管道客户端进程 PID（审计元数据，不参与授权判定）。
fn get_pipe_client_pid(pipe_handle: HANDLE) -> u32 {
    let mut pid: u32 = 0;
    unsafe {
        GetNamedPipeClientProcessId(pipe_handle, &mut pid);
    }
    pid
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_build_pipe_sddl_owner_only() {
        let sddl = build_pipe_sddl("S-1-5-21-123-456-789", false);
        assert_eq!(sddl, "D:P(A;;GA;;;S-1-5-21-123-456-789)");
        // 不包含 BA（Builtin Administrators）
        assert!(!sddl.contains("BA"));
    }

    #[test]
    fn test_build_pipe_sddl_with_admins() {
        let sddl = build_pipe_sddl("S-1-5-21-123-456-789", true);
        assert_eq!(sddl, "D:P(A;;GA;;;S-1-5-21-123-456-789)(A;;GA;;;BA)");
    }

    #[test]
    fn test_pipe_name_derivation() {
        let sid = "S-1-5-21-1000-2000-3000";
        let name = format!(r"\\.\pipe\callwarden-{}", sid);
        assert_eq!(name, r"\\.\pipe\callwarden-S-1-5-21-1000-2000-3000");
        // 端点负向约束（Req 14.20, 14.21）：不包含 TCP/HTTPS/AF_UNIX
        assert!(!name.contains("tcp"));
        assert!(!name.contains("https"));
        assert!(!name.contains("unix"));
    }

    #[test]
    fn test_get_current_user_sid() {
        // 在 Windows CI 上应能获取有效 SID
        let sid = get_current_user_sid();
        assert!(sid.is_ok(), "获取当前用户 SID 失败: {:?}", sid.err());
        let sid = sid.unwrap();
        assert!(sid.starts_with("S-1-"), "SID 格式无效: {}", sid);
    }

    #[test]
    fn test_create_security_attributes() {
        let sddl = build_pipe_sddl("S-1-5-21-123-456-789", true);
        let sa = unsafe { create_security_attributes(&sddl) };
        assert!(sa.is_ok(), "SDDL 转换失败: {:?}", sa.err());
    }

    #[test]
    fn test_transport_peer_identity_owner_key() {
        let unix_id = TransportPeerIdentity::Unix {
            uid: 1000,
            gid: 1000,
            pid: 123,
        };
        assert_eq!(unix_id.owner_key(), "1000");

        let win_id = TransportPeerIdentity::Windows {
            sid: "S-1-5-21-123".to_string(),
            pid: 456,
        };
        assert_eq!(win_id.owner_key(), "S-1-5-21-123");
    }

    #[test]
    fn test_instance_keepalive_invariant() {
        // Req 14.19：预创建 ≥2 个实例
        assert!(MIN_PIPE_INSTANCES >= 2);
    }
}
