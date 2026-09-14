"""C-14/C-15/C-17 回归：daemon assignment create/revoke 契约一致性（assignment_id 单源）。

背景（本卡 `T-1789340885245-071cb9b4` 承接 PYT 回归卡 step#4 的 finding）：

  - **C-14**：`handle_assignment_create` 的 INSERT 列清单缺 `assignment_id`
    （`TEXT NOT NULL UNIQUE`，`db/schema.py:1629`）→ 恒
    `NOT NULL constraint failed: task_assignments.assignment_id`；且返回值取
    `conn.last_insert_rowid()`（整数），与契约面 `ASG-<uuid>` 不符。
  - **C-15**：`handle_assignment_revoke` 入参为 `task_id`/`role`，与 MCP tool schema /
    `docs/mcp_tools.md` / `docs/cli_reference.md` / CLI `_METHOD_MAP` /
    Python `db.revoke_assignment` 五面一致的 `assignment_id` 相悖；返回体亦不符。
  - **C-17**（本卡实测新发现）：`admin.assignment_*` 路由传入的 `workspace_id` 是
    daemon registry 代理 id（`daemon_workspaces.workspace_id`），与 task DB
    `workspaces.id` 不是同一命名空间；而 `task_assignments` 带
    `FOREIGN KEY (workspace_id) REFERENCES workspaces(id)`（`db/schema.py:1638`）
    → `create` 恒 FK 失败、`revoke` 恒误报 `assignment_not_found`。

本测试在**源码契约层**做确定性回归（与 `tests/test_c13_semgrep_dispatch_wiring.py`
同款「文本提取 + 断言」范式，不依赖 live daemon / 不直接写 SQLite），固化：

  1. `admin_handlers.rs` 的 create/revoke 实现形态（列清单 / 入参 / WHERE / 返回体）；
  2. C-17 的权威 workspace 取值必须走 `task_bound_workspace_id`，不得照搬路由代理 id；
  3. 五面契约单源一致（route_matrix / CLI `_METHOD_MAP` / `docs/mcp_tools.md` /
     `docs/cli_reference.md`）。

实跑往返回执（`create` → `ASG-<16hex>`；`show` → `create` → `revoke` 六步）
见 `deliverables/software-company/T-1789340885245-071cb9b4-evidence.md` §2.5/§2.6。
"""
from __future__ import annotations

import os
import re

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_DAEMON_DIR = os.path.join(_REPO_ROOT, "rust_ext", "src", "daemon")
_ADMIN_HANDLERS_RS = os.path.join(_DAEMON_DIR, "admin_handlers.rs")
_ROUTE_MATRIX_RS = os.path.join(_DAEMON_DIR, "route_matrix.rs")
_DISPATCH_RS = os.path.join(_DAEMON_DIR, "dispatch.rs")
_TASK_COLLAB_RS = os.path.join(_DAEMON_DIR, "task_collab.rs")
_TASK_COLLAB_SHARED_RS = os.path.join(_DAEMON_DIR, "task_collab_shared.rs")
_CLI_MAIN = os.path.join(_REPO_ROOT, "cli", "main.py")
_DOC_MCP_TOOLS = os.path.join(_REPO_ROOT, "docs", "mcp_tools.md")
_DOC_CLI_REFERENCE = os.path.join(_REPO_ROOT, "docs", "cli_reference.md")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as fh:
        return fh.read().replace("\r\n", "\n")


def _fn_body(source: str, fn_name: str) -> str:
    """截取 `(pub )?fn <fn_name>(...)` 起、到下一个顶层 `fn ` 之前的实现体。"""
    m = re.search(rf"^\s*(?:pub )?fn {re.escape(fn_name)}\(", source, re.M)
    assert m, f"{fn_name} 未在源码中找到"
    nxt = re.search(r"^\s*(?:pub )?fn ", source[m.end():], re.M)
    return source[m.start():] if nxt is None else source[m.start():m.end() + nxt.start()]


