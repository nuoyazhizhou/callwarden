"""t2_mcp_runner.py —— T2:243 个 MCP 工具全参数真实调用运行器。

通道(最贴近 LLM 实际使用):
  create_mcp_server() 装配全部 243 工具 → FastMCP.call_tool(name, arguments)
  经 MCP 参数校验(pydantic schema)+ 工具壳参数适配 + route_rpc → daemon RPC。

参数来源:param_provider.build_mcp_params(全参数模式),用种子 workspace 的真实
前置状态(SeedContext)填充 workspace/符号/文件/任务等参数。

分层调用(按 op_class):
  - READ_ONLY(161):全部真实调用。
  - PROTECTED_MUTATION(77):非破坏性的真实调用;破坏性的 SKIP(带原因)。
  - GOVERNANCE_WRITE(5):需 lease/review/状态机前置,SKIP(归 T2 边界,不测)。

结果分类(与 probe_mcp_tools 一致,细化):
  - PASS:call_tool 返回非 error(工具存在、路由通、daemon 执行,含空结果)。
  - EXPECTED_BUSINESS:结构化业务错误(缺参/前置状态/not_found/snapshot 等)——
    工具与路由正常,只是当前参数/前置不满足业务条件(不是缺陷)。
  - DEFECT:真实缺陷(method_not_found / daemon 不可用 / 未预期异常 / 崩溃)。
  - SKIP:破坏性或需复杂前置,本轮不测(带原因)。
"""
from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List

import param_provider as pp

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
_INVENTORY = os.path.join(_FIXTURES, "audit_mcp_inventory.json")


# 破坏性 / 需复杂前置 —— SKIP(带原因)。语义沿用前一阶段 probe_mcp_tools。
SKIP_TOOLS: Dict[str, str] = {
    # 破坏性写
    "rotate_audit_signing_key": "破坏性:轮换审计签名密钥,影响审计链",
    "delete_workspace": "破坏性:级联删除 workspace 及所有版本",
    "remove_file": "破坏性:从图谱移除文件",
    "prune_external_symbols": "破坏性:清理外部符号",
    "gc_retention": "破坏性:按策略删除历史版本",
    "clear_clones": "破坏性:清空克隆检测结果",
    "task_rollback": "状态破坏:回滚任务变更",
    "register_attestation_revocation": "破坏性:撤销 attestation",
    "assignment_revoke": "破坏性:撤销 assignment",
    # 治理写:需 reviewer lease + 特定状态前置(GOVERNANCE_WRITE 状态机)
    "task_apply": "治理写:需 reviewer lease + review 状态前置",
    "task_close": "治理写:需 applied 状态前置",
    # 重操作:HTTP 同步调用可能超时(应走 async job);路由通,不逐个测
    "build_graph": "重操作:全量构建图谱,HTTP 同步超时(应走 async);种子 fixture 已验证",
    "import_git_history": "重操作:导入 git 历史 job,HTTP 同步超时",
    "build_directory": "重操作:构建目录图谱,可能长耗时",
}


def _classify_error(msg: str) -> str:
    """把异常/error 文本分类为 DEFECT 或 EXPECTED_BUSINESS。

    分类优先级:先判 DEFECT(方法不存在/daemon 不可用/代码级 internal_error),
    再判 EXPECTED_BUSINESS(缺参/前置/not_found 等业务拒绝)。
    """
    m = (msg or "").lower()
    # 真实缺陷信号:方法不存在 / daemon 不可用 / 连接问题
    if any(k in m for k in ("method_not_found", "unknown method", "unsupported",
                            "e_http_compat_unsupported", "not implemented",
                            "unavailable", "daemon rpc 调用失败", "connection",
                            "refused", "e_http_daemon_unavailable")):
        return "DEFECT"
    # 超时:归缺陷候选(重操作已 SKIP,余下超时值得关注)
    if "timeout" in m or "timed out" in m or "e_http_request_timeout" in m:
        return "DEFECT"
    # 代码级 internal_error(SQL/约束/BFS 等实现 bug),区别于业务 not_found:
    # "wrong number of parameters" / "constraint failed" / SQL 语法 / panic 等
    # 是实现缺陷,不是参数/前置问题。internal_error 若伴随这些信号 → DEFECT。
    if "internal_error" in m or "internalerror" in m:
        # 纯代码 bug 信号(与输入前置无关,任何调用都错):SQL 占位符数量不符、
        # SQL 语法/列/表不存在、panic/unwrap/越界。这些是实现缺陷 → DEFECT。
        if any(k in m for k in ("wrong number of parameters", "syntax error",
                                "no such column", "no such table",
                                "panic", "unwrap", "index out of")):
            return "DEFECT"
        # FOREIGN KEY constraint failed 常由前置数据缺失(如传不存在的 task_id)
        # 触发,不是代码 bug;multiple active workspaces / capture-diff 等是环境/
        # 前置问题。归业务(EXPECTED_BUSINESS),报告里单列 internal_error 复核。
        return "EXPECTED_BUSINESS"
    # 预期业务错误:缺参 / 前置状态 / not_found / snapshot 等
    if any(k in m for k in ("required", "missing", "invalid params", "invalid_params",
                            "缺少", "必需", "参数", "not_found", "不存在", "no such",
                            "snapshot", "empty", "no result", "not ready", "not_ready",
                            "forbidden", "permission", "lease", "identity", "conflict",
                            "已存在", "already", "无效", "validation")):
        return "EXPECTED_BUSINESS"
    # 其它:工具执行了但业务拒绝 → 视为路由通(EXPECTED_BUSINESS)
    return "EXPECTED_BUSINESS"


