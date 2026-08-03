"""多 LLM 契约协同（P1，原 [L13]）+ _collab_* 辅助

拆分自 server/mcp_server.py（4859-5137 行区间），由 register(mcp) 注册。
"""

# [L13] 多 LLM 契约协同——只读 MCP 查询工具面（Req 14.17, D0 任务 3.15）

import json
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import _get_daemon_client, get_db
from ...i18n import t


def _collab_direct_read(db: Any, method: str, params: Dict[str, Any]) -> dict:
    """当 Daemon 未匹配或降级时，直接查询 CodeGraphDB 真实表数据。

    S5 接线：P1 证据/verdict/gate 真相源在 SQLite（task_contract_revisions /
    task_verdict_events / task_evidence_events / task_gate_decisions），
    库层函数已就绪（39+ 测试），此处把 MCP 只读工具接上真实数据。

    Args:
        db: CodeGraphDB 实例
        method: RPC 方法名（role_view.get / evidence.query / freshness.status / gate.decision.query）
        params: RPC 参数
    """
    if method == "role_view.get":
        task_id = params.get("task_id", "")
        role = params.get("role", "") or "implementer"
        # 从最新契约 Envelope 生成 Role_View（view_type=role, stage=blind）
        envelope_data: Dict[str, Any] = {}
        try:
            cur = db.conn.execute(
                "SELECT envelope_payload FROM task_contract_revisions "
                "WHERE task_id = ? ORDER BY revision DESC LIMIT 1",
                (task_id,),
            )
            row = cur.fetchone()
            if row and row["envelope_payload"]:
                envelope_data = json.loads(row["envelope_payload"])
        except Exception:
            pass
        if hasattr(db, "get_role_view"):
            try:
                return db.get_role_view(
                    task_id, role, view_version="1.0", stage="blind",
                    envelope_data=envelope_data,
                )
            except Exception:
                pass
        return {"task_id": task_id, "role": role, "view": None}

    if method == "evidence.query":
        task_id = params.get("task_id", "")
        contract_id = params.get("contract_id", "")
        verifier = params.get("verifier", "")
        limit = params.get("limit", 50)
        items: List[Dict[str, Any]] = []
        try:
            if task_id and hasattr(db, "list_evidence_for_task"):
                items = db.list_evidence_for_task(task_id, contract_id)
            elif contract_id and hasattr(db, "list_evidence_for_contract"):
                items = db.list_evidence_for_contract(contract_id)
        except Exception:
            items = []
        if verifier:
            items = [i for i in items if i.get("verifier_name") == verifier]
        return {"items": items[: int(limit)], "count": len(items)}

    if method == "freshness.status":
        evidence_id = params.get("evidence_id", "")
        task_id = params.get("task_id", "")
        items: List[Dict[str, str]] = []
        try:
            # derive_freshness 需要当前契约 revision（无契约时取 0）
            rev = 0
            try:
                cur = db.conn.execute(
                    "SELECT MAX(revision) as m FROM task_contract_revisions "
                    "WHERE task_id = ?",
                    (task_id,),
                )
                row = cur.fetchone()
                rev = row["m"] or 0
            except Exception:
                pass
            ids: List[str] = []
            if evidence_id:
                ids = [evidence_id]
            elif task_id and hasattr(db, "list_evidence_for_task"):
                ids = [
                    e.get("evidence_id", "")
                    for e in db.list_evidence_for_task(task_id)
                ]
            for eid in ids:
                if not eid:
                    continue
                status = "unknown"
                try:
                    if hasattr(db, "derive_freshness"):
                        status, _reason = db.derive_freshness(
                            eid, current_contract_revision=rev
                        )
                    else:
                        ev = db.get_evidence(eid)
                        status = (ev or {}).get("freshness_status", "unknown")
                except Exception:
                    status = "unknown"
                items.append({"evidence_id": eid, "status": status})
        except Exception:
            pass
        return {"items": items}

    if method == "gate.decision.query":
        task_id = params.get("task_id", "")
        gate_id = params.get("gate_id", "")
        limit = params.get("limit", 20)
        sql = "SELECT * FROM task_gate_decisions WHERE 1=1"
        conds: List[str] = []
        vals: List[Any] = []
        if task_id:
            conds.append("task_id = ?")
            vals.append(task_id)
        if gate_id:
            conds.append("decision_id = ?")
            vals.append(gate_id)
        if conds:
            sql += " AND " + " AND ".join(conds)
        sql += " ORDER BY decision_time DESC LIMIT ?"
        vals.append(int(limit))
        try:
            cur = db.conn.execute(sql, vals)
            items = [dict(r) for r in cur.fetchall()]
            return {"items": items, "count": len(items)}
        except Exception:
            return {"items": [], "count": 0}

    return {"status": "ok", "method": method, "params": params}


