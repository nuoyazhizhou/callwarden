"""
Phase 6.1: Rust toolchain 模块测试

验证 PyO3 导出的 detect_compiler_type_py 和 compute_toolchain_fingerprint_py
在 Python 侧的正确性，以及与 Python 实现的一致性。
"""

import sys
import math
from pathlib import Path

import pytest

# 加载 callwarden_core（从 pyinstall 目录）
_pyinstall = Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall"
if _pyinstall.exists():
    sys.path.insert(0, str(_pyinstall))

# 尝试导入 Rust 扩展
try:
    from callwarden_core import (
        detect_compiler_type_py,
        compute_toolchain_fingerprint_py,
    )
    RUST_AVAILABLE = True
except ImportError:
    RUST_AVAILABLE = False

# 同时加载 Python 实现做一致性对比
import importlib.util


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_tc_mod = _load_module(
    "db_toolchain",
    str(Path(__file__).parent.parent / "db" / "db_toolchain.py"),
)
py_compute_fingerprint = _tc_mod.compute_toolchain_fingerprint
py_detect_type = _tc_mod._detect_compiler_type

pytestmark = pytest.mark.skipif(
    not RUST_AVAILABLE,
    reason="callwarden_core Rust 扩展未构建或不在 PYTHONPATH",
)


# ============================================
# TestImport —— 导入验证
# ============================================

class TestImport:
    """Rust 模块导入测试"""

    def test_import_callable(self):
        """PyO3 函数可导入且可调用"""
        assert callable(detect_compiler_type_py)
        assert callable(compute_toolchain_fingerprint_py)


# ============================================
# TestDetectCompilerType —— 编译器类型探测
# ============================================

class TestDetectCompilerType:
    """detect_compiler_type_py 测试"""

    def test_gcc(self):
        assert detect_compiler_type_py("/usr/bin/gcc") == "gcc"

    def test_gpp(self):
        assert detect_compiler_type_py("/usr/bin/g++") == "g++"

    def test_clang(self):
        assert detect_compiler_type_py("/usr/bin/clang") == "clang"

    def test_arm_none_eabi_gcc(self):
        """交叉编译器应优先匹配"""
        assert detect_compiler_type_py("/usr/bin/arm-none-eabi-gcc") == "arm-none-eabi-gcc"

    def test_aarch64_gcc(self):
        assert detect_compiler_type_py("/usr/bin/aarch64-linux-gnu-gcc") == "aarch64-linux-gnu-gcc"

    def test_unknown(self):
        """未知编译器返回 basename"""
        assert detect_compiler_type_py("/usr/bin/mycc") == "mycc"

    def test_windows_path(self):
        """Windows 路径"""
        assert detect_compiler_type_py("C:\\mingw\\bin\\gcc.exe") == "gcc"

    def test_consistency_with_python(self):
        """与 Python 实现一致"""
        paths = [
            "/usr/bin/gcc",
            "/usr/bin/g++",
            "/usr/bin/clang",
            "/usr/bin/arm-none-eabi-gcc",
            "/usr/bin/aarch64-linux-gnu-gcc",
            "/opt/foo/mycc",
            "C:\\mingw\\bin\\gcc.exe",
        ]
        for p in paths:
            assert detect_compiler_type_py(p) == py_detect_type(p), f"mismatch for {p}"


# ============================================
# TestFingerprint —— 指纹计算
# ============================================

