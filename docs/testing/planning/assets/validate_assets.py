#!/usr/bin/env python3
"""TP-02 profile / 离线语料 / 独立 oracle / 性能预算的资产验证器。

只做静态校验与磁盘存在性/散列核验：不运行产品、不构建、不联网、
不写源仓数据、不复制 corpus。

模式：
  --self-check         清点白名单文件、校验四个产物 JSON 的结构与一致性、
                       核验可得离线资产的磁盘存在性与散列，并跑内联正负向样本。
  --verify-manifests   在 --self-check 基础上额外核验 manifest：
                       语料 sha256、oracle subject sha256、source revision 与磁盘一致。

退出码：0 = 全部通过；非零 = 发现问题（负向样本必须非零）。
任何 unknown 结果都阻断，绝不放行（fail-closed）。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from pathlib import Path

INVENTORY_DIR = Path(__file__).resolve().parent
# assets → planning → testing → docs → callwarden（仓库根）
REPO_ROOT = INVENTORY_DIR.parents[3]

PROFILES = INVENTORY_DIR / "profiles.json"
CORPORA = INVENTORY_DIR / "corpora.json"
ORACLE = INVENTORY_DIR / "oracle_catalog.json"
BUDGETS = INVENTORY_DIR / "budgets.json"

# 白名单（ASSET_CONTRACT.md 由 step 3 产出，step 4 起纳入必清点）
SELF_CHECK_FILES = [
    "profiles.json",
    "corpora.json",
    "oracle_catalog.json",
    "budgets.json",
    "validate_assets.py",
    "ASSET_CONTRACT.md",
]

# 语言矩阵：15 个 Rust tree-sitter grammar + 独立 C 解析器（C 两种路径皆可）。
# 来源：rust_ext/Cargo.toml 依赖块 + TESTING_PLAN §4（JS 不重复分列 TS）
LANGUAGE_ALLOWLIST = {
    "c", "cpp", "python", "rust", "go", "java", "typescript",
    "ruby", "php", "scala", "c_sharp", "kotlin", "swift", "elixir", "hcl",
}
EXPECTED_GRAMMAR_COUNT = 15
EXPECTED_LANGUAGE_TOTAL = 16

CORPUS_STATUS_VOCAB = {
    "available_offline", "not_applicable_pending", "blocked_pending_decision",
}
PROFILE_COMMITMENT_VOCAB = {"UNDECIDED", "committed", "rejected"}
PROFILE_STATUS_VOCAB = {"candidate_from_ci", "frozen_from_measurement", "rejected"}
# profile 冻结时禁止出现的前置占位符：冻结 = 实测背书，占位符必须先回填
UNVERIFIED_PLACEHOLDER = "UNVERIFIED"
DECISION_STATUS_VOCAB = {"DECISION_REQUIRED", "resolved", "rejected"}
ORACLE_STATUS_VOCAB = {
    "golden_from_source_read_pending_human_review",
    "golden_human_verified",
    "blocked_pending_decision",
}
# 本卡允许出现的 oracle 复核状态：人工复核未完成前只能是 pending
ORACLE_ALLOWED_THIS_ROUND = {"golden_from_source_read_pending_human_review",
                             "blocked_pending_decision"}

# 语料 manifest 必填结构字段（commit 字段允许 source_revision / revision 两种命名：
# R0 单文件语料用 source_revision，R1 整仓 dirty 语料用 revision + dirty 归属）
CORPUS_MANIFEST_FIELDS = {
    "corpus_id", "tier", "status", "path", "language",
    "license_or_authorization", "copy_method",
    "oracle_source", "applicable_cases",
}
REVISION_FIELDS = ("source_revision", "revision")
# corrections_frozen 每条固化必须携带的三元组
CORRECTION_KEYS = ("historical_error", "correction", "why_it_matters")


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
    except Exception as exc:  # noqa: BLE001
        raise Violation("E_JSON_PARSE", f"{label} 读取失败：{exc}") from exc


def load_json(path: Path, label: str):
    if not path.is_file():
        raise Violation("E_FILE_MISSING", f"{label} 不存在：{path}")
    return strict_json_loads(path.read_text(encoding="utf-8"), label)


def require_keys(obj: dict, keys: set, label: str):
    missing = sorted(keys - set(obj))
    if missing:
        raise Violation("E_MISSING_FIELD", f"{label} 缺少必需字段：{missing}")


def sha256_of(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git_head() -> str:
    r = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT,
                       capture_output=True, text=True)
    return r.stdout.strip()


def git_dirty_count() -> int:
    """R1 整仓语料声称的 dirty 项数必须与真实工作树一致（行为级核验）。"""
    r = subprocess.run(["git", "status", "--porcelain"], cwd=REPO_ROOT,
                       capture_output=True, text=True)
    return len([ln for ln in r.stdout.splitlines() if ln.strip()])


def repo_path(rel: str) -> Path:
    p = Path(rel)
    if p.is_absolute():
        return p
    return (REPO_ROOT / rel).resolve()


# ---------------------------------------------------------------- profiles
def check_profiles(doc: dict) -> list[str]:
    errs = []
    try:
        require_keys(doc, {"schema_version", "candidate_profiles", "candidate_count",
                           "committed_count", "decision_points"}, "profiles")
    except Violation as exc:
        return [f"{exc.code}: {exc.detail}"]

    cands = doc.get("candidate_profiles", [])
    if doc.get("candidate_count") != len(cands):
        errs.append(f"E_PROFILE_COUNT_MISMATCH: candidate_count={doc.get('candidate_count')} "
                    f"与实际 candidate_profiles 数 {len(cands)} 不一致")
    committed = [c for c in cands if c.get("commitment") == "committed"]
    if doc.get("committed_count") != len(committed):
        errs.append(f"E_PROFILE_COUNT_MISMATCH: committed_count={doc.get('committed_count')} "
                    f"与实际已承诺数 {len(committed)} 不一致")

    for c in cands:
        cid = c.get("profile_id", "<no-id>")
        try:
            require_keys(c, {"profile_id", "os", "arch", "python_version",
                             "frozen", "commitment"}, f"profile {cid}")
        except Violation as exc:
            errs.append(f"{exc.code}: {exc.detail}")
            continue
        if c.get("commitment") not in PROFILE_COMMITMENT_VOCAB:
            errs.append(f"E_PROFILE_COMMITMENT_VOCAB: {cid} commitment={c.get('commitment')!r} 不在词表内")
        if c.get("status") not in PROFILE_STATUS_VOCAB:
            errs.append(f"E_PROFILE_STATUS_VOCAB: {cid} status={c.get('status')!r} 不在词表内")
        if not c.get("evidence"):
            errs.append(f"E_PROFILE_MISSING_EVIDENCE: {cid} 缺 CI 证据行 evidence")
        # 偷换平台：frozen=true 必须有冻结证据；未冻结不得声称已承诺
        if c.get("frozen") is True and not c.get("frozen_ref"):
            errs.append(f"E_PROFILE_FROZEN_WITHOUT_REF: {cid} frozen=true 但缺 frozen_ref")
        if c.get("frozen") is not True and c.get("commitment") == "committed":
            errs.append(f"E_PROFILE_COMMIT_UNFROZEN: {cid} 未冻结却标 committed")
        # 平台冒充防护：声明的 os / python_version 必须在 CI 证据行中真实出现，
        # 否则 evidence 与声明脱节，平台不可追溯
        ev = " ".join(c.get("evidence", []))
        for tok in (c.get("os"), c.get("python_version")):
            if tok and tok not in ev:
                errs.append(f"E_PROFILE_PLATFORM_SWAP: {cid} 声明 {tok!r} 但 evidence 无此平台证据")
        # 冻结即实测背书：BUILD_ENV §4 前置 manifest 必须齐全且不得残留 UNVERIFIED 占位符
        if c.get("frozen") is True:
            declared = set(doc.get("profile_contract", {}).get("required_fields", []))
            if declared:
                missing = sorted(declared - set(c))
                if missing:
                    errs.append(f"E_PROFILE_CONTRACT_INCOMPLETE: {cid} 冻结但缺前置 manifest 字段 {missing}")
            for k, v in c.items():
                if isinstance(v, str) and UNVERIFIED_PLACEHOLDER in v:
                    errs.append(f"E_PROFILE_FROZEN_WITH_UNVERIFIED: {cid} 冻结但字段 {k} 残留 {UNVERIFIED_PLACEHOLDER}")
        if c.get("rust_toolchain_locked") is not True and c.get("commitment") == "committed":
            errs.append(f"E_PROFILE_TOOLCHAIN_UNLOCKED: {cid} 已承诺但 Rust toolchain 未锁定")

    for d in doc.get("decision_points", []):
        did = d.get("id", "<no-id>")
        if d.get("status") not in DECISION_STATUS_VOCAB:
            errs.append(f"E_DECISION_STATUS_VOCAB: {did} status={d.get('status')!r} 不在词表内")
        if not d.get("question") or not d.get("owner"):
            errs.append(f"E_DECISION_INCOMPLETE: {did} 缺 question 或 owner")
    return errs


# ---------------------------------------------------------------- corpora
def check_corpora(doc: dict, verify_disk: bool) -> list[str]:
    errs = []
    try:
        require_keys(doc, {"schema_version", "manifest_contract", "tier_definitions",
                           "language_matrix", "corpora", "missing"}, "corpora")
    except Violation as exc:
        return [f"{exc.code}: {exc.detail}"]

    # 语言矩阵一致性
    lm = doc.get("language_matrix", {})
    grams = lm.get("rust_grammars", [])
    langs = {g.get("language") for g in grams}
    bad_langs = langs - LANGUAGE_ALLOWLIST
    if bad_langs:
        errs.append(f"E_LANGUAGE_UNKNOWN: language_matrix 含未知语言 {sorted(bad_langs)}")
    if lm.get("rust_grammar_count") != len(grams) or len(grams) != EXPECTED_GRAMMAR_COUNT:
        errs.append(f"E_LANGUAGE_COUNT_MISMATCH: rust_grammar_count={lm.get('rust_grammar_count')} "
                    f"实际 {len(grams)}，期望 {EXPECTED_GRAMMAR_COUNT}")
    if lm.get("declared_total") != EXPECTED_LANGUAGE_TOTAL:
        errs.append(f"E_LANGUAGE_COUNT_MISMATCH: declared_total={lm.get('declared_total')} "
                    f"期望 {EXPECTED_LANGUAGE_TOTAL}")

    # 四层定义齐全
    for tier in ("R0", "R1", "R2", "R3"):
        if tier not in doc.get("tier_definitions", {}):
            errs.append(f"E_MISSING_TIER_DECLARED: tier_definitions 缺 {tier}")

    # manifest 字段规则必须存在
    mc = doc.get("manifest_contract", {})
    if not mc.get("required_fields") or not mc.get("rules"):
        errs.append("E_MANIFEST_FIELD_MISSING: manifest_contract 缺 required_fields 或 rules")

    # 逐语料
    for c in doc.get("corpora", []):
        cid = c.get("corpus_id", "<no-id>")
        missing = sorted(CORPUS_MANIFEST_FIELDS - set(c))
        if not any(k in c for k in REVISION_FIELDS):
            missing.append("source_revision|revision（二者之一）")
        if missing:
            errs.append(f"E_MANIFEST_FIELD_MISSING: {cid} 缺 manifest 字段 {missing}")
            continue
        if c.get("tier") not in ("R0", "R1", "R2", "R3"):
            errs.append(f"E_CORPUS_TIER_UNKNOWN: {cid} tier={c.get('tier')!r} 不在 R0-R3")
        if c.get("status") not in CORPUS_STATUS_VOCAB:
            errs.append(f"E_CORPUS_STATUS_VOCAB: {cid} status={c.get('status')!r} 不在词表内")
        # 语言校验（支持 rust+python 复合）
        for lang in str(c.get("language", "")).split("+"):
            if lang and lang not in LANGUAGE_ALLOWLIST:
                errs.append(f"E_LANGUAGE_UNKNOWN: {cid} 语言 {lang!r} 不在语言矩阵内")
        # 未批准 N/A
        if c.get("status") == "not_applicable_pending" and not c.get("na_basis"):
            errs.append(f"E_ILLEGAL_NA: {cid} 标 not_applicable_pending 但缺批准依据 na_basis")
        # 可得离线资产必须真实存在于磁盘且与声明一致
        if c.get("status") == "available_offline" and verify_disk:
            p = repo_path(c.get("path", ""))
            if not p.exists():
                errs.append(f"E_CORPUS_PATH_MISSING: {cid} 仓库/路径不存在：{c.get('path')}")
                continue
            rev = c.get("source_revision") or c.get("revision")
            if p.is_file():
                # 文件级语料（R0）：散列必须与磁盘一致，commit 必须是当前 HEAD
                if not c.get("sha256"):
                    errs.append(f"E_CORPUS_HASH_MISSING: {cid} 文件级语料缺 sha256")
                elif sha256_of(p) != c["sha256"]:
                    errs.append(f"E_CORPUS_HASH_MISMATCH: {cid} 散列与磁盘不一致")
                if rev and rev != git_head():
                    errs.append(f"E_CORPUS_REVISION_MISMATCH: {cid} revision={rev[:12]}... "
                                f"与当前 HEAD {git_head()[:12]}... 不一致")
            else:
                # 整仓级语料（R1，dirty 工作树）：单文件散列无意义，声明了反而误导
                if c.get("sha256"):
                    errs.append(f"E_CORPUS_DIR_HASH_FORBIDDEN: {cid} 整仓 dirty 语料声明 sha256，"
                                f"散列对 dirty 工作树不可复现")
                if not rev:
                    errs.append(f"E_MANIFEST_FIELD_MISSING: {cid} 整仓语料缺 revision")
                elif rev != git_head():
                    errs.append(f"E_CORPUS_REVISION_MISMATCH: {cid} revision={rev[:12]}... "
                                f"与当前 HEAD {git_head()[:12]}... 不一致")
                if c.get("dirty") is None:
                    errs.append(f"E_CORPUS_DIRTY_UNDECLARED: {cid} 整仓语料必须显式声明 dirty 归属")
                # dirty 项数必须与真实工作树一致（行为级，防止静态声称与实际漂移）
                if c.get("dirty_entries") is not None and c["dirty_entries"] != git_dirty_count():
                    errs.append(f"E_CORPUS_DIRTY_COUNT_DRIFT: {cid} 声称 dirty_entries="
                                f"{c['dirty_entries']} 实际 {git_dirty_count()}")
        if c.get("frozen") is True and not c.get("frozen_ref"):
            errs.append(f"E_CORPUS_FROZEN_WITHOUT_REF: {cid} frozen=true 但缺 frozen_ref")

    # 缺失层必须显式声明，禁止填假路径
    declared_missing = {m.get("tier") for m in doc.get("missing", [])}
    for tier in ("R2", "R3"):
        present = any(c.get("tier") == tier for c in doc.get("corpora", []))
        if present and tier in declared_missing:
            errs.append(f"E_FAKE_PATH_FORBIDDEN: {tier} 既在 missing 又在 corpora 中，名单自相矛盾")
    for m in doc.get("missing", []):
        if m.get("status") != "DECISION_REQUIRED":
            errs.append(f"E_MISSING_TIER_STATUS: {m.get('tier')} missing 项必须是 DECISION_REQUIRED")
        if not m.get("required_from") or not m.get("forbidden"):
            errs.append(f"E_MISSING_TIER_INCOMPLETE: {m.get('tier')} 缺 required_from 或 forbidden")

    # 混淆防护：同名诱饵不得与真语料散列相同
    real = {c.get("corpus_id"): c.get("sha256") for c in doc.get("corpora", [])}
    for g in doc.get("confusion_guard", []):
        decoy = g.get("decoy_sha256")
        if decoy and decoy in real.values():
            errs.append(f"E_DECOY_COLLISION: confusion_guard 诱饵散列与真语料相同：{g.get('risk')}")
    return errs


# ---------------------------------------------------------------- oracle
def check_oracle(doc: dict, verify_disk: bool) -> list[str]:
    errs = []
    try:
        require_keys(doc, {"schema_version", "entries", "entry_count",
                           "oracle_principles", "human_review"}, "oracle")
    except Violation as exc:
        return [f"{exc.code}: {exc.detail}"]

    entries = doc.get("entries", [])
    if doc.get("entry_count") != len(entries):
        errs.append(f"E_ORACLE_COUNT_MISMATCH: entry_count={doc.get('entry_count')} "
                    f"与实际 {len(entries)} 不一致")

    for e in entries:
        oid = e.get("oracle_id", "<no-id>")
        try:
            require_keys(e, {"oracle_id", "corpus_ref", "language", "kind",
                             "independent", "human_verifiable", "status",
                             "subject_path", "subject_sha256"}, f"oracle {oid}")
        except Violation as exc:
            errs.append(f"{exc.code}: {exc.detail}")
            continue
        if e.get("status") not in ORACLE_STATUS_VOCAB:
            errs.append(f"E_ORACLE_STATUS_VOCAB: {oid} status={e.get('status')!r} 不在词表内")
        # 本卡人工复核未完成，禁止标已复核
        if e.get("status") not in ORACLE_ALLOWED_THIS_ROUND:
            errs.append(f"E_ORACLE_HUMAN_REVIEW_VIOLATED: {oid} 在本卡未完成人工复核前标"
                        f" {e.get('status')!r}")
        if e.get("independent") is not True:
            errs.append(f"E_ORACLE_NOT_INDEPENDENT: {oid} independent 必须为 true")
        if e.get("human_verifiable") is not True:
            errs.append(f"E_ORACLE_NOT_HUMAN_VERIFIABLE: {oid} human_verifiable 必须为 true")
        for lang in str(e.get("language", "")).split("+"):
            if lang and lang not in LANGUAGE_ALLOWLIST:
                errs.append(f"E_LANGUAGE_UNKNOWN: {oid} 语言 {lang!r} 不在语言矩阵内")
        if not e.get("assertions"):
            errs.append(f"E_ORACLE_NO_ASSERTIONS: {oid} 缺 assertions")
        if verify_disk:
            p = repo_path(e.get("subject_path", ""))
            if not p.is_file():
                errs.append(f"E_ORACLE_SUBJECT_MISSING: {oid} subject 文件不存在：{e.get('subject_path')}")
            elif sha256_of(p) != e.get("subject_sha256"):
                errs.append(f"E_ORACLE_SUBJECT_HASH_MISMATCH: {oid} subject 散列与磁盘不一致")
        # 来源读证必须显式可追溯：要么固化历史纠错（三元组齐全），要么 notes 说明无需纠错。
        # 沉默省略 = 来源不可追溯，阻断（fail-closed）。
        cf = e.get("corrections_frozen")
        if not cf and not e.get("notes"):
            errs.append(f"E_ORACLE_MISSING_CORRECTION: {oid} 既无 corrections_frozen 也无 notes，"
                        f"来源读证不可追溯")
        for item in cf or []:
            lack = [k for k in CORRECTION_KEYS if not item.get(k)]
            if lack:
                errs.append(f"E_ORACLE_CORRECTION_INCOMPLETE: {oid} corrections_frozen 项缺 {lack}")

    hr = doc.get("human_review", {})
    if hr.get("state") == "done":
        errs.append("E_ORACLE_HUMAN_REVIEW_VIOLATED: human_review.state=done 与本卡未完成人工复核矛盾")
    return errs


# ---------------------------------------------------------------- budgets
def check_budgets(doc: dict) -> list[str]:
    errs = []
    try:
        require_keys(doc, {"schema_version", "calibration", "required_fields_per_budget",
                           "budgets", "budget_count", "freezing_rules", "gate"}, "budgets")
    except Violation as exc:
        return [f"{exc.code}: {exc.detail}"]

    cal = doc.get("calibration", {})
    budgets = doc.get("budgets", [])
    if doc.get("budget_count") != len(budgets):
        errs.append(f"E_BUDGET_COUNT_MISMATCH: budget_count={doc.get('budget_count')} "
                    f"与实际 {len(budgets)} 不一致")
    # 未校准不得有预算数值
    if cal.get("state") == "NOT_CALIBRATED" and budgets:
        errs.append("E_UNCALIBRATED_BUDGET_PRESENT: calibration.state=NOT_CALIBRATED 但 budgets 非空"
                    "（未校准填数值=伪 SLO）")
    for b in budgets:
        missing = sorted(set(doc.get("required_fields_per_budget", [])) - set(b))
        if missing:
            errs.append(f"E_BUDGET_FIELD_MISSING: {b.get('budget_id','<no-id>')} 缺 {missing}")
        if b.get("slo_met") is True:
            errs.append(f"E_SLO_BEFORE_CALIBRATION: {b.get('budget_id','<no-id>')} 在未校准状态宣称 SLO 已达标")
    gate = doc.get("gate", {})
    if not gate.get("rule"):
        errs.append("E_BUDGET_GATE_MISSING: gate.rule 缺失")
    return errs


# ---------------------------------------------------------------- manifests
def verify_manifests() -> list[str]:
    """--verify-manifests = --self-check + manifest 版本一致性核验。

    文档约定本模式是 self-check 的超集：必须先跑白名单清点、结构校验、
    磁盘核验与内联正负向样本，再补 top-level source_revision 与 HEAD 的一致性。
    只跑 manifest 而不清点白名单会让缺文件场景静默通过（fail-closed 漏洞）。
    """
    errs = self_check()
    try:
        corpora = load_json(CORPORA, "corpora.json")
        oracle = load_json(ORACLE, "oracle_catalog.json")
    except Violation as exc:
        return errs + [f"{exc.code}: {exc.detail}"]

    # top-level source_revision 与当前 HEAD 的一致性（版本核验）
    head = git_head()
    for name, doc in (("corpora.json", corpora), ("oracle_catalog.json", oracle)):
        rev = doc.get("source_revision")
        if rev and rev != head:
            errs.append(f"E_REVISION_DRIFT: {name} source_revision={rev[:12]}... "
                        f"与当前 HEAD {head[:12]}... 不一致")
    return errs


# ---------------------------------------------------------------- 内联样本
def _base_docs():
    """构造内部自洽的最小四件套（负向样本在此上注入单一缺陷）。"""
    seed_sha = sha256_of(repo_path("tests/convergence/seed_sample/calc.py"))
    head = git_head()
    profiles = {
        "schema_version": "assets-profiles/v1",
        "profile_contract": {"required_fields": ["os", "arch", "python_version"],
                             "rule": "mini"},
        "candidate_profiles": [
            {"profile_id": "P-MIN", "label": "mini", "os": "ubuntu-22.04", "arch": "x86_64",
             "python_version": "3.12", "frozen": False, "commitment": "UNDECIDED",
             "status": "candidate_from_ci",
             "evidence": ["ci.yml :: job x :: runs-on ubuntu-22.04 :: python-version 3.12"],
             "rust_toolchain_locked": False, "source_revision": head},
        ],
        "candidate_count": 1, "committed_count": 0,
        "decision_points": [{"id": "DP-MIN", "status": "DECISION_REQUIRED",
                             "question": "q", "owner": "Planner"}],
    }
    corpora = {
        "schema_version": "assets-corpora/v1",
        "manifest_contract": {"required_fields": ["来源"], "rules": ["原仓只读"]},
        "tier_definitions": {t: {"purpose": "p", "freeze_basis": "f"}
                             for t in ("R0", "R1", "R2", "R3")},
        "language_matrix": {
            "declared_total": EXPECTED_LANGUAGE_TOTAL,
            "rust_grammar_count": EXPECTED_GRAMMAR_COUNT,
            "rust_grammars": [{"language": lang} for lang in sorted(LANGUAGE_ALLOWLIST)],
        },
        "corpora": [
            {"corpus_id": "C-MIN", "tier": "R0", "status": "available_offline",
             "path": "tests/convergence/seed_sample/calc.py", "language": "python",
             "source_revision": head, "sha256": seed_sha,
             "license_or_authorization": "本仓", "copy_method": "只读复制",
             "oracle_source": "人工 golden", "applicable_cases": ["符号"], "frozen": False},
        ],
        "missing": [{"tier": "R2", "status": "DECISION_REQUIRED", "reason": "无名单",
                     "required_from": "Planner", "forbidden": "禁假路径", "criteria": "c"},
                    {"tier": "R3", "status": "DECISION_REQUIRED", "reason": "无名单",
                     "required_from": "Planner", "forbidden": "禁假路径", "criteria": "c"}],
        "confusion_guard": [{"risk": "同名", "decoy_sha256": "0" * 64,
                             "rule": "按 hash 匹配"}],
    }
    oracle = {
        "schema_version": "assets-oracle-catalog/v1",
        "oracle_principles": ["不从被测复制"],
        "source_revision": head,
        "entries": [
            {"oracle_id": "O-MIN", "corpus_ref": "C-MIN", "language": "python",
             "kind": "symbol_set", "independent": True, "human_verifiable": True,
             "status": "golden_from_source_read_pending_human_review",
             "subject_path": "tests/convergence/seed_sample/calc.py",
             "subject_sha256": seed_sha,
             "assertions": [{"kind": "module_function", "value": "add"}],
             "corrections_frozen": [{"historical_error": "e", "correction": "c",
                                     "why_it_matters": "w"}]},
        ],
        "entry_count": 1, "human_review": {"state": "pending"},
    }
    budgets = {
        "schema_version": "assets-budgets/v1",
        "calibration": {"state": "NOT_CALIBRATED"},
        "required_fields_per_budget": ["校准环境"],
        "budgets": [], "budget_count": 0,
        "freezing_rules": ["先冻结再测量"],
        "gate": {"rule": "未校准不得 SLO 达标"},
    }
    return profiles, corpora, oracle, budgets


def run_inline_examples() -> list[str]:
    """正负向样本：正向必须通过，负向必须以期望错误码失败。

    负向覆盖卡片点名的六类：缺仓库、错commit/hash、未知语言、偷换平台、
    缺外部依赖资产、未批准N/A，外加预算/复核状态等自洽项。
    """

    def run(label, mutate, expect=None):
        p, c, o, b = _base_docs()
        mutate(p, c, o, b)
        sub = (check_profiles(p) + check_corpora(c, verify_disk=True)
               + check_oracle(o, verify_disk=True) + check_budgets(b))
        if expect is None:
            return label, None, sub
        if not sub:
            return label, expect, ["E_NEGATIVE_PASSED: 期望失败却通过"]
        if not any(s.startswith(expect + ":") for s in sub):
            return label, expect, [f"E_NEGATIVE_WRONG_CODE: 期望 {expect}，实际 {sub}"]
        return label, expect, None

    cases = [
        ("positive-minimal", lambda p, c, o, b: None, None),
        ("corpus path missing",
         lambda p, c, o, b: c["corpora"][0].__setitem__("path", "no/such/file.py"),
         "E_CORPUS_PATH_MISSING"),
        ("corpus hash mismatch",
         lambda p, c, o, b: c["corpora"][0].__setitem__("sha256", "1" * 64),
         "E_CORPUS_HASH_MISMATCH"),
        ("corpus revision mismatch",
         lambda p, c, o, b: c["corpora"][0].__setitem__("source_revision", "0" * 40),
         "E_CORPUS_REVISION_MISMATCH"),
        ("unknown language",
         lambda p, c, o, b: c["corpora"][0].__setitem__("language", "klingon"),
         "E_LANGUAGE_UNKNOWN"),
        ("platform swap (committed without freeze)",
         lambda p, c, o, b: (p["candidate_profiles"][0].__setitem__("commitment", "committed"),
                             p["candidate_profiles"][0].__setitem__("os", "plan9")),
         "E_PROFILE_COMMIT_UNFROZEN"),
        ("missing manifest field",
         lambda p, c, o, b: c["corpora"][0].__delitem__("oracle_source"),
         "E_MANIFEST_FIELD_MISSING"),
        ("illegal N/A without basis",
         lambda p, c, o, b: c["corpora"][0].__setitem__("status", "not_applicable_pending"),
         "E_ILLEGAL_NA"),
        ("uncalibrated budget present",
         lambda p, c, o, b: (b["budgets"].append({"budget_id": "B1", "校准环境": "x"}),
                             b.__setitem__("budget_count", 1)),
         "E_UNCALIBRATED_BUDGET_PRESENT"),
        ("oracle marked verified too early",
         lambda p, c, o, b: o["entries"][0].__setitem__("status", "golden_human_verified"),
         "E_ORACLE_HUMAN_REVIEW_VIOLATED"),
        ("oracle missing correction",
         lambda p, c, o, b: o["entries"][0].__delitem__("corrections_frozen"),
         "E_ORACLE_MISSING_CORRECTION"),
        ("oracle correction incomplete",
         lambda p, c, o, b: o["entries"][0]["corrections_frozen"][0].__delitem__("why_it_matters"),
         "E_ORACLE_CORRECTION_INCOMPLETE"),
        ("repo-scoped corpus declares dir hash",
         lambda p, c, o, b: (c["corpora"][0].__setitem__("path", "."),
                             c["corpora"][0].__setitem__("sha256", "3" * 64),
                             c["corpora"][0].__setitem__("dirty", False)),
         "E_CORPUS_DIR_HASH_FORBIDDEN"),
        ("repo-scoped corpus dirty undeclared",
         lambda p, c, o, b: (c["corpora"][0].__setitem__("path", "."),
                             c["corpora"][0].__delitem__("sha256"),
                             c["corpora"][0].__setitem__("dirty_entries", 0)),
         "E_CORPUS_DIRTY_UNDECLARED"),
        ("platform evidence mismatch",
         lambda p, c, o, b: p["candidate_profiles"][0].__setitem__("os", "plan9"),
         "E_PROFILE_PLATFORM_SWAP"),
        ("frozen profile with unverified placeholder",
         lambda p, c, o, b: (p["candidate_profiles"][0].__setitem__("frozen", True),
                             p["candidate_profiles"][0].__setitem__("frozen_ref", "ref"),
                             p["candidate_profiles"][0].__setitem__("python_abi",
                                                                    "UNVERIFIED：未实测")),
         "E_PROFILE_FROZEN_WITH_UNVERIFIED"),
        ("fake path for R2",
         lambda p, c, o, b: (c["corpora"].append(
             {"corpus_id": "C-FAKE", "tier": "R2", "status": "available_offline",
              "path": "no/such/r2", "language": "rust", "source_revision": git_head(),
              "sha256": None, "license_or_authorization": "x", "copy_method": "x",
              "oracle_source": "x", "applicable_cases": [], "frozen": False}),),
         "E_FAKE_PATH_FORBIDDEN"),
        ("missing tier declaration",
         lambda p, c, o, b: c["tier_definitions"].__delitem__("R3"),
         "E_MISSING_TIER_DECLARED"),
        ("decoy collides with real corpus",
         lambda p, c, o, b: c["confusion_guard"][0].__setitem__(
             "decoy_sha256", sha256_of(repo_path("tests/convergence/seed_sample/calc.py"))),
         "E_DECOY_COLLISION"),
        ("oracle subject hash mismatch",
         lambda p, c, o, b: o["entries"][0].__setitem__("subject_sha256", "2" * 64),
         "E_ORACLE_SUBJECT_HASH_MISMATCH"),
        ("budget count mismatch",
         lambda p, c, o, b: b.__setitem__("budget_count", 7),
         "E_BUDGET_COUNT_MISMATCH"),
    ]

    errs = []
    for label, mutate, expect in cases:
        _, _, sub = run(label, mutate, expect)
        if sub:
            errs.append(f"{label}: {sub[0]}")
    return errs


# ---------------------------------------------------------------- self-check
def self_check() -> list[str]:
    errs = []
    for name in SELF_CHECK_FILES:
        if not (INVENTORY_DIR / name).is_file():
            errs.append(f"E_FILE_MISSING: 白名单文件缺失：{name}")
    try:
        profiles = load_json(PROFILES, "profiles.json")
        corpora = load_json(CORPORA, "corpora.json")
        oracle = load_json(ORACLE, "oracle_catalog.json")
        budgets = load_json(BUDGETS, "budgets.json")
    except Violation as exc:
        return errs + [f"{exc.code}: {exc.detail}"]

    errs += check_profiles(profiles)
    errs += check_corpora(corpora, verify_disk=True)
    errs += check_oracle(oracle, verify_disk=True)
    errs += check_budgets(budgets)
    errs += run_inline_examples()
    return errs


# ---------------------------------------------------------------- main
def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--self-check", action="store_true",
                        help="清点白名单、校验四个产物、核验磁盘资产并跑正负向样本")
    parser.add_argument("--verify-manifests", action="store_true",
                        help="self-check 超集：白名单清点 + 结构 + 磁盘 + 内联样本 + manifest 版本一致性")
    args = parser.parse_args(argv)

    if not any([args.self_check, args.verify_manifests]):
        parser.error("需要至少一个模式：--self-check / --verify-manifests")

    errs: list[str] = []
    if args.self_check:
        errs += self_check()
    if args.verify_manifests:
        errs += verify_manifests()

    if errs:
        for e in errs:
            print(f"FAIL {e}", file=sys.stderr)
        print(f"RESULT: BLOCKED ({len(errs)} violation(s))", file=sys.stderr)
        return 1
    print("RESULT: PASS (static planning only; not a product verdict)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
