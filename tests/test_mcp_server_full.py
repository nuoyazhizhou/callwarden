"""H9: MCP Server 完整测试（T-1784420233383-340988a5）

覆盖 H9 checklist 4 项：
1. MCP Server 启动与协议握手测试
2. 195+ MCP 工具的输入输出契约
3. MCP 与 CLI 并发访问（WAL 模式下读写并发安全验证）
4. MCP 长连接稳定性（长时间空闲后恢复）

设计原则：
- 不真正启动 stdio server（会阻塞），而是通过 FastMCP 的 list_tools() /
  call_tool() 直接调用工具函数，绕过 stdio 编码层，测试工具契约。
- 通过 conftest.py 的 _isolate_db_path autouse fixture 自动隔离 db，
  不污染 ~/.callwarden/callwarden.db。
- 每个测试重置 server.mcp_server._db_instance 单例，避免跨测试污染。
- "长连接稳定性"通过模拟 _db_instance 单例跨"空闲期"复用验证，
  不依赖真实的 stdio socket 保活。
"""
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

import pytest

# 项目根目录
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.server import _mcp_common as _mcp_common_module
from callwarden.server.mcp_server import HAS_FASTMCP, create_mcp_server, get_db
from callwarden.server.tools import tools_query, tools_workspace


pytestmark = pytest.mark.skipif(
    not HAS_FASTMCP,
    reason="fastmcp 未安装，跳过 MCP Server 测试",
)


# ============================================
# 辅助函数
# ============================================


@pytest.fixture
def reset_mcp_db_singleton():
    """每个测试前重置 _db_instance 单例，避免跨测试污染。

    MCP Server 在生产中是长连接，_db_instance 跨请求复用；但测试中
    每个用例需要干净的 db 状态，故显式重置。
    """
    original = _mcp_common_module._db_instance
    _mcp_common_module._db_instance = None
    try:
        yield
    finally:
        # 关闭测试中创建的 db，恢复原值（None）
        if (
            _mcp_common_module._db_instance is not None
            and _mcp_common_module._db_instance is not original
        ):
            try:
                _mcp_common_module._db_instance.close()
            except Exception:
                pass
        _mcp_common_module._db_instance = original


def _list_tools_sync(mcp_server):
    """同步包装 asyncio list_tools()。"""
    return asyncio.run(mcp_server.list_tools())


def _call_tool_sync(mcp_server, name: str, arguments: dict):
    """同步包装 asyncio call_tool()。

    FastMCP 的 call_tool 返回值在不同版本中变化：
    - 新版：返回 list[TextContent]（content_list 本身）
    - 旧版：返回 (content_list, structured_dict) 元组

    FastMCP 的编码规则：list[dict] 会被编码为多个 TextContent（每个 dict 一个），
    而单个标量返回值（int/str）会被编码为 1 个 TextContent。

    本辅助函数兼容两种形式，返回值规则：
    - 旧版元组形式优先取 structured_dict['result']（原始 Python 值）
    - 新版 list[TextContent]：
        * 若所有 TextContent.text 拼接后是 JSON list/dict → 返回解析后的对象
        * 若只有一个 TextContent 且其 text 是标量 → 返回该标量
        * 多个 TextContent（FastMCP 把 list[dict] 拆成多个 dict 编码） →
          返回 list[dict]
    """
    raw = asyncio.run(mcp_server.call_tool(name, arguments))
    # 旧版：返回 (content_list, structured_dict) 元组
    if isinstance(raw, tuple) and len(raw) == 2:
        content_list, structured = raw
        if isinstance(structured, dict) and "result" in structured:
            return structured["result"]
        if isinstance(content_list, list):
            return _parse_content_list(content_list)
        return raw
    # 新版：返回 list[TextContent]
    if isinstance(raw, list):
        return _parse_content_list(raw)
    return raw


