#!/usr/bin/env python3
"""Generate static planning inventories; never import product code or run tests.

Windows: C:\\Python314\\python.exe docs/testing/gen_test_cases.py --revision <verified-HEAD>
Append --check to compare bytes without writing. Revision is supplied by a separate
verified VCS read; hashes bind actual inputs, including dirty/untracked source files.
This generator is not a runtime CLI/schema extractor or a release test gate.
"""
from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import subprocess

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent


def read(path):
    return (ROOT / path).read_text(encoding="utf-8-sig")


def digest(path):
    return hashlib.sha256((ROOT / path).read_bytes()).hexdigest()


def files(directory, glob):
    result = subprocess.run(
        ["rg", "--files", directory, "-g", glob], cwd=ROOT,
        capture_output=True, text=True, encoding="utf-8", check=True,
    )
    return sorted(Path(p).as_posix() for p in result.stdout.splitlines())


def tree(path):
    return ast.parse(read(path), filename=path)


def categories(path, constructor, member):
    result = []
    for node in ast.walk(tree(path)):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        if node.func.id != constructor:
            continue
        fields = {kw.arg: kw.value for kw in node.keywords}
        members = fields[member]
        if member == "commands":
            names = [ast.literal_eval(n.args[0]) for n in members.elts]
        else:
            names = ast.literal_eval(members)
        result.append({"key": ast.literal_eval(fields["key"]),
                       "title": ast.literal_eval(fields["title"]), "names": names})
    return result


def unique(values, label):
    duplicates = sorted(k for k, n in Counter(values).items() if n > 1)
    if duplicates:
        raise ValueError(f"{label}: duplicate entries {duplicates}")
    return set(values)


def equal(actual, expected, label):
    if actual != expected:
        raise ValueError(f"{label}: missing={sorted(expected-actual)}, extra={sorted(actual-expected)}")


def tool_sources():
    result = []
    for path in files("server/tools", "tools_*.py"):
        for node in ast.walk(tree(path)):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if any(isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                   and isinstance(d.func.value, ast.Name) and d.func.value.id == "mcp"
                   and d.func.attr == "tool" for d in node.decorator_list):
                result.append(node.name)
    return result


def scan_tests(paths):
    sites, definitions = [], 0
    for path in paths:
        for node in ast.walk(tree(path)):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test_"):
                definitions += 1
            if not isinstance(node, ast.Call):
                continue
            mechanism = ast.unparse(node.func)
            if mechanism not in ("pytest.skip", "pytest.mark.skipif", "pytest.mark.skip", "pytest.importorskip"):
                continue
            reason = next((k.value for k in node.keywords if k.arg == "reason"), None)
            if mechanism == "pytest.skip" and node.args:
                reason = node.args[0]
            literal = reason.value if isinstance(reason, ast.Constant) and isinstance(reason.value, str) else None
            sites.append({
                "site_id": f"{path}:{node.lineno}:{node.col_offset}:{mechanism}",
                "file": path, "line": node.lineno, "column": node.col_offset,
                "mechanism": mechanism, "reason_literal": literal,
                "reason_expression": ast.unparse(reason) if reason is not None else None,
                "condition_or_module": ast.unparse(node.args[0]) if node.args and mechanism != "pytest.skip" else None,
                "file_sha256": digest(path), "root_cause": "UNREVIEWED",
            })
    sites.sort(key=lambda s: (s["file"], s["line"], s["column"]))
    return definitions, sites


def cell(value):
    return str(value).replace("|", "\\|").replace("\n", " ").replace("`", "'")


