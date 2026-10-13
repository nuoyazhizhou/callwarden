"""TP-10C · C2 —— CLI 参数合同验收（参数等价类/候选/类型）。

selector: tests/acceptance/cli/test_cli_c2_params.py

按冻结清单的 params 合同，对每个 CLI 入口做三类**参数级负向**验证。全部
只触发 argparse 结构化拒绝（rc=2 + usage/error），命令**不会真正执行**，
故破坏性入口同样安全（argparse 在任何 handler 之前完成解析）：

  (a) 丢弃每个必填位置参数 → argparse 拒绝 rc=2 + usage，绝不 traceback；
  (b) 带 choices 的选项给非法值 → rc=2（invalid choice），绝不 traceback；
  (c) 数值型参数（int/float）给非数值 → rc=2（invalid 值），绝不 traceback。

断言：结构化错误且仅此（rc==2、含 usage:/error:、无 Python traceback）。
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
    _positional_value,
    build_argv,
    load_cli_entries,
)

ENTRIES = load_cli_entries()

_BAD_CHOICE = "__TP10C_NOT_A_VALID_CHOICE__"
_BAD_NUMBER = "not_a_number_tp10c"


def _is_required(pos) -> bool:
    return bool(pos.get("required")) or pos.get("nargs") in (None, "+", "A...")


@pytest.fixture(scope="module")
def env():
    e = CLIAcceptanceEnv()
    e.start_daemon()
    e.seed_workspace()
    try:
        yield e
    finally:
        e.stop_daemon()


# ---------------------------------------------------------------- (a)
def _argv_truncated(entry, ctx, drop_dest):
    """丢弃必填位置参数 dest：以 prog 为基，只提供 dest 之前的位置参数值。

    argparse 位置参数按声明顺序顶位填充，故丢弃中间参数时，必须同时截断其后的
    全部位置参数值，否则后继值会顶位补上而不触发「required」拒绝。
    """
    params = entry["params"]
    prog = params.get("prog") or ("cw " + entry["invocation"])
    base = prog.split()[1:]  # 去掉前导 "cw"
    exp = (params.get("expansion_basis") or {}).get("param")
    choice = (params.get("expansion_basis") or {}).get("choice")
    values = []
    for pos in params.get("positionals") or []:
        dest = pos.get("dest")
        if dest == drop_dest:
            break  # 截断：dest 及其后的位置参数一律不提供
        if dest == exp:
            values.append(str(choice))
            continue
        val = _positional_value(pos, ctx, entry)
        if val is not None:
            values.append(val)
    return base + values


def _drop_cases():
    """(entry, dest)：每个具备必填位置参数的入口展开为逐个丢弃用例。"""
    out = []
    for e in ENTRIES:
        params = e.get("params") or {}
        if not params.get("available"):
            continue
        for pos in params.get("positionals") or []:
            if not _is_required(pos):
                continue
            out.append((e, pos.get("dest")))
    return out


@pytest.mark.parametrize("entry,dest", _drop_cases(),
                         ids=lambda x: x if isinstance(x, str) else None)
def test_c2a_required_positional_missing(env, entry, dest):
    """丢弃必填位置参数 → argparse rc=2 + usage，绝不 traceback。"""
    argv = _argv_truncated(entry, env.ctx, dest)
    r = env.run_cli(argv, timeout=120.0)
    combined = r["stdout"] + r["stderr"]
    assert r["rc"] == 2, (
        f"【C2-a】{entry['entry_id']} 丢弃必填位置参数 {dest} 应被 argparse "
        f"拒绝（rc=2），实际 rc={r['rc']}\nargv={argv}\n"
        f"stdout={r['stdout'][:300]}\nstderr={r['stderr'][:300]}")
    assert "Traceback" not in combined, (
        f"【C2-a】{entry['entry_id']} 丢弃 {dest} 出现 Python traceback：\n{combined[:400]}")
    assert ("usage:" in combined or "error:" in combined.lower()
            or "必需" in combined or "缺少" in combined), (
        f"【C2-a】{entry['entry_id']} 丢弃 {dest} 未输出结构化 usage/error：\n"
        f"{combined[:300]}")


# ---------------------------------------------------------------- (b)
def _choice_option_cases():
    """(entry, flag)：每个带 choices 的取值选项展开为非法值用例。"""
    out = []
    for e in ENTRIES:
        params = e.get("params") or {}
        if not params.get("available"):
            continue
        for o in params.get("options") or []:
            if not o.get("choices") or o.get("nargs") == 0:
                continue
            for flag in (o.get("flags") or []):
                out.append((e, flag))
    return out


@pytest.mark.parametrize("entry,flag", _choice_option_cases(),
                         ids=lambda x: x if isinstance(x, str) else None)
def test_c2b_choice_option_invalid_value(env, entry, flag):
    """带 choices 的选项给非法值 → rc=2（invalid choice），绝不 traceback。"""
    argv = build_argv(entry, env.ctx, override_option={flag: _BAD_CHOICE})
    r = env.run_cli(argv, timeout=120.0)
    combined = r["stdout"] + r["stderr"]
    assert r["rc"] == 2, (
        f"【C2-b】{entry['entry_id']} {flag} 非法值应被 argparse 拒绝（rc=2），"
        f"实际 rc={r['rc']}\nargv={argv}\n{combined[:300]}")
    assert "Traceback" not in combined, (
        f"【C2-b】{entry['entry_id']} {flag} 非法值出现 traceback：\n{combined[:400]}")


# ---------------------------------------------------------------- (c)
def _numeric_cases():
    """(entry, flag_or_dest, kind)：数值型选项/位置参数展开为非数值用例。"""
    out = []
    for e in ENTRIES:
        params = e.get("params") or {}
        if not params.get("available"):
            continue
        for o in params.get("options") or []:
            if o.get("type") not in ("int", "float") or o.get("nargs") == 0:
                continue
            for flag in (o.get("flags") or []):
                out.append((e, flag, "option"))
        for pos in params.get("positionals") or []:
            if pos.get("type") in ("int", "float"):
                out.append((e, pos.get("dest"), "positional"))
    return out


@pytest.mark.parametrize("entry,target,kind", _numeric_cases(),
                         ids=lambda x: x if isinstance(x, str) else None)
def test_c2c_numeric_param_non_numeric(env, entry, target, kind):
    """数值型参数给非数值 → rc=2（invalid 值），绝不 traceback。"""
    if kind == "option":
        argv = build_argv(entry, env.ctx, override_option={target: _BAD_NUMBER})
    else:
        argv = build_argv(entry, env.ctx,
                          override_positional={target: _BAD_NUMBER})
    r = env.run_cli(argv, timeout=120.0)
    combined = r["stdout"] + r["stderr"]
    assert r["rc"] == 2, (
        f"【C2-c】{entry['entry_id']} 数值参数 {target} 给非数值应被拒绝（rc=2），"
        f"实际 rc={r['rc']}\nargv={argv}\n{combined[:300]}")
    assert "Traceback" not in combined, (
        f"【C2-c】{entry['entry_id']} 数值参数 {target} 非数值出现 traceback：\n"
        f"{combined[:400]}")
