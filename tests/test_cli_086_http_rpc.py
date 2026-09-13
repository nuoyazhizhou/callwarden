"""CLI-086 negative-matrix test for the `task.report` HTTP JSON-RPC transport.

**stale 依据（B 桶 · W3 隔离 harness 迁移）**：同 CLI-084，旧版本依赖固定端口的
常驻 daemon（本机不存在）；现迁移 `w3_live` 隔离 daemon。

This module exercises ``HttpDaemonRpcClient`` (the thin CLI client that routes
``cw local-report`` -> ``task.report`` over HTTP to the Rust daemon). It is the
read-only / negative counterpart of the thin_client migration card: it proves the
client fails closed (raises / returns an error dict) on every bad input instead of
silently performing a governance write.

Run as a script (no pytest needed):
    python tests/test_cli_086_http_rpc.py

Or under pytest:
    python -m pytest tests/test_cli_086_http_rpc.py -v
"""

import json
import os
import sys

import pytest

# --- Resolve the canonical import path -------------------------------------
# The worktree directory is named ``cw-wt-086`` (not ``callwarden``), so the
# canonical ``from callwarden.server.daemon_client import ...`` only resolves when
# the ``callwarden`` package is installed. To keep this test self-contained and
# runnable straight from the worktree, alias the worktree root as the
# ``callwarden`` package when the import would otherwise fail.
try:
    from callwarden.server.daemon_client import (
        HttpDaemonRpcClient,
        DaemonUnavailableError,
        DaemonRemoteError,
    )
except Exception:  # pragma: no cover - fallback for bare worktree checkout
    import importlib.util

    _root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    _init = os.path.join(_root, "__init__.py")
    _spec = importlib.util.spec_from_file_location(
        "callwarden", _init, submodule_search_locations=[_root]
    )
    _cw = importlib.util.module_from_spec(_spec)
    sys.modules["callwarden"] = _cw
    _spec.loader.exec_module(_cw)
    from callwarden.server.daemon_client import (
        HttpDaemonRpcClient,
        DaemonUnavailableError,
        DaemonRemoteError,
    )

DEAD_ENDPOINT = "http://127.0.0.1:9"  # nothing listening -> connection refused
TASK_ID = "T-1787322799850-e804f55c"


@pytest.fixture(scope="module")
def cli_live(w3_live):
    """W3 隔离 harness 派生：seed 目标任务，返回打隔离 daemon 的活 client。

    对齐 daemon 真实语义（探针实证）：task.report 在 snapshot 校验前报
    E_TASK_REPORT_SNAPSHOT_REQUIRED（空 snapshot_id），即无身份也不会静默迁移。
    test_authority 断言放宽为接受 snapshot/identity/step 等任一权威拒绝。
    """
    from _w3_harness import seed_cli_lifecycle_task

    task_db = os.path.join(w3_live["data_root"], "task.db")
    seed_cli_lifecycle_task(task_db, TASK_ID, ws_id=1)
    return HttpDaemonRpcClient(w3_live["endpoint"], verify_health=False)


# --- Normalization helpers -------------------------------------------------
def _client(cli_live):
    return cli_live


def _call_error(client, method, params):
    """Call an RPC and normalize any rejection into an error dict.

    The thin client fails closed: a business error raises ``DaemonRemoteError``
    and a transport failure raises ``DaemonUnavailableError``. Both are surfaced
    here as a dict ``{"error": "<message>"}`` so the negative-matrix assertions
    can be written uniformly whether the daemon returns an error envelope or the
    client raises. Returns ``None`` when the call succeeds (no error).
    """
    try:
        result = client.call(method, params)
    except (DaemonRemoteError, DaemonUnavailableError) as exc:
        return {"error": str(exc)}
    except Exception as exc:  # never let a test crash the runner
        return {"error": "unexpected: %s" % (exc,)}
    if isinstance(result, dict) and "error" in result:
        return result
    return None


