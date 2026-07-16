"""
测试矩阵 2：Semgrep 多语言静态安全扫描验证

对 testcode/repos/ 下代表性项目执行：
1. cw semgrep scan --config p/default --quick --lang <LANG>
2. 记录 findings 数量、severity 分布、rule 数量
3. 对 ERROR 级别 finding 抽样查看

输出：tests/fixtures/matrix2_semgrep_report.md
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
REPORT = REPO_ROOT / "tests" / "fixtures" / "matrix2_semgrep_report.md"
TIMEOUT_SEC = 300  # Semgrep 扫描较慢，5 分钟超时

# 每语言选 1 个代表性项目（控制总扫描时间）
REPO_LANG_MAP = {
    "flask": "python",
    "requests": "python",
    "bat": "rust",
    "ripgrep": "rust",
    "deno_std": "typescript",
    "typeorm": "typescript",
    "express": "javascript",
    "chalk": "javascript",
    "cobra": "go",
    "gin": "go",
    "guava": "java",
    "retrofit": "java",
    "kotlinx_coroutines": "kotlin",
    "ktor": "kotlin",
    "curl": "c",
    "redis": "c",
    "fmt": "cpp",
    "spdlog": "cpp",
    "Avalonia": "csharp",
    "csharplang": "csharp",
    "rubocop": "ruby",
    "sinatra": "ruby",
    "composer": "php",
    "monolog": "php",
    "Alamofire": "swift",
    "vapor": "swift",
    "cats": "scala",
    "playframework": "scala",
    "terraform_aws_security_group": "hcl",
    "terraform_aws_vpc": "hcl",
    "ecto": "elixir",
    "phoenix": "elixir",
}


def run_cw(args, timeout=TIMEOUT_SEC, cwd=None):
    """运行 cw 命令，返回 (returncode, stdout, stderr, elapsed)"""
    cmd = f"{CW} {args}"
    start = time.time()
    work_dir = cwd if cwd else str(REPO_ROOT)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=timeout, cwd=work_dir)
        elapsed = round(time.time() - start, 1)
        return r.returncode, r.stdout, r.stderr, elapsed
    except subprocess.TimeoutExpired:
        elapsed = round(time.time() - start, 1)
        return -1, "", "TIMEOUT", elapsed
    except Exception as e:
        elapsed = round(time.time() - start, 1)
        return -2, "", str(e), elapsed


def parse_semgrep_output(stdout):
    """解析 semgrep scan --quick 输出，提取 findings 数量和 severity 分布

    输出格式（JSON 或文本摘要）：
    - JSON: {"success": true, "total_findings": N, "severity_counts": {...}}
    - 文本: 包含 "Findings: N" 等摘要行
    """
    # 尝试 JSON 解析
    idx = stdout.find("{")
    if idx >= 0:
        try:
            data = json.loads(stdout[idx:])
            return {
                "total": data.get("total_findings", 0),
                "severity": data.get("severity_counts", {}),
                "rules": data.get("rules_count", 0),
                "files_scanned": data.get("files_scanned", 0),
            }
        except json.JSONDecodeError:
            pass

    # 文本解析（从 stderr/stdout 中提取关键信息）
    result = {"total": 0, "severity": {}, "rules": 0, "files_scanned": 0}
    for line in stdout.split("\n"):
        line_l = line.lower()
        if "total_findings" in line_l:
            # 形如 "  total_findings: 42"
            try:
                result["total"] = int(line.split(":")[-1].strip())
            except ValueError:
                pass
        elif "severity_counts" in line_l:
            # 后续行可能含 severity 分布
            pass
        elif "error" in line_l and ":" in line:
            try:
                val = int(line.split(":")[-1].strip())
                if val > 0:
                    result["severity"]["ERROR"] = val
            except ValueError:
                pass
        elif "warning" in line_l and ":" in line:
            try:
                val = int(line.split(":")[-1].strip())
                if val > 0:
                    result["severity"]["WARNING"] = val
            except ValueError:
                pass
        elif "info" in line_l and ":" in line:
            try:
                val = int(line.split(":")[-1].strip())
                if val > 0:
                    result["severity"]["INFO"] = val
            except ValueError:
                pass
    return result


def main():
    # 按语言分组，每语言选 1-2 个项目
    repos = sorted([d for d in REPOS_DIR.iterdir() if d.is_dir()
                    and d.name in REPO_LANG_MAP])

    results = []
    print(f"=== Matrix 2: Semgrep Scan ({len(repos)} repos) ===")
    print(f"Timeout per repo: {TIMEOUT_SEC}s\n")

    for i, repo in enumerate(repos, 1):
        name = repo.name
        lang = REPO_LANG_MAP.get(name, "?")
        ws = str(repo).replace("\\", "/")

        print(f"[{i}/{len(repos)}] {name} ({lang})...", end=" ", flush=True)

        # 运行 semgrep scan --quick（不入库，仅摘要）
        rc, out, err, elapsed = run_cw(
            f'semgrep scan --config p/default --quick --lang {lang}',
            timeout=TIMEOUT_SEC, cwd=ws
        )

        scan_ok = rc == 0
        stats = parse_semgrep_output(out) if scan_ok else {}

        result = {
            "name": name, "lang": lang, "scan_ok": scan_ok,
            "elapsed": elapsed,
            "total_findings": stats.get("total", 0),
            "severity": stats.get("severity", {}),
            "files_scanned": stats.get("files_scanned", 0),
            "error": (err[:200] if not scan_ok else ""),
        }
        results.append(result)
        status = "OK" if scan_ok else "FAIL"
        print(f"{status} {elapsed}s findings={result['total_findings']}")

    # 生成报告
    generate_report(results)
    print(f"\nReport: {REPORT}")


def generate_report(results):
    """生成 markdown 报告"""
    lines = [
        "# 测试矩阵 2：Semgrep 多语言静态安全扫描报告",
        "",
        f"> 执行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 项目数：{len(results)}",
        f"> 成功：{sum(1 for r in results if r['scan_ok'])} / 失败：{sum(1 for r in results if not r['scan_ok'])}",
        "",
        "## 扫描结果",
        "",
        "| 语言 | 项目 | 扫描耗时 | findings | ERROR | WARNING | INFO | 状态 |",
        "|------|------|----------|----------|-------|---------|------|------|",
    ]

    # 按语言排序
    lang_order = ["rust", "typescript", "javascript", "python", "kotlin", "go",
                  "java", "c", "cpp", "csharp", "ruby", "php", "swift", "scala",
                  "hcl", "elixir"]
    sorted_results = sorted(
        results,
        key=lambda r: (lang_order.index(r["lang"]) if r["lang"] in lang_order else 99,
                       r["name"])
    )

    for r in sorted_results:
        sev = r["severity"]
        status = "OK" if r["scan_ok"] else f"FAIL: {r['error'][:40]}"
        lines.append(
            f"| {r['lang']} | {r['name']} | {r['elapsed']}s | "
            f"{r['total_findings']} | {sev.get('ERROR', 0)} | "
            f"{sev.get('WARNING', 0)} | {sev.get('INFO', 0)} | {status} |"
        )

    # 汇总统计
    total_findings = sum(r["total_findings"] for r in results if r["scan_ok"])
    ok_count = sum(1 for r in results if r["scan_ok"])
    fail_count = len(results) - ok_count
    total_error = sum(r["severity"].get("ERROR", 0) for r in results if r["scan_ok"])
    total_warning = sum(r["severity"].get("WARNING", 0) for r in results if r["scan_ok"])
    total_info = sum(r["severity"].get("INFO", 0) for r in results if r["scan_ok"])

    lines.extend([
        "",
        "## 汇总统计",
        "",
        f"- 总扫描项目：{len(results)}",
        f"- 成功扫描：{ok_count}",
        f"- 失败项目：{fail_count}",
        f"- 总 findings：{total_findings}",
        f"- ERROR 级别：{total_error}",
        f"- WARNING 级别：{total_warning}",
        f"- INFO 级别：{total_info}",
        "",
        "## 语言覆盖验证",
        "",
    ])

    # 按语言分组统计
    lang_stats = {}
    for r in results:
        lang = r["lang"]
        if lang not in lang_stats:
            lang_stats[lang] = {"count": 0, "findings": 0}
        lang_stats[lang]["count"] += 1
        if r["scan_ok"]:
            lang_stats[lang]["findings"] += r["total_findings"]

    lines.append("| 语言 | 项目数 | 总 findings |")
    lines.append("|------|--------|-------------|")
    for lang in lang_order:
        if lang in lang_stats:
            s = lang_stats[lang]
            lines.append(f"| {lang} | {s['count']} | {s['findings']} |")

    REPORT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
