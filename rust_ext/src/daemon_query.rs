//! Phase 4-1: UDS framing、SO_PEERCRED 与 RPC dispatch PyO3 暴露层
//!
//! 对应 Python `server/daemon_protocol.py`：
//! - `protocol_constants` — 协议常量查询
//! - `protocol_encode_payload` / `protocol_decode_payload` — 帧编解码
//! - `protocol_build_frame` / `protocol_parse_header` — 完整帧构造/解析
//! - `protocol_validate_message_size` — 消息大小验证
//! - `protocol_parse_response` — RPC 响应解析
//! - `protocol_make_ok_response` / `protocol_make_error_response` — 响应构造
//! - `peercred_is_available` / `peercred_info` — peercred 跨平台查询
//! - `dispatch_list_methods` / `dispatch_list_error_codes` / `dispatch_is_admin_method` — 路由表查询
//!
//! 设计原则（见 docs/design/phase4-1-uds-framing-contract.md §2）：
//! - socket IO 不通过 PyO3 暴露（send_message/recv_message 保留 Python）
//! - 仅暴露纯计算 API（帧编解码、响应解析、常量查询）
//! - peercred 跨平台降级（Windows 返回 available=false）
//! - JSON 序列化与 Python json.dumps(ensure_ascii=False, separators=(",", ":")) 一致
//!
//! 不在范围（由 Python 调用方或 daemon binary 处理）：
//! - UDS socket 连接管理（UnixStream::connect/accept）
//! - SCM_RIGHTS FD 传递的 socket 操作
//! - daemon binary 的 RPC dispatch 执行

use pyo3::exceptions::PyRuntimeError;
use pyo3::prelude::*;
use pyo3::types::{PyAny, PyBytes, PyDict};
use pyo3::Bound;
use serde_json::Value;

use crate::daemon::dispatch::ADMIN_ONLY_METHODS;
use crate::daemon::protocol::{
    DEFAULT_MAX_FDS, DEFAULT_MAX_MESSAGE_BYTES, HEADER_SIZE,
};

// ===========================================================================
// 协议常量查询
// ===========================================================================

/// 获取 UDS JSON-RPC 协议常量
///
/// 对应 Python `daemon_protocol.py` 模块级常量：
/// - HEADER = struct.Struct("!I") → header_size = 4
/// - DEFAULT_MAX_MESSAGE_BYTES = 8 * 1024 * 1024 = 8388608
/// - DEFAULT_MAX_FDS = 1
///
/// 返回 dict：
/// {
///     "header_size": 4,
///     "default_max_message_bytes": 8388608,
///     "default_max_fds": 1,
/// }
#[pyfunction]
pub fn protocol_constants(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("header_size", HEADER_SIZE)?;
    dict.set_item("default_max_message_bytes", DEFAULT_MAX_MESSAGE_BYTES)?;
    dict.set_item("default_max_fds", DEFAULT_MAX_FDS)?;
    Ok(dict)
}

// ===========================================================================
// 帧编解码
// ===========================================================================

/// 将 message dict 编码为 payload bytes（不含 header）
///
/// 对应 Python: `json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")`
///
/// 行为：
/// - 验证 message 是 dict（非 dict 抛 ProtocolError）
/// - JSON 编码（紧凑模式，UTF-8 原生支持）
/// - 返回 bytes
///
/// 实现说明：直接委托 Python json.dumps 编码，确保 key 顺序、ensure_ascii、
/// separators 行为与 Python 100% 一致（serde_json::Value::Object 内部 Map 会
/// 重排 key 顺序，与 Python dict 插入顺序不一致）。
#[pyfunction]
pub fn protocol_encode_payload<'py>(
    py: Python<'py>,
    message: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyBytes>> {
    // 直接调用 Python json.dumps 编码，确保与 Python 行为完全一致
    // （包括 key 顺序、ensure_ascii=False、separators=(",", ":")）
    let json_module = py.import("json")?;
    let kwargs = PyDict::new(py);
    kwargs.set_item("ensure_ascii", false)?;
    kwargs.set_item("separators", (",", ":"))?;
    let json_str: String = json_module
        .call_method("dumps", (message,), Some(&kwargs))?
        .extract()?;

    Ok(PyBytes::new(py, json_str.as_bytes()))
}

/// 将 payload bytes 解码为 message dict
///
/// 对应 Python: `json.loads(payload.decode("utf-8"))`
///
/// 行为：
/// - UTF-8 解码（失败抛 ProtocolError）
/// - JSON 解析（失败抛 ProtocolError）
/// - 验证是 dict（非 dict 抛 ProtocolError）
#[pyfunction]
pub fn protocol_decode_payload<'py>(
    py: Python<'py>,
    payload: &[u8],
) -> PyResult<Bound<'py, PyDict>> {
    // UTF-8 解码
    let s = std::str::from_utf8(payload)
        .map_err(|e| PyRuntimeError::new_err(format!("UTF-8 解码失败: {}", e)))?;

    // JSON 解析
    let json_value: Value = serde_json::from_str(s)
        .map_err(|e| PyRuntimeError::new_err(format!("JSON 解码失败: {}", e)))?;

    if !json_value.is_object() {
        return Err(PyRuntimeError::new_err("消息必须是 JSON object"));
    }

    json_value_to_pydict(py, &json_value)
}