class TestFingerprint:
    """compute_toolchain_fingerprint_py 测试"""

    def test_same_fields_same_fingerprint(self):
        """相同字段 → 相同指纹"""
        fp1 = compute_toolchain_fingerprint_py(
            "gcc", "gcc", "10.0", "x86_64-linux", "",
            ["/usr/include"], {"__GNUC__": "10"},
        )
        fp2 = compute_toolchain_fingerprint_py(
            "gcc", "gcc", "10.0", "x86_64-linux", "",
            ["/usr/include"], {"__GNUC__": "10"},
        )
        assert fp1 == fp2

    def test_different_path_different_fingerprint(self):
        fp1 = compute_toolchain_fingerprint_py(
            "/usr/bin/gcc", "gcc", "10.0", "x86_64-linux", "", [], {},
        )
        fp2 = compute_toolchain_fingerprint_py(
            "/opt/gcc/bin/gcc", "gcc", "10.0", "x86_64-linux", "", [], {},
        )
        assert fp1 != fp2

    def test_different_version_different_fingerprint(self):
        fp1 = compute_toolchain_fingerprint_py(
            "gcc", "gcc", "10.0", "x86_64-linux", "", [], {},
        )
        fp2 = compute_toolchain_fingerprint_py(
            "gcc", "gcc", "11.0", "x86_64-linux", "", [], {},
        )
        assert fp1 != fp2

    def test_different_target_different_fingerprint(self):
        fp1 = compute_toolchain_fingerprint_py(
            "gcc", "gcc", "10.0", "x86_64-linux", "", [], {},
        )
        fp2 = compute_toolchain_fingerprint_py(
            "gcc", "gcc", "10.0", "aarch64-linux", "", [], {},
        )
        assert fp1 != fp2

    def test_different_sysroot_different_fingerprint(self):
        fp1 = compute_toolchain_fingerprint_py(
            "gcc", "gcc", "10.0", "x86_64-linux", "/sysroot1", [], {},
        )
        fp2 = compute_toolchain_fingerprint_py(
            "gcc", "gcc", "10.0", "x86_64-linux", "/sysroot2", [], {},
        )
        assert fp1 != fp2

    def test_include_dirs_order_independent(self):
        """include_dirs 顺序无关"""
        fp1 = compute_toolchain_fingerprint_py(
            "gcc", "gcc", "10.0", "x86_64-linux", "",
            ["/usr/include", "/usr/local/include"], {},
        )
        fp2 = compute_toolchain_fingerprint_py(
            "gcc", "gcc", "10.0", "x86_64-linux", "",
            ["/usr/local/include", "/usr/include"], {},
        )
        assert fp1 == fp2

    def test_predefined_macros_order_independent(self):
        """predefined_macros 顺序无关"""
        fp1 = compute_toolchain_fingerprint_py(
            "gcc", "gcc", "10.0", "x86_64-linux", "",
            [], {"A": "1", "B": "2"},
        )
        fp2 = compute_toolchain_fingerprint_py(
            "gcc", "gcc", "10.0", "x86_64-linux", "",
            [], {"B": "2", "A": "1"},
        )
        assert fp1 == fp2

    def test_empty_include_dirs_and_macros(self):
        """空 include_dirs 和 macros"""
        fp = compute_toolchain_fingerprint_py(
            "gcc", "gcc", "10.0", "x86_64-linux", "", [], {},
        )
        assert isinstance(fp, str)
        assert len(fp) == 64  # SHA-256 hex

    def test_fingerprint_is_hex(self):
        """指纹是 64 字符的 hex 字符串"""
        fp = compute_toolchain_fingerprint_py(
            "gcc", "gcc", "10.0", "x86_64-linux", "",
            ["/usr/include"], {"__GNUC__": "10"},
        )
        assert len(fp) == 64
        int(fp, 16)  # 不报错说明是合法 hex


# ============================================
# TestConsistency —— Rust 与 Python 实现一致性
# ============================================

class TestConsistency:
    """Rust 与 Python fingerprint 实现一致性"""

    def test_consistency_basic(self):
        """基础场景一致"""
        args = ("gcc", "gcc", "10.0", "x86_64-linux", "", [], {})
        rust_fp = compute_toolchain_fingerprint_py(*args)
        py_fp = py_compute_fingerprint(*args)
        assert rust_fp == py_fp, f"basic: rust={rust_fp}, py={py_fp}"

    def test_consistency_with_include_dirs(self):
        """带 include_dirs 一致"""
        args = (
            "/usr/bin/gcc", "gcc", "10.0", "x86_64-linux", "",
            ["/usr/include", "/usr/local/include"], {},
        )
        rust_fp = compute_toolchain_fingerprint_py(*args)
        py_fp = py_compute_fingerprint(*args)
        assert rust_fp == py_fp

    def test_consistency_with_macros(self):
        """带 predefined_macros 一致"""
        args = (
            "gcc", "gcc", "10.0", "x86_64-linux", "",
            [], {"__GNUC__": "10", "__STDC__": "1"},
        )
        rust_fp = compute_toolchain_fingerprint_py(*args)
        py_fp = py_compute_fingerprint(*args)
        assert rust_fp == py_fp

    def test_consistency_full(self):
        """完整字段一致"""
        args = (
            "/usr/bin/arm-none-eabi-gcc", "arm-none-eabi-gcc", "10.3.1",
            "arm-none-eabi", "/opt/arm/sysroot",
            ["/opt/arm/include", "/usr/include"],
            {"__ARM_ARCH": "7", "__GNUC__": "10"},
        )
        rust_fp = compute_toolchain_fingerprint_py(*args)
        py_fp = py_compute_fingerprint(*args)
        assert rust_fp == py_fp

    def test_consistency_order_independent(self):
        """顺序无关场景一致"""
        base = ("gcc", "gcc", "10.0", "x86_64-linux", "")
        rust1 = compute_toolchain_fingerprint_py(
            *base, ["/a", "/b"], {"X": "1"}
        )
        rust2 = compute_toolchain_fingerprint_py(
            *base, ["/b", "/a"], {"X": "1"}
        )
        py1 = py_compute_fingerprint(*base, ["/a", "/b"], {"X": "1"})
        py2 = py_compute_fingerprint(*base, ["/b", "/a"], {"X": "1"})
        # Rust 和 Python 各自顺序无关
        assert rust1 == rust2
        assert py1 == py2
        # Rust 与 Python 一致
        assert rust1 == py1