class TestAssignmentCreateContract:
    """C-14：创建必须写入并返回真实 `ASG-<16hex>`。"""

    def test_insert_declares_assignment_id_column(self):
        body = _fn_body(_read(_ADMIN_HANDLERS_RS), "handle_assignment_create")
        assert (
            "INSERT INTO task_assignments (workspace_id, assignment_id, task_id, "
            "role, agent_id, session_id, model_id, status, created_at)" in body
        ), "C-14：INSERT 列清单必须声明 assignment_id（TEXT NOT NULL UNIQUE，db/schema.py:1629）"

    def test_assignment_id_is_generated_asg_hex(self):
        src = _read(_ADMIN_HANDLERS_RS)
        gen = _fn_body(src, "gen_assignment_id")
        assert re.search(r'format!\("ASG-\{\}",\s*hex::encode\(entropy\)\)', gen), (
            "C-14：assignment_id 必须生成为 ASG-<16hex>（与 Python ASG-<uuid4.hex[:16]> 同型）"
        )
        assert "getrandom::fill" in gen, "C-14：熵源须为 OS CSPRNG（先例 task_loop/role_worker.rs）"
        create = _fn_body(src, "handle_assignment_create")
        assert "gen_assignment_id()" in create, "create 必须调用 gen_assignment_id()"

    def test_create_returns_assignment_id_string_not_rowid(self):
        body = _fn_body(_read(_ADMIN_HANDLERS_RS), "handle_assignment_create")
        assert "last_insert_rowid()" not in body, (
            "C-14：返回值不得再用 conn.last_insert_rowid()（整数），契约要求 ASG-<uuid> 字符串"
        )
        assert '"assignment_id": assignment_id' in body, "返回体必须含 assignment_id 字符串"

    def test_create_rejects_when_task_workspace_unbound(self):
        body = _fn_body(_read(_ADMIN_HANDLERS_RS), "handle_assignment_create")
        assert "task_bound_workspace_id(conn, task_id, None)?" in body, (
            "C-17：必须经权威 resolver 取 task 所属 workspace（无 binding 时 fail-closed）"
        )


class TestAssignmentRevokeContract:
    """C-15：撤销入参/WHERE/返回体必须与文档化契约逐字一致。"""

    def test_revoke_requires_assignment_id(self):
        body = _fn_body(_read(_ADMIN_HANDLERS_RS), "handle_assignment_revoke")
        assert 'require_str_param(params, "assignment_id")' in body, (
            'C-15：入参必须以 assignment_id 为准（对齐 docs/mcp_tools.md:1972 等五面）'
        )
        assert 'require_str_param(params, "task_id")' not in body, (
            "C-15：不得保留 task_id 入参（裁决 D2：不保留兼容回退，避免二义性误撤销）"
        )

    def test_revoke_update_filters_by_assignment_id(self):
        body = _fn_body(_read(_ADMIN_HANDLERS_RS), "handle_assignment_revoke")
        assert (
            "WHERE workspace_id = ?2 AND assignment_id = ?3 AND status = 'active'" in body
        ), "C-15：UPDATE 条件须为 workspace_id + assignment_id + status='active'"
        assert "status = 'revoked'" in body and "revoked_at" in body, (
            "C-15：append 语义——置 status=revoked + revoked_at，不删除记录"
        )

    def test_revoke_return_body_matches_doc(self):
        body = _fn_body(_read(_ADMIN_HANDLERS_RS), "handle_assignment_revoke")
        assert 'json!({ "ok": true, "assignment_id": assignment_id, "revoked_at": now })' in body, (
            "C-15：返回体须为 {ok, assignment_id, revoked_at}（docs/mcp_tools.md:1972）"
        )
        assert "assignment_not_found" in body, "C-15：无匹配行须返回 assignment_not_found"

    def test_revoke_lookup_then_authoritative_workspace(self):
        body = _fn_body(_read(_ADMIN_HANDLERS_RS), "handle_assignment_revoke")
        assert "SELECT task_id FROM task_assignments WHERE assignment_id = ?1" in body, (
            "C-17：assignment_id 全局 UNIQUE，先反查所属 task_id 再解析权威 workspace"
        )
        assert "task_bound_workspace_id(conn, &task_id, None)?" in body, (
            "C-17：撤销作用域必须用权威 workspaces.id，而非路由传入的 registry 代理 id"
        )


