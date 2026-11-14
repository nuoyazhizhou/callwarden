#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""gen_coverage_manifest.py 脚本行为单测（T-12 销项）。

与 test_coverage_manifest.py 的分工：
- test_coverage_manifest.py：断言磁盘 manifest 内容与真相源等价（产物视角）
- 本文件：断言脚本自身行为（CLI 参数处理、解析逻辑、自校验分支、失败路径）

测试策略：
- 解析/校验逻辑用 monkeypatch 喂 fake 输入，不真跑 86 次 `cw <cmd> --help`
  （与 test_coverage_manifest.py 头注同因：CI 时长约束；真实全量跑由
  CI coverage step 承担，本机验收用 `--print-summary` 手动实跑一次）
- monkeypatch 目标是 gen_coverage_manifest 模块命名空间（脚本用
  `from ... import` 引入真相源，属性引用须打在 gcm 上）
- 唯一真实 subprocess 用例（F 组冒烟）只跑一条顶层命令，锚定真实 argparse 行为
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from types import SimpleNamespace

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)
_SCRIPTS_DIR = os.path.join(_REPO_ROOT, "scripts")
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

import gen_coverage_manifest as gcm  # noqa: E402

# 已知叶级提取失败命令（test_coverage_manifest.py EXPECTED_EXTRACT_ERRORS 同源）
_KNOWN_EXTRACT_ERRORS = {"test", "pki"}


# ---------------------------------------------------------------------------
# fake 构造器
# ---------------------------------------------------------------------------

def _fake_cmd(name: str) -> SimpleNamespace:
    return SimpleNamespace(name=name)


def _fake_cli_cat(key: str, *names: str) -> SimpleNamespace:
    return SimpleNamespace(key=key, commands=[_fake_cmd(n) for n in names])


def _fake_tool_cat(key: str, *tools: str) -> SimpleNamespace:
    return SimpleNamespace(key=key, tools=list(tools))


def _fake_tool(name: str, op_class: str = "READ_ONLY") -> dict:
    return {
        "name": name,
        "rpc_method": f"rpc_{name}",
        "op_class": op_class,
        "target_backend": "core",
        "current_backend": "core",
    }


def _fake_matrix(*tools: dict) -> dict:
    return {"tools": list(tools)}


