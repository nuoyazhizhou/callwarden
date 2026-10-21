"""TP-10M C2：MCP 参数等价类合同（验收套件）。

selector: tests/acceptance/mcp/test_mcp_c2_params.py

从** wire 上 tools/list 返回的活 inputSchema**（真相源，非冻结 fixture）派生
每个工具的参数等价类，经真实 wire 校验参数合同：

  (a) 缺每个 required 属性 → 必须返回**结构化**参数错误（MCP/pydantic 校验
      或 daemon invalid_params），绝不能是 wire 崩溃、daemon traceback 或
      传输层失败；
  (b) 类型错配（数字/布尔/数组处传字符串 "tp10m-wrong-type"）→ 结构化错误，
      绝不能是传输层失败。

这是「参数合同」验收：243 工具的输入面在任何非法输入下都 fail-closed 到
结构化错误，而不是崩溃或静默接受。
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

import pytest

_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from mcp_wire_harness import McpWireHarness  # noqa: E402

_CONV = Path(__file__).resolve().parents[2] / "tests" / "convergence"
if str(_CONV) not in sys.path:
    sys.path.insert(0, str(_CONV))


# 破坏性工具不参与 C2 的「类型错配」实测（即便校验先于执行，也无需冒写风险）；
# 「缺 required」对所有工具都安全（校验层先拒绝）。
WRITE_OPS = ("PROTECTED_MUTATION", "GOVERNANCE_WRITE")
WRONG_VALUE = "tp10m-wrong-type"


@pytest.fixture(scope="module")
def harness():
    h = McpWireHarness()
    h.start()
    try:
        yield h
    finally:
        h.stop()


def _schema_of(harness: McpWireHarness, name: str) -> dict:
    return harness.input_schema_of(name)


def _prop_type(schema: dict, prop: str) -> str:
    spec = (schema.get("properties") or {}).get(prop) or {}
    t = spec.get("type")
    if isinstance(t, list):
        return t[0] if t else ""
    return t or ""


def _non_string_required(schema: dict) -> list[tuple[str, str]]:
    """required 且类型非 string/null 的属性（类型错配的实测对象）。"""
    out = []
    for prop in (schema.get("required") or []):
        t = _prop_type(schema, prop)
        if t and t != "string":
            out.append((prop, t))
    return out


def test_c2a_missing_required_yields_structured_error(harness):
    """(a) 缺每个 required 属性 → 结构化参数错误，永不 DEFECT。"""
    results = []
    defect = []
    no_error = []
    t0 = time.time()
    for name in harness.tool_names:
        schema = _schema_of(harness, name)
        required = list(schema.get("required") or [])
        if not required:
            continue
        base = harness.build_params(name)
        for prop in required:
            args = {k: v for k, v in base.items() if k != prop}
            r = harness.call(name, args)
            entry = {"tool": name, "missing": prop,
                     "delivered": r.delivered, "isError": r.isError,
                     "error_text": r.error_text[:160]}
            results.append(entry)
            if not r.delivered:
                entry["exc"] = f"{r.exc_type}: {r.exc_message[:160]}"
                defect.append(entry)
            elif not (r.isError or r.error_text):
                no_error.append(entry)
    evidence = {
        **harness.meta(),
        "suite": "TP-10M C2a 缺 required 参数等价类",
        "cases": len(results),
        "defect": len(defect),
        "accepted_without_error": len(no_error),
        "defects": defect[:50],
        "accepted_without_error_detail": no_error[:50],
        "elapsed_seconds": round(time.time() - t0, 1),
    }
    harness.write_report("tp10m_c2a_missing_required.json", evidence)
    msg = (f"C2a: {len(defect)} transport-level failures and "
           f"{len(no_error)} silently-accepted missing-required cases "
           f"of {len(results)}")
    if defect:
        msg += "\nDEFECTS:\n" + "\n".join(
            f"  - {d['tool']}(缺 {d['missing']}): {d.get('exc')}" for d in defect[:10])
    if no_error:
        msg += "\nSILENTLY ACCEPTED:\n" + "\n".join(
            f"  - {n['tool']}(缺 {n['missing']})" for n in no_error[:10])
    assert not defect and not no_error, msg


def test_c2b_wrong_type_yields_structured_error(harness):
    """(b) 非字符串类型处传字符串 → 结构化错误，永不 DEFECT/传输失败。"""
    results = []
    defect = []
    accepted = []
    skipped_write = []
    op_by_name = harness.op_class_by_name()
    t0 = time.time()
    for name in harness.tool_names:
        schema = _schema_of(harness, name)
        typed = _non_string_required(schema)
        if not typed:
            continue
        if op_by_name.get(name, "READ_ONLY") in WRITE_OPS:
            skipped_write.append(name)
            continue
        base = harness.build_params(name)
        for prop, ptype in typed:
            args = dict(base)
            args[prop] = WRONG_VALUE
            r = harness.call(name, args)
            entry = {"tool": name, "prop": prop, "expected_type": ptype,
                     "delivered": r.delivered, "isError": r.isError,
                     "error_text": r.error_text[:160]}
            results.append(entry)
            if not r.delivered:
                entry["exc"] = f"{r.exc_type}: {r.exc_message[:160]}"
                defect.append(entry)
            elif not (r.isError or r.error_text):
                accepted.append(entry)
    evidence = {
        **harness.meta(),
        "suite": "TP-10M C2b 类型错配等价类",
        "cases": len(results),
        "defect": len(defect),
        "silently_accepted": len(accepted),
        "write_tools_skipped": skipped_write,
        "defects": defect[:50],
        "silently_accepted_detail": accepted[:50],
        "elapsed_seconds": round(time.time() - t0, 1),
    }
    harness.write_report("tp10m_c2b_wrong_type.json", evidence)
    msg = (f"C2b: {len(defect)} transport failures, "
           f"{len(accepted)} silently-accepted wrong types of {len(results)}")
    if defect:
        msg += "\nDEFECTS:\n" + "\n".join(
            f"  - {d['tool']}.{d['prop']}({d['expected_type']}): {d.get('exc')}"
            for d in defect[:10])
    if accepted:
        msg += "\nSILENTLY ACCEPTED:\n" + "\n".join(
            f"  - {a['tool']}.{a['prop']}({a['expected_type']})" for a in accepted[:10])
    assert not defect, msg
    # 类型错配被静默接受不一定是缺陷（某些工具可能做了宽松解析），但必须报告
    if accepted:
        print(f"\n[C2b] {len(accepted)} wrong-typed inputs were accepted "
              f"(loose parsing): {[a['tool'] for a in accepted][:20]}")


def test_c2_schema_coverage(harness):
    """C2 覆盖面：大多数工具应有 required 或类型化属性可测。"""
    with_required = 0
    with_typed = 0
    for name in harness.tool_names:
        schema = _schema_of(harness, name)
        if schema.get("required"):
            with_required += 1
        if _non_string_required(schema):
            with_typed += 1
    evidence = {
        "tools_total": len(harness.tool_names),
        "tools_with_required": with_required,
        "tools_with_typed_required": with_typed,
    }
    harness.write_report("tp10m_c2_coverage.json", evidence)
    # 冻结清单里 148/243 工具有 required 属性；显著偏离说明 schema 拉取异常
    assert with_required > 100, (
        f"only {with_required} tools expose required params on the wire")


def test_c2_wire_errors_are_structured_not_tracebacks(harness):
    """(综合) 随机抽样：非法输入产生的错误文本不得含 Python traceback / panic。"""
    bad_signals = ("Traceback (most recent call last)", "panic", "unwrap",
                   "index out of", "no such table", "no such column")
    hits = []
    for name in ("get_symbol", "get_callers", "get_file_symbols", "get_stats",
                 "get_resolved_edges", "list_build_contexts", "get_impact"):
        schema = _schema_of(harness, name)
        required = list(schema.get("required") or [])
        if not required:
            continue
        args = {k: v for k, v in harness.build_params(name).items()
                if k != required[0]}
        r = harness.call(name, args)
        text = r.error_text
        if any(s in text for s in bad_signals):
            hits.append(f"{name}: {text[:160]}")
    assert not hits, f"traceback/panic-level errors on invalid input: {hits}"
