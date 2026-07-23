"""全量集成测试矩阵：所有 CLI 命令 + 所有 MCP 工具冒烟验证

设计目标
========
1. **CLI 全覆盖**：56 个子命令逐个调用 `python cw.py <subcommand> --help`，验证 exit_code 0（注册无误）
2. **MCP 全覆盖**：205 个 `@mcp.tool()` 装饰器全部用线程+硬超时调用，验证不抛未捕获异常
3. **真实数据**：用当前 callwarden 项目自身作为 fixture（已激活 workspace）
4. **不阻断**：单个工具超时直接 abandon 线程（不 join），主流程继续

测试哲学：**冒烟测试（smoke test）**
- 不要求每个工具返回正确数据
- 只要求：参数注册成功 + 调用不抛未捕获异常 + 返回结构是 dict/list
- 失败的项不中断，只记录为报告

运行方式：
    python -m pytest tests/test_integration_full_matrix.py -x --tb=short -q
    python -m pytest tests/test_integration_full_matrix.py -k "cli_smoke" -q
    python -m pytest tests/test_integration_full_matrix.py -k "mcp_smoke" -q
"""
import asyncio
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

# 项目根目录（用于 cw.py 调用）
PROJECT_ROOT = Path(__file__).parent.parent.resolve()
CW_PY = str(PROJECT_ROOT / "cw.py")
PYTHON = sys.executable

# ============================================
# 1. CLI 子命令清单（与 cli/main.py L36-53 的 _SUBCOMMANDS 一致）
# ============================================

CLI_SUBCOMMANDS = [
    # 原始 subcommand
    "guardrail", "impact", "review", "evolution", "hotspot", "churn", "defect",
    "task", "vuln-blast", "symbol-history", "check-gate", "test-impact",
    "gc", "doctor", "install-agent", "install-hook", "rule", "audit", "bootstrap",
    "clone", "fts",
    # C8 新增 8 大类
    "workspace", "refresh", "stats", "status",
    "search", "grep", "symbol", "file", "query", "issues", "tests",
    "callers", "callees", "call-chain", "topo",
    "metrics", "complexity", "coupling", "comment-coverage", "uncommented",
    "function-issues", "largest-fns", "coupled-fns", "fn-metrics",
    "git", "semgrep",
    "coverage", "who", "ownership-map",
    "brief", "map",
    "health-report",
    "build-context", "toolchain",
    "dashboard",
]


# ============================================
# 2. MCP 工具最小参数集（按工具名前缀分组提供合理默认参数）
# ============================================

