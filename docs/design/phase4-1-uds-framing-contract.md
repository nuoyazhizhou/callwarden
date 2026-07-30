# Phase 4-1 契约：UDS framing、SO_PEERCRED 与 RPC dispatch PyO3 暴露

> **范围**：将 Rust `daemon/protocol.rs`（UDS frame 编解码）+ `daemon/peercred.rs`（SO_PEERCRED）+
> `daemon/dispatch.rs`（RPC 路由表）的**纯计算部分**通过 PyO3 暴露给 Python，
> 建立协议层差分测试基线。
>
> **核心原则**：
> 1. **socket IO 不通过 PyO3 暴露**：`send_message`/`recv_message` 等需要操作 socket 文件描述符的函数不迁移，
>    仍在 Python `daemon_protocol.py` 中执行（PyO3 跨语言传递 socket 对象复杂且低效）
> 2. **暴露纯计算 API**：帧编解码（payload↔bytes）、响应解析、常量查询、错误码枚举
> 3. **wire-production 有限接入**：`parse_response` 和帧编码可用 Rust 短路，socket IO 保留 Python
> 4. **跨平台策略**：Unix-only 函数（peercred）在 Windows 上返回 `{"available": false}` 而非编译失败

> **不在范围**：
> - UDS socket 连接管理（`UnixStream::connect` / `accept`）—— 留给 daemon binary
> - SCM_RIGHTS FD 传递的 socket 操作 —— 留给 daemon binary
> - daemon binary 本身的 RPC dispatch 执行 —— 已在 `cw_daemon` binary 中实现
> - Python `daemon_server.py` 的完整迁移 —— Phase 4-4 E2E 验证协议兼容性

## 1. Python 真相源盘点

### 1.1 server/daemon_protocol.py

**入口**：UDS JSON-RPC 协议层

**常量**：
- `HEADER = struct.Struct("!I")` — 4 字节大端 header
- `DEFAULT_MAX_MESSAGE_BYTES = 8 * 1024 * 1024` — 8 MB
- `DEFAULT_MAX_FDS = 1` — SCM_RIGHTS 最大 FD 数

**异常类**：
- `ProtocolError(RuntimeError)` — IPC 帧或 JSON 请求不合法
- `DaemonRemoteError(RuntimeError)` — daemon 返回的结构化远端错误（含 `code` + `message`）

**纯计算函数（适合 PyO3 暴露）**：
- `parse_response(response: Dict) -> Any` — 解析 RPC 响应，远端错误转换为异常

**Socket IO 函数（不适合 PyO3 暴露，保留 Python）**：
- `send_message(sock, message, max_bytes)` — 发送帧
- `recv_message(sock, max_bytes)` — 接收帧
- `send_message_with_fds(sock, message, fds, max_bytes)` — 发送帧+FD
- `recv_message_with_fds(sock, max_bytes, max_fds)` — 接收帧+FD

### 1.2 server/daemon_server.py

**入口**：Python 侧 daemon server（legacy）

**核心函数**：
- `get_peer_credentials(conn: socket.socket) -> Dict[str, int]` — 获取对端凭证（uid/gid/pid）
- `api_register_workspace(owner_uid, ...)` — workspace 注册 API
- `api_list_workspaces(owner_uid)` — workspace 列表 API
- `api_get_workspace_status(workspace_instance_id)` — workspace 状态查询
- `api_update_workspace_status(workspace_instance_id, status)` — workspace 状态更新
- `EnterpriseDaemonService` 类 — Python daemon 主服务

**适合 PyO3 暴露**：
- `get_peer_credentials` 的纯计算部分（常量查询、错误码）

**不适合暴露**：
- `EnterpriseDaemonService` 类（涉及 socket accept 循环、后台任务、数据库连接）

### 1.3 server/daemon_client.py

**入口**：UDS RPC 客户端

**核心类**：
- `UnixDaemonRpcClient` — UDS RPC 客户端（`call` / `call_with_fd`）
- `DaemonClient` — 高层封装（singleton 模式，`rpc_call` / `is_daemon_ready`）

**适合 wire-production 接入**：
- `parse_response` 调用点可用 Rust 短路
- `derive_workspace_instance_id` 纯计算函数可用 Rust 短路

## 2. Rust API 契约

### 2.1 protocol 常量与帧编解码

