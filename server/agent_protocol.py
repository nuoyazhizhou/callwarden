"""G9: Agent 端 UDS 握手协议与 refresh 消息封装。

对应设计：
- `docs/design/enterprise-architecture-evolution.md` §v8 "systemd --user agent 回传 canonical bytes"
- `docs/design/watcher-generation-state-machine.md` §4.1（session epoch CAS）

职责：
1. `user_agent_connect()`：与 daemon 握手，协商 session_epoch
   - 发送 `workspace.connect` RPC（带 workspace_instance_id + agent_session_id）
   - 接收 daemon 分配的 session_epoch
   - 更新 AgentSession 状态（set_epoch）
2. `build_refresh_message()`：组装 refresh RPC 的 params dict
   - 包含 rel_path / agent_session_id / session_epoch / monotonic_seq
   - 调用 AgentSession.next_seq() 获取单调递增 seq
   - 可选注入 VCS 版本信息（vcs_kind / head_sha）
3. `send_refresh_to_daemon()`：通过 daemon_rpc_client 发送 refresh 请求
   - 小文件（≤1MB）：直接 call()（params + canonical_bytes hex）
   - 中文件（1MB..32MB）：multipart 裸 payload（Q10 raw-gz-body，
     零编码税/零压缩 CPU；同机环回实测 0.5ms/MB，HTTP 传输主力路径）
   - 大文件（>32MB）：multipart gzip payload（换 body 空间与 daemon 内存）
   - 旧 daemon（无 multipart 路由）自动降级 gz_b64 内联（Q9 路径，零回归）
   - 超大文件（>16MB）且 client 支持 FD 时 call_with_fd()（Linux memfd）
   - 失败时返回错误信息（不抛异常，由 watcher 决定重试策略）
4. `probe_vcs_info()`：client 侧 VCS 版本探测（2026-09-17 新增）
   - 读 git rev-parse HEAD（mtime 缓存，避免每文件变更都 spawn）
   - 设计原则：VCS 版本由 client 读，daemon 不再自己 spawn git

设计要点：
- agent 永不直接写 CAS，所有写入都通过 daemon RPC
- canonical_bytes 通过 Rust canonicalize_source_py 生成（BOM/换行/编码归一化）
- agent 重启后，session_epoch 必须重新协商（旧 session 已被 daemon 撤销）
- VCS 信息是增强字段：探测失败不应阻断刷新（降级为 vcs_kind=none）
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import threading
import time
import uuid
from typing import Any, Dict, Optional, Tuple

from callwarden.server.agent_session import AgentSession
from callwarden.server.daemon_client import MultipartUnsupportedError
from callwarden.server.daemon_protocol import DaemonRemoteError

logger = logging.getLogger(__name__)


def _resolve_rpc_error_code(exc: Exception, default: str = "refresh_failed") -> str:
    """从 daemon RPC 异常中提取语义化 code，供 agent 决定是否 auto-reconnect。

    DaemonRemoteError.code 是 daemon 侧 DaemonRpcError.code 的透传（见
    daemon_protocol.parse_response），其中：
      - session_not_active：daemon 侧无 active session（应重连）
      - stale_session：incoming epoch/session 与 active 不匹配（应重连）
      - stale_manifest_commit：CAS 第二阶段失败（不应重连，重试可能可行）
      - refresh_failed：其他通用失败
    """
    # DaemonRemoteError 在 daemon_protocol 中定义
    try:
        from callwarden.server.daemon_protocol import DaemonRemoteError
        if isinstance(exc, DaemonRemoteError) and getattr(exc, "code", None):
            return str(exc.code)
    except ImportError:
        pass
    return default


# ============================================
# 消息类型常量（与 daemon 侧约定）
# ============================================

# Agent → daemon 的消息类型
MSG_CONNECT = 1  # workspace.connect RPC
MSG_REFRESH = 2  # workspace.file.refresh RPC
MSG_PING = 0     # ping/health check

# Daemon → agent 的响应码
RESP_OK = "ok"
RESP_STALE_SEQ_DROPPED = "stale_seq_dropped"
RESP_PROTOCOL_ERROR = "protocol_error"


class AgentProtocolError(RuntimeError):
    """Agent 协议错误（握手失败 / RPC 错误 / session 失效）。"""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


# ============================================
# VCS 版本探测（client 侧，2026-09-17 新增）
# ============================================

# 缓存：(workspace_root) -> (git_head_mtime, vcs_info)
# 目的：避免每次文件变更都 spawn git 进程（rev-parse 耗时 5-50ms）。
# 仅当 .git/HEAD 的 mtime 变化时才重新探测。
_VCS_CACHE: Dict[str, Tuple[float, Dict[str, Any]]] = {}
_VCS_CACHE_LOCK = threading.Lock()


def probe_vcs_info(workspace_root: str) -> Dict[str, Any]:
    """探测 workspace 的 VCS 版本信息（client 侧，跨平台）。

    用户提案（2026-09-17）：VCS 版本由 client 读，随 refresh/connect 报文
    上报给 daemon，daemon 不再自己 spawn git。

    当前支持 git（rev-parse HEAD）。非 git 仓库或探测失败时返回
    ``{"vcs_kind": "none"}``，不抛异常（VCS 信息是增强字段，不应阻断刷新）。

    缓存策略：以 ``.git/HEAD`` 的 mtime 为失效依据——内容变更才重新 spawn。
    mtime 检查是一次 stat，远低于 git 进程开销。

    Args:
        workspace_root: workspace 根目录绝对路径

    Returns:
        dict，可能为：
        - ``{"vcs_kind": "git", "head_sha": "<40-hex>", "dirty": bool}``
        - ``{"vcs_kind": "none"}``（非 git 仓库 / 探测失败）
    """
    if not workspace_root or not os.path.isdir(workspace_root):
        return {"vcs_kind": "none"}

    head_file = os.path.join(workspace_root, ".git", "HEAD")
    if not os.path.exists(head_file):
        # 支持 worktree / submodule 的 .git 文件形式（内容为 gitdir: 指针）
        git_entry = os.path.join(workspace_root, ".git")
        if not os.path.exists(git_entry):
            return {"vcs_kind": "none"}
        head_file = _resolve_git_head(workspace_root, git_entry)
        if head_file is None:
            # .git 存在但无法定位 HEAD（如 submodule 未检出）
            with _VCS_CACHE_LOCK:
                _VCS_CACHE[workspace_root] = (0.0, {"vcs_kind": "git", "head_sha": ""})
            return {"vcs_kind": "git", "head_sha": ""}

    try:
        head_mtime = os.stat(head_file).st_mtime
    except OSError:
        return {"vcs_kind": "none"}

    # 缓存命中：mtime 未变则直接返回
    with _VCS_CACHE_LOCK:
        cached = _VCS_CACHE.get(workspace_root)
        if cached is not None and cached[0] == head_mtime:
            return cached[1]

    # 缓存未命中：spawn git rev-parse HEAD
    info = _probe_git_head(workspace_root)
    with _VCS_CACHE_LOCK:
        _VCS_CACHE[workspace_root] = (head_mtime, info)
    return info


def _resolve_git_head(workspace_root: str, git_entry: str) -> Optional[str]:
    """解析 .git 为文件（gitdir 指针）时的真实 HEAD 路径。

    worktree 场景 ``.git`` 内容形如 ``gitdir: /path/to/.git/worktrees/x``，
    HEAD 位于该目录下。
    """
    try:
        with open(git_entry, "r", encoding="utf-8") as f:
            first_line = f.readline().strip()
        if first_line.startswith("gitdir:"):
            gitdir = first_line[len("gitdir:"):].strip()
            if not os.path.isabs(gitdir):
                gitdir = os.path.join(workspace_root, gitdir)
            head = os.path.join(gitdir, "HEAD")
            return head if os.path.exists(head) else None
    except (OSError, UnicodeDecodeError):
        pass
    return None


def _probe_git_head(workspace_root: str) -> Dict[str, Any]:
    """spawn git rev-parse HEAD（单次调用，含基本错误处理）。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=workspace_root,
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            sha = result.stdout.strip()
            if len(sha) == 40 and all(c in "0123456789abcdef" for c in sha):
                return {"vcs_kind": "git", "head_sha": sha}
            # 空仓库（无提交）：rev-parse 失败或返回空
            return {"vcs_kind": "git", "head_sha": ""}
        # 空仓库等正常情况：returncode != 0 但不是错误
        return {"vcs_kind": "git", "head_sha": ""}
    except (OSError, subprocess.SubprocessError):
        # git 不存在或超时：视为无 VCS
        return {"vcs_kind": "none"}


