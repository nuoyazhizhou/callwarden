"""任务驱动编排（task_create/next/report/rollback/close 等，原 [L5]）

拆分自 server/mcp_server.py（1591-3050 行区间），由 register(mcp) 注册。
"""

# [L5] 任务驱动编排工具（task_create / task_next_step / work_next_job 等）

from typing import Any, Dict, Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import _get_daemon_client, _get_db_path_for_daemon, get_db


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
        db = get_db()
        return db.task_create(title=title, description=description, steps=steps, creator=creator)

    @mcp.tool()
    def task_next_step(task_id: str) -> Optional[dict]:
        """领取任务的下一个待执行步骤

        Agent 必须通过此工具领取步骤，不能自由决定下一步操作。
        返回步骤详情（文件、操作、检查项），Agent 只能执行这一步。

        Before-Edit Contract：当步骤为编辑类操作时，系统自动调用护栏检查。
        - 若返回 guardrail_alert（decision=block）：步骤状态为 blocked，
          Agent 必须先处理告警，再调用 task_resolve_block 恢复步骤。
        - 若返回 guardrail_warning（decision=warn）：步骤可执行，但需关注告警。
        - 否则正常执行。

        Args:
            task_id: 任务 ID

        Returns:
            步骤详情，如果没有待执行步骤则返回 None
        """
        db = get_db()
        return db.task_next_step(task_id=task_id)

    @mcp.tool()
    def work_next_job(task_id: str) -> Optional[dict]:
        """领取下一项 Agent 工作，并返回完成它所需的最小上下文

        这是 Agent 优先入口：相比手动 read/grep/plan，本工具返回目标、
        符号源码、调用上下文、文件健康、允许编辑范围、推荐 patch 工具
        和完成后汇报方式。
        """
        db = get_db()
        return db.work_next_job(task_id=task_id)

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
        db = get_db()
        return db.task_resolve_block(task_id=task_id, step_id=step_id, resolution=resolution)

    @mcp.tool()
    def task_report_step(task_id: str, step_id: str, result: str = "", success: bool = True, changes: list = None) -> Optional[dict]:
        """回报步骤执行结果

        如果失败，系统会自动插入"修复缺陷"步骤，Agent 无法跳过。
        如果成功且无更多步骤，任务状态变为 review。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
            result: 执行结果描述
            success: 是否成功
            changes: 变更记录列表
            identity: P3 结构化身份 JSON（{agent_id, session_id, model_id, role}，
                      可选；提供后由包装层校验并透传给 db 层，不得伪造缺省身份）

        Returns:
            下一步步骤信息（如果有）
        """
        db = get_db()
        try:
            identity_dict, id_reason = _resolve_identity_arg(db, identity)
            if id_reason:
                return _identity_mcp_reason(
                    id_reason.get("code", "E_IDENTITY_INVALID"),
                    id_reason.get("message_key", "error.identity_incomplete"),
                    id_reason.get("detail", "身份校验失败"),
                )
            kwargs: Dict[str, Any] = {
                "task_id": task_id, "step_id": step_id,
                "result": result, "success": success, "changes": changes,
            }
            if identity_dict:
                if not _db_method_accepts_identity("task_report_step"):
                    return _identity_mcp_reason(
                        "E_IDENTITY_NOT_WIRED",
                        "error.identity_not_wired",
                        "task_report_step 尚不支持 identity 参数（8.6 接线后可用）",
                    )
                kwargs["identity"] = identity_dict
            return db.task_report_step(**kwargs)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def record_task_symbol_change(task_id: str, file_path: str, step_id: str = "",
                                  edit_audit_id: int = 0, change_audit_id: str = "",
                                  qualified_name: str = "", symbol_name: str = "",
                                  symbol_hash_before: str = "", symbol_hash_after: str = "",
                                  change_type: str = "modified", source: str = "manual",
                                  metadata: dict = None) -> dict:
        """记录任务/步骤到文件或符号版本变化的归因"""
        try:
            db = get_db()
            return db.record_task_symbol_change(
                task_id=task_id,
                file_path=file_path,
                step_id=step_id,
                edit_audit_id=edit_audit_id,
                change_audit_id=change_audit_id,
                qualified_name=qualified_name,
                symbol_name=symbol_name,
                symbol_hash_before=symbol_hash_before,
                symbol_hash_after=symbol_hash_after,
                change_type=change_type,
                source=source,
                metadata=metadata or {},
            )
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def link_edit_audit_symbols(audit_id: int, step_id: str = "") -> dict:
        """刷新图谱后，将某次 edit_audit 的 before/after 文件版本映射到符号变化"""
        try:
            db = get_db()
            return db.link_edit_audit_symbols(audit_id=audit_id, step_id=step_id)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_task_symbol_changes(task_id: str, step_id: str = "", file_path: str = "", limit: int = 100) -> list:
        """查询任务或步骤归因到的文件/符号变化"""
        try:
            db = get_db()
            return db.get_task_symbol_changes(task_id=task_id, step_id=step_id, file_path=file_path, limit=limit)
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def get_symbol_change_tasks(symbol_hash: str = "", qualified_name: str = "", limit: int = 50) -> list:
        """反查某个符号版本或符号名由哪些任务改变过"""
        try:
            db = get_db()
            return db.get_symbol_change_tasks(symbol_hash=symbol_hash, qualified_name=qualified_name, limit=limit)
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def get_task_commits(task_id: str, include_commit_details: bool = True) -> list:
        """查询任务关联的所有 commit（task → commit 正向查询，三角关联）

        通过 task_symbol_changes.source_commit_hash JOIN git_commits 拿 commit 详情。

        Args:
            task_id: 任务 ID
            include_commit_details: 是否 JOIN git_commits 返回 commit 详情（author/message/timestamp）

        Returns:
            按 source_commit_hash 去重的列表，每条含：
            source_commit_hash / change_count / first_change_at / last_change_at，
            include_commit_details=True 时额外返回 commit_author / commit_message /
            commit_timestamp / commit_subject。
        """
        try:
            db = get_db()
            if not hasattr(db, "get_task_commits"):
                return [{"error": "get_task_commits not available (need schema v35+)"}]
            return db.get_task_commits(task_id=task_id, include_commit_details=include_commit_details)
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def get_commit_tasks(commit_hash: str, include_task_details: bool = True) -> list:
        """查询 commit 关联的所有 task（commit → task 反向查询，三角关联）

        通过 task_symbol_changes.source_commit_hash 反查关联的任务。

        Args:
            commit_hash: Git commit hash
            include_task_details: 是否 JOIN tasks 表返回 task 详情（title/status/parent_id）

        Returns:
            按 task_id 去重的列表，每条含：
            task_id / change_count / first_change_at / last_change_at，
            include_task_details=True 时额外返回 task_title / task_status / task_parent_id。
        """
        try:
            db = get_db()
            if not hasattr(db, "get_commit_tasks"):
                return [{"error": "get_commit_tasks not available (need schema v35+)"}]
            return db.get_commit_tasks(commit_hash=commit_hash, include_task_details=include_task_details)
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def task_rollback(task_id: str, change_id: str = None, reason: str = "") -> dict:
        """回滚任务中的变更

        Args:
            task_id: 任务 ID
            change_id: 变更 ID（可选，不指定则回滚最后一个变更）
            reason: 回滚原因

        Returns:
            回滚结果
        """
        db = get_db()
        return db.task_rollback(task_id=task_id, change_id=change_id, reason=reason)

    @mcp.tool()
    def task_apply(task_id: str, reviewer: str = "reviewer",
                   identity: str = "") -> dict:
        """审核通过：将任务状态从 review 改为 applied

        设计原则：写代码的 Agent 不能自己 applied，必须由其他会话的
        LLM 审核通过后调用此工具。只有 status=review 的任务才能 apply。

        Args:
            task_id: 任务 ID
            reviewer: 审核人标识
            identity: P3 结构化身份 JSON（{agent_id, session_id, model_id, role}，
                      可选；自由文本 reviewer 不是身份证明，Req 10.5）

        Returns:
            包含 task_id、status、applied_at 的字典；失败时包含 error 字段
        """
        db = get_db()
        try:
            identity_dict, id_reason = _resolve_identity_arg(db, identity)
            if id_reason:
                return _identity_mcp_reason(
                    id_reason.get("code", "E_IDENTITY_INVALID"),
                    id_reason.get("message_key", "error.identity_incomplete"),
                    id_reason.get("detail", "身份校验失败"),
                )
            kwargs: Dict[str, Any] = {"task_id": task_id, "reviewer": reviewer}
            if identity_dict:
                if not _db_method_accepts_identity("task_apply"):
                    return _identity_mcp_reason(
                        "E_IDENTITY_NOT_WIRED",
                        "error.identity_not_wired",
                        "task_apply 尚不支持 identity 参数（8.6 接线后可用）",
                    )
                kwargs["identity"] = identity_dict
            return db.task_apply(**kwargs)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def task_close(task_id: str, reviewer: str = "reviewer",
                   identity: str = "") -> dict:
        """关闭任务：将任务状态从 applied 改为 closed

        设计原则：写代码的 Agent 不能自己 closed，必须由其他会话的
        LLM 审核关闭后调用此工具。只有 status=applied 的任务才能 close。

        Args:
            task_id: 任务 ID
            reviewer: 审核人标识
            identity: P3 结构化身份 JSON（可选；不得伪造缺省身份）

        Returns:
            包含 task_id、status、closed_at 的字典；失败时包含 error 字段
        """
        db = get_db()
        try:
            identity_dict, id_reason = _resolve_identity_arg(db, identity)
            if id_reason:
                return _identity_mcp_reason(
                    id_reason.get("code", "E_IDENTITY_INVALID"),
                    id_reason.get("message_key", "error.identity_incomplete"),
                    id_reason.get("detail", "身份校验失败"),
                )
            kwargs: Dict[str, Any] = {"task_id": task_id, "reviewer": reviewer}
            if identity_dict:
                if not _db_method_accepts_identity("task_close"):
                    return _identity_mcp_reason(
                        "E_IDENTITY_NOT_WIRED",
                        "error.identity_not_wired",
                        "task_close 尚不支持 identity 参数（8.6 接线后可用）",
                    )
                kwargs["identity"] = identity_dict
            return db.task_close(**kwargs)
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def task_capture_diff(
        task_id: str,
        step_id: str = "",
        base: str = "",
        dry_run: bool = True,
        source_commit_hash: str = "",
        skip_quality_review: bool = False,
    ) -> dict:
        """捕获外部 Agent 真实文件改动到 task/change/symbol/audit 闭环

        Args:
            source_commit_hash: 引入此次变更的 git commit hash（可选）。
                填写后会写入 task_symbol_changes.source_commit_hash 字段，
                支持后续通过 get_task_commits / get_commit_tasks 查询三角关联。
                post-commit hook 自动调用时取当前 HEAD commit hash。
            skip_quality_review: True 时跳过 run_task_completion_review
                （Semgrep + 5 个扩展检查器），用于快速捕获场景。
                post-commit hook 自动模式默认 True；显式调用建议保持 False。

        用于把外部 Agent（非 Call Warden MCP）在文件系统中留下的真实改动
        归因到指定 task/step，并触发质量审查。这是自举闭环的核心入口。

        流程：
        1. 调用 get_workspace_changes_since 检测变更文件
        2. dry-run=True：只返回计划不写库
        3. dry-run=False（apply 模式）：
           - 写 workspace_scan_runs（status=running -> completed）
           - 每个变更文件写 change_audit（含 hash_before/hash_after）
           - 签名审计记录 sign_audit_record（best-effort，失败不阻塞）
           - 关联 task_symbol_changes（best-effort，失败不阻塞）
           - 调用 run_task_completion_review 收集 quality findings（skip_quality_review=True 时跳过）
           - 根据 quality_decision 决定 next_action

        Args:
            task_id: 关联任务 ID
            step_id: 关联步骤 ID（可选）
            base: 基线 commit（空串自动取最近一次 scan baseline 的 git_head）
            dry_run: True 只返回计划不写库，默认 True

        Returns:
            {
                "task_id": str,
                "step_id": str,
                "dry_run": bool,
                "scan_id": int,        # apply 模式才有
                "changed_files": [...],
                "linked_symbols": [...],
                "quality_findings": [...],
                "quality_decision": "pass" | "warn" | "block" | "",
                "next_action": "review" | "fix" | "commit" | "noop" | "",
            }
        """
        db = get_db()
        try:
            return db.task_capture_diff(
                task_id=task_id,
                step_id=step_id,
                base=base,
                dry_run=dry_run,
                source_commit_hash=source_commit_hash,
                skip_quality_review=skip_quality_review,
            )
        except Exception as e:
            return {"error": str(e)}

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
        db = get_db()
        try:
            return db.verify_audit_chain(table_name=table_name, limit=limit)
        except Exception as e:
            return {"error": str(e)}

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
        db = get_db()
        try:
            # key_secret 为空时自动生成 32 字节随机密钥（hex 编码，64 字符）
            if not key_secret:
                import secrets as _secrets
                key_secret = _secrets.token_hex(32)
            return db.rotate_signing_key(
                new_key_id=key_id,
                new_key_secret=key_secret,
            )
        except Exception as e:
            return {"success": False, "error": str(e)}

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
        db = get_db()
        try:
            return db.list_signing_keys()
        except Exception as e:
            return [{"error": str(e)}]

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
        db = get_db()
        try:
            return db.bootstrap_status()
        except Exception as e:
            return {"error": str(e)}

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
        db = get_db()
        try:
            return db.detect_clones(
                file_filter=file_filter,
                min_lines=min_lines,
                similarity_threshold=similarity_threshold,
            )
        except Exception as e:
            return {"error": str(e)}

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
        db = get_db()
        try:
            return db.list_clones(
                clone_type=clone_type,
                min_similarity=min_similarity,
                limit=limit,
                symbol_id=symbol_id,
            )
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def get_clone_stats() -> dict:
        """获取克隆检测统计信息

        Returns:
            {
                "total": int,
                "type1": int, "type2": int, "type3": int,
                "affected_files": int,
                "affected_symbols": int,
            }
        """
        db = get_db()
        try:
            return db.get_clone_stats()
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def clear_clones() -> dict:
        """清空当前 workspace 的所有克隆检测结果

        Returns:
            {"deleted": int} 被删除的记录数
        """
        db = get_db()
        try:
            deleted = db.clear_clones()
            return {"deleted": deleted}
        except Exception as e:
            return {"error": str(e)}

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
        client = _get_daemon_client()
        try:
            return client.get_symbol_issues(
                qualified_name,
                include_info=include_info,
                db_path=_get_db_path_for_daemon(),
            )
        except Exception as e:
            return [{"error": str(e)}]

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
        client = _get_daemon_client()
        try:
            return client.get_test_cases(
                qualified_name, db_path=_get_db_path_for_daemon()
            )
        except Exception as e:
            return [{"error": str(e)}]

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
        client = _get_daemon_client()
        try:
            return client.get_tested_functions(
                test_qualified_name, db_path=_get_db_path_for_daemon()
            )
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def get_test_coverage_summary(qualified_name: str) -> dict:
        """查询符号的测试覆盖情况摘要

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
        db = get_db()
        try:
            return db.get_test_coverage_summary(qualified_name)
        except Exception as e:
            return {"error": str(e)}

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
        client = _get_daemon_client()
        try:
            return client.get_test_stability(
                qualified_name,
                limit=limit,
                db_path=_get_db_path_for_daemon(),
            )
        except Exception as e:
            return {"error": str(e)}

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
        """
        db = get_db()
        try:
            return db.get_defect_correlation_by_qn(qualified_name, window_commits=window_commits)
        except Exception as e:
            return {"error": str(e)}

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
        db = get_db()
        try:
            from callwarden.server.job_executor_singleton import get_job_executor
            executor = get_job_executor(db.db_path, db.workspace_root)
            params = {
                "file_filter": file_filter,
                "min_lines": min_lines,
                "similarity_threshold": similarity_threshold,
            }
            ws_id = db._get_active_workspace_id()
            job = executor.submit("clone_detect", params, workspace_id=ws_id)
            return {
                "job_id": job.job_id,
                "status": job.status,
                "job_type": job.job_type,
                "message": "submitted",
            }
        except Exception as e:
            return {"error": str(e)}

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
        db = get_db()
        try:
            job = db.get_job(job_id)
            if not job:
                return {"error": f"job not found: {job_id}"}
            return job.to_dict()
        except Exception as e:
            return {"error": str(e)}

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
        db = get_db()
        try:
            ok = db.cancel_job(job_id)
            return {"cancelled": ok, "job_id": job_id}
        except Exception as e:
            return {"error": str(e)}

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
        db = get_db()
        try:
            jobs = db.list_jobs(
                job_type=job_type or None,
                status=status or None,
                limit=limit,
            )
            return [j.to_dict() for j in jobs]
        except Exception as e:
            return [{"error": str(e)}]

    @mcp.tool()
    def get_job_stats() -> dict:
        """获取任务统计信息

        Returns:
            {
                "pending": int, "running": int,
                "completed": int, "cancelled": int, "failed": int,
                "total": int,
            }
        """
        db = get_db()
        try:
            return db.get_job_stats()
        except Exception as e:
            return {"error": str(e)}

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
        import time as _time

        db = get_db()
        start = _time.time()
        try:
            deadline = start + timeout
            while _time.time() < deadline:
                job = db.get_job(job_id)
                if not job:
                    return {"error": f"job not found: {job_id}"}
                if job.is_terminal:
                    return {
                        "job_id": job_id,
                        "status": job.status,
                        "progress": job.progress,
                        "result_summary": job.result_summary,
                        "error": job.error,
                        "elapsed": _time.time() - start,
                    }
                _time.sleep(poll_interval)
            # 超时
            job = db.get_job(job_id)
            return {
                "job_id": job_id,
                "status": "timeout" if not job.is_terminal else job.status,
                "progress": job.progress if job else 0.0,
                "result_summary": job.result_summary if job else {},
                "error": f"timeout after {timeout}s",
                "elapsed": _time.time() - start,
            }
        except Exception as e:
            return {"error": str(e)}

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
        db = get_db()
        try:
            groups = db.list_clone_groups(
                clone_type=clone_type,
                min_similarity=min_similarity,
                limit=limit,
            )
            return [g.to_dict() for g in groups]
        except Exception as e:
            return [{"error": str(e)}]

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
        db = get_db()
        try:
            detail = db.get_clone_group_detail(group_id, members_limit)
            if not detail:
                return {"error": f"group not found: {group_id}"}
            return {
                "group": detail.group.to_dict(),
                "members": detail.members,
            }
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def get_clone_group_stats() -> dict:
        """获取 clone groups 统计信息

        Returns:
            {
                "total_groups": int, "type1": int, "type2": int, "type3": int,
                "total_members": int,
                "affected_files": int, "affected_symbols": int,
            }
        """
        db = get_db()
        try:
            return db.get_clone_group_stats()
        except Exception as e:
            return {"error": str(e)}

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
        db = get_db()
        try:
            from callwarden.server.job_executor_singleton import get_job_executor
            executor = get_job_executor(db.db_path, db.workspace_root)
            params = {"batch_size": batch_size, "force": force}
            ws_id = db._get_active_workspace_id()
            job = executor.submit("vector_embed", params, workspace_id=ws_id)
            return {
                "job_id": job.job_id,
                "status": job.status,
                "job_type": job.job_type,
                "message": "submitted",
            }
        except Exception as e:
            return {"error": str(e)}

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
        db = get_db()
        try:
            from callwarden.server.job_executor_singleton import get_job_executor
            executor = get_job_executor(db.db_path, db.workspace_root)
            params = {"config": config, "languages": languages, "timeout": timeout}
            ws_id = db._get_active_workspace_id()
            job = executor.submit("semgrep_scan", params, workspace_id=ws_id)
            return {
                "job_id": job.job_id,
                "status": job.status,
                "job_type": job.job_type,
                "message": "submitted",
            }
        except Exception as e:
            return {"error": str(e)}

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
        db = get_db()
        try:
            return db.rule_seed_bootstrap(dry_run=dry_run)
        except Exception as e:
            return {"error": str(e)}

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
        db = get_db()
        try:
            return db.cleanup_sync_log(
                older_than_days=older_than_days,
                keep_latest=keep_latest,
                dry_run=dry_run,
            )
        except Exception as e:
            return {"error": str(e)}

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
        db = get_db()
        return db.task_create_subtask(
            parent_task_id=parent_task_id,
            title=title,
            description=description,
            steps=steps,
            creator=creator,
        )

    @mcp.tool()
    def task_split(task_id: str, subtasks: list) -> list:
        """将大任务拆分为多个子任务

        当任务步骤过多或单个步骤描述过长时，使用此工具自动拆分。
        原任务的自身步骤保留为汇总/验证步骤，具体工作由子任务完成。
        task_next_step 会自动深度优先下钻到最底层子任务执行，
        确保 Agent 永远聚焦在具体可执行的小任务上，不会遗漏。

        Args:
            task_id: 要拆分的父任务 ID
            subtasks: 子任务定义列表，每个元素含 title/description/steps

        Returns:
            新建子任务的 ID 列表
        """
        db = get_db()
        return db.task_split(task_id=task_id, subtasks=subtasks)

    @mcp.tool()
    def task_status_tree(task_id: str) -> Optional[dict]:
        """获取任务树详情（含子任务树和进度）

        返回完整的任务树结构，包括每层的进度百分比、
        子任务列表、自身步骤状态。用于 Agent 了解整体进展，
        避免因子任务过多而迷失方向。

        Args:
            task_id: 根任务 ID

        Returns:
            任务树 dict（含 progress、steps、subtasks 递归结构）
        """
        db = get_db()
        return db.task_status_tree(task_id=task_id)

    @mcp.tool()
    def task_create_from_plan(title: str, plan_md: str, description: str = "") -> str:
        """从 Markdown 任务计划自动创建父子任务树

        Agent 只需传入任务标题和 Markdown 格式的计划，系统会自动：
        - 解析 # / ## / ### 标题层级为任务层级
        - 解析 - [ ] 列表项为任务步骤
        - 自动生成完整的父子任务树并入库
        - task_next_step 会自动深度优先下钻执行

        推荐格式：
        ```
        # 一级标题 = 根任务说明
        ## 子任务1标题
        - 步骤1描述
        - 步骤2描述
        ## 子任务2标题
        - 步骤1描述
        ```

        Args:
            title: 根任务标题
            plan_md: Markdown 格式的任务计划
            description: 根任务补充描述（可选）

        Returns:
            根任务 ID
        """
        db = get_db()
        return db.task_create_from_plan(
            title=title,
            plan_md=plan_md,
            description=description,
        )

    @mcp.tool()
    def task_plan_template() -> str:
        """获取 task_create_from_plan 的标准格式模板

        Agent 在调用 task_create_from_plan 之前，先获取此模板，
        按模板格式填写任务计划，确保解析器正确识别。

        Returns:
            Markdown 格式的模板字符串（含格式说明）
        """
        db = get_db()
        return db.task_plan_template()

    @mcp.tool()
    def task_list(status_filter: str = None, limit: int = 20) -> list:
        """列出任务

        Args:
            status_filter: 状态过滤（open/in_progress/review/applied/closed/reverted）
            limit: 返回数量限制

        Returns:
            任务列表
        """
        db = get_db()
        return db.task_list(status_filter=status_filter, limit=limit)

    @mcp.tool()
    def task_status(task_id: str) -> Optional[dict]:
        """获取任务详情和所有步骤

        Args:
            task_id: 任务 ID

        Returns:
            任务详情和步骤列表
        """
        db = get_db()
        return db.task_status(task_id=task_id)

    @mcp.tool()
    def task_completion_review(task_id: str, step_id: str = "") -> dict:
        """运行任务完成质量审查

        触发任务质量门禁：自动清理该 step 旧的 check_gate 发现，
        调用 run_check_gate（语法/Semgrep），并运行 5 个扩展检查器：
        scope/symbol_attribution/file_health/i18n_hardcoded/signature_mismatch。

        根据 open 状态的发现严重度给出决策：
        - pass: 无发现
        - warn: 仅有 info/warn（允许完成但记录）
        - block: 存在 error/block（阻塞完成，需修复后重审）

        Agent 在 task_report_step 之前或之后均可调用此工具主动复查。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID（可选，任务级审查留空）

        Returns:
            {decision, findings, summary, counts, check_gate_result}
            decision ∈ {"pass", "warn", "block"}
        """
        db = get_db()
        return db.run_task_completion_review(task_id=task_id, step_id=step_id)

    @mcp.tool()
    def task_quality_findings(task_id: str, status: str = "open", severity: str = "") -> list:
        """查询任务质量门禁发现

        返回 task_quality_findings 表中匹配过滤条件的记录，
        按 created_at 升序（旧的先处理）。

        Args:
            task_id: 任务 ID
            status: 状态过滤（open/resolved/wontfix/all），默认 open
            severity: 严重度过滤（info/warn/error/block），默认不过滤

        Returns:
            finding 列表，每项含 id/task_id/step_id/finding_type/severity/
            status/message/evidence/source/created_at/resolved_at/resolved_by
        """
        db = get_db()
        return db.get_task_quality_findings(
            task_id=task_id, status=status, severity=severity
        )

    @mcp.tool()
    def task_resolve_quality_finding(
        finding_id: int,
        resolution: str = "fixed",
        resolved_by: str = "agent",
    ) -> dict:
        """解决或豁免单条任务质量门禁发现

        将 finding 状态从 open 推进到 resolved 或 wontfix，
        记录解决者和解决时间。error/block 级别的发现被解决后，
        该 step 的阻塞状态才会解除（task_completion_review 会重新评估）。

        Args:
            finding_id: finding ID
            resolution: 解决方式
                - fixed: 已修复
                - wontfix: 暂不修复（接受风险）
                - false_positive: 误报
            resolved_by: 解决者标识（agent/human/system）

        Returns:
            {success, finding_id, status, resolution, resolved_at}
            失败时返回 {success: False, error: ...}
        """
        db = get_db()
        return db.resolve_task_quality_finding(
            finding_id=finding_id,
            resolution=resolution,
            resolved_by=resolved_by,
        )
