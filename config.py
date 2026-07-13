"""
config.py
=========

代码知识图谱配置：路径常量、工具函数、多语言配置。
"""

import codecs
import hashlib
import os
import re
import tempfile
from typing import Dict, List, Optional, Tuple

# 路径常量
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PACKAGES_DIR = os.path.dirname(SCRIPT_DIR)  # scripts/
SRC_DIR = os.path.join(PACKAGES_DIR, "..", "src")

# 数据库根目录：用户主目录下的 .callwarden/
USER_HOME = os.path.expanduser("~")
CALLWARDEN_DIR = os.path.join(USER_HOME, ".callwarden")

# 向后兼容的默认 DB_PATH（推荐使用 get_project_db_path 按项目隔离）
DB_PATH = os.path.join(CALLWARDEN_DIR, "callwarden.db")


def get_project_db_path(project_root: str) -> str:
    """根据项目根路径生成项目级数据库路径（一个用户所有项目共用一个数据库）

    路径格式: $HOME/.callwarden/<16位hash>/callwarden.db

    16 位 hash 是项目根路径绝对路径的 SHA-256 前 16 位，确保不同项目的数据库隔离。
    这样每个数据库只包含一个项目的数据，体积小、查询快、互不干扰。

    注意：根据 Enterprise Daemon Shared Snapshot 设计，Global CAS 要求
    "相同文件跨用户、跨工作区只解析一次"，因此所有项目共用一个数据库。

    Args:
        project_root: 项目根目录路径

    Returns:
        项目级数据库绝对路径
    """
    abs_root = os.path.abspath(project_root)
    # 标准化路径（统一正斜杠，消除跨平台差异）
    norm_root = norm_path(abs_root)
    # 计算项目路径的 SHA-256 前 16 位作为目录名
    path_hash = hashlib.sha256(norm_root.encode("utf-8")).hexdigest()[:16]
    project_dir = os.path.join(CALLWARDEN_DIR, path_hash)
    os.makedirs(project_dir, exist_ok=True)
    return os.path.join(project_dir, "callwarden.db")


def ensure_callwarden_dir() -> str:
    """确保数据库根目录存在，返回目录路径"""
    os.makedirs(CALLWARDEN_DIR, exist_ok=True)
    return CALLWARDEN_DIR


def atomic_write_file(file_path: str, content: str, encoding: str = "utf-8") -> None:
    """原子写入文件：先写临时文件再 rename，避免半写入状态

    SEC-001 安全修复：所有文件写入必须走此函数，确保数据完整性。

    流程：
    1. 确保目标目录存在
    2. 在同目录创建临时文件（保证同一文件系统，rename 原子）
    3. 写入内容并 flush/fsync 确保落盘
    4. os.replace 原子替换目标文件（Windows/Linux 均支持）

    Args:
        file_path: 目标文件绝对路径
        content: 要写入的内容
        encoding: 文件编码（默认 utf-8）

    Raises:
        OSError: 文件写入失败
    """
    # 确保目录存在
    dir_path = os.path.dirname(file_path)
    if dir_path and not os.path.exists(dir_path):
        os.makedirs(dir_path, exist_ok=True)

    # 在同目录创建临时文件（保证同一文件系统，rename 才原子）
    dir_for_tmp = dir_path if dir_path else "."
    fd, tmp_path = tempfile.mkstemp(
        prefix=".tmp_write_",
        suffix=".bak",
        dir=dir_for_tmp,
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="") as f:
            f.write(content)
            f.flush()
            try:
                os.fsync(f.fileno())
            except OSError:
                # 某些文件系统不支持 fsync，忽略错误
                pass
        # 原子替换（Windows/Linux 均支持 os.replace）
        os.replace(tmp_path, file_path)
    except Exception:
        # 写入失败时清理临时文件
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise


def detect_project_root(start_path: str) -> Optional[str]:
    """从 start_path 向上查找项目根目录（工作区根）

    检测优先级：
    1. .repo/ 目录（repo 管理的多仓库项目，根目录优先级最高）
    2. .git/ 目录（独立 git 仓库）
    3. 项目配置文件（Cargo.toml / package.json / pom.xml / go.mod / CMakeLists.txt / setup.py）
    4. 找不到任何标记 → 返回 None

    Args:
        start_path: 起始路径（文件或目录）

    Returns:
        项目根目录绝对路径，未找到返回 None
    """
    start_path = os.path.abspath(start_path)
    if os.path.isfile(start_path):
        current = os.path.dirname(start_path)
    else:
        current = start_path

    root_markers_dir = [".repo", ".git"]
    root_markers_file = [
        "Cargo.toml", "package.json", "pom.xml", "go.mod",
        "CMakeLists.txt", "setup.py", "pyproject.toml",
        "build.gradle", "build.gradle.kts",
    ]

    while True:
        for marker in root_markers_dir:
            if os.path.isdir(os.path.join(current, marker)):
                return norm_path(current)

        for marker in root_markers_file:
            if os.path.isfile(os.path.join(current, marker)):
                return norm_path(current)

        parent = os.path.dirname(current)
        if parent == current:
            break
        current = parent

    return None


def get_default_workspace_name(root_path: str) -> str:
    """根据根目录路径生成默认工作区名称

    Args:
        root_path: 项目根目录

    Returns:
        工作区名称（取目录名）
    """
    return os.path.basename(os.path.normpath(root_path))


