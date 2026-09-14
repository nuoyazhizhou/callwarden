"""P0-COMPAT-v3（T-1788963088148-495d7208）identity-lease-small 组 → Rust daemon native。

覆盖方法（3）：get_attestation_validity / list_attestation_revocations / assignment_show。

覆盖 task 要求：
  success / invalid 参数 / daemon unavailable（fail-closed）矩阵；
  Python compat 退役断言（_P3_READ_ONLY_METHODS / _P4_READ_ONLY_METHODS 已摘除条目）。

语义对照（Python 真相源）：
- get_attestation_validity：db.derive_attestation_validity —— compromised 一律
  invalid；rotated 仅 issuance_time > revoked_at 判 invalid；无命中 "valid"。
- list_attestation_revocations：db.list_attestation_revocations —— workspace 内
  全行（issuer/signing_key_id 可选过滤），revoked_at ASC，{"items", "count"}。
- assignment_show：db.get_assignment —— active assignment 按 id DESC LIMIT 1，
  role 可选过滤；无命中 {"status": "none", task_id, role}。
"""

import os
import sqlite3

import pytest

from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.config import get_http_authority_id


@pytest.fixture()
def live_daemon(w3_live):
    """W3 隔离 harness：注入隔离 daemon 的 client / inst / endpoint。"""
    global CANONICAL_INSTANCE, _CANONICAL_ENDPOINT
    CANONICAL_INSTANCE = w3_live["inst"]
    _CANONICAL_ENDPOINT = w3_live["endpoint"]
    return w3_live["client"]


@pytest.fixture()
def bound_task(live_daemon, w3_live):
    """C-18：向隔离 task.db 追加 well-known 已绑定 task（C-16 权威 resolver 语义下，
    无 binding 的 assignment_show 一律 E_TASK_WORKSPACE_UNBOUND，不得静默 none）。"""
    from _w3_harness import seed_task_workspace_binding, W3_ASSIGN_SHOW_BOUND_TASK

    conn = sqlite3.connect(os.path.join(w3_live["data_root"], "task.db"), timeout=10)
    try:
        seed_task_workspace_binding(conn, 1)
        conn.commit()
    finally:
        conn.close()
    return W3_ASSIGN_SHOW_BOUND_TASK


# ---------------------------------------------------------------------------
# get_attestation_validity：派生语义（无撤销记录 → valid）
# ---------------------------------------------------------------------------
def test_attestation_validity_no_revocations(live_daemon):
    """无撤销记录 → validity=valid（查询时刻派生，不持久化）。"""
    c = live_daemon
    r = c.call("get_attestation_validity", {
        "workspace_id": 1,
        "issuer": "no-such-issuer",
        "signing_key_id": "no-such-key",
        "issuance_time": 1.0,
    })
    assert isinstance(r, dict)
    assert r.get("validity") == "valid"


def test_attestation_validity_missing_params(live_daemon):
    """缺省参数（空 issuer/key）→ 仍派生 valid（与 Python 缺省语义一致）。"""
    c = live_daemon
    r = c.call("get_attestation_validity", {"workspace_id": 1})
    assert isinstance(r, dict)
    assert r.get("validity") == "valid"


# ---------------------------------------------------------------------------
# list_attestation_revocations：账本查询（空账本 → items=[] count=0）
# ---------------------------------------------------------------------------
def test_revocations_list_empty(live_daemon):
    """空账本/无匹配 → items=[] count=0（不报错）。"""
    c = live_daemon
    r = c.call("list_attestation_revocations", {
        "workspace_id": 1,
        "issuer": "no-such-issuer",
        "signing_key_id": "no-such-key",
    })
    assert isinstance(r, dict)
    assert r.get("count") == 0
    assert r.get("items") == []


def test_revocations_list_workspace_filter(live_daemon):
    """未知 workspace → 空列表（不报错，fail-soft 只读语义）。"""
    c = live_daemon
    r = c.call("list_attestation_revocations", {"workspace_id": 999999})
    assert isinstance(r, dict)
    assert r.get("count") == 0


# ---------------------------------------------------------------------------
# assignment_show：active assignment 查询
# ---------------------------------------------------------------------------
def test_assignment_show_no_match(live_daemon, bound_task):
    """已绑定 task 无 active assignment → status=none（C-18：task 须先有 binding）。"""
    c = live_daemon
    r = c.call("assignment_show", {"workspace_id": 1, "task_id": bound_task})
    assert isinstance(r, dict)
    assert r.get("status") == "none"


def test_assignment_show_role_filter(live_daemon, bound_task):
    """role 过滤无匹配 → status=none 且回显 role。"""
    c = live_daemon
    r = c.call("assignment_show",
               {"workspace_id": 1, "task_id": bound_task, "role": "reviewer"})
    assert isinstance(r, dict)
    assert r.get("status") == "none"
    assert r.get("role") == "reviewer"


# ---------------------------------------------------------------------------
# daemon unavailable：fail-closed，绝不降级本地 SQLite
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("method", [
    "get_attestation_validity",
    "list_attestation_revocations",
    "assignment_show",
])
def test_compat_methods_daemon_unavailable_fail_closed(method):
    c = HttpDaemonRpcClient(endpoint="http://127.0.0.1:9",
                            authority_id=get_http_authority_id())
    # DaemonUnavailableError 异常类型本身即 fail-closed 证据
    # （错误消息随 client health 探测路径变化：E_HTTP_DAEMON_UNAVAILABLE / 502 等）。
    with pytest.raises(DaemonUnavailableError):
        c.call(method, {"workspace_id": 1})


# ---------------------------------------------------------------------------
# Python compat 退役断言：条目已从只读白名单摘除
# ---------------------------------------------------------------------------
def test_python_compat_entries_retired():
    from callwarden.server.tools import tools_p3_identity, tools_p4_lease
    assert "get_attestation_validity" not in tools_p3_identity._P3_READ_ONLY_METHODS
    assert "list_attestation_revocations" not in tools_p3_identity._P3_READ_ONLY_METHODS
    assert "assignment_show" not in tools_p4_lease._P4_READ_ONLY_METHODS
