"""
测试矩阵 1：解析器 + 调用链批量验证

对 testcode/repos/ 下每个开源项目执行：
1. cw --workspace <path> refresh --all（带超时）
2. cw --workspace <path> --stats（提取符号/calls/文件数）
3. 抽样：取第一个符号，跑 callers + callees

输出：tests/fixtures/matrix1_parser_report.md
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path("c:/git_work/callwarden")
REPOS_DIR = REPO_ROOT / "testcode" / "repos"
CW = sys.executable + " " + str(REPO_ROOT / "cw.py")
REPORT = REPO_ROOT / "tests" / "fixtures" / "matrix1_parser_report.md"
TIMEOUT_SEC = 180  # 单项目最大解析时间

# 16 语言期望扩展名（用于验证解析覆盖）
LANG_EXTS = {
    "rust": [".rs"], "typescript": [".ts", ".tsx"], "javascript": [".js", ".jsx"],
    "python": [".py"], "kotlin": [".kt", ".kts"], "go": [".go"],
    "java": [".java"], "c": [".c", ".h"], "cpp": [".cpp", ".cc", ".cxx", ".hpp", ".h"],
    "csharp": [".cs"], "ruby": [".rb"], "php": [".php"],
    "swift": [".swift"], "scala": [".scala"], "hcl": [".tf", ".tfvars"],
    "elixir": [".ex", ".exs"],
}

# 从 realworld_repos.json 读 lang→name 映射
def load_manifest():
    p = REPO_ROOT / "tests" / "fixtures" / "realworld_repos.json"
    data = json.loads(p.read_text(encoding="utf-8"))
    return {r["name"]: r["lang"] for r in data["repos"]}

def run_cw(args, timeout=TIMEOUT_SEC, cwd=None):
    """运行 cw 命令，返回 (returncode, stdout, stderr, elapsed)"""
    cmd = f"{CW} {args}"
    start = time.time()
    work_dir = cwd if cwd else str(REPO_ROOT)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout, cwd=work_dir)
        elapsed = round(time.time() - start, 1)
        return r.returncode, r.stdout, r.stderr, elapsed
    except subprocess.TimeoutExpired:
        elapsed = round(time.time() - start, 1)
        return -1, "", "TIMEOUT", elapsed
    except Exception as e:
        elapsed = round(time.time() - start, 1)
        return -2, "", str(e), elapsed

def parse_stats(stdout):
    """从 --stats 的 JSON 输出中提取关键字段"""
    # 找 JSON 开始位置
    idx = stdout.find("{")
    if idx < 0:
        return {}
    try:
        data = json.loads(stdout[idx:])
    except json.JSONDecodeError:
        return {}
    return {
        "symbols": data.get("unique_symbol_contents", 0),
        "calls": data.get("total_call_versions", 0),
        "files": data.get("current_files", 0),
        "symbol_links": data.get("total_file_symbol_links", 0),
    }

def get_first_symbol(workspace):
    """获取第一个符号的 qualified_name（用 search 子命令）

    search 输出格式：
      Search results: 'a'  (1 total, showing 1):
        [  1] depth=  0 [ ] fn       docs.examples.10-at-a-time.write_cb
               docs/examples/10-at-a-time.c:85
    qualified_name 是数据行末尾的 token（kind 之后）。
    """
    rc, out, err, _ = run_cw('search "a" --limit 1', timeout=30, cwd=workspace)
    if rc != 0 or not out.strip():
        return None
    for line in out.split("\n"):
        line = line.rstrip()
        # 匹配数据行：以空格 + [ 开头，含 depth= 标记
        if "depth=" in line and "[" in line and "]" in line:
            tokens = line.split()
            # 数据行 tokens: [', '1]', 'depth=', '0', '[', ']', 'fn', 'qualified.name']
            # qualified_name 是最后一个 token
            if len(tokens) >= 2:
                qname = tokens[-1]
                # 过滤掉非符号行（如 "results:" 等）
                if qname and "." in qname and not qname.startswith("Search"):
                    return qname
    return None

def test_callers_callees(workspace, qname):
    """对给定符号跑 callers + callees

    输出格式：
      Functions calling X (N):   或   Functions called by X (N):
    从首行的 (N) 提取计数，避免逐行统计的误差。
    """
    if not qname:
        return {"callers": 0, "callees": 0}
    import re
    # callees
    rc, out, _, _ = run_cw(f'callees "{qname}"', timeout=30, cwd=workspace)
    callee_count = 0
    if rc == 0:
        m = re.search(r"\((\d+)\)", out.split("\n", 1)[0] if out else "")
        if m:
            callee_count = int(m.group(1))
    # callers（留 1 秒间隔避免 DB 锁冲突）
    rc, out, _, _ = run_cw(f'callers "{qname}"', timeout=30, cwd=workspace)
    caller_count = 0
    if rc == 0:
        m = re.search(r"\((\d+)\)", out.split("\n", 1)[0] if out else "")
        if m:
            caller_count = int(m.group(1))
    return {"callees": callee_count, "callers": caller_count}

def main():
    lang_map = load_manifest()
    repos = sorted([d for d in REPOS_DIR.iterdir() if d.is_dir()])

    results = []
    print(f"=== Matrix 1: Parser + Call Chain ({len(repos)} repos) ===")
    print(f"Timeout per repo: {TIMEOUT_SEC}s\n")

    for i, repo in enumerate(repos, 1):
        name = repo.name
        lang = lang_map.get(name, "?")
        ws = str(repo).replace("\\", "/")

        print(f"[{i}/{len(repos)}] {name} ({lang})...", end=" ", flush=True)

        # 1. refresh（cwd 设为项目目录，cw 自动探测 workspace）
        rc, out, err, elapsed = run_cw("refresh --all", cwd=ws)
        refresh_ok = rc == 0

        # 2. stats
        rc2, out2, _, _ = run_cw("stats", timeout=30, cwd=ws)
        stats = parse_stats(out2) if rc2 == 0 else {}

        # 3. 抽样 callers/callees
        qname = get_first_symbol(ws) if stats.get("symbols", 0) > 0 else None
        cc = test_callers_callees(ws, qname) if qname else {"callers": 0, "callees": 0}

        result = {
            "name": name, "lang": lang, "refresh_ok": refresh_ok,
            "elapsed": elapsed, "symbols": stats.get("symbols", 0),
            "calls": stats.get("calls", 0), "files": stats.get("files", 0),
            "sample_qname": qname or "", "callers": cc.get("callers", 0),
            "callees": cc.get("callees", 0), "error": err[:200] if not refresh_ok else "",
        }
        results.append(result)
        status = "OK" if refresh_ok else "FAIL"
        print(f"{status} {elapsed}s syms={result['symbols']} calls={result['calls']}")

    # 生成报告
    generate_report(results, lang_map)
    print(f"\nReport: {REPORT}")

def generate_report(results, lang_map):
    """生成 markdown 报告"""
    lines = [
        "# 测试矩阵 1：解析器 + 调用链验证报告",
        "",
        f"> 执行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 项目数：{len(results)}",
        f"> 成功：{sum(1 for r in results if r['refresh_ok'])} / 失败：{sum(1 for r in results if not r['refresh_ok'])}",
        "",
        "## 按语言汇总",
        "",
        "| 语言 | 项目 | 文件数 | 符号数 | 调用数 | 解析耗时 | 状态 | 抽样符号 | callers | callees |",
        "|------|------|--------|--------|--------|----------|------|----------|---------|---------|",
    ]

    # 按语言排序
    lang_order = ["rust","typescript","javascript","python","kotlin","go","java","c","cpp","csharp","ruby","php","swift","scala","hcl","elixir"]
    sorted_results = sorted(results, key=lambda r: (lang_order.index(r["lang"]) if r["lang"] in lang_order else 99, r["name"]))

    for r in sorted_results:
        status = "OK" if r["refresh_ok"] else f"FAIL: {r['error'][:40]}"
        qname = r["sample_qname"][:30] if r["sample_qname"] else "-"
        lines.append(f"| {r['lang']} | {r['name']} | {r['files']} | {r['symbols']} | {r['calls']} | {r['elapsed']}s | {status} | {qname} | {r['callers']} | {r['callees']} |")

    # 汇总统计
    total_syms = sum(r["symbols"] for r in results if r["refresh_ok"])
    total_calls = sum(r["calls"] for r in results if r["refresh_ok"])
    total_files = sum(r["files"] for r in results if r["refresh_ok"])
    ok_count = sum(1 for r in results if r["refresh_ok"])
    fail_count = len(results) - ok_count

    lines.extend([
        "",
        "## 汇总统计",
        "",
        f"- 总文件数：{total_files}",
        f"- 总符号数：{total_syms}",
        f"- 总调用数：{total_calls}",
        f"- 成功项目：{ok_count}/{len(results)}",
        f"- 失败项目：{fail_count}",
        "",
        "## 语言覆盖验证",
        "",
    ])

    # 语言覆盖验证
    lang_coverage = {}
    for r in results:
        if r["refresh_ok"]:
            lang_coverage.setdefault(r["lang"], []).append(r)

    for lang in lang_order:
        repos = lang_coverage.get(lang, [])
        if repos:
            total_s = sum(r["symbols"] for r in repos)
            lines.append(f"- {lang}: {len(repos)} 项目, {total_s} 符号")
        else:
            lines.append(f"- {lang}: **缺失**")

    REPORT.write_text("\n".join(lines), encoding="utf-8")

if __name__ == "__main__":
    main()
