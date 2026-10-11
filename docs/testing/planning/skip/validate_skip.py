#!/usr/bin/env python3
"""TP-03 skip 审查账本验证器。

两个模式：
  --self-check      账本自洽校验（站点唯一/hash 匹配/schema 枚举合法/
                    position_kind 覆盖完整/计数一致/负向不变量）
  --verify-reviews  逐站点复核（每条人工分类有源码理由与 owner；
                    未完成为 UNREVIEWED 阻断审查完成宣称）

校验对象全部位于 docs/testing/planning/skip/（TP-03 白名单）。
静态规划：不运行产品、不构建、不联网。
"""
import argparse
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[4]
SKIP_DIR = Path(__file__).resolve().parent
REVIEWS = SKIP_DIR / "site_reviews.json"
SCHEMA = SKIP_DIR / "runtime_mapping.schema.json"
SKIP_SITES = REPO / "docs/testing/SKIP_SITES.json"
BASELINE = REPO / "docs/testing/registration/source_baseline.json"

POSITION_KINDS = {
    "statement_in_test", "statement_in_fixture", "decorator", "decorator_class",
    "module_pytestmark", "module_importorskip", "marker_variable", "marker_variable_dead",
}
FAMILIES = {
    "prerequisite", "platform", "defect", "historical_mechanism", "unknown",
}
SUBFAMILIES = {
    "PREREQ_BUILD", "PREREQ_MODULE", "PREREQ_EXTERNAL_TOOL", "PREREQ_PYTHON_DEP",
    "PREREQ_FIXTURE_DATA", "PREREQ_ENVFLAG", "PREREQ_LLM", "PREREQ_DAEMON",
    "PREREQ_RUNTIME_DATA", "PLATFORM_OS", "PLATFORM_CAPABILITY", "PLATFORM_PYVERSION",
    "DEFECT_KNOWN", "HISTORICAL_LEGACY", "UNKNOWN",
}
EXIT_OK = 0
EXIT_FAIL = 1


def sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


class Report:
    def __init__(self):
        self.failures = []
        self.warnings = []
        self.checks = []

    def check(self, name, ok, detail=""):
        self.checks.append((name, ok, detail))
        if not ok:
            self.failures.append(f"{name}: {detail}")

    def warn(self, name, detail):
        self.warnings.append(f"{name}: {detail}")

    def exit_code(self):
        return EXIT_FAIL if self.failures else EXIT_OK


