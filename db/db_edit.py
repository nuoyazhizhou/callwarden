"""
db_edit.py
==========

安全文件编辑 Mixin（F5：Agent OS 核心能力）。

Agent 通过 propose_edit 提交编辑请求，系统执行：
1. SHA-256 hash 校验（编辑前/后内容指纹）
2. dry-run 预览（不实际写入）
3. 原子写入（先写临时文件再 rename，避免半写入状态）
4. 审计日志（每次编辑落 file_edit_audit 表，可回溯）

设计要点：
- file_hash_before / file_hash_after 用于检测并发修改冲突
- diff_summary 摘要新增/删除行数，便于审查
- status 状态机：pending -> applied / reverted / failed
- revert_edit 仅标记审计状态，实际内容回滚需依赖 git checkout 或外部备份
  （审计表不存储完整文件内容，避免数据膨胀）

依赖 file_edit_audit 表（Schema v12）。
"""

from __future__ import annotations

import hashlib
import os
import tempfile
import time
from typing import Any, Dict, List, Optional

from ..config import norm_path, atomic_write_file


# 编辑操作类型常量
EDIT_OPERATION_EDIT = "edit"
EDIT_OPERATION_CREATE = "create"
EDIT_OPERATION_DELETE = "delete"

# 编辑审计状态常量
EDIT_STATUS_PENDING = "pending"
EDIT_STATUS_APPLIED = "applied"
EDIT_STATUS_REVERTED = "reverted"
EDIT_STATUS_FAILED = "failed"

# 合法操作类型集合
_VALID_OPERATIONS = {EDIT_OPERATION_EDIT, EDIT_OPERATION_CREATE, EDIT_OPERATION_DELETE}


