"""compatibility worker——由 daemon 管理的 Python worker 进程。

契约：docs/design/http-daemon-mvp-compatibility-contract.md §3.3
- 仅使用 daemon 创建的 child stdin/stdout 私有 IPC；不监听/不暴露任何外部
  socket、TCP/HTTP 端口，不接受 MCP/CLI 直接连接；
- 帧格式：4-byte big-endian payload length + UTF-8 JSON object，单帧上限 8 MiB；
- stdout 只输出协议帧，日志一律写 stderr；
- 每帧必须包含 worker_protocol_version/request_id/method/params/
  workspace_instance_id/workspace_id/operation_class/deadline，禁止含 db_path；
- worker 只能使用 daemon 注入的显式 workspace context，通过 authority 配置
  （callwarden.config.get_project_db_path）解析用户级数据库；不得接受客户端
  或 frame 传入的 DB path，不得查询/选择 active workspace；
- worker 的 DB 写操作由 daemon 兼容写锁保证与 Rust mutation 串行
  （MVP 仅注册 read_only 方法）；
- MVP 禁止 governance_write（收到即拒绝）。
"""

from __future__ import annotations

import io
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, Optional

# 确保 server/ 与仓库根目录可导入（callwarden.config 等）
_SERVER_DIR = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(_SERVER_DIR)
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
if _SERVER_DIR not in sys.path:
    sys.path.insert(0, _SERVER_DIR)

from callwarden import config as _cfg  # noqa: E402
from server.compat_registry import (  # noqa: E402
    GOVERNANCE_WRITE,
    OPERATION_CLASSES,
    CompatCallContext,
    get_compat_registry,
)

# H4C-2+3 装配 import（T-1786716190783-ba187c88，用户批准的派发单外最小豁免）：
# 导入工具模块即触发其模块级 register_compat_routes 注册到本进程 registry 单例；
# 不触碰任何既有 handler/逻辑/协议。必须用 callwarden.server.tools.X 绝对路径
# （tools/__init__.py 同款），且工具模块内部用 server.compat_registry 保持同单例。
import callwarden.server.tools.tools_query  # noqa: E402  (H4C-2: 符号组 handler 注册)
import callwarden.server.tools.tools_task  # noqa: E402   (H4C-3: 任务组 handler 注册)
import callwarden.server.tools.tools_summary  # noqa: E402 (H4C-2 第二批: 摘要/演化/护栏/缺陷组 handler 注册)
import callwarden.server.tools.tools_semantic  # noqa: E402 (H4C-2 第二批: 语义/外部符号组 handler 注册)
import callwarden.server.tools.tools_security  # noqa: E402 (H4C-2 第三批: 分支/编辑历史/跨仓库/LSP 组 handler 注册)
import callwarden.server.tools.tools_rules  # noqa: E402   (H4C-2 第三批: toolchain/edge 组 handler 注册)
import callwarden.server.tools.tools_collab  # noqa: E402  (H4C-2 第三批: collab 组只读 handler 注册)
import callwarden.server.tools.tools_p2_graph  # noqa: E402 (H4C-2 第三批: p2 依赖图/环检测组只读 handler 注册)
import callwarden.server.tools.tools_p3_identity  # noqa: E402 (H4C-2 第三批: p3 身份/证明组只读 handler 注册)
import callwarden.server.tools.tools_p4_lease  # noqa: E402 (H4C-2 第三批: p4 assignment_show 只读 handler 注册)

# 帧协议常量（契约 §3.3）
WORKER_PROTOCOL_VERSION = 1
MAX_FRAME_BYTES = 8 * 1024 * 1024
FRAME_LEN_BYTES = 4

# 结构化错误码（worker 侧；daemon 侧另有 UNAVAILABLE/TIMEOUT）
ERR_PROTOCOL = "E_COMPAT_WORKER_PROTOCOL"
ERR_DBPATH_FORBIDDEN = "E_COMPAT_DBPATH_FORBIDDEN"
ERR_GOVERNANCE_FORBIDDEN = "E_COMPAT_GOVERNANCE_WRITE_FORBIDDEN"
ERR_METHOD_NOT_FOUND = "E_COMPAT_METHOD_NOT_FOUND"
ERR_EXECUTION = "E_COMPAT_EXECUTION_ERROR"

