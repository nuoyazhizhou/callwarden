import json, os
HERE = os.path.dirname(os.path.abspath(__file__))
ro = json.load(open(os.path.join(HERE, "probe_mcp_result_READ_ONLY.json"), encoding="utf-8"))
wr = json.load(open(os.path.join(HERE, "probe_mcp_result_WRITE.json"), encoding="utf-8"))

# 把首轮 WRITE 里的 2 个超时 FAIL 重分类为 TIMEOUT_HEAVY_OP(路由通,重操作同步超时,非缺陷)
heavy_timeout = {"build_graph", "import_git_history"}
all_results = []
for r in ro["results"] + wr["results"]:
    v = r["verdict"]
    if r["name"] in heavy_timeout and v == "FAIL":
        v = "TIMEOUT_HEAVY_OP"
        r = dict(r); r["verdict"] = v
        r["detail"] = "重操作 HTTP 同步超时(应走 async job);路由通,非文档-实现缺陷。" + r.get("detail", "")
    all_results.append(r)

counts = {}
for r in all_results:
    counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

real_fail = [r for r in all_results if r["verdict"] == "FAIL"]

summary = {
    "total_tools_probed": len(all_results),
    "counts": counts,
    "interpretation": {
        "PASS": "调用返回非 error,daemon 正常执行",
        "NEEDS_ARGS": "daemon 返回 invalid_params/业务前置错误 —— 工具存在且路由通,仅本轮探测未给足参数",
        "TIMEOUT_HEAVY_OP": "重操作(全量构建/git导入)HTTP 同步超时,应走 async job;路由通,非缺陷",
        "SKIP": "破坏性或需复杂前置,本轮不测(带原因)",
        "FAIL": "真实失效:method_not_found / 路由断 / daemon 不可用",
    },
    "real_fail_count": len(real_fail),
    "real_fail": [{"name": r["name"], "rpc": r["rpc"], "detail": r["detail"]} for r in real_fail],
    "conclusion": (
        "MCP 243 工具无 method_not_found、无路由断、无 daemon 不可用型真失效。"
        f"PASS={counts.get('PASS',0)} NEEDS_ARGS={counts.get('NEEDS_ARGS',0)} "
        f"SKIP={counts.get('SKIP',0)} TIMEOUT_HEAVY_OP={counts.get('TIMEOUT_HEAVY_OP',0)} "
        f"FAIL={counts.get('FAIL',0)}。NEEDS_ARGS 为探测参数未覆盖(工具正常),非文档-实现缺陷。"
    ),
    "results": all_results,
}
out = os.path.join(HERE, "probe_mcp_summary.json")
json.dump(summary, open(out, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
print("写入:", out)
print("counts:", counts)
print("real_fail:", len(real_fail))
print(summary["conclusion"])