# 子项目识别用的清单文件 → 语言映射（与 detect_project_root 的 root_markers_file 保持一致 + 扩展）
PROJECT_MANIFESTS: Dict[str, str] = {
    "go.mod": "go",
    "Cargo.toml": "rust",
    "package.json": "javascript",
    "pom.xml": "java",
    "pyproject.toml": "python",
    "setup.py": "python",
    "Gemfile": "ruby",
    "composer.json": "php",
    "mix.exs": "elixir",
    "Package.swift": "swift",
    "build.gradle": "java",
    "build.gradle.kts": "kotlin",
    "CMakeLists.txt": "cmake",
}

# 子项目扫描时跳过的目录（第三方库 / VCS / 构建产物）
# 注意：.git 不在这里（它在 shallow 模式下作为项目边界识别后停止递归，
# 但 .git 本身不会被 os.walk 进入，因为 dirs 过滤了 d.startswith(".")）
# 注意：.repo 不在这里（P26: .repo 是 AOSP repo 工具的项目根标记，应识别为
# 项目边界并停止递归，而不是跳过）
_SUBPROJECT_SKIP_DIRS = frozenset({
    "node_modules", "target", "vendor", ".venv", "venv",
    "dist", "build", ".gradle", "__pycache__", ".m2", ".cache",
    ".next", "out", "bin", "obj",
})

# 非真实子项目目录：含 manifest 但不是真实子项目（测试 fixture / 发布包 / 示例 / 评估 / 文档）
# 这些目录下的 package.json / Cargo.toml 等是测试数据或发布配置，不是真实项目根
# 数据来源：749 仓库基准测试，4318 个"子项目"中 3700+ 是这些目录下的假阳性
_NON_REAL_PROJECT_DIRS = frozenset({
    # 测试 fixture（最大假阳性来源，fallow-rs 一个仓库就有 360 个 tests/fixtures）
    "tests", "test", "__tests__", "__fixtures__", "test-fixtures",
    "fixtures", "mocks", "stubs", "testdata", "test-data",
    "conformance", "conformance-tests",
    # 测试应用 / 端到端测试 / SDK 测试 / 集成测试（P25 新增）
    "test_apps", "test-apps", "testapp",
    "e2e", "e2e_tests", "e2e-tests",
    "sdk_tests", "sdk-tests",
    "integration_tests", "integration-tests", "integration_test",
    # Conan 包测试 fixture（conan_recipes/recipes/xxx/all/test_package）
    "test_package", "test-package",
    # 发布包（npm/darwin-arm64, npm/linux-x64-gnu 等平台特定 manifest）
    "npm",
    # 示例代码
    "examples", "example", "samples", "sample", "demos", "demo",
    # 评估
    "evals", "eval", "evaluation", "evaluations", "benchmarks",
    # 文档站点
    "docs", "doc", "website", "site",
})

# monorepo 真实子项目目录名（这些目录下的子目录是真实子项目）
_MONOREPO_PKG_DIRS = frozenset({
    "packages", "crates", "apps", "libs", "sdks",
    "modules", "services", "plugins", "extensions",
})


def _parse_gitmodules(repo_root: str) -> set:
    """解析 .gitmodules 文件，返回 submodule 路径集合

    Git submodule 的标准识别方式：.gitmodules 文件定义了 submodule 的 path。
    submodule 目录下的 .git 是文件（指向父仓库 .git/modules/），不是目录。

    Args:
        repo_root: 仓库根目录

    Returns:
        submodule 路径集合（相对 repo_root 的路径，用 / 分隔）
    """
    gm_path = os.path.join(repo_root, ".gitmodules")
    if not os.path.isfile(gm_path):
        return set()

    paths = set()
    try:
        text = read_file_text(gm_path)
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("path"):
                # 格式: path = submodule/path
                parts = line.split("=", 1)
                if len(parts) == 2:
                    sm_path = parts[1].strip()
                    paths.add(sm_path.replace("\\", "/"))
    except Exception:
        pass
    return paths


# ============================================
# P25: workspace 边界检测（避免 workspace member 被识别成独立子项目）
# ============================================

# Cargo.toml 中 [workspace] section 的检测正则
_CARGO_WORKSPACE_SECTION_RE = re.compile(r'^\s*\[workspace', re.MULTILINE)


def _is_cargo_workspace(cargo_toml_path: str) -> bool:
    """检测 Cargo.toml 是否是 Cargo workspace root（含 [workspace] section）

    Cargo workspace 的 root Cargo.toml 含 [workspace] section，其 members
    字段列出所有 workspace 成员的路径。member 本身的 Cargo.toml 是真实包，
    但它们是 workspace 内部成员，不应作为独立子项目重复识别。

    示例：
        [workspace]
        members = ["crates/types/app-types", "crates/logic/*"]

    Args:
        cargo_toml_path: Cargo.toml 文件绝对路径

    Returns:
        True 表示是 workspace root，应停止向下递归
    """
    try:
        text = read_file_text(cargo_toml_path)
        # 简单匹配 [workspace] 或 [workspace.dependencies] / [workspace.package]
        return bool(_CARGO_WORKSPACE_SECTION_RE.search(text))
    except Exception:
        return False


