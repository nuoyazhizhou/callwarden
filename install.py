"""
install.py
==========

Call Warden 一键安装脚本：级联安装核心依赖 + 各语言 tree-sitter grammar + 可选依赖。

使用方式：
    cw install              # 安装核心 + 全部已支持语言
    cw install --all        # 安装核心 + 全部语言 + 全部可选依赖
    cw install --lang csharp ruby  # 仅安装指定语言的 grammar
    cw install --check      # 仅检查依赖状态，不安装
    cw install --no-optional  # 跳过可选依赖（默认行为）
    cw install --verbose    # 显示详细安装日志

设计原则：
1. 级联安装：核心 → 已支持语言 → 扩展语言 → 可选依赖
2. 失败不中断：单个包安装失败只警告，继续安装其他包
3. 状态可见：每个包安装前后打印状态（已有/安装中/成功/失败）
4. 幂等：重复运行不会出错，已安装的包会跳过

退出码：
    0 = 全部成功
    1 = 部分失败（查看输出）
    2 = 网络或 pip 不可用
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set


# ---------------------------------------------------------------------
# 依赖定义
# ---------------------------------------------------------------------

@dataclass
class PackageSpec:
    """单个 pip 包的安装规格"""
    pip_name: str           # pip install 用的包名
    import_name: str        # Python import 检查用的模块名
    category: str           # 分类：core / language / optional
    language: str = ""      # 语言名（仅 language 类别）
    description: str = ""   # 人类可读说明


# 核心依赖（必需）
CORE_PACKAGES: List[PackageSpec] = [
    PackageSpec("tree-sitter", "tree_sitter", "core", description="AST 解析引擎"),
    PackageSpec("tree-sitter-languages", "tree_sitter_languages", "core", description="多语言 grammar 预编译包（备份方案）"),
    PackageSpec("fastmcp", "fastmcp", "core", description="MCP Server 框架"),
]

# 已支持语言（9 种，与 Semgrep 交集）
SUPPORTED_LANGUAGE_PACKAGES: List[PackageSpec] = [
    PackageSpec("tree-sitter-rust", "tree_sitter_rust", "language", "rust", "Rust grammar"),
    PackageSpec("tree-sitter-typescript", "tree_sitter_typescript", "language", "typescript", "TypeScript/TSX grammar"),
    PackageSpec("tree-sitter-python", "tree_sitter_python", "language", "python", "Python grammar"),
    PackageSpec("tree-sitter-kotlin", "tree_sitter_kotlin", "language", "kotlin", "Kotlin grammar"),
    PackageSpec("tree-sitter-go", "tree_sitter_go", "language", "go", "Go grammar"),
    PackageSpec("tree-sitter-java", "tree_sitter_java", "language", "java", "Java grammar"),
    PackageSpec("tree-sitter-c", "tree_sitter_c", "language", "c", "C grammar"),
    PackageSpec("tree-sitter-cpp", "tree_sitter_cpp", "language", "cpp", "C++ grammar"),
    PackageSpec("tree-sitter-javascript", "tree_sitter_javascript", "language", "javascript", "JavaScript/JSX grammar"),
]

# P0 扩展语言（Semgrep 独有，新增支持）
EXTENDED_LANGUAGE_PACKAGES: List[PackageSpec] = [
    PackageSpec("tree-sitter-c-sharp", "tree_sitter_c_sharp", "language", "csharp", "C# grammar（Semgrep 170+ Pro 规则）"),
    PackageSpec("tree-sitter-ruby", "tree_sitter_ruby", "language", "ruby", "Ruby grammar（Semgrep 40+ Pro 规则）"),
]

# P1 扩展语言（Web 与 iOS 生态）
P1_LANGUAGE_PACKAGES: List[PackageSpec] = [
    PackageSpec("tree-sitter-php", "tree_sitter_php", "language", "php", "PHP grammar（Semgrep 50+ Pro 规则，Web 安全场景）"),
    PackageSpec("tree-sitter-swift", "tree_sitter_swift", "language", "swift", "Swift grammar（iOS 生态，Semgrep 60+ Pro 规则）"),
]

# P2 扩展语言（JVM 与 IaC 生态）
P2_LANGUAGE_PACKAGES: List[PackageSpec] = [
    PackageSpec("tree-sitter-scala", "tree_sitter_scala", "language", "scala", "Scala grammar（JVM 生态，社区规则）"),
    PackageSpec("tree-sitter-hcl", "tree_sitter_hcl", "language", "hcl", "Terraform/HCL grammar（IaC 场景）"),
]

# P3 扩展语言（Semgrep Beta）
P3_LANGUAGE_PACKAGES: List[PackageSpec] = [
    PackageSpec("tree-sitter-elixir", "tree_sitter_elixir", "language", "elixir", "Elixir grammar（Semgrep 仅 Beta）"),
]

# 可选依赖（按需启用）
OPTIONAL_PACKAGES: List[PackageSpec] = [
    PackageSpec("semgrep", "semgrep", "optional", description="多语言静态安全扫描（守护者架构必需）"),
    PackageSpec("sentence-transformers", "sentence_transformers", "optional", description="向量嵌入（语义搜索）"),
    PackageSpec("sqlite-vec", "sqlite_vec", "optional", description="向量索引扩展"),
    PackageSpec("numpy", "numpy", "optional", description="向量计算（PyO3 加速时需要）"),
]


# ---------------------------------------------------------------------
# 安装器实现
# ---------------------------------------------------------------------

@dataclass
class InstallResult:
    """安装结果汇总"""
    total: int = 0
    installed: int = 0          # 本次新安装数
    skipped: int = 0            # 已存在跳过数
    failed: int = 0             # 失败数
    failed_packages: List[str] = field(default_factory=list)


class CodeGraphInstaller:
    """Code Graph 级联安装器"""

    def __init__(self, verbose: bool = False):
        self.verbose = verbose
        self.result = InstallResult()

    # ------------------------------------------------------------------
    # 公共接口
    # ------------------------------------------------------------------

    def install_all(self, include_optional: bool = False,
                    languages_only: Optional[Set[str]] = None) -> InstallResult:
        """级联安装：核心 → 已支持语言 → 扩展语言 → 可选依赖

        Args:
            include_optional: 是否安装可选依赖
            languages_only: 若指定，只安装这些语言的 grammar（不装核心包）
        """
        print("=" * 60)
        print("Code Graph 一键安装")
        print("=" * 60)
        print()

        # 检查 pip 可用性
        if not self._check_pip():
            print("[FATAL] pip 不可用，无法继续安装")
            sys.exit(2)

        if languages_only:
            # 仅安装指定语言的 grammar
            self._install_languages_by_filter(languages_only)
        else:
            # 完整级联安装
            print("--- 第 1 步：安装核心依赖 ---")
            self._install_group(CORE_PACKAGES)
            print()

            print("--- 第 2 步：安装已支持语言 grammar（9 种） ---")
            self._install_group(SUPPORTED_LANGUAGE_PACKAGES)
            print()

            print("--- 第 3 步：安装 P0 扩展语言 grammar（C# / Ruby） ---")
            self._install_group(EXTENDED_LANGUAGE_PACKAGES)
            print()

            print("--- 第 4 步：安装 P1 扩展语言 grammar（PHP / Swift） ---")
            self._install_group(P1_LANGUAGE_PACKAGES)
            print()

            print("--- 第 5 步：安装 P2 扩展语言 grammar（Scala / HCL） ---")
            self._install_group(P2_LANGUAGE_PACKAGES)
            print()

            print("--- 第 6 步：安装 P3 扩展语言 grammar（Elixir） ---")
            self._install_group(P3_LANGUAGE_PACKAGES)
            print()

            if include_optional:
                print("--- 第 7 步：安装可选依赖 ---")
                self._install_group(OPTIONAL_PACKAGES)
                print()

        # 打印汇总
        self._print_summary()
        return self.result

    def check_status(self) -> None:
        """仅检查依赖状态，不安装"""
        print("=" * 60)
        print("Code Graph 依赖状态检查")
        print("=" * 60)
        print()

        print("--- 核心依赖 ---")
        self._check_group(CORE_PACKAGES)
        print()

        print("--- 已支持语言 grammar（9 种） ---")
        self._check_group(SUPPORTED_LANGUAGE_PACKAGES)
        print()

        print("--- P0 扩展语言 grammar（C# / Ruby） ---")
        self._check_group(EXTENDED_LANGUAGE_PACKAGES)
        print()

        print("--- P1 扩展语言 grammar（PHP / Swift） ---")
        self._check_group(P1_LANGUAGE_PACKAGES)
        print()

        print("--- P2 扩展语言 grammar（Scala / HCL） ---")
        self._check_group(P2_LANGUAGE_PACKAGES)
        print()

        print("--- P3 扩展语言 grammar（Elixir） ---")
        self._check_group(P3_LANGUAGE_PACKAGES)
        print()

        print("--- 可选依赖 ---")
        self._check_group(OPTIONAL_PACKAGES)
        print()

        print("提示：运行 `cw install` 安装缺失的依赖")

    # ------------------------------------------------------------------
    # 内部方法
    # ------------------------------------------------------------------

    def _check_pip(self) -> bool:
        """检查 pip 是否可用"""
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "--version"],
                capture_output=True, text=True, timeout=10, shell=False,
            )
            return result.returncode == 0
        except Exception:
            return False

    def _is_package_installed(self, spec: PackageSpec) -> bool:
        """检查包是否已安装（通过 import 测试）"""
        try:
            importlib.import_module(spec.import_name)
            return True
        except ImportError:
            return False

    def _install_group(self, packages: List[PackageSpec]) -> None:
        """安装一组包"""
        for spec in packages:
            self.result.total += 1
            self._install_one(spec)

    def _install_one(self, spec: PackageSpec) -> None:
        """安装单个包"""
        # 检查是否已安装
        if self._is_package_installed(spec):
            self.result.skipped += 1
            print(f"  [已安装] {spec.pip_name:<30} {spec.description}")
            return

        # 执行安装
        print(f"  [安装中] {spec.pip_name:<30} {spec.description}")
        cmd = [sys.executable, "-m", "pip", "install", spec.pip_name]
        if not self.verbose:
            cmd.append("--quiet")

        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=300, shell=False,
            )
            if result.returncode == 0:
                self.result.installed += 1
                print(f"  [成功]   {spec.pip_name}")
            else:
                self.result.failed += 1
                self.result.failed_packages.append(spec.pip_name)
                err_msg = result.stderr.strip().split("\n")[-1] if result.stderr else "未知错误"
                print(f"  [失败]   {spec.pip_name}: {err_msg}")
        except subprocess.TimeoutExpired:
            self.result.failed += 1
            self.result.failed_packages.append(spec.pip_name)
            print(f"  [超时]   {spec.pip_name}: 安装超过 5 分钟")
        except Exception as e:
            self.result.failed += 1
            self.result.failed_packages.append(spec.pip_name)
            print(f"  [异常]   {spec.pip_name}: {type(e).__name__}")

    def _install_languages_by_filter(self, languages: Set[str]) -> None:
        """按语言过滤安装 grammar"""
        all_langs = (SUPPORTED_LANGUAGE_PACKAGES + EXTENDED_LANGUAGE_PACKAGES
                     + P1_LANGUAGE_PACKAGES + P2_LANGUAGE_PACKAGES + P3_LANGUAGE_PACKAGES)
        filtered = [p for p in all_langs if p.language in languages]
        if not filtered:
            print(f"[警告] 未找到匹配的语言: {languages}")
            print(f"  支持的语言: {', '.join(p.language for p in all_langs)}")
            return
        print(f"--- 安装指定语言 grammar: {', '.join(languages)} ---")
        self._install_group(filtered)

    def _check_group(self, packages: List[PackageSpec]) -> None:
        """检查一组包的安装状态"""
        for spec in packages:
            installed = self._is_package_installed(spec)
            status = "[OK]  " if installed else "[MISS]"
            lang_tag = f" ({spec.language})" if spec.language else ""
            print(f"  {status} {spec.pip_name:<30} {spec.description}{lang_tag}")

    def _print_summary(self) -> None:
        """打印安装汇总"""
        print("=" * 60)
        print("安装汇总")
        print("=" * 60)
        print(f"  总计:   {self.result.total}")
        print(f"  新安装: {self.result.installed}")
        print(f"  已跳过: {self.result.skipped}（已存在）")
        print(f"  失败:   {self.result.failed}")
        if self.result.failed_packages:
            print(f"  失败包: {', '.join(self.result.failed_packages)}")
        print()

        if self.result.failed == 0:
            print("✓ 全部依赖安装成功")
            print()
            print("下一步：")
            print("  1. 验证安装: cw --help")
            print("  2. 构建图谱: cd /path/to/project && cw --init")
            print("  3. 启动 MCP: cw server")
        else:
            print(f"⚠ 部分依赖安装失败（{self.result.failed} 个）")
            print("  失败的包可稍后手动安装: pip install <package-name>")
            print("  或重新运行: cw install")
        print()


# ---------------------------------------------------------------------
# CLI 入口
# ---------------------------------------------------------------------

def main():
    """CLI 入口：解析参数并执行安装"""
    import argparse

    parser = argparse.ArgumentParser(
        description="Call Warden 一键安装脚本",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
  cw install              # 安装核心 + 全部已支持语言
  cw install --all        # 安装核心 + 全部语言 + 全部可选依赖
  cw install --lang csharp ruby  # 仅安装指定语言的 grammar
  cw install --check      # 仅检查依赖状态，不安装
        """,
    )
    parser.add_argument("--all", action="store_true",
                        help="安装全部依赖（含可选依赖）")
    parser.add_argument("--lang", nargs="+", metavar="LANG",
                        help="仅安装指定语言的 grammar（如 csharp ruby）")
    parser.add_argument("--check", action="store_true",
                        help="仅检查依赖状态，不安装")
    parser.add_argument("--no-optional", action="store_true",
                        help="跳过可选依赖（默认行为，显式声明）")
    parser.add_argument("--verbose", action="store_true",
                        help="显示详细安装日志")

    args = parser.parse_args()

    installer = CodeGraphInstaller(verbose=args.verbose)

    if args.check:
        installer.check_status()
        return

    if args.lang:
        # 仅安装指定语言
        installer.install_all(languages_only=set(args.lang))
    else:
        # 完整安装
        include_optional = args.all and not args.no_optional
        installer.install_all(include_optional=include_optional)

    # 退出码
    sys.exit(0 if installer.result.failed == 0 else 1)


if __name__ == "__main__":
    main()
