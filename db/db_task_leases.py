"""
db_task_leases.py
=================

P4 Assignment 与安全 Lease Mixin。

满足 Requirements 11.1–11.13, 13.4–13.10, 14.6, 14.11–14.12, 14.30–14.32：
- Assignment：task+role+holder Identity 绑定（Req 11.1），不把 workspace
  `active_task_id` 当作 assignment authority（Req 13.4）
- Lease：lease_id/token hash/权威时钟时间/单调 fencing counter，**永不存 raw token**
  （Req 11.2）；acquire 原子比较并递增 counter（Req 11.3）；renew 要求当前
  token/holder/counter 且未过期（Req 11.4），重复 renew 幂等不递增 counter（Req 11.5）；
  release 追加审计事件且幂等（Req 11.6-11.7）
- protected mutation 验证 token hash、expiry、role、Identity 与当前 fencing（Req 11.8-11.9）
- 时间字段一律读取 daemon Authoritative_Clock（Req 11.2/11.4/11.9 → 14.11），
  客户端时间戳只作参考元数据（Req 14.12）；此处以 time.time() 近似，daemon 时钟接入点见 _clock()

**Lease 边界（正面陈述，Req 14.32/11.13）**：Lease 保证的是 daemon 在线期间的并发
正确性——同一 task/role 任一时刻只有一个有效持有者，旧持有者在新 lease 生效后无法再写入
（fencing）。防篡改保证不属于 Lease，归属于 Attestation 校验与追加式 Evidence_Ledger；
本模块不把 Lease 描述为能防止离线直接改库。

表对齐 schema v46：
- task_assignments：task+role+holder 绑定（append 语义，status 标记生命周期）
- task_leases：当前与历史 Lease（partial UNIQUE 索引保证单 active）
- task_lease_events：append-only 审计事件（acquire/renew/release）
"""

from __future__ import annotations

import hashlib
import secrets
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple


# ============================================
# 错误码与 Structured_Reason 构造
# ============================================

# 稳定错误码（Req 1.12：跨文案变化保持稳定）
ERR_ASSIGNMENT_INCOMPLETE = "E_ASSIGNMENT_INCOMPLETE"
ERR_ASSIGNMENT_NOT_FOUND = "E_ASSIGNMENT_NOT_FOUND"

ERR_LEASE_ACTIVE_EXISTS = "E_LEASE_ACTIVE_EXISTS"
ERR_LEASE_NOT_FOUND = "E_LEASE_NOT_FOUND"
ERR_LEASE_TOKEN_MISMATCH = "E_LEASE_TOKEN_MISMATCH"
ERR_LEASE_EXPIRED = "E_LEASE_EXPIRED"
ERR_LEASE_FENCING_STALE = "E_LEASE_FENCING_STALE"
ERR_LEASE_HOLDER_MISMATCH = "E_LEASE_HOLDER_MISMATCH"
ERR_LEASE_ALREADY_RELEASED = "E_LEASE_ALREADY_RELEASED"
ERR_LEASE_INVALID = "E_LEASE_INVALID"


def _reason(code: str, message_key: str, detail: str = "", **extra: Any) -> Dict[str, Any]:
    """构造 Structured_Reason（Req 1.12）"""
    reason: Dict[str, Any] = {
        "code": code,
        "message_key": message_key,
        "detail": detail,
    }
    if extra:
        reason.update(extra)
    return reason


def _ok(**extra: Any) -> Dict[str, Any]:
    """构造成功结果"""
    result: Dict[str, Any] = {"code": "OK"}
    if extra:
        result.update(extra)
    return result


def _hash_token(raw_token: str) -> str:
    """计算 raw token 的 sha256 hash（Req 11.2：永不存 raw token）"""
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


# ============================================
# Mixin
# ============================================