def clear_vcs_cache(workspace_root: Optional[str] = None) -> None:
    """清除 VCS 缓存（测试 / 强制重新探测时使用）。"""
    with _VCS_CACHE_LOCK:
        if workspace_root is None:
            _VCS_CACHE.clear()
        else:
            _VCS_CACHE.pop(workspace_root, None)


# ============================================
# 握手协议：workspace.connect
# ============================================


def user_agent_connect(
    daemon_rpc_client,
    workspace_instance_id: str,
    agent_session: AgentSession,
) -> int:
    """G9: agent 启动时与 daemon 握手，协商 session_epoch。

    流程：
    1. agent 发送 `workspace.connect` RPC，带 workspace_instance_id + agent_session_id
    2. daemon 撤销旧 active session，分配 new_epoch = MAX(all) + 1
    3. daemon 返回 `{"session_epoch": new_epoch}`
    4. agent 调用 AgentSession.set_epoch() 保存

    规范：watcher-generation-state-machine.md §4.1（session epoch CAS）

    Args:
        daemon_rpc_client: DaemonClient 单例（或 UnixDaemonRpcClient）
        workspace_instance_id: workspace 标识符（16 位 hex）
        agent_session: AgentSession 实例

    Returns:
        daemon 分配的 session_epoch（≥1）

    Raises:
        AgentProtocolError: 握手失败
    """
    # 确保 workspace 已注册到 agent session
    agent_session.register_workspace(workspace_instance_id)

    logger.info(
        "agent %s 与 daemon 握手：workspace=%s",
        agent_session.session_id, workspace_instance_id,
    )

    try:
        # 发送 workspace.connect RPC
        result = daemon_rpc_client.call("workspace.connect", {
            "workspace_instance_id": workspace_instance_id,
            "agent_session_id": agent_session.session_id,
        })
    except Exception as e:
        raise AgentProtocolError(
            "connect_failed",
            f"workspace.connect RPC 失败：{e}",
        ) from e

    # 解析 daemon 返回的 session_epoch
    session_epoch = result.get("session_epoch")
    if session_epoch is None or int(session_epoch) < 1:
        raise AgentProtocolError(
            "invalid_epoch",
            f"daemon 返回非法 session_epoch：{session_epoch}",
        )

    session_epoch = int(session_epoch)

    # 保存到 AgentSession（重置 seq_counter=0）
    agent_session.set_epoch(workspace_instance_id, session_epoch)

    logger.info(
        "agent %s 握手成功：workspace=%s epoch=%d",
        agent_session.session_id, workspace_instance_id, session_epoch,
    )
    return session_epoch


