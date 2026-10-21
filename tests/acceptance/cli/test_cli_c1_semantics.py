"""TP-10C · C1 —— CLI 全入口语义验收（265 个冻结入口）。

selector: tests/acceptance/cli/test_cli_c1_semantics.py

对冻结清单（frozen-inventory/v1）的全部 265 个 CLI 入口做**真实调用**：
argv 直接由冻结 params 构造（必填位置参数逐个填充、可选项全量携带、
互斥组只取首个、已展开的 positional_choice 不重复追加选择值），在完全
隔离的 daemon 上执行，按 t3 分类语义判 PASS / EXPECTED_BUSINESS / DEFECT / SKIP。

硬断言：**DEFECT == 0**（Python traceback / method_not_found / daemon 不可用 /
代码级 internal_error 均视为真实缺陷）。证据 JSON 写入 <run_root>/reports/。

运行：C:/Python314/python.exe -m pytest tests/acceptance/cli/test_cli_c1_semantics.py \
        -p no:cacheprovider --noconftest -v
"""

from __future__ import annotations

import datetime as _dt
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from cli_acceptance_harness import (  # noqa: E402
    CLIAcceptanceEnv,
    load_cli_entries,
)

ENTRIES = load_cli_entries()
assert len(ENTRIES) == 265, f"冻结 CLI 入口数应为 265，实际 {len(ENTRIES)}"


@pytest.fixture(scope="module")
def env():
    """隔离 daemon + 公开 CLI 种子 workspace（module 级复用，teardown 写证据）。"""
    e = CLIAcceptanceEnv()
    manifest = e.start_daemon()
    assert manifest["daemon_binary_sha256"] == e.expected_sha256, (
        f"manifest 散列与部署候选不一致：{manifest['daemon_binary_sha256']}")
    e.seed_workspace()
    try:
        yield e
    finally:
        e.write_report("tp10c_c1_cli_semantics.json", {
            "suite": "TP-10C C1 CLI 全入口语义（265 冻结入口）",
            "generated_at": _dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "git_head": e.git_head(),
            "daemon_binary_sha256": manifest.get("daemon_binary_sha256"),
            "daemon_endpoint": manifest.get("endpoint"),
            "daemon_pid": manifest.get("pid"),
            "isolation": "完全隔离 run 根（loopback 动态端口 + run 根内全部状态），"
                         "未触碰生产 daemon / 生产 registry / ~/.callwarden",
            "seed": {
                "workspace_id": e.ctx.workspace_id if e.ctx else None,
                "root": str(e.seed_dir),
                "known_qualified_names": (e.ctx.known_qualified_names
                                          if e.ctx else []),
                "known_file_paths": (e.ctx.known_file_paths if e.ctx else []),
            },
            "counts": e.counts,
            "results": e.records,
        })
        e.stop_daemon()


@pytest.mark.parametrize("entry", ENTRIES,
                         ids=lambda e: e.get("entry_id", e.get("invocation", "?")))
def test_c1_entry_semantics(env, entry):
    """单个冻结 CLI 入口：真实执行，不得出现真实缺陷（DEFECT）。"""
    rec = env.run_entry(entry)
    assert rec["verdict"] != "DEFECT", (
        f"【TP-10C C1 真实缺陷】{rec['entry_id']} [{rec['entry_kind']}] "
        f"argv={rec['argv']}\n  {rec['detail']}")