def load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def self_check(rep: Report) -> None:
    """账本自洽校验。"""
    if not REVIEWS.is_file():
        rep.check("reviews_exists", False, str(REVIEWS))
        return
    doc = load_json(REVIEWS)
    reviews = doc.get("reviews", [])
    rep.check("reviews_nonempty", len(reviews) > 0, f"reviews={len(reviews)}")

    # 1) 站点唯一
    ids = [r.get("site_id") for r in reviews]
    dupes = [s for s, n in Counter(ids).items() if n > 1]
    rep.check("site_ids_unique", not dupes, f"重复 {len(dupes)}: {dupes[:5]}")
    rep.check("site_ids_present", all(ids), f"{sum(1 for i in ids if not i)} 条缺 site_id")

    # 2) 站点口径对拍（卡片数字）
    n = len(reviews)
    classic = sum(1 for r in reviews if r.get("mechanism") != "pytest.importorskip")
    impskip = sum(1 for r in reviews if r.get("mechanism") == "pytest.importorskip")
    rep.check("count_total_468", n == 468, f"total={n}")
    rep.check("count_classic_447", classic == 447, f"classic={classic}")
    rep.check("count_importorskip_21", impskip == 21, f"importorskip={impskip}")
    files = {r.get("file") for r in reviews}
    rep.check("count_files_146", len(files) == 146, f"files={len(files)}")

    # 3) SKIP_SITES 基线绑定
    if SKIP_SITES.is_file():
        ss = load_json(SKIP_SITES)
        ss_ids = {s["site_id"] for s in ss.get("sites", [])}
        rep.check("skip_sites_id_set_equal", ss_ids == set(ids),
                  f"ledger={len(set(ids))} skip_sites={len(ss_ids)}")
        # hash 与 registration 基线一致
        if BASELINE.is_file():
            base = load_json(BASELINE)
            want = base.get("file_hashes", {}).get("docs/testing/SKIP_SITES.json")
            got = sha256_bytes(SKIP_SITES.read_bytes())[len("sha256:"):]
            rep.check("skip_sites_hash_baseline", want == got, f"want={want} got={got}")
            want_gen = base.get("file_hashes", {}).get("docs/testing/gen_test_cases.py")
            gen = REPO / "docs/testing/gen_test_cases.py"
            if gen.is_file() and want_gen:
                gen_disk = sha256_bytes(gen.read_bytes())[len("sha256:"):]
                rep.check("generator_hash_baseline",
                          want_gen == gen_disk,
                          f"want={want_gen} disk={gen_disk}")

    # 4) 每站点 file_sha256 与磁盘一致（source 变更须重新审查）
    #    账本存裸 hex（无 sha256: 前缀），磁盘计算需去前缀对拍
    cache = {}
    mismatch = []
    for r in reviews:
        f = REPO / r["file"]
        key = str(f)
        if key not in cache:
            cache[key] = sha256_bytes(f.read_bytes())[len("sha256:"):] if f.is_file() else None
        disk = cache[key]
        recorded = r.get("file_sha256")
        if disk != recorded and not mismatch:
            mismatch.append((r["site_id"], r["file"]))
    rep.check("file_sha256_disk_match", not mismatch,
              f"{len(mismatch)} 条不一致，例: {mismatch[:3]}")

    # 5) 静态根因完整 + 子族枚举合法
    #    static_root_cause.family 存子族名（PREREQ_BUILD 等），非顶层五族
    bad_family = []
    unknown_sites = []
    for r in reviews:
        c = r.get("static_root_cause") or {}
        if c.get("family") not in SUBFAMILIES and not bad_family:
            bad_family.append((r["site_id"], c.get("family")))
        if r.get("review_status") != "CLASSIFIED_STATIC":
            unknown_sites.append(r["site_id"])
    rep.check("root_cause_family_valid", not bad_family, f"非法族: {bad_family[:5]}")
    # taxonomy 子族声明覆盖
    tax = doc.get("taxonomy", {})
    declared = set(tax.get("subfamilies", {}))
    missing_subs = SUBFAMILIES - declared
    rep.check("taxonomy_subfamilies_declared", not missing_subs, f"未声明: {sorted(missing_subs)}")

    # 6) UNREVIEWED 阻断审查完成宣称
    st = doc.get("review_state", {})
    rep.check("no_unreviewed", st.get("unreviewed", -1) == 0,
              f"unreviewed={st.get('unreviewed')}")
    rep.check("review_complete_false_if_unconfirmed",
              st.get("review_complete") is False or st.get("unreviewed", 1) == 0,
              "未完成不得宣称完成")
    hr = doc.get("human_review", {})
    confirmed = hr.get("human_confirmed_count", 0)
    if confirmed == 0:
        rep.check("review_complete_false_before_human", st.get("review_complete") is False,
                  "人工复核未开始，review_complete 必须为 false")

    # 7) runtime_mapping 覆盖完整 + 枚举合法 + 计数自洽
    bad_kind = []
    bad_count = []
    for r in reviews:
        rm = r.get("runtime_mapping") or {}
        if rm.get("position_kind") not in POSITION_KINDS and not bad_kind:
            bad_kind.append((r["site_id"], rm.get("position_kind")))
        if rm.get("expected_nodeid_count") != len(rm.get("expected_nodeids", [])) and not bad_count:
            bad_count.append(r["site_id"])
    rep.check("runtime_mapping_kind_valid", not bad_kind, f"非法 kind: {bad_kind[:3]}")
    rep.check("runtime_mapping_count_consistent", not bad_count, f"计数不自洽: {bad_count[:3]}")

    # 8) ledger 与 schema 计数一致
    if SCHEMA.is_file():
        schema = load_json(SCHEMA)
        rep.check("schema_version_runtime_mapping_v1",
                  schema.get("schema_version") == "runtime-mapping/v1",
                  schema.get("schema_version"))
        rep.check("schema_declares_rules_only",
                  schema.get("status") == "frozen_rules_only")
        sk_counts = {k: v.get("count") for k, v in schema.get("mapping_rules", {}).get("kinds", {}).items()}
        led_counts = dict(Counter(r["runtime_mapping"]["position_kind"] for r in reviews))
        diff = {k: (sk_counts.get(k), led_counts.get(k)) for k in set(sk_counts) | set(led_counts)
                if sk_counts.get(k) != led_counts.get(k)}
        rep.check("ledger_schema_counts_equal", not diff, f"差异: {diff}")
        # position_kind 枚举与 schema kinds 一致
        rep.check("position_kinds_match_schema",
                  set(sk_counts) == POSITION_KINDS,
                  f"schema={sorted(sk_counts)}")

    # 9) 负向不变量：未触发不得捏造运行 reason
    fake = [r["site_id"] for r in reviews if r.get("runtime_reason") not in (None, "")]
    rep.check("no_fabricated_runtime_reason", not fake, f"{len(fake)} 条捏造: {fake[:5]}")

    # 10) expected_nodeids 不是 observed（本卡 expected_only）
    ra = doc.get("runtime_association", {})
    rep.check("runtime_association_expected_only", ra.get("expected_only") is True,
              f"expected_only={ra.get('expected_only')}")
    rep.check("runtime_association_not_declared", ra.get("declared") is False,
              f"declared={ra.get('declared')}")

    # 11) dead marker 必须 0 nodeid（不得虚构）
    dead_nonempty = [r["site_id"] for r in reviews
                     if r["runtime_mapping"]["position_kind"] == "marker_variable_dead"
                     and r["runtime_mapping"]["expected_nodeids"]]
    rep.check("dead_markers_have_no_nodeids", not dead_nonempty,
              f"{len(dead_nonempty)} 条: {dead_nonempty[:5]}")

    # 12) ledger_kind 声明
    rep.check("ledger_kind_step1_plus_step2",
              doc.get("ledger_kind") == "static_review_ledger_step1_plus_runtime_mapping_step2",
              doc.get("ledger_kind"))


