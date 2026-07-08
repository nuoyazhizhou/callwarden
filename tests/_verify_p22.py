"""P22 验证：抽样几个项目，用 cw 的 build_full_graph 验证 P21 自动 ignore 效果"""
from __future__ import annotations
import os, sys, time, tempfile

_PKG_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_ROOT not in sys.path:
    sys.path.insert(0, _PKG_ROOT)

from callwarden.config import scan_subprojects
from callwarden.db.db import CodeGraphDB

REPOS_DIR = os.path.join(_PKG_ROOT, "testcode", "repos")

# 扫描顶层项目（max_depth=1，只看每个仓库根目录的清单文件）
projects = scan_subprojects(REPOS_DIR, max_depth=1)
print(f"顶层项目数: {len(projects)}")

# 按语言分组，每语言抽 2 个
by_lang: dict = {}
for p in projects:
    by_lang.setdefault(p["lang"], []).append(p)

# 抽样
samples = []
for lang in ["go", "rust", "javascript", "python", "java"]:
    if lang in by_lang:
        samples.extend(by_lang[lang][:2])

print(f"抽样 {len(samples)} 个项目验证 P21 自动 ignore:\n")

# 临时 DB 目录
tmp_dir = tempfile.mkdtemp(prefix="cw_p22_")

for p in samples:
    db_path = os.path.join(tmp_dir, p["name"].replace("/", "_") + ".db")
    db = CodeGraphDB(db_path=db_path, workspace_root=p["root"])
    ws = db.register_workspace(p["name"], p["root"])
    db.set_active_workspace(ws)

    t0 = time.perf_counter()
    db.build_full_graph(force=False)
    elapsed = time.perf_counter() - t0

    stats = db.get_stats()
    files = stats.get("file_count", 0)
    symbols = stats.get("symbol_count", 0)

    print(f"  {p['lang']:10s}  {p['name']:40s}  files={files:5d}  symbols={symbols:6d}  {elapsed:.1f}s")
    db.close()

# 清理临时 DB
import shutil
shutil.rmtree(tmp_dir, ignore_errors=True)
