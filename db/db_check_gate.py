"""检查门禁 Mixin —— Agent OS 的质量守卫（F6）

职责：
- 在 task_report_step 后自动运行检查（语法检查 + Semgrep 增量扫描）
- 检查失败时自动插入 fix_gate_failure 步骤，Agent 必须修复才能继续
- 结果写入 guardrail_findings 表（复用 v10 安全护栏表）
- 提供 resolve_gate_findings 标记门禁发现为已解决

设计要点：
- 语法检查通过 tree-sitter re-parse（复用 BuildMixin.create_parser，hasattr 防御）
- Semgrep 增量扫描只扫修改的文件（复用 IssueAnalyzerMixin.run_semgrep，hasattr 防御）
- 工具不可用时降级跳过，不阻塞任务流
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, List


class CheckGateMixin:
    """检查门禁 Mixin（F6）

    通过 Mixin 组合复用 EditSafetyMixin._resolve_abs_path 解析文件路径，
    复用 BuildMixin.create_parser 做语法检查，复用 IssueAnalyzerMixin.run_semgrep 做安全扫描。
    """

    def run_check_gate(
        self,
        task_id: str,
        step_id: str,
        changed_files: List[str],
    ) -> Dict[str, Any]:
        """运行检查门禁

        对变更的文件执行语法检查和 Semgrep 扫描，结果写入 guardrail_findings 表。

        Args:
            task_id: 任务 ID
            step_id: 步骤 ID
            changed_files: 变更的文件路径列表（相对或绝对路径）

        Returns:
            {
                "passed": bool,              # 是否通过（无 ERROR 级发现）
                "checks_run": ["syntax", ...], # 实际运行的检查项
                "findings": [...],           # 发现列表
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

            # 检查 1: 语法检查（tree-sitter re-parse）
            if hasattr(self, "create_parser"):
                try:
                    parser = self.create_parser(abs_path)
                    if parser:
                        result = parser.parse_file(abs_path) if hasattr(parser, "parse_file") else {}
                        if result.get("parse_error"):
                            findings.append(
                                {
                                    "check": "syntax",
                                    "file": fp,
                                    "severity": "ERROR",
                                    "message": f"语法错误: {result['parse_error']}",
                                }
                            )
                        checks_run.append("syntax")
                except Exception as e:
                    findings.append(
                        {
                            "check": "syntax",
                            "file": fp,
                            "severity": "WARNING",
                            "message": f"语法检查异常: {e}",
                        }
                    )
                    checks_run.append("syntax")

            # 检查 2: Semgrep 增量扫描
            if hasattr(self, "run_semgrep"):
                try:
                    sem_result = self.run_semgrep(
                        target_paths=[abs_path],
                        config="p/default",
                        timeout=60,
                    )
                    checks_run.append("semgrep")
                    if sem_result.get("success") and sem_result.get("total_findings", 0) > 0:
                        for f in sem_result.get("results", []):
                            findings.append(
                                {
                                    "check": "semgrep",
                                    "file": fp,
                                    "severity": f.get("severity", "WARNING"),
                                    "rule_id": f.get("rule_id", ""),
                                    "message": f.get("message", ""),
                                    "line": f.get("start_line", 0),
                                }
                            )
                except Exception:
                    pass  # Semgrep 不可用不阻塞门禁流程

        # 写入 guardrail_findings 表
        if findings:
            self._save_gate_findings(task_id, step_id, findings)

        # 判断是否通过（只有 ERROR 级发现才算失败）
        error_checks = {f["check"] for f in findings if f["severity"] == "ERROR"}
        passed = len(error_checks) == 0

        # 构建摘要
        all_checks = sorted(set(checks_run))
        summary_parts = []
        for c in all_checks:
            status = "FAIL" if c in error_checks else "pass"
            summary_parts.append(f"{c}:{status}")

        return {
            "passed": passed,
            "checks_run": all_checks,
            "findings": findings,
            "fix_required": not passed,
            "summary": f"检查{'通过' if passed else '失败'}: {', '.join(summary_parts)}" if summary_parts else "检查通过（无可用检查器）",
        }

    def _save_gate_findings(
        self,
        task_id: str,
        step_id: str,
        findings: List[Dict[str, Any]],
    ) -> None:
        """将门禁发现写入 guardrail_findings 表

        为每个发现自动创建或复用对应的 guardrail_rule（category='check_gate'）。
        """
        now = time.time()
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
                    (rule_id, file_path, symbol_hash, severity, status, message,
                     detected_at)
                VALUES (?, ?, '', ?, 'open', ?, ?)
                """,
                (
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
        for fp in files:
            cur = self.conn.execute(
                """
                UPDATE guardrail_findings
                SET status = 'resolved', resolved_at = ?
                WHERE file_path = ? AND status = 'open'
                """,
                (now, fp),
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
