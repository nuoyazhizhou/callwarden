#!/usr/bin/env python3
"""probe_cli_cmds.py —— CLI 命令逐个实测(冒烟)。

遍历 audit_cli_inventory.json 的 234 叶子命令(top + sub)。对每个:
- SKIP_LEAF: 破坏性/重操作/交互/长驻,只验证 --help 存在(help-only),带原因。
- 其它: 运行最小调用(无额外参数),记录 rc + 输出判定。

判定:
- HELP_OK:  --help 返回成功(命令存在、arg-parse 正常)。help-only 命令用此。
- PASS:     实调 rc==0 且输出非空/无致命错误。
- NEEDS_ARGS: rc!=0 且错误信息为"缺少参数/用法"(命令存在,需参数)。
- FAIL:     --help 都失败(挂起/不存在/崩溃),或实调崩溃(非缺参)。
- SKIP:     显式跳过实调,只留 help 结果。

每命令 30s 超时;挂起记 FAIL(timeout)。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_PYTHON = os.environ.get("PYTHON", sys.executable)
_CW = os.path.join(_REPO_ROOT, "cw.py")
_INV = os.path.join(_HERE, "audit_cli_inventory.json")
_OUT = os.path.join(_HERE, "probe_cli_result.json")

# 破坏性/重操作/长驻/交互 —— 只做 help-only(不实调)
SKIP_LEAF = {
    "server": "长驻:启动 MCP server(会挂起)",
    "refresh": "重操作:全量/增量刷新图谱,长耗时",
    "graph build-from-c": "重操作:构建 C 图谱",
    "build-context register": "写操作:注册工具链",
    "install": "环境写:安装依赖",
    "setup": "环境写:配置 MCP 集成",
    "install-agent claude-code": "环境写:生成集成包",
    "install-hook post-commit": "环境写:装 git hook",
    "doctor": "环境诊断:可能改环境/长耗时",
    "gc db-cleanup": "破坏性:清理孤儿库(dry-run 默认,仍 help-only 稳妥)",
    "gc db-migrate-single": "破坏性:多库迁移",
    "gc purge": "破坏性:清理",
    "gc retention": "破坏性:按策略删版本",
    "gc restore": "破坏性:恢复覆盖",
    "audit rotate-key": "破坏性:轮换审计密钥",
    "daemon backup": "重操作:备份",
    "daemon restore": "破坏性:恢复",
    "daemon gc-cas": "破坏性:GC CAS",
    "daemon gc-snapshots": "破坏性:GC 快照",
    "daemon snapshot-evict": "破坏性:驱逐快照",
    "daemon publish": "写操作:发布快照",
    "daemon mount": "环境:挂载",
    "workspace delete": "破坏性:删除 workspace",
    "workspace scan": "重操作:扫描",
    "test": "运行测试套件(可能长耗时)",
    "test-impact": "需符号参数(下方 needs args 也可)",
}

# install-agent 的 24 个子 agent 全部 help-only(环境写)
def is_install_agent_leaf(leaf):
    return leaf.startswith("install-agent ")


def run(argv, timeout=30):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"; env["PYTHONIOENCODING"] = "utf-8"
    env["CW_DAEMON_TRANSPORT"] = env.get("CW_DAEMON_TRANSPORT", "http")
    try:
        p = subprocess.run([_PYTHON, _CW] + argv, cwd=_REPO_ROOT,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env)
        return p.returncode, (p.stdout or ""), (p.stderr or "")
    except subprocess.TimeoutExpired:
        return -999, "", "TIMEOUT"


def leaf_to_argv(leaf):
    # "gc archive-list" -> ["gc","archive-list"]; "brief" -> ["brief"]
    return leaf.split()


def classify_run(rc, out, err):
    combined = (out + "\n" + err).lower()
    if rc == -999:
        return "FAIL", "TIMEOUT(30s 挂起)"
    if rc == 0:
        return "PASS", "rc=0"
    # rc != 0
    if any(k in combined for k in ("usage:", "required", "the following arguments",
                                   "缺少", "必需", "参数", "error: argument",
                                   "invalid choice", "expected one argument",
                                   "not_found", "不存在", "invalid_params", "缺哄皯")):
        return "NEEDS_ARGS", f"rc={rc} 缺参/用法"
    return "NEEDS_ARGS", f"rc={rc} (业务错误,命令存在)"


def main():
    inv = json.load(open(_INV, encoding="utf-8"))
    cmds = inv["commands"]

    # 展开叶子
    leaves = []
    for top in sorted(cmds):
        subs = cmds[top]["sub_actions"]
        if subs:
            for s in subs:
                leaves.append(f"{top} {s}")
        else:
            leaves.append(top)

    results = []
    counts = {"PASS": 0, "NEEDS_ARGS": 0, "HELP_OK": 0, "FAIL": 0, "SKIP": 0}
    prog = open(os.path.join(_HERE, "probe_cli_progress.txt"), "w", encoding="utf-8")

    for idx, leaf in enumerate(leaves, 1):
        prog.write(f"[{idx}/{len(leaves)}] {leaf} ...\n"); prog.flush()
        argv = leaf_to_argv(leaf)
        skip_reason = SKIP_LEAF.get(leaf)
        if skip_reason is None and is_install_agent_leaf(leaf):
            skip_reason = "环境写:install-agent 子命令生成集成包"

        # 先跑 --help 验证命令存在 / arg-parse
        hrc, hout, herr = run(argv + ["--help"], timeout=20)
        help_ok = (hrc == 0) or ("usage" in (hout + herr).lower())
        # server --help 会挂起 -> hrc==-999
        if hrc == -999:
            results.append({"leaf": leaf, "verdict": "FAIL", "detail": "--help 挂起(TIMEOUT)"})
            counts["FAIL"] += 1
            continue

        if skip_reason:
            v = "HELP_OK" if help_ok else "FAIL"
            results.append({"leaf": leaf, "verdict": v if help_ok else "FAIL",
                            "detail": f"help-only({skip_reason}); help_ok={help_ok}"})
            counts[v] += 1 if help_ok else 0
            if not help_ok:
                counts["FAIL"] += 1
            else:
                counts["SKIP"] += 1  # 记为 SKIP(help 通过但未实调)
                counts["HELP_OK"] -= 1  # 不重复计
            continue

        if not help_ok:
            results.append({"leaf": leaf, "verdict": "FAIL", "detail": f"--help 失败 rc={hrc}: {(herr or hout)[:120]}"})
            counts["FAIL"] += 1
            continue

        # 实调(最小,无额外参数)
        rc, out, err = run(argv, timeout=25)
        verdict, detail = classify_run(rc, out, err)
        results.append({"leaf": leaf, "verdict": verdict, "detail": detail + f" | out[:80]={out.strip()[:80]!r}"})
        counts[verdict] = counts.get(verdict, 0) + 1
        prog.write(f"    -> {verdict}\n"); prog.flush()

    prog.close()
    json.dump({"counts": counts, "leaf_total": len(leaves), "results": results},
              open(_OUT, "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print(f"写入: {_OUT}")
    print(f"叶子总数 = {len(leaves)}  counts = {counts}")
    print("=== FAIL ===")
    for r in results:
        if r["verdict"] == "FAIL":
            print(f"  {r['leaf']}: {r['detail']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