class EditSafetyMixin:
    """安全文件编辑 Mixin（Agent OS 核心）

    提供以下能力：
    - propose_edit: 提交编辑请求（hash 校验 + dry-run + 原子写入 + 审计日志）
    - revert_edit: 标记编辑为已回滚（实际内容回滚需 git checkout 或外部备份）
    - get_edit_history: 查询编辑历史（按文件过滤或全部）
    - get_edit_stats: 编辑统计（按状态/操作类型分组）

    依赖 file_edit_audit 表（Schema v12）。
    """

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _compute_sha256(content: str) -> str:
        """计算字符串内容的 SHA-256 hash（hexdigest）

        Args:
            content: 文本内容

        Returns:
            64 位十六进制 hash 字符串；空内容返回空串以区分"文件不存在"
        """
        if not content:
            return ""
        return hashlib.sha256(content.encode("utf-8")).hexdigest()

    @staticmethod
    def _compute_diff_summary(old_content: str, new_content: str) -> str:
        """生成 diff 摘要（新增 N 行 / 删除 M 行）

        采用行级集合比较（简单快速）：
        - added = 新内容行集合 - 旧内容行集合 的行数
        - removed = 旧内容行集合 - 新内容行集合 的行数
        - 若旧内容为空（新建文件），added = 新内容行数

        Args:
            old_content: 编辑前内容
            new_content: 编辑后内容

        Returns:
            摘要字符串，如 "新增 5 行 / 删除 2 行"
        """
        old_lines = old_content.splitlines() if old_content else []
        new_lines = new_content.splitlines() if new_content else []

        # 新建文件场景
        if not old_content:
            return f"新增 {len(new_lines)} 行 / 删除 0 行"

        # 删除文件场景
        if not new_content:
            return f"新增 0 行 / 删除 {len(old_lines)} 行"

        # 行级集合差异（去重后比较，避免相同行重复计数）
        old_set = set(old_lines)
        new_set = set(new_lines)
        added = len(new_set - old_set)
        removed = len(old_set - new_set)
        return f"新增 {added} 行 / 删除 {removed} 行"

    def _resolve_abs_path(self, file_path: str) -> str:
        """将文件路径解析为绝对路径

        相对路径基于 workspace_root 解析，绝对路径直接规范化。

        Args:
            file_path: 文件路径（相对或绝对）

        Returns:
            绝对路径
        """
        if os.path.isabs(file_path):
            return norm_path(file_path)
        return norm_path(os.path.join(self.workspace_root, file_path))

    def _resolve_rel_path(self, file_path: str) -> str:
        """将文件路径解析为相对 workspace_root 的路径（用于审计记录）"""
        abs_path = self._resolve_abs_path(file_path)
        try:
            rel = os.path.relpath(abs_path, self.workspace_root)
            # 统一使用正斜杠（跨平台一致性）
            return rel.replace(os.sep, "/")
        except ValueError:
            # Windows 下不同盘符 relpath 会抛 ValueError，回退为原路径
            return file_path.replace(os.sep, "/")

    def _read_file_safe(self, abs_path: str) -> str:
        """安全读取文件，文件不存在返回空串

        Args:
            abs_path: 文件绝对路径

        Returns:
            文件内容字符串；文件不存在返回 ""
        """
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                return f.read()
        except FileNotFoundError:
            return ""
        except OSError:
            return ""

    def _atomic_write(self, abs_path: str, content: str) -> None:
        """原子写入文件（委托给公共函数 atomic_write_file）

        SEC-001：所有文件写入走 config.atomic_write_file，确保数据完整性。
        """
        atomic_write_file(abs_path, content)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def propose_edit(
        self,
        file_path: str,
        new_content: str,
        operation: str = EDIT_OPERATION_EDIT,
        agent_task_id: str = "",
        symbol_hash: str = "",
        dry_run: bool = False,
    ) -> Dict[str, Any]:
        """提交编辑请求（Agent OS 核心方法）

        执行流程：
        a. 计算编辑前文件的 SHA-256 hash（file_hash_before）
        b. 计算编辑后内容的 SHA-256 hash（file_hash_after）
        c. 生成 diff 摘要（新增/删除行数）
        d. 写入 file_edit_audit 表（status=pending）
        e. 如果 dry_run=True，返回预览结果不实际写入
        f. 如果 dry_run=False，原子写入文件（先写临时文件再 rename）
        g. 更新 audit 记录 status=applied, applied_at=now

        Args:
            file_path: 文件路径（相对 workspace_root 或绝对路径）
            new_content: 编辑后的完整内容
            operation: 操作类型（edit / create / delete）
            agent_task_id: 关联的任务 ID（可选）
            symbol_hash: 关联的符号 hash（可选）
            dry_run: 是否仅预览（True 不实际写入）

        Returns:
            {
                "audit_id": int,            # 审计记录 ID（dry_run 时也为已插入的 pending 记录）
                "file_path": str,           # 相对路径
                "file_hash_before": str,    # 编辑前 hash（空串表示文件不存在）
                "file_hash_after": str,     # 编辑后 hash
                "diff_summary": str,        # 变更摘要
                "status": "applied"/"preview"/"failed",
                "success": bool,
                "error": str,               # 失败时存在
            }
        """
        # 参数校验
        if operation not in _VALID_OPERATIONS:
            return {
                "audit_id": 0,
                "file_path": file_path,
                "file_hash_before": "",
                "file_hash_after": "",
                "diff_summary": "",
                "status": EDIT_STATUS_FAILED,
                "success": False,
                "error": f"非法操作类型: {operation}（合法值: edit/create/delete）",
            }

        # 解析路径
        abs_path = self._resolve_abs_path(file_path)
        rel_path = self._resolve_rel_path(file_path)

        # 步骤 a：读取并计算编辑前 hash
        old_content = self._read_file_safe(abs_path)
        file_hash_before = self._compute_sha256(old_content)

        # 步骤 b：计算编辑后 hash
        file_hash_after = self._compute_sha256(new_content)

        # 步骤 c：生成 diff 摘要
        diff_summary = self._compute_diff_summary(old_content, new_content)

        # 获取工作区 ID
        ws_id = None
        if hasattr(self, "_get_active_workspace_id"):
            try:
                ws_id = self._get_active_workspace_id()
            except Exception:
                ws_id = None

        now = time.time()

        # 步骤 d：写入审计记录（status=pending）
        cur = self.conn.execute(
            """
            INSERT INTO file_edit_audit
                (workspace_id, file_path, operation, file_hash_before, file_hash_after,
                 symbol_hash, agent_task_id, diff_summary, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ws_id,
                rel_path,
                operation,
                file_hash_before,
                file_hash_after,
                symbol_hash,
                agent_task_id,
                diff_summary,
                EDIT_STATUS_PENDING,
                now,
            ),
        )
        audit_id = cur.lastrowid

        # 步骤 e：dry_run 模式，返回预览不写入
        if dry_run:
            # 预览模式回滚审计记录（避免 pending 残留），改用单独的 preview 状态标记
            # 这里保留记录但标记为 preview（用 status='preview' 临时表示）
            # 为保持 schema 状态机一致性，dry_run 时删除该 pending 记录
            self.conn.execute(
                "DELETE FROM file_edit_audit WHERE id = ?", (audit_id,)
            )
            self.conn.commit()
            return {
                "audit_id": 0,
                "file_path": rel_path,
                "file_hash_before": file_hash_before,
                "file_hash_after": file_hash_after,
                "diff_summary": diff_summary,
                "status": "preview",
                "success": True,
            }

        # 步骤 f：实际原子写入文件
        try:
            if operation == EDIT_OPERATION_DELETE:
                # 删除操作：删除文件（若存在）
                if os.path.exists(abs_path):
                    os.remove(abs_path)
            else:
                # edit / create 操作：原子写入新内容
                self._atomic_write(abs_path, new_content)
        except OSError as e:
            # 写入失败：更新审计状态为 failed
            self.conn.execute(
                "UPDATE file_edit_audit SET status = ? WHERE id = ?",
                (EDIT_STATUS_FAILED, audit_id),
            )
            self.conn.commit()
            # SEC-007: 错误消息不返回绝对路径，只返回相对路径和错误类型
            return {
                "audit_id": audit_id,
                "file_path": rel_path,
                "file_hash_before": file_hash_before,
                "file_hash_after": file_hash_after,
                "diff_summary": diff_summary,
                "status": EDIT_STATUS_FAILED,
                "success": False,
                "error": f"文件写入失败: {type(e).__name__}",
            }

        # 步骤 g：更新审计状态为 applied
        self.conn.execute(
            "UPDATE file_edit_audit SET status = ?, applied_at = ? WHERE id = ?",
            (EDIT_STATUS_APPLIED, time.time(), audit_id),
        )
        self.conn.commit()

        return {
            "audit_id": audit_id,
            "file_path": rel_path,
            "file_hash_before": file_hash_before,
            "file_hash_after": file_hash_after,
            "diff_summary": diff_summary,
            "status": EDIT_STATUS_APPLIED,
            "success": True,
        }

    def revert_edit(self, audit_id: int) -> Dict[str, Any]:
        """回滚编辑（标记审计状态为 reverted）

        注意：审计表不存储完整文件内容，因此本方法仅标记审计状态。
        实际内容回滚需要依赖 git checkout 或外部备份机制。

        Args:
            audit_id: 审计记录 ID

        Returns:
            {
                "audit_id": int,
                "status": "reverted",
                "message": str,
                "file_path": str,         # 关联的文件路径
                "file_hash_before": str,  # 编辑前 hash（供 git checkout 参考）
            }
            若审计记录不存在，返回 {"error": "...", "audit_id": audit_id}
        """
        cur = self.conn.execute(
            "SELECT * FROM file_edit_audit WHERE id = ?",
            (audit_id,),
        )
        row = cur.fetchone()
        if not row:
            return {
                "audit_id": audit_id,
                "error": f"审计记录不存在: id={audit_id}",
            }

        # 仅 applied 状态的记录可回滚
        current_status = row["status"]
        if current_status != EDIT_STATUS_APPLIED:
            return {
                "audit_id": audit_id,
                "status": current_status,
                "message": f"当前状态 {current_status} 不可回滚（仅 {EDIT_STATUS_APPLIED} 可回滚）",
                "file_path": row["file_path"],
                "file_hash_before": row["file_hash_before"],
            }

        # 更新审计状态为 reverted
        self.conn.execute(
            "UPDATE file_edit_audit SET status = ?, reverted_at = ? WHERE id = ?",
            (EDIT_STATUS_REVERTED, time.time(), audit_id),
        )
        self.conn.commit()

        return {
            "audit_id": audit_id,
            "status": EDIT_STATUS_REVERTED,
            "message": (
                "审计已标记为 reverted；实际内容回滚需依赖 git checkout "
                f"或外部备份（编辑前 hash: {row['file_hash_before'] or '空'}）"
            ),
            "file_path": row["file_path"],
            "file_hash_before": row["file_hash_before"],
        }

    def get_edit_history(
        self,
        file_path: str = "",
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """查询编辑历史

        按文件路径过滤或返回全部编辑记录，按 created_at 倒序排列。

        Args:
            file_path: 文件路径过滤（为空则返回全部）；相对路径匹配审计表 file_path 字段
            limit: 返回数量限制（默认 20）

        Returns:
            审计记录列表，每条记录含 id / file_path / operation / status /
            file_hash_before / file_hash_after / diff_summary / agent_task_id /
            created_at / applied_at / reverted_at
        """
        if file_path:
            # 统一为相对路径匹配（用户可能传绝对或相对路径）
            rel_path = self._resolve_rel_path(file_path)
            cur = self.conn.execute(
                """
                SELECT id, file_path, operation, file_hash_before, file_hash_after,
                       symbol_hash, agent_task_id, diff_summary, status,
                       created_at, applied_at, reverted_at
                FROM file_edit_audit
                WHERE file_path = ?
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (rel_path, limit),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT id, file_path, operation, file_hash_before, file_hash_after,
                       symbol_hash, agent_task_id, diff_summary, status,
                       created_at, applied_at, reverted_at
                FROM file_edit_audit
                ORDER BY created_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [dict(row) for row in cur]

    def get_edit_stats(self, time_window: str = "30d") -> Dict[str, Any]:
        """编辑统计

        统计指定时间窗口内的编辑情况：
        - 总编辑数 / 成功数 / 回滚数 / 失败数
        - 按操作类型分组（edit / create / delete）
        - 回滚率（reverted / applied）

        Args:
            time_window: 时间窗口（如 7d / 30d / 90d / "" 全部）

        Returns:
            {
                "time_window": str,
                "total": int,
                "by_status": {"applied": N, "reverted": N, "failed": N, "pending": N},
                "by_operation": {"edit": N, "create": N, "delete": N},
                "revert_rate": float,  # 回滚率（0.0 ~ 1.0）
            }
        """
        # 解析时间窗口
        since_ts = self._parse_time_window(time_window)

        # SEC-003: 不使用 f-string 拼接 WHERE 子句，改用完整 SQL 条件构建
        if since_ts > 0:
            sql_status = "SELECT status, COUNT(*) as cnt FROM file_edit_audit WHERE created_at >= ? GROUP BY status"
            sql_op = "SELECT operation, COUNT(*) as cnt FROM file_edit_audit WHERE created_at >= ? GROUP BY operation"
            params: tuple = (since_ts,)
        else:
            sql_status = "SELECT status, COUNT(*) as cnt FROM file_edit_audit GROUP BY status"
            sql_op = "SELECT operation, COUNT(*) as cnt FROM file_edit_audit GROUP BY operation"
            params = ()

        # 按状态分组统计
        cur = self.conn.execute(sql_status, params)
        by_status: Dict[str, int] = {
            EDIT_STATUS_PENDING: 0,
            EDIT_STATUS_APPLIED: 0,
            EDIT_STATUS_REVERTED: 0,
            EDIT_STATUS_FAILED: 0,
        }
        total = 0
        for row in cur:
            by_status[row["status"]] = row["cnt"]
            total += row["cnt"]

        # 按操作类型分组统计
        cur = self.conn.execute(sql_op, params)
        by_operation: Dict[str, int] = {
            EDIT_OPERATION_EDIT: 0,
            EDIT_OPERATION_CREATE: 0,
            EDIT_OPERATION_DELETE: 0,
        }
        for row in cur:
            by_operation[row["operation"]] = row["cnt"]

        # 回滚率 = reverted / (applied + reverted)，避免除零
        applied_count = by_status[EDIT_STATUS_APPLIED]
        reverted_count = by_status[EDIT_STATUS_REVERTED]
        denom = applied_count + reverted_count
        revert_rate = (reverted_count / denom) if denom > 0 else 0.0

        return {
            "time_window": time_window,
            "total": total,
            "by_status": by_status,
            "by_operation": by_operation,
            "revert_rate": round(revert_rate, 4),
        }

    # ------------------------------------------------------------------
    # 辅助方法
    # ------------------------------------------------------------------

    def _parse_time_window(self, time_window: str) -> float:
        """解析时间窗口字符串为 Unix 时间戳

        支持格式：
        - "Nd" / "Nw" / "Nh" / "Ny"：N 天/周/小时/年
        - "" 或 "all"：返回 0 表示不过滤
        - ISO 日期（YYYY-MM-DD）：返回该日期的时间戳

        Args:
            time_window: 时间窗口字符串

        Returns:
            起始时间戳；0 表示不过滤
        """
        if not time_window or time_window.lower() == "all":
            return 0.0

        now = time.time()

        # 数字 + 单位格式（如 30d / 7d / 24h / 1y）
        if len(time_window) >= 2 and time_window[-1].isalpha() and time_window[:-1].isdigit():
            num = int(time_window[:-1])
            unit = time_window[-1].lower()
            if unit == "d":
                return now - num * 86400.0
            if unit == "w":
                return now - num * 7 * 86400.0
            if unit == "h":
                return now - num * 3600.0
            if unit == "y":
                return now - num * 365 * 86400.0

        # ISO 日期格式
        try:
            import datetime
            dt = datetime.datetime.fromisoformat(time_window)
            return dt.timestamp()
        except (ValueError, TypeError):
            pass

        # 无法解析，返回 0（不过滤）
        return 0.0
