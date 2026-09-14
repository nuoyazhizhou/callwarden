"""CLI governance gate 与 assignment 空 role 防护的回归测试。

背景（2026-09-10 外部 agent 审计暴露的三处缺口）：
  1. `cw task report` 的 identity 是可选参数；缺省时 daemon 侧 report_role
     为空串，完成补偿写 `persist_claimed_assignment` 会用同一规范
     assignment_id 覆写 claim 行，把 role 清空（实测污染 17 行，含 G4 卡）。
     → Rust 侧 fail-closed 回填（见 assignment_queue.rs 单元测试）。
  2. daemon role prompt 权威会正确判 `BLOCKED / valid_for_claim=false`，
     但 CLI lease/task 写通道没有任何硬门禁，外部 agent 会试错式推进。
     → 新增 `_governance_gate`，在 lease acquire / task report 前拒绝
     `governance_blocked` 的卡。
  3. authority context 新增 `identity_discovery` 投影，把已注册身份与
     注册方式写进 prompt，避免外部 agent 编造 `executor-1` 之类野身份。

本文件覆盖 CLI 侧 gate 的纯逻辑（daemon 交互以 stub 注入）。
"""

import importlib
import sys

import pytest


@pytest.fixture()
def cli_main():
    """导入 callwarden.cli.main（不触发 argparse 求值）。"""
    mod = importlib.import_module("callwarden.cli.main")
    return mod


def _stub_daemon_client(monkeypatch, cli_main, *, mode="auto", projection=None, raises=None):
    """把 _governance_gate 依赖的 daemon 客户端与本模块替身化。"""
    client_mod = importlib.import_module("callwarden.server.daemon_client")

    class _StubClient:
        def call(self, method, params):
            if raises is not None:
                raise raises
            assert method == "task.governance_projection.get", method
            return projection

        def call_with_autostart(self, method, params):
            return self.call(method, params)

    class _StubCls:
        @staticmethod
        def get_instance():
            return _StubClient()

        def __init__(self, *a, **kw):
            pass

        def call(self, method, params):
            return _StubClient().call(method, params)

        def call_with_autostart(self, method, params):
            return _StubClient().call(method, params)

    monkeypatch.setattr(cli_main, "get_daemon_mode", lambda: mode)
    monkeypatch.setattr(client_mod, "UnixDaemonRpcClient", _StubCls, raising=False)
    monkeypatch.setattr(client_mod, "HttpDaemonRpcClient", _StubCls, raising=False)
    monkeypatch.setattr(client_mod, "is_http_transport_enabled", lambda: False, raising=False)
    return client_mod


def test_gate_passes_on_non_blocked_task(cli_main, monkeypatch):
    """workflow_status != governance_blocked → 放行（None）。"""
    _stub_daemon_client(
        monkeypatch, cli_main,
        projection={"workflow_status": "active", "blocking_reason": None},
    )
    assert cli_main._governance_gate("T-ok", "lease acquire") is None


def test_gate_blocks_on_governance_blocked(cli_main, monkeypatch):
    """governance_blocked → 拒绝，错误码稳定且带处置指引。"""
    _stub_daemon_client(
        monkeypatch, cli_main,
        projection={
            "workflow_status": "governance_blocked",
            "blocking_reason": "Task Contract 缺失",
        },
    )
    reason = cli_main._governance_gate("T-bad", "lease acquire")
    assert reason is not None
    assert reason["code"] == "E_TASK_GOVERNANCE_BLOCKED"
    assert "supersede" in reason["detail"]
    assert reason["workflow_status"] == "governance_blocked"


def test_gate_is_noop_in_local_mode(cli_main, monkeypatch):
    """local 模式不做门禁（保留离线调试行为）。"""
    monkeypatch.setattr(cli_main, "get_daemon_mode", lambda: "local")
    assert cli_main._governance_gate("T-any", "task report") is None


def test_gate_fails_open_on_daemon_unreachable(cli_main, monkeypatch):
    """daemon 不可达时不阻断（门禁是附加保护，不是新单点）。"""
    _stub_daemon_client(monkeypatch, cli_main, raises=RuntimeError("boom"))
    assert cli_main._governance_gate("T-any", "task report") is None


def test_gate_tolerates_non_dict_projection(cli_main, monkeypatch):
    """投影形状异常时放行，不因附加保护破坏主流程。"""
    _stub_daemon_client(monkeypatch, cli_main, projection="not-a-dict")
    assert cli_main._governance_gate("T-any", "task report") is None


def test_gate_reads_blocking_reasons_list(cli_main, monkeypatch):
    """blocking_reasons（复数）也应被识别为阻断原因。"""
    _stub_daemon_client(
        monkeypatch, cli_main,
        projection={
            "workflow_status": "governance_blocked",
            "blocking_reasons": ["Task Contract 缺失、多版本冲突"],
        },
    )
    reason = cli_main._governance_gate("T-bad", "task report")
    assert reason is not None
    assert "Task Contract 缺失" in str(reason["blocking_reason"])


