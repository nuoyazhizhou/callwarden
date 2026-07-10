# Daemon IPC 安全规范

> 从 `enterprise-architecture-evolution.md` v10.2 抽取。只保留当前规范 + 状态机 + 不变量 + 故障注入测试，不保留 v1-v9 修订过程。
> 基线版本：v10.2（`ad2e308`）。

## 1. 通信架构

```
     agent (systemd --user, 用户身份)           daemon (User=callwarden, 可信)
    ┌──────────────────────────────┐        ┌─────────────────────────────────┐
    │ canonicalize_source(Rust FFI)│  UDS   │ recv_via_memfd / recv_framed     │
    │ send_framed_stream / memfd   │ ─────► │ sha256 重新校验                   │
    │ MSG_REFRESH(msg, bytes)      │        │ 可信 Rust parser 解析             │
    │                              │        │ CAS 发布 + manifest 提交          │
    └──────────────────────────────┘        └─────────────────────────────────┘
```

**安全边界**：
- agent 永不直接写 CAS（不持有 CAS DB 路径/权限）。
- agent 只提供 canonical bytes，由 daemon 重新计算 sha256 后由**可信 Rust parser**（daemon 进程内）解析生成 ParseFact。
- daemon 不信任 agent 提供的 `content_hash` / `git_tree_oid` / `git_blob_oid`。

## 2. 传输协议

### 2.1 UDS 路径与权限

```
路径: /var/run/callwarden.sock
权限: 0660
属主: callwarden:callwarden
```

- **UDS 为主通道**，本地 TCP 默认关闭。
- 容器无法挂 UDS 时开启本地 TCP，必须启用 **mTLS + per-container token**。
- 协议：JSON-RPC 2.0（与 MCP 兼容）。

### 2.2 长度分帧 UDS stream（小/中文件，默认）

```python
MAX_MSG_BYTES = 16 * 1024 * 1024  # 16 MB

def send_framed_stream(sock, msg_type, payload, canonical_bytes):
    """长度分帧 UDS stream（小/中文件）。超过 16 MB 走 memfd。"""
    payload_json = json.dumps(payload).encode("utf-8")
    total_len = 1 + 4 + 8 + len(payload_json) + len(canonical_bytes)
    if total_len > MAX_MSG_BYTES:
        return send_via_memfd(sock, msg_type, payload, canonical_bytes)
    header = struct.pack(">BIQ", msg_type, len(payload_json), len(canonical_bytes))
    sock.sendall(header + payload_json + canonical_bytes)
```

消息头格式：`| msg_type(1B) | payload_len(4B, big-endian) | canonical_len(8B, big-endian) |`

## 3. memfd 密封协议（大文件 > 16 MB）

### 3.1 Agent 侧：send_via_memfd

```python
def write_all(fd, data):
    """循环写直到全部写完——os.write 可能短写。"""
    view = memoryview(data)
    total = 0
    while total < len(view):
        n = os.write(fd, view[total:])
        if n == 0:
            raise OSError("memfd write returned 0 (disk full?)")
        total += n

def send_via_memfd(sock, msg_type, payload, canonical_bytes):
    """大文件用 memfd_create + seals + SCM_RIGHTS 传 FD。"""
    try:
        # MFD_CLOEXEC | MFD_ALLOW_SEALING 必须同时传——
        # 缺 MFD_ALLOW_SEALING 时 Linux 预置 F_SEAL_SEAL，后续 F_ADD_SEALS → EPERM
        fd = memfd_create("cw_canonical", MFD_CLOEXEC | MFD_ALLOW_SEALING)
    except AttributeError:
        raise NotImplementedError("memfd_create requires Linux 3.17+ / Python 3.8+")

    try:
        write_all(fd, canonical_bytes)  # 处理短写

        # 完整不可变集合（必须包含 F_SEAL_GROW，防 daemon lseek+write 扩展内容）
        linux_seal(fd, F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE | F_SEAL_SEAL)

        payload["canonical_len"] = len(canonical_bytes)
        send_msg_with_fd(sock, msg_type, payload, fd)  # SCM_RIGHTS 传 FD
    finally:
        os.close(fd)  # daemon 持有 FD 后 agent 关闭自己的引用
```

**Seal 不变量**：

