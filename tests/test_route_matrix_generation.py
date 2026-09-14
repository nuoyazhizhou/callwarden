# -*- coding: utf-8 -*-
"""RP-07 route matrix SSOT reconciliation 生成链测试。

覆盖（对应 frozen spec §12 / §830 验收命令）：
- gen_route_matrix.py --check 绿（JSON + Rust mirror byte-drift 自检）；
- verify_route_matrix.py 语义 verifier 绿（N 从源计算，不硬编码总数）；
- 矩阵 JSON 可由 generator 幂等再生成（generated_at 除外）；
- names(T) == tool_names(M)；
- get_role_view 路由形态（rust_native / role_view.get / MCP-001）；
- 两个非 MCP 残留条目已移除（final_zero_python_authority_audit /
  task_cascade_close）；
- 三个新治理工具已在矩阵且路由正确；
- N=242（本轮验收记录；生产脚本不保留该常量）。

运行：`python -m pytest tests/test_route_matrix_generation.py -q`
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_REPO_ROOT, "scripts"))

import gen_route_matrix as gen  # noqa: E402

_PYTHON = sys.executable
_GEN = os.path.join(_REPO_ROOT, "scripts", "gen_route_matrix.py")
_VERIFY = os.path.join(_REPO_ROOT, "scripts", "verify_route_matrix.py")


def _load_disk_matrix() -> dict:
    with open(gen._MATRIX_PATH, "r", encoding="utf-8") as fh:
        return json.load(fh)


# ------------------------------------------------------------ CLI 门禁


def test_generator_check_passes() -> None:
    """--check 必须绿：磁盘 JSON + Rust mirror 与内存生成 byte 一致。"""
    r = subprocess.run(
        [_PYTHON, _GEN, "--check"], capture_output=True, text=True, cwd=_REPO_ROOT
    )
    assert r.returncode == 0, f"--check failed:\n{r.stdout}\n{r.stderr}"


def test_semantic_verifier_passes() -> None:
    """verify_route_matrix.py 门禁必须绿（KNOWN_DRIFT 记录性输出不阻塞）。"""
    r = subprocess.run(
        [_PYTHON, _VERIFY], capture_output=True, text=True, cwd=_REPO_ROOT
    )
    assert r.returncode == 0, f"verifier failed:\n{r.stdout}\n{r.stderr}"
    assert "KNOWN_DRIFT" in r.stdout, "verifier 必须显式登记 compat 域已知漂移"


# ------------------------------------------------------------ SSOT 关系


def test_matrix_json_regenerable_idempotent() -> None:
    """磁盘矩阵 == generator 内存再生成（generated_at 除外）。"""
    disk = _load_disk_matrix()
    disk.pop("generated_at", None)
    mem = gen.build_matrix()
    mem.pop("generated_at", None)
    assert disk == mem


def test_names_T_equals_names_M() -> None:
    """门禁 1：实际 MCP 注册集合 == 矩阵工具名集合。"""
    t_names: set = set()
    for module in gen.TOOL_MODULES:
        t_names |= set(gen.extract_tool_names(module))
    m_names = {t["name"] for t in _load_disk_matrix()["tools"]}
    assert t_names == m_names


def test_rust_mirror_names_equals_M() -> None:
    """门禁 2：Rust mirror 条目名集合 == M。"""
    _, generated, _ = gen._read_rust_sections()
    import re

    rs_names = set(re.findall(r'ToolRoute \{ name: "(\w+)"', generated))
    m_names = {t["name"] for t in _load_disk_matrix()["tools"]}
    assert rs_names == m_names


# ------------------------------------------------------------ reconciliation 结果


def test_get_role_view_route_shape() -> None:
    """get_role_view 漂移已修复：rust_native / role_view.get / MCP-001。"""
    tools = {t["name"]: t for t in _load_disk_matrix()["tools"]}
    grv = tools["get_role_view"]
    assert grv["target_backend"] == "rust_native"
    assert grv["rpc_method"] == "role_view.get"
    assert grv["batch"] == "MCP-001"
    assert grv["status"] == "migrated"
    assert grv["op_class"] == "READ_ONLY"


@pytest.mark.parametrize(
    "name",
    ["final_zero_python_authority_audit", "task_cascade_close"],
)
def test_non_mcp_residuals_removed(name: str) -> None:
    """spec §12：Gate RPC / CLI 治理 RPC 不属于 MCP matrix。"""
    m_names = {t["name"] for t in _load_disk_matrix()["tools"]}
    assert name not in m_names


@pytest.mark.parametrize(
    "name,rpc_method,op_class",
    [
        ("task_assignment_status", "task.assignment.status", "READ_ONLY"),
        ("task_assignment_heartbeat", "task.assignment.heartbeat", "PROTECTED_MUTATION"),
        ("task_governance_projection", "task.governance_projection.get", "READ_ONLY"),
    ],
)
def test_new_governance_tools_routed(name: str, rpc_method: str, op_class: str) -> None:
    """三个新治理工具已入矩阵且路由形态正确（spec §12 清单）。"""
    tools = {t["name"]: t for t in _load_disk_matrix()["tools"]}
    t = tools[name]
    assert t["rpc_method"] == rpc_method
    assert t["op_class"] == op_class
    assert t["target_backend"] == "task_rpc"


def test_n_baseline_acceptance_record() -> None:
    """N=243 本轮验收记录（RP-08 N+1；仅测试断言；生产脚本不保留常量）。"""
    assert _load_disk_matrix()["total_tools"] == 243
    assert len(gen.build_matrix()["tools"]) == 243


def test_verifier_and_generator_share_single_source() -> None:
    """verifier 不得自维护工具枚举/路由规则/总数常量（spec §12 末段）。"""
    import re

    import verify_route_matrix as ver  # noqa: E402

    src = open(_VERIFY, "r", encoding="utf-8").read()
    # 剥离 docstring 后检查代码体：不得有总数常量赋值或历史数字字面量。
    body = re.sub(r'("""[\s\S]*?"""|\'\'\'[\s\S]*?\'\'\')', "", src)
    assert "EXPECTED_TOTAL" not in body, "verifier 不得硬编码总数"
    assert not re.search(r"\b(239|241|243|298)\b", body), (
        "verifier 不得硬编码历史总数常量"
    )
    # 工具枚举唯一来源是 generator 的 TOOL_MODULES / ROUTE_OVERRIDES。
    assert ver.gen is gen
