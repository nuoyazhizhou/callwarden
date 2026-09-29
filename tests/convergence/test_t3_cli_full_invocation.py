"""T3:234 个 CLI 叶子命令全参数真实调用(收敛套件正式化)。

subprocess 执行 `python cw.py <argv>`(argv 由 param_provider.build_cli_argv
生成),cwd=种子 workspace(seed_cli_workspace fixture,CWD 绑定 + build_graph),
按分层调用并分类。

运行环境:需可用的 HTTP daemon(生产或部署后)。种子 workspace 用 CWD 绑定
(不依赖隔离 daemon 的 instance 锁),因此本文件可在生产 daemon 环境直接运行。

T3 首轮(2026-09-30,生产 daemon b495919)结果:70 PASS / 114 EXPECTED_BUSINESS
/ 18 DEFECT / 31 SKIP。18 个 DEFECT 分三类(见 t3 报告):
  - 10 个 method_not_found:CLI 映射到 daemon 未实现的 compat RPC(compat
    worker 禁用的连锁,系统性问题,非独立 bug)。
  - 5 个 rc=0 掩盖的 CLI 代码 bug:call-chain/coupled-fns/largest-fns/
    rule applicable/status(fail-soft 吞异常返回 rc=0)。
  - 3 个 traceback:collab publish/daemon publish/daemon snapshot-stats。
本 pytest 记录基线并断言缺陷数不回升(不 == 0,因缺陷已知待独立修复)。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile

import pytest

import param_provider as pp
import t3_cli_runner as runner

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CW_PY = os.path.join(_REPO_ROOT, "cw.py")
_SEED_SAMPLE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_sample")

# T3 首轮已知 DEFECT 基线(生产 daemon b495919)。断言不回升。
_KNOWN_DEFECT_BASELINE = 18


def _http_daemon_available() -> bool:
    """探测是否有可用 HTTP daemon(生产或部署后)。"""
    env = dict(os.environ)
    env["CW_DAEMON_TRANSPORT"] = "http"
    env["PYTHONUTF8"] = "1"
    try:
        r = subprocess.run(
            [sys.executable, "-u", _CW_PY, "daemon", "health"],
            cwd=_REPO_ROOT, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30, env=env,
        )
        return r.returncode == 0 and '"ok": false' not in r.stdout
    except Exception:
        return False


@pytest.fixture(scope="module")
def seed_cli_workspace():
    """CWD 绑定的种子 workspace:拷样本 → refresh --all(build_graph)。

    CLI 命令用 CWD 绑定 workspace(不依赖隔离 daemon 的 instance 锁),因此本
    fixture 用当前可用的 HTTP daemon。无可用 daemon 时 skip。
    """
    if not _http_daemon_available():
        pytest.skip("无可用 HTTP daemon(需生产或部署后的 daemon)")

    root = tempfile.mkdtemp(prefix="cw_t3_ws_")
    for f in os.listdir(_SEED_SAMPLE):
        src = os.path.join(_SEED_SAMPLE, f)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(root, f))

    env = dict(os.environ)
    env["CW_DAEMON_TRANSPORT"] = "http"
    env["PYTHONUTF8"] = "1"
    r = subprocess.run(
        [sys.executable, "-u", _CW_PY, "refresh", "--all"],
        cwd=root, capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=300, env=env,
    )
    ctx = pp.SeedContext(
        root=root, known_qualified_names=["compute", "add", "multiply"],
        known_callee_names=["add", "multiply"], known_file_paths=["calc.py"],
    )
    yield {"root": root, "ctx": ctx, "build_ok": r.returncode == 0}
    shutil.rmtree(root, ignore_errors=True)


def test_cli_commands_load_234():
    commands = runner.load_cli_commands()
    assert len(commands) >= 230, f"CLI 叶子命令数异常: {len(commands)}(预期 234)"


def test_all_cli_commands_defects_not_regress(seed_cli_workspace):
    """全量真实执行 234 CLI 命令,断言真实 DEFECT 数不超过已知基线。

    基线 18(首轮)。缺陷已知待独立修复(10 compat method_not_found +
    5 rc=0 CLI bug + 3 traceback),此处防回升,不要求 == 0。
    """
    ctx = seed_cli_workspace["ctx"]
    root = seed_cli_workspace["root"]

    report = runner.run_all(ctx, cwd=root, timeout=60)
    counts = report["counts"]

    total = sum(counts.values())
    assert total >= 230, f"覆盖命令数异常: {total}"

    defects = [r for r in report["results"] if r["verdict"] == "DEFECT"]
    assert len(defects) <= _KNOWN_DEFECT_BASELINE, (
        f"CLI DEFECT 回升: {len(defects)} > 基线 {_KNOWN_DEFECT_BASELINE}\n"
        + "\n".join(f"  {r['cmd']}: {r['detail'][:120]}" for r in defects)
    )
    # 大部分命令应真实执行(PASS + EXPECTED_BUSINESS)
    assert counts["PASS"] + counts["EXPECTED_BUSINESS"] >= 150


def test_provider_cli_argv_no_help_leak(seed_cli_workspace):
    """全参数 argv 不应残留 argparse 帮助项(-h/--help)。"""
    ctx = seed_cli_workspace["ctx"]
    commands = runner.load_cli_commands()
    leaks = []
    for cmd_str, spec in commands.items():
        argv = pp.build_cli_argv(cmd_str, spec, ctx, required_only=False)
        if "-h" in argv or "--help" in argv:
            leaks.append(cmd_str)
    assert not leaks, "argv 残留帮助项:\n" + "\n".join(leaks[:20])
