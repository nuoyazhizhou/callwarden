#!/usr/bin/env python3
"""Fresh-process HTTP E2E for the daemon-authoritative lease recovery route.

The test uses the current task as a task-bound probe.  It obtains the immutable
workspace tuple from ``task.status`` and never opens SQLite or synthesizes a
workspace id.  Raw lease tokens are kept in memory only long enough to create
an expired lease and to release the healthy negative-case lease.

Usage::

    python tests/e2e_lease_recovery.py

``CW_RECOVERY_TASK_ID`` may select another task with a currently claimed
executor assignment.  The default is the task that owns this implementation.
The script deliberately does not apply or close the task.
"""

from __future__ import annotations

import os
import sys
import time
import uuid

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO_ROOT))

from callwarden.server.daemon_client import HttpDaemonRpcClient  # noqa: E402
from callwarden.server.daemon_protocol import DaemonRemoteError  # noqa: E402


TASK_ID = os.environ.get(
    "CW_RECOVERY_TASK_ID", "T-1788447967354-616aa470"
)
PLANNER_ID = f"e2e-recovery-planner-{uuid.uuid4().hex[:12]}"
EXECUTOR_ID = f"e2e-recovery-executor-{uuid.uuid4().hex[:12]}"
PLANNER_SESSION = f"e2e-recovery-planner-session-{uuid.uuid4().hex[:12]}"
EXECUTOR_SESSION = f"e2e-recovery-executor-session-{uuid.uuid4().hex[:12]}"


def identity(agent_id: str, instance_id: str, session_id: str, role: str) -> dict:
    return {
        "agent_id": agent_id,
        "agent_instance_id": instance_id,
        "client_id": "lease-recovery-e2e",
        "provider": "openai",
        "model_id": "gpt-5.6",
        "model_mode": "agent",
        "system_fingerprint": "e2e",
        "session_id": session_id,
        "role": role,
        "runtime_hash": "lease-recovery-e2e",
    }


def register(client: HttpDaemonRpcClient, agent: dict) -> None:
    client.call(
        "agent.register",
        {
            "agent_id": agent["agent_id"],
            "agent_name": agent["agent_id"],
            "capabilities": ["lease-recovery-e2e"],
            "identity": agent,
        },
    )


def recover_params(
    task_id: str, workspace_id: int, workspace_instance_id: str,
    planner: dict, request_id: str,
) -> dict:
    return {
        "task_id": task_id,
        "role": "executor",
        "workspace_id": workspace_id,
        "workspace_instance_id": workspace_instance_id,
        "request_id": request_id,
        "reason": "fresh-process E2E previous executor session disappeared",
        "identity": planner,
    }


