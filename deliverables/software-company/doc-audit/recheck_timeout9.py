#!/usr/bin/env python3
"""recheck_timeout9.py —— 复测 9 个 DAEMON_TIMEOUT 命令的当前真实状态。

Task7 串行复测时 30s 超时;Task9-A 复查发现 status 现在 0.3s 返回(超时是间歇的)。
本脚本逐个串行复测这 9 个,拿当前行为:超时是否消失、是否有稳定业务错误。
"""
import json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
PY = os.environ.get("PYTHON", sys.executable)
CW = os.path.join(REPO, "cw.py")

CMDS = [
    ["semgrep", "scan"], ["semgrep", "list"], ["semgrep", "stats"],
    ["stats"], ["status"], ["task", "list"],
    ["toolchain", "register"], ["toolchain", "list"], ["toolchain", "show"],
]

def run(argv, timeout=45):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"; env["PYTHONIOENCODING"] = "utf-8"
    env["CW_DAEMON_TRANSPORT"] = "http"
    t0 = time.time()
    try:
        p = subprocess.run([PY, CW] + argv, cwd=REPO, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL, env=env)
        return p.returncode, (p.stdout or "") + (p.stderr or ""), time.time() - t0
    except subprocess.TimeoutExpired:
        return -999, "TIMEOUT", time.time() - t0

results = []
for argv in CMDS:
    leaf = " ".join(argv)
    rc, out, dt = run(argv)
    combined = out
    if rc == -999:
        v = "TIMEOUT_STILL"
    elif "E_HTTP_REQUEST_TIMEOUT" in combined or "timed out" in combined:
        v = "TIMEOUT_SOFT(rc0掩盖)"
    elif "失败" in combined or "error" in combined.lower() or "Traceback" in combined:
        v = "BIZ_ERROR"
    elif rc == 0:
        v = "PASS"
    else:
        v = f"rc={rc}"
    tail = combined.strip()[-160:]
    results.append({"leaf": leaf, "rc": rc, "elapsed_s": round(dt, 1), "verdict": v, "tail": tail})
    print(f"{leaf:20} rc={rc} {dt:5.1f}s {v}")

json.dump(results, open(os.path.join(HERE, "recheck_timeout9_result.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("\n写入 recheck_timeout9_result.json")