/// 构造完整帧（header + payload）
///
/// 对应 Python: `HEADER.pack(len(payload)) + payload`
///
/// 行为：
/// - 编码 message 为 payload bytes
/// - 前置 4 字节大端 payload 长度
/// - 返回 frame bytes
#[pyfunction]
pub fn protocol_build_frame<'py>(
    py: Python<'py>,
    message: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyBytes>> {
    let payload = protocol_encode_payload(py, message)?;
    let payload_bytes = payload.as_bytes();

    let mut frame = Vec::with_capacity(HEADER_SIZE + payload_bytes.len());
    let len = payload_bytes.len() as u32;
    frame.extend_from_slice(&len.to_be_bytes());
    frame.extend_from_slice(payload_bytes);

    Ok(PyBytes::new(py, &frame))
}

/// 解析帧 header（前 4 字节），返回 payload 长度
///
/// 对应 Python: `HEADER.unpack(header_bytes)[0]`
///
/// 行为：
/// - 验证 header 长度 >= 4（不足抛 ProtocolError）
/// - 返回 u32 大端 payload 长度
#[pyfunction]
pub fn protocol_parse_header(header: &[u8]) -> PyResult<u32> {
    if header.len() < HEADER_SIZE {
        return Err(PyRuntimeError::new_err(format!(
            "header 长度不足: {} < {}",
            header.len(),
            HEADER_SIZE
        )));
    }
    let mut arr = [0u8; 4];
    arr.copy_from_slice(&header[..HEADER_SIZE]);
    Ok(u32::from_be_bytes(arr))
}

/// 验证消息大小是否合法
///
/// 对应 Python: `size <= 0 or size > max_bytes → ProtocolError`
///
/// 行为：
/// - size == 0 → 抛 ProtocolError("非法消息长度: 0")
/// - size > max_bytes → 抛 ProtocolError("消息超过限制: {actual} > {limit}")
/// - 合法 → 返回 None
#[pyfunction]
#[pyo3(signature = (size, max_bytes = DEFAULT_MAX_MESSAGE_BYTES))]
pub fn protocol_validate_message_size(size: u32, max_bytes: usize) -> PyResult<()> {
    if size == 0 {
        return Err(PyRuntimeError::new_err(format!("非法消息长度: {}", size)));
    }
    if size as usize > max_bytes {
        return Err(PyRuntimeError::new_err(format!(
            "消息超过限制: {} > {}",
            size, max_bytes
        )));
    }
    Ok(())
}

// ===========================================================================
// 响应解析
// ===========================================================================

/// 解析 RPC 响应，远端错误转换为异常
///
/// 对应 Python: `parse_response(response: Dict) -> Any`
///
/// 行为：
/// - response.get("ok") == True → 返回 response.get("result")
/// - 否则 → 抛 DaemonRemoteError(code, message)
///   - code 默认 "daemon_error"，message 默认 "unknown daemon error"
#[pyfunction]
pub fn protocol_parse_response<'py>(
    py: Python<'py>,
    response: &Bound<'py, PyDict>,
) -> PyResult<Bound<'py, PyAny>> {
    let json_value = pydict_to_json_value(response)?;

    let ok = json_value
        .get("ok")
        .and_then(|v| v.as_bool())
        .unwrap_or(false);

    if ok {
        // 成功：返回 result
        let result = json_value.get("result").cloned().unwrap_or(Value::Null);
        json_value_to_pyobject(py, &result)
    } else {
        // 失败：抛异常
        let error = json_value.get("error");
        let (code, message) = if let Some(err_obj) = error.and_then(|v| v.as_object()) {
            let code = err_obj
                .get("code")
                .and_then(|v| v.as_str())
                .unwrap_or("daemon_error")
                .to_string();
            let message = err_obj
                .get("message")
                .and_then(|v| v.as_str())
                .unwrap_or("unknown daemon error")
                .to_string();
            (code, message)
        } else {
            (
                "daemon_error".to_string(),
                "unknown daemon error".to_string(),
            )
        };
        Err(PyRuntimeError::new_err(format!("{}: {}", code, message)))
    }
}

/// 构造成功响应
///
/// 对应 Python: `{"ok": True, "result": result}`
#[pyfunction]
pub fn protocol_make_ok_response<'py>(
    py: Python<'py>,
    result: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("ok", true)?;
    dict.set_item("result", result)?;
    Ok(dict)
}

/// 构造失败响应
///
/// 对应 Python: `{"ok": False, "error": {"code": code, "message": message}}`
#[pyfunction]
pub fn protocol_make_error_response<'py>(
    py: Python<'py>,
    code: &str,
    message: &str,
) -> PyResult<Bound<'py, PyDict>> {
    let error_dict = PyDict::new(py);
    error_dict.set_item("code", code)?;
    error_dict.set_item("message", message)?;

    let dict = PyDict::new(py);
    dict.set_item("ok", false)?;
    dict.set_item("error", error_dict)?;
    Ok(dict)
}

// ===========================================================================
// peercred 跨平台查询
// ===========================================================================

/// 查询 peercred API 可用性
///
/// Windows 上返回 false，Unix 上返回 true
#[pyfunction]
pub fn peercred_is_available() -> bool {
    cfg!(unix)
}