def _is_npm_workspace(package_json_path: str) -> bool:
    """检测 package.json 是否是 npm/yarn workspace root（含 "workspaces" 字段）

    npm/yarn/pnpm workspace root 的 package.json 含 "workspaces" 字段，
    声明 workspace 成员的 glob 模式。member 是 workspace 内部包，
    不应作为独立子项目重复识别。

    示例：
        {"workspaces": ["packages/*", "apps/*"]}

    Args:
        package_json_path: package.json 文件绝对路径

    Returns:
        True 表示是 workspace root，应停止向下递归
    """
    try:
        import json
        text = read_file_text(package_json_path)
        data = json.loads(text)
        if not isinstance(data, dict):
            return False
        # "workspaces" 字段存在即视为 workspace root
        # 值可以是 list（npm/yarn）或 dict（pnpm 的 {packages: [...]}）
        return "workspaces" in data
    except Exception:
        return False


def _is_manifest_workspace_root(manifest_path: str, manifest_name: str) -> bool:
    """检测 manifest 是否是 workspace root（多语言统一接口）

    Args:
        manifest_path: manifest 文件绝对路径
        manifest_name: manifest 文件名（Cargo.toml / package.json）

    Returns:
        True 表示是 workspace root，应停止向下递归
    """
    if manifest_name == "Cargo.toml":
        return _is_cargo_workspace(manifest_path)
    if manifest_name == "package.json":
        return _is_npm_workspace(manifest_path)
    # go.work 本身就是 workspace 文件（不是 manifest），在 scan_subprojects 中单独处理
    return False


