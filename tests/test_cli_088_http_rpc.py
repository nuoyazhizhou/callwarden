"""CLI-088（T-1787322799980-efcb2090）：cw local-rollback → Rust daemon HTTP thin client.

针对本卡片 RPC 焦点 `task.rollback`（线上 `cw local-rollback` 调用的就是 task.rollback），
构造 5 个负向矩阵的 pytest 用例，目标为线上 daemon 的真实 HTTP transport：

1. test_success    —— task.rollback {task_id, step_id, identity} 经 thin client 直达 daemon，
                      断言返回结构化结果且无 "error"（happy path）。需 Reviewer 提供合法
                      identity 与目标 task/step（见 LEGACY_IDENTITY / CW_TEST_TASK_ID / CW_TEST_STEP_ID）。
2. test_invalid    —— task.rollback {}（缺 task_id/step_id）：断言含 "error"。
3. test_authority  —— 不带 identity 调用：daemon 在写操作前拒绝，断言含 "error" 且
                      错误信息含 "IDENTITY"（安全负向）。
4. test_unavailable—— 连死 URL（127.0.0.1:9）：断言抛错或返回 error dict，进程不崩。
5. test_restart    —— 先对死 URL 复跑 unavailable，再新建到 127.0.0.1:12376 的活 client
                      复跑 test_success 逻辑（恢复）。

注意：CLI-088 的 Python handler `_local_rollback` 已改为 fail-closed：
local fallback 直接 raise DaemonUnavailableError，不再调用 db.task_rollback / db.task_rollback_step。
本模块只验证「thin client 把请求正确路由到 daemon 并获得结构化响应」，
业务 authority 为 Rust daemon（task_collab.rs::handle_task_rollback）。

底层 HttpDaemonRpcClient.call 对业务错误信封会抛 DaemonRemoteError，
而非返回带 "error" 的 dict。本模块用 _safe_call 归一化：成功返回 result，
错误归一为 {"error": "<code>: <message>"}，从而同时满足「断言 error 在 result 中」。
"""

import os
import sys
import types
from pathlib import Path

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

LIVE_URL = "http://127.0.0.1:12376"
DEAD_URL = "http://127.0.0.1:9"

# 回滚目标：Reviewer 提供合法 task/step（环境变量注入；缺省便于本地联调）。
TASK_ID = os.environ.get("CW_TEST_TASK_ID", "T-1787322799980-efcb2090")
STEP_ID = os.environ.get("CW_TEST_STEP_ID", "1")
RESOLUTION = "rolled_back_by_cli_088_test"

# CLI-088 系列沿用 legacy_identity_v1（四字段，无 role_worker_auth）。
LEGACY_IDENTITY = {
    "agent_id": os.environ.get("CW_TEST_AGENT_ID", "executor-workbuddy-v1"),
    "session_id": "cli-088-test-session",
    "model_id": "workbuddy-senior-developer",
    "role": "executor",
}


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
    """test_success 的可复用断言逻辑：rollback 写操作直达 daemon，结构化成功。"""
    params = {
        "task_id": TASK_ID,
        "step_id": STEP_ID,
        "resolved_by": "cli-088-test",
        "identity": LEGACY_IDENTITY,
    }
    result = client.call("task.rollback", params)
    assert isinstance(result, dict), f"result 应为 dict，实为 {type(result)}"
    assert "error" not in result, f"rollback 不应含 error（happy path）：{result}"
    return True


def _check_unavailable(dead_client):
    """test_unavailable 的可复用断言逻辑：连死 URL 必须抛错或返回 error dict，不崩。"""
    try:
        r = dead_client.call("task.rollback", {
            "task_id": TASK_ID,
            "step_id": STEP_ID,
        })
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

def test_success():
    """task.rollback 带 identity 直达 daemon：无 error（happy path）。

    依赖 Reviewer 提供合法 task/step（CW_TEST_TASK_ID / CW_TEST_STEP_ID）
    且 daemon 接受 legacy_identity_v1。
    """
    client = HttpDaemonRpcClient(LIVE_URL, verify_health=False)
    _check_success(client)


def test_invalid():
    """task.rollback {}（缺 task_id/step_id）：断言含 error。"""
    client = HttpDaemonRpcClient(LIVE_URL, verify_health=False)
    result = _safe_call(client, "task.rollback", {})
    assert "error" in result, f"缺参数应被拒绝（含 error）：{result!r}"


def test_authority():
    """task.rollback {task_id, step_id}（无 identity）：daemon 写前拒绝，error 含 IDENTITY。"""
    client = HttpDaemonRpcClient(LIVE_URL, verify_health=False)
    result = _safe_call(client, "task.rollback", {
        "task_id": TASK_ID,
        "step_id": STEP_ID,
    })
    assert "error" in result, f"无 identity 应被拒绝（含 error）：{result!r}"
    assert "IDENTITY" in result["error"].upper(), (
        f"错误应指明 identity 缺失：{result['error']!r}"
    )


def test_unavailable():
    """连死 URL（127.0.0.1:9）：必须抛错或返回 error dict，进程不崩。"""
    dead = HttpDaemonRpcClient(DEAD_URL, verify_health=False, timeout=2)
    _check_unavailable(dead)


def test_restart():
    """先复跑 unavailable（死链），再新建活 client 复跑 success（恢复）。"""
    dead = HttpDaemonRpcClient(DEAD_URL, verify_health=False, timeout=2)
    _check_unavailable(dead)
    live = HttpDaemonRpcClient(LIVE_URL, verify_health=False)
    _check_success(live)


# ----------------------------------------------------------------------
# 无 pytest 时的直接运行入口
# ----------------------------------------------------------------------

_CHECKS = [
    ("test_success", test_success),
    ("test_invalid", test_invalid),
    ("test_authority", test_authority),
    ("test_unavailable", test_unavailable),
    ("test_restart", test_restart),
]


if __name__ == "__main__":
    # 任务要求：__main__ 用 verify_health=False 的活 client 实例化。
    client = HttpDaemonRpcClient(LIVE_URL, verify_health=False)

    print("=== CLI-088 HTTP RPC 负向矩阵 ===")
    all_pass = True
    for name, fn in _CHECKS:
        try:
            fn()
            print(f"PASS  {name}")
        except Exception as exc:  # noqa: BLE001
            all_pass = False
            print(f"FAIL  {name}: {exc!r}")
    print("=== 总结:", "ALL PASS" if all_pass else "HAS FAILURES", "===")
    sys.exit(0 if all_pass else 1)
