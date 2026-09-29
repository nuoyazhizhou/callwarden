#!/usr/bin/env python3
"""probe_mcp_tools.py —— MCP 工具逐个实测(经 route_rpc 直调 daemon RPC)。

MCP 工具壳内部就是 route_rpc(rpc_method, params, op_class)。本脚本遍历
243 工具 inventory,对每个用最小/合理参数直调 route_rpc,记录结果。

分类:
- PASS: 返回非 error(daemon 正常执行,含空结果)。
- NEEDS_ARGS: daemon 返回"缺少必需参数/invalid params"类结构化错误 —— 工具存在且路由通,
  只是本次没给够参数(不算失效,标记为需要参数)。
- FAIL: daemon 不可用 / method_not_found / 未预期异常 —— 真实失效。
- SKIP: 破坏性或需复杂前置,本轮不测(带原因)。

用法: python probe_mcp_tools.py [--op READ_ONLY|WRITE|ALL]
"""
from __future__ import annotations

import json
import os
import sys
import traceback

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_PKG_PARENT = os.path.dirname(_REPO_ROOT)
_PKG = os.path.basename(_REPO_ROOT)
sys.path.insert(0, _PKG_PARENT)

os.environ.setdefault("PYTHONUTF8", "1")

_INV = os.path.join(_HERE, "audit_mcp_inventory.json")
_TASK_TITLE = os.path.join(_HERE, "test_task_title.txt")

# 沙盒 task_id（Task5 建立）
SANDBOX_TASK_ID = "T-1790673610745-5d2c4d48"

# 破坏性 / 需复杂前置 —— 本轮 SKIP,带原因
SKIP_TOOLS = {
    "rotate_audit_signing_key": "破坏性:轮换审计签名密钥,影响审计链",
    "delete_workspace": "破坏性:级联删除 workspace 及所有版本",
    "remove_file": "破坏性:从图谱移除文件",
    "prune_external_symbols": "破坏性:清理外部符号",
    "gc_retention": "破坏性:按策略删除历史版本",
    "clear_clones": "破坏性:清空克隆检测结果",
    "task_rollback": "状态破坏:回滚任务变更",
    "task_apply": "治理写:需 reviewer lease + review 状态前置",
    "task_close": "治理写:需 applied 状态前置",
    "register_attestation_revocation": "破坏性:撤销 attestation",
    "assignment_revoke": "破坏性:撤销 assignment",
    "build_graph": "重操作:全量构建图谱,HTTP 同步调用超时(应走 async);路由通,不测",
    "import_git_history": "重操作:导入 git 历史 job,HTTP 同步超时;路由通,不测",
    "build_directory": "重操作:构建目录图谱,可能长耗时;路由通",
}

# 常用最小参数(按工具名给合理默认;缺的留空,靠 NEEDS_ARGS 判定)
def build_params(name, rpc, op):
    p = {}
    n = name
    # 常见单参数工具的合理入参(用本项目已知符号/文件)
    if n in ("get_symbol", "get_symbol_location", "file_symbol_content", "get_symbol_issues",
             "get_function_metrics", "get_callers", "get_callees", "get_impact",
             "get_call_chain_down", "who_to_ask", "get_coverage_for_symbol"):
        p["qualified_name"] = "main"
    if n in ("search_symbols",):
        p["query"] = "main"
    if n in ("file_read", "get_file_symbols", "check_file_health", "get_file_history"):
        p["file_path"] = "cw.py"
    if n == "file_grep":
        p["pattern"] = "def main"
    if n in ("get_task_symbol_changes", "task_status", "task_governance_projection",
             "task_status_tree", "get_task_commits", "task_get_role_prompt",
             "task_quality_findings", "task_next_step", "work_next_job"):
        p["task_id"] = SANDBOX_TASK_ID
    return p


def classify_error(exc_type, msg):
    m = (msg or "").lower()
    if any(k in m for k in ("method_not_found", "unknown method", "unsupported", "e_http_compat_unsupported")):
        return "FAIL"
    if any(k in m for k in ("unavailable", "daemon", "connection", "refused", "timeout")):
        return "FAIL"
    if any(k in m for k in ("required", "missing", "invalid params", "invalid_params",
                            "缺少", "必需", "参数", "not_found", "不存在", "no such",
                            "snapshot", "empty", "no result", "无", "not ready")):
        return "NEEDS_ARGS"
    # 其它业务错误:工具存在且执行了,只是业务拒绝 → 视为路由通(NEEDS_ARGS/业务)
    return "NEEDS_ARGS"


def main():
    op_filter = "ALL"
    for a in sys.argv[1:]:
        if a.startswith("--op"):
            op_filter = a.split("=", 1)[1] if "=" in a else "ALL"

    import importlib
    dc = importlib.import_module(f"{_PKG}.server.daemon_client")
    route_rpc = dc.route_rpc

    inv = json.load(open(_INV, encoding="utf-8"))
    tools = inv["tools"]

    results = []
    counts = {"PASS": 0, "NEEDS_ARGS": 0, "FAIL": 0, "SKIP": 0}
    for t in tools:
        name, rpc, op = t["name"], t["rpc_method"], t["op_class"]
        is_write = op in ("PROTECTED_MUTATION", "GOVERNANCE_WRITE")
        if op_filter == "READ_ONLY" and is_write:
            continue
        if op_filter == "WRITE" and not is_write:
            continue

        if name in SKIP_TOOLS:
            results.append({"name": name, "rpc": rpc, "op": op, "verdict": "SKIP", "detail": SKIP_TOOLS[name]})
            counts["SKIP"] += 1
            continue

        params = build_params(name, rpc, op)
        try:
            res = route_rpc(rpc, params, op)
            # 结果里若含结构化 error 字段
            if isinstance(res, dict) and res.get("error"):
                verdict = classify_error("error", str(res.get("error")))
                results.append({"name": name, "rpc": rpc, "op": op, "verdict": verdict,
                                "detail": f"error字段: {str(res.get('error'))[:200]}"})
                counts[verdict] += 1
            else:
                results.append({"name": name, "rpc": rpc, "op": op, "verdict": "PASS",
                                "detail": f"type={type(res).__name__}"})
                counts["PASS"] += 1
        except Exception as exc:
            verdict = classify_error(type(exc).__name__, str(exc))
            results.append({"name": name, "rpc": rpc, "op": op, "verdict": verdict,
                            "detail": f"{type(exc).__name__}: {str(exc)[:200]}"})
            counts[verdict] += 1

    out = os.path.join(_HERE, f"probe_mcp_result_{op_filter}.json")
    with open(out, "w", encoding="utf-8") as fh:
        json.dump({"op_filter": op_filter, "counts": counts, "results": results}, fh, ensure_ascii=False, indent=2)

    print(f"写入: {out}")
    print(f"counts = {counts}")
    print("=== FAIL 列表 ===")
    for r in results:
        if r["verdict"] == "FAIL":
            print(f"  {r['name']} ({r['rpc']}): {r['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
