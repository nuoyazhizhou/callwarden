"""R15 探针：对比 Rust signature/visibility 与 golden expected"""
import json
import sys
import os
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
os.environ.setdefault("PYTHONUTF8", "1")

from callwarden_core import parse_file_lang

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"
TMP_DIR = Path(__file__).resolve().parent / "_tmp_probe"
TMP_DIR.mkdir(exist_ok=True)

sig_ok = 0
sig_fail = 0
vis_ok = 0
vis_fail = 0
details = []

for json_path in sorted(GOLDEN_DIR.glob("*.json")):
    data = json.loads(json_path.read_text(encoding="utf-8"))
    lang = data.get("language")
    if not lang:
        continue
    sample = data.get("sample_source", "")
    expected_syms = data.get("expected", {}).get("symbols", [])

    ext = {"python": "py", "rust": "rs", "go": "go", "java": "java",
           "typescript": "ts", "javascript": "js", "ruby": "rb", "php": "php",
           "scala": "scala", "csharp": "cs", "cpp": "cpp", "kotlin": "kt",
           "swift": "swift", "elixir": "ex", "hcl": "hcl", "c": "c"}.get(lang, "txt")
    sample_path = TMP_DIR / f"sample_{lang}.{ext}"
    sample_path.write_text(sample, encoding="utf-8")

    try:
        rs_result = parse_file_lang(str(sample_path), f"probe.{lang}", lang)
    except Exception as e:
        details.append(f"[{lang}] parse error: {e}")
        sig_fail += len(expected_syms)
        vis_fail += len(expected_syms)
        continue

    rs_syms = rs_result.get("symbols", [])
    rs_map = {(s.get("name", ""), s.get("start_line", 0)): s for s in rs_syms}

    for exp in expected_syms:
        key = (exp["name"], exp["line_start"])
        exp_sig = exp.get("signature", "")
        exp_vis = exp.get("visibility", "")
        rs_sym = rs_map.get(key)
        if rs_sym is None:
            sig_fail += 1
            vis_fail += 1
            details.append(f"[{lang}] {exp['name']}:{exp['line_start']} MISSING in Rust")
            continue
        act_sig = rs_sym.get("signature", "")
        act_vis = rs_sym.get("visibility", "")
        if act_sig == exp_sig:
            sig_ok += 1
        else:
            sig_fail += 1
            details.append(f"[{lang}] SIG {exp['name']}:{exp['line_start']} exp={exp_sig!r} act={act_sig!r}")
        if act_vis == exp_vis:
            vis_ok += 1
        else:
            vis_fail += 1
            details.append(f"[{lang}] VIS {exp['name']}:{exp['line_start']} exp={exp_vis!r} act={act_vis!r}")

print(f"=== signature: {sig_ok} ok, {sig_fail} fail ===")
print(f"=== visibility: {vis_ok} ok, {vis_fail} fail ===")
print()
for d in details:
    print(d)