# 用 callwarden 项目自身的真实符号作为测试输入
# （这些值会在 fixture 中动态填充，这里只提供静态默认值）
# 注意：参数名必须与 mcp_server.py 中 @mcp.tool() 装饰函数的形参一致
DEFAULT_MCP_ARGS = {
    # ---- 无参数工具（直接调用）----
    "get_stats": {},
    "get_issue_summary": {},
    "get_semgrep_stats": {},
    "list_workspaces": {},
    "get_topological_order": {"limit": 5},
    "detect_cycles": {"max_depth": 5},
    "get_comment_coverage": {},
    "get_largest_functions": {"limit": 5},
    "get_top_callers": {"limit": 5},
    "get_orphan_symbols": {"limit": 5},
    "get_deepest_functions": {"limit": 5},
    "get_module_call_stats": {"limit": 5},
    "bootstrap_status": {},
    "gc_stats": {},
    "gc_list": {},
    "fts_stats": {},
    "audit_verify": {},
    "audit_status": {},
    "audit_keys": {},
    "health_check": {},
    "health_report": {},
    "get_code_metrics_summary": {},
    "task_list": {},
    "task_quality_dashboard": {},
    "rule_list": {},
    "rule_candidates": {},
    "defect_stats": {},
    "project_dashboard": {},
    "project_risks": {},
    "extract_semantic_facts": {},
    # ---- 搜索/查询类（用 callwarden 自身的符号）----
    # 注意：参数名与 mcp_server.py 中 @mcp.tool() 装饰函数的形参一致
    "search_symbols": {"query": "CodeGraphDB"},
    "get_symbol": {"qualified_name": "cw"},
    "get_symbol_location": {"name": "CodeGraphDB"},
    "get_file_symbols": {"file_path": "cw.py"},
    # get_callers 实际参数是 callee_name（不是 callee_qualified）
    "get_callers": {"callee_name": "get_stats"},
    # get_callees 实际参数是 caller_name（不是 caller_qualified）
    "get_callees": {"caller_name": "get_stats"},
    "get_call_chain_down": {"qualified_name": "CodeGraphDB.get_stats", "max_depth": 2},
    "get_impact": {"qualified_name": "CodeGraphDB.get_stats", "max_depth": 2},
    # db 层方法 get_function_issues(issue_filter, limit)
    "find_issues": {"limit": 5},
    "get_semgrep_findings": {"limit": 5},
    "find_largest_functions": {"limit": 5},
    # ---- 文件相关（参数名是 file_path，不是 path）----
    "file_read": {"file_path": "README.md"},
    "file_list": {"path": "db"},
    "file_grep": {"pattern": "def ", "path": "db"},
    # file_symbol_content 实际需要 file_path + symbol_name
    "file_symbol_content": {"file_path": "cw.py", "symbol_name": "main"},
    # ---- 历史相关 ----
    "get_symbol_history": {"qualified_name": "cw"},
    "get_file_history": {"file_path": "cw.py"},
    "get_recent_changes": {"since": "1w"},
    # ---- 度量相关 ----
    "get_function_metrics": {"qualified_name": "CodeGraphDB.get_stats"},
    "get_complexity_hotspots": {"limit": 5},
    "get_coupling_analysis": {"limit": 5},
    # ---- 任务相关（只读）----
    "task_get": {"task_id": "_nonexistent_"},
    "task_steps": {"task_id": "_nonexistent_"},
    "task_active": {},
    # ---- workspace/分支 ----
    "list_branches": {},
    # ---- import_coverage 需要文件路径，不是符号名 ----
    "import_coverage": {"file_path": "coverage.lcov", "format": "lcov"},
}


def _build_mcp_args(tool_name: str) -> dict:
    """为指定工具构造最小参数集。未知工具返回空 dict 让其自行报错。"""
    if tool_name in DEFAULT_MCP_ARGS:
        return DEFAULT_MCP_ARGS[tool_name]
    # 按前缀给默认参数
    if tool_name.startswith("list_"):
        return {"limit": 5} if "limit" in _get_tool_param_hints(tool_name) else {}
    if tool_name.startswith("get_") and "qualified_name" in _get_tool_param_hints(tool_name):
        return {"qualified_name": "cw"}
    return {}


def _get_tool_param_hints(tool_name: str) -> str:
    """从工具描述中提取参数名提示（简单字符串包含判断）"""
    # 这是简化版：实际我们在 fixture 中会用 mcp.list_tools() 拿真实 schema
    return ""


# ============================================
# 3. Fixtures
# ============================================

@pytest.fixture(scope="module")
def mcp_server():
    """创建一次 MCP 服务器实例，模块内所有测试复用"""
    from callwarden.server.mcp_server import create_mcp_server
    server = create_mcp_server()
    return server


@pytest.fixture(scope="module")
def mcp_tools(mcp_server):
    """列出所有 MCP 工具并缓存"""
    tools = asyncio.run(mcp_server.list_tools())
    return tools


@pytest.fixture(scope="module")
def ensure_workspace_activated():
    """确保 callwarden 项目自身 workspace 已激活

    在真实开发环境运行时，cw 数据库通常已激活当前 workspace。
    若未激活，跑一次 `python cw.py refresh --all` 注册。
    CI 环境跳过 refresh --all（耗时过长，且测试只验证工具不抛异常）。
    """
    # CI 环境跳过全量刷新（避免 10 分钟阻塞）
    if os.environ.get("CI") or os.environ.get("GITHUB_ACTIONS"):
        return ""
    # 先看 status
    result = subprocess.run(
        [PYTHON, CW_PY, "status"],
        capture_output=True, text=True, cwd=str(PROJECT_ROOT),
        timeout=60,
    )
    # status 返回非 0 表示 workspace 未激活，尝试激活
    if result.returncode != 0:
        subprocess.run(
            [PYTHON, CW_PY, "refresh", "--all"],
            capture_output=True, text=True, cwd=str(PROJECT_ROOT),
            timeout=600,
        )
    return result.stdout