# ============================================
# Refresh 消息构建
# ============================================


def build_refresh_message(
    agent_session: AgentSession,
    workspace_instance_id: str,
    rel_path: str,
    vcs_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """G9: 构建 refresh RPC 的 params dict。

    包含字段：
    - workspace_instance_id: workspace 标识符
    - rel_path: 相对于 workspace 根目录的路径
    - agent_session_id: agent session UUID
    - session_epoch: daemon 分配的 epoch
    - monotonic_seq: agent 本地单调递增 seq
    - vcs_kind / head_sha（可选）：client 侧探测的 VCS 版本信息

    注意：monotonic_seq 由 AgentSession.next_seq() 分配，每次调用 +1。

    Args:
        agent_session: AgentSession 实例
        workspace_instance_id: workspace 标识符
        rel_path: 文件相对路径
        vcs_info: 可选，probe_vcs_info() 的返回值；提供时注入 vcs_kind/head_sha

    Returns:
        refresh RPC 的 params dict

    Raises:
        AgentProtocolError: session 未协商 / workspace 未注册
    """
    if not agent_session.is_active(workspace_instance_id):
        raise AgentProtocolError(
            "session_not_active",
            f"workspace {workspace_instance_id} 的 session 未协商，"
            f"先调用 user_agent_connect()",
        )

    # 分配下一个 monotonic_seq（线程安全）
    try:
        seq = agent_session.next_seq(workspace_instance_id)
    except ValueError as e:
        raise AgentProtocolError("seq_alloc_failed", str(e)) from e

    epoch = agent_session.get_epoch(workspace_instance_id)

    params = {
        "workspace_instance_id": workspace_instance_id,
        "rel_path": rel_path,
        "agent_session_id": agent_session.session_id,
        "session_epoch": epoch,
        "monotonic_seq": seq,
    }

    # VCS 版本信息（可选，向后兼容：daemon 侧 params.get 忽略缺失字段）
    if vcs_info:
        params["vcs_kind"] = vcs_info.get("vcs_kind", "none")
        params["head_sha"] = vcs_info.get("head_sha", "")

    return params


# ============================================
# Refresh 发送：选择 small (params) / large (FD) 路径
# ============================================


# 阈值：超过此大小走 FD 路径（与 ipc_transport.MAX_MSG_BYTES 对齐）
REFRESH_LARGE_FILE_THRESHOLD = 16 * 1024 * 1024  # 16 MB

# 内联（HTTP / 非 AF_UNIX）路径的 hex 编码上限：超过此大小改用 gzip + base64。
#
# Q9 大文件优化（2026-09-17）：daemon HTTP body 上限为 64MiB（http_server.rs
# MAX_BODY_BYTES）。hex 编码膨胀 2x，>32MB 的源码 hex 编码后即超限；且 8-16MB
# 区间的文件 hex 编码后 JSON 体积过大。因此：
#   - <= REFRESH_HEX_LIMIT：hex 内联（小文件默认路径，可调试、无压缩开销）
#   - >  REFRESH_HEX_LIMIT：gzip 压缩 + base64（canonical_bytes_gz_b64）
#     代码文件 gzip 压缩率典型 3-5x，b64 膨胀 1.33x，净收益 2.5-3.5x，
#     使 HTTP 传输可覆盖数十 MB 源码；daemon 侧按 canonical_len 限制解压容量。
REFRESH_HEX_LIMIT = 1 * 1024 * 1024  # 1 MB

# Q10 raw-gz-body（2026-09-17）：multipart 裸 payload 的尺寸上限。
#
# 实测同机环回：裸传 0.5ms/MB（~2GB/s），gzip 压缩+解压链 17ms/MB。
# 因此 ≤ REFRESH_RAW_LIMIT 的文件走 multipart 裸 payload（零编码税、
# 零压缩 CPU）；超过此尺寸才切换到 gzip part——此时 body 空间
# （daemon MAX_BODY_BYTES = 64MiB）与 daemon 内存压力成为主要约束，
# 压缩 CPU 开销被 3-8x 的体积收益抵消。分档切换由 caller 自动完成。
REFRESH_RAW_LIMIT = 32 * 1024 * 1024  # 32 MB

# Q10 异步受理（2026-09-17）：daemon 对 multipart refresh 先返 202 accepted，
# 解析在后台 task 完成。client 按同一 request_id 轮询（dedup InFlight→Replay）
# 直至拿到最终结果。本超时是 client 侧的整体上限（含解析耗时）。
ASYNC_REFRESH_POLL_TIMEOUT = 600.0  # 10 min


def _await_async_refresh(daemon_rpc_client, params: Dict[str, Any],
                         request_id: str, first_resp: Any,
                         poll_timeout: float = ASYNC_REFRESH_POLL_TIMEOUT) -> Dict[str, Any]:
    """Q10 异步受理的 client 侧收口：轮询同一 request_id 直至最终结果。

    daemon 见到 ``status == "accepted"`` 时表示解析仍在后台进行；此时
    按同一 request_id 重发（payload 可省——dedup Replay/InFlight 分支不
    再需要 payload），daemon 的去重层会：
    - 后台任务未完成 → ``E_REQUEST_IN_FLIGHT``（等待 deadline 后返回）→ 继续轮询
    - 后台任务已完成 → Replay 返回最终结果（与同步语义完全一致）

    Args:
        daemon_rpc_client: 支持 call_multipart 的 client
        params: refresh params（与首次请求完全一致，否则触发
            E_REQUEST_ID_REUSE_MISMATCH）
        request_id: 首次请求使用的 envelope id（复用以命中 dedup）
        first_resp: 首次请求的响应（已判定为 accepted）

    Returns:
        daemon 的最终结果（如 ``{"status": "committed", ...}``）

    Raises:
        AgentProtocolError: 超时仍为 accepted / 轮询失败
    """
    deadline = time.monotonic() + poll_timeout
    resp = first_resp
    while time.monotonic() < deadline:
        if not (isinstance(resp, dict) and resp.get("status") == "accepted"):
            return resp
        time.sleep(0.5)
        try:
            resp = daemon_rpc_client.call_multipart(
                "workspace.file.refresh", params,
                payload=None, request_id=request_id,
            )
        except DaemonRemoteError as e:
            # InFlight = 后台任务仍在解析，继续轮询；其余错误上抛
            if e.code == "E_REQUEST_IN_FLIGHT":
                continue
            raise AgentProtocolError(
                _resolve_rpc_error_code(e),
                f"异步 refresh 轮询失败（request_id={request_id}）：{e}",
            ) from e
    raise AgentProtocolError(
        "async_refresh_timeout",
        f"异步 refresh 超时（{poll_timeout:.0f}s，request_id={request_id}）",
    )


def _refresh_via_gz_b64(daemon_rpc_client, params: Dict[str, Any],
                        canonical_bytes: bytes) -> Dict[str, Any]:
    """兼容降级路径：gzip + base64 内联（canonical_bytes_gz_b64）。

    Q10 后本函数是「旧 daemon 无 multipart 路由」与「client 不支持
    multipart」两种场景的统一降级出口，语义与 Q9 的 gz_b64 主力路径
    完全一致（daemon 侧 6 档 canonical bytes 链照常消费）。
    """
    import base64 as _b64
    import gzip as _gzip

    canonical_len = len(canonical_bytes)
    gz_bytes = _gzip.compress(canonical_bytes, mtime=0)
    params["canonical_bytes_gz_b64"] = _b64.b64encode(gz_bytes).decode("ascii")
    gz_ratio = len(gz_bytes) / canonical_len if canonical_len else 0.0
    try:
        return daemon_rpc_client.call("workspace.file.refresh", params)
    except Exception as e:
        raise AgentProtocolError(
            _resolve_rpc_error_code(e),
            f"workspace.file.refresh RPC 失败（gz+b64 内联，"
            f"len={canonical_len}, gz={len(gz_bytes)}, "
            f"ratio={gz_ratio:.2f}）：{e}",
        ) from e


def send_refresh_to_daemon(
    daemon_rpc_client,
    agent_session: AgentSession,
    workspace_instance_id: str,
    rel_path: str,
    abs_path: str,
    canonical_bytes: Optional[bytes] = None,
    content_hash: Optional[str] = None,
    vcs_info: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """G9: 向 daemon 发送 workspace.file.refresh RPC。

    自动选择传输路径：
    - canonical_bytes 已提供（来自 canonicalize_source_py）：
      - 若 ≤ 1MB：params + canonical_bytes_hex（直接 JSON 内嵌）
      - 若 1MB..32MB：multipart 裸 payload（Q10 raw-gz-body，零编码税；
        同机环回裸传 0.5ms/MB vs 压缩链 17ms/MB）
      - 若 > 32MB：multipart gzip payload（换 body 空间与 daemon 内存）
      - 若 > 16MB 且 client 支持 FD 传递：call_with_fd()（UDS，Linux only，
        优先于 multipart——FD 完全不经 HTTP body）
      - 旧 daemon（无 /v1/rpc/multipart，404）自动降级 gz_b64 内联
    - canonical_bytes 未提供：仅 params（daemon 侧从 abs_path 读取，仅用于兼容旧路径）

    规范：
    - daemon-ipc-security.md §3（memfd 协议）
    - daemon-ipc-security.md §6（S10：传输路径对 agent 透明）

    Args:
        daemon_rpc_client: DaemonClient 单例（或 UnixDaemonRpcClient）
        agent_session: AgentSession 实例
        workspace_instance_id: workspace 标识符
        rel_path: 相对路径
        abs_path: 绝对路径（仅当 canonical_bytes 未提供时由 daemon 读取）
        canonical_bytes: 规范化字节流（来自 canonicalize_source_py）
        content_hash: canonical_bytes 的 sha256 hex（用于大文件 FD 校验）
        vcs_info: 可选，probe_vcs_info() 的返回值（client 侧 VCS 版本信息）

    Returns:
        daemon 响应 dict（如 `{"status": "committed", "generation": "..."}`）

    Raises:
        AgentProtocolError: RPC 失败 / session 失效
    """
    # 1. 构建 refresh params（含 session_epoch + monotonic_seq + 可选 VCS 信息）
    params = build_refresh_message(
        agent_session, workspace_instance_id, rel_path, vcs_info=vcs_info,
    )

    # 2. 根据是否有 canonical_bytes + 大小选择路径
    if canonical_bytes is None:
        # 兼容路径：让 daemon 读取 abs_path
        params["abs_path"] = abs_path
        try:
            return daemon_rpc_client.call("workspace.file.refresh", params)
        except Exception as e:
            raise AgentProtocolError(
                _resolve_rpc_error_code(e),
                f"workspace.file.refresh RPC 失败（无 canonical_bytes）：{e}",
            ) from e

    # canonical_bytes 已提供
    canonical_len = len(canonical_bytes)
    params["canonical_len"] = canonical_len
    params["content_hash"] = content_hash or ""

    # 2a. 小文件：直接 JSON 内嵌（hex 编码）
    if canonical_len <= REFRESH_HEX_LIMIT:
        params["canonical_bytes_hex"] = canonical_bytes.hex()
        try:
            return daemon_rpc_client.call("workspace.file.refresh", params)
        except Exception as e:
            raise AgentProtocolError(
                _resolve_rpc_error_code(e),
                f"workspace.file.refresh RPC 失败（小文件路径）：{e}",
            ) from e

    # 2b. 大文件：优先 FD 传递（需 client 支持 + Unix socket 传输 + 超过阈值）
    #
    # 能力探测修复（2026-09-17）：原实现仅按 socket.AF_UNIX 判定平台能力，
    # 但生产 run_agent_mode 传入的是 HttpDaemonRpcClient（HTTP 传输，
    # 无 call_with_fd 方法）。Linux 上 >16MB 文件会走到此处并触发
    # AttributeError，导致大文件刷新静默失败。正确判据是「client 本身是否
    # 支持 FD 传递」，与平台是否具备 AF_UNIX 无关。
    #
    # 不支持 FD / 非 Unix / 未超过 REFRESH_LARGE_FILE_THRESHOLD 时，走 2c 的
    # gzip+b64 内联降级（daemon 侧 workspace.rs 降级链明确支持
    # canonical_bytes_gz_b64，Windows 分支同样走此路）。
    supports_fd = hasattr(daemon_rpc_client, "call_with_fd")
    use_fd = (
        supports_fd
        and hasattr(socket, "AF_UNIX")
        and canonical_len > REFRESH_LARGE_FILE_THRESHOLD
    )
    if not use_fd:
        # 2c. Q10 raw-gz-body multipart 路径（HTTP 传输的主力路径）
        #
        # 分档切换（2026-09-17，用户批准的「尺寸阈值切换」策略）：
        #   - <= REFRESH_RAW_LIMIT（32MB）：multipart 裸 payload（零编码税、
        #     零压缩 CPU；同机环回实测 0.5ms/MB vs 压缩链 17ms/MB）
        #   - >  REFRESH_RAW_LIMIT：multipart gzip payload（换 body 空间
        #     与 daemon 内存；canonical_len 已写入 params，daemon 侧按其
        #     限制解压容量防 bomb）
        # 两种路径遇旧 daemon（无 /v1/rpc/multipart，404）或无能力 client
        # 时，统一降级到 gz_b64 JSON 内联（Q9 路径，零行为回归）。
        # 显式能力标志（非 hasattr 探测）：MagicMock 会自动创建任意属性，
        # hasattr 判定不可靠；HttpDaemonRpcClient 以类属性显式声明能力，
        # mock client 与 UDS client 无该标志 → 自动降级 gz_b64（零回归）。
        supports_multipart = bool(
            getattr(daemon_rpc_client, "supports_multipart", False)
        )
        if supports_multipart:
            # 固定 request_id：异步受理时轮询须复用同一 id 命中 dedup Replay
            async_rid = params.get("request_id") or str(uuid.uuid4())
            if canonical_len <= REFRESH_RAW_LIMIT:
                try:
                    resp = daemon_rpc_client.call_multipart(
                        "workspace.file.refresh", params,
                        payload=canonical_bytes, gz=False,
                        request_id=async_rid,
                    )
                    return _await_async_refresh(
                        daemon_rpc_client, params, async_rid, resp,
                    )
                except MultipartUnsupportedError:
                    logger.debug(
                        "daemon 不支持 multipart，降级 gz_b64（len=%d）",
                        canonical_len,
                    )
            else:
                import gzip as _gzip

                gz_bytes = _gzip.compress(canonical_bytes, mtime=0)
                try:
                    resp = daemon_rpc_client.call_multipart(
                        "workspace.file.refresh", params,
                        payload=gz_bytes, gz=True,
                        request_id=async_rid,
                    )
                    return _await_async_refresh(
                        daemon_rpc_client, params, async_rid, resp,
                    )
                except MultipartUnsupportedError:
                    logger.debug(
                        "daemon 不支持 multipart，降级 gz_b64（len=%d, gz=%d）",
                        canonical_len, len(gz_bytes),
                    )
        # 2c-降级：gzip + base64 内联（旧 daemon / 无 multipart 能力 client）
        return _refresh_via_gz_b64(daemon_rpc_client, params, canonical_bytes)

    # Linux 大文件路径（client 须支持 FD）：优先 memfd，降级普通临时文件
    from callwarden.server.ipc_transport import create_sealed_memfd
    fd = -1
    tmp_path = None
    use_memfd = False
    try:
        try:
            fd = create_sealed_memfd(canonical_bytes)
            use_memfd = True
        except (AttributeError, OSError):
            # memfd 不可用（非 Linux 或内核版本太老）：降级普通临时文件
            tmp_dir = os.path.join(
                os.path.expanduser("~"), ".callwarden", "agent_tmp",
            )
            os.makedirs(tmp_dir, exist_ok=True)
            tmp_path = os.path.join(
                tmp_dir,
                f"refresh_{agent_session.session_id}_"
                f"{params['monotonic_seq']}.tmp",
            )
            with open(tmp_path, "wb") as f:
                f.write(canonical_bytes)
            fd = os.open(tmp_path, os.O_RDONLY)

        try:
            return daemon_rpc_client.call_with_fd(
                "workspace.file.refresh", params, fd,
            )
        except Exception as e:
            raise AgentProtocolError(
                _resolve_rpc_error_code(e),
                f"workspace.file.refresh RPC 失败（FD 路径，"
                f"{'memfd' if use_memfd else '临时文件'}）：{e}",
            ) from e
    finally:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


# ============================================
# 便捷函数：ping daemon
# ============================================


def user_agent_ping(daemon_rpc_client) -> Dict[str, Any]:
    """G9: agent 启动时 ping daemon，确认 socket 可达。

    Returns:
        daemon 的 ping 响应（如 `{"status": "ok", "peer_uid": ..., "pid": ...}`）

    Raises:
        AgentProtocolError: daemon 不可达
    """
    try:
        return daemon_rpc_client.call("ping")
    except Exception as e:
        raise AgentProtocolError(
            "daemon_unreachable",
            f"daemon ping 失败：{e}",
        ) from e