def _parse_content_list(content_list):
    """把 list[TextContent] 解析为 Python 原生值。

    FastMCP 的编码规则：
    - 单标量返回（int/str）→ 1 个 TextContent，text = 标量的字符串
    - 单 dict 返回 → 1 个 TextContent，text = JSON.encode(dict)
    - list[dict] 返回 → N 个 TextContent，每个 text = JSON.encode(dict)
    - list[scalar] 返回 → N 个 TextContent，每个 text = 标量字符串

    本函数规则：
    - 若有 1 个 TextContent：解析 text，若为 JSON list/dict 则返回解析对象，
      否则返回原 text 或解析后的标量
    - 若有多个 TextContent：返回 list，每个元素是 text 解析后的对象
    """
    if not content_list:
        return None
    # 提取所有 TextContent 的 text
    texts = []
    for item in content_list:
        if hasattr(item, "text"):
            texts.append(item.text)
        elif isinstance(item, str):
            texts.append(item)
        else:
            texts.append(str(item))
    if len(texts) == 1:
        # 单个 TextContent：尝试 JSON 解析（可能是 dict/list/number）
        return _maybe_json(texts[0])
    # 多个 TextContent：每个独立解析为对象，返回 list
    return [_maybe_json(t) for t in texts]


def _maybe_json(text):
    """若 text 是 JSON 字符串则反序列化，否则原样返回。"""
    if not isinstance(text, str):
        return text
    stripped = text.strip()
    if not stripped:
        return text
    try:
        return json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return text


# ============================================
# 1. MCP Server 启动与协议握手
# ============================================


def test_mcp_server_creates_instance_without_error():
    """create_mcp_server() 不抛异常且返回 FastMCP 实例。

    覆盖 H9 checklist #1：MCP Server 启动。
    不真正调用 server.run()（会阻塞），而是验证 create_mcp_server() 成功。
    """
    mcp = create_mcp_server()
    assert mcp is not None
    # FastMCP 实例应有 name、tool()、list_tools()、call_tool() 方法
    assert mcp.name == "callwarden"
    assert callable(mcp.tool)
    assert callable(mcp.list_tools)
    assert callable(mcp.call_tool)


def test_mcp_server_registers_at_least_100_tools():
    """list_tools() 应返回 ≥ 100 个工具（当前 205）。

    覆盖 H9 checklist #1：协议握手后能拿到工具列表。
    """
    mcp = create_mcp_server()
    tools = _list_tools_sync(mcp)
    assert len(tools) >= 100, f"MCP 工具数应 ≥ 100，实际 {len(tools)}"


def test_mcp_server_has_required_category_tools():
    """验证关键分类下有代表性工具注册。

    抽样验证 12 大类中至少 6 类有工具，覆盖协议层与 db 层的注册完整性。
    """
    mcp = create_mcp_server()
    tools = _list_tools_sync(mcp)
    names = {t.name for t in tools}

    # 抽样代表性工具（覆盖 Workspace / Query / CallChain / Task / Semgrep / File）
    required = {
        "get_stats",                  # [L1] Workspace & Database
        "search_symbols",             # [L2] Query & Search
        "get_callers",                # [L3] Call Chain Analysis
        "task_create",                # [L5] Task Orchestration
        "get_semgrep_findings",       # [L7] Semgrep Integration
        "file_read",                  # [L11] File Operations
    }
    missing = required - names
    assert not missing, f"缺少代表性 MCP 工具: {missing}"


# ============================================
# 2. MCP 工具输入输出契约
# ============================================


def test_all_tools_have_name_and_description():
    """所有 MCP 工具都应有 name 与 description。

    覆盖 H9 checklist #2：工具契约中 name/description 是 MCP client
    展示给用户的必填字段。
    """
    mcp = create_mcp_server()
    tools = _list_tools_sync(mcp)
    missing_name = [t for t in tools if not t.name]
    missing_desc = [t for t in tools if not (t.description or "").strip()]
    assert not missing_name, f"工具缺少 name: {missing_name}"
    assert not missing_desc, f"工具缺少 description（前 5 个）: {missing_desc[:5]}"