/// 获取 peercred 平台信息
///
/// 返回 dict：
/// {
///     "available": bool,
///     "platform": "linux"|"macos"|"windows",
///     "supports_pid": bool,
///     "method": "SO_PEERCRED"|"LOCAL_PEERCRED"|"unsupported",
/// }
#[pyfunction]
pub fn peercred_info(py: Python<'_>) -> PyResult<Bound<'_, PyDict>> {
    let dict = PyDict::new(py);

    #[cfg(target_os = "linux")]
    {
        dict.set_item("available", true)?;
        dict.set_item("platform", "linux")?;
        dict.set_item("supports_pid", true)?;
        dict.set_item("method", "SO_PEERCRED")?;
    }

    #[cfg(target_os = "macos")]
    {
        dict.set_item("available", true)?;
        dict.set_item("platform", "macos")?;
        dict.set_item("supports_pid", false)?;
        dict.set_item("method", "LOCAL_PEERCRED")?;
    }

    #[cfg(not(any(target_os = "linux", target_os = "macos")))]
    {
        dict.set_item("available", false)?;
        dict.set_item("platform", "windows")?;
        dict.set_item("supports_pid", false)?;
        dict.set_item("method", "unsupported")?;
    }

    Ok(dict)
}

// ===========================================================================
// dispatch 路由表查询
// ===========================================================================

/// RPC 方法信息
struct MethodInfo {
    method: &'static str,
    description: &'static str,
    admin_only: bool,
}

/// 基础方法清单（对应 daemon/dispatch.rs dispatch 函数中路由的方法）
///
/// 注意：这里只列出基础方法（ping/health/schema.version）和高级方法名（workspace.*/snapshot.*/gc.*/query.*/mount.*/toolchain.*/build_context.*/resolved_edges.*/backup/restore）。
/// 高级方法的参数和返回值由具体 DaemonStateExt 实现决定，此处只做方法清单查询。
const METHODS: &[MethodInfo] = &[
    MethodInfo {
        method: "ping",
        description: "心跳检测",
        admin_only: false,
    },
    MethodInfo {
        method: "health",
        description: "健康检查（返回 daemon 状态、uptime、连接数）",
        admin_only: false,
    },
    MethodInfo {
        method: "schema.version",
        description: "查询当前 schema 版本号",
        admin_only: false,
    },
    MethodInfo {
        method: "workspace.register",
        description: "注册新 workspace",
        admin_only: false,
    },
    MethodInfo {
        method: "workspace.list",
        description: "列出 workspaces（按 UID 过滤）",
        admin_only: false,
    },
    MethodInfo {
        method: "workspace.status",
        description: "查询 workspace 状态",
        admin_only: false,
    },
    MethodInfo {
        method: "workspace.connect",
        description: "连接 workspace（分配 epoch）",
        admin_only: false,
    },
    MethodInfo {
        method: "workspace.file.refresh",
        description: "刷新单个文件",
        admin_only: false,
    },
    MethodInfo {
        method: "workspace.recover",
        description: "workspace 崩溃恢复",
        admin_only: false,
    },
    MethodInfo {
        method: "snapshot.publish",
        description: "发布 snapshot",
        admin_only: false,
    },
    MethodInfo {
        method: "snapshot.stats",
        description: "snapshot 统计",
        admin_only: false,
    },
    MethodInfo {
        method: "snapshot.list_workspaces",
        description: "列出 snapshot 中的 workspaces",
        admin_only: false,
    },
    MethodInfo {
        method: "gc.snapshots",
        description: "GC snapshots（admin-only）",
        admin_only: true,
    },
    MethodInfo {
        method: "gc.cas",
        description: "GC CAS（admin-only）",
        admin_only: true,
    },
    MethodInfo {
        method: "snapshot.evict",
        description: "驱逐 snapshot cache（admin-only）",
        admin_only: true,
    },
    MethodInfo {
        method: "query.stats",
        description: "查询统计",
        admin_only: false,
    },
    MethodInfo {
        method: "query.symbol",
        description: "符号查询",
        admin_only: false,
    },
    MethodInfo {
        method: "query.search",
        description: "符号搜索",
        admin_only: false,
    },
    MethodInfo {
        method: "query.callers",
        description: "调用者查询",
        admin_only: false,
    },
    MethodInfo {
        method: "query.callees",
        description: "被调用者查询",
        admin_only: false,
    },
    MethodInfo {
        method: "query.call_chain_down",
        description: "向下调用链",
        admin_only: false,
    },
    MethodInfo {
        method: "query.topological_order",
        description: "拓扑排序",
        admin_only: false,
    },
    MethodInfo {
        method: "query.detect_cycles",
        description: "循环检测",
        admin_only: false,
    },
    MethodInfo {
        method: "backup",
        description: "数据库备份（admin-only）",
        admin_only: true,
    },
    MethodInfo {
        method: "restore",
        description: "数据库还原（admin-only）",
        admin_only: true,
    },
    MethodInfo {
        method: "mount.register",
        description: "注册 mount 映射（admin-only）",
        admin_only: true,
    },
    MethodInfo {
        method: "mount.list",
        description: "列出 mount 映射（admin-only）",
        admin_only: true,
    },
    MethodInfo {
        method: "mount.delete",
        description: "删除 mount 映射（admin-only）",
        admin_only: true,
    },
    MethodInfo {
        method: "toolchain.register",
        description: "注册 toolchain（admin-only）",
        admin_only: true,
    },
    MethodInfo {
        method: "toolchain.list",
        description: "列出 toolchain",
        admin_only: false,
    },
    MethodInfo {
        method: "toolchain.get",
        description: "查询 toolchain",
        admin_only: false,
    },
    MethodInfo {
        method: "toolchain.delete",
        description: "删除 toolchain（admin-only）",
        admin_only: true,
    },
    MethodInfo {
        method: "toolchain.bind",
        description: "绑定 toolchain（admin-only）",
        admin_only: true,
    },
    MethodInfo {
        method: "toolchain.resolve",
        description: "解析 toolchain",
        admin_only: false,
    },
    MethodInfo {
        method: "build_context.register",
        description: "注册 build context（admin-only）",
        admin_only: true,
    },
    MethodInfo {
        method: "build_context.list",
        description: "列出 build contexts",
        admin_only: false,
    },
    MethodInfo {
        method: "build_context.set_active",
        description: "切换激活 build context（admin-only）",
        admin_only: true,
    },
    MethodInfo {
        method: "build_context.delete",
        description: "删除 build context（admin-only）",
        admin_only: true,
    },
    MethodInfo {
        method: "resolved_edges.store",
        description: "存储 resolved edge",
        admin_only: false,
    },
    MethodInfo {
        method: "resolved_edges.get",
        description: "查询 resolved edge",
        admin_only: false,
    },
    MethodInfo {
        method: "resolved_edges.count",
        description: "resolved edge 计数",
        admin_only: false,
    },
];

/// RPC 错误码信息
struct ErrorCodeInfo {
    code: &'static str,
    description: &'static str,
}

const ERROR_CODES: &[ErrorCodeInfo] = &[
    ErrorCodeInfo {
        code: "invalid_params",
        description: "参数不合法",
    },
    ErrorCodeInfo {
        code: "method_not_found",
        description: "未知方法",
    },
    ErrorCodeInfo {
        code: "internal_error",
        description: "内部错误",
    },
    ErrorCodeInfo {
        code: "permission_denied",
        description: "权限不足（非 admin 调用 admin-only 方法）",
    },
    ErrorCodeInfo {
        code: "workspace_not_found",
        description: "workspace 不存在",
    },
    ErrorCodeInfo {
        code: "workspace_forbidden",
        description: "workspace 跨 UID 越权",
    },
];

/// 获取 RPC 方法清单
///
/// 返回 list[dict]：
/// [{"method": "ping", "description": "心跳检测", "admin_only": false}, ...]
#[pyfunction]
pub fn dispatch_list_methods(py: Python<'_>) -> PyResult<Vec<Bound<'_, PyDict>>> {
    let mut result = Vec::with_capacity(METHODS.len());
    for m in METHODS {
        let d = PyDict::new(py);
        d.set_item("method", m.method)?;
        d.set_item("description", m.description)?;
        d.set_item("admin_only", m.admin_only)?;
        result.push(d);
    }
    Ok(result)
}

/// 获取 RPC 错误码枚举
///
/// 返回 list[dict]：
/// [{"code": "invalid_params", "description": "参数不合法"}, ...]
#[pyfunction]
pub fn dispatch_list_error_codes(py: Python<'_>) -> PyResult<Vec<Bound<'_, PyDict>>> {
    let mut result = Vec::with_capacity(ERROR_CODES.len());
    for e in ERROR_CODES {
        let d = PyDict::new(py);
        d.set_item("code", e.code)?;
        d.set_item("description", e.description)?;
        result.push(d);
    }
    Ok(result)
}

/// 查询方法是否为 admin-only
///
/// 对应 Python: method in ADMIN_ONLY_METHODS
#[pyfunction]
pub fn dispatch_is_admin_method(method: &str) -> bool {
    ADMIN_ONLY_METHODS.contains(&method)
}

// ===========================================================================
// Phase 4-2: UID/workspace ACL、路径安全与资源预算 PyO3 暴露层
// ===========================================================================
// 对应 Python `server/daemon_server.py` 和 `server/query_budget.py`：
// - validate_owned_path / check_path_within_workspace — 路径安全校验
// - is_admin_uid / current_daemon_uid_py — admin UID 判定
// - check_workspace_owner — workspace owner 校验
// - budget_create / budget_preset — 资源预算配置
// - budget_tracker_new / visit_node / truncate_results — 运行时跟踪
//
// 设计原则（见 docs/design/phase4-2-acl-path-budget-contract.md）：
// - 纯计算 API，不涉及 DB 连接管理（WorkspaceRegistry 保持 Python）
// - 路径校验复用 daemon::workspace::validate_owned_path
// - admin 判定不含 admin_uids 配置扩展（Python 调用方补充）
// - QueryBudget 用 dict 而非 pyclass（简化 PyO3 暴露）

use crate::daemon::dispatch::current_daemon_uid;
use crate::daemon::workspace::validate_owned_path as rust_validate_owned_path;

/// DaemonRpcError → PyRuntimeError 转换（"code: message" 格式）
fn daemon_err_to_py(err: &crate::daemon::dispatch::DaemonRpcError) -> PyErr {
    PyRuntimeError::new_err(format!("{}: {}", err.code, err.message))
}

/// 校验路径存在 + owner UID 匹配
///
/// 对应 Python `server/daemon_server.py:_validate_owned_path`
///
/// 返回：canonicalize 后的绝对路径
/// 抛出：PyRuntimeError("path_not_found: ...") / PyRuntimeError("path_forbidden: ...")
#[pyfunction]
pub fn validate_owned_path(path: &str, peer_uid: u32, require_file: bool) -> PyResult<String> {
    rust_validate_owned_path(path, peer_uid, require_file).map_err(|e| daemon_err_to_py(&e))
}

/// 校验路径逃逸：abs_path 是否落在 host_real_root 内
///
/// 对应 Python `server/daemon_server.py` workspace.file.refresh 路径逃逸检查
///
/// 抛出：PyRuntimeError("path_escape: ...")
#[pyfunction]
pub fn check_path_within_workspace(abs_path: &str, host_real_root: &str) -> PyResult<()> {
    let real_abs = std::fs::canonicalize(abs_path)
        .map_err(|_| PyRuntimeError::new_err(format!("path_not_found: {}", abs_path)))?;
    let real_host_root = std::fs::canonicalize(host_real_root)
        .map_err(|_| PyRuntimeError::new_err(format!("path_not_found: {}", host_real_root)))?;

    let real_abs_str = real_abs.to_string_lossy();
    let real_host_str = real_host_root.to_string_lossy();
    let sep = std::path::MAIN_SEPARATOR.to_string();

    if real_abs_str == real_host_str
        || real_abs_str.starts_with(&format!("{}{}", real_host_str, sep))
    {
        Ok(())
    } else {
        Err(PyRuntimeError::new_err(format!(
            "path_escape: {} 不在 workspace 根 {} 内",
            real_abs_str, real_host_str
        )))
    }
}

/// 判断 uid 是否为 admin（root 或 daemon 进程自己）
///
/// 对应 Python `server/daemon_server.py:_is_admin_peer`（前两层判定）
///
/// 注意：不含 DaemonConfig.admin_uids 配置扩展，Python 调用方需补充第三层判定
#[pyfunction]
pub fn is_admin_uid(uid: u32) -> bool {
    uid == 0 || uid == current_daemon_uid()
}

/// 获取当前 daemon 进程的 UID
///
/// 对应 Python `server/daemon_server.py:_current_uid`
///
/// Unix: libc::getuid()；Windows: 1000（与测试 current_uid() 对齐）
#[pyfunction]
pub fn current_daemon_uid_py() -> u32 {
    current_daemon_uid()
}

/// 校验 workspace owner
///
/// 对应 Python `server/daemon_server.py:_owned_workspace` 内部比较逻辑
///
/// 抛出：PyRuntimeError("workspace_forbidden: ...")
#[pyfunction]
pub fn check_workspace_owner(owner_uid: i64, peer_uid: u32) -> PyResult<()> {
    if owner_uid as u32 == peer_uid || peer_uid == 0 {
        Ok(())
    } else {
        Err(PyRuntimeError::new_err(format!(
            "workspace_forbidden: owner_uid={}，peer_uid={}",
            owner_uid, peer_uid
        )))
    }
}

/// 创建资源预算配置
///
/// 对应 Python `server/query_budget.py:QueryBudget` 5 字段
#[pyfunction]
#[pyo3(signature = (max_depth=5, max_nodes=1000, timeout_ms=5000, max_results=100, frontier_limit=500))]
pub fn budget_create<'py>(
    py: Python<'py>,
    max_depth: u32,
    max_nodes: usize,
    timeout_ms: u64,
    max_results: usize,
    frontier_limit: usize,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new(py);
    dict.set_item("max_depth", max_depth)?;
    dict.set_item("max_nodes", max_nodes)?;
    dict.set_item("timeout_ms", timeout_ms)?;
    dict.set_item("max_results", max_results)?;
    dict.set_item("frontier_limit", frontier_limit)?;
    Ok(dict)
}

/// 返回预设预算配置
///
/// 对应 Python `server/query_budget.py:default_budget` / `deep_budget` /
/// `shallow_budget` / `unlimited_budget`
#[pyfunction]
pub fn budget_preset<'py>(py: Python<'py>, name: &str) -> PyResult<Bound<'py, PyDict>> {
    let (max_depth, max_nodes, timeout_ms, max_results, frontier_limit) = match name {
        "default" => (5u32, 1000usize, 5000u64, 100usize, 500usize),
        "deep" => (10u32, 5000usize, 10000u64, 500usize, 500usize),
        "shallow" => (3u32, 100usize, 1000u64, 20usize, 500usize),
        "unlimited" => (100u32, 1_000_000usize, 300_000u64, 100_000usize, 10_000usize),
        _ => {
            return Err(PyRuntimeError::new_err(format!(
                "unknown_preset: {}",
                name
            )))
        }
    };
    budget_create(py, max_depth, max_nodes, timeout_ms, max_results, frontier_limit)
}

