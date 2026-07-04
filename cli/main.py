"""
cli/main.py
===========

代码知识图谱数据库命令行接口。

提供完整的 CLI 参数定义和命令处理逻辑，支持：
- 数据库初始化与文件监控
- 符号查询、调用关系分析
- 拓扑排序、影响面分析
- 注释管理与覆盖率统计
- 缺陷检测与多语言静态分析
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time

from ..db import CodeGraphDB
from ..config import detect_project_root, get_default_workspace_name
from ..server.watcher import FileWatcher
from ..i18n import t, set_language, get_arg_help, get_msg, get_error, DEFAULT_LANG
from .console import cprint


# ====================================================================
# 代码守护者架构子命令（四大支柱）
# ====================================================================

# 子命令关键字集合
_SUBCOMMANDS = {"guardrail", "impact", "review", "evolution", "hotspot", "churn", "defect",
                "task", "vuln-blast", "symbol-history", "check-gate", "test-impact",
                "gc", "doctor"}

# 子命令帮助文本（用于 --help 输出）
_SUBCOMMAND_HELP = """代码守护者架构子命令（四大支柱）:

  安全护栏:
    guardrail scan [--file <path>] [--category <cat>]   扫描安全护栏违规
    guardrail rules [--category <cat>]                  列出护栏规则

  变更影响:
    impact <symbol_hash> [--depth N]                    计算变更影响半径
    review <symbol_hash>                                生成审查就绪报告

  演化智能:
    evolution <qualified_name> [--window 30d]           查询函数变更频率
    hotspot [--module <path>]                           热点函数排名
    churn [--module <path>] [--window 90d]              代码流失分析

  缺陷知识库:
    defect search [--category <cat>] [--severity <sev>] 搜索缺陷模式
    defect suggest <symbol_hash> [--finding <id>]       推荐修复方案
    defect learn <commit_hash>                          从修复 commit 学习
    defect stats                                        缺陷知识库统计
    defect build                                        构建缺陷知识库

  代码图谱 GC（类 Java GC，归档被 .gitignore/.callwardenignore 命中的文件）:
    gc archive [--force] [--dry-run]                    归档被 ignore 命中的文件
    gc restore [--path <path> ...] [--force]            复活已归档文件
    gc status                                           查看 GC 状态（活跃/归档/删除统计）
    gc purge [--older-than <days>]                      彻底清除归档超过 N 天的文件

  诊断与维护:
    doctor                                              检查环境、数据库状态、推荐优化
    doctor --add-defender-exclusion                     添加 Windows Defender 排除项（需管理员权限）