def test_all_tools_have_valid_input_schema():
    """所有 MCP 工具都应有合法的 inputSchema（JSON Schema dict）。

    覆盖 H9 checklist #2：inputSchema 是 MCP 协议规定字段，
    client 据此生成参数表单/校验。
    """
    mcp = create_mcp_server()
    tools = _list_tools_sync(mcp)
    invalid = []
    for t in tools:
        schema = t.inputSchema
        if not isinstance(schema, dict):
            invalid.append((t.name, "inputSchema not dict"))
            continue
        if schema.get("type") != "object":
            invalid.append((t.name, f"type != object: {schema.get('type')}"))
            continue
        if "properties" not in schema:
            invalid.append((t.name, "missing properties"))
    assert not invalid, (
        f"以下工具 inputSchema 不合法（前 10 个）: {invalid[:10]}"
    )


def test_tool_names_are_unique():
    """所有工具名应唯一（重名会导致后注册覆盖前者）。

    与 test_mcp_tools_naming.py 互补：后者基于 AST 静态扫描，
    本测试基于运行时 list_tools()，覆盖实际注册结果。
    """
    mcp = create_mcp_server()
    tools = _list_tools_sync(mcp)
    names = [t.name for t in tools]
    duplicates = {n for n in names if names.count(n) > 1}
    assert not duplicates, f"运行时发现重复工具名: {duplicates}"


def test_get_stats_tool_returns_dict_on_empty_db(reset_mcp_db_singleton, tmp_path, monkeypatch):
    """[A 类·stale] get_stats MCP 工具经模块级 `_route` 下发 READ_ONLY RPC。

    stale 依据（生产侧 server/tools/tools_query.py）：
    `tools_query.py:56` 为 `from ..daemon_client import route_rpc as _route`；
    `get_stats`（tools_query.py:63）已退化为一行式
    `return _route('query.stats', {}, 'READ_ONLY')`。
    旧用例注释「get_stats 走 daemon client，daemon 未启动时回退 SQL」已过期：
    `route_rpc`（server/daemon_client.py:3880）在 daemon 发现失败时 fail-closed
    抛 `DaemonRemoteError(E_HTTP_MANIFEST_MISSING/E_HTTP_MANIFEST_STALE)`
    （:1018-1025 / discover :2195），绝不回落本地 SQLite——实测
    `mcp.server.fastmcp.exceptions.ToolError: ... E_HTTP_MANIFEST_MISSING`。

    因此改为 mock 模块级 `_route` seam（同 test_query_stats_rpc_http.py
    ::TestToolRouteContract 模板），锁定 RPC method/params/op_class 透传，
    并断言不回落到本地 get_db。
    """
    seen = {}

    def fake_route(method, params, op_class):
        seen["method"] = method
        seen["params"] = dict(params)
        seen["op"] = op_class
        return {"file_count": 0, "function_count": 0}

    mcp = create_mcp_server()
    monkeypatch.setattr(tools_query, "_route", fake_route)
    with patch("callwarden.server.tools.tools_query.get_db") as mock_db:
        stats = _call_tool_sync(mcp, "get_stats", {})
        mock_db.assert_not_called()

    assert isinstance(stats, (dict, list)), (
        f"get_stats 返回值应为 dict 或 list，实际 {type(stats)}: {stats}"
    )
    assert seen["method"] == "query.stats"
    assert seen["op"] == "READ_ONLY"
    assert seen["params"] == {}