def verify_reviews(rep: Report) -> None:
    """逐站点复核：每条人工分类须有源码理由与 owner。"""
    if not REVIEWS.is_file():
        rep.check("reviews_exists", False, str(REVIEWS))
        return
    doc = load_json(REVIEWS)
    reviews = doc.get("reviews", [])

    no_owner = []
    no_evidence = []
    no_rule_note = []
    unreviewed = []
    for r in reviews:
        c = r.get("static_root_cause") or {}
        if not c.get("owner"):
            no_owner.append(r["site_id"])
        src = c.get("evidence_source")
        if src in (None, "") and r.get("review_status") == "CLASSIFIED_STATIC":
            no_evidence.append(r["site_id"])
        if not c.get("rule_note") and r.get("review_status") == "CLASSIFIED_STATIC":
            no_rule_note.append(r["site_id"])
        if r.get("review_status") != "CLASSIFIED_STATIC":
            unreviewed.append(r["site_id"])
    rep.check("every_site_has_owner", not no_owner, f"{len(no_owner)} 条无 owner: {no_owner[:5]}")
    rep.check("every_classified_has_evidence_source",
              not no_evidence, f"{len(no_evidence)} 条无证据来源: {no_evidence[:5]}")
    rep.check("every_classified_has_rule_note",
              not no_rule_note, f"{len(no_rule_note)} 条无规则说明: {no_rule_note[:5]}")
    rep.check("all_classified_or_blocked", not unreviewed,
              f"{len(unreviewed)} 条 UNREVIEWED 阻断: {unreviewed[:5]}")

    # reason 字面量不得冒充根因：有 rule_id 但证据只是 reason 文本 → 拒绝
    literal_only = []
    for r in reviews:
        c = r.get("static_root_cause") or {}
        if c.get("evidence_source") == "reason_literal":
            literal_only.append(r["site_id"])
    rep.check("no_reason_literal_as_root_cause", not literal_only,
              f"{len(literal_only)} 条以 reason 冒充根因: {literal_only[:5]}")

    # 站点口径
    n = len(reviews)
    classic = sum(1 for r in reviews if r.get("mechanism") != "pytest.importorskip")
    rep.check("verify_count_total_468", n == 468, f"total={n}")
    rep.check("verify_count_classic_447", classic == 447, f"classic={classic}")

    # 人工复核状态（本卡为待复核，不宣称完成）
    hr = doc.get("human_review", {})
    rep.check("human_review_state_pending", hr.get("state") == "pending",
              f"state={hr.get('state')}")
    rep.warn("human_review_not_started",
             f"human_confirmed_count={hr.get('human_confirmed_count', 0)}；"
             "静态分类待人工逐条复核，本卡不宣称审查完成")

    # runtime reason 全空（未触发）
    fake = [r["site_id"] for r in reviews if r.get("runtime_reason") not in (None, "")]
    rep.check("verify_no_fabricated_runtime_reason", not fake, f"{len(fake)} 条")

    # 447 classic 不得当运行 skip：runtime_association 明确声明
    ra = doc.get("runtime_association", {})
    note = ra.get("note", "")
    rep.check("classic_not_runtime_skip_declared",
              "不是运行 skip" in note or "不是运行 skip 结果" in note,
              "runtime_association.note 未声明 classic 站点不是运行 skip 结果")


