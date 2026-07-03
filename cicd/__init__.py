"""cicd
====

Code Graph 的 CI/CD 集成子包。

提供以下能力：
- SARIF 报告导出（sarif_exporter.py）：将 Semgrep / guardrail findings 导出为 SARIF 2.1.0
- 增量分析（incremental.py）：基于 git diff 只分析变更文件
- PR 阻断检查（pr_check.py）：汇总 guardrail + Semgrep 结果，决定是否阻断 PR
- GitHub Action 入口（github_action.py）：在 CI 中运行的统一入口
"""

__all__ = ["SarifExporter", "IncrementalAnalyzer", "PRChecker"]
