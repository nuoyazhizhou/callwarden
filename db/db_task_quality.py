"""
db_task_quality.py
==================

任务质量门禁（Task Quality Gate）Mixin。

承载任务完成门禁发现，区别于通用 guardrail_findings：
把 Semgrep、复杂度、调用链一致性、scope violation、i18n 硬编码等
质量问题挂到 task/step 上，使 open error/block finding 阻止任务进入 done。

事实层由 task_quality_findings 表（v21 schema）表达；
本模块只提供记录/查询/解决/阻断判断/修复步骤插入五个核心方法，
以及 run_task_completion_review 调度器（聚合 run_check_gate + scope/symbol/
file_health/i18n/signature_mismatch 检查器）。

状态规则（与 plan 文档一致）：
- severity: info / warn / error / block
- status:   open / resolved / wontfix
- 严重度 info/warn：记录但不阻塞，task_status 显示 warn
- 严重度 error/block：step blocked，自动插入 fix_quality_gate_failure 步骤
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional

from ..i18n import t


# 阻塞性严重度（open 状态时阻止 step 进入 done）
BLOCKING_SEVERITIES = frozenset({"error", "block"})

# i18n 硬编码输出扫描的正则模式
# 匹配 print( / cprint( / logger.info( / logging.warning( 等硬编码输出调用
# 这些输出应通过 i18n t() 函数本地化，而非直接硬编码
_I18N_HARDCODED_PATTERNS = [
    re.compile(r"^\s*print\s*\("),
    re.compile(r"^\s*cprint\s*\("),
    re.compile(r"^\s*logger\s*\.\s*(info|warning|error|debug|critical)\s*\("),
    re.compile(r"^\s*logging\s*\.\s*(info|warning|error|debug|critical)\s*\("),
]

# 文件大小阈值（与 db_metrics.check_file_health 保持一致）
_FILE_SIZE_WARN_THRESHOLD = 1000
_FILE_SIZE_ERROR_THRESHOLD = 2000
_COMPLEXITY_HOTSPOT_THRESHOLD = 20

# 检查器适用语言映射表
# None 表示适用于所有语言（跨语言或由 parser 自动适配）
# 集合表示仅适用于集合中的语言
# 用于 run_task_completion_review 按 changed_files 推断的语言集合过滤调度
_CHECKER_LANGUAGES = {
    "scope": None,              # 路径比较，与语言无关
    "symbol_attribution": None,  # DB 查询 task_symbol_changes，与语言无关
    "file_health": None,        # 通过 tree-sitter check_file_health，parser 自动适配语言
    "i18n": frozenset({"python"}),  # 仅 Python：匹配 print/cprint/logger/logging
    "signature_mismatch": None,  # DB 查询 symbol_contents/calls，与语言无关
}


class TaskQualityMixin:
    """任务质量门禁 Mixin

    提供任务完成门禁的记录、查询、解决、阻断判断、修复步骤插入能力。
    与 TaskMixin（任务状态机）协作：task_report_step 在决定 step 是否 done 前
    应调用 task_has_blocking_findings 判断是否存在阻塞 finding。
    """

    def record_task_quality_finding(
        self,
        task_id: str,
        step_id: str = "",
        finding_type: str = "",
        severity: str = "warn",
        message: str = "",
        evidence: Any = None,
        source: str = "",
    ) -> int:
        """写入一条 task_quality_findings 记录

        Args:
            task_id: 关联的任务 ID（必填）
            step_id: 关联的步骤 ID（可选，任务级 finding 留空）
            finding_type: 发现类型（semgrep / file_health / call_chain / scope / i18n / manual）
            severity: 严重度（info / warn / error / block），默认 warn
            message: 发现描述（必填）
            evidence: 证据数据（dict / list / str），自动 JSON 序列化
            source: 来源标识（semgrep / file_health / call_chain / scope / i18n / manual）

        Returns:
            finding_id（成功时 >0；失败时返回 0）
        """
        if not task_id:
            return 0
        if not message:
            return 0

        # 标准化严重度
        if severity not in ("info", "warn", "error", "block"):
            severity = "warn"

        # 序列化 evidence
        if evidence is None:
            evidence_str = ""
        elif isinstance(evidence, str):
            evidence_str = evidence
        else:
            try:
                evidence_str = json.dumps(evidence, ensure_ascii=False, sort_keys=True)
            except (TypeError, ValueError):
                evidence_str = str(evidence)

        ws_id = self._get_active_workspace_id() if hasattr(self, "_get_active_workspace_id") else None
        now = time.time()

        try:
            cur = self.conn.execute(
                """
                INSERT INTO task_quality_findings
                    (workspace_id, task_id, step_id, finding_type, severity, status,
                     message, evidence, source, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (ws_id, task_id, step_id, finding_type, severity, "open",
                 message, evidence_str, source, now),
            )
            finding_id = cur.lastrowid
            self.conn.commit()
            # 写入审计签名链（失败不阻塞主流程）
            if hasattr(self, "sign_audit_record"):
                try:
                    self.sign_audit_record(
                        "task_quality_findings",
                        str(finding_id),
                        {
                            "task_id": task_id,
                            "step_id": step_id,
                            "finding_type": finding_type,
                            "severity": severity,
                            "message": message,
                            "evidence": evidence_str,
                            "source": source,
                        },
                    )
                except Exception:
                    pass
            return finding_id
        except Exception:
            return 0

    def get_task_quality_findings(
        self,
        task_id: str,
        status: str = "open",
        severity: str = "",
    ) -> List[Dict[str, Any]]:
        """查询任务质量发现

        Args:
            task_id: 任务 ID（必填）
            status: 状态过滤（open / resolved / wontfix / all），默认 open
            severity: 严重度过滤（info / warn / error / block），默认不过滤

        Returns:
            finding 列表，按 created_at 升序排列（旧的先处理）
        """
        if not task_id:
            return []

        sql = "SELECT * FROM task_quality_findings WHERE task_id = ?"
        params: List[Any] = [task_id]

        if status and status != "all":
            sql += " AND status = ?"
            params.append(status)

        if severity:
            sql += " AND severity = ?"
            params.append(severity)

        sql += " ORDER BY created_at ASC"

        try:
            cur = self.conn.execute(sql, params)
            rows = cur.fetchall()
            return [dict(row) for row in rows]
        except Exception:
            return []

    def resolve_task_quality_finding(
        self,
        finding_id: int,
        resolution: str = "fixed",
        resolved_by: str = "agent",
    ) -> Dict[str, Any]:
        """解决或豁免单条 finding

        Args:
            finding_id: finding ID
            resolution: 解决方式（fixed / wontfix / false_positive）
            resolved_by: 解决者标识（agent / human / system）

        Returns:
            {success, finding_id, status, resolution, resolved_at}
        """
        if not finding_id:
            return {"success": False, "error": t(
                "cli.messages.task_quality_finding_id_required",
                default="finding_id is required")}

        # 映射 resolution 到 status
        status_map = {
            "fixed": "resolved",
            "wontfix": "wontfix",
            "false_positive": "wontfix",
        }
        new_status = status_map.get(resolution, "resolved")
        now = time.time()

        try:
            cur = self.conn.execute(
                "SELECT id FROM task_quality_findings WHERE id = ?",
                (finding_id,),
            )
            if not cur.fetchone():
                return {"success": False, "error": t(
                    "cli.messages.task_quality_finding_not_found",
                    default="finding not found", id=finding_id)}

            self.conn.execute(
                """
                UPDATE task_quality_findings
                SET status = ?, resolved_at = ?, resolved_by = ?
                WHERE id = ?
                """,
                (new_status, now, resolved_by, finding_id),
            )
            self.conn.commit()
            return {
                "success": True,
                "finding_id": finding_id,
                "status": new_status,
                "resolution": resolution,
                "resolved_at": now,
            }
        except Exception as e:
            return {"success": False, "error": str(e)}

    def task_has_blocking_findings(self, task_id: str) -> bool:
        """判断任务是否存在 open 状态的 error/block finding

        用于 task_report_step 决定 step 是否可以进入 done。
        当返回 True 时，应阻止 step 进入 done，并自动插入修复步骤。

        Args:
            task_id: 任务 ID

        Returns:
            True 表示存在阻塞 finding
        """
        if not task_id:
            return False

        try:
            cur = self.conn.execute(
                """
                SELECT COUNT(*) as cnt
                FROM task_quality_findings
                WHERE task_id = ?
                  AND status = 'open'
                  AND severity IN ('error', 'block')
                """,
                (task_id,),
            )
            row = cur.fetchone()
            return row["cnt"] > 0
        except Exception:
            return False

    def insert_fix_quality_gate_step(
        self,
        task_id: str,
        source_step_id: str,
        findings: List[Dict[str, Any]],
    ) -> str:
        """为质量门禁失败自动插入修复步骤

        在 task 下插入一个新的 step（action="fix_quality_gate_failure"），
        关联触发修复的源 step 和阻塞 finding 列表。

        Args:
            task_id: 任务 ID
            source_step_id: 触发修复的源步骤 ID
            findings: 阻塞 finding 列表（dict 至少包含 id / severity / message / finding_type）

        Returns:
            新建的 step_id（失败时返回空字符串）
        """
        if not task_id:
            return ""

        from .db_tasks import _gen_step_id

        now = time.time()
        step_id = _gen_step_id()

        # 构造 check_items：每个 finding 一条修复提示
        check_lines: List[str] = []
        for f in findings:
            sev = f.get("severity", "warn")
            ftype = f.get("finding_type", "")
            msg = f.get("message", "")
            check_lines.append(f"[{sev}] {ftype}: {msg}")
        check_items = "\n".join(check_lines) if check_lines else ""

        # 序列化 findings 摘要存入 result（便于 Agent 阅读）
        findings_summary = json.dumps(
            [
                {
                    "id": f.get("id"),
                    "severity": f.get("severity"),
                    "finding_type": f.get("finding_type"),
                    "message": f.get("message"),
                    "source": f.get("source", ""),
                }
                for f in findings
            ],
            ensure_ascii=False,
        )

        # 计算新 step 的 step_index（追加到末尾）
        try:
            cur = self.conn.execute(
                "SELECT COALESCE(MAX(step_index), -1) as max_idx FROM task_steps WHERE task_id = ?",
                (task_id,),
            )
            step_index = cur.fetchone()["max_idx"] + 1
        except Exception:
            step_index = 0

        try:
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
                    step_index,
                    "fix_quality_gate_failure",
                    "",
                    source_step_id,
                    check_items,
                    "pending",
                    findings_summary,
                    now,
                    None,
                ),
            )
            self.conn.commit()
            return step_id
        except Exception:
            return ""

    # ============================================================
    # 质量检查器（由 run_task_completion_review 调度）
    # ============================================================
    # 所有检查器统一使用 source='check_gate' 标识，通过 finding_type
    # 区分具体来源（scope / call_chain / file_health / i18n）。
    # 这样 run_task_completion_review 的清理逻辑（DELETE WHERE
    # source='check_gate'）能统一去重，避免重复累积。

    def _get_step_changed_files(self, task_id: str, step_id: str) -> List[str]:
        """返回本 step 记录的变更文件（change_audit.step_id 粒度）。

        scope 检查必须按 step 粒度取变更文件：run_task_completion_review 拿到的
        changed_files 是任务累计（get_task_changed_files = SELECT DISTINCT
        file_path FROM change_audit WHERE task_id=?），若直接用它比对单个 step 的
        target_file，多文件任务（每个 step 负责一个文件）会把其他 step 的合法文件
        误判为本 step 越界，产生结构性假阳（multi-file false positive）。

        本方法仅返回 change_audit 中 step_id 匹配的记录；当某 step 没有 per-step
        归因记录时返回空列表，scope 检查据此提前返回（不产生假阳）。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID

        Returns:
            本 step 记录的变更文件路径列表（去重）；查询失败返回空列表
        """
        if not task_id or not step_id:
            return []
        try:
            cur = self.conn.execute(
                "SELECT DISTINCT file_path FROM change_audit "
                "WHERE task_id = ? AND step_id = ?",
                (task_id, step_id),
            )
            return [r["file_path"] for r in cur if r["file_path"]]
        except Exception:
            return []

    def _check_scope_violations(
        self,
        task_id: str,
        step_id: str,
        changed_files: List[str],
    ) -> None:
        """检查变更文件是否超出 step 的 target_file 范围

        若 step 指定了 target_file，但 change_audit 记录的变更文件不在
        target_file 范围内（不是同一文件或其子路径），则记录 error finding。
        这是 scope violation，通常意味着 Agent 修改了非目标文件。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
            changed_files: 变更文件路径列表
        """
        if not step_id or not changed_files:
            return

        # 读取 step 的 target_file
        try:
            cur = self.conn.execute(
                "SELECT target_file FROM task_steps WHERE id = ?",
                (step_id,),
            )
            row = cur.fetchone()
        except Exception:
            return
        if not row:
            return
        target_file = row["target_file"] or ""
        if not target_file:
            return  # 无 target_file 约束，跳过检查

        # 标准化路径比较（统一正斜杠）
        target_norm = target_file.replace("\\", "/")
        for fp in changed_files:
            fp_norm = fp.replace("\\", "/")
            # 变更文件不是 target_file 本身，也不是其子路径
            if fp_norm != target_norm and not fp_norm.startswith(target_norm + "/"):
                self.record_task_quality_finding(
                    task_id=task_id,
                    step_id=step_id,
                    finding_type="scope",
                    severity="error",
                    message=t(
                        "cli.messages.task_quality_scope_violation",
                        default="Scope violation: file '{file}' is outside step target '{target}'",
                        file=fp,
                        target=target_file,
                    ),
                    evidence={"file_path": fp, "target_file": target_file},
                    source="check_gate",
                )

    def _check_symbol_attribution(
        self,
        task_id: str,
        step_id: str,
    ) -> None:
        """检查 target_symbol 非空但无 task_symbol_changes 记录

        若 step 指定了 target_symbol，但 task_symbol_changes 表中没有
        对应的符号变更归因记录，则记录 warn finding。可能是 Agent 修改了
        非目标符号，或忘记记录归因。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
        """
        if not step_id:
            return

        try:
            cur = self.conn.execute(
                "SELECT target_symbol FROM task_steps WHERE id = ?",
                (step_id,),
            )
            row = cur.fetchone()
        except Exception:
            return
        if not row:
            return
        target_symbol = row["target_symbol"] or ""
        if not target_symbol:
            return  # 无 target_symbol 约束，跳过检查

        # 检查 task_symbol_changes 表是否有记录
        try:
            cur = self.conn.execute(
                "SELECT COUNT(*) as cnt FROM task_symbol_changes "
                "WHERE task_id = ? AND step_id = ?",
                (task_id, step_id),
            )
            cnt = cur.fetchone()["cnt"]
        except Exception:
            return

        if cnt == 0:
            self.record_task_quality_finding(
                task_id=task_id,
                step_id=step_id,
                finding_type="call_chain",
                severity="warn",
                message=t(
                    "cli.messages.task_quality_symbol_attribution_missing",
                    default="Symbol attribution missing: target_symbol='{symbol}' but no task_symbol_changes recorded",
                    symbol=target_symbol,
                ),
                evidence={"target_symbol": target_symbol},
                source="check_gate",
            )

    def _check_file_health_findings(
        self,
        task_id: str,
        step_id: str,
        changed_files: List[str],
    ) -> None:
        """调用 check_file_health 生成文件级 health warning

        对每个变更文件调用 check_file_health（如果可用），将文件大小/复杂度
        警告转换为 task_quality_findings 记录。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
            changed_files: 变更文件路径列表
        """
        if not hasattr(self, "check_file_health"):
            return

        for fp in changed_files:
            try:
                health = self.check_file_health(fp)
            except Exception:
                continue
            if not health:
                continue

            # 文件过大检查
            total_lines = health.get("total_lines", 0) or 0
            if total_lines >= _FILE_SIZE_WARN_THRESHOLD:
                severity = "error" if total_lines >= _FILE_SIZE_ERROR_THRESHOLD else "warn"
                self.record_task_quality_finding(
                    task_id=task_id,
                    step_id=step_id,
                    finding_type="file_health",
                    severity=severity,
                    message=t(
                        "cli.messages.task_quality_file_too_large",
                        default="File too large: {file} has {lines} lines (threshold: {threshold})",
                        file=fp,
                        lines=total_lines,
                        threshold=_FILE_SIZE_WARN_THRESHOLD,
                    ),
                    evidence={"total_lines": total_lines, "file_path": fp},
                    source="check_gate",
                )

            # 复杂度热点检查（check_file_health 返回 function_issues 字段，
            # 每项含 qualified_name / cyclomatic_complexity / line_count / severity）
            hotspots = health.get("function_issues", []) or []
            for hs in hotspots:
                complexity = hs.get("cyclomatic_complexity", 0) or 0
                if complexity >= _COMPLEXITY_HOTSPOT_THRESHOLD:
                    self.record_task_quality_finding(
                        task_id=task_id,
                        step_id=step_id,
                        finding_type="file_health",
                        severity="warn",
                        message=t(
                            "cli.messages.task_quality_complexity_hotspot",
                            default="Complexity hotspot: {symbol} complexity={complexity}",
                            symbol=hs.get("qualified_name", ""),
                            complexity=complexity,
                        ),
                        evidence=hs,
                        source="check_gate",
                    )

    def _check_i18n_hardcoded(
        self,
        task_id: str,
        step_id: str,
        changed_files: List[str],
    ) -> None:
        """扫描变更文件中的硬编码 print/cprint/logger 输出

        对每个变更文件读取内容，扫描硬编码输出语句（print / cprint /
        logger.* / logging.*），记录 warn finding。
        这些输出应通过 i18n t() 函数本地化，而非直接硬编码。

        豁免规则：
        - tests/ 目录下的文件不扫描（测试文件允许直接 print）
        - 注释行（以 # 开头）不扫描

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
            changed_files: 变更文件路径列表
        """
        for fp in changed_files:
            # 豁免测试文件
            fp_norm = fp.replace("\\", "/")
            if fp_norm.startswith("tests/") or "/tests/" in fp_norm:
                continue

            # 仅扫描 Python 文件：i18n 硬编码检查器的模式（print/cprint/logger/logging）
            # 是 Python 特有语法，对 Rust/Go/TS/Java 等其他语言无意义
            # （Rust 用 println!/eprintln!，Go 用 fmt.Print，TS/JS 用 console.log）
            if not fp_norm.endswith(".py"):
                continue

            # 解析绝对路径：优先用 _resolve_abs_path，否则用 workspace_root 拼接
            if hasattr(self, "_resolve_abs_path"):
                abs_path = self._resolve_abs_path(fp) or ""
            elif hasattr(self, "workspace_root") and self.workspace_root:
                abs_path = os.path.join(self.workspace_root, fp)
            else:
                abs_path = fp
            if not abs_path or not os.path.exists(abs_path):
                continue

            try:
                with open(abs_path, "r", encoding="utf-8", errors="ignore") as f:
                    for line_no, line in enumerate(f, start=1):
                        stripped = line.lstrip()
                        # 跳过注释行
                        if stripped.startswith("#"):
                            continue
                        # 检查是否匹配硬编码输出模式
                        for pattern in _I18N_HARDCODED_PATTERNS:
                            if pattern.match(line):
                                self.record_task_quality_finding(
                                    task_id=task_id,
                                    step_id=step_id,
                                    finding_type="i18n",
                                    severity="warn",
                                    message=t(
                                        "cli.messages.task_quality_i18n_hardcoded",
                                        default="Hardcoded output: {file}:{line} - {code}",
                                        file=fp,
                                        line=line_no,
                                        code=stripped.strip(),
                                    ),
                                    evidence={
                                        "file_path": fp,
                                        "line": line_no,
                                        "code": stripped.strip(),
                                    },
                                    source="check_gate",
                                )
                                break  # 一行只记录一次
            except Exception:
                pass

    def _check_signature_mismatch(
        self,
        task_id: str,
        step_id: str,
    ) -> None:
        """检查函数签名变更后是否存在未解析的调用方

        流程：
        1. 查 task_symbol_changes WHERE task_id=? AND step_id=?
        2. 对每条记录，通过 symbol_contents JOIN 比对 before/after signature
        3. 若 signature 变化：
           a. 调 get_callers(symbol_name) 获取全部调用方
           b. 检查 calls 表中 callee_id=0 或 callee_qualified 为空的记录
              （这些是刷新后仍未解析的调用）
           c. 若存在 unresolved callers → 生成 block finding
              否则 → 生成 info finding（签名变更但调用方都已更新）

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
        """
        if not step_id:
            return

        # 查询本 step 的符号变更记录
        try:
            cur = self.conn.execute(
                """
                SELECT tsc.qualified_name, tsc.symbol_name,
                       tsc.symbol_hash_before, tsc.symbol_hash_after,
                       tsc.file_path
                FROM task_symbol_changes tsc
                WHERE tsc.task_id = ? AND tsc.step_id = ?
                """,
                (task_id, step_id),
            )
            changes = [dict(row) for row in cur.fetchall()]
        except Exception:
            return

        if not changes:
            return  # 无符号变更记录，跳过

        for ch in changes:
            qualified_name = ch.get("qualified_name", "") or ""
            symbol_name = ch.get("symbol_name", "") or ""
            hash_before = ch.get("symbol_hash_before", "") or ""
            hash_after = ch.get("symbol_hash_after", "") or ""
            file_path = ch.get("file_path", "") or ""

            # 无 before/after hash 无法比较，跳过
            if not hash_before or not hash_after:
                continue
            # hash 相同 → 内容未变，跳过
            if hash_before == hash_after:
                continue

            # 通过 symbol_contents 查 signature（before / after）
            try:
                cur = self.conn.execute(
                    """
                    SELECT
                        (SELECT signature FROM symbol_contents
                         WHERE content_hash = ?) as old_sig,
                        (SELECT signature FROM symbol_contents
                         WHERE content_hash = ?) as new_sig
                    """,
                    (hash_before, hash_after),
                )
                sig_row = cur.fetchone()
            except Exception:
                continue
            if not sig_row:
                continue

            old_sig = sig_row["old_sig"] or ""
            new_sig = sig_row["new_sig"] or ""

            # signature 未变 → 不是签名变更，跳过
            if old_sig == new_sig:
                continue

            # 签名变更：查询调用方
            symbol_to_query = symbol_name or qualified_name
            if not symbol_to_query:
                continue

            callers = []
            if hasattr(self, "get_callers"):
                try:
                    callers = self.get_callers(symbol_to_query) or []
                except Exception:
                    callers = []

            caller_count = len(callers)
            # 检查 unresolved callers：callee_id=0 或 callee_qualified 为空
            unresolved_callers = []
            for c in callers:
                callee_id = c.get("callee_id", 0) or 0
                if callee_id == 0:
                    unresolved_callers.append({
                        "caller": c.get("caller_name", ""),
                        "file": c.get("caller_file", ""),
                        "line": c.get("call_line", 0),
                    })

            unresolved_count = len(unresolved_callers)
            # 无调用方 → 签名变更但无人调用，记 info（不阻塞）
            if caller_count == 0:
                self.record_task_quality_finding(
                    task_id=task_id,
                    step_id=step_id,
                    finding_type="call_chain",
                    severity="info",
                    message=t(
                        "cli.messages.task_quality_signature_changed_no_callers",
                        default="Signature changed for {symbol} but no callers found",
                        symbol=qualified_name or symbol_name,
                    ),
                    evidence={
                        "changed_symbol": qualified_name or symbol_name,
                        "old_signature": old_sig,
                        "new_signature": new_sig,
                        "caller_count": 0,
                        "unresolved_callers": [],
                    },
                    source="check_gate",
                )
                continue

            # 有 unresolved callers → block finding
            if unresolved_count > 0:
                self.record_task_quality_finding(
                    task_id=task_id,
                    step_id=step_id,
                    finding_type="call_chain",
                    severity="block",
                    message=t(
                        "cli.messages.task_quality_signature_mismatch",
                        default="Signature changed for {symbol}: {unresolved}/{total} callers unresolved",
                        symbol=qualified_name or symbol_name,
                        unresolved=unresolved_count,
                        total=caller_count,
                    ),
                    evidence={
                        "changed_symbol": qualified_name or symbol_name,
                        "old_signature": old_sig,
                        "new_signature": new_sig,
                        "caller_count": caller_count,
                        "unresolved_callers": unresolved_callers,
                    },
                    source="check_gate",
                )
            else:
                # 签名变更但所有调用方都已更新 → info finding
                self.record_task_quality_finding(
                    task_id=task_id,
                    step_id=step_id,
                    finding_type="call_chain",
                    severity="info",
                    message=t(
                        "cli.messages.task_quality_signature_updated",
                        default="Signature changed for {symbol}: all {total} callers resolved",
                        symbol=qualified_name or symbol_name,
                        total=caller_count,
                    ),
                    evidence={
                        "changed_symbol": qualified_name or symbol_name,
                        "old_signature": old_sig,
                        "new_signature": new_sig,
                        "caller_count": caller_count,
                        "unresolved_callers": [],
                    },
                    source="check_gate",
                )

    # ============================================================
    # 设计文档：_check_signature_mismatch（调用链一致性检查器）
    # ============================================================
    # 来源：plan 文档「调用链一致性」章节
    #
    # 数据来源（按优先级复用现有能力，不引入新表）：
    # 1. task_symbol_changes 表：记录每个 step 修改的符号及其前后 hash
    #    - symbol_hash_before：变更前的符号内容 hash
    #    - symbol_hash_after：变更后的符号内容 hash
    #    - qualified_name：符号限定名（用于查 get_callers）
    #    - file_path：符号所在文件
    # 2. symbol_contents 表：通过 content_hash 查 signature 字段
    #    - 用 symbol_hash_before / symbol_hash_after 分别 JOIN
    #      symbol_contents.content_hash，取 signature 比对
    #    - 若 signature 不同 → 判定为签名变更
    # 3. get_callers(qualified_name)：查询旧调用方列表
    #    - 返回 caller_qualified / caller_file / call_line 等
    # 4. calls 表（is_cross_file / callee_id）：刷新后检查 unresolved calls
    #    - callee_id=0 或 callee_qualified 与新签名不匹配 → 未解析
    #
    # 检查流程：
    # 1. 查 task_symbol_changes WHERE task_id=? AND step_id=?
    # 2. 对每条记录，比较 before/after 的 signature（通过 symbol_contents JOIN）
    # 3. 若 signature 变化：
    #    a. 调 get_callers(qualified_name) 获取全部调用方
    #    b. 检查 calls 表中 callee_id=0 或 callee_qualified 不匹配的记录
    #       （这些是刷新后仍未解析的调用）
    #    c. 若存在 unresolved callers → 生成 block finding
    #       否则 → 生成 info finding（签名变更但调用方都已更新）
    #
    # evidence JSON 格式（满足 check_items 第 3 条）：
    # {
    #   "changed_symbol": "module::parse_policy",
    #   "old_signature": "fn parse_policy(text: &str) -> Result<Policy>",
    #   "new_signature": "fn parse_policy(text: &str, strict: bool) -> Result<Policy>",
    #   "caller_count": 23,
    #   "unresolved_callers": [
    #     {"caller": "module::main", "file": "src/main.rs", "line": 45},
    #     {"caller": "module::utils", "file": "src/utils.rs", "line": 12}
    #   ]
    # }
    #
    # finding 字段：
    # - finding_type: "call_chain"（与 _check_symbol_attribution 同类）
    # - severity: "block"（有 unresolved callers）/ "info"（签名变更但调用方已更新）
    # - source: "check_gate"（统一标识，由调度器清理去重）
    # - message: t("cli.messages.task_quality_signature_mismatch",
    #              default="函数 {symbol} 签名已变更，但 {unresolved}/{total} 个调用方未更新",
    #              symbol=..., unresolved=..., total=...)

    def run_task_completion_review(
        self,
        task_id: str,
        step_id: str = "",
    ) -> Dict[str, Any]:
        """运行任务完成质量审查

        流程：
        1. 清理该 step 的旧 check_gate finding（避免重复累积）
        2. 调用 run_check_gate 对变更文件做语法/Semgrep 检查，结果写入
           task_quality_findings（source='check_gate'）
        3. 运行 5 个扩展检查器（均使用 source='check_gate'）：
           - _check_scope_violations: 变更文件超出 target_file 范围 → error
           - _check_symbol_attribution: target_symbol 无 task_symbol_changes → warn
           - _check_file_health_findings: 文件过大/复杂度热点 → warn/error
           - _check_i18n_hardcoded: 硬编码 print/cprint/logger 输出 → warn
           - _check_signature_mismatch: 签名变更后调用方未解析 → block/info
        4. 收集 task/step 下的所有 open finding，根据 severity 决策：
           - 无 finding → pass（允许 step 进入 done）
           - 仅有 info/warn → warn（记录但允许完成）
           - 存在 error/block → block（step 阻塞，自动插入 fix_quality_gate_failure）

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID（可选，任务级审查留空）

        Returns:
            {decision, findings, summary, counts, check_gate_result}
            decision ∈ {"pass", "warn", "block"}
        """
        if not task_id:
            return {
                "decision": "pass",
                "findings": [],
                "summary": t("cli.messages.task_quality_finding_id_required",
                             default="task_id is required"),
                "counts": {"info": 0, "warn": 0, "error": 0, "block": 0},
                "check_gate_result": None,
            }

        # 获取任务关联的变更文件（change_audit 表由 task_report_step 写入）
        changed_files: List[str] = []
        if hasattr(self, "get_task_changed_files"):
            try:
                changed_files = self.get_task_changed_files(task_id)
            except Exception:
                changed_files = []

        # 清理该 step 关联的旧 check_gate finding（所有检查器统一使用 source='check_gate'）
        if step_id:
            try:
                self.conn.execute(
                    "DELETE FROM task_quality_findings "
                    "WHERE task_id = ? AND step_id = ? AND source = 'check_gate'",
                    (task_id, step_id),
                )
                self.conn.commit()
            except Exception:
                pass

        # 步骤 2: 调用 run_check_gate 并把结果转换为 task_quality_findings
        check_gate_result: Optional[Dict[str, Any]] = None
        if changed_files and hasattr(self, "run_check_gate"):
            try:
                # 调用 run_check_gate（语法 + Semgrep 检查）
                check_gate_result = self.run_check_gate(
                    task_id, step_id, changed_files
                )

                # 把 findings 转换为 task_quality_findings 记录
                for f in check_gate_result.get("findings", []):
                    self.record_task_quality_finding(
                        task_id=task_id,
                        step_id=step_id,
                        finding_type=f.get("finding_type", ""),
                        severity=f.get("severity", "warn"),
                        message=f.get("message", ""),
                        evidence={
                            "rule_id": f.get("rule_id", ""),
                            "line": f.get("line", 0),
                            "file_path": f.get("file_path", ""),
                        },
                        source="check_gate",
                    )
            except Exception:
                check_gate_result = None

        # 步骤 3: 运行扩展检查器（scope / symbol / file_health / i18n / signature）
        # 这些检查器也使用 source='check_gate'，与 run_check_gate 的 finding
        # 一起被清理和决策。
        # 按 changed_files 推断语言集合，根据 _CHECKER_LANGUAGES 映射表过滤调度：
        # - i18n 仅适用于 Python 文件（其他语言没有 print/cprint 概念）
        # - scope/symbol_attribution/file_health/signature_mismatch 跨语言或自动适配
        from ..config import detect_language_from_path
        changed_languages: set = set()
        for fp in changed_files:
            lang = detect_language_from_path(fp)
            if lang:
                changed_languages.add(lang)

        def _checker_applies(checker_name: str) -> bool:
            """根据 _CHECKER_LANGUAGES 映射表判断检查器是否适用于当前 changed_files"""
            langs = _CHECKER_LANGUAGES.get(checker_name)
            if langs is None:
                return True  # None 表示适用于所有语言
            # 检查 changed_files 的语言集合是否与检查器适用语言有交集
            return bool(changed_languages & langs)

        if changed_files:
            if _checker_applies("scope"):
                try:
                    # scope 检查按 step 粒度取变更文件，避免任务累计变更把其他
                    # step 的合法文件误判为本 step 越界（多文件任务结构性假阳）。
                    # 其他检查器（file_health/i18n）仍使用任务累计 changed_files。
                    scope_files = self._get_step_changed_files(task_id, step_id)
                    self._check_scope_violations(task_id, step_id, scope_files)
                except Exception:
                    pass
            if _checker_applies("symbol_attribution"):
                try:
                    self._check_symbol_attribution(task_id, step_id)
                except Exception:
                    pass
            if _checker_applies("file_health"):
                try:
                    self._check_file_health_findings(task_id, step_id, changed_files)
                except Exception:
                    pass
            if _checker_applies("i18n"):
                try:
                    self._check_i18n_hardcoded(task_id, step_id, changed_files)
                except Exception:
                    pass
        # signature_mismatch 不依赖 changed_files，依赖 task_symbol_changes
        if _checker_applies("signature_mismatch"):
            try:
                self._check_signature_mismatch(task_id, step_id)
            except Exception:
                pass

        # 步骤 4: 收集 open findings 做决策
        findings = self.get_task_quality_findings(task_id, status="open")

        # 按 step_id 过滤（如果指定了 step_id）
        if step_id:
            step_findings = [f for f in findings if f.get("step_id") == step_id]
            task_level_findings = [f for f in findings if not f.get("step_id")]
            scoped = step_findings + task_level_findings
        else:
            scoped = findings

        # 分类统计
        counts = {"info": 0, "warn": 0, "error": 0, "block": 0}
        for f in scoped:
            sev = f.get("severity", "warn")
            if sev in counts:
                counts[sev] += 1

        # 决策：error/block → block；info/warn → warn；无 → pass
        if counts["error"] > 0 or counts["block"] > 0:
            decision = "block"
        elif counts["info"] > 0 or counts["warn"] > 0:
            decision = "warn"
        else:
            decision = "pass"

        # 构造摘要
        summary = t(
            "cli.messages.task_quality_review_summary",
            default="review: {decision}, {total} findings (info={info}, warn={warn}, error={error}, block={block})",
            decision=decision,
            total=len(scoped),
            info=counts["info"],
            warn=counts["warn"],
            error=counts["error"],
            block=counts["block"],
        )

        return {
            "decision": decision,
            "findings": scoped,
            "summary": summary,
            "counts": counts,
            "check_gate_result": check_gate_result,
        }