```rust
/// 获取协议常量
#[pyfunction]
fn protocol_constants() -> PyResult<Bound<PyDict>> {
    // 返回 {"header_size": 4, "default_max_message_bytes": 8388608, "default_max_fds": 1}
}

/// 将 message dict 编码为 payload bytes（不含 header）
///
/// 对应 Python: json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
/// 行为：验证 message 是 dict，JSON 编码，返回 bytes
#[pyfunction]
fn protocol_encode_payload(message: &Bound<PyDict>) -> PyResult<PyBytes>;

/// 将 payload bytes 解码为 message dict
///
/// 对应 Python: json.loads(payload.decode("utf-8"))
/// 行为：UTF-8 解码，JSON 解析，验证是 dict
#[pyfunction]
fn protocol_decode_payload(payload: &[u8]) -> PyResult<Bound<PyDict>>;

/// 构造完整帧（header + payload）
///
/// 对应 Python: HEADER.pack(len(payload)) + payload
#[pyfunction]
fn protocol_build_frame(message: &Bound<PyDict>) -> PyResult<PyBytes>;

/// 解析帧 header（前 4 字节），返回 payload 长度
///
/// 对应 Python: HEADER.unpack(header_bytes)[0]
#[pyfunction]
fn protocol_parse_header(header: &[u8]) -> PyResult<u32>;

/// 验证消息大小是否合法
///
/// 对应 Python: size <= 0 or size > max_bytes → ProtocolError
#[pyfunction]
fn protocol_validate_message_size(size: u32, max_bytes: usize) -> PyResult<()>;
```

### 2.2 响应解析

```rust
/// 解析 RPC 响应，远端错误转换为异常
///
/// 对应 Python: parse_response(response: Dict) -> Any
/// 行为：
///   - response.get("ok") == True → 返回 response.get("result")
///   - 否则 → 抛 DaemonRemoteError(code, message)
#[pyfunction]
fn protocol_parse_response(response: &Bound<PyDict>) -> PyResult<PyObject>;

/// 构造成功响应
///
/// 对应 Python: {"ok": True, "result": result}
#[pyfunction]
fn protocol_make_ok_response(result: &Bound<PyAny>) -> PyResult<Bound<PyDict>>;

/// 构造失败响应
///
/// 对应 Python: {"ok": False, "error": {"code": code, "message": message}}
#[pyfunction]
fn protocol_make_error_response(code: &str, message: &str) -> PyResult<Bound<PyDict>>;
```

### 2.3 peercred 查询（Unix-only，跨平台降级）

```rust
/// 查询 peercred API 可用性
///
/// Windows 上返回 false，Unix 上返回 true
#[pyfunction]
fn peercred_is_available() -> bool;

/// 获取 peercred 常量信息
///
/// 返回 {"available": bool, "platform": "linux"|"macos"|"windows",
///       "supports_pid": bool, "method": "SO_PEERCRED"|"LOCAL_PEERCRED"|"unsupported"}
#[pyfunction]
fn peercred_info() -> PyResult<Bound<PyDict>>;
```

**注意**：`get_peer_cred(stream)` 需要 `UnixStream` 参数，不适合通过 PyO3 暴露（Python 的 socket 对象与 Rust 的 `UnixStream` 类型不兼容）。peercred 的实际查询逻辑保留在 Python `daemon_server.py:get_peer_credentials` 中。

### 2.4 dispatch 路由表查询

```rust
/// 获取 RPC 方法清单
///
/// 返回 [{"method": "ping", "params": [], "description": "心跳检测"},
///        {"method": "health", ...}, ...]
#[pyfunction]
fn dispatch_list_methods() -> PyResult<Vec<Bound<PyDict>>>;

/// 获取 RPC 错误码枚举
///
/// 返回 [{"code": "invalid_params", "description": "参数不合法"},
///        {"code": "method_not_found", ...}, ...]
#[pyfunction]
fn dispatch_list_error_codes() -> PyResult<Vec<Bound<PyDict>>>;

/// 查询方法是否为 admin-only
///
/// 对应 Python: method in ADMIN_ONLY_METHODS
#[pyfunction]
fn dispatch_is_admin_method(method: &str) -> bool;
```

## 3. 行为契约

### 3.1 帧编解码行为契约