# ---------------------------------------------------------------------------
# 2026-09-10 回归：gate 作用域过宽导致裸卡**不可修复**（自锁死锁）
#
# 背景：`task.contract-bootstrap`（P0-L 自举修复，专治 governance projection
# 为空的裸卡）、`task.contract-revise`、`task.attest-legacy-workspace-binding`
# 与 `task.supersede` 均要求**该卡上的 reviewer lease token + fencing_counter**。
# 初版 `_governance_gate` 对所有 role 一律拦截 lease acquire，把 reviewer 修复
# 票也拦了 → 裸卡再也无法被任何路径修复。
#
# 修正：门禁只拦"认领工作"的写通道（executor claim / report），role=reviewer
# 必须放行。以下用例锁定该作用域，防止再次收窄成死锁。
# ---------------------------------------------------------------------------


def test_gate_allows_reviewer_role_on_blocked_task(cli_main, monkeypatch):
    """reviewer 租约是治理修复路径的前置票 → governance_blocked 也必须放行。"""
    _stub_daemon_client(
        monkeypatch, cli_main,
        projection={
            "workflow_status": "governance_blocked",
            "blocking_reason": "Task Contract 缺失",
        },
    )
    assert cli_main._governance_gate("T-naked", "lease acquire", role="reviewer") is None


def test_gate_allows_legacy_independent_reviewer_alias(cli_main, monkeypatch):
    """legacy runtime 别名 independent_reviewer 同样放行。"""
    _stub_daemon_client(
        monkeypatch, cli_main,
        projection={"workflow_status": "governance_blocked"},
    )
    assert (
        cli_main._governance_gate("T-naked", "lease acquire", role="independent_reviewer")
        is None
    )


def test_gate_still_blocks_executor_on_blocked_task(cli_main, monkeypatch):
    """executor 认领仍必须被拦（这才是污染来源）。"""
    _stub_daemon_client(
        monkeypatch, cli_main,
        projection={
            "workflow_status": "governance_blocked",
            "blocking_reason": "Task Contract 缺失",
        },
    )
    reason = cli_main._governance_gate("T-naked", "lease acquire", role="executor")
    assert reason is not None
    assert reason["code"] == "E_TASK_GOVERNANCE_BLOCKED"


def test_gate_reviewer_exemption_does_not_need_daemon(cli_main, monkeypatch):
    """reviewer 放行是本地短路，不依赖 daemon 可达（修复路径不得被网络抖动阻断）。"""
    _stub_daemon_client(monkeypatch, cli_main, raises=RuntimeError("daemon down"))
    assert cli_main._governance_gate("T-naked", "lease acquire", role="reviewer") is None


# ---------------------------------------------------------------------------
# 2026-09-10 回归：`task supersede` 缺 --agent-instance-id → 恢复路径对注册身份不可用
#
# 背景：恢复裸卡的治理路径是 supersede（源卡 reviewer lease + adjudicator 身份）。
# 但 supersede_p 手工定义了 agent_id/session_id/model_id/role 四字段以把 role 限死
# adjudicator，却漏了 agent_instance_id；而身份注册强制非空 agent_instance_id，
# daemon 的 verify_registered_identity 遂报
#   E_IDENTITY_INSTANCE_MISMATCH: 注册 instance X 与本次  不一致
# → 该路径对任何正规注册身份完全不可用（实证：收编 P0-CR 裸卡时命中）。
# 其余修复入口（contract-bootstrap / contract-revise /
# attest-legacy-workspace-binding）均已暴露该参数，仅 supersede 缺失。
# ---------------------------------------------------------------------------


def test_supersede_parser_exposes_agent_instance_id(cli_main):
    """supersede 子命令必须注册 --agent-instance-id（回归守卫）。

    此前遗漏该参数 → 已注册身份（注册强制非空 agent_instance_id）调用
    task.supersede 一律 E_IDENTITY_INSTANCE_MISMATCH，裸卡 supersede 收口路径
    对正规注册身份完全不可用（实证：收编 P0-CR 裸卡时命中）。
    其余修复入口（contract-bootstrap / contract-revise /
    attest-legacy-workspace-binding）均已暴露该参数，仅 supersede 缺失。

    说明：task 子 parser 在 cli/main.py 内部按 sys.argv 惰性构建，独立构造
    parser 需伪造 sys.argv 且经 daemon 路由，故此处做**源码级结构守卫**：
    锁定 supersede 子 parser 的参数注册块中同时存在四字段与 --agent-instance-id。
    """
    import inspect

    src = inspect.getsource(cli_main)
    anchor = "supersede_p = sub.add_parser("
    idx = src.find(anchor)
    assert idx != -1, "未找到 supersede 子 parser 定义（cli/main.py 结构已变）"
    # 截到下一个子 parser 定义之前的片段
    nxt = src.find("add_parser(", idx + len(anchor))
    block = src[idx : nxt if nxt != -1 else idx + 8000]
    for flag in ("--agent-id", "--session-id", "--model-id", "--role"):
        assert f'"{flag}"' in block, f"supersede 缺少 {flag}"
    assert '"--agent-instance-id"' in block, (
        "supersede 缺 --agent-instance-id → 已注册身份调用必报 E_IDENTITY_INSTANCE_MISMATCH"
    )
    # role 仍严格限定 adjudicator（P0-H 权限边界不得回归）
    assert 'choices=["adjudicator"]' in block, "supersede --role 必须仍只允许 adjudicator"