class TestC17WorkspaceAuthority:
    """C-17：admin 路由代理 id 与 task DB workspaces.id 的命名空间隔离。"""

    def test_route_supplied_workspace_id_is_intentionally_unused(self):
        src = _read(_ADMIN_HANDLERS_RS)
        for fn in ("handle_assignment_create", "handle_assignment_revoke"):
            body = _fn_body(src, fn)
            assert "let _ = workspace_id;" in body, (
                f"C-17：{fn} 必须显式声明忽略路由传入的 registry 代理 workspace_id 并重绑定"
            )

    def test_authoritative_resolver_exists_and_is_reexported(self):
        shared = _read(_TASK_COLLAB_SHARED_RS)
        assert "pub(crate) fn task_bound_workspace_id(" in shared, (
            "权威 resolver 必须位于 task_collab_shared.rs（读不可变 task_workspace_bindings）"
        )
        assert "task_workspace_bindings" in shared, "resolver 的取值来源须为 task_workspace_bindings"
        assert "pub(crate) use task_collab_shared::*;" in _read(_TASK_COLLAB_RS), (
            "task_collab 必须再导出 shared，使 crate::daemon::task_collab::task_bound_workspace_id 可达"
        )


class TestCrossFaceContract:
    """五面契约单源：route_matrix / dispatch / CLI `_METHOD_MAP` / docs。"""

    def test_route_matrix_rpc_methods(self):
        src = _read(_ROUTE_MATRIX_RS)
        for tool, rpc in (
            ("assignment_create", "admin.assignment_create"),
            ("assignment_revoke", "admin.assignment_revoke"),
        ):
            pattern = (
                r'ToolRoute \{ name: "'
                + tool
                + r'",[^}]*rpc_method: "'
                + re.escape(rpc)
                + r'"'
            )
            assert re.search(pattern, src, re.S), f"route_matrix 缺 {tool} -> {rpc}"

    def test_dispatch_protected_mutation_whitelist(self):
        src = _read(_DISPATCH_RS)
        assert '"admin.assignment_create"' in src and '"admin.assignment_revoke"' in src, (
            "dispatch.rs 的 PROTECTED_MUTATION_METHODS / native 面须登记两个 admin 方法"
        )

    def test_cli_method_map_matches_contract(self):
        src = _read(_CLI_MAIN)
        assert re.search(
            r'"create_assignment":\s*\(\s*"admin\.assignment_create",\s*"GOVERNANCE_WRITE",\s*'
            r'\("task_id",\s*"role",\s*"agent_id",\s*"session_id",\s*"model_id"\)\s*\)',
            src,
        ), "CLI _METHOD_MAP: create_assignment 契约不符"
        assert re.search(
            r'"revoke_assignment":\s*\(\s*"admin\.assignment_revoke",\s*"GOVERNANCE_WRITE",\s*'
            r'\("assignment_id",\)\s*\)',
            src,
        ), "CLI _METHOD_MAP: revoke_assignment 入参必须单源为 (assignment_id,)"

    def test_cli_revoke_positional_is_assignment_id(self):
        src = _read(_CLI_MAIN)
        assert 'revoke_p.add_argument("assignment_id"' in src, (
            "CLI `cw assignment revoke` 位置参数必须是 assignment_id"
        )
        assert "db.revoke_assignment(opts.assignment_id)" in src, (
            "CLI revoke 分支必须透传 assignment_id"
        )

    def test_docs_match_implementation(self):
        mcp = _read(_DOC_MCP_TOOLS)
        revoke_row = next(
            line for line in mcp.splitlines() if line.startswith("| `assignment_revoke` |")
        )
        assert "| `assignment_id` |" in revoke_row, (
            "docs/mcp_tools.md: assignment_revoke 参数面必须为 assignment_id"
        )
        assert "`{ok: True, assignment_id, revoked_at}`" in revoke_row, (
            "docs/mcp_tools.md: assignment_revoke 返回体必须为 {ok: True, assignment_id, revoked_at}"
        )
        create_row = next(
            line for line in mcp.splitlines() if line.startswith("| `assignment_create` |")
        )
        assert "`{ok: True, assignment_id, task_id, role, ...}`" in create_row, (
            "docs/mcp_tools.md: assignment_create 返回体必须含 assignment_id"
        )

        cli_ref = _read(_DOC_CLI_REFERENCE)
        assert "cw assignment revoke <assignment-id>" in cli_ref, (
            "docs/cli_reference.md: revoke 用法须为 `cw assignment revoke <assignment-id>`"
        )
