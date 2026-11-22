"""技能包结构自检器单测（REQ-01 / AC-01…07）。

形态沿用 tests/test_cicd_gates.py 先例：全部 fixture 用 tmp_path 临时构建，
不落仓库、不读 git，保持 hermetic。导入走 PEP 420 命名空间包
（``skills/_shared`` 无 ``__init__.py``，实测可导入）。

缺陷回归锚：本检查器首版设计曾按「SKILL.md 引用的 references/* 必须存在」实现，
而三包 SKILL.md 全部引用 ``references/cw-gates.md``——该文件是 install.ps1 安装期
从 ``_shared/`` 注入的，仓库内本就不存在 ⇒ 正例本身即反例，AC-01 与 AC-03 互斥。
故引入 ``INJECTED_AT_INSTALL`` 显式豁免，并用
``test_other_missing_reference_not_exempt`` 反向锁定豁免不可通配。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:  # 兼容无 PYTHONPATH 的直接 pytest 调用
    sys.path.insert(0, str(_REPO_ROOT))

from skills._shared import check_skill_packages as csp  # noqa: E402

_FM = "---\nname: {n}\ndescription: d\n---\n"
# 合法 install.ps1 片段：两个标记（_shared + cw-gates.md）都必须出现，见 csp.check_package 第 ⑤ 项。
_INSTALL_OK = "$sharedGates = Join-Path (Split-Path $PSScriptRoot -Parent) '_shared/cw-gates.md'\n"


def _mk(root: Path, name: str, skill_md: str, refs=(), smoke=None, install=_INSTALL_OK) -> Path:
    """在 root 下造一个最小技能包；refs 为 references/ 下要真实建出的文件名。"""
    pkg = root / name
    (pkg / "references").mkdir(parents=True)
    (pkg / "SKILL.md").write_text(skill_md, encoding="utf-8")
    if install is not None:
        (pkg / "install.ps1").write_text(install, encoding="utf-8")
    for fn in refs:
        (pkg / "references" / fn).write_text("x", encoding="utf-8")
    if smoke is not None:
        (pkg / "references" / "smoke.txt").write_text(smoke, encoding="utf-8")
    return pkg


def _codes(root: Path, name: str) -> list[str]:
    """跑单包五类检查，只返回违规码（断言只看码，不看文案）。"""
    pkgs = csp.load_packages(root)
    target = next(p for p in pkgs if p.path.name == name)
    return [f.code for f in csp.check_package(target, _REPO_ROOT)]


def test_real_packages_pass():
    """AC-01：真实三包全合规——误报会在这里立刻红。"""
    packages = csp.load_packages(_REPO_ROOT / "skills")
    names = [p.path.name for p in packages]
    assert names == ["code-review-cw", "requirements-cw", "test-cw"]
    for pkg in packages:
        assert csp.check_package(pkg, _REPO_ROOT) == [], pkg.path.name
    assert csp.main(["--root", str(_REPO_ROOT / "skills")]) == csp.EXIT_OK


def test_bad_frontmatter_detected(tmp_path):
    """AC-02：name 与目录名不一致。"""
    _mk(tmp_path, "mismatch", _FM.format(n="other"))
    assert "FRONTMATTER_NAME_MISMATCH" in _codes(tmp_path, "mismatch")


def test_missing_frontmatter_detected(tmp_path):
    """AC-02：无 frontmatter / 未闭合 / description 空。"""
    _mk(tmp_path, "nofm", "# 标题\n")
    _mk(tmp_path, "unclosed", "---\nname: unclosed\ndescription: d\n")
    _mk(tmp_path, "nodesc", "---\nname: nodesc\ndescription:\n---\n")
    assert _codes(tmp_path, "nofm") == ["FRONTMATTER_MISSING"]
    assert _codes(tmp_path, "unclosed") == ["FRONTMATTER_MISSING"]
    assert _codes(tmp_path, "nodesc") == ["FRONTMATTER_DESCRIPTION_EMPTY"]


def test_duplicate_package_name_detected(tmp_path):
    """AC-02：跨包同名——单包内看不出来，只能聚合层判。"""
    _mk(tmp_path, "aa", _FM.format(n="same"))
    _mk(tmp_path, "same", _FM.format(n="same"))
    assert csp.main(["--root", str(tmp_path)]) == csp.EXIT_VIOLATION


def test_missing_reference_detected(tmp_path):
    """AC-03：引用的 references/<f> 不存在 → 红。"""
    _mk(tmp_path, "refmiss", _FM.format(n="refmiss") + "见 references/nope.md\n")
    assert _codes(tmp_path, "refmiss") == ["REFERENCE_MISSING"]


def test_injected_reference_exempt(tmp_path):
    """AC-03 正向：cw-gates.md 为安装期注入，仓库缺失不算违规；../_shared 形态不计入。"""
    _mk(
        tmp_path,
        "exempt",
        _FM.format(n="exempt") + "见 `references/cw-gates.md` 与仓库源 `../_shared/cw-gates.md`\n",
    )
    assert _codes(tmp_path, "exempt") == []


def test_other_missing_reference_not_exempt(tmp_path):
    """AC-03 反向：豁免只对 cw-gates.md 生效，换个名字必须立刻红（防通配后门）。"""
    _mk(tmp_path, "other", _FM.format(n="other") + "见 references/cw-gates.md.bak\n")
    assert _codes(tmp_path, "other") == ["REFERENCE_MISSING"]
    assert "cw-gates.md.bak" not in csp.INJECTED_AT_INSTALL


def test_duplicate_gates_rejected(tmp_path):
    """AC-04：包内出现 references/cw-gates.md = 违反门禁单一源。"""
    _mk(tmp_path, "dup", _FM.format(n="dup"), refs=["cw-gates.md"])
    assert _codes(tmp_path, "dup") == ["DUPLICATE_GATES"]


def test_bad_smoke_path_detected(tmp_path):
    """AC-05：smoke.txt 非注释行必须是真实路径；注释行与空行跳过。"""
    _mk(tmp_path, "smoke", _FM.format(n="smoke"), smoke="# 注释\n\nskills\nnope/xx.py\n")
    assert _codes(tmp_path, "smoke") == ["SMOKE_BAD_PATH"]
    _mk(tmp_path, "smokeok", _FM.format(n="smokeok"), smoke="# 仅注释\nskills\n")
    assert _codes(tmp_path, "smokeok") == []


def test_install_missing_injection_detected(tmp_path):
    """AC-06：install.ps1 存在但无 _shared 注入片段。"""
    _mk(tmp_path, "noinj", _FM.format(n="noinj"), install="Write-Host hi\n")
    assert _codes(tmp_path, "noinj") == ["INSTALL_MISSING_INJECTION"]


def test_install_missing_file_detected(tmp_path):
    """AC-06：install.ps1 不存在（与「缺注入」分列两个码，否则归因不清）。"""
    _mk(tmp_path, "noinst", _FM.format(n="noinst"), install=None)
    assert _codes(tmp_path, "noinst") == ["INSTALL_MISSING"]


def test_usage_error_exit64(tmp_path):
    """AC-07：未知参数 → 64，且 SystemExit 不漏出 main（返回 int 而非抛异常）。

    这是「用法错」与「发现违规(2)」的分界线：若 SystemExit 漏出，调用方拿到的是
    异常而不是退出码，两态就合并了。
    """
    assert csp.main(["--definitely-not-an-option"]) == csp.EXIT_USAGE
    assert csp.EXIT_USAGE != csp.EXIT_VIOLATION


@pytest.mark.parametrize("sub", ["", "nope"])
def test_no_packages_is_violation(tmp_path, sub):
    """防假绿：--root 打错 / 0 个包必须是 2，不是 0。"""
    root = tmp_path / sub if sub else tmp_path
    assert csp.main(["--root", str(root)]) == csp.EXIT_VIOLATION
    assert csp.load_packages(root) == []


def test_gbk_smoke_file_reports_read_failed(tmp_path):
    """AC-08：非 UTF-8 编码的 smoke.txt → READ_FAILED + exit 2，且不抛栈。

    回归锚（CL-02）：UnicodeDecodeError 继承 ValueError 而非 OSError，旧实现只
    ``except OSError`` ⇒ 中文 Windows 记事本另存的 GBK smoke.txt 会让检查器以
    traceback 崩掉、退出码退化为 1。此断言锁住「编码错也要有干净诊断」。
    """
    pkg = _mk(tmp_path, "gbk", _FM.format(n="gbk"))
    (pkg / "references" / "smoke.txt").write_bytes("# 中文注释\nskills\n".encode("gbk"))

    assert csp.main(["--root", str(tmp_path)]) == csp.EXIT_VIOLATION
    assert "READ_FAILED" in _codes(tmp_path, "gbk")


def test_gbk_skill_md_does_not_traceback(tmp_path):
    """AC-08 补充：SKILL.md 非 UTF-8 时 main 返回 2（不是抛异常）。"""
    pkg = _mk(tmp_path, "gbk2", "placeholder", install=None)
    (pkg / "SKILL.md").write_bytes("---\nname: gbk2\ndescription: 中文\n---\n".encode("gbk"))
    assert csp.main(["--root", str(tmp_path)]) == csp.EXIT_VIOLATION


def test_parse_frontmatter_edge_cases():
    """C-01 契约：折行续写 / 空块 / BOM 已由 utf-8-sig 处理。"""
    assert csp.parse_frontmatter("---\ndescription: 第一行\n  第二行\n---\n") == {"description": "第一行 第二行"}
    assert csp.parse_frontmatter("---\n---\n") is None
    assert csp.parse_frontmatter("") is None


def test_json_report_parsable(tmp_path):
    """C-05：--json 可解析，ok 与退出码同源。"""
    _mk(tmp_path, "ok", _FM.format(n="ok"))
    out = subprocess.run(
        [sys.executable, str(_REPO_ROOT / "skills/_shared/check_skill_packages.py"), "--root", str(tmp_path), "--json"],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    assert out.returncode == csp.EXIT_OK
    payload = json.loads(out.stdout)
    assert payload == {"ok": True, "packages": 1, "findings": []}
