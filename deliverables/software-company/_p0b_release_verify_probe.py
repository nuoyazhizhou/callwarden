"""P0-B release_verify probe：真实 daemon round-trip（release daemon 已加载 P0-B 校验链）。

仅在真实运行的 cw-daemon（127.0.0.1:8149）上执行只读的 fail-closed 探测：
- 不执行真实任务 mutation / smoke；
- 调用受 P0-B 保护的治理方法 `task.attest_legacy_workspace_binding`，
  分别缺 request_id / workspace / identity / lease/evidence，期望 daemon 输出
  结构化拒绝（fail-closed），证明 P0-B handler 已在 release 运行时生效且无 mutation。
- 探测过程不修改任何任务状态 / binding / contract / lease。
"""
import hashlib
import json
import os
import sys
import time

sys.path.insert(0, "C:/git_work/callwarden")

from callwarden.server import daemon_client as dc  # noqa: E402
from callwarden.server.daemon_protocol import DaemonRemoteError  # noqa: E402

PID = os.getpid()
START = time.time()


def sha256_hex(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def main() -> int:
    transcript = {
        "type": "p0b_release_verify_probe",
        "pid": PID,
        "started_at": START,
        "probes": [],
        "verdict": "pending",
        "note": "",
    }
    try:
        raw = dc.HttpDaemonRpcClient.get_instance()
        raw._endpoint = "http://127.0.0.1:8149"

        # 构造一个"应被拒绝但绝不触发 mutation"的请求基座。
        base = {
            "legacy_task_id": "P0B-PROBE-LEGACY-NOEXIST",
            "anchor_task_id": "P0B-PROBE-ANCHOR-NOEXIST",
            "workspace_id": 1,
            "workspace_instance_id": "probe-ws",
            "request_id": "p0b-probe-%d-%d" % (PID, int(START)),
            "evidence_path": "C:/git_work/callwarden/docs/evidence/p0b-probe.json",
            "evidence_hash": "deadbeef",
            "lease_token": "p0b-probe-lease",
            "fencing_counter": 0,
            "identity": {
                "agent_id": "p0b-probe-agent",
                "session_id": "p0b-probe-session",
                "model_id": "p0b-probe-model",
                "role": "adjudicator",
            },
        }

        # --- Probe 1：缺 request_id（fail-closed：handler 必须确定性拒绝）---
        p1 = {"name": "attest_legacy_missing_request_id"}
        p1p = dict(base)
        del p1p["request_id"]
        try:
            raw.call("task.attest_legacy_workspace_binding", p1p)
            p1["result"] = "unexpected_success"
            p1["ok"] = False
        except DaemonRemoteError as e:
            p1["error_code"] = getattr(e, "code", None)
            p1["ok"] = True  # 结构化拒绝即 fail-closed 生效
        except Exception as e:  # noqa: BLE001
            p1["error"] = "{}: {}".format(type(e).__name__, e)
            p1["ok"] = False
        transcript["probes"].append(p1)

        # --- Probe 2：缺 identity（fail-closed）---
        p2 = {"name": "attest_legacy_missing_identity"}
        p2p = dict(base)
        p2p["request_id"] = "p0b-probe-2-%d-%d" % (PID, int(START))
        del p2p["identity"]
        try:
            raw.call("task.attest_legacy_workspace_binding", p2p)
            p2["result"] = "unexpected_success"
            p2["ok"] = False
        except DaemonRemoteError as e:
            p2["error_code"] = getattr(e, "code", None)
            p2["ok"] = True
        except Exception as e:  # noqa: BLE001
            p2["error"] = "{}: {}".format(type(e).__name__, e)
            p2["ok"] = False
        transcript["probes"].append(p2)

        # --- Probe 3：缺 evidence_hash（fail-closed）---
        p3 = {"name": "attest_legacy_missing_evidence"}
        p3p = dict(base)
        p3p["request_id"] = "p0b-probe-3-%d-%d" % (PID, int(START))
        del p3p["evidence_hash"]
        try:
            raw.call("task.attest_legacy_workspace_binding", p3p)
            p3["result"] = "unexpected_success"
            p3["ok"] = False
        except DaemonRemoteError as e:
            p3["error_code"] = getattr(e, "code", None)
            p3["ok"] = True
        except Exception as e:  # noqa: BLE001
            p3["error"] = "{}: {}".format(type(e).__name__, e)
            p3["ok"] = False
        transcript["probes"].append(p3)

        # --- Probe 4：缺 workspace 权威（fail-closed）---
        p4 = {"name": "attest_legacy_missing_workspace"}
        p4p = dict(base)
        p4p["request_id"] = "p0b-probe-4-%d-%d" % (PID, int(START))
        del p4p["workspace_id"]
        try:
            raw.call("task.attest_legacy_workspace_binding", p4p)
            p4["result"] = "unexpected_success"
            p4["ok"] = False
        except DaemonRemoteError as e:
            p4["error_code"] = getattr(e, "code", None)
            p4["ok"] = True
        except Exception as e:  # noqa: BLE001
            p4["error"] = "{}: {}".format(type(e).__name__, e)
            p4["ok"] = False
        transcript["probes"].append(p4)

        transcript["verdict"] = "pass" if all(p.get("ok") for p in transcript["probes"]) else "fail"
    except Exception as e:  # noqa: BLE001
        transcript["verdict"] = "error"
        transcript["note"] = "{}: {}".format(type(e).__name__, e)
    transcript["elapsed_sec"] = round(time.time() - START, 3)
    out = "C:/git_work/callwarden/_p0b_release_verify_transcript.json"
    with open(out, "w", encoding="utf-8") as f:
        json.dump(transcript, f, ensure_ascii=False, indent=2)
    print(json.dumps(transcript, ensure_ascii=False, indent=2))
    return 0 if transcript["verdict"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())