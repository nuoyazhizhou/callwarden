"""github_action.py
=================

GitHub Actions 集成入口。

在 CI 中由 workflow 调用 `cw cicd github-action` 触发，
完成以下工作：
1. 从环境变量读取 PR 上下文（base / head / workspace）
2. 创建 CodeGraphDB 实例
3. 运行 PRChecker.run_pr_check
4. 导出 SARIF 报告到 $GITHUB_WORKSPACE/callwarden_results.sarif
5. 若有阻断发现，打印错误并以 exit code 1 退出
"""

from __future__ import annotations

import os
import sys

from ..i18n import t


# SARIF 报告默认输出文件名
_SARIF_FILENAME = "callwarden_results.sarif"


def _env(key: str, default: str = "") -> str:
    """读取环境变量，缺失返回默认值"""
    val = os.environ.get(key)
    return val if val else default


def run_github_action() -> int:
    """GitHub Action 入口函数

    Returns:
        退出码：0 表示通过，1 表示有阻断发现或致命错误
    """
    # 1. 读取 PR 上下文环境变量
    base_ref = _env("GITHUB_BASE_REF", "main")
    head_ref = _env("GITHUB_HEAD_REF", "HEAD")
    workspace = _env("GITHUB_WORKSPACE", os.getcwd())

    print("=" * 60)
    print(t("cli.messages.github_action_title"))
    print("=" * 60)
    print(t("cli.messages.github_action_base_ref", ref=base_ref))
    print(t("cli.messages.github_action_head_ref", ref=head_ref))
    print(t("cli.messages.github_action_workspace", workspace=workspace))
    print("-" * 60)

    # 2. 创建 CodeGraphDB 实例（延迟导入，避免模块加载期副作用）
    try:
        from ..db import CodeGraphDB
        db = CodeGraphDB(workspace_root=workspace)
    except Exception as e:
        print(t("cli.messages.github_action_init_db_failed", error=e))
        return 1

    # 3. 运行 PR 检查
    from .pr_check import PRChecker
    checker = PRChecker(db)
    try:
        result = checker.run_pr_check(base_branch=base_ref, head=head_ref)
    except Exception as e:
        print(t("cli.messages.github_action_pr_check_failed", error=e))
        return 1

    # 4. 导出 SARIF 报告（run_pr_check 已生成 sarif_report，这里直接落盘）
    sarif_path = os.path.join(workspace, _SARIF_FILENAME)
    try:
        import json
        from ..config import atomic_write_file
        sarif_report = result.get("sarif_report", {})
        atomic_write_file(
            sarif_path,
            json.dumps(sarif_report, ensure_ascii=False, indent=2),
        )
        print(t("cli.messages.github_action_sarif_written", path=sarif_path))
    except Exception as e:
        print(t("cli.messages.github_action_sarif_write_failed", error=e))

    # 5. 打印摘要
    passed = bool(result.get("passed"))
    total = result.get("total_findings", 0)
    errors = result.get("errors", 0)
    warnings = result.get("warnings", 0)

    print("-" * 60)
    print(t("cli.messages.github_action_pr_summary"))
    print(t("cli.messages.github_action_pass_status", status='PASSED' if passed else 'BLOCKED'))
    print(t("cli.messages.github_action_total_findings", count=total))
    print(t("cli.messages.github_action_errors", count=errors))
    print(t("cli.messages.github_action_warnings", count=warnings))

    # 6. 阻断判定：未通过则打印错误并 exit 1
    if not passed:
        print("-" * 60)
        print(t("cli.messages.github_action_pr_blocked"))
        print("=" * 60)
        return 1

    print("=" * 60)
    return 0


def main() -> None:
    """命令行入口：供 `cw cicd github-action` 调用"""
    sys.exit(run_github_action())


if __name__ == "__main__":
    main()
