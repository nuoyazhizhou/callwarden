"""
db_ownership.py
===============

代码知识图谱文件所有权分析 Mixin 类。

提供 CODEOWNERS 解析、git blame 集成、文件负责人查询、模块所有权映射等功能。
帮助 AI Agent 在修改文件前快速定位"该问谁"。
"""

from __future__ import annotations

import os
import re
import subprocess
import time
from collections import defaultdict
from typing import Any, Dict, List, Optional

from ..i18n import t


class OwnershipMixin:
    """文件所有权分析功能 Mixin

    通过 self.conn 访问数据库连接，提供文件负责人识别能力。
    数据来源：
    1. CODEOWNERS 文件（显式声明，confidence=1.0）
    2. git blame / git log（最近修改者，confidence=0.7）
    综合两者给出最终负责人。
    """

    # CODEOWNERS 默认查找路径（相对 workspace_root）
    _CODEOWNERS_CANDIDATES = (
        ".github/CODEOWNERS",
        "docs/CODEOWNERS",
        "CODEOWNERS",
    )

    # git blame 来源的置信度
    _GIT_BLAME_CONFIDENCE = 0.7

    def _find_codeowners_file(self) -> Optional[str]:
        """在工作区中查找 CODEOWNERS 文件

        Returns:
            找到的文件绝对路径，未找到返回 None
        """
        workspace_root = getattr(self, "workspace_root", None)
        if not workspace_root:
            ws = self.get_active_workspace()
            if ws:
                workspace_root = ws["root_path"]
        if not workspace_root:
            return None

        for rel in self._CODEOWNERS_CANDIDATES:
            candidate = os.path.join(workspace_root, rel)
            if os.path.isfile(candidate):
                return candidate
        return None

    def parse_codeowners(self, file_path: Optional[str] = None) -> List[Dict[str, Any]]:
        """解析 CODEOWNERS 文件

        格式：每行 `路径模式 @owner1 @owner2`，# 开头为注释，空行忽略。
        路径模式支持 gitignore 风格：* / ** / ?。

        Args:
            file_path: CODEOWNERS 文件路径。为 None 时自动在工作区查找。

        Returns:
            规则列表 [{pattern, owners}]，按文件中出现顺序保留。
            文件不存在时返回空列表（不报错）。
        """
        if file_path is None:
            file_path = self._find_codeowners_file()
        if not file_path or not os.path.isfile(file_path):
            return []

        try:
            with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
        except OSError:
            return []

        rules: List[Dict[str, Any]] = []
        for raw_line in lines:
            # 去掉行尾换行
            line = raw_line.strip()
            # 跳过空行和注释
            if not line or line.startswith("#"):
                continue
            # 去掉行内注释（# 前需有空格分隔，避免误伤路径中的 #）
            comment_idx = line.find(" #")
            if comment_idx > 0:
                line = line[:comment_idx].strip()

            parts = line.split()
            if not parts:
                continue
            pattern = parts[0]
            owners = [p for p in parts[1:] if p.startswith("@") or "@" in p]
            # 没有任何 owner 也保留规则（可能仅用作占位）
            if not pattern:
                continue
            rules.append({"pattern": pattern, "owners": owners})
        return rules

    @staticmethod
    def _codeowners_pattern_to_regex(pattern: str) -> re.Pattern:
        """将 CODEOWNERS 路径模式编译为正则

        支持：
        - `**/` 匹配零或多层目录前缀（如 `**/*.py` 可匹配根目录的 `x.py`）
        - `**`  匹配任意字符（含 `/`），通常用于末尾通配
        - `*`   匹配除 `/` 之外任意字符（单层）
        - `?`   匹配除 `/` 之外单个字符
        - 末尾 `/` 表示目录前缀，匹配其下所有文件
        - 不以 `/` 开头时允许匹配任意目录前缀（basename 模式）

        Returns:
            编译后的正则对象（匹配路径相对 workspace_root，正斜杠）
        """
        # 末尾斜杠：目录前缀
        is_dir_prefix = pattern.endswith("/")

        # 转义正则特殊字符（保留 * 和 ? 后续处理）
        escaped = re.escape(pattern.rstrip("/") if is_dir_prefix else pattern)

        # 顺序敏感：先处理 **/（零或多层目录），再处理 **，最后处理单 *
        # re.escape 会把 * 和 ? 转义为 \\* / \\?，需要还原
        escaped = escaped.replace(r"\*\*/", "\0STARPATH\0")
        escaped = escaped.replace(r"\*\*", "\0DOUBLESTAR\0")
        escaped = escaped.replace(r"\*", "\0STAR\0")
        escaped = escaped.replace(r"\?", "\0QUESTION\0")

        # 替换为正则片段
        escaped = escaped.replace("\0STARPATH\0", "(?:.*/)?")
        escaped = escaped.replace("\0DOUBLESTAR\0", ".*")
        escaped = escaped.replace("\0STAR\0", "[^/]*")
        escaped = escaped.replace("\0QUESTION\0", "[^/]")

        if is_dir_prefix:
            # 目录前缀：匹配该目录下任意文件（锚定到根，去前导 /）
            regex = "^" + escaped.lstrip("/") + "(/.*)?$"
        elif pattern.startswith("/"):
            # 绝对路径模式：从根开始
            regex = "^" + escaped.lstrip("/") + "(/.*)?$"
        else:
            # 相对模式：匹配任意目录前缀（basename 风格）
            regex = "(^|.*/)" + escaped + "(/.*)?$"

        return re.compile(regex)

    def _match_codeowners_rule(self, rel_path: str, rules: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
        """对单个文件路径匹配 CODEOWNERS 规则

        CODEOWNERS 语义：最后一条匹配的规则生效（覆盖前面的）。

        Args:
            rel_path: 相对 workspace_root 的路径（正斜杠）
            rules: parse_codeowners 返回的规则列表

        Returns:
            最后匹配的规则 dict，无匹配返回 None
        """
        matched: Optional[Dict[str, Any]] = None
        for rule in rules:
            pattern = rule["pattern"]
            try:
                regex = self._codeowners_pattern_to_regex(pattern)
            except re.error:
                continue
            if regex.search(rel_path):
                matched = rule
        return matched

    def import_ownership_from_codeowners(self) -> Dict[str, Any]:
        """从 CODEOWNERS 文件导入所有权到数据库

        流程：
        1. 解析 CODEOWNERS 文件
        2. 遍历当前工作区的所有 file_instance
        3. 对每个文件匹配规则（最后匹配生效）
        4. 写入 / 更新 file_ownership 表（source='codeowners'）
        5. 保留已有 git blame 信息（last_commit_*），仅更新 owner/source/confidence

        Returns:
            统计字典 {rules_count, files_total, files_matched, files_updated}
        """
        ws_id = self._get_active_workspace_id()
        rules = self.parse_codeowners()
        now = time.time()

        # 预编译正则提升性能
        compiled_rules: List[Dict[str, Any]] = []
        for rule in rules:
            try:
                compiled_rules.append({
                    "pattern": rule["pattern"],
                    "owners": rule["owners"],
                    "regex": self._codeowners_pattern_to_regex(rule["pattern"]),
                })
            except re.error:
                continue

        # 获取所有文件实例
        cur = self.conn.execute(
            "SELECT id, rel_path FROM file_instances WHERE workspace_id = ? AND status != 'archived'",
            (ws_id,),
        )
        files = [dict(row) for row in cur.fetchall()]

        files_matched = 0
        files_updated = 0

        for fi in files:
            rel_path = (fi["rel_path"] or "").replace("\\", "/")
            if not rel_path:
                continue

            matched_rule: Optional[Dict[str, Any]] = None
            for rule in compiled_rules:
                if rule["regex"].search(rel_path):
                    matched_rule = rule

            if not matched_rule or not matched_rule["owners"]:
                continue

            files_matched += 1
            # 多个 owner 时取第一个作为主负责人
            primary_owner = matched_rule["owners"][0]
            file_instance_id = fi["id"]

            # 检查是否已有记录（保留 last_commit_* 字段）
            cur = self.conn.execute(
                "SELECT id, last_commit_hash, last_commit_author, last_commit_time FROM file_ownership WHERE file_instance_id = ?",
                (file_instance_id,),
            )
            existing = cur.fetchone()

            if existing:
                self.conn.execute(
                    """
                    UPDATE file_ownership
                    SET owner = ?, source = 'codeowners', confidence = 1.0, updated_at = ?
                    WHERE id = ?
                    """,
                    (primary_owner, now, existing["id"]),
                )
                files_updated += 1
            else:
                self.conn.execute(
                    """
                    INSERT INTO file_ownership
                        (file_instance_id, owner, source, confidence, last_commit_hash,
                         last_commit_author, last_commit_time, updated_at)
                    VALUES (?, ?, 'codeowners', 1.0, ?, ?, ?, ?)
                    """,
                    (file_instance_id, primary_owner, "", "", None, now),
                )
                files_updated += 1

        self.conn.commit()

        return {
            "rules_count": len(rules),
            "files_total": len(files),
            "files_matched": files_matched,
            "files_updated": files_updated,
        }

    def import_ownership_from_git_blame(self) -> Dict[str, Any]:
        """从 git log 导入每个文件最近一次提交者信息

        对每个 file_instance 执行 `git log -1 --format=... -- <file>` 获取
        最近一次 commit 的 hash、作者、时间，写入 file_ownership 表：
        - 若已有 codeowners 记录：仅更新 last_commit_* 字段，保留 owner/source
        - 若无记录：以最近提交者作为 owner，source='git_blame'，confidence=0.7

        Returns:
            统计字典 {files_total, files_processed, files_updated, errors}
        """
        ws_id = self._get_active_workspace_id()
        workspace_root = getattr(self, "workspace_root", None)
        if not workspace_root:
            ws = self.get_active_workspace()
            if ws:
                workspace_root = ws["root_path"]
        if not workspace_root or not os.path.exists(os.path.join(workspace_root, ".git")):
            return {"success": False, "error": t("cli.messages.git_repo_not_found", default="Git repository not found"), "files_total": 0,
                    "files_processed": 0, "files_updated": 0, "errors": 0}

        cur = self.conn.execute(
            "SELECT id, rel_path, abs_path FROM file_instances WHERE workspace_id = ? AND status != 'archived'",
            (ws_id,),
        )
        files = [dict(row) for row in cur.fetchall()]

        files_processed = 0
        files_updated = 0
        errors = 0
        now = time.time()

        for fi in files:
            rel_path = (fi["rel_path"] or "").replace("\\", "/")
            if not rel_path:
                continue

            files_processed += 1

            try:
                result = subprocess.run(
                    ["git", "log", "-1", "--format=%H|%an|%ae|%ct", "--", rel_path],
                    cwd=workspace_root,
                    capture_output=True,
                    text=True,
                    check=False,
                )
            except (subprocess.CalledProcessError, FileNotFoundError):
                errors += 1
                continue

            output = result.stdout.strip()
            if not output:
                continue

            parts = output.split("|", 3)
            if len(parts) < 4:
                continue
            commit_hash, author, _email, ts_str = parts
            try:
                commit_time = float(ts_str)
            except ValueError:
                commit_time = 0.0

            file_instance_id = fi["id"]

            cur = self.conn.execute(
                "SELECT id, source FROM file_ownership WHERE file_instance_id = ?",
                (file_instance_id,),
            )
            existing = cur.fetchone()

            if existing:
                # 已有记录：保留 owner/source，仅更新 last_commit_* 字段
                self.conn.execute(
                    """
                    UPDATE file_ownership
                    SET last_commit_hash = ?, last_commit_author = ?,
                        last_commit_time = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (commit_hash, author, commit_time, now, existing["id"]),
                )
                files_updated += 1
            else:
                # 无记录：以最近提交者作为 owner
                self.conn.execute(
                    """
                    INSERT INTO file_ownership
                        (file_instance_id, owner, source, confidence, last_commit_hash,
                         last_commit_author, last_commit_time, updated_at)
                    VALUES (?, ?, 'git_blame', ?, ?, ?, ?, ?)
                    """,
                    (file_instance_id, author, self._GIT_BLAME_CONFIDENCE,
                     commit_hash, author, commit_time, now),
                )
                files_updated += 1

        self.conn.commit()

        return {
            "success": True,
            "files_total": len(files),
            "files_processed": files_processed,
            "files_updated": files_updated,
            "errors": errors,
        }

    def who_to_ask(self, file_path: str) -> Optional[Dict[str, Any]]:
        """查询文件负责人

        综合 CODEOWNERS 和 git blame 信息，给出该文件该问谁。

        Args:
            file_path: 文件路径（相对或绝对，统一转为正斜杠匹配）

        Returns:
            负责人信息字典：
            {file_path, owner, source, confidence,
             last_commit_author, last_commit_time, last_commit_hash}
            未找到文件或无所有权记录返回 None。
        """
        ws_id = self._get_active_workspace_id()

        # 标准化路径
        if os.path.isabs(file_path):
            abs_path = os.path.normpath(file_path).replace("\\", "/")
            cur = self.conn.execute(
                """
                SELECT fo.owner, fo.source, fo.confidence,
                       fo.last_commit_hash, fo.last_commit_author, fo.last_commit_time,
                       fi.rel_path
                FROM file_ownership fo
                JOIN file_instances fi ON fo.file_instance_id = fi.id
                WHERE fi.workspace_id = ? AND fi.abs_path = ?
                LIMIT 1
                """,
                (ws_id, abs_path),
            )
        else:
            rel_path = file_path.replace("\\", "/")
            cur = self.conn.execute(
                """
                SELECT fo.owner, fo.source, fo.confidence,
                       fo.last_commit_hash, fo.last_commit_author, fo.last_commit_time,
                       fi.rel_path
                FROM file_ownership fo
                JOIN file_instances fi ON fo.file_instance_id = fi.id
                WHERE fi.workspace_id = ? AND fi.rel_path = ?
                LIMIT 1
                """,
                (ws_id, rel_path),
            )

        row = cur.fetchone()
        if not row:
            return None

        return {
            "file_path": row["rel_path"],
            "owner": row["owner"],
            "source": row["source"],
            "confidence": row["confidence"],
            "last_commit_author": row["last_commit_author"] or "",
            "last_commit_time": row["last_commit_time"],
            "last_commit_hash": row["last_commit_hash"] or "",
        }

    def get_ownership_map(self, module_filter: str = "") -> List[Dict[str, Any]]:
        """获取模块所有权映射

        按 module_path 分组统计每个模块的负责人分布。

        Args:
            module_filter: 模块路径前缀过滤（空字符串表示全部）

        Returns:
            模块列表，按文件数降序：
            [{module, primary_owner, file_count, owners: [{name, file_count}]}]
        """
        ws_id = self._get_active_workspace_id()

        sql = """
            SELECT fi.module_path, fo.owner
            FROM file_ownership fo
            JOIN file_instances fi ON fo.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
        """
        params: list = [ws_id]
        if module_filter:
            sql += " AND fi.module_path LIKE ?"
            params.append(f"{module_filter}%")

        cur = self.conn.execute(sql, params)

        # 模块 -> owner -> 文件数
        module_owners: Dict[str, Dict[str, int]] = defaultdict(lambda: defaultdict(int))
        module_total: Dict[str, int] = defaultdict(int)

        for row in cur:
            module = row["module_path"] or "(未分类)"
            owner = row["owner"] or "(未知)"
            module_owners[module][owner] += 1
            module_total[module] += 1

        results = []
        for module, owner_counts in module_owners.items():
            # 主负责人：文件数最多的 owner
            sorted_owners = sorted(owner_counts.items(), key=lambda x: x[1], reverse=True)
            primary_owner = sorted_owners[0][0] if sorted_owners else "(未知)"

            owners_list = [
                {"name": name, "file_count": cnt}
                for name, cnt in sorted_owners
            ]
            results.append({
                "module": module,
                "primary_owner": primary_owner,
                "file_count": module_total[module],
                "owners": owners_list,
            })

        # 按文件数降序
        results.sort(key=lambda x: x["file_count"], reverse=True)
        return results
