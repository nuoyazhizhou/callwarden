"""artifact_e2e.py
================

交付门禁「产物」项的本体（TESTING_PLAN §8）：

  产物 | 同候选 release 干净安装及真实入口通过，不以源码 debug 替代。

流程（全部离线、run 根内，零生产触碰）：
 1. 从 HEAD 构建 wheel（setuptools，--no-build-isolation --no-deps）；
 2. 全新 venv（--system-site-packages：依赖由主机提供——离线约束，
    明示记录；被验包装本身全新）；
 3. wheel 装进 venv（--no-deps --force-reinstall）；
 4. `import callwarden` 必须解析到 **venv 内**（非仓库源码，防 "以源码
    debug 替代"）；
 5. 真实入口冒烟（全部经 venv 的 cw）：
    a. `cw --version` == pyproject 版本；
    b. 隔离 daemon（候选二进制，revision/hash 自洽 manifest）上
       `cw workspace list`；
    c. 种子 workspace `cw refresh --all` 全链路建图 rc=0；
    d. MCP wire：venv `cw server`（stdio）+ tools/list == 243。

honesty 声明（写入 verdict）：
 - callwarden_core.pyd 被 .gitignore 忽略（非 revision 钉住），本机 wheel
   内的 pyd 为工作树既有构建；其 sha256 记录在 verdict。CI 的 wheel job
   从源码构建 pyd（revision 钉住），不受此限；
 - daemon 候选为 tp11a-fix（sha 记录），与 acceptance 域同基准。

用法::

    python -m cicd.artifact_e2e [--run-root <dir>] [--daemon-src <exe>]
        [--json-out <path>]
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

REPO_ROOT = Path(__file__).resolve().parents[1]
HERE = Path(__file__).resolve().parent
for _p in (str(REPO_ROOT), str(HERE),
           str(REPO_ROOT / "tests" / "acceptance" / "harness" / "bootstrap")):
    if _p not in sys.path:
        sys.path.insert(0, _p)

EXPECTED_TOOLS = 243
DEFAULT_DAEMON_SRC = Path(
    os.environ.get(
        "CW_ARTIFACT_DAEMON_SRC",
        r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/candidates/tp11a-fix/cw-daemon.exe"))
DEFAULT_RUN_ROOT = Path(
    os.environ.get("CW_ARTIFACT_RUN_ROOT",
                   r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/artifact-e2e"))


class E2EError(Exception):
    pass


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run(cmd: List[str], cwd: Optional[Path] = None, env: Optional[dict] = None,
         timeout: float = 600.0) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=str(cwd) if cwd else None, env=env,
                          capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def _project_version() -> str:
    import tomllib
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)["project"]["version"]


def _isolated_env(run_root: Path, daemon_exe: Path) -> dict:
    env = dict(os.environ)
    home = run_root / "home"
    env["USERPROFILE"] = str(home)
    env["HOME"] = str(home)
    env["CALLWARDEN_HOME"] = str(home)
    env["CALLWARDEN_DB"] = str(home / ".callwarden" / "callwarden.db")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    env["CW_DAEMON_BIN"] = str(daemon_exe)
    for k in ("CW_DAEMON_TRANSPORT", "CW_DAEMON_HTTP_BIND"):
        env.pop(k, None)
    return env


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(prog="cicd.artifact_e2e")
    ap.add_argument("--run-root", default=str(DEFAULT_RUN_ROOT))
    ap.add_argument("--daemon-src", default=str(DEFAULT_DAEMON_SRC))
    ap.add_argument("--json-out", default="")
    args = ap.parse_args(argv)

    run_root = Path(args.run_root)
    daemon_src = Path(args.daemon_src)
    verdict: Dict[str, Any] = {"ok": False, "steps": [], "notes": []}

    def step(name: str, ok: bool, detail: str = "") -> bool:
        verdict["steps"].append({"step": name, "ok": ok, "detail": detail[:400]})
        return ok

    # 自清洁
    if run_root.exists():
        shutil.rmtree(run_root, ignore_errors=True)
    (run_root / "dist").mkdir(parents=True, exist_ok=True)
    (run_root / "home").mkdir(parents=True, exist_ok=True)

    try:
        # ---- 1. 构建 wheel ----
        # 🔴 前置：清理陈旧 bdist 暂存（WinError 183：build/bdist.win-amd64/
        # wheel/<name>.dist-info 已存在则 wheel 组装失败；legacy ci.yml 亦有
        # 同款清理）。只删 bdist 暂存，保留 build/callwarden_core*.pyd。
        bdist = REPO_ROOT / "build" / "bdist.win-amd64"
        if bdist.is_dir():
            shutil.rmtree(bdist, ignore_errors=True)
            verdict["notes"].append("已清理陈旧 build/bdist.win-amd64 暂存目录")
        r = _run([sys.executable, "-m", "pip", "wheel", ".",
                  "--no-build-isolation", "--no-deps",
                  "-w", str(run_root / "dist")],
                 cwd=REPO_ROOT, timeout=900)
        (run_root / "wheel_build.log").write_text(
            f"rc={r.returncode}\n--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr}",
            encoding="utf-8", errors="replace")
        wheels = sorted((run_root / "dist").glob("*.whl"))
        if r.returncode != 0 or not wheels:
            tail = "\n".join(
                ln for ln in (r.stdout + "\n" + r.stderr).splitlines()
                if ln.strip().lower().startswith(("error", "  error", "warning: no")))
            raise E2EError(f"wheel 构建失败：rc={r.returncode}；关键行：{tail[-800:]}"
                           f"（完整日志：{run_root / 'wheel_build.log'}）")
        wheel = wheels[-1]
        verdict["wheel"] = {"path": str(wheel), "name": wheel.name,
                            "sha256": _sha256(wheel)}
        step("build_wheel", True, wheel.name)

        # ---- 2. venv（离线：依赖由主机提供，明示） ----
        venv_py = run_root / "venv" / "Scripts" / "python.exe"
        r = _run([sys.executable, "-m", "venv", str(run_root / "venv"),
                  "--system-site-packages"], timeout=300)
        if r.returncode != 0 or not venv_py.is_file():
            raise E2EError(f"venv 创建失败：{r.stderr[-300:]}")
        verdict["notes"].append(
            "venv 使用 --system-site-packages（离线约束：依赖由主机提供，"
            "被验包装本身为全新安装）")
        step("create_venv", True, str(run_root / "venv"))

        # ---- 3. 安装 wheel ----
        r = _run([str(venv_py), "-m", "pip", "install", str(wheel),
                  "--no-deps", "--force-reinstall"], timeout=600)
        if r.returncode != 0:
            raise E2EError(f"wheel 安装失败：{r.stdout[-300:]}{r.stderr[-300:]}")
        step("install_wheel", True, wheel.name)

        # ---- 4. import 必须落在 venv 内（禁源码替代） ----
        r = _run([str(venv_py), "-c",
                  "import callwarden, sys; "
                  "print(callwarden.__file__); "
                  "assert 'venv' in callwarden.__file__.replace('\\\\','/'), "
                  "'import 未落在 venv 内'; "
                  "print(callwarden.__version__ if hasattr(callwarden,'__version__') else '')"],
                 timeout=120)
        pkg_file = (r.stdout or "").strip().splitlines()
        if r.returncode != 0:
            raise E2EError(f"import 核验失败：{r.stderr[-300:]}")
        step("import_in_venv", True, pkg_file[0] if pkg_file else "")
        verdict["installed_pkg_file"] = pkg_file[0] if pkg_file else ""

        # ---- 5a. cw --version ----
        cw = run_root / "venv" / "Scripts" / "cw.exe"
        if not cw.is_file():
            raise E2EError(f"venv 缺 cw 入口：{cw}")
        r = _run([str(cw), "--version"], timeout=120)
        version = _project_version()
        if r.returncode != 0 or version not in (r.stdout or ""):
            raise E2EError(f"cw --version 异常：rc={r.returncode} "
                           f"out={r.stdout[:120]!r}（期望含 {version}）")
        step("cw_version", True, (r.stdout or "").strip()[:80])

        # ---- 5b/5c. 隔离 daemon + CLI 真实链路 ----
        from daemon_bootstrap import IsolatedDaemon
        if not daemon_src.is_file():
            raise E2EError(f"daemon 候选不存在：{daemon_src}")
        daemon_exe = run_root / "src" / "cw-daemon.exe"
        daemon_exe.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(daemon_src, daemon_exe)
        if _sha256(daemon_exe) != _sha256(daemon_src):
            raise E2EError("daemon 候选落盘保真校验失败")
        verdict["daemon"] = {"src": str(daemon_src),
                             "sha256": _sha256(daemon_src)}

        d = IsolatedDaemon(run_root=run_root, daemon_exe=daemon_exe,
                           expected_sha256=_sha256(daemon_src))
        manifest = d.start(timeout=120)
        d.verify_consistency()
        step("daemon_start", True,
             f"endpoint={manifest.get('endpoint')} pid={manifest.get('pid')}")

        env = _isolated_env(run_root, daemon_exe)

        r = _run([str(cw), "workspace", "list"], env=env, timeout=180)
        step("cw_workspace_list", r.returncode == 0,
             (r.stdout or r.stderr)[:200])

        ws = run_root / "ws"
        ws.mkdir(parents=True, exist_ok=True)
        (ws / "app.py").write_text("def hello():\n    return 'world'\n",
                                   encoding="utf-8")
        r = _run([str(cw), "refresh", "--all"], cwd=ws, env=env, timeout=600)
        step("cw_refresh_all", r.returncode == 0,
             (r.stdout or r.stderr)[:200])

        # ---- 5d. MCP wire：venv cw server + tools/list == 243 ----
        tools = _mcp_tools_list(venv_py, cw, env)
        if tools != EXPECTED_TOOLS:
            raise E2EError(f"MCP tools/list = {tools}，期望 {EXPECTED_TOOLS}")
        step("mcp_tools_list", True, f"tools={tools}")

        d.stop()
        verdict["ok"] = all(s["ok"] for s in verdict["steps"])
        verdict["exit_reason"] = "" if verdict["ok"] else "存在失败步骤"
    except E2EError as e:
        verdict["exit_reason"] = str(e)
        verdict["ok"] = False
        try:
            d.stop()  # type: ignore[has-type]
        except Exception:
            pass
    except Exception as e:  # 未预期错误同样 fail-closed
        verdict["exit_reason"] = f"未预期错误：{type(e).__name__}: {e}"
        verdict["ok"] = False

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return 0 if verdict["ok"] else 2


def _mcp_tools_list(venv_py: Path, cw: Path, env: dict) -> int:
    """经 stdio 起 venv 的 cw server，翻页拉全 tools/list，返回工具数。"""
    import asyncio
    from mcp.client.stdio import StdioServerParameters, stdio_client
    from mcp import ClientSession

    async def _list() -> int:
        params = StdioServerParameters(
            command=str(venv_py), args=[str(cw), "server"], env=env)
        total = 0
        cursor = None
        async with stdio_client(params) as (read, write):
            async with ClientSession(read, write) as session:
                await asyncio.wait_for(session.initialize(), timeout=120)
                for _ in range(50):
                    kw = {"cursor": cursor} if cursor else {}
                    res = await asyncio.wait_for(
                        session.list_tools(**kw), timeout=120)
                    total += len(res.tools)
                    cursor = getattr(res, "nextCursor", None)
                    if not cursor:
                        break
        return total

    return asyncio.run(_list())


if __name__ == "__main__":
    sys.exit(main())
