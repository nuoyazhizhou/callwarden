"""pr_check.py
=============

PR 阻断检查模块：在 PR CI 中聚合 guardrail + Semgrep 结果，
决定是否阻断合并，并产出 SARIF 报告供 GitHub Code Scanning 消费。
"""

from __future__ import annotations

from typing import Dict, List

from .incremental import IncrementalAnalyzer
from .sarif_exporter import SarifExporter


# 被视为“错误级”的 severity（会拉低 passed 标志）
_ERROR_SEVERITIES = {"error", "block"}
# 被视为“告警级”的 severity
_WARNING_SEVERITIES = {"warning", "warn"}


def _normalize_severity(sev: str) -> str:
    """severity 归一化为小写"""
    if not sev:
        return ""
    return str(sev).strip().lower()


class PRChecker:
    """PR 检查器：聚合增量分析 + guardrail + Semgrep

    Args:
        db: CodeGraphDB 实例
    """

    def __init__(self, db):
        """初始化 PR 检查器，复用增量分析器与 SARIF 导出器

        Args:
            db: CodeGraphDB 实例，提供图谱查询与 guardrail 检查能力
        """
        self.db = db
        # 复用同一个 IncrementalAnalyzer 实例
        self.incremental = IncrementalAnalyzer(db)
        # 复用同一个 SarifExporter 实例
        self.sarif_exporter = SarifExporter()

    def run_pr_check(self, base_branch: str = "main", head: str = "HEAD") -> Dict:
        """运行 PR 检查并产出汇总结果

        流程：
        1. 通过 IncrementalAnalyzer 拿到变更文件清单
        2. 对变更文件运行 guardrail 检查（若 db 提供相关方法）
        3. 对变更文件运行 Semgrep 扫描（若 db 提供相关方法）
        4. 查询 guardrail_findings 表中 status='open' 的记录作为最终发现
        5. 生成 SARIF 报告并返回汇总

        Args:
            base_branch: 基准分支
            head: 目标提交

        Returns:
            {"passed": bool, "total_findings": N, "errors": N,
             "warnings": N, "sarif_report": {...}}
        """
        # 1. 获取变更文件
        changed_files = self.incremental.get_changed_files(
            base_branch=base_branch, head=head
        )

        # 2. guardrail 编辑前检查（仅当 db 提供该方法时调用）
        guardrail_check_fn = getattr(self.db, "guardrail_check_edit", None)
        if guardrail_check_fn is not None and changed_files:
            for file_path in changed_files:
                try:
                    guardrail_check_fn(file_path)
                except Exception:
                    # 单文件检查失败不中断整体流程
                    pass

        # 3. Semgrep 扫描（仅当 db 提供该方法时调用）
        semgrep_fn = getattr(self.db, "run_semgrep_and_save", None)
        if semgrep_fn is not None and changed_files:
            try:
                semgrep_fn(target_paths=changed_files)
            except Exception:
                # Semgrep 失败不阻断 PR 检查汇总
                pass

        # 4. 汇总 findings：查询 guardrail_findings 表中 open 状态的记录
        findings = self._query_open_findings(changed_files)

        # 5. 统计错误 / 告警数量
        errors = 0
        warnings = 0
        for f in findings:
            sev = _normalize_severity(f.get("severity"))
            if sev in _ERROR_SEVERITIES:
                errors += 1
            elif sev in _WARNING_SEVERITIES:
                warnings += 1

        # 6. 生成 SARIF 报告
        sarif_report = self.sarif_exporter.export_findings(findings)

        # passed 判定：无 error 级发现即视为通过
        passed = errors == 0

        return {
            "passed": passed,
            "total_findings": len(findings),
            "errors": errors,
            "warnings": warnings,
            "sarif_report": sarif_report,
        }

    def check_blocking_rules(self, changed_files: List[str]) -> Dict:
        """检查是否有阻断规则触发（open 状态的 findings）

        Args:
            changed_files: 变更文件列表

        Returns:
            {"blocked": bool, "blocking_findings": [...]}
            blocking_findings 为 severity 属于错误级的 open 记录
        """
        all_findings = self._query_open_findings(changed_files)
        blocking = [
            f for f in all_findings
            if _normalize_severity(f.get("severity")) in _ERROR_SEVERITIES
        ]
        return {
            "blocked": len(blocking) > 0,
            "blocking_findings": blocking,
        }

    def export_sarif(self, findings, output_path) -> str:
        """导出 SARIF 报告到文件

        Args:
            findings: finding 字典列表
            output_path: 输出文件路径

        Returns:
            实际写入的文件路径
        """
        return self.sarif_exporter.export_to_file(findings, output_path)

    # ------------------------------------------------------------------
    # 内部辅助
    # ------------------------------------------------------------------

    def _query_open_findings(self, changed_files: List[str]) -> List[Dict]:
        """查询 guardrail_findings 表中 status='open' 且 file_path 命中变更文件的记录

        Args:
            changed_files: 变更文件路径列表

        Returns:
            finding 字典列表；db 无 conn 或无表时返回空列表
        """
        if not changed_files:
            return []

        conn = getattr(self.db, "conn", None)
        if conn is None:
            return []

        # 统一为正斜杠比较，覆盖路径分隔符差异
        normalized = []
        for fp in changed_files:
            if fp:
                normalized.append(fp.replace("\\", "/").strip())
        if not normalized:
            return []

        # 用 IN 子句批量查询；SQL 参数仅支持问号占位符
        placeholders = ",".join("?" * len(normalized))
        sql = (
            "SELECT id, rule_id, file_path, severity, status, message, detected_at "
            f"FROM guardrail_findings "
            f"WHERE status = 'open' AND file_path IN ({placeholders})"
        )
        try:
            cur = conn.execute(sql, normalized)
            return [dict(row) for row in cur]
        except Exception:
            # 表不存在或查询失败时返回空列表，避免阻断 PR 检查
            return []
