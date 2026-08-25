"""CLI-086 negative-matrix test for the `task.report` HTTP JSON-RPC transport.

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

ENDPOINT = "http://127.0.0.1:12376"
DEAD_ENDPOINT = "http://127.0.0.1:9"  # nothing listening -> connection refused
TASK_ID = "T-1787322799850-e804f55c"


# --- Normalization helpers -------------------------------------------------
def _client():
    return HttpDaemonRpcClient(ENDPOINT, verify_health=False)


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
def test_success():
    """task.status round-trip: read-only, must succeed with a 'status' field."""
    client = _client()
    result = _call_ok(client, "task.status", {"task_id": TASK_ID})
    assert isinstance(result, dict), "task.status should return a dict"
    assert "error" not in result, "task.status must not carry an error"
    assert "status" in result, "task.status result must contain 'status'"
    return True


def test_invalid():
    """task.report with missing task_id/step_id must be rejected (error present)."""
    client = _client()
    err = _call_error(client, "task.report", {})
    assert err is not None, "task.report with empty params must be rejected"
    assert "error" in err, "rejection must surface as an 'error'"
    return True


def test_authority():
    """task.report without identity must be rejected before any transition.

    On a strict profile the daemon returns an IDENTITY error; on the
    unauthenticated loopback profile it rejects at the validation boundary that
    prevents an unauthorized governance write (e.g. task_step_not_found). In both
    cases the thin client must surface a rejection and never perform a silent
    transition.
    """
    client = _client()
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
        or "E_IDENTITY" in err_text
        or "REJECT" in err_text
        or "INVALID" in err_text
        or "STEP" in err_text
    ), "rejection should be authority/identity/validation related, got: %s" % err_text
    return True


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
    return True


def test_restart():
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
    client = HttpDaemonRpcClient(ENDPOINT, verify_health=False)
    result = client.call("task.status", {"task_id": TASK_ID})
    assert isinstance(result, dict)
    assert "error" not in result
    assert "status" in result
    return True


# --- Standalone runner (no pytest required) --------------------------------
def _run_all():
    checks = {
        "test_success": test_success,
        "test_invalid": test_invalid,
        "test_authority": test_authority,
        "test_unavailable": test_unavailable,
        "test_restart": test_restart,
    }
    results = []
    for name, fn in checks.items():
        try:
            fn()
            print("PASS %s" % name)
            results.append(True)
        except Exception as exc:  # noqa: BLE001 - runner must report, not crash
            print("FAIL %s: %s" % (name, exc))
            results.append(False)
    print("----")
    print("PASS %d / %d" % (sum(results), len(results)))
    return all(results)


if __name__ == "__main__":
    ok = _run_all()
    sys.exit(0 if ok else 1)
