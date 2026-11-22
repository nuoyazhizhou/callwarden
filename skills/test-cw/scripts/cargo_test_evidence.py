#!/usr/bin/env python3
"""cargo test 证据转换器 —— libtest stdout → 等价 junit XML + 证据骨架 + 六条件机检。

用途（test-cw 技能，DESIGN.md §15）：
  Rust 没有原生的 junit 输出（cargo-nextest 未纳入分发依赖），本脚本把
  `cargo test` 的 stdout 解析成与 pytest `--junitxml` 等价的证据工件，
  保证「骨架必须由脚本生成，禁止手抄 nodeid 表」在 Rust 侧同样成立。

nodeid 形态：`<target>#<test_path>`，例如 `lib#daemon::pki_api::tests::test_foo`。
  - target：cargo 测试二进制名（`--lib` → `lib`；`--test X` → `X`）。
    一次解析只接受单个二进制的输出（多批次混入时 fail-closed 报错）。
  - test_path：libtest 打印的完整路径（模块::函数）。
  - doc-tests 不纳入归属：`--lib` / `--test X` 不触发 Doc-tests 段。

两个模式：
  list   解析 `cargo test --<target> -- --list` 的输出 → 每行一个 nodeid（采集集）。
  parse  解析 `cargo test --<target> -- --exact ...` 的执行输出 → junit + 骨架 + 机检。

六条件机检（判据细则见 DESIGN.md §15；①run/code revision 由技能核对，不在本脚本内）：
  ② required ⊆ 执行结果且全部 passed
  ③ failed=0；rc!=0 且无任何 test 行 → 编译/收集错误（collection error）
  ④ required 上 ignored=0（libtest 无 xfail 概念，#[ignore] 即 skip）
  ⑤ required 缺席执行结果 = deselected → FAIL
  ⑥ rc==0 且 executed>0 且 executed==len(required)

示例：
  python cargo_test_evidence.py list --input raw-list.txt --target lib
  python cargo_test_evidence.py parse --input raw-run.txt --target lib --rc 0 ^
      --required required.txt --junit reports/junit-AC-01.xml ^
      --command "cargo test --lib -- --exact ..." [--stderr raw-run.err]
"""

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

# libtest 结果行：`test <path> ... ok|FAILED|ignored|bench`（前导空格容忍）。
RESULT_LINE = re.compile(r"^\s*test (\S+) \.\.\. (\S+)(?:\s|$)")
# 每个测试二进制末尾的汇总行。
SUMMARY_LINE = re.compile(
    r"^test result: (\S+)\. (\d+) passed; (\d+) failed; (\d+) ignored; "
    r"(\d+) measured; (\d+) filtered out"
)

OUTCOME_MAP = {"ok": "passed", "FAILED": "failed", "ignored": "skipped"}
MUTED_OUTCOMES = {"bench"}  # 基准测试不是回归用例，记录但不计入 executed。


def fail(msg):
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(2)


def read_lines(path):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as e:
        fail(f"无法读取输入文件 {path}: {e}")


def cmd_list(args):
    ids = []
    for line in read_lines(args.input):
        line = line.strip()
        if not line or line.startswith(("running ", "Compiling ", "Finished ", "Doc-tests")):
            continue
        # --list 输出形如 `name: test`（也可能带 `: benchmark`）。
        m = re.match(r"^(\S+): (test|benchmark)$", line)
        if m and m.group(2) == "test":
            ids.append(f"{args.target}#{m.group(1)}")
    if not ids:
        fail("未解析到任何测试（确认输入是 `cargo test --<target> -- --list` 的 stdout）")
    for i in ids:
        print(i)
    print(f"-- list 模式：{args.target} 共 {len(ids)} 个测试 --", file=sys.stderr)