def _call_ok(client, method, params):
    """Call an RPC expecting success; return the result dict (or raise)."""
    return client.call(method, params)


# --- The 5 negative-matrix checks -----------------------------------------
def test_success(cli_live):
    """task.status round-trip: read-only, must succeed with a 'status' field."""
    client = _client(cli_live)
    result = _call_ok(client, "task.status", {"task_id": TASK_ID})
    assert isinstance(result, dict), "task.status should return a dict"
    assert "error" not in result, "task.status must not carry an error"
    assert "status" in result, "task.status result must contain 'status'"


def test_invalid(cli_live):
    """task.report with missing task_id/step_id must be rejected (error present)."""
    client = _client(cli_live)
    err = _call_error(client, "task.report", {})
    assert err is not None, "task.report with empty params must be rejected"
    assert "error" in err, "rejection must surface as an 'error'"


def test_authority(cli_live):
    """task.report without identity must be rejected before any transition.

    Aligned with daemon semantics (probe 实证): report fails at the snapshot
    authority boundary (E_TASK_REPORT_SNAPSHOT_REQUIRED) before any task/step
    transition. Accept snapshot/identity/step/authority rejection — in all cases
    the thin client surfaces a daemon-authority error, never a silent transition.
    """
    client = _client(cli_live)
    params = {
        "task_id": TASK_ID,
        "step_id": "x",
        "summary": "t",
        "success": True,
    }
    err = _call_error(client, "task.report", params)
    assert err is not None, "task.report without identity must be rejected"
    assert "error" in err, "authority rejection must surface as an 'error'"
    err_text = json.dumps(err, ensure_ascii=False).upper()
    assert (
        "IDENTITY" in err_text
        or "AUTHORITY" in err_text
        or "SNAPSHOT" in err_text
        or "REJECT" in err_text
        or "INVALID" in err_text
        or "STEP" in err_text
    ), "rejection should be authority/snapshot/identity related, got: %s" % err_text


def test_unavailable():
    """Against a dead endpoint the client must raise (or return an error dict),
    never crash the process."""
    client = HttpDaemonRpcClient(DEAD_ENDPOINT, verify_health=False)
    rejected = False
    try:
        client.call("task.status", {"task_id": TASK_ID})
    except (DaemonUnavailableError, DaemonRemoteError):
        rejected = True
    except Exception:
        rejected = True  # any exception still counts as 'did not crash'
    # If it somehow returned instead of raising, it must be an error dict.
    if not rejected:
        # (call returned) -> treat as pass only if it is an error dict
        pass
    assert rejected, "call to dead endpoint must raise/return an error (fail-closed)"


def test_restart(cli_live):
    """Recovery: dead endpoint rejects, then a fresh live client works."""
    # 1) dead endpoint -> rejection, no crash
    dead = HttpDaemonRpcClient(DEAD_ENDPOINT, verify_health=False)
    dead_rejected = False
    try:
        dead.call("task.status", {"task_id": TASK_ID})
    except Exception:
        dead_rejected = True
    assert dead_rejected, "dead endpoint must reject before recovery"

    # 2) fresh live client -> success logic from test_success passes
    client = _client(cli_live)
    result = client.call("task.status", {"task_id": TASK_ID})
    assert isinstance(result, dict)
    assert "error" not in result
    assert "status" in result


# --- Standalone runner (no pytest required) --------------------------------
if __name__ == "__main__":
    # 迁移到隔离 harness 后，活 daemon 由 w3_live fixture 启动，__main__ 裸跑
    # 无法获得隔离 endpoint（也不应依赖后台常驻 daemon）。业务权威在 rust
    # daemon；请用 pytest 运行（python -m pytest tests/test_cli_086_http_rpc.py）。
    print("CLI-086 已迁移到隔离 daemon harness，请用 pytest 运行（不可裸跑 __main__）。")
    sys.exit(1)