# 帧必填字段（契约 §3.3 逐项核对）
REQUIRED_FIELDS = (
    "request_id",
    "method",
    "params",
    "workspace_instance_id",
    "workspace_id",
    "operation_class",
    "deadline",
)


# ---------------------------------------------------------------
# 帧编解码
# ---------------------------------------------------------------


def _read_exact(stream: io.BufferedIOBase, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = stream.read(n - len(buf))
        if not chunk:
            raise EOFError("输入流提前结束")
        buf.extend(chunk)
    return bytes(buf)


def read_frame(stream: io.BufferedIOBase) -> Optional[Dict[str, Any]]:
    """从二进制流读取一帧。正常返回 dict；EOF 返回 None；损坏抛 ValueError。"""
    try:
        header = _read_exact(stream, FRAME_LEN_BYTES)
    except EOFError:
        return None
    length = int.from_bytes(header, byteorder="big")
    if length == 0 or length > MAX_FRAME_BYTES:
        raise ValueError(f"非法帧长度 {length}（上限 {MAX_FRAME_BYTES}）")
    payload = _read_exact(stream, length)
    obj = json.loads(payload.decode("utf-8", errors="replace"))
    if not isinstance(obj, dict):
        raise ValueError("帧体必须是 JSON object")
    return obj


def write_frame(stream: io.BufferedIOBase, obj: Dict[str, Any]) -> None:
    """向二进制流写入一帧（4-byte BE length + UTF-8 JSON）。"""
    payload = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    if len(payload) > MAX_FRAME_BYTES:
        raise ValueError(f"帧超过 {MAX_FRAME_BYTES} 字节上限")
    stream.write(len(payload).to_bytes(FRAME_LEN_BYTES, byteorder="big"))
    stream.write(payload)


# ---------------------------------------------------------------
# 错误/响应构造
# ---------------------------------------------------------------


def _error_frame(request_id: Optional[str], code: str, message: str,
                 retryable: bool = True, recovery: str = "") -> Dict[str, Any]:
    return {
        "worker_protocol_version": WORKER_PROTOCOL_VERSION,
        "request_id": request_id or "",
        "ok": False,
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "recovery": recovery,
        },
    }


def _success_frame(request_id: str, result: Any) -> Dict[str, Any]:
    return {
        "worker_protocol_version": WORKER_PROTOCOL_VERSION,
        "request_id": request_id,
        "ok": True,
        "result": result,
    }


# ---------------------------------------------------------------
# 帧校验与分发
# ---------------------------------------------------------------