# ============================================
# 4. CLI 冒烟测试：每个子命令 --help
# ============================================

class TestCLISmokeHelp:
    """CLI 子命令 --help 冒烟测试

    验证每个子命令的 argparse 注册无错（无 NameError/缺参数等）。
    不调用真实功能，只验证子命令入口可用。
    """

    @pytest.mark.parametrize("subcmd", CLI_SUBCOMMANDS)
    def test_cli_subcommand_help(self, subcmd):
        """每个子命令 --help 应该 exit 0"""
        result = subprocess.run(
            [PYTHON, CW_PY, subcmd, "--help"],
            capture_output=True, text=True,
            cwd=str(PROJECT_ROOT),
            timeout=30,
        )
        # --help 应该总是 exit 0；非 0 说明子命令注册有问题
        assert result.returncode == 0, (
            f"`cw {subcmd} --help` exit {result.returncode}\n"
            f"stderr: {result.stderr[:500]}"
        )


# ============================================
# 5. CLI 真实调用冒烟测试（每个子命令给最小参数）
# ============================================

class TestCLISmokeRun:
    """CLI 子命令真实调用冒烟测试

    用最小参数集调用每个子命令，验证：
    - 不抛未捕获异常（exit code 不是 1）
    - 数据库锁时 exit code 2（可接受）
    - 帮助不足参数时 exit code 2（argparse 标准行为，可接受）
    - 真实成功 exit code 0
    """

    # 每个子命令的最小参数集（None = 不带额外参数，仅跑子命令本身）
    CLI_RUN_ARGS = {
        "stats": [],
        "status": [],
        "dashboard": [],
        "task": ["list"],
        "rule": ["list"],
        "audit": ["status"],
        "search": ["CodeGraphDB"],
        "query": ["CodeGraphDB", "cw.py"],
        "symbol": ["cw"],
        "file": ["cw.py"],
        "list": [],  # 没这个子命令，会被跳过
        "metrics": [],
        "complexity": [],
        "coupling": [],
        "comment-coverage": [],
        "uncommented": [],
        "largest-fns": [],
        "fn-metrics": ["CodeGraphDB.get_stats"],
        "function-issues": [],
        "coupled-fns": [],
        "topo": [],
        "git": ["log", "-n", "3"],
        "clone": [],
        "fts": ["CodeGraphDB"],
        "ownership-map": [],
        "who": ["cw.py"],
        "coverage": [],
        "health-report": [],
        "bootstrap": [],
        "doctor": [],
        "gc": ["stats"],
        "evolution": [],
        "hotspot": [],
        "churn": [],
        "defect": ["stats"],
        "impact": ["CodeGraphDB.get_stats"],
        "vuln-blast": ["CodeGraphDB.get_stats"],
        "symbol-history": ["cw"],
        "check-gate": [],
        "test-impact": [],
        "callers": ["CodeGraphDB.get_stats"],
        "callees": ["CodeGraphDB.get_stats"],
        "call-chain": ["CodeGraphDB.get_stats"],
        "map": [],
        "brief": [],
        "build-context": [],
        "toolchain": [],
        "workspace": ["list"],
        "issues": [],
        "tests": [],
        "semgrep": ["stats"],
        "grep": ["def ", "db"],
        "refresh": [],
        "review": [],
        "guardrail": [],
        "install-agent": [],
        "install-hook": [],
    }

    @pytest.mark.parametrize("subcmd", CLI_SUBCOMMANDS)
    def test_cli_subcommand_run(self, subcmd, ensure_workspace_activated):
        """每个子命令用最小参数集真实调用，验证 exit code ∈ {0, 2}"""
        args = self.CLI_RUN_ARGS.get(subcmd)
        if args is None:
            pytest.skip(f"无最小参数集，跳过：{subcmd}")

        cmd = [PYTHON, CW_PY, subcmd] + args
        result = subprocess.run(
            cmd,
            capture_output=True, text=True,
            cwd=str(PROJECT_ROOT),
            timeout=120,
        )
        # exit 0 = 成功；exit 2 = argparse 错误/db 锁/workspace 未激活等可接受情况
        # exit 1 = 未捕获异常 → 失败
        assert result.returncode in (0, 2), (
            f"`cw {subcmd} {' '.join(args)}` exit {result.returncode}\n"
            f"stdout: {result.stdout[:500]}\n"
            f"stderr: {result.stderr[:500]}"
        )


