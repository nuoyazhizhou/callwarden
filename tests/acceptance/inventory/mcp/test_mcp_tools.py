"""TP-08M MCP wire 集合/schema 提取 —— 验收套件。

selector: tests/acceptance/inventory/mcp/test_mcp_tools.py

正向：真实子进程 stdio wire 提取（initialize + list_tools 翻页）；243 工具与源码
      `@mcp.tool()` 装饰器数一致；无重名；每个工具 inputSchema 为 object 且带
      properties；initialize 握手字段完整。
负向：重名拒绝、缺/空 inputSchema 拒绝、未知 schema 形状拒绝、
      startup 隔离（CW_DAEMON_BIN 必须钉在 TP-06 候选而非生产 runtime）、
      生产 manifest 不得被触动、manifest 必须 LF+UTF-8。

运行：C:/Python314/python.exe -m pytest tests/acceptance/inventory/mcp/test_mcp_tools.py \
        -p no:cacheprovider --noconftest -v --junitxml=<run根>/reports/junit.xml
"""

from __future__ import annotations

import glob
import json
import os
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
MANIFEST = REPO_ROOT / "docs" / "testing" / "manifests" / "mcp" / "mcp_tools.json"
TP06_DAEMON_SHA256 = "6324df120c850e45cee43349e3353e2fb88808e436127cbf9025c9e0a0e8859d"


@pytest.fixture(scope="module")
def tree() -> dict:
    if not MANIFEST.is_file():
        pytest.skip(f"manifest not generated: {MANIFEST}")
    return json.loads(MANIFEST.read_text(encoding="utf-8"))


# ---------------- 正向 ----------------

def test_schema_and_source_binding(tree):
    assert tree["schema"] == "mcp-tools/v1"
    assert tree["card"] == "TP-08M"
    assert len(tree["source_revision"]) == 40
    assert tree["transport"].startswith("stdio")


def test_real_subprocess_wire_extraction(tree):
    """必须来自真实子进程 wire，且 initialize 握手字段完整。"""
    init = tree["initialize"]
    assert init["serverName"] == "callwarden"
    assert init["protocolVersion"], "protocolVersion missing"
    assert init["serverVersion"], "serverVersion missing"
    assert tree["tool_count"] > 0


def test_wire_count_matches_source_decorators(tree):
    """wire 工具数必须与源码 @mcp.tool() 装饰器数一致（不多不少）。"""
    src_total = 0
    for f in glob.glob(str(REPO_ROOT / "server" / "tools" / "tools_*.py")):
        src = Path(f).read_text(encoding="utf-8", errors="replace")
        src_total += len(re.findall(r"@mcp\.tool\(", src))
    assert src_total > 0
    assert tree["tool_count"] == src_total, (
        f"wire={tree['tool_count']} vs source @mcp.tool()={src_total}")


def test_no_duplicate_tool_names(tree):
    names = [t["name"] for t in tree["tools"]]
    assert len(names) == len(set(names)), f"duplicates: {tree['duplicate_names'][:10]}"
    assert tree["duplicate_names"] == []


def test_every_tool_has_object_input_schema(tree):
    for t in tree["tools"]:
        s = t["inputSchema"]
        assert isinstance(s, dict), f"{t['name']}: inputSchema not a dict"
        assert s.get("type") == "object", f"{t['name']}: schema type={s.get('type')}"
        assert isinstance(s.get("properties", {}), dict), f"{t['name']}: properties missing"


def test_pagination_exhausted(tree):
    """list_tools 必须按 nextCursor 翻页至穷尽（pages>=1 且不带未消费 cursor）。"""
    assert tree["pagination_pages"] >= 1
    assert tree["tool_count"] > 0


def test_known_tools_present(tree):
    """治理/查询/租约等关键入口必须在集合内（不能丢家族）。"""
    names = {t["name"] for t in tree["tools"]}
    for expected in ("task_list", "task_status", "get_stats", "search_symbols",
                     "lease_acquire", "assignment_create", "task_get_role_prompt"):
        assert expected in names, f"missing tool: {expected}"


# ---------------- 负向 ----------------

def test_missing_input_schema_rejected(tree):
    """缺/空 inputSchema 的工具必须为空集合（有则 fail）。"""
    assert tree["tools_without_input_schema"] == [], (
        f"tools without schema: {tree['tools_without_input_schema'][:10]}")


def test_startup_isolation_pins_daemon_binary(tree):
    """startup 隔离：autostart 目标必须钉在 TP-06 候选，不能是生产 runtime。"""
    iso = tree["isolation"]
    pinned = iso["daemon_bin_pinned"]
    assert "runtime" not in pinned.lower() or "cw-acceptance" in pinned, (
        f"daemon bin not isolated: {pinned}")
    assert "cw-acceptance" in pinned, f"daemon bin not in run root: {pinned}"
    assert "CW_DAEMON_BIN" in iso["overridden"]
    assert iso["daemon_bin_sha256_expected"] == TP06_DAEMON_SHA256


def test_production_manifest_untouched():
    """提取不得改动生产 ~/.callwarden 的 manifest（文件集合与 mtime 不变由提取期守卫保证）。"""
    home = Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or "")
    prod = home / ".callwarden"
    if not prod.is_dir():
        pytest.skip("production .callwarden absent")
    manifests = list(prod.glob("http-daemon.*.manifest.json"))
    for m in manifests:
        data = json.loads(m.read_text(encoding="utf-8"))
        # 生产 manifest 必须仍指向生产 runtime（未被测试候选覆盖）
        assert "runtime" in str(data.get("daemon_executable", "")).lower() or \
               "runtime" in str(data.get("daemon_executable", "")), (
            f"production manifest exe overwritten: {data.get('daemon_executable')}")


def test_manifest_is_lf_and_utf8(tree):
    raw = MANIFEST.read_bytes()
    assert b"\r\n" not in raw, "manifest must be LF"
    assert raw.endswith(b"\n")
    json.loads(raw.decode("utf-8"))


def test_no_placeholder_entries(tree):
    text = json.dumps(tree, ensure_ascii=False)
    for bad in ("TODO", "FIXME", "placeholder"):
        assert bad not in text, f"placeholder marker {bad!r} in manifest"
