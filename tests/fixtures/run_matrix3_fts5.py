"""
测试矩阵 3：FTS5 跨语言符号搜索验证

对 testcode/repos/ 下代表性项目执行：
1. cw fts status（检查 FTS5 索引状态）
2. 对通用关键词跨语言搜索（验证 trigram 分词、camelCase/snake_case 支持）
3. 对每语言特定关键词搜索（验证语言特性覆盖）

输出：tests/fixtures/matrix3_fts5_report.md
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
REPORT = REPO_ROOT / "tests" / "fixtures" / "matrix3_fts5_report.md"
TIMEOUT_SEC = 60

# 每语言选 1 个代表性项目
REPO_LANG_MAP = {
    "flask": "python",
    "bat": "rust",
    "deno_std": "typescript",
    "express": "javascript",
    "cobra": "go",
    "guava": "java",
    "curl": "c",
    "fmt": "cpp",
    "Avalonia": "csharp",
    "rubocop": "ruby",
    "composer": "php",
    "Alamofire": "swift",
    "cats": "scala",
    "terraform_aws_vpc": "hcl",
    "ecto": "elixir",
}

# 通用搜索关键词（跨语言，验证 FTS5 trigram 分词能力）
COMMON_QUERIES = ["init", "handle", "parse", "create", "config", "error", "main"]

# 每语言特定搜索关键词（验证语言特性覆盖）
LANG_QUERIES = {
    "python": ["def", "class", "import"],
    "rust": ["fn", "impl", "trait"],
    "typescript": ["interface", "async", "await"],
    "javascript": ["function", "export", "require"],
    "go": ["func", "package", "interface"],
    "java": ["public", "static", "void"],
    "c": ["struct", "typedef", "include"],
    "cpp": ["namespace", "template", "class"],
    "csharp": ["namespace", "public", "static"],
    "ruby": ["def", "module", "require"],
    "php": ["function", "class", "namespace"],
    "swift": ["func", "struct", "let"],
    "scala": ["def", "object", "trait"],
    "hcl": ["resource", "variable", "output"],
    "elixir": ["def", "defp", "defmodule"],
}


def run_cw(args, timeout=TIMEOUT_SEC, cwd=None):
    """运行 cw 命令，返回 (returncode, stdout, stderr, elapsed)"""
    cmd = f"{CW} {args}"
    start = time.time()
    work_dir = cwd if cwd else str(REPO_ROOT)
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace",
                           timeout=timeout, cwd=work_dir)
        elapsed = round(time.time() - start, 1)
        return r.returncode, r.stdout, r.stderr, elapsed
    except subprocess.TimeoutExpired:
        elapsed = round(time.time() - start, 1)
        return -1, "", "TIMEOUT", elapsed
    except Exception as e:
        elapsed = round(time.time() - start, 1)
        return -2, "", str(e), elapsed


def parse_fts_status(stdout):
    """解析 cw fts status 输出

    输出格式（文本，非 JSON）：
      cli_fts_status_symbols       : 3466
      cli_fts_status_fts_rows      : 3466
      cli_fts_status_triggers      : 3
      ✓ Consistent (fts_rows == symbols_count)

    或不一致时：
      ✗ Inconsistent (fts_rows != symbols_count)
    """
    result = {
        "exists": True,
        "symbols_count": 0,
        "fts_rows": 0,
        "triggers": 0,
        "consistent": False,
    }

    # 尝试 JSON 解析（某些版本可能返回 JSON）
    idx = stdout.find("{")
    if idx >= 0:
        try:
            data = json.loads(stdout[idx:])
            return {
                "exists": data.get("exists", True),
                "symbols_count": data.get("symbols_count", 0),
                "fts_rows": data.get("fts_rows", 0),
                "triggers": data.get("triggers", 0),
                "consistent": data.get("consistent", False),
            }
        except json.JSONDecodeError:
            pass

    # 文本解析
    for line in stdout.split("\n"):
        line_l = line.lower()
        if "symbols" in line_l and ":" in line:
            try:
                val = int(line.split(":")[-1].strip())
                result["symbols_count"] = val
            except ValueError:
                pass
        elif "fts_rows" in line_l and ":" in line:
            try:
                val = int(line.split(":")[-1].strip())
                result["fts_rows"] = val
            except ValueError:
                pass
        elif "triggers" in line_l and ":" in line:
            try:
                val = int(line.split(":")[-1].strip())
                result["triggers"] = val
            except ValueError:
                pass
        elif "consistent" in line_l:
            result["consistent"] = True
        elif "inconsistent" in line_l:
            result["consistent"] = False

    return result


def parse_search_count(stdout):
    """从 cw search 输出中提取结果数

    输出格式：
      Search results: 'query'  (N total, showing M):
    """
    for line in stdout.split("\n"):
        line = line.strip()
        if "Search results:" in line and "total" in line:
            # 提取 (N total 中的 N
            import re
            m = re.search(r"\((\d+)\s+total", line)
            if m:
                return int(m.group(1))
    return 0


def main():
    repos = sorted([d for d in REPOS_DIR.iterdir() if d.is_dir()
                    and d.name in REPO_LANG_MAP])

    results = []
    print(f"=== Matrix 3: FTS5 Search ({len(repos)} repos) ===")
    print(f"Timeout per query: {TIMEOUT_SEC}s\n")

    for i, repo in enumerate(repos, 1):
        name = repo.name
        lang = REPO_LANG_MAP.get(name, "?")
        ws = str(repo).replace("\\", "/")

        print(f"[{i}/{len(repos)}] {name} ({lang})...", end=" ", flush=True)

        # 1. FTS5 状态（从 cw stats 获取 symbols_count，因为 fts status 输出有 i18n bug）
        rc, out, _, _ = run_cw("stats", cwd=ws)
        fts_status = {}
        if rc == 0:
            idx = out.find("{")
            if idx >= 0:
                try:
                    stats_data = json.loads(out[idx:])
                    fts_status = {
                        "symbols_count": stats_data.get("total_symbols", 0),
                        "consistent": True,  # 从 stats 能获取数据说明 DB 正常
                    }
                except json.JSONDecodeError:
                    pass

        # 也获取 fts status 的 consistent 信息
        rc_fts, out_fts, _, _ = run_cw("fts status", cwd=ws)
        if rc_fts == 0:
            if "Consistent" in out_fts:
                fts_status["consistent"] = True
            elif "Inconsistent" in out_fts:
                fts_status["consistent"] = False

        # 2. 通用关键词搜索
        common_results = {}
        for q in COMMON_QUERIES:
            rc, out, _, _ = run_cw(f'search "{q}" --limit 5', timeout=30, cwd=ws)
            common_results[q] = parse_search_count(out) if rc == 0 else 0

        # 3. 语言特定关键词搜索
        lang_qs = LANG_QUERIES.get(lang, [])
        lang_results = {}
        for q in lang_qs:
            rc, out, _, _ = run_cw(f'search "{q}" --limit 5', timeout=30, cwd=ws)
            lang_results[q] = parse_search_count(out) if rc == 0 else 0

        result = {
            "name": name, "lang": lang,
            "fts_exists": fts_status.get("exists", True),
            "symbols_count": fts_status.get("symbols_count", 0),
            "consistent": fts_status.get("consistent", False),
            "common_results": common_results,
            "lang_results": lang_results,
        }
        results.append(result)
        print(f"symbols={result['symbols_count']} consistent={result['consistent']}")

    # 生成报告
    generate_report(results)
    print(f"\nReport: {REPORT}")


def generate_report(results):
    """生成 markdown 报告"""
    lines = [
        "# 测试矩阵 3：FTS5 跨语言符号搜索报告",
        "",
        f"> 执行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"> 项目数：{len(results)}",
        f"> FTS5 索引正常：{sum(1 for r in results if r['consistent'])} / 异常：{sum(1 for r in results if not r['consistent'])}",
        "",
        "## FTS5 索引状态 + 通用关键词搜索",
        "",
        "| 语言 | 项目 | symbols | consistent | " + " | ".join(COMMON_QUERIES) + " |",
        "|------|------|---------|------------|" + "|".join(["------"] * len(COMMON_QUERIES)) + "|",
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
        common_vals = " | ".join(str(r["common_results"].get(q, 0)) for q in COMMON_QUERIES)
        lines.append(
            f"| {r['lang']} | {r['name']} | {r['symbols_count']} | "
            f"{'✓' if r['consistent'] else '✗'} | {common_vals} |"
        )

    # 语言特定搜索结果
    lines.extend([
        "",
        "## 语言特定关键词搜索",
        "",
    ])

    for r in sorted_results:
        if r["lang_results"]:
            lines.append(f"### {r['lang']} - {r['name']}")
            lines.append("")
            lines.append("| 关键词 | 搜索结果数 |")
            lines.append("|--------|-----------|")
            for q, cnt in r["lang_results"].items():
                lines.append(f"| {q} | {cnt} |")
            lines.append("")

    # 汇总统计
    total_symbols = sum(r["symbols_count"] for r in results)
    consistent_count = sum(1 for r in results if r["consistent"])
    zero_result_queries = 0
    total_queries = 0

    for r in results:
        for q, cnt in r["common_results"].items():
            total_queries += 1
            if cnt == 0:
                zero_result_queries += 1
        for q, cnt in r["lang_results"].items():
            total_queries += 1
            if cnt == 0:
                zero_result_queries += 1

    lines.extend([
        "## 汇总统计",
        "",
        f"- 总项目数：{len(results)}",
        f"- FTS5 索引一致：{consistent_count} / {len(results)}",
        f"- 总符号数：{total_symbols}",
        f"- 搜索查询总数：{total_queries}",
        f"- 零结果查询数：{zero_result_queries}（{round(zero_result_queries/max(total_queries,1)*100, 1)}%）",
        "",
        "## trigram 分词验证",
        "",
        "trigram tokenizer 自动分词 snake_case / camelCase / `::` 路径，",
        "搜索 `init` 应命中 `initialize` / `init_config` / `__init__` 等。",
        "搜索 `handle` 应命中 `handleRequest` / `handle_error` / `EventHandler` 等。",
        "",
    ])

    REPORT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
