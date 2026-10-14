"""TP-11A 安全面验收 harness（tests/acceptance/domains/security/）。

在完全隔离的 daemon（独立 run 根：loopback 随机端口 + run 根内全部状态）上
验证 fs RPC 家族的**根目录限制（root containment）不变量**：

- A1：越根 file.remove 必须被 `path_escape` 拒绝且磁盘文件**不得**被删除
  （D1：任意删除任意本用户文件的安全缺陷回归保护）；
- A2：越根 refresh_file / build_directory / build_graph(scan_root) 必须
  `path_escape`，且 file_instances **不得**出现越根 abs_path 行
  （D3：越根内容读取/索引防护）；
- A3：越根 file.health（文件与目录）必须 `path_escape`（D4 信息泄露防护）；
- A4：根内 file.health 必须按文档合同工作（文件：exists/size/mtime/readable），
  不再误拒"不是目录"（D4 功能恢复）；
- A5：根内 file.remove 必须写入 destructive_operations 审计行
  （D2：审计完整性——此前 INSERT 引用不存在的 target_path 列恒失败），
  并清理被删文件的 file_instances 行（D5：此前单条 DELETE 被 FK 约束中止
  且被 `let _ =` 吞错，索引恒残留）。

设计要点：
- 复用 TP-11I 隔离底座（IsolationEnv → GovernanceEnv → TP-09 隔离面），
  零生产触碰；victim 区放在 run 根内、ws 外（同用户所有权，删除无副作用）；
- **双向核验**：RPC 断言（业务码）+ 权威库直读（DB 交叉核对，防止
  "写错库/静默吞错"类假绿）；
- file_instances 越根行检测以 abs_path 为准（canonical 路径，跨 rel_path
  坏形式 `C:/...` 与 `//?/C:/...` 均命中）。
"""

from __future__ import annotations

import os
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

_HERE = Path(__file__).resolve().parent
_ISO_DIR = _HERE.parent / "isolation"
_GOV_DIR = _HERE.parent / "governance"
for _d in (str(_HERE), str(_ISO_DIR), str(_GOV_DIR)):
    if _d not in sys.path:
        sys.path.insert(0, _d)

from isolation_harness import IsolationEnv  # noqa: E402

