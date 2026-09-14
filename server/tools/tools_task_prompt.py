"""Role Prompt Compiler v1 MCP HTTP thin client（RP-08，spec §4.4）

单一薄工具 task_get_role_prompt(task_id) -> RolePromptBundle：
- 唯一 authority 是 daemon RPC ``task.prompt.compile``（RP-05 落地，
  dispatch.rs ``task.prompt.compile`` 分支）；本模块只做 HTTP JSON-RPC
  透传，无本地模板、无 SQLite、无 workspace/role 推导、无 fallback 业务。
- spec §4.4 硬约束：工具不接受 role、format、workspace、credential 或
  lease 参数，不复用 blind ``get_role_view``。
- fail-closed：daemon 不可达/降级时经 route_rpc 返回结构化错误，绝不
  回退本地渲染或本地 SQL（§4.3 客户端不得推导 workspace；§4.1 单一
  RPC 取代一切本地拼装）。
- 纯净度：不 import sqlite3 / get_db / db 业务模块（check_client_purity
  HARD gate；server/tools 239+1 工具薄壳纪律）。

RP-05 语义锚定：task.prompt.compile 为 READ_ONLY（同 Connection 单
snapshot，8 表零写入），故 op_class=READ_ONLY；route matrix 由
gen_route_matrix.py ROUTE_OVERRIDES（batch RP-08）发布，N=243。
"""

from typing import Any, Dict

from mcp.server.fastmcp import FastMCP

from ..daemon_client import route_rpc as _route

_RPC = "task.prompt.compile"


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def task_get_role_prompt(task_id: str) -> Dict[str, Any]:
        """编译指定任务的 Role Prompt Bundle（Role Prompt Compiler v1，只读薄透传）

        调用 daemon RPC task.prompt.compile，由 daemon 权威完成路由选择
        （§6 状态机）、role prompt 渲染（§8）、canonical 化与 hash（§9），
        返回可直接供 LLM 使用的 RolePromptBundle（§5.1 schema）。

        本工具是 spec §4.4 定义的 MCP HTTP 薄客户端：
        - 只接受 task_id 一个参数；不接受 role/format/workspace/credential/
          lease，也不复用 blind get_role_view；
        - 不做任何本地 workspace 推导（§4.3）；workspace authority 由
          daemon 侧 binding/capture 两跳自解析，guard mismatch 时 fail-closed
          返回 E_TASK_PROMPT_AUTHORITY_MISMATCH；
        - daemon 不可达时返回结构化 E_*_DAEMON_UNAVAILABLE，无本地 fallback。

        Args:
            task_id: 任务 ID（T-...，精确匹配，daemon 侧校验存在性与状态）

        Returns:
            RolePromptBundle dict（schema_version=role_prompt_bundle_v1，
            含 prompt/bundle hash、template 引用与 next_action 投影）
        """
        return _route("task.prompt.compile", {"task_id": task_id}, "READ_ONLY")