def main() -> int:
    client = HttpDaemonRpcClient()
    status = client.call("task.status", {"task_id": TASK_ID})
    workspace_id = int(status["workspace_id"])
    workspace_instance_id = status["workspace_instance_id"]
    step_id = status["governance"]["step_id"]
    if not workspace_instance_id or not step_id:
        raise AssertionError("task.status 未返回不可变 workspace tuple 与 current step")

    planner = identity(
        PLANNER_ID,
        f"{PLANNER_ID}-instance",
        PLANNER_SESSION,
        "planner",
    )
    executor = identity(
        EXECUTOR_ID,
        f"{EXECUTOR_ID}-instance",
        EXECUTOR_SESSION,
        "executor",
    )
    register(client, planner)
    register(client, executor)

    # Claim the exact current step through the public task route.  This makes
    # the probe's lease and durable assignment pair explicit and lets the
    # recovery assertion identify that pair instead of guessing from a task-
    # level "current" projection that may contain older queued steps.
    client.call(
        "task.claim",
        {
            "task_id": TASK_ID,
            "agent_session_id": EXECUTOR_SESSION,
            "identity": executor,
            "contract_claim": {
                "skill_id": "cw-executor-senior-engineer",
                "skill_version": "v1",
                "prompt_hash": "",
                "task_contract_id": status["governance"]["task_contract"]["id"],
                "task_contract_revision": status["governance"]["task_contract"]["revision"],
                "task_contract_hash": status["governance"]["task_contract"]["hash"],
                "step_id": step_id,
            },
        },
    )
    assignment_before = client.call(
        "task.assignment.status", {"task_id": TASK_ID, "role": "executor"}
    ).get("current_assignment")
    if not assignment_before or assignment_before.get("status") != "claimed":
        raise AssertionError(f"task.claim 未建立当前 executor assignment: {assignment_before}")

    # Positive: a one-second lease becomes expired in the authoritative daemon
    # clock.  The current task already has a claimed executor assignment, so
    # recovery must stale that assignment in the same transaction.
    acquired = client.call(
        "lease.acquire",
        {
            "task_id": TASK_ID,
            "role": "executor",
            "ttl_seconds": 1.0,
            "identity": executor,
        },
    )
    token = acquired.get("token")
    if not token:
        raise AssertionError("lease.acquire 未返回一次性 raw token")
    fencing = acquired.get("fencing_counter")
    time.sleep(2.2)

    request_id = f"e2e-lease-recover-{uuid.uuid4().hex}"
    params = recover_params(
        TASK_ID, workspace_id, workspace_instance_id, planner, request_id
    )
    recovered = client.call("lease.recover", params, request_id=request_id)
    assert recovered["lease_status"] == "expired", recovered
    assert recovered["assignment_status"] == "stale", recovered
    assert recovered["recovery_reason"] == "lease_expired", recovered
    assert recovered["replayed"] is False, recovered
    assert "token" not in recovered and "lease_token" not in recovered

    assignment = client.call(
        "task.assignment.status", {"task_id": TASK_ID, "role": "executor"}
    )
    recovered_assignment = next(
        (
            item for item in assignment.get("assignments", [])
            if item.get("assignment_id") == assignment_before.get("assignment_id")
        ),
        None,
    )
    if not recovered_assignment or recovered_assignment.get("status") != "stale":
        raise AssertionError(
            "recovery 后与 lease 配对的 assignment 未变 stale: "
            f"expected={assignment_before.get('assignment_id')} got={recovered_assignment}"
        )

    replay = client.call("lease.recover", params, request_id=request_id)
    # The HTTP transport's durable dedup layer intercepts a duplicate envelope
    # before Rust dispatch and returns the original result verbatim.  Therefore
    # the transport replay keeps the original ``replayed=false`` marker; the
    # proof of idempotency is stable response plus unchanged assignment/lease
    # projections, not a client-invented marker.
    assert replay == recovered, {"first": recovered, "replay": replay}

    # Negative: an active lease with a registered, fresh holder cannot be
    # cleared by the same governance actor.
    healthy = client.call(
        "lease.acquire",
        {
            "task_id": TASK_ID,
            "role": "executor",
            "ttl_seconds": 30.0,
            "identity": executor,
        },
    )
    healthy_token = healthy["token"]
    try:
        try:
            client.call(
                "lease.recover",
                recover_params(
                    TASK_ID,
                    workspace_id,
                    workspace_instance_id,
                    planner,
                    f"e2e-lease-recover-healthy-{uuid.uuid4().hex}",
                ),
            )
        except DaemonRemoteError as error:
            assert error.code == "E_LEASE_RECOVERY_NOT_ELIGIBLE", error
        else:
            raise AssertionError("healthy active lease unexpectedly recoverable")
    finally:
        client.call(
            "lease.release",
            {
                "task_id": TASK_ID,
                "role": "executor",
                "token": healthy_token,
                "fencing_counter": healthy.get("fencing_counter"),
                "identity": executor,
            },
        )

    print(
        "LEASE_RECOVERY_E2E_PASS "
        f"task={TASK_ID} workspace={workspace_id}/{workspace_instance_id} "
        f"lease_id={recovered['lease_id']} fencing={fencing} "
        "positive=expired+assignment_stale replay=stable healthy=refused"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