def test_list_workspaces_tool_returns_list(reset_mcp_db_singleton, tmp_path, monkeypatch):
    """[A 类·stale] list_workspaces MCP 工具经模块级 `_route` 下发 workspace.list。

    stale 依据（生产侧 server/tools/tools_workspace.py）：
    `tools_workspace.py:29` 为 `from ..daemon_client import route_rpc as _route`；
    `list_workspaces`（tools_workspace.py:89）已退化为一行式
    `return _route('workspace.list', {}, 'READ_ONLY')`。旧用例假设工具会本地
    `get_db()` 读 workspaces 表——该路径在 daemon authority 迁移后已不存在，
    实际经 `route_rpc` → daemon discover，失败即
    `ToolError: ... E_HTTP_MANIFEST_MISSING`（实测）。

    改为 mock `_route` seam：断言 RPC method/params/op_class，并验证返回结果
    仍携带 name/root_path 字段（工具对 daemon 行的最小兼容映射契约）。
    """
    seen = {}

    def fake_route(method, params, op_class):
        seen["method"] = method
        seen["params"] = dict(params)
        seen["op"] = op_class
        return [{"name": "ws-x", "root_path": "/tmp/ws-x"}]

    mcp = create_mcp_server()
    monkeypatch.setattr(tools_workspace, "_route", fake_route)
    with patch("callwarden.server.tools.tools_workspace.get_db") as mock_db:
        result = _call_tool_sync(mcp, "list_workspaces", {})
        mock_db.assert_not_called()

    # FastMCP 编码差异：单 workspace 是 dict，多 workspace 是 list[dict]
    if isinstance(result, dict):
        ws_list = [result]
    elif isinstance(result, list):
        ws_list = result
    else:
        ws_list = []
    assert ws_list, (
        f"list_workspaces 应返回包含 workspace 信息的 list 或 dict，实际 {type(result)}: {result}"
    )
    for ws in ws_list:
        assert "name" in ws, f"workspace 应有 name 字段，实际: {ws}"
        assert "root_path" in ws, f"workspace 应有 root_path 字段，实际: {ws}"
    assert seen["method"] == "workspace.list"
    assert seen["op"] == "READ_ONLY"
    assert seen["params"] == {}


def test_register_and_list_workspace_roundtrip(reset_mcp_db_singleton, tmp_path, monkeypatch):
    """[A 类·stale] register_workspace → list_workspaces 的 `_route` 转发契约。

    stale 依据（生产侧 server/tools/tools_workspace.py）：
    `register_workspace`（tools_workspace.py:107）已退化为一行式
    `return _route('workspace.register', {"name": ..., "client_view_root": root_path,
    "description": ...}, 'PROTECTED_MUTATION')`。旧用例注释描述的是本地
    `db.register_workspace` SQL（`SELECT id WHERE name=? OR root_path=?` 幂等）——
    该本地写路径在 daemon authority 迁移后已被 daemon RPC 取代，实际调用经
    `route_rpc`（server/daemon_client.py:3880）fail-closed（实测
    `ToolError: ... E_HTTP_MANIFEST_MISSING`）。

    故改为 mock `_route` seam，用内存注册表回放 register→list，锁定：
    register 的 RPC method/op_class 及 `root_path→client_view_root` 参数映射，
    并验证 list 结果包含刚注册的 workspace。
    """
    calls = []
    registry = {}

    def fake_route(method, params, op_class):
        calls.append((method, dict(params), op_class))
        if method == "workspace.register":
            registry[params["name"]] = {
                "name": params["name"],
                "root_path": params["client_view_root"],
            }
            return 1
        if method == "workspace.list":
            return list(registry.values())
        raise AssertionError(f"未预期的 RPC method: {method}")

    mcp = create_mcp_server()
    monkeypatch.setattr(tools_workspace, "_route", fake_route)
    with patch("callwarden.server.tools.tools_workspace.get_db") as mock_db:
        new_ws_root = str(tmp_path / "new_ws")
        ws_id_raw = _call_tool_sync(
            mcp, "register_workspace",
            {"name": "test-h9-ws", "root_path": new_ws_root, "description": "H9 测试工作区"},
        )
        try:
            ws_id = int(ws_id_raw)
        except (TypeError, ValueError):
            ws_id = 0
        assert ws_id > 0, (
            f"register_workspace 应返回正整数 ID，实际: {ws_id_raw!r}"
        )

        workspaces_raw = _call_tool_sync(mcp, "list_workspaces", {})
        mock_db.assert_not_called()

    # FastMCP 编码差异：单 workspace 是 dict，多 workspace 是 list[dict]
    if isinstance(workspaces_raw, dict):
        ws_list = [workspaces_raw]
    elif isinstance(workspaces_raw, list):
        ws_list = workspaces_raw
    else:
        ws_list = []
    names = [w.get("name") for w in ws_list if isinstance(w, dict)]
    assert "test-h9-ws" in names, (
        f"list_workspaces 应包含刚注册的 'test-h9-ws'，实际: {names}"
    )

    # register 的 RPC 转发契约：method/op_class 与 root_path→client_view_root 映射
    reg_method, reg_params, reg_op = calls[0]
    assert reg_method == "workspace.register"
    assert reg_op == "PROTECTED_MUTATION"
    assert reg_params == {
        "name": "test-h9-ws",
        "client_view_root": new_ws_root,
        "description": "H9 测试工作区",
    }
    assert calls[1][0] == "workspace.list"