| Seal | 防止 |
|------|------|
| `F_SEAL_SHRINK` | daemon `ftruncate` 缩小 → 漏掉末尾内容 |
| `F_SEAL_GROW` | daemon `lseek+write` 扩展 → 追加恶意内容 |
| `F_SEAL_WRITE` | daemon `write` 覆盖 → 篡改内容 |
| `F_SEAL_SEAL` | daemon 移除其他 seals → 重新解封 |

**mtime vs hash 顺序**：先 `mtime_ns`，再 `sha256`。不能反过来——先 hash 再取 mtime 可能拿到修改时间（文件在 hash 后、mtime 前被外部改动）。实现直接用 `canonicalize_source()` 内部顺序（Rust 实现已保证先 mtime 再 hash）。

### 3.2 Daemon 侧：recv_via_memfd（FD 所有权传递）

```python
MAX_MEMFD_BYTES = 256 * 1024 * 1024  # 256 MB，防 OOM

def recv_via_memfd(sock, expected_canonical_len, expected_content_hash, peer_uid):
    """daemon 接收端校验——不信任 agent 提供的 memfd。

    四重校验，任一失败则拒绝并关闭 FD：
    1. fstat().st_size == expected_canonical_len
    2. F_GET_SEALS 包含 F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE | F_SEAL_SEAL
    3. st_size <= MAX_MEMFD_BYTES
    4. sha256(memfd content) == expected_content_hash

    校验通过后返回 FD 所有权（不关闭），由调用方传给可信 Rust parser
    mmap 只读后解析，用完负责 close。避免复制 256MB 内容。
    """
    fd, msg = recv_msg_with_fd(sock)
    try:
        st = os.fstat(fd)

        # 1. 大小校验
        if st.st_size != expected_canonical_len:
            raise ProtocolError(f"memfd size mismatch")

        # 2. seal flags 校验
        actual_seals = linux_get_seals(fd)
        required_seals = (F_SEAL_SHRINK | F_SEAL_GROW | F_SEAL_WRITE | F_SEAL_SEAL)
        if (actual_seals & required_seals) != required_seals:
            raise ProtocolError(f"memfd missing seals")

        # 3. 最大尺寸校验
        if st.st_size > MAX_MEMFD_BYTES:
            raise ProtocolError(f"memfd exceeds MAX_MEMFD_BYTES")

        # 4. 内容 hash 校验
        os.lseek(fd, 0, SEEK_SET)  # 回到起点（返回值是文件位置，不用）
        actual_hash = sha256_streaming(fd, st.st_size)  # 流式 hash，不一次性载入
        if actual_hash != expected_content_hash:
            raise ProtocolError(f"memfd content hash mismatch")

        # 校验通过 → 回到起点，返回 FD 所有权
        os.lseek(fd, 0, SEEK_SET)
        return fd, msg  # FD 所有权转移给调用方
    except Exception:
        os.close(fd)  # 校验失败才关闭 FD
        raise
```

**FD 所有权传递的调用模式**：

```python
# daemon_handle_refresh 的 memfd 分支（大文件 > 16MB）
fd, msg = recv_via_memfd(sock, expected_canonical_len, expected_hash, peer_uid)
try:
    # 可信 Rust parser 直接从 FD mmap 只读解析，不复制内容到 Python 堆
    parse_result = parse_canonical_bytes_from_fd(fd, canonical_len, rel_path, lang)
    # ... hash 校验、CAS 发布、manifest 提交 ...
finally:
    os.close(fd)  # Rust parser 用完后 daemon 关闭 FD
```

## 4. Inflight Bytes 限制

| 常量 | 值 | 作用域 |
|------|----|--------|
| `MAX_CONN_QUEUED_BYTES` | 256 MB | 单连接队列 |
| `MAX_DAEMON_INFLIGHT_BYTES` | 2 GB | daemon 全局 |
| `MAX_UID_INFLIGHT_BYTES` | 512 MB | 单 UID 全局 |
| `MAX_MEMFD_BYTES` | 256 MB | 单条 memfd 消息 |

- 超过任一限制 → 暂停该连接的 `recv`。
- `sendall` 因 UDS socket buffer 满而阻塞时，daemon 消费队列后 TCP-like 流控自然生效。
- 资源预算最终应与 systemd `MemoryMax` 联动（实施任务 P2#2），而非固定常量。

