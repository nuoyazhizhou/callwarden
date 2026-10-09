#!/usr/bin/env python3
"""seed_workspace_probe.py —— 验证种子 workspace 可行性(T1 核心风险点)。

在隔离 HOME(不碰权威库)下,用 CLI 对 testcode/code-review-graph 跑 refresh,
产出真实 CodeGraphDB(符号/调用/git),验证:
1. refresh 能完成且耗时可接受;
2. 产出真实符号数(get_stats)。

隔离手段:临时 USERPROFILE/HOME → ~/.callwarden 落到临时目录 → 独立 daemon + 库。
"""
from __future__ import annotations
import json
import os
import subprocess
import sys
import tempfile
import time

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO = os.path.dirname(os.path.dirname(os.path.dirname(_HERE)))
_PY = os.environ.get("PYTHON", sys.executable)
_CW = os.path.join(_REPO, "cw.py")
_SEED_PROJECT = os.path.join(_REPO, "testcode", "code-review-graph")


def run(argv, env, timeout, cwd=None):
    t0 = time.time()
    try:
        p = subprocess.run([_PY, "-u", _CW] + argv, cwd=cwd or _REPO,
                           capture_output=True, text=True, encoding="utf-8",
                           errors="replace", timeout=timeout, env=env,
                           stdin=subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (p.stderr or ""), time.time() - t0
    except subprocess.TimeoutExpired:
        return -999, "TIMEOUT", time.time() - t0


def main():
    home = tempfile.mkdtemp(prefix="cw_seed_home_")
    env = dict(os.environ)
    env["USERPROFILE"] = home
    env["HOME"] = home
    env["PYTHONUTF8"] = "1"; env["PYTHONIOENCODING"] = "utf-8"
    env["CW_DAEMON_TRANSPORT"] = "http"
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    env["CALLWARDEN_WORKSPACE"] = _SEED_PROJECT

    print(f"隔离 HOME: {home}", flush=True)
    print(f"种子项目: {_SEED_PROJECT}", flush=True)

    # 1. 全量 refresh 种子项目(在种子项目目录内执行,workspace=该项目)
    print("=== refresh --all (可能数分钟) ===", flush=True)
    rc, out, dt = run(["refresh", "--all"], env, timeout=600, cwd=_SEED_PROJECT)
    print(f"refresh rc={rc} 耗时={dt:.1f}s", flush=True)
    print(f"refresh 输出尾: {out.strip()[-300:]}", flush=True)

    # 2. get_stats 看产出符号
    print("=== stats ===", flush=True)
    rc2, out2, dt2 = run(["stats"], env, timeout=60, cwd=_SEED_PROJECT)
    print(f"stats rc={rc2} 耗时={dt2:.1f}s", flush=True)
    print(f"stats 输出: {out2.strip()[:600]}", flush=True)

    # 记录结果
    result = {
        "home": home,
        "seed_project": _SEED_PROJECT,
        "refresh_rc": rc, "refresh_seconds": round(dt, 1),
        "stats_rc": rc2, "stats_output_head": out2.strip()[:600],
    }
    json.dump(result, open(os.path.join(_HERE, "seed_workspace_probe_result.json"), "w",
                           encoding="utf-8"), ensure_ascii=False, indent=2)
    print("=== 结果已写 seed_workspace_probe_result.json ===", flush=True)
    print(f"注意:隔离 HOME {home} 未删除(供检查),确认后可手动清理", flush=True)


if __name__ == "__main__":
    main()
