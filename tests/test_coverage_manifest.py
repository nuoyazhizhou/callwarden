#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""覆盖 manifest 完整性断言（full-cli-mcp-coverage-test step 2，AC-5）。

闸门语义：命令/工具增删若未重新生成 manifest（或未归类）直接挂测试。

- CLI 顶层集合 == cli.categories.COMMAND_CATEGORIES 展开（当前 84）
- MCP 集合 == server.tools._categories.TOOL_CATEGORIES
           == scripts.gen_route_matrix.build_matrix() 工具集（当前 243）
- op_class 三值枚举合法；id 全局唯一；cli_leaf.parent 必须是已登记顶层
- counts 与条目实际数量一致

叶级（cli_leaf）一致性由生成器 gen_coverage_manifest.generate_manifest()
的「提取两次结果一致」自校验保障；本测试不做 84 次 help 重跑（CI 时长约束），
只校验磁盘 manifest 与真相源的结构等价。
"""
from __future__ import annotations

import json
import os
import sys
from collections import Counter

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from cli.categories import COMMAND_CATEGORIES  # noqa: E402
from gen_route_matrix import OP_CLASSES, build_matrix  # noqa: E402
from server.tools._categories import TOOL_CATEGORIES  # noqa: E402

MANIFEST_PATH = os.path.join(_REPO_ROOT, "outputs", "coverage_manifest.json")

EXPECTED_OP_CLASSES = set(OP_CLASSES)
VALID_KINDS = {"cli_top", "cli_leaf", "mcp"}


@pytest.fixture(scope="module")
def manifest() -> dict:
    if not os.path.exists(MANIFEST_PATH):
        pytest.fail(
            f"manifest 不存在: {MANIFEST_PATH} —— 先运行 "
            "`python scripts/gen_coverage_manifest.py` 重新生成"
        )
    with open(MANIFEST_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


@pytest.fixture(scope="module")
def entries(manifest: dict) -> list:
    return manifest["entries"]


# ---------------------------------------------------------------------------
# 真相源期望集合
# ---------------------------------------------------------------------------

def _expected_cli_tops() -> dict:
    """{顶层命令名: 分类 key}，含重复归类检测。"""
    tops: dict = {}
    for cat in COMMAND_CATEGORIES:
        for cmd in cat.commands:
            if cmd.name in tops:
                pytest.fail(f"真相源内部缺陷: 顶层命令 {cmd.name} 归类重复")
            tops[cmd.name] = cat.key
    return tops


def _expected_mcp() -> dict:
    """{工具名: (分类 key, op_class)}，双真相源集合必须一致。"""
    matrix = build_matrix()
    by_name = {t["name"]: t for t in matrix["tools"]}
    cat_of = {}
    for cat in TOOL_CATEGORIES:
        for name in cat.tools:
            if name in cat_of:
                pytest.fail(f"真相源内部缺陷: MCP 工具 {name} 分类重复")
            cat_of[name] = cat.key
    assert set(by_name) == set(cat_of), (
        "MCP 双真相源集合不一致: "
        f"matrix-only={sorted(set(by_name) - set(cat_of))} "
        f"cats-only={sorted(set(cat_of) - set(by_name))}"
    )
    return {n: (cat_of[n], by_name[n]["op_class"]) for n in cat_of}


# ---------------------------------------------------------------------------
# 断言用例
# ---------------------------------------------------------------------------

def test_ids_unique(entries: list) -> None:
    ids = [e["id"] for e in entries]
    dup = [i for i, c in Counter(ids).items() if c > 1]
    assert not dup, f"manifest id 重复: {dup}"


def test_kinds_valid(entries: list) -> None:
    bad = {e["id"]: e["kind"] for e in entries if e["kind"] not in VALID_KINDS}
    assert not bad, f"非法 kind: {bad}"


def test_cli_top_matches_source_of_truth(entries: list) -> None:
    expected = _expected_cli_tops()
    actual = {
        e["name"]: e["category"]
        for e in entries if e["kind"] == "cli_top"
    }
    assert set(actual) == set(expected), (
        f"manifest-only={sorted(set(actual) - set(expected))} "
        f"source-only={sorted(set(expected) - set(actual))}"
    )
    mismatched = {
        n: (actual[n], expected[n])
        for n in actual if actual[n] != expected[n]
    }
    assert not mismatched, f"顶层命令分类不一致: {mismatched}"


def test_cli_top_count_is_84(entries: list) -> None:
    """当前事实锚点：84 顶层。增减命令时本用例与真相源同步更新。"""
    n = sum(1 for e in entries if e["kind"] == "cli_top")
    assert n == 84, f"CLI 顶层命令数漂移: {n} != 84（若为合法增减，"
    f"请同步 cli/categories.py 与本锚点）"


def test_cli_leaf_parents_registered(entries: list) -> None:
    tops = {e["name"] for e in entries if e["kind"] == "cli_top"}
    bad = [
        e["id"] for e in entries
        if e["kind"] == "cli_leaf" and e.get("parent") not in tops
    ]
    assert not bad, f"cli_leaf.parent 未登记为顶层命令: {bad[:10]}"


def test_cli_leaf_categories_match_parent(entries: list) -> None:
    cat_of_top = {
        e["name"]: e["category"] for e in entries if e["kind"] == "cli_top"
    }
    bad = [
        e["id"] for e in entries
        if e["kind"] == "cli_leaf"
        and e["category"] != cat_of_top.get(e.get("parent"))
    ]
    assert not bad, f"cli_leaf 分类与父命令不一致: {bad[:10]}"


def test_mcp_matches_source_of_truth(entries: list) -> None:
    expected = _expected_mcp()
    actual = {
        e["name"]: (e["category"], e["op_class"])
        for e in entries if e["kind"] == "mcp"
    }
    assert set(actual) == set(expected), (
        f"manifest-only={sorted(set(actual) - set(expected))} "
        f"source-only={sorted(set(expected) - set(actual))}"
    )
    mismatched = {
        n: (actual[n], expected[n])
        for n in actual if actual[n] != expected[n]
    }
    assert not mismatched, f"MCP 工具分类/op_class 不一致: {mismatched}"


def test_mcp_count_is_243(entries: list) -> None:
    """当前事实锚点：243 工具。增减工具时本用例与真相源同步更新。"""
    n = sum(1 for e in entries if e["kind"] == "mcp")
    assert n == 243, f"MCP 工具数漂移: {n} != 243（若为合法增减，"
    f"请同步 server/tools/_categories.py 与本锚点）"


def test_mcp_op_classes_valid(entries: list) -> None:
    bad = {
        e["id"]: e["op_class"] for e in entries
        if e["kind"] == "mcp" and e["op_class"] not in EXPECTED_OP_CLASSES
    }
    assert not bad, f"非法 op_class: {bad}"


def test_counts_consistent(manifest: dict, entries: list) -> None:
    counts = manifest["counts"]
    for kind in ("cli_top", "cli_leaf", "mcp"):
        actual = sum(1 for e in entries if e["kind"] == kind)
        assert counts[kind] == actual, (
            f"counts.{kind}={counts[kind]} 与实际条目数 {actual} 不一致"
        )


def test_leaf_extract_errors_are_intentional(manifest: dict) -> None:
    """叶级提取失败必须逐个显式豁免（防止静默降级造成假覆盖）。

    cw test 为无 argparse 的 pytest 直通入口（cw.py:150-168），--help 被
    当作模块名导入而失败 —— 属已知形态，豁免；新增失败命令须人工核实后
    在 EXPECTED_EXTRACT_ERRORS 登记，否则测试挂。
    """
    EXPECTED_EXTRACT_ERRORS = {"test"}
    actual = set(manifest.get("leaf_extract_errors", {}))
    unexpected = actual - EXPECTED_EXTRACT_ERRORS
    assert not unexpected, (
        f"出现未豁免的叶级提取失败: {sorted(unexpected)} —— "
        f"先人工核实 cw <cmd> --help 行为，再更新 EXPECTED_EXTRACT_ERRORS"
    )
    stale = EXPECTED_EXTRACT_ERRORS - actual
    assert not stale, (
        f"豁免项已不再失败，请从 EXPECTED_EXTRACT_ERRORS 移除: {sorted(stale)}"
    )