def register(mcp: FastMCP) -> None:
    def _collab_error_response(tool_name: str, code: str, message_key: str,
                               detail: str) -> dict:
        """构造只读查询失败路径的 Structured_Reason（Req 1.12）。

        稳定错误码 + i18n message key（通过 t(key, default=...) 解析）。
        文案变化不改变错误码值。

        Args:
            tool_name: 工具名称
            code: 稳定错误码（如 E_COLLAB_QUERY_FAILED）
            message_key: i18n message key
            detail: 错误详情

        Returns:
            结构化错误响应 dict
        """
        msg = t(
            message_key,
            default=f"只读协同查询失败 ({tool_name}): {detail}",
            tool=tool_name,
            detail=detail,
        )
        return {
            "status": "error",
            "tool": tool_name,
            "reason": {
                "code": code,
                "message_key": message_key,
                "message": msg,
                "detail": detail,
            },
        }

    def _collab_rpc_call(tool_name: str, method: str,
                         params: Optional[Dict[str, Any]] = None) -> dict:
        """只读协同查询的公共 RPC 调用逻辑（不触发写操作）。

        流程：
        1. 通过 call_with_autostart 走 daemon（只读方法归类 READ_ONLY，允许降级）
        2. daemon 端未注册这些方法（collab RPC 在 daemon 层为 method_not_found）
        3. daemon 返回错误 / 降级 / 不可用 → direct_read 直查 SQLite 真实表（S5 接线）

        Args:
            tool_name: 调用方工具名称
            method: RPC 方法名（如 role_view.get）
            params: RPC 参数

        Returns:
            查询结果 dict 或错误响应
        """
        try:
            client = _get_daemon_client()
            response = client.call_with_autostart(method, params)
        except Exception as exc:
            # daemon 不可用或 RPC 未注册：直查 SQLite 真实表（库层就绪）
            return _collab_direct_read(get_db(), method, params or {})

        # 降级路径：只读方法允许 direct_read，直接查询物理 DB
        if response.get("degraded"):
            return _collab_direct_read(get_db(), method, params or {})

        result = response.get("result")
        if isinstance(result, dict) and result.get("error"):
            # daemon 返回了错误（如 method not found），回退到直读物理 DB
            err = result["error"]
            err_msg = err.get("message", str(err)) if isinstance(err, dict) else str(err)
            if "not found" in err_msg.lower() or "unknown method" in err_msg.lower():
                return _collab_direct_read(get_db(), method, params or {})
            # 其他错误返回 Structured_Reason
            return _collab_error_response(
                tool_name, "E_COLLAB_QUERY_FAILED",
                "error.collab_query_failed", err_msg
            )

        return result

    @mcp.tool()
    def get_role_view(task_id: str, role: str = "") -> dict:
        """获取指定任务和角色的 Role_View 投影（只读，Req 14.17）

        Role_View 是从 Envelope 生成的角色最小投影，包含角色履责所需字段。
        P1 阶段产品化后通过 daemon 的 role_view.get RPC 方法获取。

        Args:
            task_id: 任务 ID
            role: 角色名称（planner/implementer/reviewer/verifier）；
                  为空时返回调用方默认角色投影

        Returns:
            P1 启用后：Role_View dict（含 view_type/view_version/Contract_Hash 等）
            P1 未启用时：{"status": "planned", "stage": "P1", ...}
        """
        params = {"task_id": task_id}
        if role:
            params["role"] = role
        return _collab_rpc_call("get_role_view", "role_view.get", params)

    @mcp.tool()
    def find_evidence(task_id: str = "", contract_id: str = "",
                      verifier: str = "", limit: int = 50) -> dict:
        """查询 Evidence 记录（只读，Req 14.17）

        Evidence 是 verifier 针对指定契约和代码快照产生的不可变事实记录。
        P1 阶段产品化后通过 daemon 的 evidence.query RPC 方法查询。

        Args:
            task_id: 按任务 ID 过滤（可选）
            contract_id: 按契约 ID 过滤（可选）
            verifier: 按 verifier 名称过滤（可选）
            limit: 返回数量限制（默认 50）

        Returns:
            P1 启用后：{"items": [...], "count": N}
            P1 未启用时：{"status": "planned", "stage": "P1", ...}
        """
        params: Dict[str, Any] = {"limit": limit}
        if task_id:
            params["task_id"] = task_id
        if contract_id:
            params["contract_id"] = contract_id
        if verifier:
            params["verifier"] = verifier
        return _collab_rpc_call("find_evidence", "evidence.query", params)

    @mcp.tool()
    def get_freshness_status(evidence_id: str = "",
                             task_id: str = "") -> dict:
        """查询 Evidence 的 Freshness_Status（只读，Req 14.17）

        Freshness_Status 是查询时刻派生值（fresh/stale/invalid/superseded/
        historical_unbound），**不构成 gate 结论**——gate 结论只由 CLI 写路径
        在串行化点产生并记录（设计 §13.5 约束）。

        P1 阶段产品化后通过 daemon 的 freshness.status RPC 方法查询。

        Args:
            evidence_id: 指定 Evidence ID 查询其新鲜度（可选）
            task_id: 指定任务 ID 查询关联 Evidence 的新鲜度（可选）

        Returns:
            P1 启用后：{"items": [{"evidence_id": ..., "status": ...}]}
            P1 未启用时：{"status": "planned", "stage": "P1", ...}
        """
        params: Dict[str, Any] = {}
        if evidence_id:
            params["evidence_id"] = evidence_id
        if task_id:
            params["task_id"] = task_id
        return _collab_rpc_call("get_freshness_status", "freshness.status", params)

    @mcp.tool()
    def get_gate_decision(task_id: str = "", gate_id: str = "",
                          limit: int = 20) -> dict:
        """查询 gate decision 历史记录（只读，Req 14.17）

        gate decision 是 Evidence_Gate 依据 Current_Envelope、Gate_Snapshot、
        Evidence 和 verdict 做出的状态转换判定。本工具只读取历史判定，
        不触发新的 gate 评估。

        P1 阶段产品化后通过 daemon 的 gate.decision.query RPC 方法查询。

        Args:
            task_id: 按任务 ID 过滤（可选）
            gate_id: 按 gate 会话 ID 过滤（可选）
            limit: 返回数量限制（默认 20）

        Returns:
            P1 启用后：{"items": [...], "count": N}
            P1 未启用时：{"status": "planned", "stage": "P1", ...}
        """
        params: Dict[str, Any] = {"limit": limit}
        if task_id:
            params["task_id"] = task_id
        if gate_id:
            params["gate_id"] = gate_id
        return _collab_rpc_call("get_gate_decision", "gate.decision.query", params)

    @mcp.tool()
    def submit_verdict(task_id: str, contract_id: str,
                       contract_revision: int, contract_hash: str,
                       phase: str = "PRE_VERDICT", overall: str = "",
                       clause_results: str = "", findings: str = "",
                       reviewer_identity: str = "",
                       view_manifest_hash: str = "",
                       snapshot_id: str = "", attestation: str = "",
                       amendment_ref: str = "", verdict_id: str = "",
                       lease_token: str = "",
                       fencing_counter: int = 0) -> dict:
        """提交 Reviewer Verdict（写路径，P1）

        Verdict 写入 task_verdict_events，供 Evidence_Gate 评估消费。
        追加式记录，不修改既有 payload。属于受保护写操作：
        提供 lease_token + fencing_counter 时校验 Lease 有效性，过期/
        token 不匹配/旧 counter 在写入前拒绝（P4，Req 11.8-11.9）。

        Args:
            task_id: 关联任务 ID
            contract_id: 契约 ID
            contract_revision: 契约 revision（正整数）
            contract_hash: 契约 hash
            phase: Verdict 阶段（PRE_VERDICT/POST_VERDICT）
            overall: 总体结论（approved/rejected/needs_changes/unclear）
            clause_results: 条款级评审结果 JSON 字符串（可选）
            findings: 发现列表 JSON 字符串（可选）
            reviewer_identity: 评审者身份（agent/session marker）
            view_manifest_hash: 盲视 manifest hash（可选）
            snapshot_id: 绑定的 workspace snapshot id（可选）
            attestation: 评审者声明（可选）
            amendment_ref: 修订引用（可选）
            verdict_id: 显式 verdict id（可选，默认生成）
            lease_token: P4 Lease raw token（可选，提供时启用受保护写校验）
            fencing_counter: P4 当前 fencing counter（提供 lease_token 时必填）

        Returns:
            {"success": True, "verdict_id": ..., "event_id": ...}
            或 {"success": False, "error": ...}
        """
        import json as _json

        def _parse_json(s: str):
            if not s:
                return None
            try:
                return _json.loads(s)
            except Exception:
                return None

        try:
            db = get_db()
            if not hasattr(db, "submit_verdict"):
                return {"success": False, "error": "submit_verdict not available"}
            kwargs: Dict[str, Any] = {
                "task_id": task_id,
                "contract_id": contract_id,
                "contract_revision": int(contract_revision),
                "contract_hash": contract_hash,
                "phase": phase,
                "overall": overall,
                "clause_results": _parse_json(clause_results),
                "findings": _parse_json(findings),
                "reviewer_identity": reviewer_identity,
                "view_manifest_hash": view_manifest_hash,
                "snapshot_id": snapshot_id,
                "attestation": attestation,
                "amendment_ref": amendment_ref,
            }
            if verdict_id:
                kwargs["verdict_id"] = verdict_id
            if lease_token:
                kwargs["lease_token"] = lease_token
                kwargs["fencing_counter"] = int(fencing_counter)
            return db.submit_verdict(**kwargs)
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    @mcp.tool()
    def append_evidence(task_id: str, contract_id: str,
                        contract_revision: int, contract_hash: str,
                        evidence_type: str, snapshot_id: str = "",
                        verifier_name: str = "", verifier_version: str = "",
                        verifier_config_hash: str = "",
                        producer_identity: str = "",
                        payload: str = "", payload_hash: str = "",
                        test_run_id: str = "",
                        lease_token: str = "",
                        fencing_counter: int = 0) -> dict:
        """追加一条不可变 Evidence 记录（写路径，P1）

        Evidence 绑定 contract + snapshot + verifier 三元组，追加式记录
        不替换（重跑 verifier 追加新记录）。属于受保护写操作：
        提供 lease_token + fencing_counter 时校验 Lease 有效性（P4，
        Req 11.8-11.9），校验失败在写入前拒绝。

        Args:
            task_id: 关联任务 ID
            contract_id: 契约 ID
            contract_revision: 契约 revision（正整数）
            contract_hash: 契约 hash
            evidence_type: Evidence 类型（test_run/static_check/diff_manifest/
                           symbol_change/reviewer_verdict）
            snapshot_id: 绑定的 workspace snapshot id（可查 get_snapshot）
            verifier_name: Verifier 名称
            verifier_version: Verifier 版本
            verifier_config_hash: Verifier 配置摘要
            producer_identity: 生产者身份（agent/session/tool）
            payload: Evidence payload JSON 字符串（可选）
            payload_hash: payload 摘要（可选，为空自动计算）
            test_run_id: 关联测试运行 ID（可选）
            lease_token: P4 Lease raw token（可选，提供时启用受保护写校验）
            fencing_counter: P4 当前 fencing counter（提供 lease_token 时必填）

        Returns:
            {"success": True, "evidence_id": ..., "event_id": ...}
            或 {"success": False, "error": ...}
        """
        import json as _json

        def _parse_json(s: str):
            if not s:
                return None
            try:
                return _json.loads(s)
            except Exception:
                return None

        try:
            db = get_db()
            if not hasattr(db, "append_evidence"):
                return {"success": False, "error": "append_evidence not available"}
            # snapshot 为可选的 WorkspaceSnapshot；提供 snapshot_id 时构造最小快照，
            # 不解析文件/符号 hash（快照内容由调用方通过 payload 提供）
            snapshot = None
            if snapshot_id:
                try:
                    from ...db.task_snapshot import WorkspaceSnapshot
                    snapshot = WorkspaceSnapshot(snapshot_id=snapshot_id)
                except Exception:
                    snapshot = None
            kwargs: Dict[str, Any] = {
                "task_id": task_id,
                "contract_id": contract_id,
                "contract_revision": int(contract_revision),
                "contract_hash": contract_hash,
                "evidence_type": evidence_type,
                "snapshot": snapshot,
                "verifier_name": verifier_name,
                "verifier_version": verifier_version,
                "verifier_config_hash": verifier_config_hash,
                "producer_identity": producer_identity,
                "payload": _parse_json(payload),
                "payload_hash": payload_hash,
                "test_run_id": test_run_id,
            }
            if lease_token:
                kwargs["lease_token"] = lease_token
                kwargs["fencing_counter"] = int(fencing_counter)
            return db.append_evidence(**kwargs)
        except Exception as exc:
            return {"success": False, "error": str(exc)}
