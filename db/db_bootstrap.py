"""
db_bootstrap.py
==============

自举扫描基线与变化检测 Mixin。

提供 workspace_scan_runs 表的写入与查询能力，以及基于 git/file_instances 的
变化检测，支撑 task capture-diff 闭环：

- record_workspace_scan_run：记录一次扫描基线（git_head / status_hash / mtime）
- get_workspace_changes_since：返回两次扫描/commit 之间的变更文件
- get_latest_scan_run：读取最近一次扫描基线
- update_scan_run_status：更新扫描状态（running -> completed/failed）

设计原则：
1. Git 项目优先使用 git_head + git diff --name-status + git status --porcelain，
   速度快且准确。
2. 非 Git 项目回退到 file_instances.mtime / current_content_hash 对比。
3. 目录 mtime 只作为快速提示，不作为唯一真相源（Windows 和深层文件变更不可靠）。
4. 异常不静默吞掉，向外抛出供调用方处理。
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from typing import Any, Dict, List, Optional

from ..i18n import t


class BootstrapMixin:
    """自举扫描基线与变化检测功能 Mixin

    通过 self.conn 访问数据库连接，self.workspace_root 访问工作区根路径。
    所有方法均假设 workspace 已激活（_get_active_workspace_id 可用）。
    """

    # ============================================
    # Git 工具方法
    # ============================================

    def _is_git_repo(self) -> bool:
        """判断当前工作区是否为 Git 仓库"""
        root = getattr(self, "workspace_root", None)
        if not root:
            ws = self.get_active_workspace()
            root = ws["root_path"] if ws else None
        if not root:
            return False
        return os.path.isdir(os.path.join(root, ".git"))

    def _run_git(self, args: List[str], timeout: int = 30) -> str:
        """执行 git 命令并返回 stdout

        Args:
            args: git 命令参数列表（不含 "git" 前缀）
            timeout: 超时秒数

        Returns:
            stdout 输出（已 strip）

        Raises:
            subprocess.CalledProcessError: git 命令失败
            FileNotFoundError: git 未安装
        """
        root = getattr(self, "workspace_root", "")
        result = subprocess.run(
            ["git"] + args,
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=timeout,
        )
        # 仅去除尾部换行，保留前导空格（git status --porcelain 首行可能以空格开头）
        return result.stdout.rstrip("\r\n")

    def _get_git_head(self) -> str:
        """获取当前 HEAD commit hash，失败返回空串"""
        try:
            return self._run_git(["rev-parse", "HEAD"])
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return ""

    def _get_git_status_porcelain(self) -> str:
        """获取 git status --porcelain=v1 原始输出

        包含 staged/unstaged/untracked 文件状态。
        """
        try:
            return self._run_git(["status", "--porcelain=v1", "--untracked-files=all"])
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return ""

    def _compute_git_status_hash(self) -> str:
        """计算 git status 的 hash，用于快速判断 dirty 状态是否变化"""
        status = self._get_git_status_porcelain()
        if not status:
            return ""
        return hashlib.sha256(status.encode("utf-8")).hexdigest()[:16]

    def _parse_git_porcelain(self, status: str) -> List[Dict[str, str]]:
        """解析 git status --porcelain=v1 输出为变更列表

        格式: XY <path>  或  XY <path> -> <renamed>

        X: staged 状态（A/M/D/R/C/...）
        Y: 工作区状态（M/D/R/C/...）
        空格表示该侧无变更。
        '??' 表示 untracked。

        Returns:
            [{"path": ..., "status": ..., "staged": ..., "worktree": ...}, ...]
        """
        results: List[Dict[str, str]] = []
        if not status:
            return results
        for line in status.splitlines():
            if not line or len(line) < 3:
                continue
            xy = line[:2]
            path_part = line[3:].strip()
            if not path_part:
                continue
            # 处理重命名: old -> new
            if " -> " in path_part:
                path_part = path_part.split(" -> ")[-1]
            x = xy[0] if xy[0] != " " else ""
            y = xy[1] if xy[1] != " " else ""
            # 合并状态用于展示
            if xy == "??":
                combined = "untracked"
            elif x and y:
                combined = f"staged+worktree"
            elif x:
                combined = f"staged-{x}"
            elif y:
                combined = f"worktree-{y}"
            else:
                combined = "modified"
            results.append({
                "path": path_part.strip(),
                "status": combined,
                "staged": x,
                "worktree": y,
            })
        return results

    def _get_git_diff_files(self, base_commit: str) -> List[Dict[str, str]]:
        """通过 git diff --name-status 获取 base..HEAD 的变更文件

        Args:
            base_commit: 基线 commit（空串则使用 HEAD 自身，无变更）

        Returns:
            [{"path": ..., "status": ...}, ...]，status 为 A/M/D/R/C 等
        """
        if not base_commit:
            return []
        try:
            output = self._run_git(
                ["diff", "--name-status", f"{base_commit}...HEAD"]
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            return []
        results: List[Dict[str, str]] = []
        for line in output.splitlines():
            if not line:
                continue
            parts = line.split("\t", 1)
            if len(parts) < 2:
                continue
            status_code = parts[0].strip()
            path = parts[1].strip()
            # 重命名: R100\told\tnew
            if " -> " in path:
                path = path.split(" -> ")[-1]
            results.append({"path": path, "status": status_code})
        return results

    # ============================================
    # 非 Git 项目变化检测（file_instances 回退）
    # ============================================

    def _get_file_instances_changes(self, scan_id: int) -> List[Dict[str, str]]:
        """非 Git 项目回退方案：对比 file_instances.mtime 与磁盘实际 mtime

        Args:
            scan_id: 上次扫描基线 ID（用于读取 manifest_hash 和 root_mtime）

        Returns:
            [{"path": ..., "status": ...}, ...]
        """
        ws_id = self._get_active_workspace_id()
        root = getattr(self, "workspace_root", "")
        if not root:
            return []

        changes: List[Dict[str, str]] = []
        try:
            cur = self.conn.execute(
                "SELECT rel_path, abs_path, mtime, current_content_hash "
                "FROM file_instances WHERE workspace_id = ?",
                (ws_id,),
            )
            rows = cur.fetchall()
        except Exception:
            return []

        for row in rows:
            rel_path = row["rel_path"] if isinstance(row, dict) else row[0]
            abs_path = row["abs_path"] if isinstance(row, dict) else row[1]
            db_mtime = row["mtime"] if isinstance(row, dict) else row[2]
            try:
                if not os.path.exists(abs_path):
                    changes.append({"path": rel_path, "status": "D"})
                    continue
                disk_mtime = os.path.getmtime(abs_path)
                if disk_mtime > db_mtime:
                    changes.append({"path": rel_path, "status": "M"})
            except OSError:
                continue
        return changes

    # ============================================
    # 公开方法：扫描基线记录与变化检测
    # ============================================

    def record_workspace_scan_run(
        self,
        purpose: str = "bootstrap",
        task_id: str = "",
        step_id: str = "",
        status: str = "running",
        metadata: Optional[Dict[str, Any]] = None,
    ) -> int:
        """记录一次扫描基线到 workspace_scan_runs 表

        自动检测 Git 状态（git_head / git_status_hash），非 Git 项目回退到
        root_mtime + file_count + manifest_hash。

        Args:
            purpose: 扫描目的（bootstrap / capture / review）
            task_id: 关联任务 ID（可选）
            step_id: 关联步骤 ID（可选）
            status: 扫描状态（running / completed / failed）
            metadata: 附加元数据（JSON 序列化存储）

        Returns:
            新建的 scan_run ID，失败返回 0
        """
        ws_id = self._get_active_workspace_id()
        root = getattr(self, "workspace_root", "")
        started_at = time.time()

        baseline_type = "git" if self._is_git_repo() else "mtime"
        git_head = ""
        git_status_hash = ""
        git_merge_base = ""
        root_mtime = 0.0
        file_count = 0
        manifest_hash = ""

        if baseline_type == "git":
            git_head = self._get_git_head()
            git_status_hash = self._compute_git_status_hash()
        else:
            # 非 Git 项目：记录 root_mtime + file_count + manifest_hash
            if root and os.path.isdir(root):
                try:
                    root_mtime = os.path.getmtime(root)
                except OSError:
                    root_mtime = 0.0
            try:
                cur = self.conn.execute(
                    "SELECT COUNT(*) FROM file_instances WHERE workspace_id = ?",
                    (ws_id,),
                )
                row = cur.fetchone()
                file_count = row[0] if row else 0
            except Exception:
                file_count = 0
            # manifest_hash: file_instances 的 mtime 聚合 hash
            try:
                cur = self.conn.execute(
                    "SELECT rel_path, mtime FROM file_instances WHERE workspace_id = ? "
                    "ORDER BY rel_path",
                    (ws_id,),
                )
                rows = cur.fetchall()
                parts = []
                for r in rows:
                    rp = r["rel_path"] if isinstance(r, dict) else r[0]
                    mt = r["mtime"] if isinstance(r, dict) else r[1]
                    parts.append(f"{rp}:{mt}")
                manifest_hash = hashlib.sha256(
                    "\n".join(parts).encode("utf-8")
                ).hexdigest()[:16]
            except Exception:
                manifest_hash = ""

        metadata_json = "{}"
        if metadata:
            try:
                metadata_json = json.dumps(metadata, ensure_ascii=False)
            except (TypeError, ValueError):
                metadata_json = "{}"

        try:
            cur = self.conn.execute(
                "INSERT INTO workspace_scan_runs "
                "(workspace_id, purpose, task_id, step_id, baseline_type, "
                " git_head, git_merge_base, git_status_hash, root_mtime, "
                " file_count, manifest_hash, changed_files_json, metadata_json, "
                " started_at, completed_at, status) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?)",
                (
                    ws_id, purpose, task_id, step_id, baseline_type,
                    git_head, git_merge_base, git_status_hash, root_mtime,
                    file_count, manifest_hash, "[]", metadata_json,
                    started_at, status,
                ),
            )
            self.conn.commit()
            return int(cur.lastrowid or 0)
        except Exception:
            return 0

    def update_scan_run_status(
        self,
        scan_id: int,
        status: str,
        changed_files: Optional[List[Dict[str, str]]] = None,
    ) -> bool:
        """更新扫描状态（running -> completed/failed）

        Args:
            scan_id: 扫描基线 ID
            status: 新状态（completed / failed）
            changed_files: 变更文件列表（JSON 序列化存储到 changed_files_json）

        Returns:
            成功返回 True，失败返回 False
        """
        if scan_id <= 0:
            return False
        changed_json = "[]"
        if changed_files:
            try:
                changed_json = json.dumps(changed_files, ensure_ascii=False)
            except (TypeError, ValueError):
                changed_json = "[]"
        try:
            cur = self.conn.execute(
                "UPDATE workspace_scan_runs SET status = ?, completed_at = ?, "
                "changed_files_json = ? WHERE id = ?",
                (status, time.time(), changed_json, scan_id),
            )
            self.conn.commit()
            return cur.rowcount > 0
        except Exception:
            return False

    def get_latest_scan_run(
        self,
        purpose: str = "",
        task_id: str = "",
    ) -> Optional[Dict[str, Any]]:
        """读取最近一次扫描基线

        Args:
            purpose: 限定扫描目的（空串表示不限）
            task_id: 限定任务 ID（空串表示不限）

        Returns:
            扫描基线 dict，无记录返回 None
        """
        ws_id = self._get_active_workspace_id()
        sql = (
            "SELECT * FROM workspace_scan_runs WHERE workspace_id = ? "
        )
        params: List[Any] = [ws_id]
        if purpose:
            sql += " AND purpose = ?"
            params.append(purpose)
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY started_at DESC LIMIT 1"
        try:
            cur = self.conn.execute(sql, params)
            row = cur.fetchone()
            if row is None:
                return None
            return dict(row) if not isinstance(row, dict) else row
        except Exception:
            return None

    def get_workspace_changes_since(
        self,
        scan_id: int = 0,
        base_commit: str = "",
        include_untracked: bool = True,
    ) -> Dict[str, Any]:
        """返回两次扫描/commit 之间的变更文件

        优先级：
        1. Git 项目：git diff --name-status + git status --porcelain
        2. 非 Git 项目：file_instances mtime 对比

        Args:
            scan_id: 上次扫描基线 ID（0 表示使用最近一次）
            base_commit: 基线 commit（优先于 scan_id 的 git_head）
            include_untracked: 是否包含 untracked 文件

        Returns:
            {
                "baseline_type": "git" | "mtime",
                "base_commit": ...,
                "current_head": ...,
                "changed_files": [{"path": ..., "status": ...}, ...],
                "status_hash": ...,
                "is_dirty": bool,
            }
        """
        # 读取基线
        baseline = None
        if scan_id > 0:
            try:
                cur = self.conn.execute(
                    "SELECT * FROM workspace_scan_runs WHERE id = ?",
                    (scan_id,),
                )
                baseline = cur.fetchone()
            except Exception:
                baseline = None
        if baseline is None:
            baseline = self.get_latest_scan_run()

        # 确定基线 commit
        if base_commit:
            base = base_commit
        elif baseline:
            base = (
                baseline["git_head"]
                if isinstance(baseline, dict)
                else baseline["git_head"]
                if "git_head" in baseline.keys()
                else ""
            )
        else:
            base = ""

        current_head = self._get_git_head()
        status_porcelain = self._get_git_status_porcelain()
        status_hash = (
            hashlib.sha256(status_porcelain.encode("utf-8")).hexdigest()[:16]
            if status_porcelain else ""
        )
        is_dirty = bool(status_porcelain)

        changed_files: List[Dict[str, str]] = []

        if self._is_git_repo():
            # Git 项目：合并 diff 和 status 结果
            diff_files = self._get_git_diff_files(base) if base else []
            status_files = self._parse_git_porcelain(status_porcelain)

            # 用 path 去重（diff 优先，status 补充）
            seen_paths = set()
            for f in diff_files:
                seen_paths.add(f["path"])
                changed_files.append(f)
            for f in status_files:
                if f["path"] not in seen_paths:
                    if not include_untracked and f["status"] == "untracked":
                        continue
                    changed_files.append(f)
        else:
            # 非 Git 项目回退到 file_instances mtime 对比
            bid = scan_id if scan_id > 0 else (
                baseline["id"] if baseline else 0
            )
            changed_files = self._get_file_instances_changes(bid)

        return {
            "baseline_type": "git" if self._is_git_repo() else "mtime",
            "base_commit": base,
            "current_head": current_head,
            "changed_files": changed_files,
            "status_hash": status_hash,
            "is_dirty": is_dirty,
        }
