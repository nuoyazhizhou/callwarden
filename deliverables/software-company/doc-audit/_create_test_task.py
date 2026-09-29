import subprocess, os, sys, datetime, json

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
PY = os.environ.get("PYTHON", sys.executable)
ts = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
title = f"DOC-AUDIT-{ts}"
steps = json.dumps([{"action": "annotate", "target_file": "deliverables/software-company/doc-audit/audit_probe.txt"}])

env = dict(os.environ)
env["PYTHONUTF8"] = "1"
env["PYTHONIOENCODING"] = "utf-8"

proc = subprocess.run(
    [PY, os.path.join(REPO, "cw.py"), "task", "create",
     "--title", title,
     "--desc", "文档实现一致性审计写操作实测沙盒(Task5-7),不承载真实交付",
     "--steps", steps],
    cwd=REPO, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=180,
)
print("TITLE:", title)
print("RC:", proc.returncode)
print("STDOUT:\n", proc.stdout)
print("STDERR:\n", proc.stderr)
# 保存 title 供后续 task show 查 task_id
with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "test_task_title.txt"), "w", encoding="utf-8") as f:
    f.write(title)
