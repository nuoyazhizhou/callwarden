"""
db_external.py
==============

第三方包解析模块（多语言通用版）：管理项目依赖的第三方包符号信息，
支持从已安装包中提取函数、类、常量等符号，用于跨文件调用解析。

支持的语言与包管理器：
- Python: pip（requirements.txt / pyproject.toml / setup.py）
- Rust: cargo（Cargo.toml）
- Java/Scala: maven（pom.xml）/ gradle（build.gradle）
- Go: go modules（go.mod）
- JavaScript/TypeScript: npm（package.json）
- Ruby: gem（Gemfile / *.gemspec）
- PHP: composer（composer.json）
- Swift: SwiftPM（Package.swift）
- Kotlin: gradle（build.gradle.kts）
- Elixir: mix（mix.exs）

设计思路：
1. 各语言独立解析依赖清单文件
2. 符号统一存储到 external_symbols 表（与标准库共用）
3. 支持增量更新和版本记录
4. Python 走 importlib 动态导入；其他语言走源码解析（已安装包目录）
"""

from __future__ import annotations

import importlib
import inspect
import json
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional, Tuple

from ..config import norm_path, read_file_text
from ..i18n import t
from .rust_parser_facade import RustParserFacade

MAX_EXTERNAL_SYMBOLS_PER_PACKAGE = 300
MAX_EXTERNAL_SOURCE_FILES_PER_PACKAGE = 20
MAX_EXTERNAL_MODULE_DEPTH = 1


# 各语言包管理器配置
# 字段说明：
#   manifest_files: 依赖清单文件名列表（按优先级）
#   lock_files: 锁文件列表（用于确定实际安装版本）
#   install_dirs: 已安装包存放目录（相对项目根）
#   ext: 源码文件扩展名（用于扫描包内符号）
LANG_PACKAGE_MANAGERS: Dict[str, Dict[str, Any]] = {
    "python": {
        "manifest_files": ["requirements.txt", "pyproject.toml", "setup.py"],
        "lock_files": ["requirements.lock", "Pipfile.lock", "poetry.lock"],
        "install_dirs": [],  # Python 用 importlib 动态导入
        "ext": ".py",
    },
    "rust": {
        "manifest_files": ["Cargo.toml"],
        "lock_files": ["Cargo.lock"],
        "install_dirs": [],  # cargo registry 在 ~/.cargo/registry，体积过大不扫描
        "ext": ".rs",
    },
    "java": {
        "manifest_files": ["pom.xml", "build.gradle"],
        "lock_files": ["pom.xml.tag", "build.gradle.lockfile"],
        "install_dirs": [],  # maven ~/.m2/repository，体积过大不扫描
        "ext": ".java",
    },
    "scala": {
        "manifest_files": ["build.sbt", "pom.xml", "build.gradle"],
        "lock_files": ["build.sbt.lock"],
        "install_dirs": [],
        "ext": ".scala",
    },
    "go": {
        "manifest_files": ["go.mod"],
        "lock_files": ["go.sum"],
        "install_dirs": ["vendor"],  # vendor 目录可被解析
        "ext": ".go",
    },
    "typescript": {
        "manifest_files": ["package.json"],
        "lock_files": ["package-lock.json", "yarn.lock", "pnpm-lock.yaml"],
        "install_dirs": ["node_modules"],
        "ext": ".ts",
    },
    "javascript": {
        "manifest_files": ["package.json"],
        "lock_files": ["package-lock.json", "yarn.lock", "pnpm-lock.yaml"],
        "install_dirs": ["node_modules"],
        "ext": ".js",
    },
    "ruby": {
        "manifest_files": ["Gemfile", "*.gemspec"],
        "lock_files": ["Gemfile.lock"],
        "install_dirs": [],  # gem install path 复杂，暂不扫描
        "ext": ".rb",
    },
    "php": {
        "manifest_files": ["composer.json"],
        "lock_files": ["composer.lock"],
        "install_dirs": ["vendor"],
        "ext": ".php",
    },
    "swift": {
        "manifest_files": ["Package.swift"],
        "lock_files": ["Package.resolved"],
        "install_dirs": [],  # SPM 在 .build/debug 里，结构复杂
        "ext": ".swift",
    },
    "kotlin": {
        "manifest_files": ["build.gradle.kts", "build.gradle"],
        "lock_files": ["gradle.lockfile"],
        "install_dirs": [],
        "ext": ".kt",
    },
    "csharp": {
        "manifest_files": ["*.csproj", "packages.config"],
        "lock_files": ["packages.lock.json"],
        "install_dirs": [],  # nuget 包在 ~/.nuget/packages，体积过大不扫描
        "ext": ".cs",
    },
    "elixir": {
        "manifest_files": ["mix.exs"],
        "lock_files": ["mix.lock"],
        "install_dirs": ["deps"],
        "ext": ".ex",
    },
}