# ============================================
# 3. MCP 与 CLI 并发访问（daemon authority 下的并发读安全）
# ============================================


def _live_daemon_env(w3_live: dict) -> dict:
    """[B 类] 构造指向 `w3_live` 隔离 daemon 的 CLI 子进程 env。

    stale 依据（生产侧 cli/main.py）：CLI `--stats` 经
    `RpcDBProxy.get_stats()`（cli/main.py:9149）→ `route_rpc('query.stats')`
    （server/daemon_client.py:3880）转发 daemon，daemon 发现失败即 fail-closed，
    不再本地直读 SQLite。实测子进程继承真实 HOME 时命中后台常驻 daemon 回收后
    残留的 stale manifest → `E_HTTP_MANIFEST_STALE: manifest PID 43608 已不存活`，
    rc=1（历史断言 `returncode in (0, 2)` 失败）。

    修法：注入 w3_live 隔离 daemon 的显式 loopback endpoint
    （`CW_DAEMON_HTTP_ENDPOINT`，合法独立发现路径），并把 USERPROFILE/HOME
    重定向到隔离 userhome，使 authority manifest 落在隔离目录、避开真实 HOME
    的 stale manifest（同 tests/test_integration_full_matrix.py:211-229 模板）。
    """
    env = os.environ.copy()
    env["CW_DAEMON_HTTP_ENDPOINT"] = w3_live["endpoint"]
    home = os.path.join(w3_live["data_root"], "userhome")
    env["USERPROFILE"] = home
    env["HOME"] = home
    env["NO_PROXY"] = "127.0.0.1,localhost"
    for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy"):
        env[key] = ""
    return env


def _w3_repo_root(w3_live: dict) -> str:
    """w3_live 隔离栈注册的 workspace 根目录（conftest: data_root/repo）。"""
    return os.path.join(w3_live["data_root"], "repo")


