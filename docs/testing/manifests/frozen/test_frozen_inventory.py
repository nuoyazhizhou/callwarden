"""TP-08F 清单/义务合并冻结 —— 验收套件。

selector: docs/testing/manifests/frozen/test_frozen_inventory.py

正向：验证器 self-check/verify-freeze 通过；508 入口（CLI 265 + MCP 243）；
      GAP-14 以 wire 实测闭合；对账完备（静态多出=0、MCP 集合相等）。
负向：**未审漂移阻断**——重提取 wire 工具集与冻结集比对，任何差异即失败；
      源码装饰器数与冻结 MCP 数对拍；反证（篡改 manifest 必须被验证器拒绝）；
      LF+UTF-8。

运行：C:/Python314/python.exe -m pytest docs/testing/manifests/frozen/test_frozen_inventory.py \
        -p no:cacheprovider --noconftest -v --junitxml=<run根>/reports/junit.xml
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_ROOT = HERE.parents[3]
FROZEN = HERE / "inventory_frozen.json"
VALIDATOR = HERE / "validate_frozen.py"
OBLIGATIONS = {"C1", "C2", "C3", "C4", "C5", "C6"}


@pytest.fixture(scope="module")
def frozen() -> dict:
    if not FROZEN.is_file():
        pytest.skip(f"frozen manifest not generated: {FROZEN}")
    return json.loads(FROZEN.read_text(encoding="utf-8"))


# ---------------- 正向 ----------------

def test_validator_self_check_passes():
    r = subprocess.run([sys.executable, str(VALIDATOR), "--self-check"],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"self-check failed: {r.stdout}{r.stderr}"


def test_validator_verify_freeze_passes():
    r = subprocess.run([sys.executable, str(VALIDATOR), "--verify-freeze"],
                       capture_output=True, text=True)
    assert r.returncode == 0, f"verify-freeze failed: {r.stdout}{r.stderr}"


def test_counts_and_profiles(frozen):
    c = frozen["counts"]
    assert c["mcp_entries"] == 243
    assert c["cli_entries"] == 265
    assert c["total"] == 508
    assert frozen["profile"] == "P-COMMITTED-windows-amd64-py314"


def test_gap14_closed_with_evidence(frozen):
    gaps = {g["gap"]: g for g in frozen["gaps_closed"]}
    assert "GAP-14" in gaps, "GAP-14 must be explicitly closed"
    ev = gaps["GAP-14"]["evidence"]
    assert set(ev["required_present"]) == {"detect_call_cycles", "detect_dependency_cycle"}
    assert set(ev["stale_absent"]) == {"detect_cycle", "detect_cycles"}
    assert ev["wire_tool_count"] == ev["source_decorator_count"] == 243


def test_reconciliation_complete(frozen):
    rec = frozen["reconciliation"]
    assert rec["cli"]["static_only"] == [], "static-only CLI entries must be fully explained"
    assert rec["mcp"]["static_only"] == []
    assert rec["mcp"]["real_new"] == [], "MCP wire set must equal source set"
    assert rec["cli"]["real_new"], "real tree must record entries the static snapshot missed"
    assert set(rec["cli"]["reclassified_as_flag_variant"]) == {"map text", "map mermaid"}


def test_every_entry_has_full_obligation_binding(frozen):
    for e in frozen["entries"]:
        assert set(e["obligations"]) == OBLIGATIONS, f"{e['entry_id']} obligations incomplete"
        assert e["profile"] == frozen["profile"]
        assert e["status"] == "frozen_inventory"
        assert e["provenance"]["method"]


def test_no_duplicate_entry_ids(frozen):
    ids = [e["entry_id"] for e in frozen["entries"]]
    assert len(ids) == len(set(ids))


def test_entry_kinds_cover_expansion_basis(frozen):
    kinds = {}
    for e in frozen["entries"]:
        kinds[e["entry_kind"]] = kinds.get(e["entry_kind"], 0) + 1
    assert kinds.get("positional_choice_expansion", 0) > 0, "positional choices must be expanded"
    assert kinds.get("nested_subcommand_level3", 0) == 23, "daemon level-3 must be 23"
    assert kinds.get("mcp_wire_tool") == 243


# ---------------- 负向 ----------------

def test_unreviewed_wire_drift_blocks(frozen):
    """未审漂移阻断：重提取 wire 工具名集，与冻结集逐一比对。"""
    sys.path.insert(0, str(REPO_ROOT / "tests" / "acceptance" / "inventory" / "mcp"))
    import anyio
    import extract_mcp_tools as ext

    data = anyio.run(ext.extract)
    live = {t["name"] for t in data["tools"]}
    frozen_names = {e["tool_name"] for e in frozen["entries"] if e["surface"] == "mcp"}
    assert live == frozen_names, (
        f"unreviewed MCP drift: added={sorted(live - frozen_names)[:5]} "
        f"removed={sorted(frozen_names - live)[:5]}")


def test_source_decorator_count_matches_frozen(frozen):
    import re
    total = 0
    for p in (REPO_ROOT / "server" / "tools").glob("tools_*.py"):
        total += len(re.findall(r"@mcp\.tool\(", p.read_text(encoding="utf-8", errors="replace")))
    frozen_mcp = len([e for e in frozen["entries"] if e["surface"] == "mcp"])
    assert total == frozen_mcp == 243, f"source={total} frozen={frozen_mcp}"


def test_tampered_manifest_is_rejected(tmp_path):
    """反证：删入口/改义务/改散列的篡改必须被验证器 fail-closed 拒绝。"""
    import importlib.util
    spec = importlib.util.spec_from_file_location("vf", VALIDATOR)
    vf = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(vf)

    good = json.loads(FROZEN.read_text(encoding="utf-8"))

    # ① 删一个入口
    bad1 = json.loads(json.dumps(good))
    bad1["entries"] = bad1["entries"][:-1]
    bad1["counts"]["total"] = len(bad1["entries"])
    p1 = tmp_path / "tampered1.json"
    p1.write_text(json.dumps(bad1), encoding="utf-8")
    vf.FROZEN = p1
    assert vf.self_check() != [], "removing an entry must fail self-check"

    # ② 破坏义务覆盖
    bad2 = json.loads(json.dumps(good))
    bad2["entries"][0]["obligations"].pop("C3")
    p2 = tmp_path / "tampered2.json"
    p2.write_text(json.dumps(bad2), encoding="utf-8")
    vf.FROZEN = p2
    assert vf.self_check() != [], "incomplete obligations must fail self-check"

    # ③ 版本绑定散列漂移
    bad3 = json.loads(json.dumps(good))
    bad3["version_binding"]["mcp_tools_sha256"] = "0" * 64
    p3 = tmp_path / "tampered3.json"
    p3.write_text(json.dumps(bad3), encoding="utf-8")
    vf.FROZEN = p3
    assert vf.verify_freeze() != [], "hash drift must fail verify-freeze"


def test_manifest_is_lf_and_utf8(frozen):
    raw = FROZEN.read_bytes()
    assert b"\r\n" not in raw
    assert raw.endswith(b"\n")
    json.loads(raw.decode("utf-8"))
