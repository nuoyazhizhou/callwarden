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
        parent_id: str = "",
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
            parent_id: 父任务 ID（可选），用于构建任务树

        Returns:
            新建任务的 task_id
        """
        now = time.time()
        task_id = _gen_task_id()

        # 计算 depth 和 sort_order
        depth = 0
        sort_order = 0
        if parent_id:
            cur = self.conn.execute(
                "SELECT depth FROM tasks WHERE id = ?",
                (parent_id,),
            )
            parent_row = cur.fetchone()
            if parent_row:
                depth = parent_row["depth"] + 1
            # 计算同级排序
            cur = self.conn.execute(
                "SELECT COALESCE(MAX(sort_order), -1) as max_order FROM tasks WHERE parent_id = ?",
                (parent_id,),
            )
            sort_order = cur.fetchone()["max_order"] + 1

        # 插入任务记录，初始状态为 open
        self.conn.execute(
            """
            INSERT INTO tasks (id, title, description, creator, status, created_at, updated_at,
                               parent_id, depth, sort_order)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (task_id, title, description, creator, TASK_STATUS_OPEN, now, now,
             parent_id, depth, sort_order),
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

        若任务有子任务，采用深度优先遍历：
        - 优先下钻到最底层子任务（按 sort_order 从左到右）
        - 子任务全部完成后才回到父任务的自身步骤
        - 这样保证 Agent 永远在处理最具体的小任务，不会遗漏

        查找该任务树中第一个 status=pending 的步骤，将其状态改为 in_progress。
        如果任务状态是 open，改为 in_progress。

        Before-Edit Contract（编辑前契约）：
        - 当步骤 action 属于编辑类操作（见 EDIT_ACTIONS）且 target_file 非空时，
          自动调用 self.check_before_edit(target_file) 执行护栏阻断式检查。
        - decision == "block"：步骤状态改为 blocked（而非 in_progress），
          返回 guardrail_alert 字段告知 Agent 必须先处理告警。
        - decision == "warn"：步骤仍进入 in_progress，但返回 guardrail_warning 字段。
        - decision == "pass" 或非编辑类步骤：正常流程。

        Args:
            task_id: 任务 ID（可以是根任务或任意子任务）

        Returns:
            步骤详情 dict，包含：
            - step_id, action, target_file, target_symbol, check_items
            - task_id, task_title, step_index
            - parent_task_chain: 从根到当前步骤所属任务的祖先链（含自身）
            - guardrail_alert（仅 block 时存在）：{decision, message, findings}
            - guardrail_warning（仅 warn 时存在）：{decision, message, findings}
            如果没有待执行步骤，返回 None
        """
        # 深度优先遍历任务树，找到第一个 pending 步骤
        row = self._find_next_pending_step_tree(task_id)
        if not row:
            return None

        step_id = row["step_id"]
        actual_task_id = row["task_id"]
        now = time.time()
        action = row["action"] or ""
        target_file = row["target_file"] or ""

        # Before-Edit Contract：编辑类动作且存在目标文件时，触发护栏检查
        guardrail_alert: Optional[Dict[str, Any]] = None
        guardrail_warning: Optional[Dict[str, Any]] = None
        new_status = STEP_STATUS_IN_PROGRESS

        if action.lower() in EDIT_ACTIONS and target_file:
            try:
                gr_result = self.check_before_edit(target_file, proposed_change="")
                decision = (gr_result.get("decision") or "pass").lower()
                if decision == "block":
                    new_status = STEP_STATUS_BLOCKED
                    guardrail_alert = {
                        "decision": "block",
                        "message": gr_result.get("message", ""),
                        "findings": gr_result.get("findings", []),
                    }
                elif decision == "warn":
                    guardrail_warning = {
                        "decision": "warn",
                        "message": gr_result.get("message", ""),
                        "findings": gr_result.get("findings", []),
                    }
            except Exception as exc:
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

        # 根任务状态从 open → in_progress
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

        # 步骤所属任务也改为 in_progress（如果还是 open）
        if actual_task_id != task_id:
            cur = self.conn.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (actual_task_id,),
            )
            actual_task_row = cur.fetchone()
            if actual_task_row and actual_task_row["status"] == TASK_STATUS_OPEN:
                self.conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (TASK_STATUS_IN_PROGRESS, now, actual_task_id),
                )

        self.conn.commit()

        # 构建父任务链（从根到当前任务）
        parent_chain = self._build_parent_chain(actual_task_id)

        result: Dict[str, Any] = {
            "step_id": step_id,
            "task_id": actual_task_id,
            "step_index": row["step_index"],
            "action": row["action"],
            "target_file": row["target_file"],
            "target_symbol": row["target_symbol"],
            "check_items": _deserialize_check_items(row["check_items"]),
            "task_title": row["task_title"],
            "status": new_status,
            "parent_task_chain": parent_chain,
        }
        if guardrail_alert:
            result["guardrail_alert"] = guardrail_alert
        if guardrail_warning:
            result["guardrail_warning"] = guardrail_warning

        # 构建结构化指令
        try:
            result["structured_instruction"] = self.build_structured_instruction(result)
        except Exception:
            result["structured_instruction"] = None

        return result

    def _build_parent_chain(self, task_id: str) -> List[Dict[str, Any]]:
        """构建从根任务到当前任务的祖先链（含自身）

        Args:
            task_id: 当前任务 ID

        Returns:
            祖先列表，按根→叶顺序排列，每个元素为 {task_id, title, status, depth}
        """
        chain = []
        current_id = task_id

        # 从当前任务向上追溯到根
        temp_chain = []
        while current_id:
            cur = self.conn.execute(
                "SELECT id, title, status, depth FROM tasks WHERE id = ?",
                (current_id,),
            )
            row = cur.fetchone()
            if not row:
                break
            temp_chain.append({
                "task_id": row["id"],
                "title": row["title"],
                "status": row["status"],
                "depth": row["depth"],
            })
            # 取父任务 ID
            cur2 = self.conn.execute(
                "SELECT parent_id FROM tasks WHERE id = ?",
                (current_id,),
            )
            parent_row = cur2.fetchone()
            if not parent_row or not parent_row["parent_id"]:
                break
            current_id = parent_row["parent_id"]

        # 反转成根→叶顺序
        chain = list(reversed(temp_chain))
        return chain

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
        - 父子任务支持：子任务完成后递归向上检查，所有子任务完成则父任务进入 review
        - task_id 可以是根任务 ID 或任意子任务 ID，通过 step_id 反查真实所属任务

        Args:
            task_id: 任务 ID（根任务或子任务均可）
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

        # 通过 step_id 找到步骤实际所属的任务 ID（支持父子任务）
        cur = self.conn.execute(
            "SELECT task_id FROM task_steps WHERE id = ?",
            (step_id,),
        )
        step_row = cur.fetchone()
        actual_task_id = step_row["task_id"] if step_row else task_id

        # 更新步骤状态、结果和完成时间
        self.conn.execute(
            """
            UPDATE task_steps
            SET status = ?, result = ?, completed_at = ?
            WHERE id = ? AND task_id = ?
            """,
            (new_status, result, now, step_id, actual_task_id),
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
                        actual_task_id,
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
            cur = self.conn.execute(
                "SELECT MAX(step_index) as max_idx FROM task_steps WHERE task_id = ?",
                (actual_task_id,),
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
                    actual_task_id,
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

        # 更新步骤所属任务的 updated_at
        self.conn.execute(
            "UPDATE tasks SET updated_at = ? WHERE id = ?",
            (now, actual_task_id),
        )
        # 也更新根任务的 updated_at
        if task_id != actual_task_id:
            self.conn.execute(
                "UPDATE tasks SET updated_at = ? WHERE id = ?",
                (now, task_id),
            )

        # ---- F6 检查门禁 ----
        gate_failed = False
        if success and changes:
            changed_files = [c.get("file_path", "") for c in changes if c.get("file_path")]
            if changed_files and hasattr(self, "run_check_gate"):
                try:
                    gate_result = self.run_check_gate(actual_task_id, step_id, changed_files)
                    if not gate_result["passed"]:
                        gate_failed = True
                        cur = self.conn.execute(
                            "SELECT MAX(step_index) as max_idx FROM task_steps WHERE task_id = ?",
                            (actual_task_id,),
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
                                actual_task_id,
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
                    pass

        # 成功且没有更多 pending 步骤时，将任务状态改为 review
        if success and not gate_failed:
            cur = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM task_steps WHERE task_id = ? AND status = ?",
                (actual_task_id, STEP_STATUS_PENDING),
            )
            pending_count = cur.fetchone()["cnt"]
            if pending_count == 0:
                cur = self.conn.execute(
                    "SELECT status FROM tasks WHERE id = ?",
                    (actual_task_id,),
                )
                t_status = cur.fetchone()["status"]
                if t_status in (TASK_STATUS_OPEN, TASK_STATUS_IN_PROGRESS):
                    self.conn.execute(
                        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                        (TASK_STATUS_REVIEW, now, actual_task_id),
                    )
                # 子任务完成后，递归向上更新父任务状态
                self._update_parent_status(actual_task_id)

        self.conn.commit()

        # 返回任务树中的下一步步骤信息（深度优先）
        next_row = self._find_next_pending_step_tree(task_id)
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

    # --------------------------------------------------------------------
    # 父子任务相关方法
    # --------------------------------------------------------------------

    def _get_direct_subtasks(self, task_id: str) -> List[Dict[str, Any]]:
        """获取任务的直接子任务列表（按 sort_order 排序）

        Args:
            task_id: 父任务 ID

        Returns:
            子任务列表，每个元素为任务基本信息 dict
        """
        cur = self.conn.execute(
            """
            SELECT id, title, description, status, depth, sort_order, created_at, updated_at
            FROM tasks
            WHERE parent_id = ?
            ORDER BY sort_order ASC
            """,
            (task_id,),
        )
        return [dict(row) for row in cur]

    def _compute_task_progress(self, task_id: str) -> Dict[str, Any]:
        """计算任务的完成进度（包括子任务和自身步骤）

        进度 = 已完成步骤 / 总步骤数。若有子任务，则子任务的步骤也计入。

        Args:
            task_id: 任务 ID

        Returns:
            {"total": 总步骤数, "done": 已完成数, "progress": 0.0~1.0}
        """
        total = 0
        done = 0

        # 自身步骤
        cur = self.conn.execute(
            "SELECT status FROM task_steps WHERE task_id = ?",
            (task_id,),
        )
        for row in cur:
            total += 1
            if row["status"] in (STEP_STATUS_DONE, STEP_STATUS_SKIPPED):
                done += 1

        # 递归累加子任务
        subtasks = self._get_direct_subtasks(task_id)
        for st in subtasks:
            sub_progress = self._compute_task_progress(st["id"])
            total += sub_progress["total"]
            done += sub_progress["done"]

        progress = (done / total) if total > 0 else 0.0
        return {"total": total, "done": done, "progress": progress}

    def _find_next_pending_step_tree(self, task_id: str) -> Optional[Dict[str, Any]]:
        """深度优先遍历任务树，找到下一个待执行步骤

        优先下钻到最底层子任务，从左到右（按 sort_order），
        子任务全部完成才回到父任务的自身步骤。

        Args:
            task_id: 根任务 ID

        Returns:
            步骤信息 dict（含 task_id, step_id, step_index 等），没有则返回 None
        """
        subtasks = self._get_direct_subtasks(task_id)

        # 1. 先遍历所有子任务（深度优先）
        for st in subtasks:
            # 跳过已完成或已关闭的子任务
            if st["status"] in (TASK_STATUS_CLOSED, TASK_STATUS_APPLIED, TASK_STATUS_REVERTED):
                continue
            sub_step = self._find_next_pending_step_tree(st["id"])
            if sub_step:
                return sub_step

        # 2. 子任务都没有 pending 步骤，则看自身步骤
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
        if row:
            return dict(row)

        return None

    def task_create_subtask(
        self,
        parent_task_id: str,
        title: str,
        description: str = "",
        steps: Optional[List[Dict[str, Any]]] = None,
        creator: str = "agent",
    ) -> str:
        """在父任务下创建子任务

        子任务的 depth = 父任务 depth + 1，sort_order 自动递增。

        Args:
            parent_task_id: 父任务 ID
            title: 子任务标题
            description: 子任务描述
            steps: 子任务步骤列表
            creator: 创建者标识

        Returns:
            新建子任务的 task_id
        """
        return self.task_create(
            title=title,
            description=description,
            steps=steps,
            creator=creator,
            parent_id=parent_task_id,
        )

    def task_split(
        self,
        task_id: str,
        subtasks: List[Dict[str, Any]],
    ) -> List[str]:
        """将一个大任务拆分为多个子任务

        当检测到任务过大时使用。原任务的自身步骤保留为汇总/验证步骤，
        具体工作拆分为子任务执行。

        Args:
            task_id: 要拆分的父任务 ID
            subtasks: 子任务定义列表，每个元素为 dict：
                     {title, description, steps}
                     - title: 子任务标题
                     - description: 子任务描述（可选）
                     - steps: 子任务步骤列表（可选）

        Returns:
            新建子任务的 ID 列表（顺序与输入一致）
        """
        new_ids = []
        for st_def in subtasks:
            sub_id = self.task_create_subtask(
                parent_task_id=task_id,
                title=st_def.get("title", ""),
                description=st_def.get("description", ""),
                steps=st_def.get("steps", []),
            )
            new_ids.append(sub_id)

        # 如果原任务如果是 open，确保保持 open；其自身步骤在所有子任务之后执行
        # （_find_next_pending_step_tree 的深度优先策略自然保证）
        return new_ids

    def _update_parent_status(self, task_id: str):
        """递归更新父任务状态：当所有子任务+自身步骤都完成时，父任务进入 review

        从当前任务向上递归，检查每一层父任务是否满足完成条件。
        完成条件：所有直接子任务已关闭 + 自身步骤全部 done。

        Args:
            task_id: 当前完成的任务 ID
        """
        now = time.time()

        # 获取当前任务的父任务
        cur = self.conn.execute(
            "SELECT parent_id FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if not row or not row["parent_id"]:
            return  # 没有父任务，停止

        parent_id = row["parent_id"]

        # 检查父任务的所有子任务状态
        subtasks = self._get_direct_subtasks(parent_id)
        all_subtasks_done = True
        for st in subtasks:
            if st["status"] not in (
                TASK_STATUS_CLOSED, TASK_STATUS_APPLIED,
                TASK_STATUS_REVIEW, TASK_STATUS_REVERTED
            ):
                all_subtasks_done = False
                break

        # 检查父任务自身的步骤
        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM task_steps WHERE task_id = ? AND status = ?",
            (parent_id, STEP_STATUS_PENDING),
        )
        own_pending = cur.fetchone()["cnt"]

        # 所有子任务完成 且 自身没有 pending 步骤 → 父任务进入 review
        if all_subtasks_done and own_pending == 0:
            cur = self.conn.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (parent_id,),
            )
            parent_status = cur.fetchone()["status"]
            if parent_status in (TASK_STATUS_OPEN, TASK_STATUS_IN_PROGRESS):
                self.conn.execute(
                    "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                    (TASK_STATUS_REVIEW, now, parent_id),
                )
                self.conn.commit()  # 立即提交，保证上层递归时能读到最新状态

        # 继续向上递归
        self._update_parent_status(parent_id)

    def task_status_tree(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取任务树详情（含子任务树和进度）

        Args:
            task_id: 根任务 ID

        Returns:
            任务树 dict，包含：
            - task_id, title, description, status, creator
            - depth, sort_order
            - progress: {total, done, progress}
            - steps: 自身步骤列表
            - subtasks: 子任务树列表（递归结构）
            任务不存在返回 None
        """
        # 获取自身详情
        cur = self.conn.execute(
            "SELECT * FROM tasks WHERE id = ?",
            (task_id,),
        )
        task_row = cur.fetchone()
        if not task_row:
            return None

        # 自身步骤
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

        # 递归子任务
        subtasks = self._get_direct_subtasks(task_id)
        subtask_trees = []
        for st in subtasks:
            sub_tree = self.task_status_tree(st["id"])
            if sub_tree:
                subtask_trees.append(sub_tree)

        # 进度
        progress = self._compute_task_progress(task_id)

        return {
            "task_id": task_row["id"],
            "title": task_row["title"],
            "description": task_row["description"],
            "status": task_row["status"],
            "creator": task_row["creator"],
            "depth": task_row["depth"],
            "sort_order": task_row["sort_order"],
            "created_at": task_row["created_at"],
            "updated_at": task_row["updated_at"],
            "closed_at": task_row["closed_at"],
            "progress": progress,
            "steps": steps,
            "subtasks": subtask_trees,
        }
