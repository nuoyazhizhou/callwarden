"""
db_tasks.py
===========

任务驱动 MCP 系统 Mixin 类。

提供任务创建、步骤领取、结果回报、回滚、查询等功能。
基于 tasks / task_steps / change_audit 三张表实现，不依赖工作区。
"""

from __future__ import annotations

import json
import secrets
import time
from typing import Any, Dict, List, Optional

from .schema import (
    TASK_STATUS_OPEN,
    TASK_STATUS_IN_PROGRESS,
    TASK_STATUS_REVIEW,
    TASK_STATUS_APPLIED,
    TASK_STATUS_CLOSED,
    TASK_STATUS_REVERTED,
    STEP_STATUS_PENDING,
    STEP_STATUS_IN_PROGRESS,
    STEP_STATUS_DONE,
    STEP_STATUS_FAILED,
    STEP_STATUS_SKIPPED,
    STEP_STATUS_BLOCKED,
    EDIT_ACTIONS,
)


def _gen_id(prefix: str) -> str:
    """生成唯一 ID

    格式: {prefix}-{timestamp_ms}-{random4hex}

    Args:
        prefix: ID 前缀（T / S / C）

    Returns:
        形如 T-1719900000000-a1b2 的唯一标识
    """
    ts_ms = int(time.time() * 1000)
    rand4 = secrets.token_hex(2)  # 2 字节 = 4 个十六进制字符
    return f"{prefix}-{ts_ms}-{rand4}"


def _gen_task_id() -> str:
    """生成任务 ID"""
    return _gen_id("T")


def _gen_step_id() -> str:
    """生成步骤 ID"""
    return _gen_id("S")


def _gen_change_id() -> str:
    """生成变更记录 ID"""
    return _gen_id("C")


