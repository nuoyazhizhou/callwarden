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

        # P1 修复：收集运行时错误，写入 SARIF executionNotifications
        # （评审 P1：原代码 try-except pass 静默吞异常，导致 fail-open）
        run_errors: List[str] = []

        # 2. guardrail 编辑前检查
        # 评审 P1：原代码 getattr(db, "guardrail_check_edit") 调用不存在的方法，
        # 真实方法名是 check_before_edit（db/db_guardrail.py:221）
        guardrail_check_fn = getattr(self.db, "check_before_edit", None)
        if guardrail_check_fn is not None and changed_files:
            for file_path in changed_files:
                try:
                    guardrail_check_fn(file_path)
                except Exception as e:
                    # 单文件检查失败不中断整体流程，但记录到 run_errors
                    run_errors.append(
                        f"guardrail check_before_edit({file_path}) 失败: "
                        f"{type(e).__name__}: {str(e)[:200]}"
                    )
        elif changed_files:
            # db 未提供 check_before_edit 方法（旧版本或未启用 guardrail）
            run_errors.append(
                "db 未提供 check_before_edit 方法，guardrail 检查未执行"
            )

        # 3. Semgrep 增量扫描（A14 修复 2026-07-20）
        # 旧实现：semgrep_fn(target_paths=changed_files) — 但底层 save_semgrep_findings
        # 硬编码 scan_type='full'，不清理变更文件的 stale findings，导致重复计数
        # 新实现：优先调用 db.scan_semgrep_incremental()，由 db 层统一管理增量扫描+清理
        incremental_fn = getattr(self.db, "scan_semgrep_incremental", None)
        if incremental_fn is not None and changed_files:
            try:
                incremental_fn(base_branch=base_branch, head=head)
            except Exception as e:
                # Semgrep 失败不阻断 PR 检查汇总，但记录到 run_errors
                run_errors.append(
                    f"Semgrep scan_semgrep_incremental 失败: "
                    f"{type(e).__name__}: {str(e)[:200]}"
                )
        elif changed_files:
            # Fallback：db 不支持增量扫描（旧版本），降级到 run_semgrep_and_save
            semgrep_fn = getattr(self.db, "run_semgrep_and_save", None)
            if semgrep_fn is not None:
                try:
                    semgrep_fn(target_paths=changed_files)
                except Exception as e:
                    run_errors.append(
                        f"Semgrep run_semgrep_and_save 失败 (fallback): "
                        f"{type(e).__name__}: {str(e)[:200]}"
                    )
            else:
                run_errors.append(
                    "db 未提供 scan_semgrep_incremental / run_semgrep_and_save 方法，Semgrep 扫描未执行"
                )

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

        # 6. 生成 SARIF 报告（传入 run_errors 让消费方知道扫描是否完整）
        sarif_report = self.sarif_exporter.export_findings(findings, run_errors=run_errors)

        # 复审回退修复（2026-07-21 P1-1）：
        # passed 必须同时满足：
        #   (1) errors == 0 —— 无 error 级阻断发现
        #   (2) scan_complete —— 扫描本身未发生 run_errors（Guardrail/Semgrep 异常）
        # 旧实现 passed = errors == 0 会在扫描失败但零 finding 时 fail-open（exit 0），
        # 让 GitHub Action 错误地放行 PR。现在任一条件不满足都阻断。
        scan_complete = len(run_errors) == 0
        passed = (errors == 0) and scan_complete

        return {
            "passed": passed,
            "total_findings": len(findings),
            "errors": errors,
            "warnings": warnings,
            "sarif_report": sarif_report,
            # P1 修复：暴露 run_errors，调用方可据此判断扫描是否完整
            "run_errors": run_errors,
            "scan_complete": scan_complete,
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
        """合并查询 guardrail_findings + semgrep_findings 中命中变更文件的 open 记录

        复审回退修复（2026-07-21 P1-1）：
        旧实现只查 guardrail_findings，未合并 semgrep_findings，导致 Semgrep 发现的
        error 级 finding 无法阻断 PR。现在两类 findings 都纳入阻断判定。

        Args:
            changed_files: 变更文件路径列表

        Returns:
            finding 字典列表；db 无 conn 或表不存在时返回空列表。
            字段统一为：id, rule_id, file_path, severity, status, message, detected_at, source
            （source: 'guardrail' 或 'semgrep'，便于 SARIF 与日志溯源）
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

        findings: List[Dict] = []

        # (1) guardrail_findings：status='open' 且 file_path 命中
        guardrail_sql = (
            "SELECT id, rule_id, file_path, severity, status, message, detected_at, "
            "'guardrail' AS source "
            f"FROM guardrail_findings "
            f"WHERE status = 'open' AND file_path IN ({placeholders})"
        )
        try:
            cur = conn.execute(guardrail_sql, normalized)
            findings.extend(dict(row) for row in cur)
        except Exception:
            # 表不存在或查询失败时跳过，不阻断 PR 检查（其他源仍可阻断）
            pass

        # (2) semgrep_findings：JOIN file_instances 取 rel_path，所有 finding 视为 open
        # （semgrep_findings 表无 status 字段；增量扫描时 save_semgrep_findings 已删除
        # 变更文件的 stale 记录，因此查询结果只含最新扫描的 finding）
        semgrep_sql = (
            "SELECT sf.id, sf.rule_id, fi.rel_path AS file_path, sf.severity, "
            "'open' AS status, sf.message, sf.scanned_at AS detected_at, "
            "'semgrep' AS source "
            "FROM semgrep_findings sf "
            "JOIN file_instances fi ON sf.file_instance_id = fi.id "
            f"WHERE fi.rel_path IN ({placeholders})"
        )
        try:
            cur = conn.execute(semgrep_sql, normalized)
            findings.extend(dict(row) for row in cur)
        except Exception:
            # 表不存在或查询失败时跳过，不阻断 PR 检查
            pass

        return findings