def scan_subprojects(root_dir: str, max_depth: int = 5,
                     skip_non_real: bool = True,
                     shallow: bool = True) -> List[Dict[str, str]]:
    """向下扫描目录，识别所有子项目根（基于清单文件）

    与 detect_project_root（向上找）互补：本函数向下递归发现子项目。
    用于处理 "一个目录下有多个独立开源项目" 的场景（如 testcode/repos/）。

    算法：
    1. os.walk 递归遍历目录树
    2. 跳过第三方库/VCS/构建产物目录（性能优化）
    3. skip_non_real=True 时，跳过测试 fixture / 发布包 / 示例 / 评估 / 文档目录
       这些目录含 manifest 但不是真实子项目（如 tests/fixtures/angular-component-rollup
       下的 package.json 是测试数据）
    4. 当一个目录包含清单文件（go.mod/Cargo.toml/package.json 等）时，
       标记为项目根，记录项目名、语言、清单文件
    5. 利用 .gitmodules 识别 submodule 边界，submodule 目录不作为子项目重复识别
    6. P25: workspace 边界检测 —— Cargo.toml 含 [workspace] / package.json 含
       "workspaces" / go.work 文件 → 识别为子项目后停止向下递归，
       workspace member 不再作为独立子项目重复识别（这是 749 仓库识别成
       3028 子项目的主要原因，guardrail3 一个仓库就有 11 个 workspace member
       被错识别成独立 crate）
    7. P26: shallow 模式（默认）—— 用 git 语义作为项目边界
       - .git 目录存在 → 当前目录是仓库根 → 识别为 1 个子项目 → 停止向下递归
         （不进 packages/crates 找 monorepo member，因为它们共享同一 .git）
       - .repo 目录存在（AOSP repo 工具）→ 当前目录是项目根 → 停止递归
       - manifest 只在"无 .git 的目录"作为 fallback
       这修复了 749 仓库被识别成 1783 的问题：每个 .git = 1 个项目
       （testcode/repos/ 下 749 个 .git → 749 个项目）
       设 shallow=False 启用 deep 模式，进入仓库内部识别 monorepo 子项目

    项目根优先级（从高到低）：
    - .repo 目录（AOSP repo 工具的项目根）
    - .git 目录（git 仓库根，标准 git 语义）
    - manifest 文件（fallback：纯 Python 包等无 git 的项目）

    Args:
        root_dir: 扫描根目录
        max_depth: 最大递归深度（相对 root_dir 的目录层数），默认 5
        skip_non_real: 是否跳过非真实子项目目录（tests/fixtures/npm/examples/evals/
                      benchmarks/docs 等），默认 True。设为 False 恢复旧行为
        shallow: 是否启用 shallow 模式（默认 True）。True 时每个 .git/.repo
                识别为 1 个项目并停止递归；False 时进入仓库内部识别 monorepo
                子项目（保留 P25 workspace 边界检测）

    Returns:
        项目列表，每项含 root/rel_path/name/lang/manifest 字段
    """
    root_dir = os.path.abspath(root_dir)
    projects: List[Dict[str, str]] = []

    # P23.5: onerror 回调，跳过不可访问的目录
    def _scan_onerror(err):
        pass

    # 跟踪每个仓库的 submodule 路径（避免重复解析 .gitmodules）
    _submodule_cache: Dict[str, set] = {}

    def _get_submodule_paths(repo_root: str) -> set:
        """获取仓库的 submodule 路径（带缓存）"""
        if repo_root not in _submodule_cache:
            _submodule_cache[repo_root] = _parse_gitmodules(repo_root)
        return _submodule_cache[repo_root]

    def _find_repo_root(current_dir: str) -> Optional[str]:
        """从 current_dir 向上找最近的 .git 目录或 .gitmodules 文件所在目录"""
        d = current_dir
        while d and d != os.path.dirname(d):
            if os.path.isdir(os.path.join(d, ".git")) or os.path.isfile(os.path.join(d, ".gitmodules")):
                return d
            d = os.path.dirname(d)
        return None

    for root, dirs, files in os.walk(root_dir, onerror=_scan_onerror):
        # 计算相对路径（循环开头一次，后续重复使用）
        rel = os.path.relpath(root, root_dir).replace("\\", "/")

        # P26: shallow 模式下，在 dirs 过滤之前检测 .git/.repo 作为项目边界
        # （过滤逻辑会移除所有 d.startswith(".") 的目录，包括 .git/.repo，
        # 所以必须在过滤之前检测）
        if shallow:
            repo_marker = None
            repo_lang_marker = "git"
            if ".repo" in dirs:
                # .repo 优先级最高（AOSP repo 工具的项目根标记）
                repo_marker = ".repo"
                repo_lang_marker = "repo"
            elif ".git" in dirs:
                repo_marker = ".git"
                repo_lang_marker = "git"

            if repo_marker:
                # 识别为子项目，记录其 manifest（如果有，用真实 manifest 替代 .git/.repo）
                best_manifest = repo_marker
                best_lang = repo_lang_marker
                for manifest, lang in PROJECT_MANIFESTS.items():
                    if manifest in files:
                        best_manifest = manifest
                        best_lang = lang
                        break
                rel_path = norm_path(rel) if rel != "." else ""
                projects.append({
                    "root": root,
                    "rel_path": rel_path,
                    "name": os.path.basename(root) if rel != "." else os.path.basename(root_dir),
                    "lang": best_lang,
                    "manifest": best_manifest,
                })
                # 停止向下递归：每个 .git/.repo = 1 个项目
                # （monorepo 内部的 packages/crates 等子目录共享同一 .git，不应独立识别）
                dirs[:] = []
                continue

        # 跳过第三方/VCS/构建目录
        dirs[:] = [d for d in dirs if d not in _SUBPROJECT_SKIP_DIRS and not d.startswith(".")]

        # skip_non_real: 跳过非真实子项目目录
        if skip_non_real:
            # 检查路径中任何一级目录名是否在非真实列表中
            # 如 repo/tests/fixtures/demo → parts 含 "tests" → 跳过整个子树
            # 如 repo/packages/core → parts 不含非真实目录 → 保留
            if rel != ".":
                parts = rel.split("/")
                if any(p in _NON_REAL_PROJECT_DIRS for p in parts):
                    dirs[:] = []
                    continue
            # 同时从 dirs 中移除非真实目录，防止进入
            dirs[:] = [d for d in dirs if d not in _NON_REAL_PROJECT_DIRS]

        # 深度限制
        depth = 0 if rel == "." else rel.count("/")
        if depth > max_depth:
            dirs[:] = []
            continue

        # 跳过 submodule 目录（git submodule 是独立仓库，不作为子项目重复识别）
        if skip_non_real:
            repo_root = _find_repo_root(root)
            if repo_root and repo_root != root:
                sm_paths = _get_submodule_paths(repo_root)
                if sm_paths:
                    rel_to_repo = os.path.relpath(root, repo_root).replace("\\", "/")
                    if rel_to_repo in sm_paths:
                        # 当前目录是 submodule，跳过（不识别为子项目）
                        dirs[:] = []
                        continue

        # 检查是否是项目根（含清单文件）
        # P25: 同时检测 workspace 边界，workspace root 识别后停止向下递归
        is_workspace_root = False
        for manifest, lang in PROJECT_MANIFESTS.items():
            if manifest in files:
                manifest_abs = os.path.join(root, manifest)
                # 检测此 manifest 是否是 workspace root
                if skip_non_real and _is_manifest_workspace_root(manifest_abs, manifest):
                    is_workspace_root = True
                rel_path = norm_path(rel) if rel != "." else ""
                projects.append({
                    "root": root,
                    "rel_path": rel_path,
                    "name": os.path.basename(root) if rel != "." else os.path.basename(root_dir),
                    "lang": lang,
                    "manifest": manifest,
                })
                break  # 一个目录只取第一个匹配的清单文件

        # P25: 检测 go.work 文件（Go workspace root，单独处理因为不是 PROJECT_MANIFESTS）
        if not is_workspace_root and "go.work" in files:
            is_workspace_root = True
            rel_path = norm_path(rel) if rel != "." else ""
            projects.append({
                "root": root,
                "rel_path": rel_path,
                "name": os.path.basename(root) if rel != "." else os.path.basename(root_dir),
                "lang": "go",
                "manifest": "go.work",
            })

        # P25: workspace root 停止向下递归（member 不再作为独立子项目）
        # 重要：这是减少 749→3028 过度识别的关键，避免 workspace member 被重复识别
        if is_workspace_root:
            dirs[:] = []
            continue

    # P26.7: 容器目录启发式（shallow 模式后处理）
    # 当 member 的父目录在 _MONOREPO_PKG_DIRS（crates/packages/apps/libs/sdks 等）中时，
    # 项目根 = 容器目录的父目录（而不是 member 本身）
    # 场景：无 .git 的裸 monorepo（my_mono/crates/foo/Cargo.toml → 项目根 = my_mono/）
    # 去重：同一个项目根只保留第一次识别（避免 crates/foo + crates/bar 产生 2 条）
    if shallow:
        seen_roots = set()
        deduped = []
        for p in projects:
            rel = p["rel_path"]
            if rel and "/" in rel:
                parts = rel.split("/")
                # 检查倒数第二级是否是容器目录（如 my_mono/crates/foo → parts[-2] = "crates"）
                if len(parts) >= 2 and parts[-2] in _MONOREPO_PKG_DIRS:
                    # 项目根 = 容器目录的父目录
                    new_rel = "/".join(parts[:-2])
                    new_root = os.path.dirname(os.path.dirname(p["root"]))
                    if new_root not in seen_roots:
                        deduped.append({
                            "root": new_root,
                            "rel_path": new_rel,
                            "name": os.path.basename(new_root) if new_rel else os.path.basename(root_dir),
                            "lang": p["lang"],
                            "manifest": p["manifest"],
                        })
                        seen_roots.add(new_root)
                    continue
            if p["root"] not in seen_roots:
                deduped.append(p)
                seen_roots.add(p["root"])
        projects = deduped

    return projects


