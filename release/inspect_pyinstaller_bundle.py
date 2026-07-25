#!/usr/bin/env python3
"""检查 PyInstaller 发布目录的结构、模块清单和体积门禁。"""

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
    "fastmcp",
    "opentelemetry",
    "s3transfer",
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
) -> tuple[dict, list[str]]:
    """生成报告并返回不满足发布门禁的错误列表。"""
    bundle = bundle.resolve()
    if not bundle.is_dir():
        return {"bundle": str(bundle)}, [f"bundle 目录不存在: {bundle}"]
    if not pyz_toc.is_file():
        return {"bundle": str(bundle)}, [f"PYZ TOC 不存在: {pyz_toc}"]

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
    top_files = [
        {
            "path": str(path.relative_to(bundle)).replace("\\", "/"),
            "bytes": path.stat().st_size,
        }
        for path in sorted(files, key=lambda item: item.stat().st_size, reverse=True)[:20]
    ]

    report = {
        "bundle": str(bundle),
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
        "top_files": top_files,
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
    if (
        max_unpacked_mb is not None
        and total_bytes > max_unpacked_mb * 1024 * 1024
    ):
        errors.append(
            f"解压体积 {report['unpacked_mb']} MB 超过门禁 {max_unpacked_mb} MB"
        )
    return report, errors


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bundle", type=Path, required=True)
    parser.add_argument("--pyz-toc", type=Path, required=True)
    parser.add_argument("--artifact", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--max-unpacked-mb", type=float)
    args = parser.parse_args()

    report, errors = inspect_bundle(
        bundle=args.bundle,
        pyz_toc=args.pyz_toc,
        artifact=args.artifact,
        max_unpacked_mb=args.max_unpacked_mb,
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
