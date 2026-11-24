"""MCP HTTP 网关原型单测 + wire 集成测试。

覆盖：
- Bearer token 签发/校验（角色/项目/过期/篡改/密钥）
- 工具面 profile 裁剪（core/graph/security/all）
- 响应预算出口 enforce_budget（未超限原样 / 超限截断 fail-closed）
- LoopForge MCPServerDefinition 形状解析（合法 / 非法 transport / 缺认证）
- wire 集成：起网关（随机端口），streamable HTTP client 拉工具清单、
  认证失败 401、/health 豁免
"""

import asyncio
import json
import threading
import time

import pytest
from mcp.types import TextContent

from callwarden.server import mcp_http_gateway as gw
from callwarden.server.tools._categories import TOOL_CATEGORIES


# ---------------------------------------------------------------------------
# Bearer token 生命周期
# ---------------------------------------------------------------------------


class TestTokenLifecycle:
    def test_issue_and_verify_roundtrip(self):
        secret = b"unit-test-secret"
        token = gw.issue_token("harness", "callwarden", ttl_seconds=600, secret=secret)
        ok, payload, reason = gw.verify_token(token, secret=secret)
        assert ok, reason
        assert payload["role"] == "harness"
        assert payload["project"] == "callwarden"
        assert payload["exp"] > time.time()

    def test_tampered_payload_rejected(self):
        secret = b"unit-test-secret"
        token = gw.issue_token("harness", "callwarden", ttl_seconds=600, secret=secret)
        prefix, body, sig = token.split(".")
        # 篡改 payload（换角色）后签名不匹配
        forged_body = body[:-2] + ("A" if body[-2] != "A" else "B") + body[-1]
        ok, _, reason = gw.verify_token(f"{prefix}.{forged_body}.{sig}", secret=secret)
        assert not ok
        assert "签名" in reason

    def test_wrong_secret_rejected(self):
        token = gw.issue_token("harness", "callwarden", ttl_seconds=600, secret=b"secret-a")
        ok, _, reason = gw.verify_token(token, secret=b"secret-b")
        assert not ok

    def test_expired_token_rejected(self):
        secret = b"unit-test-secret"
        token = gw.issue_token("harness", "callwarden", ttl_seconds=-10, secret=secret)
        ok, _, reason = gw.verify_token(token, secret=secret)
        assert not ok
        assert "过期" in reason

    def test_malformed_token_rejected(self):
        ok, _, reason = gw.verify_token("not-a-token", secret=b"s")
        assert not ok
        ok2, _, reason2 = gw.verify_token("v1.onlybody", secret=b"s")
        assert not ok2

    def test_issue_requires_role_and_project(self):
        with pytest.raises(ValueError):
            gw.issue_token("", "callwarden", secret=b"s")
        with pytest.raises(ValueError):
            gw.issue_token("harness", "", secret=b"s")

    def test_verifier_accepts_bearer_header(self):
        secret = b"unit-test-secret"
        verifier = gw.make_token_verifier(secret)
        token = gw.issue_token("harness", "callwarden", secret=secret)
        ok, payload, _ = verifier(f"Bearer {token}")
        assert ok
        ok2, _, reason2 = verifier("Basic abc")
        assert not ok2
        ok3, _, reason3 = verifier("")
        assert not ok3


# ---------------------------------------------------------------------------
# 工具面 profile
# ---------------------------------------------------------------------------


class TestProfile:
    def test_normalize_accepts_known_profiles(self):
        for p in ("core", "graph", "security", "all", " ALL ", "Core"):
            assert gw.normalize_profile(p) in ("core", "graph", "security", "all")

    def test_normalize_rejects_unknown(self):
        with pytest.raises(ValueError):
            gw.normalize_profile("everything")

    def test_profile_map_covers_all_categories(self):
        mapped = set()
        for cats in gw.PROFILE_CATEGORY_MAP.values():
            mapped.update(cats)
        assert mapped == {cat.key for cat in TOOL_CATEGORIES}, "17 分类必须全部归入某一档"

    def test_core_excludes_security_tools(self):
        core = gw.profile_tool_names("core")
        security = gw.profile_tool_names("security")
        assert "search_symbols" in core
        assert "audit_verify_chain" in security
        # 分类之间允许工具重叠（仓库现状），但关键治理工具不得进入 core 档
        assert "audit_verify_chain" not in core
        assert "search_symbols" not in security

    def test_all_equals_full_registry(self):
        all_names = gw.profile_tool_names("all")
        expected = set()
        for cat in TOOL_CATEGORIES:
            expected.update(cat.tools)
        assert all_names == expected
        assert len(all_names) == 244

    def test_apply_profile_removes_out_of_scope_tools(self):
        mcp = gw.FastMCP("profile-test")

        @mcp.tool()
        def search_symbols(query: str) -> str:
            return query

        @mcp.tool()
        def audit_bootstrap(dry_run: bool) -> str:
            return str(dry_run)

        removed = gw.apply_profile(mcp, "core")
        remaining = set(mcp._tool_manager._tools.keys())
        assert "search_symbols" in remaining
        assert "audit_bootstrap" not in remaining
        assert removed == 1


