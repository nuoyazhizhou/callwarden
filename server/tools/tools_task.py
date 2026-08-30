"""任务驱动编排（task_create/next/report/rollback/close 等，原 [L5]）

拆分自 server/mcp_server.py（1591-3050 行区间），由 register(mcp) 注册。

H4B-E（T-1786590214634-9e740cdc-h4b-unsupported-error）：governance/unsupported/error cutover
- 任务工具已统一走 route_task_write/route_task_read（task.* 在 dispatch.rs 有真实
  RPC 分支，HTTP 模式经 HttpDaemonRpcClient.call 透传，无伪路由），本任务不触碰。
- get_symbol_issues / get_test_cases / get_tested_functions /
  get_test_coverage_summary / get_test_stability 5 个工具曾直接调用
  `_get_daemon_client()` 的便捷方法，而 HttpDaemonRpcClient 无这些便捷方法——
  HTTP 模式必 AttributeError。已改为 HTTP 模式经通用 call 透传真实 RPC
  query.issues / query.tests（dispatch.rs 真名，与 daemon_client.py 便捷方法
  内部 _remote_query 同名对齐）；legacy 模式保持便捷方法调用，语义不变。
- get_symbol_change_tasks / get_commit_tasks / task_plan_template 3 个工具曾因
  route_task_read 直传 dispatch.rs 不存在的 RPC（task.get_change_tasks /
  task.get_commit_tasks / task.plan_template）在 HTTP 模式必抛 method_not_found。
  已按 H4C-2/3 方案改走 route_worker_call（Rust 白名单 + Python 镜像 handler
  三端齐全），HTTP/enterprise 经 compat worker 执行（fail-closed）；local/auto
  降级 _local() 本地 SQL，公开语义不变（T-1786716190783-ba187c88 整改 2）。
"""

# [L5] 任务驱动编排工具（task_create / task_next_step / work_next_job 等）

from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import _get_daemon_client, _get_db_path_for_daemon, get_db
from callwarden.server.daemon_client import (
    is_http_transport_enabled,
    route_task_write,
    route_task_read,
    route_worker_call,
)

# H4C-3（T-1786716190783-ba187c88 步骤#1）：任务组只读工具接入 compat worker。
# 必须用顶层 `server.compat_registry` 导入，与 compat_worker.py 保持同一模块
# 单例（模块单例风险，见 tools_query.py 同款注释）。
from ...db import CodeGraphDB
from server.compat_registry import (  # noqa: E402
    SCOPE_WORKSPACE,
    CompatCallContext,
    register_compat_routes,
)

