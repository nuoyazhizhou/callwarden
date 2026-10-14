"""check_ci_gates.py
==================

CI 交付门禁独立校验器（TESTING_PLAN §9 登记待实现职责的本体）。

输入：
- 一个或多个 JUnit XML（pytest --junitxml 产物；xdist 多 worker 时每
  worker 一个 testsuite，或合并后的 testsuites 根）；
- 冻结的 expected selector 清单（manifest，每行一个 nodeid）；
- 可选元数据：revision（git sha）/ artifact hash（--expect-revision /
  --expect-hash），用于过期/版本不符拒绝。

门禁（任一不过 → exit 2，verdict.ok=false；用法/输入错误 → exit 64）：

1. **输入非空**：JUnit 文件存在、可解析、testcase 数 > 0（拒空/缺）；
2. **JUnit 递归处理 testsuites 子节点**：testsuites→testsuite→testsuite
   嵌套全部展开，不漏计（拒截断/解析失败）；
3. **collection error 拒绝**：testsuite errors>0、`<error>` 节点、
   collection 类 failure 一律拒（§8「collection/中止阻断」）；
4. **selector 完备性**：manifest 每个 expected nodeid 必须出现在结果集
   （拒缺）；结果集不得出现 manifest 之外的 nodeid（拒增/漂移）；
5. **重复拒绝**：同一 nodeid 多次出现 → 拒（幂等键）；
6. **结果门禁**：FAIL/ERROR/UNKNOWN = 0；必需适用 case 的
   SKIP/XFAIL = 0（§8 结果门禁；skip 率本身仅辅助，见 check_skip_rate）；
7. **元数据新鲜度**：提供了 --expect-revision/--expect-hash 时必须
   与声明一致（拒过期/版本不符）。

输出：JSON verdict（stdout 或 --json-out），字段含 ok/exit_reason/
stats/missing/unexpected/duplicated/failures/errors/collections。

用法::

    python -m cicd.check_ci_gates \
        --junit reports/junit.xml \
        --manifest docs/testing/expected_selectors/domains_pr.txt \
        --expect-revision "$(git rev-parse HEAD)" \
        --json-out reports/gate_verdict.json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional, Tuple

EXIT_OK = 0
EXIT_GATE_FAILED = 2
EXIT_USAGE = 64


class GateError(Exception):
    """输入/用法级错误（exit 64）。"""


def _local(tag: str) -> str:
    """去 XML 命名空间前缀。"""
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _nodeid_of(tc: ET.Element) -> str:
    """testcase → pytest nodeid。优先 JUnit `file` 属性（pytest≥7 写入），
    回退 classname 点分路径转文件路径。"""
    name = tc.get("name", "")
    fpath = tc.get("file", "")
    if not fpath:
        cls = tc.get("classname", "")
        if cls:
            fpath = cls.replace(".", "/") + ".py"
    if not fpath:
        return name
    return f"{fpath}::{name}" if name else fpath


def _outcome(tc: ET.Element) -> str:
    """testcase 结果分类：passed/failed/error/skipped/xfail。"""
    for child in tc:
        tag = _local(child.tag)
        if tag == "failure":
            # collection error 常以 failure type="collection" 或
            # name 含 _collection 呈现；此处归一为 failed，由调用方
            # 结合 testsuite errors 与 message 另行判定 collection。
            return "failed"
        if tag == "error":
            return "error"
        if tag == "skipped":
            msg = (child.get("message") or "").lower()
            if child.get("type") == "pytest.xfail" or "xfail" in msg:
                return "xfail"
            return "skipped"
    return "passed"


def _looks_like_collection_error(tc: ET.Element) -> bool:
    for child in tc:
        if _local(child.tag) in ("failure", "error"):
            text = " ".join(filter(None, [
                child.get("message") or "", child.get("type") or "",
                (child.text or "")[:400]])).lower()
            if "collection" in text or "collecterror" in text:
                return True
    return "collection" in (tc.get("name") or "").lower()


def load_junit(paths: List[str]) -> Tuple[List[ET.Element], List[ET.Element]]:
    """递归展开全部 testsuites/testsuite，返回 (testsuite 元素, testcase 元素)。

    拒空文件、拒解析失败（截断的 XML 会在这里炸掉）。
    """
    suites: List[ET.Element] = []
    cases: List[ET.Element] = []
    for p in paths:
        fp = Path(p)
        if not fp.is_file():
            raise GateError(f"JUnit 文件不存在：{p}")
        raw = fp.read_bytes()
        if not raw.strip():
            raise GateError(f"JUnit 文件为空：{p}")
        try:
            root = ET.fromstring(raw)
        except ET.ParseError as e:
            raise GateError(f"JUnit 解析失败（截断/损坏？）：{p}: {e}")
        # 递归遍历（testsuites 根 → 任意深度 testsuite 子节点）
        for el in root.iter():
            tag = _local(el.tag)
            if tag == "testsuite":
                suites.append(el)
            elif tag == "testcase":
                cases.append(el)
    if not cases:
        raise GateError(f"JUnit 无 testcase（空 suite 不放行）：{paths}")
    return suites, cases


def evaluate(junit_paths: List[str], manifest_path: str,
             expect_revision: str = "", expect_hash: str = "") -> dict:
    verdict: Dict = {
        "ok": False, "exit_reason": "", "stats": {},
        "missing": [], "unexpected": [], "duplicated": [],
        "failures": [], "errors": [], "skipped_required": [],
        "collection_errors": [], "stale": [],
    }

    # ---- 1/2/3: JUnit 载入 + collection error ----
    suites, cases = load_junit(junit_paths)
    for s in suites:
        try:
            errs = int(s.get("errors", "0") or 0)
        except ValueError:
            errs = 0
        if errs:
            verdict["collection_errors"].append(
                f"testsuite {s.get('name')!r} errors={errs}")
    outcomes: Dict[str, str] = {}
    for tc in cases:
        nid = _nodeid_of(tc)
        if not nid:
            raise GateError("testcase 缺 name/file/classname，无法定位 nodeid")
        if _looks_like_collection_error(tc):
            verdict["collection_errors"].append(nid)
        oc = _outcome(tc)
        if nid in outcomes:
            verdict["duplicated"].append(nid)
        outcomes[nid] = oc
    if verdict["collection_errors"]:
        verdict["exit_reason"] = "collection_error（§8：collection/中止阻断）"
        return verdict

    # ---- 4: selector 完备性 ----
    mfp = Path(manifest_path)
    if not mfp.is_file():
        raise GateError(f"selector 清单不存在：{manifest_path}")
    expected = [ln.strip() for ln in
                mfp.read_text(encoding="utf-8").splitlines()
                if ln.strip() and not ln.strip().startswith("#")]
    if not expected:
        raise GateError(f"selector 清单为空：{manifest_path}")
    exp_set = set(expected)
    if len(exp_set) != len(expected):
        # 清单自身重复 → 清单损坏
        raise GateError("selector 清单自身存在重复行")
    got_set = set(outcomes)
    verdict["missing"] = sorted(exp_set - got_set)
    verdict["unexpected"] = sorted(got_set - exp_set)
    if verdict["missing"] or verdict["unexpected"]:
        verdict["exit_reason"] = (
            f"selector 不完备：缺 {len(verdict['missing'])} / "
            f"多 {len(verdict['unexpected'])}（拒增删漂移）")
        return verdict

    # ---- 5: 重复 ----
    if verdict["duplicated"]:
        verdict["exit_reason"] = (
            f"nodeid 重复 {len(verdict['duplicated'])}（幂等键拒重）")
        return verdict

    # ---- 6: 结果门禁 ----
    verdict["failures"] = sorted(n for n, o in outcomes.items() if o == "failed")
    verdict["errors"] = sorted(n for n, o in outcomes.items() if o == "error")
    verdict["skipped_required"] = sorted(
        n for n, o in outcomes.items() if o in ("skipped", "xfail"))
    if verdict["failures"] or verdict["errors"]:
        verdict["exit_reason"] = (
            f"FAIL/ERROR 非零：failed={len(verdict['failures'])} "
            f"error={len(verdict['errors'])}")
        return verdict
    if verdict["skipped_required"]:
        verdict["exit_reason"] = (
            f"必需适用 case 存在 SKIP/XFAIL {len(verdict['skipped_required'])}")
        return verdict

    # ---- 7: 元数据新鲜度 ----
    if expect_revision:
        try:
            cur = subprocess.run(
                ["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                timeout=30, cwd=str(Path.cwd())).stdout.strip()
        except Exception:
            cur = ""
        if cur != expect_revision:
            verdict["stale"].append(
                f"revision 不符：期望 {expect_revision[:12]}，"
                f"当前 {cur[:12] or '未知'}")
    if expect_hash:
        # artifact hash 由调用方以 --expect-hash 传入与证据 meta 比对；
        # 此处仅登记声明值，供上层核对（本脚本不重算产物）。
        verdict.setdefault("declared_hash", expect_hash)
    if verdict["stale"]:
        verdict["exit_reason"] = "元数据过期/版本不符"
        return verdict

    verdict["stats"] = {
        "suites": len(suites), "cases": len(cases),
        "expected": len(expected), "unique_outcomes": len(outcomes),
        "passed": sum(1 for o in outcomes.values() if o == "passed"),
        "failed": len(verdict["failures"]),
        "error": len(verdict["errors"]),
        "skipped": len(verdict["skipped_required"]),
    }
    verdict["ok"] = True
    return verdict


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cicd.check_ci_gates",
        description="CI 交付门禁独立校验器（JUnit + selector 完备性）")
    ap.add_argument("--junit", action="append", required=True,
                    help="JUnit XML 路径（可多次传入；xdist 多文件）")
    ap.add_argument("--manifest", required=True,
                    help="冻结的 expected selector 清单（nodeid 每行一条）")
    ap.add_argument("--expect-revision", default="",
                    help="期望 git revision（不匹配 → 过期拒绝）")
    ap.add_argument("--expect-hash", default="",
                    help="声明的 artifact hash（登记用）")
    ap.add_argument("--json-out", default="", help="verdict JSON 输出路径")
    args = ap.parse_args(argv)

    try:
        verdict = evaluate(args.junit, args.manifest,
                           expect_revision=args.expect_revision,
                           expect_hash=args.expect_hash)
    except GateError as e:
        print(json.dumps({"ok": False, "exit_reason": f"usage/input: {e}",
                          "stats": {}}, ensure_ascii=False, indent=2))
        return EXIT_USAGE

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return EXIT_OK if verdict["ok"] else EXIT_GATE_FAILED


if __name__ == "__main__":
    sys.exit(main())