| ID | 场景 | Python 行为 | Rust 预期行为 |
|---|---|---|---|
| F1 | 正常 dict 编码 | `json.dumps({"a": 1}, ensure_ascii=False, separators=(",", ":"))` → `{"a":1}` | 相同 bytes |
| F2 | 非 dict 编码 | `ProtocolError("消息必须是 JSON object")` | `ProtocolError::NotJsonObject` |
| F3 | 含中文 dict 编码 | `json.dumps({"名": "值"}, ensure_ascii=False)` → `{"名":"值"}` | 相同 bytes（UTF-8） |
| F4 | payload 解码 | `json.loads(payload.decode("utf-8"))` | 相同 dict |
| F5 | 非法 UTF-8 解码 | `UnicodeDecodeError` → `ProtocolError` | `ProtocolError::Utf8` |
| F6 | 非法 JSON 解码 | `json.JSONDecodeError` → `ProtocolError` | `ProtocolError::JsonDecode` |
| F7 | 非 dict JSON 解码 | `ProtocolError("消息必须是 JSON object")` | `ProtocolError::NotJsonObject` |
| F8 | 完整帧构造 | `HEADER.pack(len(payload)) + payload` | 相同 bytes |
| F9 | header 解析 | `HEADER.unpack(header)[0]` | 相同 u32 |
| F10 | header 长度不足 | Python 需 `_recv_exact` 补齐 | `ProtocolError::Other("header 长度不足")` |
| F11 | 消息大小验证 | `size <= 0 or size > max_bytes` → `ProtocolError` | `protocol_validate_message_size` 抛相同错误 |

### 3.2 响应解析行为契约

| ID | 场景 | Python 行为 | Rust 预期行为 |
|---|---|---|---|
| R1 | 成功响应 | `response["ok"] == True` → 返回 `response["result"]` | 相同 |
| R2 | 失败响应（有 error） | 抛 `DaemonRemoteError(code, message)` | 抛相同异常 |
| R3 | 失败响应（无 error） | `error = {}` → `DaemonRemoteError("daemon_error", "unknown daemon error")` | 相同 |
| R4 | 失败响应（无 code） | `code = "daemon_error"` | 相同 |
| R5 | 失败响应（无 message） | `message = "unknown daemon error"` | 相同 |
| R6 | ok 字段缺失 | `response.get("ok")` → None → 视为 False → 抛异常 | 相同（`unwrap_or(false)`） |
| R7 | 构造成功响应 | `{"ok": True, "result": result}` | 相同 dict |
| R8 | 构造失败响应 | `{"ok": False, "error": {"code": ..., "message": ...}}` | 相同 dict |

### 3.3 peercred 跨平台行为契约

| ID | 场景 | 预期行为 |
|---|---|---|
| P1 | Linux 平台 | `peercred_is_available() = True`, `platform = "linux"`, `supports_pid = True`, `method = "SO_PEERCRED"` |
| P2 | macOS 平台 | `peercred_is_available() = True`, `platform = "macos"`, `supports_pid = False`, `method = "LOCAL_PEERCRED"` |
| P3 | Windows 平台 | `peercred_is_available() = False`, `platform = "windows"`, `supports_pid = False`, `method = "unsupported"` |

### 3.4 dispatch 路由表行为契约

| ID | 场景 | 预期行为 |
|---|---|---|
| D1 | 方法清单非空 | `dispatch_list_methods()` 返回非空列表，含 ping/health/schema.version |
| D2 | 错误码清单 | `dispatch_list_error_codes()` 含 invalid_params/method_not_found/internal_error/permission_denied |
| D3 | admin-only 方法 | backup/restore/GC/mount/workspace delete → `True` |
| D4 | 非 admin 方法 | ping/health/schema.version → `False` |

## 4. 跨平台策略

### 4.1 Unix-only 模块处理

Rust `daemon/peercred.rs` 和 `daemon/server.rs` 使用 `#[cfg(unix)]` 条件编译。
PyO3 暴露层（新模块 `rust_ext/src/daemon_query.rs`）需要：

1. **常量查询跨平台**：`peercred_is_available()` / `peercred_info()` 在所有平台编译
   - Windows 上返回 `false` / `{"available": false, "platform": "windows", ...}`
2. **socket IO 函数不暴露**：`get_peer_cred(stream)` 不通过 PyO3 暴露
3. **dispatch 路由表跨平台**：方法清单和错误码在所有平台可查询（纯数据，无平台依赖）

### 4.2 差分测试跨平台

- Windows 上：F1-F11（帧编解码）+ R1-R8（响应解析）+ D1-D4（dispatch）可运行
- Windows 上：P3（peercred Windows）可运行，P1/P2（Linux/macOS）skip
- Linux 上：P1（peercred Linux）可运行
- macOS 上：P2（peercred macOS）可运行

## 5. wire-production 接入方案

### 5.1 接入点

| Python 调用点 | Rust 短路 | 备注 |
|---|---|---|
| `daemon_protocol.py:parse_response()` | `protocol_parse_response` | 纯计算，适合短路 |
| `daemon_protocol.py:send_message()` 的 payload 编码 | `protocol_encode_payload` | 仅编码部分短路，socket IO 保留 Python |
| `daemon_client.py:derive_workspace_instance_id()` | 暂不接入 | 纯计算但非热路径 |

### 5.2 不接入的部分

