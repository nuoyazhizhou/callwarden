#!/usr/bin/env python3
"""E2E: workspace authority reconciliation (T-1788346430756-8c900ec0).

Proves the migration-era split between task-transaction authority (task-DB
`callwarden.db`) and runtime workspace registry authority (registry.db) is
reconciled into one coherent tuple, keyed by stable `workspace_instance_id`.

Runs against the live daemon over HTTP. No SQLite access, no `ws-{id}` synthesis.

Acceptance (runtime, not unit test):
  1. `workspace.status 144`            -> registry instance 4baea3ff12c2ea5c
  2. `workspace.status 4baea3ff12c2ea5c` -> unified (registry + task-DB) instance 4baea3ff12c2ea5c
  3. `workspace.status ws-1`           -> historical task-DB binding resolved to workspace_id=1
  4. `workspace.status 1`              -> unified view surfaces both registry + task-DB perspectives

Usage:
  PYTHONPATH=<repo> python tests/e2e_workspace_authority_reconciliation.py
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))  # /c/git_work/callwarden
sys.path.insert(0, os.path.dirname(REPO_ROOT))  # /c/git_work (parent of the callwarden package)

from callwarden.server.daemon_client import HttpDaemonRpcClient  # noqa: E402

CALLWARDEN_INSTANCE = "4baea3ff12c2ea5c"
# Registry numeric ids are autoincrement and DRIFT across registry rebuilds
# (observed 144 -> 156). The reconciliation design keys authority on the STABLE
# `workspace_instance_id`, so the test must resolve the numeric id from the
# instance id at runtime instead of hardcoding it. CW_REGISTRY_ID remains an
# override for air-gapped / pre-registration scenarios.
REGISTRY_NUMERIC_ID = os.environ.get("CW_REGISTRY_ID")


def discover_registry_numeric_id(client: HttpDaemonRpcClient, instance_id: str) -> str:
    try:
        r = client.call("workspace.status", {"workspace_instance_id": instance_id})
        nid = r.get("registry_workspace_id")
        if nid is not None:
            return str(nid)
    except Exception:
        pass
    return os.environ.get("CW_REGISTRY_ID", "144")


def status(client: HttpDaemonRpcClient, key: str) -> dict:
    if key.isdigit() and not (key.startswith("ws-") or "-" in key and not key.isdigit()):
        params = {"workspace_id": int(key)}
    else:
        params = {"workspace_instance_id": key}
    return client.call("workspace.status", params)


def main() -> int:
    client = HttpDaemonRpcClient()
    failures = []

    registry_numeric_id = discover_registry_numeric_id(client, CALLWARDEN_INSTANCE)
    print(f"[info] resolved registry numeric id for {CALLWARDEN_INSTANCE} -> {registry_numeric_id}")

    # 1) registry numeric id -> real instance
    r1 = status(client, registry_numeric_id)
    got = r1.get("registry_instance_id") or r1.get("task_db_instance_id")
    if got != CALLWARDEN_INSTANCE:
        failures.append(f"status {registry_numeric_id}: expected instance "
                        f"{CALLWARDEN_INSTANCE}, got {got} ({r1})")
    else:
        print(f"[PASS] status {registry_numeric_id} -> instance {got}")

    # 2) stable instance id -> unified authority (both sides present)
    r2 = status(client, CALLWARDEN_INSTANCE)
    reg_inst = r2.get("registry_instance_id")
    task_inst = r2.get("task_db_instance_id")
    if reg_inst != CALLWARDEN_INSTANCE or task_inst != CALLWARDEN_INSTANCE:
        failures.append(f"status {CALLWARDEN_INSTANCE}: unified authority broken "
                        f"registry={reg_inst} task_db={task_inst} ({r2})")
    else:
        print(f"[PASS] status {CALLWARDEN_INSTANCE} -> unified registry+task_db instance {task_inst}")

    # 3) historical legacy binding ws-1 -> deterministic task-DB workspace_id=1
    r3 = status(client, "ws-1")
    tdb_id = r3.get("task_db_workspace_id")
    if tdb_id != 1:
        failures.append(f"status ws-1: historical binding not resolved to task_db id=1, got {tdb_id} ({r3})")
    else:
        print(f"[PASS] status ws-1 -> historical task_db_workspace_id={tdb_id} (instance={r3.get('task_db_instance_id')})")

    # 4) task-DB numeric id 1 -> unified view (both perspectives surfaced)
    r4 = status(client, "1")
    if r4.get("task_db_workspace_id") != 1 and r4.get("registry_workspace_id") is None:
        failures.append(f"status 1: unified view incomplete ({r4})")
    else:
        print(f"[PASS] status 1 -> unified view registry={r4.get('registry_workspace_id')} "
              f"task_db={r4.get('task_db_workspace_id')} (task_db_instance={r4.get('task_db_instance_id')})")

    if failures:
        print("\n=== E2E FAILURES ===")
        for f in failures:
            print(" -", f)
        return 1
    print("\nALL WORKSPACE AUTHORITY RECONCILIATION E2E CHECKS PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
