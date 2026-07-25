"""P0-A 基线生成脚本：捕获当前 commit 下 16 语言 ParseFact 能力与 bundle 字节占比。

用途：
    py -3.14 tests/parser_contract/generate_baseline.py [--bundle <dir>] [--pyz-toc <toc>] [--output <path>]

输出 JSON 结构（tests/parser_contract/baseline.json）：
    {
        "generated_at": "ISO8601",
        "commit_sha": "<git HEAD>",
        "platform": {"python": "...", "platform": "...", "machine": "..."},
        "language_capability": {
            "<lang>": {
                "rust_supported": bool,
                "python_parser_available": bool,
                "sample_path": "<fixture filename>",
                "symbols_count_py": int,
                "symbols_count_rs": int,
                "kinds_py": [str],
                "kinds_rs": [str],
                "signature_present_py": bool,
                "signature_present_rs": bool,
                "visibility_present_py": bool,
                "visibility_present_rs": bool,
                "calls_count_py": int,
                "calls_count_rs": int,
                "imports_count_py": int,
                "imports_count_rs": int,
                "references_present_py": bool,
                "references_present_rs": bool,
                "rust_module_path": bool,
                "python_module_path": bool,
                "known_symbol_diffs_count": int,
                "known_call_diffs_count": int,
                "known_symbol_diffs_reason": str,
                "known_call_diffs_reason": str,
                "gaps": [str],   # 当前明确暴露的缺口描述
            },
            ...
        },
        "bundle_distribution_breakdown": {...} | null,
        "bundle_size_baseline": {
            "unpacked_bytes": int | null,
            "unpacked_mb": float | null,
            "methodology": "..."   # 当无构建产物时记录基线方法
        }
    }

设计原则：
- 此脚本不修改任何代码，只读取 parser 输出并记录
- 失败的解析（任一 parser 报错）必须如实记录到 ``gaps``
- 不依赖任何外部 fixture：所有样本代码内联在脚本中
- 复用 tests/test_p31_multi_lang.py 中已经定义的 11 种语言样本
- 另补充 Kotlin/Swift/Elixir/HCL 样本（取自 tests/test_l9_rust_multilang.py）
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import platform
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

# 让脚本在未安装 callwarden 时也能运行（通过 sys.path 注入）
_PKG_ROOT = Path(__file__).resolve().parents[2]  # 仓库根目录
_TESTS_DIR = Path(__file__).resolve().parents[1]  # tests/ 目录
if str(_PKG_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PKG_ROOT.parent))
if str(_TESTS_DIR) not in sys.path:
    sys.path.insert(0, str(_TESTS_DIR))


# ============================================
# 16 语言样本（覆盖 Phase 0 契约检查）
# ============================================

# 复用 test_p31_multi_lang 中的 11 种语言样本
from test_p31_multi_lang import _LANGUAGE_SAMPLES as _P31_SAMPLES  # noqa: E402


# 补充 5 种语言样本（Kotlin/Swift/Elixir/HCL/C 不在 _LANGUAGE_SAMPLES 中）
_KOTLIN_SAMPLE = """\
package com.example

import kotlin.collections.List

class UserService {
    fun findUser(id: Int): String {
        return getName(id)
    }
    fun getName(id: Int): String {
        return "user_" + id.toString()
    }
}
"""

_SWIFT_SAMPLE = """\
import Foundation

class UserService {
    func findUser(id: Int) -> String {
        return getName(id: Int)
    }
    func getName(id: Int) -> String {
        return "user_\\(id)"
    }
}

protocol Drawable {
    func draw()
}
"""

_ELIXIR_SAMPLE = """\
defmodule MyModule do
  def hello(name) do
    IO.puts("Hello, " <> name)
  end
end
"""

_HCL_SAMPLE = """\
resource "aws_instance" "web" {
  ami           = "ami-12345"
  instance_type = "t3.micro"
  public_ip     = aws_instance.web.private_ip
}

variable "region" {
  default = "us-east-1"
}
"""

# C 语言样本（C 走专用 batch_parse_c_files_pool 路径，但仍可用 parse_c_file 单文件解析）
_C_SAMPLE = """\
#include <stdio.h>

struct Point {
    int x;
    int y;
};

int add(int a, int b) {
    return a + b;
}

