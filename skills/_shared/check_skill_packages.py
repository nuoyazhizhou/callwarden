#!/usr/bin/env python3
"""技能包结构自检（只读）：把四条靠人记的结构约束变成一条能进 CI 的命令。

为什么不用 YAML 解析 frontmatter：技能包 frontmatter 只有顶层 `key: value`，
引入第三方依赖（PyYAML）会让本检查器在裸 Python 环境不可运行，与本仓
「门禁脚本零第三方依赖」的既有做法冲突（见 requirements-brief §3 NFR）。

退出码三态沿用 ``cicd/known_red.py``：0=合规 / 2=发现违规 / 64=用法错误。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

EXIT_OK = 0
EXIT_VIOLATION = 2
EXIT_USAGE = 64

# 安装期由各包 install.ps1 从 skills/_shared/ 注入的文件，仓库内本就不存在，
# 因此不参与「引用必须存在」检查。必须是**显式集合**而非通配：新增注入文件时
# 须同时改本常量与 tests/test_skill_packages_guard.py 的反向断言
# （::test_other_missing_reference_not_exempt），否则豁免会退化成后门。
INJECTED_AT_INSTALL = frozenset({"cw-gates.md"})

# 只认包内 references/<filename> 形态；`../_shared/cw-gates.md` 不含该子串，天然不匹配。
_REF_RE = re.compile(r"references/([\w.\-]+)")

# frontmatter 顶层键：不含冒号的续行视为上一个键的折行续写。
_KV_RE = re.compile(r"^([A-Za-z0-9_\-]+)\s*:\s*(.*)$")


@dataclass(frozen=True)
class Finding:
    """一条违规。``package`` 为包目录名；跨包问题（同名冲突）记 ``-``。"""

    package: str
    code: str
    detail: str


@dataclass(frozen=True)
class Package:
    """一个技能包。``frontmatter`` 为 None 表示没有 frontmatter（违规）。"""

    path: Path
    skill_text: str
    frontmatter: dict | None


def _read_text(path: Path) -> str:
    """读文本：``utf-8-sig`` 剥 BOM；非 UTF-8 编码归一为 ``OSError``。

    中文 Windows 记事本默认存 ANSI/GBK——若把 ``UnicodeDecodeError`` 原样上抛，
    检查器会以 traceback 崩掉、退出码退化为 1，而用户真正需要的是「哪个文件
    编码不对」。归一为 OSError 后，上层用同一条路径就能给出干净诊断。
    """
    try:
        return path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise OSError(
            f"{path}: 非 UTF-8 编码（{exc.reason}，byte {exc.start}）；请另存为 UTF-8"
        ) from exc


def parse_frontmatter(text: str) -> dict | None:
    """解析 SKILL.md 的 frontmatter，返回顶层键值；无/未闭合/空块一律返回 None。

    返回 None 而非 {}：调用方据此区分「没有 frontmatter」（违规）与「有但为空」。
    折行续写（不以 key: 开头的非空行）拼到上一个键，用单空格连接——三包现有
    description 均为单行，折行支持是为避免长描述换行后静默丢内容。
    """
    lines = text.splitlines()
    start = next((i for i, line in enumerate(lines) if line.strip()), None)
    if start is None or lines[start].strip() != "---":
        return None

    data: dict[str, str] = {}
    key: str | None = None
    closed = False
    for line in lines[start + 1:]:
        if line.strip() == "---":
            closed = True
            break
        if not line.strip():
            continue
        match = _KV_RE.match(line)
        if match:
            key = match.group(1)
            data[key] = match.group(2).strip()
        elif key is not None:
            data[key] = f"{data[key]} {line.strip()}".strip()

    if not closed or not data:
        return None
    return data


def load_packages(skills_root: Path) -> list[Package]:
    """扫描 ``skills_root`` 的**直接子目录**，凡含 SKILL.md 者即视为一个技能包。

    「含 SKILL.md」是包的唯一判据：``_shared/`` 因无 SKILL.md 自然排除，
    ``README.md`` 等文件因非目录自然排除——不需要额外的忽略名单。
    路径不存在/非目录时返回 ``[]`` 而不抛 FileNotFoundError，由调用方统一
    报「no skill packages found」；真正不可读（权限）时上抛 OSError。
    """
    if not skills_root.is_dir():
        return []
    packages: list[Package] = []
    for child in sorted(skills_root.iterdir(), key=lambda p: p.name):
        skill_md = child / "SKILL.md"
        if not child.is_dir() or not skill_md.is_file():
            continue
        # _read_text 剥 BOM（PowerShell 写的文件常带 BOM，否则首行 --- 匹配不上）；
        # 非 UTF-8 编码在此转成 OSError 上抛给 main。
        text = _read_text(skill_md)
        packages.append(Package(path=child, skill_text=text, frontmatter=parse_frontmatter(text)))
    return packages


def referenced_reference_files(text: str) -> set[str]:
    """从 SKILL.md 正文提取被引用的 references 文件名（不含 references/ 前缀）。

    不做代码块感知：宁可把示例里的假路径报出来让人改文档，也不漏掉真实断链
    （漏报的代价是装到宿主后技能跑到一半找不到规范）。
    """
    return set(_REF_RE.findall(text))


def check_package(pkg: Package, repo_root: Path) -> list[Finding]:
    """对一个包跑五类检查，返回违规清单（**空列表 = 合规**）。

    五类 ↔ 约束：①frontmatter 元数据完整 ②引用的 reference 必须存在
    ③门禁单一源（包内禁 cw-gates.md）④smoke.txt 只能是真实路径
    ⑤install.ps1 必须从 _shared 注入。

    单条检查失败**不短路**：一次运行暴露全部问题，避免「修一条跑一次」。
    smoke.txt 缺失**不算违规**（可选文件）；只有它存在时才约束其内容为真实路径。
    """
    name = pkg.path.name
    findings: list[Finding] = []

    if pkg.frontmatter is None:
        findings.append(
            Finding(name, "FRONTMATTER_MISSING", "SKILL.md 无 frontmatter（首行须为 --- 且存在闭合 ---）")
        )
    else:
        fm_name = (pkg.frontmatter.get("name") or "").strip()
        if fm_name != name:
            findings.append(
                Finding(name, "FRONTMATTER_NAME_MISMATCH", f"frontmatter name={fm_name!r} 与目录名 {name!r} 不一致")
            )
        if not (pkg.frontmatter.get("description") or "").strip():
            findings.append(
                Finding(name, "FRONTMATTER_DESCRIPTION_EMPTY", "frontmatter description 缺失或为空")
            )

    for ref in sorted(referenced_reference_files(pkg.skill_text)):
        if ref in INJECTED_AT_INSTALL:
            continue
        if not (pkg.path / "references" / ref).is_file():
            findings.append(
                Finding(name, "REFERENCE_MISSING", f"references/{ref} 不存在（SKILL.md 有引用）")
            )

    if (pkg.path / "references" / "cw-gates.md").exists():
        findings.append(
            Finding(
                name,
                "DUPLICATE_GATES",
                "包内存在 references/cw-gates.md；门禁唯一源是 skills/_shared/cw-gates.md（安装期注入）",
            )
        )

    smoke = pkg.path / "references" / "smoke.txt"
    if smoke.is_file():
        try:
            smoke_text = _read_text(smoke)
        except OSError as exc:
            # 单文件读失败只记一条，继续做后面的检查（C-04「本函数不抛」）。
            findings.append(Finding(name, "READ_FAILED", str(exc)))
            smoke_text = ""
        for raw in smoke_text.splitlines():
            line = raw.strip()
            # 整行做路径，不切 '#'：行内注释会被当成路径的一部分（历史真实故障）。
            if not line or line.startswith("#"):
                continue
            if not (repo_root / line).exists():
                findings.append(
                    Finding(name, "SMOKE_BAD_PATH", f"smoke.txt 路径不存在：{line}（相对仓库根）")
                )

    install = pkg.path / "install.ps1"
    if not install.is_file():
        findings.append(Finding(name, "INSTALL_MISSING", "install.ps1 不存在"))
    else:
        try:
            ps1 = _read_text(install)
        except OSError as exc:
            findings.append(Finding(name, "READ_FAILED", str(exc)))
        else:
            # 只查「有没有从 _shared 注入」的标记，不查注入目标目录是否正确——
            # 语义校验需跑 PowerShell 或解析 AST，成本远超收益；本检查定位是防忘记。
            if "_shared" not in ps1 or "cw-gates.md" not in ps1:
                findings.append(
                    Finding(
                        name,
                        "INSTALL_MISSING_INJECTION",
                        "install.ps1 未从 ../_shared 注入 cw-gates.md（缺 _shared 或 cw-gates.md 标记）",
                    )
                )

    return findings


def format_report(findings: list[Finding], package_count: int, as_json: bool) -> str:
    """渲染报告。``ok`` 字段与退出码同源（同一 ``not findings`` 判据），不另设判断。

    ``ensure_ascii=False``：detail 含中文路径与说明，转义后 CI 日志不可读。
    """
    ok = not findings
    if as_json:
        payload = {
            "ok": ok,
            "packages": package_count,
            "findings": [{"package": f.package, "code": f.code, "detail": f.detail} for f in findings],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)
    if ok:
        return f"OK: {package_count} skill packages compliant"
    lines = [f"VIOLATION {f.package}: [{f.code}] {f.detail}" for f in findings]
    bad_packages = len({f.package for f in findings})
    lines.append(f"{len(findings)} violation(s) in {bad_packages} package(s)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    """CLI 入口：返回退出码而非 ``sys.exit``，便于单测直接断言。

    三态：0 合规 / 2 违规（含路径打错、0 个包、读盘失败）/ 64 用法错误。
    argparse 的 ``SystemExit`` 必须在此转成 64，不许漏出 main——否则调用方
    拿到的是异常而不是退出码，「用法错」与「发现违规」就分不开了。

    路径打错与 0 个包都归 2（不是 0）：否则 `--root` 写错会静默假绿。
    """
    repo_root = Path(__file__).resolve().parents[2]
    parser = argparse.ArgumentParser(
        prog="check_skill_packages.py",
        description="技能包结构自检（只读）：frontmatter / references 完整性 / 门禁单一源 / smoke 路径 / install 注入",
    )
    parser.add_argument("--root", default=str(repo_root / "skills"), help="技能包根目录（默认 <repo>/skills）")
    parser.add_argument("--json", action="store_true", help="输出 JSON（CI 解析用）")
    try:
        args = parser.parse_args(argv)
    except SystemExit as exc:  # --help → 0；用法错 → argparse 抛 2，统一转 64
        return EXIT_OK if not exc.code else EXIT_USAGE

    skills_root = Path(args.root)
    try:
        packages = load_packages(skills_root)
    except OSError as exc:
        print(f"cannot read: {skills_root}: {exc}", file=sys.stderr)
        return EXIT_VIOLATION

    if not packages:
        print(f"no skill packages found: {skills_root}", file=sys.stderr)
        return EXIT_VIOLATION

    findings: list[Finding] = []
    try:
        for pkg in packages:
            findings.extend(check_package(pkg, repo_root))
    except OSError as exc:  # 兜底：check_package 之外的读盘失败也要干净退出
        print(f"cannot read: {exc}", file=sys.stderr)
        return EXIT_VIOLATION

    # 跨包同名：单个包内看不出来，只能在聚合层判。
    by_name: dict[str, list[str]] = {}
    for pkg in packages:
        declared = ((pkg.frontmatter or {}).get("name") or "").strip()
        if declared:
            by_name.setdefault(declared, []).append(pkg.path.name)
    for declared, dirs in sorted(by_name.items()):
        if len(dirs) > 1:
            findings.append(
                Finding(
                    "-",
                    "DUPLICATE_PACKAGE_NAME",
                    f"frontmatter name={declared!r} 被多个包使用：{', '.join(sorted(dirs))}",
                )
            )

    print(format_report(findings, len(packages), args.json))
    return EXIT_OK if not findings else EXIT_VIOLATION


if __name__ == "__main__":
    sys.exit(main())