# ============================================
# 自动 .callwardenignore 生成（基于 749 仓库基准测试总结）
# ============================================

# 默认基线规则（与 db_build.py _load_ignore_patterns 的 default_ignores 保持一致）
# 这些规则已硬编码内置，auto_generate_ignore 不会重复生成
_DEFAULT_IGNORE_PATTERNS = frozenset({
    ".git/", "node_modules/", ".next/",
    "__pycache__/", ".venv/", "venv/", "env/", ".tox/", "*.egg-info/",
    "target/", "dist/", "build/", "out/", "output/", "outputs/",
    "obj/", "bin/", "rootfs/", "staging/", "sysroot/", "ccache/",
    "prebuilt/", "prebuilts/", "blob/", "toolchain/", "toolchains/",
    "ndk/", "jdk/",
    "thirdParty/", "third_party/", "vendor/",
    "autogen/", "auto_gen/", "generated/", "gen/", "generated_src/",
    "proto_gen/", "protobuf_gen/", "grpc_gen/", "moc/",
    "*.pb.cc", "*.pb.h", "*.pb.go",
    "*_pb2.py", "*_pb2.pyi", "*_pb2_grpc.py",
    "*.grpc.cc", "*.grpc.h",
    "moc_*.cpp", "ui_*.h", "qrc_*.cpp",
    "*.pyc", "*.pyo",
    ".repo/",
})

# 已知第三方库目录名（与 db_build.py _THIRD_PARTY_DIR_NAMES 保持一致）
_KNOWN_THIRD_PARTY_DIRS = frozenset({
    "node_modules", "vendor", "third_party", "thirdparty", "3rdparty",
    "bower_components", "jspm_packages", "web_modules",
    ".m2", ".gradle", "ivy",
    "deps", "deps_packages",
})

# 大文件阈值（> 500KB 的源码文件通常是打包后的第三方库）
_AUTO_LARGE_FILE_THRESHOLD = 500 * 1024
# 目录内大文件数量阈值
_AUTO_LARGE_FILE_COUNT_THRESHOLD = 3
# minified 文件特征
_AUTO_MINIFIED_MARK = ".min."


