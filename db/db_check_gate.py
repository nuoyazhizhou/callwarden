"""检查门禁 Mixin —— Agent OS 的质量守卫（F6）

职责：
- 在 task_report_step 后自动运行检查（语法检查 + Semgrep 增量扫描）
- 检查失败时自动插入 fix_gate_failure 步骤，Agent 必须修复才能继续
- 结果写入 guardrail_findings 表（复用 v10 安全护栏表）
- 提供 resolve_gate_findings 标记门禁发现为已解决

设计要点：
- P1-E: 语法检查通过 RustParserFacade re-parse + extract_diagnostics（Rust-only）
- Semgrep 增量扫描只扫修改的文件（复用 IssueAnalyzerMixin.run_semgrep，hasattr 防御）
- 工具不可用时降级跳过，不阻塞任务流
- run_check_gate 只负责检查与报告，不直接承担 task 状态决策（由 task_report_step 决定）
- findings 列表已标准化：每个 finding 包含 finding_type / severity（小写）/ file_path /
  line / rule_id / message，与 task_quality_findings 表字段对齐
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List

from .rust_parser_facade import RustParserFacade


# severity 大写 → 小写映射（与 task_quality_findings.severity 对齐）
_SEVERITY_MAP = {
    "ERROR": "error",
    "WARNING": "warn",
    "INFO": "info",
    "BLOCK": "block",
}


def _normalize_severity(raw: str) -> str:
    """将大写 severity（ERROR/WARNING/INFO/BLOCK）标准化为小写（error/warn/info/block）"""
    return _SEVERITY_MAP.get((raw or "").upper(), "warn")


class CheckGateMixin:
    """检查门禁 Mixin（F6）

    通过 Mixin 组合复用 EditSafetyMixin._resolve_abs_path 解析文件路径，
    P1-E: 语法检查走 RustParserFacade（Rust-only），复用 IssueAnalyzerMixin.run_semgrep 做安全扫描。
    """

    def run_check_gate(
        self,
        task_id: str,
        step_id: str,
        changed_files: List[str],
    ) -> Dict[str, Any]:
        """运行检查门禁

        对变更的文件执行语法检查和 Semgrep 扫描，结果写入 guardrail_findings 表。
        本方法只负责检查与报告，不直接修改 task/step 状态（由 task_report_step 决策）。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
            changed_files: 变更的文件路径列表（相对或绝对路径）

        Returns:
            {
                "passed": bool,              # 是否通过（无 error/block 级发现）
                "checks_run": ["syntax", ...], # 实际运行的检查项
                "findings": [...],           # 标准化发现列表（含 finding_type/severity 小写等）
                "fix_required": bool,        # 是否需要修复（= !passed）
                "summary": "..."             # 人类可读摘要
            }
        """
        findings: List[Dict[str, Any]] = []
        checks_run: List[str] = []

        for fp in changed_files:
            abs_path = self._resolve_abs_path(fp) if hasattr(self, "_resolve_abs_path") else fp
            if not abs_path or not os.path.exists(abs_path):
                continue

            # 检查 1: 语法检查（P1-E: RustParserFacade re-parse + extract_diagnostics）
            # 设计 §5.3：Rust 解析失败 fail closed，partial/failed 状态显式记录。
            # 不再实例化 Python parser，通过 RustParserFacade 统一窄接口访问 Rust diagnostics。
            try:
                from ..config import detect_language_from_path
                file_lang = detect_language_from_path(fp)
                if file_lang and RustParserFacade.supports_language(file_lang):
                    result = RustParserFacade.parse_file(abs_path, "", file_lang)
                    diag = RustParserFacade.extract_diagnostics(result)
                    status = diag.get("status", "ok")
                    if status == "failed":
                        # fatal parse error → error 级 finding
                        msg = diag.get("fatal_parse_error") or "Rust parse failed"
                        findings.append(
                            self._standardize_finding(
                                check="syntax",
                                file_path=fp,
                                severity="ERROR",
                                message=f"语法错误: {msg}",
                            )
                        )
                    elif diag.get("syntax_error_count", 0) > 0:
                        # 有语法错误但 parse 未完全失败 → error 级 finding
                        findings.append(
                            self._standardize_finding(
                                check="syntax",
                                file_path=fp,
                                severity="ERROR",
                                message=f"语法错误: {diag['syntax_error_count']} 处",
                            )
                        )
                    elif status == "partial" and diag.get("unsupported_construct_count", 0) > 0:
                        # partial 状态（unsupported construct）→ warning 级 finding
                        findings.append(
                            self._standardize_finding(
                                check="syntax",
                                file_path=fp,
                                severity="WARNING",
                                message=f"部分构造不支持: {diag['unsupported_construct_count']} 处",
                            )
                        )
                    checks_run.append("syntax")
            except Exception as e:
                findings.append(
                    self._standardize_finding(
                        check="syntax",
                        file_path=fp,
                        severity="WARNING",
                        message=f"语法检查异常: {e}",
                    )
                )
                checks_run.append("syntax")

            # 检查 2: Semgrep 增量扫描
            # 按文件扩展名推断语言并传给 semgrep，避免全量语言规则扫描
            # （run_semgrep 会把 languages 转换为 --include 过滤，只加载相关语言规则）
            if hasattr(self, "run_semgrep"):
                try:
                    from ..config import detect_language_from_path
                    file_lang = detect_language_from_path(fp)
                    sem_languages = [file_lang] if file_lang else None

                    sem_result = self.run_semgrep(
                        target_paths=[abs_path],
                        config="p/default",
                        languages=sem_languages,
                        timeout=60,
                    )
                    checks_run.append("semgrep")
                    if sem_result.get("success") and sem_result.get("total_findings", 0) > 0:
                        for f in sem_result.get("results", []):
                            findings.append(
                                self._standardize_finding(
                                    check="semgrep",
                                    file_path=fp,
                                    severity=f.get("severity", "WARNING"),
                                    rule_id=f.get("rule_id", ""),
                                    message=f.get("message", ""),
                                    line=f.get("start_line", 0),
                                )
                            )
                except Exception:
                    pass  # Semgrep 不可用不阻塞门禁流程

        # 写入 guardrail_findings 表（用标准化字段）
        if findings:
            self._save_gate_findings(task_id, step_id, findings)

        # 判断是否通过（只有 error/block 级发现才算失败）
        blocking_checks = {
            f["finding_type"] for f in findings
            if f["severity"] in ("error", "block")
        }
        passed = len(blocking_checks) == 0

        # 构建摘要
        all_checks = sorted(set(checks_run))
        summary_parts = []
        for c in all_checks:
            status = "FAIL" if c in blocking_checks else "pass"
            summary_parts.append(f"{c}:{status}")

        return {
            "passed": passed,
            "checks_run": all_checks,
            "findings": findings,
            "fix_required": not passed,
            "summary": f"检查{'通过' if passed else '失败'}: {', '.join(summary_parts)}" if summary_parts else "检查通过（无可用检查器）",
        }

    @staticmethod
    def _standardize_finding(
        check: str,
        file_path: str,
        severity: str,
        message: str,
        rule_id: str = "",
        line: int = 0,
    ) -> Dict[str, Any]:
        """标准化 finding 字段，与 task_quality_findings 表对齐

        同时保留旧字段（check/file/severity 大写）向后兼容，
        新增字段（finding_type/file_path/severity 小写）供 TaskQualityMixin 使用。
        """
        return {
            # 标准化字段（与 task_quality_findings 对齐）
            "finding_type": check,
            "file_path": file_path,
            "severity": _normalize_severity(severity),
            "rule_id": rule_id,
            "line": line,
            "message": message,
            # 向后兼容字段（旧代码可能引用 check/file/severity 大写）
            "check": check,
            "file": file_path,
            "raw_severity": severity,
        }

    def _save_gate_findings(
        self,
        task_id: str,
        step_id: str,
        findings: List[Dict[str, Any]],
    ) -> None:
        """将门禁发现写入 guardrail_findings 表

        为每个发现自动创建或复用对应的 guardrail_rule（category='check_gate'）。

        v48（W2.3 P1-1）：每条 finding 必须绑定当前 active workspace_id，
        禁止插入 workspace_id=0 的 open finding（查询层 fail-closed）。
        """
        now = time.time()
        ws_id = self._get_active_workspace_id()
        for f in findings:
            rule_id = f.get("rule_id") or f"gate_{f['check']}_{f['severity'].lower()}"
            # 插入规则（已存在则忽略）
            self.conn.execute(
                """
                INSERT OR IGNORE INTO guardrail_rules
                    (rule_id, category, severity, pattern, action, description,
                     is_builtin, created_at)
                VALUES (?, 'check_gate', ?, '*', 'require_review', ?, 1, ?)
                """,
                (
                    rule_id,
                    f["severity"],
                    f"检查门禁: {f['check']} - {f.get('message', '')}",
                    now,
                ),
            )
            # 插入发现
            self.conn.execute(
                """
                INSERT INTO guardrail_findings
                    (workspace_id, rule_id, file_path, symbol_hash, severity, status, message,
                     detected_at)
                VALUES (?, ?, ?, '', ?, 'open', ?, ?)
                """,
                (
                    ws_id,
                    rule_id,
                    f["file"],
                    f["severity"],
                    f.get("message", ""),
                    now,
                ),
            )
        self.conn.commit()

    def resolve_gate_findings(self, task_id: str) -> Dict[str, Any]:
        """标记任务的门禁发现为已解决

        通过 change_audit 表查找该任务关联的所有变更文件，
        将这些文件上的 open 状态 guardrail_findings 标记为 resolved。

        Args:
            task_id: 任务 ID

        Returns:
            {"resolved_count": int, "task_id": str}
        """
        now = time.time()
        # 查找任务关联的变更文件（change_audit 表由 task_report_step 写入）
        cur = self.conn.execute(
            "SELECT DISTINCT file_path FROM change_audit WHERE task_id = ?",
            (task_id,),
        )
        files = [row["file_path"] for row in cur if row["file_path"]]

        resolved = 0
        ws_id = self._get_active_workspace_id()
        for fp in files:
            cur = self.conn.execute(
                """
                UPDATE guardrail_findings
                SET status = 'resolved', resolved_at = ?
                WHERE workspace_id = ? AND file_path = ? AND status = 'open'
                """,
                (now, ws_id, fp),
            )
            resolved += cur.rowcount
        self.conn.commit()

        return {"resolved_count": resolved, "task_id": task_id}

    def get_task_changed_files(self, task_id: str) -> List[str]:
        """获取任务关联的变更文件列表

        从 change_audit 表查询指定任务的所有变更文件路径（去重）。

        Args:
            task_id: 任务 ID

        Returns:
            变更文件路径列表（相对路径）
        """
        cur = self.conn.execute(
            "SELECT DISTINCT file_path FROM change_audit WHERE task_id = ?",
            (task_id,),
        )
        return [row["file_path"] for row in cur if row["file_path"]]
