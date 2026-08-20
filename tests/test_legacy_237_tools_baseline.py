# -*- coding: utf-8 -*-
"""Legacy 237 Tools Baseline 守护测试（B1 冻结 + B6 全量验收）。

对应任务：
- B1（T-1786590722456-db00d074-sub-1）：冻结 237 工具矩阵基线的完整性与诚实性约束；
- B6（T-1786590722456-db00d074-sub-6）：全量验收收口——current_status 从 unknown
  收口为核验后状态（runtime_verified / entry_verified），并固化全量入口冒烟断言。

验证点：
1. 矩阵 JSON 文件存在且可解析；
2. 工具总数为 237（与 MCP 注册数一致）；
3. 每个工具包含所有必需字段（含 4 个交叉核对字段）；
4. current_status 已收口：无 unknown、无 available，全部为
   runtime_verified（B 系列测试运行时覆盖）或 entry_verified（B6 入口核验通过）；
5. 全量入口冒烟：237 个工具的 source_file 存在、`def {tool_name}` 定义存在、
   `@mcp.tool(` 注册装饰器存在、函数体含统一入口引用（get_db / daemon client）；
6. 矩阵 SHA-256 与 matrix-sha256.txt 记录一致（含文件大小）。

本测试为纯只读文件校验，不访问数据库、不启动 daemon。
"""

import hashlib
import json
from pathlib import Path

import pytest

# 仓库根目录（tests/ 的上一级）
REPO_ROOT = Path(__file__).resolve().parent.parent
MATRIX_PATH = REPO_ROOT / ".trae-cn" / "evidence" / "mcp-tool-matrix-baseline.json"
SHA_PATH = REPO_ROOT / ".trae-cn" / "evidence" / "legacy-237-baseline-B1" / "matrix-sha256.txt"

# 每个工具必须具备的字段（B1 基线冻结的 18 个字段）
REQUIRED_FIELDS = [
    "tool_name",
    "source_file",
    "line_number",
    "module",
    "python_entry",
    "cli_entry",
    "daemon_rpc_method",
    "rust_handler",
    "backend",
    "route",
    "direct_sqlite_access",
    "operation_class",
    "workspace_scope",
    "fallback_policy",
    "test_file",
    "current_status",
    "blocking_reason",
    "db_calls_sample",
]

# 4 个交叉核对字段的合法取值约束：不允许再出现 "unknown" 占位
# （unknown 仅在确实无法静态确认时允许，B1 修复后已全部核对完毕）
CROSS_CHECK_FIELDS = ["cli_entry", "daemon_rpc_method", "rust_handler", "test_file"]


