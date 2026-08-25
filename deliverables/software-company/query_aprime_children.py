"""只读查询 A′ 迁移恢复父任务直属子任务。"""
from __future__ import annotations
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGE_PARENT = ROOT.parent
if str(PACKAGE_PARENT) not in sys.path:
    sys.path.insert(0, str(PACKAGE_PARENT))
from callwarden.server.daemon_client import get_daemon_client

PARENT_ID = "T-1787293451688-c14b1e44"


def main() -> None:
    result = get_daemon_client().call(
        "task.status_tree",
        {
            "task_id": PARENT_ID,
            "workspace_id": 1,
            "workspace_instance_id": "ws-1",
        },
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
