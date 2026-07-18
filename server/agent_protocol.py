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
3. `send_refresh_to_daemon()`：通过 daemon_rpc_client 发送 refresh 请求
   - 小文件：直接 call()（params + canonical_bytes hex）
   - 大文件：call_with_fd()（FD 传递）
   - 失败时返回错误信息（不抛异常，由 watcher 决定重试策略）

设计要点：
- agent 永不直接写 CAS，所有写入都通过 daemon RPC
- canonical_bytes 通过 Rust canonicalize_source_py 生成（BOM/换行/编码归一化）
- agent 重启后，session_epoch 必须重新协商（旧 session 已被 daemon 撤销）
"""

from __future__ import annotations

import logging
import os
import socket
from typing import Any, Dict, Optional, Tuple

from callwarden.server.agent_session import AgentSession

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
) -> Dict[str, Any]:
    """G9: 构建 refresh RPC 的 params dict。

    包含字段：
    - workspace_instance_id: workspace 标识符
    - rel_path: 相对于 workspace 根目录的路径
    - agent_session_id: agent session UUID
    - session_epoch: daemon 分配的 epoch
    - monotonic_seq: agent 本地单调递增 seq

    注意：monotonic_seq 由 AgentSession.next_seq() 分配，每次调用 +1。

    Args:
        agent_session: AgentSession 实例
        workspace_instance_id: workspace 标识符
        rel_path: 文件相对路径

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

    return {
        "workspace_instance_id": workspace_instance_id,
        "rel_path": rel_path,
        "agent_session_id": agent_session.session_id,
        "session_epoch": epoch,
        "monotonic_seq": seq,
    }


# ============================================
# Refresh 发送：选择 small (params) / large (FD) 路径
# ============================================


# 阈值：超过此大小走 FD 路径（与 ipc_transport.MAX_MSG_BYTES 对齐）
REFRESH_LARGE_FILE_THRESHOLD = 16 * 1024 * 1024  # 16 MB


def send_refresh_to_daemon(
    daemon_rpc_client,
    agent_session: AgentSession,
    workspace_instance_id: str,
    rel_path: str,
    abs_path: str,
    canonical_bytes: Optional[bytes] = None,
    content_hash: Optional[str] = None,
) -> Dict[str, Any]:
    """G9: 向 daemon 发送 workspace.file.refresh RPC。

    自动选择传输路径：
    - canonical_bytes 已提供（来自 canonicalize_source_py）：
      - 若 ≤ 16MB：params + canonical_bytes_hex（直接 JSON 内嵌）
      - 若 > 16MB：call_with_fd()（FD 传递，Linux only）
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

    Returns:
        daemon 响应 dict（如 `{"status": "committed", "generation": "..."}`）

    Raises:
        AgentProtocolError: RPC 失败 / session 失效
    """
    # 1. 构建 refresh params（含 session_epoch + monotonic_seq）
    params = build_refresh_message(
        agent_session, workspace_instance_id, rel_path,
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
    if canonical_len <= REFRESH_LARGE_FILE_THRESHOLD:
        params["canonical_bytes_hex"] = canonical_bytes.hex()
        try:
            return daemon_rpc_client.call("workspace.file.refresh", params)
        except Exception as e:
            raise AgentProtocolError(
                _resolve_rpc_error_code(e),
                f"workspace.file.refresh RPC 失败（小文件路径）：{e}",
            ) from e

    # 2b. 大文件：通过 FD 传递（Linux only，需要 sendmsg + SCM_RIGHTS）
    # 写入临时文件 → 打开只读 FD → call_with_fd
    if not hasattr(socket, "AF_UNIX"):
        # 非 Unix 平台：降级到 hex 编码（可能超过 16MB，但 daemon 会处理）
        # 实际上这种情况下不应该走到这里——agent 模式本身仅 Linux 启动
        params["canonical_bytes_hex"] = canonical_bytes.hex()
        try:
            return daemon_rpc_client.call("workspace.file.refresh", params)
        except Exception as e:
            raise AgentProtocolError(
                _resolve_rpc_error_code(e),
                f"workspace.file.refresh RPC 失败（降级 hex）：{e}",
            ) from e

    # Linux 大文件路径：写临时文件 → 传 FD
    tmp_path = None
    fd = -1
    try:
        # 写入临时文件（在 agent 用户 home 下）
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

        # 打开只读 FD
        fd = os.open(tmp_path, os.O_RDONLY)
        try:
            return daemon_rpc_client.call_with_fd(
                "workspace.file.refresh", params, fd,
            )
        except Exception as e:
            raise AgentProtocolError(
                _resolve_rpc_error_code(e),
                f"workspace.file.refresh RPC 失败（FD 路径）：{e}",
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