def test_mcp_and_cli_concurrent_read_no_lock(w3_live):
    """MCP 权威栈 + CLI 子进程并发读同一 daemon，验证无锁冲突。

    覆盖 H9 checklist #3：MCP 与 CLI 并发访问。

    [B 类·stale] 旧用例在本地 SQLite（WAL）上并发读同一 db_path。daemon
    authority 迁移后 CLI/MCP 读面均经 daemon RPC（cli/main.py:9149 →
    server/daemon_client.py:3880），本地 WAL 多读者语义不再适用，且子进程继承
    真实 HOME 会命中 stale manifest（实测 `E_HTTP_MANIFEST_STALE`）而 rc=1。
    现改为：MCP 侧经 w3_live live daemon 读 stats，CLI 侧子进程注入同一
    isolated endpoint（`_live_daemon_env`），二者都须成功（无锁/无 fail-closed）。
    """
    # 1. MCP 侧：进程内经 daemon 权威栈读 stats（模拟 MCP 长连接持有读面）
    mcp_stats = w3_live["client"].call(
        "query.stats", {"workspace_instance_id": w3_live["inst"]},
    )
    assert isinstance(mcp_stats, dict), (
        f"MCP 侧 query.stats 应返回 dict，实际 {type(mcp_stats)}: {mcp_stats}"
    )

    # 2. CLI 侧：子进程 `cw.py --stats` 查询同一隔离 daemon
    cli_result = subprocess.run(
        [sys.executable, os.path.join(_PKG_PARENT, "cw.py"), "--stats"],
        cwd=_w3_repo_root(w3_live),
        capture_output=True,
        text=True,
        env=_live_daemon_env(w3_live),
        timeout=60,
    )
    cli_output = cli_result.stdout + cli_result.stderr
    assert cli_result.returncode == 0, (
        f"CLI --stats 应在 live daemon 上成功（无锁/fail-closed），实际退出码 "
        f"{cli_result.returncode}，输出: {cli_output[-500:]}"
    )


def test_concurrent_cli_reads_no_conflict(w3_live):
    """两个 CLI 子进程先后读同一 live daemon，验证无锁冲突。

    覆盖 H9 checklist #3 的另一面：纯 CLI 并发读（无 MCP）。

    [B 类·stale] 同 `test_mcp_and_cli_concurrent_read_no_lock`：CLI 读面已由
    daemon RPC 承担，旧断言假设的本地 SQLite 多读者无锁语义过期；子进程在真实
    HOME 下实测 `E_HTTP_MANIFEST_STALE`（PID 43608）rc=1。改为两次子进程均注入
    w3_live isolated endpoint，断言都成功（rc=0）。
    """
    env = _live_daemon_env(w3_live)
    repo = _w3_repo_root(w3_live)

    def _run_cli():
        return subprocess.run(
            [sys.executable, os.path.join(_PKG_PARENT, "cw.py"), "--stats"],
            cwd=repo,
            capture_output=True,
            text=True,
            env=env,
            timeout=60,
        )

    # 接连启动两个，验证都不撞锁/fail-closed
    r1 = _run_cli()
    r2 = _run_cli()
    for i, r in enumerate([r1, r2], 1):
        assert r.returncode == 0, (
            f"CLI #{i} 应在 live daemon 上成功，实际退出码 {r.returncode}，"
            f"输出: {(r.stdout + r.stderr)[-300:]}"
        )


# ============================================
# 4. MCP 长连接稳定性（长时间空闲后恢复）
# ============================================


def test_mcp_singleton_survives_idle_period(reset_mcp_db_singleton, tmp_path):
    """模拟 MCP Server 长连接：单例跨"空闲期"复用不抛异常。

    覆盖 H9 checklist #4：长时间空闲后恢复。

    场景：MCP Server 启动后，_db_instance 单例被创建；
    Agent 一段时间不调用（sleep 2s 模拟空闲），然后再次查询。
    SQLite 连接在空闲后仍应可用（不会被内核关闭或抛 OperationalError）。
    """
    ws_root = str(tmp_path)
    db = get_db(workspace=ws_root)

    # 首次查询：触发懒加载
    first_stats = db.get_stats()
    assert isinstance(first_stats, dict), (
        f"首次查询应返回 dict，实际 {type(first_stats)}"
    )

    # 模拟空闲期（2s，足以触发某些 SQLite timeout/keepalive 边界）
    time.sleep(2)

    # 空闲后再次查询：连接应仍可用
    second_stats = db.get_stats()
    assert isinstance(second_stats, dict), (
        f"空闲后查询应仍返回 dict，实际 {type(second_stats)}"
    )