# TP-11A 独立 run 根（不复用 TP-11I/TP-11G，避免互踩）
DEFAULT_RUN_ROOT_SEC = Path(
    os.environ.get("CW_TP11A_RUN_ROOT",
                   r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-11A"))


class SecurityEnv(IsolationEnv):
    """TP-11A 安全面环境：TP-11I 隔离底座 + fs RPC 助手 + DB 交叉核对。"""

    def __init__(self, run_root: Optional[Path] = None,
                 daemon_src: Optional[Path] = None,
                 workspace_name: str = "tp11a-ws"):
        super().__init__(run_root=run_root or DEFAULT_RUN_ROOT_SEC,
                         daemon_src=daemon_src,
                         workspace_name=workspace_name)

    # ---------- victim 区（run 根内、ws 外） ----------

    @property
    def victim_dir(self) -> Path:
        d = self.run_root / "victim"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def make_victim(self, name: str, content: str = "TOP-SECRET tp11a\n") -> Path:
        """在 victim 区建文件（越根请求的目标）。"""
        p = self.victim_dir / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return p

    # ---------- fs 面 RPC 助手 ----------

    def file_remove(self, file_path: str):
        return self.rpc("workspace.file.remove", {
            "workspace_instance_id": self.workspace_instance_id,
            "file_path": str(file_path)})

    def file_health(self, file_path: str):
        return self.rpc("workspace.file.health", {
            "workspace_instance_id": self.workspace_instance_id,
            "file_path": str(file_path)})

    def refresh_file(self, file_path: str):
        return self.rpc("workspace.file.refresh_file", {
            "workspace_instance_id": self.workspace_instance_id,
            "file_path": str(file_path)})

    def build_directory(self, dir_path: str, recursive: bool = True):
        return self.rpc("workspace.build_directory", {
            "workspace_instance_id": self.workspace_instance_id,
            "dir_path": str(dir_path),
            "recursive": recursive})

    def build_graph_scan(self, scan_root: str):
        return self.rpc("workspace.build_graph", {
            "workspace_instance_id": self.workspace_instance_id,
            "scan_root": str(scan_root)})

    def file_read(self, file_path: str):
        return self.rpc("workspace.file.read", {
            "workspace_instance_id": self.workspace_instance_id,
            "file_path": str(file_path)})

    def file_grep(self, pattern: str, path: str = ""):
        params: Dict[str, Any] = {
            "workspace_instance_id": self.workspace_instance_id,
            "pattern": pattern}
        if path:
            params["path"] = str(path)
        return self.rpc("workspace.file.grep", params)

    def list_destructive_operations(self, limit: int = 20):
        return self.rpc("list_destructive_operations", {
            "workspace_instance_id": self.workspace_instance_id,
            "limit": limit})

    # ---------- 权威库直读（交叉核对，防静默吞错/写错库） ----------

    @property
    def authority_db(self) -> Path:
        return self.run_root / "home" / ".callwarden" / "callwarden.db"

    def destructive_rows_direct(self) -> List[Dict[str, Any]]:
        """直接读 destructive_operations 全表（不经 RPC，防过滤/映射掩盖）。"""
        db = self.authority_db
        if not db.is_file():
            return []
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT * FROM destructive_operations ORDER BY id")]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    def file_instance_abs_paths(self) -> List[str]:
        """本 workspace 全部 file_instances 的 abs_path（越根检测基准）。"""
        db = self.authority_db
        if not db.is_file() or self.workspace_id is None:
            return []
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            return [str(r[0]) for r in conn.execute(
                "SELECT abs_path FROM file_instances WHERE workspace_id = ?1",
                (self.workspace_id,))]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    @staticmethod
    def _norm_fs(p: str) -> str:
        """文件系统路径规范化（对齐 daemon canonicalize 落库形式）：
        去除 \\\\?\\ 前缀、统一小写、\\→/、去尾分隔符。"""
        s = str(p).replace("\\\\?\\", "").replace("\\", "/")
        return s.rstrip("/").lower()

    def file_instance_rows_for(self, abs_path: str) -> List[Dict[str, Any]]:
        """按 abs_path 查 file_instances 行（规范化比对，跨 \\?\\ 前缀形式）。"""
        db = self.authority_db
        if not db.is_file() or self.workspace_id is None:
            return []
        needle = self._norm_fs(abs_path)
        conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
        try:
            conn.row_factory = sqlite3.Row
            return [dict(r) for r in conn.execute(
                "SELECT * FROM file_instances WHERE workspace_id = ?1",
                (self.workspace_id,))
                if self._norm_fs(dict(r).get("abs_path") or "") == needle]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    # ---------- 断言助手 ----------

    @staticmethod
    def assert_path_escape(r, label: str) -> None:
        """断言 RPC 被以 path_escape 拒绝（与 file.read 同款错误码契约）。"""
        assert not r.ok, (
            f"{label}：越根请求未被拒绝（期望 path_escape，实际 ok={r.ok}，"
            f"result={r.result}）")
        assert r.code == "path_escape", (
            f"{label}：期望错误码 path_escape，实际 {r.code!r}：{r.message[:200]}")

    def assert_no_out_of_root_index(self, label: str) -> None:
        """断言 file_instances 无越根 abs_path 行（victim 区外路径）。"""
        victim_prefix = str(self.victim_dir).replace("\\", "/").lower()
        bad = [p for p in self.file_instance_abs_paths()
               if victim_prefix in p.replace("\\", "/").lower()]
        assert not bad, (
            f"{label}：file_instances 出现越根行（abs_path 含 victim 区）：{bad}")

    def assert_destructive_row(self, operation_type: str,
                               path_contains: str) -> None:
        """断言 destructive_operations 存在指定类型且任一文本列命中路径的审计行。"""
        needle = path_contains.replace("\\", "/").lower()
        rows = [r for r in self.destructive_rows_direct()
                if r.get("operation_type") == operation_type
                and any(isinstance(v, str)
                        and needle in v.replace("\\", "/").lower()
                        for v in r.values())]
        assert rows, (
            f"期望 destructive_operations 存在 operation_type={operation_type} "
            f"且含 {path_contains!r} 的审计行，实际全表="
            f"{self.destructive_rows_direct()}")
