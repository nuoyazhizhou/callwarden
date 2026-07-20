"""setup.py —— P0-3 修复：让 wheel 包含预编译的 Rust 扩展二进制。

评审 P0-3：跨平台安装包目前不能算完成
- wheel 不含 callwarden_core（py3-none-any），安装后无 Rust 加速

修复方案：
1. BinaryDistribution.has_ext_modules() 返回 True，让 bdist_wheel 把 wheel
   标记为平台特定（cp311-cp311-{plat_tag}），而不是默认的 py3-none-any
2. pyproject.toml 中 ext-modules = [{ name = "callwarden_core", sources = [] }]
   声明 callwarden_core 为扩展模块，让 setuptools 把根目录的 .pyd/.so 打入 wheel
3. PrebuiltBuildExt 跳过编译步骤（Rust 扩展由 cargo build 单独构建）

注意：本文件只覆盖 distclass 和 build_ext，所有其他元数据仍由 pyproject.toml 提供。
不在 setup.py 中重复 name/version/description，避免双真相源。
"""

import os
import shutil
import sys
from pathlib import Path

from setuptools import Distribution, Extension, setup
from setuptools.command.build_ext import build_ext


class BinaryDistribution(Distribution):
    """让 setuptools 知道这是 binary distribution。

    默认 Distribution 把 wheel 标记为 py3-none-any，导致：
    1. wheel 不含 callwarden_core.pyd/.so（setuptools 认为是纯 Python）
    2. 平台标记错误，pip 在不兼容平台误装

    BinaryDistribution 通过 has_ext_modules() 返回 True，让 bdist_wheel 把
    wheel 标记为平台特定（如 cp311-cp311-win_amd64）。
    """

    def has_ext_modules(self):
        return True


class PrebuiltBuildExt(build_ext):
    """跳过编译步骤，使用预编译的 .pyd/.so。

    Rust 扩展由 release/build.py 调用 cargo build 单独构建，
    产物已复制到项目根目录（callwarden_core.pyd 或 callwarden_core.so）。
    本类的职责只是让 setuptools 知道有 ext_modules，把 .pyd 打入 wheel。
    """

    def run(self):
        # 不调用 super().run()，跳过编译
        # 把预编译的 .pyd/.so 复制到 build_ext 输出目录
        for ext in self.extensions:
            self._copy_prebuilt(ext.name)

    def _copy_prebuilt(self, ext_name):
        """复制预编译的二进制到 build_ext 输出目录。"""
        if sys.platform == "win32":
            binary_name = f"{ext_name}.pyd"
        elif sys.platform == "darwin":
            binary_name = f"{ext_name}.so"
        else:
            binary_name = f"{ext_name}.so"

        # 项目根目录
        root = Path(__file__).resolve().parent
        src_path = root / binary_name

        if not src_path.exists():
            raise FileNotFoundError(
                f"预编译二进制不存在: {src_path}\n"
                f"请先运行 'python release/build.py --rust' 构建 Rust 扩展"
            )

        # build_ext 输出目录（build/lib...）
        build_lib = Path(self.build_lib)
        build_lib.mkdir(parents=True, exist_ok=True)
        dest_path = build_lib / binary_name

        shutil.copy2(str(src_path), str(dest_path))
        print(f"  [P0-3] Copied prebuilt {binary_name} to {dest_path}")


setup(
    distclass=BinaryDistribution,
    cmdclass={"build_ext": PrebuiltBuildExt},
    ext_modules=[Extension("callwarden_core", sources=[])],
)
