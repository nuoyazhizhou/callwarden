#!/usr/bin/env python3
"""TP-01 静态清单与 case/报告合同验证器。

只做静态校验：不运行产品、不构建、不联网、不调用 daemon 写面、不导入产品代码。
校验对象为本任务白名单内的规划产物（candidate_entries.json 与两个 JSON Schema）
以及 validation_examples.json 中的正负向样本。

模式：
  --self-check              自检：清点白名单文件、复算候选入口集合、元校验两个
                            schema，并跑通 validation_examples 的全部正负向样本。
  --candidate PATH          校验 candidate_entries.json（相对 cwd/绝对/相对本目录）。
  --examples PATH           只跑 validation_examples.json 的正负向样本。
  --schema case|report PATH 用对应 schema 校验单个 case 合同 / 运行报告实例。

退出码：0 = 全部通过；非零 = 发现问题（负向样本必须非零）。
任何 unknown 结果都阻断，绝不放行（fail-closed）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

INVENTORY_DIR = Path(__file__).resolve().parent
REPO_ROOT = INVENTORY_DIR.parents[3]

CANDIDATE_DEFAULT = INVENTORY_DIR / "candidate_entries.json"
EXAMPLES_DEFAULT = INVENTORY_DIR / "validation_examples.json"
CASE_SCHEMA = INVENTORY_DIR / "case_contract.schema.json"
REPORT_SCHEMA = INVENTORY_DIR / "run_report.schema.json"

# 审批基线（TP-01 注册时冻结的权威文档散列）
BASELINE_FILE = REPO_ROOT / "docs/testing/registration/source_baseline.json"

# 与审批 STATIC_AUDIT 一致的权威集合（候选数可复算的机器期望）
EXPECTED = {
    "cli_categories": 21,
    "cli_top_levels": 84,
    "cli_snapshot_leaves": 234,
    "cli_extracted": 233,
    "cli_unextracted": ["server"],
    "mcp_categories": 17,
    "mcp_tools": 243,
    "independent_flags": 8,
    "mcp_backends": {"rust_native": 200, "task_rpc": 43},
    "mcp_op_classes": {"GOVERNANCE_WRITE": 5, "PROTECTED_MUTATION": 77, "READ_ONLY": 161},
    "skip_ledger": {"sites_total": 468, "classic_skip_sites": 447,
                    "reason_literal_sites": 282, "reason_expression_sites": 167,
                    "reason_absent_sites": 19, "root_cause_reviewed_sites": 0},
}

STATUS_VOCABULARY = {
    "pending_contract", "params_snapshot_only", "blocked_schema_drift",
    "params_not_extracted", "deprecated_legacy", "not_applicable_pending",
    "contracted", "implemented", "passed", "blocked", "unknown_result",
}

OBLIGATION_KEYS = {"C1", "C2", "C3", "C4", "C5", "C6"}

CASE_ID_PATTERN = re.compile(r"^(CLI|MCP|CLI-FLAG):[^/]+/(C[1-6]|NEG|LIFECYCLE)/[A-Za-z0-9_.-]+$")


class Violation(Exception):
    """单条校验失败，带稳定错误码。"""

    def __init__(self, code: str, detail: str):
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


def strict_json_loads(text: str, label: str):
    """严格 JSON 解析：递归拒绝任意层的重复键。"""

    def no_duplicates(pairs):
        obj = {}
        for key, value in pairs:
            if key in obj:
                raise Violation("E_DUPLICATE_JSON_KEY", f"{label} 重复键 {key!r}")
            obj[key] = value
        return obj

    try:
        return json.loads(text, object_pairs_hook=no_duplicates)
    except json.JSONDecodeError as exc:
        raise Violation("E_JSON_PARSE", f"{label} 无法解析为 JSON：{exc}") from exc
    except Violation:
        raise
    except Exception as exc:  # noqa: BLE001 - 只报告，不吞真因
        raise Violation("E_JSON_PARSE", f"{label} 读取失败：{exc}") from exc


def load_json(path: Path, label: str):
    if not path.is_file():
        raise Violation("E_FILE_MISSING", f"{label} 不存在：{path}")
    return strict_json_loads(path.read_text(encoding="utf-8"), label)


def resolve_candidate_path(raw: str) -> Path:
    """cwd/绝对/相对本目录三种解析规则（CONTRACT 冻结）。"""
    p = Path(raw)
    if p.is_absolute():
        return p
    if p.exists():
        return p.resolve()
    alt = INVENTORY_DIR / raw
    if alt.exists():
        return alt.resolve()
    raise Violation("E_FILE_MISSING", f"无法解析 candidate 路径：{raw}（cwd/绝对/本目录均不存在）")


def require_keys(obj: dict, keys: set, label: str):
    missing = sorted(keys - set(obj))
    if missing:
        raise Violation("E_MISSING_FIELD", f"{label} 缺少必需字段：{missing}")


def check_sets(doc: dict, strict: bool) -> list[str]:
    """静态 source/matrix/category 集合一致性 + 候选数可复算。

    strict=True 时绑定审批 STATIC_AUDIT 的全局期望（真实产物用）；
    strict=False 只做内部自洽（正负向样本用，使注入的单一缺陷可被单独钉死）。
    """
    errs = []
    sets = doc.get("sets", {})
    require_keys(sets, {"candidate_entries_total", "entry_counts_by_surface",
                        "schema_matrix_set_consistent", "schema_name_drift"}, "sets")
    entries = doc.get("entries", [])
    # 候选数可复算：声明总数与分面板计数必须与真实 entries 一致（始终检查）
    from collections import Counter
    actual_by_surface = dict(Counter(e.get("surface", "?") for e in entries))
    declared = sets.get("entry_counts_by_surface", {})
    if sets.get("candidate_entries_total") != len(entries):
        errs.append("E_ENTRY_COUNT_MISMATCH: candidate_entries_total 与实际 entries 数不一致")
    if declared != actual_by_surface:
        errs.append(f"E_ENTRY_COUNT_MISMATCH: entry_counts_by_surface 期望 {actual_by_surface}，实际 {declared}")
    if sets.get("candidate_entries_total") != sum(declared.values()):
        errs.append("E_ENTRY_COUNT_MISMATCH: candidate_entries_total 与分面板计数之和不一致")
    if strict:
        for key, expected in EXPECTED.items():
            if key == "skip_ledger":
                continue
            actual = sets.get(key)
            if actual != expected:
                errs.append(f"E_SET_MISMATCH: sets.{key} 期望 {expected!r}，实际 {actual!r}")
    # schema 漂移必须显式 BLOCKED
    if sets.get("schema_matrix_set_consistent") is not False:
        errs.append("E_SCHEMA_DRIFT_HIDDEN: schema_matrix_set_consistent 在存在漂移时必须为 false")
    drift = sets.get("schema_name_drift", {})
    if not (drift.get("missing_current_tools") and drift.get("obsolete_schema_tools")):
        errs.append("E_SCHEMA_DRIFT_HIDDEN: schema_name_drift 必须同时记录缺失当前工具与历史额外项")
    skip = sets.get("skip_ledger")
    if not isinstance(skip, dict) or not skip:
        # 静态分母必须事前冻结，否则后续 skip 率与覆盖分母无依据
        errs.append("E_UNFROZEN_DENOMINATOR: sets.skip_ledger 缺失（静态分母未冻结）")
    elif strict:
        for key, expected in EXPECTED["skip_ledger"].items():
            if skip.get(key) != expected:
                errs.append(f"E_SET_MISMATCH: sets.skip_ledger.{key} 期望 {expected!r}，实际 {skip.get(key)!r}")
    return errs


def check_entries(doc: dict) -> list[str]:
    """逐入口校验：唯一 ID、状态词表、义务齐全、归属存在、参数义务不丢失。"""
    errs = []
    entries = doc.get("entries", [])
    if not entries:
        return ["E_MISSING_ENTRY: entries 为空（缺入口）"]
    seen = {}
    for e in entries:
        eid = e.get("entry_id")
        if not eid:
            errs.append("E_MISSING_ENTRY: 存在无 entry_id 的入口")
            continue
        if eid in seen:
            errs.append(f"E_DUPLICATE_ENTRY_ID: 重复入口 {eid}")
            continue
        seen[eid] = e
        status = e.get("status")
        if status not in STATUS_VOCABULARY:
            errs.append(f"E_UNKNOWN_STATUS: {eid} 状态 {status!r} 不在词表内")
        if not e.get("owner"):
            errs.append(f"E_MISSING_OWNER: {eid} 缺少责任归属 owner")
        obls = e.get("obligations", {})
        if set(obls) != OBLIGATION_KEYS:
            errs.append(f"E_OBLIGATION_LOST: {eid} 义务键不是 C1-C6 全集：{sorted(obls)}")
        if status == "unknown_result":
            errs.append(f"E_UNKNOWN_RELEASED: {eid} 处于 unknown_result 却被登记为可交付")
        if status == "not_applicable_pending" and not e.get("na_basis"):
            errs.append(f"E_ILLEGAL_NA: {eid} 标 N/A 但缺冻结依据 na_basis")
    # 参数义务不丢失：提取过的入口必须逐参数登记
    for e in entries:
        eid = e["entry_id"]
        snap = e.get("snapshot", {})
        if e.get("surface") == "cli" and snap.get("extracted"):
            pos = set(snap.get("positionals", []))
            opts = {o["opt"] for o in snap.get("options", [])}
            declared = {p["param"] for p in e.get("param_obligations", [])}
            lost = (pos | opts) - declared
            if lost:
                errs.append(f"E_LOST_PARAM_OBLIGATION: {eid} 未登记参数义务：{sorted(lost)}")
        if e.get("surface") == "mcp" and e.get("schema", {}).get("present"):
            req = set(e["schema"].get("required") or [])
            opt = set(e["schema"].get("optional") or [])
            declared = {p["param"] for p in e.get("param_obligations", [])}
            lost = (req | opt) - declared
            if lost:
                errs.append(f"E_LOST_PARAM_OBLIGATION: {eid} 未登记参数义务：{sorted(lost)}")
    return errs


def check_drift_blocked(doc: dict) -> list[str]:
    """schema 漂移受影响入口必须显式 blocked_schema_drift，禁止放行。"""
    errs = []
    drift = doc.get("sets", {}).get("schema_name_drift", {})
    blocked_ids = {e["entry_id"] for e in doc.get("entries", [])
                   if e.get("status") == "blocked_schema_drift"}
    for name in drift.get("missing_current_tools", []):
        eid = f"MCP:{name}"
        if eid not in blocked_ids:
            errs.append(f"E_SCHEMA_DRIFT_RELEASED: 漂移工具 {eid} 未标 blocked_schema_drift")
    for name in drift.get("obsolete_schema_tools", []):
        eid = f"MCP:{name}"
        if any(e["entry_id"] == eid for e in doc.get("entries", [])):
            errs.append(f"E_SCHEMA_DRIFT_RELEASED: 已废弃工具 {eid} 仍作为当前入口登记")
    return errs


def check_binding(doc: dict) -> list[str]:
    """绑定字段必须与审批基线一致（散列绑定，非 HEAD 独断）。"""
    errs = []
    binding = doc.get("binding", {})
    require_keys(binding, {"baseline_document_hashes", "inventory_inputs",
                           "static_audit_inputs_recomputed"}, "binding")
    for path, info in binding.get("baseline_document_hashes", {}).items():
        if not info.get("match"):
            errs.append(f"E_BASELINE_DRIFT: {path} 与审批基线散列不一致")
    recomputed = binding.get("static_audit_inputs_recomputed", {})
    if recomputed.get("inputs_matched") != recomputed.get("inputs_recorded"):
        errs.append("E_AUDIT_INPUTS_DRIFT: STATIC_AUDIT 输入散列复算不一致")
    if not binding.get("inventory_inputs"):
        errs.append("E_MISSING_FIELD: binding.inventory_inputs 为空")
    return errs


def check_cross_end(doc: dict) -> list[str]:
    """跨端映射：rpc_method 级槽位必须有 MCP 侧声明，CLI 侧不得伪造。"""
    errs = []
    mapping = doc.get("cross_end_mapping", [])
    if not mapping:
        return ["E_MISSING_FIELD: cross_end_mapping 为空（缺跨端映射）"]
    entry_ids = {e["entry_id"] for e in doc.get("entries", [])}
    for slot in mapping:
        if not slot.get("rpc_method"):
            errs.append("E_CROSS_END_EMPTY: 存在无 rpc_method 的跨端槽位")
        mcp_ids = slot.get("mcp_entry_ids") or []
        if not mcp_ids:
            errs.append(f"E_CROSS_END_EMPTY: rpc_method={slot.get('rpc_method')} 无 MCP 侧入口")
        for mid in mcp_ids:
            if mid not in entry_ids:
                errs.append(f"E_ORPHAN_SELECTOR: 跨端槽位引用孤立入口 {mid}")
        if slot.get("cli_entry_ids") is not None and not slot.get("cli_binding_status"):
            errs.append("E_CROSS_END_FABRICATED: rpc_method 槽位声称已有 CLI 绑定但无状态标记")
    return errs


def validate_candidate(doc: dict, strict_sets: bool = True) -> list[str]:
    """candidate_entries.json 的完整校验，返回错误码列表（空 = 通过）。"""
    if not isinstance(doc, dict):
        return ["E_JSON_PARSE: 根不是 JSON object"]
    errs = []
    try:
        require_keys(doc, {"schema_version", "task_id", "binding", "sets",
                           "obligation_model", "status_vocabulary",
                           "ownership_model", "cross_end_mapping", "gaps", "entries"}, "root")
    except Violation as exc:
        return [f"{exc.code}: {exc.detail}"]
    if doc.get("schema_version") != "candidate-entries/v1":
        errs.append(f"E_SCHEMA_VERSION: 期望 candidate-entries/v1，实际 {doc.get('schema_version')!r}")
    errs += check_binding(doc)
    errs += check_sets(doc, strict_sets)
    errs += check_entries(doc)
    errs += check_drift_blocked(doc)
    errs += check_cross_end(doc)
    if not doc.get("gaps"):
        errs.append("E_MISSING_FIELD: gaps 为空（缺漏/风险登记）")
    return errs


# ---------------------------------------------------------------- case 合同

def validate_case_contract(case: dict, known_entry_ids: set) -> list[str]:
    errs = []
    if not isinstance(case, dict):
        return ["E_CASE_SHAPE: case 不是 JSON object"]
    try:
        require_keys(case, {"case_id", "case_family", "surface", "profile", "requirement",
                            "risk", "preconditions", "repo_revision", "provenance",
                            "param_equivalence_classes", "act", "oracle",
                            "allowed_events", "forbidden_side_effects", "timeout",
                            "cleanup", "selector", "implementation_status",
                            "obligations", "evidence_ref"}, "case")
    except Violation as exc:
        return [f"{exc.code}: {exc.detail}"]
    cid = case["case_id"]
    if not CASE_ID_PATTERN.match(cid):
        errs.append(f"E_CASE_ID_FORMAT: {cid!r} 不符合 <entry_id>/<obligation>/<scenario>")
    if case["case_family"] not in known_entry_ids:
        errs.append(f"E_ORPHAN_SELECTOR: case {cid} 的 case_family 指向不存在入口 {case['case_family']!r}")
    sel = case.get("selector", {})
    if sel.get("resolves_to_entry") not in known_entry_ids:
        errs.append(f"E_ORPHAN_SELECTOR: case {cid} 的 selector 解析到孤立入口 {sel.get('resolves_to_entry')!r}")
    prof = case.get("profile", {})
    if prof.get("frozen") is False and case.get("implementation_status") == "passed":
        errs.append(f"E_PROFILE_UNFROZEN_PASSED: case {cid} profile 未冻结却声称 passed")
    prov = case.get("provenance", {})
    if prov.get("real") is False and case.get("implementation_status") == "passed":
        errs.append(f"E_PROVENANCE_FAKE: case {cid} provenance 非真实却声称 passed")
    if case.get("cleanup", {}).get("owned_only") is False:
        errs.append(f"E_CLEANUP_NOT_OWNED: case {cid} cleanup.owned_only=false")
    for pre in case.get("preconditions", []):
        if pre.get("establishment", {}).get("method") == "forbidden_sql":
            errs.append(f"E_FORBIDDEN_SQL: case {cid} 前置使用 SQL 补绿")
    if case.get("implementation_status") == "unknown_result":
        errs.append(f"E_UNKNOWN_RELEASED: case {cid} 处于 unknown_result，阻断")
    if case.get("oracle", {}).get("kind") == "differential" and not case.get("oracle", {}).get("normalization"):
        errs.append(f"E_DIFFERENTIAL_SELF: case {cid} 差分 oracle 缺少跨端规范化说明")
    return errs


# ---------------------------------------------------------------- 运行报告

REPORT_RESULT_KEYS = {"passed", "failed", "error", "setup_skip", "call_skip",
                      "teardown_skip", "skipped", "xfail", "xpass", "unknown",
                      "not_applicable", "missing", "not_triggered"}


def validate_run_report(report: dict, known_case_ids: set) -> list[str]:
    errs = []
    if not isinstance(report, dict):
        return ["E_REPORT_SHAPE: report 不是 JSON object"]
    try:
        require_keys(report, {"run_id", "run_manifest", "phases", "collection",
                              "results", "case_results", "denominators", "gate_status"}, "report")
    except Violation as exc:
        return [f"{exc.code}: {exc.detail}"]
    phases = report.get("phases", {})
    if set(phases) != {"setup", "call", "teardown"}:
        errs.append(f"E_REPORT_PHASES: 必须且仅含 setup/call/teardown，实际 {sorted(phases)}")
    results = report.get("results", {})
    if set(results) != REPORT_RESULT_KEYS:
        errs.append(f"E_REPORT_RESULTS: 结果键不是全集，缺 {sorted(REPORT_RESULT_KEYS - set(results))}")
    if results.get("unknown", 0) > 0:
        errs.append("E_UNKNOWN_RELEASED: 报告含 UNKNOWN 结果，阻断")
    coll = report.get("collection", {})
    if coll.get("collection_errors") or coll.get("truncated"):
        errs.append("E_COLLECTION_BLOCKED: collection 错误或被截断")
    den = report.get("denominators", {})
    if den.get("platform_frozen") is False:
        errs.append("E_UNFROZEN_DENOMINATOR: 平台分母未冻结")
    manifest = report.get("run_manifest", {})
    selectors = set(manifest.get("selectors", []))
    for cr in report.get("case_results", []):
        if cr.get("case_id") not in known_case_ids:
            errs.append(f"E_ORPHAN_SELECTOR: 报告引用未知 case_id {cr.get('case_id')!r}")
        if cr.get("selector") not in selectors:
            errs.append(f"E_ORPHAN_SELECTOR: 报告 case {cr.get('case_id')} 的 selector 未在 manifest 内")
        if cr.get("status") == "UNKNOWN":
            errs.append(f"E_UNKNOWN_RELEASED: case {cr.get('case_id')} 结果 UNKNOWN")
        if cr.get("status") == "NOT_APPLICABLE" and not cr.get("na_basis"):
            errs.append(f"E_ILLEGAL_NA: case {cr.get('case_id')} 的 N/A 缺冻结依据")
        if cr.get("status") == "NOT_TRIGGERED" and not cr.get("review_basis"):
            errs.append(f"E_ILLEGAL_NOT_TRIGGERED: case {cr.get('case_id')} 的 NOT_TRIGGERED 缺审查依据")
    if coll.get("collected", 0) == 0 and report.get("case_results"):
        errs.append("E_EMPTY_SUITE: collected=0 但存在 case 结果（空套件）")
    return errs


# ---------------------------------------------------------------- 样本

def run_examples(examples: dict) -> list[str]:
    """正负向样本：正向必须通过，负向必须以期望错误码失败。

    每项可给 ``doc``（已解析对象）或 ``doc_raw``（JSON 文本，用于构造重复键等
    解析期缺陷）。kind 默认 candidate。
    """
    errs = []

    def materialize(ex, label):
        if ex.get("doc_raw") is not None:
            try:
                return strict_json_loads(ex["doc_raw"], label), None
            except Violation as exc:
                return None, f"{exc.code}: {exc.detail}"
        if ex.get("doc_ref") is not None:
            ref = INVENTORY_DIR / ex["doc_ref"]
            try:
                return load_json(ref, f"{label} -> {ex['doc_ref']}"), None
            except Violation as exc:
                return None, f"{exc.code}: {exc.detail}"
        doc = ex.get("doc")
        if doc is None:
            return None, None
        return doc, None

    def dispatch(ex, doc, label):
        kind = ex.get("kind", "candidate")
        if kind == "candidate":
            return validate_candidate(doc, strict_sets=ex.get("strict_sets", True))
        if kind == "case":
            try:
                candidate = load_json(CANDIDATE_DEFAULT, "candidate_entries.json")
            except Violation as exc:
                return [f"{exc.code}: {exc.detail}"]
            known = {e["entry_id"] for e in candidate.get("entries", [])}
            return validate_case_contract(doc, known)
        return [f"E_EXAMPLES_SHAPE: {label} 未知 kind {kind!r}"]

    positives = examples.get("positive", [])
    negatives = examples.get("negative", [])
    if not positives or not negatives:
        return ["E_EXAMPLES_MISSING: 正向或负向样本为空"]
    for idx, ex in enumerate(positives):
        label = f"positive[{idx}]"
        doc, pre = materialize(ex, label)
        if pre:
            errs.append(f"E_POSITIVE_FAILED: {label}（{ex.get('name')}）应通过却报：{pre}")
            continue
        if doc is None:
            errs.append(f"E_EXAMPLES_SHAPE: {label} 缺 doc/doc_raw")
            continue
        sub = dispatch(ex, doc, label)
        if sub:
            errs.append(f"E_POSITIVE_FAILED: {label}（{ex.get('name')}）应通过却报：{sub}")
    for idx, ex in enumerate(negatives):
        label = f"negative[{idx}]"
        expected = ex.get("expected_code")
        if not expected:
            errs.append(f"E_EXAMPLES_SHAPE: {label} 缺 expected_code")
            continue
        doc, pre = materialize(ex, label)
        if pre is not None:
            sub = [pre]
        elif doc is None:
            errs.append(f"E_EXAMPLES_SHAPE: {label} 缺 doc/doc_raw")
            continue
        else:
            sub = dispatch(ex, doc, label)
        if not sub:
            errs.append(f"E_NEGATIVE_PASSED: {label}（{ex.get('name')}）期望 {expected} 却通过")
        elif not any(s.startswith(expected + ":") or s.startswith(expected + " ") for s in sub):
            errs.append(f"E_NEGATIVE_WRONG_CODE: {label}（{ex.get('name')}）期望 {expected}，实际 {sub}")
    return errs


# ---------------------------------------------------------------- schema 元校验

def meta_validate_schema(path: Path, label: str) -> list[str]:
    errs = []
    try:
        schema = load_json(path, label)
    except Violation as exc:
        return [f"{exc.code}: {exc.detail}"]
    try:
        from jsonschema import Draft7Validator
    except ImportError:  # 无 jsonschema 时降级为结构自检，不冒充通过
        if not isinstance(schema, dict) or "$schema" not in schema:
            errs.append(f"E_SCHEMA_SHAPE: {label} 不是带 $schema 的 JSON Schema")
        return errs or None  # type: ignore[return-value]
    validator = Draft7Validator(Draft7Validator.META_SCHEMA)
    schema_errs = sorted(validator.iter_errors(schema), key=lambda e: list(e.path))
    for e in schema_errs[:5]:
        errs.append(f"E_SCHEMA_META: {label} @{'/'.join(map(str, e.path)) or 'root'}: {e.message[:160]}")
    return errs


# ---------------------------------------------------------------- self-check

SELF_CHECK_FILES = [
    "candidate_entries.json",
    "case_contract.schema.json",
    "run_report.schema.json",
    "validate.py",
    "validation_examples.json",
    "CONTRACT.md",
]


def self_check() -> list[str]:
    errs = []
    # 白名单清点：CONTRACT.md 由 step 3 产出，自该步起纳入必清点项
    for name in SELF_CHECK_FILES:
        if not (INVENTORY_DIR / name).is_file():
            errs.append(f"E_FILE_MISSING: 白名单文件缺失：{name}")
    # 候选入口自洽
    try:
        doc = load_json(CANDIDATE_DEFAULT, "candidate_entries.json")
        errs += validate_candidate(doc)
    except Violation as exc:
        errs.append(f"{exc.code}: {exc.detail}")
    # schema 元校验
    errs += meta_validate_schema(CASE_SCHEMA, "case_contract.schema.json")
    errs += meta_validate_schema(REPORT_SCHEMA, "run_report.schema.json")
    # 正负向样本
    try:
        examples = load_json(EXAMPLES_DEFAULT, "validation_examples.json")
        errs += run_examples(examples)
    except Violation as exc:
        errs.append(f"{exc.code}: {exc.detail}")
    # 基线散列复核（审批输入仍可复算）
    try:
        baseline = load_json(BASELINE_FILE, "source_baseline.json")
        for path, expected in baseline.get("file_hashes", {}).items():
            actual = hashlib.sha256((REPO_ROOT / path).read_bytes()).hexdigest()
            if actual != expected:
                errs.append(f"E_BASELINE_DRIFT: {path} 与审批基线不一致")
    except (Violation, OSError) as exc:
        errs.append(f"E_BASELINE_UNREADABLE: {exc}")
    return errs


# ---------------------------------------------------------------- main

def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-check", action="store_true", help="清点白名单并自检全部静态合同")
    parser.add_argument("--candidate", metavar="PATH", help="校验 candidate_entries.json")
    parser.add_argument("--examples", metavar="PATH", help="只跑 validation_examples.json")
    parser.add_argument("--schema", choices=["case", "report"], help="用对应 schema 校验单个实例")
    parser.add_argument("--instance", metavar="PATH", help="--schema 时的实例文件")
    args = parser.parse_args(argv)

    if not any([args.self_check, args.candidate, args.examples, args.schema]):
        parser.error("需要至少一个模式：--self-check / --candidate / --examples / --schema")

    errs: list[str] = []
    try:
        if args.self_check:
            errs += self_check()
        if args.candidate:
            path = resolve_candidate_path(args.candidate)
            doc = load_json(path, path.name)
            errs += validate_candidate(doc)
            print(f"validated candidate: {path}")
        if args.examples:
            path = Path(args.examples)
            if not path.is_absolute():
                path = (INVENTORY_DIR / args.examples).resolve()
            examples = load_json(path, path.name)
            errs += run_examples(examples)
            print(f"ran examples: {path}")
        if args.schema:
            if not args.instance:
                parser.error("--schema 需要 --instance")
            inst = Path(args.instance)
            if not inst.is_absolute():
                inst = (INVENTORY_DIR / args.instance).resolve()
            obj = load_json(inst, inst.name)
            if args.schema == "case":
                candidate = load_json(CANDIDATE_DEFAULT, "candidate_entries.json")
                known = {e["entry_id"] for e in candidate.get("entries", [])}
                errs += validate_case_contract(obj, known)
            else:
                errs.append("E_UNSUPPORTED: 报告实例校验需要合同集合，请用 --self-check 覆盖")
            print(f"validated {args.schema} instance: {inst}")
    except Violation as exc:
        errs.append(f"{exc.code}: {exc.detail}")

    if errs:
        for e in errs:
            print(f"FAIL {e}", file=sys.stderr)
        print(f"RESULT: BLOCKED ({len(errs)} violation(s))", file=sys.stderr)
        return 1
    print("RESULT: PASS (static planning only; not a product verdict)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
