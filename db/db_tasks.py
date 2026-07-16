"""
db_tasks.py
===========

任务驱动 MCP 系统 Mixin 类。

提供任务创建、步骤领取、结果回报、回滚、查询等功能。
基于 tasks / task_steps / change_audit 三张表实现，不依赖工作区。
"""

from __future__ import annotations

import json
import os
import secrets
import time
from typing import Any, Dict, List, Optional

from ..i18n import t
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

    格式: {prefix}-{timestamp_ms}-{random8hex}

    后缀 8 位 hex（32 bit，~42 亿种）而非 4 位 hex：
    4 位 hex 在毫秒内连续生成 100 个 ID 时按生日悖论有 ~7.3% 碰撞概率；
    8 位 hex 将此概率降到 ~10⁻⁶，足以支撑快速循环调用。

    Args:
        prefix: ID 前缀（T / S / C）

    Returns:
        ID 字符串
    """
    ts_ms = int(time.time() * 1000)
    rand8 = secrets.token_hex(4)  # 4 字节 = 8 个十六进制字符
    return f"{prefix}-{ts_ms}-{rand8}"


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

        Soft Warning（C1 新增）：
        当 parent_id 为空（根任务）且 steps 数量 > 5 或涉及文件 > 3 时，
        通过 stderr 输出 soft warning 建议使用 task_split 创建父子任务树，
        避免创建难以管理的孤儿任务。warning 不阻断创建，仅提示。
        """
        # C1: 孤儿任务 soft warning（不阻断，仅提示）
        if not parent_id and steps:
            self._check_orphan_task_warning(title, steps)

        now = time.time()
        task_id = _gen_task_id()

        # 计算 depth 和 sort_order
        depth = 0
        sort_order = 0
        if parent_id:
            cur = self.conn.execute(
                "SELECT depth, status FROM tasks WHERE id = ?",
                (parent_id,),
            )
            parent_row = cur.fetchone()
            if parent_row:
                depth = parent_row["depth"] + 1
                # Reopen 机制：当父任务处于 review/applied/closed 状态时，自动 reopen
                # 父任务链为 in_progress（清理 applied_at/closed_at）
                # 父任务 open/in_progress 时直接挂，不改状态
                # task_create 场景 check_siblings=True：检查兄弟子任务状态
                #   - 所有兄弟子任务都是 closed（或无兄弟）→ reopen 父任务
                #   - 有兄弟子任务非 closed → 直接挂，不 reopen
                self._reopen_parent_chain_if_needed(
                    parent_id, parent_row["status"], check_siblings=True
                )

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

    def set_active_task(self, task_id: str) -> None:
        """设置当前 workspace 的 active task

        在 task_next_step 进入 in_progress / task_reopen 回到 in_progress 时调用。
        幂等：重复设置同一 task_id 不产生副作用。
        覆盖语义：若之前已有 active_task，直接覆盖（用户显式 claim 新任务）。

        Args:
            task_id: 任务 ID（空串表示清除）
        """
        self.conn.execute(
            "UPDATE workspaces SET active_task_id = ? WHERE is_active = 1",
            (task_id,),
        )
        self.conn.commit()

    def get_active_task(self) -> Optional[str]:
        """读取当前 workspace 的 active task_id

        用于 task_capture_diff_auto 优先读取，替代 CALLWARDEN_TASK_ID 环境变量。

        Returns:
            active task_id，无则返回 None
        """
        cur = self.conn.execute(
            "SELECT active_task_id FROM workspaces WHERE is_active = 1"
        )
        row = cur.fetchone()
        if not row:
            return None
        tid = row["active_task_id"] or ""
        return tid if tid else None

    def clear_active_task(self, task_id: str = "") -> None:
        """清除当前 workspace 的 active task

        在 task_close 成功后调用。防御性：传入 task_id 时仅当 active_task == task_id
        才清除，避免误清除后续已 claim 的新任务；传入空串时无条件清除。

        Args:
            task_id: 已关闭的任务 ID（防御性匹配）；空串表示无条件清除
        """
        if task_id:
            self.conn.execute(
                "UPDATE workspaces SET active_task_id = '' "
                "WHERE is_active = 1 AND active_task_id = ?",
                (task_id,),
            )
        else:
            self.conn.execute(
                "UPDATE workspaces SET active_task_id = '' WHERE is_active = 1"
            )
        self.conn.commit()

    def is_task_active(self, task_id: str) -> bool:
        """校验 task_id 是否存在且处于活跃状态（open / in_progress）

        L1 软门禁基础设施：为 propose_edit 系列提供 task_id 真实性校验。
        不改变 task 状态，纯只读查询。

        Args:
            task_id: 任务 ID

        Returns:
            True 表示 task 存在且 status ∈ {open, in_progress}；
            False 表示 task 不存在或已 review/applied/closed
        """
        if not task_id:
            return False
        cur = self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            return False
        return row["status"] in (TASK_STATUS_OPEN, TASK_STATUS_IN_PROGRESS)

    def get_task_context(self, task_id: str) -> Optional[Dict[str, Any]]:
        """获取 task 上下文（L1 赋能激励：让 Agent 看到关联 task_id 的价值）

        轻量级查询，返回 task 基本信息 + steps 概况，不查 callers/impact
        等重数据（Agent 可通过专用 MCP 工具按需获取）。

        Args:
            task_id: 任务 ID

        Returns:
            {
                "task_id": str,
                "title": str,
                "status": str,
                "is_active_task": bool,     # 是否当前 workspace 的 active task
                "steps": {
                    "total": int,
                    "completed": int,
                    "in_progress": int,
                },
            }
            task 不存在时返回 None
        """
        cur = self.conn.execute(
            "SELECT id, title, status FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        # steps 概况
        cur_steps = self.conn.execute(
            "SELECT status FROM task_steps WHERE task_id = ?",
            (task_id,),
        )
        steps_total = 0
        steps_completed = 0
        steps_in_progress = 0
        for srow in cur_steps.fetchall():
            steps_total += 1
            s = srow["status"]
            if s == "completed":
                steps_completed += 1
            elif s == "in_progress":
                steps_in_progress += 1
        # 是否当前 workspace 的 active task
        is_active_task = False
        try:
            active_tid = self.get_active_task()
            is_active_task = (active_tid == task_id)
        except Exception:
            pass
        return {
            "task_id": row["id"],
            "title": row["title"],
            "status": row["status"],
            "is_active_task": is_active_task,
            "steps": {
                "total": steps_total,
                "completed": steps_completed,
                "in_progress": steps_in_progress,
            },
        }

    def _reopen_parent_chain_if_needed(
        self,
        parent_id: str,
        parent_status: str,
        check_siblings: bool = False,
    ) -> None:
        """当父任务处于 review/applied/closed 状态时，reopen 父任务链为 in_progress

        Reopen 机制（支持已 closed 父任务添加新子任务）：
        - 父任务 open/in_progress：直接挂，不改状态
        - 父任务 review/applied/closed：
          - check_siblings=True（task_create 场景）：
            - 所有兄弟子任务都是 closed（或无兄弟）→ reopen 父任务为 in_progress
            - 有兄弟子任务非 closed（如 open/in_progress）→ 不 reopen，直接挂
          - check_siblings=False（task_reopen 递归场景）：无条件 reopen
        - 递归向上 reopen 祖父任务链（check_siblings=False，因为祖先链已被触发）

        Args:
            parent_id: 父任务 ID
            parent_status: 父任务当前状态
            check_siblings: 是否检查兄弟子任务状态（task_create 场景为 True）
        """
        # 仅当父任务处于 review/applied/closed 时才需要 reopen
        REOPEN_STATUSES = (TASK_STATUS_REVIEW, TASK_STATUS_APPLIED, TASK_STATUS_CLOSED)
        if parent_status not in REOPEN_STATUSES:
            return  # open/in_progress，直接挂，不改状态

        # task_create 场景：检查兄弟子任务状态
        # - 所有兄弟子任务都是 closed（或无兄弟）→ reopen 父任务
        # - 有兄弟子任务非 closed → 直接挂，不 reopen
        if check_siblings:
            cur = self.conn.execute(
                "SELECT status FROM tasks WHERE parent_id = ?",
                (parent_id,),
            )
            siblings = cur.fetchall()
            if siblings and any(
                s["status"] != TASK_STATUS_CLOSED for s in siblings
            ):
                # 有兄弟子任务非 closed，直接挂，不 reopen 父任务
                return

        now = time.time()

        # Reopen 当前父任务为 in_progress，清理时间戳
        self.conn.execute(
            "UPDATE tasks SET status = ?, applied_at = NULL, closed_at = NULL, "
            "updated_at = ? WHERE id = ?",
            (TASK_STATUS_IN_PROGRESS, now, parent_id),
        )

        # 在 audit_chain 中记录 reopen 事件（fail-soft，不阻断流程）
        try:
            if hasattr(self, "sign_audit_record"):
                self.sign_audit_record(
                    "tasks",
                    parent_id,
                    {
                        "task_id": parent_id,
                        "operation": "reopen",
                        "previous_status": parent_status,
                        "new_status": TASK_STATUS_IN_PROGRESS,
                        "reason": "new subtask created after parent closed",
                        "timestamp": now,
                    },
                    operation="update",
                )
        except Exception:
            pass  # fail-soft，审计签名失败不阻断 reopen

        # 递归向上 reopen 祖父任务链
        cur = self.conn.execute(
            "SELECT parent_id FROM tasks WHERE id = ?",
            (parent_id,),
        )
        row = cur.fetchone()
        if row and row["parent_id"]:
            grandparent_id = row["parent_id"]
            cur = self.conn.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (grandparent_id,),
            )
            grandparent_row = cur.fetchone()
            if grandparent_row:
                self._reopen_parent_chain_if_needed(
                    grandparent_id, grandparent_row["status"]
                )

    def _check_orphan_task_warning(
        self,
        title: str,
        steps: List[Dict[str, Any]],
    ) -> None:
        """检查是否可能创建孤儿任务，输出 soft warning（C1 新增）

        判断条件（满足任一即触发 warning）：
        - steps 数量 > 5（任务步骤过多，难以管理）
        - 涉及不同文件数 > 3（跨文件改动应拆分为子任务）

        warning 不阻断创建，仅通过 stderr 输出提示，建议使用 task_split
        创建父子任务树，避免孤儿任务难以追踪和回滚。

        Args:
            title: 任务标题（用于 warning 消息）
            steps: 步骤列表（用于统计数量和涉及文件数）
        """
        import sys

        step_count = len(steps)
        # 统计涉及的不同文件数
        files_set = set()
        for step in steps:
            target_file = step.get("target_file", "")
            if target_file:
                # 可能是 "file1.py + file2.py" 形式，拆分统计
                for f in str(target_file).split("+"):
                    f = f.strip()
                    if f:
                        files_set.add(f)
        file_count = len(files_set)

        # 阈值：steps > 5 或 files > 3
        if step_count <= 5 and file_count <= 3:
            return

        # 输出 soft warning 到 stderr（不阻断创建）
        warning = t(
            "cli.messages.task_orphan_warning",
            title=title,
            step_count=step_count,
            file_count=file_count,
            default=(
                f"[Soft Warning] Task '{title}' has {step_count} steps and "
                f"involves {file_count} files. Consider using task_split to "
                f"create a parent-child task tree for better manageability."
            ),
        )
        print(warning, file=sys.stderr)

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

        # 质量门禁优先级：若任务树中存在 fix_quality_gate_failure 的 pending 步骤，
        # 优先返回它（让 Agent 先处理质量门禁失败，再继续普通步骤）
        try:
            fix_row = self.conn.execute(
                """
                SELECT ts.id as step_id, ts.task_id, ts.step_index, ts.action,
                       ts.target_file, ts.target_symbol, ts.check_items,
                       t.title as task_title
                FROM task_steps ts
                JOIN tasks t ON ts.task_id = t.id
                WHERE ts.task_id IN (
                    WITH RECURSIVE task_tree(id) AS (
                        SELECT id FROM tasks WHERE id = ?
                        UNION ALL
                        SELECT t.id FROM tasks t JOIN task_tree tt ON t.parent_id = tt.id
                    )
                    SELECT id FROM task_tree
                )
                AND ts.action = 'fix_quality_gate_failure'
                AND ts.status = ?
                ORDER BY ts.created_at ASC
                LIMIT 1
                """,
                (task_id, STEP_STATUS_PENDING),
            ).fetchone()
            if fix_row:
                row = fix_row
        except Exception:
            pass

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
                    "message": t(
                        "cli.messages.task_guardrail_check_failed",
                        default="Guardrail check failed and was downgraded to warn: {error}",
                        error=exc,
                    ),
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

        # 领取子任务时，把从根任务到 actual_task_id 路径上的所有 open 状态父任务推进到 in_progress
        # （深度优先遍历时，中间层父任务可能仍是 open，需同步推进）
        if actual_task_id != task_id:
            parent_chain = self._build_parent_chain(actual_task_id)
            for chain_item in parent_chain:
                chain_id = chain_item["task_id"]
                chain_status = chain_item["status"]
                if chain_status == TASK_STATUS_OPEN:
                    self.conn.execute(
                        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                        (TASK_STATUS_IN_PROGRESS, now, chain_id),
                    )

        self.conn.commit()

        # P1: 持久化 active_task（替代 CALLWARDEN_TASK_ID 环境变量）
        # task_next_step 进入 in_progress 后自动设置，task_close 时清除
        self.set_active_task(task_id)

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

        # 附加 open_quality_findings 摘要（让 Agent 知道任务下有哪些未解决的质量发现）
        if hasattr(self, "get_task_quality_findings"):
            try:
                open_findings = self.get_task_quality_findings(actual_task_id, status="open")
                result["open_quality_findings"] = {
                    "count": len(open_findings),
                    "blocking": sum(1 for f in open_findings if f.get("severity") in ("error", "block")),
                    "items": [
                        {
                            "id": f["id"],
                            "severity": f["severity"],
                            "finding_type": f["finding_type"],
                            "message": f["message"],
                        }
                        for f in open_findings[:10]  # 最多 10 条摘要
                    ],
                }
            except Exception:
                pass

        # 构建结构化指令
        try:
            result["structured_instruction"] = self.build_structured_instruction(result)
        except Exception:
            result["structured_instruction"] = None

        # 注入适用规则（applicable_rules）：基于 step 的 action/target_file/
        # target_symbol 构造上下文，从 agent_rules 中匹配 active 规则。
        # fail-soft：规则注入失败时降级为空列表，不影响任务领取主流程。
        result["applicable_rules"] = self._inject_applicable_rules(result)

        return result

    def work_next_job(self, task_id: str) -> Optional[Dict[str, Any]]:
        """领取下一项 Agent 工作，并附带最小可执行上下文

        这是面向 Agent 的高层入口：相比 task_next_step 只返回步骤，
        本方法会补齐目标符号源码、文件健康、调用上下文、允许编辑范围
        和推荐检查，让 Agent 不需要手动组合 read/grep/guardrail。
        """
        step = self.task_next_step(task_id)
        if step is None:
            return None

        job: Dict[str, Any] = {
            "job_id": step.get("step_id", ""),
            "task_id": step.get("task_id", ""),
            "task_title": step.get("task_title", ""),
            "job_type": step.get("action") or "todo",
            "target_file": step.get("target_file", ""),
            "target_symbol": step.get("target_symbol", ""),
            "status": step.get("status", ""),
            "parent_task_chain": step.get("parent_task_chain", []),
            "check_items": step.get("check_items", ""),
            "context": {},
            "allowed_edits": [],
            "recommended_tools": [],
            "checks": [],
            "report_with": {
                "tool": "task_report_step",
                "task_id": step.get("task_id", ""),
                "step_id": step.get("step_id", ""),
            },
        }

        structured = step.get("structured_instruction") or {}
        if structured:
            job["structured_instruction"] = structured
            job["allowed_edits"] = structured.get("constraints", [])
            job["checks"] = structured.get("checks", [])

        if step.get("guardrail_alert"):
            job["guardrail_alert"] = step["guardrail_alert"]
        if step.get("guardrail_warning"):
            job["guardrail_warning"] = step["guardrail_warning"]

        target_file = step.get("target_file") or ""
        target_symbol = step.get("target_symbol") or ""

        if target_file and hasattr(self, "check_file_health"):
            try:
                job["context"]["file_health"] = self.check_file_health(target_file)
            except Exception:
                pass

        if target_symbol and hasattr(self, "get_symbol"):
            try:
                sym = self.get_symbol(target_symbol)
            except Exception:
                sym = None
            if sym:
                symbol_id = 0
                try:
                    row = self.conn.execute(
                        """
                        SELECT s.id
                        FROM symbols s
                        JOIN file_instances fi ON s.file_instance_id = fi.id
                        WHERE s.qualified_name = ?
                          AND (? = '' OR fi.rel_path = ? OR fi.abs_path = ?)
                        LIMIT 1
                        """,
                        (
                            sym.get("qualified_name", target_symbol),
                            sym.get("file_path", ""),
                            sym.get("file_path", ""),
                            sym.get("file_path", ""),
                        ),
                    ).fetchone()
                    symbol_id = int(row["id"]) if row else 0
                except Exception:
                    symbol_id = 0
                job["context"]["target_symbol"] = {
                    "symbol_id": symbol_id,
                    "qualified_name": sym.get("qualified_name", ""),
                    "name": sym.get("name", ""),
                    "kind": sym.get("kind", ""),
                    "signature": sym.get("signature", ""),
                    "file_path": sym.get("file_path", ""),
                    "start_line": sym.get("start_line", 0),
                    "end_line": sym.get("end_line", 0),
                    "content_hash": sym.get("content_hash", ""),
                    "has_comment": sym.get("has_comment", 0),
                    "comment_content": sym.get("comment_content", ""),
                }
                target_file = target_file or sym.get("file_path", "")
                source = self._read_symbol_source_for_job(sym)
                if source is not None:
                    job["context"]["target_source"] = source
                # 调用方/被调用方摘要：精简字段 + 总数（让 Agent 知道是否还有更多）
                _callers_all = sym.get("called_by") or []
                _callees_all = sym.get("calls_out") or []
                job["context"]["callers_total"] = len(_callers_all)
                job["context"]["callees_total"] = len(_callees_all)
                job["context"]["callers"] = [
                    {
                        "caller": c.get("caller_name", ""),
                        "file": c.get("caller_file", ""),
                        "call_line": c.get("call_line", 0),
                    }
                    for c in _callers_all[:8]
                ]
                job["context"]["callees"] = [
                    {
                        "callee": c.get("callee_name", ""),
                        "module": c.get("callee_module", ""),
                        "file": c.get("callee_file", ""),
                        "call_line": c.get("call_line", 0),
                    }
                    for c in _callees_all[:8]
                ]
                job["allowed_edit_scope"] = {
                    "type": "symbol",
                    "file_path": target_file,
                    "symbol_id": symbol_id,
                    "symbol_name": target_symbol,
                    "start_line": sym.get("start_line", 0),
                    "end_line": sym.get("end_line", 0),
                    "preferred_tool": "propose_symbol_patch",
                }

        if not job.get("allowed_edit_scope") and target_file:
            job["allowed_edit_scope"] = {
                "type": "file",
                "file_path": target_file,
                "preferred_tool": "propose_range_patch",
            }

        action = (step.get("action") or "").lower()
        if action in EDIT_ACTIONS:
            job["recommended_tools"] = [
                "propose_symbol_patch" if target_symbol else "propose_range_patch",
                "task_report_step",
            ]
        else:
            job["recommended_tools"] = ["task_report_step"]

        job["agent_guidance"] = t(
            "cli.messages.work_next_job_guidance",
            default=(
                "Prefer the context and allowed_edit_scope returned by this job; "
                "when writing, use patch tools from recommended_tools to avoid whole-file rewrites."
            ),
        )

        # 注入 project_rules：与 task_next_step 的 applicable_rules 同源，
        # 但 work_next_job 是面向 Agent 的主入口，应返回更完整但仍受 limit 控制的规则。
        # fail-soft：拉取失败时降级为空列表
        job["project_rules"] = step.get("applicable_rules") or []
        # 同时把规则摘要放到 context.applicable_rules，便于只读 context 的 Agent 也能看到
        try:
            job["context"]["applicable_rules"] = [
                {
                    "id": r.get("id", ""),
                    "title": r.get("title", ""),
                    "severity": r.get("severity", "info"),
                }
                for r in job["project_rules"]
            ]
        except Exception:
            job["context"]["applicable_rules"] = []
        return job

    def _read_symbol_source_for_job(self, sym: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """按符号行号读取最小源码片段"""
        file_path = sym.get("file_path") or sym.get("file") or ""
        if not file_path:
            return None
        abs_path = sym.get("abs_path") or file_path
        if not os.path.isabs(abs_path):
            abs_path = os.path.join(self.workspace_root, file_path)
        if not os.path.isfile(abs_path):
            return None
        try:
            with open(abs_path, "r", encoding="utf-8", errors="replace") as f:
                lines = f.readlines()
            start = max(1, int(sym.get("start_line") or 1))
            end = max(start, int(sym.get("end_line") or start))
            content = "".join(lines[start - 1:end])
            return {
                "file_path": file_path,
                "start_line": start,
                "end_line": end,
                "content": content,
            }
        except Exception:
            return None

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
            "message": t(
                "cli.messages.task_guardrail_resolved",
                default="Guardrail alert resolved; step restored to pending and can be claimed again",
            ),
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
            instruction["constraints"] = t(
                "cli.messages.task_constraints_annotate",
                default=[
                    "Only add comments; do not change function logic",
                    "Comment language: Chinese",
                    "Use the target language's comment style (Rust: /// or /** */, Python: # or docstring)",
                ],
            )
            instruction["checks"] = ["syntax", "semgrep_quick"]
        elif action in ("refactor", "refactor_function"):
            instruction["constraints"] = t(
                "cli.messages.task_constraints_refactor",
                default=[
                    "Keep external behavior unchanged (signature, return value, side effects)",
                    "Run syntax checks after changes",
                    "Update callers when public API changes are involved",
                ],
            )
            instruction["checks"] = ["syntax", "semgrep"]
        elif action in ("fix", "fix_defect", "fix_gate_failure"):
            instruction["constraints"] = t(
                "cli.messages.task_constraints_fix",
                default=[
                    "Fix only the reported issue; avoid unrelated changes",
                    "The previous check gate must pass after the fix",
                ],
            )
            instruction["checks"] = ["syntax", "semgrep"]
        elif action in ("edit", "propose_edit", "write"):
            instruction["constraints"] = t(
                "cli.messages.task_constraints_edit",
                default=[
                    "Use propose_edit for writes; do not write directly to the filesystem",
                    "Run dry_run first to inspect the diff",
                    "Provide expected_hash to prevent concurrent overwrite",
                ],
            )
            instruction["checks"] = ["syntax"]
        else:
            instruction["constraints"] = t(
                "cli.messages.task_constraints_default",
                default=["Follow the step check_items"],
            )
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

        # 注入 project_rules 摘要（让只读 structured_instruction 的 Agent 也能看到规则）
        # fail-soft：拉取失败时降级为空列表
        instruction["project_rules"] = self._get_rule_summaries_for_step(step)

        return instruction

    def _inject_applicable_rules(self, step: Dict[str, Any]) -> List[Dict[str, Any]]:
        """为 task_next_step 返回值注入 applicable_rules

        基于 step 的 action / target_file / target_symbol 构造上下文，
        调用 get_applicable_rules 匹配 active 规则。

        fail-soft：任何异常都降级为空列表，绝不影响任务领取主流程。

        Args:
            step: task_next_step 内部构造的 step dict

        Returns:
            适用规则列表（每条含 id/title/rule_text/severity/matched_scope）
        """
        try:
            if not hasattr(self, "get_applicable_rules"):
                return []
            context = self._build_rule_context_for_step(step)
            rules = self.get_applicable_rules(context, limit=5)
            # 精简字段，避免返回过大
            return [
                {
                    "id": r.get("id", ""),
                    "title": r.get("title", ""),
                    "rule_text": r.get("rule_text", ""),
                    "severity": r.get("severity", "info"),
                    "matched_scope": r.get("matched_scope", []),
                }
                for r in rules
            ]
        except Exception:
            return []

    def _get_rule_summaries_for_step(self, step: Dict[str, Any]) -> List[Dict[str, Any]]:
        """为 build_structured_instruction 返回 project_rules 摘要

        与 _inject_applicable_rules 共享上下文构造，但只返回摘要字段
        （id/title/severity），减少 token 占用。

        Args:
            step: 步骤 dict

        Returns:
            规则摘要列表
        """
        try:
            if not hasattr(self, "get_applicable_rules"):
                return []
            context = self._build_rule_context_for_step(step)
            rules = self.get_applicable_rules(context, limit=5)
            return [
                {
                    "id": r.get("id", ""),
                    "title": r.get("title", ""),
                    "severity": r.get("severity", "info"),
                }
                for r in rules
            ]
        except Exception:
            return []

    def _build_rule_context_for_step(self, step: Dict[str, Any]) -> Dict[str, Any]:
        """根据 step 构造规则匹配上下文

        推断 language（从 target_file 扩展名）、symbol_kind（从 target_symbol
        限定名格式）、action（从 step.action），并填入 file_path 和 module_prefix。

        Args:
            step: 步骤 dict

        Returns:
            上下文 dict，可传入 get_applicable_rules
        """
        context: Dict[str, Any] = {}
        target_file = step.get("target_file") or ""
        target_symbol = step.get("target_symbol") or ""
        action = (step.get("action") or "").lower()

        if target_file:
            context["file_path"] = target_file
            # 推断语言（基于扩展名）
            ext_lang_map = {
                ".py": "python",
                ".rs": "rust",
                ".ts": "typescript",
                ".tsx": "typescript",
                ".js": "javascript",
                ".jsx": "javascript",
                ".go": "go",
                ".java": "java",
                ".kt": "kotlin",
                ".c": "c",
                ".cpp": "cpp",
                ".cc": "cpp",
                ".cs": "csharp",
                ".rb": "ruby",
                ".php": "php",
                ".swift": "swift",
                ".scala": "scala",
            }
            _, ext = os.path.splitext(target_file)
            lang = ext_lang_map.get(ext.lower())
            if lang:
                context["language"] = lang

        if target_symbol:
            # 简单推断 symbol_kind：如果以 :: 或 . 分隔且最后一段首字母大写，视为 class
            last_seg = target_symbol.rsplit("::", 1)[-1].rsplit(".", 1)[-1]
            if last_seg and last_seg[0].isupper():
                context["symbol_kind"] = "class"
            else:
                context["symbol_kind"] = "function"
            # module_prefix 取最后一段之前的部分（用 . 或 :: 分隔）
            if "::" in target_symbol:
                prefix = target_symbol.rsplit("::", 1)[0]
                if prefix:
                    context["module_prefix"] = prefix
            elif "." in target_symbol:
                prefix = target_symbol.rsplit(".", 1)[0]
                if prefix:
                    context["module_prefix"] = prefix

        if action:
            context["action"] = action

        # task_id 也写入 context，便于 evidence 匹配（虽然当前 _match_scope 不用它）
        if step.get("task_id"):
            context["task_id"] = step["task_id"]

        return context


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
        attribution_changes: List[Dict[str, Any]] = []
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
                attribution_changes.append({
                    "change_id": change_id,
                    "change": change,
                })

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

        # ---- 质量门禁（Task Quality Gate）----
        quality_gate: Dict[str, Any] = {"decision": "pass", "findings": [], "blocked": False}
        if success and not gate_failed and hasattr(self, "run_task_completion_review"):
            try:
                review = self.run_task_completion_review(actual_task_id, step_id)
                quality_gate = {
                    "decision": review["decision"],
                    "findings": review["findings"],
                    "counts": review["counts"],
                    "summary": review["summary"],
                    "blocked": review["decision"] == "block",
                }
                # decision=block：step 标记为 blocked（不进入 done），自动插入修复步骤
                if review["decision"] == "block":
                    gate_failed = True  # 复用 gate_failed 阻止后续 done 逻辑
                    # 把已标记为 done 的 step 改为 blocked
                    self.conn.execute(
                        "UPDATE task_steps SET status = ? WHERE id = ?",
                        (STEP_STATUS_BLOCKED, step_id),
                    )
                    blocking_findings = [
                        f for f in review["findings"]
                        if f.get("severity") in ("error", "block")
                    ]
                    new_step_id = self.insert_fix_quality_gate_step(
                        actual_task_id, step_id, blocking_findings
                    )
                    quality_gate["fix_step_id"] = new_step_id
                # decision=warn：记录 finding，但允许 step 完成（不阻塞）
                # decision=pass：正常完成
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
                # 边缘 bug 修复（T-1783428495806-71f6）：
                # 父任务有自身步骤（如 verify），子任务全部 closed/applied 后
                # _update_parent_status 已把父任务推到 REVIEW；之后父任务自身步骤
                # 完成时 t_status 已经是 REVIEW，不再满足 OPEN/IN_PROGRESS 条件，
                # 导致级联 close 调用被跳过，父任务卡在 review。
                # 修复：无论 t_status 是刚推到 REVIEW 还是已经是 REVIEW，
                # 都检查父任务（有子任务）是否所有子任务都已 applied/closed，
                # 若是则自动调用 _cascade_close_if_ready 级联 close。
                # 注意：叶子任务（无子任务）不触发，保持人工 apply/close 流程。
                cur = self.conn.execute(
                    "SELECT status FROM tasks WHERE id = ?",
                    (actual_task_id,),
                )
                current_status = cur.fetchone()["status"]
                if current_status == TASK_STATUS_REVIEW:
                    cur = self.conn.execute(
                        "SELECT COUNT(*) as cnt FROM tasks WHERE parent_id = ?",
                        (actual_task_id,),
                    )
                    has_subtasks = cur.fetchone()["cnt"] > 0
                    if has_subtasks:
                        cascaded = self._cascade_close_if_ready(
                            actual_task_id, "system", now
                        )
                        if cascaded:
                            self.conn.commit()
                # 子任务完成后，递归向上更新父任务状态
                self._update_parent_status(actual_task_id)

        self.conn.commit()

        # change_audit 写入后签名（commit 之后，避免破坏事务原子性）
        if attribution_changes and hasattr(self, "sign_audit_record"):
            for item in attribution_changes:
                change = item["change"]
                try:
                    self.sign_audit_record(
                        "change_audit",
                        item["change_id"],
                        {
                            "task_id": actual_task_id,
                            "step_id": step_id,
                            "file_path": change.get("file_path", ""),
                            "hash_before": change.get("hash_before", ""),
                            "hash_after": change.get("hash_after", ""),
                            "diff": change.get("diff", ""),
                            "author": change.get("author", "agent"),
                        },
                    )
                except Exception:
                    pass

        if attribution_changes and hasattr(self, "record_task_symbol_change"):
            for item in attribution_changes:
                change = item["change"]
                try:
                    self.record_task_symbol_change(
                        task_id=actual_task_id,
                        step_id=step_id,
                        change_audit_id=item["change_id"],
                        file_path=change.get("file_path", ""),
                        qualified_name=change.get("qualified_name", ""),
                        symbol_name=change.get("symbol_name", ""),
                        symbol_hash_before=change.get("symbol_hash_before", ""),
                        symbol_hash_after=change.get("symbol_hash_after", ""),
                        change_type=change.get("change_type", "modified"),
                        source="task_report_step",
                        metadata={
                            "file_hash_before": change.get("hash_before", ""),
                            "file_hash_after": change.get("hash_after", ""),
                            "diff": change.get("diff", ""),
                            "author": change.get("author", "agent"),
                        },
                    )
                except Exception:
                    pass

        # 返回任务树中的下一步步骤信息（深度优先）
        next_row = self._find_next_pending_step_tree(task_id)
        if not next_row:
            # 即使没有下一步，若质量门禁阻断也返回 quality_gate 信息
            if quality_gate.get("blocked"):
                return {"quality_gate": quality_gate}
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
            "quality_gate": quality_gate,
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
        rollback_signed: List[tuple] = []  # [(rollback_change_id, change), ...]

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

            rollback_signed.append((rollback_change_id, change))
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

        # 回滚 change_audit 写入后签名（commit 之后）
        if rollback_signed and hasattr(self, "sign_audit_record"):
            for rollback_change_id, change in rollback_signed:
                try:
                    self.sign_audit_record(
                        "change_audit",
                        rollback_change_id,
                        {
                            "task_id": task_id,
                            "step_id": change.get("step_id", ""),
                            "file_path": change["file_path"],
                            "hash_before": change.get("hash_after", ""),
                            "hash_after": change.get("hash_before", ""),
                            "diff": f"[ROLLBACK] reason={reason or 'unspecified'}",
                            "author": "agent",
                        },
                    )
                except Exception:
                    pass

        return {
            "rolled_back_changes": rolled_back,
            "task_status": TASK_STATUS_REVERTED,
            "note": t(
                "cli.messages.task_rollback_note",
                default=(
                    "Only rollback intent was recorded; no filesystem changes were made. "
                    "Callers should restore file content using hash_before from rolled_back_changes."
                ),
            ),
        }

    def task_apply(
        self,
        task_id: str,
        reviewer: str = "reviewer",
    ) -> Dict[str, Any]:
        """审核通过：将任务状态从 review 改为 applied

        设计原则：写代码的 Agent 不能自己 applied，必须由其他会话的 LLM 审核。
        只有 status=review 的任务才能 apply，其他状态拒绝。

        父任务禁止手动 apply：必须由子任务 apply 时自动级联触发。
        父任务的 apply 由 _cascade_close_if_ready 在最后一个子任务 apply 时原子完成
        （review → applied → closed 一次推进到位）。

        级联规则：apply 后查询所有兄弟子任务状态
        - 若全部 applied/closed → 原子级联 close：所有 applied 兄弟 + 自己 + 父任务
        - 否则保持 applied，等待其他兄弟任务被 apply

        Args:
            task_id: 任务 ID
            reviewer: 审核人标识

        Returns:
            包含 task_id、status、applied_at 的字典；失败时包含 error 字段。
            若触发级联，额外返回 cascaded_close 字段（自动 close 的 task_id 列表）。
        """
        now = time.time()
        cur = self.conn.execute(
            "SELECT status, parent_id FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            return {
                "error": t("cli.messages.task_not_found", default="Task not found"),
                "task_id": task_id,
            }

        current_status = row["status"]
        parent_id = row["parent_id"] or ""

        # 父任务禁止手动 apply：必须由级联触发
        # 检查是否有子任务（若自身有子任务则为父任务）
        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE parent_id = ?",
            (task_id,),
        )
        subtask_count = cur.fetchone()["cnt"]
        if subtask_count > 0:
            return {
                "error": t(
                    "cli.messages.task_apply_parent_manual_forbidden",
                    default="Parent task cannot be applied manually; it is auto-cascaded when all subtasks are applied",
                ),
                "task_id": task_id,
                "status": current_status,
                "reason": "parent_task_must_cascade",
                "subtask_count": subtask_count,
            }

        if current_status != TASK_STATUS_REVIEW:
            # 修复：无 steps 的叶子任务可以从 open/in_progress 自动推进到 review。
            # task_split 创建的子任务如果 plan 中没有列表项，就不会创建 steps，
            # 导致任务卡在 open 无法 apply（状态机要求 open→in_progress→review→applied→closed）。
            # 这里跳过中间状态直接推进到 review，让 apply/close 流程正常工作。
            if current_status in (TASK_STATUS_OPEN, TASK_STATUS_IN_PROGRESS):
                cur = self.conn.execute(
                    "SELECT COUNT(*) as cnt FROM task_steps WHERE task_id = ?",
                    (task_id,),
                )
                step_count = cur.fetchone()["cnt"]
                if step_count == 0:
                    # 无 steps，自动推进到 review
                    self.conn.execute(
                        "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                        (TASK_STATUS_REVIEW, now, task_id),
                    )
                    current_status = TASK_STATUS_REVIEW
                else:
                    return {
                        "error": t(
                            "cli.messages.task_apply_invalid_status",
                            default="Cannot apply task in status '{status}', only 'review' can be applied",
                            status=current_status,
                        ),
                        "task_id": task_id,
                        "status": current_status,
                    }
            else:
                return {
                    "error": t(
                        "cli.messages.task_apply_invalid_status",
                        default="Cannot apply task in status '{status}', only 'review' can be applied",
                        status=current_status,
                    ),
                    "task_id": task_id,
                    "status": current_status,
                }

        # 将当前任务 review → applied
        self.conn.execute(
            "UPDATE tasks SET status = ?, applied_at = ?, updated_at = ? WHERE id = ?",
            (TASK_STATUS_APPLIED, now, now, task_id),
        )

        # 级联检查前，先更新父任务状态（无 steps 的子任务跳过了 task_report_step，
        # 父任务可能还是 open，需要 _update_parent_status 推进到 review）
        if parent_id:
            self._update_parent_status(task_id)

        # 级联检查：若所有兄弟子任务都已 applied/closed，则原子 close 全部
        cascaded_close: List[str] = []
        if parent_id:
            cascaded_close = self._cascade_close_if_ready(parent_id, reviewer, now)

        self.conn.commit()

        result: Dict[str, Any] = {
            "task_id": task_id,
            "status": TASK_STATUS_APPLIED,
            "applied_at": now,
            "reviewer": reviewer,
        }
        if cascaded_close:
            result["cascaded_close"] = cascaded_close
        return result

    def _cascade_close_if_ready(
        self,
        parent_id: str,
        reviewer: str,
        now: float,
    ) -> List[str]:
        """级联 close 检查：若父任务的所有子任务都 applied/closed，则原子 close 全部

        触发条件：最后一个子任务被 apply 时调用。
        原子操作：
        1. close 所有 applied 状态的子任务（applied → closed）
        2. 父任务 review → applied → closed（一次性推进）
        3. 递归向上：若父任务也有父任务，且其兄弟都已 closed，继续级联

        Args:
            parent_id: 父任务 ID
            reviewer: 审核人标识（用于写入 closed_at 记录）
            now: 当前时间戳

        Returns:
            被自动 close 的 task_id 列表（含子任务和父任务）
        """
        cascaded: List[str] = []

        # 查询所有兄弟子任务状态
        cur = self.conn.execute(
            "SELECT id, status FROM tasks WHERE parent_id = ?",
            (parent_id,),
        )
        siblings = [dict(row) for row in cur]

        # 若有非 applied/closed 状态的兄弟，则不级联
        all_ready = all(
            s["status"] in (TASK_STATUS_APPLIED, TASK_STATUS_CLOSED)
            for s in siblings
        )
        if not all_ready:
            return cascaded

        # 查询父任务状态：必须为 review 才级联（避免重复 close）
        cur = self.conn.execute(
            "SELECT status, parent_id FROM tasks WHERE id = ?",
            (parent_id,),
        )
        parent_row = cur.fetchone()
        if not parent_row:
            return cascaded
        parent_status = parent_row["status"]
        grandparent_id = parent_row["parent_id"] or ""

        # 父任务不是 review 状态：不级联（可能已被 close 或还在 in_progress）
        if parent_status != TASK_STATUS_REVIEW:
            return cascaded

        # 1. close 所有 applied 状态的子任务
        for s in siblings:
            if s["status"] == TASK_STATUS_APPLIED:
                self.conn.execute(
                    "UPDATE tasks SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
                    (TASK_STATUS_CLOSED, now, now, s["id"]),
                )
                cascaded.append(s["id"])

        # 2. 父任务 review → applied → closed（一次性推进）
        self.conn.execute(
            "UPDATE tasks SET status = ?, applied_at = ?, updated_at = ? WHERE id = ?",
            (TASK_STATUS_APPLIED, now, now, parent_id),
        )
        self.conn.execute(
            "UPDATE tasks SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
            (TASK_STATUS_CLOSED, now, now, parent_id),
        )
        cascaded.append(parent_id)

        # 3. 递归向上：若父任务也有父任务，检查祖父的所有子任务（父任务的兄弟）是否都已 closed
        if grandparent_id:
            cascaded.extend(
                self._cascade_close_if_ready(grandparent_id, reviewer, now)
            )

        return cascaded

    def task_close(
        self,
        task_id: str,
        reviewer: str = "reviewer",
    ) -> Dict[str, Any]:
        """关闭任务：将任务状态从 applied 改为 closed

        设计原则：写代码的 Agent 不能自己 closed，必须由其他会话的 LLM 审核关闭。
        只有 status=applied 的任务才能 close，其他状态拒绝。

        父任务禁止手动 close：必须由子任务 apply 时自动级联触发
        （_cascade_close_if_ready 在最后一个子任务 apply 时原子完成）。
        若手动 close 父任务，返回 error 提示由级联触发。

        Args:
            task_id: 任务 ID
            reviewer: 审核人标识

        Returns:
            包含 task_id、status、closed_at 的字典；失败时包含 error 字段和 reason 字段
        """
        now = time.time()
        cur = self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            return {
                "error": t("cli.messages.task_not_found", default="Task not found"),
                "task_id": task_id,
            }

        current_status = row["status"]

        # 父任务禁止手动 close：必须由级联触发
        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM tasks WHERE parent_id = ?",
            (task_id,),
        )
        subtask_count = cur.fetchone()["cnt"]
        if subtask_count > 0:
            return {
                "error": t(
                    "cli.messages.task_close_parent_manual_forbidden",
                    default="Parent task cannot be closed manually; it is auto-cascaded when all subtasks are applied",
                ),
                "task_id": task_id,
                "status": current_status,
                "reason": "parent_task_must_cascade",
                "subtask_count": subtask_count,
            }

        if current_status != TASK_STATUS_APPLIED:
            # 修复：无 steps 的叶子任务可以从 open/in_progress/review 自动推进到 applied。
            # 与 task_apply 的修复对应，允许无 steps 的任务直接 close。
            if current_status in (TASK_STATUS_OPEN, TASK_STATUS_IN_PROGRESS, TASK_STATUS_REVIEW):
                cur = self.conn.execute(
                    "SELECT COUNT(*) as cnt FROM task_steps WHERE task_id = ?",
                    (task_id,),
                )
                step_count = cur.fetchone()["cnt"]
                if step_count == 0:
                    # 无 steps，自动推进到 applied
                    now_applied = now
                    self.conn.execute(
                        "UPDATE tasks SET status = ?, applied_at = ?, updated_at = ? WHERE id = ?",
                        (TASK_STATUS_APPLIED, now_applied, now_applied, task_id),
                    )
                    current_status = TASK_STATUS_APPLIED
                else:
                    return {
                        "error": t(
                            "cli.messages.task_close_invalid_status",
                            default="Cannot close task in status '{status}', only 'applied' can be closed",
                            status=current_status,
                        ),
                        "task_id": task_id,
                        "status": current_status,
                    }
            else:
                return {
                    "error": t(
                        "cli.messages.task_close_invalid_status",
                        default="Cannot close task in status '{status}', only 'applied' can be closed",
                        status=current_status,
                    ),
                    "task_id": task_id,
                    "status": current_status,
                }

        self.conn.execute(
            "UPDATE tasks SET status = ?, closed_at = ?, updated_at = ? WHERE id = ?",
            (TASK_STATUS_CLOSED, now, now, task_id),
        )
        self.conn.commit()

        # P1: 清除 active_task（防御性：仅当 active_task == task_id 时才清除，
        # 避免误清除后续已 claim 的新任务）
        self.clear_active_task(task_id)

        return {
            "task_id": task_id,
            "status": TASK_STATUS_CLOSED,
            "closed_at": now,
            "reviewer": reviewer,
        }

    def task_reopen(
        self,
        task_id: str,
        reviewer: str = "reviewer",
        reason: str = "",
    ) -> Dict[str, Any]:
        """显式 reopen 任务：将任务从 review/applied/closed 状态回到 in_progress

        使用场景：
        - Code review 发现已 applied/closed 的任务有问题，需要修复
        - 任务被误 close，需要重新打开
        - 有新子任务需要挂入已 closed 的父任务

        状态判断逻辑：
        - review/applied/closed → in_progress（清理 applied_at/closed_at）
        - open/in_progress：无需 reopen，返回提示
        - 父任务 reopen 时递归向上 reopen 祖父任务链

        Args:
            task_id: 任务 ID
            reviewer: 审核人标识
            reason: reopen 原因（可选）

        Returns:
            包含 task_id、status、previous_status、reopened_at 的字典；
            失败时包含 error 字段
        """
        now = time.time()
        cur = self.conn.execute(
            "SELECT status FROM tasks WHERE id = ?",
            (task_id,),
        )
        row = cur.fetchone()
        if not row:
            return {
                "error": t("cli.messages.task_not_found", default="Task not found"),
                "task_id": task_id,
            }

        current_status = row["status"]
        REOPEN_STATUSES = (TASK_STATUS_REVIEW, TASK_STATUS_APPLIED, TASK_STATUS_CLOSED)

        if current_status not in REOPEN_STATUSES:
            return {
                "error": t(
                    "cli.messages.task_reopen_no_need",
                    default="Task is in status '{status}', no need to reopen (only review/applied/closed can be reopened)",
                    status=current_status,
                ),
                "task_id": task_id,
                "status": current_status,
                "reason": "not_closed",
            }

        # Reopen 当前任务为 in_progress，清理时间戳
        self.conn.execute(
            "UPDATE tasks SET status = ?, applied_at = NULL, closed_at = NULL, "
            "updated_at = ? WHERE id = ?",
            (TASK_STATUS_IN_PROGRESS, now, task_id),
        )

        # 在 audit_chain 中记录 reopen 事件（fail-soft）
        try:
            if hasattr(self, "sign_audit_record"):
                self.sign_audit_record(
                    "tasks",
                    task_id,
                    {
                        "task_id": task_id,
                        "operation": "reopen",
                        "previous_status": current_status,
                        "new_status": TASK_STATUS_IN_PROGRESS,
                        "reason": reason or "manual reopen",
                        "reviewer": reviewer,
                        "timestamp": now,
                    },
                    operation="update",
                )
        except Exception:
            pass  # fail-soft

        # 递归向上 reopen 祖父任务链
        cur = self.conn.execute(
            "SELECT parent_id FROM tasks WHERE id = ?",
            (task_id,),
        )
        parent_row = cur.fetchone()
        if parent_row and parent_row["parent_id"]:
            grandparent_id = parent_row["parent_id"]
            cur = self.conn.execute(
                "SELECT status FROM tasks WHERE id = ?",
                (grandparent_id,),
            )
            grandparent_row = cur.fetchone()
            if grandparent_row:
                self._reopen_parent_chain_if_needed(
                    grandparent_id, grandparent_row["status"]
                )

        self.conn.commit()

        # P1: reopen 后设置为 active_task（用户显式 reopen 表示要重新开始干这个任务）
        self.set_active_task(task_id)

        return {
            "task_id": task_id,
            "status": TASK_STATUS_IN_PROGRESS,
            "previous_status": current_status,
            "reopened_at": now,
            "reviewer": reviewer,
            "reason": reason,
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
            {task_id, title, status, step_count, created_at,
             parent_id, depth, sort_order}
            排序规则：根任务在前 → 按 sort_order → 创建时间倒序
            便于 CLI 端按 depth 进行树形缩进展示
        """
        # parent_id 为空的根任务排在最前，便于树形遍历
        # 注意：SQLite 中 parent_id IS NULL 在 ORDER BY 中需要放在首位
        order_clause = (
            "ORDER BY CASE WHEN t.parent_id IS NULL OR t.parent_id = '' THEN 0 ELSE 1 END, "
            "t.parent_id ASC, t.sort_order ASC, t.created_at DESC"
        )
        if status_filter:
            cur = self.conn.execute(
                f"""
                SELECT t.id as task_id, t.title, t.status, t.created_at,
                       t.parent_id, t.depth, t.sort_order,
                       (SELECT COUNT(*) FROM task_steps ts WHERE ts.task_id = t.id) as step_count
                FROM tasks t
                WHERE t.status = ?
                {order_clause}
                LIMIT ?
                """,
                (status_filter, limit),
            )
        else:
            cur = self.conn.execute(
                f"""
                SELECT t.id as task_id, t.title, t.status, t.created_at,
                       t.parent_id, t.depth, t.sort_order,
                       (SELECT COUNT(*) FROM task_steps ts WHERE ts.task_id = t.id) as step_count
                FROM tasks t
                {order_clause}
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

    def task_create_from_plan(
        self,
        title: str,
        plan_md: str,
        description: str = "",
        creator: str = "agent",
    ) -> str:
        """从 Markdown 计划自动创建父子任务树

        支持的 Markdown 格式（鲁棒解析，兼容多种变体）：
        - 一级标题 (#) = 根任务描述
        - 二级标题 (##) = 子任务标题
        - 三级标题 (###) = 步骤分组
        - 列表项（兼容以下所有格式）：
          - 无序列表: - / * / + 开头
          - 有序列表: 1. / 2. / 3. 开头
          - checkbox: - [ ] / * [x] / + [ ] 等
          - 支持缩进（空格/tab 开头的列表项）
        - 代码块（``` 围栏）内的内容不解析
        - 标题前后空格和末尾 # 字符自动清理

        Args:
            title: 根任务标题
            plan_md: Markdown 格式的任务计划
            description: 根任务描述（可选，为 plan 中的一级标题内容）
            creator: 创建者

        Returns:
            根任务 ID
        """
        import re

        # 预编译正则
        # 标题正则：匹配 # 开头，末尾可选 # 字符
        re_h1 = re.compile(r'^#\s+(.+?)\s*#*\s*$')
        re_h2 = re.compile(r'^##\s+(.+?)\s*#*\s*$')
        re_h3 = re.compile(r'^###\s+(.+?)\s*#*\s*$')
        re_h4plus = re.compile(r'^####+\s+(.+?)\s*#*\s*$')

        # 列表项正则（兼容 - / * / + / 1. / 2. 等格式）
        # 组1: checkbox 标记 [ ] 或 [x] 或 [X]（可选）
        # 组2: 列表项内容
        re_list = re.compile(
            r'^[-*+]\s+'               # 无序列表标记 - * +
            r'(?:\[[ xX]\]\s+)?'       # 可选 checkbox [ ] [x] [X]
            r'(.+)$'                    # 内容
        )
        re_ordered = re.compile(
            r'^\d+\.\s+'                # 有序列表标记 1. 2. 3.
            r'(?:\[[ xX]\]\s+)?'        # 可选 checkbox
            r'(.+)$'                    # 内容
        )

        lines = plan_md.strip().split("\n")

        root_steps = []
        subtasks_def = []
        current_h2_title = None
        current_h2_desc_lines = []
        current_h2_steps = []
        in_h1_section = False
        h1_desc_lines = []
        in_code_block = False  # 代码块状态

        for line in lines:
            stripped = line.strip()

            # 代码块围栏检测（``` 或 ~~~）
            if stripped.startswith("```") or stripped.startswith("~~~"):
                in_code_block = not in_code_block
                continue

            # 在代码块内不解析任何内容
            if in_code_block:
                continue

            # 空行跳过
            if not stripped:
                continue

            # 一级标题 = 根任务描述（不匹配 ## ### 等）
            m = re_h1.match(stripped)
            if m and not stripped.startswith("## "):
                in_h1_section = True
                continue

            # 二级标题 = 子任务
            m = re_h2.match(stripped)
            if m and not stripped.startswith("### "):
                # 保存上一个子任务
                if current_h2_title is not None:
                    subtasks_def.append({
                        "title": current_h2_title,
                        "description": "\n".join(current_h2_desc_lines).strip(),
                        "steps": current_h2_steps,
                        "depth": 1,
                    })
                current_h2_title = m.group(1).strip()
                current_h2_desc_lines = []
                current_h2_steps = []
                continue

            # 三级标题 = 步骤分组
            m = re_h3.match(stripped)
            if m and not stripped.startswith("#### "):
                if current_h2_title is not None:
                    current_h2_steps.append({
                        "action": "group",
                        "target_file": "",
                        "target_symbol": "",
                        "check_items": [m.group(1).strip()],
                    })
                continue

            # 四级及以上标题 = 作为步骤分组（降级处理）
            m = re_h4plus.match(stripped)
            if m:
                if current_h2_title is not None:
                    current_h2_steps.append({
                        "action": "group",
                        "target_file": "",
                        "target_symbol": "",
                        "check_items": [m.group(1).strip()],
                    })
                continue

            # 无序列表项 (- / * / +)
            m = re_list.match(stripped)
            if m:
                item_text = m.group(1).strip()
                step = {
                    "action": "todo",
                    "target_file": "",
                    "target_symbol": "",
                    "check_items": [item_text] if item_text else [],
                }
                if current_h2_title is not None:
                    current_h2_steps.append(step)
                elif in_h1_section:
                    root_steps.append(step)
                continue

            # 有序列表项 (1. / 2. / 3.)
            m = re_ordered.match(stripped)
            if m:
                item_text = m.group(1).strip()
                step = {
                    "action": "todo",
                    "target_file": "",
                    "target_symbol": "",
                    "check_items": [item_text] if item_text else [],
                }
                if current_h2_title is not None:
                    current_h2_steps.append(step)
                elif in_h1_section:
                    root_steps.append(step)
                continue

            # 普通文本行 = 描述
            if current_h2_title is not None:
                current_h2_desc_lines.append(stripped)
            elif in_h1_section:
                h1_desc_lines.append(stripped)

        # 保存最后一个子任务
        if current_h2_title is not None:
            subtasks_def.append({
                "title": current_h2_title,
                "description": "\n".join(current_h2_desc_lines).strip(),
                "steps": current_h2_steps,
                "depth": 1,
            })

        # 合并描述
        full_desc = description
        if h1_desc_lines:
            h1_desc = "\n".join(h1_desc_lines).strip()
            if full_desc:
                full_desc = full_desc + "\n\n" + h1_desc
            else:
                full_desc = h1_desc

        # 如果根任务没有步骤，加一个汇总验证步骤
        if not root_steps:
            root_steps = [{
                "action": "verify",
                "target_file": "",
                "target_symbol": "",
                "check_items": t(
                    "cli.messages.task_plan_root_check_items",
                    default=["All subtasks are complete", "Final verification passed"],
                ),
            }]

        # 创建根任务
        root_id = self.task_create(
            title=title,
            description=full_desc,
            steps=root_steps,
            creator=creator,
        )

        # 创建子任务
        for st_def in subtasks_def:
            if not st_def["steps"]:
                st_def["steps"] = [{
                    "action": "todo",
                    "target_file": "",
                    "target_symbol": "",
                    "check_items": [
                        t(
                            "cli.messages.task_plan_subtask_check_item",
                            default="Complete {title}",
                            title=st_def["title"],
                        )
                    ],
                }]
            self.task_create_subtask(
                parent_task_id=root_id,
                title=st_def["title"],
                description=st_def["description"],
                steps=st_def["steps"],
                creator=creator,
            )

        return root_id

    def task_plan_template(self) -> str:
        """获取 task_create_from_plan 的标准格式模板

        Agent 调用 task_create_from_plan 前可以先获取此模板，
        按模板格式填写后传入，确保解析正确。

        Returns:
            Markdown 格式的模板字符串
        """
        return t("cli.messages.task_plan_template", default="""# {Root task title}
{Root task description (plain text)}

## {Subtask 1 title}
{Subtask 1 description (optional)}

- {Step 1 description}
- {Step 2 description}
- [ ] {Incomplete step (checkbox format)}
- [x] {Completed step}

### {Step group title (optional)}
- {Grouped step}

## {Subtask 2 title}
1. {Ordered list step}
2. {Ordered list step}

## {Subtask 3 title (default step will be added when no steps exist)}

Format notes:
- # heading -> root task description
- ## heading -> subtask (created automatically)
- ### heading -> step group
- - / * / + -> unordered list item (recognized as step)
- 1. / 2. / 3. -> ordered list item (recognized as step)
- [ ] / [x] / [X] -> checkbox variants (marker is removed)
- ``` or ~~~ -> code block (content is not parsed)
- trailing # in headings is cleaned automatically (for example, ## Title ## -> Title)
""")

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
