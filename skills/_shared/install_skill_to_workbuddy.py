# -*- coding: utf-8 -*-
"""把 ALM 三个技能包安装到 WorkBuddy 用户级技能目录（`~/.workbuddy/skills`）。

为什么需要独立脚本：三个包的 `install.ps1` 只覆盖 OpenAI Codex（`~/.codex`）、
Claude Code（`~/.claude`）、Trae CN（`~/.trae-cn`）三个宿主，
**不含 WorkBuddy 自己的用户级技能目录**。安装语义与 `install.ps1` 对齐：

    SKILL.md + README.md + references/ scripts/ assets/（跳过缓存产物）
    再注入 `../_shared/cw-gates.md` -> `references/cw-gates.md`

单一事实源口径不变：`references/cw-gates.md` 仍然只从 `_shared/` 注入，
包内不留副本（见 `check_skill_packages.py` 的 AC-04）。

用法：
    python skills/_shared/install_skill_to_workbuddy.py            # 安装
    python skills/_shared/install_skill_to_workbuddy.py --dry-run  # 只报告不落盘
    python skills/_shared/install_skill_to_workbuddy.py --dest D:/x/skills

退出码（与 `cicd/known_red.py` 三态一致）：0=OK，1=违规，64=用法错误。
"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import sys
from pathlib import Path

EXIT_OK = 0
EXIT_VIOLATION = 1
EXIT_USAGE = 64

SKILLS_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DEST = Path.home() / ".workbuddy" / "skills"
PACKAGES = ("requirements-cw", "code-review-cw", "test-cw")
ASSET_DIRS = ("references", "scripts", "assets")
EXTRA_FILES = ("README.md",)
SKIP_DIR_NAMES = frozenset({"__pycache__"})
SKIP_SUFFIXES = frozenset({".pyc", ".pyo"})
GATES_NAME = "cw-gates.md"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def copy_tree(src: Path, dst: Path, dry_run: bool) -> int:
    """递归复制，跳过字节码缓存；返回文件数。"""
    count = 0
    for item in sorted(src.iterdir()):
        if item.is_dir():
            if item.name in SKIP_DIR_NAMES:
                continue
            count += copy_tree(item, dst / item.name, dry_run)
        elif item.suffix not in SKIP_SUFFIXES:
            if not dry_run:
                dst.mkdir(parents=True, exist_ok=True)
                shutil.copy2(item, dst / item.name)
            count += 1
    return count


def frontmatter_name(skill_md: Path) -> str:
    for line in skill_md.read_text(encoding="utf-8-sig").splitlines():
        if line.startswith("name:"):
            return line.split(":", 1)[1].strip()
    return ""


def install_one(pkg: str, dest_root: Path, gates: Path, dry_run: bool) -> list[str]:
    src = SKILLS_ROOT / pkg
    dest = dest_root / pkg
    problems: list[str] = []

    if not (src / "SKILL.md").is_file():
        return [f"{pkg}: 源缺 SKILL.md"]

    if not dry_run:
        if dest.exists():
            shutil.rmtree(dest)
        dest.mkdir(parents=True)
        shutil.copy2(src / "SKILL.md", dest / "SKILL.md")

    files = 1
    for extra in EXTRA_FILES:
        if (src / extra).is_file():
            if not dry_run:
                shutil.copy2(src / extra, dest / extra)
            files += 1
    for name in ASSET_DIRS:
        if (src / name).is_dir():
            files += copy_tree(src / name, dest / name, dry_run)

    # 注入共享门禁（唯一事实源，不落包内副本）
    if not dry_run:
        ref = dest / "references"
        ref.mkdir(parents=True, exist_ok=True)
        shutil.copy2(gates, ref / GATES_NAME)
    files += 1

    if not dry_run:
        got = frontmatter_name(dest / "SKILL.md")
        if got != pkg:
            problems.append(f"{pkg}: frontmatter name={got!r} != {pkg!r}")
        injected = dest / "references" / GATES_NAME
        if not injected.is_file() or sha256(injected) != sha256(gates):
            problems.append(f"{pkg}: {GATES_NAME} 注入校验失败")
        if list(dest.rglob("*.pyc")):
            problems.append(f"{pkg}: 混入 .pyc 缓存产物")

    print(f"{'PLAN' if dry_run else 'OK  '} {pkg:<16} files={files}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="安装 ALM 技能包到 WorkBuddy 用户级技能目录")
    parser.add_argument("--dest", default=str(DEFAULT_DEST), help="目标目录（默认 ~/.workbuddy/skills）")
    parser.add_argument("--dry-run", action="store_true", help="只报告将要复制的内容，不落盘")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help → 0；用法错 → argparse 抛 2，统一转 64
        return EXIT_OK if not exc.code else EXIT_USAGE

    gates = SKILLS_ROOT / "_shared" / GATES_NAME
    if not gates.is_file():
        print(f"失败：找不到共享门禁 {gates}（唯一事实源，不允许包内副本兜底）", file=sys.stderr)
        return EXIT_VIOLATION

    dest_root = Path(args.dest).expanduser()
    problems: list[str] = []
    for pkg in PACKAGES:
        problems.extend(install_one(pkg, dest_root, gates, args.dry_run))

    for p in problems:
        print(f"  - {p}", file=sys.stderr)
    if problems:
        return EXIT_VIOLATION

    print(f"\n目标目录：{dest_root}")
    print(f"共享门禁 sha256：{sha256(gates)}")
    if not args.dry_run:
        print("重启宿主会话后生效（WorkBuddy 技能列表按目录签名缓存，新增目录会自动重建）。")
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