from ..daemon_client import route_rpc as _route


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def task_create(title: str, description: str = "", steps: list = None, creator: str = "agent") -> str:
        """创建任务并返回 task_id

        Agent 通过此工具创建有步骤的任务，然后通过 task_next_step 逐步执行。

        Args:
            title: 任务标题
            description: 任务描述
            steps: 步骤列表，每个元素含 action/target_file/target_symbol/check_items
            creator: 创建者标识

        Returns:
            task_id
        """
        return _route('task.create', {"title": title, "description": description, "steps": steps, "creator": creator}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def task_next_step(task_id: str, agent_session_id: str = "", identity: dict = None, contract_claim: dict = None, agent_instance_id: str = "") -> Optional[dict]:
        """领取任务的下一个待执行步骤

        Agent 必须通过此工具领取步骤，不能自由决定下一步操作。
        返回步骤详情（文件、操作、检查项），Agent 只能执行这一步。

        Before-Edit Contract：当步骤为编辑类操作时，系统自动调用护栏检查。
        - 若返回 guardrail_alert（decision=block）：步骤状态为 blocked，
          Agent 必须先处理告警，再调用 task_resolve_block 恢复步骤。
        - 若返回 guardrail_warning（decision=warn）：步骤可执行，但需关注告警。
        - 否则正常执行。

        A2/A3 合同领取（agent-task-contract-design.md §4.1/4.2）：
        - identity: Agent 身份 JSON（agent_id/agent_instance_id/session_id/model_id/role 等）。
          冻结 Role Contract 的任务必须携带 identity（fail-closed），未注册身份禁止领取。
        - agent_instance_id: 独立参数；提供时并入 identity（identity 已含该字段则以其为准）。
          注册 instance 非空的 agent 必须携带一致值，否则 daemon 报 E_IDENTITY_INSTANCE_MISMATCH。
        - contract_claim: 声明本次领取使用的 skill_id/skill_version/prompt_hash；
          与任务冻结合同不符时拒绝领取（E_CONTRACT_*_MISMATCH）。
        - 领取成功时返回 role_contract（Task Envelope）供 Agent 遵守。

        Args:
            task_id: 任务 ID
            agent_session_id: Agent 会话 ID（可选）。同一 Windows 用户多 Agent/多 IDE
                并发认领同一任务时用于区分不同逻辑 Agent；缺省时 daemon 以连接身份为准。
            identity: Agent 身份 JSON（可选；合同任务必填）
            contract_claim: 合同声明 JSON（可选）
            agent_instance_id: Agent 实例 ID（可选；identity 未含该字段时并入）

        Returns:
            步骤详情，如果没有待执行步骤则返回 None
        """
        if identity and agent_instance_id and not identity.get("agent_instance_id"):
            identity = {**identity, "agent_instance_id": agent_instance_id}
        return _route('task.claim', {"task_id": task_id, "agent_session_id": agent_session_id, "identity": identity, "contract_claim": contract_claim}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def work_next_job(task_id: str) -> Optional[dict]:
        """领取下一项 Agent 工作，并返回完成它所需的最小上下文

        这是 Agent 优先入口：相比手动 read/grep/plan，本工具返回目标、
        符号源码、调用上下文、文件健康、允许编辑范围、推荐 patch 工具
        和完成后汇报方式。
        """
        return _route('task.work_next', {"task_id": task_id}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def task_assignment_status(
        task_id: str, step_id: str = "", role: str = ""
    ) -> dict:
        """读取任务的 daemon durable assignment 队列投影。

        返回当前及历史 assignment 事件投影；不会在查询时创建、释放或接管任务。
        """
        params = {"task_id": task_id}
        if step_id:
            params["step_id"] = step_id
        if role:
            params["role"] = role
        return _route('task.assignment.status', params, 'READ_ONLY')

    @mcp.tool()
    def task_assignment_heartbeat(
        task_id: str,
        assignment_id: str,
        agent_session_id: str = "",
        identity: dict = None,
        request_id: str = "",
        fencing_counter: int = None,
    ) -> dict:
        """向 daemon 发送当前 assignment 的心跳。

        daemon 会校验 task、assignment、holder/session 和 fencing；MCP 不维护队列状态。
        """
        return _route(
            'task.assignment.heartbeat',
            {
                "task_id": task_id,
                "assignment_id": assignment_id,
                "agent_session_id": agent_session_id,
                "identity": identity,
                "request_id": request_id,
                "fencing_counter": fencing_counter,
            },
            'PROTECTED_MUTATION',
        )

    @mcp.tool()
    def task_resolve_block(task_id: str, step_id: str, resolution: str = "ack") -> Optional[dict]:
        """处理 blocked 步骤的护栏告警，恢复为 pending 以便重新领取

        当 task_next_step 返回 guardrail_alert（block 级别）时，Agent 处理告警后
        调用此方法将步骤从 blocked 恢复为 pending，之后可再次 task_next_step 领取。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
            resolution: 处理方式（ack/override/fix_applied）

        Returns:
            更新后的步骤详情，若步骤不存在或非 blocked 状态则返回 None
        """
        return _route('task.reopen', {"task_id": task_id, "step_id": step_id, "resolution": resolution}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def task_report_step(task_id: str, step_id: str, result: str = "", success: bool = True, changes: list = None, identity: dict = None, agent_instance_id: str = "", snapshot_id: str = "") -> Optional[dict]:
        """回报步骤执行结果

        如果失败，系统会自动插入"修复缺陷"步骤，Agent 无法跳过。
        如果成功且无更多步骤，任务状态变为 review。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
            result: 执行结果描述
            success: 是否成功
            changes: 变更记录列表
            identity: P3 结构化身份 JSON（{agent_id, agent_instance_id, session_id, model_id, role}，
                      可选；提供后由包装层校验并透传给 db 层，不得伪造缺省身份）
            agent_instance_id: Agent 实例 ID（可选；identity 未含该字段时并入）
            snapshot_id: 真实 review snapshot reference（可选；缺省时保持 no_snapshot）

        Returns:
            下一步步骤信息（如果有）
        """
        if identity and agent_instance_id and not identity.get("agent_instance_id"):
            identity = {**identity, "agent_instance_id": agent_instance_id}
        return _route('task.report', {"task_id": task_id, "step_id": step_id, "result": result, "success": success, "changes": changes, "identity": identity, "snapshot_id": snapshot_id}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def record_task_symbol_change(task_id: str, file_path: str, step_id: str = "",
                                  edit_audit_id: int = 0, change_audit_id: str = "",
                                  qualified_name: str = "", symbol_name: str = "",
                                  symbol_hash_before: str = "", symbol_hash_after: str = "",
                                  change_type: str = "modified", source: str = "manual",
                                  metadata: dict = None) -> dict:
        """记录任务/步骤到文件或符号版本变化的归因"""
        return _route('task.record_symbol_change', {"task_id": task_id, "file_path": file_path, "step_id": step_id, "edit_audit_id": edit_audit_id, "change_audit_id": change_audit_id, "qualified_name": qualified_name, "symbol_name": symbol_name, "symbol_hash_before": symbol_hash_before, "symbol_hash_after": symbol_hash_after, "change_type": change_type, "source": source, "metadata": metadata}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def link_edit_audit_symbols(audit_id: int, step_id: str = "") -> dict:
        """刷新图谱后，将某次 edit_audit 的 before/after 文件版本映射到符号变化"""
        return _route('task.link_edit_audit_symbols', {"audit_id": audit_id, "step_id": step_id}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def get_task_symbol_changes(task_id: str, step_id: str = "", file_path: str = "", limit: int = 100) -> list:
        """查询任务或步骤归因到的文件/符号变化"""
        return _route('task.get_symbol_changes', {"task_id": task_id, "step_id": step_id, "file_path": file_path, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def get_symbol_change_tasks(symbol_hash: str = "", qualified_name: str = "", limit: int = 50) -> list:
        """反查某个符号版本或符号名由哪些任务改变过"""
        return _route('get_symbol_change_tasks', {"symbol_hash": symbol_hash, "qualified_name": qualified_name, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def get_task_commits(task_id: str, include_commit_details: bool = True) -> list:
        """查询任务关联的所有 commit"""
        return _route('task.get_commits', {"task_id": task_id, "include_commit_details": include_commit_details}, 'READ_ONLY')

    @mcp.tool()
    def get_commit_tasks(commit_hash: str, include_task_details: bool = True) -> list:
        """查询 commit 关联的所有 task

        W4-1（T-1786886251769-22b94ee8-sub-1）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.commit_tasks，经
        snapshot query_db_path 访问主库 task_symbol_changes LEFT JOIN tasks，
        注入权威 workspace_instance_id）；local/legacy 模式保留原路由语义
        （local 走本地 db 回退，enterprise/auto 走 compat worker）。
        """
        return _route('query.commit_tasks', {"commit_hash": commit_hash, "include_task_details": include_task_details}, 'READ_ONLY')

    @mcp.tool()
    def task_rollback(task_id: str, change_id: str = None, reason: str = "") -> dict:
        """回滚任务中的变更"""
        return _route('task.rollback', {"task_id": task_id, "change_id": change_id, "reason": reason}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def task_apply(
        task_id: str,
        reviewer: str = "reviewer",
        identity: str = "",
        lease_token: str = "",
        fencing_counter: int = 0,
    ) -> dict:
        """审核通过：将任务状态从 review 改为 applied

        enterprise/auto（daemon 权威路径）：必须携带完整 reviewer lease 凭证
        （lease_token + fencing_counter，来自 lease_acquire 返回值），
        否则 daemon 返回 E_LEASE_REQUIRED fail-closed。
        identity: P3 结构化身份 JSON 字符串或对象（{agent_id, agent_instance_id,
                  session_id, model_id, role}）；注册 instance 非空的 agent 必须携带
                  一致 agent_instance_id，否则 E_IDENTITY_INSTANCE_MISMATCH。
        local（本地开发兼容路径）：提供凭证时执行受保护写校验，缺省时跳过校验。
        """
        return _route('task.apply', {"task_id": task_id, "reviewer": reviewer, "identity": identity, "lease_token": lease_token, "fencing_counter": fencing_counter}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def task_close(
        task_id: str,
        reviewer: str = "reviewer",
        identity: str = "",
        lease_token: str = "",
        fencing_counter: int = 0,
    ) -> dict:
        """关闭任务：将任务状态从 applied 改为 closed

        enterprise/auto（daemon 权威路径）：必须携带完整 reviewer lease 凭证
        （lease_token + fencing_counter），否则 daemon 返回 E_LEASE_REQUIRED fail-closed。
        identity: P3 结构化身份 JSON 字符串或对象（{agent_id, agent_instance_id,
                  session_id, model_id, role}）；注册 instance 非空的 agent 必须携带
                  一致 agent_instance_id，否则 E_IDENTITY_INSTANCE_MISMATCH。
        local（本地开发兼容路径）：提供凭证时执行受保护写校验，缺省时跳过校验。
        """
        return _route('task.close', {"task_id": task_id, "reviewer": reviewer, "identity": identity, "lease_token": lease_token, "fencing_counter": fencing_counter}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def task_capture_diff(
        task_id: str,
        step_id: str = "",
        base: str = "",
        dry_run: bool = True,
        source_commit_hash: str = "",
        skip_quality_review: bool = False,
    ) -> dict:
        """捕获外部 Agent 真实文件改动到 task/change/symbol/audit 闭环"""
        return _route('task.capture_diff', {"task_id": task_id, "step_id": step_id, "base": base, "dry_run": dry_run, "source_commit_hash": source_commit_hash, "skip_quality_review": skip_quality_review}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def audit_verify_chain(table_name: str = "", limit: int = 1000) -> dict:
        """验证审计签名链连续性与签名匹配

        检查 audit_chain 表中每条记录：
        1. record_signature 是否匹配重新计算的签名
        2. prev_signature 是否匹配上一条记录的 record_signature
        3. 首条记录的 prev_signature 是否为空串

        用于检测直接改库导致的篡改。

        Args:
            table_name: 指定表名时只验证该表的链；为空时验证全部
            limit: 最多验证的记录数，默认 1000

        Returns:
            {
                "table_name": str,       # 验证的表名（空串表示全部）
                "total_count": int,      # 验证的记录总数
                "verified_count": int,   # 通过验证的记录数
                "broken_count": int,     # 不通过的记录数
                "broken_records": [     # 不通过的记录列表
                    {"id": int, "table_name": str, "record_id": str, "reasons": [str]}
                ],
                "security_level": str,  # "hmac" 或 "hash_only"
            }
        """
        return _route('audit_verify_chain', {"table_name": table_name, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def rotate_audit_signing_key(key_id: str, key_secret: str = "") -> dict:
        """轮换审计签名密钥（C7）

        流程：
        1. 将当前 active 密钥置为 inactive（is_active=0）
        2. 插入新密钥记录（is_active=1）

        轮换后：
        - 新的 sign_audit_record 调用使用新密钥签名（signing_key_id = key_id）
        - 旧记录保持原签名不变（signing_key_id 不变）
        - verify_audit_chain 按 signing_key_id 查找对应密钥验证

        Args:
            key_id: 新密钥标识（唯一，如 "key-2026-07"）
            key_secret: 新密钥内容（用于 HMAC 计算）；为空时自动生成 32 字节随机密钥

        Returns:
            {
                "success": True,
                "key_id": str,           # 新密钥标识
                "rotated_at": float,     # 轮换时间戳
                "previous_key_id": str,  # 前一个 active 密钥的 key_id（无则为空串）
            }
            失败时：{"success": False, "error": str}
        """
        return _route('admin.audit_rotate_key', {"key_id": key_id, "key_secret": key_secret}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def list_audit_signing_keys() -> list:
        """列出所有签名密钥轮换记录（C7）

        按 rotated_at 倒序返回，每项含 key_id/rotated_at/is_active，
        不返回 key_secret 以避免泄露密钥内容。

        Returns:
            [
                {"key_id": str, "rotated_at": float, "is_active": int},
                ...
            ]
            失败时：[{"error": str}]
        """
        return _route('list_audit_signing_keys', {}, 'READ_ONLY')

    @mcp.tool()
    def bootstrap_status() -> dict:
        """返回自举健康状态摘要

        汇总以下信息，帮助判断当前自举闭环是否健康：

        1. db_stale：DB 是否滞后（最近一次 scan_run 的 git_head 与当前 HEAD 不一致）
        2. active_rules_count：已生效的 agent_rules 数量
        3. pending_candidates_count：待审核的 rule candidates 数量
        4. open_findings_count：open 状态的 quality findings 数量
        5. blocking_findings_count：block 严重度的 quality findings 数量
        6. audit_verify：audit_chain 验证结果摘要
        7. latest_scan_run：最近一次 workspace_scan_runs 记录
        8. tasks：按状态分组的任务计数（open / in_progress / review / applied）
        9. recommended_next_action：推荐下一条命令

        Returns:
            {
                "db_stale": bool,
                "current_head": str,
                "active_rules_count": int,
                "pending_candidates_count": int,
                "open_findings_count": int,
                "blocking_findings_count": int,
                "audit_verify": {
                    "total_count": int,
                    "verified_count": int,
                    "broken_count": int,
                    "security_level": str,
                },
                "latest_scan_run": {...} | None,
                "tasks": {"open": int, "in_progress": int, "review": int, "applied": int},
                "recommended_next_action": str,
            }
        """
        return _route('bootstrap_status', {}, 'READ_ONLY')

    @mcp.tool()
    def detect_clones(
        file_filter: str = "",
        min_lines: int = 5,
        similarity_threshold: float = 0.8,
    ) -> dict:
        """检测重复代码（Type-1/2/3 克隆）— 同步版本

        注意：对于 20 万符号级别的大型代码库，请使用 detect_clones_async
        提交后台 job，避免 MCP 请求超时。
        detect_clones_async 提交后可用 wait_for_job 等待完成，
        结果通过 list_clone_groups / get_clone_group_stats 查询。

        检测范围：
        - Type-1：完全相同的符号内容（content_hash 相同）
        - Type-2：重命名克隆（token 序列相同，标识符名不同）
        - Type-3：微调克隆（token 集合 Jaccard 相似度 >= similarity_threshold）

        结果持久化到 clone_pairs 表（UPSERT），支持重复执行。

        Args:
            file_filter: 文件路径前缀过滤（如 "src/core/"），空字符串扫描所有
            min_lines: 最小符号行数，低于此值的符号跳过（默认 5）
            similarity_threshold: Type-3 相似度阈值 [0,1]（默认 0.8）

        Returns:
            {
                "total_pairs": int,
                "type1_pairs": int,
                "type2_pairs": int,
                "type3_pairs": int,
                "scanned_symbols": int,
                "skipped_symbols": int,
                "similarity_threshold": float,
                "min_lines": int,
            }
        """
        _res = _route('task.job_submit', {**{"file_filter": file_filter, "min_lines": min_lines, "similarity_threshold": similarity_threshold}, "job_type": "clone_detect", "sync": True}, 'PROTECTED_MUTATION')
        return _res.get("result") if isinstance(_res, dict) and "result" in _res else _res

    @mcp.tool()
    def list_clones(
        clone_type: int = 0,
        min_similarity: float = 0.0,
        limit: int = 100,
        symbol_id: int = 0,
    ) -> list:
        """列出检测到的克隆对

        Args:
            clone_type: 克隆类型过滤（0=全部，1/2/3 对应 Type-N）
            min_similarity: 最低相似度过滤（默认 0.0）
            limit: 返回上限（默认 100）
            symbol_id: 只返回涉及此符号的克隆对（0=不过滤）

        Returns:
            克隆对列表，按相似度降序，每项包含：
            {
                "clone_type": int,
                "similarity": float,
                "token_hash": str,
                "lines_a": int, "lines_b": int,
                "detected_at": float,
                "symbol_a_name": str, "symbol_a_qualified": str,
                "symbol_a_line": int,
                "symbol_b_name": str, "symbol_b_qualified": str,
                "symbol_b_line": int,
                "file_a": str, "file_b": str,
            }
        """
        return _route('list_clones', {"clone_type": clone_type, "min_similarity": min_similarity, "limit": limit, "symbol_id": symbol_id}, 'READ_ONLY')

    @mcp.tool()
    def get_clone_stats() -> dict:
        """获取克隆检测统计信息

        W2-2（T-1786840097330-a9e0ec69）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native task.clone_stats，
        经 snapshot query_db_path 访问主库 clone_pairs，注入权威
        workspace_instance_id）；local/legacy 模式保留原路由语义
        （local 走本地 db 回退，enterprise/auto 走 compat worker）。

        Returns:
            {
                "total": int,
                "type1": int, "type2": int, "type3": int,
                "affected_files": int,
                "affected_symbols": int,
            }
        """
        return _route('task.clone_stats', {}, 'READ_ONLY')

    @mcp.tool()
    def clear_clones() -> dict:
        """清空当前 workspace 的所有克隆检测结果

        Returns:
            {"deleted": int} 被删除的记录数
        """
        return _route('admin.clear_clones', {}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def get_symbol_issues(qualified_name: str, include_info: bool = False) -> list:
        """查询符号相关的静态检查问题（Semgrep findings + Guardrail findings 聚合）

        整合两类静态检查数据，让 agent 查符号时一站式看到已知缺陷/告警。
        对应 CLI: cw issues <QN>

        查询路径：
        1. semgrep_findings：按 symbol_qualified 精确匹配（首选）
                         OR file_instance_id + line 范围交集（兜底）
        2. guardrail_findings：按 file_path + symbol_hash 匹配

        Args:
            qualified_name: 符号限定名
            include_info: 是否包含 INFO 级别（默认只 WARNING+，避免噪音）

        Returns:
            issues 列表，按 severity 降序（ERROR > WARNING > INFO），每条含：
            {
                "source": "semgrep" / "guardrail",
                "rule_id": str, "rule_name": str,
                "severity": str, "message": str,
                "start_line": int, "end_line": int,
                "snippet": str, "fix": str (仅 semgrep),
            }
        """
        return _route('query.issues', {"qualified_name": qualified_name, "include_info": include_info}, 'READ_ONLY')

    @mcp.tool()
    def get_test_cases(qualified_name: str) -> list:
        """查询符号的测试 case 列表

        回答 agent 高频问题："foo() 有哪些 test 在测它？"
        对应 CLI: cw tests <QN>

        Args:
            qualified_name: 被测函数的限定名

        Returns:
            测试 case 列表，按 confidence 降序（high > mid > low），每条含：
            {
                "test_fn_id": int,
                "match_method": "direct_call" / "name_convention" / "indirect",
                "confidence": "high" / "mid" / "low",
                "test_name": str, "test_qualified_name": str,
                "test_file": str, "test_start_line": int,
            }
        """
        return _route('query.tests', {"qualified_name": qualified_name}, 'READ_ONLY')

    @mcp.tool()
    def get_tested_functions(test_qualified_name: str) -> list:
        """反向查询：test 函数测了哪些被测函数

        对应 CLI: cw tests <QN> --reverse（反向查询）

        Args:
            test_qualified_name: test 函数的限定名

        Returns:
            被测函数列表，按 confidence 降序，每条含：
            {
                "tested_fn_id": int,
                "match_method": str, "confidence": str,
                "tested_name": str, "tested_qualified_name": str,
                "tested_file": str, "tested_start_line": int, "tested_end_line": int,
            }
        """
        return _route('query.tests', {"test_qualified_name": test_qualified_name}, 'READ_ONLY')

    @mcp.tool()
    def get_test_coverage_summary(qualified_name: str) -> dict:
        """查询符号的测试覆盖情况摘要

        M2.5（T-1786584287058-7f712ff4）：迁移到 daemon RPC query.tests
        （DaemonClient.get_test_coverage_summary 聚合），fail-closed 语义：
        enterprise/auto 模式下 daemon 不可用抛 DaemonUnavailableError，
        不回退本地 SQLite；local 模式返回 None。

        Args:
            qualified_name: 被测函数的限定名

        Returns:
            {
                "has_tests": bool,
                "test_count": int,
                "high_confidence_count": int,
                "tests": [...],  # 最多 10 条
            }
        """
        return _route('query.tests', {"qualified_name": qualified_name}, 'READ_ONLY')

    @mcp.tool()
    def get_test_stability(qualified_name: str, limit: int = 50) -> dict:
        """查询符号关联测试的稳定性（基于 test_runs 历史）

        查找通过 test_case_relations 关联到此符号的所有 test_fn，
        再查 test_runs 表获取它们的运行历史。
        对应 CLI: cw tests <QN> --history

        Args:
            qualified_name: 被测函数的限定名
            limit: 最多返回多少条运行记录（默认 50）

        Returns:
            {
                "total_runs": int,
                "pass_rate": float,        # 0.0-1.0
                "avg_duration_ms": float,
                "recent_failures": [...],   # 最近的失败记录
                "by_test": {                # 按 test_name 分组统计
                    "test_name": {"total": int, "passed": int, "failed": int, ...},
                }
            }
        """
        return _route('query.tests', {"qualified_name": qualified_name, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def get_defect_correlation(qualified_name: str, window_commits: int = 5) -> dict:
        """查询符号的变更-缺陷关联（defect correlation）

        分析符号的变更频率与缺陷（Semgrep findings）的时间关联性，
        回答"这个函数改得多不多？改完之后容易引入缺陷吗？"
        对应 CLI: cw evolution <QN> --defects

        Args:
            qualified_name: 符号限定名
            window_commits: 变更后观察的提交窗口数（默认 5，即变更后 5 次提交内出现的 findings 算关联）

        Returns:
            {
                "qualified_name": str,
                "change_count": int,        # 变更次数
                "defect_count": int,        # 关联缺陷数
                "defect_rate": float,       # defect_count / change_count
                "recent_defects": [...],   # 最近的关联缺陷
            }

        W4-3（T-1786886251769-22b94ee8-sub-3）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native query.get_defect_correlation，
        经 snapshot query_db_path 访问主库，注入权威 workspace_instance_id
        做 workspace 隔离）；local/legacy 模式保留原路由语义（local 走本地
        db 回退，enterprise/auto 走 compat worker）。
        """
        return _route('query.get_defect_correlation', {"qualified_name": qualified_name, "window_commits": window_commits}, 'READ_ONLY')

    @mcp.tool()
    def detect_clones_async(
        file_filter: str = "",
        min_lines: int = 5,
        similarity_threshold: float = 0.8,
    ) -> dict:
        """异步检测重复代码（后台 job，不阻塞 MCP 请求）

        把 clone detect 提交为后台 job，存 clone groups（不展开 pairs）。
        适合 20 万符号级别的代码库，避免同步执行导致 MCP 请求超时。

        Args:
            file_filter: 文件路径前缀过滤（如 "src/core/"），空字符串扫描所有
            min_lines: 最小符号行数（默认 5）
            similarity_threshold: Type-3 相似度阈值 [0,1]（默认 0.8）

        Returns:
            {
                "job_id": str,         # 任务 ID
                "status": "pending",    # 初始状态
                "job_type": "clone_detect",
                "message": "submitted",
            }
        """
        return _route('task.job_submit', {**{"file_filter": file_filter, "min_lines": min_lines, "similarity_threshold": similarity_threshold}, "job_type": "clone_detect", "sync": False}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def get_job_status(job_id: str) -> dict:
        """查询后台任务状态

        Args:
            job_id: 任务 ID（如 "J-1783698970719-3a4b5c6d"）

        Returns:
            {
                "job_id": str,
                "job_type": str,
                "status": str,         # pending/running/completed/cancelled/failed
                "progress": float,     # 0.0 ~ 1.0
                "message": str,
                "result_summary": dict,
                "error": str,
                "created_at": float,
                "started_at": float,
                "finished_at": float,
            }
        """
        return _route('task.job_status', {"job_id": job_id}, 'READ_ONLY')

    @mcp.tool()
    def cancel_job(job_id: str) -> dict:
        """请求取消后台任务

        行为：
        - pending 状态：直接标记为 cancelled
        - running 状态：设置 cancel_requested，executor 轮询后退出
        - 终态：无操作

        Args:
            job_id: 任务 ID

        Returns:
            {"cancelled": bool, "job_id": str}
        """
        return _route('task.job_cancel', {"job_id": job_id}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def list_jobs(
        job_type: str = "",
        status: str = "",
        limit: int = 100,
    ) -> list:
        """列出后台任务

        Args:
            job_type: 任务类型过滤（"" = 全部，如 "clone_detect"）
            status: 状态过滤（"" = 全部，如 "running"）
            limit: 返回上限（默认 100）

        Returns:
            任务列表，按 created_at 降序
        """
        return _route('task.list_jobs', {"job_type": job_type, "status": status, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def get_job_stats() -> dict:
        """获取任务统计信息

        W2-2（T-1786840097330-a9e0ec69）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native task.job_stats，
        经 snapshot query_db_path 访问主库 jobs，注入权威
        workspace_instance_id）；local/legacy 模式保留原路由语义
        （local 走本地 db 回退，enterprise/auto 走 compat worker）。

        Returns:
            {
                "pending": int, "running": int,
                "completed": int, "cancelled": int, "failed": int,
                "total": int,
            }
        """
        return _route('task.job_stats', {}, 'READ_ONLY')

    @mcp.tool()
    def wait_for_job(
        job_id: str,
        timeout: float = 30.0,
        poll_interval: float = 0.5,
    ) -> dict:
        """等待后台任务完成并返回 result_summary

        Phase 7.4：提交 async job 后调用此工具等待完成，获取结果摘要。
        适用于 "submit → wait → get result" 模式。

        Args:
            job_id: 任务 ID（如 "J-1783698970719-3a4b5c6d"）
            timeout: 最大等待秒数（默认 30）
            poll_interval: 轮询间隔秒数（默认 0.5）

        Returns:
            {
                "job_id": str,
                "status": str,             # completed/cancelled/failed/timeout
                "progress": float,
                "result_summary": dict,    # job 完成时的结果摘要
                "error": str,              # 失败时的错误信息
                "elapsed": float,          # 实际等待秒数
            }
        """
        return _route('task.wait_for_job', {"job_id": job_id, "timeout": timeout, "poll_interval": poll_interval}, 'READ_ONLY')

    @mcp.tool()
    def list_clone_groups(
        clone_type: int = 0,
        min_similarity: float = 0.0,
        limit: int = 100,
    ) -> list:
        """列出 clone groups（Phase 7.0 新增）

        读取 detect_clones_async 的结果。每组含 representative + member_count，
        不展开成 pairs，避免 N×N 爆炸。

        Args:
            clone_type: 0=全部，1/2/3 对应 Type-N
            min_similarity: 最低相似度过滤
            limit: 返回上限（默认 100）

        Returns:
            clone group 列表，按相似度降序
        """
        return _route('list_clone_groups', {"clone_type": clone_type, "min_similarity": min_similarity, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def get_clone_group_detail(
        group_id: int,
        members_limit: int = 100,
    ) -> dict:
        """获取 clone group 详情（含成员符号）

        Args:
            group_id: group ID
            members_limit: 成员返回上限（默认 100）

        Returns:
            {
                "group": {...},
                "members": [{"symbol_id", "name", "qualified_name",
                            "file_path", "start_line"}, ...]
            }
        """
        return _route('get_clone_group_detail', {"group_id": group_id, "members_limit": members_limit}, 'READ_ONLY')

    @mcp.tool()
    def get_clone_group_stats() -> dict:
        """获取 clone groups 统计信息

        W2-2（T-1786840097330-a9e0ec69）：HTTP 模式（默认）直连
        HttpDaemonRpcClient 便捷方法（Rust native task.clone_group_stats，
        经 snapshot query_db_path 访问主库 clone_groups，注入权威
        workspace_instance_id）；local/legacy 模式保留原路由语义
        （local 走本地 db 回退，enterprise/auto 走 compat worker）。

        Returns:
            {
                "total_groups": int, "type1": int, "type2": int, "type3": int,
                "total_members": int,
                "affected_files": int, "affected_symbols": int,
            }
        """
        return _route('task.clone_group_stats', {}, 'READ_ONLY')

    @mcp.tool()
    def embed_symbols_async(
        batch_size: int = 32,
        force: bool = False,
    ) -> dict:
        """异步嵌入向量（后台 job，不阻塞 MCP 请求）

        Phase 7.2：把 vector embedding 提交为后台 job。
        增量模式（force=False）只嵌入尚未有嵌入的符号，适合 20 万符号级别的代码库，
        避免同步执行导致 MCP 请求超时。

        Args:
            batch_size: 每批处理数量（默认 32）
            force: True 时强制重新嵌入所有符号（默认 False，增量模式）

        Returns:
            {
                "job_id": str,          # 任务 ID
                "status": "pending",     # 初始状态
                "job_type": "vector_embed",
                "message": "submitted",
            }
        """
        return _route('task.job_submit', {**{"batch_size": batch_size, "force": force}, "job_type": "embed", "sync": False}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def semgrep_scan_async(
        config: str = "p/default",
        languages: list = None,
        timeout: int = 300,
    ) -> dict:
        """异步运行 Semgrep 扫描（后台 job，不阻塞 MCP 请求）

        Phase 7.3：把 Semgrep CLI 扫描提交为后台 job。
        Semgrep 作为 bounded external process 执行（有 timeout 限制），
        适合大型代码库，避免同步执行导致 MCP 请求超时。

        Args:
            config: Semgrep 规则配置（默认 p/default，可选 p/security 等）
            languages: 限制扫描的语言列表（如 ["python", "rust"]），为空则扫描所有
            timeout: Semgrep CLI 超时秒数（默认 300）

        Returns:
            {
                "job_id": str,
                "status": "pending",
                "job_type": "semgrep_scan",
                "message": "submitted",
            }
        """
        return _route('task.job_submit', {**{"config": config, "languages": languages, "timeout": timeout}, "job_type": "semgrep_scan", "sync": False}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def rule_seed_bootstrap(dry_run: bool = True) -> dict:
        """种子化内置自举 active rules

        把内置的 5 条 bootstrap 规则写入 agent_rules（status=active），
        让规则注入不再空转。规则覆盖：
        - i18n 强制（warning）
        - 提交前刷新代码图谱（critical）
        - 大任务必须拆分（warning）
        - 任务完成必须运行 completion review（critical）
        - 外部编辑后必须运行 task capture-diff（warning）

        幂等性：通过固定 ID（AR-bootstrap-*）实现，重复 seed 不会重复创建。
        已存在且无变化 → skip；已存在但内容变化 → update；不存在 → create。

        Args:
            dry_run: True 只返回计划不写库，默认 True

        Returns:
            {
                "dry_run": bool,
                "total": int,           # 内置规则总数（5）
                "created": int,          # 新建数量
                "updated": int,          # 更新数量
                "skipped": int,          # 跳过数量
                "rules": [               # 每条规则的执行结果
                    {"id": str, "title": str, "action": "create"|"update"|"skip"}
                ],
            }
        """
        return _route('rule.seed_bootstrap', {"dry_run": dry_run}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def cleanup_agent_rule_sync_log(
        older_than_days: int = 90,
        keep_latest: int = 100,
        dry_run: bool = True,
    ) -> dict:
        """清理 agent_rule_sync_log 表中的旧记录，防止无限增长（C6 GC）

        策略（同时满足才删除）：
        1. created_at 早于 older_than_days 天前
        2. 不在最近 keep_latest 条记录内（按 created_at 倒序）

        默认 dry-run（只预估不删除），需传 dry_run=False 才真正执行 DELETE。
        fail-soft：任何异常都封装为 {"success": False, "error": ...}，不抛出。

        Args:
            older_than_days: 超过多少天的记录进入候选（默认 90）
            keep_latest: 保留最近多少条记录不删除（默认 100）
            dry_run: True 只预演不删除（默认 True），False 真正执行删除

        Returns:
            {
                "success": bool,
                "dry_run": bool,
                "deleted_count": int,      # dry_run 时为预估值，apply 时为实删数
                "remaining_count": int,
                "total_before": int,       # 清理前总记录数
                "older_than_days": int,
                "keep_latest": int,
                "error": str,              # 仅 success=False 时存在
            }
        """
        return _route('admin.cleanup_rule_sync_log', {"older_than_days": older_than_days, "keep_latest": keep_latest, "dry_run": dry_run}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def task_create_subtask(parent_task_id: str, title: str, description: str = "", steps: list = None, creator: str = "agent") -> str:
        """在父任务下创建子任务

        当任务过大时，可将其拆分为多个子任务。子任务完成后，
        系统自动推进父任务状态，避免 Agent 遗漏任务或遗忘上下文。

        Args:
            parent_task_id: 父任务 ID
            title: 子任务标题
            description: 子任务描述
            steps: 子任务步骤列表
            creator: 创建者标识

        Returns:
            新建子任务的 task_id
        """
        return _route('task.create_subtask', {"parent_task_id": parent_task_id, "title": title, "description": description, "steps": steps, "creator": creator}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def task_split(task_id: str, subtasks: list) -> list:
        """将大任务拆分为多个子任务"""
        return _route('task.split', {"task_id": task_id, "subtasks": subtasks}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def task_status_tree(task_id: str) -> Optional[dict]:
        """获取任务树详情（含生命周期/治理状态、子任务树和进度）。

        ``progress.progress`` 仍是 0..1 的历史兼容 ratio；新调用方应使用
        ``progress.ratio`` 或带单位且已四舍五入到两位的 ``progress.percent``。
        每个节点还包含 daemon 派生的 ``lifecycle_status``、``workflow_status``
        和 ``governance`` 投影；历史任务缺少 binding/合同时会明确返回
        ``governance_blocked``，不会伪造状态。
        """
        return _route('task.status_tree', {"task_id": task_id}, 'READ_ONLY')

    @mcp.tool()
    def task_create_from_plan(title: str, plan_md: str, description: str = "") -> str:
        """从 Markdown 任务计划自动创建父子任务树"""
        return _route('task.create_from_plan', {"title": title, "plan_md": plan_md, "description": description}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def task_plan_template() -> str:
        """获取 task_create_from_plan 的标准格式模板"""
        return _route('task_plan_template', {}, 'READ_ONLY')

    @mcp.tool()
    def task_list(status_filter: str = None, limit: int = 20) -> list:
        """列出任务"""
        return _route('task.list', {"status_filter": status_filter, "limit": limit}, 'READ_ONLY')

    @mcp.tool()
    def task_status(task_id: str) -> Optional[dict]:
        """获取任务详情和所有步骤"""
        return _route('task.status', {"task_id": task_id}, 'READ_ONLY')

    @mcp.tool()
    def task_governance_projection(task_id: str) -> Optional[dict]:
        """获取任务治理进度投影（生命周期、Reviewer verdict、下一角色与下一动作）"""
        return _route('task.governance_projection.get', {"task_id": task_id}, 'READ_ONLY')

    @mcp.tool()
    def task_completion_review(task_id: str, step_id: str = "") -> dict:
        """运行任务完成质量审查"""
        return _route('task.completion_review', {"task_id": task_id, "step_id": step_id}, 'PROTECTED_MUTATION')

    @mcp.tool()
    def task_quality_findings(task_id: str, status: str = "open", severity: str = "") -> list:
        """查询任务质量门禁发现"""
        return _route('task.quality_findings', {"task_id": task_id, "status": status, "severity": severity}, 'READ_ONLY')

    @mcp.tool()
    def task_resolve_quality_finding(finding_id: int, resolution: str = "fixed", resolved_by: str = "agent") -> dict:
        """解决或豁免单条任务质量门禁发现"""
        return _route('task.resolve_quality_finding', {"finding_id": finding_id, "resolution": resolution, "resolved_by": resolved_by}, 'PROTECTED_MUTATION')


# ============================================================
# H4C-3（T-1786716190783-ba187c88 步骤#1）：任务组只读工具 worker handler
# ============================================================
# 接入说明（用户三项决策，见任务描述）：
# - 仅接入**纯读**任务工具；写语义工具保持 fail-closed 不接入：
#   detect_clones（UPSERT）、detect_clones_async / semgrep_scan_async（提交 job）、
#   rule_seed_bootstrap（写 agent_rules）、cleanup_agent_rule_sync_log（DELETE）；
# - governance_write 工具（task_create/next/report/apply/close 等）维持 fail-closed，
#   不注册 worker handler（MVP 禁止 governance_write）；
# - 轻量只读绑定复用 tools_query._bind_readonly_db 同款模式（object.__new__ +
#   ctx.conn + ctx.workspace_id + workspace_root），由本模块独立实现避免跨模块
#   耦合；
# - bootstrap_status 依赖 workspace_root（_is_git_repo 检查 .git 目录），
#   _bind_readonly_db 已从 workspaces 表解析注入。

_TASK_COMPAT_SCOPE = SCOPE_WORKSPACE  # 矩阵 workspace_scoped


def _bind_readonly_db(ctx: CompatCallContext) -> CodeGraphDB:
    """轻量只读绑定（任务组副本）：绕过 CodeGraphDB.__init__，注入 worker 只读连接。

    与 tools_query._bind_readonly_db 同款实现；两模块各自持有副本避免循环依赖。
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


def _h_get_symbol_change_tasks(ctx: CompatCallContext) -> Any:
    """worker handler：符号由哪些任务改变过（只读）"""
    return _bind_readonly_db(ctx).get_symbol_change_tasks(
        symbol_hash=ctx.params.get("symbol_hash", ""),
        qualified_name=ctx.params.get("qualified_name", ""),
        limit=ctx.params.get("limit", 50),
    )


def _h_audit_verify_chain(ctx: CompatCallContext) -> Any:
    """worker handler：审计签名链验证（只读）"""
    db = _bind_readonly_db(ctx)
    try:
        return db.verify_audit_chain(
            table_name=ctx.params.get("table_name", ""),
            limit=ctx.params.get("limit", 1000),
        )
    except Exception as e:
        return {"error": str(e)}


def _h_list_audit_signing_keys(ctx: CompatCallContext) -> Any:
    """worker handler：列出签名密钥轮换记录（只读）"""
    db = _bind_readonly_db(ctx)
    try:
        return db.list_signing_keys()
    except Exception as e:
        return [{"error": str(e)}]


def _h_bootstrap_status(ctx: CompatCallContext) -> Any:
    """worker handler：自举健康状态摘要（只读）"""
    return _bind_readonly_db(ctx).bootstrap_status()


def _h_list_clones(ctx: CompatCallContext) -> Any:
    """worker handler：列出克隆对（只读）"""
    db = _bind_readonly_db(ctx)
    try:
        return db.list_clones(
            clone_type=ctx.params.get("clone_type", 0),
            min_similarity=ctx.params.get("min_similarity", 0.0),
            limit=ctx.params.get("limit", 100),
            symbol_id=ctx.params.get("symbol_id", 0),
        )
    except Exception as e:
        return [{"error": str(e)}]


def _h_list_clone_groups(ctx: CompatCallContext) -> Any:
    """worker handler：列出 clone groups（只读）"""
    db = _bind_readonly_db(ctx)
    try:
        groups = db.list_clone_groups(
            clone_type=ctx.params.get("clone_type", 0),
            min_similarity=ctx.params.get("min_similarity", 0.0),
            limit=ctx.params.get("limit", 100),
        )
        return [g.to_dict() for g in groups]
    except Exception as e:
        return [{"error": str(e)}]


def _h_get_clone_group_detail(ctx: CompatCallContext) -> Any:
    """worker handler：clone group 详情（只读）"""
    db = _bind_readonly_db(ctx)
    try:
        detail = db.get_clone_group_detail(
            ctx.params.get("group_id", 0),
            ctx.params.get("members_limit", 100),
        )
        if not detail:
            return {"error": f"group not found: {ctx.params.get('group_id', 0)}"}
        return {
            "group": detail.group.to_dict(),
            "members": detail.members,
        }
    except Exception as e:
        return {"error": str(e)}


def _h_task_plan_template(ctx: CompatCallContext) -> Any:
    """worker handler：任务计划模板（只读，返回模板字符串）"""
    return _bind_readonly_db(ctx).task_plan_template()


# 任务组只读白名单（8 个）：跳过写语义 5 个（detect_clones / detect_clones_async /
# semgrep_scan_async / rule_seed_bootstrap / cleanup_agent_rule_sync_log）与
# governance_write 工具（task_create/next/report/apply/close 等），均保持 fail-closed；
# get_clone_stats / get_job_stats / get_clone_group_stats 3 个已 W2-2 迁移 rust_native
# （T-1786840097330-a9e0ec69），从本白名单移除（16->13）；
# get_job_status / list_jobs / wait_for_job 3 个已 W3-2 迁移 rust_native
# （T-1786861820151-f3cecf40），从本白名单移除（13->10）；
# get_commit_tasks 已 W4-1 迁移 rust_native（T-1786886251769-22b94ee8-sub-1），
# 从本白名单移除（10->9）；
# get_defect_correlation 已 W4-3 迁移 rust_native（T-1786886251769-22b94ee8-sub-3），
# 从本白名单移除（9->8）。
_TASK_READ_ONLY_METHODS: Dict[str, Any] = {
    "get_symbol_change_tasks": _h_get_symbol_change_tasks,
    "audit_verify_chain": _h_audit_verify_chain,
    "list_audit_signing_keys": _h_list_audit_signing_keys,
    "bootstrap_status": _h_bootstrap_status,
    "list_clones": _h_list_clones,
    "list_clone_groups": _h_list_clone_groups,
    "get_clone_group_detail": _h_get_clone_group_detail,
    "task_plan_template": _h_task_plan_template,
}

# 模块级注册：worker 装配 import 本模块时执行（compat_worker.py L44-45），
# 注册到 compat_registry 单例并同步 RUST_COMPAT_ROUTE（Rust 侧步骤#2 同步）。
register_compat_routes(
    _TASK_READ_ONLY_METHODS,
    workspace_scope=_TASK_COMPAT_SCOPE,
    description="H4C-3 任务组只读工具（8 个，T-1786716190783-ba187c88 步骤#1；"
    "W3-2 T-1786861820151-f3cecf40 迁移 get_job_status/list_jobs/wait_for_job 后 13->10；"
    "W4-1 T-1786886251769-22b94ee8-sub-1 迁移 get_commit_tasks 后 10->9；"
    "W4-3 T-1786886251769-22b94ee8-sub-3 迁移 get_defect_correlation 后 9->8）",
)