def _fake_proc(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(args=[], returncode=returncode,
                                       stdout=stdout, stderr="")


def _light_manifest() -> dict:
    """main() 行为测试用轻量 manifest（结构同 generate_manifest 产物）。"""
    return {
        "schema_version": "1.0",
        "generated_at": "2026-11-14T00:00:00",
        "counts": {"cli_top": 1, "cli_leaf": 1, "mcp": 1},
        "cli_leaves": {"db": ["add"]},
        "leaf_extract_errors": {},
        "entries": [
            {"id": "cli:db", "kind": "cli_top", "category": "workspace_database",
             "name": "db"},
            {"id": "cli:db:add", "kind": "cli_leaf", "category": "workspace_database",
             "name": "add", "parent": "db"},
            {"id": "mcp:tool_x", "kind": "mcp", "category": "workspace_database",
             "name": "tool_x", "rpc_method": "rpc_x", "op_class": "READ_ONLY",
             "target_backend": "core", "current_backend": "core"},
        ],
    }


# ---------------------------------------------------------------------------
# A 组：collect_cli_tops
# ---------------------------------------------------------------------------

def test_collect_cli_tops_positive():
    """真相源展开：返回 (name, key) 对，无重复归类，名字非空。"""
    tops = gcm.collect_cli_tops()
    assert tops, "COMMAND_CATEGORIES 展开不应为空"
    names = [n for n, _ in tops]
    assert len(names) == len(set(names)), "顶层命令重复归类未被拦截"
    assert all(isinstance(n, str) and n for n, _ in tops)


def test_collect_cli_tops_rejects_duplicate(monkeypatch):
    """负向：同一顶层命令归两类 → SystemExit。"""
    monkeypatch.setattr(gcm, "COMMAND_CATEGORIES", [
        _fake_cli_cat("cat_a", "db", "init"),
        _fake_cli_cat("cat_b", "db"),
    ])
    with pytest.raises(SystemExit, match="重复归类"):
        gcm.collect_cli_tops()


# ---------------------------------------------------------------------------
# B 组：extract_cli_leaves 解析逻辑（monkeypatch subprocess.run）
# ---------------------------------------------------------------------------

def _patch_run(monkeypatch, stdout: str) -> None:
    monkeypatch.setattr(gcm.subprocess, "run",
                        lambda *a, **kw: _fake_proc(stdout))


def test_extract_leaves_simple_choices(monkeypatch):
    _patch_run(monkeypatch, "usage: cw db [-h] {add,list,remove} ...\n")
    assert gcm.extract_cli_leaves("db") == ["add", "list", "remove"]


def test_extract_leaves_multiline_usage(monkeypatch):
    """跨行 usage 块拼接后仍能提取 choices 组。"""
    _patch_run(monkeypatch,
               "usage: cw db [-h] {add,\n"
               "                   list} ...\n\n"
               "description text\n")
    assert gcm.extract_cli_leaves("db") == ["add", "list"]


def test_extract_leaves_chinese_usage_prefix(monkeypatch):
    """standalone 命令自定义中文 help（前缀 `用法:`）同样可解析。"""
    _patch_run(monkeypatch, "用法: cw install {all,core} ...\n")
    assert gcm.extract_cli_leaves("install") == ["all", "core"]


def test_extract_leaves_strips_choices_whitespace(monkeypatch):
    _patch_run(monkeypatch, "usage: cw x { a , b } ...\n")
    assert gcm.extract_cli_leaves("x") == ["a", "b"]


def test_extract_leaves_no_choices_returns_empty(monkeypatch):
    """无子命令的顶层命令 → 空列表（非失败）。"""
    _patch_run(monkeypatch, "usage: cw init [-h] [--opt]\n")
    assert gcm.extract_cli_leaves("init") == []


def test_extract_leaves_missing_usage_rejected(monkeypatch):
    """输出无 usage/用法 前缀 → RuntimeError（不静默吞掉）。"""
    _patch_run(monkeypatch, "hello world, no usage here\n")
    with pytest.raises(RuntimeError, match="未产出 usage"):
        gcm.extract_cli_leaves("x")


def test_extract_leaves_help_in_choices_rejected(monkeypatch):
    """choices 组异常含 -h/--help → RuntimeError。"""
    _patch_run(monkeypatch, "usage: cw x {-h,--help,real} ...\n")
    with pytest.raises(RuntimeError, match="choices 组异常"):
        gcm.extract_cli_leaves("x")


# ---------------------------------------------------------------------------
# C 组：build_cli_entries（降级与确定性）
# ---------------------------------------------------------------------------

def _patch_two_tops(monkeypatch):
    monkeypatch.setattr(gcm, "COMMAND_CATEGORIES", [
        _fake_cli_cat("cat_a", "db", "init"),
    ])


def test_build_cli_entries_positive(monkeypatch):
    _patch_two_tops(monkeypatch)
    monkeypatch.setattr(gcm, "extract_cli_leaves",
                        lambda name: {"db": ["add", "list"], "init": []}[name])
    entries, leaves, errors = gcm.build_cli_entries()
    assert errors == {}
    assert leaves == {"db": ["add", "list"], "init": []}
    ids = [e["id"] for e in entries]
    assert ids == ["cli:db", "cli:db:add", "cli:db:list", "cli:init"]
    leaf = next(e for e in entries if e["kind"] == "cli_leaf")
    assert leaf["parent"] == "db"


def test_build_cli_entries_degrades_single_failure(monkeypatch):
    """单命令提取异常降级：记入 errors，不炸整体（正误混合）。"""
    _patch_two_tops(monkeypatch)

    def flaky(name):
        if name == "db":
            raise RuntimeError("no usage")
        return []

    monkeypatch.setattr(gcm, "extract_cli_leaves", flaky)
    entries, leaves, errors = gcm.build_cli_entries()
    assert set(errors) == {"db"}
    assert leaves == {"db": [], "init": []}
    assert "cli:init" in [e["id"] for e in entries]


def test_build_cli_entries_rejects_nondeterministic(monkeypatch):
    """负向：两次提取结果不一致（非确定性）→ SystemExit。"""
    _patch_two_tops(monkeypatch)
    calls: dict = {}

    def unstable(name):
        calls[name] = calls.get(name, 0) + 1
        # 同一命令第一次调用（pass1）与第二次（pass2）返回不同结果
        return ["a"] if calls[name] == 1 else ["b"]

    monkeypatch.setattr(gcm, "extract_cli_leaves", unstable)
    with pytest.raises(SystemExit, match="不一致"):
        gcm.build_cli_entries()


# ---------------------------------------------------------------------------
# D 组：build_mcp_entries（双真相源校验）
# ---------------------------------------------------------------------------

def _patch_mcp(monkeypatch, matrix, cats):
    monkeypatch.setattr(gcm, "build_matrix", lambda: matrix)
    monkeypatch.setattr(gcm, "TOOL_CATEGORIES", cats)


def test_build_mcp_entries_positive(monkeypatch):
    _patch_mcp(monkeypatch,
               _fake_matrix(_fake_tool("tool_x")),
               [_fake_tool_cat("cat_a", "tool_x")])
    entries = gcm.build_mcp_entries()
    assert len(entries) == 1
    e = entries[0]
    assert e["id"] == "mcp:tool_x"
    assert e["op_class"] == "READ_ONLY"
    assert e["rpc_method"] == "rpc_tool_x"


def test_build_mcp_entries_rejects_source_mismatch(monkeypatch):
    """负向：matrix 有、分类无 → SystemExit。"""
    _patch_mcp(monkeypatch,
               _fake_matrix(_fake_tool("tool_x"), _fake_tool("tool_y")),
               [_fake_tool_cat("cat_a", "tool_x")])
    with pytest.raises(SystemExit, match="双真相源集合不一致"):
        gcm.build_mcp_entries()


def test_build_mcp_entries_rejects_duplicate_classification(monkeypatch):
    """负向：同一工具归两类 → SystemExit。"""
    _patch_mcp(monkeypatch,
               _fake_matrix(_fake_tool("tool_x")),
               [_fake_tool_cat("cat_a", "tool_x"),
                _fake_tool_cat("cat_b", "tool_x")])
    with pytest.raises(SystemExit, match="重复分类"):
        gcm.build_mcp_entries()


def test_build_mcp_entries_rejects_duplicate_tool_names(monkeypatch):
    """负向：build_matrix 内部工具名重复 → SystemExit。"""
    _patch_mcp(monkeypatch,
               _fake_matrix(_fake_tool("tool_x"), _fake_tool("tool_x")),
               [_fake_tool_cat("cat_a", "tool_x")])
    with pytest.raises(SystemExit, match="工具名重复"):
        gcm.build_mcp_entries()


def test_build_mcp_entries_rejects_illegal_op_class(monkeypatch):
    """负向：op_class 不在 OP_CLASSES 枚举 → SystemExit。"""
    _patch_mcp(monkeypatch,
               _fake_matrix(_fake_tool("tool_x", op_class="BOGUS")),
               [_fake_tool_cat("cat_a", "tool_x")])
    with pytest.raises(SystemExit, match="非法 op_class"):
        gcm.build_mcp_entries()


# ---------------------------------------------------------------------------
# E 组：main() CLI 行为（monkeypatch generate_manifest，不真跑全量）
# ---------------------------------------------------------------------------

def test_main_writes_output_json(monkeypatch, tmp_path):
    out = tmp_path / "m.json"
    monkeypatch.setattr(gcm, "generate_manifest", _light_manifest)
    monkeypatch.setattr(sys, "argv",
                        ["gen_coverage_manifest.py", "--output", str(out)])
    rc = gcm.main()
    assert rc == 0
    assert out.exists(), "默认形态必须写输出文件"
    raw = out.read_text(encoding="utf-8")
    assert raw.endswith("\n"), "输出文件须以换行结尾"
    data = json.loads(raw)
    assert data["counts"] == {"cli_top": 1, "cli_leaf": 1, "mcp": 1}
    assert [e["id"] for e in data["entries"]] == [
        "cli:db", "cli:db:add", "mcp:tool_x"]


def test_main_print_summary_writes_nothing(monkeypatch, tmp_path, capsys):
    out = tmp_path / "m.json"
    monkeypatch.setattr(gcm, "generate_manifest", _light_manifest)
    monkeypatch.setattr(sys, "argv",
                        ["gen_coverage_manifest.py", "--print-summary"])
    rc = gcm.main()
    assert rc == 0
    assert not out.exists(), "--print-summary 形态不得写文件"
    captured = capsys.readouterr()
    assert "cli_top=1" in captured.out
    assert "cli_leaf=1" in captured.out
    assert "mcp=1" in captured.out


# ---------------------------------------------------------------------------
# F 组：真实冒烟（各 1 条，锚定真实行为；全量跑由 CI coverage step 承担）
# ---------------------------------------------------------------------------

def test_real_extract_cli_leaves_smoke():
    """真实 subprocess 跑一条顶层命令的 --help，锚定真实 argparse 行为。"""
    tops = gcm.collect_cli_tops()
    # 跳过已知无 argparse 提取形态的命令（test/pki，见模块头注）
    name = next(n for n, _ in tops if n not in _KNOWN_EXTRACT_ERRORS)
    leaves = gcm.extract_cli_leaves(name)
    assert isinstance(leaves, list)
    assert all(isinstance(x, str) and x for x in leaves), (
        f"真实叶提取返回非法项: {name} -> {leaves!r}"
    )
