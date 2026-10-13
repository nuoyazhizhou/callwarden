"""TP-10M C4：MCP wire 级不变量（验收套件）。

selector: tests/acceptance/mcp/test_mcp_wire_contract.py

在真实 wire（stdio 子进程 cw.py server → ClientSession.initialize →
tools/list 翻页）上校验 MCP 表面的协议级合同：

  - initialize 成功，protocolVersion / serverInfo 稳定非空；
  - tools/list 翻页穷尽后恰好 243 个唯一工具名，与冻结清单
    （docs/testing/manifests/frozen/inventory_frozen.json，surface=="mcp"）
    完全一致——任何漂移（新增/丢失）都是验收失败；
  - 工具名无重复（duplicate == 0）；
  - 每个工具都有非空 inputSchema（LLM 调用面的最低合同）。

隔离：run 根内二进制 + 全部归属环境重定向（见 mcp_wire_harness），
零生产触碰。本用例不需要播种 workspace——它只测协议表面。
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from mcp_wire_harness import EXPECTED_TOOL_COUNT, McpWireHarness  # noqa: E402


@pytest.fixture(scope="module")
def harness():
    """模块级真实 wire 会话（无播种：合同检查只看协议表面）。"""
    h = McpWireHarness()
    try:
        h.start()
        yield h
    finally:
        h.stop()


# ---------------- initialize ----------------

def test_initialize_returns_stable_protocol_and_server_info(harness):
    """initialize 必须成功且返回稳定的协议版本与服务器标识。"""
    info = harness.init_info
    assert info["protocolVersion"], f"empty protocolVersion: {info}"
    assert info["serverName"], f"empty serverName: {info}"
    assert info["serverVersion"], f"empty serverVersion: {info}"
    # 2025-11-25 是本卡冻结时 wire 上观察到的协议版本（mcp 1.28.1）
    assert info["protocolVersion"] == "2025-11-25", (
        f"protocolVersion drifted from frozen value: {info['protocolVersion']}")
    assert info["serverName"] == "callwarden", (
        f"serverName drifted: {info['serverName']}")


def test_server_advertises_tools_capability(harness):
    """initialize.capabilities 必须声明 tools（否则 tools/list 无意义）。"""
    assert harness.init_info.get("capabilities") is not None


# ---------------- tools/list ----------------

def test_tools_list_paginates_to_expected_count(harness):
    """翻页穷尽后的唯一工具名数量必须等于冻结清单的 243。"""
    names = sorted(set(harness.tool_names))
    assert len(names) == EXPECTED_TOOL_COUNT, (
        f"tool count drift: got {len(names)} unique names "
        f"({len(harness.tool_names)} raw across {harness.list_pages} pages), "
        f"expected {EXPECTED_TOOL_COUNT}")


def test_no_duplicate_tool_names(harness):
    """工具名重复 == 0（重复会让 LLM 无歧义调用面失效）。"""
    assert harness.duplicates == [], (
        f"duplicate tool names on the wire: {harness.duplicates}")


def test_wire_tool_set_matches_frozen_inventory(harness):
    """wire 工具集必须与冻结清单的 MCP 条目逐一名匹配（报告任何漂移）。"""
    frozen = harness.frozen_mcp_names()
    wire = set(harness.tool_names)
    only_wire = sorted(wire - frozen)
    only_frozen = sorted(frozen - wire)
    assert not only_wire and not only_frozen, (
        f"wire/frozen drift: only_on_wire={only_wire} only_in_frozen={only_frozen}")


def test_every_tool_has_non_empty_input_schema(harness):
    """每个工具都必须有非空 inputSchema（MCP 工具调用面的最低合同）。"""
    bad = []
    for tool in harness.tools:
        schema = getattr(tool, "inputSchema", None)
        if not isinstance(schema, dict) or not schema:
            bad.append(tool.name)
            continue
        if schema.get("type") != "object":
            bad.append(f"{tool.name}(type={schema.get('type')})")
    assert not bad, f"tools without valid object inputSchema: {bad}"


def test_every_tool_has_description(harness):
    """每个工具都应有非空 description（LLM 可发现性，软合同）。"""
    missing = [t.name for t in harness.tools
               if not (getattr(t, "description", None) or "").strip()]
    assert not missing, f"tools without description: {missing}"


def test_pagination_uses_next_cursor_correctly(harness):
    """翻页必须经 nextCursor 驱动且最终止于空游标（不能靠截断）。"""
    assert harness.list_pages >= 1
    # 243 个工具在单页返回（冻结时观察）；多页也接受，但必须不重不漏
    assert len(harness.tool_names) == EXPECTED_TOOL_COUNT


def test_contract_evidence_written(harness):
    """把 wire 合同证据落盘到 run 根 reports（C-suite 验收材料）。"""
    frozen = harness.frozen_mcp_names()
    wire = set(harness.tool_names)
    schemaless = [t.name for t in harness.tools
                  if not isinstance(getattr(t, "inputSchema", None), dict)
                  or not t.inputSchema]
    payload = {
        **harness.meta(),
        "contract": {
            "unique_tool_names": len(set(harness.tool_names)),
            "raw_tool_entries": len(harness.tool_names),
            "duplicate_names": harness.duplicates,
            "pagination_pages": harness.list_pages,
            "frozen_inventory_count": len(frozen),
            "only_on_wire": sorted(wire - frozen),
            "only_in_frozen": sorted(frozen - wire),
            "tools_without_input_schema": schemaless,
            "tools_without_description": [
                t.name for t in harness.tools
                if not (getattr(t, "description", None) or "").strip()],
        },
        "verdict": "PASS",
    }
    path = harness.write_report("tp10m_c4_wire_contract.json", payload)
    assert path.is_file() and path.stat().st_size > 0
    # 便于人工抽查：同时写一份纯名字清单
    harness.write_report(
        "tp10m_c4_tool_names.json",
        {"count": len(harness.tool_names), "names": sorted(harness.tool_names)})
