#!/usr/bin/env python3
"""E2E: task.create accepts the RECONCILED live workspace tuple (T-1788392053931-05b8a0e4).

This is the append-only remediation of T-1788382908707-bbdd0cfc. That task closed with an
INCOMPLETE WRITE proof: its E2E only wrote the legacy carrier tuple ``(1, ws-1)`` and
explicitly documented that a create with the canonical stable instance
``4baea3ff12c2ea5c`` was *intentionally rejected* ("instance-consistency, anti-phantom"),
because ``bind_task_to_workspace`` re-read the latest capture (legacy ``ws-1``) and refused
the reconciled canonical instance.

This E2E proves the fix in T-1788392053931-05b8a0e4:

- ``resolve_create_authority`` resolves the requested registry numeric id (e.g. 1102) to the
  canonical task-DB authority (workspace 1) via the stable ``workspace_instance_id``.
- ``handle_task_create`` threads that resolved ``ReconciledAuthority.canonical_instance_id``
  into ``bind_task_to_workspace``, and ``bind_task_to_workspace`` no longer rejects a canonical
  instance merely because the *latest* capture row is a legacy ``ws-1``; it only fail-closes
  when the requested instance is not established in the workspace capture chain at all.

Runs against the LIVE daemon over HTTP as a fresh client process (pytest or
``python tests/e2e_workspace_authority_live_tuple_v3.py``). Read-only DB checks are permitted
for the "exactly one binding" assertion; no writes.

Acceptance:
  AUTH  status(<instance>)       -> registry + task_db both non-null, instances equal
  WRITE task.create(<live tuple>)-> exactly one binding/capture, instance == stable instance
  NEG   mismatch / stale / unknown / cross-project tuple -> fail-closed, zero partial rows
"""

import os
import sys
import json
import re
import subprocess

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(REPO_ROOT))

from callwarden.server.daemon_client import HttpDaemonRpcClient  # noqa: E402

CALLWARDEN_INSTANCE = "4baea3ff12c2ea5c"
CW = "C:/Python314/python.exe"
CW_CMD = [CW, "C:/git_work/callwarden/cw.py"]
TASK_DB = "C:/Users/wanpi/.callwarden/callwarden.db"


def call(method, params):
    return HttpDaemonRpcClient().call(method, params)


def cw(*args, **kw):
    env = dict(os.environ)
    env["PYTHONPATH"] = "C:/git_work"
    return subprocess.run(CW_CMD + list(args), capture_output=True, text=True, env=env, **kw)


def status_by_instance(instance):
    return call("workspace.status", {"workspace_instance_id": instance})


def discover_registry_numeric_id(instance):
    try:
        r = status_by_instance(instance)
        nid = r.get("registry_workspace_id")
        if nid is not None:
            return str(nid)
    except Exception:
        pass
    return os.environ.get("CW_REGISTRY_ID", "1102")


def parse_task_id(out):
    m = re.search(r"T-\d{13}-\w+", out)
    return m.group(0) if m else None


def count_bindings(task_id):
    import sqlite3
    con = sqlite3.connect("file:" + TASK_DB + "?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM task_workspace_bindings WHERE task_id = ?",
            (task_id,),
        ).fetchone()[0]
    finally:
        con.close()


def count_tasks_by_title(title):
    import sqlite3
    con = sqlite3.connect("file:" + TASK_DB + "?mode=ro", uri=True)
    try:
        return con.execute(
            "SELECT COUNT(*) FROM tasks WHERE title = ?", (title,),
        ).fetchone()[0]
    finally:
        con.close()


def binding_authority(task_id):
    """Read-only: return (workspace_id, instance) of the created task's binding."""
    import sqlite3
    con = sqlite3.connect("file:" + TASK_DB + "?mode=ro", uri=True)
    try:
        row = con.execute(
            "SELECT b.workspace_id, c.workspace_instance_id "
            "FROM task_workspace_bindings b "
            "JOIN workspace_authority_captures c ON c.workspace_capture_id = b.workspace_capture_id "
            "WHERE b.task_id = ?",
            (task_id,),
        ).fetchone()
        return row
    finally:
        con.close()


# ---------------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------------

def check_authority(failures):
    r = status_by_instance(CALLWARDEN_INSTANCE)
    if (r.get("registry_workspace_id") is None or r.get("task_db_workspace_id") is None
            or r.get("registry_instance_id") is None or r.get("task_db_instance_id") is None):
        failures.append(f"AUTH status({CALLWARDEN_INSTANCE}): incomplete authority: {r}")
        return None
    if r["registry_instance_id"] != r["task_db_instance_id"]:
        failures.append(f"AUTH instances mismatch: {r}")
        return None
    print(f"[PASS] AUTH status({CALLWARDEN_INSTANCE}) -> registry(id={r['registry_workspace_id']},"
          f"inst={r['registry_instance_id']}) task_db(id={r['task_db_workspace_id']},"
          f"inst={r['task_db_instance_id']})")
    return r


# ---------------------------------------------------------------------------
# WRITE (positive: the exact live tuple)
# ---------------------------------------------------------------------------

