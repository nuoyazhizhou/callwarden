"""test-cw 六条件判据机检 + 证据骨架生成（skillpkg-guard / REQ-01）。

判据来源：skills/test-cw/SKILL.md §4。nodeid 表由本脚本从 junit 生成，
不手写（evidence-spec 强制要求）。
"""
import json
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

REPO = Path(r"C:/git_work/callwarden")
DESIGN = REPO / ".design/skillpkg-guard"
PYEXE = sys.executable

# ac-map.md 登记的 required nodeid（AC-01…08）
REQUIRED = {
    "AC-01": ["test_real_packages_pass"],
    "AC-02": ["test_bad_frontmatter_detected", "test_missing_frontmatter_detected",
              "test_duplicate_package_name_detected"],
    "AC-03": ["test_missing_reference_detected", "test_injected_reference_exempt",
              "test_other_missing_reference_not_exempt"],
    "AC-04": ["test_duplicate_gates_rejected"],
    "AC-05": ["test_bad_smoke_path_detected"],
    "AC-06": ["test_install_missing_injection_detected", "test_install_missing_file_detected"],
    "AC-07": ["test_usage_error_exit64"],
    "AC-08": ["test_gbk_smoke_file_reports_read_failed", "test_gbk_skill_md_does_not_traceback"],
}
TEST_FILE = "tests/test_skill_packages_guard.py"
CHANGED = ["skills/_shared/check_skill_packages.py", TEST_FILE]


def sh(args):
    return subprocess.run(args, cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace")


def git(*a):
    return sh(["git", *a]).stdout.strip()


all_nodeids = [f"{TEST_FILE}::{n}" for names in REQUIRED.values() for n in names]
print("required nodeid 数:", len(all_nodeids))

# ---- 批 A：严格 required 集（满足判据 ⑥ executed == len(required)）----
junit_a = DESIGN / "reports" / "junit-accept.xml"
env_cmd = [PYEXE, "-u", "-m", "pytest", *all_nodeids, "-q", "--no-header", f"--junitxml={junit_a}"]
import os  # noqa: E402

env = dict(os.environ, PYTHONPATH=r"C:/git_work", CW_TEST_MODE="1")
r = subprocess.run(env_cmd, cwd=str(REPO), capture_output=True, text=True,
                   encoding="utf-8", errors="replace", env=env)
rc_a = r.returncode
print("批A rc=", rc_a, "|", r.stdout.strip().splitlines()[-1] if r.stdout.strip() else "")

tree = ET.parse(junit_a)
root = tree.getroot()
cases = list(root.iter("testcase"))


def nodeid_of(tc):
    cls, name = tc.get("classname", ""), tc.get("name", "")
    path = cls.replace(".", "/") + ".py" if cls else ""
    return f"{path}::{name}"


def outcome(tc):
    for tag in ("failure", "error", "skipped"):
        if tc.find(tag) is not None:
            return tag
    return "passed"


results = {nodeid_of(tc): outcome(tc) for tc in cases}
suites = list(root.iter("testsuite"))
agg = {k: sum(int(s.get(k, 0)) for s in suites) for k in ("tests", "failures", "errors", "skipped")}
collected = agg["tests"]
executed = sum(1 for v in results.values() if v == "passed")
missing = [n for n in all_nodeids if n not in results]
notpassed = {n: results[n] for n in all_nodeids if n in results and results[n] != "passed"}

# ---- 六条件 ----
head = git("rev-parse", "HEAD")
code_rev = git("log", "-1", "--format=%H", "--", *CHANGED)
drift = git("diff", "--stat", f"{code_rev}..HEAD", "--", *CHANGED)

cond = {
    "① revision 一致且零漂移": bool(code_rev) and drift == "",
    "② required 全 passed": not missing and not notpassed,
    "③ 本批 failures/errors/collection_errors = 0": agg["failures"] == 0 and agg["errors"] == 0,
    "④ required 集 skipped/xfail = 0": not any(results.get(n) == "skipped" for n in all_nodeids),
    "⑤ deselected ∩ required = ∅": True,  # 本批未传 --deselect / -k / --deselect 来源
    "⑥ rc=0 且 collected>0 且 collected==len(required)": rc_a == 0 and collected > 0 and collected == len(all_nodeids),
}

print("\n=== 六条件 ===")
for k, v in cond.items():
    print(f"  {'PASS' if v else 'FAIL'}  {k}")
print("\n  run_revision :", head)
print("  code_revision:", code_rev)
print("  drift        :", drift or "(空)")
print("  collected    :", collected, " executed(passed):", executed, " 汇总:", agg)
if missing:
    print("  !! required 缺席:", missing)
if notpassed:
    print("  !! required 未 passed:", notpassed)

# ---- 批 B：全文件回归（含契约覆盖的非 AC 用例）----
junit_b = DESIGN / "reports" / "junit-full.xml"
rb = subprocess.run([PYEXE, "-u", "-m", "pytest", TEST_FILE, "-q", "--no-header", f"--junitxml={junit_b}"],
                    cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace", env=env)
rootb = ET.parse(junit_b).getroot()
casesb = list(rootb.iter("testcase"))
print(f"\n批B（全文件回归）rc={rb.returncode} 例数={len(casesb)} "
      f"非通过={sum(1 for c in casesb if outcome(c)!='passed')}")

verdict = all(cond.values()) and rb.returncode == 0
print("\n总判:", "PASS" if verdict else "FAIL")

out = {
    "required": REQUIRED, "all_nodeids": all_nodeids, "results": results,
    "agg": agg, "rc_a": rc_a, "rc_b": rb.returncode, "cond": cond,
    "head": head, "code_revision": code_rev, "drift": drift,
    "full_count": len(casesb), "verdict": verdict,
}
(DESIGN / "reports" / "acceptance.json").write_text(
    json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
sys.exit(0 if verdict else 1)
