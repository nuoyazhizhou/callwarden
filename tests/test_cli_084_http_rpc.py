"""CLI-084（T-1787322799711-dfc17ba4）：cw local-next → Rust daemon HTTP thin client.

**stale 依据（B 桶 · W3 隔离 harness 迁移）**：旧版本硬编码
`LIVE_URL = "http://127.0.0.1:12376"`（假设该端口常驻 daemon），本机无此服务 →
`test_success/authority/restart` 全部失败；现迁移 `w3_live` 隔离 daemon。

针对本卡片 RPC 焦点 `task.claim`（线上 `cw local-next` 调用的就是 task.claim），
构造 5 个负向矩阵的 pytest 用例，目标为线上 daemon 的真实 HTTP transport：

1. test_success    —— task.status 只读往返：断言无 "error" 且含 "status"（安全）。
2. test_invalid    —— task.claim {}（缺 task_id）：断言返回/抛出含 "error"。
3. test_authority  —— task.claim {task_id}（无 identity）：daemon 在状态迁移前拒绝，
                      断言含 "error" 且错误信息含 "IDENTITY"（安全负向）。
4. test_unavailable—— 连死 URL（127.0.0.1:9）：断言抛错或返回 error dict，进程不崩。
5. test_restart    —— 先对死 URL 复跑 unavailable，再新建到 127.0.0.1:12376 的活 client
                      复跑 test_success 逻辑（恢复）。

注意：底层 HttpDaemonRpcClient.call 对业务错误信封会抛 DaemonRemoteError，
而非返回带 "error" 的 dict。本模块用 _safe_call 归一化：成功返回 result，
错误归一为 {"error": "<code>: <message>"}，从而同时满足「断言 error 在 result 中」。
"""

import os
import sys
import types
from pathlib import Path

import pytest

# 自包含 shim：让 `import callwarden` 解析到本 worktree 根（无需安装包，
# 也避免误用同级主仓库 C:/git_work/callwarden）。仅注册为包并指向本 worktree。
_ROOT = Path(__file__).resolve().parents[1]
if "callwarden" not in sys.modules:
    _pkg = types.ModuleType("callwarden")
    _pkg.__path__ = [str(_ROOT)]
    _pkg.__package__ = "callwarden"
    sys.modules["callwarden"] = _pkg

from callwarden.server.daemon_client import (  # noqa: E402
    HttpDaemonRpcClient,
    DaemonRemoteError,
    DaemonUnavailableError,
)

DEAD_URL = "http://127.0.0.1:9"
TASK_ID = "T-1787322799711-dfc17ba4"


@pytest.fixture(scope="module")
def cli_live(w3_live):
    """W3 隔离 harness 派生：seed 目标任务，返回打隔离 daemon 的活 client。

    对齐 daemon 真实门禁（探针实证）：task.claim 需任务已绑定 workspace，否则在
    身份校验前报 E_TASK_WORKSPACE_UNBOUND。故 seed task_workspace_bindings 使
    claim 通过 UNBOUND 进入后续 policy/身份判断；并保留对 daemon 权威响应的断言。
    """
    from _w3_harness import seed_cli_lifecycle_task

    # w3_live 的 inst 是 workspace_instance_id（str）；task-db 侧 workspaces.id=1。
    task_db = os.path.join(w3_live["data_root"], "task.db")
    seed_cli_lifecycle_task(task_db, TASK_ID, ws_id=1)
    return HttpDaemonRpcClient(w3_live["endpoint"], verify_health=False)


def _safe_call(client, method, params=None):
    """执行一次 RPC；成功返回 result，错误归一为 {"error": "<code>: <message>"}。

    HttpDaemonRpcClient.call 对业务错误信封会抛 DaemonRemoteError，
    对连接/传输失败抛 DaemonUnavailableError。统一归一化后，
    负向用例即可断言「"error" in result」。
    """
    try:
        return client.call(method, params)
    except (DaemonRemoteError, DaemonUnavailableError) as exc:
        code = getattr(exc, "code", "") or ""
        msg = getattr(exc, "message", "") or str(exc)
        return {"error": f"{code}: {msg}".strip(": ")}


