"""检查 G0 任务声明的三份规格真相源是否完整可读。"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from callwarden.experiments.truth_source_sync import (
    TruthSourceSyncError,
    validate_truth_source_change_set,
    validate_truth_sources,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default=".")
    parser.add_argument("--anchor", action="append", default=[])
    parser.add_argument("--changed-path", action="append", default=[])
    parser.add_argument("--required-path", action="append", default=[])
    parser.add_argument("--allowed-path", action="append", default=[])
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        result = validate_truth_sources(Path(args.root), args.anchor)
        if args.changed_path or args.required_path or args.allowed_path:
            result["change_set"] = validate_truth_source_change_set(
                args.changed_path, args.required_path, args.allowed_path)
    except TruthSourceSyncError as exc:
        payload = {"ok": False, "code": exc.code, "detail": exc.detail}
        print(json.dumps(payload, ensure_ascii=False) if args.json else f"FAIL {exc}")
        return 2
    payload = {"ok": True, **result}
    print(json.dumps(payload, ensure_ascii=False) if args.json else "OK: three truth sources are present")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
