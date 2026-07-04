"""incremental.py
================

增量分析模块：基于 git diff 只分析本次变更涉及的文件。

在 CI / PR 场景下，全量构建代码知识图谱开销过大，因此通过 git diff
拿到变更文件清单，仅对这些文件做刷新与分析，从而大幅降低 CI 耗时。
"""

from __future__ import annotations

import re
import subprocess
from typing import Dict, List


# git diff --stat 最后一行的汇总格式，例如：
#   " 3 files changed, 10 insertions(+), 5 deletions(-)"
# 也可能只含 insertions 或只含 deletions
_FILES_RE = re.compile(r"(\d+)\s+files?\s+changed", re.IGNORECASE)
_INSERT_RE = re.compile(r"(\d+)\s+insertions?\(\+\)", re.IGNORECASE)
_DELETE_RE = re.compile(r"(\d+)\s+deletions?\(-\)", re.IGNORECASE)


class IncrementalAnalyzer:
    """增量分析器：基于 git diff 限定分析范围

    Args:
        db: CodeGraphDB 实例，用于刷新文件图谱
    """

    def __init__(self, db):
        """初始化增量分析器

        Args:
            db: CodeGraphDB 实例，用于刷新文件图谱
        """
        self.db = db

    def get_changed_files(self, base_branch: str = "main", head: str = "HEAD") -> List[str]:
        """获取 base_branch...head 范围内变更的文件列表

        使用 `git diff --name-only` 获取，仅返回文件路径，不含 diff 内容。

        Args:
            base_branch: 基准分支（默认 main）
            head: 目标提交（默认 HEAD）

        Returns:
            变更文件路径列表（按 git 输出顺序），失败返回空列表
        """
        # shell=False 防止 shell 注入；三点 diff 取 base 与 head 的合并基础
        result = subprocess.run(
            ["git", "diff", "--name-only", f"{base_branch}...{head}"],
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
        if result.returncode != 0:
            # git diff 失败（如 base 分支不存在）时返回空列表，不抛异常
            return []
        # 按行切分并过滤空行
        return [line.strip() for line in result.stdout.splitlines() if line.strip()]

    def analyze_changed_files(self, base_branch: str = "main", head: str = "HEAD") -> Dict:
        """只分析变更文件：刷新图谱

        Args:
            base_branch: 基准分支
            head: 目标提交

        Returns:
            {"total_changed": N, "refreshed": N, "failed": N, "files": [...]}
        """
        changed_files = self.get_changed_files(base_branch=base_branch, head=head)
        refreshed = 0
        failed = 0
        files_info: List[Dict] = []

        # 仅当 db 上存在 refresh_file 方法时才调用（防御式编程）
        refresh_fn = getattr(self.db, "refresh_file", None)

        for file_path in changed_files:
            entry: Dict = {"file": file_path, "status": "skipped"}
            if refresh_fn is None:
                # db 不支持单文件刷新，整体跳过
                files_info.append(entry)
                continue
            try:
                refresh_fn(file_path)
                refreshed += 1
                entry["status"] = "refreshed"
            except Exception as e:
                failed += 1
                entry["status"] = "failed"
                entry["error"] = str(e)
            files_info.append(entry)

        return {
            "total_changed": len(changed_files),
            "refreshed": refreshed,
            "failed": failed,
            "files": files_info,
        }

    def get_diff_stat(self, base_branch: str = "main", head: str = "HEAD") -> Dict:
        """获取 base_branch...head 的变更统计

        解析 `git diff --stat` 最后一行的汇总信息。

        Args:
            base_branch: 基准分支
            head: 目标提交

        Returns:
            {"files_changed": N, "insertions": N, "deletions": N}
            失败时各项均为 0
        """
        result = subprocess.run(
            ["git", "diff", "--stat", f"{base_branch}...{head}"],
            capture_output=True,
            text=True,
            timeout=30,
            shell=False,
        )
        if result.returncode != 0:
            return {"files_changed": 0, "insertions": 0, "deletions": 0}

        # 汇总信息在最后一行（"--stat" 输出的 summary 行）
        summary_line = ""
        for line in result.stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            summary_line = line

        files_changed = 0
        insertions = 0
        deletions = 0

        m = _FILES_RE.search(summary_line)
        if m:
            files_changed = int(m.group(1))
        m = _INSERT_RE.search(summary_line)
        if m:
            insertions = int(m.group(1))
        m = _DELETE_RE.search(summary_line)
        if m:
            deletions = int(m.group(1))

        return {
            "files_changed": files_changed,
            "insertions": insertions,
            "deletions": deletions,
        }
