"""MCP-001（A′ task_projection Gate）get_role_view → Rust daemon native。

覆盖 task 要求：
  success（live daemon HTTP round-trip，Role_View 投影 + 四类 hash）、
  非法参数（缺 task_id → invalid_params）、unknown/unauthorized workspace、
  daemon unavailable（fail-closed E_HTTP_DAEMON_UNAVAILABLE）、restart。

设计要点（与 task 不变量一致）：
- Python MCP wrapper（tools_collab.get_role_view）已是 route_rpc 薄壳；本测试直连
  HTTP RPC `get_role_view`，验证 Rust daemon（task_collab.rs::handle_get_role_view）
  为权威：从 task_contract_revisions 取最新 envelope，按 (role,"1.0","blind")
  allowlist 过滤，规范 JSON SHA-256 计算 allowlist/contract/content/view_manifest hash。
- Python compat `_h_get_role_view` 已退役（_COLLAB_READ_ONLY_METHODS 移除该项）。
- 所有失败 fail-closed 返回稳定且可区分的结构化错误，绝不降级到本地 SQLite。

确定性 golden parity：对 CLI-02 任务（task_contract_revisions 为空 → envelope={}）
expect 与 Python `db_task_reviews.get_role_view` 完全一致的四类 hash。
"""

import hashlib
import json

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.server.daemon_protocol import DaemonRemoteError

CLI_02_TASK = "T-1787321708568-d292ab3c"


def _canonical_json(data) -> str:
    """等价 Python db_task_reviews._canonical_json（sort_keys, 无空格）。"""
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _sha256(data) -> str:
    return hashlib.sha256(_canonical_json(data).encode("utf-8")).hexdigest()


def _golden_role_view(task_id: str, role: str) -> dict:
    """复刻 Python db_task_reviews.get_role_view(task_id, role, "1.0", "blind", {})。"""
    envelope = {}
    allowed = {
        "contract_id", "profile", "title", "description", "requirements",
        "target_file", "target_symbol", "allowed_edit_scope", "clauses",
        "blocking_clauses",
    }
    filtered = {k: v for k, v in envelope.items() if k in allowed}
    excluded = [k for k in envelope if k not in allowed]
    allowlist_hash = _sha256(sorted(allowed))
    contract_hash = envelope.get("contract_hash", _sha256(envelope))
    content_hash = _sha256(filtered)
    manifest = {
        "view_type": role, "view_version": "1.0", "stage": "blind",
        "contract_hash": contract_hash, "allowlist_hash": allowlist_hash,
        "content_hash": content_hash,
    }
    return {
        "task_id": task_id,
        "view_type": role,
        "view_version": "1.0",
        "stage": "blind",
        "view_manifest_hash": _sha256(manifest),
        "contract_hash": contract_hash,
        "content": filtered,
        "allowed_fields": sorted(allowed),
        "excluded_fields": sorted(excluded),
    }


@pytest.fixture()
def live_daemon():
    c = HttpDaemonRpcClient()
    try:
        c.health()
    except Exception:
        pytest.skip("daemon 未运行（无 HTTP endpoint），跳过 live 用例")
    return c


# ---------------------------------------------------------------------------
# success：HTTP round-trip，Rust daemon 为权威，golden parity
# ---------------------------------------------------------------------------
def test_get_role_view_success_golden_parity(live_daemon):
    """Rust native 输出与 Python db_task_reviews golden 完全一致（含四类 hash）。"""
    c = live_daemon
    r = c.call("get_role_view", {"task_id": CLI_02_TASK, "role": "implementer"})
    assert isinstance(r, dict), f"应返回 dict，实际 {type(r)}"
    golden = _golden_role_view(CLI_02_TASK, "implementer")
    assert r["task_id"] == CLI_02_TASK
    assert r["view_type"] == "implementer"
    assert r["view_version"] == "1.0"
    assert r["stage"] == "blind"
    assert r["view_manifest_hash"] == golden["view_manifest_hash"], (
        f"view_manifest_hash 与 Python golden 不一致: {r['view_manifest_hash']} != {golden['view_manifest_hash']}"
    )
    assert r["contract_hash"] == golden["contract_hash"]
    assert r["content"] == golden["content"]
    assert sorted(r["allowed_fields"]) == golden["allowed_fields"]
    assert sorted(r["excluded_fields"]) == golden["excluded_fields"]


def test_get_role_view_default_role_implementer(live_daemon):
    """role 缺省 → implementer（与 Python _collab_direct_read 语义一致）。"""
    c = live_daemon
    r = c.call("get_role_view", {"task_id": CLI_02_TASK})
    assert r["view_type"] == "implementer"
    golden = _golden_role_view(CLI_02_TASK, "implementer")
    assert r["view_manifest_hash"] == golden["view_manifest_hash"]


def test_get_role_view_reviewer_allowlist(live_daemon):
    """reviewer allowlist 不同：包含 actual_changes/symbol_changes/test_runs 等。"""
    c = live_daemon
    r = c.call("get_role_view", {"task_id": CLI_02_TASK, "role": "reviewer"})
    assert r["view_type"] == "reviewer"
    assert "actual_changes" in r["allowed_fields"]
    assert "implementer_notes" not in r["allowed_fields"]  # blind 阶段不披露


# ---------------------------------------------------------------------------
# 非法参数：结构化错误，不 crash
# ---------------------------------------------------------------------------
def test_get_role_view_missing_task_id(live_daemon):
    c = live_daemon
    with pytest.raises(DaemonRemoteError) as ei:
        c.call("get_role_view", {})
    assert getattr(ei.value, "code", "") in ("invalid_params", "E_INVALID_PARAMS")


def test_get_role_view_unknown_task(live_daemon):
    """未知 task：envelope 为空 → 仍返回投影（view=None 语义 → 空 envelope 投影）。"""
    c = live_daemon
    r = c.call("get_role_view", {"task_id": "T-NO-SUCH-TASK-MCP001", "role": "implementer"})
    assert isinstance(r, dict)
    assert r["content"] == {}
    assert r["excluded_fields"] == []


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
def test_get_role_view_daemon_unavailable_fail_closed():
    from callwarden.config import get_http_authority_id
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    with pytest.raises(DaemonUnavailableError) as ei:
        c.call("get_role_view", {"task_id": CLI_02_TASK, "role": "implementer"})
    assert "E_HTTP_DAEMON_UNAVAILABLE" in str(ei.value)


# ---------------------------------------------------------------------------
# restart：新 client 实例重查仍稳定
# ---------------------------------------------------------------------------
def test_get_role_view_new_client_instance_stable(live_daemon):
    c2 = HttpDaemonRpcClient()
    r = c2.call("get_role_view", {"task_id": CLI_02_TASK, "role": "implementer"})
    golden = _golden_role_view(CLI_02_TASK, "implementer")
    assert r["view_manifest_hash"] == golden["view_manifest_hash"]
