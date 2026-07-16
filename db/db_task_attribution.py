"""
db_task_attribution.py
======================

任务-符号变更归因层。

事实层仍由 file_versions / file_symbol_versions / symbol_contents 表表达；
本模块只记录一次任务或步骤为什么导致某个文件/符号版本发生变化。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, List, Optional

from ..i18n import t


class TaskAttributionMixin:
    """任务-符号变更归因 Mixin"""

    def _rel_path_for_attribution(self, file_path: str) -> str:
        if os.path.isabs(file_path):
            try:
                return os.path.relpath(file_path, self.workspace_root).replace("\\", "/")
            except ValueError:
                return file_path.replace("\\", "/")
        return file_path.replace("\\", "/")

    def _infer_in_progress_step_id(self, task_id: str) -> str:
        """为 task_id 推断当前 in_progress 步骤，兼容父子任务树"""
        if not task_id:
            return ""
        try:
            cur = self.conn.execute(
                """
                WITH RECURSIVE task_tree(id) AS (
                    SELECT id FROM tasks WHERE id = ?
                    UNION ALL
                    SELECT t.id FROM tasks t JOIN task_tree tt ON t.parent_id = tt.id
                )
                SELECT ts.id
                FROM task_steps ts
                JOIN task_tree tt ON ts.task_id = tt.id
                WHERE ts.status = 'in_progress'
                ORDER BY ts.created_at DESC
                LIMIT 1
                """,
                (task_id,),
            )
            row = cur.fetchone()
            return row["id"] if row else ""
        except Exception:
            return ""

    def record_task_symbol_change(
        self,
        task_id: str,
        file_path: str,
        step_id: str = "",
        edit_audit_id: int = 0,
        change_audit_id: str = "",
        qualified_name: str = "",
        symbol_name: str = "",
        symbol_hash_before: str = "",
        symbol_hash_after: str = "",
        change_type: str = "modified",
        source: str = "manual",
        metadata: Optional[Dict[str, Any]] = None,
        source_commit_hash: str = "",
    ) -> Dict[str, Any]:
        """记录一次任务到文件/符号变化的归因

        Args:
            source_commit_hash: 引入此次变更的 git commit hash（可选）。
                填写后可通过 get_task_commits / get_commit_tasks 查询 task↔commit 关联。
                post-commit hook 自动调用时应传当前 HEAD commit hash。
        """
        if not task_id:
            return {"success": False, "error": t("cli.messages.attribution_task_id_required", default="task_id is required")}
        if not file_path:
            return {"success": False, "error": t("cli.messages.attribution_file_path_required", default="file_path is required")}

        ws_id = self._get_active_workspace_id() if hasattr(self, "_get_active_workspace_id") else None
        rel_path = self._rel_path_for_attribution(file_path)
        if not step_id:
            step_id = self._infer_in_progress_step_id(task_id)

        meta = json.dumps(metadata or {}, ensure_ascii=False, sort_keys=True)
        now = time.time()
        cur = self.conn.execute(
            """
            INSERT INTO task_symbol_changes
                (workspace_id, task_id, step_id, edit_audit_id, change_audit_id,
                 file_path, qualified_name, symbol_name, symbol_hash_before,
                 symbol_hash_after, change_type, source, source_commit_hash,
                 metadata, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ws_id,
                task_id,
                step_id,
                edit_audit_id,
                change_audit_id,
                rel_path,
                qualified_name,
                symbol_name,
                symbol_hash_before,
                symbol_hash_after,
                change_type or "modified",
                source or "manual",
                source_commit_hash or "",
                meta,
                now,
            ),
        )
        change_id_int = cur.lastrowid
        self.conn.commit()
        # 写入审计签名链（失败不阻塞主流程）
        if hasattr(self, "sign_audit_record"):
            try:
                self.sign_audit_record(
                    "task_symbol_changes",
                    str(change_id_int),
                    {
                        "task_id": task_id,
                        "step_id": step_id,
                        "edit_audit_id": edit_audit_id,
                        "change_audit_id": change_audit_id,
                        "file_path": rel_path,
                        "qualified_name": qualified_name,
                        "symbol_name": symbol_name,
                        "symbol_hash_before": symbol_hash_before,
                        "symbol_hash_after": symbol_hash_after,
                        "change_type": change_type or "modified",
                        "source": source or "manual",
                        "metadata": meta,
                    },
                )
            except Exception:
                pass
        return {"success": True, "id": change_id_int}

    def record_edit_attribution(self, audit_id: int, step_id: str = "") -> Optional[Dict[str, Any]]:
        """为 file_edit_audit 记录文件级归因，符号级归因可在刷新后补齐"""
        cur = self.conn.execute("SELECT * FROM file_edit_audit WHERE id = ?", (audit_id,))
        row = cur.fetchone()
        if not row:
            return None
        task_id = row["agent_task_id"] or ""
        if not task_id:
            return None

        return self.record_task_symbol_change(
            task_id=task_id,
            step_id=step_id,
            edit_audit_id=audit_id,
            file_path=row["file_path"],
            symbol_hash_before=row["symbol_hash"] or "",
            symbol_hash_after="",
            change_type=row["operation"] or "modified",
            source="file_edit_audit",
            metadata={
                "file_hash_before": row["file_hash_before"] or "",
                "file_hash_after": row["file_hash_after"] or "",
                "status": row["status"] or "",
            },
        )

    def _symbols_for_file_version(self, file_version_id: int) -> Dict[str, Dict[str, Any]]:
        cur = self.conn.execute(
            """
            SELECT fsv.qualified_name, fsv.symbol_hash, sc.name, sc.kind
            FROM file_symbol_versions fsv
            LEFT JOIN symbol_contents sc ON fsv.symbol_hash = sc.content_hash
            WHERE fsv.file_version_id = ? AND fsv.is_deleted = 0
            """,
            (file_version_id,),
        )
        return {row["qualified_name"]: dict(row) for row in cur.fetchall()}

    def _file_version_for_hash(self, file_path: str, file_hash: str, position: str = "") -> Optional[int]:
        """根据文件 hash 查找文件版本 ID

        Args:
            file_path: 文件路径
            file_hash: 文件内容 hash（file_edit_audit 中存储的 SHA-256）
            position: hash 匹配失败时的回退策略，"before" 取编辑前版本（is_current=0），
                      "after" 取编辑后版本（is_current=1）。空串不回退。

        背景：Rust parser 使用 blake_hash（SipHash u64，16 hex chars）作为
        file_versions.content_hash，而 file_edit_audit 使用 Python _compute_sha256
        （SHA-256，64 hex chars）。两种 hash 算法不同，无法直接匹配。
        当 hash 精确匹配失败时，按版本位置回退查找。
        """
        if not file_hash and not position:
            return None
        ws_id = self._get_active_workspace_id()
        rel_path = self._rel_path_for_attribution(file_path)

        # 优先尝试精确 hash 匹配（Python parser 使用 SHA-256，与 audit 一致）
        if file_hash:
            cur = self.conn.execute(
                """
                SELECT fv.id
                FROM file_versions fv
                JOIN file_instances fi ON fv.file_instance_id = fi.id
                WHERE fi.workspace_id = ? AND fi.rel_path = ? AND fv.content_hash = ?
                ORDER BY fv.parsed_at DESC, fv.id DESC
                LIMIT 1
                """,
                (ws_id, rel_path, file_hash),
            )
            row = cur.fetchone()
            if row:
                return int(row["id"])

        # 回退：Rust parser 的 blake_hash 与 audit 的 SHA-256 不匹配，
        # 按版本位置查找
        if position == "after":
            cur = self.conn.execute(
                """
                SELECT fv.id
                FROM file_versions fv
                JOIN file_instances fi ON fv.file_instance_id = fi.id
                WHERE fi.workspace_id = ? AND fi.rel_path = ? AND fv.is_current = 1
                ORDER BY fv.version_num DESC
                LIMIT 1
                """,
                (ws_id, rel_path),
            )
        elif position == "before":
            cur = self.conn.execute(
                """
                SELECT fv.id
                FROM file_versions fv
                JOIN file_instances fi ON fv.file_instance_id = fi.id
                WHERE fi.workspace_id = ? AND fi.rel_path = ? AND fv.is_current = 0
                ORDER BY fv.version_num DESC
                LIMIT 1
                """,
                (ws_id, rel_path),
            )
        else:
            return None
        row = cur.fetchone()
        return int(row["id"]) if row else None

    def link_edit_audit_symbols(self, audit_id: int, step_id: str = "") -> Dict[str, Any]:
        """根据 edit audit 的文件 hash，将该次编辑关联到具体符号版本变化"""
        cur = self.conn.execute("SELECT * FROM file_edit_audit WHERE id = ?", (audit_id,))
        audit = cur.fetchone()
        if not audit:
            return {"success": False, "error": t("cli.messages.attribution_edit_audit_not_found", default="edit audit not found"), "linked": 0}

        task_id = audit["agent_task_id"] or ""
        if not task_id:
            return {"success": False, "error": t("cli.messages.attribution_edit_audit_no_task", default="edit audit has no task id"), "linked": 0}
        if not step_id:
            step_id = self._infer_in_progress_step_id(task_id)

        before_version_id = self._file_version_for_hash(audit["file_path"], audit["file_hash_before"] or "", position="before")
        after_version_id = self._file_version_for_hash(audit["file_path"], audit["file_hash_after"] or "", position="after")
        if not before_version_id and not after_version_id:
            return {"success": False, "error": t("cli.messages.attribution_file_versions_missing", default="file versions not found; refresh graph first"), "linked": 0}

        before = self._symbols_for_file_version(before_version_id) if before_version_id else {}
        after = self._symbols_for_file_version(after_version_id) if after_version_id else {}
        names = sorted(set(before) | set(after))
        linked: List[Dict[str, Any]] = []

        for qualified_name in names:
            before_sym = before.get(qualified_name)
            after_sym = after.get(qualified_name)
            before_hash = before_sym["symbol_hash"] if before_sym else ""
            after_hash = after_sym["symbol_hash"] if after_sym else ""
            if before_hash == after_hash:
                continue
            if before_sym and after_sym:
                change_type = "modified"
            elif after_sym:
                change_type = "added"
            else:
                change_type = "deleted"

            result = self.record_task_symbol_change(
                task_id=task_id,
                step_id=step_id,
                edit_audit_id=audit_id,
                file_path=audit["file_path"],
                qualified_name=qualified_name,
                symbol_name=(after_sym or before_sym or {}).get("name", ""),
                symbol_hash_before=before_hash,
                symbol_hash_after=after_hash,
                change_type=change_type,
                source="edit_audit_symbol_diff",
                metadata={
                    "file_hash_before": audit["file_hash_before"] or "",
                    "file_hash_after": audit["file_hash_after"] or "",
                    "before_file_version_id": before_version_id or 0,
                    "after_file_version_id": after_version_id or 0,
                },
            )
            if result.get("success"):
                linked.append({"id": result["id"], "qualified_name": qualified_name, "change_type": change_type})

        return {"success": True, "audit_id": audit_id, "linked": len(linked), "changes": linked}

    def get_task_symbol_changes(
        self,
        task_id: str,
        step_id: str = "",
        file_path: str = "",
        limit: int = 100,
    ) -> List[Dict[str, Any]]:
        """查询任务或步骤归因到的符号变化"""
        sql = "SELECT * FROM task_symbol_changes WHERE task_id = ?"
        params: List[Any] = [task_id]
        if step_id:
            sql += " AND step_id = ?"
            params.append(step_id)
        if file_path:
            sql += " AND file_path = ?"
            params.append(self._rel_path_for_attribution(file_path))
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        cur = self.conn.execute(sql, params)
        rows = []
        for row in cur.fetchall():
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.get("metadata") or "{}")
            except Exception:
                item["metadata"] = {}
            rows.append(item)
        return rows

    def get_symbol_change_tasks(self, symbol_hash: str = "", qualified_name: str = "", limit: int = 50) -> List[Dict[str, Any]]:
        """反查某个符号版本或符号名由哪些任务改变过"""
        clauses = []
        params: List[Any] = []
        if symbol_hash:
            clauses.append("(symbol_hash_before = ? OR symbol_hash_after = ?)")
            params.extend([symbol_hash, symbol_hash])
        if qualified_name:
            clauses.append("qualified_name = ?")
            params.append(qualified_name)
        if not clauses:
            return []
        sql = "SELECT * FROM task_symbol_changes WHERE " + " AND ".join(clauses)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ?"
        params.append(limit)
        cur = self.conn.execute(sql, params)
        return [dict(row) for row in cur.fetchall()]

    def get_task_commits(
        self,
        task_id: str,
        include_commit_details: bool = True,
    ) -> List[Dict[str, Any]]:
        """查询任务关联的所有 commit（task → commit 正向查询）

        通过 task_symbol_changes.source_commit_hash 字段 JOIN git_commits 拿 commit 详情。

        Args:
            task_id: 任务 ID
            include_commit_details: 是否 JOIN git_commits 返回 commit 详情（author/message/timestamp）
                True 时每条返回额外字段：commit_author / commit_message / commit_timestamp / commit_subject
                False 时只返回 source_commit_hash 和出现次数

        Returns:
            按 source_commit_hash 去重的列表，每条含：
            {
                "source_commit_hash": str,
                "change_count": int,        # 此 commit 在此 task 下的 symbol change 条数
                "first_change_at": float,
                "last_change_at": float,
                # include_commit_details=True 时额外：
                "commit_author": str,
                "commit_message": str,
                "commit_timestamp": float,
                "commit_subject": str,     # message 首行
            }
        """
        if not task_id:
            return []
        if include_commit_details:
            sql = """
                SELECT tsc.source_commit_hash,
                       COUNT(*) AS change_count,
                       MIN(tsc.created_at) AS first_change_at,
                       MAX(tsc.created_at) AS last_change_at,
                       gc.author AS commit_author,
                       gc.message AS commit_message,
                       gc.timestamp AS commit_timestamp
                FROM task_symbol_changes tsc
                LEFT JOIN git_commits gc ON tsc.source_commit_hash = gc.commit_hash
                WHERE tsc.task_id = ? AND tsc.source_commit_hash != ''
                GROUP BY tsc.source_commit_hash
                ORDER BY last_change_at DESC
            """
        else:
            sql = """
                SELECT source_commit_hash,
                       COUNT(*) AS change_count,
                       MIN(created_at) AS first_change_at,
                       MAX(created_at) AS last_change_at
                FROM task_symbol_changes
                WHERE task_id = ? AND source_commit_hash != ''
                GROUP BY source_commit_hash
                ORDER BY last_change_at DESC
            """
        cur = self.conn.execute(sql, (task_id,))
        rows = []
        for row in cur.fetchall():
            item = dict(row)
            # commit_subject 仅在 include_commit_details=True 时提取（避免泄露字段到精简模式）
            if include_commit_details:
                msg = item.get("commit_message") or ""
                item["commit_subject"] = msg.split("\n", 1)[0] if msg else ""
            rows.append(item)
        return rows

    def get_commit_tasks(
        self,
        commit_hash: str,
        include_task_details: bool = True,
    ) -> List[Dict[str, Any]]:
        """查询 commit 关联的所有 task（commit → task 反向查询）

        通过 task_symbol_changes.source_commit_hash 字段 JOIN tasks 拿 task 详情。

        Args:
            commit_hash: git commit hash
            include_task_details: 是否 JOIN tasks 返回 task 详情（title/status）
                True 时每条返回额外字段：task_title / task_status / task_parent_id

        Returns:
            按 task_id 去重的列表，每条含：
            {
                "task_id": str,
                "change_count": int,
                "first_change_at": float,
                "last_change_at": float,
                # include_task_details=True 时额外：
                "task_title": str,
                "task_status": str,
                "task_parent_id": str,
            }
        """
        if not commit_hash:
            return []
        if include_task_details:
            sql = """
                SELECT tsc.task_id,
                       COUNT(*) AS change_count,
                       MIN(tsc.created_at) AS first_change_at,
                       MAX(tsc.created_at) AS last_change_at,
                       t.title AS task_title,
                       t.status AS task_status,
                       t.parent_id AS task_parent_id
                FROM task_symbol_changes tsc
                LEFT JOIN tasks t ON tsc.task_id = t.id
                WHERE tsc.source_commit_hash = ?
                GROUP BY tsc.task_id
                ORDER BY last_change_at DESC
            """
        else:
            sql = """
                SELECT task_id,
                       COUNT(*) AS change_count,
                       MIN(created_at) AS first_change_at,
                       MAX(created_at) AS last_change_at
                FROM task_symbol_changes
                WHERE source_commit_hash = ?
                GROUP BY task_id
                ORDER BY last_change_at DESC
            """
        cur = self.conn.execute(sql, (commit_hash,))
        return [dict(row) for row in cur.fetchall()]