def auto_generate_ignore(project_root: str, dry_run: bool = True) -> Dict:
    """自动扫描项目，生成/更新 .callwardenignore 规则

    基于 749 仓库基准测试总结的文件特征，自动检测需要忽略的目录和文件：
    1. 第三方库目录（node_modules/vendor 等已知 + 大文件密度 + minified）
    2. 测试 fixture 目录（tests/fixtures/、__fixtures__/ 等）
    3. 发布包目录（npm/ 下的平台特定 manifest）
    4. 示例/评估/文档目录（examples/、evals/、benchmarks/、docs/）
    5. 资源文件目录（hex_array/lvgl_resource）
    6. 大文件（> 500KB 的单个源码文件）

    生成的规则只包含项目特有的，不重复默认基线（.git/ node_modules/ build/ 等）。
    合并到现有 .callwardenignore 时保留用户手写规则。

    Args:
        project_root: 项目根目录
        dry_run: True 只返回建议不写入文件，False 实际写入/更新 .callwardenignore

    Returns:
        {
            "ignore_file": .callwardenignore 路径,
            "new_patterns": [新发现的规则],
            "existing_patterns": [已存在的规则],
            "default_covered": [默认基线已覆盖的规则],
            "written": bool,  # 是否实际写入文件
        }
    """
    project_root = os.path.abspath(project_root)
    ignore_file = os.path.join(project_root, ".callwardenignore")

    # 加载现有 .callwardenignore 规则（用户手写）
    existing_patterns = set()
    if os.path.isfile(ignore_file):
        try:
            text = read_file_text(ignore_file)
            for line in text.splitlines():
                line = line.strip()
                if line and not line.startswith("#"):
                    existing_patterns.add(line)
        except Exception:
            pass

    # 新发现的规则
    new_patterns = []  # 有序列表，保持可读性
    new_set = set()
    # 默认基线已覆盖的规则（不生成）
    default_covered = set()

    def _add_pattern(pattern: str, reason: str):
        """添加规则（去重，跳过默认基线已覆盖的）"""
        # 去掉末尾 / 比较
        bare = pattern.rstrip("/")
        if not bare:
            return
        if pattern in existing_patterns or bare in existing_patterns:
            return  # 用户已手写
        if pattern in new_set or bare in new_set:
            return  # 已添加
        # 检查是否被默认基线覆盖
        if pattern in _DEFAULT_IGNORE_PATTERNS or bare in _DEFAULT_IGNORE_PATTERNS:
            default_covered.add(pattern)
            return
        new_patterns.append(pattern)
        new_set.add(pattern)

    # 扫描项目目录树（深度限制 3 层，性能优化）
    for root, dirs, files in os.walk(project_root, onerror=lambda e: None):
        rel = os.path.relpath(root, project_root)
        # 标准化为正斜杠（.callwardenignore 格式要求）
        rel = rel.replace("\\", "/")
        depth = 0 if rel == "." else rel.count("/")
        if depth > 3:
            dirs[:] = []
            continue

        # 跳过 .git/.callwardenignore 已覆盖的目录
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in _KNOWN_THIRD_PARTY_DIRS]

        dir_name = os.path.basename(root)

        # 1. 检测第三方库目录（已知目录名）
        if dir_name in _KNOWN_THIRD_PARTY_DIRS:
            _add_pattern(f"{dir_name}/", "known_third_party")
            dirs[:] = []
            continue

        # 2. 检测第三方库目录（大文件密度 / minified）
        if depth <= 2 and dirs or files:
            large_files = 0
            has_minified = False
            for f in files:
                if _AUTO_MINIFIED_MARK in f.lower():
                    has_minified = True
                    continue
                ext = os.path.splitext(f)[1].lower()
                if ext in (".js", ".ts", ".py", ".rb", ".go", ".rs", ".java", ".c", ".cpp", ".h"):
                    f_path = os.path.join(root, f)
                    try:
                        if os.path.getsize(f_path) > _AUTO_LARGE_FILE_THRESHOLD:
                            large_files += 1
                    except OSError:
                        pass

            if large_files >= _AUTO_LARGE_FILE_COUNT_THRESHOLD or has_minified:
                if rel != ".":
                    _add_pattern(f"{rel}/", "large_files_or_minified")

        # 3. 检测测试 fixture 目录
        if dir_name in ("fixtures", "__fixtures__", "test-fixtures", "testdata", "test-data", "mocks", "stubs"):
            if rel != ".":
                _add_pattern(f"{rel}/", "test_fixture")
            dirs[:] = []
            continue

        # 4. 检测发布包目录（npm/ 下的平台特定 manifest）
        if dir_name == "npm" and depth <= 2:
            _add_pattern(f"{rel}/", "npm_publish_packages")
            dirs[:] = []
            continue

        # 5. 检测示例/评估目录
        if dir_name in ("examples", "example", "samples", "sample", "demos", "demo"):
            if rel != ".":
                _add_pattern(f"{rel}/", "examples")
            dirs[:] = []
            continue
        if dir_name in ("evals", "eval", "evaluation", "benchmarks"):
            if rel != ".":
                _add_pattern(f"{rel}/", "evals_benchmarks")
            dirs[:] = []
            continue

        # 6. 检测文档站点目录
        if dir_name in ("docs", "doc", "website", "site") and depth <= 2:
            if rel != ".":
                _add_pattern(f"{rel}/", "docs_site")

    written = False
    if not dry_run and new_patterns:
        # 合并写入：保留用户手写规则 + 追加新规则
        existing_lines = []
        if os.path.isfile(ignore_file):
            existing_lines = read_file_text(ignore_file).splitlines()

        # 生成追加内容
        append_lines = ["", "# === auto-generated by cw workspace generate-ignore ==="]
        append_lines.append(f"# Total: {len(new_patterns)} new patterns")
        append_lines.append("# (default baseline like .git/ node_modules/ build/ are built-in, not listed here)")
        append_lines.append("")
        for p in new_patterns:
            append_lines.append(p)
        append_lines.append("# === end auto-generated ===")

        content = "\n".join(existing_lines + append_lines) + "\n"
        atomic_write_file(ignore_file, content)
        written = True

    return {
        "ignore_file": ignore_file,
        "new_patterns": new_patterns,
        "existing_patterns": sorted(existing_patterns),
        "default_covered": sorted(default_covered),
        "written": written,
    }