- `send_message` / `recv_message` 的 socket IO — 保留 Python
- `send_message_with_fds` / `recv_message_with_fds` — 保留 Python（SCM_RIGHTS）
- `daemon_server.py:EnterpriseDaemonService` — 留给 Phase 4-4 E2E 验证
- `daemon_client.py:UnixDaemonRpcClient.call()` — socket IO 保留 Python

### 5.3 rollback_config

- feature_name: `rust_protocol_parse_response`
- 默认 flag=0（Rust 短路）
- flag=1 时回退 Python `parse_response`

## 6. 事务与错误处理

### 6.1 错误处理策略

- Rust 端 fail-soft：PyO3 函数内部 try-except，失败返回 `None` 或抛 `ProtocolError`
- Python 端 fail-soft：调用 Rust 失败时降级到 Python 路径
- 差分测试：验证 Rust 和 Python 在相同输入下产生相同输出或相同异常

### 6.2 JSON 序列化一致性

**关键风险**：Python `json.dumps(ensure_ascii=False, separators=(",", ":"))` vs Rust `serde_json::to_vec`

- Python：`ensure_ascii=False` 保留 UTF-8，`separators=(",", ":")` 紧凑
- Rust：`serde_json::to_vec` 默认紧凑（无空格），UTF-8 原生支持
- **差分测试 F3 验证**：含中文的 dict 编码后 bytes 完全一致

**已知差异**：
- Python `json.dumps` 对 `NaN`/`Infinity` 默认输出 `NaN`/`Infinity`（非标准 JSON）
- Rust `serde_json` 默认拒绝 `NaN`/`Infinity`（序列化失败）
- **契约**：RPC 协议不应包含 NaN/Infinity，差分测试不覆盖此场景

## 7. 实现计划

1. **Rust 实现**（`rust_ext/src/daemon_query.rs`）：
   - `protocol_constants()` — 返回常量 dict
   - `protocol_encode_payload(message)` — dict → bytes
   - `protocol_decode_payload(payload)` — bytes → dict
   - `protocol_build_frame(message)` — dict → frame bytes
   - `protocol_parse_header(header)` — bytes → u32
   - `protocol_validate_message_size(size, max_bytes)` — 验证
   - `protocol_parse_response(response)` — dict → result/exception
   - `protocol_make_ok_response(result)` — result → dict
   - `protocol_make_error_response(code, message)` — code+message → dict
   - `peercred_is_available()` — bool
   - `peercred_info()` — dict
   - `dispatch_list_methods()` — Vec<dict>
   - `dispatch_list_error_codes()` — Vec<dict>
   - `dispatch_is_admin_method(method)` — bool

2. **PyO3 注册**（`rust_ext/src/lib.rs`）：
   - `mod daemon_query` + 14 个 `m.add_function`

3. **差分测试**（`tests/test_phase4_1_daemon_protocol_diff.py`）：
   - F1-F11 帧编解码差分
   - R1-R8 响应解析差分
   - P1-P3 peercred 跨平台（Windows skip P1/P2）
   - D1-D4 dispatch 路由表差分

4. **wire-production**（`server/daemon_protocol.py`）：
   - `parse_response` 接入 Rust 短路
   - rollback_config 登记

## 8. 验收标准

- `cargo check` 无 error
- `maturin build` + `pip install` 成功
- `pytest tests/test_phase4_1_daemon_protocol_diff.py` 全部通过（Windows 上 P1/P2 skip）
- `cw server --check-imports` 通过
- `cw rollback config` 显示 `rust_protocol_parse_response` 登记
- 端到端：daemon_client.py 调用路径不破坏

## 9. 风险与注意事项

- **JSON 序列化差异**：Python `json.dumps` 与 Rust `serde_json::to_vec` 在大多数场景行为一致，但 NaN/Infinity 处理不同。RPC 协议不应包含这些值。
- **socket IO 不迁移**：`send_message`/`recv_message` 的 socket IO 保留 Python，仅编解码部分 Rust 短路。性能提升有限（协议层非热路径），但建立了差分测试基线。
- **peercred 跨平台**：Windows 上 peercred 不可用，Python `daemon_server.py` 在 Windows 上已有降级逻辑（返回 uid=0/gid=0/pid=0）。Rust 暴露层与 Python 保持一致。
- **daemon binary 不受影响**：Rust daemon binary 内部使用 `daemon/protocol.rs` 和 `daemon/peercred.rs`，本子任务的 PyO3 暴露层不影响 daemon binary 行为。
- **不切换默认路径**：Python `daemon_protocol.py:parse_response` 仍主导。Rust API 仅作为可选短路（通过 rollback_config 控制）。
