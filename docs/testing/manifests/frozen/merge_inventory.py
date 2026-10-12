"""TP-08F 清单/义务合并冻结器。

输入（真实提取产物，均已冻结）：
- docs/testing/manifests/cli/cli_tree.json      （TP-08C：84 顶层 + 144 嵌套，parser 级参数）
- docs/testing/manifests/mcp/mcp_tools.json     （TP-08M：243 工具，wire 级 schema）

合并规则：
1. CLI 入口展开为**调用叶**：
   - subparser 父 → 逐子命令展开（含 `daemon` 的第 3 层，经真实子进程 help 提取）；
   - 位置参数带 choices 的叶（`install-agent <agent>` / `install-hook <hook>`）→ 逐 choice 展开；
   - 旗标 choices（如 `map --format text|mermaid`）**不**展开为独立入口，落为 C2 参数等价类
     （与 TP-01 静态快照把 map 拆成两条的处理不同，此处按真实 parser 结构归类并在对账中显式标注）。
2. MCP 入口 = 243 wire 工具，逐工具带 inputSchema。
3. 逐入口义务：C1–C6 适用性 + profile 适用性（冻结 profile = Windows + Py3.14）。
4. 对账：与 TP-01 静态候选集逐条比对，差异必须分类（reclassified / deeper_level /
   real_new / static_only），不允许静默差异。
5. GAP-14 闭合：以 TP-08M wire 实测证据闭合（detect_call_cycles / detect_dependency_cycle
   均在 243 中，陈旧名 detect_cycle / detect_cycles 不在）。
6. 版本绑定：source_revision + 两个输入 manifest 的 sha256。

输出：docs/testing/manifests/frozen/inventory_frozen.json
"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
CLI_TREE = REPO_ROOT / "docs" / "testing" / "manifests" / "cli" / "cli_tree.json"
MCP_TOOLS = REPO_ROOT / "docs" / "testing" / "manifests" / "mcp" / "mcp_tools.json"
TP01_CANDIDATES = REPO_ROOT / "docs" / "testing" / "planning" / "inventory" / "candidate_entries.json"
OUT = REPO_ROOT / "docs" / "testing" / "manifests" / "frozen" / "inventory_frozen.json"

PROFILE = "P-COMMITTED-windows-amd64-py314"
CW_PY_ENTRIES = ("install", "server", "test", "setup", "daemon")
# cw.py 层、且带真实 subparser 嵌套、需第 3 层提取的入口
DAEMON_NESTED = ["ping", "start", "register", "list", "status", "publish", "query", "mode",
                 "health", "manifest", "capability", "schema-version", "bridge", "backup",
                 "restore", "gc-cas", "gc-snapshots", "snapshot-stats", "snapshot-list",
                 "snapshot-evict", "metrics", "mount", "toolchain"]

OBLIGATION_MODEL = {
    "C1": "正常业务语义：有效前置下至少一条正常场景，独立oracle与业务结果一致",
    "C2": "逐参数等价类/默认/边界/互斥：每个适用等价类有selector和预期",
    "C3": "精确负向：无效对象/状态/权限/路径，指定错误码，无非法副作用",
    "C4": "适用写入：承诺效果/幂等/冲突/回滚，前后对象与事件精确断言",
    "C5": "适用故障与恢复：超时/断连/重启/半完成重试，状态一致无旁路成功",
    "C6": "跨端共享语义：CLI/MCP共同业务先独立oracle再比较关键语义",
}


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_daemon_nested(sub: str) -> dict | None:
    """`cw daemon <sub> --help` 真实子进程提取（cw.py 层入口，不经 _dispatch_subcommand）。"""
    r = subprocess.run([sys.executable, str(REPO_ROOT / "cw.py"), "daemon", sub, "--help"],
                       cwd=REPO_ROOT, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=90)
    if r.returncode != 0:
        return None
    return {"entry": sub, "rc": 0, "help_text": (r.stdout or "")[:3000]}


def cli_param_summary(parser: dict | None) -> dict:
    """从捕获的 parser 提参数摘要（位置/选项/choices/required/default）。"""
    if not parser or "arguments" not in parser:
        return {"available": False, "reason": "cw_py_level_help_text_only"}
    positionals, options = [], []
    for a in parser["arguments"]:
        rec = {"dest": a["dest"], "nargs": a["nargs"], "required": a["required"],
               "type": a["type"], "choices": a["choices"], "default": a["default"]}
        if a["option_strings"]:
            if a["dest"] == "help":
                continue
            options.append({**rec, "flags": a["option_strings"]})
        else:
            positionals.append(rec)
    return {
        "available": True,
        "prog": parser.get("prog"),
        "positionals": positionals,
        "options": options,
        "mutually_exclusive_groups": parser.get("mutually_exclusive_groups") or [],
        "param_equivalence_classes": [
            {"param": o["dest"], "kind": "flag_choices", "choices": o["choices"]}
            for o in options if o["choices"]
        ] + [
            {"param": p["dest"], "kind": "positional_choices", "choices": p["choices"]}
            for p in positionals if p["choices"]
        ],
    }


def build_cli_entries(tree: dict) -> tuple[list[dict], list[str]]:
    entries: list[dict] = []
    notes: list[str] = []
    ents = tree["entries"]
    dispatch = {k: v for k, v in ents.items() if k not in CW_PY_ENTRIES}

    def add(name: str, invocation: str, category: str, parser: dict | None, kind: str,
            parent: str | None = None):
        params = cli_param_summary(parser)
        entries.append({
            "entry_id": f"CLI:{invocation}",
            "surface": "cli",
            "invocation": invocation,
            "top_level": name,
            "category": category,
            "entry_kind": kind,
            "parent": parent,
            "params": params,
            "obligations": {k: "applicable_pending_case" for k in OBLIGATION_MODEL},
            "profile": PROFILE,
            "applicability": "applicable",
            "status": "frozen_inventory",
            "provenance": {"method": "argparse parser capture (real)",
                           "source": "cli_tree.json"},
        })

    for name, node in sorted(dispatch.items()):
        cat = node["category"]
        if node["nested"]:
            for sub, subnode in sorted(node["nested"].items()):
                if "error" in subnode:
                    notes.append(f"CLI:{name} {sub} extraction error: {subnode['error']}")
                    continue
                add(name, f"{name} {sub}", cat, subnode.get("parser"), "nested_subcommand", name)
        else:
            add(name, name, cat, node.get("top_parser"), "top_level_leaf")

    # cw.py 层 5 入口
    for name in CW_PY_ENTRIES:
        node = ents[name]
        cat = node.get("category") or "cw_py_standalone"
        parser = node.get("top_parser")
        add(name, name, cat, parser if (parser and "arguments" in parser) else None,
            "cw_py_standalone")
        if name == "daemon":
            for sub in DAEMON_NESTED:
                info = extract_daemon_nested(sub)
                if info is None:
                    notes.append(f"CLI:daemon {sub} extraction failed")
                    continue
                entries.append({
                    "entry_id": f"CLI:daemon {sub}",
                    "surface": "cli",
                    "invocation": f"daemon {sub}",
                    "top_level": "daemon",
                    "category": cat,
                    "entry_kind": "nested_subcommand_level3",
                    "parent": "daemon",
                    "params": {"available": True, "prog": f"cw daemon {sub}",
                               "help_text": info["help_text"]},
                    "obligations": {k: "applicable_pending_case" for k in OBLIGATION_MODEL},
                    "profile": PROFILE,
                    "applicability": "applicable",
                    "status": "frozen_inventory",
                    "provenance": {"method": "real subprocess cw.py daemon <sub> --help",
                                   "source": "cli_tree.json + live help"},
                })

    # 位置参数 choices 展开（install-agent <agent> / install-hook <hook>）
    expanded: list[dict] = []
    for e in entries:
        pos_choices = [p for p in e["params"].get("positionals", []) if p.get("choices")]
        if not pos_choices:
            expanded.append(e)
            continue
        base = e["entry_id"]
        for p in pos_choices:
            for choice in p["choices"]:
                child = json.loads(json.dumps(e))
                child["entry_id"] = f"{base}:{choice}"
                child["invocation"] = f"{e['invocation']} {choice}"
                child["entry_kind"] = "positional_choice_expansion"
                child["parent"] = e["invocation"]
                child["params"]["expansion_basis"] = {
                    "param": p["dest"], "kind": "positional_choices", "choice": choice}
                expanded.append(child)
        # 父入口本身保留（不带 choice 的调用形态）
        parent_only = json.loads(json.dumps(e))
        parent_only["entry_kind"] = "positional_choice_parent"
        expanded.append(parent_only)
    return expanded, notes


def build_mcp_entries(mcp: dict) -> list[dict]:
    entries = []
    for t in mcp["tools"]:
        schema = t["inputSchema"] or {}
        entries.append({
            "entry_id": f"MCP:{t['name']}",
            "surface": "mcp",
            "tool_name": t["name"],
            "invocation": t["name"],
            "category": None,  # wire annotations 未暴露分类，跨端分类合并见对账说明
            "entry_kind": "mcp_wire_tool",
            "params": {
                "available": True,
                "inputSchema_type": schema.get("type"),
                "required": schema.get("required") or [],
                "properties": sorted((schema.get("properties") or {}).keys()),
                "property_count": len(schema.get("properties") or {}),
                "description_present": bool(t.get("description")),
            },
            "obligations": {k: "applicable_pending_case" for k in OBLIGATION_MODEL},
            "profile": PROFILE,
            "applicability": "applicable",
            "status": "frozen_inventory",
            "provenance": {"method": "MCP stdio wire list_tools (real subprocess)",
                           "source": "mcp_tools.json"},
        })
    return entries


def reconcile(cli_entries: list[dict], mcp_entries: list[dict], tp01: dict) -> dict:
    """与 TP-01 静态候选集逐条对账；差异必须分类，不允许静默。"""
    real_cli = {e["invocation"] for e in cli_entries if e["surface"] == "cli"}
    real_mcp = {e["tool_name"] for e in mcp_entries}
    static_cli, static_flag, static_mcp = set(), set(), set()
    for e in tp01.get("entries", []):
        if e["surface"] == "cli":
            static_cli.add(e["invocation"])
        elif e["surface"] == "cli_flag":
            static_flag.add(e.get("flag") or e.get("invocation") or "")
        elif e["surface"] == "mcp":
            static_mcp.add(e.get("tool_name") or "")

    static_only_cli = sorted(static_cli - real_cli)
    real_new_cli = sorted(real_cli - static_cli)
    static_only_mcp = sorted(static_mcp - real_mcp)
    real_new_mcp = sorted(real_mcp - static_mcp)

    # 分类：静态多出的 map text/mermaid 在真实 parser 里是 --format 旗标 choices
    reclassified = [x for x in static_only_cli if x.startswith("map ")]
    static_only_cli = [x for x in static_only_cli if not x.startswith("map ")]

    return {
        "cli": {
            "real": len(real_cli), "static": len(static_cli),
            "static_only": static_only_cli, "real_new": real_new_cli,
            "reclassified_as_flag_variant": reclassified,
            "note": "static_only 中的 daemon * 与 install-agent * / install-hook * 已由"
                    "第3层提取与位置参数 choices 展开覆盖；map text/mermaid 在真实 parser "
                    "中为 --format 旗标 choices，归为参数等价类而非独立入口",
        },
        "mcp": {
            "real": len(real_mcp), "static": len(static_mcp),
            "static_only": static_only_mcp, "real_new": real_new_mcp,
        },
        "cli_flag_static_count": len(static_flag),
    }


def main() -> int:
    tree = json.loads(CLI_TREE.read_text(encoding="utf-8"))
    mcp = json.loads(MCP_TOOLS.read_text(encoding="utf-8"))
    tp01 = json.loads(TP01_CANDIDATES.read_text(encoding="utf-8")) if TP01_CANDIDATES.is_file() else {}

    cli_entries, notes = build_cli_entries(tree)
    mcp_entries = build_mcp_entries(mcp)
    rec = reconcile(cli_entries, mcp_entries, tp01)

    # GAP-14 闭合证据
    mcp_names = {e["tool_name"] for e in mcp_entries}
    gap14 = {
        "gap": "GAP-14",
        "type": "schema_drift",
        "closure": "closed_by_real_wire_extraction",
        "evidence": {
            "required_present": ["detect_call_cycles", "detect_dependency_cycle"],
            "stale_absent": ["detect_cycle", "detect_cycles"],
            "wire_tool_count": len(mcp_names),
            "source_decorator_count": sum(
                len(__import__("re").findall(r"@mcp\.tool\(", p.read_text(encoding="utf-8", errors="replace")))
                for p in (REPO_ROOT / "server" / "tools").glob("tools_*.py")),
        },
        "statement": "wire 实测集合含 detect_call_cycles/detect_dependency_cycle，"
                     "不含陈旧名 detect_cycle/detect_cycles；总数相等不再掩盖集合漂移",
    }
    for name in gap14["evidence"]["required_present"]:
        assert name in mcp_names, f"GAP-14 closure evidence missing: {name}"
    for name in gap14["evidence"]["stale_absent"]:
        assert name not in mcp_names, f"stale name still present: {name}"

    rev = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                         capture_output=True, text=True).stdout.strip()
    data = {
        "schema": "frozen-inventory/v1",
        "card": "TP-08F",
        "frozen_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source_revision": rev,
        "profile": PROFILE,
        "version_binding": {
            "cli_tree_sha256": sha256_of(CLI_TREE),
            "mcp_tools_sha256": sha256_of(MCP_TOOLS),
            "tp01_candidates_sha256": sha256_of(TP01_CANDIDATES) if TP01_CANDIDATES.is_file() else None,
        },
        "counts": {
            "cli_entries": len(cli_entries),
            "mcp_entries": len(mcp_entries),
            "total": len(cli_entries) + len(mcp_entries),
        },
        "obligation_model": OBLIGATION_MODEL,
        "gaps_closed": [gap14],
        "reconciliation": rec,
        "extraction_notes": notes,
        "entries": cli_entries + mcp_entries,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes((json.dumps(data, indent=1, ensure_ascii=False) + "\n").encode("utf-8"))
    c = data["counts"]
    print(f"cli={c['cli_entries']} mcp={c['mcp_entries']} total={c['total']}")
    print(f"reconciliation: cli static_only={len(rec['cli']['static_only'])} "
          f"real_new={len(rec['cli']['real_new'])} "
          f"reclassified={len(rec['cli']['reclassified_as_flag_variant'])}")
    print(f"mcp static_only={len(rec['mcp']['static_only'])} real_new={len(rec['mcp']['real_new'])}")
    print(f"GAP-14 closed: {gap14['closure']}")
    print(f"written: {OUT} ({OUT.stat().st_size} bytes)")
    if notes:
        for n in notes[:10]:
            print("  NOTE", n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