/// 创建运行时预算跟踪器
///
/// 对应 Python `server/query_budget.py:QueryBudget.start()`
///
/// 返回 dict 包含 budget 配置和运行时状态
#[pyfunction]
pub fn budget_tracker_new<'py>(
    py: Python<'py>,
    budget: &Bound<'_, PyDict>,
) -> PyResult<Bound<'py, PyDict>> {
    // 复制 budget 配置到 tracker
    let tracker = PyDict::new(py);
    // 复制 budget 字段
    for key in ["max_depth", "max_nodes", "timeout_ms", "max_results", "frontier_limit"] {
        if let Some(val) = budget.get_item(key)? {
            tracker.set_item(key, val)?;
        }
    }
    // 运行时状态
    let now: f64 = py.import("time")?.call_method("monotonic", (), None)?.extract()?;
    tracker.set_item("start_time", now)?;
    tracker.set_item("visited_count", 0u64)?;
    tracker.set_item("exceeded", false)?;
    tracker.set_item("exhausted_reason", py.None())?;
    Ok(tracker)
}

/// 访问节点并检查预算是否超限
///
/// 对应 Python `server/query_budget.py:QueryBudget.visit_node()`
///
/// 返回 true 表示可继续访问，false 表示预算超限
#[pyfunction]
pub fn budget_tracker_visit_node(py: Python<'_>, tracker: &Bound<'_, PyDict>) -> PyResult<bool> {
    // 检查是否已超限
    let exceeded: bool = tracker
        .get_item("exceeded")?
        .map(|v| v.extract().unwrap_or(false))
        .unwrap_or(false);
    if exceeded {
        return Ok(false);
    }

    // 自增 visited_count
    let visited: u64 = tracker
        .get_item("visited_count")?
        .map(|v| v.extract().unwrap_or(0u64))
        .unwrap_or(0);
    let new_visited = visited + 1;
    tracker.set_item("visited_count", new_visited)?;

    // 检查 max_nodes
    let max_nodes: u64 = tracker
        .get_item("max_nodes")?
        .map(|v| v.extract().unwrap_or(u64::MAX))
        .unwrap_or(u64::MAX);
    if new_visited > max_nodes {
        tracker.set_item("exceeded", true)?;
        tracker.set_item("exhausted_reason", "max_nodes")?;
        return Ok(false);
    }

    // 检查超时
    let timeout_ms: u64 = tracker
        .get_item("timeout_ms")?
        .map(|v| v.extract().unwrap_or(u64::MAX))
        .unwrap_or(u64::MAX);
    let start_time: f64 = tracker
        .get_item("start_time")?
        .map(|v| v.extract().unwrap_or(0.0))
        .unwrap_or(0.0);
    let now: f64 = py.import("time")?.call_method("monotonic", (), None)?.extract()?;
    let elapsed_ms = (now - start_time) * 1000.0;
    if elapsed_ms > timeout_ms as f64 {
        tracker.set_item("exceeded", true)?;
        tracker.set_item("exhausted_reason", "timeout")?;
        return Ok(false);
    }

    Ok(true)
}