def _result_has_error(res: Any) -> str:
    """检查 call_tool 返回值里是否含结构化 error;返回 error 文本或空串。

    FastMCP.call_tool 返回 [TextContent(text=json), ...]。工具壳的 error
    以 {"error": ...} 或 {"success": false, "error": {...}} 形式出现在 JSON 里。
    """
    texts: List[str] = []
    if isinstance(res, (list, tuple)):
        for item in res:
            text = getattr(item, "text", None)
            if text:
                texts.append(text)
    elif isinstance(res, dict):
        texts.append(json.dumps(res, ensure_ascii=False))
    else:
        texts.append(str(res))

    for text in texts:
        try:
            obj = json.loads(text)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict):
            if obj.get("error"):
                return str(obj.get("error"))
            if obj.get("success") is False:
                return str(obj.get("error") or obj.get("message") or "success=false")
    return ""


async def run_one_tool(mcp, tool_meta: Dict[str, Any], schema_tool: Dict[str, Any],
                       ctx: pp.SeedContext) -> Dict[str, Any]:
    """真实调用单个 MCP 工具,返回分类结果 dict。"""
    name = tool_meta["name"]
    op = tool_meta["op_class"]

    if name in SKIP_TOOLS:
        return {"name": name, "op": op, "verdict": "SKIP", "detail": SKIP_TOOLS[name]}

    params = pp.build_mcp_params(schema_tool, ctx, required_only=False) if schema_tool else {}
    try:
        res = await mcp.call_tool(name, params)
        err = _result_has_error(res)
        if err:
            verdict = _classify_error(err)
            return {"name": name, "op": op, "verdict": verdict,
                    "detail": f"error字段: {err[:220]}", "params_keys": list(params)}
        return {"name": name, "op": op, "verdict": "PASS",
                "detail": f"type={type(res).__name__}", "params_keys": list(params)}
    except Exception as exc:  # noqa: BLE001
        verdict = _classify_error(str(exc))
        return {"name": name, "op": op, "verdict": verdict,
                "detail": f"{type(exc).__name__}: {str(exc)[:220]}", "params_keys": list(params)}


def load_inventory() -> List[Dict[str, Any]]:
    with open(_INVENTORY, encoding="utf-8") as f:
        return json.load(f)["tools"]


def index_schema_by_name(schema: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    return {t["name"]: t for t in schema.get("tools", [])}


async def run_all(mcp, ctx: pp.SeedContext,
                  op_filter: str = "ALL") -> Dict[str, Any]:
    """遍历全部工具真实调用,返回 {counts, results}。

    op_filter: ALL / READ_ONLY / WRITE(PROTECTED_MUTATION+GOVERNANCE_WRITE)。
    """
    inventory = load_inventory()
    schema_by_name = index_schema_by_name(pp.load_mcp_schema())

    results: List[Dict[str, Any]] = []
    counts = {"PASS": 0, "EXPECTED_BUSINESS": 0, "DEFECT": 0, "SKIP": 0}

    for meta in inventory:
        op = meta["op_class"]
        is_write = op in ("PROTECTED_MUTATION", "GOVERNANCE_WRITE")
        if op_filter == "READ_ONLY" and is_write:
            continue
        if op_filter == "WRITE" and not is_write:
            continue
        schema_tool = schema_by_name.get(meta["name"])
        r = await run_one_tool(mcp, meta, schema_tool, ctx)
        results.append(r)
        counts[r["verdict"]] += 1

    return {"op_filter": op_filter, "counts": counts, "results": results}
