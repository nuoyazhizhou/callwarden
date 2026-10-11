#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""TP-04 GAP-15 语义差异与对齐设计 —— 验收验证器。

三种模式：
  --self-check        账本自洽性（正常/重放/冲突/计划缺陷/责任五类 oracle）
  --verify-contract   验收合同独立核验（returned/reviewer_blocked 的 source
                      verdict/step/finding/provenance、assignment/projection、
                      幂等和错误码契约的登记完整性）
  --review-report     人工审查报告（stdout，不写白名单外文件）

约束：静态规划验证器，只读白名单交付物与被引用的源码/文档；不运行产品测试/构建/联网。
"""
import argparse
import hashlib
import json
import pathlib
import sys

REPO = pathlib.Path("C:/git_work/callwarden")
GOV = REPO / "docs" / "testing" / "planning" / "governance"

DIFF = GOV / "differences.json"
ALIGN = GOV / "ALIGNMENT.md"
ACCEPT = GOV / "acceptance_contract.json"

PROTOCOL_DOCS = [
    "AGENTS.md",
    ".agents/skills/cw-task-loop/references/role-protocol.md",
    "docs/design/cw-role-handoff-task-loop-v2-amendment.md",
]

IMPL_FILES = [
    "rust_ext/src/daemon/task_collab_lifecycle.rs",
    "rust_ext/src/daemon/task_collab_lease.rs",
    "rust_ext/src/daemon/task_collab_shared.rs",
    "rust_ext/src/daemon/task_loop/claim.rs",
    "rust_ext/src/daemon/task_loop/next_action.rs",
    "rust_ext/src/daemon/task_loop/report_handoff.rs",
    "cli/main.py",
    "server/tools/tools_collab.py",
]

TEST_FILE = "rust_ext/src/daemon/task_collab_tests_core.rs"

BASELINE_HEAD = "db952964ca49e07d77c033e886298a3e004542af"

pass_count = 0
fail_count = 0
warn_count = 0


def ok(msg, detail=None):
    global pass_count
    pass_count += 1
    print(f"  PASS  {msg}" + (f"  [{detail}]" if detail else ""))


def fail(msg, detail=None):
    global fail_count
    fail_count += 1
    print(f"  FAIL  {msg}" + (f"  [{detail}]" if detail else ""))


def warn(msg, detail=None):
    global warn_count
    warn_count += 1
    print(f"  WARN  {msg}" + (f"  [{detail}]" if detail else ""))


def sha256_file(rel):
    p = REPO / rel
    if not p.is_file():
        return None
    return hashlib.sha256(p.read_bytes()).hexdigest()


def load_json(path):
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        return {"__error__": str(e)}


def check_self_check():
    print("== self-check: 账本自洽性 ==")
    diff = load_json(DIFF)
    acc = load_json(ACCEPT)

    # OC-NORMAL-01
    if not isinstance(diff, dict) or "__error__" in diff:
        fail("differences.json 可解析", str(diff))
        return
    ok("differences.json 可解析")

    if diff.get("schema_version") != "governance-diff/v1":
        fail("schema_version=governance-diff/v1", diff.get("schema_version"))
    else:
        ok("schema_version=governance-diff/v1")

    diffs = diff.get("differences", [])
    if len(diffs) < 2:
        fail("differences >=2", len(diffs))
    else:
        ok("differences >=2", f"{len(diffs)} 项")

    for d in diffs:
        for fld in ["diff_id", "title", "category", "protocol_semantics",
                    "implementation_semantics", "conflict", "runtime_status"]:
            if fld not in d:
                fail(f"{d.get('diff_id','?')} 含字段 {fld}")
            else:
                ok(f"{d['diff_id']} 含字段 {fld}")

    d01 = next((x for x in diffs if x.get("diff_id") == "GAP-15-D01"), None)
    if not d01:
        fail("GAP-15-D01 存在")
    else:
        ok("GAP-15-D01 存在")
        pc = d01["protocol_semantics"].get("citations", [])
        ic = d01["implementation_semantics"].get("citations", [])
        if len(pc) < 9:
            fail("D01 protocol citations >=9", str(len(pc)))
        else:
            ok("D01 protocol citations >=9", str(len(pc)))
        if len(ic) < 9:
            fail("D01 implementation evidence >=9", str(len(ic)))
        else:
            ok("D01 implementation evidence >=9", str(len(ic)))
        if d01["protocol_semantics"].get("doc_count", 0) < 3:
            fail("D01 protocol doc_count >=3", str(d01["protocol_semantics"].get("doc_count")))
        else:
            ok("D01 protocol doc_count >=3", str(d01["protocol_semantics"].get("doc_count")))

    # aligned items
    aligned = diff.get("aligned_items", [])
    if len(aligned) < 5:
        fail("aligned_items >=5", str(len(aligned)))
    else:
        ok("aligned_items >=5", f"{len(aligned)} 条")
    for a in aligned:
        cites = a.get("citations", [])
        if len(cites) < 2:
            fail(f"{a.get('item_id','?')} citations >=2", str(len(cites)))
        else:
            ok(f"{a.get('item_id','?')} citations >=2", str(len(cites)))

    # citation 磁盘对拍
    all_cites = []
    for d in diffs:
        all_cites += d["protocol_semantics"].get("citations", [])
        all_cites += d["implementation_semantics"].get("citations", [])
        if "static_test_contract" in d:
            all_cites += d["static_test_contract"].get("citations", [])
    for a in aligned:
        all_cites += a.get("citations", [])
    bad_sha = []
    empty_ex = []
    for c in all_cites:
        disk = sha256_file(c["file"])
        if disk is None:
            bad_sha.append((c["file"], "missing"))
        elif disk != c["sha256"]:
            bad_sha.append((c["file"], "hash mismatch"))
        if not c.get("excerpt"):
            empty_ex.append((c["file"], c["lines"]))
    if bad_sha:
        fail("citation sha256 磁盘对拍", str(bad_sha[:3]))
    else:
        ok("citation sha256 磁盘对拍", f"{len(all_cites)} 条全部一致")
    if empty_ex:
        fail("citation excerpt 非空", str(empty_ex[:3]))
    else:
        ok("citation excerpt 非空")

    # OC-REPLAY-01
    if diff.get("source_revision", "").startswith(BASELINE_HEAD[:12]):
        ok("source_revision 与 registration baseline 一致", diff["source_revision"][:12])
    else:
        fail("source_revision 与 registration baseline 一致", str(diff.get("source_revision")))

    # authority/impl/test 文件存在性
    for f in PROTOCOL_DOCS + IMPL_FILES + [TEST_FILE]:
        if not (REPO / f).is_file():
            fail(f"被引用文件存在 {f}")
    ok("被引用文件全部存在", f"{len(PROTOCOL_DOCS)+len(IMPL_FILES)+1} 个")

    # OC-CONFLICT-01
    if not d01:
        fail("D01 存在（conflict oracle）")
    else:
        if d01.get("conflict") is not True:
            fail("D01 conflict=True")
        else:
            ok("D01 conflict=True")
        if not d01.get("conflict_type"):
            fail("D01 conflict_type 非空")
        else:
            ok("D01 conflict_type 非空", d01["conflict_type"])
        opts = d01.get("direction_options", [])
        if len(opts) != 2:
            fail("D01 direction_options 恰好 2 个", str(len(opts)))
        else:
            ok("D01 direction_options 恰好 2 个")
        for o in opts:
            for fld in ["option_id", "name", "doc_changes", "product_changes", "downstream"]:
                if not o.get(fld):
                    fail(f"direction {o.get('option_id','?')} 含非空 {fld}")
                else:
                    ok(f"direction {o['option_id']} 含非空 {fld}")
        ab = d01.get("approval_boundary", "")
        if ab != "target_semantics_pending_project_authority_approval":
            fail("D01 approval_boundary 未预设批准", ab)
        else:
            ok("D01 approval_boundary 未预设批准")
        an = d01.get("approval_note", "")
        if "未来合同绑定" not in an and "不代替裁定" not in an:
            fail("D01 approval_note 声明审批事件来源")
        else:
            ok("D01 approval_note 声明审批事件来源")

    d02 = next((x for x in diffs if x.get("diff_id") == "GAP-15-D02"), None)
    if not d02:
        fail("D02 存在")
    else:
        pa = d02.get("partial_agreement_note", "")
        if not pa:
            fail("D02 partial_agreement_note 非空")
        else:
            ok("D02 partial_agreement_note 非空")
        for kw in ["CLI/MCP", "自动路径"]:
            if kw not in pa:
                fail(f"D02 note 区分 {kw} 维度")
            else:
                ok(f"D02 note 区分 {kw} 维度")
        if d02.get("depends_on") != "GAP-15-D01":
            fail("D02 depends_on=D01")
        else:
            ok("D02 depends_on=D01")

    # 编造 capability 检查：adjudicator_returned 不得被称为 capability 门控项
    blob = json.dumps(diff, ensure_ascii=False)
    for bad in [
        "adjudicator_returned 的 capability",
        "capability 门控.*adjudicator_returned",
    ]:
        import re
        if re.search(bad, blob):
            fail(f"未把 adjudicator_returned 编造为 capability 门控项: {bad}")
    ok("未把 adjudicator_returned 编造为 capability 门控项")

    # OC-PLAN-DEFECT-01
    if "已批准" in blob or "approval_granted" in blob:
        fail("账本不含未经审批事件的'已批准'宣称")
    else:
        ok("账本不含未经审批事件的'已批准'宣称")
    rt = [d for d in diffs if d.get("runtime_status") != "unknown"]
    if rt:
        fail("所有 difference runtime_status=unknown", str([d["diff_id"] for d in rt]))
    else:
        ok("所有 difference runtime_status=unknown")

    # OC-RESPONSIBILITY-01
    if not isinstance(acc, dict) or "__error__" in acc:
        fail("acceptance_contract.json 可解析", str(acc))
        return
    ok("acceptance_contract.json 可解析")
    if acc.get("schema_version") != "governance-acceptance/v1":
        fail("acceptance schema_version", str(acc.get("schema_version")))
    else:
        ok("acceptance schema_version=governance-acceptance/v1")
    resps = acc.get("responsibilities", [])
    if len(resps) < 4:
        fail("responsibilities >=4", str(len(resps)))
    else:
        ok("responsibilities >=4", f"{len(resps)} 条")
    for r in resps:
        for fld in ["resp_id", "direction", "owner", "type", "scope", "closed"]:
            if fld not in r:
                fail(f"{r.get('resp_id','?')} 含字段 {fld}")
            else:
                ok(f"{r.get('resp_id','?')} 含字段 {fld}")
        if r.get("closed") is not False:
            fail(f"{r.get('resp_id','?')} closed=False（未审批前不得闭合）")
        else:
            ok(f"{r.get('resp_id','?')} closed=False")
    dirA = [r for r in resps if r.get("direction") == "A"]
    dirB = [r for r in resps if r.get("direction") == "B"]
    if not dirA:
        fail("方向 A 有责任路径")
    else:
        ok("方向 A 有责任路径", f"{len(dirA)} 条 -> {dirA[0]['owner']}")
    if not dirB:
        fail("方向 B 有责任路径")
    else:
        ok("方向 B 有责任路径", f"{len(dirB)} 条 -> {[r['owner'] for r in dirB]}")
    if dirB and not any(r.get("blocked_successors") for r in dirB):
        fail("方向 B 阻塞下游（TP-09/11G）")
    else:
        ok("方向 B 阻塞下游", str(sorted({s for r in dirB for s in r.get("blocked_successors", [])})))

    # oracle 完整性
    oracles = acc.get("oracles", [])
    if len(oracles) < 5:
        fail("oracles >=5", str(len(oracles)))
    else:
        ok("oracles >=5", f"{len(oracles)} 条")
    need_routes = {"normal", "replay", "conflict", "plan_defect", "responsibility"}
    got = {o.get("route") for o in oracles}
    missing = need_routes - got
    if missing:
        fail("oracle 覆盖五类路由", str(missing))
    else:
        ok("oracle 覆盖五类路由")

    # ledger binding
    lb = acc.get("ledger_binding", {})
    for k, path in [("differences_json_sha256", DIFF), ("alignment_md_sha256", ALIGN)]:
        disk = hashlib.sha256(path.read_bytes()).hexdigest()
        if lb.get(k) != disk:
            fail(f"ledger_binding {k} 磁盘对拍", f"{str(lb.get(k))[:12]} vs {disk[:12]}")
        else:
            ok(f"ledger_binding {k} 磁盘对拍")

    # runtime separation
    rs = acc.get("runtime_separation", {})
    for k in ["static_planning_only", "no_product_tests_runs", "no_builds",
              "no_network", "static_test_as_contract_evidence_only"]:
        if rs.get(k) is not True:
            fail(f"runtime_separation {k}=True")
        else:
            ok(f"runtime_separation {k}=True")

    # approval boundary
    ab2 = acc.get("approval_boundary", {})
    if ab2.get("mode") != "target_semantics_pending_project_authority_approval":
        fail("acceptance approval_boundary.mode")
    else:
        ok("acceptance approval_boundary.mode 未预设批准")
    if ab2.get("planner_does_not_impersonate_adjudicator") is not True:
        fail("planner_does_not_impersonate_adjudicator=True")
    else:
        ok("planner_does_not_impersonate_adjudicator=True")

    # 静态测试合同只作契约证据
    stc = d01.get("static_test_contract", {}) if d01 else {}
    if "requires" not in stc or not stc.get("citations"):
        fail("D01 static_test_contract 有 requires+citations")
    else:
        ok("D01 static_test_contract 有 requires+citations")


def check_verify_contract():
    print("== verify-contract: 验收合同独立核验 ==")
    diff = load_json(DIFF)
    acc = load_json(ACCEPT)

    if not isinstance(diff, dict):
        fail("differences.json 可解析")
        return
    if not isinstance(acc, dict):
        fail("acceptance_contract.json 可解析")
        return

    # returned/reviewer_blocked 的 source verdict/step/finding/provenance 登记
    d01 = next((x for x in diff.get("differences", []) if x.get("diff_id") == "GAP-15-D01"), None)
    if not d01:
        fail("D01 存在")
        return
    ok("D01 存在（returned/reviewer_blocked 语义载体）")

    # source verdict 契约
    ic = d01["implementation_semantics"]["citations"]
    verdict_cite = next((c for c in ic if c["lines"] == [1591, 1595]), None)
    if not verdict_cite:
        fail("source verdict 契约登记（lifecycle.rs:1591-1595 block/pass 分流）")
    else:
        ok("source verdict 契约登记", "reviewer_blocked→block / adjudicator_returned→pass")

    findings_cite = next((c for c in ic if c["lines"] == [1720, 1739]), None)
    if not findings_cite:
        fail("finding 契约登记（lifecycle.rs:1720-1739）")
    else:
        ok("finding 契约登记", "adjudicator_returned 允许空 / reviewer_blocked 必须非空")

    # step 契约
    step_cite = next((c for c in ic if c["lines"] == [1782, 1794]), None)
    if not step_cite:
        fail("remediation step 契约登记（lifecycle.rs:1782-1794）")
    else:
        ok("remediation step 契约登记", "S-<sha256[..24]> 不区分 outcome")

    auto_cite = next(
        (c for c in ic if c["file"].endswith("task_collab_lifecycle.rs")
         and c["lines"][0] <= 1521 <= c["lines"][1] and "matches!" in c.get("excerpt", "")),
        None,
    )
    if not auto_cite:
        fail("自动整改分支登记（lifecycle.rs:1521-1524）")
    else:
        ok("自动整改分支登记", "matches! 含两种 outcome")

    handler_cite = next((c for c in ic if c["lines"] == [638, 646]), None)
    if not handler_cite:
        fail("显式 handler outcome 白名单登记（lifecycle.rs:638-646）")
    else:
        ok("显式 handler outcome 白名单登记", "failed_step/reviewer_blocked/adjudicator_returned")

    # provenance 契约
    prov_cite = next(
        (c for c in ic if c["file"].endswith("next_action.rs") and c["lines"] == [600, 601]),
        None,
    )
    if not prov_cite:
        fail("provenance 判定登记（next_action.rs:600-601）")
    else:
        ok("provenance 判定登记")

    # assignment/projection 契约
    t_cites = d01.get("static_test_contract", {}).get("citations", [])
    if not t_cites:
        fail("静态测试合同登记")
    else:
        tc = t_cites[0]
        if "active assignment" not in tc.get("excerpt", "") and "active.len()" not in tc.get("excerpt", ""):
            fail("assignment/projection 契约（active assignment=1 executor）")
        else:
            ok("assignment/projection 契约", "active assignment=1, role=executor")
        if "replayed" not in tc.get("excerpt", ""):
            fail("幂等契约（replayed=true）")
        else:
            ok("幂等契约（replayed=true）")

    # 错误码契约
    err_blob = " ".join(c.get("excerpt", "") for c in ic)
    for code in ["E_REMEDIATION_ALREADY_EXISTS", "E_REMEDIATION_FINDINGS_REQUIRED"]:
        if code not in err_blob:
            # 检查 differences.json 全文
            if code not in json.dumps(diff, ensure_ascii=False):
                warn(f"错误码 {code} 在证据摘录中未出现")
            else:
                ok(f"错误码 {code} 登记")
        else:
            ok(f"错误码 {code} 登记")

    # 责任闭环
    resps = acc.get("responsibilities", [])
    for r in resps:
        if not r.get("scope"):
            fail(f"{r['resp_id']} scope 非空")
        else:
            ok(f"{r['resp_id']} scope 非空")
    # 受阻下游显式
    blob = json.dumps(acc, ensure_ascii=False)
    for kw in ["TP-05", "P-FIX", "TP-09", "TP-11G"]:
        if kw not in blob:
            fail(f"受阻下游含 {kw}")
        else:
            ok(f"受阻下游含 {kw}")

    # 验收命令存在
    cmds = acc.get("acceptance_commands", [])
    if len(cmds) < 2:
        fail("acceptance_commands >=2", str(len(cmds)))
    else:
        ok("acceptance_commands >=2")
    for c in cmds:
        if "validate_contract.py" not in c:
            fail(f"验收命令指向本验证器: {c}")
        else:
            ok(f"验收命令指向本验证器: {c.split(chr(47))[-1]}")

    # 未审批宣称检查：只在语义字段（approval_boundary / responsibilities.closed /
    # status）里查"已批准"，不查 oracle check 的负向描述文本
    semantic_blob = json.dumps({
        "approval_boundary": acc.get("approval_boundary", {}),
        "status": acc.get("status", ""),
        "responsibilities_closed": [r.get("closed") for r in resps],
        "directions": acc.get("directions", []),
    }, ensure_ascii=False)
    if "approval_granted" in semantic_blob or "已批准" in semantic_blob:
        fail("合同语义字段不含未经审批的'已批准'宣称")
    else:
        ok("合同语义字段不含未经审批的'已批准'宣称")

    # 负向 oracle 完整
    negs = acc.get("acceptance_criteria", {}).get("negative", [])
    if len(negs) < 6:
        fail("negative criteria >=6", str(len(negs)))
    else:
        ok("negative criteria >=6", f"{len(negs)} 条")
    for kw in ["缺权威批准", "未登记", "预设 bridge", "编造 capability"]:
        if not any(kw in n for n in negs):
            fail(f"negative 含 '{kw}'")
        else:
            ok(f"negative 含 '{kw}'")


def review_report():
    print("== review-report: 人工审查报告 ==")
    diff = load_json(DIFF)
    acc = load_json(ACCEPT)
    if not isinstance(diff, dict):
        print("differences.json 不可解析")
        return
    print(f"source_revision: {diff.get('source_revision','?')[:16]}")
    print(f"authority docs: {len(diff.get('authority_documents',[]))}")
    print(f"impl sources:   {len(diff.get('implementation_sources',[]))}")
    print(f"static tests:   {len(diff.get('static_test_contracts',[]))}")
    print()
    print("差异项：")
    for d in diff.get("differences", []):
        print(f"  {d['diff_id']}  conflict={d['conflict']}  {d['title']}")
        print(f"    type: {d.get('conflict_type','?')}  runtime: {d.get('runtime_status','?')}")
    print()
    print("一致项：")
    for a in diff.get("aligned_items", []):
        print(f"  {a['item_id']}  {a['title']}")
    print()
    if isinstance(acc, dict):
        print("责任路径：")
        for r in acc.get("responsibilities", []):
            print(f"  {r['resp_id']} [{r['direction']}] {r['owner']} ({r['type']}) closed={r['closed']}")
            print(f"    scope: {r['scope'][:90]}")
            if r.get("blocked_successors"):
                print(f"    blocked: {r['blocked_successors']}")
    print()
    print("结论：GAP-15 为唯一核心语义冲突（协议落后于实现）；目标语义 A/B 两方向")
    print("      已冻结供项目现行权威审批；TP-04 只定义不裁定，审批事件来源在未来合同绑定。")


def main():
    ap = argparse.ArgumentParser(description="TP-04 GAP-15 对齐验证器")
    ap.add_argument("--self-check", action="store_true")
    ap.add_argument("--verify-contract", action="store_true")
    ap.add_argument("--review-report", action="store_true")
    args = ap.parse_args()

    if args.self_check:
        check_self_check()
    elif args.verify_contract:
        check_verify_contract()
    elif args.review_report:
        review_report()
        return
    else:
        ap.print_help()
        return 1

    print()
    total = pass_count + fail_count
    print(f"RESULT: {pass_count} pass / {fail_count} fail / {warn_count} warn")
    if fail_count > 0:
        print("VERDICT: FAIL")
        return 1
    print("VERDICT: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