/// 截断结果到 max_results 长度
///
/// 对应 Python `server/query_budget.py:QueryBudget.truncate_results()`
#[pyfunction]
pub fn budget_tracker_truncate_results<'py>(
    py: Python<'py>,
    tracker: &Bound<'py, PyDict>,
    results: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyAny>> {
    let max_results: usize = tracker
        .get_item("max_results")?
        .map(|v| v.extract().unwrap_or(usize::MAX))
        .unwrap_or(usize::MAX);

    // 调用 Python list() 转换为 list
    let builtins = py.import("builtins")?;
    let full_list = builtins.getattr("list")?.call1((results,))?;
    let len: usize = builtins
        .getattr("len")?
        .call1((&full_list,))?
        .extract()?;

    if len <= max_results {
        return Ok(full_list);
    }

    // 用 Python slice 对象做截断：list[:max_results]
    let py_slice = builtins.getattr("slice")?.call1((0, max_results))?;
    let truncated = full_list.get_item(py_slice)?;
    Ok(truncated)
}

// ===========================================================================
// Phase 4-3: health_check_all PyO3 暴露
// ===========================================================================
// Phase 4-3 implement 状态：metrics / health / audit / admin operations
// ---------------------------------------------------------------------------
// 已实现的 7 个 PyO3 API（lib.rs L1800-1815 已注册）：
// - health_check_all（L894）：聚合 4 项健康检查，返回 JSON 字符串
// - metrics_percentile（L925）：百分位计算（P50/P95/P99）
// - metrics_format_labels（L948）：Prometheus 标签格式化
// - audit_canonical_json（L985）：审计载荷规范化 JSON（字段排序 + 紧凑）
// - audit_compute_signature（L1002）：HMAC-SHA256 签名
// - backup_compute_file_sha256（L1039）：文件 SHA256（流式读取）
// - backup_compute_meta_checksum（L1071）：备份元数据聚合校验和
//
// admin operations（backup/restore/GC/mount/workspace delete）受 dispatch.rs
// ADMIN_ONLY_METHODS ACL 保护（Phase 4-2 已实现 fail-closed 检查）。
// 真实 handler 在 SnapshotDaemonState / WorkspaceDaemonState 中实现（R4-R6）。
// ===========================================================================
// 包装 crate::daemon::health::check_all_py，供 Python daemon_server.py 调用。
// 契约：docs/design/phase4-3-metrics-health-audit-contract.md §3.1
// - 返回 JSON 字符串（与 Python check_all() 格式一致）
// - 通过回退 start_time 模拟 uptime（避免 Instant 从 epoch 构造的限制）
// - Windows 内存检查返回 "unsupported"（status=healthy），Python 有 psutil fallback

