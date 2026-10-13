"""TP-10C · C3 —— CLI 负向容错验收（不存在实体 → 业务错误，绝不崩溃）。

selector: tests/acceptance/cli/test_cli_c3_negatives.py

对只读命令注入**不存在的实体**（不存在的符号名 / 文件路径 / workspace id），
断言一律得到**结构化业务错误或明确空态**（not_found / empty），绝不出现
Python traceback、method_not_found 或其它未捕获崩溃。

全部用例均为只读入口（build-context show/list/edges、toolchain list-bound、
symbol/file 查询族），不触碰任何写面。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from cli_acceptance_harness import (  # noqa: E402
    CLIAcceptanceEnv,
    build_argv,
    load_cli_entries,
)
import param_provider as pp  # noqa: E402 （cli_acceptance_harness 已把 tests/convergence 加入 sys.path）

ENTRIES = load_cli_entries()
_BY_INV = {e["invocation"]: e for e in ENTRIES}

# 不存在实体（确定性、可复现，且不可能与种子符号/文件/ID 冲突）
_NO_SYMBOL = "No_Such_Symbol_TP10C::does_not_exist"
_NO_SYMBOL_SIMPLE = "no_such_function_tp10c"
_NO_FILE = "no/such/file_tp10c.py"
_NO_WS_ID = "9999999"  # 隔离 registry 的 workspace id 从 1 开始，9999999 必然不存在

_SYM_DESTS = {"qualified_name", "qname", "symbol", "symbol_name", "function_name",
              "name", "callee_name", "caller_name"}
_FILE_DESTS = {"file_path", "path", "file", "rel_path", "relative_path"}
_WS_DESTS = {"workspace_id", "id_or_name"}

_SYMBOL_READS = ["call-chain", "callees", "callers", "symbol", "fn-metrics",
                 "similar", "evolution", "issues", "test-impact", "tests",
                 "query"]
_FILE_READS = ["file", "who", "query"]
_WS_READS = ["build-context show", "build-context list", "build-context edges",
             "toolchain list-bound"]

# 明确的业务拒绝/空态信号（rc!=0 时必须命中其一）
_BUSINESS_SIGNALS = (
    "not_found", "not found", "不存在", "no such", "empty", "无", "未找到",
    "no rows", "no match", "没有找到", "无结果", "unknown", "invalid",
    "无法解析", "forbidden", "no test cases", "not registered",
)


def _neg_ctx():
    """C3 只需 dest 覆盖；其余位置参数取「不存在」占位 ctx（命令必然在实体处失败）。"""
    return pp.SeedContext(root=".", known_qualified_names=[_NO_SYMBOL_SIMPLE],
                          known_callee_names=[_NO_SYMBOL_SIMPLE],
                          known_file_paths=[_NO_FILE])


@pytest.fixture(scope="module")
def env():
    e = CLIAcceptanceEnv()
    e.start_daemon()
    e.seed_workspace()
    try:
        yield e
    finally:
        e.stop_daemon()


def _cases(invocations, bad_value, dest_filter):
    """(invocation, dest, argv)：把首个匹配 dest 的位置参数替换为不存在值。"""
    out = []
    for inv in invocations:
        e = _BY_INV.get(inv)
        if not e or not (e.get("params") or {}).get("available"):
            continue
        dest = None
        for pos in (e["params"].get("positionals") or []):
            if pos.get("dest") in dest_filter:
                dest = pos.get("dest")
                break
        if dest is None:
            continue
        argv = build_argv(e, _neg_ctx(), include_options=False,
                          override_positional={dest: bad_value})
        out.append((inv, dest, argv))
    return out


def _assert_business_not_crash(inv, argv, r):
    combined = r["stdout"] + r["stderr"]
    low = combined.lower()
    assert "Traceback" not in combined, (
        f"【C3】{inv} 不存在实体触发 Python traceback：\nargv={argv}\n{combined[:500]}")
    assert "method_not_found" not in low and "未知方法" not in combined, (
        f"【C3】{inv} 不存在实体触发 method_not_found（真实缺陷）：\n"
        f"argv={argv}\n{combined[:400]}")
    if r["rc"] == 0:
        return  # rc=0 + 无 traceback：明确空态（如 "No test cases found"）
    assert any(sig in low for sig in _BUSINESS_SIGNALS), (
        f"【C3】{inv} 不存在实体返回 rc={r['rc']} 但无结构化业务错误信号：\n"
        f"argv={argv}\n{combined[:400]}")


@pytest.mark.parametrize("inv,dest,argv",
                         _cases(_SYMBOL_READS, _NO_SYMBOL, _SYM_DESTS),
                         ids=lambda x: x if isinstance(x, str) else None)
def test_c3_nonexistent_symbol(env, inv, dest, argv):
    """不存在的符号名 → 业务错误/空态，绝不 traceback。"""
    _assert_business_not_crash(inv, argv, env.run_cli(argv, timeout=120.0))


@pytest.mark.parametrize("inv,dest,argv",
                         _cases(_FILE_READS, _NO_FILE, _FILE_DESTS),
                         ids=lambda x: x if isinstance(x, str) else None)
def test_c3_nonexistent_file(env, inv, dest, argv):
    """不存在的文件路径 → 业务错误/空态，绝不 traceback。"""
    _assert_business_not_crash(inv, argv, env.run_cli(argv, timeout=120.0))


@pytest.mark.parametrize("inv,dest,argv",
                         _cases(_WS_READS, _NO_WS_ID, _WS_DESTS),
                         ids=lambda x: x if isinstance(x, str) else None)
def test_c3_nonexistent_workspace(env, inv, dest, argv):
    """不存在的 workspace id → 业务错误/空态，绝不 traceback。"""
    _assert_business_not_crash(inv, argv, env.run_cli(argv, timeout=120.0))
