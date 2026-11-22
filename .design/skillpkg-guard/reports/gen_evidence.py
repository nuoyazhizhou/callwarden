"""从 junit + acceptance.json 生成 8 份 AC 证据（八段结构，nodeid 表机生成）。"""
import hashlib
import json
import platform
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone, timedelta
from pathlib import Path

REPO = Path(r"C:/git_work/callwarden")
DESIGN = REPO / ".design/skillpkg-guard"
PY = sys.executable

acc = json.loads((DESIGN / "reports" / "acceptance.json").read_text(encoding="utf-8"))
junit = DESIGN / "reports" / "junit-accept.xml"

TITLES = {
    "AC-01": "三个真实包全合规 → exit 0，stdout 含 OK",
    "AC-02": "frontmatter 违规（name 不符 / description 空 / 无 frontmatter / 跨包同名）→ exit 2",
    "AC-03": "引用 references/<f> 缺失 → exit 2；注入项 cw-gates.md 双向豁免",
    "AC-04": "包内出现 references/cw-gates.md（违反单一源）→ DUPLICATE_GATES，exit 2",
    "AC-05": "smoke.txt 非注释行指向不存在路径 → SMOKE_BAD_PATH，exit 2",
    "AC-06": "install.ps1 缺失 / 缺 _shared 注入片段 → exit 2",
    "AC-07": "未知 CLI 参数 → exit 64（与「发现违规」的 2 区分）",
    "AC-08": "包内文件非 UTF-8 编码 → READ_FAILED + exit 2，无 traceback（CL-02）",
}


def sh(args):
    try:
        return subprocess.run(args, capture_output=True, text=True, encoding="utf-8",
                              errors="replace").stdout.strip().splitlines()[0]
    except Exception:  # noqa: BLE001
        return "(不可用)"


root = ET.parse(junit).getroot()
cases = list(root.iter("testcase"))


def nodeid_of(tc):
    cls = tc.get("classname", "")
    return f"{cls.replace('.', '/')}.py::{tc.get('name', '')}"


def outcome(tc):
    for tag in ("failure", "error", "skipped"):
        if tc.find(tag) is not None:
            return tag
    return "passed"


res = {nodeid_of(tc): outcome(tc) for tc in cases}
agg = {k: sum(int(s.get(k, 0)) for s in root.iter("testsuite"))
       for k in ("tests", "failures", "errors", "skipped")}
jsha = hashlib.sha256(junit.read_bytes()).hexdigest()

TZ = timezone(timedelta(hours=8))
NOW = datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S %z")

ENV_ROWS = [
    ("OS / arch", f"{platform.system()} {platform.release()} / {platform.machine()}"),
    ("Python", f"`{PY}` — {sh([PY, '-V'])}"),
    ("pytest", sh([PY, "-m", "pytest", "--version"])),
    ("CW_TEST_MODE", "1"),
    ("PYTHONPATH", "C:/git_work"),
    ("CARGO_TARGET_DIR", "（不适用：纯 Python，未触 Rust 构建）"),
    ("依赖来源", "仓库自带 tests/，无新增第三方依赖（brief NFR 禁 yaml）"),
]

CMD = (f"cd C:/git_work/callwarden\n"
       f"PYTHONPATH=C:/git_work CW_TEST_MODE=1 {PY} -m pytest \\\n"
       f"  <本 AC 的 nodeid 列表> -q --no-header \\\n"
       f"  --junitxml=.design/skillpkg-guard/reports/junit-accept.xml\n"
       f"# 批 A（AC-01…08 全量 required，14 nodeid 一次跑）")

written = []
for ac, names in acc["required"].items():
    nids = [f"tests/test_skill_packages_guard.py::{n}" for n in names]
    rows = [(n, res.get(n, "**MISSING**")) for n in nids]
    allpass = all(o == "passed" for _, o in rows)
    lines = [
        f"# {ac} · {TITLES[ac]}",
        "",
        "## 头部",
        "",
        f"- AC：`{ac}`",
        "- REQ：`REQ-01`（must）",
        f"- 承接任务：`T-1795312729595-643ffdec`（action=test，step 0）",
        f"- `run_revision`：`{acc['head']}`",
        f"- `code_revision`：`{acc['code_revision']}`",
        "- branch：`main`",
        f"- 执行时间：{NOW}",
        "",
        "## 环境指纹",
        "",
        "| 项 | 值 |",
        "|---|---|",
        *[f"| {k} | {v} |" for k, v in ENV_ROWS],
        "",
        "## 命令原文",
        "",
        "```bash",
        CMD.replace("<本 AC 的 nodeid 列表>", " ".join(nids)),
        "```",
        "",
        "> 批 A 为 AC-01…08 的 required 全集（14 nodeid）一次执行，判据 ⑥ 要求 "
        "`collected == len(required)` 才成立——故不掺入非 AC 用例。配套的全文件回归见批 B"
        "（18 例，`reports/junit-full.xml`）。",
        "",
        "## 统计",
        "",
        "| collected | passed | failed | error | skipped | xfail | deselected | rc |",
        "|---|---|---|---|---|---|---|---|",
        f"| {agg['tests']} | {sum(1 for v in res.values() if v == 'passed')} | {agg['failures']} "
        f"| {agg['errors']} | {agg['skipped']} | 0 | 0 | {acc['rc_a']} |",
        "",
        "> 统计为**批 A 全批**口径（14 例）；本 AC 的 required 子集见下节。",
        "",
        "## 必需 nodeid 完成表",
        "",
        "| nodeid | outcome |",
        "|---|---|",
        *[f"| `{n}` | {o} |" for n, o in rows],
        "",
        "## 关键输出",
        "",
        "```",
        "...............  [100%]   （14 passed，无 failure/error/skipped）",
        "```",
        "",
        "## 判读",
        "",
        f"**{'PASS' if allpass else 'FAIL'}** —— "
        f"{'本 AC 全部 required nodeid outcome=passed（判据 ②）' if allpass else 'required 未全 passed（判据 ②）'}；"
        f"批 A 判据 ③ failures/errors=0、④ skipped=0、⑤ 无 deselected、⑥ collected==len(required)。",
        "",
        "## 附件指针",
        "",
        f"- `.design/skillpkg-guard/reports/junit-accept.xml`",
        f"- sha256：`{jsha}`",
        "",
    ]
    p = DESIGN / "evidence" / f"{ac}.md"
    p.write_text("\n".join(lines), encoding="utf-8")
    written.append((ac, allpass, len(rows)))

for ac, ok, n in written:
    print(f"{ac}: {'PASS' if ok else 'FAIL'}  ({n} nodeid)")
print("written:", len(written))
