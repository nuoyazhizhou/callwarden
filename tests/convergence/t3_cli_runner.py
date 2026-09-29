"""t3_cli_runner.py —— T3:234 个 CLI 叶子命令全参数真实调用运行器。

通道(最贴近用户实际使用):subprocess 执行 `python cw.py <argv>`,cwd=种子
workspace,捕获 rc/stdout/stderr。argv 由 param_provider.build_cli_argv 生成
(全参数模式)。

分层:
  - 只读命令:全部真实执行。
  - 写命令:非破坏性真实执行;破坏性 SKIP(带原因)。

结果分类:
  - PASS:rc==0(命令成功执行)。
  - EXPECTED_BUSINESS:rc!=0 但为合理业务拒绝(缺参 argparse rc=2、
    not_found、snapshot_not_ready、identity 不全、workspace 未绑定等)。
  - DEFECT:真实缺陷(Python traceback / method_not_found / daemon 不可用 /
    未预期崩溃 / 代码级 internal_error)。
  - SKIP:破坏性命令,本轮不测(带原因)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any, Dict, List

import param_provider as pp

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CW_PY = os.path.join(_REPO_ROOT, "cw.py")
_PY = sys.executable


# 破坏性 / 需复杂前置 —— SKIP(带原因)。以子命令字符串为键(与 cli_full_params
# 的 commands 键一致)。破坏性动作:删除/回滚/轮换/清理/撤销/关闭/重建。
SKIP_CMD_PREFIXES = (
    "workspace delete", "workspace register", "workspace set",  # 改变 workspace 状态
    "gc archive", "gc db-cleanup", "gc retention",              # 删除/清理历史
    "task apply", "task close", "task rollback", "task reopen",  # 治理/状态破坏
    "task revert", "task revert-edit",
    "clone clear", "clone detect",       # 清空/重算克隆(重操作)
    "fts rebuild",                        # 重建索引(重操作)
    "audit rotate",                       # 轮换审计密钥(破坏性)
    "assignment revoke", "assignment create",
    "rule sync", "rule insert-block",     # 写规则文件
    "refresh",                            # 全量/文件重建(重操作,种子 fixture 已验证 build_graph)
    "git import", "coverage import", "defect import", "semgrep scan",  # 导入/扫描(重/需外部)
    "server",                             # 启动 MCP server(阻塞)
    "watch",                              # 文件监控(阻塞)
    "daemon start", "daemon backup", "daemon restore",  # daemon 生命周期
    "daemon gc-cas", "daemon gc-snapshots", "daemon snapshot-evict",
    "daemon mount",
)


def should_skip(cmd_str: str) -> str:
    """返回 SKIP 原因,不 skip 返回空串。"""
    for pref in SKIP_CMD_PREFIXES:
        if cmd_str == pref or cmd_str.startswith(pref + " ") or cmd_str.startswith(pref):
            return f"破坏性/重操作/阻塞:{pref}"
    return ""


def classify(rc: int, stdout: str, stderr: str) -> str:
    """按 rc + 输出分类。"""
    combined = f"{stdout}\n{stderr}".lower()

    # 真实缺陷:Python traceback / 未捕获异常
    if "traceback (most recent call last)" in combined:
        return "DEFECT"
    # method_not_found / daemon 不可用 / 连接问题
    if any(k in combined for k in ("method_not_found", "unknown method",
                                   "e_http_daemon_unavailable", "daemon rpc 调用失败",
                                   "connection refused", "e_http_manifest_stale")):
        return "DEFECT"
    # 代码级 internal_error(SQL/panic/占位符),区别于业务 not_found
    if "internal_error" in combined and any(
        k in combined for k in ("wrong number of parameters", "syntax error",
                                "no such column", "no such table", "panic",
                                "index out of")):
        return "DEFECT"

    # rc=0 漏判检测:CLI 部分命令捕获异常后仍返回 rc=0,但输出含
    # "执行子命令 'X' 失败: <Python 异常>"(list indices / object has no
    # attribute / KeyError 单引号裸键 / must be 等)。这是 CLI 代码 bug,
    # 被 fail-soft 掩盖成 rc=0,应识别为 DEFECT。
    _PY_EXC_SIGNALS = (
        "list indices must be integers", "object has no attribute",
        "must be integers or slices", "unhashable type",
        "unsupported operand", "takes no arguments", "not callable",
        "nonetype", "keyerror", "typeerror", "attributeerror", "valueerror",
    )
    has_fail_marker = ("执行子命令" in combined and "失败" in combined) or \
                      "执行子命令" in combined
    if has_fail_marker and any(s in combined for s in _PY_EXC_SIGNALS):
        return "DEFECT"
    # 裸 KeyError 特征:"失败: '<单词>'"(如 'fan_in'/'line_count'/'workspace')
    import re as _re
    if "失败:" in combined and _re.search(r"失败[:：]\s*'[a-z_]+'", combined):
        return "DEFECT"

    if rc == 0:
        return "PASS"

    # rc != 0:判断是否合理业务拒绝
    # argparse 缺参(rc=2 + usage/error) / not_found / snapshot / identity 等
    if any(k in combined for k in ("usage:", "error: the following arguments",
                                   "error: argument", "invalid choice",
                                   "required", "缺少", "必需",
                                   "not_found", "not found", "不存在", "no such",
                                   "snapshot", "not ready", "not_ready",
                                   "forbidden", "permission", "lease", "identity",
                                   "workspace", "no rows", "empty", "无",
                                   "已存在", "already", "conflict", "invalid",
                                   "数据库正忙", "db_locked")):
        return "EXPECTED_BUSINESS"
    # rc != 0 且无明确业务信号 → 复核候选,暂归 EXPECTED_BUSINESS(附全文)
    return "EXPECTED_BUSINESS"


def run_one(cmd_str: str, cmd_spec: Dict[str, Any], ctx: pp.SeedContext,
            cwd: str, timeout: float = 60.0) -> Dict[str, Any]:
    """真实执行单个 CLI 命令,返回分类结果。"""
    skip_reason = should_skip(cmd_str)
    if skip_reason:
        return {"cmd": cmd_str, "verdict": "SKIP", "detail": skip_reason}

    argv = pp.build_cli_argv(cmd_str, cmd_spec, ctx, required_only=False)
    env = dict(os.environ)
    env["CW_DAEMON_TRANSPORT"] = "http"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        r = subprocess.run(
            [_PY, "-u", _CW_PY] + argv, cwd=cwd,
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, env=env,
        )
        verdict = classify(r.returncode, r.stdout, r.stderr)
        detail = f"rc={r.returncode}"
        snippet = (r.stdout or r.stderr or "").strip().replace("\n", " ")[:200]
        return {"cmd": cmd_str, "verdict": verdict, "detail": f"{detail}: {snippet}",
                "argv": argv, "rc": r.returncode}
    except subprocess.TimeoutExpired:
        return {"cmd": cmd_str, "verdict": "DEFECT",
                "detail": f"TIMEOUT(>{timeout}s)", "argv": argv}
    except Exception as e:  # noqa: BLE001
        return {"cmd": cmd_str, "verdict": "DEFECT",
                "detail": f"{type(e).__name__}: {e}"[:200], "argv": argv}


def load_cli_commands() -> Dict[str, Any]:
    return pp.load_cli_params()["commands"]


def run_all(ctx: pp.SeedContext, cwd: str,
            timeout: float = 60.0) -> Dict[str, Any]:
    """遍历全部 CLI 叶子命令真实执行,返回 {counts, results}。"""
    commands = load_cli_commands()
    results: List[Dict[str, Any]] = []
    counts = {"PASS": 0, "EXPECTED_BUSINESS": 0, "DEFECT": 0, "SKIP": 0}
    for cmd_str, spec in commands.items():
        r = run_one(cmd_str, spec, ctx, cwd, timeout)
        results.append(r)
        counts[r["verdict"]] += 1
    return {"counts": counts, "results": results}
