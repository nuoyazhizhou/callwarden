"""多 LLM 契约协同（P1，原 [L13]）+ _collab_* 辅助

拆分自 server/mcp_server.py（4859-5137 行区间），由 register(mcp) 注册。

H4B-E（T-1786590214634-9e740cdc-h4b-unsupported-error）：governance/unsupported/error cutover
- dispatch.rs 无 collab RPC 分支（handle_collab_rpc 兜底一律 method_not_found），
  HttpDaemonRpcClient 也无 call_with_autostart。本模块 4 个只读工具
  （get_role_view/find_evidence/get_freshness_status/get_gate_decision）曾经
  `_collab_rpc_call` 在 HTTP 模式下触发 AttributeError 后意外掉入
  `_collab_direct_read(get_db())` 构造 CodeGraphDB——违反 fail-closed 契约，
  已改为 HTTP 模式直接 _http_unsupported() 结构化 unsupported
  （不构造 CodeGraphDB，无 SQLite fallback）。
- 非 HTTP（legacy）模式保持既有行为：4 只读工具经 _collab_rpc_call 尝试 daemon
  （method_not_found）后走 _collab_direct_read 直查 SQLite 真实表（S5 显式降级，
  工具 docstring 已注明 P1 计划）。

任务 4（MCP/CLI 路由转发与旧路径拒绝）：submit_verdict/append_evidence 写路径
薄壳化——Python 端只做 cw 客户端薄壳，把 legacy MCP 参数适配为 daemon native v1
wire 后经 `verdict.submit` / `evidence.append` RPC 透传（_collab_write_rpc）：
- 所有业务校验/落库全在 Rust daemon（handle_verdict_submit/handle_evidence_append，
  dispatch.handle_collab_rpc 路由），不再 get_db() 本地写；
- legacy→v1 冻结枚举迁移（PRE_VERDICT→blind_first_pass 等），非 v1 值 fail-closed；
- daemon 不可达/降级返回 E_*_DAEMON_UNAVAILABLE，不回退本地 SQLite（
  degraded_mode.GOVERNANCE_WRITE_OPS 语义）。
"""

# [L13] 多 LLM 契约协同——只读 MCP 查询工具面（Req 14.17, D0 任务 3.15）

import hashlib
import json
from typing import Any, Dict, List, Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import _get_daemon_client, get_db
from ...db import CodeGraphDB
from ...i18n import t
from callwarden.server.daemon_client import DaemonUnavailableError, route_worker_call
from callwarden.server.daemon_protocol import DaemonRemoteError

# H4C-2 第三批（T-1786747295227-b876fddf）：collab 组只读工具接入 compat worker。
# 注意：必须用顶层 `server.compat_registry` 导入，与 compat_worker.py 保持同一
# 模块单例（模块单例风险，见 tools_query.py L41-49 注释）。
from server.compat_registry import (  # noqa: E402
    SCOPE_WORKSPACE,
    CompatCallContext,
    register_compat_routes,
)

from ..daemon_client import route_rpc as _route