/// 执行 4 项健康检查（db_registry / disk_space / memory_usage / uptime），返回 JSON 字符串
#[pyfunction]
pub fn health_check_all(
    registry_db_path: &str,
    data_root: &str,
    uptime_secs: f64,
    memory_max_bytes: u64,
) -> PyResult<String> {
    Ok(crate::daemon::health::check_all_py(
        registry_db_path,
        data_root,
        uptime_secs,
        memory_max_bytes,
    ))
}

// ===========================================================================
// Phase 4-3 P1: metrics 纯计算（percentile / format_labels）
// ===========================================================================
// 契约：docs/design/phase4-3-metrics-health-audit-contract.md §3.2
// - metrics_percentile: 线性插值法分位数
// - metrics_format_labels: Prometheus 标签格式化

/// 计算分位数（线性插值法）。
///
/// 对应 Python `server/metrics.py:_percentile()`。
/// 算法：
/// 1. 空列表返回 0.0
/// 2. 单元素返回该元素
/// 3. k = (n-1) * p, f = floor(k), c = k - f
/// 4. 若 f+1 < n，线性插值 values[f] + c * (values[f+1] - values[f])
/// 5. 否则返回 values[f]
#[pyfunction]
pub fn metrics_percentile(sorted_values: Vec<f64>, p: f64) -> f64 {
    if sorted_values.is_empty() {
        return 0.0;
    }
    let n = sorted_values.len();
    if n == 1 {
        return sorted_values[0];
    }
    let k = (n - 1) as f64 * p;
    let f = k.floor() as usize;
    let c = k - f as f64;
    if f + 1 < n {
        return sorted_values[f] + c * (sorted_values[f + 1] - sorted_values[f]);
    }
    sorted_values[f]
}

