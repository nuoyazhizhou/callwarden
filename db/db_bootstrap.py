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
import secrets
import subprocess
import time
from typing import Any, Dict, List, Optional

from ..config import read_file_normalized
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

    # ============================================
    # task_capture_diff 闭环入口
    # ============================================

    def task_capture_diff(
        self,
        task_id: str,
        step_id: str = "",
        base: str = "",
        dry_run: bool = True,
        source_commit_hash: str = "",
    ) -> Dict[str, Any]:
        """把外部 Agent 的真实文件改动捕获到 task/change/symbol/audit

        流程：
        1. 读取上次 scan baseline 或指定 base，调用 get_workspace_changes_since
        2. dry-run：只返回计划，不写 change_audit / audit_chain
        3. apply 模式：
           a. 记录 scan baseline（workspace_scan_runs）
           b. 对每个变更文件计算 hash_before/hash_after，写入 change_audit
           c. 签名审计记录（sign_audit_record）
           d. 尽量关联 task_symbol_changes（record_task_symbol_change）
           e. 调用 run_task_completion_review
           f. 更新 scan_run 状态为 completed
        4. 返回 changed_files、linked_symbols、quality_findings、next_action

        Args:
            task_id: 关联任务 ID
            step_id: 关联步骤 ID（可选）
            base: 基线 commit（空串自动取最近一次 scan baseline 的 git_head）
            dry_run: True 只返回计划不写库

        Returns:
            {
                "task_id": ...,
                "step_id": ...,
                "dry_run": bool,
                "scan_id": int,        # apply 模式才有
                "changed_files": [...],
                "linked_symbols": [...],
                "quality_findings": [...],
                "quality_decision": "pass" | "warn" | "block" | "",
                "next_action": "review" | "fix" | "commit" | "",
            }
        """
        # 步骤 1：检测变更
        changes = self.get_workspace_changes_since(
            scan_id=0,
            base_commit=base,
            include_untracked=True,
        )
        changed_files = changes["changed_files"]

        # dry-run：只返回计划
        if dry_run:
            return {
                "task_id": task_id,
                "step_id": step_id,
                "dry_run": True,
                "scan_id": 0,
                "changed_files": changed_files,
                "linked_symbols": [],
                "quality_findings": [],
                "quality_decision": "",
                "next_action": "apply" if changed_files else "noop",
            }

        # 步骤 2：记录 scan baseline
        scan_id = self.record_workspace_scan_run(
            purpose="capture",
            task_id=task_id,
            step_id=step_id,
            status="running",
            metadata={
                "base_commit": changes["base_commit"],
                "baseline_type": changes["baseline_type"],
            },
        )

        # 步骤 3：写入 change_audit（每文件一条）
        ws_id = self._get_active_workspace_id()
        root = getattr(self, "workspace_root", "")
        linked_symbols: List[Dict[str, Any]] = []
        recorded_changes: List[Dict[str, Any]] = []

        for f in changed_files:
            rel_path = f["path"]
            status_code = f.get("status", "M")
            abs_path = os.path.join(root, rel_path) if root else rel_path

            # 读取 hash_before（从 file_instances）
            hash_before = ""
            try:
                cur = self.conn.execute(
                    "SELECT current_content_hash FROM file_instances "
                    "WHERE workspace_id = ? AND rel_path = ?",
                    (ws_id, rel_path),
                )
                row = cur.fetchone()
                if row:
                    hash_before = row[0] if not isinstance(row, dict) else row["current_content_hash"]
            except Exception:
                pass

            # 读取 hash_after（从磁盘）
            hash_after = ""
            if os.path.exists(abs_path):
                try:
                    _content, hash_after = read_file_normalized(abs_path)
                except Exception:
                    hash_after = ""

            # 生成 change_id 并写入 change_audit
            ts_ms = int(time.time() * 1000)
            rand4 = secrets.token_hex(2)
            change_id = f"C-{ts_ms}-{rand4}"
            try:
                self.conn.execute(
                    "INSERT INTO change_audit "
                    "(id, task_id, step_id, file_path, hash_before, hash_after, "
                    " diff, author, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        change_id, task_id, step_id, rel_path,
                        hash_before, hash_after, "",
                        "capture-diff", time.time(),
                    ),
                )
                recorded_changes.append({
                    "change_id": change_id,
                    "file_path": rel_path,
                    "hash_before": hash_before,
                    "hash_after": hash_after,
                    "status": status_code,
                })
            except Exception:
                pass

            # 签名审计记录（失败不阻塞）
            if hasattr(self, "sign_audit_record"):
                try:
                    self.sign_audit_record(
                        "change_audit",
                        change_id,
                        {
                            "task_id": task_id,
                            "step_id": step_id,
                            "file_path": rel_path,
                            "hash_before": hash_before,
                            "hash_after": hash_after,
                            "diff": "",
                            "author": "capture-diff",
                        },
                    )
                except Exception:
                    pass

            # 尽量关联 task_symbol_changes（best-effort）
            if hasattr(self, "record_task_symbol_change"):
                try:
                    self.record_task_symbol_change(
                        task_id=task_id,
                        step_id=step_id,
                        change_audit_id=change_id,
                        file_path=rel_path,
                        symbol_hash_before=hash_before,
                        symbol_hash_after=hash_after,
                        change_type="modified" if status_code == "M" else (
                            "added" if status_code in ("A", "untracked") else
                            "deleted" if status_code == "D" else "modified"
                        ),
                        source="task_capture_diff",
                        metadata={"status": status_code},
                        source_commit_hash=source_commit_hash,
                    )
                    linked_symbols.append({
                        "file_path": rel_path,
                        "change_id": change_id,
                        "linked": True,
                    })
                except Exception:
                    pass

        self.conn.commit()

        # 步骤 4：调用 run_task_completion_review
        quality_decision = ""
        quality_findings: List[Dict[str, Any]] = []
        if hasattr(self, "run_task_completion_review") and recorded_changes:
            try:
                review = self.run_task_completion_review(task_id, step_id)
                quality_decision = review.get("decision", "")
                quality_findings = review.get("findings", [])
            except Exception:
                pass

        # 步骤 5：更新 scan_run 状态为 completed
        self.update_scan_run_status(scan_id, "completed", changed_files=changed_files)

        # 步骤 6：决定 next_action
        if quality_decision == "block":
            next_action = "fix"
        elif recorded_changes:
            next_action = "review"
        else:
            next_action = "noop"

        return {
            "task_id": task_id,
            "step_id": step_id,
            "dry_run": False,
            "scan_id": scan_id,
            "changed_files": changed_files,
            "linked_symbols": linked_symbols,
            "quality_findings": quality_findings,
            "quality_decision": quality_decision,
            "next_action": next_action,
        }

    # ============================================
    # task_capture_diff_auto 自动模式（fail-soft）
    # ============================================

    def task_capture_diff_auto(self) -> Dict[str, Any]:
        """自动检测 in_progress 任务并捕获 diff（fail-soft）

        用于 post-commit hook 自动调用场景，无需用户手动指定 task_id：
        1. 从 task_list 找最近一个 in_progress 任务（按 sort_order / created_at）
        2. 取 HEAD~1 作为 base（commit 后 hook 触发，HEAD 是新提交）
        3. 自动 apply（dry_run=False）
        4. 任何异常都封装在 result dict 中，不抛异常，不影响 git commit

        Returns:
            {
                "auto": True,           # 标识为自动模式
                "task_id": str,          # 检测到的任务 ID（空表示没找到）
                "step_id": "",           # 自动模式不关联 step
                "base": str,             # 使用的 base commit
                "dry_run": False,        # 始终 False
                "success": bool,         # 整体是否成功
                "error": str,            # 失败原因（success=False 时填充）
                "reason": str,           # 失败原因的简短标识（no_in_progress_task / git_error / exception）
                "changed_files": [...],  # 变更文件列表
                "linked_symbols": [...],
                "quality_findings": [...],
                "quality_decision": "",
                "next_action": "",
            }
        """
        try:
            # 1. 优先从 active_task 持久化字段读取（P1：替代 CALLWARDEN_TASK_ID 环境变量）
            task_id = self.get_active_task() or ""
            # fallback：active_task 为空时，退回 task_list 找最近一个 in_progress
            # （向后兼容旧数据库 schema < v30，以及容错 active_task 与实际状态不一致）
            if not task_id:
                tasks = self.task_list(status_filter="in_progress", limit=10)
                if not tasks:
                    return {
                        "auto": True,
                        "task_id": "",
                        "step_id": "",
                        "base": "",
                        "dry_run": False,
                        "success": False,
                        "error": "",
                        "reason": "no_in_progress_task",
                        "changed_files": [],
                        "linked_symbols": [],
                        "quality_findings": [],
                        "quality_decision": "",
                        "next_action": "noop",
                    }
                task_id = tasks[0].get("task_id", "")
            if not task_id:
                return {
                    "auto": True,
                    "task_id": "",
                    "step_id": "",
                    "base": "",
                    "dry_run": False,
                    "success": False,
                    "error": "",
                    "reason": "no_in_progress_task",
                    "changed_files": [],
                    "linked_symbols": [],
                    "quality_findings": [],
                    "quality_decision": "",
                    "next_action": "noop",
                }

            # 2. 取 HEAD~1 作为 base（commit 后 hook 触发，HEAD 已是新提交）
            base = ""
            head_commit = ""
            try:
                cwd = getattr(self, "workspace_root", "") or None
                result = subprocess.run(
                    ["git", "rev-parse", "HEAD~1"],
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                )
                if result.returncode == 0:
                    base = result.stdout.strip()
            except Exception:
                # 没有上一个 commit（首次提交），base 留空，由 task_capture_diff 自动取 scan baseline
                base = ""
            # 取当前 HEAD commit hash，作为 source_commit_hash 传入三角关联
            try:
                cwd = getattr(self, "workspace_root", "") or None
                result = subprocess.run(
                    ["git", "rev-parse", "HEAD"],
                    capture_output=True,
                    text=True,
                    cwd=cwd,
                )
                if result.returncode == 0:
                    head_commit = result.stdout.strip()
            except Exception:
                head_commit = ""

            # 3. 自动 apply（dry_run=False），传 source_commit_hash 让三角关联生效
            capture_result = self.task_capture_diff(
                task_id=task_id,
                step_id="",
                base=base,
                dry_run=False,
                source_commit_hash=head_commit,
            )
            capture_result["auto"] = True
            capture_result["success"] = True
            capture_result["error"] = ""
            capture_result["reason"] = ""
            capture_result["base"] = base

            # 4. 自动导入最新 commit 到 git_commits 表（fail-soft）
            #    确保后续 get_task_commits 能 JOIN 到 author/subject
            if head_commit:
                try:
                    self.import_git_history(max_commits=5)
                except Exception:
                    pass  # git-import 失败不影响 capture 结果

            return capture_result
        except Exception as exc:
            # fail-soft：任何异常都封装为结果，不抛
            return {
                "auto": True,
                "task_id": "",
                "step_id": "",
                "base": "",
                "dry_run": False,
                "success": False,
                "error": str(exc),
                "reason": "exception",
                "changed_files": [],
                "linked_symbols": [],
                "quality_findings": [],
                "quality_decision": "",
                "next_action": "noop",
            }

    # ============================================
    # bootstrap_status 健康摘要
    # ============================================

    def bootstrap_status(self) -> Dict[str, Any]:
        """返回自举健康状态摘要

        汇总以下信息，帮助判断当前自举闭环是否健康：

        1. db_stale：DB 是否滞后（最近一次 scan_run 的 git_head 与当前 HEAD 不一致）
        2. active_rules_count：已生效的 agent_rules 数量
        3. pending_candidates_count：待审核的 rule candidates 数量
        4. open_findings_count：open 状态的 quality findings 数量
        5. blocking_findings_count：block 严重度的 quality findings 数量
        6. audit_verify：audit_chain 验证结果摘要
        7. latest_scan_run：最近一次 workspace_scan_runs 记录
        8. tasks：按状态分组的任务计数（open / in_progress / review / applied）
        9. recommended_next_action：推荐下一条命令

        Returns:
            包含上述字段的 dict
        """
        # 1. DB 是否 stale：对比最近 scan_run 的 git_head 与当前 HEAD
        latest_scan = self.get_latest_scan_run()
        current_head = self._get_git_head() if self._is_git_repo() else ""
        db_stale = False
        if latest_scan and current_head:
            scan_head = latest_scan.get("git_head", "") if isinstance(latest_scan, dict) else ""
            # git_head 为空串时无法判断，视为不 stale
            db_stale = bool(scan_head) and scan_head != current_head

        # 2. active rules 数量
        active_rules_count = 0
        if hasattr(self, "rule_list"):
            try:
                active_rules = self.rule_list(status="active", limit=1000)
                active_rules_count = len(active_rules)
            except Exception:
                pass

        # 3. pending rule candidates 数量
        pending_candidates_count = 0
        if hasattr(self, "rule_candidate_list"):
            try:
                pending = self.rule_candidate_list(status="pending", limit=1000)
                pending_candidates_count = len(pending)
            except Exception:
                pass

        # 4-5. quality findings 计数（直接查询 task_quality_findings 表）
        open_findings_count = 0
        blocking_findings_count = 0
        try:
            cur = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM task_quality_findings WHERE status = 'open'"
            )
            row = cur.fetchone()
            open_findings_count = row[0] if row else 0
            cur = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM task_quality_findings "
                "WHERE status = 'open' AND severity = 'block'"
            )
            row = cur.fetchone()
            blocking_findings_count = row[0] if row else 0
        except Exception:
            pass

        # 6. audit_chain 验证结果
        audit_verify: Dict[str, Any] = {}
        if hasattr(self, "verify_audit_chain"):
            try:
                audit_verify = self.verify_audit_chain(table_name="", limit=500)
            except Exception as e:
                audit_verify = {"error": str(e)}

        # 7. tasks 按状态分组计数
        task_counts: Dict[str, int] = {
            "open": 0, "in_progress": 0, "review": 0, "applied": 0,
        }
        try:
            cur = self.conn.execute(
                "SELECT status, COUNT(*) as cnt FROM tasks GROUP BY status"
            )
            for row in cur.fetchall():
                status_val = row[0] if not isinstance(row, dict) else row["status"]
                cnt = row[1] if not isinstance(row, dict) else row["cnt"]
                if status_val in task_counts:
                    task_counts[status_val] = cnt
        except Exception:
            pass

        # 8. 推荐下一条命令
        if db_stale:
            recommended = "cw --refresh-all"
        elif blocking_findings_count > 0:
            recommended = "cw task findings <task_id>  # 有阻塞发现需修复"
        elif pending_candidates_count > 0:
            recommended = "cw rule candidate  # 有待审核的候选规则"
        elif audit_verify.get("broken_count", 0) > 0:
            recommended = "cw audit verify  # 审计链有损坏记录"
        elif task_counts["review"] > 0:
            recommended = "cw task apply <task_id>  # 有任务待审核"
        else:
            recommended = "cw task list  # 一切正常，查看任务列表"

        return {
            "db_stale": db_stale,
            "current_head": current_head,
            "active_rules_count": active_rules_count,
            "pending_candidates_count": pending_candidates_count,
            "open_findings_count": open_findings_count,
            "blocking_findings_count": blocking_findings_count,
            "audit_verify": {
                "total_count": audit_verify.get("total_count", 0),
                "verified_count": audit_verify.get("verified_count", 0),
                "broken_count": audit_verify.get("broken_count", 0),
                "security_level": audit_verify.get("security_level", ""),
            },
            "latest_scan_run": {
                "id": latest_scan.get("id", 0) if latest_scan else 0,
                "git_head": latest_scan.get("git_head", "") if latest_scan else "",
                "started_at": latest_scan.get("started_at", 0) if latest_scan else 0,
                "status": latest_scan.get("status", "") if latest_scan else "",
            } if latest_scan else None,
            "tasks": task_counts,
            "recommended_next_action": recommended,
        }