def _validate_frame(frame: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """校验请求帧，返回错误帧（None 表示通过）。"""
    if frame.get("worker_protocol_version") != WORKER_PROTOCOL_VERSION:
        return _error_frame(
            None,
            ERR_PROTOCOL,
            f"worker_protocol_version 必须为 {WORKER_PROTOCOL_VERSION}",
            retryable=False,
            recovery="检查 daemon 与 worker 协议版本一致性",
        )
    request_id = frame.get("request_id")
    if not isinstance(request_id, str) or not request_id:
        return _error_frame(
            None, ERR_PROTOCOL, "request_id 必须是非空字符串", retryable=False
        )
    if "db_path" in frame:
        return _error_frame(
            request_id,
            ERR_DBPATH_FORBIDDEN,
            "frame 禁止携带 db_path（worker 通过 authority 配置解析数据库）",
            retryable=False,
            recovery="移除 db_path 后重试，数据库路径由 daemon authority 配置决定",
        )
    for field_name in REQUIRED_FIELDS:
        if field_name not in frame:
            return _error_frame(
                request_id,
                ERR_PROTOCOL,
                f"帧缺少必填字段: {field_name}",
                retryable=False,
            )
    operation_class = frame.get("operation_class")
    if not isinstance(operation_class, str) or operation_class not in OPERATION_CLASSES:
        return _error_frame(
            request_id, ERR_PROTOCOL, f"非法 operation_class: {operation_class!r}", retryable=False
        )
    if operation_class == GOVERNANCE_WRITE:
        return _error_frame(
            request_id,
            ERR_GOVERNANCE_FORBIDDEN,
            "MVP 禁止 governance_write 兼容方法",
            retryable=False,
            recovery="该操作不通过 compatibility worker 执行",
        )
    method = frame.get("method")
    if not isinstance(method, str) or not method:
        return _error_frame(request_id, ERR_PROTOCOL, "method 必须是非空字符串", retryable=False)
    params = frame.get("params")
    if not isinstance(params, dict):
        return _error_frame(request_id, ERR_PROTOCOL, "params 必须是 object", retryable=False)
    return None


def _resolve_db_path() -> str:
    """通过 authority 配置解析用户级数据库路径（不信任任何外部传入路径）。"""
    return _cfg.get_project_db_path()


def handle_frame(frame: Dict[str, Any]) -> Dict[str, Any]:
    """处理单帧请求，返回响应帧。"""
    bad = _validate_frame(frame)
    if bad is not None:
        return bad
    request_id = frame["request_id"]
    method = frame["method"]
    params = frame["params"]
    registry = get_compat_registry()
    entry = registry.get(method)
    if entry is None:
        return _error_frame(
            request_id,
            ERR_METHOD_NOT_FOUND,
            f"未知兼容方法: {method}",
            retryable=False,
            recovery="检查 capability registry 是否已声明该方法",
        )
    operation_class = frame["operation_class"]
    if entry.operation_class != operation_class:
        return _error_frame(
            request_id,
            ERR_PROTOCOL,
            f"frame operation_class {operation_class!r} 与注册 {entry.operation_class!r} 不一致",
            retryable=False,
        )

    ctx = CompatCallContext(
        request_id=request_id,
        method=method,
        params=params,
        workspace_instance_id=frame["workspace_instance_id"],
        workspace_id=frame["workspace_id"] if frame["workspace_id"] is not None else None,
        operation_class=operation_class,
        deadline=frame.get("deadline"),
        db_path=_resolve_db_path(),
    )
    try:
        with _open_readonly_conn(ctx.db_path) as conn:
            ctx = CompatCallContext(**{**ctx.__dict__, "conn": conn})
            result = entry.handler(ctx)
        return _success_frame(request_id, result)
    except sqlite3.OperationalError as e:
        # 表结构缺失/数据库不可用等：可重试，恢复指引指向重建索引
        return _error_frame(
            request_id,
            ERR_EXECUTION,
            f"数据库查询失败: {e}",
            retryable=True,
            recovery="确认用户级数据库 schema 已初始化（cw --refresh-all 后重试）",
        )
    except Exception as e:  # noqa: BLE001 —— worker 边界必须结构化返回，禁止静默吞异常
        return _error_frame(
            request_id,
            ERR_EXECUTION,
            f"方法执行异常: {type(e).__name__}: {e}",
            retryable=True,
        )


def _open_readonly_conn(db_path: str) -> sqlite3.Connection:
    """打开用户级数据库的只读连接（WAL 模式下读到已提交最新数据）。"""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------
# 主循环
# ---------------------------------------------------------------


def main() -> int:
    """worker 主循环：stdin 读帧 → 分发 → stdout 写帧。"""
    stdin = sys.stdin.buffer
    stdout = sys.stdout.buffer
    while True:
        try:
            frame = read_frame(stdin)
        except EOFError:
            break  # daemon 关闭管道 → 正常退出
        except ValueError as e:
            # 协议损坏：尽力回错误帧；stdout 不可写则退出
            try:
                write_frame(stdout, _error_frame(
                    None, ERR_PROTOCOL, f"帧解码失败: {e}",
                    retryable=False, recovery="协议损坏，检查 daemon/worker 版本一致性",
                ))
                stdout.flush()
            except (BrokenPipeError, OSError):
                return 0
            continue
        if frame is None:
            break
        response = handle_frame(frame)
        try:
            write_frame(stdout, response)
            stdout.flush()
        except (BrokenPipeError, OSError):
            return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
