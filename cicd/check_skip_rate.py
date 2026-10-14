"""check_skip_rate.py
===================

运行 skip 率统计器（TESTING_PLAN §8/§9 登记待实现职责的本体）。

口径铁律（§8 原文）：「运行 skip 率仅辅助，用**实际适用 collected nodeid
和运行结果**计算；collection 模块 skip 另列。静态 447/7133 不能称执行
skip 率。」「零 unexpected skip 硬门禁不能被总 skip≤5% 抵消。」

因此本脚本：
- **只统计运行结果里真实发生的 skip**（JUnit `<skipped>`），分母是
  collected 清单（实际适用 nodeid 集合）；
- **collection 阶段 skip 单列**（collect-only 报告里的 skip 不计入
  运行 skip 率，避免把「根本没收集到」混成「跑了但跳过」）；
- **NOT_TRIGGERED 登记**：collected 但既未运行也未跳过的 nodeid
  （空跑/中断/选择器漏跑信号），不强行触发凑全量；
- **不设硬门禁**：本脚本输出统计与登记结构（退出码恒 0，除非用法/
  输入错误 exit 64）；skip 相关的硬门禁在 check_ci_gates（必需适用
  case SKIP/XFAIL=0）。

用法::

    python -m cicd.check_skip_rate \
        --junit reports/junit.xml \
        --collected reports/collected_nodeids.txt \
        [--collect-junit reports/collect_only_junit.xml] \
        --json-out reports/skip_rate.json
"""

from __future__ import annotations

import argparse
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Dict, List, Optional

EXIT_OK = 0
EXIT_USAGE = 64


class SkipRateError(Exception):
    """输入/用法级错误（exit 64）。"""


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def _nodeid_of(tc: ET.Element) -> str:
    name = tc.get("name", "")
    fpath = tc.get("file", "")
    if not fpath:
        cls = tc.get("classname", "")
        if cls:
            fpath = cls.replace(".", "/") + ".py"
    return f"{fpath}::{name}" if fpath and name else (fpath or name)


def _is_skipped(tc: ET.Element) -> bool:
    return any(_local(c.tag) == "skipped" for c in tc)


def _is_ran(tc: ET.Element) -> bool:
    """运行过 = 有 passed/failed/error/skipped/xfail 任一终态。"""
    return any(_local(c.tag) in ("failure", "error", "skipped")
               for c in tc) or not list(tc)


def load_cases(path: str) -> List[ET.Element]:
    fp = Path(path)
    if not fp.is_file():
        raise SkipRateError(f"JUnit 文件不存在：{path}")
    raw = fp.read_bytes()
    if not raw.strip():
        raise SkipRateError(f"JUnit 文件为空：{path}")
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        raise SkipRateError(f"JUnit 解析失败（截断/损坏？）：{path}: {e}")
    return [el for el in root.iter() if _local(el.tag) == "testcase"]


def evaluate(junit_path: str, collected_path: str,
             collect_junit: str = "") -> dict:
    run_cases = load_cases(junit_path)

    cfp = Path(collected_path)
    if not cfp.is_file():
        raise SkipRateError(f"collected 清单不存在：{collected_path}")
    collected = [ln.strip() for ln in
                 cfp.read_text(encoding="utf-8").splitlines()
                 if ln.strip() and not ln.strip().startswith("#")]
    if not collected:
        raise SkipRateError(f"collected 清单为空：{collected_path}")
    collected_set = set(collected)

    skipped: List[str] = []
    ran: List[str] = []
    outside: List[str] = []
    for tc in run_cases:
        nid = _nodeid_of(tc)
        if nid not in collected_set:
            outside.append(nid)  # 跑了清单外的 → 选择器漂移信号
            continue
        (ran if _is_ran(tc) else ran).append(nid)
        if _is_skipped(tc):
            skipped.append(nid)

    ran_set = set(ran)
    skip_set = set(skipped)
    not_triggered = sorted(collected_set - ran_set - skip_set)

    # collection 阶段 skip 单列（不计入运行 skip 率）
    collection_skips: List[str] = []
    if collect_junit:
        for tc in load_cases(collect_junit):
            if _is_skipped(tc):
                collection_skips.append(_nodeid_of(tc))

    applicable = len(collected_set)
    skip_rate = (len(skip_set) / applicable) if applicable else 0.0
    return {
        "ok": True,
        "collected": applicable,
        "ran": len(ran_set),
        "skipped": len(skip_set),
        "skipped_nodeids": sorted(skip_set),
        "collection_phase_skips": collection_skips,
        "not_triggered": not_triggered,
        "outside_collected": sorted(set(outside)),
        "skip_rate": round(skip_rate, 6),
        "note": ("skip 率仅辅助指标；硬门禁见 check_ci_gates "
                 "（必需适用 case SKIP/XFAIL=0），"
                 "零 unexpected skip 不能被总 skip≤5% 抵消"),
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cicd.check_skip_rate",
        description="运行 skip 率统计（实际 collected nodeid + 运行结果口径）")
    ap.add_argument("--junit", required=True, help="运行结果 JUnit XML")
    ap.add_argument("--collected", required=True,
                    help="实际适用 collected nodeid 清单（pytest --collect-only -q）")
    ap.add_argument("--collect-junit", default="",
                    help="collect-only 阶段 JUnit（其 skip 单列，不计入运行率）")
    ap.add_argument("--json-out", default="", help="统计 JSON 输出路径")
    args = ap.parse_args(argv)

    try:
        report = evaluate(args.junit, args.collected,
                          collect_junit=args.collect_junit)
    except SkipRateError as e:
        print(json.dumps({"ok": False, "error": f"usage/input: {e}"},
                         ensure_ascii=False, indent=2))
        return EXIT_USAGE

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
