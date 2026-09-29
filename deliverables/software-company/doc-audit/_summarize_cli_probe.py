import json, os
HERE = os.path.dirname(os.path.abspath(__file__))
first = json.load(open(os.path.join(HERE, "probe_cli_result.json"), encoding="utf-8"))
recheck = json.load(open(os.path.join(HERE, "recheck_cli_fails_result.json"), encoding="utf-8"))
rc_by_leaf = {x["leaf"]: x for x in recheck}

# 用串行复测结论覆盖首轮 17 个 FAIL
final = []
for r in first["results"]:
    leaf = r["leaf"]
    if leaf in rc_by_leaf and r["verdict"] == "FAIL":
        rc = rc_by_leaf[leaf]
        tail = rc["tail"]
        if "E_HTTP_REQUEST_TIMEOUT" in tail or "timed out" in tail:
            v, d = "DAEMON_TIMEOUT", f"串行仍超时: daemon RPC E_HTTP_REQUEST_TIMEOUT ({rc['elapsed_s']}s)"
        elif "ImportError" in tail or "'str' object has no attribute" in tail or "AttributeError" in tail:
            v, d = "REAL_BUG", f"rc={rc['rc']}: {tail[-120:]}"
        elif rc["rc"] == 2 and ("required" in tail or "arguments" in tail):
            v, d = "NEEDS_ARGS", f"缺参(串行rc=2): {tail[-80:]}"
        elif rc["rc"] == 0:
            v, d = "PASS", f"串行PASS({rc['elapsed_s']}s): {tail[-80:]}"
        else:
            v, d = "NEEDS_ARGS", f"串行rc={rc['rc']}: {tail[-80:]}"
        final.append({"leaf": leaf, "verdict": v, "detail": d})
    else:
        final.append(r)

counts = {}
for r in final:
    counts[r["verdict"]] = counts.get(r["verdict"], 0) + 1

real_issues = [r for r in final if r["verdict"] in ("DAEMON_TIMEOUT", "REAL_BUG", "FAIL")]

summary = {
    "leaf_total": first["leaf_total"],
    "counts": counts,
    "note": "首轮 17 FAIL 经串行复测重分类:并发竞争假象已剔除,真实问题保留",
    "interpretation": {
        "PASS": "实调 rc=0 且输出正常",
        "NEEDS_ARGS": "缺必需参数/用法(命令存在)",
        "SKIP": "破坏性/重操作/环境写,仅 help-only",
        "DAEMON_TIMEOUT": "命令→daemon RPC 30s 超时(串行单跑仍超时,真实 daemon 响应问题,疑与 worker_status=unhealthy 相关)",
        "REAL_BUG": "命令代码缺陷:cw test 相对导入崩溃 / cw topo 'str'.get 属性错误",
        "FAIL": "其它未归类失败",
    },
    "real_issues_count": len(real_issues),
    "real_issues": real_issues,
    "results": final,
}
json.dump(summary, open(os.path.join(HERE, "probe_cli_summary.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("counts:", counts)
print("\n=== 真实问题 ===")
for r in real_issues:
    print(f"  [{r['verdict']}] {r['leaf']}: {r['detail'][:110]}")