def check_live_tuple_create(failures, registry_id):
    title = "E2E live-tuple create probe (T-1788392053931-05b8a0e4)"
    res = cw("task", "create",
             "--title", title,
             "--desc", "probe: create must accept reconciled live tuple",
             "--steps", json.dumps([{"action": "annotate",
                                     "target_file": "rust_ext/src/daemon/task_collab.rs"}]),
             "--workspace-id", registry_id,
             "--workspace-instance-id", CALLWARDEN_INSTANCE)
    out = res.stdout + res.stderr
    task_id = parse_task_id(out)
    if not task_id:
        failures.append(f"WRITE task.create({registry_id},{CALLWARDEN_INSTANCE}) failed: {out[:400]}")
        return None
    nb = count_bindings(task_id)
    if nb != 1:
        failures.append(f"WRITE task.create produced {nb} bindings for {task_id} (expected 1)")
        return None
    ba = binding_authority(task_id)
    if ba is None:
        failures.append(f"WRITE no binding authority row for {task_id}")
        return None
    ws_id, inst = ba
    if inst != CALLWARDEN_INSTANCE:
        failures.append(f"WRITE binding instance={inst} != canonical {CALLWARDEN_INSTANCE}")
        return None
    if ws_id != 1:
        failures.append(f"WRITE binding workspace_id={ws_id} != task-DB canonical 1")
        return None
    print(f"[PASS] WRITE task.create({registry_id},{CALLWARDEN_INSTANCE}) -> task {task_id}, "
          f"1 binding, instance={inst}, task_db_workspace_id={ws_id}")
    return task_id


# ---------------------------------------------------------------------------
# NEGATIVE MATRIX (fail-closed, zero partial rows)
# ---------------------------------------------------------------------------

def check_negative_matrix(failures):
    cases = [
        # (label, title, args)
        ("mismatch (valid ws + un-established instance)",
         "v3 mismatch probe",
         ["--workspace-id", "1",
          "--workspace-instance-id", "v3-synthetic-instance-zzz-not-in-chain"]),
        ("stale (stale numeric id + unknown instance)",
         "v3 stale probe",
         ["--workspace-id", "144",
          "--workspace-instance-id", "v3-stale-unknown-instance-zzz"]),
        ("unknown (never-seen instance)",
         "v3 unknown probe",
         ["--workspace-instance-id", "v3-unknown-instance-xyz"]),
    ]
    for label, title, extra in cases:
        before = count_tasks_by_title(title)
        res = cw("task", "create",
                 "--title", title,
                 "--desc", "should fail-closed",
                 "--steps", json.dumps([{"action": "annotate", "target_file": "a.py"}]),
                 *extra)
        out = res.stdout + res.stderr
        if parse_task_id(out):
            failures.append(f"NEG {label} must FAIL-CLOSED but succeeded: {out[:300]}")
            continue
        after = count_tasks_by_title(title)
        if after > before:
            failures.append(f"NEG {label} left {after-before} partial task row(s)")
            continue
        print(f"[PASS] NEG {label} -> fail-closed (no partial rows)")


def check_cross_project(failures):
    # 从 registry 找一个「非 CallWarden 项目」的真实 workspace instance（有 registry
    # 记录但 CallWarden task-DB 无 capture），证明跨项目 instance 不可绑定 CallWarden 任务。
    # 不依赖 workspace.register（register 写 registry.db 自增 id，与 workspace.status/
    # list 读的 task-DB 投影语义分裂，register 返回 id 在 status 侧查不到）。
    lst = call("workspace.list", {})
    if not isinstance(lst, list):
        failures.append("cross-project could not list workspaces")
        return
    cross_inst = None
    for w in lst:
        inst = w.get("workspace_instance_id")
        root = (w.get("client_view_root") or "").lower()
        if not inst or inst == CALLWARDEN_INSTANCE or "callwarden" in root:
            continue
        st = call("workspace.status", {"workspace_instance_id": inst})
        if isinstance(st, dict) and st.get("task_db_workspace_id") is None:
            cross_inst = inst
            break
    if not cross_inst:
        failures.append("cross-project could not find a registry-only foreign instance")
        return
    title = "v3 cross-project probe"
    before = count_tasks_by_title(title)
    res = cw("task", "create",
             "--title", title,
             "--desc", "should fail: foreign instance has no CallWarden task-DB authority",
             "--steps", json.dumps([{"action": "annotate", "target_file": "a.py"}]),
             "--workspace-instance-id", cross_inst)
    out = res.stdout + res.stderr
    if parse_task_id(out):
        failures.append(f"cross-project foreign instance ({cross_inst}) create must FAIL but succeeded: {out[:300]}")
        return
    after = count_tasks_by_title(title)
    if after > before:
        failures.append(f"cross-project left {after-before} partial task row(s)")
        return
    print(f"[PASS] NEG cross-project foreign instance ({cross_inst}) cannot be task-bound")


# ---------------------------------------------------------------------------

def run_all():
    failures = []
    authority = check_authority(failures)
    if authority is None:
        return 1
    registry_id = str(authority["registry_workspace_id"])
    check_live_tuple_create(failures, registry_id)
    check_negative_matrix(failures)
    check_cross_project(failures)
    if failures:
        print("\n=== E2E FAILURES ===")
        for f in failures:
            print(" -", f)
        return 1
    print("\nALL LIVE-TUPLE WORKSPACE AUTHORITY V3 E2E CHECKS PASSED")
    return 0


def test_authority_consistent():
    failures = []
    check_authority(failures)
    assert not failures, failures


def test_live_tuple_create_accepted():
    failures = []
    authority = check_authority(failures)
    if authority:
        check_live_tuple_create(failures, str(authority["registry_workspace_id"]))
    assert not failures, failures


def test_negative_matrix_fail_closed():
    failures = []
    check_negative_matrix(failures)
    assert not failures, failures


def test_cross_project_no_bind():
    failures = []
    check_cross_project(failures)
    assert not failures, failures


if __name__ == "__main__":
    sys.exit(run_all())
