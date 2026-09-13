"""Phase 4 Minor: MCP 工具暴露 diff/compare 方法测试

修复 T-1783751538837-33e1: DaemonClient 已有 diff_callers/diff_callees/compare_snapshots 方法，
但 MCP 层未暴露。本测试验证 3 个 MCP 工具已注册。
"""
import ast
import os
import unittest


class TestMcpDiffExpose(unittest.TestCase):
    """验证 mcp_server.py 中 diff_callers/diff_callees/compare_snapshots MCP 工具已注册"""

    def setUp(self):
        server_dir = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "server",
        )
        # MCP 重构后 @mcp.tool() 装饰器分布在 server/tools 功能域模块，
        # 合并扫描 mcp_server.py + server/tools/*.py，避免漏检迁移后的工具。
        paths = [os.path.join(server_dir, "mcp_server.py")]
        tools_dir = os.path.join(server_dir, "tools")
        if os.path.isdir(tools_dir):
            paths += [
                os.path.join(tools_dir, fname)
                for fname in sorted(os.listdir(tools_dir))
                if fname.endswith(".py") and fname != "__init__.py"
            ]
        chunks = []
        for path in paths:
            with open(path, "r", encoding="utf-8") as f:
                chunks.append(f.read())
        self.content = "\n".join(chunks)

    def test_diff_callers_mcp_tool_exists(self):
        """mcp_server.py 应包含 diff_callers MCP 工具"""
        self.assertIn("def diff_callers(", self.content,
                      "diff_callers MCP 工具应存在")
        self.assertIn("@mcp.tool()", self.content)

    def test_diff_callees_mcp_tool_exists(self):
        """mcp_server.py 应包含 diff_callees MCP 工具"""
        self.assertIn("def diff_callees(", self.content,
                      "diff_callees MCP 工具应存在")

    def test_compare_snapshots_mcp_tool_exists(self):
        """mcp_server.py 应包含 compare_snapshots MCP 工具"""
        self.assertIn("def compare_snapshots(", self.content,
                      "compare_snapshots MCP 工具应存在")

    def test_diff_callers_routes_read_only_rpc(self):
        """diff_callers MCP 工具经 `_route` 下发 query.diff_callers（READ_ONLY）。

        stale 依据：旧断言 `client.diff_callers(`（客户端便捷方法直连）已过期——
        `server/tools/tools_security.py:113` 已退化为一行式
        `return _route('query.diff_callers', {...}, 'READ_ONLY')`，
        工具层不再出现 `client.diff_callers(` 调用点。
        """
        self.assertIn("_route('query.diff_callers'", self.content)
        self.assertIn("'READ_ONLY'", self.content)

    def test_diff_callees_routes_read_only_rpc(self):
        """diff_callees MCP 工具经 `_route` 下发 query.diff_callees（READ_ONLY）。

        stale 依据：旧断言 `client.diff_callees(` 已过期——
        `server/tools/tools_security.py:135` 已退化为一行式
        `return _route('query.diff_callees', {...}, 'READ_ONLY')`。
        """
        self.assertIn("_route('query.diff_callees'", self.content)

    def test_compare_snapshots_routes_read_only_rpc(self):
        """compare_snapshots MCP 工具经 `_route` 下发 admin.snapshot_compare（READ_ONLY）。

        stale 依据：旧断言 `client.compare_snapshots(` 已过期——
        `server/tools/tools_security.py:159` 已退化为一行式
        `return _route('admin.snapshot_compare', {...}, 'READ_ONLY')`。
        """
        self.assertIn("_route('admin.snapshot_compare'", self.content)

    def test_all_three_decorated_with_mcp_tool(self):
        """3 个工具都应有 @mcp.tool() 装饰器"""
        # 解析 AST 找到函数定义前的装饰器
        tree = ast.parse(self.content)
        found = {"diff_callers": False, "diff_callees": False, "compare_snapshots": False}
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in found:
                for dec in node.decorator_list:
                    dec_str = ast.dump(dec) if hasattr(ast, 'dump') else str(dec)
                    if 'mcp.tool' in dec_str or 'tool' in dec_str:
                        found[node.name] = True
        for name, is_decorated in found.items():
            self.assertTrue(is_decorated,
                            f"{name} 应有 @mcp.tool() 装饰器")

    def test_daemon_client_has_methods(self):
        """DaemonClient 类应有 diff_callers/diff_callees/compare_snapshots 方法"""
        client_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "server", "daemon_client.py"
        )
        with open(client_path, "r", encoding="utf-8") as f:
            client_content = f.read()
        self.assertIn("def diff_callers(", client_content)
        self.assertIn("def diff_callees(", client_content)
        self.assertIn("def compare_snapshots(", client_content)


if __name__ == "__main__":
    unittest.main()
