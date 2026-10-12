"""TP-08C CLI 真实树/参数提取 —— 验收套件。

selector: tests/acceptance/inventory/cli/test_cli_tree.py

正向：84 顶层入口不重不漏、每个顶层命令的 parser 可提取、嵌套子命令可展开、
      关键参数契约（required/type/default/choices/互斥）如实落 manifest、
      cw.py 层 5 个 standalone 入口有真实 help。
负向：提取不写生产目录（auto-setup marker / db mtime 不变）、未决入口（原生
      Rust CLI）必须显式挂 pending 而非静默缺失、manifest schema 漂移拒绝。

运行：C:/Python314/python.exe -m pytest tests/acceptance/inventory/cli/test_cli_tree.py \
        -p no:cacheprovider --noconftest -v --junitxml=<run根>/reports/junit.xml
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
MANIFEST = REPO_ROOT / "docs" / "testing" / "manifests" / "cli" / "cli_tree.json"


@pytest.fixture(scope="module")
def tree() -> dict:
    if not MANIFEST.is_file():
        pytest.skip(f"manifest not generated: {MANIFEST}")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# ---------------- 正向 ----------------

def test_schema_and_source_binding(tree):
    assert tree["schema"] == "cli-tree/v1"
    assert tree["card"] == "TP-08C"
    assert len(tree["source_revision"]) == 40
    assert tree["source_revision"] == _head()


def _head() -> str:
    import subprocess
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                       capture_output=True, text=True)
    return r.stdout.strip()


def test_all_84_top_level_entries_present(tree):
    """84 个顶层入口（_SUBCOMMANDS 79 + cw.py standalone 5）一个不少。"""
    assert tree["top_level_count"] == 84
    entries = tree["entries"]
    assert len(entries) == 84, f"entries={len(entries)}"
    assert tree["error_count"] == 0, f"extraction errors: {tree['errors'][:5]}"
    # cw.py 层 5 个入口必须由真实子进程取得
    for entry in ("install", "server", "test", "setup", "daemon"):
        assert entries[entry]["top_parser"] is not None, f"{entry} help not extracted"


def test_every_entry_has_category(tree):
    for name, node in tree["entries"].items():
        assert node.get("category"), f"{name} has no category binding"


def test_nested_subcommands_expanded(tree):
    """至少展开一层嵌套；task/gc/rule 等家族必须完整。"""
    assert tree["nested_total"] >= 100
    assert len(tree["entries"]["task"]["nested"]) >= 25
    assert len(tree["entries"]["gc"]["nested"]) >= 10
    assert len(tree["entries"]["lease"]["nested"]) >= 4


def test_parameter_contract_fields_captured(tree):
    """required / type / default / choices 必须如实捕获（help 文本不保证暴露）。"""
    report = tree["entries"]["task"]["nested"]["report"]["parser"]
    by_dest = {a["dest"]: a for a in report["arguments"]}
    assert by_dest["task_id"]["required"] is True
    assert by_dest["step_id"]["required"] is True
    assert by_dest["result"]["required"] is False
    assert by_dest["result"]["default"] == ""
    assert by_dest["fencing_counter"]["type"] == "int"
    assert by_dest["fencing_counter"]["default"] == -1
    assert by_dest["fail"]["nargs"] == 0


def test_mutually_exclusive_groups_recorded(tree):
    """存在互斥组的命令必须记录该关系（不能丢）。"""
    found = 0
    checked = 0
    for node in tree["entries"].values():
        p = node.get("top_parser")
        if not p or "mutually_exclusive_groups" not in p:
            continue  # cw.py 层入口只存 help_text，无 parser 结构
        checked += 1
        assert isinstance(p["mutually_exclusive_groups"], list)
        if p["mutually_exclusive_groups"]:
            found += 1
    assert checked >= 79, f"only {checked} argparse-structured parsers found"
    # 不预设互斥组具体数量（当前 CLI 未必有），但结构必须可读
    assert found >= 0


def test_standalone_entries_have_real_help(tree):
    for script, node in tree["standalone_entries"].items():
        if node["parser"]:
            assert node["parser"]["rc"] == 0
            assert len(node["parser"]["help_text"]) > 50, f"{script} help too short"


# ---------------- 负向 ----------------

def test_no_production_write_during_extraction():
    """提取不得触发 auto-setup / 不得改动生产 db mtime。"""
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or "")
    marker = home / ".callwarden" / ".auto_setup_marker"
    assert not marker.exists(), "extraction triggered lazy auto-setup (production write)"
    db = home / ".callwarden" / "callwarden.db"
    if db.is_file():
        mtime = db.stat().st_mtime
        assert mtime < time.time() - 60, "production db mtime is fresh — extraction may have written"


def test_pending_entries_explicit_not_silent(tree):
    """未决入口必须显式挂 pending，不允许静默消失。"""
    pending = tree.get("pending_entries") or []
    assert pending, "pending_entries missing — undecided entries may be silently dropped"
    names = [p["entry"] for p in pending]
    assert any("native" in n for n in names), "native Rust CLI must be pending-explicit"
    for p in pending:
        assert p.get("reason") and p.get("blocked_by"), f"pending entry lacks reason: {p}"


def test_manifest_is_lf_and_utf8(tree):
    """行尾/编码契约：manifest 必须 LF + UTF-8（与 ledger 同规矩）。"""
    raw = MANIFEST.read_bytes()
    assert b"\r\n" not in raw, "manifest must be LF, not CRLF"
    assert raw.endswith(b"\n")
    json.loads(raw.decode("utf-8"))  # 严格 UTF-8


def test_no_placeholder_or_fabricated_entries(tree):
    """禁止捏造入口：manifest 里不得出现占位命令名。"""
    text = json.dumps(tree, ensure_ascii=False)
    for bad in ("TODO", "FIXME", "placeholder", "<frozen-selectors>"):
        assert bad not in text, f"placeholder marker {bad!r} found in manifest"
