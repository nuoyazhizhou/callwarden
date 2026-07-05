"""
db_git.py
=========

代码知识图谱 Git 集成 Mixin 类。

提供 Git 历史导入、变更追踪、commit 关联等功能。
"""

from __future__ import annotations

import os
import subprocess
from typing import Any, Dict, List, Optional

from ..i18n import t


class GitMixin:
    """Git 集成功能 Mixin

    通过 self.conn 访问数据库连接，提供 Git 相关功能。
    """

    def import_git_history(self, max_commits: int = 100) -> Dict[str, Any]:
        """导入 Git 历史记录到数据库

        Args:
            max_commits: 最大导入 commit 数量

        Returns:
            导入结果统计
        """
        ws_id = self._get_active_workspace_id()
        workspace_root = getattr(self, 'workspace_root', None)
        if not workspace_root:
            ws = self.get_active_workspace()
            if ws:
                workspace_root = ws['root_path']
        
        if not workspace_root or not os.path.exists(os.path.join(workspace_root, '.git')):
            return {"success": False, "error": t("cli.messages.git_repo_not_found", default="Git repository not found"), "commits_imported": 0}

        try:
            result = subprocess.run(
                ["git", "log", f"--max-count={max_commits}",
                 "--format=%H|%s|%an|%ae|%ct"],
                cwd=workspace_root,
                capture_output=True,
                text=True,
                check=True,
            )
        except (subprocess.CalledProcessError, FileNotFoundError) as e:
            return {"success": False, "error": str(e), "commits_imported": 0}

        lines = result.stdout.strip().split('\n')
        if not lines or not lines[0]:
            return {"success": True, "commits_imported": 0}

        commits = []
        for line in lines:
            parts = line.split('|', 4)
            if len(parts) >= 5:
                commits.append({
                    'hash': parts[0],
                    'message': parts[1],
                    'author': parts[2],
                    'email': parts[3],
                    'timestamp': float(parts[4]),
                })

        imported = 0
        for commit in commits:
            try:
                self.conn.execute(
                    "INSERT OR IGNORE INTO git_commits (commit_hash, message, author, email, timestamp, workspace_id) VALUES (?, ?, ?, ?, ?, ?)",
                    (commit['hash'], commit['message'], commit['author'],
                     commit['email'], commit['timestamp'], ws_id),
                )
                imported += 1
            except Exception:
                pass

        self._import_git_file_changes(commits, workspace_root)
        self.conn.commit()

        return {
            "success": True,
            "commits_imported": imported,
            "total_commits": len(commits),
        }

    def _import_git_file_changes(self, commits: List[Dict], repo_root: str):
        """导入 Git 文件变更记录

        对每个 commit：
        1. 用 `git show --name-status` 获取文件级变更，写入 git_file_changes 表
        2. 对每个变更文件，用 `git show <commit> -- <file>` 获取 diff，
           通过行号范围匹配 symbols 表，提取符号级变更写入 git_symbol_changes 表

        Args:
            commits: commit 列表
            repo_root: 仓库根目录
        """
        ws_id = self._get_active_workspace_id()

        for commit in commits:
            commit_hash = commit['hash']
            try:
                result = subprocess.run(
                    ["git", "show", "--name-status", "--format=", commit_hash],
                    cwd=repo_root,
                    capture_output=True,
                    text=True,
                    check=True,
                )
            except subprocess.CalledProcessError:
                continue

            for line in result.stdout.strip().split('\n'):
                if not line.strip():
                    continue
                parts = line.split('\t')
                if len(parts) < 2:
                    continue

                change_type = parts[0][0] if parts[0] else 'M'
                file_path = parts[-1]
                abs_path = os.path.join(repo_root, file_path)
                norm_abs = os.path.normpath(abs_path).replace('\\', '/')

                cur = self.conn.execute(
                    "SELECT id FROM file_instances WHERE workspace_id = ? AND abs_path = ?",
                    (ws_id, norm_abs),
                )
                row = cur.fetchone()
                file_instance_id = row['id'] if row else 0

                if file_instance_id > 0:
                    self.conn.execute(
                        "INSERT OR IGNORE INTO git_file_changes (commit_hash, file_instance_id, change_type) VALUES (?, ?, ?)",
                        (commit_hash, file_instance_id, change_type),
                    )
                    # 提取符号级变更并写入 git_symbol_changes 表
                    # 复用 ImpactMixin.diff_to_symbol 的行号匹配逻辑（通过 git diff 提取变更行号）
                    self._extract_and_store_symbol_changes(
                        commit_hash, file_instance_id, file_path, repo_root
                    )

    def _extract_and_store_symbol_changes(
        self, commit_hash: str, file_instance_id: int, file_path: str, repo_root: str
    ) -> None:
        """提取单个 commit 中单个文件的符号级变更，写入 git_symbol_changes 表

        实现思路：
        1. 用 `git show <commit> -- <file>` 获取该文件在该 commit 的 diff
        2. 解析 diff 的 hunk header（@@ -old,n +new,m @@）获取变更行号范围
        3. 查询 symbols 表，找到行号范围与符号 start_line/end_line 重叠的符号
        4. 将符号变更写入 git_symbol_changes 表（commit_hash + symbol_hash + change_type）

        幂等性：用 INSERT OR IGNORE 避免重复导入。

        Args:
            commit_hash: commit 哈希
            file_instance_id: 文件实例 ID
            file_path: 文件相对路径（git 路径格式）
            repo_root: 仓库根目录
        """
        import re

        # 获取该 commit 对该文件的 diff
        try:
            diff_result = subprocess.run(
                ["git", "show", commit_hash, "--", file_path],
                cwd=repo_root,
                capture_output=True,
                text=True,
                check=True,
            )
        except subprocess.CalledProcessError:
            return

        diff_text = diff_result.stdout
        if not diff_text:
            return

        # 解析 diff hunks，提取变更行号范围
        # hunk 格式: @@ -old_start,old_len +new_start,new_len @@ context
        hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@")
        changed_ranges: List[tuple] = []  # [(new_start, new_end), ...]

        for line in diff_text.splitlines():
            m = hunk_re.match(line)
            if m:
                new_start = int(m.group(3))
                new_len = int(m.group(4)) if m.group(4) else 1
                new_end = new_start + max(new_len, 1) - 1
                changed_ranges.append((new_start, new_end))

        if not changed_ranges:
            return

        # 查询该文件的所有符号
        cur = self.conn.execute(
            """
            SELECT s.symbol_hash, s.qualified_name, s.start_line, s.end_line
            FROM symbols s
            WHERE s.file_instance_id = ?
            """,
            (file_instance_id,),
        )
        symbols = [dict(r) for r in cur.fetchall()]
        if not symbols:
            return

        # 匹配符号与变更行号范围（重叠即视为变更）
        # change_type 判定：diff 中纯新增 → added，纯删除 → deleted，混合 → modified
        ws_id = self._get_active_workspace_id()
        for sym in symbols:
            sym_start = sym["start_line"] or 0
            sym_end = sym["end_line"] or 0
            if sym_start == 0 or sym_end == 0:
                continue

            # 检查符号行范围是否与任何变更范围重叠
            is_changed = False
            for (chg_start, chg_end) in changed_ranges:
                if sym_start <= chg_end and sym_end >= chg_start:
                    is_changed = True
                    break

            if not is_changed:
                continue

            # 简化：所有变更统一标记为 modified（精确判定 added/deleted 需要解析 +/- 行）
            sym_change_type = "modified"

            # 写入 git_symbol_changes 表（幂等：INSERT OR IGNORE）
            self.conn.execute(
                "INSERT OR IGNORE INTO git_symbol_changes (commit_hash, symbol_hash, change_type, old_content, new_content) VALUES (?, ?, ?, ?, ?)",
                (commit_hash, sym["symbol_hash"], sym_change_type, "", ""),
            )

    def get_git_commits(self, limit: int = 20, offset: int = 0) -> List[Dict]:
        """获取 Git commit 列表

        Args:
            limit: 返回数量限制
            offset: 偏移量

        Returns:
            commit 列表
        """
        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute(
            "SELECT * FROM git_commits WHERE workspace_id = ? ORDER BY timestamp DESC LIMIT ? OFFSET ?",
            (ws_id, limit, offset),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_commit_changes(self, commit_hash: str) -> Dict[str, Any]:
        """获取指定 commit 的变更详情

        Args:
            commit_hash: commit 哈希值

        Returns:
            commit 详情和变更列表
        """
        ws_id = self._get_active_workspace_id()

        cur = self.conn.execute(
            "SELECT * FROM git_commits WHERE workspace_id = ? AND commit_hash = ?",
            (ws_id, commit_hash),
        )
        commit_row = cur.fetchone()
        if not commit_row:
            return {"commit": None, "file_changes": []}

        cur = self.conn.execute(
            """
            SELECT gfc.*, fi.rel_path, fi.abs_path
            FROM git_file_changes gfc
            LEFT JOIN file_instances fi ON gfc.file_instance_id = fi.id
            WHERE gfc.commit_hash = ?
            ORDER BY fi.rel_path
            """,
            (commit_hash,),
        )
        file_changes = [dict(row) for row in cur.fetchall()]

        return {
            "commit": dict(commit_row),
            "file_changes": file_changes,
        }

    def get_symbol_commit_history(self, symbol_hash: str, limit: int = 20) -> List[Dict]:
        """获取符号的 Git 变更历史

        Args:
            symbol_hash: 符号内容哈希
            limit: 返回数量限制

        Returns:
            符号变更的 commit 列表
        """
        cur = self.conn.execute(
            """
            SELECT gc.*, gsc.change_type
            FROM git_symbol_changes gsc
            JOIN git_commits gc ON gsc.commit_hash = gc.commit_hash
            WHERE gsc.symbol_hash = ?
            ORDER BY gc.timestamp DESC
            LIMIT ?
            """,
            (symbol_hash, limit),
        )
        return [dict(row) for row in cur.fetchall()]

    def get_git_stats(self) -> Dict[str, Any]:
        """获取 Git 集成统计信息

        Returns:
            Git 相关统计数据
        """
        ws_id = self._get_active_workspace_id()

        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM git_commits WHERE workspace_id = ?",
            (ws_id,),
        )
        commit_count = cur.fetchone()['cnt']

        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM git_file_changes gfc JOIN git_commits gc ON gfc.commit_hash = gc.commit_hash WHERE gc.workspace_id = ?",
            (ws_id,),
        )
        file_change_count = cur.fetchone()['cnt']

        cur = self.conn.execute(
            "SELECT change_type, COUNT(*) as cnt FROM git_file_changes gfc JOIN git_commits gc ON gfc.commit_hash = gc.commit_hash WHERE gc.workspace_id = ? GROUP BY change_type",
            (ws_id,),
        )
        change_types = {row['change_type']: row['cnt'] for row in cur.fetchall()}

        return {
            "commit_count": commit_count,
            "file_change_count": file_change_count,
            "change_types": change_types,
        }