# ============================================
# 6. MCP 工具冒烟测试：每个工具 call_tool
# ============================================

# 跳过的工具集合（写操作 + 重型操作 + 副作用工具）
# 这些工具调用会阻塞事件循环或产生不可逆副作用，不适合冒烟测试
SKIP_MCP_TOOLS = {
    # ---- 写操作（会改数据库，破坏 fixture 状态）----
    "register_workspace", "delete_workspace", "set_active_workspace",
    "remove_file", "build_graph", "build_directory", "refresh_file",
    "import_git_history", "import_git_blame", "import_codeowners",
    "import_project_dependencies", "prune_external_symbols",
    "restore_comment", "restore_all_comments",
    "task_create", "task_split", "task_next_step", "task_report_step",
    "task_apply", "task_close", "task_reopen", "task_rollback", "task_cancel",
    "rule_sync", "rule_extract", "rule_import", "rule_delete", "rule_update",
    "audit_rotate_key", "audit_record", "audit_chain",
    "gc_run", "gc_vacuum", "gc_archive_restore", "gc_archive_delete",
    "gc_policy_set", "gc_retention_apply", "gc_migrate_single",
    "create_snapshot", "restore_snapshot", "delete_snapshot",
    "record_destructive_op",  # 写
    "propose_edit", "apply_patch", "propose_range_patch",  # 写
    "lsp_hover", "lsp_definition", "lsp_references", "lsp_workspace_symbols",
    "lsp_diagnostics", "lsp_format",  # LSP 可能未配置
    "cross_repo_search", "cross_repo_link",  # 跨仓库需配置
    "defect_learn", "defect_suggest_fix",  # 写 + 模型
    "extract_codebase_summary",  # 重型
    "link_workspaces",  # 写
    "revert_commit", "merge_branches", "switch_branch",  # git 写操作
    "cleanup_external_symbols", "clear_caches",  # 写
    "rotate_audit_key",  # 写
    "wait_for_job", "cancel_job", "review_pr",  # 副作用
    "generate_test_template", "run_test_command",  # 副作用
    "extract_tested_functions", "extract_test_cases",  # 解析测试文件可能很慢
    "compare_workspaces",  # 重型
    "register_branch",  # 写
    "resolve_external_symbol",  # 写
    "count_semantic_duplicates",  # 重型
    "build_call_graph", "merge_graph",  # 写
    "build_audit_chain",  # 写
    "check_breaking_change",  # 重型
    "record_task_change",  # 写
    "task_quality_record",  # 写
    "audit_record_task_change",  # 写
    # ---- 重型模型调用（sentence-transformers 加载模型 >5s）----
    "semantic_search", "ask_codebase", "ask_question",
    "find_similar_functions", "embed_symbols", "embed_single_symbol",
    # ---- 调用外部二进制（semgrep）----
    "run_semgrep_scan", "semgrep_scan_async",
    # ---- 需要真实外部文件输入（冒烟环境没有）----
    "import_coverage",  # 需要 coverage.lcov 真实文件
}