## 5. Agent 不可信约束

| Agent 提供的值 | Daemon 处理 | 说明 |
|---------------|------------|------|
| `content_hash` | **不信任**：重新计算 `sha256(canonical_bytes)` | 以 daemon 计算为准 |
| `canonical_bytes` | **不信任**：daemon 重新算 hash 后传入可信 Rust parser | agent 无法伪造 ParseFact |
| `git_tree_oid` / `git_blob_oid` | **不信任**：不再由 agent 提供 | daemon 以 bare mirror 校验 |
| `registered_commit` | **间接校验**：必须在 daemon bare mirror 中且属于受信 ref 祖先 | 用于 clean/dirty 判定 |
| `session_epoch` | **必须匹配**：与 `workspace_active_session.active_session_epoch` 比对 | 不匹配则 ProtocolError |
| `monotonic_seq` | **单调性校验**：同 epoch 内 `seq > latest_seq` | stale seq 丢弃 |
| `observed_raw_hash` / `mtime_ns` | **不校验**（用于审计/调试，不用于安全判定） | 可选日志 |

## 6. 不变量

| # | 不变量 | 测试覆盖 |
|---|--------|---------|
| S1 | agent 永不直接写 CAS | 架构约束：CAS DB 路径 daemon-only |
| S2 | daemon 收到 bytes 后重新计算 sha256，不等于 agent 报告的 hash 时以 daemon 为准 | `daemon_handle_refresh` |
| S3 | memfd 完整四重 seal（`SHRINK|GROW|WRITE|SEAL`），缺一则 ProtocolError 拒绝 | `recv_via_memfd` seal 校验 |
| S4 | `write_all` 循环写处理短写，`memfd_create` 传 `MFD_CLOEXEC|MFD_ALLOW_SEALING` | `send_via_memfd` |
| S5 | `recv_via_memfd` 校验通过后返回 FD 所有权，调用方负责 close；校验失败则关闭 FD | FD 所有权传递 |
| S6 | memfd 内容校验用流式 `sha256_streaming`，不一次性载入 256MB 到内存 | `recv_via_memfd` |
| S7 | inflight bytes 超任一限制 → 暂停该连接 recv | `MAX_DAEMON_INFLIGHT_BYTES` 等 |
| S8 | mtime 在 hash 之前获取（先 mtime 再 sha256） | `canonicalize_source` 内部顺序 |
| S9 | 编辑路径下，agent 永不自己写原文件（除非通过 daemon 的安全编辑合约） | 架构约束 |
| S10 | 传输路径（UDS / memfd）对 agents 透明：SDK 封装 `send_msg`，自动选择分帧或 memfd | `MAX_MSG_BYTES` 阈值 |

## 7. 故障注入测试

| 场景 | 注入方式 | 期望结果 |
|------|---------|---------|
| memfd seal 缺失 | agent 不传 `F_SEAL_GROW` | daemon seal 校验失败 → ProtocolError + FD close |
| memfd 内容被篡改 | agent seal 后另一线程写 FD（应不可能，但模拟） | daemon sha256 校验失败 → ProtocolError + FD close |
| memfd 未 seal | agent 传 `memfd_create(..., 0)` | seal seal bit 预置 → 无 `MFD_ALLOW_SEALING` → agent 侧 `F_ADD_SEALS` EPERM |
| `write_all` 短写 | 模拟 `os.write` 只写 1 字节 | `write_all` 循环重试直到全部写完 |
| memfd 超限 | agent 传 300 MB memfd | `st_size > MAX_MEMFD_BYTES` → ProtocolError + FD close |
| agent 报告 hash 错误 | `content_hash` 填错误值 | daemon 重新计算后以实际 hash 为准，仅 warning |
| inflight bytes 超限 | agent 持续发大文件不消费 daemon 响应 | daemon 暂停 recv，UDS buffer 满后 agent sendall 阻塞 |
| UDP 被截断 | 模拟 partial read | daemon 按长度分帧头重读，不足则等待 |
| daemon 校验阶段 crash | kill daemon 在 seal 校验和 sha256 之间 | agent FD 在 `SCM_RIGHTS` 后已关闭自己引用，memfd 引用计数归零释放 |