# ---------------------------------------------------------------------------
# Governance 写路径：daemon 权威转发薄壳（任务 4：旧路径拒绝）
# ---------------------------------------------------------------------------
# Python 端只做 cw 客户端薄壳：把 legacy MCP 参数适配为 daemon native v1 wire
# 后逐字段透传，所有业务校验/落库全在 Rust daemon，绝不 get_db() 本地写
# （degraded_mode.GOVERNANCE_WRITE_OPS = fail-closed）。
#
# legacy → v1 冻结枚举迁移（§4.2 表；对非 v1 值 fail-closed 拒绝）：
_VERDICT_PHASE_V1 = {
    "PRE_VERDICT": "blind_first_pass",
    "POST_VERDICT": "post_reveal_amendment",
}
_VERDICT_OVERALL_V1 = {
    "approved": "pass",
    "rejected": "block",
    "needs_changes": "block",
    "unclear": "block",
}
# daemon 不可达时的稳定错误码（per method）。
_WRITE_DAEMON_UNAVAILABLE_CODE = {
    "verdict.submit": "E_VERDICT_DAEMON_UNAVAILABLE",
    "evidence.append": "E_EVIDENCE_DAEMON_UNAVAILABLE",
    "task.remediation.create": "E_REMEDIATION_DAEMON_UNAVAILABLE",
    "task.step.resolve": "E_RESOLUTION_DAEMON_UNAVAILABLE",
}


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
        return _route('get_role_view', {"task_id": task_id, "role": role}, 'READ_ONLY')

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
        return _route('find_evidence', {"task_id": task_id, "contract_id": contract_id, "verifier": verifier, "limit": limit}, 'READ_ONLY')

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
        return _route('get_freshness_status', {"evidence_id": evidence_id, "task_id": task_id}, 'READ_ONLY')

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
        return _route('get_gate_decision', {"task_id": task_id, "gate_id": gate_id, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def submit_verdict(task_id: str, step_id: str,
                       contract_id: str, contract_revision: int,
                       contract_hash: str,
                       role_contract_id: str, role_contract_revision: int,
                       role_contract_hash: str,
                       phase: str = "PRE_VERDICT", overall: str = "",
                       clause_results: str = "", findings: str = "",
                       reviewer_identity: str = "",
                       view_manifest_hash: str = "",
                       snapshot_id: str = "", attestation: str = "",
                       amendment_ref: str = "", verdict_id: str = "",
                       lease_token: str = "",
                       fencing_counter: int = 0,
                       identity_role: str = "reviewer",
                       identity_agent_id: str = "",
                       identity_session_id: str = "",
                       identity_model_id: str = "",
                       request_id: str = "") -> dict:
        """提交 Reviewer Verdict（写路径，P1，daemon 权威）

        薄壳转发：把 legacy MCP 枚举适配为 daemon native v1 wire 后经
        ``verdict.submit`` RPC 透传（业务校验/落库全在 Rust daemon，不回退
        本地 SQLite）。不可以执行时 fail-closed 返回结构化错误。

        Args:
            task_id: 关联任务 ID
            step_id: 被审步骤 ID（daemon 校验 step current binding）
            contract_id: Task Contract ID
            contract_revision: Task Contract revision（正整数）
            contract_hash: Task Contract hash
            role_contract_id: Role Contract revision id（role_contract_revision_id）
            role_contract_revision: Role Contract revision（正整数）
            role_contract_hash: Role Contract hash
            phase: Verdict 阶段（legacy PRE_VERDICT/POST_VERDICT，→ v1
                   blind_first_pass/post_reveal_amendment）
            overall: 总体结论（legacy approved/rejected/needs_changes/unclear，
                     → v1 pass/block）
            clause_results: 条款级评审结果 JSON 字符串（可选）
            findings: 发现列表 JSON 字符串（可选）
            reviewer_identity: 评审者身份说明（legacy 兼容，透传仅供参考）
            view_manifest_hash: 盲视 manifest hash（可选）
            snapshot_id: 绑定的 workspace snapshot id（可选）
            attestation: 评审者声明
            amendment_ref: post_reveal_amendment 时引用的 sealed verdict_id
            verdict_id: 兼容保留字段（native 由 daemon 确定性生成）
            lease_token: daemon reviewer lease raw token（受保护写必填，P4）
            fencing_counter: 当前 fencing counter（受保护写必填）
            identity_role: 授权身份 role（reviewer/independent_reviewer）
            identity_agent_id: 授权身份 agent_id（必填）
            identity_session_id: 授权身份 session_id（必填）
            identity_model_id: 授权身份 model_id（必填）
            request_id: 幂等 request_id（可选，缺省 daemon client 生成 uuid）

        Returns:
            {"success": True, "verdict_id": ..., "event_id": ...}
            或 {"success": False, "error": {code, message}}
        """
        return _route('verdict.submit', {"task_id": task_id, "step_id": step_id, "contract_id": contract_id, "contract_revision": contract_revision, "contract_hash": contract_hash, "role_contract_id": role_contract_id, "role_contract_revision": role_contract_revision, "role_contract_hash": role_contract_hash, "phase": phase, "overall": overall, "clause_results": clause_results, "findings": findings, "reviewer_identity": reviewer_identity, "view_manifest_hash": view_manifest_hash, "snapshot_id": snapshot_id, "attestation": attestation, "amendment_ref": amendment_ref, "verdict_id": verdict_id, "lease_token": lease_token, "fencing_counter": fencing_counter, "identity_role": identity_role, "identity_agent_id": identity_agent_id, "identity_session_id": identity_session_id, "identity_model_id": identity_model_id, "request_id": request_id}, 'GOVERNANCE_WRITE')

    @mcp.tool()
    def append_evidence(task_id: str, step_id: str,
                        evidence_id: str, evidence_type: str,
                        manifest_path: str,
                        contract_id: str = "", contract_revision: int = 0,
                        contract_hash: str = "",
                        snapshot_id: str = "",
                        verifier_name: str = "", verifier_version: str = "",
                        verifier_config_hash: str = "",
                        producer_identity: str = "",
                        payload: str = "", payload_hash: str = "",
                        test_run_id: str = "",
                        lease_token: str = "",
                        fencing_counter: int = 0,
                        identity_role: str = "implementer",
                        identity_agent_id: str = "",
                        identity_session_id: str = "",
                        identity_model_id: str = "",
                        request_id: str = "") -> dict:
        """追加一条不可变 Evidence 记录（写路径，P1，daemon 权威）

        薄壳转发：把 MCP 参数适配为 daemon native v1 wire 后经
        ``evidence.append`` RPC 透传（业务校验/落库全在 Rust daemon，不回退
        本地 SQLite）。不可以执行时 fail-closed 返回结构化错误。

        Args:
            task_id: 关联任务 ID
            step_id: 关联 step ID（daemon 校验属于 task）
            evidence_id: Evidence ID（必填，唯一约束由 daemon 强制）
            evidence_type: Evidence 类型（test_run/static_check/diff_manifest/
                           symbol_change/reviewer_verdict）
            manifest_path: 证据 manifest 相对路径，必须以 docs/evidence/ 开头
                           （daemon 校验 E_EVIDENCE_MANIFEST_PATH_INVALID）
            contract_id: Task Contract ID（legacy 兼容，透传）
            contract_revision: Task Contract revision（legacy 兼容，透传）
            contract_hash: Task Contract hash（legacy 兼容，透传）
            snapshot_id: 绑定的 workspace snapshot id（→ daemon workspace_snapshot_id）
            verifier_name: Verifier 名称
            verifier_version: Verifier 版本
            verifier_config_hash: Verifier 配置摘要
            producer_identity: 生产者身份说明（legacy 兼容，仅供参考）
            payload: Evidence payload JSON 字符串（→ daemon evidence_json）
            payload_hash: payload 摘要（可选，为空时按 payload 文本派生 sha256）
            test_run_id: 关联测试运行 ID（legacy 兼容，仅供参考）
            lease_token: daemon lease raw token（受保护写必填，P4）
            fencing_counter: 当前 fencing counter（受保护写必填）
            identity_role: 授权身份 role（默认 implementer）
            identity_agent_id: 授权身份 agent_id（必填）
            identity_session_id: 授权身份 session_id（必填）
            identity_model_id: 授权身份 model_id（必填）
            request_id: 幂等 request_id（可选，缺省 daemon client 生成 uuid）

        Returns:
            {"success": True, "evidence_id": ..., "event_id": ...}
            或 {"success": False, "error": {code, message}}
        """
        return _route('evidence.append', {"task_id": task_id, "step_id": step_id, "evidence_id": evidence_id, "evidence_type": evidence_type, "manifest_path": manifest_path, "contract_id": contract_id, "contract_revision": contract_revision, "contract_hash": contract_hash, "snapshot_id": snapshot_id, "verifier_name": verifier_name, "verifier_version": verifier_version, "verifier_config_hash": verifier_config_hash, "producer_identity": producer_identity, "payload": payload, "payload_hash": payload_hash, "test_run_id": test_run_id, "lease_token": lease_token, "fencing_counter": fencing_counter, "identity_role": identity_role, "identity_agent_id": identity_agent_id, "identity_session_id": identity_session_id, "identity_model_id": identity_model_id, "request_id": request_id}, 'GOVERNANCE_WRITE')

    @mcp.tool()
    def task_remediation_create(
        task_id: str,
        source_step_id: str,
        request_id: str,
        lease_token: str,
        fencing_counter: int,
        source_outcome: str = "failed_step",
        source_verdict_id: str = "",
        source_findings: str = "",
        identity_role: str = "implementer",
        identity_agent_id: str = "",
        identity_session_id: str = "",
        identity_model_id: str = "",
    ) -> dict:
        """为历史 failed/reviewer_blocked 步骤创建带 provenance 的 fix_defect 步骤（写路径，daemon 权威）

        薄壳转发：把 MCP 参数适配为 daemon native v1 wire 后经
        ``task.remediation.create`` RPC 透传（业务校验/落库全在 Rust daemon，
        不回退本地 SQLite）。不可以执行时 fail-closed 返回结构化错误。

        Args:
            task_id: 目标任务 ID（source_step_id 必须属于该任务）
            source_step_id: 被 remediation 的源步骤 ID（failed_step 场景即
                            failed 步骤；别名 failed_step_id 亦可，daemon 兼容）
            source_outcome: 源结局枚举：failed_step（默认）| reviewer_blocked |
                            adjudicator_returned
            source_verdict_id: reviewer_blocked/adjudicator_returned 时必填的
                               source verdict id（failed_step 场景必须留空）
            source_findings: reviewer_blocked/adjudicator_returned 时必填的
                             source findings JSON 字符串（failed_step 场景留空）
            request_id: 幂等 request_id（daemon 端去重/重放，必填）
            lease_token: daemon implementer lease raw token（受保护写必填）
            fencing_counter: 当前 fencing counter（受保护写必填）
            identity_role: 授权身份 role（默认 implementer）
            identity_agent_id: 授权身份 agent_id（必填）
            identity_session_id: 授权身份 session_id（必填）
            identity_model_id: 授权身份 model_id（必填）

        Returns:
            {"success": True, "remediation_step_id": ..., "source_step_id": ...,
             "request_id": ..., "replayed": ...}
            或 {"success": False, "error": {code, message}}
        """
        return _route('task.remediation.create', {"task_id": task_id, "source_step_id": source_step_id, "request_id": request_id, "lease_token": lease_token, "fencing_counter": fencing_counter, "source_outcome": source_outcome, "source_verdict_id": source_verdict_id, "source_findings": source_findings, "identity_role": identity_role, "identity_agent_id": identity_agent_id, "identity_session_id": identity_session_id, "identity_model_id": identity_model_id}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def task_step_resolve(
        task_id: str,
        failed_step_id: str,
        remediation_step_id: str,
        request_id: str,
        evidence_path: str,
        evidence_hash: str,
        lease_token: str,
        fencing_counter: int,
        identity_role: str = "implementer",
        identity_agent_id: str = "",
        identity_session_id: str = "",
        identity_model_id: str = "",
    ) -> dict:
        """将已 done 的 fix_defect 绑定到不可变 failed 步骤的 resolution event（写路径，daemon 权威）

        薄壳转发：把 MCP 参数适配为 daemon native v1 wire 后经
        ``task.step.resolve`` RPC 透传（业务校验/落库全在 Rust daemon，
        不回退本地 SQLite）。不修改 failed 步骤本身，只追加
        ``step_resolved`` 事件并推进任务状态。

        Args:
            task_id: 目标任务 ID（failed_step_id 与 remediation_step_id 必须属于该任务）
            failed_step_id: 被解析的 failed 步骤 ID（daemon 校验仍为 failed）
            remediation_step_id: 已 done 且 provenance 指向 failed_step_id 的
                                 fix_defect 步骤 ID
            request_id: 幂等 request_id（daemon 端去重/重放，必填）
            evidence_path: 证据 manifest 相对路径（必填，非空）
            evidence_hash: 证据 sha256 摘要（必填，非空）
            lease_token: daemon implementer lease raw token（受保护写必填）
            fencing_counter: 当前 fencing counter（受保护写必填）
            identity_role: 授权身份 role（默认 implementer）
            identity_agent_id: 授权身份 agent_id（必填）
            identity_session_id: 授权身份 session_id（必填）
            identity_model_id: 授权身份 model_id（必填）

        Returns:
            {"success": True, "resolution_event_id": ..., "status": ...,
             "request_id": ..., "replayed": ...}
            或 {"success": False, "error": {code, message}}
        """
        return _route('task.step.resolve', {"task_id": task_id, "failed_step_id": failed_step_id, "remediation_step_id": remediation_step_id, "request_id": request_id, "evidence_path": evidence_path, "evidence_hash": evidence_hash, "lease_token": lease_token, "fencing_counter": fencing_counter, "identity_role": identity_role, "identity_agent_id": identity_agent_id, "identity_session_id": identity_session_id, "identity_model_id": identity_model_id}, 'PROTECTED_MUTATION')


# ============================================================
# H4C-2 第三批（T-1786747295227-b876fddf）：collab 组只读工具 worker handler
# ============================================================
# 接入说明（与 tools_summary/tools_security 同款模式）：
# - handler 直接复用模块级 `_collab_direct_read`（L53-176 纯只读：SQL SELECT +
#   db 只读方法调用，已核验 derive_freshness / list_evidence_for_task /
#   list_evidence_for_contract / get_evidence / get_role_view 均为纯查询，
#   worker 只读连接可承载），仅需注入 _bind_readonly_db 绑定的只读 db 实例；
# - 写语义工具（submit_verdict / append_evidence，governance_write）不接入
#   worker（任务 4 起改经 daemon RPC 薄壳转发，见上方 _collab_write_rpc）。
_COLLAB_COMPAT_SCOPE = SCOPE_WORKSPACE  # 矩阵 workspace_scoped


def _bind_readonly_db(ctx: CompatCallContext) -> CodeGraphDB:
    """轻量只读绑定：绕过 CodeGraphDB.__init__，注入 worker 只读连接与显式 workspace。

    与 tools_query.py / tools_summary.py / tools_security.py 同款：ctx.conn 由
    compat_worker 用 `file:{db_path}?mode=ro` 打开（read_only 契约）；
    active_workspace 注入 ctx.workspace_id，db 层查询基于
    `_get_active_workspace_id()` 过滤。
    """
    db = object.__new__(CodeGraphDB)
    db.conn = ctx.conn
    db.active_workspace = {"id": ctx.workspace_id} if ctx.workspace_id else None
    db.workspace_root = None
    if ctx.workspace_id is not None:
        try:
            row = ctx.conn.execute(
                "SELECT root_path FROM workspaces WHERE id = ?",
                (ctx.workspace_id,),
            ).fetchone()
            if row is not None:
                db.workspace_root = row["root_path"]
        except Exception:
            db.workspace_root = None
    return db


def _h_get_freshness_status(ctx: CompatCallContext) -> Any:
    """worker handler：查询 Evidence Freshness_Status（只读，复刻 freshness.status）"""
    return _collab_direct_read(_bind_readonly_db(ctx), "freshness.status", ctx.params)


def _h_gate_decision(ctx: CompatCallContext) -> Any:
    """worker handler：查询 gate decision 历史（只读，复刻 gate.decision.query）"""
    return _collab_direct_read(_bind_readonly_db(ctx), "gate.decision.query", ctx.params)


# collab 组只读白名单（原 4 个；get_role_view 已 MCP-001 迁移 rust_native，
# T-1787321708699-da5d8224；find_evidence 已 MCP-002 迁移 rust_native，
# T-1787321708760-de068a9c，移除 compat 注册，剩 2 个）。写语义工具
# （submit_verdict / append_evidence，governance_write）不接入，fail-closed。
_COLLAB_READ_ONLY_METHODS: Dict[str, Any] = {
    "get_freshness_status": _h_get_freshness_status,
    "get_gate_decision": _h_gate_decision,
}

# 模块级注册：worker 装配 import 本 .module 时执行，注册到 compat_registry 单例并
# 同步 RUST_COMPAT_ROUTE（Rust 侧 http_server.rs 白名单在步骤#2 同步）。
register_compat_routes(
    _COLLAB_READ_ONLY_METHODS,
    workspace_scope=_COLLAB_COMPAT_SCOPE,
    description="H4C-2 第三批 collab 组只读工具（2 个；get_role_view/find_evidence 已迁移 rust_native）",
)


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