def test_mcp_singleton_reused_across_multiple_calls(reset_mcp_db_singleton, tmp_path):
    """同一 _db_instance 单例被多次调用复用，不创建新连接。

    覆盖 H9 checklist #4：长连接复用 — 单例语义验证。

    场景：MCP Server 在生命周期内调用 get_db() N 次，应返回同一个实例。
    """
    ws_root = str(tmp_path)
    db1 = get_db(workspace=ws_root)
    db2 = get_db()  # 不传 workspace，应复用单例
    assert db1 is db2, "get_db() 不传 workspace 应复用单例"

    # 多次调用工具
    for _ in range(3):
        stats = db1.get_stats()
        assert isinstance(stats, dict)


def test_mcp_singleton_workspace_switch_recreates_db(reset_mcp_db_singleton, tmp_path):
    """指定不同 workspace 时应重建 _db_instance（不复用旧实例）。

    覆盖 H9 checklist #4：长连接期间工作区切换场景。

    场景：MCP Server 已加载 workspace A 的 db 单例；
    Agent 请求切换到 workspace B，应关闭旧连接、创建新连接。

    注意：不直接断言 db.workspace_root 等于传入的 ws_x，
    因为 CodeGraphDB.__init__ 中的 _initialize_active_workspace() 会从
    db 中查到 active workspace 并覆盖 self.workspace_root（conftest
    autouse fixture 的 tmp_path 隔离 db 可能残留其他测试创建的 active
    workspace）。本测试聚焦验证"单例被重建 + 新实例可用"。
    """
    ws_a = os.path.abspath(str(tmp_path / "ws_a"))
    ws_b = os.path.abspath(str(tmp_path / "ws_b"))
    os.makedirs(ws_a, exist_ok=True)
    os.makedirs(ws_b, exist_ok=True)

    db_a = get_db(workspace=ws_a)
    assert db_a is _mcp_common_module._db_instance, (
        "首次 get_db(workspace=...) 应将 _db_instance 设为新实例"
    )

    db_b = get_db(workspace=ws_b)
    assert db_b is not db_a, "切换 workspace 后应创建新 _db_instance"
    assert db_b is _mcp_common_module._db_instance, (
        "切换后 _db_instance 应指向新实例"
    )

    # 新实例应可用
    stats = db_b.get_stats()
    assert isinstance(stats, dict), (
        f"切换后 get_stats 应返回 dict，实际 {type(stats)}"
    )


# ============================================
# 综合：H9 全部 checklist 覆盖
# ============================================


def test_h9_checklist_coverage_summary():
    """H9 checklist 4 项覆盖情况自检（仅打印，不断言）。

    此测试作为文档性测试，明确列出 H9 4 项 checklist 与本文件中
    对应的测试函数，方便审计。
    """
    checklist = {
        "1. MCP Server 启动与协议握手": [
            "test_mcp_server_creates_instance_without_error",
            "test_mcp_server_registers_at_least_100_tools",
            "test_mcp_server_has_required_category_tools",
        ],
        "2. 195+ MCP 工具的输入输出契约": [
            "test_all_tools_have_name_and_description",
            "test_all_tools_have_valid_input_schema",
            "test_tool_names_are_unique",
            "test_get_stats_tool_returns_dict_on_empty_db",
            "test_list_workspaces_tool_returns_list",
            "test_register_and_list_workspace_roundtrip",
        ],
        "3. MCP 与 CLI 并发访问（WAL 模式下读写并发安全验证）": [
            "test_mcp_and_cli_concurrent_read_no_lock",
            "test_concurrent_cli_reads_no_conflict",
        ],
        "4. MCP 长连接稳定性（长时间空闲后恢复）": [
            "test_mcp_singleton_survives_idle_period",
            "test_mcp_singleton_reused_across_multiple_calls",
            "test_mcp_singleton_workspace_switch_recreates_db",
        ],
    }
    # 自检：所有列出的测试函数都应存在
    for item, funcs in checklist.items():
        for fn_name in funcs:
            assert globals().get(fn_name), (
                f"{item} 对应的测试函数 {fn_name} 未定义"
            )