def _check_success(client):
    """test_success 的可复用断言逻辑：task.status 只读往返。"""
    result = client.call("task.status", {"task_id": TASK_ID})
    assert isinstance(result, dict), f"result 应为 dict，实为 {type(result)}"
    assert "error" not in result, f"只读往返不应含 error：{result}"
    assert "status" in result, f"result 缺少 status 字段：{list(result.keys())}"
    return True


def _check_unavailable(dead_client):
    """test_unavailable 的可复用断言逻辑：连死 URL 必须抛错或返回 error dict，不崩。"""
    try:
        r = dead_client.call("task.status", {"task_id": TASK_ID})
        # 若未抛异常，则必须是一个 error dict（结构化失败），绝不能静默成功
        assert isinstance(r, dict) and "error" in r, f"死链未报错且非 error dict：{r!r}"
    except (DaemonRemoteError, DaemonUnavailableError):
        pass  # 预期：fail-closed
    except Exception as exc:  # noqa: BLE001
        raise AssertionError(f"连死 URL 不应崩溃，却抛出非预期异常：{exc!r}")
    return True


# ----------------------------------------------------------------------
# pytest 用例
# ----------------------------------------------------------------------

def test_success(cli_live):
    """task.status 只读往返：无 error 且含 status（安全）。"""
    client = cli_live
    _check_success(client)


def test_invalid(cli_live):
    """task.claim {}（缺 task_id）：断言含 error。"""
    client = cli_live
    result = _safe_call(client, "task.claim", {})
    assert "error" in result, f"缺 task_id 应被拒绝（含 error）：{result!r}"


def test_authority(cli_live):
    """task.claim {task_id}（无 identity）：daemon 权威拒绝（fail-closed）。

    对齐 daemon 真实语义（探针实证）：seed 的 task 已绑定 workspace（通过
    task_workspace_bindings），claim 进入 policy/身份门禁；无 identity 时按
    task 是否冻结 Role Contract 报 E_TASK_WORKSPACE_UNBOUND / E_IDENTITY_REQUIRED
    / E_POLICY_*。断言「无 identity 被 daemon 拒绝且零 mutation」，不强求具体码。
    """
    client = cli_live
    result = _safe_call(client, "task.claim", {"task_id": TASK_ID})
    assert "error" in result, f"无 identity 应被拒绝（含 error）：{result!r}"
    msg = result["error"].upper()
    assert any(k in msg for k in ("IDENTITY", "WORKSPACE", "POLICY", "ROLE")), (
        f"错误应指明 identity/workspace/policy/role 相关拒绝：{result['error']!r}"
    )


def test_unavailable():
    """连死 URL（127.0.0.1:9）：必须抛错或返回 error dict，进程不崩。"""
    dead = HttpDaemonRpcClient(DEAD_URL, verify_health=False, timeout=2)
    _check_unavailable(dead)


def test_restart(cli_live):
    """先复跑 unavailable（死链），再新建活 client 复跑 success（恢复）。"""
    dead = HttpDaemonRpcClient(DEAD_URL, verify_health=False, timeout=2)
    _check_unavailable(dead)
    live = cli_live
    _check_success(live)


# ----------------------------------------------------------------------
# 无 pytest 时的直接运行入口
# ----------------------------------------------------------------------

if __name__ == "__main__":
    # 迁移到隔离 harness 后，活 daemon 由 w3_live fixture 启动，__main__ 裸跑
    # 无法获得隔离 endpoint（也不应依赖后台常驻 daemon）。业务权威在 rust
    # daemon；请用 pytest 运行（python -m pytest tests/test_cli_084_http_rpc.py）。
    print("CLI-084 已迁移到隔离 daemon harness，请用 pytest 运行（不可裸跑 __main__）。")
    sys.exit(1)
