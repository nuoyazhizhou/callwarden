"""TP-11I 隔离环境 harness：复用 TP-11G 治理底座 + registry fixture 助手。

在完全隔离的 daemon 上验证 workspace/用户/权限隔离（IDOR owner ACL）：

- I1 owner ACL：foreign-owner workspace 的 status/activate/remove 一律
  `workspace_forbidden`（owned_workspace，workspace.rs:1182）；
- I2 list 过滤：workspace.list 只回本 owner 的 registry 行 + 其对应 capture，
  foreign 行与孤儿 capture 均排除（list_unified_workspaces(Some(uid))）；
- I3 存在性不泄露：不存在的 instance → `workspace_not_found`（**不是**
  forbidden，两者错误码必须可区分）；
- I4 数字 id 变体：owned_workspace_by_id 同款 ACL；
- I5 任务绑定隔离：task.create 绑定 foreign-owner workspace 被拒；
- I6 archived 语义：读面 workspace_archived，activate/remove 幂等可读写。

registry fixture：HTTP 回路下所有 workspace 的 owner 都是同一 peer uid
（loopback 无 UDS peer creds，实测 register 响应 owner_uid=4294967295），
故 foreign-owner 行由 harness 在**隔离 registry** 内直接改写 owner_uid
构造（仅 run 根内，零生产触碰；与 TP-10C 的 codegraph 读面补表同模式）。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

_HERE = Path(__file__).resolve().parent
_GOV_DIR = _HERE.parent / "governance"
for _d in (str(_HERE), str(_GOV_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from governance_harness import (  # noqa: E402
    GovernanceEnv,
)

# HTTP 回路 peer uid（loopback 无 UDS peer creds → u32::MAX）
HTTP_PEER_UID = 4294967295
# 构造 foreign-owner 行用的非本机 uid（确定性、不可能与 peer uid 冲突）
FOREIGN_OWNER_UID = 1000
# TP-11I 独立 run 根（不复用 TP-11G 的默认值，避免两套件互踩）
DEFAULT_RUN_ROOT_ISO = Path(
    os.environ.get("CW_TP11I_RUN_ROOT",
                   r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-11I"))


class IsolationEnv(GovernanceEnv):
    """TP-11I 隔离执行环境：TP-11G 底座 + registry fixture 助手。"""

    def __init__(self, run_root: Optional[Path] = None,
                 daemon_src: Optional[Path] = None,
                 workspace_name: str = "tp11i-ws"):
        # TP-11I 用自己的 run 根默认值（不复用 TP-11G 的，避免两套件互踩）
        super().__init__(run_root=run_root or DEFAULT_RUN_ROOT_ISO,
                         daemon_src=daemon_src,
                         workspace_name=workspace_name)

    # ---------- run 根自清洁（防上一轮残留污染本轮断言） ----------

    @classmethod
    def wipe_run_root(cls, run_root: Optional[Path] = None) -> None:
        """清空 run 根：先杀 exe 位于 run 根内的 daemon，再整目录删除。

        🔴 删 run 根会使 daemon 的锁文件一并消失，`_cleanup_leftover_daemon`
        （按 run 根内锁文件发现）检测不到孤儿 daemon；孤儿进程持有旧 registry
        句柄会导致本轮读到上一轮的 owner 改写（foreign owner=1000 污染）。
        故按**可执行路径**主动 taskkill，再删目录。
        """
        import subprocess
        root = Path(run_root or DEFAULT_RUN_ROOT_ISO)
        src = root / "src" / "cw-daemon.exe"
        try:
            out = subprocess.run(
                ["tasklist", "/FI", "IMAGENAME eq cw-daemon.exe", "/FO", "CSV", "/NH"],
                capture_output=True, text=True, encoding="gbk",
                errors="replace", timeout=15).stdout
            for line in out.splitlines():
                parts = [p.strip('"') for p in line.split('","')]
                if not parts or not parts[0].isdigit():
                    continue
                pid = parts[0]
                # 只杀本 run 根部署的 daemon（按 exe 路径判定，禁误杀生产/IDE daemon）
                cmd = subprocess.run(
                    ["wmic", "process", "where", f"ProcessId={pid}", "get",
                     "ExecutablePath", "/value"],
                    capture_output=True, text=True, encoding="utf-8",
                    errors="replace", timeout=15).stdout
                exe = ""
                for ln in cmd.splitlines():
                    if ln.lower().startswith("executablepath="):
                        exe = ln.split("=", 1)[1].strip()
                if exe and str(src).lower() in exe.lower():
                    subprocess.run(["taskkill", "/F", "/PID", pid],
                                   capture_output=True, timeout=15)
        except Exception:
            pass  # 探测失败不阻断；后续断言会暴露脏状态
        if root.exists():
            import shutil
            shutil.rmtree(root, ignore_errors=True)

    # ---------- registry fixture（仅隔离 run 根内） ----------

    @property
    def registry_db(self) -> Path:
        return self.run_root / "home" / ".callwarden" / "daemon" / "registry.db"

    def _registry_conn(self, readonly: bool = False) -> sqlite3.Connection:
        if not self.registry_db.is_file():
            raise FileNotFoundError(f"隔离 registry 不存在：{self.registry_db}")
        if readonly:
            return sqlite3.connect(f"file:{self.registry_db}?mode=ro", uri=True)
        return sqlite3.connect(str(self.registry_db), timeout=15)

    def registry_owner_of(self, instance_id: str) -> Optional[int]:
        """只读查 registry 行 owner_uid。"""
        conn = self._registry_conn(readonly=True)
        try:
            row = conn.execute(
                "SELECT owner_uid FROM daemon_workspaces WHERE workspace_instance_id=?1",
                (instance_id,)).fetchone()
            return int(row[0]) if row else None
        finally:
            conn.close()

    def registry_row(self, instance_id: str) -> Optional[Dict[str, Any]]:
        conn = self._registry_conn(readonly=True)
        try:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM daemon_workspaces WHERE workspace_instance_id=?1",
                (instance_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()

    def set_registry_owner(self, instance_id: str, owner_uid: int) -> None:
        """fixture：把隔离 registry 行的 owner_uid 改写为指定值（模拟他人 workspace）。"""
        conn = self._registry_conn()
        try:
            cur = conn.execute(
                "UPDATE daemon_workspaces SET owner_uid=?1 WHERE workspace_instance_id=?2",
                (owner_uid, instance_id))
            conn.commit()
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"registry owner 改写未命中（instance={instance_id}, "
                    f"rowcount={cur.rowcount}）")
        finally:
            conn.close()

    def set_registry_status(self, instance_id: str, status: str) -> None:
        """fixture：改写 registry 行 status（active/archived）。"""
        conn = self._registry_conn()
        try:
            cur = conn.execute(
                "UPDATE daemon_workspaces SET status=?1 WHERE workspace_instance_id=?2",
                (status, instance_id))
            conn.commit()
            if cur.rowcount != 1:
                raise RuntimeError(f"registry status 改写未命中（instance={instance_id}）")
        finally:
            conn.close()

    def force_reregister_own(self) -> Tuple[str, Optional[int]]:
        """清缓存并重新 register 主 workspace（registry 行被 fixture 删后恢复）。

        setup_workspace 命中缓存会短路返回，故删行后必须清缓存再注册。
        """
        self.workspace_id = None
        self.workspace_instance_id = None
        return self.setup_workspace()

    def delete_registry_row(self, instance_id: str) -> None:
        """fixture：删除隔离 registry 行（构造孤儿 capture）。"""
        conn = self._registry_conn()
        try:
            cur = conn.execute(
                "DELETE FROM daemon_workspaces WHERE workspace_instance_id=?1",
                (instance_id,))
            conn.commit()
            if cur.rowcount != 1:
                raise RuntimeError(
                    f"registry 行删除未命中（instance={instance_id}）")
        finally:
            conn.close()

    # ---------- 隔离面 RPC 助手 ----------

    def register_extra_workspace(self, name: str) -> str:
        """在隔离 run 根内再注册一个 workspace（返回 instance_id）。

        断言新行 owner 为本 peer uid——若读到 foreign owner 说明 run 根有
        上一轮残留（脏状态），此时 fail-fast 而非让后续断言误判产品行为。
        """
        wd = self.run_root / f"ws-{name}"
        wd.mkdir(parents=True, exist_ok=True)
        (wd / "README.md").write_text(f"# {name}\n", encoding="utf-8")
        r = self.rpc("workspace.register", {"client_view_root": str(wd)})
        assert r.ok, f"workspace.register({name}) 失败：{r!r}"
        inst = str(r.get("workspace_instance_id"))
        owner = self.registry_owner_of(inst)
        assert owner == HTTP_PEER_UID, (
            f"新注册 workspace {name} 的 owner 应为本 peer uid "
            f"{HTTP_PEER_UID}，实际 {owner}——run 根疑似有上一轮残留，"
            f"请先 wipe_run_root()")
        return inst

    def workspace_status_by_instance(self, instance_id: str):
        return self.rpc("workspace.status", {"workspace_instance_id": instance_id})

    def workspace_status_by_id(self, numeric_id: int):
        return self.rpc("workspace.status", {"workspace_id": numeric_id})

    def workspace_activate(self, instance_id: str):
        return self.rpc("workspace.activate", {"workspace_instance_id": instance_id})

    def workspace_remove(self, instance_id: str):
        return self.rpc("workspace.remove", {"workspace_instance_id": instance_id})

    def workspace_list(self):
        return self.rpc("workspace.list", {})

    def instances_in_list(self) -> List[str]:
        r = self.workspace_list()
        assert r.ok, f"workspace.list 失败：{r!r}"
        out = []
        for e in (r.result or []):
            inst = e.get("workspace_instance_id")
            if inst:
                out.append(str(inst))
        return out