class LeaseMixin:
    """P4 Assignment and Lease Mixin

    提供 assignment 绑定、lease 生命周期（acquire/renew/release/status）与
    fencing 验证（Req 11.1-11.13）。

    Identity 与 Lease 均只作受限授权，不等于 SQLite 事务所有权：
    - assignment/lease 不得绕过角色权限、Independent Review 或 Evidence Gate（Req 11.11）
    - SQLite 写锁只负责短事务互斥，不提供业务 ownership（Req 11.10, 13.5）
    """

    # ------------------------------------------------------------------
    # 权威时钟（Req 14.11）
    # ------------------------------------------------------------------

    def _clock(self) -> float:
        """读取 Authoritative_Clock（Req 14.11）

        当前以 daemon 进程时钟（time.time()）近似；daemon 串行化点接入后，
        此方法改为读取 daemon 权威时钟，客户端时间戳只作参考元数据（Req 14.12），
        不参与 Lease 过期判定与 protected mutation 校验。
        """
        return time.time()

    # ------------------------------------------------------------------
    # 1. Assignment（Req 11.1, 13.4）
    # ------------------------------------------------------------------

    def create_assignment(
        self,
        task_id: str,
        role: str,
        identity: Dict[str, Any],
        workspace_id: Optional[int] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """创建 task+role+holder Identity 绑定（Req 11.1）

        assignment 只做授权绑定，不把 workspace `active_task_id` 当作
        assignment authority（Req 13.4）。assignment 可以没有 lease（Req 11.12）。

        Args:
            task_id: 关联任务 ID
            role: 角色（implementer/reviewer/tester/planner）
            identity: {agent_id, session_id, model_id, role}
            workspace_id: 工作区 ID，None 时取活动工作区

        Returns:
            (success, result_dict)
        """
        agent_id = identity.get("agent_id", "")
        session_id = identity.get("session_id", "")
        model_id = identity.get("model_id", "")
        if not all([agent_id, session_id, model_id]):
            return False, _reason(
                ERR_ASSIGNMENT_INCOMPLETE,
                "error.assignment_incomplete",
                detail=f"缺失 Identity 字段: agent_id={bool(agent_id)}, "
                       f"session_id={bool(session_id)}, model_id={bool(model_id)}",
            )

        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()

        now = self._clock()
        assignment_id = f"ASG-{uuid.uuid4().hex[:16]}"
        try:
            self.conn.execute(
                """
                INSERT INTO task_assignments
                    (workspace_id, assignment_id, task_id, role, agent_id,
                     session_id, model_id, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)
                """,
                (workspace_id, assignment_id, task_id, role, agent_id,
                 session_id, model_id, now),
            )
            self.conn.commit()
        except Exception as e:
            return False, _reason(
                ERR_LEASE_INVALID,
                "error.lease_invalid",
                detail=f"assignment 创建失败: {e}",
            )

        return True, _ok(
            assignment_id=assignment_id,
            task_id=task_id,
            role=role,
            agent_id=agent_id,
            session_id=session_id,
            model_id=model_id,
            created_at=now,
        )

    def get_assignment(
        self,
        task_id: str,
        role: str = "",
        workspace_id: Optional[int] = None,
    ) -> Optional[Dict[str, Any]]:
        """查询任务当前 active assignment（只读）

        Args:
            task_id: 任务 ID
            role: 角色，空时返回该任务最近一条 active assignment
            workspace_id: 工作区 ID

        Returns:
            assignment 字典（不含敏感字段）或 None
        """
        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()
        sql = (
            "SELECT * FROM task_assignments "
            "WHERE workspace_id = ? AND task_id = ? AND status = 'active'"
        )
        params: list = [workspace_id, task_id]
        if role:
            sql += " AND role = ?"
            params.append(role)
        sql += " ORDER BY id DESC LIMIT 1"
        cur = self.conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def revoke_assignment(
        self,
        assignment_id: str,
        workspace_id: Optional[int] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """撤销 assignment（追加 revoked_at，不删除记录）"""
        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()
        now = self._clock()
        cur = self.conn.execute(
            "UPDATE task_assignments SET status = 'revoked', revoked_at = ? "
            "WHERE workspace_id = ? AND assignment_id = ? AND status = 'active'",
            (now, workspace_id, assignment_id),
        )
        self.conn.commit()
        if cur.rowcount == 0:
            return False, _reason(
                ERR_ASSIGNMENT_NOT_FOUND,
                "error.assignment_not_found",
                detail=f"assignment_id={assignment_id} 不存在或已撤销",
                assignment_id=assignment_id,
            )
        return True, _ok(assignment_id=assignment_id, revoked_at=now)

    # ------------------------------------------------------------------
    # 2. Lease 生命周期（Req 11.2-11.7）
    # ------------------------------------------------------------------

    def acquire_lease(
        self,
        task_id: str,
        role: str,
        identity: Dict[str, Any],
        ttl_seconds: float = 3600.0,
        workspace_id: Optional[int] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """获取 Lease（Req 11.2-11.3）

        原子比较当前 Lease 状态（BEGIN IMMEDIATE）：
        - 存在未过期 active lease → 拒绝（ERR_LEASE_ACTIVE_EXISTS）
        - 存在已过期 active lease → 置为 expired 后创建新 lease
        - 无 active lease → 创建新 lease
        fencing counter 取该 task+role 全部历史最大 counter + 1（单调递增，Req 11.3）。

        raw token 仅在本次成功响应返回一次，数据库只存 sha256 hash（Req 11.2）。

        Args:
            task_id: 任务 ID
            role: 角色
            identity: {agent_id, session_id, model_id}
            ttl_seconds: 有效期（秒），expires_at = Authoritative_Clock + ttl
            workspace_id: 工作区 ID

        Returns:
            (success, result_dict)；成功时 result_dict 含 **raw token**（仅此一次）
        """
        agent_id = identity.get("agent_id", "")
        session_id = identity.get("session_id", "")
        model_id = identity.get("model_id", "")
        if not all([agent_id, session_id, model_id]):
            return False, _reason(
                ERR_ASSIGNMENT_INCOMPLETE,
                "error.assignment_incomplete",
                detail=f"缺失 Identity 字段: agent_id={bool(agent_id)}, "
                       f"session_id={bool(session_id)}, model_id={bool(model_id)}",
            )

        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()

        now = self._clock()
        token = secrets.token_urlsafe(32)
        token_hash = _hash_token(token)
        lease_id = f"L-{uuid.uuid4().hex[:16]}"
        expires_at = now + ttl_seconds

        try:
            self.conn.execute("BEGIN IMMEDIATE")
            # 1. 原子比较当前 active lease（Req 11.2）
            cur = self.conn.execute(
                "SELECT * FROM task_leases "
                "WHERE workspace_id = ? AND task_id = ? AND role = ? AND status = 'active'",
                (workspace_id, task_id, role),
            )
            active = cur.fetchone()
            if active is not None:
                if now <= active["expires_at"]:
                    self.conn.execute("ROLLBACK")
                    return False, _reason(
                        ERR_LEASE_ACTIVE_EXISTS,
                        "error.lease_active_exists",
                        detail=f"task={task_id} role={role} 已有未过期 lease "
                               f"({active['lease_id']}, expires_at={active['expires_at']:.1f})",
                        lease_id=active["lease_id"],
                        expires_at=active["expires_at"],
                        fencing_counter=active["fencing_counter"],
                    )
                # 已过期 → 旧 lease 置 expired（释放唯一 active 槽位）
                self.conn.execute(
                    "UPDATE task_leases SET status = 'expired' WHERE id = ?",
                    (active["id"],),
                )

            # 2. 单调递增 counter（Req 11.3）
            row = self.conn.execute(
                "SELECT COALESCE(MAX(fencing_counter), 0) AS m FROM task_leases "
                "WHERE workspace_id = ? AND task_id = ? AND role = ?",
                (workspace_id, task_id, role),
            ).fetchone()
            fencing_counter = int(row["m"]) + 1

            # 3. 插入新 lease
            self.conn.execute(
                """
                INSERT INTO task_leases
                    (workspace_id, lease_id, task_id, role, agent_id,
                     session_id, model_id, token_hash, fencing_counter,
                     acquired_at, expires_at, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'active')
                """,
                (workspace_id, lease_id, task_id, role, agent_id,
                 session_id, model_id, token_hash, fencing_counter,
                 now, expires_at),
            )

            # 4. 追加审计事件（append-only，不写 raw token）
            self._append_lease_event(
                lease_id, task_id, role, "acquire", fencing_counter,
                now, agent_id, session_id, model_id,
                detail=f"acquired, expires_at={expires_at:.1f}",
            )

            self.conn.commit()
        except Exception as e:
            try:
                self.conn.execute("ROLLBACK")
            except Exception:
                pass
            return False, _reason(
                ERR_LEASE_INVALID,
                "error.lease_invalid",
                detail=f"acquire 失败: {e}",
            )

        return True, _ok(
            lease_id=lease_id,
            task_id=task_id,
            role=role,
            token=token,  # raw token 仅此一次返回（Req 11.2）
            fencing_counter=fencing_counter,
            acquired_at=now,
            expires_at=expires_at,
        )

    def renew_lease(
        self,
        task_id: str,
        role: str,
        token: str,
        identity: Optional[Dict[str, Any]] = None,
        ttl_seconds: float = 3600.0,
        workspace_id: Optional[int] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """续租 Lease（Req 11.4-11.5）

        要求当前 token hash、当前 holder Identity、当前 fencing counter 且未过期；
        校验通过后从 Authoritative_Clock 设置更晚的 expires_at 并更新 renewed_at。

        幂等（Req 11.5）：重复有效的 renew 返回同一 lease 状态（同一 lease_id 与
        fencing counter），不递增 counter、不创建新 lease。

        Args:
            task_id: 任务 ID
            role: 角色
            token: raw token（调用方持有）
            identity: holder Identity（可选；提供时校验与 lease holder 一致，Req 11.4）
            ttl_seconds: 续租后有效期（秒）
            workspace_id: 工作区 ID

        Returns:
            (success, result_dict)
        """
        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()

        now = self._clock()
        cur = self.conn.execute(
            "SELECT * FROM task_leases "
            "WHERE workspace_id = ? AND task_id = ? AND role = ? AND status = 'active'",
            (workspace_id, task_id, role),
        )
        active = cur.fetchone()
        if active is None:
            return False, _reason(
                ERR_LEASE_NOT_FOUND,
                "error.lease_not_found",
                detail=f"task={task_id} role={role} 无 active lease",
                task_id=task_id,
                role=role,
            )

        # token 校验（Req 11.9）
        if _hash_token(token) != active["token_hash"]:
            return False, _reason(
                ERR_LEASE_TOKEN_MISMATCH,
                "error.lease_token_mismatch",
                detail=f"token hash 不匹配 (lease_id={active['lease_id']})",
                lease_id=active["lease_id"],
            )

        # holder Identity 校验（Req 11.4）
        if identity:
            if (identity.get("agent_id") != active["agent_id"]
                    or identity.get("session_id") != active["session_id"]
                    or identity.get("model_id") != active["model_id"]):
                return False, _reason(
                    ERR_LEASE_HOLDER_MISMATCH,
                    "error.lease_holder_mismatch",
                    detail=f"holder Identity 与 lease ({active['lease_id']}) 不一致",
                    lease_id=active["lease_id"],
                )

        # 过期判定（Authoritative_Clock，Req 11.9）
        if now > active["expires_at"]:
            return False, _reason(
                ERR_LEASE_EXPIRED,
                "error.lease_expired",
                detail=f"lease {active['lease_id']} 已过期 (expires_at={active['expires_at']:.1f})",
                lease_id=active["lease_id"],
                expires_at=active["expires_at"],
            )

        # 幂等续租：不递增 counter，不创建新 lease（Req 11.5）
        new_expires = now + ttl_seconds
        try:
            self.conn.execute(
                "UPDATE task_leases SET renewed_at = ?, expires_at = ? WHERE id = ?",
                (now, new_expires, active["id"]),
            )
            self._append_lease_event(
                active["lease_id"], task_id, role, "renew", active["fencing_counter"],
                now, active["agent_id"], active["session_id"], active["model_id"],
                detail=f"renewed, expires_at={new_expires:.1f}",
            )
            self.conn.commit()
        except Exception as e:
            return False, _reason(
                ERR_LEASE_INVALID,
                "error.lease_invalid",
                detail=f"renew 失败: {e}",
            )

        return True, _ok(
            lease_id=active["lease_id"],
            task_id=task_id,
            role=role,
            fencing_counter=active["fencing_counter"],
            renewed_at=now,
            expires_at=new_expires,
        )

    def release_lease(
        self,
        task_id: str,
        role: str,
        token: str,
        identity: Optional[Dict[str, Any]] = None,
        workspace_id: Optional[int] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """释放 Lease（Req 11.6-11.7）

        当前 token 匹配时原子追加 release 审计事件并将 lease 置 released。

        幂等（Req 11.7）：重复的 release 返回同一 released 状态，不改变
        fencing counter，不创建第二个 active lease。

        Args:
            task_id: 任务 ID
            role: 角色
            token: raw token
            identity: 发起者 Identity（可选）
            workspace_id: 工作区 ID

        Returns:
            (success, result_dict)
        """
        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()

        now = self._clock()
        cur = self.conn.execute(
            "SELECT * FROM task_leases "
            "WHERE workspace_id = ? AND task_id = ? AND role = ? AND status = 'active'",
            (workspace_id, task_id, role),
        )
        active = cur.fetchone()
        if active is None:
            # 幂等分支：无 active lease 时，若最近一条历史 lease token 匹配，
            # 视为已释放状态（Req 11.7），否则返回未找到
            hist = self.conn.execute(
                "SELECT * FROM task_leases "
                "WHERE workspace_id = ? AND task_id = ? AND role = ? "
                "ORDER BY id DESC LIMIT 1",
                (workspace_id, task_id, role),
            ).fetchone()
            if hist is not None and _hash_token(token) == hist["token_hash"] \
                    and hist["status"] == "released":
                return True, _ok(
                    lease_id=hist["lease_id"],
                    task_id=task_id,
                    role=role,
                    fencing_counter=hist["fencing_counter"],
                    released_at=hist["released_at"],
                    status="released",
                    idempotent=True,
                )
            return False, _reason(
                ERR_LEASE_NOT_FOUND,
                "error.lease_not_found",
                detail=f"task={task_id} role={role} 无 active lease",
                task_id=task_id,
                role=role,
            )

        if _hash_token(token) != active["token_hash"]:
            return False, _reason(
                ERR_LEASE_TOKEN_MISMATCH,
                "error.lease_token_mismatch",
                detail=f"token hash 不匹配 (lease_id={active['lease_id']})",
                lease_id=active["lease_id"],
            )

        try:
            self.conn.execute(
                "UPDATE task_leases SET status = 'released', released_at = ? WHERE id = ?",
                (now, active["id"]),
            )
            self._append_lease_event(
                active["lease_id"], task_id, role, "release", active["fencing_counter"],
                now, active["agent_id"], active["session_id"], active["model_id"],
                detail=f"released at {now:.1f}",
            )
            self.conn.commit()
        except Exception as e:
            return False, _reason(
                ERR_LEASE_INVALID,
                "error.lease_invalid",
                detail=f"release 失败: {e}",
            )

        return True, _ok(
            lease_id=active["lease_id"],
            task_id=task_id,
            role=role,
            fencing_counter=active["fencing_counter"],
            released_at=now,
            status="released",
        )

    def get_lease_status(
        self,
        task_id: str,
        role: str = "",
        workspace_id: Optional[int] = None,
    ) -> Dict[str, Any]:
        """查询 Lease 状态（只读，Req 11.2）

        返回当前 active lease（含 token_hash 用于校验，不含 raw token）。
        无 active lease 时返回最近一条历史 lease 的状态摘要。
        """
        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()

        sql = (
            "SELECT * FROM task_leases "
            "WHERE workspace_id = ? AND task_id = ?"
        )
        params: list = [workspace_id, task_id]
        if role:
            sql += " AND role = ?"
            params.append(role)
        sql += " ORDER BY (status = 'active') DESC, id DESC LIMIT 1"
        cur = self.conn.execute(sql, params)
        row = cur.fetchone()
        if row is None:
            return {"status": "none", "task_id": task_id, "role": role}
        d = dict(row)
        # token_hash 保留供受保护校验使用；raw token 永不出现
        return {
            "status": d["status"],
            "lease_id": d["lease_id"],
            "task_id": d["task_id"],
            "role": d["role"],
            "agent_id": d["agent_id"],
            "session_id": d["session_id"],
            "model_id": d["model_id"],
            "token_hash": d["token_hash"],
            "fencing_counter": d["fencing_counter"],
            "acquired_at": d["acquired_at"],
            "expires_at": d["expires_at"],
            "renewed_at": d["renewed_at"],
            "released_at": d["released_at"],
        }

    # ------------------------------------------------------------------
    # 3. protected mutation 校验（Req 11.8-11.9, 11.11）
    # ------------------------------------------------------------------

    def validate_lease_for_mutation(
        self,
        task_id: str,
        role: str,
        token: str,
        fencing_counter: int,
        identity: Optional[Dict[str, Any]] = None,
        workspace_id: Optional[int] = None,
    ) -> Tuple[bool, Dict[str, Any]]:
        """校验 protected mutation 的 Lease 凭证（Req 11.8-11.9）

        校验项（任一失败即拒绝，且**不改变 task data**）：
        1. 存在 active lease（ERR_LEASE_NOT_FOUND）
        2. token hash 匹配（ERR_LEASE_TOKEN_MISMATCH）
        3. 未过期（Authoritative_Clock，ERR_LEASE_EXPIRED）
        4. fencing counter 等于当前 counter（ERR_LEASE_FENCING_STALE）
        5. holder Identity 匹配（可选，ERR_LEASE_HOLDER_MISMATCH）

        注意：Lease 校验通过不代表 mutation 被授权——仍须经过角色权限、
        Independent Review 与 Evidence Gate（Req 11.11）。

        Args:
            task_id: 任务 ID
            role: 角色
            token: raw token
            fencing_counter: 调用方持有的 counter
            identity: holder Identity（可选）
            workspace_id: 工作区 ID

        Returns:
            (is_valid, result_dict)；成功时 result_dict 含 lease_id/fencing_counter/expires_at
        """
        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()

        now = self._clock()
        cur = self.conn.execute(
            "SELECT * FROM task_leases "
            "WHERE workspace_id = ? AND task_id = ? AND role = ? AND status = 'active'",
            (workspace_id, task_id, role),
        )
        active = cur.fetchone()
        if active is None:
            return False, _reason(
                ERR_LEASE_NOT_FOUND,
                "error.lease_not_found",
                detail=f"task={task_id} role={role} 无 active lease，"
                       f"受保护写操作需要先 acquire_lease",
                task_id=task_id,
                role=role,
            )

        if _hash_token(token) != active["token_hash"]:
            return False, _reason(
                ERR_LEASE_TOKEN_MISMATCH,
                "error.lease_token_mismatch",
                detail=f"token hash 不匹配 (lease_id={active['lease_id']})",
                lease_id=active["lease_id"],
            )

        if now > active["expires_at"]:
            return False, _reason(
                ERR_LEASE_EXPIRED,
                "error.lease_expired",
                detail=f"lease {active['lease_id']} 已过期 "
                       f"(expires_at={active['expires_at']:.1f}, now={now:.1f})",
                lease_id=active["lease_id"],
                expires_at=active["expires_at"],
            )

        if fencing_counter != active["fencing_counter"]:
            return False, _reason(
                ERR_LEASE_FENCING_STALE,
                "error.lease_fencing_stale",
                detail=f"fencing counter {fencing_counter} != 当前 {active['fencing_counter']}；"
                       f"旧持有者在新 lease 生效后写入被拒绝（Property 11）",
                expected=active["fencing_counter"],
                actual=fencing_counter,
                lease_id=active["lease_id"],
            )

        if identity:
            if (identity.get("agent_id") != active["agent_id"]
                    or identity.get("session_id") != active["session_id"]
                    or identity.get("model_id") != active["model_id"]):
                return False, _reason(
                    ERR_LEASE_HOLDER_MISMATCH,
                    "error.lease_holder_mismatch",
                    detail=f"holder Identity 与 lease ({active['lease_id']}) 不一致",
                    lease_id=active["lease_id"],
                )

        return True, _ok(
            lease_id=active["lease_id"],
            task_id=task_id,
            role=role,
            fencing_counter=active["fencing_counter"],
            expires_at=active["expires_at"],
        )

    # ------------------------------------------------------------------
    # 4. 审计事件（append-only，Req 11.6, 11.12）
    # ------------------------------------------------------------------

    def _append_lease_event(
        self,
        lease_id: str,
        task_id: str,
        role: str,
        event_type: str,
        fencing_counter: int,
        event_at: float,
        actor_agent_id: str,
        actor_session_id: str,
        actor_model_id: str,
        detail: str = "",
    ) -> str:
        """追加一条 Lease 审计事件（调用方负责 commit；不写 raw token）"""
        event_id = f"EVT-{uuid.uuid4().hex[:16]}"
        self.conn.execute(
            """
            INSERT INTO task_lease_events
                (workspace_id, event_id, lease_id, task_id, role, event_type,
                 fencing_counter, event_at, actor_agent_id, actor_session_id,
                 actor_model_id, detail)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (self._get_active_workspace_id(), event_id, lease_id, task_id, role,
             event_type, fencing_counter, event_at, actor_agent_id,
             actor_session_id, actor_model_id, detail),
        )
        return event_id

    def list_lease_events(
        self,
        task_id: str = "",
        role: str = "",
        workspace_id: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """查询 Lease 审计事件（只读，append-only 账本）"""
        if workspace_id is None:
            workspace_id = self._get_active_workspace_id()
        sql = "SELECT * FROM task_lease_events WHERE workspace_id = ?"
        params: list = [workspace_id]
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        if role:
            sql += " AND role = ?"
            params.append(role)
        sql += " ORDER BY id ASC"
        cur = self.conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]