@pytest.fixture(scope="module")
def matrix():
    """加载并解析矩阵 JSON（模块级缓存，避免重复 IO）"""
    assert MATRIX_PATH.is_file(), f"矩阵基线文件不存在: {MATRIX_PATH}"
    with open(MATRIX_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data


@pytest.fixture(scope="module")
def tools(matrix):
    """矩阵中的工具列表"""
    assert "tools" in matrix, "矩阵缺少 tools 顶层键"
    return matrix["tools"]


def test_matrix_exists_and_parsable():
    """验证 1：矩阵 JSON 文件存在且可解析"""
    assert MATRIX_PATH.is_file(), f"矩阵基线文件不存在: {MATRIX_PATH}"
    with open(MATRIX_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)  # 解析失败会直接抛 JSONDecodeError
    assert isinstance(data, dict), "矩阵顶层必须是 JSON 对象"
    assert "metadata" in data, "矩阵缺少 metadata 顶层键"
    assert "tools" in data, "矩阵缺少 tools 顶层键"


def test_tool_count_is_237(tools):
    """验证 2：工具总数为 237（与 MCP 注册数一致）"""
    assert len(tools) == 237, f"工具总数应为 237，实际 {len(tools)}"
    # 工具名不得重复
    names = [t["tool_name"] for t in tools]
    assert len(set(names)) == 237, "存在重复的工具名"


def test_required_fields_present(tools):
    """验证 3：每个工具包含所有必需字段"""
    for tool in tools:
        missing = [f for f in REQUIRED_FIELDS if f not in tool]
        assert not missing, f"工具 {tool.get('tool_name', '?')} 缺少字段: {missing}"


def test_cross_check_fields_completed(tools):
    """验证 3b：4 个交叉核对字段已被实际核对（不允许大面积 unknown 占位）。

    B1 修复后：cli_entry/test_file 全部为确认值或 none；
    daemon_rpc_method/rust_handler 允许极少量 unknown（确实无法静态确认的
    3 个 snapshot-diff 工具），但总数不得超过 5。
    """
    for field in ("cli_entry", "test_file"):
        unknowns = [t["tool_name"] for t in tools if t[field] == "unknown"]
        assert not unknowns, f"字段 {field} 仍存在 unknown 占位: {unknowns[:5]}..."
    for field in ("daemon_rpc_method", "rust_handler"):
        unknowns = [t["tool_name"] for t in tools if t[field] == "unknown"]
        assert len(unknowns) <= 5, (
            f"字段 {field} 的 unknown 数量异常（{len(unknowns)}）: {unknowns[:10]}")


def test_current_status_finalized(tools):
    """验证 4：current_status 已收口（B6 全量验收）。

    B1 阶段 237 工具一律 unknown（仅确认注册）；B6 全量验收后收口：
    - 不允许 unknown（计划通过条件 2：无未解释的 unknown）；
    - 不允许 available（运行时全量验证未逐工具声明，避免过度声称）；
    - 状态只能是 runtime_verified（被 B 系列测试运行时覆盖）
      或 entry_verified（B6 入口核验通过：def + @mcp.tool 注册 + 统一入口）；
    - 每个工具必须记录 blocking_reason。
    """
    allowed = {"runtime_verified", "entry_verified"}
    unknown = [t["tool_name"] for t in tools if t["current_status"] == "unknown"]
    assert not unknown, f"仍存在 unknown 状态工具（B6 应全部收口）: {unknown[:5]}..."
    available = [t["tool_name"] for t in tools if t["current_status"] == "available"]
    assert not available, f"存在被误标为 available 的工具: {available[:5]}..."
    illegal = [
        t["tool_name"] for t in tools if t["current_status"] not in allowed
    ]
    assert not illegal, f"current_status 存在非法取值: {illegal[:5]}..."
    for tool in tools:
        assert tool["blocking_reason"], (
            f"工具 {tool['tool_name']} 缺少 blocking_reason")
    # 两种状态的占比应覆盖全部 237
    assert len(tools) == sum(1 for t in tools if t["current_status"] in allowed)


def test_all_tools_have_mcp_registration(tools):
    """验证 5：全量入口冒烟——每个工具在源码中存在 def 定义与 @mcp.tool() 注册。

    B6 固化：237 工具均须在对应 source_file 中以 `def {tool_name}(` 定义，
    且定义前文存在 `@mcp.tool(` 装饰器（MCP 注册入口）。防止矩阵与实现漂移。
    """
    import json as _json
    import re as _re

    with open(MATRIX_PATH, "r", encoding="utf-8") as f:
        data = _json.load(f)

    def next_def(text: str, start: int) -> int:
        m = _re.search(r"\ndef ", text[start:])
        return start + m.start() if m else len(text)

    problems = []
    for t in data["tools"]:
        name = t["tool_name"]
        sf = t["source_file"].replace("\\", "/")
        p = REPO_ROOT / sf
        if not p.is_file():
            problems.append(f"{name}: source_file 不存在 {sf}")
            continue
        text = p.read_text(encoding="utf-8")
        m = _re.search(r"\bdef\s+" + _re.escape(name) + r"\s*\(", text)
        if not m:
            problems.append(f"{name}: 未找到 def {name}（{sf}）")
            continue
        head = text[max(0, m.start() - 400):m.start()]
        if "@mcp.tool(" not in head:
            problems.append(f"{name}: def 前无 @mcp.tool( 装饰器（{sf}）")
    assert not problems, f"入口注册冒烟失败（{len(problems)}）:\n" + "\n".join(problems[:10])


def test_all_tools_have_unified_entry(tools):
    """验证 5b：全量入口冒烟——每个工具函数体含统一入口引用。

    统一入口 = get_db()（CodeGraphDB 单例直调）或 daemon client RPC。
    不允许工具绕过统一入口直连 SQLite（矩阵 direct_sqlite_access 已为 False）。
    部分工具经辅助函数中转（如 _collab_rpc_call），体正则放宽到 .call( 。
    """
    import json as _json
    import re as _re

    with open(MATRIX_PATH, "r", encoding="utf-8") as f:
        data = _json.load(f)

    def next_def(text: str, start: int) -> int:
        m = _re.search(r"\ndef ", text[start:])
        return start + m.start() if m else len(text)

    problems = []
    for t in data["tools"]:
        name = t["tool_name"]
        sf = t["source_file"].replace("\\", "/")
        p = REPO_ROOT / sf
        if not p.is_file():
            continue  # 已在上一个用例覆盖
        text = p.read_text(encoding="utf-8")
        m = _re.search(r"\bdef\s+" + _re.escape(name) + r"\s*\(", text)
        if not m:
            continue  # 已在上一个用例覆盖
        body = text[m.end(): next_def(text, m.end())]
        if not _re.search(r"\bget_db\b|\b_get_daemon_client\b|\.call\(", body):
            problems.append(f"{name}: 函数体无统一入口引用（{sf}）")
    assert not problems, f"统一入口冒烟失败（{len(problems)}）:\n" + "\n".join(problems[:10])


def test_sha256_matches_record():
    """验证 5：矩阵 SHA-256 与 matrix-sha256.txt 记录一致（含文件大小）"""
    assert SHA_PATH.is_file(), f"SHA 记录文件不存在: {SHA_PATH}"
    raw = MATRIX_PATH.read_bytes()
    actual_hash = hashlib.sha256(raw).hexdigest().upper()
    actual_size = len(raw)

    lines = SHA_PATH.read_text(encoding="utf-8").splitlines()
    assert lines, "matrix-sha256.txt 为空"
    recorded_hash = lines[0].split()[0].upper()
    assert recorded_hash == actual_hash, (
        f"矩阵 SHA-256 不一致: 记录 {recorded_hash}，实际 {actual_hash}"
        f"（矩阵被修改后必须重新运行 .trae-cn/evidence/B1_fix_fields.py 刷新记录）")

    # 文件大小必须与记录一致（防止"约 XXKB"式模糊声明回潮）
    size_lines = [l for l in lines if l.startswith("文件大小")]
    assert size_lines, "matrix-sha256.txt 缺少文件大小记录行"
    assert str(actual_size) in size_lines[0], (
        f"文件大小不一致: 记录行 '{size_lines[0]}'，实际 {actual_size} bytes")