# ---------------------------------------------------------------------------
# 响应预算出口
# ---------------------------------------------------------------------------


class TestEnforceBudget:
    def test_small_result_passes_through(self):
        result = [{"ok": True, "data": "小结果"}]
        budgeted, info = gw.enforce_budget(result, max_bytes=4096)
        assert budgeted is result
        assert info["exceeded"] is False
        assert info["actual_bytes"] > 0

    def test_oversized_result_fail_closed(self):
        result = ([TextContent(type="text", text="x" * 100000)], {"result": "x" * 100000})
        budgeted, info = gw.enforce_budget(result, max_bytes=1024)
        assert info["exceeded"] is True
        assert info["actual_bytes"] > 1024
        # 保持 FastMCP.call_tool 的 (unstructured, structured) 形状
        assert isinstance(budgeted, tuple) and len(budgeted) == 2
        payload = json.loads(budgeted[0][0].text)
        assert payload["budget_exceeded"] is True
        assert payload["budget_bytes"] == 1024
        assert "preview" in payload
        assert budgeted[1]["budget_exceeded"] is True

    def test_exact_boundary_not_exceeded(self):
        result = "x" * 100
        raw_len = len(json.dumps(result, ensure_ascii=False).encode("utf-8"))
        budgeted, info = gw.enforce_budget(result, max_bytes=raw_len)
        assert info["exceeded"] is False
        assert budgeted is result

    def test_install_response_budget_wraps_call_tool(self):
        mcp = gw.FastMCP("budget-test")

        @mcp.tool()
        def make_blob() -> str:
            return "y" * 50000

        gw.install_response_budget(mcp, max_bytes=1024)
        contents, structured = asyncio.run(mcp.call_tool("make_blob", {}))
        payload = json.loads(contents[0].text)
        assert payload["budget_exceeded"] is True
        assert structured["budget_exceeded"] is True

    def test_install_response_budget_passes_small_results(self):
        mcp = gw.FastMCP("budget-test-small")

        @mcp.tool()
        def small() -> str:
            return "ok"

        gw.install_response_budget(mcp, max_bytes=4096)
        contents, structured = asyncio.run(mcp.call_tool("small", {}))
        assert contents[0].text == "ok"
        assert structured == {"result": "ok"}


# ---------------------------------------------------------------------------
# LoopForge MCPServerDefinition 形状解析
# ---------------------------------------------------------------------------