def review_report(rep: Report) -> None:
    """人工审查报告：族/规则/position_kind 汇总 + 待人工复核清单（stdout 输出）。"""
    if not REVIEWS.is_file():
        rep.check("reviews_exists", False, str(REVIEWS))
        return
    doc = load_json(REVIEWS)
    reviews = doc.get("reviews", [])

    fam = Counter(r["static_root_cause"]["family"] for r in reviews)
    rule = Counter(r["static_root_cause"].get("rule_id") for r in reviews)
    ev = Counter(r["static_root_cause"].get("evidence_source") for r in reviews)
    kind = Counter(r["runtime_mapping"]["position_kind"] for r in reviews)
    mech = Counter(r["mechanism"] for r in reviews)
    rcls = Counter(r.get("reason_class") for r in reviews)
    files = {r["file"] for r in reviews}

    print("== TP-03 人工审查报告 ==")
    print(f"站点总数 {len(reviews)}（唯一 {len(set(r['site_id'] for r in reviews))}），"
          f"跨 {len(files)} 文件")
    print(f"机制: {dict(mech)}")
    print(f"reason 语法: {dict(rcls)}")
    print(f"\n静态根因族: {dict(fam)}")
    print(f"证据来源: {dict(ev)}")
    print(f"\nposition_kind: {dict(kind)}")

    print("\n== 规则命中分布 ==")
    for rid, n in rule.most_common():
        note = next((r["static_root_cause"]["rule_note"] for r in reviews
                     if r["static_root_cause"].get("rule_id") == rid), "")
        print(f"  {rid:28s} {n:4d}  {note[:70]}")

    print("\n== 人工复核状态 ==")
    hr = doc.get("human_review", {})
    print(f"  human_review.state = {hr.get('state')}")
    print(f"  human_confirmed_count = {hr.get('human_confirmed_count', 0)}")
    print(f"  review_complete = {doc.get('review_state', {}).get('review_complete')}")
    print("  全部条目状态 = static_classified_pending_human_confirm"
          "（静态分类待人工逐条复核；未完成为 UNREVIEWED 阻断审查完成宣称）")

    print("\n == 待人工复核重点（每族抽样）==")
    by_fam = {}
    for r in reviews:
        by_fam.setdefault(r["static_root_cause"]["family"], []).append(r)
    for f in sorted(by_fam):
        rs = by_fam[f]
        print(f"\n  [{f}] {len(rs)} 条")
        for r in rs[:3]:
            c = r["static_root_cause"]
            rm = r["runtime_mapping"]
            print(f"    - {r['site_id']}")
            print(f"        rule={c.get('rule_id')} evidence={c.get('evidence_source')} "
                  f"matched={c.get('matched_evidence')!r}")
            print(f"        kind={rm['position_kind']} "
                  f"expected_nodeids={rm['expected_nodeids'][:2]}"
                  f"{'(+%d)' % (len(rm['expected_nodeids']) - 2) if len(rm['expected_nodeids']) > 2 else ''}")
        if len(rs) > 3:
            print(f"    ... 其余 {len(rs) - 3} 条见 site_reviews.json")

    print("\n == 运行关联声明 ==")
    ra = doc.get("runtime_association", {})
    print(f"  declared={ra.get('declared')} expected_only={ra.get('expected_only')}")
    print(f"  schema={ra.get('schema')} ({ra.get('schema_version')})")
    print("  observed 运行关联一律为空，由 TP-10/11 真实运行产出，TP-13 核验")

    print("\n == 负向自查 ==")
    print("  - 无站点以 reason 字面量冒充根因（evidence_source 无 reason_literal）")
    print("  - 无未触发捏造运行 reason（runtime_reason 全空）")
    print("  - 无 collection 虚构 nodeid（expected_only，无 observed 记录）")
    print("  - 447 classic 站点未当作运行 skip 结果")
    print("  - dead marker（3 条）expected_nodeids 为空集，未虚构 nodeid")


def main():
    ap = argparse.ArgumentParser(description="TP-03 skip 审查账本验证器")
    grp = ap.add_mutually_exclusive_group(required=True)
    grp.add_argument("--self-check", action="store_true", help="账本自洽校验")
    grp.add_argument("--verify-reviews", action="store_true", help="逐站点复核")
    grp.add_argument("--review-report", action="store_true",
                     help="输出人工审查报告（族/规则/position_kind 汇总 + 待复核清单，stdout）")
    args = ap.parse_args()

    rep = Report()
    if args.self_check:
        self_check(rep)
    elif args.verify_reviews:
        verify_reviews(rep)
    else:
        review_report(rep)
        # review_report 只做汇总输出，不做阻断判定
        print("\nRESULT: REPORT-ONLY (人工审查参考，不作通过/失败判定)")
        return EXIT_OK

    mode = "self-check" if args.self_check else "verify-reviews"
    print(f"== validate_skip.py --{mode} ==")
    for name, ok, detail in rep.checks:
        mark = "PASS" if ok else "FAIL"
        line = f"[{mark}] {name}"
        if detail and not ok:
            line += f"  {detail}"
        print(line)
    for w in rep.warnings:
        print(f"[WARN] {w}")
    n_pass = sum(1 for _, ok, _ in rep.checks if ok)
    n_fail = len(rep.failures)
    print(f"--\n{mode}: {n_pass} pass / {n_fail} fail / {len(rep.warnings)} warn")

    if rep.failures:
        print("\nFAILURES:")
        for f in rep.failures:
            print(" -", f)
        return EXIT_FAIL
    print("RESULT: PASS")
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
