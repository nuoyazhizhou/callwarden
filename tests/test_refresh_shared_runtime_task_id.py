"""P0-J-D targeted guard for refresh_shared_runtime TaskId grammar.

This test intentionally reads the entrypoint rather than invoking it: a valid
TaskId would proceed into a real runtime refresh. It proves the source gate that
runs before any build, daemon stop, runtime replacement or database access.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "refresh_shared_runtime.ps1"
# stale 依据（A 类：测试侧期望陈旧）：
# 生产脚本 scripts/refresh_shared_runtime.ps1:30 的 TaskId fail-closed 门禁已收紧为
#   if ($TaskId -notmatch '^T-[0-9]+-[0-9a-z-]+$')
# 只接受真实部署任务 ID（T-<毫秒时间戳>-<小写 hex/短横线>），不再接受
# T-P0J-XXX 这类不透明治理/占位 ID。原 EXPECTED 仍接受 opaque 形式，故同步收紧。
EXPECTED = r"^T-[0-9]+-[0-9a-z-]+$"


def source_gate_pattern() -> str:
    source = SCRIPT.read_text(encoding="utf-8")
    match = re.search(r"\$TaskId\s+-notmatch\s+'([^']+)'", source)
    assert match, "refresh script must contain an explicit TaskId fail-closed gate"
    return match.group(1)


def test_refresh_runtime_accepts_real_deployment_task_ids() -> None:
    pattern = source_gate_pattern()
    assert pattern == EXPECTED
    gate = re.compile(pattern)
    for value in (
        "T-1786346158666-e9316534",
        "T-1787402257549-67ba81e6",
        "T-1787203926824-9f873bfc-sub-1",
    ):
        assert gate.fullmatch(value), value


def test_refresh_runtime_rejects_unsafe_or_non_task_inputs() -> None:
    gate = re.compile(source_gate_pattern())
    for value in (
        "", "T", "T-", "T-P0J", "T--P0J", "T-P0J-ROLE WORKER",
        "T-P0J/ROLE", "T-P0J\\ROLE", "T-P0J;Stop-Process", "--TaskId",
        "T-../../callwarden", "T-P0J-ROLE_", "T-P0J-ROLE.$",
        "T-P0J-ROLE-WORKER-IDENTITY", "T-P0J-D-DEPLOYMENT-GOVERNANCE",
        "T-1786346158666-E9316534",
    ):
        assert not gate.fullmatch(value), value


if __name__ == "__main__":
    test_refresh_runtime_accepts_real_deployment_task_ids()
    test_refresh_runtime_rejects_unsafe_or_non_task_inputs()
    print("P0JD_TASK_ID_GATE_TESTS_OK")