class ExternalMixin:
    """第三方包符号解析 Mixin（多语言通用）

    通过 self.conn 访问数据库连接，提供文件解析和调用图构建功能。
    Python 包走 importlib 动态导入；其他语言走清单解析 + 可选源码扫描。
    """

    # ==================== 公共入口 ====================

    def import_external_packages(
        self,
        package_names: Optional[List[str]] = None,
        languages: Optional[List[str]] = None,
    ) -> int:
        """导入第三方包符号到数据库（多语言通用入口）

        Args:
            package_names: 指定要导入的包名列表，为空则导入所有项目依赖
            languages: 指定语言列表，为空则自动检测项目语言

        Returns:
            导入的符号数量
        """
        total_created = 0
        total_skipped = 0

        # 自动检测项目使用的语言
        if languages is None:
            languages = self._detect_project_languages()

        for lang in languages:
            try:
                created, skipped = self._import_external_packages_for_lang(
                    lang, package_names
                )
                total_created += created
                total_skipped += skipped
            except Exception as e:
                print(t(
                    "cli.messages.external_import_lang_failed",
                    lang=lang,
                    error=e,
                    default=f"  [{lang}] import failed: {e}",
                ))
                total_skipped += 1

        self.conn.commit()
        print(
            t(
                "cli.messages.external_import_done",
                created=total_created,
                skipped=total_skipped,
            )
        )
        return total_created

    def get_project_dependencies(
        self, languages: Optional[List[str]] = None
    ) -> Dict[str, Dict[str, str]]:
        """读取项目依赖列表（多语言）

        Args:
            languages: 指定语言列表，为空则自动检测

        Returns:
            {语言: {包名: 版本约束}} 嵌套字典
        """
        if languages is None:
            languages = self._detect_project_languages()

        result: Dict[str, Dict[str, str]] = {}
        for lang in languages:
            result[lang] = self._get_project_dependencies_for_lang(lang)
        return result

    def import_project_dependencies(self) -> int:
        """导入项目依赖的第三方包符号（构建流程入口）

        派发策略：
        - Python：走 importlib 动态导入（需包已 pip install）
        - Rust：扫描 ~/.cargo/registry/src/ 下的 crate 源码
        - TS/JS/PHP/Elixir/Go(vendor)：扫描 install_dirs 下的包源码
        - 其他语言：仅记录依赖关系到 package_versions

        Returns:
            导入的符号数量
        """
        languages = self._detect_project_languages()
        total = 0
        for lang in languages:
            deps = self._get_project_dependencies_for_lang(lang)
            if not deps:
                continue

            if lang == "python":
                # Python 走 importlib 动态导入（需包已 pip install）
                installed = self._get_installed_packages()
                pkg_names = [
                    name for name in deps.keys() if name.lower() in installed
                ]
                if pkg_names:
                    total += self.import_external_packages(
                        pkg_names, ["python"])
            else:
                # 非Python语言：派发到 _import_<lang>_package_symbols 或默认实现
                for pkg_name, version in deps.items():
                    try:
                        total += self._import_package_symbols_for_lang(
                            lang, pkg_name, version
                        )
                        self._touch_package_version(
                            f"ext-{lang}-{pkg_name}", version or "unknown", "last_seen_at", "manifest"
                        )
                    except Exception as e:
                        print(t(
                            "cli.messages.external_import_pkg_failed",
                            lang=lang,
                            package=pkg_name,
                            error=e,
                            default=f"  [{lang}] {pkg_name} symbol import failed: {e}",
                        ))

        self.conn.commit()
        return total

    def _touch_package_version(
        self,
        package_name: str,
        package_version: str,
        field: str = "last_seen_at",
        import_source: str = "external",
    ) -> None:
        """更新外部包冷热数据时间戳。"""
        if field not in ("last_seen_at", "last_used_at"):
            return
        now = time.time()
        pkg_ver = package_version or "unknown"
        self.conn.execute(
            """INSERT OR IGNORE INTO package_versions
               (package_name, package_version, installed_at, last_seen_at, last_used_at, import_source)
               VALUES (?, ?, ?, ?, 0, ?)""",
            (package_name, pkg_ver, now, now, import_source),
        )
        self.conn.execute(
            f"UPDATE package_versions SET {field} = ?, import_source = COALESCE(NULLIF(import_source, ''), ?) "
            "WHERE package_name = ? AND package_version = ?",
            (now, import_source, package_name, pkg_ver),
        )

    # ==================== 语言检测 ====================

    def _detect_project_languages(self) -> List[str]:
        """自动检测项目使用的语言（基于清单文件）

        Returns:
            语言标识列表（如 ['python', 'rust']）
        """
        detected: List[str] = []
        for lang, config in LANG_PACKAGE_MANAGERS.items():
            for manifest in config["manifest_files"]:
                if "*" in manifest:
                    # 通配符匹配（如 *.gemspec）
                    import glob

                    matches = glob.glob(
                        os.path.join(self.workspace_root, manifest)
                    )
                    if matches:
                        detected.append(lang)
                        break
                else:
                    if os.path.exists(
                        os.path.join(self.workspace_root, manifest)
                    ):
                        detected.append(lang)
                        break
        return detected

    def _get_installed_packages_for_lang(self, lang: str) -> Dict[str, str]:
        """获取指定语言的已安装包列表

        Args:
            lang: 语言标识

        Returns:
            {包名: 版本号} 字典
        """
        if lang == "python":
            return self._get_installed_packages()

        # 通用实现：从锁文件解析
        config = LANG_PACKAGE_MANAGERS.get(lang, {})
        for lock_file in config.get("lock_files", []):
            lock_path = os.path.join(self.workspace_root, lock_file)
            if os.path.exists(lock_path):
                parser = getattr(self, f"_parse_{lang}_lockfile", None)
                if parser:
                    return parser(lock_path)
        return {}

    def _get_project_dependencies_for_lang(self, lang: str) -> Dict[str, str]:
        """获取指定语言的项目依赖列表

        Args:
            lang: 语言标识

        Returns:
            {包名: 版本约束} 字典
        """
        if lang == "python":
            return self._get_python_project_dependencies()

        # 各语言独立解析
        parser = getattr(self, f"_parse_{lang}_manifest", None)
        if parser is None:
            return {}

        config = LANG_PACKAGE_MANAGERS.get(lang, {})
        for manifest in config.get("manifest_files", []):
            if "*" in manifest:
                import glob

                matches = glob.glob(os.path.join(
                    self.workspace_root, manifest))
                if matches:
                    return parser(matches[0])
            else:
                manifest_path = os.path.join(self.workspace_root, manifest)
                if os.path.exists(manifest_path):
                    return parser(manifest_path)
        return {}

    def _import_external_packages_for_lang(
        self, lang: str, package_names: Optional[List[str]] = None
    ) -> Tuple[int, int]:
        """导入指定语言的第三方包符号

        Args:
            lang: 语言标识
            package_names: 指定包名列表，为空则导入所有项目依赖

        Returns:
            (created, skipped) 元组
        """
        if lang == "python":
            packages = self._get_installed_packages()
            if package_names:
                packages = {
                    p: v
                    for p, v in packages.items()
                    if p.lower() in [n.lower() for n in package_names]
                }
            else:
                deps = self._get_project_dependencies_for_lang("python")
                wanted = {name.lower() for name in deps}
                packages = {
                    p: v
                    for p, v in packages.items()
                    if p.lower() in wanted
                }
            created = 0
            skipped = 0
            for pkg_name, pkg_version in packages.items():
                try:
                    created += self._import_python_package(
                        pkg_name, pkg_version)
                    self._touch_package_version(
                        pkg_name, pkg_version, "last_seen_at", "manifest"
                    )
                except Exception:
                    skipped += 1
            return (created, skipped)

        # 非Python：解析清单 + 可选扫描 install_dirs
        deps = self._get_project_dependencies_for_lang(lang)
        if package_names:
            deps = {
                p: v
                for p, v in deps.items()
                if p.lower() in [n.lower() for n in package_names]
            }

        created = 0
        skipped = 0
        for pkg_name, version_constraint in deps.items():
            try:
                created += self._import_package_symbols_for_lang(
                    lang, pkg_name, version_constraint
                )
            except Exception:
                skipped += 1
        return (created, skipped)

    def _import_package_symbols_for_lang(
        self, lang: str, package_name: str, version: str
    ) -> int:
        """导入指定语言的单个包符号（非Python语言）

        派发逻辑：
        1. 优先调用语言特定方法 `_import_<lang>_package_symbols`（若存在）
        2. 否则走默认实现：扫描 install_dirs 下的包源码

        Args:
            lang: 语言标识
            package_name: 包名
            version: 版本约束

        Returns:
            导入的符号数量
        """
        # 优先派发到语言特定处理器
        handler = getattr(self, f"_import_{lang}_package_symbols", None)
        if handler is not None:
            return handler(package_name, version)

        # 默认实现：记录到 package_versions + 扫描 install_dirs
        pkg_key = f"ext-{lang}-{package_name}"
        pkg_ver = version or "unknown"
        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM package_versions WHERE package_name = ? AND package_version = ?",
            (pkg_key, pkg_ver),
        )
        if cur.fetchone()["cnt"] > 0:
            return 0

        self.conn.execute(
            "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
            (pkg_key, pkg_ver),
        )

        # 尝试扫描 install_dirs 下的包源码
        config = LANG_PACKAGE_MANAGERS.get(lang, {})
        install_dirs = config.get("install_dirs", [])
        ext = config.get("ext", "")

        for install_dir in install_dirs:
            pkg_path = os.path.join(
                self.workspace_root, install_dir, package_name)
            if os.path.isdir(pkg_path):
                return self._scan_package_source_files(
                    lang,
                    package_name,
                    version,
                    pkg_path,
                    ext,
                    max_files=MAX_EXTERNAL_SOURCE_FILES_PER_PACKAGE,
                    max_depth=0,
                )

        return 0

    def _import_rust_package_symbols(
        self, package_name: str, version: str
    ) -> int:
        """Rust 包符号导入：从 cargo registry 扫描已下载的 crate 源码

        cargo registry 结构：
            ~/.cargo/registry/src/<index>-<hash>/<crate_name>-<version>/

        其中 `<index>-<hash>` 是 cargo 内部生成的索引目录名
        （如 index.crates.io-1949cf8c6b5b557f），需要遍历发现。

        Args:
            package_name: crate 名（如 "serde"）
            version: 版本（如 "1.0.0"），可为空字符串

        Returns:
            导入的符号数量
        """
        pkg_key = f"ext-rust-{package_name}"
        pkg_ver = version or "unknown"

        # 检查是否已导入（命中 package_versions 即跳过）
        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM package_versions WHERE package_name = ? AND package_version = ?",
            (pkg_key, pkg_ver),
        )
        if cur.fetchone()["cnt"] > 0:
            return 0

        # 定位 cargo registry 源码目录
        pkg_path = self._find_cargo_registry_package_path(
            package_name, version)
        if not pkg_path:
            # 未找到源码：仅记录依赖关系到 package_versions
            self.conn.execute(
                "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
                (pkg_key, pkg_ver),
            )
            return 0

        # 扫描 crate 源码提取符号（使用 Rust parser）
        created = self._scan_package_source_files(
            "rust",
            package_name,
            version,
            pkg_path,
            ".rs",
            max_files=MAX_EXTERNAL_SOURCE_FILES_PER_PACKAGE,
            max_depth=0,
        )

        # 记录到 package_versions（即使符号为 0 也要标记已完成）
        self.conn.execute(
            "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
            (pkg_key, pkg_ver),
        )
        return created

    def _find_cargo_registry_package_path(
        self, package_name: str, version: Optional[str] = None
    ) -> Optional[str]:
        """定位 cargo registry 中 crate 的源码目录

        查找路径：
            ~/.cargo/registry/src/<index_dir>/<crate_name>-<version>/

        其中 `<index_dir>` 形如 `index.crates.io-1949cf8c6b5b557f`，
        不同 registry 源（crates.io / 私有源）有不同的 hash。

        Args:
            package_name: crate 名
            version: 期望版本，为空则取最新可用版本

        Returns:
            源码目录绝对路径，未找到返回 None
        """
        cargo_home = self._get_cargo_home()
        registry_src = os.path.join(cargo_home, "registry", "src")
        if not os.path.isdir(registry_src):
            return None

        # 遍历所有 index 目录（crates.io / 私有源 / 镜像源）
        candidate_dirs: List[Tuple[str, str]] = []
        for index_dir in os.listdir(registry_src):
            index_path = os.path.join(registry_src, index_dir)
            if not os.path.isdir(index_path):
                continue
            # 遍历该 index 下的 crate 目录
            for entry in os.listdir(index_path):
                if not entry.startswith(f"{package_name}-"):
                    continue
                entry_path = os.path.join(index_path, entry)
                if not os.path.isdir(entry_path):
                    continue
                # 提取版本部分：entry = "<crate_name>-<version>"
                entry_version = entry[len(package_name) + 1:]
                # 过滤掉 .cache 等非版本目录
                if not entry_version or entry_version.startswith("."):
                    continue
                candidate_dirs.append((entry_version, entry_path))

        if not candidate_dirs:
            return None

        # 版本匹配：精确匹配 > 取字母序最大的（一般是最新版）
        if version:
            version_clean = version.lstrip("=^~<>").strip()
            for entry_version, entry_path in candidate_dirs:
                if entry_version == version_clean:
                    return entry_path
            # 版本约束（如 "^1.0"）模糊匹配前缀
            if version_clean:
                for entry_version, entry_path in candidate_dirs:
                    if entry_version.startswith(version_clean.split(".")[0]):
                        return entry_path

        # 默认取字母序最大的版本（通常是最新版）
        candidate_dirs.sort(key=lambda x: x[0], reverse=True)
        return candidate_dirs[0][1]

    def _scan_package_source_files(
        self,
        lang: str,
        package_name: str,
        version: str,
        pkg_path: str,
        ext: str,
        max_files: int = MAX_EXTERNAL_SOURCE_FILES_PER_PACKAGE,
        max_depth: int = 0,
    ) -> int:
        """扫描包目录下的源码文件，用对应语言解析器提取符号

        Args:
            lang: 语言标识
            package_name: 包名
            version: 版本
            pkg_path: 包安装目录
            ext: 源码扩展名
            max_files: 最多扫描文件数（避免巨型包拖慢构建）

        Returns:
            导入的符号数量
        """
        # P1-E: 改走 RustParserFacade（Rust-only），不再实例化 Python parser。
        # 设计 §3.1.5：Rust 解析失败 fail closed（跳过该文件，不静默回退 Python）。
        if not RustParserFacade.supports_language(lang):
            return 0

        created = 0
        file_count = 0
        pkg_key = f"ext-{lang}-{package_name}"
        pkg_ver = version or "unknown"

        root_depth = len(os.path.normpath(pkg_path).split(os.sep))
        for root, dirs, files in os.walk(pkg_path):
            depth = len(os.path.normpath(root).split(os.sep)) - root_depth
            if depth >= max_depth:
                dirs[:] = []
            # 跳过测试 / 文档 / 构建目录
            dirs[:] = [
                d
                for d in dirs
                if d not in ("test", "tests", "docs", "doc", "examples", "build", "dist", ".git")
            ]
            for filename in files:
                if not filename.endswith(ext):
                    continue
                if file_count >= max_files:
                    return created
                file_count += 1

                abs_path = os.path.join(root, filename)
                rel_path = os.path.relpath(abs_path, pkg_path)
                module_path = (
                    f"{package_name}.{os.path.splitext(rel_path.replace(os.sep, '.'))[0]}"
                )

                try:
                    result = RustParserFacade.parse_file(abs_path, module_path, lang)
                    if result.get("error"):
                        # Rust parse 失败 → fail closed，跳过该文件
                        continue
                    # 批量收集符号 + 一次性查重，避免 N+1 查询
                    pending = []  # 待插入的符号行
                    for sym in result.get("symbols", []):
                        qualified_name = (
                            f"{module_path}.{sym['name']}"
                            if module_path
                            else sym["name"]
                        )
                        pending.append({
                            "qualified_name": qualified_name,
                            "sym": sym,
                        })

                    if not pending:
                        continue

                    # 一次性查询已存在的 qualified_name（IN 子句，限制单批 <= 500 个占位符）
                    batch_size = 500
                    existing = set()
                    for i in range(0, len(pending), batch_size):
                        chunk = pending[i:i + batch_size]
                        placeholders = ",".join("?" * len(chunk))
                        cur = self.conn.execute(
                            f"SELECT qualified_name FROM external_symbols WHERE qualified_name IN ({placeholders})",
                            [item["qualified_name"] for item in chunk],
                        )
                        for row in cur.fetchall():
                            existing.add(row["qualified_name"])

                    # 批量 INSERT 新符号
                    new_rows = []
                    for item in pending:
                        if item["qualified_name"] in existing:
                            continue
                        sym = item["sym"]
                        new_rows.append((
                            pkg_key, pkg_ver, module_path,
                            item["qualified_name"], sym["name"],
                            sym.get("kind", "fn"),
                            sym.get("signature", ""),
                            (sym.get("comment_content") or "")[:500],
                            abs_path,
                        ))
                        created += 1
                        if created >= MAX_EXTERNAL_SYMBOLS_PER_PACKAGE:
                            break

                    if new_rows:
                        self.conn.executemany(
                            """INSERT INTO external_symbols
                               (package_name, package_version, module_path, qualified_name,
                                symbol_name, symbol_kind, signature, docstring, source_file)
                               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                            new_rows,
                        )
                    if created >= MAX_EXTERNAL_SYMBOLS_PER_PACKAGE:
                        return created
                except Exception:
                    continue

        return created

    # ==================== Python 包解析（保留原逻辑） ====================

    def _get_python_project_dependencies(self) -> Dict[str, str]:
        """读取 Python 项目依赖（requirements.txt > pyproject.toml > setup.py）"""
        deps: Dict[str, str] = {}

        requirements_path = os.path.join(
            self.workspace_root, "requirements.txt")
        if os.path.exists(requirements_path):
            deps.update(self._parse_requirements_txt(requirements_path))

        pyproject_path = os.path.join(self.workspace_root, "pyproject.toml")
        if os.path.exists(pyproject_path):
            deps.update(self._parse_pyproject_toml(pyproject_path))

        setup_path = os.path.join(self.workspace_root, "setup.py")
        if os.path.exists(setup_path):
            deps.update(self._parse_setup_py(setup_path))

        return deps

    def _get_installed_packages(self) -> Dict[str, str]:
        """获取 Python 所有已安装的第三方包及其版本"""
        packages: Dict[str, str] = {}
        try:
            result = subprocess.run(
                [sys.executable, "-m", "pip", "list", "--format", "json"],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if result.returncode == 0:
                pkg_list = json.loads(result.stdout)
                for pkg in pkg_list:
                    pkg_name = pkg["name"].lower()
                    pkg_version = pkg["version"]
                    packages[pkg_name] = pkg_version
        except Exception:
            pass

        return packages

    def _import_python_package(self, package_name: str, package_version: str) -> int:
        """导入单个 Python 包的符号"""
        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM package_versions WHERE package_name = ? AND package_version = ?",
            (package_name, package_version),
        )
        row = cur.fetchone()
        if row and row["cnt"] > 0:
            self._touch_package_version(
                package_name, package_version, "last_seen_at", "manifest"
            )
            return 0

        created = 0
        try:
            module = importlib.import_module(package_name)
            created += self._extract_package_symbols(
                package_name, package_version, module, ""
            )
        except ImportError:
            return 0

        self.conn.execute(
            "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
            (package_name, package_version),
        )
        return created

    def _parse_requirements_txt(self, path: str) -> Dict[str, str]:
        """解析 requirements.txt"""
        deps: Dict[str, str] = {}
        for line in read_file_text(path).splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.split("==")
            if len(parts) >= 2:
                deps[parts[0].strip().lower()] = parts[1].strip()
            else:
                deps[line.strip().lower()] = ""
        return deps

    def _parse_pyproject_toml(self, path: str) -> Dict[str, str]:
        """解析 pyproject.toml"""
        deps: Dict[str, str] = {}
        try:
            try:
                import tomllib
            except ModuleNotFoundError:
                import tomli as tomllib  # Python 3.10 回退

            with open(path, "rb") as f:
                data = tomllib.load(f)

            for section in ["project.dependencies", "tool.poetry.dependencies"]:
                parts = section.split(".")
                current = data
                valid = True
                for part in parts:
                    if part in current:
                        current = current[part]
                    else:
                        valid = False
                        break
                if valid and isinstance(current, list):
                    for dep in current:
                        dep = dep.strip()
                        parts = dep.split("==")
                        if len(parts) >= 2:
                            deps[parts[0].strip().lower()] = parts[1].strip()
                        else:
                            deps[dep.strip().lower()] = ""
        except Exception:
            pass
        return deps

    def _parse_setup_py(self, path: str) -> Dict[str, str]:
        """解析 setup.py"""
        deps: Dict[str, str] = {}
        try:
            content = read_file_text(path)

            match = re.search(r"install_requires\s*=\s*\[([^\]]+)\]", content)
            if match:
                for dep in match.group(1).split(","):
                    dep = dep.strip().strip("'\"")
                    if dep:
                        parts = dep.split("==")
                        if len(parts) >= 2:
                            deps[parts[0].strip().lower()] = parts[1].strip()
                        else:
                            deps[dep.strip().lower()] = ""
        except Exception:
            pass
        return deps

    def _extract_package_symbols(
        self,
        package_name: str,
        package_version: str,
        module,
        prefix: str,
        depth: int = 0,
    ) -> int:
        """提取 Python 包的顶层导出符号

        外部包索引只服务于项目边界上的 API 识别，不递归扫描依赖包内部实现。
        """
        if depth >= MAX_EXTERNAL_MODULE_DEPTH:
            return 0

        created = 0
        module_name = prefix if prefix else package_name
        module_path = getattr(module, "__file__", "")

        # 收集所有候选符号 + 一次性查重，避免 N+1 查询
        # [(qualified_name, name, kind, signature, docstring), ...]
        candidates = []
        for name, obj in inspect.getmembers(module):
            if name.startswith("_"):
                continue
            if created + len(candidates) >= MAX_EXTERNAL_SYMBOLS_PER_PACKAGE:
                break
            kind = self._infer_symbol_kind(obj)
            if kind is None or kind == "module":
                continue
            qualified_name = f"{module_name}.{name}" if module_name else name
            signature = self._get_symbol_signature(obj, name)
            docstring = inspect.getdoc(obj) or ""
            candidates.append(
                (qualified_name, name, kind, signature, docstring))

        if not candidates:
            return 0

        # 一次性查询已存在的 qualified_name
        batch_size = 500
        existing = set()
        for i in range(0, len(candidates), batch_size):
            chunk = candidates[i:i + batch_size]
            placeholders = ",".join("?" * len(chunk))
            cur = self.conn.execute(
                f"SELECT qualified_name FROM external_symbols WHERE qualified_name IN ({placeholders})",
                [c[0] for c in chunk],
            )
            for row in cur.fetchall():
                existing.add(row["qualified_name"])

        # 批量 INSERT 新符号
        new_rows = []
        for qname, name, kind, signature, docstring in candidates:
            if qname in existing:
                continue
            new_rows.append((
                package_name, package_version, module_name,
                qname, name, kind, signature,
                docstring[:500] if docstring else "",
                module_path,
            ))
            created += 1
            if created >= MAX_EXTERNAL_SYMBOLS_PER_PACKAGE:
                break

        if new_rows:
            self.conn.executemany(
                """INSERT INTO external_symbols
                   (package_name, package_version, module_path, qualified_name,
                    symbol_name, symbol_kind, signature, docstring, source_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                new_rows,
            )

        return created

    def _infer_symbol_kind(self, obj) -> Optional[str]:
        """推断符号类型"""
        if inspect.isfunction(obj) or inspect.ismethod(obj):
            return "fn"
        elif inspect.isclass(obj):
            return "class"
        elif isinstance(obj, property):
            return "property"
        elif isinstance(obj, (int, float, str, bool)):
            return "constant"
        elif inspect.ismodule(obj):
            return "module"
        else:
            return None

    def _get_symbol_signature(self, obj, name: str) -> str:
        """获取符号签名"""
        try:
            if inspect.isfunction(obj) or inspect.ismethod(obj):
                sig = inspect.signature(obj)
                params = str(sig)
                return f"def {name}{params}"
            elif inspect.isclass(obj):
                return f"class {name}"
            elif isinstance(obj, property):
                return f"@property {name}"
            elif isinstance(obj, (int, float, str, bool)):
                return f"{name} = {repr(obj)[:50]}"
        except (ValueError, TypeError):
            pass

        return name

    # ==================== 各语言清单解析器 ====================

    def _parse_rust_manifest(self, path: str) -> Dict[str, str]:
        """解析 Cargo.toml

        仅提取 [dependencies] 里的运行时直接依赖。

        每个依赖项支持两种格式：
        - 简单字符串：`serde = "1.0"`
        - 表格形式：`serde = { version = "1.0", features = [...] }`
        """
        deps: Dict[str, str] = {}
        try:
            try:
                import tomllib
            except ModuleNotFoundError:
                import tomli as tomllib  # Python 3.10 回退

            with open(path, "rb") as f:
                data = tomllib.load(f)
            direct_deps = data.get("dependencies", {})
            if isinstance(direct_deps, dict):
                for dep_name, spec in direct_deps.items():
                    if isinstance(spec, str):
                        deps[dep_name] = spec
                    elif isinstance(spec, dict) and "version" in spec:
                        deps[dep_name] = spec["version"]
                    else:
                        deps[dep_name] = ""
        except Exception:
            pass
        return deps

    def _parse_rust_lockfile(self, path: str) -> Dict[str, str]:
        """解析 Cargo.lock"""
        deps: Dict[str, str] = {}
        try:
            try:
                import tomllib
            except ModuleNotFoundError:
                import tomli as tomllib  # Python 3.10 回退

            with open(path, "rb") as f:
                data = tomllib.load(f)
            for pkg in data.get("package", []):
                name = pkg.get("name", "")
                version = pkg.get("version", "")
                if name:
                    deps[name] = version
        except Exception:
            pass
        return deps

    def _parse_go_manifest(self, path: str) -> Dict[str, str]:
        """解析 go.mod

        支持两种 require 形式：
        - 块形式：
            require (
                github.com/pkg/errors v0.9.1
                golang.org/x/text v0.3.7
            )
        - 单行形式：
            require github.com/spf13/cobra v1.6.1

        跳过 replace / exclude / retract 段。
        """
        deps: Dict[str, str] = {}
        try:
            content = read_file_text(path)

            # 1) 匹配 require ( ... ) 块
            block_match = re.search(r"require\s*\(([^)]+)\)", content)
            if block_match:
                for line in block_match.group(1).split("\n"):
                    line = line.strip()
                    if not line or line.startswith("//"):
                        continue
                    if "// indirect" in line:
                        continue
                    parts = line.split()
                    if len(parts) >= 2:
                        deps[parts[0]] = parts[1]
            # 2) 匹配单行 require（排除 require ( 块开头）
            # 单行 require 后跟空格+非(字符+空格+版本
            for m in re.finditer(
                r"^require\s+(\S+)\s+(\S+)(?:\s+//.*)?\s*$",
                content,
                re.MULTILINE,
            ):
                if m.group(1) == "(":
                    continue
                if "// indirect" in m.group(0):
                    continue
                deps[m.group(1)] = m.group(2)
        except Exception:
            pass
        return deps

    def _parse_go_lockfile(self, path: str) -> Dict[str, str]:
        """解析 go.sum"""
        deps: Dict[str, str] = {}
        try:
            for line in read_file_text(path).splitlines():
                parts = line.strip().split()
                if len(parts) >= 2:
                    # 格式: <modpath> <version> <hash>
                    # 取版本（去除 /go.mod 后缀）
                    deps[parts[0]] = parts[1]
        except Exception:
            pass
        return deps

    def _import_go_package_symbols(
        self, package_name: str, version: str
    ) -> int:
        """Go 包符号导入：从 GOPATH/pkg/mod 或 vendor/ 扫描 .go 源码

        Go module cache 结构：
            $GOPATH/pkg/mod/<module_path>@<version>/

        其中 module_path 可包含斜杠（如 github.com/pkg/errors），
        version 形如 v1.2.3 或 pseudo-version v0.0.0-20210101000000-abcdef。

        Args:
            package_name: Go module path（如 "github.com/pkg/errors"）
            version: 版本（如 "v0.9.1"）

        Returns:
            导入的符号数量
        """
        pkg_key = f"ext-go-{package_name}"
        pkg_ver = version or "unknown"

        # 已导入则跳过
        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM package_versions WHERE package_name = ? AND package_version = ?",
            (pkg_key, pkg_ver),
        )
        if cur.fetchone()["cnt"] > 0:
            return 0

        # 定位模块源码目录
        module_path = self._find_go_module_path(package_name, version)
        if not module_path:
            # 未找到源码：仅记录依赖关系到 package_versions
            self.conn.execute(
                "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
                (pkg_key, pkg_ver),
            )
            return 0

        # 扫描 Go 源码提取符号（使用 GoParser）
        created = self._scan_package_source_files(
            "go", package_name, version, module_path, ".go", max_files=MAX_EXTERNAL_SOURCE_FILES_PER_PACKAGE
        )

        # 记录到 package_versions
        self.conn.execute(
            "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
            (pkg_key, pkg_ver),
        )
        return created

    def _find_go_module_path(
        self, package_name: str, version: Optional[str] = None
    ) -> Optional[str]:
        """定位 GOPATH/pkg/mod/ 下的 Go 模块源码目录

        查找路径：
            $GOPATH/pkg/mod/<module_path_part1>/<module_path_part2>/.../<module_name>@<version>/

        Go module cache 使用嵌套目录结构：
        - module path 的斜杠转为目录分隔符
        - 最后一部分加上 @version 后缀
        - 大写字母转为 !小写（如 GitHub.com/foo -> !github.com/foo）

        例如 github.com/pkg/errors v0.9.1 的 cache 路径：
            $GOPATH/pkg/mod/github.com/pkg/errors@v0.9.1/

        Args:
            package_name: Go module path（如 "github.com/pkg/errors"）
            version: 期望版本，为空则取最新可用版本

        Returns:
            模块源码目录绝对路径，未找到返回 None
        """
        # 优先查找 vendor/<package_name>（vendor mode）
        vendor_path = os.path.join(self.workspace_root, "vendor", package_name)
        if os.path.isdir(vendor_path):
            return vendor_path

        # Go module 缓存路径查找：
        # 1. GOMODCACHE 环境变量（go env GOMODCACHE 的值）
        # 2. GOPATH/pkg/mod（GOPATH 默认 ~/go，可通过环境变量覆盖）
        mod_cache = os.environ.get("GOMODCACHE")
        if not mod_cache:
            go_path = os.environ.get(
                "GOPATH",
                os.path.join(os.path.expanduser("~"), "go"),
            )
            mod_cache = os.path.join(go_path, "pkg", "mod")
        if not os.path.isdir(mod_cache):
            return None

        # 将 module path 拆分为各部分，最后一部分加 @version
        parts = package_name.split("/")
        if not parts:
            return None

        # 逐级遍历除最后一部分外的目录
        parent = mod_cache
        for part in parts[:-1]:
            # 处理大写字母转义：!x -> X
            encoded_part = part.replace("!", "")  # 简化处理，先尝试直接查找
            next_dir = os.path.join(parent, encoded_part)
            if os.path.isdir(next_dir):
                parent = next_dir
                continue
            # 尝试大写字母反转义：!x -> X
            for entry in os.listdir(parent):
                if entry.replace("!", "").lower() == part.lower() or entry == part:
                    parent = os.path.join(parent, entry)
                    break
            else:
                return None

        # 最后一部分形如 <name>@<version>
        last_part_base = parts[-1]
        candidates: List[Tuple[str, str]] = []

        if not os.path.isdir(parent):
            return None
        for entry in os.listdir(parent):
            # 期望形如 "<name>@<version>" 或 "<name>"
            if "@" in entry:
                name_part, ver_part = entry.split("@", 1)
            else:
                name_part, ver_part = entry, ""
            # 名字匹配（忽略 ! 转义）
            if name_part.replace("!", "").lower() != last_part_base.lower():
                continue
            if not ver_part:
                continue
            candidates.append((ver_part, os.path.join(parent, entry)))

        if not candidates:
            return None

        # 版本匹配：精确 > 模糊前缀 > 字母序最大（最新）
        if version:
            ver_clean = version.split("+incompatible")[0].strip()
            # 精确匹配
            for entry_ver, entry_path in candidates:
                if entry_ver == ver_clean:
                    return entry_path
            # 模糊前缀匹配
            if ver_clean:
                for entry_ver, entry_path in candidates:
                    if entry_ver.startswith(ver_clean):
                        return entry_path

        # 默认取字母序最大的版本（一般是最新版）
        candidates.sort(key=lambda x: x[0], reverse=True)
        return candidates[0][1]

    def _parse_typescript_manifest(self, path: str) -> Dict[str, str]:
        """解析 package.json（TS/JS 通用）"""
        return self._parse_package_json(path)

    def _parse_javascript_manifest(self, path: str) -> Dict[str, str]:
        """解析 package.json（JS）"""
        return self._parse_package_json(path)

    def _parse_package_json(self, path: str) -> Dict[str, str]:
        """解析 package.json 的直接 runtime dependencies

        Args:
            path: package.json 文件路径

        Returns:
            {包名: 版本约束} 字典
        """
        deps: Dict[str, str] = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            section = data.get("dependencies", {})
            if isinstance(section, dict):
                for dep_name, version in section.items():
                    deps[dep_name] = str(version)
        except Exception:
            pass
        return deps

    def _read_package_json_entry(self, pkg_dir: str) -> Dict[str, str]:
        """读取 npm 包的 package.json 入口字段

        用于确定包的入口文件，优先扫描入口文件而非全部源码。

        Args:
            pkg_dir: npm 包目录（如 node_modules/lodash）

        Returns:
            包含 main/module/types/typings 字段的字典
        """
        result: Dict[str, str] = {}
        pkg_json_path = os.path.join(pkg_dir, "package.json")
        if not os.path.isfile(pkg_json_path):
            return result
        try:
            with open(pkg_json_path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            for key in ("main", "module", "types", "typings", "browser"):
                val = data.get(key)
                if isinstance(val, str) and val:
                    result[key] = val
        except Exception:
            pass
        return result

    def _import_typescript_package_symbols(
        self, package_name: str, version: str
    ) -> int:
        """TypeScript 包符号导入：扫描 node_modules/<pkg>/ 下的 .ts 和 .js 文件

        TS 包通常同时发布 .js（编译产物）和 .d.ts（类型声明），
        偶尔包含 .ts（源码）。本方法扫描 .ts + .tsx + .js 文件，
        使用对应语言的 parser 提取符号。

        Args:
            package_name: npm 包名（支持 scoped，如 @types/node）
            version: 版本约束

        Returns:
            导入的符号数量
        """
        return self._import_npm_package_symbols(
            "typescript", package_name, version
        )

    def _import_javascript_package_symbols(
        self, package_name: str, version: str
    ) -> int:
        """JavaScript 包符号导入：扫描 node_modules/<pkg>/ 下的 .js 文件

        Args:
            package_name: npm 包名
            version: 版本约束

        Returns:
            导入的符号数量
        """
        return self._import_npm_package_symbols(
            "javascript", package_name, version
        )

    def _import_npm_package_symbols(
        self, lang: str, package_name: str, version: str
    ) -> int:
        """npm 包符号扫描通用实现（TS/JS 共用）

        扫描策略：
        1. 定位 node_modules/<package_name>/ 目录
        2. 读取 package.json 的 main/module/types 字段确定入口
        3. 优先扫描入口文件，再扫描包根第一层源码
        4. TS 同时扫描 .ts 和 .js 文件；JS 仅扫描 .js

        Args:
            lang: "typescript" 或 "javascript"
            package_name: npm 包名
            version: 版本约束

        Returns:
            导入的符号数量
        """
        pkg_key = f"ext-{lang}-{package_name}"
        pkg_ver = version or "unknown"

        # 已导入则跳过
        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM package_versions WHERE package_name = ? AND package_version = ?",
            (pkg_key, pkg_ver),
        )
        if cur.fetchone()["cnt"] > 0:
            return 0

        # 定位包目录
        pkg_dir = os.path.join(self.workspace_root,
                               "node_modules", package_name)
        if not os.path.isdir(pkg_dir):
            # 标记已完成（避免重复查找）
            self.conn.execute(
                "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
                (pkg_key, pkg_ver),
            )
            return 0

        # 决定要扫描的扩展名
        # TS: .ts + .tsx + .js + .jsx（很多 TS 包只发布编译后的 .js）
        # JS: .js + .jsx
        if lang == "typescript":
            extensions = [".ts", ".tsx", ".js", ".jsx", ".d.ts"]
        else:
            extensions = [".js", ".jsx"]

        # 读取入口字段（main/module/types）
        entry_fields = self._read_package_json_entry(pkg_dir)
        # 入口文件优先级：types > typings > module > main > browser
        entry_candidates = []
        for key in ("types", "typings", "module", "main", "browser"):
            v = entry_fields.get(key)
            if v:
                entry_candidates.append(v)

        created = 0
        scanned_files = set()
        max_files = MAX_EXTERNAL_SOURCE_FILES_PER_PACKAGE

        # 优先扫描入口文件
        for entry_rel in entry_candidates:
            entry_path = os.path.join(pkg_dir, entry_rel)
            if not os.path.isfile(entry_path):
                continue
            ext = os.path.splitext(entry_path)[1]
            if ext not in extensions:
                continue
            created += self._scan_one_npm_file(
                lang, package_name, version, entry_path, pkg_dir
            )
            scanned_files.add(os.path.normpath(entry_path))
            if len(scanned_files) >= max_files:
                break

        # 再扫描包根第一层源码文件，不递归进入依赖包内部实现
        if len(scanned_files) < max_files:
            try:
                root_files = os.listdir(pkg_dir)
            except OSError:
                root_files = []
            for filename in root_files:
                if len(scanned_files) >= max_files:
                    break
                abs_path = os.path.join(pkg_dir, filename)
                if not os.path.isfile(abs_path):
                    continue
                ext = os.path.splitext(filename)[1]
                if ext not in extensions:
                    continue
                norm_path = os.path.normpath(abs_path)
                if norm_path in scanned_files:
                    continue
                scanned_files.add(norm_path)
                created += self._scan_one_npm_file(
                    lang, package_name, version, abs_path, pkg_dir
                )

        # 标记完成
        self.conn.execute(
            "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
            (pkg_key, pkg_ver),
        )
        return created

    def _scan_one_npm_file(
        self,
        lang: str,
        package_name: str,
        version: str,
        abs_path: str,
        pkg_dir: str,
    ) -> int:
        """扫描 npm 包中的单个源码文件

        根据文件扩展名选择合适的 parser：
        - .ts/.tsx/.d.ts → TypeScriptParser
        - .js/.jsx → TypeScriptParser (JS 模式)

        Args:
            lang: 语言标识
            package_name: npm 包名
            version: 版本
            abs_path: 文件绝对路径
            pkg_dir: 包根目录（用于计算相对路径作为 module_path）

        Returns:
            导入的符号数量
        """
        # P1-E: 改走 RustParserFacade（Rust-only），不再实例化 Python parser。
        ext = os.path.splitext(abs_path)[1]
        # 根据扩展名决定解析语言
        if ext in (".ts", ".tsx", ".d.ts"):
            file_lang = "typescript"
        elif ext in (".js", ".jsx"):
            file_lang = "javascript"
        else:
            return 0
        if not RustParserFacade.supports_language(file_lang):
            return 0

        # 推导 module_path
        rel_path = os.path.relpath(abs_path, pkg_dir)
        rel_path = rel_path.replace(os.sep, "/")
        # 去掉扩展名，把 / 转为 .
        module_path = os.path.splitext(rel_path)[0].replace("/", ".")
        if module_path.endswith(".index"):
            module_path = module_path[:-6]  # 去 .index 后缀

        pkg_key = f"ext-{lang}-{package_name}"
        pkg_ver = version or "unknown"
        created = 0

        try:
            result = RustParserFacade.parse_file(abs_path, module_path, file_lang)
            if result.get("error"):
                # Rust parse 失败 → fail closed，返回 0
                return 0
            # 批量收集 + 一次性查重，避免 N+1
            pending = []
            for sym in result.get("symbols", []):
                qualified_name = (
                    f"{module_path}.{sym['name']}" if module_path else sym["name"]
                )
                pending.append({"qualified_name": qualified_name, "sym": sym})

            if not pending:
                return 0

            # 一次性查询已存在
            batch_size = 500
            existing = set()
            for i in range(0, len(pending), batch_size):
                chunk = pending[i:i + batch_size]
                placeholders = ",".join("?" * len(chunk))
                cur = self.conn.execute(
                    f"SELECT qualified_name FROM external_symbols WHERE qualified_name IN ({placeholders})",
                    [item["qualified_name"] for item in chunk],
                )
                for row in cur.fetchall():
                    existing.add(row["qualified_name"])

            # 批量 INSERT 新符号
            new_rows = []
            for item in pending:
                if item["qualified_name"] in existing:
                    continue
                sym = item["sym"]
                new_rows.append((
                    pkg_key, pkg_ver, module_path,
                    item["qualified_name"], sym["name"],
                    sym.get("kind", "fn"),
                    sym.get("signature", ""),
                    (sym.get("comment_content") or "")[:500],
                    abs_path,
                ))
                created += 1

            if new_rows:
                self.conn.executemany(
                    """INSERT INTO external_symbols
                       (package_name, package_version, module_path, qualified_name,
                        symbol_name, symbol_kind, signature, docstring, source_file)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    new_rows,
                )
        except Exception:
            pass
        return created

    def _parse_typescript_lockfile(self, path: str) -> Dict[str, str]:
        """解析 package-lock.json"""
        return self._parse_package_lock(path)

    def _parse_javascript_lockfile(self, path: str) -> Dict[str, str]:
        """解析 package-lock.json"""
        return self._parse_package_lock(path)

    def _parse_package_lock(self, path: str) -> Dict[str, str]:
        """解析 package-lock.json v2/v3 的 packages 字段"""
        deps: Dict[str, str] = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            packages = data.get("packages", {})
            root_info = packages.get("", {})
            direct_names = set()
            if isinstance(root_info, dict):
                direct_names.update(root_info.get("dependencies", {}).keys())
            if not direct_names:
                direct_names.update(data.get("dependencies", {}).keys())
            for pkg_path, info in packages.items():
                if not pkg_path or pkg_path == "":
                    continue
                # 路径形如 node_modules/lodash 或 node_modules/@types/node
                name = pkg_path.split("node_modules/")[-1]
                if name not in direct_names:
                    continue
                if "version" in info:
                    deps[name] = info["version"]
        except Exception:
            pass
        return deps

    def _parse_java_manifest(self, path: str) -> Dict[str, str]:
        """解析 Java 依赖清单（pom.xml 或 build.gradle）

        Args:
            path: 清单文件路径

        Returns:
            {groupId:artifactId: version} 字典
        """
        if path.endswith(".xml"):
            return self._parse_maven_pom(path)
        else:
            return self._parse_gradle_build(path)

    def _parse_maven_pom(self, path: str) -> Dict[str, str]:
        """解析 pom.xml（maven）

        使用 ElementTree 解析 XML，支持：
        - <dependencies> 段下的 <dependency> 条目
        - <dependencyManagement> 段下定义的版本（继承给 <dependencies>）
        - <properties> 段定义的 ${变量} 替换
        - 父 POM 继承（仅当 properties 段定义了 ${parent.version}）

        Args:
            path: pom.xml 文件路径

        Returns:
            {groupId:artifactId: version} 字典
        """
        deps: Dict[str, str] = {}
        try:
            import xml.etree.ElementTree as ET

            # 忽略 maven 命名空间前缀
            content = read_file_text(path)
            # 去除 <project xmlns="..."> 中的命名空间，简化后续 xpath 查询
            content_no_ns = re.sub(r'\sxmlns="[^"]+"', "", content, count=1)
            root = ET.fromstring(content_no_ns)

            # 1) 收集 <properties> 段用于变量替换
            properties: Dict[str, str] = {}
            props_elem = root.find("properties")
            if props_elem is not None:
                for prop in props_elem:
                    properties[prop.tag] = (prop.text or "").strip()
            # 内置变量
            properties.setdefault(
                "project.version",
                (root.findtext("version") or "").strip(),
            )
            properties.setdefault(
                "project.groupId",
                (root.findtext("groupId") or "").strip(),
            )

            # 2) 收集 <dependencyManagement> 中的版本（作为后备版本）
            managed_versions: Dict[str, str] = {}
            dep_mgmt = root.find("dependencyManagement/dependencies")
            if dep_mgmt is not None:
                for dep in dep_mgmt.findall("dependency"):
                    g = (dep.findtext("groupId") or "").strip()
                    a = (dep.findtext("artifactId") or "").strip()
                    v = (dep.findtext("version") or "").strip()
                    if g and a:
                        managed_versions[f"{g}:{a}"] = self._resolve_maven_props(
                            v, properties
                        )

            # 3) 收集 <dependencies> 段（直接依赖）
            for deps_parent in (root, root.find("dependencyManagement") or ET.Element("none")):
                if deps_parent is None:
                    continue
                deps_elem = deps_parent.find("dependencies")
                if deps_elem is None:
                    continue
                # dependencyManagement 里的依赖只用于版本管理，不算项目依赖
                if deps_parent.tag == "dependencyManagement":
                    continue
                for dep in deps_elem.findall("dependency"):
                    g = (dep.findtext("groupId") or "").strip()
                    a = (dep.findtext("artifactId") or "").strip()
                    v = (dep.findtext("version") or "").strip()
                    if not g or not a:
                        continue
                    # 版本解析：直接 > dependencyManagement > 空字符串
                    if not v:
                        v = managed_versions.get(f"{g}:{a}", "")
                    v = self._resolve_maven_props(v, properties)
                    scope = (dep.findtext("scope") or "").strip()
                    # 跳过测试与运行期装配依赖，保留源码可直接引用的编译依赖
                    if scope in ("test", "runtime"):
                        continue
                    deps[f"{g}:{a}"] = v
        except Exception:
            pass
        return deps

    def _parse_gradle_build(self, path: str) -> Dict[str, str]:
        """解析 build.gradle / build.gradle.kts

        简化解析，匹配以下格式：
        - Groovy: implementation 'group:artifact:version'
        - Groovy: implementation group: 'g', name: 'a', version: 'v'
        - Kotlin DSL: implementation("group:artifact:version")
        - Kotlin DSL: implementation(group = "g", name = "a", version = "v")

        Args:
            path: build.gradle 或 build.gradle.kts 文件路径

        Returns:
            {groupId:artifactId: version} 字典
        """
        deps: Dict[str, str] = {}
        try:
            content = read_file_text(path)

            scopes = ("implementation", "api", "compileOnly", "compile")
            scope_pattern = "|".join(scopes)

            # 格式1: implementation 'group:artifact:version' 或 implementation("group:artifact:version")
            for m in re.finditer(
                rf'(?:{scope_pattern})\s*[\(]?\s*["\']([^:"\']+):([^:"\']+):([^:"\']+)["\']',
                content,
            ):
                group_id = m.group(1)
                artifact_id = m.group(2)
                version = m.group(3)
                deps[f"{group_id}:{artifact_id}"] = version

            # 格式2: implementation group: 'g', name: 'a', version: 'v'
            # （Groovy 风格，带命名参数）
            for m in re.finditer(
                rf'(?:{scope_pattern})\s+group:\s*["\']([^"\']+)["\']\s*,\s*name:\s*["\']([^"\']+)["\'](?:\s*,\s*version:\s*["\']([^"\']*)["\'])?',
                content,
            ):
                group_id = m.group(1)
                artifact_id = m.group(2)
                version = m.group(3) or ""
                deps[f"{group_id}:{artifact_id}"] = version

            # 格式3: implementation(group = "g", name = "a", version = "v")
            # （Kotlin DSL 风格，带命名参数）
            for m in re.finditer(
                rf'(?:{scope_pattern})\s*\(\s*group\s*=\s*["\']([^"\']+)["\']\s*,\s*name\s*=\s*["\']([^"\']+)["\'](?:\s*,\s*version\s*=\s*["\']([^"\']*)["\'])?',
                content,
            ):
                group_id = m.group(1)
                artifact_id = m.group(2)
                version = m.group(3) or ""
                deps[f"{group_id}:{artifact_id}"] = version
        except Exception:
            pass
        return deps

    def _resolve_maven_props(self, value: str, properties: Dict[str, str]) -> str:
        """解析 Maven 属性占位符 ${var.name}

        Args:
            value: 包含占位符的字符串
            properties: 属性字典

        Returns:
            替换后的字符串，未匹配的占位符保留原样
        """
        if not value or "${" not in value:
            return value
        result = value
        # 循环替换支持嵌套属性
        for _ in range(5):
            m = re.search(r"\$\{([^}]+)\}", result)
            if not m:
                break
            key = m.group(1)
            replacement = properties.get(key)
            if replacement is None:
                break
            result = result[: m.start()] + replacement + result[m.end():]
        return result

    def _import_java_package_symbols(
        self, package_name: str, version: str
    ) -> int:
        """Java 包符号导入：从 maven/gradle 本地仓库扫描 jar

        策略：
        1. 优先扫描 sources jar（含 .java 源码，用 JavaParser 解析）
        2. 回退到 class jar，使用 javap 提取公共 API（需 JDK）
        3. 查找路径：~/.m2/repository → ~/.gradle/caches/modules-2/files-2.1

        Args:
            package_name: 形如 "groupId:artifactId"
            version: 版本号

        Returns:
            导入的符号数量
        """
        pkg_key = f"ext-java-{package_name}"
        pkg_ver = version or "unknown"

        # 已导入则跳过
        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM package_versions WHERE package_name = ? AND package_version = ?",
            (pkg_key, pkg_ver),
        )
        if cur.fetchone()["cnt"] > 0:
            return 0

        # 定位 jar 文件（先查 maven，再查 gradle 缓存）
        sources_jar = None
        class_jar = None

        # 1) Maven 本地仓库
        mvn_sources = self._find_maven_artifact_jar(
            package_name, version, prefer_sources=True
        )
        mvn_class = self._find_maven_artifact_jar(
            package_name, version, prefer_sources=False
        )
        if mvn_sources and mvn_sources.endswith("-sources.jar"):
            sources_jar = mvn_sources
        if mvn_class and not mvn_class.endswith("-sources.jar"):
            class_jar = mvn_class

        # 2) Gradle 缓存（如果 maven 没找到）
        if not sources_jar or not class_jar:
            gradle_sources = self._find_gradle_artifact_jar(
                package_name, version, prefer_sources=True
            )
            gradle_class = self._find_gradle_artifact_jar(
                package_name, version, prefer_sources=False
            )
            if not sources_jar and gradle_sources and gradle_sources.endswith("-sources.jar"):
                sources_jar = gradle_sources
            if not class_jar and gradle_class and not gradle_class.endswith("-sources.jar"):
                class_jar = gradle_class

        created = 0
        if sources_jar:
            # 优先：sources jar 含 .java 源码，使用 JavaParser 扫描
            created = self._scan_java_sources_jar(
                sources_jar, package_name, version
            )
        elif class_jar:
            # 回退：class jar 用 javap 提取公共 API
            created = self._scan_java_class_jar_via_javap(
                class_jar, package_name, version
            )

        # 标记已完成（即使符号为 0 也要标记，避免重复扫描）
        self.conn.execute(
            "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
            (pkg_key, pkg_ver),
        )
        return created

    def _find_maven_artifact_jar(
        self,
        package_name: str,
        version: str,
        prefer_sources: bool = False,
    ) -> Optional[str]:
        """定位 maven 本地仓库中的 jar 文件

        maven 仓库结构：
            ~/.m2/repository/<group_path>/<artifact>/<version>/<artifact>-<version>[-sources].jar

        其中 group_path 是 groupId 的点号替换为斜杠。

        本地仓库路径查找优先级：
        1. 环境变量 M2_REPO
        2. ~/.m2/settings.xml 中的 <localRepository>
        3. 默认 ~/.m2/repository

        Args:
            package_name: "groupId:artifactId"
            version: 期望版本
            prefer_sources: True 优先返回 -sources.jar

        Returns:
            jar 文件绝对路径，未找到返回 None
        """
        if ":" not in package_name:
            return None
        group_id, artifact_id = package_name.split(":", 1)
        group_path = group_id.replace(".", os.sep)
        m2_home = self._get_maven_local_repo()
        # 版本可为空或带约束（如 [1.0,2.0)），取干净版本部分
        ver_clean = (version or "").strip().lstrip("[(").rstrip("])")
        if not ver_clean or ver_clean == "unknown":
            return None

        artifact_dir = os.path.join(
            m2_home, group_path, artifact_id, ver_clean)
        if not os.path.isdir(artifact_dir):
            return None

        # 优先匹配 sources jar，回退到 class jar
        sources_name = f"{artifact_id}-{ver_clean}-sources.jar"
        class_name = f"{artifact_id}-{ver_clean}.jar"
        sources_path = os.path.join(artifact_dir, sources_name)
        class_path = os.path.join(artifact_dir, class_name)

        if prefer_sources:
            return sources_path if os.path.isfile(sources_path) else (
                class_path if os.path.isfile(class_path) else None
            )
        return class_path if os.path.isfile(class_path) else (
            sources_path if os.path.isfile(sources_path) else None
        )

    def _find_gradle_artifact_jar(
        self,
        package_name: str,
        version: str,
        prefer_sources: bool = False,
    ) -> Optional[str]:
        """定位 Gradle 缓存中的 jar 文件

        Gradle 缓存结构：
            <gradle_home>/caches/modules-2/files-2.1/<group>/<artifact>/<version>/<hash>/<artifact>-<version>[-sources].jar

        gradle_home 查找优先级：
        1. 环境变量 GRADLE_USER_HOME
        2. ~/.gradle/gradle.properties 中的 gradle.user.home 属性
        3. 项目级 gradle.properties 中的 gradle.user.home 属性
        4. 默认 ~/.gradle

        Args:
            package_name: "groupId:artifactId"
            version: 期望版本
            prefer_sources: True 优先返回 -sources.jar

        Returns:
            jar 文件绝对路径，未找到返回 None
        """
        if ":" not in package_name:
            return None
        group_id, artifact_id = package_name.split(":", 1)
        gradle_home = self._get_gradle_user_home()
        # 版本可为空或带约束，取干净版本部分
        ver_clean = (version or "").strip().lstrip("[(").rstrip("])")
        if not ver_clean or ver_clean == "unknown":
            return None

        version_dir = os.path.join(
            gradle_home, "caches", "modules-2", "files-2.1",
            group_id, artifact_id, ver_clean
        )
        if not os.path.isdir(version_dir):
            return None

        # Gradle 在版本目录下有 hash 子目录，每个子目录放一个 jar
        # 遍历所有 hash 子目录寻找匹配的 jar
        sources_path = None
        class_path = None
        sources_name = f"{artifact_id}-{ver_clean}-sources.jar"
        class_name = f"{artifact_id}-{ver_clean}.jar"

        for hash_dir in os.listdir(version_dir):
            hash_path = os.path.join(version_dir, hash_dir)
            if not os.path.isdir(hash_path):
                continue
            for fname in os.listdir(hash_path):
                fpath = os.path.join(hash_path, fname)
                if not os.path.isfile(fpath):
                    continue
                if fname == sources_name:
                    sources_path = fpath
                elif fname == class_name:
                    class_path = fpath

        if prefer_sources:
            return sources_path if sources_path else class_path
        return class_path if class_path else sources_path

    def _get_maven_local_repo(self) -> str:
        """获取 Maven 本地仓库路径

        查找优先级：
        1. 环境变量 M2_REPO（指向 repository 目录本身）
        2. 环境变量 MAVEN_REPO（同义别名）
        3. ~/.m2/settings.xml 中的 <localRepository> 标签
        4. 项目级 .mvn/maven.config 中的 -Dmaven.repo.local 参数
        5. 默认 ~/.m2/repository

        Returns:
            Maven 本地仓库的绝对路径
        """
        # 1) 环境变量
        for var in ("M2_REPO", "MAVEN_REPO"):
            val = os.environ.get(var)
            if val and os.path.isdir(val):
                return os.path.abspath(val)

        # 2) ~/.m2/settings.xml 中的 <localRepository>
        m2_dir = os.path.join(os.path.expanduser("~"), ".m2")
        settings_path = os.path.join(m2_dir, "settings.xml")
        if os.path.isfile(settings_path):
            try:
                import xml.etree.ElementTree as ET

                content = read_file_text(settings_path)
                # 去除命名空间
                content_no_ns = re.sub(
                    r'\sxmlns="[^"]+"', "", content, count=1)
                root = ET.fromstring(content_no_ns)
                local_repo = root.findtext("localRepository")
                if local_repo and local_repo.strip():
                    local_repo = os.path.expanduser(local_repo.strip())
                    if os.path.isdir(local_repo):
                        return os.path.abspath(local_repo)
            except Exception:
                pass

        # 3) 项目级 .mvn/maven.config（含 -Dmaven.repo.local=...）
        project_mvn_config = os.path.join(
            self.workspace_root, ".mvn", "maven.config")
        if os.path.isfile(project_mvn_config):
            try:
                for line in read_file_text(project_mvn_config).splitlines():
                    m = re.search(r"-Dmaven\.repo\.local=(\S+)", line)
                    if m:
                        path = os.path.expanduser(m.group(1))
                        if os.path.isdir(path):
                            return os.path.abspath(path)
            except Exception:
                pass

        # 4) 默认值
        return os.path.join(m2_dir, "repository")

    def _get_gradle_user_home(self) -> str:
        """获取 Gradle 用户主目录（GRADLE_USER_HOME）

        查找优先级：
        1. 环境变量 GRADLE_USER_HOME
        2. ~/.gradle/gradle.properties 中的 gradle.user.home 属性
        3. 项目级 gradle.properties 中的 gradle.user.home 属性
        4. 默认 ~/.gradle

        Returns:
            Gradle 用户主目录的绝对路径
        """
        # 1) 环境变量
        env_val = os.environ.get("GRADLE_USER_HOME")
        if env_val and os.path.isdir(env_val):
            return os.path.abspath(env_val)

        # 2) 解析 gradle.properties 文件
        candidate_files = [
            # 用户级：~/.gradle/gradle.properties
            os.path.join(os.path.expanduser("~"),
                         ".gradle", "gradle.properties"),
            # 项目级：<workspace>/gradle.properties
            os.path.join(self.workspace_root, "gradle.properties"),
        ]
        for props_path in candidate_files:
            if not os.path.isfile(props_path):
                continue
            try:
                for line in read_file_text(props_path).splitlines():
                    # gradle.properties 格式：key=value 或 key:value
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    m = re.match(r"^gradle\.user\.home\s*[=:]\s*(.+)$", line)
                    if m:
                        path = os.path.expanduser(m.group(1).strip())
                        if os.path.isdir(path):
                            return os.path.abspath(path)
            except Exception:
                pass

        # 3) 默认值
        return os.path.join(os.path.expanduser("~"), ".gradle")

    def _get_nuget_packages_root(self) -> str:
        """获取 NuGet 全局包目录

        查找优先级：
        1. 环境变量 NUGET_PACKAGES
        2. 环境变量 NUGET_HTTP_CACHE_PATH（部分旧版本使用）
        3. ~/.nuget/NuGet/NuGet.Config 中的 globalPackagesFolder
        4. 项目级 nuget.config 中的 globalPackagesFolder
        5. 默认 ~/.nuget/packages

        Returns:
            NuGet 全局包目录的绝对路径
        """
        # 1) 环境变量
        for var in ("NUGET_PACKAGES", "NUGET_HTTP_CACHE_PATH"):
            val = os.environ.get(var)
            if val and os.path.isdir(val):
                return os.path.abspath(val)

        # 2) nuget.config 配置文件
        candidate_configs = [
            # 用户级：~/.nuget/NuGet/NuGet.Config
            os.path.join(os.path.expanduser("~"), ".nuget",
                         "NuGet", "NuGet.Config"),
            # 项目级：<workspace>/nuget.config
            os.path.join(self.workspace_root, "nuget.config"),
        ]
        for config_path in candidate_configs:
            if not os.path.isfile(config_path):
                continue
            try:
                import xml.etree.ElementTree as ET

                content = read_file_text(config_path)
                content_no_ns = re.sub(
                    r'\sxmlns="[^"]+"', "", content, count=1)
                root = ET.fromstring(content_no_ns)
                # 查找 <config><add key="globalPackagesFolder" value="..." /></config>
                config_elem = root.find("config")
                if config_elem is not None:
                    for add_elem in config_elem.findall("add"):
                        key = (add_elem.get("key") or "").strip()
                        if key == "globalPackagesFolder":
                            value = (add_elem.get("value") or "").strip()
                            if value:
                                value = os.path.expanduser(value)
                                if os.path.isdir(value):
                                    return os.path.abspath(value)
            except Exception:
                pass

        # 3) 默认值
        return os.path.join(os.path.expanduser("~"), ".nuget", "packages")

    def _get_cargo_home(self) -> str:
        """获取 Cargo 用户主目录

        查找优先级：
        1. 环境变量 CARGO_HOME
        2. ~/.cargo/config.toml 中的 [env] 段 CARGO_HOME
        3. ~/.cargo/config 中的 [env] 段 CARGO_HOME（旧格式，无 .toml 扩展名）
        4. 默认 ~/.cargo

        Returns:
            Cargo 主目录的绝对路径
        """
        # 1) 环境变量
        env_val = os.environ.get("CARGO_HOME")
        if env_val and os.path.isdir(env_val):
            return os.path.abspath(env_val)

        # 2) ~/.cargo/config.toml 或 ~/.cargo/config
        cargo_default_dir = os.path.join(os.path.expanduser("~"), ".cargo")
        for config_name in ("config.toml", "config"):
            config_path = os.path.join(cargo_default_dir, config_name)
            if not os.path.isfile(config_path):
                continue
            try:
                content = read_file_text(config_path)
                # 简化解析：查找 [env] 段下的 CARGO_HOME = "..."
                in_env_section = False
                for line in content.splitlines():
                    stripped = line.strip()
                    if not stripped or stripped.startswith("#"):
                        continue
                    # 段头检测
                    if stripped.startswith("[") and stripped.endswith("]"):
                        in_env_section = stripped == "[env]"
                        continue
                    if not in_env_section:
                        continue
                    # CARGO_HOME = "/path" 或 CARGO_HOME = '/path'
                    m = re.match(
                        r'^CARGO_HOME\s*=\s*["\']([^"\']+)["\']', stripped)
                    if m:
                        path = os.path.expanduser(m.group(1).strip())
                        if os.path.isdir(path):
                            return os.path.abspath(path)
            except Exception:
                pass

        # 3) 默认值
        return cargo_default_dir

    def _scan_java_sources_jar(
        self, jar_path: str, package_name: str, version: str
    ) -> int:
        """扫描 sources jar 中的 .java 源码，提取符号

        Args:
            jar_path: sources jar 文件路径
            package_name: "groupId:artifactId"
            version: 版本

        Returns:
            导入的符号数量
        """
        import zipfile

        # P1-E: 改走 RustParserFacade（Rust-only），不再实例化 Python parser。
        if not RustParserFacade.supports_language("java"):
            return 0

        pkg_key = f"ext-java-{package_name}"
        pkg_ver = version or "unknown"
        created = 0
        file_count = 0
        max_files = MAX_EXTERNAL_SOURCE_FILES_PER_PACKAGE

        try:
            with zipfile.ZipFile(jar_path) as zf:
                java_files = sorted(
                    n for n in zf.namelist()
                    if n.endswith(".java") and not n.endswith("/package-info.java")
                    and not self._is_external_archive_internal_path(n)
                )
                java_files.sort(key=lambda n: (n.count("/"), n))
                for name in java_files:
                    if file_count >= max_files:
                        break
                    file_count += 1
                    try:
                        content = zf.read(name).decode(
                            "utf-8", errors="replace")
                    except Exception:
                        continue
                    # 写入临时文件让 parser 解析
                    import tempfile

                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".java", delete=False, encoding="utf-8"
                    ) as tmp:
                        tmp.write(content)
                        tmp_path = tmp.name
                    try:
                        # 推导 module_path：jar 内路径的目录替换为 .
                        module_path = os.path.splitext(
                            name.replace("/", "."))[0]
                        # 去掉 module_path 末尾的文件名
                        if "." in module_path:
                            module_path = module_path.rsplit(".", 1)[0]
                        result = RustParserFacade.parse_file(tmp_path, module_path, "java")
                        if result.get("error"):
                            # Rust parse 失败 → fail closed，跳过该文件
                            continue
                        # 批量收集 + 一次性查重，避免 N+1
                        pending = []
                        for sym in result.get("symbols", []):
                            qualified_name = (
                                f"{module_path}.{sym['name']}"
                                if module_path else sym["name"]
                            )
                            pending.append(
                                {"qualified_name": qualified_name, "sym": sym})

                        if not pending:
                            continue

                        # 一次性查询已存在
                        batch_size = 500
                        existing = set()
                        for i in range(0, len(pending), batch_size):
                            chunk = pending[i:i + batch_size]
                            placeholders = ",".join("?" * len(chunk))
                            cur = self.conn.execute(
                                f"SELECT qualified_name FROM external_symbols WHERE qualified_name IN ({placeholders})",
                                [item["qualified_name"] for item in chunk],
                            )
                            for row in cur.fetchall():
                                existing.add(row["qualified_name"])

                        # 批量 INSERT 新符号
                        new_rows = []
                        for item in pending:
                            if item["qualified_name"] in existing:
                                continue
                            sym = item["sym"]
                            new_rows.append((
                                pkg_key, pkg_ver, module_path,
                                item["qualified_name"], sym["name"],
                                sym.get("kind", "fn"),
                                sym.get("signature", ""),
                                (sym.get("comment_content") or "")[:500],
                                f"jar:{jar_path}!/{name}",
                            ))
                            created += 1
                            if created >= MAX_EXTERNAL_SYMBOLS_PER_PACKAGE:
                                break

                        if new_rows:
                            self.conn.executemany(
                                """INSERT INTO external_symbols
                                   (package_name, package_version, module_path, qualified_name,
                                    symbol_name, symbol_kind, signature, docstring, source_file)
                                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                                new_rows,
                            )
                        if created >= MAX_EXTERNAL_SYMBOLS_PER_PACKAGE:
                            return created
                    except Exception:
                        continue
                    finally:
                        try:
                            os.unlink(tmp_path)
                        except Exception:
                            pass
        except Exception:
            pass
        return created

    def _scan_java_class_jar_via_javap(
        self, jar_path: str, package_name: str, version: str
    ) -> int:
        """使用 javap 命令提取 class jar 中的公共 API 符号

        需要 JDK 安装。对每个 .class 文件运行 javap -public 输出公共字段和方法签名。

        优化（P16）：批量 javap 调用。原实现对每个 .class 文件单独启动 javap 子进程，
        JVM 启动开销 ~0.3-0.5s/次，20 类 = 6-10s/包，17 个包 = 100-170s。
        现在改为一次 javap 调用处理一批 class（最多 20 个），JVM 只启动一次。

        Args:
            jar_path: class jar 文件路径
            package_name: "groupId:artifactId"
            version: 版本

        Returns:
            导入的符号数量
        """
        import zipfile

        # 检查 javap 可用性
        try:
            probe = subprocess.run(
                ["javap", "-version"],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            if probe.returncode != 0:
                return 0
        except Exception:
            return 0

        pkg_key = f"ext-java-{package_name}"
        pkg_ver = version or "unknown"
        created = 0
        class_count = 0
        max_classes = MAX_EXTERNAL_SOURCE_FILES_PER_PACKAGE  # 限制处理类数

        try:
            with zipfile.ZipFile(jar_path) as zf:
                class_files = sorted(
                    n for n in zf.namelist()
                    if n.endswith(".class")
                    and not n.endswith("package-info.class")
                    and not n.endswith("module-info.class")
                    and not self._is_external_archive_internal_path(n)
                )
                class_files.sort(key=lambda n: (n.count("/"), n))
                # 收集要处理的 class 列表（跳过内部类）
                classes_to_process = []
                for name in class_files:
                    if class_count >= max_classes:
                        break
                    # class 名：com/foo/Bar.class -> com.foo.Bar
                    class_name = name[:-6].replace("/", ".")
                    # 内部类跳过（含 $）
                    if "$" in class_name:
                        continue
                    classes_to_process.append((name, class_name))
                    class_count += 1

                if not classes_to_process:
                    return 0

                # P16: 批量 javap 调用
                # javap 支持一次处理多个 class：javap -classpath X -public A B C...
                # 输出用 "Compiled from \"X.java\"" 分隔不同类
                # 分批处理，每批最多 20 个 class（避免命令行过长）
                BATCH_SIZE = 20
                for batch_start in range(0, len(classes_to_process), BATCH_SIZE):
                    if created >= MAX_EXTERNAL_SYMBOLS_PER_PACKAGE:
                        return created
                    batch = classes_to_process[batch_start:batch_start + BATCH_SIZE]
                    class_names = [cn for _, cn in batch]

                    try:
                        result = subprocess.run(
                            ["javap", "-classpath", jar_path,
                                "-public"] + class_names,
                            capture_output=True,
                            text=True,
                            encoding="utf-8",
                            timeout=30,  # 批量处理给更长超时
                        )
                        if result.returncode != 0:
                            # 批量失败，回退到逐类处理
                            for name, class_name in batch:
                                if created >= MAX_EXTERNAL_SYMBOLS_PER_PACKAGE:
                                    return created
                                created = self._javap_single_class(
                                    jar_path, name, class_name,
                                    pkg_key, pkg_ver, created,
                                )
                            continue
                        # 解析批量输出
                        created = self._parse_javap_batch_output(
                            result.stdout, jar_path, batch,
                            pkg_key, pkg_ver, created,
                        )
                    except subprocess.TimeoutExpired:
                        # 超时，回退到逐类处理
                        for name, class_name in batch:
                            if created >= MAX_EXTERNAL_SYMBOLS_PER_PACKAGE:
                                return created
                            created = self._javap_single_class(
                                jar_path, name, class_name,
                                pkg_key, pkg_ver, created,
                            )
                    except Exception:
                        continue
        except Exception:
            pass
        return created

    def _parse_javap_batch_output(
        self,
        stdout: str,
        jar_path: str,
        batch: list,
        pkg_key: str,
        pkg_ver: str,
        created: int,
    ) -> int:
        """解析批量 javap 输出，提取符号并插入数据库

        Args:
            stdout: javap 的标准输出
            jar_path: jar 文件路径
            batch: [(archive_name, class_name), ...] 本批处理的 class 列表
            pkg_key: 包键
            pkg_ver: 版本
            created: 已导入符号数

        Returns:
            更新后的已导入符号数
        """
        # javap 批量输出格式：
        #   Compiled from "Bar.java"
        #   public class com.foo.Bar {
        #     public void doSomething();
        #     ...
        #   }
        #   Compiled from "Baz.java"
        #   public class com.foo.Baz {
        #     ...
        #   }
        # 按 "Compiled from" 分割成不同 class 的块
        blocks = re.split(r'^Compiled from ".*?"$', stdout, flags=re.MULTILINE)
        # 第一块是空（开头就是 Compiled from）
        blocks = [b for b in blocks if b.strip()]

        # 构建类名到 archive_name 的映射
        name_to_archive = {cn: an for an, cn in batch}

        for block in blocks:
            if created >= MAX_EXTERNAL_SYMBOLS_PER_PACKAGE:
                return created
            # 从块中提取类名（匹配 "public class/abstract/interface/enum/final class X"）
            class_match = re.search(
                r"public\s+(?:final\s+|abstract\s+)?(?:class|interface|enum)\s+(\S+)",
                block,
            )
            if not class_match:
                continue
            class_name = class_match.group(1).rstrip("{").strip()
            # 找到对应的 archive name
            archive_name = name_to_archive.get(class_name, "")
            if not archive_name:
                # 可能是内部类或其他，跳过
                continue

            created = self._parse_and_insert_javap_block(
                block, class_name, archive_name, jar_path,
                pkg_key, pkg_ver, created,
            )

        return created

    def _parse_and_insert_javap_block(
        self,
        block: str,
        class_name: str,
        archive_name: str,
        jar_path: str,
        pkg_key: str,
        pkg_ver: str,
        created: int,
    ) -> int:
        """解析单个 class 的 javap 输出块，插入符号

        Args:
            block: javap 输出中一个 class 的块
            class_name: 类完整限定名
            archive_name: jar 内路径
            jar_path: jar 文件路径
            pkg_key: 包键
            pkg_ver: 版本
            created: 已导入符号数

        Returns:
            更新后的已导入符号数
        """
        module_path = class_name.rsplit(".", 1)[0] if "." in class_name else ""
        class_short = class_name.rsplit(".", 1)[-1]
        source_file = f"jar:{jar_path}!/{archive_name}"

        # 类自身
        if self._insert_java_external_symbol(
            pkg_key, pkg_ver, module_path,
            class_name, class_short, "class",
            f"public class {class_short}",
            source_file,
        ):
            created += 1

        if created >= MAX_EXTERNAL_SYMBOLS_PER_PACKAGE:
            return created

        # 解析方法和字段
        for line in block.split("\n"):
            line = line.strip().rstrip(";")
            if not line or line.startswith("//") or line.startswith("Compiled"):
                continue
            # 跳过类声明行
            if line.startswith(("public class", "public final class",
                                "public abstract class",
                                "public interface", "public enum",
                                "public final class", "class ")):
                continue
            # 形如 "public void doSomething()" 或 "public static int MAX"
            m = re.match(
                r"public\s+(?:static\s+)?(?:final\s+)?"
                r"([\w<>\[\],.\s]+?)\s+(\w+)\s*\(([^)]*)\)",
                line,
            )
            if m:
                ret_type = m.group(1).strip()
                method_name = m.group(2).strip()
                params = m.group(3).strip()
                qname = f"{class_name}.{method_name}"
                if self._insert_java_external_symbol(
                    pkg_key, pkg_ver, class_name,
                    qname, method_name, "method",
                    f"public {ret_type} {method_name}({params})",
                    source_file,
                ):
                    created += 1
                    if created >= MAX_EXTERNAL_SYMBOLS_PER_PACKAGE:
                        return created
                continue
            # 字段
            m = re.match(
                r"public\s+(?:static\s+)?(?:final\s+)?"
                r"([\w<>\[\],.]+)\s+(\w+)",
                line,
            )
            if m:
                field_type = m.group(1).strip()
                field_name = m.group(2).strip()
                qname = f"{class_name}.{field_name}"
                if self._insert_java_external_symbol(
                    pkg_key, pkg_ver, class_name,
                    qname, field_name, "field",
                    f"public {field_type} {field_name}",
                    source_file,
                ):
                    created += 1
                    if created >= MAX_EXTERNAL_SYMBOLS_PER_PACKAGE:
                        return created

        return created

    def _javap_single_class(
        self,
        jar_path: str,
        archive_name: str,
        class_name: str,
        pkg_key: str,
        pkg_ver: str,
        created: int,
    ) -> int:
        """对单个 class 调用 javap（批量失败时的回退路径）

        Args:
            jar_path: jar 文件路径
            archive_name: jar 内路径
            class_name: 类完整限定名
            pkg_key: 包键
            pkg_ver: 版本
            created: 已导入符号数

        Returns:
            更新后的已导入符号数
        """
        try:
            result = subprocess.run(
                ["javap", "-classpath", jar_path, "-public", class_name],
                capture_output=True,
                text=True,
                encoding="utf-8",
                timeout=10,
            )
            if result.returncode != 0:
                return created
            return self._parse_and_insert_javap_block(
                result.stdout, class_name, archive_name, jar_path,
                pkg_key, pkg_ver, created,
            )
        except Exception:
            return created

    def _is_external_archive_internal_path(self, archive_path: str) -> bool:
        """判断 jar 内路径是否属于内部实现、测试或元数据目录。"""
        parts = [p.lower() for p in archive_path.replace("\\", "/").split("/")]
        blocked = {
            "meta-inf",
            "internal",
            "impl",
            "implementation",
            "generated",
            "test",
            "tests",
            "examples",
            "benchmark",
            "benchmarks",
        }
        return any(part in blocked for part in parts)

    def _insert_java_external_symbol(
        self,
        pkg_key: str,
        pkg_ver: str,
        module_path: str,
        qualified_name: str,
        symbol_name: str,
        kind: str,
        signature: str,
        source_file: str,
    ) -> bool:
        """插入一个 Java 外部符号，已存在则跳过

        Args:
            pkg_key: 包键（如 ext-java-com.google.guava:guava）
            pkg_ver: 版本
            module_path: 模块路径（通常是 Java 包名）
            qualified_name: 完整限定名
            symbol_name: 短名
            kind: 符号类型（class/method/field）
            signature: 签名
            source_file: 源文件路径

        Returns:
            是否新增（已存在返回 False）
        """
        cur = self.conn.execute(
            "SELECT id FROM external_symbols WHERE qualified_name = ?",
            (qualified_name,),
        )
        if cur.fetchone():
            return False
        self.conn.execute(
            """INSERT INTO external_symbols
               (package_name, package_version, module_path, qualified_name,
                symbol_name, symbol_kind, signature, docstring, source_file)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                pkg_key, pkg_ver, module_path,
                qualified_name, symbol_name, kind,
                signature, "", source_file,
            ),
        )
        return True

    def _parse_scala_manifest(self, path: str) -> Dict[str, str]:
        """解析 build.sbt

        支持以下 libraryDependencies 形式：
        - 单依赖：% 操作符：
            libraryDependencies += "org.typelevel" %% "cats-core" % "2.9.0"
          （%% 表示按 Scala 版本自动添加 _2.13 后缀）
        - 单依赖：纯 % 操作符：
            libraryDependencies += "org.apache.commons" % "commons-lang3" % "3.12.0"
        - 块形式：
            libraryDependencies ++= Seq(
              "org.typelevel" %% "cats-core" % "2.9.0",
              "com.typesafe.play" %% "play" % "2.8.18"
            )
        - 带 scope 后缀：% Test/% "test"
        """
        deps: Dict[str, str] = {}
        try:
            content = read_file_text(path)

            # 匹配 "group" [%|%%] "artifact" % "version"
            # 注意 %% 是 Scala 特有：按 scalaVersion 自动加后缀
            # 这里统一记录为 group:artifact（不带 _2.13 后缀）
            pattern = (
                r'"([^"]+)"\s*'                # group_id
                r'(?:%%|%)'                     # %% 或 %
                r'\s*"([^"]+)"\s*'              # artifact_id
                r'%\s*"([^"]+)"'                # version
            )
            for m in re.finditer(pattern, content):
                group_id = m.group(1)
                artifact_id = m.group(2)
                version = m.group(3)
                deps[f"{group_id}:{artifact_id}"] = version
        except Exception:
            pass
        return deps

    def _parse_ruby_manifest(self, path: str) -> Dict[str, str]:
        """解析 Gemfile

        匹配 gem 'name', 'version' 或 gem "name", "version"
        """
        deps: Dict[str, str] = {}
        try:
            for line in read_file_text(path).splitlines():
                m = re.match(
                    r"gem\s+['\"]([^'\"]+)['\"](?:\s*,\s*['\"]([^'\"]+)['\"])?",
                    line.strip(),
                )
                if m:
                    deps[m.group(1)] = m.group(2) or ""
        except Exception:
            pass
        return deps

    def _parse_ruby_lockfile(self, path: str) -> Dict[str, str]:
        """解析 Gemfile.lock

        格式：
            GEM
              remote: ...
              specs:
                rake (13.0.6)
                rspec (3.10.0)
        """
        deps: Dict[str, str] = {}
        try:
            in_specs = False
            for line in read_file_text(path).splitlines():
                if "specs:" in line:
                    in_specs = True
                    continue
                if in_specs:
                    if line.startswith("    ") and not line.startswith("      "):
                        # 形如 "    rake (13.0.6)"
                        m = re.match(r"\s+(\S+)\s+\(([^)]+)\)", line)
                        if m:
                            deps[m.group(1)] = m.group(2)
                    elif line.strip() and not line.startswith(" "):
                        in_specs = False
        except Exception:
            pass
        return deps

    def _parse_php_manifest(self, path: str) -> Dict[str, str]:
        """解析 composer.json"""
        deps: Dict[str, str] = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            section = data.get("require", {})
            if isinstance(section, dict):
                for dep_name, version in section.items():
                    deps[dep_name] = str(version)
        except Exception:
            pass
        return deps

    def _parse_php_lockfile(self, path: str) -> Dict[str, str]:
        """解析 composer.lock"""
        deps: Dict[str, str] = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            for section in ["packages"]:
                for pkg in data.get(section, []):
                    name = pkg.get("name", "")
                    version = pkg.get("version", "")
                    if name:
                        deps[name] = version
        except Exception:
            pass
        return deps

    def _parse_swift_manifest(self, path: str) -> Dict[str, str]:
        """解析 Package.swift

        简化解析：匹配 .package(url: "...", from: "1.0.0") 或 .package(path: "...")
        """
        deps: Dict[str, str] = {}
        try:
            content = read_file_text(path)

            # 匹配 .package(url: "<url>", from: "1.0.0") 或 .package(url: ..., .exact("<version>"))
            for m in re.finditer(
                r'\.package\(\s*url:\s*"([^"]+)"[^)]*?(?:from:\s*"([^"]+)"|\.exact\("([^"]+)"\)|version:\s*"([^"]+)"\))',
                content,
            ):
                url = m.group(1)
                version = m.group(2) or m.group(3) or m.group(4) or ""
                # 从 URL 提取包名（取最后一段，去掉 .git）
                name = url.rstrip("/").split("/")[-1].replace(".git", "")
                deps[name] = version

            # 匹配 .package(name: "X", url: "...", from: "...")
            for m in re.finditer(
                r'\.package\(\s*name:\s*"([^"]+)"[^)]*?(?:from:\s*"([^"]+)"|version:\s*"([^"]+)")',
                content,
            ):
                deps[m.group(1)] = m.group(2) or m.group(3) or ""
        except Exception:
            pass
        return deps

    def _parse_kotlin_manifest(self, path: str) -> Dict[str, str]:
        """解析 build.gradle.kts / build.gradle

        简化解析：匹配 implementation("group:artifact:version") 等
        """
        deps: Dict[str, str] = {}
        try:
            content = read_file_text(path)

            scopes = ("implementation", "api", "compileOnly", "compile")
            scope_pattern = "|".join(scopes)

            # 匹配 implementation "group:artifact:version" 或 implementation("group:artifact:version")
            for m in re.finditer(
                rf'(?:{scope_pattern})\s*[\(]?\s*["\']([^:"\']+):([^:"\']+):([^:"\']+)["\']',
                content,
            ):
                group_id = m.group(1)
                artifact_id = m.group(2)
                version = m.group(3)
                deps[f"{group_id}:{artifact_id}"] = version
        except Exception:
            pass
        return deps

    def _parse_elixir_manifest(self, path: str) -> Dict[str, str]:
        """解析 mix.exs

        简化解析：匹配 {:name, "~> 1.0"} 或 {:name, "~> 1.0", ...}
        """
        deps: Dict[str, str] = {}
        try:
            content = read_file_text(path)

            for m in re.finditer(
                r'\{:(\w+),\s*"([^"]+)"',
                content,
            ):
                deps[m.group(1)] = m.group(2)
        except Exception:
            pass
        return deps

    def _parse_elixir_lockfile(self, path: str) -> Dict[str, str]:
        """解析 mix.lock

        格式（Elixir Map 字面量）：
        %{
          "phoenix": {:hex, :phoenix, "1.7.0", ...},
          ...
        }
        """
        deps: Dict[str, str] = {}
        try:
            content = read_file_text(path)

            for m in re.finditer(
                r'"([^"]+)":\s*\{:hex,\s*:(\w+),\s*"([^"]+)"',
                content,
            ):
                deps[m.group(2)] = m.group(3)
        except Exception:
            pass
        return deps

    def _parse_csharp_manifest(self, path: str) -> Dict[str, str]:
        """解析 .csproj（C# 项目文件）或 packages.config

        .csproj 格式（PackageReference）：
            <ItemGroup>
              <PackageReference Include="Newtonsoft.Json" Version="13.0.1" />
            </ItemGroup>

        packages.config 格式（旧式）：
            <packages>
              <package id="Newtonsoft.Json" version="13.0.1" />
            </packages>
        """
        deps: Dict[str, str] = {}
        try:
            import xml.etree.ElementTree as ET

            content = read_file_text(path)
            # 去除命名空间
            content_no_ns = re.sub(r'\sxmlns="[^"]+"', "", content, count=1)
            root = ET.fromstring(content_no_ns)

            # .csproj: <PackageReference Include="..." Version="..." />
            for pr in root.iter("PackageReference"):
                name = (pr.get("Include") or "").strip()
                version = (pr.get("Version") or "").strip()
                if name:
                    deps[name] = version

            # packages.config: <package id="..." version="..." />
            for pkg in root.iter("package"):
                name = (pkg.get("id") or "").strip()
                version = (pkg.get("version") or "").strip()
                if name:
                    deps[name] = version
        except Exception:
            pass
        return deps

    def _parse_csharp_lockfile(self, path: str) -> Dict[str, str]:
        """解析 packages.lock.json（.NET Core 锁文件）

        格式：
            {
              "dependencies": {
                "Newtonsoft.Json": { "resolved": "13.0.1" },
                ...
              }
            }
        """
        deps: Dict[str, str] = {}
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            dependencies = data.get("dependencies", {})
            if isinstance(dependencies, dict):
                # .NET 8+ 格式：{ "framework": { "dep": {...} } }
                for framework, framework_deps in dependencies.items():
                    if isinstance(framework_deps, dict) and "dependencies" in framework_deps:
                        for name, info in framework_deps["dependencies"].items():
                            deps[name] = info.get("resolved", "") if isinstance(
                                info, dict) else ""
                    elif isinstance(framework_deps, dict):
                        # 直接格式：{ "dep": {"resolved": "1.0"} }
                        for name, info in framework_deps.items():
                            if isinstance(info, dict):
                                deps[name] = info.get("resolved", "")
                            else:
                                deps[name] = str(info)
        except Exception:
            pass
        return deps

    # ==================== 各语言包符号导入处理器 ====================

    def _import_csharp_package_symbols(
        self, package_name: str, version: str
    ) -> int:
        """C# 包符号导入：扫描 ~/.nuget/packages/<name>/<version>/ 下的源码

        NuGet 包结构（解压后）：
            ~/.nuget/packages/<lowercase_name>/<version>/
                lib/<tfm>/*.dll           # 编译产物
                src/*.cs                  # 源码（部分包提供）
                contentFiles/*.cs         # 内容文件

        本方法仅扫描 .cs 源码文件（非 .dll 二进制）。

        Args:
            package_name: NuGet 包名
            version: 版本号

        Returns:
            导入的符号数量
        """
        pkg_key = f"ext-csharp-{package_name}"
        pkg_ver = version or "unknown"

        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM package_versions WHERE package_name = ? AND package_version = ?",
            (pkg_key, pkg_ver),
        )
        if cur.fetchone()["cnt"] > 0:
            return 0

        # nuget 包路径查找：环境变量 → nuget.config → 默认值
        nuget_root = self._get_nuget_packages_root()
        ver_clean = (version or "").strip()
        if not ver_clean or ver_clean == "unknown":
            return 0
        pkg_dir = os.path.join(nuget_root, package_name.lower(), ver_clean)
        if not os.path.isdir(pkg_dir):
            self.conn.execute(
                "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
                (pkg_key, pkg_ver),
            )
            return 0

        # 扫描 .cs 源码
        created = self._scan_package_source_files(
            "csharp", package_name, version, pkg_dir, ".cs", max_files=MAX_EXTERNAL_SOURCE_FILES_PER_PACKAGE
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
            (pkg_key, pkg_ver),
        )
        return created

    def _import_ruby_package_symbols(
        self, package_name: str, version: str
    ) -> int:
        """Ruby gem 包符号导入：扫描 gem 源码目录

        Gem 安装位置：
        - 默认：~/.gem/ruby/<ver>/gems/<gem_name>-<ver>/
        - rbenv：~/.rbenv/versions/<ver>/lib/ruby/gems/<ver>/gems/<gem>-<ver>/
        - bundler（项目本地）：vendor/bundle/ruby/<ver>/gems/<gem>-<ver>/

        Args:
            package_name: gem 名
            version: 版本

        Returns:
            导入的符号数量
        """
        pkg_key = f"ext-ruby-{package_name}"
        pkg_ver = version or "unknown"

        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM package_versions WHERE package_name = ? AND package_version = ?",
            (pkg_key, pkg_ver),
        )
        if cur.fetchone()["cnt"] > 0:
            return 0

        # 查找 gem 源码目录（多个候选位置）
        gem_path = self._find_ruby_gem_path(package_name, version)
        if not gem_path:
            self.conn.execute(
                "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
                (pkg_key, pkg_ver),
            )
            return 0

        created = self._scan_package_source_files(
            "ruby", package_name, version, gem_path, ".rb", max_files=MAX_EXTERNAL_SOURCE_FILES_PER_PACKAGE
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
            (pkg_key, pkg_ver),
        )
        return created

    def _find_ruby_gem_path(
        self, package_name: str, version: Optional[str] = None
    ) -> Optional[str]:
        """定位 Ruby gem 源码目录

        候选位置（按优先级）：
        1. vendor/bundle/ruby/<ver>/gems/<gem>-<ver>/  (bundler 项目本地)
        2. $GEM_HOME/gems/<gem>-<ver>/                 (GEM_HOME 环境变量)
        3. $GEM_PATH 中每个路径下的 gems/<gem>-<ver>/  (GEM_PATH 环境变量)
        4. ~/.gemrc 中 gempath 指定的路径              (配置文件)
        5. ~/.gem/ruby/<ver>/gems/<gem>-<ver>/         (用户级默认 gem 路径)
        6. ~/.rbenv/versions/<ver>/lib/ruby/gems/<ver>/gems/<gem>-<ver>/  (rbenv)

        Args:
            package_name: gem 名
            version: 版本（可选）

        Returns:
            gem 源码目录绝对路径，未找到返回 None
        """
        import glob

        def match_gem_in_gems_dir(gems_dir: str) -> Optional[str]:
            """在 gems 目录下查找匹配的 gem"""
            if not os.path.isdir(gems_dir):
                return None
            matches = []
            for entry in os.listdir(gems_dir):
                if not entry.startswith(f"{package_name}-"):
                    continue
                # 确保是版本号开头（避免匹配 gemname-rails 这类非版本前缀）
                suffix = entry[len(package_name) + 1:]
                if not suffix or not suffix[0].isdigit():
                    continue
                matches.append(os.path.join(gems_dir, entry))
            if matches:
                matches.sort(reverse=True)  # 选最新版本
                return matches[0]
            return None

        # 1. 项目本地 vendor/bundle
        vendor_glob = os.path.join(
            self.workspace_root, "vendor", "bundle", "ruby", "*", "gems",
            f"{package_name}-*",
        )
        candidates = glob.glob(vendor_glob)
        if candidates:
            candidates.sort(reverse=True)
            return candidates[0]

        # 2. GEM_HOME 环境变量
        gem_home = os.environ.get("GEM_HOME")
        if gem_home:
            gems_dir = os.path.join(gem_home, "gems")
            result = match_gem_in_gems_dir(gems_dir)
            if result:
                return result

        # 3. GEM_PATH 环境变量（可能含多个路径，用路径分隔符分隔）
        gem_path = os.environ.get("GEM_PATH")
        if gem_path:
            for path in re.split(r"[;:]", gem_path):
                path = path.strip()
                if not path:
                    continue
                gems_dir = os.path.join(path, "gems")
                result = match_gem_in_gems_dir(gems_dir)
                if result:
                    return result

        # 4. ~/.gemrc 配置文件（含 gempath 配置）
        gemrc_path = os.path.join(os.path.expanduser("~"), ".gemrc")
        if os.path.isfile(gemrc_path):
            try:
                for line in read_file_text(gemrc_path).splitlines():
                    # YAML 格式: gempath: /path/to/gems
                    # 或 gempath: [/path1, /path2]
                    m = re.match(r"^\s*gempath\s*:\s*(.+)$", line)
                    if m:
                        paths_str = m.group(1).strip()
                        # 去除 YAML 数组方括号
                        paths_str = paths_str.lstrip("[").rstrip("]")
                        for path in re.split(r"[,;:]", paths_str):
                            path = path.strip().strip("'\"")
                            if not path:
                                continue
                            gems_dir = os.path.join(path, "gems")
                            result = match_gem_in_gems_dir(gems_dir)
                            if result:
                                return result
            except Exception:
                pass

        # 5. ~/.gem/ruby (默认 gem 路径)
        home_gem_dir = os.path.join(os.path.expanduser("~"), ".gem", "ruby")
        if os.path.isdir(home_gem_dir):
            for ruby_ver in os.listdir(home_gem_dir):
                gems_dir = os.path.join(home_gem_dir, ruby_ver, "gems")
                result = match_gem_in_gems_dir(gems_dir)
                if result:
                    return result

        # 6. ~/.rbenv
        rbenv_dir = os.path.join(os.path.expanduser("~"), ".rbenv", "versions")
        if os.path.isdir(rbenv_dir):
            for rbver in os.listdir(rbenv_dir):
                gems_dir = os.path.join(
                    rbenv_dir, rbver, "lib", "ruby", "gems")
                if not os.path.isdir(gems_dir):
                    continue
                for sub in os.listdir(gems_dir):
                    sub_gems = os.path.join(gems_dir, sub, "gems")
                    result = match_gem_in_gems_dir(sub_gems)
                    if result:
                        return result

        return None

    def _import_swift_package_symbols(
        self, package_name: str, version: str
    ) -> int:
        """Swift Package Manager 包符号导入：扫描 .build/checkouts/<name>/

        SwiftPM 把依赖源码检出到 .build/checkouts/<package_name>/ 目录。

        Args:
            package_name: Swift 包名
            version: 版本（仅用于记录）

        Returns:
            导入的符号数量
        """
        pkg_key = f"ext-swift-{package_name}"
        pkg_ver = version or "unknown"

        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM package_versions WHERE package_name = ? AND package_version = ?",
            (pkg_key, pkg_ver),
        )
        if cur.fetchone()["cnt"] > 0:
            return 0

        checkout_dir = os.path.join(
            self.workspace_root, ".build", "checkouts", package_name,
        )
        if not os.path.isdir(checkout_dir):
            self.conn.execute(
                "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
                (pkg_key, pkg_ver),
            )
            return 0

        created = self._scan_package_source_files(
            "swift", package_name, version, checkout_dir, ".swift", max_files=MAX_EXTERNAL_SOURCE_FILES_PER_PACKAGE
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
            (pkg_key, pkg_ver),
        )
        return created

    def _import_scala_package_symbols(
        self, package_name: str, version: str
    ) -> int:
        """Scala 包符号导入：复用 Java maven 仓库

        Scala 也使用 maven 仓库（通过 sbt），包名格式同 Java：groupId:artifactId。
        本方法直接调用 _import_java_package_symbols，但记录的 package_key 用 scala 前缀。

        Args:
            package_name: "groupId:artifactId"
            version: 版本号

        Returns:
            导入的符号数量
        """
        pkg_key = f"ext-scala-{package_name}"
        pkg_ver = version or "unknown"

        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM package_versions WHERE package_name = ? AND package_version = ?",
            (pkg_key, pkg_ver),
        )
        if cur.fetchone()["cnt"] > 0:
            return 0

        # 复用 Java 的 jar 定位和扫描逻辑
        # 但 package_name 用 scala 前缀，避免与 Java 包冲突
        # 通过临时调用 _import_java_package_symbols 但更新 package_key
        # 简化实现：直接调用 Java 处理器，依赖 qualified_name 去重避免重复
        created = self._import_java_package_symbols(package_name, version)
        # 修正 package_name 前缀
        if created > 0:
            self.conn.execute(
                "UPDATE external_symbols SET package_name = ? WHERE package_name = ?",
                (pkg_key, f"ext-java-{package_name}"),
            )
        self.conn.execute(
            "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
            (pkg_key, pkg_ver),
        )
        return created

    def _import_kotlin_package_symbols(
        self, package_name: str, version: str
    ) -> int:
        """Kotlin 包符号导入：复用 Java maven/gradle 仓库

        Kotlin JVM 项目使用 gradle/maven，依赖项与 Java 共享 ~/.m2 或
        ~/.gradle/caches/modules-2/files-2.1/<group>/<artifact>/<version>/

        Args:
            package_name: "groupId:artifactId"
            version: 版本号

        Returns:
            导入的符号数量
        """
        pkg_key = f"ext-kotlin-{package_name}"
        pkg_ver = version or "unknown"

        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM package_versions WHERE package_name = ? AND package_version = ?",
            (pkg_key, pkg_ver),
        )
        if cur.fetchone()["cnt"] > 0:
            return 0

        # 优先尝试 ~/.gradle/caches 下的 Kotlin 源码 jar
        # 回退到 Java maven 仓库
        created = self._import_java_package_symbols(package_name, version)
        if created > 0:
            self.conn.execute(
                "UPDATE external_symbols SET package_name = ? WHERE package_name = ?",
                (pkg_key, f"ext-java-{package_name}"),
            )
        self.conn.execute(
            "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
            (pkg_key, pkg_ver),
        )
        return created

    def _import_php_package_symbols(
        self, package_name: str, version: str
    ) -> int:
        """PHP composer 包符号导入：扫描 vendor/<package_name>/

        composer 把包安装到 vendor/<vendor>/<package>/ 目录。
        本方法使用默认 install_dirs 扫描。

        Args:
            package_name: 形如 "vendor/package" 的 composer 包名
            version: 版本

        Returns:
            导入的符号数量
        """
        pkg_key = f"ext-php-{package_name}"
        pkg_ver = version or "unknown"

        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM package_versions WHERE package_name = ? AND package_version = ?",
            (pkg_key, pkg_ver),
        )
        if cur.fetchone()["cnt"] > 0:
            return 0

        # composer 包名形如 "vendor/package" -> vendor/<vendor>/<package>/
        pkg_dir = os.path.join(self.workspace_root, "vendor", package_name)
        if not os.path.isdir(pkg_dir):
            self.conn.execute(
                "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
                (pkg_key, pkg_ver),
            )
            return 0

        created = self._scan_package_source_files(
            "php", package_name, version, pkg_dir, ".php", max_files=MAX_EXTERNAL_SOURCE_FILES_PER_PACKAGE
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
            (pkg_key, pkg_ver),
        )
        return created

    def _import_elixir_package_symbols(
        self, package_name: str, version: str
    ) -> int:
        """Elixir mix 包符号导入：扫描 deps/<package_name>/

        mix 把依赖源码放在 deps/<package_name>/ 目录。

        Args:
            package_name: Elixir 包名
            version: 版本

        Returns:
            导入的符号数量
        """
        pkg_key = f"ext-elixir-{package_name}"
        pkg_ver = version or "unknown"

        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM package_versions WHERE package_name = ? AND package_version = ?",
            (pkg_key, pkg_ver),
        )
        if cur.fetchone()["cnt"] > 0:
            return 0

        pkg_dir = os.path.join(self.workspace_root, "deps", package_name)
        if not os.path.isdir(pkg_dir):
            self.conn.execute(
                "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
                (pkg_key, pkg_ver),
            )
            return 0

        # Elixir 同时扫描 .ex 和 .exs 文件
        created = self._scan_package_source_files(
            "elixir", package_name, version, pkg_dir, ".ex", max_files=MAX_EXTERNAL_SOURCE_FILES_PER_PACKAGE
        )
        # .exs 文件（脚本）也扫一遍
        created += self._scan_package_source_files(
            "elixir", package_name, version, pkg_dir, ".exs", max_files=20
        )
        self.conn.execute(
            "INSERT OR IGNORE INTO package_versions (package_name, package_version) VALUES (?, ?)",
            (pkg_key, pkg_ver),
        )
        return created

    # ==================== 查询接口 ====================

    def get_external_symbol(self, qualified_name: str) -> Optional[Dict[str, Any]]:
        """查询外部符号

        Args:
            qualified_name: 符号限定名

        Returns:
            符号信息字典，未找到返回 None
        """
        cur = self.conn.execute(
            "SELECT * FROM external_symbols WHERE qualified_name = ?",
            (qualified_name,),
        )
        row = cur.fetchone()
        if row:
            self._touch_package_version(
                row["package_name"], row["package_version"], "last_used_at"
            )
            self.conn.commit()
        return dict(row) if row else None

    def search_external_symbols(self, name: str) -> List[Dict[str, Any]]:
        """搜索外部符号"""
        cur = self.conn.execute(
            "SELECT * FROM external_symbols WHERE symbol_name LIKE ?",
            (f"%{name}%",),
        )
        rows = [dict(row) for row in cur.fetchall()]
        touched = set()
        for row in rows:
            key = (row["package_name"], row["package_version"])
            if key in touched:
                continue
            self._touch_package_version(key[0], key[1], "last_used_at")
            touched.add(key)
        if touched:
            self.conn.commit()
        return rows

    def prune_external_symbols(
        self,
        keep_project_deps: bool = True,
        package_names: Optional[List[str]] = None,
        vacuum: bool = False,
    ) -> Dict[str, Any]:
        """清理外部符号索引

        Args:
            keep_project_deps: True 时保留 stdlib 和当前项目直接依赖对应的外部包
            package_names: 指定要删除的 package_name 前缀列表，例如 ["networkx", "ext-python-networkx"]
            vacuum: True 时在删除后执行 VACUUM 释放磁盘空间
        """
        before = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM external_symbols").fetchone()["cnt"]
        keep: set[str] = {"stdlib"}
        if keep_project_deps:
            deps = self.get_project_dependencies()
            for lang, lang_deps in deps.items():
                for dep in lang_deps:
                    keep.add(dep.lower())
                    keep.add(f"ext-{lang}-{dep}".lower())

        deleted = 0
        if package_names:
            for name in package_names:
                pattern = name.lower()
                cur = self.conn.execute(
                    "DELETE FROM external_symbols WHERE lower(package_name) = ? OR lower(package_name) LIKE ?",
                    (pattern, f"ext-%-{pattern}"),
                )
                deleted += cur.rowcount if cur.rowcount is not None else 0
                self.conn.execute(
                    "DELETE FROM package_versions WHERE lower(package_name) = ? OR lower(package_name) LIKE ?",
                    (pattern, f"ext-%-{pattern}"),
                )
        elif keep_project_deps:
            placeholders = ",".join("?" for _ in keep)
            cur = self.conn.execute(
                f"DELETE FROM external_symbols WHERE lower(package_name) NOT IN ({placeholders})",
                tuple(keep),
            )
            deleted += cur.rowcount if cur.rowcount is not None else 0
            self.conn.execute(
                f"DELETE FROM package_versions WHERE lower(package_name) NOT IN ({placeholders})",
                tuple(keep),
            )

        self.conn.commit()
        if vacuum:
            self.conn.execute("VACUUM")
        after = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM external_symbols").fetchone()["cnt"]
        return {"before": before, "after": after, "deleted": deleted, "vacuum": vacuum}

    def has_external_symbol(self, qualified_name: str) -> bool:
        """检查外部符号是否存在"""
        cur = self.conn.execute(
            "SELECT package_name, package_version FROM external_symbols WHERE qualified_name = ? LIMIT 1",
            (qualified_name,),
        )
        row = cur.fetchone()
        if row:
            self._touch_package_version(
                row["package_name"], row["package_version"], "last_used_at")
            self.conn.commit()
            return True
        return False
