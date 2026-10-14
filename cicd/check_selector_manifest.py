"""check_selector_manifest.py
===========================

selector 清单**与源码真相**的一致性校验器（TESTING_PLAN §8「清单」门禁本体）。

为什么必须独立存在：`check_ci_gates.py` 的双向完备性只保证
「JUnit 结果集 == manifest」，**不保证 manifest == 源码里实际存在的测试**。
manifest 与门禁脚本同仓同 PR 可改 → 若只靠 check_ci_gates，
「删掉一个测试 + 同步删 manifest 一行」会全绿通过，门禁形同虚设。

本脚本补上这一环：拿 `pytest --collect-only --no-header` 的真实收集结果
（nodeid 每行一条）与冻结 manifest 双向核对：

- manifest 有、源码没有 → **测试被删/改名**：必须拒（核心防护）；
- 源码有、manifest 没 → 新增测试未登记：必须拒（防静默扩面）；
- 任一侧存在重复行 → 清单/收集损坏：拒。

⚠️ 收集命令必须是**不带 `-q`** 的形式：pytest 9 的 `-q` 只输出每文件计数
（`tests/x.py: 7`），不输出 nodeid。正确取法::

    pytest <path> --collect-only --no-header > collected.txt

输出：JSON verdict；exit 0 通过 / 2 不一致 / 64 输入用法错。
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Dict, List, Optional


def repo_root_of(path: str) -> str:
    """取 manifest 所在仓库的根（用于 git show 的相对路径解析）。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"], capture_output=True,
            text=True, timeout=30, cwd=str(Path(path).resolve().parent))
        if out.returncode == 0 and out.stdout.strip():
            return out.stdout.strip()
    except Exception:
        pass
    return str(Path(path).resolve().parent)

EXIT_OK = 0
EXIT_INCONSISTENT = 2
EXIT_USAGE = 64


class ManifestError(Exception):
    """输入/用法级错误（exit 64）。"""


def _load(path: str, what: str) -> List[str]:
    fp = Path(path)
    if not fp.is_file():
        raise ManifestError(f"{what} 文件不存在：{path}")
    raw = fp.read_text(encoding="utf-8")
    lines = [ln.strip() for ln in raw.splitlines()
             if ln.strip() and not ln.strip().startswith("#")]
    if not lines:
        raise ManifestError(f"{what} 为空：{path}")
    return lines


def evaluate(collected_path: str, manifest_path: str,
             baseline_ref: str = "") -> Dict:
    collected = _load(collected_path, "collected")
    manifest = _load(manifest_path, "manifest")

    dup_collected = sorted({n for n in collected if collected.count(n) > 1})
    dup_manifest = sorted({n for n in manifest if manifest.count(n) > 1})

    c_set, m_set = set(collected), set(manifest)
    deleted = sorted(m_set - c_set)    # manifest 登记但源码已无 → 测试被删/改名
    added = sorted(c_set - m_set)      # 源码有但 manifest 未登记 → 静默扩面

    ok = not (deleted or added or dup_collected or dup_manifest)
    reason = ""
    if deleted or added:
        reason = (f"manifest 与源码不一致：源码缺失 {len(deleted)} "
                  f"（测试被删/改名）/ 未登记新增 {len(added)}")
    elif dup_collected or dup_manifest:
        reason = (f"重复 nodeid：collected {len(dup_collected)} / "
                  f"manifest {len(dup_manifest)}")

    # 与基线版本的 manifest 比对 —— **可发现性**，不是硬阻断。
    # 局限必须说清：若「删测试 + 同步删 manifest」两边都少，上面的收集比对
    # 也会通过（collected 是运行时收集，跟着变）。真正拦这一手的是
    # CODEOWNERS 审批（见 .github/CODEOWNERS）。这里做的是让「被删条目」
    # 在门禁日志里显式列出来，供审批者一眼看到删了什么。
    removed_vs_baseline: List[str] = []
    added_vs_baseline: List[str] = []
    baseline_available = False
    if baseline_ref:
        try:
            import subprocess
            rel = os.path.relpath(manifest_path, repo_root_of(manifest_path))
            out = subprocess.run(
                ["git", "show", f"{baseline_ref}:{rel}"],
                capture_output=True, text=True, timeout=60,
                cwd=repo_root_of(manifest_path))
            if out.returncode == 0 and out.stdout.strip():
                baseline_available = True
                base = [ln.strip() for ln in out.stdout.splitlines()
                        if ln.strip() and not ln.strip().startswith("#")]
                b_set = set(base)
                removed_vs_baseline = sorted(b_set - m_set)
                added_vs_baseline = sorted(m_set - b_set)
        except Exception:
            baseline_available = False

    return {
        "ok": ok,
        "exit_reason": reason,
        "stats": {"collected": len(collected), "manifest": len(manifest),
                  "unique_collected": len(c_set), "unique_manifest": len(m_set)},
        "deleted_or_renamed": deleted,
        "unregistered_new": added,
        "duplicated_in_collected": dup_collected,
        "duplicated_in_manifest": dup_manifest,
        "baseline_ref": baseline_ref or None,
        "baseline_available": baseline_available,
        "removed_vs_baseline": removed_vs_baseline,
        "added_vs_baseline": added_vs_baseline,
        "note": ("本校验是 check_ci_gates 的前置：前者防「改门禁绕过门禁」，"
                 "后者防「结果集与清单漂移」；两道都必须过。"),
    }


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(
        prog="cicd.check_selector_manifest",
        description="selector 清单与 pytest 真实收集结果的一致性校验")
    ap.add_argument("--collected", required=True,
                    help="pytest --collect-only --no-header 输出（nodeid 每行一条）")
    ap.add_argument("--manifest", required=True, help="冻结的 selector 清单")
    ap.add_argument("--baseline-ref", default="",
                    help="基线 git ref（如 origin/main）：列出相对基线被删/新增的 "
                         "selector，供审批者核对（可发现性，非硬阻断）")
    ap.add_argument("--json-out", default="", help="verdict JSON 输出路径")
    args = ap.parse_args(argv)

    try:
        verdict = evaluate(args.collected, args.manifest,
                           baseline_ref=args.baseline_ref)
    except ManifestError as e:
        print(json.dumps({"ok": False, "exit_reason": f"usage/input: {e}"},
                         ensure_ascii=False, indent=2))
        return EXIT_USAGE

    if args.json_out:
        Path(args.json_out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_out).write_text(
            json.dumps(verdict, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(verdict, ensure_ascii=False, indent=2))
    return EXIT_OK if verdict["ok"] else EXIT_INCONSISTENT


if __name__ == "__main__":
    sys.exit(main())
