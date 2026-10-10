#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
gen_test_cases.py —— 从 CallWarden **权威真相源** 生成分级测试用例清单。

与旧 gen_io_spec.py / WB 会话版 gen_cases.py 的根本区别：
- 全部从实仓权威数据导出，零虚构；
- MCP 侧改用 `server/tools/_categories.py` 的 17 分类（与 CLI 同构）做权威归类，
  并从工具 description 抽取 P0/P1 暗示，结合动词前缀判定读写；
- 直接落库到 `docs/testing/TEST_CASES.md`，供其他 agent 审计与复现。

权威真相源：
    1. cli/categories.py                     -> 21 类 / 84 顶层命令
    2. tests/convergence/fixtures/cli_full_params.json -> 234 叶子（提取 233 + 跳过 1）
    3. server/tools/_categories.py          -> 17 类 / 243 MCP 工具（TOOL_CATEGORIES）
    4. tests/convergence/fixtures/mcp_full_schema.json -> 243 工具真 schema（name/desc/required/params）
    5. tests/convergence/seed_sample/        -> 真实种子 fixture（calc.py + service.ts）

种子已知事实（用于 Assert 精确断言，不是 substring）：
    - calc.py:  add(a,b) / multiply(a,b)，且 multiply -> add 存在真实调用边
    - service.ts: MemoryRepo.find / MemoryRepo.save，Service 类

用法：python docs/testing/gen_test_cases.py
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "TEST_CASES.md")


# ---------------------------------------------------------------------------
# 载入权威真相源（用 importlib 直接 load 文件，避免触发 server/cli 的重依赖）
# ---------------------------------------------------------------------------
def _load_module(name: str, path: str):
    # 必须先登记到 sys.modules，否则 dataclass 在 _is_type 里取 cls.__module__ 会 None
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def load_cli_categories():
    mod = _load_module("cli_categories", os.path.join(REPO, "cli", "categories.py"))
    cats = []
    for c in mod.COMMAND_CATEGORIES:
        cats.append({
            "key": c.key,
            "title": c.title,
            "cli_only": c.cli_only,
            "commands": [ci.name for ci in c.commands],
        })
    return cats


def load_mcp_categories():
    mod = _load_module("mcp_categories", os.path.join(REPO, "server", "tools", "_categories.py"))
    cats = []
    for c in mod.TOOL_CATEGORIES:
        cats.append({
            "key": c.key,
            "title": c.title,
            "cli_category": c.cli_category,
            "tools": list(c.tools),
        })
    return cats


