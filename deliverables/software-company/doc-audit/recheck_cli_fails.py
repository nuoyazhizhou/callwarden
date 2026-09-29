#!/usr/bin/env python3
"""recheck_cli_fails.py —— 串行复测 CLI 首轮 17 个 FAIL(排除并发干扰)。

首轮 probe_cli_cmds.py 并发/连续跑时 17 个命令记 TIMEOUT。但单独跑 cw status 仅 0.47s。
本脚本逐个串行跑(每个给 60s),拿真实行为,区分:真挂起 vs 首轮假超时。
"""
import json, os, subprocess, sys, time

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(os.path.dirname(HERE)))
PY = os.environ.get("PYTHON", sys.executable)
CW = os.path.join(REPO, "cw.py")

FAILS = [
    ["semgrep", "scan"], ["semgrep", "list"], ["semgrep", "stats"],
    ["server"], ["stats"], ["status"], ["task", "list"], ["test"],
    ["toolchain", "register"], ["toolchain", "list"], ["toolchain", "show"],
    ["toolchain", "delete"], ["toolchain", "bind"], ["toolchain", "list-bound"],
    ["topo"], ["uncommented"], ["vuln-blast"],
]

def run(argv, timeout):
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"; env["PYTHONIOENCODING"] = "utf-8"
    env["CW_DAEMON_TRANSPORT"] = "http"
    t0 = time.time()
    try:
        p = subprocess.run([PY, CW] + argv, cwd=REPO, capture_output=True,
                           text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, stdin=subprocess.DEVNULL, env=env)
        return p.returncode, (p.stdout or ""), (p.stderr or ""), time.time() - t0
    except subprocess.TimeoutExpired:
        return -999, "", "TIMEOUT", time.time() - t0

results = []
for argv in FAILS:
    leaf = " ".join(argv)
    # server 单独短超时(会挂起)
    to = 12 if leaf == "server" else 55
    rc, out, err, dt = run(argv, to)
    combined = (out + "\n" + err)
    if rc == -999:
        verdict = "REAL_HANG"
    elif rc == 0:
        verdict = "PASS(串行)"
    else:
        verdict = "NEEDS_ARGS_or_BIZERR"
    results.append({"leaf": leaf, "rc": rc, "elapsed_s": round(dt, 2),
                    "verdict": verdict, "tail": combined.strip()[-160:]})
    print(f"{leaf}: rc={rc} {dt:.1f}s -> {verdict}")

json.dump(results, open(os.path.join(HERE, "recheck_cli_fails_result.json"), "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("\n写入 recheck_cli_fails_result.json")
