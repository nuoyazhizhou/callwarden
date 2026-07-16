"""
测试矩阵 4：resolved_edges + L5 include_path/sysroot 解析验证

对 testcode/repos/ 下的 C/C++ 项目执行：
1. 注册 build context（手动指定 --includes 模拟 include_path）
2. cw build-context resolve（计算 resolved_edges，含 L5 include_path/sysroot 解析）
3. 查询 resolved_edges 统计，验证 resolution_method 分布

输出：tests/fixtures/matrix4_resolved_edges_report.md
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
REPORT = REPO_ROOT / "tests" / "fixtures" / "matrix4_resolved_edges_report.md"
TIMEOUT_SEC = 120

# C/C++ 项目（L5 include_path/sysroot 解析的主要场景）
CC_REPOS = ["curl", "redis", "fmt", "spdlog"]


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


def get_workspace_id(cwd):
    """获取当前 workspace 的 ID"""
    rc, out, _, _ = run_cw("workspace list", timeout=30, cwd=cwd)
    if rc != 0:
        return None
    # 解析输出格式：[1] curl [active]
    # 查找含 [active] 标记的行，提取 [N] 中的数字
    import re
    for line in out.split("\n"):
        if "[active]" in line or "*" in line:
            # 提取 [N] 中的数字
            m = re.search(r"\[(\d+)\]", line)
            if m:
                return int(m.group(1))
    # 如果没有 [active]，取第一个 [N] 行
    for line in out.split("\n"):
        m = re.search(r"\[(\d+)\]", line)
        if m:
            return int(m.group(1))
    return None


def parse_resolve_output(stdout):
    """解析 build-context resolve 输出"""
    idx = stdout.find("{")
    if idx < 0:
        return {}
    try:
        data = json.loads(stdout[idx:])
        return {
            "count": data.get("count", 0),
            "source": data.get("source", ""),
            "skipped": data.get("skipped", 0),
        }
    except json.JSONDecodeError:
        return {}


def parse_edges_output(stdout):
    """解析 build-context edges 输出，统计 resolution_method 分布

    输出格式（文本表格）：
      Resolved edges (N shown):
      Caller     Callee     Callee Name                    File                 Line   Method
      -----------------------------------------------------------------------------------------------
      3590       0          curl_easy_init                                      95     from_calls
      3590       4788       curl_easy_setopt               lib/setopt.c         97     from_calls
    """
    method_counts = {}
    total = 0

    # 文本解析（统计 resolution_method 出现次数）
    all_methods = ["exact_match", "simple_name_unique", "same_file",
                   "include_path", "sysroot", "from_calls", "unresolved"]
    for line in stdout.split("\n"):
        for method in all_methods:
            if method in line:
                method_counts[method] = method_counts.get(method, 0) + 1
                total += 1
                break

    return {"total": total, "methods": method_counts}


def main():
    repos = sorted([d for d in REPOS_DIR.iterdir() if d.is_dir() and d.name in CC_REPOS])

    results = []
    print(f"=== Matrix 4: resolved_edges + L5 ({len(repos)} C/C++ repos) ===")
    print(f"Timeout per repo: {TIMEOUT_SEC}s\n")

    for i, repo in enumerate(repos, 1):
        name = repo.name
        ws = str(repo).replace("\\", "/")

        print(f"[{i}/{len(repos)}] {name}...", end=" ", flush=True)

        # 1. 获取 workspace ID
        ws_id = get_workspace_id(ws)
        if not ws_id:
            # 先 refresh 确保 workspace 已注册
            run_cw("refresh --all", timeout=180, cwd=ws)
            ws_id = get_workspace_id(ws)

        if not ws_id:
            print("FAIL: no workspace")
            results.append({"name": name, "error": "no workspace", "edges_count": 0,
                            "methods": {}})
            continue

        # 2. 注册 build context（手动指定 include_paths 模拟 L5 场景）
        # 包含常见 C/C++ 头文件目录
        include_flags = [
            "--includes include",
            "--includes src",
            "--includes lib",
        ]
        bc_name = f"test_l5_{name}"

        # 先删除可能存在的旧 build context（通过名称查找 hash）
        rc, out, _, _ = run_cw(f"build-context list {ws_id}", timeout=30, cwd=ws)
        old_hash = None
        for line in out.split("\n"):
            if bc_name in line:
                # 提取 hash
                parts = line.split()
                for p in parts:
                    if len(p) == 8 and all(c in "0123456789abcdef" for c in p):
                        old_hash = p
                        break
                break

        if old_hash:
            run_cw(f"build-context delete {ws_id} {old_hash}", timeout=30, cwd=ws)

        # 注册新 build context
        register_cmd = f'build-context register {ws_id} {bc_name} --activate'
        for inc in include_flags:
            register_cmd += f' {inc}'

        rc, out, err, elapsed = run_cw(register_cmd, timeout=30, cwd=ws)
        if rc != 0:
            print(f"FAIL: register ({err[:50]})")
            results.append({"name": name, "error": f"register: {err[:100]}",
                            "edges_count": 0, "methods": {}})
            continue

        # 提取 build context hash
        # build-context register 输出可能不含 hash，需要从 list 中查找
        # hash 是 16 位 hex（如 b29e6d413683889e）
        bc_hash = None
        rc, out_list, _, _ = run_cw(f"build-context list {ws_id}", timeout=30, cwd=ws)
        for line in out_list.split("\n"):
            if bc_name in line:
                # 提取 16 位 hex hash
                import re
                m = re.search(r'\b[0-9a-f]{16}\b', line, re.IGNORECASE)
                if m:
                    bc_hash = m.group(0)
                    break

        if not bc_hash:
            print("FAIL: no hash")
            results.append({"name": name, "error": "no bc_hash",
                            "edges_count": 0, "methods": {}})
            continue

        # 3. 计算 resolved_edges
        rc, out, err, resolve_elapsed = run_cw(
            f"build-context resolve {ws_id} {bc_hash}", timeout=120, cwd=ws
        )
        resolve_stats = parse_resolve_output(out) if rc == 0 else {}

        # 4. 查询 resolved_edges
        rc2, out2, _, _ = run_cw(
            f"build-context edges {ws_id} {bc_hash} --limit 500", timeout=60, cwd=ws
        )
        edges_stats = parse_edges_output(out2) if rc2 == 0 else {"total": 0, "methods": {}}

        result = {
            "name": name,
            "ws_id": ws_id,
            "bc_hash": bc_hash,
            "resolve_elapsed": resolve_elapsed,
            "edges_count": resolve_stats.get("count", edges_stats.get("total", 0)),
            "source": resolve_stats.get("source", ""),
            "skipped": resolve_stats.get("skipped", 0),
            "methods": edges_stats.get("methods", {}),
        }
        results.append(result)
        print(f"edges={result['edges_count']} methods={result['methods']}")

    # 生成报告
    generate_report(results)
    print(f"\nReport: {REPORT}")


def generate_report(results):
    """生成 markdown 报告"""
    lines = [
        "# 测试矩阵 4：resolved_edges + L5 include_path/sysroot 解析报告",
        "",
        f"> 执行时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"> C/C++ 项目数：{len(results)}",
        "",
        "## resolved_edges 计算结果",
        "",
        "| 项目 | ws_id | bc_hash | resolve 耗时 | edges 总数 | source | skipped |",
        "|------|-------|---------|-------------|-----------|--------|---------|",
    ]

    for r in results:
        if "error" in r:
            lines.append(f"| {r['name']} | - | - | - | - | ERROR: {r['error'][:40]} | - |")
        else:
            lines.append(
                f"| {r['name']} | {r['ws_id']} | {r['bc_hash']} | "
                f"{r['resolve_elapsed']}s | {r['edges_count']} | "
                f"{r['source']} | {r['skipped']} |"
            )

    # resolution_method 分布
    lines.extend([
        "",
        "## resolution_method 分布（L1-L5）",
        "",
        "| 项目 | exact_match | simple_name_unique | same_file | include_path | sysroot | from_calls | unresolved |",
        "|------|-------------|---------------------|-----------|--------------|---------|------------|-----------|",
    ])

    all_methods = ["exact_match", "simple_name_unique", "same_file",
                   "include_path", "sysroot", "from_calls", "unresolved"]
    for r in results:
        if "error" in r:
            lines.append(f"| {r['name']} | - | - | - | - | - | - | - |")
            continue
        methods = r["methods"]
        vals = " | ".join(str(methods.get(m, 0)) for m in all_methods)
        lines.append(f"| {r['name']} | {vals} |")

    # L5 验证
    lines.extend([
        "",
        "## L5 include_path/sysroot 解析验证",
        "",
        "L5 新增的 `include_path` 和 `sysroot` resolution_method 用于解决 C/C++ 头文件多候选歧义。",
        "触发条件：简名有多个候选（`len(candidates) > 1`）且 build context 有 include_paths 或 toolchain sysroot。",
        "",
        "### 验证点",
        "",
        "1. **include_path 命中**：candidate 的 rel_path 前缀匹配 build_context.include_paths",
        "2. **sysroot 命中**：candidate 的 rel_path 前缀匹配 toolchain.sysroot 或 include_dirs 的 basename",
        "3. **include_path 优先于 sysroot**",
        "4. **多匹配则交由 unresolved**",
        "",
    ])

    # 汇总
    total_edges = sum(r.get("edges_count", 0) for r in results if "error" not in r)
    total_resolved = sum(
        sum(v for k, v in r.get("methods", {}).items() if k != "unresolved")
        for r in results if "error" not in r
    )
    total_include_path = sum(r.get("methods", {}).get("include_path", 0) for r in results if "error" not in r)
    total_sysroot = sum(r.get("methods", {}).get("sysroot", 0) for r in results if "error" not in r)

    lines.extend([
        "## 汇总统计",
        "",
        f"- 总 C/C++ 项目数：{len(results)}",
        f"- 总 resolved_edges：{total_edges}",
        f"- 已解析（非 unresolved）：{total_resolved}",
        f"- L4a include_path 命中：{total_include_path}",
        f"- L4b sysroot 命中：{total_sysroot}",
        f"- L5 解析率（已解析/总数）：{round(total_resolved/max(total_edges,1)*100, 1)}%",
    ])

    REPORT.write_text("\n".join(lines), encoding="utf-8")


if __name__ == "__main__":
    main()