# 多语言配置：统一管理各语言的扩展名、注释符号、入口文件规则
LANGUAGE_CONFIG: Dict[str, Dict] = {
    "rust": {
        "extensions": [".rs"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "///",
        "entry_files": ["lib.rs", "main.rs", "mod.rs"],
        "module_keyword": "mod",
        "import_keyword": "use",
    },
    "typescript": {
        "extensions": [".ts", ".tsx"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/*",
        "entry_files": ["index.ts", "index.tsx", "main.ts", "main.tsx"],
        "module_keyword": "",
        "import_keyword": "import",
    },
    "javascript": {
        "extensions": [".js", ".jsx", ".mjs", ".cjs"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/*",
        "entry_files": ["index.js", "index.jsx", "main.js", "main.jsx"],
        "module_keyword": "",
        "import_keyword": "import",
    },
    "python": {
        "extensions": [".py"],
        "comment_line": "#",
        "comment_block_start": '"""',
        "comment_block_end": '"""',
        "doc_comment_line": '"""',
        "entry_files": ["__init__.py", "main.py", "__main__.py"],
        "module_keyword": "",
        "import_keyword": "import",
    },
    "kotlin": {
        "extensions": [".kt", ".kts"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/*",
        "entry_files": ["Main.kt"],
        "module_keyword": "package",
        "import_keyword": "import",
    },
    "go": {
        "extensions": [".go"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "//",
        "entry_files": ["main.go"],
        "module_keyword": "package",
        "import_keyword": "import",
    },
    "java": {
        "extensions": [".java"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/*",
        "entry_files": ["Main.java"],
        "module_keyword": "package",
        "import_keyword": "import",
    },
    "c": {
        "extensions": [".c", ".h"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/*",
        "entry_files": ["main.c"],
        "module_keyword": "",
        "import_keyword": "#include",
    },
    "cpp": {
        "extensions": [".cpp", ".cc", ".cxx", ".hpp", ".hh", ".hxx", ".h"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/*",
        "entry_files": ["main.cpp", "main.cc"],
        "module_keyword": "namespace",
        "import_keyword": "#include",
    },
    "csharp": {
        "extensions": [".cs"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "///",
        "entry_files": ["Program.cs", "Startup.cs"],
        "module_keyword": "namespace",
        "import_keyword": "using",
    },
    "ruby": {
        "extensions": [".rb"],
        "comment_line": "#",
        "comment_block_start": "=begin",
        "comment_block_end": "=end",
        "doc_comment_line": "#",
        "entry_files": ["main.rb", "app.rb", "Gemfile"],
        "module_keyword": "module",
        "import_keyword": "require",
    },
    "php": {
        "extensions": [".php"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/",
        "entry_files": ["index.php", "composer.json"],
        "module_keyword": "namespace",
        "import_keyword": "use",
    },
    "swift": {
        "extensions": [".swift"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "///",
        "entry_files": ["main.swift", "Package.swift"],
        "module_keyword": "class",
        "import_keyword": "import",
    },
    "scala": {
        "extensions": [".scala", ".sc"],
        "comment_line": "//",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "/",
        "entry_files": ["Main.scala", "build.sbt"],
        "module_keyword": "package",
        "import_keyword": "import",
    },
    "hcl": {
        "extensions": [".tf", ".hcl"],
        "comment_line": "#",
        "comment_block_start": "/*",
        "comment_block_end": "*/",
        "doc_comment_line": "#",
        "entry_files": ["main.tf", "variables.tf", "outputs.tf"],
        "module_keyword": "resource",
        "import_keyword": "module",
    },
    "elixir": {
        "extensions": [".ex", ".exs"],
        "comment_line": "#",
        "comment_block_start": "=begin",
        "comment_block_end": "=end",
        "doc_comment_line": "@moduledoc",
        "entry_files": ["mix.exs", "application.ex"],
        "module_keyword": "defmodule",
        "import_keyword": "alias",
    },
}


def detect_language_from_path(file_path: str) -> str:
    """根据文件路径检测语言

    Args:
        file_path: 文件路径

    Returns:
        语言名称（如 "rust", "typescript"），未识别返回 ""
    """
    _, ext = os.path.splitext(file_path)
    ext_lower = ext.lower()
    for lang, config in LANGUAGE_CONFIG.items():
        if ext_lower in config["extensions"]:
            return lang
    return ""


def get_supported_extensions() -> List[str]:
    """获取所有支持的文件扩展名"""
    exts = []
    for config in LANGUAGE_CONFIG.values():
        exts.extend(config["extensions"])
    return exts


def norm_path(path: str) -> str:
    """标准化路径：统一正斜杠 + 去末尾斜杠 + Windows 盘符小写

    消除跨平台和大小写差异，确保同一路径的不同写法产生相同的 hash。
    - 反斜杠 → 正斜杠
    - 去掉末尾斜杠（根目录 "/" 除外）
    - Windows 盘符统一小写（C:\\ 和 c:\\ 等价）
    """
    if not path:
        return path
    # 反斜杠 → 正斜杠
    normalized = path.replace("\\", "/")
    # 去掉末尾斜杠（但保留根目录 "/"）
    if len(normalized) > 1 and normalized.endswith("/"):
        normalized = normalized.rstrip("/")
    # Windows 盘符统一小写（如 C:/ → c:/）
    if len(normalized) >= 2 and normalized[1] == ":" and normalized[0].isalpha():
        normalized = normalized[0].lower() + normalized[1:]
    return normalized


def norm_newlines(text: str) -> str:
    """标准化换行符：CRLF/CR -> LF"""
    return text.replace("\r\n", "\n").replace("\r", "\n")


def compute_content_hash(content: str | bytes) -> str:
    """计算内容 hash（标准化换行符后）"""
    if isinstance(content, bytes):
        try:
            text = content.decode("utf-8", errors="replace")
        except Exception:
            text = content.decode("latin-1", errors="replace")
    else:
        text = content
    normalized = norm_newlines(text)
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def read_file_normalized(file_path: str) -> Tuple[str, str]:
    """读取文件并标准化换行符，返回 (标准化后的内容, hash)"""
    with open(file_path, "rb") as f:
        raw = f.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("latin-1", errors="replace")
    normalized = norm_newlines(text)
    content_hash = compute_content_hash(normalized)
    return normalized, content_hash


def read_file_text(file_path: str, errors: str = "replace") -> str:
    """安全读取文本文件（UTF-8 优先，失败降级 latin-1）

    P23.4: 项目依赖文件（requirements.txt / package.json / Cargo.toml 等）
    可能使用非 UTF-8 编码（如 UTF-16 BOM、GB2312）。直接 open(encoding="utf-8")
    会抛 UnicodeDecodeError 导致整个项目构建失败。本函数自动降级，确保任何
    编码的文件都能读出文本（无效字节用 ? 替换）。

    Args:
        file_path: 文件绝对路径
        errors: 解码错误处理策略（默认 "replace"，用 ? 替换无效字节）

    Returns:
        文件文本内容（换行符已标准化为 LF）
    """
    with open(file_path, "rb") as f:
        raw = f.read()
    # 尝试 UTF-8（含 BOM 自动剥离）
    if raw.startswith(codecs.BOM_UTF8):
        text = raw[3:].decode("utf-8", errors=errors)
    else:
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            # UTF-16 BOM 检测
            if raw.startswith(codecs.BOM_UTF16_LE):
                text = raw[2:].decode("utf-16-le", errors=errors)
            elif raw.startswith(codecs.BOM_UTF16_BE):
                text = raw[2:].decode("utf-16-be", errors=errors)
            else:
                # 降级 latin-1：逐字节映射，不会失败
                text = raw.decode("latin-1", errors=errors)
    return norm_newlines(text)


def to_long_path(path: str) -> str:
    """Windows 长路径支持：添加 \\\\?\\ 前缀绕过 MAX_PATH 260 限制

    P23.6: Java Maven 深层目录路径可能超过 260 字符
    (src/main/java/com/xxx/xxx/...)，Windows 默认不支持。
    添加 \\\\?\\ 前缀后，Windows API 支持最长 32767 字符路径。

    仅在 Windows 且路径长度可能超限时添加。\\\\?\\ 前缀要求：
    - 绝对路径
    - 不含 .. 或 .
    - 反斜杠分隔

    Args:
        path: 原始路径

    Returns:
        可能添加了 \\\\?\\ 前缀的路径
    """
    if os.name != "nt":
        return path
    if not path:
        return path
    # 已有 \\?\ 前缀，不重复添加
    if path.startswith("\\\\?\\"):
        return path
    # 路径未超 260，不需要长路径前缀
    if len(path) < 250:
        return path
    # 必须是绝对路径
    if not os.path.isabs(path):
        return path
    # 标准化为绝对路径，解析 .. 和 .
    abs_path = os.path.abspath(path)
    # 转为反斜杠分隔
    abs_path = abs_path.replace("/", "\\")
    # 添加 \\?\ 前缀
    if abs_path[1] == ":" and abs_path[0].isalpha():
        return "\\\\?\\" + abs_path
    # UNC 路径 \\\\server\\share -> \\\\?\\UNC\\server\\share
    if abs_path.startswith("\\\\"):
        return "\\\\?\\UNC\\" + abs_path[2:]
    return abs_path


def safe_walk(root_dir: str, max_depth: int = -1, **kwargs):
    """带错误处理的 os.walk：跳过不可访问的目录/文件

    P23.5: Windows 下可能遇到文件锁定（WinError 1920）、权限不足、
    路径过长（WinError 3）等错误，os.walk 默认会中断遍历。
    本函数通过 onerror 回调捕获错误并继续遍历。

    Args:
        root_dir: 遍历根目录
        max_depth: 最大递归深度（-1 表示无限制）
        **kwargs: 传递给 os.walk 的额外参数

    Yields:
        (dirpath, dirnames, filenames) 三元组
    """
    _walk_errors = []

    def _onerror(err):
        _walk_errors.append(err)

    root_dir = to_long_path(root_dir)

    for root, dirs, files in os.walk(root_dir, onerror=_onerror, **kwargs):
        # 深度限制
        if max_depth >= 0:
            rel = os.path.relpath(root, root_dir)
            depth = 0 if rel == "." else rel.count(os.sep)
            if depth > max_depth:
                dirs[:] = []
                continue
        yield root, dirs, files


# ── Enterprise daemon 配置 ──

# daemon socket 路径（Linux）
DAEMON_SOCKET_PATH = os.environ.get(
    "CW_DAEMON_SOCKET", "/var/run/callwarden/callwarden.sock"
)

# daemon 数据根目录
DAEMON_DATA_ROOT = os.environ.get("CW_DAEMON_DATA_ROOT", "/var/lib/callwarden")

# workspace registry DB 路径
DAEMON_REGISTRY_DB = os.path.join(DAEMON_DATA_ROOT, "registry.db")

# CAS DB 路径
DAEMON_CAS_DB = os.path.join(DAEMON_DATA_ROOT, "cas.db")

# toolchain DB 路径
DAEMON_TOOLCHAIN_DB = os.path.join(DAEMON_DATA_ROOT, "toolchain.db")

# container mount mapping 默认配置
# 格式: {container_id: {container_path: host_path}}
CONTAINER_MOUNT_MAPPINGS = {}

# daemon 运行模式
# auto: 自动检测（有 daemon 用 daemon，没有用 local）
# enterprise: 强制走 daemon
# local: 强制走本地 SQLite
DAEMON_MODE = os.environ.get("CW_DAEMON_MODE", "auto")


def resolve_container_path(path: str, container_mappings: dict = None) -> str:
    """将容器内路径解析为宿主机路径。

    Args:
        path: 容器内路径（如 /home/user1/work/firmware）
        container_mappings: {container_path_prefix: host_path_prefix}

    Returns:
        宿主机路径
    """
    if container_mappings is None:
        container_mappings = CONTAINER_MOUNT_MAPPINGS

    for container_prefix, host_prefix in container_mappings.items():
        if path.startswith(container_prefix):
            return path.replace(container_prefix, host_prefix, 1)

    return path


def get_daemon_mode() -> str:
    """获取当前 daemon 模式。"""
    return DAEMON_MODE


def is_daemon_required() -> bool:
    """是否强制要求 daemon。"""
    return DAEMON_MODE == "enterprise"


def is_daemon_available() -> bool:
    """检测 daemon 是否可用（socket 是否存在且可连接）。

    Windows 上永远返回 False。
    """
    if os.name == "nt":
        return False
    return os.path.exists(DAEMON_SOCKET_PATH)