def load_cli_params():
    p = os.path.join(REPO, "tests", "convergence", "fixtures", "cli_full_params.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


def load_mcp_schema():
    p = os.path.join(REPO, "tests", "convergence", "fixtures", "mcp_full_schema.json")
    with open(p, encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# CLI 叶子分级规则（沿用 tests/convergence/t3_cli_runner.py 的 skip 口径，但不静默）
# ---------------------------------------------------------------------------
DESTRUCTIVE = (
    "workspace delete", "workspace register", "workspace set",
    "gc archive", "gc db-cleanup", "gc retention",
    "task apply", "task close", "task rollback", "task reopen",
    "task revert", "clone clear", "clone detect",
    "fts rebuild", "audit rotate", "assignment revoke", "assignment create",
    "rule sync", "rule insert-block", "refresh",
    "git import", "coverage import", "defect import", "semgrep scan",
    "server", "watch",
    "daemon start", "daemon backup", "daemon restore",
    "daemon gc-cas", "daemon gc-snapshots", "daemon snapshot-evict", "daemon mount",
)

KNOWN_FAILSOFT = ["call-chain", "coupled-fns", "largest-fns", "rule applicable", "status"]
KNOWN_TRACEBACK = ["collab publish", "daemon publish", "daemon snapshot-stats"]
READONLY_CORE = (
    "search", "grep", "symbol", "file", "query", "callers", "callees",
    "call-chain", "topo", "impact", "deepest", "stats", "status", "brief",
    "orphan-symbols", "top-callers", "call-heatmap", "module-calls",
)
WRITE_ISO = (
    "task create", "task next", "task report", "task split", "task list",
    "audit verify", "identity revoke", "lease", "gc policy",
)


def classify_cli(cmd: str):
    if any(cmd == p or cmd.startswith(p + " ") for p in DESTRUCTIVE):
        return "P2", "DESTRUCTIVE", "破坏性/重操作/阻塞 → 隔离沙箱专门环境，禁止在主回归跑"
    if cmd in KNOWN_FAILSOFT:
        return "P0", "FAIL_SOFT", "⚠ rc=0 掩盖异常（fail-soft 吞错）→ 必须断言 fail-closed"
    if cmd in KNOWN_TRACEBACK:
        return "P0", "TRACEBACK", "⚠ 现 traceback → 修复前先钉住「不允许 traceback」"
    if any(cmd == p or cmd.startswith(p + " ") for p in WRITE_ISO):
        return "P1", "WRITE_ISO", "写路径 → 隔离 daemon / 临时 DB 实例"
    if any(cmd == p or cmd.startswith(p + " ") for p in READONLY_CORE):
        return "P0", "READONLY", "只读查询 → 用 seed_sample 已知事实做精确断言"
    return "P1", "GENERAL", "常规 → 契约级断言（exit/结构/字段）"


def build_argv(cmd: str, spec: dict) -> str:
    parts = [cmd]
    seed_val = {
        "symbol": "multiply", "name": "multiply", "qn": "multiply",
        "qualified_name": "multiply", "path": "calc.py", "file": "calc.py",
        "hash": "<seed_hash>", "task_id": "<seed_task_id>", "step_id": "<seed_step_id>",
        "limit": "20",
    }
    for pos in spec.get("positionals", [])[:3]:
        n = pos.get("name", "?")
        parts.append(seed_val.get(n, f"<{n}>"))
    for opt in spec.get("options", []):
        o = opt.get("opt", "")
        for flag in [x.strip() for x in o.split(",")]:
            if flag == "--json":
                parts.append(flag)
                break
    return "python cw.py " + " ".join(parts)


def seed_assert(cmd: str) -> str:
    table = {
        "callers": 'set(result["callers"]) == set()  # multiply 无调用者；add 的调用者 == {"multiply"}',
        "callees": 'set(result["callees"]) == {"add"}  # multiply -> add 真实调用边',
        "call-chain": 'result 非空 且 首节点 == "multiply"；⚠ 已知 fail-soft，rc==0 但空结果须判失败',
        "impact": 'result["impacted"] 集合确定（multiply → add）；入参是 hash 不是限定名',
        "search": 'multiply 必在结果内；结果条数 == 已知固定值',
        "symbol": 'result["name"]=="multiply" 且 有 line/signature 字段',
        "file": 'set(s["name"] for s in result) == {"add","multiply"}  # calc.py 恰 2 函数',
        "stats": 'result["symbols"]/["calls"] 为 int 且 >0（>0 非空）',
        "orphan-symbols": '结果集确定（种子 fixture 的孤儿集合）',
        "top-callers": '结果按调用数降序；add 应排在 multiply 之前',
        "topo": '返回可解析结构 且 无 traceback',
        "brief": '返回可解析结构 且 关键字段存在 且 无 traceback',
        "grep": '返回可解析结构 且 关键字段存在 且 无 traceback',
        "query": '返回可解析结构 且 关键字段存在 且 无 traceback',
    }
    return table.get(cmd, 'exit code / 结构契约：返回可解析结构 且 关键字段存在 且 无 traceback')


# ---------------------------------------------------------------------------
# MCP 工具读写 / 优先级判定
# ---------------------------------------------------------------------------
WRITE_PREFIXES = (
    "propose_", "task_", "lease_", "guardrail_add", "assignment_", "audit_",
    "gc_", "delete_", "cancel_", "register", "create", "revoke", "set_",
    "insert", "apply", "publish", "rotate", "sync", "build_", "detect_clones",
    "import", "restore", "purge", "clear", "backup", "remove_", "update_",
    "add_", "mark_", "reset_", "archive_",
)


def classify_mcp(name: str, desc: str):
    d = (desc or "").lower()
    if any(name.startswith(p) for p in WRITE_PREFIXES) or "写路径" in d or "写面" in d:
        return "P1", "WRITE"
    return "P0", "READ"


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    cli_cats = load_cli_categories()
    mcp_cats = load_mcp_categories()
    cli = load_cli_params()
    mcp = load_mcp_schema()

    leaf_cmds = cli["commands"]           # dict: leaf cmd string -> spec
    tools = {t["name"]: t for t in mcp["tools"]}
    tool_names = [t["name"] for t in mcp["tools"]]

    L = []
    A = L.append

    # ---------------- 0. 总览 ----------------
    A("# CallWarden 分级测试用例清单（锚定权威真相源）\n")
    A("> 由 `docs/testing/gen_test_cases.py` 从**实仓权威数据**生成，所有命令/参数/工具均真实，非虚构。\n")
    A("> 真相源：`cli/categories.py`(21类/84顶层) · `cli_full_params.json`(234叶子/提取233) · ")
    A("`server/tools/_categories.py`(17类/243工具) · `mcp_full_schema.json`(243工具) · `seed_sample/`(真实种子)\n")
    A("> 本清单是收敛套件（T1–T5 / M1–M4）的**人工可读映射层**；真正执行由收敛套件以同批权威 JSON 全量驱动。\n")

    A("\n## 0. 权威口径总览\n")
    A("| 项 | 真实值 | 来源 |")
    A("|----|--------|------|")
    A(f"| CLI 顶层命令 | {sum(len(c['commands']) for c in cli_cats)}（分 {len(cli_cats)} 类，[18]-[21] 为 CLI-only） | `cli/categories.py` |")
    A(f"| CLI 叶子命令 | {cli['total_leaves']}（已提取参数 {cli['extracted']}，跳过 {len(cli['skipped'])}） | `cli_full_params.json` |")
    A(f"| MCP 工具 | {mcp['total']}（分 {len(mcp_cats)} 类，与 CLI [1]-[17] 同构） | `server/tools/_categories.py` |")
    A("| 真实种子 fixture | `tests/convergence/seed_sample/`（calc.py + service.ts） | 实仓 |")

    # ---------------- 1. CLI 分级统计 ----------------
    A("\n## 1. CLI 叶子用例分级统计\n")
    buckets = {"P0": [], "P1": [], "P2": []}
    kindcnt: dict[str, int] = {}
    for cmd in leaf_cmds:
        pr, kind, _ = classify_cli(cmd)
        buckets[pr].append((cmd, kind))
        kindcnt[kind] = kindcnt.get(kind, 0) + 1
    A("| 优先级 | 用例数 | 说明 |")
    A("|--------|--------|------|")
    A(f"| **P0** | {len(buckets['P0'])} | 只读查询精确断言 + 已知缺陷钉死（fail-closed） |")
    A(f"| **P1** | {len(buckets['P1'])} | 常规契约 / 写隔离 |")
    A(f"| **P2** | {len(buckets['P2'])} | 破坏性/重操作 → 隔离沙箱，不进主回归 |")
    A(f"| **合计** | {len(leaf_cmds)} | CLI 叶子全覆盖 |\n")
    A("按类别分布：" + "、".join(f"{k}={v}" for k, v in sorted(kindcnt.items(), key=lambda x: -x[1])) + "\n")

    # ---------------- 2. P0 逐条 ----------------
    A("\n## 2. P0 用例（先做，含 fail-closed 钉死）\n")
    A("统一 AAA：`Arrange`=种子 workspace 已 build_graph（seed_sample）｜`Act`=真实 argv｜`Assert`=精确断言\n")
    A("| case_id | 优先级 | 类别 | Act（真实 argv） | Assert（精确断言） |")
    A("|---------|--------|------|------------------|-------------------|")
    for i, (cmd, kind) in enumerate(buckets["P0"], 1):
        spec = leaf_cmds[cmd]
        argv = build_argv(cmd, spec)
        A(f"| TC-CLI-{i:03d} | P0 | {kind} | `{argv}` | {seed_assert(cmd)} |")
    A("")

    # ---------------- 3. 已知缺陷基线组 ----------------
    A("\n## 3. 已知缺陷基线组（来自 T3 首轮，必须先钉住不许回升）\n")
    A("T3 首轮结果（生产 daemon b495919）：**70 PASS / 114 EXPECTED_BUSINESS / 18 DEFECT / 31 SKIP**\n")
    A("| 缺陷类 | 数量 | 代表命令 | 钉死方式 |")
    A("|--------|------|----------|----------|")
    A(f"| rc=0 掩盖真 bug（fail-soft 吞异常） | {len(KNOWN_FAILSOFT)} | {', '.join(KNOWN_FAILSOFT)} | 断言：rc==0 时必须返回有效载荷，**空结果/吞异常 = FAIL** |")
    A(f"| traceback | {len(KNOWN_TRACEBACK)} | {', '.join(KNOWN_TRACEBACK)} | 断言：stdout/stderr 不得含 `Traceback (most recent call last)` |")
    A("| method_not_found（CLI→daemon compat RPC 未实现） | 10 | 由探测得出 | 断言：不得新增，只许减少 |")
    A("")
    A("> **关键**：rc=0 却返回空/错误 = 比崩溃更危险。这 5 个 fail-soft 是当前最高价值用例。\n")

    # ---------------- 4. MCP 侧分级 ----------------
    A("\n## 4. MCP 侧分级（243 工具，T2 已 157 PASS / 72 BUSINESS / **0 DEFECT**）\n")
    mcp_grade = {}
    for name, t in tools.items():
        pr, rw = classify_mcp(name, t.get("description", ""))
        mcp_grade[name] = (pr, rw)
    ro = [n for n, (pr, rw) in mcp_grade.items() if rw == "READ"]
    wr = [n for n, (pr, rw) in mcp_grade.items() if rw == "WRITE"]
    A(f"共 {len(tools)} 个工具，按动词/描述判定：**只读 ≈ {len(ro)}（P0）**，**写 ≈ {len(wr)}（P1）**。\n")
    A("- 只读类工具 → P0，用 seed 事实做集合/数量断言；")
    A("- 写类工具（`propose_`/`task_`/`lease_`/`guardrail_add`/`gc_*` 等）→ P1，跑在隔离 daemon；")
    A("- 基线门禁：`DEFECT == 0` 且 `PASS >= 100` 且 `覆盖合计 == 243`。\n")

    A("### 4.1 17 分类 × 243 工具（权威，与 CLI [1]-[17] 同构）\n")
    for idx, c in enumerate(mcp_cats, 1):
        ro_n = sum(1 for t in c["tools"] if mcp_grade.get(t, ("P1", "WRITE"))[1] == "READ")
        wr_n = len(c["tools"]) - ro_n
        A(f"\n#### [{idx}] `{c['key']}` — {c['title']}（{len(c['tools'])}：读 {ro_n} / 写 {wr_n}）\n")
        A("| 工具 | 优先级 | 读写 |")
        A("|------|--------|------|")
        for t in c["tools"]:
            pr, rw = mcp_grade.get(t, ("P1", "WRITE"))
            A(f"| `{t}` | {pr} | {rw} |")
    A("")

    # ---------------- 5. 21 CLI 分类 × 84 命令 ----------------
    A("\n## 5. 21 个 CLI 分类 × 84 顶层命令（权威）\n")
    A("> 注：[18]-[21] 为 CLI-only（MCP 无对应分类）。新增命令漏归类会被既有 `test_category_source.py` 拦截。\n")
    for idx, c in enumerate(cli_cats, 1):
        tag = " *(CLI-only)*" if c["cli_only"] else ""
        A(f"\n### [{idx}] `{c['key']}` — {c['title']}（{len(c['commands'])}）{tag}\n")
        A(", ".join(f"`{x}`" for x in c["commands"]))
    A("")

    # ---------------- 6. 收敛套件覆盖映射 ----------------
    A("\n## 6. 收敛套件覆盖映射（执行层 ↔ 本清单）\n")
    A("| 套件 | 职责 | 覆盖本清单的哪部分 | 基线 / 门禁 |")
    A("|------|------|---------------------|----------------|")
    A("| **T1** 基建冒烟 | 种子 workspace fixture + param_provider 骨架 | 不直接测业务，是 T2/T3 的前提 | 必须 PASS 才能跑 T2/T3 |")
    A(f"| **T2** MCP 全参数 | 243 MCP 工具真实调用 | §4 全部 243 工具 | 157 PASS / 72 BUSINESS / **0 DEFECT**；门禁 DEFECT==0 & PASS>=100 & 覆盖==243 |")
    A(f"| **T3** CLI 全参数 | 234 CLI 叶子真实调用 | §1 全部 233 提取叶子 + §5 84 顶层 | 70 PASS / 114 BUSINESS / 18 DEFECT / 31 SKIP；门禁 DEFECT 不回升 |")
    A("| **T4** 多 workspace 隔离 | 多用户/多 workspace/多 agent 并发正确性 | §4 写类工具的并发隔离场景 | 并发无死锁、隔离性成立 |")
    A("| **T5** LLM 可理解性 | 真实 LLM 按工具 description 选对率 | §4 工具 description 质量 | 选对率 ≥ 基线阈值（需 OPENAI_API_KEY） |")
    A("| **M1** 路由矩阵 | 243/243 路由验证（每个工具 rpc_method 在 dispatch.rs） | §4 全部 243 工具的路由可达性 | 243/243 通过 |")
    A("| **M2** 纯 client 审计 | Python 侧无新违例 | cli/ 包 purity | 零新增违例 |")
    A("| **M3** 并发写 | 双 agent 单 workspace 并发一致性 | §4 写类工具的并发正确性 | 混合读写无脏写 |")
    A("| **M4** fail-closed | daemon 不可达 → DaemonUnavailableError（不降级本地） | §2 的 fail-closed 钉死 + §5 所有 CLI 命令 | 不可达一律结构化错误，绝不本地执行 |")
    A("")
    A("> **覆盖结论（诚实口径，v6 订正）**：T2 + T3 当前只做到**表面可达调用**——")
    A("> 每个工具/命令的路由存在、参数可被构造、调用能往返。**不构成「全参数真实调用」**：")
    A("> `param_provider.py` 对未匹配参数返回占位值（`\"seed\"` / `\"0\"*64` / `\"T-seed-...\"`），")
    A("> `seed_workspace` 不预建 task/lease/agent/snapshot，结果多为 `EXPECTED_BUSINESS`(not_found)，")
    A("> **不验证业务正确性**；T2/T3 断言仅数总量，无逐项精确断言。")
    A(">")
    A("> **另一关键事实**：主 `ci.yml` 的 `test` job 跑在 Linux，而")
    A("> `tests/convergence/conftest.py:40-49` 的 `_pick_bin()` 硬编码 `cw-daemon.exe`，")
    A("> 且该 job 不构建 `cw-daemon` 二进制 → **收敛套件在主 CI 中真实执行量为 0**。")
    A("> 下表基线数字来自首轮本地实测（生产 daemon `b495919`），非 CI 门禁产出。")
    A(">")
    A("> 全仓真实 skip 站点 **447 处**（213 `pytest.skip(` + 234 `pytest.mark.skipif`，")
    A("> 分布于 131 个测试文件），**不是早期版本所述的 219 处**。")
    A("> 其中仅 52 处（11.6%）有实测归因，最大单一家族为 `callwarden_core 未安装` 39 处，")
    A("> 归因明细与算术修正详见 `COVERAGE_AUDIT.md` §2、§3。\n")

    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8") as f:
        f.write("\n".join(L))
    print(f"[ok] {OUT}")
    print(f"     CLI tops={sum(len(c['commands']) for c in cli_cats)} / leaves={len(leaf_cmds)} "
          f"(P0={len(buckets['P0'])} P1={len(buckets['P1'])} P2={len(buckets['P2'])})")
    print(f"     MCP={len(tools)} (READ={len(ro)} WRITE={len(wr)})")


if __name__ == "__main__":
    main()
