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
    print("Code Graph Analysis - GitHub Action")
    print("=" * 60)
    print(f"  base ref : {base_ref}")
    print(f"  head ref : {head_ref}")
    print(f"  workspace: {workspace}")
    print("-" * 60)

    # 2. 创建 CodeGraphDB 实例（延迟导入，避免模块加载期副作用）
    try:
        from ..db import CodeGraphDB
        db = CodeGraphDB(workspace_root=workspace)
    except Exception as e:
        print(f"[FATAL] 初始化 CodeGraphDB 失败: {e}")
        return 1

    # 3. 运行 PR 检查
    from .pr_check import PRChecker
    checker = PRChecker(db)
    try:
        result = checker.run_pr_check(base_branch=base_ref, head=head_ref)
    except Exception as e:
        print(f"[FATAL] PR 检查异常: {e}")
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
        print(f"[SARIF] 报告已写入: {sarif_path}")
    except Exception as e:
        print(f"[WARN] 写入 SARIF 报告失败: {e}")

    # 5. 打印摘要
    passed = bool(result.get("passed"))
    total = result.get("total_findings", 0)
    errors = result.get("errors", 0)
    warnings = result.get("warnings", 0)

    print("-" * 60)
    print("PR 检查摘要:")
    print(f"  通过状态    : {'PASSED' if passed else 'BLOCKED'}")
    print(f"  总发现数    : {total}")
    print(f"  错误(error) : {errors}")
    print(f"  告警(warn)  : {warnings}")

    # 6. 阻断判定：未通过则打印错误并 exit 1
    if not passed:
        print("-" * 60)
        print("[ERROR] PR 被阻断：存在 error 级发现，请修复后再合并。")
        print("=" * 60)
        return 1

    print("=" * 60)
    return 0


def main() -> None:
    """命令行入口：供 `cw cicd github-action` 调用"""
    sys.exit(run_github_action())


if __name__ == "__main__":
    main()