class TestServerDefinition:
    def test_valid_definition(self):
        defn = {
            "name": "callwarden",
            "transport": "http",
            "url": "http://127.0.0.1:8765/mcp",
            "headers": {"Authorization": "Bearer v1.abc.def"},
            "timeout_sec": 30,
        }
        parsed = gw.parse_mcp_server_definition(defn)
        assert parsed["url"] == "http://127.0.0.1:8765/mcp"
        assert parsed["token"] == "v1.abc.def"
        assert parsed["timeout_sec"] == 30.0
        assert parsed["name"] == "callwarden"

    def test_default_timeout(self):
        parsed = gw.parse_mcp_server_definition({
            "transport": "http",
            "url": "https://gw.example/mcp",
            "headers": {"Authorization": "Bearer t"},
        })
        assert parsed["timeout_sec"] == 30.0

    def test_rejects_non_http_transport(self):
        with pytest.raises(ValueError, match="E_MCP_DEF_TRANSPORT"):
            gw.parse_mcp_server_definition({
                "transport": "stdio",
                "url": "http://127.0.0.1/mcp",
                "headers": {"Authorization": "Bearer t"},
            })

    def test_rejects_missing_authorization(self):
        with pytest.raises(ValueError, match="E_MCP_DEF_AUTH"):
            gw.parse_mcp_server_definition({
                "transport": "http",
                "url": "http://127.0.0.1/mcp",
                "headers": {"X-Other": "1"},
            })

    def test_rejects_non_bearer_authorization(self):
        with pytest.raises(ValueError, match="E_MCP_DEF_AUTH"):
            gw.parse_mcp_server_definition({
                "transport": "http",
                "url": "http://127.0.0.1/mcp",
                "headers": {"Authorization": "Basic abc"},
            })

    def test_rejects_bad_url(self):
        with pytest.raises(ValueError, match="E_MCP_DEF_URL"):
            gw.parse_mcp_server_definition({
                "transport": "http",
                "url": "not-a-url",
                "headers": {"Authorization": "Bearer t"},
            })

    def test_rejects_bad_timeout(self):
        with pytest.raises(ValueError, match="E_MCP_DEF_TIMEOUT"):
            gw.parse_mcp_server_definition({
                "transport": "http",
                "url": "http://127.0.0.1/mcp",
                "headers": {"Authorization": "Bearer t"},
                "timeout_sec": -1,
            })

    def test_rejects_non_object(self):
        with pytest.raises(ValueError, match="E_MCP_DEF_TYPE"):
            gw.parse_mcp_server_definition("http://example")


# ---------------------------------------------------------------------------
# wire 集成：真实起网关（streamable HTTP），client 连接拉工具清单
# ---------------------------------------------------------------------------


def _run_uvicorn(app, holder):
    import uvicorn

    config = uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning")
    server = uvicorn.Server(config)
    holder["server"] = server
    server.run()


def _wait_port(holder, timeout=30.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        server = holder.get("server")
        if server is not None and server.started and server.servers:
            return server.servers[0].sockets[0].getsockname()[1]
        time.sleep(0.05)
    raise RuntimeError("网关未在超时内启动")


@pytest.fixture(scope="module")
def gateway():
    secret = b"wire-test-secret"
    app, _mcp = gw.create_gateway_app(
        profile="security", secret=secret, budget_bytes=65536, bind_workspace=False
    )
    holder = {}
    thread = threading.Thread(target=_run_uvicorn, args=(app, holder), daemon=True)
    thread.start()
    port = _wait_port(holder)
    yield {"port": port, "secret": secret}
    server = holder.get("server")
    if server is not None:
        server.should_exit = True
    thread.join(timeout=10)


def test_health_endpoint_exempts_auth(gateway):
    import urllib.request

    url = f"http://127.0.0.1:{gateway['port']}/health"
    with urllib.request.urlopen(url, timeout=10) as resp:
        body = json.loads(resp.read())
        assert resp.status == 200
        assert body["ok"] is True


def test_missing_token_gets_401(gateway):
    import urllib.error
    import urllib.request

    url = f"http://127.0.0.1:{gateway['port']}/mcp"
    req = urllib.request.Request(url, data=b"{}", method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            pytest.fail(f"期望 401，实际 {resp.status}")
    except urllib.error.HTTPError as exc:
        assert exc.code == 401
        body = json.loads(exc.read())
        assert body["error"] == "unauthorized"


def _wire_tools_list(port: str, secret: bytes) -> dict:
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async def _run() -> dict:
        token = gw.issue_token("harness", "callwarden", ttl_seconds=600, secret=secret)
        url = f"http://127.0.0.1:{port}/mcp"
        headers = {"Authorization": f"Bearer {token}"}
        async with streamablehttp_client(url, headers=headers) as (read, write, _):
            async with ClientSession(read, write) as session:
                init = await session.initialize()
                tools = await session.list_tools()
                return init, tools

    init, tools = asyncio.run(_run())
    return {"server_name": init.serverInfo.name, "tool_names": {t.name for t in tools.tools}}


def test_wire_tools_list_respects_profile(gateway):
    result = _wire_tools_list(str(gateway["port"]), gateway["secret"])
    names = result["tool_names"]
    assert result["server_name"] == "callwarden-gateway"
    # profile=security：security 档位工具可见
    assert "audit_verify_chain" in names
    # core 档位工具被裁剪
    assert "search_symbols" not in names
    # 清单是 security 档位的子集（分类清单可能超前于实际注册）
    assert names <= gw.profile_tool_names("security")
    assert len(names) > 0