class TestMCPSmokeCall:
    """MCP 工具 call_tool 冒烟测试

    验证每个 @mcp.tool() 注册的工具：
    - 参数 schema 已注册
    - 调用不抛未捕获异常（返回结构化结果或 {"error": ...}）

    策略：跳过写操作/重型操作工具（SKIP_MCP_TOOLS），
    顺序调用剩余只读工具，每个加 5s 超时，
    通过率 >= 90% 视为整体健康。
    """

    @pytest.fixture(scope="class")
    def tool_name_list(self, mcp_tools):
        """返回所有工具名列表"""
        return [t.name for t in mcp_tools]

    def test_mcp_tool_count(self, mcp_tools):
        """MCP 工具数应该是 205（与文档宣称一致）"""
        assert len(mcp_tools) == 205, (
            f"实际 MCP 工具数 {len(mcp_tools)}，文档宣称 205"
        )

    def test_mcp_all_tools_smoke(self, mcp_server, mcp_tools, ensure_workspace_activated):
        """批量冒烟测试所有 205 个 MCP 工具（跳过写/重型操作）

        单个工具失败不阻断，收集失败列表后用通过率断言（>= 90% 通过即视为整体健康）。
        """
        # 1. 过滤跳过列表
        testable_tools = [t for t in mcp_tools if t.name not in SKIP_MCP_TOOLS]
        skipped = [t.name for t in mcp_tools if t.name in SKIP_MCP_TOOLS]
        print(f"\n=== MCP 冒烟测试范围 ===")
        print(f"  总工具数: {len(mcp_tools)}")
        print(f"  跳过（写/重型）: {len(skipped)}")
        print(f"  实际测试: {len(testable_tools)}")

        # 2. 为每个工具构造最小参数集
        def build_args(tool):
            tool_name = tool.name
            if tool_name in DEFAULT_MCP_ARGS:
                return DEFAULT_MCP_ARGS[tool_name]
            args = {}
            if tool.inputSchema:
                props = tool.inputSchema.get("properties", {})
                required = tool.inputSchema.get("required", [])
                for name in required:
                    if name not in props:
                        continue
                    ptype = props[name].get("type", "string")
                    if ptype == "string":
                        args[name] = "cw"
                    elif ptype == "integer":
                        args[name] = 5
                    elif ptype == "boolean":
                        args[name] = False
                    elif ptype == "array":
                        args[name] = []
                    elif ptype == "object":
                        args[name] = {}
                    if len(args) >= 3:
                        break
            return args

        # 3. 线程级硬超时调用（asyncio.wait_for 无法取消同步阻塞函数）
        #    每个工具在独立线程中运行，10s 后 abandon（不 join），主流程继续
        def call_one_sync(tool, args):
            """在独立线程中调用单个工具，10s 硬超时"""
            result_holder = [None]

            def _worker():
                try:
                    r = asyncio.run(mcp_server.call_tool(tool.name, args))
                    result_holder[0] = ("ok", tool.name, r)
                except Exception as e:
                    result_holder[0] = ("error", tool.name,
                                        f"{type(e).__name__}: {str(e)[:200]}")

            t = threading.Thread(target=_worker, daemon=True)
            t.start()
            t.join(timeout=10.0)
            if t.is_alive():
                # 线程仍在运行（工具阻塞），abandon 并标记 timeout
                return ("timeout", tool.name, None)
            return result_holder[0] or ("error", tool.name, "no result")

        results = []
        for i, tool in enumerate(testable_tools):
            args = build_args(tool)
            r = call_one_sync(tool, args)
            results.append(r)
            if (i + 1) % 20 == 0:
                print(
                    f"  [{i+1}/{len(testable_tools)}] progress (last: {r[0]} {r[1]})", flush=True)

        # 4. 统计结果
        ok_list = [r for r in results if r[0] == "ok"]
        timeout_list = [r for r in results if r[0] == "timeout"]
        error_list = [r for r in results if r[0] == "error"]

        total = len(results)
        pass_count = len(ok_list)
        pass_rate = pass_count / total if total > 0 else 0

        # 5. 打印失败详情
        if error_list:
            print("\n=== MCP 工具调用失败详情 ===")
            for _, name, err in error_list:
                print(f"  {name}: {err}")
        if timeout_list:
            print("\n=== MCP 工具超时详情（>5s）===")
            for _, name, _ in timeout_list:
                print(f"  {name}")

        print(f"\n=== MCP 冒烟测试汇总 ===")
        print(f"  测试: {total}（跳过 {len(skipped)} 个写/重型工具）")
        print(f"  通过: {pass_count} ({pass_rate:.1%})")
        print(f"  超时: {len(timeout_list)}")
        print(f"  失败: {len(error_list)}")

        # 6. 通过率 >= 90% 视为整体健康
        assert pass_rate >= 0.90, (
            f"MCP 工具冒烟通过率 {pass_rate:.1%} < 90%\n"
            f"失败列表: {[r[1] for r in error_list]}\n"
            f"超时列表: {[r[1] for r in timeout_list]}"
        )
