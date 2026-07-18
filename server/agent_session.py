"""G9: AgentSession —— per-UID systemd --user agent 的 session 状态管理。

对应设计：
- `docs/design/enterprise-architecture-evolution.md` §v8 "systemd --user agent 回传 canonical bytes"
- `docs/design/watcher-generation-state-machine.md` §4.1（session epoch CAS）

职责：
- 生成 session_id（UUID，首次启动时生成，持久化到 ~/.callwarden/agent_session.json）
- 持有 session_epoch（daemon 通过 workspace.connect RPC 分配，单调递增）
- 维护 seq_counter（单调递增，每次 refresh +1；与 daemon 侧 file_generations.latest_seq 对应）
- 持久化 session 状态（崩溃恢复后能继续用同一 session_id，但 epoch 必须重新协商）

设计要点：
- session_id 是 agent 本地生成的 UUID，daemon 用它做 session_epoch CAS
- session_epoch 是 daemon 分配的单调值，旧 session 永久失效
  （daemon 侧 G33 已实现 daemon_handle_connect）
- seq_counter 是 agent 本地维护的单调计数器，确保 daemon 收到的 refresh 消息有序
- 状态文件在 ~/.callwarden/agent_session.json，记录 session_id + workspace_id 列表

线程安全：AgentSession 内部加锁，可被 watcher 线程 + 主线程并发访问。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from typing import Optional, Dict, Any


# AgentSession 状态文件默认路径
DEFAULT_AGENT_SESSION_FILE = os.path.join(
    os.path.expanduser("~"), ".callwarden", "agent_session.json"
)


class AgentSession:
    """G9: Agent session 状态管理。

    用法：
    ```python
    session = AgentSession.create_or_load("/path/to/session.json")
    # 与 daemon 协商 epoch
    epoch = session.negotiate_epoch(daemon_client, workspace_instance_id)
    # 每次 refresh
    seq = session.next_seq(workspace_instance_id)
    # 发送 refresh 消息到 daemon
    ```
    """

    def __init__(
        self,
        session_id: str,
        session_file: str = DEFAULT_AGENT_SESSION_FILE,
    ):
        self._session_id = session_id
        self._session_file = session_file
        # RLock（可重入）：_save() 在 set_epoch/next_seq 等 with self._lock 内部调用，
        # 自身也需要 with self._lock 保护写入文件操作。Lock 会死锁，必须用 RLock。
        self._lock = threading.RLock()
        # workspace_instance_id → {epoch, seq_counter, last_refresh_at}
        self._workspaces: Dict[str, Dict[str, Any]] = {}
        self._loaded = False

    # ============================================
    # 工厂方法
    # ============================================

    @classmethod
    def create_or_load(
        cls,
        session_file: str = DEFAULT_AGENT_SESSION_FILE,
    ) -> "AgentSession":
        """加载现有 session 或创建新 session。

        - 若 session_file 存在且合法：加载 session_id + workspace 列表
        - 否则：生成新 UUID 作为 session_id，写入文件

        注意：加载的 session_epoch 会被清零（必须重新与 daemon 协商），
        因为 daemon 侧可能已经撤销了旧 session。
        """
        session_id = None
        workspaces = {}
        if os.path.exists(session_file):
            try:
                with open(session_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                session_id = data.get("session_id")
                # 加载 workspace 列表（但 epoch 清零，必须重新协商）
                for ws_id, ws_data in (data.get("workspaces") or {}).items():
                    workspaces[ws_id] = {
                        "epoch": 0,  # 强制重新协商
                        "seq_counter": 0,
                        "last_refresh_at": 0.0,
                    }
            except (json.JSONDecodeError, OSError):
                pass  # 文件损坏，重新创建

        if not session_id:
            session_id = f"agent-{uuid.uuid4().hex[:12]}"

        session = cls(session_id, session_file)
        session._workspaces = workspaces
        session._loaded = True
        session._save()
        return session

    @classmethod
    def create_in_memory(cls, session_id: Optional[str] = None) -> "AgentSession":
        """测试用：创建内存 session（不持久化）。"""
        sid = session_id or f"agent-test-{uuid.uuid4().hex[:8]}"
        session = cls(sid, session_file=":memory:")
        session._loaded = True
        return session

    # ============================================
    # 属性
    # ============================================

    @property
    def session_id(self) -> str:
        """Agent session UUID（持久化，跨重启保持）。"""
        return self._session_id

    @property
    def session_file(self) -> str:
        """Session 状态文件路径。"""
        return self._session_file

    # ============================================
    # Workspace 级别操作
    # ============================================

    def register_workspace(self, workspace_instance_id: str) -> None:
        """注册一个 workspace（尚未协商 epoch）。"""
        with self._lock:
            if workspace_instance_id not in self._workspaces:
                self._workspaces[workspace_instance_id] = {
                    "epoch": 0,
                    "seq_counter": 0,
                    "last_refresh_at": 0.0,
                }
                self._save()

    def get_epoch(self, workspace_instance_id: str) -> int:
        """获取 workspace 的当前 session_epoch（0 表示尚未协商）。"""
        with self._lock:
            ws = self._workspaces.get(workspace_instance_id)
            return ws["epoch"] if ws else 0

    def set_epoch(self, workspace_instance_id: str, epoch: int) -> None:
        """设置 workspace 的 session_epoch（daemon 协商后调用）。"""
        with self._lock:
            if workspace_instance_id not in self._workspaces:
                self._workspaces[workspace_instance_id] = {
                    "epoch": 0,
                    "seq_counter": 0,
                    "last_refresh_at": 0.0,
                }
            self._workspaces[workspace_instance_id]["epoch"] = int(epoch)
            # 新 epoch 重置 seq_counter（daemon 侧会 UPDATE latest_seq=0）
            self._workspaces[workspace_instance_id]["seq_counter"] = 0
            self._save()

    def next_seq(self, workspace_instance_id: str) -> int:
        """分配下一个单调递增的 seq（每次 refresh 调用）。

        返回：分配的 seq 值（从 1 开始）
        """
        with self._lock:
            if workspace_instance_id not in self._workspaces:
                raise ValueError(
                    f"workspace {workspace_instance_id} 未注册，"
                    f"先调用 register_workspace()"
                )
            ws = self._workspaces[workspace_instance_id]
            if ws["epoch"] == 0:
                raise ValueError(
                    f"workspace {workspace_instance_id} 的 session_epoch 未协商，"
                    f"先调用 set_epoch()"
                )
            ws["seq_counter"] += 1
            ws["last_refresh_at"] = time.time()
            self._save()
            return ws["seq_counter"]

    def get_seq(self, workspace_instance_id: str) -> int:
        """获取当前 seq_counter（不递增）。"""
        with self._lock:
            ws = self._workspaces.get(workspace_instance_id)
            return ws["seq_counter"] if ws else 0

    def list_workspaces(self) -> list[str]:
        """列出所有已注册的 workspace_instance_id。"""
        with self._lock:
            return list(self._workspaces.keys())

    def remove_workspace(self, workspace_instance_id: str) -> bool:
        """移除 workspace（agent 不再监控该 workspace）。"""
        with self._lock:
            if workspace_instance_id in self._workspaces:
                del self._workspaces[workspace_instance_id]
                self._save()
                return True
            return False

    def is_active(self, workspace_instance_id: str) -> bool:
        """检查 workspace 是否已注册且 epoch 已协商。"""
        with self._lock:
            ws = self._workspaces.get(workspace_instance_id)
            return ws is not None and ws["epoch"] > 0

    # ============================================
    # 持久化
    # ============================================

    def _save(self) -> None:
        """持久化 session 状态到文件（内存模式跳过）。"""
        if self._session_file == ":memory:":
            return
        try:
            parent = os.path.dirname(os.path.abspath(self._session_file))
            if parent and not os.path.isdir(parent):
                os.makedirs(parent, exist_ok=True)
            with self._lock:
                data = {
                    "session_id": self._session_id,
                    "version": 1,
                    "saved_at": time.time(),
                    "workspaces": dict(self._workspaces),
                }
            # 写临时文件 + 原子 rename
            tmp = self._session_file + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            os.replace(tmp, self._session_file)
        except OSError:
            pass  # 持久化失败不阻塞主流程

    def to_dict(self) -> dict:
        """返回 session 状态快照（监控/调试用）。"""
        with self._lock:
            return {
                "session_id": self._session_id,
                "session_file": self._session_file,
                "workspaces": {
                    ws_id: dict(ws_data)
                    for ws_id, ws_data in self._workspaces.items()
                },
            }

    def __repr__(self) -> str:
        return (
            f"AgentSession(session_id={self._session_id!r}, "
            f"workspaces={len(self._workspaces)})"
        )