传统命令（--flag 模式）:
  以下选项为传统 --flag 风格命令，与上述子命令并存。"""


def _run_subcommand_mode():
    """子命令模式入口：初始化 db 并调度代码守护者架构子命令"""
    # 使用系统检测到的默认语言（CALLWARDEN_LANG / LANG / LC_ALL / 系统语言）
    # 用户可通过环境变量 CALLWARDEN_LANG=en_US 切换为英文
    set_language(DEFAULT_LANG)

    # 自动检测工作区根目录
    cwd = os.getcwd()
    detected = detect_project_root(cwd)
    workspace_root = detected if detected else None

    # 初始化数据库
    db = CodeGraphDB(workspace_root=workspace_root) if workspace_root else CodeGraphDB()

    try:
        # 自动注册工作区（与 --flag 模式行为一致）
        if workspace_root:
            ws_name = get_default_workspace_name(workspace_root)
            existing = None
            for ws in db.list_workspaces():
                if ws["root_path"] == workspace_root:
                    existing = ws
                    break
            if not existing:
                ws_id = db.register_workspace(ws_name, workspace_root)
                db.set_active_workspace(ws_id)
            elif not existing.get("is_active"):
                db.set_active_workspace(existing["id"])

        # 调度子命令
        _dispatch_subcommand(sys.argv[2:], db)
    finally:
        db.close()


def _dispatch_subcommand(argv, db):
    """调度代码守护者架构子命令

    Args:
        argv: 子命令参数（不含子命令关键字本身，即 sys.argv[2:]）
        db: CodeGraphDB 实例

    Returns:
        True 表示已处理，False 表示不是有效子命令
    """
    # 子命令关键字（_run_subcommand_mode 调用时已确认在 _SUBCOMMANDS 中）
    cmd = sys.argv[1]

    try:
        if cmd == "guardrail":
            return _handle_guardrail(argv, db)
        elif cmd == "impact":
            return _handle_impact(argv, db)
        elif cmd == "review":
            return _handle_review(argv, db)
        elif cmd == "evolution":
            return _handle_evolution(argv, db)
        elif cmd == "hotspot":
            return _handle_hotspot(argv, db)
        elif cmd == "churn":
            return _handle_churn(argv, db)
        elif cmd == "defect":
            return _handle_defect(argv, db)
        elif cmd == "task":
            return _handle_task(argv, db)
        elif cmd == "vuln-blast":
            return _handle_vuln_blast(argv, db)
        elif cmd == "symbol-history":
            return _handle_symbol_history(argv, db)
        elif cmd == "check-gate":
            return _handle_check_gate(argv, db)
        elif cmd == "test-impact":
            return _handle_test_impact(argv, db)
        elif cmd == "gc":
            return _handle_gc(argv, db)
        elif cmd == "doctor":
            return _handle_doctor(argv, db)
    except Exception as e:
        cprint(f"✗ 执行子命令 '{cmd}' 失败: {e}", "red")
        return True

    return False


# --------------------------------------------------------------------
# 安全护栏子命令
# --------------------------------------------------------------------

def _handle_guardrail(args, db):
    """处理 guardrail 子命令（安全护栏）"""
    parser = argparse.ArgumentParser(prog="cw guardrail", description="生产安全护栏")
    sub = parser.add_subparsers(dest="action", required=True)

    scan_p = sub.add_parser("scan", help="扫描安全护栏违规")
    scan_p.add_argument("--file", default="", help="文件路径前缀过滤")
    scan_p.add_argument("--category", default="",
                        help="按类别过滤（db_safety/api_compat/incident）")

    rules_p = sub.add_parser("rules", help="列出护栏规则")
    rules_p.add_argument("--category", default="", help="按类别过滤")

    opts = parser.parse_args(args)

    if opts.action == "scan":
        findings = db.scan_guardrails(file_filter=opts.file)
        # findings 没有 category 字段，通过 rule_id 映射到类别后过滤
        if opts.category:
            rules = db.guardrail_list_rules()
            rule_cat = {r["rule_id"]: r["category"] for r in rules}
            findings = [f for f in findings
                        if rule_cat.get(f.get("rule_id", ""), "") == opts.category]

        cprint(t("cli.messages.guardrail_scan_title"), "cyan", bold=True)
        file_info = f"（{t('cli.args.file')}: {opts.file}）" if opts.file else ""
        cat_info = f"（{t('cli.args.category')}: {opts.category}）" if opts.category else ""
        print(t("cli.messages.guardrail_scan_found", count=len(findings),
                file_info=file_info, cat_info=cat_info))
        print()

        if not findings:
            cprint(t("cli.messages.guardrail_scan_no_violation"), "green")
        else:
            sev_icon = {"block": "[!]", "warn": "[~]", "info": "[i]"}
            sev_color = {"block": "red", "warn": "yellow", "info": "cyan"}
            for i, f in enumerate(findings, 1):
                sev = f.get("severity", "warn")
                icon = sev_icon.get(sev, "[?]")
                color = sev_color.get(sev, "white")
                cprint(t("cli.messages.guardrail_scan_item",
                         idx=i, icon=icon, severity=sev,
                         rule_id=f.get('rule_id', '')), color)
                print(t("cli.messages.guardrail_scan_file",
                        path=f.get('file_path', '')))
                print(t("cli.messages.guardrail_scan_message",
                        msg=f.get('message', '')))
                if f.get("symbol_hash"):
                    print(t("cli.messages.guardrail_scan_symbol",
                            hash=f['symbol_hash'][:12]))
                print()
        print()

    elif opts.action == "rules":
        rules = db.guardrail_list_rules(category_filter=opts.category)
        cprint(t("cli.messages.guardrail_rules_title"), "cyan", bold=True)
        cat_info = f"（{t('cli.args.category')}: {opts.category}）" if opts.category else ""
        print(t("cli.messages.guardrail_rules_count", count=len(rules), cat_info=cat_info))
        print()

        if not rules:
            cprint(t("cli.messages.guardrail_rules_none"), "dim")
        else:
            sev_icon = {"block": "[!]", "warn": "[~]", "info": "[i]"}
            for i, r in enumerate(rules, 1):
                sev = r.get("severity", "warn")
                icon = sev_icon.get(sev, "[?]")
                builtin = t("cli.messages.guardrail_builtin") if r.get("is_builtin") else t("cli.messages.guardrail_custom")
                print(t("cli.messages.guardrail_rules_item",
                        idx=i, icon=icon, rule_id=r['rule_id'],
                        category=r.get('category', ''), builtin=builtin))
                print(t("cli.messages.guardrail_rules_severity",
                        severity=sev, action=r.get('action', '')))
                if r.get("description"):
                    print(t("cli.messages.guardrail_rules_desc", desc=r['description']))
                print()
        print()

    return True


# --------------------------------------------------------------------
# 变更影响子命令
# --------------------------------------------------------------------

def _handle_impact(args, db):
    """处理 impact 子命令（变更影响半径）"""
    parser = argparse.ArgumentParser(prog="cw impact", description="变更影响半径分析")
    parser.add_argument("symbol_hash", help="源符号 hash")
    parser.add_argument("--depth", type=int, default=3, help="BFS 遍历最大深度（默认 3）")

    opts = parser.parse_args(args)
    result = db.blast_radius(opts.symbol_hash, depth=opts.depth)

    cprint(t("cli.messages.impact_title"), "cyan", bold=True)

    if not result.get("source_symbol"):
        cprint(t("cli.messages.impact_symbol_not_found",
                 symbol_hash=opts.symbol_hash), "red")
        return True

    print(t("cli.messages.impact_source_symbol",
            symbol=result['source_symbol']))
    print(t("cli.messages.impact_source_hash",
            hash=result['source_hash'][:12]))
    print(t("cli.messages.impact_depth", depth=result['depth']))
    print(t("cli.messages.impact_total", total=result['total_impacted']))
    print()

    # 跨层影响分布
    by_layer = result.get("by_layer", {})
    print(t("cli.messages.impact_by_layer_title"))
    print(t("cli.messages.impact_layer_code", count=by_layer.get('code', 0)))
    print(t("cli.messages.impact_layer_db", count=by_layer.get('db', 0)))
    print(t("cli.messages.impact_layer_api", count=by_layer.get('api', 0)))
    print(t("cli.messages.impact_layer_config", count=by_layer.get('config', 0)))
    print()

    # 各层符号详情
    layers = result.get("layers", [])
    for layer in layers:
        depth = layer["depth"]
        symbols = layer["symbols"]
        label = (t("cli.messages.impact_layer_label_source")
                 if depth == 0
                 else t("cli.messages.impact_layer_label_depth", depth=depth))
        print(t("cli.messages.impact_layer_symbols",
                label=label, count=len(symbols)))
        for sym in symbols[:15]:
            qn = sym.get("qualified_name", "")
            kind = sym.get("kind", "")
            fp = sym.get("file_path", "")
            print(t("cli.messages.impact_symbol_item", kind=kind, name=qn))
            if fp:
                print(t("cli.messages.impact_symbol_file", file=fp))
        if len(symbols) > 15:
            print(t("cli.messages.impact_more_symbols",
                    count=len(symbols) - 15))
        print()

    return True


def _handle_review(args, db):
    """处理 review 子命令（审查就绪报告）"""
    parser = argparse.ArgumentParser(prog="cw review", description=t("cli.messages.review_title"))
    parser.add_argument("symbol_hash", help=t("cli_review_arg_symbol_hash", default="源符号 hash"))

    opts = parser.parse_args(args)
    report = db.review_readiness_report(opts.symbol_hash)

    cprint(t("cli.messages.review_title"), "cyan", bold=True)

    scope = report.get("impact_scope", "low")
    scope_color = {"high": "red", "medium": "yellow", "low": "green"}.get(scope, "white")
    scope_i18n = {
        "high": t("cli.messages.review_scope_high"),
        "medium": t("cli.messages.review_scope_medium"),
        "low": t("cli.messages.review_scope_low"),
    }.get(scope, scope)
    print(t("cli.messages.review_risk_level"), end="")
    cprint(scope_i18n, scope_color, bold=True)
    print(t("cli.messages.review_impact_scope", count=report.get('total_impacted', 0)))
    print()

    # 跨层影响
    by_layer = report.get("by_layer", {})
    print(t("cli.messages.review_cross_layer_title"))
    print(t("cli.messages.review_layer_counts",
          code=by_layer.get('code', 0), db=by_layer.get('db', 0),
          api=by_layer.get('api', 0), config=by_layer.get('config', 0)))
    print()

    # 必测项
    must_test = report.get("must_test", [])
    print(t("cli.messages.review_must_test_title", count=len(must_test)))
    if must_test:
        for i, m in enumerate(must_test, 1):
            print(t("cli.messages.review_must_test_item",
                    idx=i, name=m.get('qualified_name', '')))
            if m.get("file_path"):
                print(t("cli.messages.review_must_test_file", path=m['file_path']))
    else:
        cprint(t("cli.messages.review_none"), "dim")
    print()

    # 人工审查点
    review_points = report.get("review_points", [])
    print(t("cli.messages.review_points_title", count=len(review_points)))
    if review_points:
        for r in review_points:
            layer = r.get("layer", "")
            target = r.get("target", "")
            msg = r.get("message", "")
            icon = "[!]" if layer in ("db", "api") else "[~]"
            cprint(t("cli.messages.review_point_item",
                     icon=icon, layer=layer, target=target), "yellow")
            print(t("cli.messages.review_point_msg", msg=msg))
    else:
        cprint(t("cli.messages.review_none"), "dim")
    print()

    # 覆盖率（可选）
    cov = report.get("coverage")
    if cov:
        print(t("cli.messages.review_coverage_title"))
        print(t("cli.messages.review_coverage_fn", name=cov.get('qualified_name', '')))
        print(t("cli.messages.review_coverage_pct",
                pct=cov.get('coverage_pct', 0),
                covered=cov.get('covered_lines', 0),
                total=cov.get('tracked_lines', 0)))
        print()

    return True


# --------------------------------------------------------------------
# 演化智能子命令
# --------------------------------------------------------------------

def _handle_evolution(args, db):
    """处理 evolution 子命令（函数变更频率）"""
    parser = argparse.ArgumentParser(prog="cw evolution", description="函数变更频率查询")
    parser.add_argument("qualified_name", help="函数限定名")
    parser.add_argument("--window", default="", help="时间窗口（如 30d/90d/1y）")

    opts = parser.parse_args(args)
    result = db.function_change_frequency(opts.qualified_name, time_window=opts.window)

    cprint("=== 函数变更频率 ===", "cyan", bold=True)
    print(f"  函数: {result.get('qualified_name', '')}")

    window_info = f"（时间窗口: {opts.window}）" if opts.window else "（全部历史）"
    print(f"  变更次数: {result.get('change_count', 0)} {window_info}")

    if result.get("first_seen"):
        first = time.strftime("%Y-%m-%d %H:%M", time.localtime(result["first_seen"]))
        print(f"  首次出现: {first}")
    if result.get("last_changed"):
        last = time.strftime("%Y-%m-%d %H:%M", time.localtime(result["last_changed"]))
        print(f"  最近变更: {last}")

    avg_interval = result.get("avg_interval", 0)
    if avg_interval > 0:
        print(f"  平均变更间隔: {avg_interval / 86400:.1f} 天")
    print()

    # 变更者
    changers = result.get("changers", [])
    print(f"  【变更者】（{len(changers)} 人）:")
    if changers:
        for c in changers[:10]:
            print(f"    - {c}")
        if len(changers) > 10:
            print(f"    ... 还有 {len(changers) - 10} 人")
    else:
        cprint("    (无)", "dim")
    print()

    # 变更时间线
    timeline = result.get("timeline", [])
    print(f"  【变更时间线】（{len(timeline)} 条）:")
    if timeline:
        for i, t in enumerate(timeline[:20], 1):
            ts = time.strftime("%Y-%m-%d %H:%M", time.localtime(t.get("timestamp", 0)))
            author = (t.get("author", "") or "unknown")[:12]
            msg = (t.get("message", "") or "")[:50]
            commit = (t.get("commit_hash", "") or "")[:8]
            print(f"    {i:3d}. {ts}  {author:<12s}  {commit}  {msg}")
        if len(timeline) > 20:
            print(f"    ... 还有 {len(timeline) - 20} 条")
    else:
        cprint("    (无变更记录)", "dim")
    print()

    # 变更分布
    dist = result.get("distribution", {})
    if dist:
        print(f"  【变更分布】:")
        for period, counts in dist.items():
            print(f"    {period}: {counts}")
        print()

    return True


def _handle_hotspot(args, db):
    """处理 hotspot 子命令（热点函数排名）"""
    parser = argparse.ArgumentParser(prog="cw hotspot", description="热点函数排名")
    parser.add_argument("--module", default="", help="模块路径前缀过滤")
    parser.add_argument("--limit", type=int, default=20, help="显示数量（默认 20）")

    opts = parser.parse_args(args)
    results = db.hotspot_evolution(module_filter=opts.module)

    cprint("=== 热点函数排名 ===", "cyan", bold=True)
    mod_info = f"（模块: {opts.module}）" if opts.module else ""
    print(f"共找到 {len(results)} 个函数 {mod_info}")
    print()

    if not results:
        cprint("  (无数据，请先运行 --init 构建图谱)", "dim")
    else:
        shown = results[:opts.limit]
        print(f"  {'#':>3}  {'热点分':>6}  {'变更':>4}  {'缺陷':>4}  {'复杂度':>6}  "
              f"{'标签':<8}  函数名")
        print(f"  {'-'*3}  {'-'*6}  {'-'*4}  {'-'*4}  {'-'*6}  {'-'*8}  {'-'*50}")

        for i, item in enumerate(shown, 1):
            score = item.get("hotspot_score", 0)
            changes = item.get("change_count", 0)
            defects = item.get("defect_count", 0)
            complexity = item.get("complexity", 0)
            label = item.get("label") or ""
            qn = item.get("qualified_name", "")[:60]

            line = (f"  {i:3d}  {score:>6.3f}  {changes:>4d}  {defects:>4d}  "
                    f"{complexity:>6d}  {label:<8s}  {qn}")
            if label == "持续热点":
                cprint(line, "red")
            elif label == "新兴热点":
                cprint(line, "yellow")
            else:
                print(line)

        if len(results) > opts.limit:
            print(f"\n  ... 还有 {len(results) - opts.limit} 个，用 --limit N 调整显示数量")
    print()

    return True


def _handle_churn(args, db):
    """处理 churn 子命令（代码流失分析）"""
    parser = argparse.ArgumentParser(prog="cw churn", description="代码流失（churn）分析")
    parser.add_argument("--module", default="", help="模块路径前缀过滤")
    parser.add_argument("--window", default="90d", help="时间窗口（默认 90d）")

    opts = parser.parse_args(args)
    result = db.churn_analysis(module_filter=opts.module, time_window=opts.window)

    cprint("=== 代码流失分析 ===", "cyan", bold=True)
    mod_info = f"（模块: {opts.module}）" if opts.module else ""
    print(f"  时间窗口: {opts.window} {mod_info}")
    print()

    print(f"  变更文件数: {result.get('changed_files', 0)}")
    print(f"  当前总行数: {result.get('total_lines_current', 0)}")
    print(f"  流失总行数: {result.get('total_churned_lines', 0)}")
    churn_rate = result.get("churn_rate", 0)
    print(f"  流失率: {churn_rate * 100:.2f}%")
    print()

    # 高频变更文件
    top_files = result.get("top_churned_files", [])
    print(f"  【高频变更文件 Top 10】:")
    if top_files:
        for i, f in enumerate(top_files, 1):
            path = (f.get("rel_path", "") or "")[:60]
            changes = f.get("change_count", 0)
            churned = f.get("churned_lines", 0)
            print(f"    {i:2d}. {path}")
            print(f"        变更 {changes} 次，流失 {churned} 行")
    else:
        cprint("    (无)", "dim")
    print()

    # 流失趋势
    trend = result.get("trend", [])
    print(f"  【流失趋势】（{len(trend)} 个时间点）:")
    if trend:
        for t in trend[:20]:
            date = t.get("date", "")
            lines = t.get("churned_lines", 0)
            bar_len = min(int(lines / 10), 30)
            bar = "█" * bar_len
            print(f"    {date}  {bar} {lines} 行")
        if len(trend) > 20:
            print(f"    ... 还有 {len(trend) - 20} 个时间点")
    else:
        cprint("    (无趋势数据)", "dim")
    print()

    return True


# --------------------------------------------------------------------
# 缺陷知识库子命令
# --------------------------------------------------------------------

def _handle_defect(args, db):
    """处理 defect 子命令（缺陷知识库）"""
    parser = argparse.ArgumentParser(prog="cw defect", description="缺陷知识库")
    sub = parser.add_subparsers(dest="action", required=True)

    search_p = sub.add_parser("search", help="搜索缺陷模式")
    search_p.add_argument("--category", default="", help="类别过滤（前缀匹配）")
    search_p.add_argument("--severity", default="",
                          help="严重度过滤（error/warning/info）")
    search_p.add_argument("--limit", type=int, default=20, help="显示数量")

    suggest_p = sub.add_parser("suggest", help="推荐修复方案")
    suggest_p.add_argument("symbol_hash", help="符号内容 hash")
    suggest_p.add_argument("--finding", type=int, default=0, help="具体 finding ID")

    learn_p = sub.add_parser("learn", help="从修复 commit 学习缺陷模式")
    learn_p.add_argument("commit_hash", help="修复提交的 commit hash")

    sub.add_parser("stats", help="缺陷知识库统计")
    sub.add_parser("build", help="构建缺陷知识库")

    opts = parser.parse_args(args)

    if opts.action == "search":
        patterns = db.defect_pattern_search(
            category=opts.category, severity_filter=opts.severity
        )

        cprint("=== 缺陷模式搜索 ===", "cyan", bold=True)
        filter_parts = []
        if opts.category:
            filter_parts.append(f"类别: {opts.category}")
        if opts.severity:
            filter_parts.append(f"严重度: {opts.severity}")
        filter_str = " | ".join(filter_parts) if filter_parts else "全部"
        print(f"  过滤条件: {filter_str}")
        print(f"  找到 {len(patterns)} 个模式")
        print()

        if not patterns:
            cprint("  (无匹配模式，请先运行 'cw defect build' 构建知识库)", "dim")
        else:
            shown = patterns[:opts.limit]
            sev_icon = {"error": "[!]", "warning": "[~]", "info": "[i]"}
            for i, p in enumerate(shown, 1):
                pid = p.get("pattern_id", "")
                cat = p.get("category", "")
                sev = p.get("severity", "")
                desc = (p.get("description", "") or "")[:60]
                cnt = p.get("case_count", 0)
                icon = sev_icon.get(sev, "[?]")
                print(f"  #{i} {icon} {pid}  ({cat})  案例 {cnt} 次")
                if desc:
                    print(f"        {desc}")
            if len(patterns) > opts.limit:
                print(f"\n  ... 还有 {len(patterns) - opts.limit} 个，用 --limit N 调整")
        print()

    elif opts.action == "suggest":
        result = db.suggest_fix(opts.symbol_hash, finding_id=opts.finding)

        cprint("=== 修复方案推荐 ===", "cyan", bold=True)
        print(f"  符号 hash: {opts.symbol_hash[:12]}...")
        if opts.finding:
            print(f"  finding ID: {opts.finding}")
        print()

        pid = result.get("pattern_id", "")
        if pid:
            print(f"  匹配模式: {pid}")
        else:
            cprint("  匹配模式: (未匹配到已知模式)", "yellow")

        eff = result.get("effectiveness_score", 0)
        print(f"  有效性分数: {eff:.2f}")
        print()

        fix = result.get("fix_template", "")
        if fix:
            cprint("  【推荐修复方案】:", "green")
            for line in fix.split("\n")[:20]:
                print(f"    {line}")
            if len(fix.split("\n")) > 20:
                print(f"    ...")
        else:
            cprint("  (无可用修复模板)", "yellow")
        print()

        similar = result.get("similar_fixes", [])
        print(f"  【相似修复案例】（{len(similar)} 个）:")
        if similar:
            for i, s in enumerate(similar, 1):
                eff_s = s.get("effectiveness", 0)
                print(f"    {i}. 有效性: {eff_s:.2f}  pattern: {s.get('pattern_id', '')}")
        else:
            cprint("    (无)", "dim")
        print()

    elif opts.action == "learn":
        cprint(f"从修复 commit 学习缺陷模式: {opts.commit_hash}", "cyan")
        result = db.learn_defect_from_fix(opts.commit_hash)

        print()
        print(f"  学习到的模式数: {result.get('learned_patterns', 0)}")
        print(f"  学习到的修复数: {result.get('learned_fixes', 0)}")

        details = result.get("details", [])
        if details:
            print()
            print(f"  【详情】（{len(details)} 条）:")
            for i, d in enumerate(details[:20], 1):
                print(f"    {i}. {d}")
            if len(details) > 20:
                print(f"    ... 还有 {len(details) - 20} 条")
        print()

    elif opts.action == "stats":
        stats = db.defect_stats()

        cprint("=== 缺陷知识库统计 ===", "cyan", bold=True)
        print(f"  模式总数: {stats.get('total_patterns', 0)}")
        print(f"  修复总数: {stats.get('total_fixes', 0)}")
        print(f"  平均有效性: {stats.get('avg_effectiveness', 0):.2f}")
        print()

        by_cat = stats.get("by_category", {})
        if by_cat:
            print("  【按类别分布】:")
            for cat, cnt in sorted(by_cat.items(), key=lambda x: -x[1]):
                print(f"    {cat:<20s} {cnt} 个")
            print()

        by_sev = stats.get("by_severity", {})
        if by_sev:
            print("  【按严重度分布】:")
            sev_icon = {"error": "[!]", "warning": "[~]", "info": "[i]"}
            for sev, cnt in sorted(by_sev.items(), key=lambda x: -x[1]):
                icon = sev_icon.get(sev, "[?]")
                print(f"    {icon} {sev:<12s} {cnt} 个")
            print()

        top = stats.get("top_defects", [])
        if top:
            print("  【最常见缺陷 Top 10】:")
            for i, d in enumerate(top, 1):
                pid = d.get("pattern_id", "")
                cat = d.get("category", "")
                cnt = d.get("case_count", 0)
                desc = (d.get("description", "") or "")[:50]
                print(f"    {i:2d}. [{cat}] {pid}  ({cnt} 次)")
                if desc:
                    print(f"        {desc}")
            print()
        else:
            cprint("  (知识库为空，请先运行 'cw defect build' 构建)", "yellow")

    elif opts.action == "build":
        cprint("构建缺陷知识库...", "cyan")
        result = db.build_defect_knowledge()

        print()
        cprint("✓ 构建完成", "green")
        print(f"  新建模式数: {result.get('patterns_built', 0)}")
        print(f"  学习修复数: {result.get('fixes_learned', 0)}")

        cats = result.get("categories", {})
        if cats:
            print()
            print("  【类别分布】:")
            for cat, cnt in sorted(cats.items(), key=lambda x: -x[1]):
                print(f"    {cat:<20s} {cnt} 个")
        print()

    return True


# --------------------------------------------------------------------
# 任务管理 / 漏洞爆炸半径 / 符号 Git 历史
# --------------------------------------------------------------------

def _handle_task(args, db):
    """处理 task 子命令（任务管理：create/next/report/rollback）"""
    parser = argparse.ArgumentParser(prog="cw task", description="任务管理")
    sub = parser.add_subparsers(dest="action", required=True)

    # create：创建任务和步骤
    create_p = sub.add_parser("create", help="创建任务和步骤")
    create_p.add_argument("--title", required=True, help="任务标题")
    create_p.add_argument("--desc", default="", help="任务描述")
    create_p.add_argument("--steps", default="",
                          help='步骤 JSON 数组，例如 [{"action":"annotate","target_file":"a.py"}]')

    # next：领取下一个待执行步骤
    next_p = sub.add_parser("next", help="领取当前待执行的步骤")
    next_p.add_argument("task_id", help="任务 ID")

    # report：回报步骤执行结果
    report_p = sub.add_parser("report", help="回报步骤执行结果")
    report_p.add_argument("task_id", help="任务 ID")
    report_p.add_argument("step_id", help="步骤 ID")
    report_p.add_argument("--result", default="", help="执行结果描述")
    report_p.add_argument("--fail", action="store_true", help="标记为失败（默认成功）")

    # rollback：回滚变更
    rollback_p = sub.add_parser("rollback", help="回滚变更")
    rollback_p.add_argument("task_id", help="任务 ID")
    rollback_p.add_argument("step_id", help="步骤 ID（作为 change_id 定位回滚范围）")

    opts = parser.parse_args(args)

    if opts.action == "create":
        # 解析步骤 JSON（若提供）
        steps = []
        if opts.steps:
            try:
                steps = json.loads(opts.steps)
                if not isinstance(steps, list):
                    cprint("  ✗ --steps 必须是 JSON 数组", "red")
                    return True
            except json.JSONDecodeError as e:
                cprint(f"  ✗ --steps JSON 解析失败: {e}", "red")
                return True

        task_id = db.task_create(opts.title, opts.desc, steps, creator="agent")

        cprint("=== 任务创建成功 ===", "cyan", bold=True)
        print(f"  任务 ID: {task_id}")
        print(f"  标题: {opts.title}")
        if opts.desc:
            print(f"  描述: {opts.desc}")
        print(f"  步骤数: {len(steps)}")
        if steps:
            print()
            print("  【步骤列表】:")
            for i, s in enumerate(steps, 1):
                action = s.get("action", "")
                target = s.get("target_file", "") or s.get("target_symbol", "")
                print(f"    {i}. [{action}] {target}")
        print()
        return True

    elif opts.action == "next":
        result = db.task_next_step(opts.task_id)

        cprint("=== 领取下一步骤 ===", "cyan", bold=True)
        print(f"  任务 ID: {opts.task_id}")
        if result is None:
            cprint("  (没有待执行的步骤，任务可能已完成)", "yellow")
            return True

        print(f"  步骤 ID: {result.get('step_id', '')}")
        print(f"  步骤序号: {result.get('step_index', 0)}")
        print(f"  动作: {result.get('action', '')}")
        if result.get("target_file"):
            print(f"  目标文件: {result['target_file']}")
        if result.get("target_symbol"):
            print(f"  目标符号: {result['target_symbol']}")
        print(f"  状态: {result.get('status', '')}")
        print()

        # 检查项
        check_items = result.get("check_items", "")
        if check_items:
            print("  【检查项】:")
            if isinstance(check_items, list):
                for ci in check_items:
                    print(f"    - {ci}")
            else:
                print(f"    {check_items}")
            print()

        # 护栏阻断告警（block）
        alert = result.get("guardrail_alert")
        if alert:
            cprint("  [!] 护栏阻断告警", "red", bold=True)
            print(f"      决策: {alert.get('decision', '')}")
            print(f"      消息: {alert.get('message', '')}")
            findings = alert.get("findings", [])
            if findings:
                print(f"      发现数: {len(findings)}")
            cprint("      请先处理告警后再调用 task_next_step", "yellow")
            print()

        # 护栏警告（warn）
        warning = result.get("guardrail_warning")
        if warning:
            cprint("  [~] 护栏警告（可继续执行）", "yellow")
            print(f"      消息: {warning.get('message', '')}")
            print()

        # F7: 结构化指令展示（Agent 必须遵循的操作约束）
        si = result.get("structured_instruction")
        if si:
            cprint("  📐 结构化指令:", "cyan", bold=True)
            if si.get("read_targets"):
                print("    读取目标:")
                for rt in si["read_targets"]:
                    print(f"      {rt.get('file', '?')}:{rt.get('lines', '?')} "
                          f"({rt.get('symbol', '')})")
            if si.get("constraints"):
                print("    约束:")
                for c in si["constraints"]:
                    print(f"      • {c}")
            if si.get("checks"):
                print(f"    完成后检查: {', '.join(si['checks'])}")
            ctx = si.get("context", {})
            if ctx.get("callers"):
                callers_str = ", ".join(c.get("name", "") for c in ctx["callers"][:3])
                print(f"    调用者: {callers_str}")
            if ctx.get("existing_summary"):
                print(f"    已有摘要: {ctx['existing_summary'][:60]}...")
            print()

        return True

    elif opts.action == "report":
        success = not opts.fail
        result = db.task_report_step(
            opts.task_id, opts.step_id, opts.result, success, None
        )

        cprint("=== 步骤回报完成 ===", "cyan", bold=True)
        print(f"  任务 ID: {opts.task_id}")
        print(f"  步骤 ID: {opts.step_id}")
        print(f"  结果: {'成功' if success else '失败'}")
        if opts.result:
            print(f"  结果描述: {opts.result}")
        print()

        if result is None:
            cprint("  (没有更多待执行步骤，任务进入 review 状态)", "yellow")
        else:
            cprint("  【下一步骤已就绪】", "green")
            print(f"    步骤 ID: {result.get('step_id', '')}")
            print(f"    动作: {result.get('action', '')}")
            if result.get("target_file"):
                print(f"    目标文件: {result['target_file']}")
        print()
        return True

    elif opts.action == "rollback":
        # 优先调用 task_rollback_step；方法不存在则回退到 task_rollback
        if hasattr(db, "task_rollback_step"):
            result = db.task_rollback_step(opts.task_id, opts.step_id)
        else:
            result = db.task_rollback(opts.task_id, opts.step_id)

        cprint("=== 任务回滚 ===", "cyan", bold=True)
        print(f"  任务 ID: {opts.task_id}")
        print(f"  任务状态: {result.get('task_status', '')}")
        rolled = result.get("rolled_back_changes", [])
        print(f"  回滚变更数: {len(rolled)}")
        print()

        if rolled:
            print("  【回滚变更详情】:")
            for i, c in enumerate(rolled, 1):
                fp = c.get("file_path", "")
                restorable = c.get("restorable", False)
                icon = "[✓]" if restorable else "[✗]"
                print(f"    {i}. {icon} {fp}")
                if c.get("hash_before"):
                    print(f"        原始 hash: {c['hash_before'][:12]}...")

        note = result.get("note", "")
        if note:
            print()
            cprint(f"  注意: {note}", "yellow")
        print()
        return True

    return True


def _handle_vuln_blast(args, db):
    """处理 vuln-blast 子命令（漏洞爆炸半径分析）"""
    parser = argparse.ArgumentParser(prog="cw vuln-blast", description="漏洞爆炸半径分析")
    parser.add_argument("--finding-id", type=int, default=0,
                        help="指定 Semgrep finding ID（默认扫描全部）")
    parser.add_argument("--severity", default="",
                        help="严重度过滤（ERROR/WARN/INFO）")
    parser.add_argument("--depth", type=int, default=3,
                        help="调用图反向遍历深度（默认 3）")

    opts = parser.parse_args(args)
    result = db.get_vulnerability_blast_radius(
        finding_id=opts.finding_id, severity_filter=opts.severity, depth=opts.depth
    )

    cprint("=== 漏洞爆炸半径分析 ===", "cyan", bold=True)

    # 风险等级
    risk = result.get("risk_level", "low")
    risk_color = {"critical": "red", "high": "red",
                  "medium": "yellow", "low": "green"}.get(risk, "white")
    print("  风险等级: ", end="")
    cprint(risk, risk_color, bold=True)
    print(f"  漏洞总数: {result.get('total_findings', 0)}")
    print(f"  受影响符号数: {result.get('total_impacted_symbols', 0)}")
    print()

    # 过滤条件
    if opts.finding_id:
        print(f"  过滤: finding_id={opts.finding_id}")
    elif opts.severity:
        print(f"  过滤: severity={opts.severity}")
    print()

    # 各漏洞影响详情
    findings = result.get("findings", [])
    if not findings:
        cprint("  (无匹配的漏洞发现)", "yellow")
        return True

    print(f"  【漏洞影响详情】（{len(findings)} 个）:")
    sev_icon = {"ERROR": "[!]", "WARN": "[~]", "INFO": "[i]"}
    for i, f in enumerate(findings, 1):
        sev = f.get("severity", "")
        icon = sev_icon.get(sev, "[?]")
        print(f"  #{i} {icon} finding#{f.get('finding_id', '')}  [{sev}]")
        print(f"        规则: {f.get('rule_name', '') or f.get('rule_id', '')}")
        if f.get("file_path"):
            print(f"        文件: {f['file_path']}")
        if f.get("symbol_qualified"):
            print(f"        符号: {f['symbol_qualified']}")
        print(f"        影响符号数: {f.get('impacted_count', 0)}")

        # 影响树（复用 blast_radius 输出）
        br = f.get("blast_radius", {})
        by_layer = br.get("by_layer", {}) if br else {}
        if by_layer:
            layer_str = "  ".join(f"{k}:{v}" for k, v in by_layer.items())
            print(f"        跨层分布: {layer_str}")
        print()

    # 受影响符号汇总
    summary = result.get("impacted_symbols_summary", {})
    if summary:
        by_layer = summary.get("by_layer", {})
        if by_layer:
            print("  【受影响符号跨层汇总】:")
            print(f"    代码层: {by_layer.get('code', 0)}  DB 层: {by_layer.get('db', 0)}  "
                  f"API 层: {by_layer.get('api', 0)}  配置层: {by_layer.get('config', 0)}")
            print()

        high_risk = summary.get("high_risk_callers", [])
        if high_risk:
            print(f"  【高风险调用方】（{len(high_risk)} 个，被多个漏洞影响）:")
            for i, h in enumerate(high_risk[:10], 1):
                qn = h.get("qualified_name", "") if isinstance(h, dict) else str(h)
                print(f"    {i}. {qn}")
            if len(high_risk) > 10:
                print(f"    ... 还有 {len(high_risk) - 10} 个")
            print()

    return True


def _handle_symbol_history(args, db):
    """处理 symbol-history 子命令（符号 Git 变更历史）"""
    parser = argparse.ArgumentParser(prog="cw symbol-history", description="符号 Git 变更历史")
    parser.add_argument("symbol_hash", help="符号内容 hash")
    parser.add_argument("--limit", type=int, default=20, help="返回数量限制（默认 20）")

    opts = parser.parse_args(args)
    commits = db.get_symbol_commit_history(opts.symbol_hash, limit=opts.limit)

    cprint("=== 符号 Git 变更历史 ===", "cyan", bold=True)
    print(f"  符号 hash: {opts.symbol_hash[:12]}...")
    print(f"  变更次数: {len(commits)}")
    print()

    if not commits:
        cprint("  (无 Git 变更记录)", "yellow")
        return True

    print("  【提交历史】:")
    # 变更类型图标映射
    type_icon = {"added": "[+]", "modified": "[~]", "deleted": "[-]"}.get
    for i, c in enumerate(commits, 1):
        commit_hash = c.get("commit_hash", "")
        change_type = c.get("change_type", "")
        author = c.get("author", "")
        message = (c.get("message", "") or "").strip()
        ts = c.get("timestamp", 0)

        icon = type_icon(change_type, "[?]")
        print(f"  #{i} {icon} {commit_hash[:12]}...  ({change_type})")
        if author:
            print(f"        作者: {author}")
        if ts:
            t_str = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts))
            print(f"        时间: {t_str}")
        if message:
            # 消息截断显示首行
            msg_line = message.split("\n")[0][:80]
            print(f"        消息: {msg_line}")
        print()

    return True


# --------------------------------------------------------------------
# 检查门禁子命令（F6）
# --------------------------------------------------------------------

def _handle_check_gate(args, db):
    """处理 check-gate 子命令（手动运行检查门禁）

    用法:
      cw check-gate <task_id>              检查任务关联的变更文件
      cw check-gate <task_id> --resolve    标记门禁发现为已解决
    """
    parser = argparse.ArgumentParser(prog="cw check-gate", description="检查门禁（F6）")
    parser.add_argument("task_id", help="任务 ID")
    parser.add_argument("--resolve", action="store_true",
                        help="标记该任务的门禁发现为已解决（Agent 修复后调用）")
    parser.add_argument("--step-id", default="", help="关联步骤 ID（可选）")

    opts = parser.parse_args(args)

    if opts.resolve:
        result = db.resolve_gate_findings(task_id=opts.task_id)
        cprint("=== 门禁发现已标记解决 ===", "cyan", bold=True)
        print(f"  任务 ID: {opts.task_id}")
        print(f"  已解决发现数: {result.get('resolved_count', 0)}")
        print()
        return True

    # 查找任务关联的变更文件
    changed_files = db.get_task_changed_files(opts.task_id)
    if not changed_files:
        cprint(f"任务 {opts.task_id} 没有文件变更记录，跳过检查", "yellow")
        print()
        return True

    result = db.run_check_gate(opts.task_id, opts.step_id, changed_files)
    icon = "✅" if result["passed"] else "❌"
    cprint(f"{icon} 检查门禁结果", "cyan", bold=True)
    print(f"  任务 ID: {opts.task_id}")
    print(f"  结果: {result['summary']}")
    print(f"  检查项: {', '.join(result.get('checks_run', []))}")
    print()

    findings = result.get("findings", [])
    if findings:
        cprint(f"  发现 {len(findings)} 个问题:", "yellow")
        sev_icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}
        sev_color = {"ERROR": "red", "WARNING": "yellow", "INFO": "cyan"}
        for i, f in enumerate(findings, 1):
            sev = f.get("severity", "WARNING")
            f_icon = sev_icon.get(sev, "[?]")
            color = sev_color.get(sev, "white")
            cprint(f"  #{i} {f_icon} [{sev}] {f.get('file', '')}"
                   f":{f.get('line', '?')} ({f.get('check', '')})", color)
            if f.get("message"):
                print(f"        {f['message']}")
        print()

    if result.get("fix_required"):
        cprint("  ⚠ 门禁未通过，已插入 fix_gate_failure 步骤，请修复后重新检查", "yellow")
        print()

    return True


# --------------------------------------------------------------------
# 测试影响选择子命令
# --------------------------------------------------------------------

def _handle_test_impact(args, db):
    """处理 test-impact 子命令（改了某函数后需要运行哪些测试）

    通过反向调用链 BFS，找到所有直接和间接调用该函数的测试函数。
    """
    parser = argparse.ArgumentParser(
        prog="cw test-impact", description="测试影响选择"
    )
    parser.add_argument("qualified_name", help="被修改函数的限定名")
    opts = parser.parse_args(args)

    tests = db.test_impact_selection(qualified_name=opts.qualified_name)

    cprint("=== 测试影响分析 ===", "cyan", bold=True)
    print(f"  目标函数: {opts.qualified_name}")
    print(f"  需运行的测试数: {len(tests)}")
    print()

    if not tests:
        cprint("  (未找到关联测试，可能该函数无测试覆盖)", "yellow")
        return True

    print("  【需运行的测试】:")
    for i, t in enumerate(tests, 1):
        name = t.get("name", "")
        qn = t.get("qualified_name", "")
        fp = t.get("file_path", "")
        line = t.get("start_line", "?")
        print(f"  #{i} {name}")
        print(f"        限定名: {qn}")
        print(f"        位置: {fp}:{line}")
    print()

    return True


# --------------------------------------------------------------------
# 代码图谱 GC 子命令（归档被 .gitignore/.callwardenignore 命中的文件）
# --------------------------------------------------------------------

def _handle_gc(args, db):
    """处理 gc 子命令（代码图谱 GC）"""
    parser = argparse.ArgumentParser(prog="cw gc", description="代码图谱 GC（归档/复活/清除被 ignore 的文件）")
    sub = parser.add_subparsers(dest="action", required=True)

    # gc archive
    archive_p = sub.add_parser("archive", help="归档被 ignore 命中的文件")
    archive_p.add_argument("--force", action="store_true", help="Full GC：扫描所有活跃文件（默认只扫描 pending）")
    archive_p.add_argument("--dry-run", action="store_true", help="预演：只统计不实际归档")

    # gc restore
    restore_p = sub.add_parser("restore", help="复活已归档文件")
    restore_p.add_argument("--path", nargs="*", default=None, help="要复活的文件相对路径（为空则扫描所有归档文件）")
    restore_p.add_argument("--force", action="store_true", help="即使仍命中 ignore 也强制复活")

    # gc status
    sub.add_parser("status", help="查看 GC 状态")

    # gc purge
    purge_p = sub.add_parser("purge", help="彻底清除归档超过 N 天的文件")
    purge_p.add_argument("--older-than", type=int, default=30, help="归档超过多少天才清除（默认 30）")

    parsed = parser.parse_args(args)

    if parsed.action == "archive":
        result = db.gc_archive(force=parsed.force, dry_run=parsed.dry_run)
        mode = "Full GC" if parsed.force else "Young GC"
        dry = " [DRY-RUN]" if parsed.dry_run else ""
        cprint(f"\n=== {mode}{dry} ===", "cyan", bold=True)
        cprint(f"  扫描文件: {result['scanned']}", "dim")
        cprint(f"  归档文件: {result['archived']}", "yellow" if result["archived"] else "green")
        cprint(f"  已归档跳过: {result['skipped']}", "dim")
        if result["reasons"]:
            cprint(f"  归档原因:", "dim")
            for reason, count in result["reasons"].items():
                cprint(f"    {reason}: {count} 个", "dim")
        cprint()
        return True

    elif parsed.action == "restore":
        result = db.gc_restore(rel_paths=parsed.path, force=parsed.force)
        cprint(f"\n=== GC Restore ===", "cyan", bold=True)
        cprint(f"  扫描归档: {result['scanned']}", "dim")
        cprint(f"  复活文件: {result['restored']}", "green" if result["restored"] else "dim")
        cprint(f"  仍被忽略: {result['still_ignored']}", "dim")
        if result["restored"] > 0:
            cprint(f"  提示: 复活文件状态为 pending，下次 build 会自动重新解析", "yellow")
        cprint()
        return True

    elif parsed.action == "status":
        status = db.gc_status()
        cprint(f"\n=== GC Status ===", "cyan", bold=True)
        cprint(f"  活跃文件: {status['active_files']}", "green")
        cprint(f"  归档文件: {status['archived_files']}", "yellow" if status["archived_files"] else "dim")
        cprint(f"  已删除文件: {status['deleted_files']}", "dim")
        cprint(f"  归档率: {status['archive_ratio']*100:.1f}%", "dim")
        if status["archived_files"] > 0:
            cprint(f"  归档符号数: {status['archived_symbols']}", "dim")
            cprint(f"  归档调用关系数: {status['archived_calls']}", "dim")
        if status["recent_archives"]:
            cprint(f"\n  最近归档:", "dim")
            for r in status["recent_archives"]:
                from datetime import datetime
                ts = datetime.fromtimestamp(r["archived_at"]).strftime("%Y-%m-%d %H:%M")
                cprint(f"    [{ts}] {r['rel_path']} (符号 {r['symbol_count']}) <- {r['archive_reason']}", "dim")
        cprint()
        return True

    elif parsed.action == "purge":
        result = db.gc_purge(older_than_days=parsed.older_than)
        cprint(f"\n=== GC Purge ===", "cyan", bold=True)
        cprint(f"  清除文件: {result['purged_files']}", "yellow" if result["purged_files"] else "green")
        cprint(f"  清除符号: {result['purged_symbols']}", "dim")
        cprint(f"  清除调用关系: {result['purged_calls']}", "dim")
        cprint()
        return True

    return False


# --------------------------------------------------------------------
# 诊断与维护子命令
# --------------------------------------------------------------------

def _handle_doctor(args, db):
    """处理 doctor 子命令（环境诊断与维护）

    提供两种模式：
    1. cw doctor           - 检查环境、数据库状态、推荐优化
    2. cw doctor --add-defender-exclusion - 添加 Windows Defender 排除项（需管理员）
    """
    parser = argparse.ArgumentParser(prog="cw doctor", description="环境诊断与维护")
    parser.add_argument("--add-defender-exclusion", action="store_true",
                       help="添加 .callwarden 目录到 Windows Defender 排除项（需管理员权限）")
    opts = parser.parse_args(args)

    if opts.add_defender_exclusion:
        return _doctor_add_defender_exclusion(db)

    return _doctor_check(db)


def _doctor_check(db):
    """环境诊断：检查数据库状态、性能配置、Defender 状态等"""
    cprint("=== Call Warden 环境诊断 ===", "cyan", bold=True)
    print()

    # 1. 数据库基本信息
    cprint("[1] 数据库信息", "yellow", bold=True)
    db_path = db.db_path
    import os
    import sqlite3
    print(f"  路径: {db_path}")
    print(f"  大小: {os.path.getsize(db_path) / 1024 / 1024:.2f} MB")

    # PRAGMA 检查
    # 注意：SQLite 返回值可能是小写或数字（如 wal 而非 WAL，1 而非 NORMAL）
    # 用等价映射统一比较
    pragma_aliases = {
        "journal_mode": {"wal": "WAL", "wal2": "WAL"},
        "synchronous": {"0": "OFF", "1": "NORMAL", "2": "FULL", "3": "EXTRA"},
    }
    pragmas = {
        "journal_mode": "WAL",
        "synchronous": "NORMAL",
        "busy_timeout": "30000",
        "cache_size": "-64000",
        "mmap_size": "268435456",
    }
    print(f"  PRAGMA 配置:")
    all_pragma_ok = True
    for key, expected in pragmas.items():
        actual = db.conn.execute(f"PRAGMA {key}").fetchone()[0]
        actual_str = str(actual)
        # 应用别名映射
        aliases = pragma_aliases.get(key, {})
        actual_normalized = aliases.get(actual_str.lower(), aliases.get(actual_str, actual_str))
        expected_normalized = aliases.get(expected.lower(), aliases.get(expected, expected))
        ok = actual_normalized == expected_normalized
        mark = "✓" if ok else "✗"
        color = "green" if ok else "red"
        cprint(f"    {mark} {key} = {actual_str} (期望: {expected})", color)
        if not ok:
            all_pragma_ok = False
    print()

    # 2. WAL 文件检查
    cprint("[2] WAL 文件状态", "yellow", bold=True)
    wal_path = db_path + "-wal"
    shm_path = db_path + "-shm"
    print(f"  WAL 文件: {wal_path}")
    if os.path.exists(wal_path):
        wal_size = os.path.getsize(wal_path) / 1024
        print(f"    大小: {wal_size:.1f} KB")
        if wal_size > 1024 * 10:  # > 10MB
            cprint("    ! WAL 文件较大，建议运行 cw doctor --checkpoint", "yellow")
        else:
            print(f"    ✓ 大小正常")
    else:
        print(f"    ✓ 不存在（已 checkpoint）")
    print()

    # 3. Defender 排除项检查（仅 Windows）
    if sys.platform == "win32":
        cprint("[3] Windows Defender 排除项", "yellow", bold=True)
        callwarden_dir = os.path.dirname(db_path)
        try:
            import subprocess
            result = subprocess.run(
                ["powershell", "-Command",
                 f"Get-MpPreference | Select-Object -ExpandProperty ExclusionPath"],
                capture_output=True, text=True, timeout=10,
            )
            exclusions = result.stdout.strip()
            if callwarden_dir.lower() in exclusions.lower():
                cprint(f"  ✓ 已添加排除项: {callwarden_dir}", "green")
            else:
                cprint(f"  ✗ 未添加排除项（推荐添加以避免间歇性 SQLITE_CANTOPEN）", "red")
                cprint(f"    排除目录: {callwarden_dir}", "dim")
                cprint(f"    添加命令（需管理员）:", "dim")
                cprint(f"      cw doctor --add-defender-exclusion", "dim")
                cprint(f"    或手动执行:", "dim")
                cprint(f"      powershell -Command \"Add-MpPreference -ExclusionPath '{callwarden_dir}'\"",
                       "dim")
        except Exception as e:
            cprint(f"  ? 无法检查 Defender 状态: {e}", "yellow")
        print()

    # 4. 快速连接测试
    cprint("[4] 数据库连接测试", "yellow", bold=True)
    import time
    success = 0
    fail = 0
    for i in range(5):
        try:
            conn = sqlite3.connect(db_path)
            conn.execute("SELECT 1").fetchone()
            conn.close()
            success += 1
        except sqlite3.OperationalError:
            fail += 1
        time.sleep(0.1)
    if fail == 0:
        cprint(f"  ✓ 5 次连接测试全部成功", "green")
    else:
        cprint(f"  ✗ {fail}/5 次失败，Defender 间歇性锁定", "red")
    print()

    # 5. 总体评估
    cprint("[5] 总体评估", "yellow", bold=True)
    if all_pragma_ok and fail == 0:
        cprint("  ✓ 环境健康", "green")
    elif all_pragma_ok:
        cprint("  ~ 环境基本健康，但有间歇性连接失败（建议添加 Defender 排除项）", "yellow")
    else:
        cprint("  ✗ 环境需要优化（PRAGMA 配置不正确）", "red")
    print()

    return True


def _doctor_add_defender_exclusion(db):
    """添加 Windows Defender 排除项（需管理员权限）"""
    if sys.platform != "win32":
        cprint("✗ 此命令仅在 Windows 上可用", "red")
        return True

    import os
    import subprocess
    callwarden_dir = os.path.dirname(db.db_path)
    # 排除到 .callwarden 根目录（涵盖所有项目的 db）
    parent_dir = os.path.dirname(callwarden_dir)

    cprint("=== 添加 Windows Defender 排除项 ===", "cyan", bold=True)
    print(f"  将添加排除目录: {parent_dir}")
    cprint("  注意: 此操作需要管理员权限", "yellow")
    print()

    # 检查当前是否已是管理员
    try:
        import ctypes
        is_admin = ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        is_admin = False

    if not is_admin:
        cprint("✗ 当前不是管理员权限，正在尝试 UAC 提权...", "yellow")
        # 通过 PowerShell Start-Process -Verb RunAs 提权
        cmd = f"Add-MpPreference -ExclusionPath '{parent_dir}'"
        try:
            subprocess.Popen(
                ["powershell", "-Command",
                 f"Start-Process powershell -Verb RunAs -ArgumentList '-Command', '{cmd}; Start-Sleep 2'"],
            )
            cprint("✓ 已弹出 UAC 提示，请在弹出的窗口中确认", "green")
            print()
            print("  验证是否添加成功：")
            cprint(f"    cw doctor", "cyan")
        except Exception as e:
            cprint(f"✗ UAC 提权失败: {e}", "red")
            print()
            print("  请手动以管理员身份运行 PowerShell，执行：")
            cprint(f"    Add-MpPreference -ExclusionPath '{parent_dir}'", "yellow")
        return True

    # 已是管理员
    try:
        subprocess.run(
            ["powershell", "-Command",
             f"Add-MpPreference -ExclusionPath '{parent_dir}'"],
            capture_output=True, text=True, timeout=10,
        )
        cprint(f"✓ 已添加 Defender 排除项: {parent_dir}", "green")
    except Exception as e:
        cprint(f"✗ 添加失败: {e}", "red")

    return True


def create_parser() -> argparse.ArgumentParser:
    """创建命令行参数解析器（使用 i18n 文本）"""
    parser = argparse.ArgumentParser(
        description=t("cli.description") + "\n\n" + _SUBCOMMAND_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--lang", metavar="LANG", default=DEFAULT_LANG,
                       help="Language (zh_CN/en_US)")
    parser.add_argument("--workspace", metavar="ROOT", help=get_arg_help("workspace"))
    parser.add_argument("--root", metavar="ROOT", help=get_arg_help("root"))
    parser.add_argument("--list-workspaces", action="store_true", help=get_arg_help("list_workspaces"))
    parser.add_argument("--register-workspace", nargs=2, metavar=("NAME", "ROOT"), help=get_arg_help("register_workspace"))
    parser.add_argument("--set-workspace", metavar="ID_OR_NAME", help=get_arg_help("set_workspace"))
    parser.add_argument("--delete-workspace", metavar="ID_OR_NAME", help=get_arg_help("delete_workspace"))
    parser.add_argument("--init", action="store_true", help=get_arg_help("init"))
    parser.add_argument("--force", action="store_true", help=get_arg_help("force"))
    parser.add_argument("--watch", action="store_true", help=get_arg_help("watch"))
    parser.add_argument("--stats", action="store_true", help=get_arg_help("stats"))
    parser.add_argument("--status", action="store_true", help=get_arg_help("status"))
    parser.add_argument("--query", nargs=2, metavar=("NAME", "FILE"), help=get_arg_help("query"))
    parser.add_argument("--callers", metavar="NAME", help=get_arg_help("callers"))
    parser.add_argument("--callees", metavar="NAME", help=get_arg_help("callees"))
    parser.add_argument("--topo", action="store_true", help=get_arg_help("topo"))
    parser.add_argument("--topo-limit", type=int, default=50, help=get_arg_help("topo_limit"))
    parser.add_argument("--file", metavar="PATH", help=get_arg_help("file"))
    parser.add_argument("--refresh", metavar="PATH", help=get_arg_help("refresh"))
    parser.add_argument("--history", metavar="NAME", help=get_arg_help("history"))
    parser.add_argument("--show-content", action="store_true", help=get_arg_help("show_content"))
    parser.add_argument("--diff", nargs=2, metavar=("HASH1", "HASH2"), help=get_arg_help("diff"))
    parser.add_argument("--changes", metavar="SINCE", nargs="?", const="1h", help=get_arg_help("changes"))
    parser.add_argument("--changes-detail", action="store_true", help=get_arg_help("changes_detail"))
    parser.add_argument("--restore-comment", metavar="SPEC", help=get_arg_help("restore_comment"))
    parser.add_argument("--restore-all-comments", action="store_true", help=get_arg_help("restore_all_comments"))
    parser.add_argument("--restore-file", metavar="PATH", help=get_arg_help("restore_file"))
    parser.add_argument("--preview", action="store_true", help=get_arg_help("preview"))
    parser.add_argument("--comment-coverage", action="store_true", help=get_arg_help("comment_coverage"))
    parser.add_argument("--coverage-by", metavar="GROUP", default="module", help=get_arg_help("coverage_by"))
    parser.add_argument("--uncommented", metavar="KIND", nargs="?", const="fn", help=get_arg_help("uncommented"))
    parser.add_argument("--uncommented-module", metavar="MODULE", help=get_arg_help("uncommented_module"))
    parser.add_argument("--uncommented-limit", metavar="N", type=int, default=50, help=get_arg_help("uncommented_limit"))
    parser.add_argument("--search", metavar="QUERY", help=get_arg_help("search"))
    parser.add_argument("--search-kind", metavar="KIND", help=get_arg_help("search_kind"))
    parser.add_argument("--search-limit", metavar="N", type=int, default=50, help=get_arg_help("search_limit"))
    parser.add_argument("--symbol", metavar="QUALIFIED_NAME", help=get_arg_help("symbol"))
    parser.add_argument("--impact", metavar="QUALIFIED_NAME", help=get_arg_help("impact"))
    parser.add_argument("--call-chain", metavar="QUALIFIED_NAME", help=get_arg_help("call_chain"))
    parser.add_argument("--chain-depth", metavar="N", type=int, default=10, help=get_arg_help("chain_depth"))
    parser.add_argument("--top-callers", metavar="N", type=int, nargs="?", const=20, help=get_arg_help("top_callers"))
    parser.add_argument("--top-callers-module", metavar="MODULE", help=get_arg_help("top_callers_module"))
    parser.add_argument("--orphan-symbols", metavar="KIND", nargs="?", const="fn", help=get_arg_help("orphan_symbols"))
    parser.add_argument("--orphan-module", metavar="MODULE", help=get_arg_help("orphan_module"))
    parser.add_argument("--orphan-limit", metavar="N", type=int, default=50, help=get_arg_help("orphan_limit"))
    parser.add_argument("--deepest", metavar="N", type=int, nargs="?", const=20, help=get_arg_help("deepest"))
    parser.add_argument("--deepest-module", metavar="MODULE", help=get_arg_help("deepest_module"))
    parser.add_argument("--module-calls", metavar="N", type=int, nargs="?", const=20, help=get_arg_help("module_calls"))
    parser.add_argument("--detect-cycles", action="store_true", help=get_arg_help("detect_cycles"))
    parser.add_argument("--cycle-depth", metavar="N", type=int, default=10, help=get_arg_help("cycle_depth"))
    parser.add_argument("--export-module-graph", metavar="FORMAT", nargs="?", const="mermaid", help=get_arg_help("export_module_graph"))
    parser.add_argument("--graph-output", metavar="FILE", help=get_arg_help("graph_output"))
    parser.add_argument("--call-heatmap", metavar="GROUP_BY", nargs="?", const="module", help=get_arg_help("call_heatmap"))
    parser.add_argument("--heatmap-limit", metavar="N", type=int, default=20, help=get_arg_help("heatmap_limit"))
    parser.add_argument("--test-coverage", action="store_true", help=get_arg_help("test_coverage"))
    parser.add_argument("--function-issues", metavar="FN", nargs="?", const="", help=get_arg_help("function_issues"))
    parser.add_argument("--issue-summary", action="store_true", help=get_arg_help("issue_summary"))
    parser.add_argument("--issue-type", metavar="TYPE", help=get_arg_help("issue_type"))
    parser.add_argument("--issue-module", metavar="MODULE", help=get_arg_help("issue_module"))
    parser.add_argument("--issue-limit", metavar="N", type=int, default=30, help=get_arg_help("issue_limit"))
    parser.add_argument("--semgrep", metavar="PATH", nargs="*", help=get_arg_help("semgrep"))
    parser.add_argument("--semgrep-config", metavar="CONFIG", default="p/default", help=get_arg_help("semgrep_config"))
    parser.add_argument("--semgrep-scan-lang", metavar="LANG", nargs="*", help=get_arg_help("semgrep_scan_lang"))
    parser.add_argument("--semgrep-timeout", metavar="N", type=int, default=180, help=get_arg_help("semgrep_timeout"))
    parser.add_argument("--semgrep-quick", action="store_true", help=get_arg_help("semgrep_quick"))
    parser.add_argument("--semgrep-save", action="store_true", help=get_arg_help("semgrep_save"))
    parser.add_argument("--semgrep-list", nargs="?", const="", metavar="FILTER", help=get_arg_help("semgrep_list"))
    parser.add_argument("--semgrep-severity", metavar="SEV", help=get_arg_help("semgrep_severity"))
    parser.add_argument("--semgrep-list-lang", metavar="LANG", help=get_arg_help("semgrep_list_lang"))
    parser.add_argument("--semgrep-stats", action="store_true", help=get_arg_help("semgrep_stats"))
    parser.add_argument("--semgrep-limit", metavar="N", type=int, default=50, help=get_arg_help("semgrep_limit"))
    
    # Git 集成
    parser.add_argument("--git-import", metavar="N", type=int, nargs="?", const=100, help="导入 Git 历史记录（可指定最大 commit 数，默认 100）")
    parser.add_argument("--git-log", metavar="N", type=int, nargs="?", const=20, help="显示 Git commit 历史（默认 20 条）")
    parser.add_argument("--git-show", metavar="COMMIT", help="显示指定 commit 的变更详情")
    parser.add_argument("--git-stats", action="store_true", help="显示 Git 集成统计信息")
    
    # 代码度量
    parser.add_argument("--metrics", action="store_true", help="显示代码度量汇总统计")
    parser.add_argument("--complexity", metavar="N", type=int, nargs="?", const=20, help="显示圈复杂度最高的函数（默认 20 个）")
    parser.add_argument("--complexity-module", metavar="MODULE", help="圈复杂度模块过滤（前缀匹配）")
    parser.add_argument("--coupling", action="store_true", help="显示模块耦合度分析")
    parser.add_argument("--largest-fns", metavar="N", type=int, nargs="?", const=20, help="显示代码行数最多的函数（默认 20 个）")
    parser.add_argument("--coupled-fns", metavar="N", type=int, nargs="?", const=20, help="显示耦合度最高的函数（默认 20 个）")
    parser.add_argument("--fn-metrics", metavar="NAME", help="显示指定函数的详细度量")

    # 语义搜索
    parser.add_argument("--semantic-search", metavar="QUERY", help="语义搜索函数（自然语言查询）")
    parser.add_argument("--embed", action="store_true", help="为所有函数生成向量嵌入")
    parser.add_argument("--embed-force", action="store_true", help="强制重新嵌入所有函数")
    parser.add_argument("--similar", metavar="NAME", help="查找与指定函数相似的其他函数")

    # 任务管理
    parser.add_argument("--task-list", action="store_true", help="列出所有任务")
    parser.add_argument("--task-show", metavar="TASK_ID", help="显示任务详情")

    # 项目简报和仓库地图
    parser.add_argument("--brief", action="store_true", help="显示项目简报")
    parser.add_argument("--map", action="store_true", help="显示仓库模块依赖图")
    parser.add_argument("--map-format", choices=["text", "mermaid"], default="text", help="仓库图格式")

    # 覆盖率
    parser.add_argument("--coverage-import", metavar="FILE", help="导入覆盖率报告（LCOV/Cobertura）")
    parser.add_argument("--coverage-format", choices=["lcov", "cobertura"], default="lcov", help="覆盖率报告格式")
    parser.add_argument("--coverage-fn", metavar="NAME", help="显示函数的覆盖率")
    parser.add_argument("--coverage-uncovered", action="store_true", help="显示未覆盖的函数")

    # 所有权
    parser.add_argument("--who", metavar="FILE", help="查询文件负责人")
    parser.add_argument("--ownership-map", action="store_true", help="显示所有权映射")

    return parser


def main():
    """CLI 主入口函数"""
    # 代码守护者架构子命令拦截（四大支柱）
    # 子命令格式: cw <subcommand> [options]，如 cw defect stats
    if len(sys.argv) > 1 and sys.argv[1] in _SUBCOMMANDS:
        _run_subcommand_mode()
        return

    # 显示 --help 时，先打印子命令概览（确保 head -50 可见）
    # argparse 默认将 description 放在超长 usage 之后，子命令信息会被推到 50 行之外
    if len(sys.argv) > 1 and sys.argv[1] in ("--help", "-h"):
        print(_SUBCOMMAND_HELP)
        print()

    # 第一阶段：先解析 --lang 参数（不创建完整 parser）
    pre_parser = argparse.ArgumentParser(add_help=False)
    pre_parser.add_argument("--lang", metavar="LANG", default=DEFAULT_LANG)
    pre_args, _ = pre_parser.parse_known_args()
    
    # 设置语言
    set_language(pre_args.lang)
    
    # 第二阶段：用正确的语言创建完整 parser 并解析所有参数
    parser = create_parser()
    args = parser.parse_args()
    
    # 确定工作区根目录
    workspace_root = None
    if args.workspace:
        workspace_root = args.workspace
    elif args.root:
        workspace_root = args.root
    else:
        # 自动检测：从当前目录向上查找项目根
        cwd = os.getcwd()
        detected = detect_project_root(cwd)
        if detected:
            workspace_root = detected
    
    # 初始化数据库
    db = CodeGraphDB(workspace_root=workspace_root) if workspace_root else CodeGraphDB()

    # 如果自动检测到了工作区，自动注册并设置为活动工作区
    if workspace_root and not args.list_workspaces:
        ws_name = get_default_workspace_name(workspace_root)
        # 检查是否已注册
        existing = None
        for ws in db.list_workspaces():
            if ws["root_path"] == workspace_root:
                existing = ws
                break
        if not existing:
            ws_id = db.register_workspace(ws_name, workspace_root)
            db.set_active_workspace(ws_id)
        elif not existing.get("is_active"):
            db.set_active_workspace(existing["id"])
    
    try:
        # 工作区管理命令
        if args.list_workspaces:
            workspaces = db.list_workspaces()
            print(f"工作区列表（共 {len(workspaces)} 个）:")
            for ws in workspaces:
                active_mark = " [活动]" if ws.get("is_active") else ""
                print(f"  [{ws['id']}] {ws['name']}{active_mark}")
                print(f"      路径: {ws['root_path']}")
                if ws.get("description"):
                    print(f"      描述: {ws['description']}")
            return
        
        if args.register_workspace:
            name, root = args.register_workspace
            ws_id = db.register_workspace(name, root)
            print(f"工作区已注册: ID={ws_id}, name={name}, root={root}")
            return
        
        if args.set_workspace:
            ws_arg = args.set_workspace
            # 尝试转换为 int（ID）
            try:
                ws_id = int(ws_arg)
                success = db.set_active_workspace(ws_id)
            except ValueError:
                success = db.set_active_workspace(ws_arg)
            if success:
                active = db.get_active_workspace()
                print(f"已切换到活动工作区: {active['name']} ({active['root_path']})")
            else:
                print(f"切换失败: 未找到工作区 '{ws_arg}'")
            return
        
        if args.delete_workspace:
            ws_arg = args.delete_workspace
            # 尝试转换为 int（ID）
            try:
                ws_id = int(ws_arg)
                success = db.delete_workspace(ws_id)
            except ValueError:
                success = db.delete_workspace(ws_arg)
            if success:
                print(f"工作区 '{ws_arg}' 已删除")
            else:
                print(f"删除失败: 未找到工作区 '{ws_arg}'")
            return
        
        if args.init:
            if args.force:
                print("构建代码知识图谱（强制重新解析）...")
            else:
                print("构建代码知识图谱（增量构建）...")
            db.build_full_graph(force=args.force)
        
        elif args.watch:
            watcher = FileWatcher(db)
            watcher.start()
        
        elif args.stats:
            stats = db.get_stats()
            print(json.dumps(stats, indent=2, ensure_ascii=False))
        
        elif args.status:
            status = db.get_status()
            ws = status["workspace"]
            fi = status["files"]
            sy = status["symbols"]
            ca = status["calls"]

            def fmt_size(n):
                if n < 1024:
                    return f"{n} B"
                if n < 1024 * 1024:
                    return f"{n/1024:.1f} KB"
                return f"{n/1024/1024:.1f} MB"

            def fmt_ago(ts):
                if not ts:
                    return get_msg("status_never_built", default="从未构建")
                delta = time.time() - ts
                if delta < 60:
                    return get_msg("status_just_now", default="刚刚")
                if delta < 3600:
                    m = int(delta // 60)
                    return get_msg("status_minutes_ago", default=f"{m} 分钟前", m=m)
                if delta < 86400:
                    h = int(delta // 3600)
                    return get_msg("status_hours_ago", default=f"{h} 小时前", h=h)
                d = int(delta // 86400)
                return get_msg("status_days_ago", default=f"{d} 天前", d=d)

            print()
            print(f"  {get_msg('status_title', default='=== 代码图谱状态 ===')}")
            print()
            print(f"  {get_msg('status_workspace', default='工作区')}: {ws['name']}")
            print(f"  {get_msg('status_root', default='路径')}: {ws['root']}")
            print(f"  {get_msg('status_db_size', default='数据库大小')}: {fmt_size(ws['db_size'])}")
            print(f"  {get_msg('status_last_build', default='上次构建')}: {fmt_ago(status['last_build'])}")
            print()
            print(f"  {get_msg('status_files_title', default='── 文件 ──')}")
            print(f"    {get_msg('status_files_on_disk', default='磁盘文件')}: {fi['on_disk']}  ({get_msg('status_files_tracked', default='已跟踪')}: {fi['tracked']})")
            if fi["new"]:
                print(f"    {get_msg('status_files_new', default='新增未入库')}: {fi['new']}  {', '.join(fi['new_files'][:5])}{'...' if len(fi['new_files'])>5 else ''}")
            if fi["stale"]:
                print(f"    {get_msg('status_files_stale', default='已过期（需刷新）')}: {fi['stale']}  {', '.join(fi['stale_files'][:5])}{'...' if len(fi['stale_files'])>5 else ''}")
            if fi["deleted"]:
                print(f"    {get_msg('status_files_deleted', default='已删除（仍在库中）')}: {fi['deleted']}  {', '.join(fi['deleted_files'][:5])}{'...' if len(fi['deleted_files'])>5 else ''}")
            if fi["by_language"]:
                parts = []
                for ext, cnt in sorted(fi["by_language"].items(), key=lambda x: -x[1])[:6]:
                    parts.append(f"{ext}: {cnt}")
                print(f"    {get_msg('status_by_language', default='按语言分布')}: {', '.join(parts)}")
            print()
            print(f"  {get_msg('status_symbols_title', default='── 符号 ──')}")
            print(f"    {get_msg('status_symbols_total', default='总符号数')}: {sy['total']}")
            kind_parts = []
            kind_names = {"fn": get_msg("kind_fn", default="函数"), "test_fn": get_msg("kind_test_fn", default="测试"), "struct": get_msg("kind_struct", default="结构体"),
                          "enum": get_msg("kind_enum", default="枚举"), "trait": get_msg("kind_trait", default="trait"), "impl": "impl",
                          "const": "const", "static": "static", "method": get_msg("kind_method", default="方法"),
                          "class": get_msg("kind_class", default="类"), "interface": get_msg("kind_interface", default="接口")}
            for kind, cnt in sorted(sy["by_kind"].items(), key=lambda x: -x[1])[:8]:
                kn = kind_names.get(kind, kind)
                kind_parts.append(f"{kn}: {cnt}")
            print(f"    {get_msg('status_by_kind', default='类型分布')}: {', '.join(kind_parts)}")
            print(f"    {get_msg('status_uncommented_fns', default='未注释函数')}: {sy['uncommented_fns']}")
            print()
            print(f"  {get_msg('status_calls_title', default='── 调用关系 ──')}")
            print(f"    {get_msg('status_calls_total', default='总调用数')}: {ca['total']}")
            print(f"    {get_msg('status_calls_resolved', default='已解析')}: {ca['resolved']}  ({get_msg('status_calls_rate', default='解析率')}: {ca['resolve_rate']}%)")
            print(f"    {get_msg('status_calls_cross', default='跨文件')}: {ca['cross_file']}")
            print()
            if status["needs_rebuild"]:
                print(f"  ⚠ {get_msg('status_rebuild_hint', default='提示: 有文件变更，建议运行 cw --init 增量更新')}")
            else:
                print(f"  {get_msg('status_up_to_date', default='✓ 代码图谱已是最新')}")
            print()

        elif args.query:
            name, file_path = args.query
            result = db.get_symbol_location(name, file_path)
            if result:
                print(json.dumps(result, indent=2, ensure_ascii=False))
            else:
                print(f"未找到符号: {name}")
        
        elif args.callers:
            callers = db.get_callers(args.callers)
            print(f"调用 {args.callers} 的函数（{len(callers)} 个）:")
            for c in callers:
                cross = " [跨文件]" if c["is_cross_file"] else ""
                print(f"  {c['caller_file']}:{c['call_line']} -> {c['caller_name']}{cross}")
        
        elif args.callees:
            callees = db.get_callees(args.callees)
            print(f"{args.callees} 调用的函数（{len(callees)} 个）:")
            for c in callees:
                cross = " [跨文件]" if c["is_cross_file"] else ""
                file_info = f" ({c['callee_file']})" if c["callee_file"] else " [未解析]"
                print(f"  line {c['call_line']}: {c['callee_name']}{cross}{file_info}")
        
        elif args.topo:
            order = db.get_topological_order(args.topo_limit)
            print(f"拓扑排序（前 {len(order)} 个，按 depth 升序 = 底层在前）:")
            for i, sym in enumerate(order):
                print(f"  {i+1}. depth={sym['depth']:2d}  {sym['path']}:{sym['start_line']}  {sym['name']}")
        
        elif args.file:
            symbols = db.get_file_symbols(args.file)
            print(f"{args.file} 内的符号（{len(symbols)} 个）:")
            for s in symbols:
                print(f"  {s['start_line']}-{s['end_line']}: {s['kind']} {s['name']} ({s['visibility']})")
        
        elif args.refresh:
            db.refresh_file(args.refresh)
            print(f"已刷新: {args.refresh}")
        
        elif args.history:
            history = db.get_history(args.history)
            if not history:
                print(f"未找到函数: {args.history}")
            else:
                print(f"函数 {args.history} 的历史版本（{len(history)} 个）:")
                for i, h in enumerate(history, 1):
                    current = " [当前]" if h["is_current"] else ""
                    parsed_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(h["parsed_at"]))
                    print(f"  {i}. v{h['version_num']}{current} | {parsed_time} | hash={h['symbol_hash'][:12]}... | {h['file_path']}:{h['start_line']}-{h['end_line']}")
                    
                    if args.show_content:
                        content = db.get_symbol_content_by_hash(h["symbol_hash"])
                        if content:
                            print(f"     内容:")
                            for line in content["content"].split("\n")[:5]:
                                print(f"       {line}")
                            if len(content["content"].split("\n")) > 5:
                                print(f"       ...")
        
        elif args.diff:
            hash1, hash2 = args.diff
            content1 = db.get_symbol_content_by_hash(hash1)
            content2 = db.get_symbol_content_by_hash(hash2)
            
            if not content1:
                print(f"未找到 hash: {hash1}")
            elif not content2:
                print(f"未找到 hash: {hash2}")
            else:
                print(f"对比 {hash1[:12]}... vs {hash2[:12]}...")
                print(f"函数: {content1['qualified_name']}")
                print(f"类型: {content1['kind']}")
                print("-" * 40)
                
                lines1 = content1["content"].split("\n")
                lines2 = content2["content"].split("\n")
                
                # 简单对比：显示差异行数
                max_lines = max(len(lines1), len(lines2))
                for i in range(max_lines):
                    l1 = lines1[i] if i < len(lines1) else ""
                    l2 = lines2[i] if i < len(lines2) else ""
                    if l1 != l2:
                        if l1:
                            print(f"  - {i+1}: {l1}")
                        if l2:
                            print(f"  + {i+1}: {l2}")
        
        elif args.changes:
            result = db.get_recent_changes(args.changes)
            changed_files = result["changed_files"]
            changed_funcs = result["changed_functions"]
            
            # 只显示真正有变化的文件（有多个版本的）
            multi_version_files = [f for f in changed_files if f["version_num"] > 1]
            
            print(f"最近 {args.changes} 内的变化:")
            print(f"  新增文件版本数: {len(changed_files)}")
            print(f"  有内容变化的文件数: {len(multi_version_files)}")
            print(f"  变化的函数数: {len(changed_funcs)}")
            print()
            
            if multi_version_files:
                print("【有变化的文件】")
                for fv in multi_version_files:
                    parsed_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(fv["parsed_at"]))
                    current = " [当前]" if fv["is_current"] else ""
                    print(f"  v{fv['version_num']}{current} | {parsed_time} | {fv['path']}")
            
            if changed_funcs:
                print()
                print("【变化的函数】")
                for cf in changed_funcs:
                    parsed_time = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(cf["parsed_at"]))
                    type_tag = f"[{cf['change_type']}]"
                    print(f"  {type_tag:4} {cf['qualified_name']}")
                    print(f"       {cf['file_path']}:{cf['line']} | {parsed_time}")
                    
                    if args.changes_detail:
                        prev = cf['prev_hash']
                        curr = cf['curr_hash']
                        print(f"       prev: {prev[:12]}..." if prev else "       prev: (无)")
                        print(f"       curr: {curr[:12]}..." if curr else "       curr: (无)")
        
        elif args.restore_comment:
            result = db.restore_comment(args.restore_comment, preview=args.preview)
            
            if not result["success"]:
                print(f"恢复失败: {result['error']}")
            elif result.get("preview"):
                print(f"预览恢复结果:")
                print(f"  函数: {result['qualified_name']}")
                print(f"  文件: {result['file_path']}")
                print(f"  当前注释: {result['old_comment']}")
                print(f"  恢复的注释:")
                print(result['new_comment'])
                print()
                print(f"新文件内容预览:")
                print(result['new_content_preview'])
            else:
                print(f"恢复成功!")
                print(f"  函数: {result['qualified_name']}")
                print(f"  文件: {result['file_path']}")
                print(f"  从版本 {result['restored_from']} 恢复了 {result['comment_lines']} 行注释")
        
        elif args.restore_all_comments:
            file_filter = args.restore_file if args.restore_file else None
            result = db.restore_all_comments(preview=args.preview, file_filter=file_filter)
            
            mode = "预览" if args.preview else "恢复"
            print(f"批量{mode}完成!")
            print(f"  找到有注释历史的函数: {result['total_found']} 个")
            print(f"  已恢复: {result['restored']} 个")
            print(f"  已跳过（已有注释）: {result['skipped']} 个")
            print(f"  失败: {result['failed']} 个")
            print(f"  涉及文件: {len(result['files'])} 个")
            
            if result["files"]:
                print()
                print(f"按文件统计:")
                for fpath, finfo in sorted(result["files"].items()):
                    if finfo["restored"] > 0 or finfo["failed"] > 0:
                        print(f"  {fpath}: 恢复 {finfo['restored']}, 跳过 {finfo['skipped']}, 失败 {finfo['failed']} / 共 {finfo['total']}")
            
            if result["errors"]:
                print()
                print(f"错误:")
                for err in result["errors"]:
                    print(f"  - {err}")
        
        elif args.comment_coverage:
            result = db.get_comment_coverage(group_by=args.coverage_by)
            
            print(f"注释覆盖率统计")
            print(f"  总计: {result['total']} 个符号")
            print(f"  已注释: {result['commented']} 个")
            print(f"  覆盖率: {result['coverage']}%")
            print()
            
            print(f"按类型分布:")
            for kind, info in sorted(result["by_kind"].items(), key=lambda x: -x[1]["total"]):
                pct = round(info["commented"] / info["total"] * 100, 1) if info["total"] > 0 else 0
                bar_len = int(pct / 5)
                bar = "█" * bar_len + "░" * (20 - bar_len)
                print(f"  {bar} {pct:5.1f}%  {kind:12s}  ({info['commented']}/{info['total']})")
            
            if result.get("by_module"):
                print()
                print(f"按模块分布（前 30 个）:")
                modules = sorted(result["by_module"].items(), key=lambda x: x[1]["coverage"])
                for i, (mod, info) in enumerate(modules[:30]):
                    pct = info["coverage"]
                    bar_len = int(pct / 5)
                    bar = "█" * bar_len + "░" * (20 - bar_len)
                    print(f"  {bar} {pct:5.1f}%  {mod:50s}  ({info['commented']}/{info['total']})")
                if len(modules) > 30:
                    print(f"  ... 还有 {len(modules) - 30} 个模块")
            
            if result.get("by_file"):
                print()
                print(f"按文件分布（前 30 个）:")
                files = sorted(result["by_file"].items(), key=lambda x: x[1]["coverage"])
                for i, (fpath, info) in enumerate(files[:30]):
                    pct = info["coverage"]
                    bar_len = int(pct / 5)
                    bar = "█" * bar_len + "░" * (20 - bar_len)
                    print(f"  {bar} {pct:5.1f}%  {fpath:50s}  ({info['commented']}/{info['total']})")
                if len(files) > 30:
                    print(f"  ... 还有 {len(files) - 30} 个文件")
        
        elif args.uncommented is not None:
            kind = args.uncommented
            mod_filter = args.uncommented_module
            limit = args.uncommented_limit
            
            symbols = db.get_uncommented_symbols(kind=kind, module_filter=mod_filter)
            
            filter_info = f"（模块: {mod_filter}）" if mod_filter else ""
            print(f"未注释的 {kind} 列表 {filter_info}（共 {len(symbols)} 个，显示前 {min(limit, len(symbols))} 个）:")
            print()
            
            for i, sym in enumerate(symbols[:limit]):
                depth = sym["depth"] if sym["depth"] >= 0 else "?"
                sig = sym.get("signature", "")[:60] if sym.get("signature") else ""
                print(f"  [{i+1:3d}] depth={depth:>3}  {sym['qualified_name']}")
                print(f"         {sym['file_path']}:{sym['start_line']}")
                if sig:
                    print(f"         {sig}")
            
            if len(symbols) > limit:
                print()
                print(f"... 还有 {len(symbols) - limit} 个，用 --uncommented-limit N 调整显示数量")
        
        elif args.search:
            kind = args.search_kind
            limit = args.search_limit
            
            symbols = db.search_symbols(args.search, kind=kind, limit=limit)
            
            kind_info = f"（类型: {kind}）" if kind else ""
            print(f"搜索结果: '{args.search}' {kind_info}（共 {len(symbols)} 个，显示前 {min(limit, len(symbols))} 个）:")
            print()
            
            for i, sym in enumerate(symbols[:limit]):
                depth = sym["depth"] if sym["depth"] >= 0 else "?"
                sig = sym.get("signature", "")[:50] if sym.get("signature") else ""
                comment_mark = "✓" if sym["has_comment"] else " "
                print(f"  [{i+1:3d}] depth={depth:>3} [{comment_mark}] {sym['kind']:8s} {sym['qualified_name']}")
                print(f"         {sym['file_path']}:{sym['start_line']}")
                if sig:
                    print(f"         {sig}")
            
            if len(symbols) >= limit:
                print()
                print(f"... 用 --search-limit N 调整显示数量")
        
        elif args.symbol:
            detail = db.get_symbol_detail(args.symbol)
            
            if not detail:
                print(f"未找到符号: {args.symbol}")
                print("提示: 用 --search 搜索符号名称")
            else:
                print(f"符号详情")
                print(f"  名称: {detail['qualified_name']}")
                print(f"  类型: {detail['kind']}")
                print(f"  深度: {detail['depth']}")
                print(f"  文件: {detail['file_path']}:{detail['start_line']}-{detail['end_line']}")
                print(f"  签名: {detail['signature'][:100] if detail['signature'] else '(无)'}")
                print(f"  注释: {'有' if detail['has_comment'] else '无'}")
                if detail.get("comment_content"):
                    print(f"  注释内容:")
                    for line in detail["comment_content"].split("\n")[:10]:
                        print(f"    {line}")
                
                print()
                print(f"调用的函数（{len(detail['calls_out'])} 个）:")
                if detail["calls_out"]:
                    for call in detail["calls_out"][:20]:
                        target = call["target_name"]
                        line = call.get("call_line", "")
                        line_info = f" (line {line})" if line else ""
                        print(f"  → {target}{line_info}")
                    if len(detail["calls_out"]) > 20:
                        print(f"  ... 还有 {len(detail['calls_out']) - 20} 个")
                else:
                    print(f"  (无)")
                
                print()
                print(f"被谁调用（{len(detail['called_by'])} 个）:")
                if detail["called_by"]:
                    for call in detail["called_by"][:20]:
                        caller = call["caller_name"]
                        line = call.get("call_line", "")
                        line_info = f" (line {line})" if line else ""
                        print(f"  ← {caller}{line_info}")
                    if len(detail["called_by"]) > 20:
                        print(f"  ... 还有 {len(detail['called_by']) - 20} 个")
                else:
                    print(f"  (无)")
        
        elif args.impact:
            result = db.get_call_chain_up(args.impact, max_depth=args.chain_depth)
            
            print(f"影响面分析（向上追踪）: {result['start']}")
            print(f"  总上游函数数: {result['total_upstream']}")
            print(f"  最大深度: {result['max_depth_reached']}")
            print()
            
            for level in result["levels"]:
                print(f"第 {level['depth']} 层（{level['count']} 个调用者）:")
                for item in level["callers"][:15]:
                    print(f"  ← {item['caller']}")
                if level["count"] > 15:
                    print(f"  ... 还有 {level['count'] - 15} 个")
                print()
        
        elif args.call_chain:
            result = db.get_call_chain_down(args.call_chain, max_depth=args.chain_depth)
            
            print(f"调用链向下: {result['start']}")
            print(f"  总下游函数数: {result['total_downstream']}")
            print(f"  最大深度: {result['max_depth_reached']}")
            print()
            
            for level in result["levels"]:
                print(f"第 {level['depth']} 层（{level['count']} 个被调用）:")
                for item in level["callees"][:15]:
                    print(f"  → {item['callee']}")
                if level["count"] > 15:
                    print(f"  ... 还有 {level['count'] - 15} 个")
                print()
        
        elif args.top_callers is not None:
            limit = args.top_callers if args.top_callers else 20
            module_filter = args.top_callers_module or ""
            results = db.get_top_callers(limit=limit, module_filter=module_filter)
            
            if module_filter:
                print(f"被调用次数最多的函数排行（模块: {module_filter}，前 {len(results)} 名）:")
            else:
                print(f"被调用次数最多的函数排行（前 {len(results)} 名）:")
            print()
            
            # 计算排名宽度
            rank_width = len(str(len(results)))
            
            for i, item in enumerate(results, 1):
                rank = str(i).rjust(rank_width)
                callers = f"被 {item['caller_count']} 个函数调用"
                calls = f"（共 {item['call_count']} 次）"
                print(f"  #{rank}  {item['qualified_name']}")
                print(f"        {callers} {calls}")
            print()
        
        elif args.orphan_symbols:
            kind = args.orphan_symbols
            module_filter = args.orphan_module or ""
            limit = args.orphan_limit
            results = db.get_orphan_symbols(kind=kind, module_filter=module_filter, limit=limit)
            
            if module_filter:
                print(f"未被调用的孤立 {kind}（模块: {module_filter}，共找到 {len(results)} 个）:")
            else:
                print(f"未被调用的孤立 {kind}（共找到 {len(results)} 个）:")
            print()
            
            if results:
                # 按模块分组显示
                current_module = ""
                for item in results:
                    mod = item.get("module_path", "") or "(unknown)"
                    if mod != current_module:
                        current_module = mod
                        print(f"  [{current_module}]")
                    print(f"    {item['qualified_name']}")
                
                if len(results) >= limit:
                    print(f"\n  ... 可能还有更多，用 --orphan-limit 调整显示数量")
            else:
                print("  (无)")
            print()
        
        elif args.deepest is not None:
            limit = args.deepest if args.deepest else 20
            module_filter = args.deepest_module or ""
            results = db.get_deepest_functions(limit=limit, module_filter=module_filter)
            
            if module_filter:
                print(f"调用深度最深的函数排行（模块: {module_filter}，前 {len(results)} 名）:")
            else:
                print(f"调用深度最深的函数排行（前 {len(results)} 名）:")
            print()
            
            rank_width = len(str(len(results)))
            
            for i, item in enumerate(results, 1):
                rank = str(i).rjust(rank_width)
                mod = item.get("module_path", "") or "(unknown)"
                print(f"  #{rank}  [深度 {item['depth']:2d}]  {item['qualified_name']}")
            print()
        
        elif args.module_calls is not None:
            limit = args.module_calls if args.module_calls else 20
            results = db.get_module_call_stats(limit=limit)
            
            print(f"模块间调用统计（前 {len(results)} 对）:")
            print()
            
            # 计算列宽
            max_caller_len = max(len(r["caller_module"]) for r in results) if results else 0
            max_callee_len = max(len(r["callee_module"]) for r in results) if results else 0
            
            for i, item in enumerate(results, 1):
                caller = item["caller_module"].ljust(max_caller_len)
                callee = item["callee_module"].ljust(max_callee_len)
                print(f"  #{i:2d}  {caller}  →  {callee}  ({item['call_count']} 次调用, {item['unique_caller_count']} 个调用者, {item['unique_callee_count']} 个被调用函数)")
            print()
        
        elif args.detect_cycles:
            cycles = db.detect_cycles(max_depth=args.cycle_depth)
            
            print(f"循环调用检测结果:")
            print(f"  最大检测深度: {args.cycle_depth}")
            print(f"  找到循环数: {len(cycles)}")
            print()
            
            if cycles:
                # 按环的长度排序
                cycles_sorted = sorted(cycles, key=lambda c: len(c) - 1)
                
                for i, cycle in enumerate(cycles_sorted[:20], 1):
                    cycle_len = len(cycle) - 1  # 减去重复的结尾
                    print(f"  循环 #{i}（长度 {cycle_len}）:")
                    for j, fn in enumerate(cycle):
                        arrow = " → " if j < len(cycle) - 1 else ""
                        print(f"      {fn}{arrow}")
                    print()
                
                if len(cycles) > 20:
                    print(f"  ... 还有 {len(cycles) - 20} 个循环未显示")
                    print()
            else:
                print("  未检测到循环调用")
                print()
        
        elif args.export_module_graph:
            fmt = args.export_module_graph
            output_file = args.graph_output or ""
            
            if fmt not in ("mermaid", "dot"):
                print(f"错误: 不支持的格式 '{fmt}'，支持: mermaid, dot")
            else:
                result = db.export_module_graph(format=fmt, output_file=output_file)
                
                if output_file:
                    print(f"模块依赖图已导出到: {output_file}")
                    print(f"格式: {fmt}")
                else:
                    print(f"模块依赖图（{fmt} 格式）:")
                    print()
                    print(result)
                print()
        
        elif args.call_heatmap:
            group_by = args.call_heatmap
            top_n = args.heatmap_limit
            
            if group_by not in ("module", "file"):
                print(f"错误: 不支持的分组方式 '{group_by}'，支持: module, file")
            else:
                results = db.get_call_heatmap(group_by=group_by, top_n=top_n)
                
                unit = "模块" if group_by == "module" else "文件"
                print(f"函数调用频率热力图（按{unit}分组，前 {len(results)} 名）:")
                print()
                
                if results:
                    max_calls = max(r["total_calls"] for r in results)
                    max_group_len = max(len(r["group"]) for r in results)
                    
                    # 热力图标度：用不同字符表示密度
                    heat_chars = " ▁▂▃▄▅▆▇█"
                    
                    for i, item in enumerate(results, 1):
                        # 计算热力等级（0-8）
                        ratio = item["total_calls"] / max_calls if max_calls > 0 else 0
                        heat_level = min(int(ratio * 8), 8)
                        heat_bar = heat_chars[heat_level] * (heat_level + 1)
                        
                        group_name = item["group"].ljust(max_group_len)
                        print(f"  #{i:2d}  {group_name}  {heat_bar}  {item['total_calls']:4d} 次调用  ({item['unique_callers']} 个调用者, {item['unique_callees']} 个被调用函数)")
                else:
                    print("  (无数据)")
                print()
        
        elif args.test_coverage:
            stats = db.get_test_coverage()
            
            print("测试覆盖率统计:")
            print()
            print(f"  总函数数: {stats['total_functions']}")
            print(f"  测试函数数: {stats['test_functions']}")
            print(f"  测试函数占比: {stats['test_ratio']}%")
            print()
            print(f"  总模块数: {stats['total_modules']}")
            print(f"  有测试的模块数: {stats['modules_with_tests']}")
            print(f"  模块覆盖率: {stats['module_coverage']}%")
            print()
            
            if stats["test_by_module"]:
                print(f"测试函数分布（前 20 个模块）:")
                print()
                
                max_test_count = max(m["test_count"] for m in stats["test_by_module"])
                max_mod_len = max(len(m["module"]) for m in stats["test_by_module"][:20])
                
                for i, mod in enumerate(stats["test_by_module"][:20], 1):
                    bar_len = int(mod["test_count"] / max_test_count * 30) if max_test_count > 0 else 0
                    bar = "█" * bar_len
                    mod_name = mod["module"].ljust(max_mod_len)
                    print(f"  #{i:2d}  {mod_name}  {bar}  {mod['test_count']:3d} 个测试")
                
                if len(stats["test_by_module"]) > 20:
                    print(f"\n  ... 还有 {len(stats['test_by_module']) - 20} 个模块")
            print()
        
        elif args.function_issues is not None:
            fn_name = args.function_issues
            module_filter = args.issue_module or ""
            issue_filter = args.issue_type or ""
            limit = args.issue_limit
            
            results = db.get_function_issues(
                qualified_name=fn_name,
                module_filter=module_filter,
                issue_filter=issue_filter,
                limit=limit,
            )
            
            # 严重程度标记
            severity_icon = {"danger": "[!]", "warn": "[~]", "info": "[i]"}
            
            if fn_name:
                # 单函数详情模式
                if results:
                    r = results[0]
                    print(f"函数缺陷检测: {r['qualified_name']}")
                    print(f"  模块: {r['module_path'] or '(unknown)'}")
                    print(f"  缺陷数: {r['issue_count']}")
                    print()
                    for issue in r["issues"]:
                        icon = severity_icon.get(issue["severity"], "[?]")
                        print(f"  {icon} {issue['label']}  (x{issue['count']})")
                        print(f"      {issue['description']}")
                    print()
                else:
                    print(f"函数缺陷检测: {fn_name}")
                    print("  未检测到缺陷" + (f"（类型过滤: {issue_filter}）" if issue_filter else ""))
                    print()
            else:
                # 列表模式
                if issue_filter:
                    print(f"函数缺陷检测（类型: {issue_filter}，共 {len(results)} 个函数）:")
                elif module_filter:
                    print(f"函数缺陷检测（模块: {module_filter}，共 {len(results)} 个函数）:")
                else:
                    print(f"函数缺陷检测（共 {len(results)} 个函数）:")
                print()
                
                for i, r in enumerate(results, 1):
                    issue_labels = []
                    for issue in r["issues"]:
                        icon = severity_icon.get(issue["severity"], "")
                        issue_labels.append(f"{icon}{issue['label']}" + (f"(x{issue['count']})" if issue["count"] > 1 else ""))
                    
                    issue_str = "  ".join(issue_labels)
                    print(f"  #{i:2d}  {r['qualified_name']}")
                    print(f"        {issue_str}")
                print()
        
        elif args.issue_summary:
            module_filter = args.issue_module or ""
            stats = db.get_issue_summary(module_filter=module_filter)
            
            if module_filter:
                print(f"缺陷类型汇总统计（模块: {module_filter}）:")
            else:
                print(f"缺陷类型汇总统计:")
            print()
            print(f"  总函数数: {stats['total_functions']}（不含 test 函数）")
            print(f"  有缺陷函数: {stats['functions_with_issues']}")
            print(f"  无缺陷函数: {stats['issue_free_functions']}  ({stats['issue_free_ratio']}%)")
            print()
            
            severity_icon = {"danger": "[!]", "warn": "[~]", "info": "[i]"}
            
            print(f"  缺陷类型分布:")
            print()
            
            # 按严重程度分组
            for severity in ["danger", "warn", "info"]:
                severity_issues = [i for i in stats["issues"] if i["severity"] == severity and i["function_count"] > 0]
                if severity_issues:
                    severity_label = {"danger": "危险", "warn": "警告", "info": "提示"}[severity]
                    print(f"  [{severity_label}]")
                    for issue in severity_issues:
                        icon = severity_icon.get(issue["severity"], "")
                        bar_len = int(issue["function_count"] / stats["total_functions"] * 40) if stats["total_functions"] > 0 else 0
                        bar = "█" * bar_len
                        print(f"    {icon} {issue['label']:<14s}  {bar} {issue['function_count']:4d} 个函数 ({issue['ratio']}%)  共 {issue['total_occurrences']} 次")
                    print()
            
            # 显示零缺陷的函数
            zero_issues = [i for i in stats["issues"] if i["function_count"] == 0]
            if zero_issues:
                print(f"  [未检出]")
                for issue in zero_issues:
                    print(f"    [✓] {issue['label']:<14s}  0 个函数")
                print()
        
        elif args.semgrep is not None:
            # Semgrep 多语言静态分析
            target_paths = args.semgrep if args.semgrep else None  # None 表示扫描整个 workspace
            config = args.semgrep_config
            languages = args.semgrep_scan_lang
            timeout = args.semgrep_timeout
            
            print(f"Semgrep 多语言静态分析:")
            print(f"  规则配置: {config}")
            if languages:
                print(f"  语言限制: {', '.join(languages)}")
            print(f"  超时设置: {timeout} 秒")
            print()
            
            if args.semgrep_save:
                # 扫描并存入数据库
                result = db.run_semgrep_and_save(
                    target_paths=target_paths or [db.workspace_root],
                    config=config,
                    languages=languages,
                    timeout=timeout,
                )
                if not result.get("success"):
                    print(f"  [错误] {result.get('error', '未知错误')}")
                else:
                    print(f"  扫描完成，共发现 {result['total_findings']} 个问题")
                    print(f"  已存入数据库: {result['saved_findings']} 条")
                    print()
                    print("提示: 使用 --semgrep-list 查看已保存的缺陷")
            
            elif args.semgrep_quick:
                # 快速扫描（只显示汇总）
                result = db.get_semgrep_summary(target_paths)
                
                if not result.get("success"):
                    print(f"  [错误] {result.get('error', '未知错误')}")
                else:
                    print(f"  总发现数: {result['total_findings']}")
                    print()
                    
                    # 按严重程度展示
                    if result.get("by_severity"):
                        print(f"  严重程度分布:")
                        for sev in ["ERROR", "WARNING", "INFO"]:
                            count = result["by_severity"].get(sev, 0)
                            if count > 0:
                                icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}[sev]
                                print(f"    {icon} {sev}: {count} 个")
                        print()
                    
                    # 按语言展示
                    if result.get("by_language"):
                        print(f"  语言分布:")
                        for lang, count in sorted(result["by_language"].items(), key=lambda x: x[1], reverse=True):
                            print(f"    {lang}: {count} 个")
                        print()
                    
                    # Top 规则
                    if result.get("top_rules"):
                        print(f"  最常见规则（前 10）:")
                        for rule_id, stats in result["top_rules"][:10]:
                            sev_icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}[stats.get("severity", "INFO")]
                            print(f"    {sev_icon} {rule_id}: {stats['count']} 次")
                            print(f"        {stats['message'][:80]}...")
                        print()
                
                if result.get("errors"):
                    print(f"  [警告] 扫描过程中有 {len(result['errors'])} 个错误")
            
            else:
                # 详细扫描
                result = db.run_semgrep(
                    target_paths=target_paths or [db.workspace_root],
                    config=config,
                    languages=languages,
                    timeout=timeout,
                )
                
                if not result.get("success"):
                    print(f"  [错误] {result.get('error', '未知错误')}")
                else:
                    print(f"  扫描完成，共发现 {result['total_findings']} 个问题")
                    print()
                    
                    # 按严重程度分组展示
                    severity_icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}
                    
                    for sev in ["ERROR", "WARNING", "INFO"]:
                        sev_findings = [f for f in result["results"] if f["severity"] == sev]
                        if sev_findings:
                            icon = severity_icon[sev]
                            sev_label = {"ERROR": "错误", "WARNING": "警告", "INFO": "提示"}[sev]
                            print(f"  [{sev_label}] ({len(sev_findings)} 个):")
                            print()
                            
                            for f in sev_findings[:15]:
                                print(f"    {icon} {f['rule_name']}")
                                print(f"        文件: {f['path']}:{f['start_line']}")
                                print(f"        语言: {f['language']}")
                                print(f"        {f['message'][:100]}")
                                if f.get("fix"):
                                    print(f"        修复建议: {f['fix'][:50]}")
                                print()
                            
                            if len(sev_findings) > 15:
                                print(f"    ... 还有 {len(sev_findings) - 15} 个同级别问题")
                                print()
            
            print("提示: 使用 --semgrep-quick 可快速获取汇总统计")
            print()
        
        elif args.semgrep_stats:
            stats = db.get_semgrep_stats()
            print("Semgrep 缺陷统计:")
            print(f"  总发现数: {stats['total_findings']}")
            print()
            
            if stats["by_severity"]:
                print("  按严重程度:")
                for sev in ["ERROR", "WARNING", "INFO"]:
                    count = stats["by_severity"].get(sev, 0)
                    if count > 0:
                        icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}[sev]
                        print(f"    {icon} {sev}: {count} 个")
                print()
            
            if stats["by_language"]:
                print("  按语言分布:")
                for lang, count in sorted(stats["by_language"].items(), key=lambda x: x[1], reverse=True):
                    print(f"    {lang:<15s} {count:4d} 个")
                print()
            
            if stats["by_rule"]:
                print("  Top 规则（前 10）:")
                for i, rule in enumerate(stats["by_rule"][:10], 1):
                    sev_icon = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}.get(rule["severity"], "[?]")
                    print(f"    #{i:2d} {sev_icon} {rule['rule_id'][:50]:<50s}  {rule['cnt']:3d} 次")
                print()
            
            if stats["by_symbol"]:
                print("  问题最多的符号（前 10）:")
                for i, sym in enumerate(stats["by_symbol"][:10], 1):
                    print(f"    #{i:2d} {sym['symbol_qualified'][:60]:<60s}  {sym['cnt']:2d} 个")
                print()
        
        elif args.semgrep_list is not None:
            rule_filter = args.semgrep_list if args.semgrep_list else ""
            severity = args.semgrep_severity or ""
            language = args.semgrep_list_lang or ""
            limit = args.semgrep_limit
            
            findings = db.get_semgrep_findings(
                severity=severity,
                language=language,
                rule_id=rule_filter,
                limit=limit,
            )
            
            filter_parts = []
            if severity:
                filter_parts.append(f"严重程度: {severity}")
            if language:
                filter_parts.append(f"语言: {language}")
            if rule_filter:
                filter_parts.append(f"规则: {rule_filter}")
            filter_str = " | ".join(filter_parts) if filter_parts else "全部"
            
            print(f"Semgrep 缺陷列表（{filter_str}，共 {len(findings)} 个，显示前 {min(limit, len(findings))} 个）:")
            print()
            
            sev_icon_map = {"ERROR": "[!]", "WARNING": "[~]", "INFO": "[i]"}
            
            for i, f in enumerate(findings[:limit], 1):
                icon = sev_icon_map.get(f["severity"], "[?]")
                sym_info = f" -> {f['symbol_qualified']}" if f["symbol_qualified"] else ""
                print(f"  #{i:3d} {icon} {f['rule_name'][:40]:<40s} {f['language']:<12s}{sym_info}")
                print(f"        {f['file_path']}:{f['start_line']}")
                print(f"        {f['message'][:80]}")
                print()
            
            if len(findings) > limit:
                print(f"  ... 还有 {len(findings) - limit} 个，用 --semgrep-limit N 调整显示数量")
        
        elif args.git_import is not None:
            max_commits = args.git_import if args.git_import else 100
            print(f"导入 Git 历史记录（最多 {max_commits} 个 commit）...")
            result = db.import_git_history(max_commits=max_commits)
            if result.get("success"):
                print(f"  成功导入 {result['commits_imported']} 个 commit")
                print(f"  总计 {result['total_commits']} 个 commit")
            else:
                print(f"  导入失败: {result.get('error', '未知错误')}")
        
        elif args.git_log is not None:
            limit = args.git_log if args.git_log else 20
            commits = db.get_git_commits(limit=limit)
            print(f"Git commit 历史（共 {len(commits)} 条）:")
            print()
            for c in commits:
                short_hash = c['commit_hash'][:8]
                timestamp = time.strftime('%Y-%m-%d %H:%M', time.localtime(c['timestamp']))
                msg = c['message'][:60] if c['message'] else '(无提交信息)'
                author = c['author'][:15] if c['author'] else 'unknown'
                print(f"  {short_hash}  {timestamp}  {author:<15s}  {msg}")
        
        elif args.git_show:
            details = db.get_commit_changes(args.git_show)
            commit = details.get("commit")
            if not commit:
                print(f"未找到 commit: {args.git_show}")
            else:
                print(f"Commit: {commit['commit_hash']}")
                print(f"作者: {commit['author']} <{commit['email']}>")
                print(f"时间: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(commit['timestamp']))}")
                print(f"信息: {commit['message']}")
                print()
                file_changes = details.get("file_changes", [])
                print(f"变更文件（共 {len(file_changes)} 个）:")
                type_map = {'A': '新增', 'M': '修改', 'D': '删除', 'R': '重命名'}
                for fc in file_changes:
                    ct = fc.get('change_type', '?')
                    type_label = type_map.get(ct, ct)
                    path = fc.get('rel_path') or fc.get('abs_path') or 'unknown'
                    print(f"  [{type_label}] {path}")
        
        elif args.git_stats:
            stats = db.get_git_stats()
            print("Git 集成统计:")
            print(f"  Commit 总数: {stats['commit_count']}")
            print(f"  文件变更总数: {stats['file_change_count']}")
            print()
            if stats.get("change_types"):
                print("  按变更类型:")
                type_map = {'A': '新增', 'M': '修改', 'D': '删除', 'R': '重命名'}
                for ct, cnt in sorted(stats["change_types"].items(), key=lambda x: x[1], reverse=True):
                    label = type_map.get(ct, ct)
                    print(f"    {label}: {cnt} 次")
        
        # ----------------------------------------------------------------
        # 代码度量
        # ----------------------------------------------------------------
        
        elif args.metrics:
            summary = db.get_code_metrics_summary()
            print("代码度量汇总:")
            print(f"  文件数: {summary['file_count']}")
            print(f"  函数数: {summary['function_count']}")
            print(f"  总代码行: {summary['total_lines']}")
            print(f"  调用关系: {summary['total_calls']}")
            print()
            print(f"  平均圈复杂度: {summary['avg_complexity']}")
            print(f"  最高圈复杂度: {summary['max_complexity']}")
            print()
            print("  复杂度分布:")
            dist = summary["complexity_distribution"]
            total_fn = sum(dist.values()) or 1
            for level, count in dist.items():
                pct = count / total_fn * 100
                bar = "#" * int(pct / 2)
                print(f"    {level:<12s} {count:4d} ({pct:5.1f}%) {bar}")
            print()
            print(f"  注释覆盖率: {summary['comment_coverage']}%")
        
        elif args.complexity is not None:
            limit = args.complexity if args.complexity else 20
            mod_filter = args.complexity_module or ""
            hotspots = db.get_complexity_hotspots(limit=limit, module_filter=mod_filter)
            
            filter_info = f"（模块: {mod_filter}）" if mod_filter else ""
            print(f"圈复杂度热点 {filter_info}（共 {len(hotspots)} 个）:")
            print()
            print(f"  {'#':>3}  {'复杂度':>6}  {'行数':>5}  {'深度':>4}  函数名")
            print(f"  {'-'*3}  {'-'*6}  {'-'*5}  {'-'*4}  {'-'*50}")
            
            for i, fn in enumerate(hotspots, 1):
                risk = "!" if fn["cyclomatic_complexity"] > 10 else " "
                print(f"  {i:3d}{risk}  {fn['cyclomatic_complexity']:>6}  {fn['line_count']:>5}  {fn['depth']:>4}  {fn['qualified_name'][:60]}")
                print(f"        {fn['file_path']}:{fn['start_line']}")
            print()
            print("  提示: 复杂度 >10 的函数建议重构（标记 !）")
        
        elif args.coupling:
            modules = db.get_coupling_analysis(limit=30)
            print(f"模块耦合度分析（共 {len(modules)} 个模块）:")
            print()
            print(f"  {'#':>3}  {'模块':<40s}  {'传入':>4}  {'传出':>4}  {'总计':>4}  {'不稳定性':>6}")
            print(f"  {'-'*3}  {'-'*40}  {'-'*4}  {'-'*4}  {'-'*4}  {'-'*6}")
            
            for i, mod in enumerate(modules, 1):
                inst = mod["instability"]
                inst_label = f"{inst:.2f}"
                if inst > 0.7:
                    inst_label += " (不稳定)"
                elif inst < 0.3:
                    inst_label += " (稳定)"
                print(f"  {i:3d}  {mod['module'][:40]:<40s}  {mod['afferent']:>4}  {mod['efferent']:>4}  {mod['total_coupling']:>4}  {inst_label:>6}")
        
        elif args.largest_fns is not None:
            limit = args.largest_fns if args.largest_fns else 20
            fns = db.get_largest_functions(limit=limit)
            print(f"代码行数最多的函数（共 {len(fns)} 个）:")
            print()
            print(f"  {'#':>3}  {'行数':>5}  {'深度':>4}  函数名")
            print(f"  {'-'*3}  {'-'*5}  {'-'*4}  {'-'*50}")
            
            for i, fn in enumerate(fns, 1):
                print(f"  {i:3d}  {fn['line_count']:>5}  {fn['depth']:>4}  {fn['qualified_name'][:60]}")
                print(f"        {fn['file_path']}:{fn['start_line']}")
        
        elif args.coupled_fns is not None:
            limit = args.coupled_fns if args.coupled_fns else 20
            fns = db.get_most_coupled_functions(limit=limit)
            print(f"耦合度最高的函数（共 {len(fns)} 个）:")
            print()
            print(f"  {'#':>3}  {'扇入':>4}  {'扇出':>4}  {'总计':>4}  函数名")
            print(f"  {'-'*3}  {'-'*4}  {'-'*4}  {'-'*4}  {'-'*50}")
            
            for i, fn in enumerate(fns, 1):
                print(f"  {i:3d}  {fn['fan_in']:>4}  {fn['fan_out']:>4}  {fn['total_coupling']:>4}  {fn['qualified_name'][:60]}")
                print(f"        {fn['file_path']}")
        
        elif args.fn_metrics:
            metrics = db.get_function_metrics(args.fn_metrics)
            if not metrics:
                print(f"未找到函数: {args.fn_metrics}")
                print("提示: 用 --search 搜索函数名称")
            else:
                print(f"函数度量: {metrics['qualified_name']}")
                print(f"  类型: {metrics['kind']}")
                print(f"  文件: {metrics['file_path']}:{metrics['start_line']}-{metrics['end_line']}")
                print(f"  行数: {metrics['line_count']}")
                print(f"  圈复杂度: {metrics['cyclomatic_complexity']} ({metrics['risk_level']})")
                print(f"  扇入: {metrics['fan_in']}（被调用次数）")
                print(f"  扇出: {metrics['fan_out']}（调用其他函数数）")
                print(f"  调用深度: {metrics['depth']}")
                print(f"  模块: {metrics['module_path']}")
                if metrics['signature']:
                    print(f"  签名: {metrics['signature'][:100]}")

        # ----------------------------------------------------------------
        # 语义搜索 / 向量嵌入
        # ----------------------------------------------------------------

        elif args.semantic_search:
            query = args.semantic_search
            print(f"语义搜索: '{query}'")
            print("-" * 50)
            results = db.semantic_search(query, top_k=10)
            if not results:
                print("  未找到匹配的函数")
                print("提示: 嵌入模型可能未启用，请先运行 --embed 生成向量嵌入")
            else:
                for i, r in enumerate(results, 1):
                    print(f"  [{i}] 相似度={r['similarity']:.4f}  {r['qualified_name']}")
                    print(f"      {r['file_path']}:{r['start_line']}")
                    if r.get('summary'):
                        print(f"      摘要: {r['summary'][:80]}")
            print()

        elif args.embed or args.embed_force:
            force = args.embed_force
            mode = "强制重新嵌入" if force else "增量嵌入"
            print(f"为所有函数生成向量嵌入（{mode}）...")
            print("-" * 50)
            stats = db.embed_all_symbols(force=force)
            print(f"  总符号数: {stats['total']}")
            print(f"  成功: {stats['success']}")
            print(f"  跳过: {stats['skipped']}")
            print(f"  失败: {stats['failed']}")
            if stats['success'] == 0 and stats['total'] > 0:
                print()
                print("提示: 嵌入模型不可用，请安装 sentence-transformers 或启动 ollama 服务")
            print()

        elif args.similar:
            name = args.similar
            print(f"查找与 '{name}' 相似的函数（阈值 0.7）:")
            print("-" * 50)
            results = db.find_similar_functions(name, threshold=0.7)
            if not results:
                print("  未找到相似函数")
                print("提示: 目标函数可能不存在，或未生成嵌入（请先运行 --embed）")
            else:
                for i, r in enumerate(results, 1):
                    print(f"  [{i}] 相似度={r['similarity']:.4f}  {r['qualified_name']}")
                    print(f"      {r['file_path']}:{r['start_line']}")
                    if r.get('summary'):
                        print(f"      摘要: {r['summary'][:80]}")
            print()

        # ----------------------------------------------------------------
        # 任务管理
        # ----------------------------------------------------------------

        elif args.task_list:
            tasks = db.task_list()
            print(f"任务列表（共 {len(tasks)} 个）:")
            print("-" * 50)
            for t in tasks:
                created = time.strftime('%Y-%m-%d %H:%M', time.localtime(t['created_at'])) if t.get('created_at') else '?'
                print(f"  [{t['task_id']}] {t['title']}")
                print(f"      状态: {t['status']} | 步骤数: {t['step_count']} | 创建: {created}")
            print()

        elif args.task_show:
            detail = db.task_status(args.task_show)
            if not detail:
                print(f"未找到任务: {args.task_show}")
            else:
                print(f"任务详情")
                print("-" * 50)
                print(f"  ID: {detail['task_id']}")
                print(f"  标题: {detail['title']}")
                print(f"  状态: {detail['status']}")
                if detail.get('description'):
                    print(f"  描述: {detail['description']}")
                if detail.get('creator'):
                    print(f"  创建者: {detail['creator']}")
                created = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(detail['created_at'])) if detail.get('created_at') else '?'
                print(f"  创建时间: {created}")
                print()
                steps = detail.get('steps', [])
                print(f"  步骤（{len(steps)} 个）:")
                for s in steps:
                    print(f"    #{s['step_index']} [{s['status']}] {s['action']}")
                    if s.get('target_file'):
                        print(f"        文件: {s['target_file']}")
                    if s.get('target_symbol'):
                        print(f"        符号: {s['target_symbol']}")
            print()

        # ----------------------------------------------------------------
        # 项目简报和仓库地图
        # ----------------------------------------------------------------

        elif args.brief:
            brief = db.project_brief()
            print("=== 项目简报 ===")
            print()
            print(f"  项目类型: {brief['project_type']}")
            print(f"  文件数: {brief['file_count']}")
            print(f"  函数数: {brief['function_count']}")
            print(f"  总代码行: {brief['total_lines']}")
            print(f"  健康评分: {brief['health_score']} ({brief['health_level']})")
            print(f"  平均圈复杂度: {brief['avg_complexity']}")
            print(f"  注释覆盖率: {brief['comment_coverage']}%")
            print()
            modules = brief.get('modules', [])
            if modules:
                print(f"  模块（前 {len(modules)} 个，按函数数降序）:")
                for i, m in enumerate(modules, 1):
                    print(f"    [{i}] {m['module']}  ({m['function_count']} 个函数)")
                print()
            hotspots = brief.get('hot_functions', [])
            if hotspots:
                print(f"  复杂度热点（前 {len(hotspots)} 个）:")
                for i, fn in enumerate(hotspots, 1):
                    print(f"    [{i}] 复杂度={fn['cyclomatic_complexity']}  {fn['qualified_name']}")
            print()

        elif args.map:
            output = db.repo_map(format=args.map_format)
            print(f"=== 仓库模块依赖图（{args.map_format} 格式）===")
            print()
            print(output)
            print()

        # ----------------------------------------------------------------
        # 覆盖率导入与查询
        # ----------------------------------------------------------------

        elif args.coverage_import:
            file_path = args.coverage_import
            fmt = args.coverage_format
            print(f"导入覆盖率报告: {file_path}（格式: {fmt}）")
            print("-" * 50)
            try:
                if fmt == "lcov":
                    stats = db.import_lcov(file_path)
                else:
                    stats = db.import_cobertura(file_path)
                print(f"  报告文件总数: {stats['files_total']}")
                print(f"  匹配文件数: {stats['files_matched']}")
                print(f"  导入行数: {stats['lines_imported']}")
                print(f"  关联符号数: {stats['symbols_matched']}")
            except FileNotFoundError:
                print(f"  [错误] 文件不存在: {file_path}")
            except Exception as e:
                print(f"  [错误] 解析失败: {e}")
            print()

        elif args.coverage_fn:
            name = args.coverage_fn
            info = db.get_coverage_for_symbol(name)
            if not info:
                print(f"未找到函数: {name}")
                print("提示: 用 --search 搜索函数名称")
            else:
                print(f"函数覆盖率: {info['qualified_name']}")
                print("-" * 50)
                print(f"  文件: {info['file_path']}:{info['start_line']}-{info['end_line']}")
                print(f"  总行数: {info['total_lines']}")
                print(f"  有覆盖率数据行: {info['tracked_lines']}")
                print(f"  已覆盖行: {info['covered_lines']}")
                print(f"  覆盖率: {info['coverage_pct']}%")
                if info['uncovered_lines']:
                    lines_preview = info['uncovered_lines'][:30]
                    more = '...' if len(info['uncovered_lines']) > 30 else ''
                    print(f"  未覆盖行: {lines_preview}{more}")
            print()

        elif args.coverage_uncovered:
            results = db.find_uncovered_functions()
            print(f"未充分覆盖的函数（覆盖率 < 50%，共 {len(results)} 个）:")
            print("-" * 50)
            for i, r in enumerate(results, 1):
                print(f"  [{i:3d}] 覆盖率={r['coverage_pct']:5.1f}%  {r['qualified_name']}")
                print(f"        {r['file_path']}:{r['start_line']}-{r['end_line']}  (覆盖 {r['covered_lines']}/{r['tracked_lines']} 行)")
            print()

        # ----------------------------------------------------------------
        # 所有权查询
        # ----------------------------------------------------------------

        elif args.who:
            info = db.who_to_ask(args.who)
            if not info:
                print(f"未找到文件负责人: {args.who}")
                print("提示: 请先运行 --init 构建图谱，文件路径可以是相对或绝对路径")
            else:
                print(f"文件负责人查询")
                print("-" * 50)
                print(f"  文件: {info['file_path']}")
                print(f"  负责人: {info['owner']}")
                print(f"  来源: {info['source']}")
                print(f"  置信度: {info['confidence']}")
                if info.get('last_commit_author'):
                    print(f"  最近提交者: {info['last_commit_author']}")
                if info.get('last_commit_time'):
                    ts = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(info['last_commit_time']))
                    print(f"  最近提交时间: {ts}")
                if info.get('last_commit_hash'):
                    print(f"  最近提交 hash: {info['last_commit_hash'][:12]}...")
            print()

        elif args.ownership_map:
            results = db.get_ownership_map()
            print(f"所有权映射（共 {len(results)} 个模块）:")
            print("-" * 50)
            for i, m in enumerate(results, 1):
                print(f"  [{i}] {m['module']}")
                print(f"      主负责人: {m['primary_owner']}  (共 {m['file_count']} 个文件)")
                owners_str = ", ".join(f"{o['name']}({o['file_count']})" for o in m['owners'][:5])
                print(f"      负责人分布: {owners_str}")
                if len(m['owners']) > 5:
                    print(f"      ... 还有 {len(m['owners']) - 5} 个负责人")
            print()

        else:
            parser.print_help()
    
    finally:
        db.close()


if __name__ == "__main__":
    main()
