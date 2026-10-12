"""T3:234 个 CLI 叶子命令全参数真实调用(收敛套件正式化)。

subprocess 执行 `python cw.py <argv>`(argv 由 param_provider.build_cli_argv
生成),cwd=种子 workspace(seed_cli_workspace fixture,CWD 绑定 + build_graph),
按分层调用并分类。

运行环境:需可用的 HTTP daemon(生产或部署后)。种子 workspace 用 CWD 绑定
(不依赖隔离 daemon 的 instance 锁),因此本文件可在生产 daemon 环境直接运行。

基线演进:
- 首轮(2026-09-30,生产 daemon b495919):70 PASS / 114 EXPECTED_BUSINESS
  / 18 DEFECT / 31 SKIP。18 个 DEFECT = 10 compat method_not_found +
  5 rc=0 掩盖的 CLI bug + 3 traceback。
- 修 5 个 rc=0 CLI bug 后:75 PASS / 114 / 13 DEFECT / 31。
- **2026-10-13 重跑(隔离 daemon c150807,零生产触碰)**:85 PASS /
  117 EXPECTED_BUSINESS / **0 DEFECT** / 31 SKIP。13 个 DEFECT 全部消除:
    - 10 个 method_not_found 经 compat→native 迁移后真实执行
      (defect build / fts status / gc restore / gc status / function-issues /
      git destructive-log / rollback show / rollback is-rolled-back 转 PASS;
      toolchain show/bind 的 RPC 已存在,返回业务 not_found「工具链不存在」
      而非 method_not_found);
    - 3 个 traceback(collab publish / daemon publish / daemon snapshot-stats)
      转为业务错误(E_FILE_NOT_FOUND / workspace_not_found)。
  证据:deliverables/software-company/doc-audit/t3_cli_result.json +
  t3_cli_full_invocation_report.md,重跑脚本 run_t2_t3_evidence.py。
  故门禁从「DEFECT <= 18 容忍已知缺陷」升级为「method_not_found=0 且
  traceback=0」(见 test_all_cli_commands_zero_defect)。
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

# 其它类别 DEFECT（timeout / internal_error 等）的容忍上限。两类系统性缺陷
# （method_not_found / traceback）已清零、门禁要求严格为 0；此处仅留少量容忍
# 防共享 daemon 高负载时的偶发超时误伤，任何系统性新增缺陷都会轻易越过此值。
_OTHER_DEFECT_TOLERANCE = 2


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

    build_graph 后须补齐 codegraph 读面 5 表(workspace_build_contexts /
    resolved_edges / clone_groups / clone_group_members / clone_pairs):
    隔离 codegraph 库由 storage::initialize_or_migrate 初始化,不含这 5 张表
    的 DDL(生产库由 Python 时代 db 层历史创建),缺表会使 build-context *
    与 clone 读组 no such table → 误判为 internal_error DEFECT。与 conftest
    seed_workspace 同源(见 conftest._ensure_codegraph_seed_tables)。
    codegraph 库路径随 USERPROFILE:生产=真实 HOME,隔离 daemon=data_root,
    expanduser("~") 两种情形都自洽。
    """
    if not _http_daemon_available():
        pytest.skip("无可用 HTTP daemon(需生产或部署后的 daemon)")

    from conftest import _ensure_codegraph_seed_tables

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
    # 补齐隔离 codegraph 库缺的读面表(同 conftest seed_workspace)
    _ensure_codegraph_seed_tables(os.path.expanduser("~"))
    ctx = pp.SeedContext(
        root=root, known_qualified_names=["compute", "add", "multiply"],
        known_callee_names=["add", "multiply"], known_file_paths=["calc.py"],
    )
    yield {"root": root, "ctx": ctx, "build_ok": r.returncode == 0}
    shutil.rmtree(root, ignore_errors=True)


def test_cli_commands_load_234():
    commands = runner.load_cli_commands()
    assert len(commands) >= 230, f"CLI 叶子命令数异常: {len(commands)}(预期 234)"


def test_all_cli_commands_zero_defect(seed_cli_workspace):
    """全量真实执行 234 CLI 命令,断言 method_not_found=0 且 traceback=0。

    2026-10-13 隔离 daemon 重跑基线:0 DEFECT(见模块 docstring)。门禁要求
    两类系统性缺陷保持清零——compat 路由缺失(method_not_found)与 CLI 未捕获
    异常(traceback)任何回升即失败,不再容忍已知缺陷。
    """
    ctx = seed_cli_workspace["ctx"]
    root = seed_cli_workspace["root"]

    report = runner.run_all(ctx, cwd=root, timeout=60)
    counts = report["counts"]

    total = sum(counts.values())
    assert total >= 230, f"覆盖命令数异常: {total}"

    defects = [r for r in report["results"] if r["verdict"] == "DEFECT"]
    mn = [r for r in defects if "method_not_found" in r.get("detail", "")]
    tb = [r for r in defects if "Traceback" in r.get("detail", "")]
    other = [r for r in defects if r not in mn and r not in tb]

    assert not mn, (
        "method_not_found DEFECT 回升(compat 路由缺失):\n"
        + "\n".join(f"  {r['cmd']}: {r['detail'][:120]}" for r in mn)
    )
    assert not tb, (
        "traceback DEFECT 回升(CLI 未捕获异常):\n"
        + "\n".join(f"  {r['cmd']}: {r['detail'][:120]}" for r in tb)
    )
    assert len(other) <= _OTHER_DEFECT_TOLERANCE, (
        f"其它 DEFECT 异常增多({len(other)} > {_OTHER_DEFECT_TOLERANCE}):\n"
        + "\n".join(f"  {r['cmd']}: {r['detail'][:120]}" for r in other)
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