/// 将 label_key 字符串格式化为 Prometheus 标签格式。
///
/// 对应 Python `server/metrics.py:_format_labels()`。
/// "status=ok,method=query" -> `{status="ok",method="query"}`
/// "" -> ""
#[pyfunction]
pub fn metrics_format_labels(label_key: &str) -> String {
    if label_key.is_empty() {
        return String::new();
    }
    let parts: Vec<&str> = label_key.split(',').collect();
    let formatted: Vec<String> = parts
        .iter()
        .map(|part| {
            if let Some(eq_pos) = part.find('=') {
                let k = &part[..eq_pos];
                let v = &part[eq_pos + 1..];
                format!("{}=\"{}\"", k, v)
            } else {
                (*part).to_string()
            }
        })
        .collect();
    format!("{{{}}}", formatted.join(","))
}

// ===========================================================================
// Phase 4-3 P2: audit 纯计算（canonical_json / compute_signature）
// ===========================================================================
// 契约：docs/design/phase4-3-metrics-health-audit-contract.md §3.3
// - audit_canonical_json: 稳定序列化（key 排序 + 紧凑分隔符）
// - audit_compute_signature: HMAC-SHA256 / SHA-256

/// 稳定序列化 payload 为 JSON 字符串。
///
/// 对应 Python `db/db_audit_chain.py:canonical_json()`。
/// - sort_keys=True：递归排序所有 dict 的 key
/// - ensure_ascii=False：保留 Unicode 字符
/// - separators=(",", ":")：紧凑格式
///
/// 输入是已序列化的 JSON 字符串，此函数解析后重新序列化以保证稳定性。
/// 若输入无效 JSON，返回原始字符串。
#[pyfunction]
pub fn audit_canonical_json(payload_json: &str) -> PyResult<String> {
    // 解析 JSON 为 serde_json::Value
    let value: serde_json::Value = serde_json::from_str(payload_json)
        .map_err(|e| PyRuntimeError::new_err(format!("JSON 解析失败: {}", e)))?;
    // 递归排序 object key（canonical 形式：字段按字典序排序，与 Python json.dumps(sort_keys=True) 一致）
    // 注意：serde_json 启用 preserve_order feature 后默认保持插入顺序，需手动排序。
    let sorted = sort_json_keys(value);
    // 紧凑序列化（无空白，与 Python separators=(",", ":") 一致）
    let result = serde_json::to_string(&sorted)
        .map_err(|e| PyRuntimeError::new_err(format!("JSON 序列化失败: {}", e)))?;
    Ok(result)
}

/// 递归排序 JSON 对象的 key（canonical 形式）。
///
/// - Object：key 按字典序排序，value 递归处理
/// - Array：每个元素递归处理
/// - 其他类型：原样返回
fn sort_json_keys(value: serde_json::Value) -> serde_json::Value {
    use serde_json::{Map, Value};
    match value {
        Value::Object(map) => {
            let mut entries: Vec<(String, Value)> = map.into_iter().collect();
            entries.sort_by(|a, b| a.0.cmp(&b.0));
            let mut sorted_map = Map::new();
            for (k, v) in entries {
                sorted_map.insert(k, sort_json_keys(v));
            }
            Value::Object(sorted_map)
        }
        Value::Array(arr) => {
            Value::Array(arr.into_iter().map(sort_json_keys).collect())
        }
        other => other,
    }
}

/// 计算 record_signature。
///
/// 对应 Python `db/db_audit_chain.py:_compute_signature()`。
/// - 有 hmac_key：HMAC-SHA256(key, prev_signature + "|" + payload_hash) → hex
/// - 无 hmac_key：SHA-256(prev_signature + "|" + payload_hash) → hex
#[pyfunction]
pub fn audit_compute_signature(
    prev_signature: &str,
    payload_hash: &str,
    hmac_key: Option<&[u8]>,
) -> String {
    use sha2::{Digest, Sha256};

    let message = format!("{}|{}", prev_signature, payload_hash);
    let message_bytes = message.as_bytes();

    if let Some(key) = hmac_key {
        // HMAC-SHA256
        use hmac::{Hmac, Mac};
        type HmacSha256 = Hmac<Sha256>;
        let mut mac = HmacSha256::new_from_slice(key).expect("HMAC key 长度任意");
        mac.update(message_bytes);
        hex::encode(mac.finalize().into_bytes())
    } else {
        // SHA-256
        let mut hasher = Sha256::new();
        hasher.update(message_bytes);
        hex::encode(hasher.finalize())
    }
}