def _serialize_check_items(check_items: Any) -> str:
    """序列化 check_items 为字符串存储

    列表/字典 → JSON 字符串；字符串原样存储；None → 空串。
    """
    if check_items is None:
        return ""
    if isinstance(check_items, str):
        return check_items
    try:
        return json.dumps(check_items, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(check_items)


def _deserialize_check_items(raw: str) -> Any:
    """反序列化 check_items

    JSON 字符串 → 还原为列表/字典；普通字符串原样返回；空串返回 ""。
    """
    if not raw:
        return ""
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


class TaskMixin:
    """任务驱动 MCP 功能 Mixin

    通过 self.conn 访问数据库连接，提供任务/步骤/变更审计管理。
    所有任务和步骤使用 TEXT 主键（T-xxx / S-xxx），全局唯一，不绑定工作区。

    状态机：
    - 任务: open → in_progress → review → applied → closed
                                 ↘ reverted
    - 步骤: pending → in_progress → done / failed
                                  ↘ failed 后自动插入 fix_defect 步骤
    """

    def task_create(
        self,
        title: str,
        description: str = "",
        steps: Optional[List[Dict[str, Any]]] = None,
        creator: str = "agent",
    ) -> str:
        """创建任务和步骤

        Args:
            title: 任务标题
            description: 任务描述
            steps: 步骤列表，每个元素为 dict：
                   {action, target_file, target_symbol, check_items}
                   - action: 动作类型（annotate/refactor/fix 等）
                   - target_file: 目标文件路径
                   - target_symbol: 目标符号限定名
                   - check_items: 检查项（列表或字符串）
            creator: 创建者标识

        Returns:
            新建任务的 task_id
        """
        now = time.time()
        task_id = _gen_task_id()

        # 插入任务记录，初始状态为 open
        self.conn.execute(
            """
            INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, title, description, creator, TASK_STATUS_OPEN, now, now),
        )

        # 逐个插入步骤（step_index 从 0 开始递增）
        if steps:
            for idx, step in enumerate(steps):
                step_id = _gen_step_id()
                action = step.get("action", "")
                target_file = step.get("target_file", "")
                target_symbol = step.get("target_symbol", "")
                check_items = _serialize_check_items(step.get("check_items", ""))

                self.conn.execute(
                    """
                    INSERT INTO task_steps
                        (id, task_id, step_index, action, target_file, target_symbol,
                         check_items, status, result, created_at, completed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        step_id,
                        task_id,
                        idx,
                        action,
                        target_file,
                        target_symbol,
                        check_items,
                        STEP_STATUS_PENDING,
                        "",
                        now,
                        None,
                    ),
                )

        self.conn.commit()
        return task_id

    def task_next_step(self, task_id: str) -> Optional[Dict[str, Any]]:
        """领取当前待执行的步骤

        查找该任务中第一个 status=pending 的步骤，将其状态改为 in_progress。
        如果任务状态是 open，改为 in_progress。

        Before-Edit Contract（编辑前契约）：
        - 当步骤 action 属于编辑类操作（见 EDIT_ACTIONS）且 target_file 非空时，
          自动调用 self.check_before_edit(target_file) 执行护栏阻断式检查。
        - decision == "block"：步骤状态改为 blocked（而非 in_progress），
          返回 guardrail_alert 字段告知 Agent 必须先处理告警。
        - decision == "warn"：步骤仍进入 in_progress，但返回 guardrail_warning 字段。
        - decision == "pass" 或非编辑类步骤：正常流程。

        Args:
            task_id: 任务 ID

        Returns:
            步骤详情 dict，包含：
            - step_id, action, target_file, target_symbol, check_items
            - task_id, task_title, step_index
            - guardrail_alert（仅 block 时存在）：{decision, message, findings}
            - guardrail_warning（仅 warn 时存在）：{decision, message, findings}
            如果没有待执行步骤，返回 None
        """
        # 查找第一个 pending 步骤（按 step_index 升序）
        cur = self.conn.execute(
            """
            SELECT ts.id as step_id, ts.task_id, ts.step_index, ts.action,
                   ts.target_file, ts.target_symbol, ts.check_items,
                   t.title as task_title
            FROM task_steps ts
            JOIN tasks t ON ts.task_id = t.id
            WHERE ts.task_id = ? AND ts.status = ?
            ORDER BY ts.step_index ASC
            LIMIT 1
            """,
            (task_id, STEP_STATUS_PENDING),
        )
        row = cur.fetchone()
        if not row:
            return None

        step_id = row["step_id"]
        now = time.time()
        action = row["action"] or ""
        target_file = row["target_file"] or ""

        # Before-Edit Contract：编辑类动作且存在目标文件时，触发护栏检查
        guardrail_alert: Optional[Dict[str, Any]] = None
        guardrail_warning: Optional[Dict[str, Any]] = None
        new_status = STEP_STATUS_IN_PROGRESS

        if action.lower() in EDIT_ACTIONS and target_file:
            # 调用 GuardrailMixin.check_before_edit（Mixin 组合保证 self 具备该方法）
            try:
                gr_result = self.check_before_edit(target_file, proposed_change="")
                decision = (gr_result.get("decision") or "pass").lower()
                if decision == "block":
                    # 阻断：步骤进入 blocked 状态，Agent 必须处理告警后调用 task_resolve_block
                    new_status = STEP_STATUS_BLOCKED
                    guardrail_alert = {
                        "decision": "block",
                        "message": gr_result.get("message", ""),
                        "findings": gr_result.get("findings", []),
                    }
                elif decision == "warn":
                    # 警告：步骤仍可执行，但返回告警信息供 Agent 知悉
                    guardrail_warning = {
                        "decision": "warn",
                        "message": gr_result.get("message", ""),
                        "findings": gr_result.get("findings", []),
                    }
            except Exception as exc:
                # 护栏检查自身异常不应阻塞任务流，降级为警告
                guardrail_warning = {
                    "decision": "warn",
                    "message": f"护栏检查异常，已降级放行：{exc}",
                    "findings": [],
                }

        # 将步骤状态改为 in_progress 或 blocked
        self.conn.execute(
            "UPDATE task_steps SET status = ? WHERE id = ?",
            (new_status, step_id),
        )

        # 如果任务状态是 open，改为 in_progress
        cur = self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        )
        task_row = cur.fetchone()
        if task_row and task_row["status"] == TASK_STATUS_OPEN:
            self.conn.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                (TASK_STATUS_IN_PROGRESS, now, task_id),
            )

        self.conn.commit()

        result: Dict[str, Any] = {
            "step_id": step_id,
            "task_id": row["task_id"],
            "step_index": row["step_index"],
            "action": row["action"],
            "target_file": row["target_file"],
            "target_symbol": row["target_symbol"],
            "check_items": _deserialize_check_items(row["check_items"]),
            "task_title": row["task_title"],
            "status": new_status,
        }
        if guardrail_alert:
            result["guardrail_alert"] = guardrail_alert
        if guardrail_warning:
            result["guardrail_warning"] = guardrail_warning

        # F7: 构建结构化指令（Agent 必须遵循的操作约束，替代自由文本提示）
        try:
            result["structured_instruction"] = self.build_structured_instruction(result)
        except Exception:
            result["structured_instruction"] = None

        return result

    def task_resolve_block(self, task_id: str, step_id: str, resolution: str = "ack") -> Optional[Dict[str, Any]]:
        """处理 blocked 步骤的告警，将其恢复为 pending 以便重新领取

        Agent 处理 guardrail_alert 后调用此方法：
        - 将步骤状态从 blocked 改回 pending
        - 记录 resolution（ack/override/fix_applied）
        - 之后 Agent 可再次调用 task_next_step 领取该步骤

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
            resolution: 处理方式描述
                - "ack"：已确认告警，请求放行
                - "override"：强制覆盖（需人工确认）
                - "fix_applied"：已修正代码，重新检查

        Returns:
            更新后的步骤详情 dict，包含 step_id / status="pending" / resolution
            若步骤不存在或非 blocked 状态，返回 None
        """
        cur = self.conn.execute(
            "SELECT id, status FROM task_steps WHERE id = ? AND task_id = ?",
            (step_id, task_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        if row["status"] != STEP_STATUS_BLOCKED:
            return None

        now = time.time()
        self.conn.execute(
            "UPDATE task_steps SET status = ?, completed_at = ? WHERE id = ?",
            (STEP_STATUS_PENDING, now, step_id),
        )
        self.conn.commit()

        return {
            "step_id": step_id,
            "task_id": task_id,
            "status": STEP_STATUS_PENDING,
            "resolution": resolution,
            "message": "告警已处理，步骤恢复为 pending，可重新领取",
        }

    # ------------------------------------------------------------------
    # F7: 结构化指令构建
    # ------------------------------------------------------------------

    def build_structured_instruction(self, step: Dict[str, Any]) -> Dict[str, Any]:
        """为步骤构建结构化指令（F7，替代自由文本提示词）

        根据步骤的 action / target_file / target_symbol 自动拉取上下文，
        生成 Agent 必须遵循的结构化操作指令。Agent 无法自由发挥，
        只能执行指定操作。

        Args:
            step: task_next_step 返回的步骤 dict

        Returns:
            结构化指令 dict，包含：
            - action: 操作类型
            - target_file / target_symbol: 操作目标
            - read_targets: Agent 需要读取的代码段
            - context: 自动拉取的上下文（符号签名、调用者、已有摘要）
            - constraints: Agent 必须遵守的约束列表
            - checks: 完成后自动运行的检查列表
        """
        action = (step.get("action") or "").lower()
        target_file = step.get("target_file") or ""
        target_symbol = step.get("target_symbol") or ""

        instruction: Dict[str, Any] = {
            "action": action,
            "target_file": target_file,
            "target_symbol": target_symbol,
            "read_targets": [],
            "context": {},
            "constraints": [],
            "checks": [],
        }

        # 根据 action 类型填充不同的约束和检查
        if action in ("annotate_function", "annotate", "add_comment", "comment"):
            instruction["constraints"] = [
                "只添加注释，不修改函数逻辑",
                "注释语言: 中文",
                "注释格式遵循目标语言规范（Rust: /// 或 /** */，Python: # 或 docstring）",
            ]
            instruction["checks"] = ["syntax", "semgrep_quick"]
        elif action in ("refactor", "refactor_function"):
            instruction["constraints"] = [
                "保持函数的外部行为不变（签名、返回值、副作用）",
                "修改后必须通过语法检查",
                "如涉及公共 API 变更须同步更新调用方",
            ]
            instruction["checks"] = ["syntax", "semgrep"]
        elif action in ("fix", "fix_defect", "fix_gate_failure"):
            instruction["constraints"] = [
                "只修复报告的问题，不做额外修改",
                "修复后必须通过之前的检查门禁",
            ]
            instruction["checks"] = ["syntax", "semgrep"]
        elif action in ("edit", "propose_edit", "write"):
            instruction["constraints"] = [
                "使用 propose_edit 工具执行写入，禁止直接操作文件系统",
                "写入前先 dry_run 确认 diff",
                "提供 expected_hash 防止并发冲突",
            ]
            instruction["checks"] = ["syntax"]
        else:
            instruction["constraints"] = ["按步骤 check_items 描述执行"]
            instruction["checks"] = ["syntax"]

        # 自动拉取符号上下文（target_symbol 非空时）
        if target_symbol and hasattr(self, "get_symbol"):
            try:
                sym = self.get_symbol(target_symbol)
                if sym:
                    instruction["read_targets"].append(
                        {
                            "file": target_file,
                            "symbol": target_symbol,
                            "lines": f"{sym.get('start_line', '?')}-{sym.get('end_line', '?')}",
                        }
                    )
                    instruction["context"]["symbol_signature"] = sym.get("signature", "")

                    # 拉取调用者摘要（影响面感知）
                    if hasattr(self, "get_callers"):
                        callers = self.get_callers(target_symbol)
                        instruction["context"]["callers"] = [
                            {
                                "name": c.get("caller_name", ""),
                                "file": c.get("caller_file", ""),
                            }
                            for c in (callers or [])[:5]
                        ]

                    # 拉取已有摘要（避免重复劳动）
                    if hasattr(self, "get_summary"):
                        summary = self.get_summary(target_symbol)
                        if summary:
                            instruction["context"]["existing_summary"] = summary.get("summary", "")
            except Exception:
                pass  # 上下文拉取失败不影响指令生成

        return instruction

    def task_report_step(
        self,
        task_id: str,
        step_id: str,
        result: str = "",
        success: bool = True,
        changes: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Dict[str, Any]]:
        """回报步骤执行结果

        - 将步骤状态改为 done（成功）或 failed（失败）
        - 记录 result 和 completed_at
        - 如果 changes 不为空，记录到 change_audit 表
        - 如果失败，自动插入一个 fix_defect 步骤（step_index 在当前之后）
        - 如果成功且没有更多 pending 步骤，将任务状态改为 review

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
            result: 执行结果描述
            success: 是否成功
            changes: 变更列表，每个元素为 dict：
                     {file_path, hash_before, hash_after, diff, author}

        Returns:
            下一步步骤信息（如果有，状态仍为 pending，留给 task_next_step 领取）；
            如果没有更多待执行步骤，返回 None
        """
        now = time.time()
        new_status = STEP_STATUS_DONE if success else STEP_STATUS_FAILED

        # 更新步骤状态、结果和完成时间
        self.conn.execute(
            """
            UPDATE task_steps
            SET status = ?, result = ?, completed_at = ?
            WHERE id = ? AND task_id = ?
            """,
            (new_status, result, now, step_id, task_id),
        )

        # 记录变更审计（每条变更生成一条 change_audit 记录）
        if changes:
            for change in changes:
                change_id = _gen_change_id()
                self.conn.execute(
                    """
                    INSERT INTO change_audit
                        (id, task_id, step_id, file_path, hash_before, hash_after,
                         diff, author, timestamp)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        change_id,
                        task_id,
                        step_id,
                        change.get("file_path", ""),
                        change.get("hash_before", ""),
                        change.get("hash_after", ""),
                        change.get("diff", ""),
                        change.get("author", "agent"),
                        now,
                    ),
                )

        # 失败时自动插入"修复缺陷"步骤
        if not success:
            # 计算新的 step_index（当前最大值 + 1）
            cur = self.conn.execute(
                "SELECT MAX(step_index) as max_idx FROM task_steps WHERE task_id = ?",
                (task_id,),
            )
            max_row = cur.fetchone()
            max_idx = max_row["max_idx"] if max_row and max_row["max_idx"] is not None else -1
            new_step_index = max_idx + 1

            fix_step_id = _gen_step_id()
            self.conn.execute(
                """
                INSERT INTO task_steps
                    (id, task_id, step_index, action, target_file, target_symbol,
                     check_items, status, result, created_at, completed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    fix_step_id,
                    task_id,
                    new_step_index,
                    "fix_defect",
                    "",
                    "",
                    "",
                    STEP_STATUS_PENDING,
                    "",
                    now,
                    None,
                ),
            )

        # 更新任务的 updated_at
        self.conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?",
            (now, task_id),
        )

        # ---- F6 检查门禁 ----
        # 步骤成功且存在文件变更时，自动运行语法+Semgrep 检查。
        # 门禁失败则插入 fix_gate_failure 步骤，且不转为 review。
        gate_failed = False
        if success and changes:
            changed_files = [c.get("file_path", "") for c in changes if c.get("file_path")]
            if changed_files and hasattr(self, "run_check_gate"):
                try:
                    gate_result = self.run_check_gate(task_id, step_id, changed_files)
                    if not gate_result["passed"]:
                        gate_failed = True
                        # 插入 fix_gate_failure 步骤（step_index 取当前最大值 + 1）
                        cur = self.conn.execute(
                            "SELECT MAX(step_index) as max_idx FROM task_steps WHERE task_id = ?",
                            (task_id,),
                        )
                        max_idx = cur.fetchone()["max_idx"] or 0
                        fix_step_id = _gen_step_id()
                        check_items_json = _serialize_check_items(
                            {
                                "gate_findings": gate_result["findings"],
                                "checks_run": gate_result["checks_run"],
                                "summary": gate_result["summary"],
                            }
                        )
                        self.conn.execute(
                            """
                            INSERT INTO task_steps
                                (id, task_id, step_index, action, target_file, target_symbol,
                                 check_items, status, result, created_at, completed_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                fix_step_id,
                                task_id,
                                max_idx + 1,
                                "fix_gate_failure",
                                changed_files[0],
                                "",
                                check_items_json,
                                STEP_STATUS_PENDING,
                                "",
                                now,
                                None,
                            ),
                        )
                except Exception:
                    pass  # 门禁检查自身异常不阻塞任务流

        # 成功且没有更多 pending 步骤时，将任务状态改为 review
        # （门禁失败时不转 review，留待 Agent 领取 fix_gate_failure 步骤）
        if success and not gate_failed:
            cur = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM task_steps WHERE task_id = ? AND status = ?",
                (task_id, STEP_STATUS_PENDING),
            )
            pending_count = cur.fetchone()["cnt"]
            if pending_count == 0:
                self.conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (TASK_STATUS_REVIEW, now, task_id),
                )

        self.conn.commit()

        # 返回下一步步骤信息（不修改状态，留给后续 task_next_step 领取）
        cur = self.conn.execute(
            """
            SELECT ts.id as step_id, ts.task_id, ts.step_index, ts.action,
                   ts.target_file, ts.target_symbol, ts.check_items,
                   t.title as task_title
            FROM task_steps ts
            JOIN tasks t ON ts.task_id = t.id
            WHERE ts.task_id = ? AND ts.status = ?
            ORDER BY ts.step_index ASC
            LIMIT 1
            """,
            (task_id, STEP_STATUS_PENDING),
        )
        next_row = cur.fetchone()
        if not next_row:
            return None

        return {
            "step_id": next_row["step_id"],
            "task_id": next_row["task_id"],
            "step_index": next_row["step_index"],
            "action": next_row["action"],
            "target_file": next_row["target_file"],
            "target_symbol": next_row["target_symbol"],
            "check_items": _deserialize_check_items(next_row["check_items"]),
            "task_title": next_row["task_title"],
        }

    def task_rollback(
        self,
        task_id: str,
        change_id: Optional[str] = None,
        reason: str = "",
    ) -> Dict[str, Any]:
        """回滚变更

        注意：本系统是图谱系统而非文件系统管理器，只记录回滚意图和元数据，
        不直接操作文件系统。调用方应根据返回的变更信息自行恢复文件内容。

        Args:
            task_id: 任务 ID
            change_id: 指定回滚的变更 ID，None 表示回滚该任务全部变更
            reason: 回滚原因

        Returns:
            回滚结果 dict，包含：
            - rolled_back_changes: 回滚的变更列表（每项含 original_change_id,
              file_path, hash_before, hash_after, restorable）
            - task_status: 任务最终状态（reverted）
            - note: 操作说明
        """
        now = time.time()

        # 查找需要回滚的变更记录
        if change_id:
            cur = self.conn.execute(
                "SELECT * FROM change_audit WHERE id = ? AND task_id = ?",
                (change_id, task_id),
            )
        else:
            cur = self.conn.execute(
                "SELECT * FROM change_audit WHERE task_id = ? ORDER BY timestamp ASC",
                (task_id,),
            )

        changes = [dict(row) for row in cur]
        rolled_back: List[Dict[str, Any]] = []

        # 为每条原始变更记录一条回滚操作（hash 前后对调）
        for change in changes:
            rollback_change_id = _gen_change_id()
            rollback_diff = f"[ROLLBACK] reason={reason or 'unspecified'}"

            self.conn.execute(
                """
                INSERT INTO change_audit
                    (id, task_id, step_id, file_path, hash_before, hash_after,
                     diff, author, timestamp)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    rollback_change_id,
                    task_id,
                    change.get("step_id"),
                    change["file_path"],
                    # 回滚后哈希 = 原变更前哈希；回滚前哈希 = 原变更后哈希
                    change.get("hash_after", ""),
                    change.get("hash_before", ""),
                    rollback_diff,
                    "agent",
                    now,
                ),
            )

            rolled_back.append({
                "original_change_id": change["id"],
                "file_path": change["file_path"],
                "hash_before": change.get("hash_before", ""),
                "hash_after": change.get("hash_after", ""),
                "restorable": bool(change.get("hash_before")),
            })

        # 将任务状态改为 reverted，并记录关闭时间
        self.conn.execute(
            "UPDATE tasks SET status = ?, updated_at = ?, closed_at = ? WHERE id = ?",
            (TASK_STATUS_REVERTED, now, now, task_id),
        )

        self.conn.commit()

        return {
            "rolled_back_changes": rolled_back,
            "task_status": TASK_STATUS_REVERTED,
            "note": (
                "仅记录回滚意图，未操作文件系统。"
                "调用方应根据 rolled_back_changes 中的 hash_before 自行恢复文件内容。"
            ),
        }

    def task_list(
        self,
        status_filter: Optional[str] = None,
        limit: int = 20,
    ) -> List[Dict[str, Any]]:
        """列出任务

        Args:
            status_filter: 状态过滤（open/in_progress/review/applied/closed/reverted）
                           None 表示不过滤
            limit: 返回数量限制

        Returns:
            任务摘要列表，每个元素为 dict：
            {task_id, title, status, step_count, created_at}
        """
        if status_filter:
            cur = self.conn.execute(
                """
                SELECT t.id as task_id, t.title, t.status, t.created_at,
                       (SELECT COUNT(*) FROM task_steps ts WHERE ts.task_id = t.id) as step_count
                FROM tasks t
                WHERE t.status = ?
                ORDER BY t.created_at DESC
                LIMIT ?
                """,
                (status_filter, limit),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT t.id as task_id, t.title, t.status, t.created_at,
                       (SELECT COUNT(*) FROM task_steps ts WHERE ts.task_id = t.id) as step_count
                FROM tasks t
                ORDER BY t.created_at DESC
                LIMIT ?
                """,
                (limit,),
            )

        return [dict(row) for row in cur]

    def task_status(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务详情

        Args:
            task_id: 任务 ID

        Returns:
            任务详情 dict，包含：
            - task_id, title, description, status, creator
            - created_at, updated_at, closed_at
            - steps: 步骤列表，每个元素含 step_id, step_index, action, target_file,
                     target_symbol, check_items, status, result, created_at, completed_at
            如果任务不存在，返回 None
        """
        cur = self.conn.execute(
            "SELECT * FROM tasks WHERE id = ?",
            (task_id,),
        )
        task_row = cur.fetchone()
        if not task_row:
            return None

        cur = self.conn.execute(
            """
            SELECT id as step_id, step_index, action, target_file, target_symbol,
                   check_items, status, result, created_at, completed_at
            FROM task_steps
            WHERE task_id = ?
            ORDER BY step_index ASC
            """,
            (task_id,),
        )
        steps = []
        for row in cur:
            steps.append({
                "step_id": row["step_id"],
                "step_index": row["step_index"],
                "action": row["action"],
                "target_file": row["target_file"],
                "target_symbol": row["target_symbol"],
                "check_items": _deserialize_check_items(row["check_items"]),
                "status": row["status"],
                "result": row["result"],
                "created_at": row["created_at"],
                "completed_at": row["completed_at"],
            })

        return {
            "task_id": task_row["id"],
            "title": task_row["title"],
            "description": task_row["description"],
            "status": task_row["status"],
            "creator": task_row["creator"],
            "created_at": task_row["created_at"],
            "updated_at": task_row["updated_at"],
            "closed_at": task_row["closed_at"],
            "steps": steps,
        }
