"""check_baseline.py
==================

从源码生成 Call Warden 基线数字，并扫描文档中引用的不一致。

复审报告 §8.5 要求："从源码生成 MCP/Mixin/CLI 基线，文档只引用生成结果，
不再人工维护三套数字。"本脚本即为该要求的落地实现。

用法：
    python scripts/check_baseline.py            # 仅打印基线
    python scripts/check_baseline.py --check    # 扫描所有 .md 文档，报告不一致
    python scripts/check_baseline.py --json     # JSON 格式输出（供 CI 集成）

退出码：
    0 = 全部一致
    1 = 发现不一致（仅 --check 模式）
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Dict, List, Tuple

# 项目根目录（脚本位于 scripts/ 下）
PROJECT_ROOT = Path(__file__).resolve().parent.parent


def count_mcp_tools() -> int:
    """统计 server/mcp_server.py 中 @mcp.tool() 装饰器数量"""
    mcp_server = PROJECT_ROOT / "server" / "mcp_server.py"
    if not mcp_server.exists():
        return 0
    text = mcp_server.read_text(encoding="utf-8")
    # 匹配 @mcp.tool() 装饰器（容忍空格变化）
    return len(re.findall(r"@mcp\.tool\(\)", text))


def count_db_files() -> int:
    """统计 db/db_*.py 文件数量"""
    db_dir = PROJECT_ROOT / "db"
    if not db_dir.exists():
        return 0
    return len(list(db_dir.glob("db_*.py")))


def count_mixins() -> Tuple[int, int]:
    """统计 CodeGraphDB 继承的 Mixin 数

    Returns:
        (功能 Mixin 数, 含基类的总数)
        - 功能 Mixin 数：CodeGraphDB( 中除 CodeGraphBase 外的基类数
        - 含基类总数：上述 + 1（CodeGraphBase）
    """
    db_py = PROJECT_ROOT / "db" / "db.py"
    if not db_py.exists():
        return (0, 0)
    text = db_py.read_text(encoding="utf-8")
    # 匹配 class CodeGraphDB( 后的继承列表，直到 ):
    m = re.search(r"class\s+CodeGraphDB\(([^)]+)\)", text, re.DOTALL)
    if not m:
        return (0, 0)
    bases = [b.strip() for b in m.group(1).split(",") if b.strip()]
    # 排除 CodeGraphBase
    functional = [b for b in bases if b != "CodeGraphBase"]
    return (len(functional), len(bases))


def count_languages() -> int:
    """统计 config.py 中 LANGUAGE_CONFIG 字典支持的语言数"""
    config_py = PROJECT_ROOT / "config.py"
    if not config_py.exists():
        return 0
    text = config_py.read_text(encoding="utf-8")
    # 匹配 LANGUAGE_CONFIG: Dict[str, Dict] = { 开头到对应 }
    m = re.search(r"LANGUAGE_CONFIG\s*:\s*Dict\[str,\s*Dict\]\s*=\s*\{", text)
    if not m:
        return 0
    # 计数顶层 key（"lang_name": {...},）
    # 简单解析：从 LANGUAGE_CONFIG 开始扫描，到对应的闭合 }
    start = m.end() - 1  # 指向 {
    depth = 0
    end = start
    for i in range(start, len(text)):
        c = text[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i
                break
    body = text[start:end + 1]
    # 顶层 key 模式："lang_name": {  （只匹配语言名，跳过嵌套字典的 key）
    # 简单方案：匹配行首空白 + "xxx": { 模式
    keys = re.findall(r'^\s*"([a-z_]+)"\s*:\s*\{', body, re.MULTILINE)
    # 去重（避免嵌套字典 key 被误算）
    return len(set(keys))


def read_schema_version() -> int:
    """读取 db/schema.py 中的 SCHEMA_VERSION"""
    schema_py = PROJECT_ROOT / "db" / "schema.py"
    if not schema_py.exists():
        return 0
    text = schema_py.read_text(encoding="utf-8")
    m = re.search(r"SCHEMA_VERSION\s*=\s*(\d+)", text)
    return int(m.group(1)) if m else 0


def read_product_version() -> str:
    """读取 release/version.toml 中的产品版本"""
    version_toml = PROJECT_ROOT / "release" / "version.toml"
    if not version_toml.exists():
        return ""
    text = version_toml.read_text(encoding="utf-8")
    # 简单解析 TOML 的 [product] version = "x.y.z"
    m = re.search(r'\[product\][^[]*?version\s*=\s*"([^"]+)"', text, re.DOTALL)
    return m.group(1) if m else ""


def generate_baseline() -> Dict:
    """生成完整基线字典"""
    mixin_functional, mixin_total = count_mixins()
    return {
        "mcp_tools": count_mcp_tools(),
        "db_files": count_db_files(),
        "mixin_functional": mixin_functional,
        "mixin_total": mixin_total,
        "languages": count_languages(),
        "schema_version": read_schema_version(),
        "product_version": read_product_version(),
    }


# ============================================
# 文档一致性扫描
# ============================================

# 文档中允许出现的数字模式（与基线 key 对应）
# 例如：mcp_tools=206 时，文档中应出现 "206 个 MCP" / "206+ MCP" / "206 个 @mcp.tool"
# 反例：mcp_tools=206 时不应出现 "205 个 MCP" / "205+ MCP" / "204 个 MCP"
_BASELINE_PATTERNS = {
    "mcp_tools": {
        # scan_pattern 捕获 "N MCP" / "N 个 MCP" / "N tools" / "N 个工具" / "N 个 `@mcp.tool"
        # 以及反向 "工具数：N" / "工具数: N"
        "scan_pattern": r"(?<!\w)(\d{2,3})\s*\+?\s*(?:MCP|个\s*MCP|tools|个\s*@mcp\.tool|个\s*`@mcp\.tool|个\s*工具)",
        "reverse_pattern": r"工具数\s*[:：]\s*(\d{2,3})",
        "expected_value_name": "MCP 工具数",
    },
    "mixin_functional": {
        "scan_pattern": r"(?<!\w)(\d{1,2})\s*个\s*(?:功能\s*)?Mixin",
        "expected_value_name": "功能 Mixin 数",
    },
    "db_files": {
        # 捕获 "N 个 db_*.py"
        "scan_pattern": r"(?<!\w)(\d{1,2})\s*个\s*db_\*\.py",
        "expected_value_name": "db_*.py 文件数",
    },
    "schema_version": {
        # 捕获 "Schema vN" / "vN Schema"，排除 "Schema vN-vM"(范围) / "Schema vN+"(最低版本) / "Schema vN 新增"(历史引用)
        # (?![\d\-+]|[\s*]*新增) 防止 \d+ 回溯导致 "v30+" 被匹配为 "v3"，
        # 同时跳过 "**Schema v29** 新增" 等 markdown 加粗格式后的历史引用
        "scan_pattern": r"Schema\s*v(\d+)(?![\d\-+]|[\s*]*新增)",
        "reverse_pattern": r"v(\d+)\s*Schema",
        "expected_value_name": "Schema 版本",
        "is_version": True,  # 标记：比较时不需要加 v 前缀
    },
}


def scan_document_consistency(baseline: Dict, doc_paths: List[Path]) -> List[Dict]:
    """扫描所有 .md 文档，找出与基线数字不一致的引用

    Args:
        baseline: generate_baseline() 的返回值
        doc_paths: 待扫描的文档列表

    Returns:
        不一致项列表，每项含 {file, line, expected, found, source}
    """
    inconsistencies: List[Dict] = []

    # 跳过历史文档目录（这些是版本演化记录，旧数字是有意保留的）
    SKIP_DIRS = {
        "docs/history",            # 历史版本快照
        "docs/design/evolve-guardian-architecture",  # Guardian spec 是设计阶段文档
    }
    # 跳过整个文件（这些文件是历史审计/废弃文档，旧数字是有意保留的）
    SKIP_FILES = {
        ".mcp_audit.md",                                    # 173 工具时点的 MCP 审计
        ".cli_audit.md",                                    # 173 工具时点的 CLI 审计
        "CHANGELOG.md",                                      # 版本演化记录
        "callwarden 功能差距分析报告.md",                    # 历史差距分析（含其他项目数据）
        "docs/naming-analysis-report.md",                   # 已标"过时提示"的命名分析
        "docs/design/rust_daemon_architecture.md",          # 已废弃文档
        "docs/design/enterprise-daemon-shared-snapshot-plan.md",  # 历史设计文档
        "docs/design/enterprise-phase1-phase3-detail.md",   # 历史设计文档
        "docs/design/feature-matrix-code-audit-2026-07-20.md",    # 历史审计报告
        "docs/design/feature-matrix-code-reaudit-2026-07-21.md",   # 历史复审报告
        "callwarden 与 200 个仓库的交叉对比分析.md",              # 历史模块分析
    }
    # 跳过审计/复审报告中的描述性文本（这些是历史记录，记录旧错误）
    SKIP_MARKERS = (
        "复审回退", "与源码", "需统一至", "不符",
        "声称", "声称 205", "声称 33", "声称 40",
        # 版本演化语境
        "v3 (", "v9-", "v11-v13", "→", "新增约", "→ 9 语言", "→ 16 语言",
        # 历史快照标识
        "历史文档", "已过时", "不代表当前",
        # 历史数据引用
        "旧现状", "过时提示", "历史数据",
        # 序号语境（"第 N 个 Mixin" 是序号，不是总数）
        "第 ", "Guardian 表 + ",
    )

    for doc_path in doc_paths:
        # 跳过历史目录
        rel_path = doc_path.relative_to(PROJECT_ROOT).as_posix()
        if any(rel_path.startswith(skip) for skip in SKIP_DIRS):
            continue
        # 跳过整个文件
        if rel_path in SKIP_FILES:
            continue

        try:
            text = doc_path.read_text(encoding="utf-8")
        except Exception:
            continue

        for line_no, line in enumerate(text.splitlines(), 1):
            # 跳过描述性文本
            if any(marker in line for marker in SKIP_MARKERS):
                continue
            # 跳过版本演化语境（v3/v9/v10/v11/v13 等历史版本号）
            if re.search(r"\bv[0-9]+(?:\s*\(|\s*→)", line):
                continue

            for key, spec in _BASELINE_PATTERNS.items():
                expected = baseline[key]
                # 正向扫描："N MCP" / "N 个 Mixin" / "Schema vN" 等
                for match in re.finditer(spec["scan_pattern"], line):
                    found = int(match.group(1))
                    if found != expected:
                        inconsistencies.append({
                            "file": rel_path,
                            "line_no": line_no,
                            "line": line.strip(),
                            "expected": expected,
                            "found": found,
                            "key": key,
                            "expected_name": spec["expected_value_name"],
                        })
                # 反向扫描："工具数：N" / "vN Schema" 等
                if "reverse_pattern" in spec:
                    for match in re.finditer(spec["reverse_pattern"], line):
                        found = int(match.group(1))
                        if found != expected:
                            inconsistencies.append({
                                "file": rel_path,
                                "line_no": line_no,
                                "line": line.strip(),
                                "expected": expected,
                                "found": found,
                                "key": key,
                                "expected_name": spec["expected_value_name"],
                            })

    return inconsistencies


def main() -> int:
    parser = argparse.ArgumentParser(description="从源码生成基线并检查文档一致性")
    parser.add_argument("--check", action="store_true", help="扫描所有 .md 文档，报告不一致")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    args = parser.parse_args()

    baseline = generate_baseline()

    if args.json:
        output = {"baseline": baseline}
        if args.check:
            doc_paths = list(PROJECT_ROOT.glob("*.md"))
            doc_paths.extend(PROJECT_ROOT.glob("docs/**/*.md"))
            doc_paths.extend(PROJECT_ROOT.glob("tests/**/*.md"))
            output["inconsistencies"] = scan_document_consistency(baseline, doc_paths)
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0

    # 默认打印基线
    print("=== Call Warden 基线（从源码生成）===")
    print(f"MCP 工具数:        {baseline['mcp_tools']}")
    print(f"db_*.py 文件数:     {baseline['db_files']}")
    print(f"功能 Mixin 数:      {baseline['mixin_functional']}")
    print(f"含基类 Mixin 总数:  {baseline['mixin_total']}")
    print(f"支持语言数:         {baseline['languages']}")
    print(f"Schema 版本:        v{baseline['schema_version']}")
    print(f"产品版本:          {baseline['product_version']}")
    print()

    if args.check:
        print("=== 文档一致性扫描 ===")
        doc_paths = list(PROJECT_ROOT.glob("*.md"))
        doc_paths.extend(PROJECT_ROOT.glob("docs/**/*.md"))
        doc_paths.extend(PROJECT_ROOT.glob("tests/**/*.md"))

        inconsistencies = scan_document_consistency(baseline, doc_paths)
        if not inconsistencies:
            print(f"OK 全部 {len(doc_paths)} 个 .md 文档与基线一致")
            return 0

        print(f"FAIL 发现 {len(inconsistencies)} 处不一致：")
        for item in inconsistencies:
            print(f"  [{item['file']}:{item['line_no']}]")
            print(f"    期望 {item['expected_name']}={item['expected']}，实际 {item['found']}")
            print(f"    {item['line'][:120]}")
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