// ===========================================================================
// Phase 4-3 P3: backup 纯计算（compute_file_sha256 / compute_meta_checksum）
// ===========================================================================
// 契约：docs/design/phase4-3-metrics-health-audit-contract.md §3.4
// - backup_compute_file_sha256: 文件 SHA-256（流式读取）
// - backup_compute_meta_checksum: meta JSON 哈希（排除 checksum 字段）

/// 计算文件的 SHA-256 哈希（hex）。
///
/// 对应 Python `server/backup_restore.py:_compute_file_sha256()`。
/// 流式读取（64KB chunk），避免大文件内存爆炸。
#[pyfunction]
pub fn backup_compute_file_sha256(file_path: &str) -> PyResult<String> {
    use sha2::{Digest, Sha256};
    use std::fs::File;
    use std::io::{BufReader, Read};

    let file = File::open(file_path)
        .map_err(|e| PyRuntimeError::new_err(format!("打开文件失败: {}: {}", file_path, e)))?;
    let mut reader = BufReader::new(file);
    let mut hasher = Sha256::new();
    let mut buffer = [0u8; 65536]; // 64KB chunk（与 Python 一致）
    loop {
        let n = reader
            .read(&mut buffer)
            .map_err(|e| PyRuntimeError::new_err(format!("读取文件失败: {}", e)))?;
        if n == 0 {
            break;
        }
        hasher.update(&buffer[..n]);
    }
    Ok(hex::encode(hasher.finalize()))
}

/// 计算 meta JSON 的校验和（排除 checksum 字段）。
///
/// 对应 Python `server/backup_restore.py:_compute_meta_checksum()`。
/// 输入是 meta JSON 字符串，函数解析后排除 "checksum" 字段，重新序列化后 SHA-256。
///
/// **重要**：Python `json.dumps(sort_keys=True, ensure_ascii=False)` 默认使用
/// `, ` 和 `: ` 作为分隔符（带空格），而 `serde_json::to_string` 产生紧凑输出
/// （无空格），两者 byte-level 不一致。为保证校验和稳定可比对，必须调用
/// Python `json` 模块进行序列化（与真相源 byte-for-byte 一致）。
#[pyfunction]
pub fn backup_compute_meta_checksum<'py>(py: Python<'py>, meta_json: &str) -> PyResult<String> {
    use sha2::{Digest, Sha256};

    // 使用 Python json 模块解析 + 序列化（确保与 Python 真相源 byte-for-byte 一致）
    let json_module = py.import("json")?;

    // 解析 JSON 得到 Bound<PyDict>
    let value: Bound<'py, PyDict> = json_module
        .call_method("loads", (meta_json,), None)?
        .extract()?;

    // 排除 checksum 字段（若存在）
    if value.contains("checksum")? {
        value.del_item("checksum")?;
    }

    // 重新序列化（sort_keys=True, ensure_ascii=False，与 Python 真相源一致）
    let kwargs = PyDict::new(py);
    kwargs.set_item("sort_keys", true)?;
    kwargs.set_item("ensure_ascii", false)?;
    let content: String = json_module
        .call_method("dumps", (&value,), Some(&kwargs))?
        .extract()?;

    // SHA-256
    let mut hasher = Sha256::new();
    hasher.update(content.as_bytes());
    Ok(hex::encode(hasher.finalize()))
}

// ===========================================================================
// 工具函数：PyDict ↔ serde_json::Value 转换
// ===========================================================================

/// 将 PyDict 转换为 serde_json::Value
fn pydict_to_json_value(dict: &Bound<'_, PyDict>) -> PyResult<Value> {
    // 利用 Python 的 json.dumps 来序列化，再反序列化为 serde_json::Value
    // 这是最可靠的方式，确保与 Python 的 JSON 序列化行为完全一致
    let py = dict.py();
    let json_module = py.import("json")?;
    let json_str: String = json_module
        .call_method("dumps", (dict,), None)?
        .extract()?;

    let value: Value = serde_json::from_str(&json_str)
        .map_err(|e| PyRuntimeError::new_err(format!("PyDict JSON 反序列化失败: {}", e)))?;

    Ok(value)
}

/// 将 serde_json::Value 转换为 PyDict
fn json_value_to_pydict<'py>(
    py: Python<'py>,
    value: &Value,
) -> PyResult<Bound<'py, PyDict>> {
    let json_str = serde_json::to_string(value)
        .map_err(|e| PyRuntimeError::new_err(format!("JSON 序列化失败: {}", e)))?;

    let json_module = py.import("json")?;
    let py_obj = json_module.call_method("loads", (&json_str,), None)?;

    // 验证是 dict
    let dict: Bound<'py, PyDict> = py_obj
        .extract()
        .map_err(|_| PyRuntimeError::new_err("消息必须是 JSON object"))?;
    Ok(dict)
}

/// 将 serde_json::Value 转换为 PyAny
fn json_value_to_pyobject<'py>(
    py: Python<'py>,
    value: &Value,
) -> PyResult<Bound<'py, PyAny>> {
    let json_str = serde_json::to_string(value)
        .map_err(|e| PyRuntimeError::new_err(format!("JSON 序列化失败: {}", e)))?;

    let json_module = py.import("json")?;
    let py_obj = json_module.call_method("loads", (&json_str,), None)?;

    Ok(py_obj)
}
