#!/usr/bin/env python3
"""检查 PyInstaller 发布目录的结构、模块清单和体积门禁。

P0-A 扩展（2026-07-25）：新增 distribution 字节占比报告，用于 Rust-only parser
切换计划（docs/design/rust-only-parser-cutover-plan.md Phase 0 步骤 6+7）。
报告将发布目录中的文件按 distribution 维度聚合：
- ``tree_sitter``（Python tree-sitter 核心）
- ``tree_sitter_<lang>``（16 种 grammar wheel）
- ``callwarden_parsers``（Python 各语言 parser 实现模块）
- ``callwarden_core``（Rust 扩展 .pyd/.so）
- ``python_runtime``（_internal/ 中的标准库与其他第三方依赖）
- ``other``（无法归类的文件）

每个 distribution 输出 file_count / byte_count / byte_ratio，
配合 ``--forbid-distribution`` 可对指定 distribution 设置零容忍门禁，
供 Phase 1 拆 client/agent 轻包时使用（本步骤只产报告，不强制零容忍）。
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable


FORBIDDEN_MODULE_ROOTS = {
    "boto3",
    "botocore",
    "dns",
    "email_validator",
    "fastmcp",
    "opentelemetry",
    "s3transfer",
    "semgrep",
    "sentence_transformers",
    "sqlite_vec",
    "torch",
    "transformers",
}

REQUIRED_MODULE_ROOTS = {
    "callwarden",
    "mcp",
    "numpy",
    "pathspec",
    "psutil",
    "requests",
    "rich",
    "watchdog",
}

# client/agent bundle 必需模块根（无 numpy/parser，纯 RPC + watcher）。
# client/agent 不做本地解析，numpy 仅由 local bundle 的向量搜索/余弦相似度路径使用。
REQUIRED_MODULE_ROOTS_CLIENT = {
    "callwarden",
    "mcp",
    "pathspec",
    "psutil",
    "requests",
    "rich",
    "watchdog",
}


# ============================================
# P0-A: distribution 分类
# ============================================

# 16 种 tree-sitter Python grammar distribution 名（与 pyproject.toml 依赖一致）
TREE_SITTER_GRAMMAR_DISTRIBUTIONS: tuple[str, ...] = (
    "tree_sitter_rust",
    "tree_sitter_typescript",
    "tree_sitter_python",
    "tree_sitter_kotlin",
    "tree_sitter_go",
    "tree_sitter_java",
    "tree_sitter_c",
    "tree_sitter_cpp",
    "tree_sitter_javascript",
    "tree_sitter_c_sharp",
    "tree_sitter_ruby",
    "tree_sitter_php",
    "tree_sitter_swift",
    "tree_sitter_scala",
    "tree_sitter_hcl",
    "tree_sitter_elixir",
)

# 视为 parser 实现模块的 distribution 名（callwarden.parsers 子模块）
CALLWARDEN_PARSERS_DISTRIBUTION = "callwarden_parsers"

# Rust 扩展 distribution 名
CALLWARDEN_CORE_DISTRIBUTION = "callwarden_core"

# Python tree-sitter 核心 distribution 名
TREE_SITTER_CORE_DISTRIBUTION = "tree_sitter"

# Python 运行时 distribution 名
PYTHON_RUNTIME_DISTRIBUTION = "python_runtime"

# 兜底 distribution 名
OTHER_DISTRIBUTION = "other"

# 所有 parser 相关 distribution 名（client/agent bundle 零容忍）。
# 包含 tree-sitter 核心、16 种 grammar、callwarden.parsers Python 实现模块。
# 用于 ``--role client`` 自动禁止，或 ``--forbid-all-parser-distributions`` 显式禁止。
PARSER_DISTRIBUTIONS: tuple[str, ...] = (
    TREE_SITTER_CORE_DISTRIBUTION,
    CALLWARDEN_PARSERS_DISTRIBUTION,
    *TREE_SITTER_GRAMMAR_DISTRIBUTIONS,
)


def _classify_distribution(rel_path: str) -> str:
    """根据文件相对路径归类到 distribution。

    PyInstaller --onedir 产物布局：
        dist/callwarden/
          cw.exe / cw                       # 根级入口
          _internal/
            tree_sitter/*.pyc               # 核心
            tree_sitter_python/*.pyc + .pyd # grammar
            ...
            callwarden/parsers/*.pyc         # Python parser 实现
            callwarden_core.pyd / .so        # Rust 扩展
            <其他第三方与标准库>

    归类规则按优先级：
    1. 路径片段以 ``tree_sitter_<lang>/`` 或 ``tree_sitter_<lang>.`` 开头 → grammar
    2. 路径片段以 ``tree_sitter/`` 或文件名为 ``tree_sitter*.pyd/.so`` → 核心
    3. 路径片段以 ``callwarden/parsers/`` 开头 → Python parser 实现
    4. 文件名为 ``callwarden_core.pyd`` / ``callwarden_core.so`` → Rust 扩展
    5. 其他 _internal/ 下的文件 → python_runtime
    6. 根级入口（cw/cw-client/cw-agent）→ entry（计入 python_runtime，不单独分组）
    """
    # 统一使用 / 作为分隔符，便于跨平台比较
    norm = rel_path.replace("\\", "/")
    parts = norm.split("/")

    # 4. Rust 扩展：根级或 _internal/ 下的 callwarden_core.{pyd,so}
    base = parts[-1]
    if base in {"callwarden_core.pyd", "callwarden_core.so"}:
        return CALLWARDEN_CORE_DISTRIBUTION

    # 1. grammar：tree_sitter_<lang>/ 目录或 tree_sitter_<lang>.* 文件
    for grammar in TREE_SITTER_GRAMMAR_DISTRIBUTIONS:
        # 目录形式：.../tree_sitter_python/...
        if any(part == grammar for part in parts):
            return grammar
        # 文件形式：tree_sitter_python.cp310-win_amd64.pyd / tree_sitter_python.so
        if base.startswith(grammar + ".") or base == f"{grammar}.pyd" or base == f"{grammar}.so":
            return grammar

    # 2. tree-sitter 核心：tree_sitter/ 目录或根级 tree_sitter.*.pyd/.so
    if "tree_sitter" in parts:
        return TREE_SITTER_CORE_DISTRIBUTION
    if base.startswith("tree_sitter.") or base in {"tree_sitter.pyd", "tree_sitter.so"}:
        return TREE_SITTER_CORE_DISTRIBUTION

    # 3. callwarden.parsers 子模块
    # 找到 callwarden/parsers/ 路径片段
    for i, part in enumerate(parts):
        if part == "callwarden" and i + 1 < len(parts) and parts[i + 1] == "parsers":
            return CALLWARDEN_PARSERS_DISTRIBUTION

    # 5/6. _internal/ 下的其他文件或根级入口 → python_runtime
    return PYTHON_RUNTIME_DISTRIBUTION


def compute_distribution_breakdown(bundle: Path) -> dict:
    """按 distribution 聚合 bundle 中的文件数与字节数。

    返回结构：
        {
            "total_bytes": int,
            "total_files": int,
            "distributions": {
                "<name>": {
                    "file_count": int,
                    "byte_count": int,
                    "byte_ratio": float,   # 0.0~1.0，相对 total_bytes
                    "file_ratio": float,   # 0.0~1.0，相对 total_files
                },
                ...
            },
        }

    包含所有 16 种 grammar distribution 名（即使文件数为 0 也列出），
    便于后续零容忍门禁和差异比较。
    """
    files = list(_iter_files(bundle))
    total_bytes = sum(path.stat().st_size for path in files)
    total_files = len(files)

    # 初始化所有 distribution 名（保证输出稳定）
    dist_bytes: dict[str, int] = {}
    dist_files: dict[str, int] = {}
    for grammar in TREE_SITTER_GRAMMAR_DISTRIBUTIONS:
        dist_bytes[grammar] = 0
        dist_files[grammar] = 0
    for fixed in (
        TREE_SITTER_CORE_DISTRIBUTION,
        CALLWARDEN_PARSERS_DISTRIBUTION,
        CALLWARDEN_CORE_DISTRIBUTION,
        PYTHON_RUNTIME_DISTRIBUTION,
        OTHER_DISTRIBUTION,
    ):
        dist_bytes[fixed] = 0
        dist_files[fixed] = 0

    for path in files:
        rel = str(path.relative_to(bundle))
        dist = _classify_distribution(rel)
        size = path.stat().st_size
        dist_bytes[dist] = dist_bytes.get(dist, 0) + size
        dist_files[dist] = dist_files.get(dist, 0) + 1

    distributions: dict[str, dict] = {}
    for name in sorted(dist_bytes.keys()):
        byte_count = dist_bytes[name]
        file_count = dist_files[name]
        distributions[name] = {
            "file_count": file_count,
            "byte_count": byte_count,
            "byte_ratio": round(byte_count / total_bytes, 6) if total_bytes else 0.0,
            "file_ratio": round(file_count / total_files, 6) if total_files else 0.0,
        }

    return {
        "total_bytes": total_bytes,
        "total_files": total_files,
        "distributions": distributions,
    }


def _iter_files(root: Path) -> Iterable[Path]:
    """按稳定顺序返回目录中的普通文件。"""
    return sorted(path for path in root.rglob("*") if path.is_file())


def _read_pyz_modules(toc_path: Path) -> list[str]:
    """读取 PyInstaller PYZ TOC 中的模块名。"""
    value = ast.literal_eval(toc_path.read_text(encoding="utf-8"))
    # PyInstaller 6 写入 (pyz_path, module_toc)；测试夹具和旧版可直接是 TOC。
    if (
        isinstance(value, tuple)
        and len(value) >= 2
        and isinstance(value[1], (list, tuple))
    ):
        value = value[1]
    modules = []
    for item in value:
        if isinstance(item, (list, tuple)) and item and isinstance(item[0], str):
            modules.append(item[0])
    return sorted(set(modules))


def inspect_bundle(
    bundle: Path,
    pyz_toc: Path,
    artifact: Path | None = None,
    max_unpacked_mb: float | None = None,
    forbid_distributions: tuple[str, ...] | None = None,
    role: str | None = None,
) -> tuple[dict, list[str]]:
    """生成报告并返回不满足发布门禁的错误列表。

    Args:
        bundle: PyInstaller --onedir 产物根目录。
        pyz_toc: PYZ-00.toc 文件路径，包含 PyInstaller 收集的模块清单。
        artifact: 压缩包路径，可选。提供时记录压缩包大小用于包体门禁。
        max_unpacked_mb: 解压目录体积上限（MiB），超过则报错。
        forbid_distributions: 零容忍 distribution 名列表，存在任意文件即报错。
            用于 Phase 1 拆 client/agent 轻包时强制 parser distribution 为 0。
        role: bundle 角色（``"local"`` / ``"client"`` / ``None``）。

            - ``"client"``：使用 ``REQUIRED_MODULE_ROOTS_CLIENT``（不含 numpy），
              并自动将 ``PARSER_DISTRIBUTIONS`` 加入零容忍列表（parser distribution=0）。
            - ``"local"`` 或 ``None``：使用 ``REQUIRED_MODULE_ROOTS``（含 numpy），
              不自动禁止 parser distribution（local bundle 保留 parser 回退路径）。
    """
    bundle = bundle.resolve()
    if not bundle.is_dir():
        return {"bundle": str(bundle)}, [f"bundle 目录不存在: {bundle}"]
    if not pyz_toc.is_file():
        return {"bundle": str(bundle)}, [f"PYZ TOC 不存在: {pyz_toc}"]

    # 角色驱动的必需模块根与零容忍 distribution
    if role == "client":
        required_roots = REQUIRED_MODULE_ROOTS_CLIENT
        # client/agent 自动禁止所有 parser distribution（parser distribution=0）
        forbid_set = set(forbid_distributions or ())
        forbid_set.update(PARSER_DISTRIBUTIONS)
        forbid_distributions = tuple(sorted(forbid_set))
    else:
        required_roots = REQUIRED_MODULE_ROOTS

    files = list(_iter_files(bundle))
    total_bytes = sum(path.stat().st_size for path in files)
    internal_dirs = sorted(
        str(path.relative_to(bundle))
        for path in bundle.rglob("_internal")
        if path.is_dir()
    )
    modules = _read_pyz_modules(pyz_toc)
    module_roots = Counter(name.split(".", 1)[0] for name in modules)
    forbidden_modules = sorted(
        name
        for name in modules
        if name.split(".", 1)[0] in FORBIDDEN_MODULE_ROOTS
    )
    missing_required_roots = sorted(required_roots - set(module_roots))
    top_files = [
        {
            "path": str(path.relative_to(bundle)).replace("\\", "/"),
            "bytes": path.stat().st_size,
        }
        for path in sorted(files, key=lambda item: item.stat().st_size, reverse=True)[:20]
    ]

    # P0-A: distribution 字节占比报告
    breakdown = compute_distribution_breakdown(bundle)

    report = {
        "bundle": str(bundle),
        "role": role or "local",
        "file_count": len(files),
        "unpacked_bytes": total_bytes,
        "unpacked_mb": round(total_bytes / (1024 * 1024), 2),
        "internal_directories": internal_dirs,
        "root_executables": sorted(
            path.name for path in bundle.iterdir() if path.is_file()
        ),
        "module_count": len(modules),
        "module_roots": dict(sorted(module_roots.items())),
        "forbidden_modules": forbidden_modules,
        "missing_required_module_roots": missing_required_roots,
        "top_files": top_files,
        # distribution 维度聚合（每个 distribution 的文件数与字节占比）
        "distributions": breakdown["distributions"],
        "distribution_total_bytes": breakdown["total_bytes"],
        "distribution_total_files": breakdown["total_files"],
    }

    if artifact is not None:
        artifact = artifact.resolve()
        if artifact.is_file():
            report["artifact"] = str(artifact)
            report["artifact_bytes"] = artifact.stat().st_size
            report["artifact_mb"] = round(
                artifact.stat().st_size / (1024 * 1024), 2
            )

    errors = []
    if internal_dirs != ["_internal"]:
        errors.append(
            f"必须且只能有一个根级 _internal，实际: {internal_dirs}"
        )
    if forbidden_modules:
        roots = sorted({name.split(".", 1)[0] for name in forbidden_modules})
        errors.append(f"发现禁止打包的模块根: {', '.join(roots)}")
    if missing_required_roots:
        errors.append(
            "发布运行时缺少必需模块根: " + ", ".join(missing_required_roots)
        )
    if (
        max_unpacked_mb is not None
        and total_bytes > max_unpacked_mb * 1024 * 1024
    ):
        errors.append(
            f"解压体积 {report['unpacked_mb']} MB 超过门禁 {max_unpacked_mb} MB"
        )

    # P0-A/P0-B: 零容忍 distribution 门禁
    # 用于 Phase 1 拆 client/agent 轻包时强制 parser/grammar 为 0。
    # role="client" 自动注入 PARSER_DISTRIBUTIONS；local 角色默认不触发。
    if forbid_distributions:
        for dist_name in forbid_distributions:
            info = breakdown["distributions"].get(dist_name)
            if info is None:
                # 允许传入未知 distribution 名（如未来新增 grammar），不视为错误
                continue
            if info["file_count"] > 0:
                errors.append(
                    f"distribution '{dist_name}' 必须为空，"
                    f"实际 {info['file_count']} 文件 / "
                    f"{info['byte_count']} 字节"
                )
    return report, errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "检查 PyInstaller 发布目录的结构、模块清单、体积门禁与 distribution 占比。"
        ),
    )
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--pyz-toc", type=Path, required=True)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-unpacked-mb", type=float)
    parser.add_argument(
        "--forbid-distribution",
        action="append",
        default=[],
        metavar="NAME",
        help=(
            "零容忍 distribution 名（可重复传入）。"
            "存在任意文件即报错。"
            "可用值见 TREE_SITTER_GRAMMAR_DISTRIBUTIONS / "
            "CALLWARDEN_PARSERS_DISTRIBUTION / TREE_SITTER_CORE_DISTRIBUTION 等。"
        ),
    )
    parser.add_argument(
        "--role",
        choices=["local", "client"],
        default="local",
        help=(
            "bundle 角色（默认 local）。"
            "client 角色自动禁止所有 parser distribution（parser distribution=0），"
            "且不要求 numpy 模块根。"
            "local 角色保留 parser 回退路径，要求 numpy。"
        ),
    )
    args = parser.parse_args()

    report, errors = inspect_bundle(
        bundle=args.bundle,
        pyz_toc=args.pyz_toc,
        artifact=args.artifact,
        max_unpacked_mb=args.max_unpacked_mb,
        forbid_distributions=tuple(args.forbid_distribution) or None,
        role=args.role,
    )
    report["errors"] = errors
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if errors:
        for error in errors:
            print(f"ERROR: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
