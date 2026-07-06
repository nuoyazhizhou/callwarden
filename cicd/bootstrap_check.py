"""bootstrap_check.py
=====================

CI 门禁脚本：聚合 bootstrap_status 健康摘要，决定是否阻断合并。

调用 `db.bootstrap_status()` 获取自举闭环健康度，检查以下关键指标：
1. db_stale=True → 失败（数据库滞后于当前 HEAD）
2. blocking_findings_count > 0 → 失败（有阻塞级质量发现）
3. audit_verify.broken_count > 0 → 失败（审计链有损坏记录）

退出码：
- 0：通过（所有检查项 OK）
- 1：失败（有任一检查项未通过）

使用方式：
    # 直接运行（Python API）
    python -m callwarden.cicd.bootstrap_check

    # 在 CI 脚本中使用
    python -m callwarden.cicd.bootstrap_check && echo "PASS" || echo "FAIL"

    # 作为库调用
    from callwarden.cicd.bootstrap_check import run_bootstrap_gate
    result = run_bootstrap_gate(db)
    if not result["passed"]:
        print(result["reason"])
        sys.exit(1)
"""

from __future__ import annotations

import sys
from typing import Any, Dict, Optional

from ..db import CodeGraphDB
from ..i18n import t


def run_bootstrap_gate(db) -> Dict[str, Any]:
    """运行 bootstrap 门禁检查

    聚合 db.bootstrap_status() 的关键指标，判断当前自举闭环是否健康。

    检查项（任一失败即阻断）：
    1. db_stale=True：数据库滞后于当前 HEAD（需先 cw --refresh-all）
    2. blocking_findings_count > 0：有阻塞级质量发现（需先修复）
    3. audit_verify.broken_count > 0：审计链有损坏记录（需先修复）

    Args:
        db: CodeGraphDB 实例

    Returns:
        dict 包含：
        - passed: bool（是否通过所有检查）
        - reason: str（失败原因，通过时为空串）
        - failed_checks: list[str]（失败的检查项名称）
        - status: dict（bootstrap_status() 返回的完整摘要）
    """
    # 获取 bootstrap_status 摘要
    try:
        status = db.bootstrap_status()
    except Exception as exc:
        return {
            "passed": False,
            "reason": t(
                "cli.messages.bootstrap_gate_status_failed",
                error=str(exc),
                default=f"Failed to get bootstrap_status: {exc}",
            ),
            "failed_checks": ["status_query"],
            "status": {},
        }

    failed_checks: list = []

    # 检查 1: db_stale
    db_stale = status.get("db_stale", False)
    if db_stale:
        failed_checks.append("db_stale")

    # 检查 2: blocking_findings_count
    blocking_count = status.get("blocking_findings_count", 0)
    if blocking_count > 0:
        failed_checks.append("blocking_findings")

    # 检查 3: audit_verify.broken_count
    audit_verify = status.get("audit_verify", {})
    broken_count = audit_verify.get("broken_count", 0) if isinstance(audit_verify, dict) else 0
    if broken_count > 0:
        failed_checks.append("audit_broken")

    # 构造失败原因
    if failed_checks:
        reason = t(
            "cli.messages.bootstrap_gate_failed",
            checks=", ".join(failed_checks),
            default=f"Bootstrap gate failed: {', '.join(failed_checks)}",
        )
    else:
        reason = ""

    return {
        "passed": len(failed_checks) == 0,
        "reason": reason,
        "failed_checks": failed_checks,
        "status": status,
    }


def _print_gate_result(result: Dict[str, Any]) -> None:
    """打印门禁检查结果到 stdout

    Args:
        result: run_bootstrap_gate() 返回的结果字典
    """
    if result["passed"]:
        print(t(
            "cli.messages.bootstrap_gate_passed",
            default="[Bootstrap Gate] PASS: all checks passed",
        ))
        status = result.get("status", {})
        active_rules = status.get("active_rules_count", 0)
        open_findings = status.get("open_findings_count", 0)
        audit = status.get("audit_verify", {})
        verified = audit.get("verified_count", 0)
        total = audit.get("total_count", 0)
        print(t(
            "cli.messages.bootstrap_gate_summary",
            active_rules=active_rules,
            open_findings=open_findings,
            verified=verified,
            total=total,
            default=f"  active_rules={active_rules}, open_findings={open_findings}, audit_verified={verified}/{total}",
        ))
    else:
        print(t(
            "cli.messages.bootstrap_gate_failed_title",
            default="[Bootstrap Gate] FAIL",
        ))
        print(f"  {result['reason']}")
        status = result.get("status", {})
        if status:
            db_stale = status.get("db_stale", False)
            blocking = status.get("blocking_findings_count", 0)
            audit = status.get("audit_verify", {})
            broken = audit.get("broken_count", 0)
            print(t(
                "cli.messages.bootstrap_gate_details",
                db_stale=db_stale,
                blocking=blocking,
                broken=broken,
                default=f"  db_stale={db_stale}, blocking_findings={blocking}, audit_broken={broken}",
            ))
            recommended = status.get("recommended_next_action", "")
            if recommended:
                print(t(
                    "cli.messages.bootstrap_gate_recommended",
                    action=recommended,
                    default=f"  Recommended: {recommended}",
                ))


def main() -> int:
    """CI 门禁入口：运行检查并返回退出码

    Returns:
        0：通过（所有检查项 OK）
        1：失败（有任一检查项未通过或查询异常）
    """
    db = CodeGraphDB()
    try:
        result = run_bootstrap_gate(db)
        _print_gate_result(result)
        return 0 if result["passed"] else 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
