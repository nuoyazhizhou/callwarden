"""TP-08F 冻结清单验证器。

用法：
  python validate_frozen.py --self-check       # 结构 + 版本绑定 + 对账完备性
  python validate_frozen.py --verify-freeze    # 重算输入 manifest 散列与冻结值对拍

fail-closed：任一律不满足即非零退出。
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
FROZEN = HERE / "inventory_frozen.json"
CLI_TREE = REPO_ROOT / "docs" / "testing" / "manifests" / "cli" / "cli_tree.json"
MCP_TOOLS = REPO_ROOT / "docs" / "testing" / "manifests" / "mcp" / "mcp_tools.json"
TP01 = REPO_ROOT / "docs" / "testing" / "planning" / "inventory" / "candidate_entries.json"

OBLIGATIONS = {"C1", "C2", "C3", "C4", "C5", "C6"}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def check(cond: bool, msg: str, failures: list) -> None:
    if not cond:
        failures.append(msg)


def self_check() -> list:
    f: list = []
    if not FROZEN.is_file():
        return [f"frozen manifest missing: {FROZEN}"]
    raw = FROZEN.read_bytes()
    check(b"\r\n" not in raw, "manifest must be LF", f)
    check(raw.endswith(b"\n"), "manifest must end with newline", f)
    try:
        d = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        return [f"manifest not strict UTF-8: {exc}"]

    check(d.get("schema") == "frozen-inventory/v1", "schema must be frozen-inventory/v1", f)
    check(d.get("card") == "TP-08F", "card must be TP-08F", f)
    check(len(d.get("source_revision", "")) == 40, "source_revision must be a full sha", f)
    check(d.get("profile") == "P-COMMITTED-windows-amd64-py314", "profile must be the frozen one", f)

    entries = d.get("entries") or []
    check(len(entries) > 0, "entries must be non-empty", f)
    ids = [e["entry_id"] for e in entries]
    check(len(ids) == len(set(ids)), f"duplicate entry_id: {len(ids)-len(set(ids))}", f)

    for e in entries:
        check(set(e.get("obligations", {})) == OBLIGATIONS,
              f"{e['entry_id']}: obligations must cover C1-C6", f)
        check(e.get("profile") == d["profile"], f"{e['entry_id']}: profile mismatch", f)
        check(bool(e.get("provenance", {}).get("method")),
              f"{e['entry_id']}: provenance.method missing", f)
        check(e.get("status") == "frozen_inventory",
              f"{e['entry_id']}: status must be frozen_inventory", f)

    _cli = [e for e in entries if e["surface"] == "cli"]
    mcp = [e for e in entries if e["surface"] == "mcp"]
    check(len(mcp) == 243, f"mcp entries must be 243, got {len(mcp)}", f)
    check(d["counts"]["total"] == len(entries), "counts.total mismatch", f)

    # GAP-14 闭合证据
    gaps = d.get("gaps_closed") or []
    g14 = [g for g in gaps if g.get("gap") == "GAP-14"]
    check(len(g14) == 1, "GAP-14 must be explicitly closed", f)
    if g14:
        ev = g14[0]["evidence"]
        mcp_names = {e["tool_name"] for e in mcp}
        for n in ev["required_present"]:
            check(n in mcp_names, f"GAP-14 evidence: {n} not in wire set", f)
        for n in ev["stale_absent"]:
            check(n not in mcp_names, f"GAP-14 evidence: stale {n} still present", f)
        check(ev["wire_tool_count"] == ev["source_decorator_count"],
              "GAP-14: wire count must equal source @mcp.tool() count", f)

    # 对账完备性：静态多出必须为 0 或被显式重分类
    rec = d.get("reconciliation") or {}
    check(len(rec.get("cli", {}).get("static_only", [])) == 0,
          f"unexplained static-only CLI entries: {rec.get('cli', {}).get('static_only')}", f)
    check(len(rec.get("mcp", {}).get("static_only", [])) == 0,
          f"unexplained static-only MCP entries: {rec.get('mcp', {}).get('static_only')}", f)
    check(len(rec.get("mcp", {}).get("real_new", [])) == 0,
          "MCP wire set must equal source set (GAP-14 closure)", f)
    return f


def verify_freeze() -> list:
    f: list = []
    d = json.loads(FROZEN.read_text(encoding="utf-8"))
    vb = d.get("version_binding") or {}
    for key, path in (("cli_tree_sha256", CLI_TREE), ("mcp_tools_sha256", MCP_TOOLS),
                      ("tp01_candidates_sha256", TP01)):
        if not path.is_file():
            check(vb.get(key) is None, f"{key}: input missing but bound value present", f)
            continue
        check(vb.get(key) == _sha(path), f"{key}: hash drift vs disk ({path.name})", f)
    return f


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "--self-check"
    if mode == "--self-check":
        failures = self_check()
    elif mode == "--verify-freeze":
        failures = verify_freeze()
    else:
        print(f"unknown mode: {mode}")
        return 2
    if failures:
        print(f"[FAIL] {len(failures)} violation(s):")
        for x in failures[:20]:
            print("  -", x)
        return 1
    print(f"[PASS] {mode}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