def cmd_parse(args):
    results = []  # (test_path, outcome) 按出现顺序
    summaries = []
    for line in read_lines(args.input):
        if line.startswith("test result:"):
            m = SUMMARY_LINE.match(line)
            if m:
                summaries.append(
                    {
                        "result": m.group(1),
                        "passed": int(m.group(2)),
                        "failed": int(m.group(3)),
                        "ignored": int(m.group(4)),
                        "measured": int(m.group(5)),
                        "filtered_out": int(m.group(6)),
                    }
                )
            continue
        m = RESULT_LINE.match(line)
        if m:
            results.append((m.group(1), m.group(2)))

    if len(summaries) > 1:
        fail("检测到多条 `test result:` 汇总行——一次解析只接受单个测试二进制的输出；"
             "请按 --lib / --test X 分批采集后再解析。")

    if results and not summaries:
        fail("解析到测试行但没有 `test result:` 汇总行——输出可能被截断，证据不可用。")

    outcome_map = {}
    for path, raw in results:
        oid = f"{args.target}#{path}"
        if oid in outcome_map:
            fail(f"同一 nodeid 出现两次（二进制内重名？）：{oid}")
        if raw in OUTCOME_MAP:
            outcome_map[oid] = OUTCOME_MAP[raw]
        elif raw in MUTED_OUTCOMES:
            outcome_map[oid] = "bench"
        else:
            outcome_map[oid] = "unknown:" + raw

    executed_ids = [i for i, o in outcome_map.items() if o in ("passed", "failed", "skipped")]
    passed = sum(1 for o in outcome_map.values() if o == "passed")
    failed = sum(1 for o in outcome_map.values() if o == "failed")
    skipped = sum(1 for o in outcome_map.values() if o == "skipped")
    bench = sum(1 for o in outcome_map.values() if o == "bench")
    summary = summaries[0] if summaries else {}
    filtered_out = summary.get("filtered_out", 0)
    collection_error = args.rc != 0 and not results

    # ---- junit XML（等价 pytest --junitxml：failure/skipped 用同样的子元素表达）----
    if args.junit:
        ts = ET.Element(
            "testsuite",
            {
                "name": f"cargo-test-{args.target}",
                "tests": str(len(executed_ids)),
                "failures": str(failed),
                "errors": "1" if collection_error else "0",
                "skipped": str(skipped),
            },
        )
        if collection_error:
            tc = ET.SubElement(tc_parent := ts, "testcase", {"classname": args.target, "name": f"{args.target}#<collection>"})
            ET.SubElement(tc, "error", {"message": "cargo 编译/收集失败（无任何 test 行）", "type": "CollectionError"})
        for oid, outcome in outcome_map.items():
            tc = ET.SubElement(ts, "testcase", {"classname": args.target, "name": oid})
            if outcome == "failed":
                ET.SubElement(tc, "failure", {"message": "test FAILED", "type": "AssertionError"})
            elif outcome == "skipped":
                ET.SubElement(tc, "skipped")
        Path(args.junit).parent.mkdir(parents=True, exist_ok=True)
        ET.ElementTree(ts).write(args.junit, encoding="utf-8", xml_declaration=True)

    # ---- 六条件机检 ----
    req = []
    if args.required:
        for line in read_lines(args.required):
            line = line.strip()
            if line and not line.startswith("#"):
                req.append(line)
        if len(req) != len(set(req)):
            fail("required 文件存在重复行——先去重再作为判据输入。")
    req_set = set(req)

    checks = {}
    if req_set:
        missing = req_set - set(executed_ids)
        ignored_req = {i for i in req_set if outcome_map.get(i) == "skipped"}
        not_passed = sorted(i for i in req_set if outcome_map.get(i) not in ("passed",))
        checks["② required ⊆ 执行结果且全 passed"] = (
            not missing and not not_passed,
            f"missing={sorted(missing) if missing else '∅'}；non_passed={not_passed if not_passed else '∅'}",
        )
        checks["③ failed=0 且无收集错误"] = (
            failed == 0 and not collection_error,
            f"failed={failed}" + ("；rc!=0 且无 test 行 → 编译/收集错误" if collection_error else ""),
        )
        checks["④ required 上 ignored=0"] = (
            not ignored_req,
            f"ignored={sorted(ignored_req) if ignored_req else '∅'}（libtest 无 xfail；#[ignore] 即 skip）",
        )
        checks["⑤ required 无缺席（deselected∩required=∅）"] = (
            not missing,
            f"缺席={sorted(missing) if missing else '∅'}；filtered_out={filtered_out}（备查）",
        )
        checks["⑥ rc=0 且 executed>0 且 executed=len(required)"] = (
            args.rc == 0 and len(executed_ids) == len(req_set) and executed_ids,
            f"rc={args.rc} executed={len(executed_ids)} len(required)={len(req_set)}",
        )
    else:
        for k in ("② required ⊆ 执行结果且全 passed", "④ required 上 ignored=0",
                  "⑤ required 无缺席（deselected∩required=∅）", "⑥ rc=0 且 executed>0 且 executed=len(required)"):
            checks[k] = (None, "未提供 --required，跳过机检（人工核对）")
        checks["③ failed=0 且无收集错误"] = (failed == 0 and not collection_error, f"failed={failed}")

    # ---- 输出骨架 ----
    out = []
    out.append("## 机检统计（由 cargo_test_evidence.py 生成）\n")
    if args.command:
        out.append(f"- 命令原文：`{args.command}`")
    out.append(
        f"- 统计：executed={len(executed_ids)} passed={passed} failed={failed} "
        f"skipped={skipped} bench={bench} filtered_out={filtered_out} rc={args.rc}"
        + ("；**编译/收集失败**（rc!=0 且无任何 test 行）" if collection_error else "")
    )
    if summary:
        out.append(f"- libtest 汇总：{summary['result']}. {summary['passed']} passed; "
                   f"{summary['failed']} failed; {summary['ignored']} ignored")
    out.append("")
    out.append("## 判据机检")
    all_pass = True
    for name, (ok, detail) in checks.items():
        if ok is None:
            mark = "SKIP"
        elif ok:
            mark = "PASS"
        else:
            mark = "FAIL"
            all_pass = False
        out.append(f"- {mark} {name} —— {detail}")
    out.append("")
    out.append("## 全量完成表")
    out.append("")
    out.append("| nodeid | outcome |")
    out.append("|---|---|")
    for oid, outcome in outcome_map.items():
        out.append(f"| `{oid}` | {outcome} |")
    if req_set:
        out.append("")
        out.append("## 必需 nodeid 完成表")
        out.append("")
        out.append("| nodeid | outcome |")
        out.append("|---|---|")
        for oid in req:
            out.append(f"| `{oid}` | {outcome_map.get(oid, 'missing')} |")
    if args.stderr and collection_error:
        out.append("")
        out.append("## 编译错误摘录（stderr 前 8 行）")
        out.append("```")
        for line in read_lines(args.stderr):
            if "error" in line.lower():
                out.append(line)
                if sum(1 for l in out if l.startswith(("error", "E:"))) > 8:
                    break
        out.append("```")
    text = "\n".join(out)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    print(text)
    sys.exit(0 if all_pass else 1)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="mode", required=True)

    pl = sub.add_parser("list", help="解析 `cargo test --<target> -- --list` 输出 → nodeid 列表")
    pl.add_argument("--input", required=True)
    pl.add_argument("--target", default="lib", help="测试二进制名：--lib → lib；--test X → X")

    pp = sub.add_parser("parse", help="解析 `cargo test --<target> -- --exact ...` 执行输出 → junit + 骨架 + 机检")
    pp.add_argument("--input", required=True)
    pp.add_argument("--target", default="lib")
    pp.add_argument("--rc", type=int, default=0, help="该 cargo 命令的退出码")
    pp.add_argument("--required", help="必需 nodeid 文件（一行一个，# 注释）")
    pp.add_argument("--junit", help="junit XML 输出路径（等价 --junitxml 产物）")
    pp.add_argument("--out", help="骨架 markdown 输出路径（缺省打到 stdout）")
    pp.add_argument("--command", help="命令原文（回填进骨架第 3 段）")
    pp.add_argument("--stderr", help="cargo 的 stderr 文件（收集错误时摘录）")

    args = ap.parse_args()
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
    if args.mode == "list":
        cmd_list(args)
    else:
        cmd_parse(args)


if __name__ == "__main__":
    main()