def generate(revision):
    cli_path = "tests/convergence/fixtures/cli_full_params.json"
    mcp_path = "tests/convergence/fixtures/mcp_full_schema.json"
    matrix_path = "deliverables/software-company/tool_migration_matrix.json"
    cli, mcp, matrix = [json.loads(read(p)) for p in (cli_path, mcp_path, matrix_path)]
    cli_cats = categories("cli/categories.py", "CommandCategory", "commands")
    mcp_cats = categories("server/tools/_categories.py", "ToolCategory", "tools")
    tops = unique([n for c in cli_cats for n in c["names"]], "CLI categories")
    category_tools = unique([n for c in mcp_cats for n in c["names"]], "MCP categories")
    names = unique([t["name"] for t in matrix["tools"]], "matrix")
    equal(unique(tool_sources(), "source registrations"), names, "source vs matrix")
    schema_names = unique([t["name"] for t in mcp["tools"]], "MCP schema")
    schema_drift = {"missing_current_tools": sorted(names - schema_names),
                    "obsolete_schema_tools": sorted(schema_names - names)}
    equal(category_tools, names, "categories vs matrix")
    if matrix["total_tools"] != len(names) or mcp["total"] != len(names):
        raise ValueError("tool count fields disagree")
    leaves = unique(list(cli["commands"]) + cli["skipped"], "CLI snapshot")
    if cli["total_leaves"] != len(leaves) or cli["extracted"] != len(cli["commands"]):
        raise ValueError("CLI snapshot count fields disagree")
    if {n.split()[0] for n in leaves} - tops:
        raise ValueError("CLI snapshot has uncategorized top-level commands")

    py_paths = files("tests", "*.py")
    rust_paths = files("rust_ext/src", "*.rs")
    definitions, sites = scan_tests(py_paths)
    counts = Counter(s["mechanism"] for s in sites)
    classic = [s for s in sites if s["mechanism"] in ("pytest.skip", "pytest.mark.skipif")]
    exact = Counter(s["reason_literal"] for s in classic if s["reason_literal"] is not None)
    parser_sites = [s for s in classic if s["file"].startswith("tests/parser_contract/")]
    convergence_defs = sum(len([n for n in ast.walk(tree(p)) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                               and n.name.startswith("test_")]) for p in py_paths if p.startswith("tests/convergence/"))
    inputs = sorted(set(py_paths + rust_paths + files("server/tools", "*.py") + [
        cli_path, mcp_path, matrix_path, "cli/categories.py", "server/tools/_categories.py",
        "tests/convergence/fixtures/audit_mcp_inventory.json", "docs/testing/gen_test_cases.py",
        "tests/convergence/seed_sample/calc.py", "tests/convergence/seed_sample/service.ts",
    ]))
    audit = {
        "schema_version": "static-planning-audit/v1", "supplied_source_revision": revision,
        "revision_note": "Externally verified HEAD; input hashes, not HEAD alone, bind working-tree content.",
        "execution_status": "NOT_RUN", "runtime_inventory_status": "NOT_VERIFIED",
        "planning_inventory_gate": "BLOCKED_SCHEMA_DRIFT" if schema_names != names else "STATIC_SETS_MATCH",
        "scope": "rg-visible tests/*.py, rust_ext/src/*.rs, server/tools/*.py plus listed manifests; no archive/build/ignored corpus recursion",
        "algorithms": {"python": "AST FunctionDef/AsyncFunctionDef name startswith test_; not collected nodeids",
                       "skip": "AST direct qualified Call sites; aliases/dynamic skips excluded; reasons are not root causes",
                       "rust": "literal #[test] or #[tokio::test] attributes in src; not executed test count"},
        "inventory": {"cli_categories": len(cli_cats), "cli_top_levels": len(tops),
                      "cli_snapshot_leaves": len(leaves), "cli_extracted": len(cli["commands"]),
                      "cli_unextracted": cli["skipped"], "mcp_categories": len(mcp_cats), "mcp_tools": len(names),
                      "mcp_backends": dict(sorted(Counter(t["target_backend"] for t in matrix["tools"]).items())),
                      "mcp_op_classes": dict(sorted(Counter(t["op_class"] for t in matrix["tools"]).items())),
                      "source_matrix_category_set_consistent": True,
                      "schema_matrix_set_consistent": schema_names == names,
                      "schema_name_drift": schema_drift},
        "static_tests": {"python_definitions": definitions, "python_source_files": len(py_paths),
                         "convergence_definitions": convergence_defs, "skip_calls": dict(sorted(counts.items())),
                         "classic_skip_sites": len(classic), "classic_skip_files": len({s["file"] for s in classic}),
                         "parser_contract_classic_sites": len(parser_sites),
                         "core_not_installed_exact_sites": exact["callwarden_core 未安装"],
                         "core_not_built_exact_sites": exact["callwarden_core Rust 扩展未构建"],
                         "parser_core_not_installed_exact_sites": sum(s["reason_literal"] == "callwarden_core 未安装" for s in parser_sites),
                         "root_cause_reviewed_sites": 0,
                         "rust_src_attributes": sum(len(re.findall(r"#\[(?:test|tokio::test)\]", read(p))) for p in rust_paths),
                         "rust_src_cfg_test_files": sum("#[cfg(test)]" in read(p) for p in rust_paths)},
        "inputs": [{"path": p, "sha256": digest(p)} for p in inputs],
    }
    lines = ["# CallWarden 测试入口与参数规划清单（v7 · 自动生成）", "",
             "> gen_test_cases.py只读静态源生成；没有运行产品或测试，不代表功能通过。",
             "> 数字与输入hash见STATIC_AUDIT.json；策略/交付门禁见TESTING_PLAN.md。", "",
             "## 1. 清单与证据边界", "",
             f"CLI分类{len(cli_cats)}，顶层{len(tops)}；快照叶子{len(leaves)}，提取{len(cli['commands'])}，未提取{cell(cli['skipped'])}。",
             f"MCP分类{len(mcp_cats)}，工具{len(names)}；source/matrix/category名字集合静态一致。",
             f"固化schema集合一致={schema_names == names}；缺当前工具={cell(schema_drift['missing_current_tools'])}；历史额外项={cell(schema_drift['obsolete_schema_tools'])}。漂移阻断清单冻结，不能凭总数相等补绿。",
             "CLI snapshot未与当前runtime/argparse比对；MCP未取wire runtime schema。参数表亦为固化快照，默认值/类型/互斥和语义均待复核。",
             "所有行状态为待合同化/未计入语义覆盖，表示未完成逐入口证据映射，不表示存量测试完全不存在。", "",
             "## 2. 每行的合同义务", "",
             "C1正常语义；C2逐参数等价类/默认/边界/互斥；C3精确负向及无非法副作用；C4适用写入/幂等/回滚；C5适用故障/恢复；C6跨端共享语义。",
             "须填入真实corpus/profile/实体provenance、独立oracle、runnable selector、timeout/cleanup与证据；适用性不能从工具名推断。常驻入口用启动/ready/交互/停止。", "",
             "## 3. CLI逐叶子（含未提取项）", "",
             "| 稳定case族ID | 入口 | 分类 | 快照positionals | 快照options | 状态 |",
             "|---|---|---|---|---|---|"]
    cli_category = {n: c["key"] for c in cli_cats for n in c["names"]}
    for name in sorted(leaves):
        spec = cli["commands"].get(name)
        pos = ", ".join(p["name"] for p in spec["positionals"]) if spec else "未提取"
        options = ", ".join(o["opt"] for o in spec["options"]) if spec else "未提取"
        lines.append(f"| CLI:{cell(name)} | `{cell(name)}` | {cli_category[name.split()[0]]} | {cell(pos)} | {cell(options)} | 待合同化 |")
    lines += ["", "## 4. MCP逐工具（矩阵声明，不是副作用实测）", "",
              "| 稳定case族ID | 分类 | backend / op_class | 必填快照参数 | 可选快照参数 | 状态 |",
              "|---|---|---|---|---|---|"]
    mcp_category = {n: c["key"] for c in mcp_cats for n in c["names"]}
    schemas = {t["name"]: t for t in mcp["tools"]}
    for tool in sorted(matrix["tools"], key=lambda t: t["name"]):
        name = tool["name"]
        schema = schemas.get(name)
        schema_status = "快照待复核" if schema else "SCHEMA缺失，阻断"
        schema = schema or {"params": {}, "required": []}
        required = schema.get("required") or []
        optional = sorted(set(schema["params"]) - set(required))
        lines.append(f"| MCP:{name} | {mcp_category[name]} | {tool['target_backend']} / {tool['op_class']} | {cell(', '.join(required)) if schema_status != 'SCHEMA缺失，阻断' else '未知（schema缺失）'} | {cell(', '.join(optional)) if schema_status != 'SCHEMA缺失，阻断' else '未知（schema缺失）'} | 待合同化；{schema_status} |")
    lines += ["", "## 5. 执行映射与阻断条件", "",
              "现有T1参数骨架、T2进程内MCP、T3源码CLI、M1静态路由、M2纯度、M3并发、M4入口故障、T4 workspace、T5 LLM各有不同深度；不自动折算为上表C1–C6全部完成。",
              "MCP进程内调用不代替wire；CLI源码不代替安装/冻结产物；假ID业务拒绝不代替正常case；破坏性/重操作在隔离实例验而非永久跳过。",
              "正式合同实现后追加逐case selector/证据映射（独立机器manifest待建），本生成器不得自行把行改为PASS。未知错误、清单漂移、缺selector/报告/前置按策略阻断。", ""]
    return {"TEST_CASES.md": "\n".join(lines),
            "STATIC_AUDIT.json": json.dumps(audit, ensure_ascii=False, indent=2) + "\n",
            "SKIP_SITES.json": json.dumps({"schema_version": "static-skip-sites/v1", "sites": sites}, ensure_ascii=False, indent=2) + "\n"}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True, help="externally verified 40-hex source HEAD")
    parser.add_argument("--check", action="store_true", help="compare only; no writes")
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        parser.error("--revision must be a verified lowercase 40-hex HEAD")
    outputs = generate(args.revision)  # all validations precede writes
    mismatches = []
    for name, content in outputs.items():
        path = OUT / name
        expected = content.encode("utf-8")
        if args.check:
            if not path.exists() or path.read_bytes() != expected:
                mismatches.append(name)
        else:
            path.write_bytes(expected)
    if mismatches:
        raise SystemExit("Generated content differs: " + ", ".join(mismatches))
    print("Static planning inventories " + ("match" if args.check else "written") + "; tests NOT_RUN")


if __name__ == "__main__":
    main()