int main() {
    struct Point p;
    p.x = 1;
    p.y = 2;
    int s = add(1, 2);
    printf("%d\\n", s);
    return 0;
}
"""


_ALL_LANGUAGE_SAMPLES: list[tuple[str, str, str]] = list(_P31_SAMPLES) + [
    ("kotlin", "Sample.kt", _KOTLIN_SAMPLE),
    ("swift", "sample.swift", _SWIFT_SAMPLE),
    ("elixir", "sample.ex", _ELIXIR_SAMPLE),
    ("hcl", "sample.tf", _HCL_SAMPLE),
    ("c", "sample.c", _C_SAMPLE),
]


# ============================================
# 已知差异清单（与 tests/test_rust_python_alignment.py 保持一致）
# ============================================

# 直接 import 以确保基线与测试白名单同步
try:
    from test_rust_python_alignment import KNOWN_SYMBOL_DIFFS, KNOWN_CALL_DIFFS  # type: ignore
except Exception:  # pragma: no cover - 仅在测试模块无法 import 时降级
    KNOWN_SYMBOL_DIFFS = {}
    KNOWN_CALL_DIFFS = {}


# ============================================
# 能力探测
# ============================================

def _has_rust_ext() -> bool:
    """检测 callwarden_core 是否可导入。"""
    try:
        import callwarden_core  # type: ignore  # noqa: F401
        return True
    except Exception:
        return False


def _rust_supported_languages() -> set[str]:
    """获取 Rust parser 声明支持的语言集合。"""
    if not _has_rust_ext():
        return set()
    try:
        import callwarden_core  # type: ignore
        return set(callwarden_core.supported_languages())
    except Exception:
        return set()


def _python_create_parser(file_path: str):
    """创建 Python parser（失败返回 None）。"""
    try:
        from callwarden.parsers import create_parser  # type: ignore
        return create_parser(file_path)
    except Exception:
        return None


def _rust_parse_file(path: str, lang: str, module_path: str = "test.baseline"):
    """调用 Rust parse_file_lang，失败返回 None。"""
    try:
        from callwarden_core import parse_file_lang  # type: ignore
        return parse_file_lang(path, module_path, lang)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _rust_parse_c_file(path: str, module_path: str = "test.baseline"):
    """C 语言专用路径：parse_c_file。"""
    try:
        from callwarden_core import parse_c_file  # type: ignore
        return parse_c_file(path, module_path)
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def _probe_language(lang: str, filename: str, content: str, rust_supported: set[str]) -> dict:
    """对单种语言运行 Python + Rust parser，捕获能力清单。"""
    sample_path = filename
    capability: dict[str, Any] = {
        "rust_supported": lang in rust_supported,
        "python_parser_available": False,
        "sample_path": sample_path,
        "symbols_count_py": 0,
        "symbols_count_rs": 0,
        "kinds_py": [],
        "kinds_rs": [],
        "signature_present_py": False,
        "signature_present_rs": False,
        "visibility_present_py": False,
        "visibility_present_rs": False,
        "calls_count_py": 0,
        "calls_count_rs": 0,
        "imports_count_py": 0,
        "imports_count_rs": 0,
        "references_present_py": False,
        "references_present_rs": False,
        "rust_module_path": False,
        "python_module_path": False,
        "known_symbol_diffs_count": 0,
        "known_call_diffs_count": 0,
        "known_symbol_diffs_reason": "",
        "known_call_diffs_reason": "",
        "gaps": [],
    }

    # 已知差异摘要（与 alignment 测试白名单同步）
    if lang in KNOWN_SYMBOL_DIFFS:
        reason, counter = KNOWN_SYMBOL_DIFFS[lang]
        capability["known_symbol_diffs_count"] = int(sum(counter.values()))
        capability["known_symbol_diffs_reason"] = reason
    if lang in KNOWN_CALL_DIFFS:
        reason, counter = KNOWN_CALL_DIFFS[lang]
        capability["known_call_diffs_count"] = int(sum(counter.values()))
        capability["known_call_diffs_reason"] = reason

    with tempfile.TemporaryDirectory(prefix=f"cw_baseline_{lang}_") as tmp:
        fpath = os.path.join(tmp, filename)
        with open(fpath, "w", encoding="utf-8") as f:
            f.write(content)

        # Python parser
        py_parser = _python_create_parser(fpath)
        py_result: dict[str, Any] | None = None
        if py_parser is not None:
            capability["python_parser_available"] = True
            try:
                py_result = py_parser.parse_file(fpath, "test.baseline")
            except Exception as e:
                capability["gaps"].append(
                    f"python_parser_crash: {type(e).__name__}: {e}"
                )

        if py_result is not None:
            py_symbols = py_result.get("symbols", []) or []
            py_calls = py_result.get("raw_calls", []) or []
            py_imports = py_result.get("imports", []) or []
            capability["symbols_count_py"] = len(py_symbols)
            capability["calls_count_py"] = len(py_calls)
            capability["imports_count_py"] = len(py_imports)
            capability["kinds_py"] = sorted({s.get("kind", "") for s in py_symbols})
            capability["signature_present_py"] = any(
                s.get("signature") for s in py_symbols
            )
            capability["visibility_present_py"] = any(
                s.get("visibility") for s in py_symbols
            )
            capability["python_module_path"] = bool(
                py_result.get("module_path") or any(
                    s.get("module_path") for s in py_symbols
                )
            )
            # HCL 的 references 是 raw_calls 中含 "." 的 callee_name
            capability["references_present_py"] = any(
                "." in (c.get("callee_name") or "") for c in py_calls
            )

        # Rust parser
        rs_result: dict[str, Any] | None = None
        if lang == "c":
            # C 走专用路径
            rs_result = _rust_parse_c_file(fpath)
        elif lang in rust_supported:
            rs_result = _rust_parse_file(fpath, lang)

        if rs_result is not None:
            if rs_result.get("error"):
                capability["gaps"].append(
                    f"rust_parser_error: {rs_result['error']}"
                )
            else:
                rs_symbols = rs_result.get("symbols", []) or []
                rs_calls = rs_result.get("raw_calls", []) or rs_result.get("calls", []) or []
                rs_imports = rs_result.get("imports", []) or []
                capability["symbols_count_rs"] = len(rs_symbols)
                capability["calls_count_rs"] = len(rs_calls)
                capability["imports_count_rs"] = len(rs_imports)
                capability["kinds_rs"] = sorted({s.get("kind", "") for s in rs_symbols})
                capability["signature_present_rs"] = any(
                    s.get("signature") for s in rs_symbols
                )
                capability["visibility_present_rs"] = any(
                    s.get("visibility") for s in rs_symbols
                )
                capability["rust_module_path"] = bool(
                    rs_result.get("module_path") or any(
                        s.get("module_path") for s in rs_symbols
                    )
                )
                # references：HCL Rust 路径暂未实现；其他语言不适用
                capability["references_present_rs"] = any(
                    "." in (c.get("callee_name") or "") for c in rs_calls
                )

        # 显式暴露的缺口（与设计文档 §2.3 已知差异对齐）
        if lang == "typescript":
            if capability["symbols_count_rs"] == 0 and capability["symbols_count_py"] > 0:
                capability["gaps"].append(
                    "typescript_zero_symbols: Rust 未提取任何符号，与 Python 严重不一致"
                )
            elif capability["symbols_count_rs"] < capability["symbols_count_py"]:
                capability["gaps"].append(
                    f"typescript_missing_symbols: Rust {capability['symbols_count_rs']} "
                    f"< Python {capability['symbols_count_py']}"
                )
        if lang == "php":
            if capability["symbols_count_rs"] < capability["symbols_count_py"]:
                capability["gaps"].append(
                    f"php_missing_symbols: Rust {capability['symbols_count_rs']} "
                    f"< Python {capability['symbols_count_py']}（property 缺失）"
                )
        if lang == "scala":
            if capability["calls_count_rs"] < capability["calls_count_py"]:
                capability["gaps"].append(
                    f"scala_missing_calls: Rust {capability['calls_count_rs']} "
                    f"< Python {capability['calls_count_py']}（对象方法调用缺失）"
                )
        if lang == "hcl":
            if not capability["rust_supported"]:
                capability["gaps"].append(
                    "hcl_not_in_rust_supported_languages: HCL 引用提取未在 Rust 路径实现"
                )
            if capability["references_present_py"] and not capability["references_present_rs"]:
                capability["gaps"].append(
                    "hcl_references_not_extracted_in_rust: attribute 引用未由 Rust 提取"
                )
        if lang == "c":
            # C 走专用 Rust 路径，但仍需检查 baseline 是否产生符号
            if capability["symbols_count_rs"] == 0:
                capability["gaps"].append(
                    "c_zero_symbols_rust: Rust parse_c_file 未提取任何符号"
                )

        # 通用缺口：Python 报告非空 signature/visibility 但 Rust 全空
        if (
            capability["signature_present_py"]
            and not capability["signature_present_rs"]
            and capability["symbols_count_rs"] > 0
        ):
            capability["gaps"].append(
                "signature_missing_in_rust: Python 有非空 signature，Rust 全空"
            )
        if (
            capability["visibility_present_py"]
            and not capability["visibility_present_rs"]
            and capability["symbols_count_rs"] > 0
        ):
            capability["gaps"].append(
                "visibility_missing_in_rust: Python 有非空 visibility，Rust 全空"
            )

    return capability


# ============================================
# Bundle 字节占比
# ============================================

def _capture_bundle_baseline(
    bundle: Path | None,
    pyz_toc: Path | None,
) -> dict:
    """若提供了 bundle 目录，调用 inspector 生成 distribution 报告。"""
    if bundle is None or not bundle.is_dir():
        return {
            "bundle_path": str(bundle) if bundle else None,
            "unpacked_bytes": None,
            "unpacked_mb": None,
            "distributions": None,
            "methodology": (
                "无可用 PyInstaller 构建产物。基线获取方法：在目标平台运行 "
                "`pyinstaller release/pyinstaller/callwarden.spec --noconfirm --clean`，"
                "然后运行 `py -3.14 release/inspect_pyinstaller_bundle.py "
                "--bundle dist/callwarden --pyz-toc dist/callwarden/_internal/PYZ-00.toc "
                "--report tests/parser_contract/bundle_snapshot.json`，"
                "并将 bundle_snapshot.json 的 distributions 字段合并到 baseline.json。"
                "CI 应将 dist/callwarden/ 作为 artifact 持久化，再由 baseline 生成步骤读取。"
            ),
        }

    # 延迟导入，避免无 bundle 时强依赖
    sys.path.insert(0, str(_PKG_ROOT))
    from release.inspect_pyinstaller_bundle import (  # type: ignore
        compute_distribution_breakdown,
    )

    breakdown = compute_distribution_breakdown(bundle)
    return {
        "bundle_path": str(bundle.resolve()),
        "unpacked_bytes": breakdown["total_bytes"],
        "unpacked_mb": round(breakdown["total_bytes"] / (1024 * 1024), 2),
        "distributions": breakdown["distributions"],
        "methodology": (
            "由 release/inspect_pyinstaller_bundle.py compute_distribution_breakdown 生成。"
            "包含 16 种 grammar distribution 名（即使文件数为 0 也列出）。"
            "CI 应在生成此 baseline 后保留对应 dist/callwarden/ artifact，"
            "供切换后 before/after 差异比较。"
        ),
    }


def _git_head_sha() -> str:
    """获取当前 git HEAD commit sha。"""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(_PKG_ROOT),
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except Exception:
        pass
    return "unknown"


# ============================================
# 主流程
# ============================================

def generate_baseline(
    bundle: Path | None = None,
    pyz_toc: Path | None = None,
) -> dict:
    """生成完整 baseline 字典。"""
    rust_supported = _rust_supported_languages()

    language_capability: dict[str, Any] = {}
    for lang, filename, content in _ALL_LANGUAGE_SAMPLES:
        language_capability[lang] = _probe_language(
            lang, filename, content, rust_supported
        )

    bundle_baseline = _capture_bundle_baseline(bundle, pyz_toc)

    return {
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "commit_sha": _git_head_sha(),
        "platform": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "machine": platform.machine(),
            "rust_extension_available": _has_rust_ext(),
            "rust_supported_languages": sorted(rust_supported),
        },
        "language_capability": language_capability,
        "bundle_size_baseline": bundle_baseline,
        # Phase 0 完成门清单（供后续步骤验证）
        "phase0_completion_gates": {
            "tests_expose_typescript_gap": "typescript" in language_capability
            and any("typescript" in g for g in language_capability["typescript"]["gaps"]),
            "tests_expose_php_gap": "php" in language_capability
            and any("php" in g for g in language_capability["php"]["gaps"]),
            "tests_expose_scala_gap": "scala" in language_capability
            and any("scala" in g for g in language_capability["scala"]["gaps"]),
            "tests_expose_hcl_gap": "hcl" in language_capability
            and any("hcl" in g for g in language_capability["hcl"]["gaps"]),
            "baseline_persisted_as_ci_artifact": bundle_baseline.get("unpacked_bytes") is not None,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "P0-A 基线生成：捕获 16 语言 ParseFact 能力 + bundle distribution 字节占比。"
        ),
    )
    parser.add_argument(
        "--bundle",
        type=Path,
        default=None,
        help="PyInstaller --onedir 产物根目录（可选；缺失时只生成语言能力清单）。",
    )
    parser.add_argument(
        "--pyz-toc",
        type=Path,
        default=None,
        help="PYZ-00.toc 文件路径（与 --bundle 同时提供时计入模块清单）。",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=_PKG_ROOT / "tests" / "parser_contract" / "baseline.json",
        help="输出 JSON 路径。",
    )
    args = parser.parse_args()

    baseline = generate_baseline(bundle=args.bundle, pyz_toc=args.pyz_toc)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        json.dumps(baseline, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"baseline 写入: {args.output}")
    # 控制台摘要
    rust_langs = baseline["platform"]["rust_supported_languages"]
    print(f"Rust 支持语言数: {len(rust_langs)} ({', '.join(rust_langs)})")
    gates = baseline["phase0_completion_gates"]
    print("Phase 0 完成门:")
    for name, passed in gates.items():
        mark = "✓" if passed else "✗"
        print(f"  {mark} {name}: {passed}")
    bundle_size = baseline["bundle_size_baseline"]
    if bundle_size.get("unpacked_bytes") is not None:
        print(
            f"Bundle: {bundle_size['unpacked_mb']} MiB / "
            f"{bundle_size.get('distributions', {}) and 'distributions 已记录' or 'distributions 缺失'}"
        )
    else:
        print("Bundle: 未提供 --bundle，仅记录语言能力基线（methodology 已写入）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
