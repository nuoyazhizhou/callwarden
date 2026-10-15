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
    CloseHandle, GetLastError, ERROR_IO_INCOMPLETE, ERROR_IO_PENDING, ERROR_PIPE_CONNECTED, FALSE,
    HANDLE, INVALID_HANDLE_VALUE, TRUE, WAIT_FAILED, WAIT_OBJECT_0, WAIT_TIMEOUT,
};
use windows_sys::Win32::Security::Authorization::{
    ConvertStringSecurityDescriptorToSecurityDescriptorW, SDDL_REVISION_1,
};
use windows_sys::Win32::Security::{
    GetTokenInformation, RevertToSelf, TokenUser, SECURITY_ATTRIBUTES, TOKEN_QUERY,
};
use windows_sys::Win32::Storage::FileSystem::PIPE_ACCESS_DUPLEX;
use windows_sys::Win32::System::Pipes::{
    ConnectNamedPipe, CreateNamedPipeW, DisconnectNamedPipe, GetNamedPipeClientProcessId,
    GetNamedPipeServerProcessId, ImpersonateNamedPipeClient, PIPE_READMODE_MESSAGE,
    PIPE_TYPE_MESSAGE, PIPE_UNLIMITED_INSTANCES, PIPE_WAIT,
};
use windows_sys::Win32::System::Threading::{
    CreateEventW, GetCurrentProcess, OpenProcessToken, ResetEvent, SetEvent, WaitForMultipleObjects,
};
use windows_sys::Win32::System::IO::GetOverlappedResult;
use windows_sys::Win32::System::IO::OVERLAPPED;

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
///
/// `first_instance`：为 true 时追加 `FILE_FLAG_FIRST_PIPE_INSTANCE`，若管道名已存在
/// 则以 ERROR_ACCESS_DENIED 失败 —— bind 用它做 fail-closed 的先占检测（TP-11G 2a）。
/// **只能加在首个实例上**：keep-alive「先补建后服务」依赖同名追加，补建实例必须传 false，
/// 否则补建路径 ERROR_ACCESS_DENIED、竞态防护（Req 14.19）失效。
unsafe fn create_pipe_instance(
    pipe_name: &str,
    sa: &SecurityAttributesGuard,
    first_instance: bool,
) -> io::Result<HANDLE> {
    let wide: Vec<u16> = pipe_name.encode_utf16().chain(std::iter::once(0)).collect();

    // SECURITY_ATTRIBUTES 结构体
    let sec_attr = SECURITY_ATTRIBUTES {
        nLength: std::mem::size_of::<SECURITY_ATTRIBUTES>() as u32,
        lpSecurityDescriptor: sa.sd,
        bInheritHandle: 0,
    };

    let first_pipe_instance_flag = if first_instance {
        windows_sys::Win32::Storage::FileSystem::FILE_FLAG_FIRST_PIPE_INSTANCE
    } else {
        0
    };
    let handle = CreateNamedPipeW(
        wide.as_ptr(),
        PIPE_ACCESS_DUPLEX
            | windows_sys::Win32::Storage::FileSystem::FILE_FLAG_OVERLAPPED
            | first_pipe_instance_flag,
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
// TP-11G 2a 层 3：arm 前创建者自检（设计文档 v3.2 §1.1）
// ============================================

/// arm 前创建者自检结果。
///
/// 实测依据 §A7（2/2，pre/post 一致）：`GetNamedPipeServerProcessId` 对
/// server/client 句柄均返回**管道最初创建者** PID，与句柄类型无关。
/// 合法单实例 daemon：creator == self；事故性第二实例：creator == 最初创建者 ≠ 自身。
#[derive(Debug)]
enum CreatorCheck {
    /// 创建者 == 自身：合法（本进程即管道首建者）
    Ok,
    /// 创建者 != 自身：检出共存实例（拒绝服务——Unavailable 优于错误归属）
    Mismatch { creator_pid: u32 },
    /// 查询不可得：告警放行（与 Mismatch 严格区分——「无法判断」不当「已确认异常」）
    Unknown { stage: &'static str, error: u32 },
}

/// 自检指定管道句柄的创建者是否为本进程。
///
/// `self_pid` 必须传调用方进程的真实 PID（`std::process::id()`）。
/// 放置层级：NamedPipeListener 服务层（bind 预创建 / replenish），
/// 低层 `PipeInstance::arm()` 保持中性（取证与测量路径需要能 arm 非创建者实例）。
fn check_pipe_creator_self(handle: HANDLE, self_pid: u32) -> CreatorCheck {
    let mut creator = 0u32;
    let ok = unsafe { GetNamedPipeServerProcessId(handle, &mut creator) };
    if ok == 0 {
        return CreatorCheck::Unknown {
            stage: "query_failed",
            error: unsafe { GetLastError() },
        };
    }
    if creator == 0 {
        // API 成功但创建者不可得：不应发生于已创建实例，防御性分支
        return CreatorCheck::Unknown {
            stage: "no_capability",
            error: 0,
        };
    }
    if creator == self_pid {
        CreatorCheck::Ok
    } else {
        CreatorCheck::Mismatch { creator_pid: creator }
    }
}

// ============================================
// NamedPipeListener
// ============================================

/// 单个命名管道实例及其挂起中的 ConnectNamedPipe 状态。
///
/// 实例存放在 `Box` 中：OVERLAPPED 由内核持指针，实例移动会悬垂，
/// Box 保证堆上地址稳定（Vec<Box<...>> 重分配只移动 Box 指针本身）。
struct PipeInstance {
    /// 管道实例句柄
    handle: HANDLE,
    /// 该实例专用的连接完成事件（手动重置；与 shutdown_event 分离，避免误判）
    event: HANDLE,
    /// ConnectNamedPipe 的 OVERLAPPED（hEvent = event，跨多次超时轮询保持存活）
    overlapped: OVERLAPPED,
    /// true = ConnectNamedPipe 已调用（挂起或已连接）；false = 尚未 arm
    armed: bool,
    /// true = 客户端已连接（ConnectNamedPipe 返回非零或 ERROR_PIPE_CONNECTED）
    connected: bool,
}

impl PipeInstance {
    fn new(
        pipe_name: &str,
        sa: &SecurityAttributesGuard,
        first_instance: bool,
    ) -> io::Result<Self> {
        let handle = unsafe { create_pipe_instance(pipe_name, sa, first_instance)? };
        let event = unsafe { CreateEventW(ptr::null(), TRUE, 0, ptr::null()) };
        if event.is_null() {
            unsafe { CloseHandle(handle) };
            return Err(io::Error::last_os_error());
        }
        let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
        overlapped.hEvent = event;
        Ok(Self {
            handle,
            event,
            overlapped,
            armed: false,
            connected: false,
        })
    }

    /// 调用 ConnectNamedPipe 使实例进入监听状态（可能立即检测到已连接客户端）。
    fn arm(&mut self) -> io::Result<()> {
        unsafe { ResetEvent(self.event) };
        self.overlapped.hEvent = self.event;
        let result = unsafe { ConnectNamedPipe(self.handle, &mut self.overlapped) };
        if result == 0 {
            let err = unsafe { GetLastError() };
            if err == ERROR_PIPE_CONNECTED {
                // 客户端在 arm 前已连接（CreateFileW 无需等待 ConnectNamedPipe）
                self.armed = true;
                self.connected = true;
                return Ok(());
            }
            if err == ERROR_IO_PENDING {
                self.armed = true;
                self.connected = false;
                return Ok(());
            }
            return Err(io::Error::new(
                io::ErrorKind::Other,
                format!("ConnectNamedPipe 失败 (error {})", err),
            ));
        }
        // 返回非零：客户端已连接
        self.armed = true;
        self.connected = true;
        Ok(())
    }

    /// 关闭实例句柄与完成事件。
    fn close(&mut self) {
        unsafe {
            DisconnectNamedPipe(self.handle);
            CloseHandle(self.handle);
            CloseHandle(self.event);
        }
    }
}

/// Windows 命名管道监听器。
///
/// Req 14.19（实例保活，硬要求）：
/// 预创建 ≥2 个管道实例，且**每个实例都挂起 ConnectNamedPipe（监听中）**；
/// 服务每个已接受连接之前补建替换实例并立即 arm（先补建、后服务），
/// 消除两次 accept 之间的 pipe-busy / 端点缺失竞态窗口。
///
/// ## 为什么必须全实例挂起
/// 客户端 CreateFileW 不需要服务端已调用 ConnectNamedPipe 即可连接（连接先建立，
/// 服务端随后 ConnectNamedPipe 返回 ERROR_PIPE_CONNECTED）。若存在"空闲但未监听"
/// 的实例，客户端连上后无人服务会永久挂起。因此所有实例必须同时处于监听状态，
/// accept 用 WaitForMultipleObjects 等待任意一个完成事件。
pub struct NamedPipeListener {
    /// 管道名（`\\.\pipe\callwarden-<user-sid>`）
    pipe_name: String,
    /// SDDL 安全描述符字符串（保留用于诊断 / endpoint_description 扩展）
    #[allow(dead_code)]
    sddl: String,
    /// SECURITY_ATTRIBUTES guard（生命周期与 listener 绑定）
    sa: SecurityAttributesGuard,
    /// 全部监听中的管道实例（始终保持 ≥ MIN_PIPE_INSTANCES 个；Box 保证 OVERLAPPED 地址稳定）
    instances: Vec<Box<PipeInstance>>,
    /// 停止标志
    stop_flag: Arc<AtomicBool>,
    /// 用于打断 WaitForMultipleObjects 阻塞的事件（等待数组第 0 个）
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
    /// 2. 派生管道名 `\\.\pipe\callwarden-<user-sid>` 与 SDDL（仅授权 owner SID）
    /// 3. 预创建 ≥2 个管道实例并全部 arm（挂起 ConnectNamedPipe，Req 14.19）
    pub fn bind(config: &TransportConfig) -> io::Result<Self> {
        let owner_sid = get_current_user_sid()?;
        let pipe_name = format!(r"\\.\pipe\callwarden-{}", owner_sid);
        let sddl = build_pipe_sddl(&owner_sid, false);
        let sa = unsafe { create_security_attributes(&sddl)? };

        // 创建 shutdown 事件（手动重置，初始无信号）
        let shutdown_event = unsafe { CreateEventW(ptr::null(), TRUE, 0, ptr::null()) };
        if shutdown_event.is_null() {
            return Err(io::Error::last_os_error());
        }

        // 预创建 ≥2 个管道实例并全部 arm（Req 14.19：全实例监听）
        // 首实例带 FIRST_PIPE_INSTANCE（2a：bind 时发现先占即 fail-closed）；
        // 其余为同名追加，**不能**带 flag（keep-alive 补建依赖，见 create_pipe_instance 文档）。
        let mut instances = Vec::<Box<PipeInstance>>::with_capacity(MIN_PIPE_INSTANCES);
        for i in 0..MIN_PIPE_INSTANCES {
            let mut instance = Box::new(PipeInstance::new(&pipe_name, &sa, i == 0)?);
            // TP-11G 2a 层 3：arm 前创建者自检（防御纵深：flag 回归时的最后防线）。
            // 首实例由 FIRST_PIPE_INSTANCE 保证 creator==self，此处自检恒过；其余实例
            // 理论上也应 creator==self（本进程即首建者），失配即拒绝服务。
            match check_pipe_creator_self(instance.handle, std::process::id()) {
                CreatorCheck::Ok => {}
                CreatorCheck::Mismatch { creator_pid } => {
                    instance.close();
                    for inst in instances.iter_mut() {
                        inst.close();
                    }
                    if !shutdown_event.is_null() {
                        unsafe { CloseHandle(shutdown_event) };
                    }
                    return Err(io::Error::new(
                        io::ErrorKind::Other,
                        format!(
                            "[TP-11G 2a 层3] 管道创建者自检失败（拒绝服务：Unavailable 优于错误归属）: \
                             管道 {pipe_name} 创建者 pid={creator_pid} != 本进程 pid={}，检出共存实例；\
                             先占场景应由 FIRST_PIPE_INSTANCE 在首实例创建处拦截",
                            std::process::id()
                        ),
                    ));
                }
                CreatorCheck::Unknown { stage, error } => {
                    eprintln!(
                        "[cw_daemon] [WARN] [TP-11G 2a 层3] bind creator 自检不可得 \
                         (stage={stage}, err={error})，告警放行：本次无法判断共存，\
                         由 client 侧交叉校验兜底"
                    );
                }
            }
            if let Err(e) = instance.arm() {
                instance.close();
                for inst in instances.iter_mut() {
                    inst.close();
                }
                if !shutdown_event.is_null() {
                    unsafe { CloseHandle(shutdown_event) };
                }
                return Err(e);
            }
            instances.push(instance);
        }

        Ok(Self {
            pipe_name,
            sddl,
            sa,
            instances,
            stop_flag: Arc::new(AtomicBool::new(false)),
            shutdown_event,
            config: config.clone(),
        })
    }

    /// 补建一个替换管道实例并立即 arm（Req 14.19：先补建、后服务）。
    ///
    /// 必须在返回已接受连接**之前**调用，保证任意时刻都有 ≥ MIN_PIPE_INSTANCES
    /// 个实例处于监听状态。
    fn replenish_instance(&mut self) -> io::Result<()> {
        // 同名追加（keep-alive 补建），不可带 FIRST_PIPE_INSTANCE
        let mut instance = Box::new(PipeInstance::new(&self.pipe_name, &self.sa, false)?);
        // TP-11G 2a 层 3：补建实例 arm 前创建者自检（与 bind 同一防线）。
        // Mismatch = 本进程已不是管道创建者（异常共存态）→ 拒绝补建，
        // 保留现有监听实例继续服务（降级不扩面），双 PID 响亮诊断。
        match check_pipe_creator_self(instance.handle, std::process::id()) {
            CreatorCheck::Ok => {}
            CreatorCheck::Mismatch { creator_pid } => {
                instance.close();
                return Err(io::Error::new(
                    io::ErrorKind::Other,
                    format!(
                        "[TP-11G 2a 层3] replenish 创建者自检失败（拒绝补建：Unavailable 优于错误归属）: \
                         管道 {} 创建者 pid={creator_pid} != 本进程 pid={}，检出共存实例",
                        self.pipe_name,
                        std::process::id()
                    ),
                ));
            }
            CreatorCheck::Unknown { stage, error } => {
                eprintln!(
                    "[cw_daemon] [WARN] [TP-11G 2a 层3] replenish creator 自检不可得 \
                     (stage={stage}, err={error})，告警放行：本次无法判断共存，\
                     由 client 侧交叉校验兜底"
                );
            }
        }
        if let Err(e) = instance.arm() {
            instance.close();
            return Err(e);
        }
        self.instances.push(instance);
        Ok(())
    }

    /// 接受一个已连接/已就绪的实例（Req 14.19：先补建替换实例，再返回连接）。
    fn take_connected(&mut self, index: usize) -> io::Result<Box<dyn TransportConnection>> {
        let mut instance = self.instances.swap_remove(index);
        // 先补建替换实例并 arm（先补建、后服务——顺序颠倒即重新引入竞态）
        if let Err(e) = self.replenish_instance() {
            instance.close();
            return Err(e);
        }
        let pipe_handle = instance.handle;
        // 连接已建立，完成事件不再需要
        unsafe { CloseHandle(instance.event) };
        let peer_pid = get_pipe_client_pid(pipe_handle);
        Ok(Box::new(NamedPipeConnection::new(
            pipe_handle,
            self.config.max_message_bytes,
            peer_pid,
        )))
    }
}

impl TransportListener for NamedPipeListener {
    /// 接受一个连接。
    ///
    /// 流程：
    /// 1. 所有实例均挂起 ConnectNamedPipe（监听中），不存在"可连但无人服务"的实例
    /// 2. WaitForMultipleObjects 等待 shutdown 事件或任意实例的完成事件
    /// 3. 完成后先补建替换实例并 arm（Req 14.19），再返回已连接的 NamedPipeConnection
    fn accept(&mut self) -> io::Result<Box<dyn TransportConnection>> {
        loop {
            if self.stop_flag.load(Ordering::SeqCst) {
                return Err(io::Error::new(
                    io::ErrorKind::Interrupted,
                    "listener shutdown",
                ));
            }

            // 若有实例在 arm 时已检测到连接（ERROR_PIPE_CONNECTED / 非零返回），直接接受
            if let Some(index) = self
                .instances
                .iter()
                .position(|instance| instance.connected)
            {
                return self.take_connected(index);
            }

            // 等待 shutdown 或任意实例的连接完成事件
            // handles[0] = shutdown_event；handles[1..] = 各实例 event
            let mut handles: Vec<HANDLE> = Vec::with_capacity(self.instances.len() + 1);
            handles.push(self.shutdown_event);
            for instance in self.instances.iter() {
                handles.push(instance.event);
            }
            let wait = unsafe {
                WaitForMultipleObjects(
                    handles.len() as u32,
                    handles.as_ptr(),
                    FALSE,
                    ACCEPT_TIMEOUT_MS,
                )
            };
            if wait == WAIT_TIMEOUT {
                continue;
            }
            if wait == WAIT_FAILED {
                return Err(io::Error::new(
                    io::ErrorKind::Other,
                    format!("WaitForMultipleObjects 失败 (error {})", unsafe {
                        GetLastError()
                    }),
                ));
            }
            let signal_index = (wait - WAIT_OBJECT_0) as usize;
            if signal_index == 0 {
                // shutdown 信号
                return Err(io::Error::new(
                    io::ErrorKind::Interrupted,
                    "listener shutdown",
                ));
            }
            let instance_index = signal_index - 1;
            let Some(instance) = self.instances.get_mut(instance_index) else {
                return Err(io::Error::new(
                    io::ErrorKind::Other,
                    "实例索引越界（不应发生）",
                ));
            };

            // 对应实例事件已触发：确认连接是否真正建立
            let mut transferred: u32 = 0;
            let completed = unsafe {
                GetOverlappedResult(
                    instance.handle,
                    &instance.overlapped,
                    &mut transferred,
                    FALSE,
                )
            };
            if completed != 0 {
                return self.take_connected(instance_index);
            }
            let err = unsafe { GetLastError() };
            if err == ERROR_IO_INCOMPLETE {
                // 事件误触发（理论上不应发生）：忽略并继续等待
                continue;
            }
            // 连接异常终止（如客户端在完成前断开）：丢弃该实例并补建替换
            let mut instance = self.instances.swap_remove(instance_index);
            instance.close();
            if let Err(e) = self.replenish_instance() {
                return Err(e);
            }
            continue;
        }
    }

    fn shutdown(&self) -> io::Result<()> {
        self.stop_flag.store(true, Ordering::SeqCst);
        // 触发事件，打断 WaitForMultipleObjects
        unsafe { SetEvent(self.shutdown_event) };
        Ok(())
    }

    fn endpoint_description(&self) -> String {
        format!("named-pipe:{}", self.pipe_name)
    }
}

impl Drop for NamedPipeListener {
    fn drop(&mut self) {
        // 关闭所有管道实例与完成事件
        for instance in self.instances.iter_mut() {
            instance.close();
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
    /// I/O 超时时间
    timeout: Option<Duration>,
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
            timeout: None,
        }
    }

    /// 通过 ImpersonateNamedPipeClient 获取对端令牌 SID。
    fn get_peer_sid(&mut self) -> io::Result<String> {
        if let Some(ref sid) = self.cached_sid {
            return Ok(sid.clone());
        }

        unsafe {
            if ImpersonateNamedPipeClient(self.handle) == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::Other,
                    format!("ImpersonateNamedPipeClient 失败 (error {})", GetLastError()),
                ));
            }

            let mut token: HANDLE = ptr::null_mut();
            let thread = windows_sys::Win32::System::Threading::GetCurrentThread();
            if windows_sys::Win32::System::Threading::OpenThreadToken(
                thread,
                TOKEN_QUERY,
                0,
                &mut token,
            ) == 0
            {
                RevertToSelf();
                return Err(io::Error::last_os_error());
            }

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
        let mut frame: Vec<u8> = Vec::new();
        loop {
            let mut buf = vec![0u8; limit];
            let mut n: u32 = 0;

            let event = unsafe { CreateEventW(ptr::null(), TRUE, 0, ptr::null()) };
            if event.is_null() {
                return Err(io::Error::last_os_error());
            }
            let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
            overlapped.hEvent = event;

            let ok = unsafe {
                windows_sys::Win32::Storage::FileSystem::ReadFile(
                    self.handle,
                    buf.as_mut_ptr() as *mut _,
                    limit as u32,
                    &mut n,
                    &mut overlapped,
                )
            };
            if ok == 0 {
                let err = unsafe { GetLastError() };
                if err == ERROR_IO_PENDING {
                    let ms = self
                        .timeout
                        .map(|t| t.as_millis() as u32)
                        .unwrap_or(windows_sys::Win32::System::Threading::INFINITE);
                    let wait = unsafe {
                        windows_sys::Win32::System::Threading::WaitForSingleObject(event, ms)
                    };
                    if wait == WAIT_TIMEOUT {
                        unsafe {
                            windows_sys::Win32::System::IO::CancelIoEx(
                                self.handle,
                                &overlapped,
                            );
                            CloseHandle(event);
                        }
                        return Err(io::Error::new(
                            io::ErrorKind::TimedOut,
                            "Windows named pipe ReadFile 超时",
                        ));
                    }
                    if unsafe {
                        GetOverlappedResult(self.handle, &overlapped, &mut n, TRUE)
                    } == 0
                    {
                        unsafe { CloseHandle(event) };
                        return Err(io::Error::last_os_error());
                    }
                } else if err == 234 {
                    unsafe { CloseHandle(event) };
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!("消息过大: > {} 字节", limit),
                    ));
                } else {
                    unsafe { CloseHandle(event) };
                    return Err(io::Error::last_os_error());
                }
            }
            unsafe { CloseHandle(event) };

            if n == 0 {
                return Err(io::Error::new(
                    io::ErrorKind::UnexpectedEof,
                    "管道连接关闭（读取消息时）",
                ));
            }
            frame.extend_from_slice(&buf[..n as usize]);
            if frame.len() >= 4 {
                let len = u32::from_be_bytes([frame[0], frame[1], frame[2], frame[3]]) as usize;
                if len > limit {
                    return Err(io::Error::new(
                        io::ErrorKind::InvalidData,
                        format!("消息过大: {} > {}", len, limit),
                    ));
                }
                if frame.len() >= 4 + len {
                    return Ok(frame[4..4 + len].to_vec());
                }
            }
        }
    }

    fn send_message(&mut self, data: &[u8]) -> io::Result<()> {
        let len = (data.len() as u32).to_be_bytes();
        let mut frame = Vec::with_capacity(4 + data.len());
        frame.extend_from_slice(&len);
        frame.extend_from_slice(data);

        let mut bytes_written: u32 = 0;
        while (bytes_written as usize) < frame.len() {
            let mut n: u32 = 0;
            let event = unsafe { CreateEventW(ptr::null(), TRUE, 0, ptr::null()) };
            if event.is_null() {
                return Err(io::Error::last_os_error());
            }
            let mut overlapped: OVERLAPPED = unsafe { std::mem::zeroed() };
            overlapped.hEvent = event;

            let to_write = (frame.len() - bytes_written as usize) as u32;
            let ok = unsafe {
                windows_sys::Win32::Storage::FileSystem::WriteFile(
                    self.handle,
                    frame.as_ptr().add(bytes_written as usize) as *const _,
                    to_write,
                    &mut n,
                    &mut overlapped,
                )
            };
            if ok == 0 {
                let err = unsafe { GetLastError() };
                if err == ERROR_IO_PENDING {
                    let ms = self
                        .timeout
                        .map(|t| t.as_millis() as u32)
                        .unwrap_or(windows_sys::Win32::System::Threading::INFINITE);
                    let wait = unsafe {
                        windows_sys::Win32::System::Threading::WaitForSingleObject(event, ms)
                    };
                    if wait == WAIT_TIMEOUT {
                        unsafe {
                            windows_sys::Win32::System::IO::CancelIoEx(
                                self.handle,
                                &overlapped,
                            );
                            CloseHandle(event);
                        }
                        return Err(io::Error::new(
                            io::ErrorKind::TimedOut,
                            "Windows named pipe WriteFile 超时",
                        ));
                    }
                    if unsafe {
                        GetOverlappedResult(self.handle, &overlapped, &mut n, TRUE)
                    } == 0
                    {
                        unsafe { CloseHandle(event) };
                        return Err(io::Error::last_os_error());
                    }
                } else {
                    unsafe { CloseHandle(event) };
                    return Err(io::Error::last_os_error());
                }
            }
            unsafe { CloseHandle(event) };

            if n == 0 {
                return Err(io::Error::new(io::ErrorKind::WriteZero, "管道写入零字节"));
            }
            bytes_written += n;
        }

        unsafe {
            windows_sys::Win32::Storage::FileSystem::FlushFileBuffers(self.handle);
        }

        Ok(())
    }

    fn peer_identity(&self) -> io::Result<TransportPeerIdentity> {
        let this = self as *const Self as *mut Self;
        let sid = unsafe { (*this).get_peer_sid()? };
        Ok(TransportPeerIdentity::Windows {
            sid,
            pid: self.peer_pid,
        })
    }

    fn set_timeout(&mut self, timeout: Duration) -> io::Result<()> {
        self.timeout = Some(timeout);
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

    // ============ 用例 9（TP-11G 2a-flag）：FIRST_PIPE_INSTANCE 语义锁定 ============
    // 隔离名，零生产接触。锁两组语义：
    //   A. 名字已存在（无 flag 先建）→ 带 flag 的 create 必须失败且错误码为
    //      ERROR_ACCESS_DENIED(5) —— 这是 bind fail-closed 先占检测的机制基础；
    //   B. 带 flag 首建成功，且随后的无 flag 同名追加成功 —— 证明 flag 只约束
    //      携带它的那次 create，不破坏 keep-alive「先补建后服务」的补建路径。

    #[test]
    fn test_first_pipe_instance_flag_semantics() {
        let owner_sid = get_current_user_sid().expect("get_current_user_sid");
        let sddl = build_pipe_sddl(&owner_sid, false);
        let sa = unsafe { create_security_attributes(&sddl) }.expect("sddl");
        let ts = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_millis();

        // Case A：无 flag 先建 → 带 flag 再建 → ERROR_ACCESS_DENIED(5)
        let name_a = format!(
            r"\\.\pipe\callwarden-isotest-fpi-a-{}-{ts}",
            std::process::id()
        );
        let h1 = unsafe { create_pipe_instance(&name_a, &sa, false) }
            .expect("A1: 无 flag 首建应成功");
        let err_a = unsafe { create_pipe_instance(&name_a, &sa, true) }
            .expect_err("A2: 名字已存在时带 flag 的 create 应失败");
        assert!(
            err_a.to_string().contains("error 5)"),
            "A2 应为 ERROR_ACCESS_DENIED(5)，实际: {err_a}"
        );
        unsafe { CloseHandle(h1) };

        // Case B：带 flag 首建成功 → 无 flag 追加成功（keep-alive 兼容）
        let name_b = format!(
            r"\\.\pipe\callwarden-isotest-fpi-b-{}-{ts}",
            std::process::id()
        );
        let h2 = unsafe { create_pipe_instance(&name_b, &sa, true) }
            .expect("B1: 带 flag 首建应成功（全新名字）");
        let h3 = unsafe { create_pipe_instance(&name_b, &sa, false) }
            .expect("B2: 无 flag 同名追加应成功（keep-alive 依赖）");
        unsafe { CloseHandle(h3) };
        unsafe { CloseHandle(h2) };
    }

    // ============ 回归用例 4a（TP-11G 2a 层 3 行为断言）：非创建者实例自检必须判 Mismatch ============
    // 拓扑：子进程先建实例（成为管道创建者并持有 3s）；本进程对同名追加实例跑自检
    // → creator(=子进程) ≠ self → Mismatch，即「拒绝 arm/服务」的判据实测锁定。
    // 合法侧（creator==self）由 Case B 型全新名字实例覆盖。

    #[test]
    fn test_creator_check_refuses_non_creator() {
        // 子进程角色：创建实例并持有（成为管道最初创建者）
        if let Ok(role) = std::env::var("CW_FORENSIC_ROLE") {
            if role == "holder" {
                let name = std::env::var("CW_FORENSIC_NAME").unwrap_or_default();
                if name.is_empty() {
                    return;
                }
                let sddl =
                    build_pipe_sddl(&get_current_user_sid().expect("sid"), false);
                let sa = unsafe { create_security_attributes(&sddl) }.expect("sddl");
                let inst = PipeInstance::new(&name, &sa, false).expect("holder create");
                // 持有 3s，给测试进程留出自检窗口
                std::thread::sleep(std::time::Duration::from_millis(3000));
                drop(inst);
                return;
            }
            return; // 其他角色不适用本用例
        }

        // ---- 编排（父进程 = 追加实例的非创建者）----
        let exe = std::env::current_exe().unwrap();
        let name = format!(
            r"\\.\pipe\callwarden-isotest-cc-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_millis()
        );
        let mut child = std::process::Command::new(&exe)
            .args([
                "--exact",
                "daemon::transport_windows::tests::test_creator_check_refuses_non_creator",
                "--nocapture",
                "--test-threads=1",
            ])
            .env("CW_FORENSIC_ROLE", "holder")
            .env("CW_FORENSIC_NAME", &name)
            .stdout(std::process::Stdio::null())
            .stderr(std::process::Stdio::null())
            .spawn()
            .expect("spawn holder");
        std::thread::sleep(std::time::Duration::from_millis(1200)); // 等 holder 建好

        // 本进程对同名追加实例（creator = holder 子进程）
        let sddl = build_pipe_sddl(&get_current_user_sid().expect("sid"), false);
        let sa = unsafe { create_security_attributes(&sddl) }.expect("sddl");
        let inst = PipeInstance::new(&name, &sa, false).expect("append create");
        match check_pipe_creator_self(inst.handle, std::process::id()) {
            CreatorCheck::Mismatch { creator_pid } => {
                assert_ne!(
                    creator_pid,
                    std::process::id(),
                    "creator 应为 holder 子进程而非本进程"
                );
                println!(
                    "D5-4a(行为): Mismatch 判定成立 creator_pid={creator_pid} != self={} \
                     → 拒绝服务判据实测锁定",
                    std::process::id()
                );
            }
            other => panic!("应判 Mismatch，实际 {other:?}"),
        }
        drop(inst);
        let _ = child.wait();

        // 合法侧对照：全新名字自建实例 → creator==self → Ok
        let name_ok = format!(
            r"\\.\pipe\callwarden-isotest-cc-ok-{}-{}",
            std::process::id(),
            std::time::SystemTime::now()
                .duration_since(std::time::UNIX_EPOCH)
                .unwrap()
                .as_millis()
        );
        let inst_ok = PipeInstance::new(&name_ok, &sa, false).expect("ok create");
        match check_pipe_creator_self(inst_ok.handle, std::process::id()) {
            CreatorCheck::Ok => println!("D5-4a(行为): 合法侧 Ok 判定成立（creator==self）"),
            other => panic!("合法侧应判 Ok，实际 {other:?}"),
        }
        drop(inst_ok);
    }
}
