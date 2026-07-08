"""
db_build.py
===========

代码知识图谱构建 Mixin 类。

提供文件扫描、多语言解析、调用图构建、增量更新等功能。
"""

from __future__ import annotations

import os
import pickle
import time
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, Dict, List, Optional, Set

from ..config import (
    norm_path, read_file_normalized,
    detect_language_from_path, get_supported_extensions, compute_content_hash,
)
from ..parsers import RustParser, ModuleResolver, CallResolver, create_parser
from ..cli.console import cprint, print_progress, clear_progress, Spinner, format_duration, print_build_summary
from ..i18n import t


# ============================================
# P15: ProcessPoolExecutor worker 函数（模块级，可 pickle）
# ============================================

# 每个 worker 进程的 parser 缓存（进程级，避免每文件创建）
_worker_parsers: Dict[str, Any] = {}


def _init_worker_parsers():
    """worker 进程初始化：惰性加载 parser（不预加载）。

    在 ProcessPoolExecutor 的 initializer 中调用，每个 worker 进程启动时
    执行一次。空初始化，parser 在 _parse_file_worker 中按需创建并缓存。

    P15 修正：不预加载所有 16 语言（每进程省 ~200MB），改为惰性加载。
    firmware 只有 C/C++，只需加载 2 个 parser，内存占用从 ~300MB 降到 ~80MB。
    """
    global _worker_parsers
    _worker_parsers = {}


def _get_or_create_parser(lang: str, rel_path: str):
    """获取或创建 parser（带进程级缓存）。

   惰性加载：首次遇到某语言时创建 parser 并缓存，后续复用。
    """
    global _worker_parsers
    if lang in _worker_parsers:
        return _worker_parsers[lang]

    # 按需 import + 创建
    from ..parsers import (
        RustParser, TypeScriptParser, PythonParser, KotlinParser,
        GoParser, JavaParser, CParser, CppParser,
        CSharpParser, RubyParser, PhpParser, SwiftParser,
        ScalaParser, HclParser, ElixirParser,
    )
    p = None
    if lang == "rust":
        p = RustParser()
    elif lang == "typescript":
        p = TypeScriptParser(dialect="tsx" if rel_path.endswith(".tsx") else "typescript")
    elif lang == "javascript":
        p = TypeScriptParser(dialect="jsx" if rel_path.endswith(".jsx") else "javascript")
    elif lang == "python":
        p = PythonParser()
    elif lang == "kotlin":
        p = KotlinParser()
    elif lang == "go":
        p = GoParser()
    elif lang == "java":
        p = JavaParser()
    elif lang == "c":
        p = CParser()
    elif lang == "cpp":
        p = CppParser()
    elif lang == "csharp":
        p = CSharpParser()
    elif lang == "ruby":
        p = RubyParser()
    elif lang == "php":
        p = PhpParser()
    elif lang == "swift":
        p = SwiftParser()
    elif lang == "scala":
        p = ScalaParser()
    elif lang == "hcl":
        p = HclParser()
    elif lang == "elixir":
        p = ElixirParser()

    if p is not None:
        _worker_parsers[lang] = p
    return p


def _parse_file_worker(args):
    """多进程 worker：解析单个源文件（模块级函数，可 pickle）。

    在 worker 进程中执行，绕开 GIL 实现真正的并行 parse。
    使用进程级 _worker_parsers 缓存，避免每文件创建 parser。

    Args:
        args: 元组 (rel_path, abs_path, lang, module_path, file_instance_id)

    Returns:
        元组 (status, rel_path, payload)
            - status: "ok" / "fail" / "skip"
            - payload: 成功时为解析结果 dict，失败时为错误字符串，跳过时为 None
    """
    rel_path, abs_path, lang, module_path, file_instance_id = args
    try:
        # P15 安全网：跳过 > 1MB 的源文件（字体/图片数据的 C 数组，非业务代码）
        # tree-sitter parse 大文件时 AST 内存爆炸（8.9MB 文件 → 7GB+ AST）
        # 1MB 的 C 文件约 3 万行，超过此大小的通常是生成的资源文件
        try:
            fsize = os.path.getsize(abs_path)
            if fsize > 1 * 1024 * 1024:  # 1MB
                return ("skip_large", rel_path, fsize)
        except OSError:
            pass

        parser = _get_or_create_parser(lang, rel_path)

        if not parser:
            return ("skip", rel_path, None)

        result = parser.parse_file(abs_path, module_path)
        result["abs_path"] = abs_path
        result["file_instance_id"] = file_instance_id
        result["module_path"] = module_path
        result["rel_path"] = rel_path
        result.setdefault("inline_modules", [])
        return ("ok", rel_path, result)
    except Exception as e:
        return ("fail", rel_path, str(e))


class BuildMixin:
    """构建功能 Mixin

    通过 self.conn 访问数据库连接，提供文件解析和调用图构建功能。
    """

    def build(self):
        """完整构建知识图谱"""
        print(t("cli.messages.db_build_step1_parse_modules"))
        self.module_resolver.resolve_all(self.parser)
        print(t("cli.messages.db_build_modules_found", count=len(self.module_resolver.module_to_file)))

        crate_name = self._detect_crate_name()
        print(t("cli.messages.db_build_crate_name", name=crate_name))

        print(t("cli.messages.db_build_step2_parse_files"))
        file_results = {}
        failed_files = []
        total = len(self.module_resolver.module_to_file)
        for i, (mod_path, rel_path) in enumerate(self.module_resolver.module_to_file.items(), 1):
            abs_path = os.path.join(self.workspace_root, rel_path)
            file_instance_id = self._register_file_db(abs_path, mod_path)
            try:
                result = self.parser.parse_file(abs_path, mod_path)
                result["abs_path"] = abs_path
                result["file_instance_id"] = file_instance_id
                result["module_path"] = mod_path
                result["rel_path"] = norm_path(rel_path)
                result.setdefault("inline_modules", [])
                file_results[norm_path(rel_path)] = result
                print(t("cli.messages.db_build_parse_progress", i=i, total=total, path=rel_path))
            except Exception as e:
                failed_files.append((rel_path, str(e)))
                print(t("cli.messages.db_build_parse_fail", i=i, total=total, path=rel_path, error=e))

        print(t("cli.messages.db_build_parsed_count", count=len(file_results)), end="")
        if failed_files:
            print(t("cli.messages.db_build_failed_count", count=len(failed_files)))
        else:
            print()

        print(t("cli.messages.db_build_step3_versions"))
        for rel_path, result in file_results.items():
            file_version_id = self._save_file_version(result["file_instance_id"], result)
            result["file_version_id"] = file_version_id
            self._save_symbols_for_version(file_version_id, result["file_instance_id"], result)

        print(t("cli.messages.db_build_step4_calls"))
        self.build_call_graph(file_results, crate_name)

        print(t("cli.messages.db_build_step5_depth"))
        self._build_depth()

        print(t("cli.messages.db_build_step6_update_depth"))
        self._update_symbol_version_depths()

        self.conn.commit()

        if failed_files:
            print(t("cli.messages.db_build_done_failures", count=len(failed_files)))
        else:
            print(t("cli.messages.db_build_done"))


    def build_full_graph(self, force: bool = False):
        """构建完整知识图谱（自动检测所有支持的语言）

        Args:
            force: 是否强制重新解析所有文件（忽略增量）
        """
        # 扫描所有支持的文件，统一走多语言构建流程（支持增量）
        files = self._scan_supported_files()
        self._build_multi_lang(files, force=force)


    def build_directory(self, dir_path: str):
        """构建指定目录的知识图谱

        Args:
            dir_path: 相对项目根目录的路径（如 "src/cli"）或绝对路径
        """
        # 标准化路径
        abs_dir = os.path.abspath(dir_path)
        if not os.path.isabs(dir_path):
            abs_dir = os.path.join(self.workspace_root, dir_path)

        if not os.path.isdir(abs_dir):
            print(t("cli.messages.db_build_dir_not_exist", path=abs_dir))
            return

        # 扫描该目录下的源文件
        supported_extensions = set(get_supported_extensions())
        skip_dirs = {".git", "node_modules", "target", "dist", "build", ".next", "__pycache__"}
        files = []
        for root, dirs, filenames in os.walk(abs_dir):
            dirs[:] = [d for d in dirs if d not in skip_dirs and not d.startswith(".")]
            for filename in filenames:
                ext = os.path.splitext(filename)[1].lower()
                if ext in supported_extensions:
                    abs_path = os.path.join(root, filename)
                    rel_path = norm_path(os.path.relpath(abs_path, self.workspace_root))
                    files.append(rel_path)

        files.sort()
        if not files:
            print(t("cli.messages.db_build_no_source_files", path=dir_path))
            return

        rust_files = [f for f in files if f.endswith(".rs")]
        other_files = [f for f in files if not f.endswith(".rs")]

        print(t("cli.messages.db_build_build_dir", path=dir_path, count=len(files)))
        if other_files or len(rust_files) != len(files):
            self._build_multi_lang(files)
        else:
            # 纯 Rust 目录，也走多语言流程以保持一致
            self._build_multi_lang(files)


    def _load_ignore_patterns(self) -> List[str]:
        """加载 .callwardenignore 规则

        规则格式（类似 .gitignore）：
        - 每行一个模式
        - # 开头是注释
        - 支持通配符：* 匹配任意字符，** 匹配任意目录
        - 目录名后加 / 只匹配目录
        - 以 / 开头匹配根目录
        """
        import fnmatch

        patterns = []
        ignore_file = os.path.join(self.workspace_root, ".callwardenignore")

        # 默认忽略规则（硬编码基线，覆盖 VCS/包管理/构建输出/预构建/autogen）
        # 这些规则对 repo manifest/AOSP/嵌入式项目尤其关键——
        # autogen 代码和预构建产物动辄几十万文件，不排除会让 DB 爆掉
        default_ignores = [
            # === VCS / 包管理 / Python 虚拟环境 ===
            ".git/", "node_modules/", ".next/",
            "__pycache__/", ".venv/", "venv/", "env/", ".tox/", "*.egg-info/",
            # === 构建输出目录（AOSP/嵌入式/Make/Cargo）===
            # 这些目录包含编译中间产物和 autogen 源码，体积巨大且无分析价值
            "target/", "dist/", "build/", "out/", "output/", "outputs/",
            "obj/", "bin/", "rootfs/", "staging/", "sysroot/", "ccache/",
            # === 预构建 / 二进制 / 工具链 ===
            # prebuilt/prebuilts/blob 是厂商二进制，toolchain/ndk/jdk 是工具链
            "prebuilt/", "prebuilts/", "blob/", "toolchain/", "toolchains/",
            "ndk/", "jdk/",
            # === 第三方依赖源码 ===
            # thirdParty/third_party/vendor 是常见第三方库存放目录
            # 包含大量外部代码，非项目业务逻辑，分析价值低且会显著增加 DB 体积
            "thirdParty/", "third_party/", "vendor/",
            # === autogen 代码目录（核心痛点）===
            # 这些目录存放 protobuf/grpc/Qt moc 等自动生成的源码
            # 扩展名是 .c/.cpp/.py，会被扫描器拾取，但内容无人工维护语义
            "autogen/", "auto_gen/", "generated/", "gen/", "generated_src/",
            "proto_gen/", "protobuf_gen/", "grpc_gen/", "moc/",
            # === autogen 文件名模式（protobuf / Qt / Python 字节码）===
            "*.pb.cc", "*.pb.h", "*.pb.go",           # protobuf C++/Go 生成
            "*_pb2.py", "*_pb2.pyi", "*_pb2_grpc.py",  # protobuf Python 生成
            "*.grpc.cc", "*.grpc.h",                   # grpc C++ 生成
            "moc_*.cpp", "ui_*.h", "qrc_*.cpp",        # Qt moc/ui 资源生成
            "*.pyc", "*.pyo",                           # Python 字节码
            # === repo 工具元数据（AOSP repo manifest 项目）===
            ".repo/",
        ]

        if os.path.exists(ignore_file):
            try:
                with open(ignore_file, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#"):
                            patterns.append(line)
            except Exception:
                pass

        # 合并默认规则和用户规则
        all_patterns = default_ignores + patterns
        return all_patterns


    def _should_ignore(self, rel_path: str, is_dir: bool, patterns: List[str]) -> bool:
        """判断路径是否应该被忽略

        Args:
            rel_path: 相对路径（使用 / 分隔符）
            is_dir: 是否是目录
            patterns: 忽略规则列表
        """
        import fnmatch

        path_parts = rel_path.split("/")

        for pattern in patterns:
            orig_pattern = pattern
            p = pattern.rstrip("/")
            match_dir_only = pattern.endswith("/")

            # 以 / 开头：只匹配根目录
            if p.startswith("/"):
                p = p[1:]
                if fnmatch.fnmatch(rel_path, p):
                    return True
                if is_dir and fnmatch.fnmatch(rel_path + "/", p + "/"):
                    return True
                continue

            # 包含 /：匹配完整路径
            if "/" in p:
                if fnmatch.fnmatch(rel_path, p):
                    return True
                continue

            # 不包含 /：匹配任意层级的文件名/目录名
            if match_dir_only:
                if is_dir and path_parts[-1] == p:
                    return True
            else:
                if fnmatch.fnmatch(path_parts[-1], p):
                    return True
                # 也检查是否匹配完整路径的任意后缀
                for i in range(len(path_parts)):
                    subpath = "/".join(path_parts[i:])
                    if fnmatch.fnmatch(subpath, p):
                        return True

        return False


    def _scan_supported_files(self) -> List[str]:
        """扫描项目中所有支持的源文件（尊重 .callwardenignore）"""
        supported_extensions = set(get_supported_extensions())
        files = []
        ignore_patterns = self._load_ignore_patterns()

        for root, dirs, filenames in os.walk(self.workspace_root):
            # 过滤目录：原地修改 dirs 以跳过
            rel_root = norm_path(os.path.relpath(root, self.workspace_root))
            if rel_root == ".":
                rel_root = ""

            # 过滤要跳过的目录
            dirs_to_keep = []
            for d in dirs:
                if d.startswith(".") and d not in (".codegraph",):
                    continue
                d_rel = (rel_root + "/" + d) if rel_root else d
                if not self._should_ignore(d_rel, True, ignore_patterns):
                    dirs_to_keep.append(d)
            dirs[:] = dirs_to_keep

            for filename in filenames:
                ext = os.path.splitext(filename)[1].lower()
                if ext in supported_extensions:
                    abs_path = os.path.join(root, filename)
                    rel_path = norm_path(os.path.relpath(abs_path, self.workspace_root))
                    if not self._should_ignore(rel_path, False, ignore_patterns):
                        files.append(rel_path)

        return sorted(files)


    def _detect_repo_manifest(self) -> int:
        """检测 repo manifest 并注册子仓库为 workspace

        AOSP/嵌入式项目用 `repo` 工具管理多仓库，根目录有 `.repo/` 元数据。
        本方法解析 manifest.xml，把每个 <project> 注册为独立 workspace 记录，
        让 list_workspaces / 跨仓库分析能识别子仓库边界。

        注册的 workspace：
        - name: manifest 的 name 属性（如 "firmware/middleware"）
        - root_path: workspace_root + "/" + path 属性
        - description: "repo subproject: <remote>/<name>"

        Returns:
            注册的子仓库数量（已存在的跳过）
        """
        import xml.etree.ElementTree as ET

        repo_dir = os.path.join(self.workspace_root, ".repo")
        if not os.path.isdir(repo_dir):
            return 0

        # 定位 manifest.xml：优先 .repo/manifests/<default>.xml，回退到根目录
        manifest_path = None
        manifests_dir = os.path.join(repo_dir, "manifests")
        if os.path.isdir(manifests_dir):
            # 找 default.xml 或任意 .xml
            for candidate in ("default.xml", "manifest.xml"):
                p = os.path.join(manifests_dir, candidate)
                if os.path.isfile(p):
                    manifest_path = p
                    break
            if not manifest_path:
                # 找目录里第一个 .xml
                for fn in os.listdir(manifests_dir):
                    if fn.endswith(".xml"):
                        manifest_path = os.path.join(manifests_dir, fn)
                        break
        if not manifest_path:
            # 回退：根目录的 manifest.xml
            root_manifest = os.path.join(self.workspace_root, "manifest.xml")
            if os.path.isfile(root_manifest):
                manifest_path = root_manifest

        if not manifest_path:
            return 0

        try:
            tree = ET.parse(manifest_path)
            root_elem = tree.getroot()
        except Exception:
            return 0

        registered = 0
        for proj in root_elem.findall("project"):
            name = proj.get("name", "")
            path = proj.get("path", name)
            remote = proj.get("remote", "")
            if not name:
                continue
            # 子仓库的绝对路径
            sub_abs = os.path.join(self.workspace_root, path)
            if not os.path.isdir(sub_abs):
                continue
            # 注册为 workspace（register_workspace 内部已做去重）
            desc = f"repo subproject: {remote}/{name}" if remote else f"repo subproject: {name}"
            self.register_workspace(name, sub_abs, description=desc)
            registered += 1

        return registered


    def _build_multi_lang(self, files: List[str], force: bool = False):
        """多语言通用构建流程（支持并行解析）

        Args:
            files: 相对路径列表
            force: 是否强制重新解析所有文件（忽略增量）
        """
        t_start = time.time()
        total = len(files)

        # 检测 repo manifest，注册子仓库为 workspace（让跨仓库分析能识别边界）
        subrepo_count = self._detect_repo_manifest()
        if subrepo_count > 0:
            cprint(t("cli.messages.db_build_repo_manifest", count=subrepo_count), "dim")

        cprint(t("cli.messages.db_build_step1_5_scan", count=total), "cyan", bold=True)

        cprint(t("cli.messages.db_build_step2_5_parse"), "cyan", bold=True)
        file_results = {}
        skipped = 0
        unchanged = 0
        failed = 0

        to_parse = []
        parsed_new = 0  # P11: 初始化为 0，用于 GC 条件化判断

        # P10: 细拆 register 阶段计时（逐文件 SQL: _register_file_db + _get_file_version）
        t_register_start = time.perf_counter()
        for i, rel_path in enumerate(files, 1):
            abs_path = os.path.join(self.workspace_root, rel_path)
            lang = detect_language_from_path(rel_path)
            parser = create_parser(rel_path)

            if not parser:
                skipped += 1
                continue

            module_path = self._infer_module_path_generic(rel_path, lang)
            file_instance_id = self._register_file_db(abs_path, module_path)

            if not force:
                current_mtime = os.path.getmtime(abs_path)
                latest_fv = self._get_file_version(file_instance_id)
                if latest_fv and abs(latest_fv["mtime"] - current_mtime) < 0.001:
                    old_result = self._load_file_result_from_db(file_instance_id, latest_fv["id"], rel_path, abs_path, module_path)
                    if old_result:
                        file_results[rel_path] = old_result
                        unchanged += 1
                        continue

            to_parse.append((i, rel_path, abs_path, lang, module_path, file_instance_id))
        t_register = time.perf_counter() - t_register_start

        if unchanged > 0 and not to_parse:
            cprint(t("cli.messages.db_build_all_unchanged", count=unchanged), "green")
            duration = time.time() - t_start
            cprint()
            cprint(t("cli.messages.db_build_summary_title"), "cyan", bold=True)
            cprint()
            cprint(t("cli.messages.db_build_summary_files"), "bold")
            cprint(t("cli.messages.db_build_summary_unchanged", count=unchanged), "dim")
            cprint()
            cprint(t("cli.messages.db_build_summary_duration", duration=format_duration(duration)), "yellow")
            cprint(t("cli.messages.db_build_summary_done"), "green")
            cprint()
            # P10: even in all-unchanged path, show register timing
            cprint("── 阶段耗时分解 ──────────────────────", "cyan", bold=True)
            cprint(f"  register (逐文件SQL)    : {t_register:8.2f}s  (注册/mtime/version 查询)", "dim")
            cprint(f"  total                   : {duration:8.2f}s", "cyan", bold=True)
            cprint("──────────────────────────────────────", "cyan")
            # P13: early return 路径也暴露 _stage_timings（供 perf test 做基线对比）
            self._stage_timings = {
                "register": t_register,
                "parse": 0,
                "symbol_write": 0,
                "stdlib_import": 0,
                "call_resolve_write": 0,
                "depth": 0,
                "fts_rebuild": 0,
                "commit": 0,
                "gc_archive": 0,
                "total": duration,
                "files_total": unchanged + skipped,
                "files_parsed": 0,
                "files_unchanged": unchanged,
            }
            return

        t_parse_start = time.perf_counter()
        if to_parse:
            parse_total = len(to_parse)
            failed_files = []

            # P15: 文件数超过阈值时用 ProcessPoolExecutor（绕开 GIL 真正并行 parse）
            # tree-sitter Parser.parse() 不释放 GIL，ThreadPoolExecutor 实际只有 2x 加速
            # ProcessPoolExecutor 用独立进程，每个进程有自己的 GIL，可真正 N 核并行
            #
            # 内存预算：每个 worker 进程约 200-400MB（Python + 16 语言 tree-sitter grammar）
            # max_workers 限制为 4，避免内存爆炸（4 × 400MB = 1.6GB 上限）
            # 在 22 核机器上只用 4 核，但避免把宿主机搞崩溃
            MP_THRESHOLD = 50  # 文件数 >= 50 才用多进程（避免进程创建开销）
            use_multiprocess = len(to_parse) >= MP_THRESHOLD

            if use_multiprocess:
                # 多进程路径：限制 worker 数，控制内存
                # 每进程惰性加载 parser（仅实际用到的语言），约 80-150MB/进程
                # 默认 min(4, cpu-1)，可用 CW_MP_WORKERS 环境变量覆盖
                env_workers = os.environ.get("CW_MP_WORKERS")
                if env_workers:
                    mp_workers = max(1, min(8, int(env_workers)))
                else:
                    mp_workers = min(4, max(1, (os.cpu_count() or 4) - 1))
                cprint(t("cli.messages.db_build_parallel_parse", workers=mp_workers, count=len(to_parse)), "dim")
                from concurrent.futures import ProcessPoolExecutor, as_completed
                # worker 参数：(rel_path, abs_path, lang, module_path, file_instance_id)
                mp_args = [(rel_path, abs_path, lang, module_path, file_instance_id)
                           for _, rel_path, abs_path, lang, module_path, file_instance_id in to_parse]
                # chunksize: 每批处理多少文件，减少 IPC 开销
                chunksize = max(1, len(mp_args) // (mp_workers * 4))
                cprint(f"  (P15 multiprocess: {mp_workers} workers, chunksize={chunksize})", "dim")

                try:
                    with ProcessPoolExecutor(
                        max_workers=mp_workers,
                        initializer=_init_worker_parsers,
                    ) as pool:
                        done_count = 0
                        for status, rel_path, payload in pool.map(
                            _parse_file_worker, mp_args, chunksize=chunksize
                        ):
                            if status == "ok":
                                file_results[rel_path] = payload
                            elif status == "fail":
                                failed += 1
                                failed_files.append((rel_path, payload))
                            elif status == "skip_large":
                                skipped += 1
                                # P15: 大文件跳过提示（字体/图片数据的 C 数组）
                                fsize_mb = payload / (1024 * 1024)
                                cprint(f"  ⚠ 跳过超大文件 ({fsize_mb:.1f}MB): {rel_path}", "yellow")
                            else:
                                skipped += 1
                            done_count += 1
                            print_progress(done_count, parse_total,
                                           t("cli.messages.db_build_parse_progress_lang", path=rel_path, lang=""))
                except ( pickle.PickleError, BrokenPipeError, OSError) as e:
                    # fallback: 进程创建/pickle 失败时降级为 ThreadPoolExecutor
                    cprint(f"  (P15 fallback to ThreadPool: {e})", "yellow")
                    use_multiprocess = False

            if not use_multiprocess:
                # 原多线程路径（小文件量或 fallback）
                # 线程比进程轻量，8 线程内存占用远低于 4 进程
                max_workers = min(8, max(1, (os.cpu_count() or 4) - 1))
                cprint(t("cli.messages.db_build_parallel_parse", workers=max_workers, count=len(to_parse)), "dim")
                print_lock = threading.Lock()
                done_count = [0]

                def _parse_one(args):
                    """多线程工作函数：解析单个源文件并返回结果元组"""
                    _, rel_path, abs_path, lang, module_path, file_instance_id = args
                    try:
                        from ..parsers import (
                            RustParser, TypeScriptParser, PythonParser, KotlinParser,
                            GoParser, JavaParser, CParser, CppParser,
                            CSharpParser, RubyParser, PhpParser, SwiftParser,
                            ScalaParser, HclParser, ElixirParser,
                        )
                        if lang == "rust":
                            p = RustParser()
                        elif lang == "typescript":
                            p = TypeScriptParser(dialect="tsx" if rel_path.endswith(".tsx") else "typescript")
                        elif lang == "javascript":
                            p = TypeScriptParser(dialect="jsx" if rel_path.endswith(".jsx") else "javascript")
                        elif lang == "python":
                            p = PythonParser()
                        elif lang == "kotlin":
                            p = KotlinParser()
                        elif lang == "go":
                            p = GoParser()
                        elif lang == "java":
                            p = JavaParser()
                        elif lang == "c":
                            p = CParser()
                        elif lang == "cpp":
                            p = CppParser()
                        elif lang == "csharp":
                            p = CSharpParser()
                        elif lang == "ruby":
                            p = RubyParser()
                        elif lang == "php":
                            p = PhpParser()
                        elif lang == "swift":
                            p = SwiftParser()
                        elif lang == "scala":
                            p = ScalaParser()
                        elif lang == "hcl":
                            p = HclParser()
                        elif lang == "elixir":
                            p = ElixirParser()
                        else:
                            with print_lock:
                                done_count[0] += 1
                            return ("skip", rel_path, None)
                        result = p.parse_file(abs_path, module_path)
                        result["abs_path"] = abs_path
                        result["file_instance_id"] = file_instance_id
                        result["module_path"] = module_path
                        result["rel_path"] = rel_path
                        result.setdefault("inline_modules", [])
                        with print_lock:
                            done_count[0] += 1
                            print_progress(done_count[0], parse_total, t("cli.messages.db_build_parse_progress_lang", path=rel_path, lang=lang))
                        return ("ok", rel_path, result)
                    except Exception as e:
                        with print_lock:
                            done_count[0] += 1
                        return ("fail", rel_path, str(e))

                with ThreadPoolExecutor(max_workers=max_workers) as pool:
                    for status, rel_path, payload in pool.map(_parse_one, to_parse):
                        if status == "ok":
                            file_results[rel_path] = payload
                        elif status == "fail":
                            failed += 1
                            failed_files.append((rel_path, payload))
                        else:
                            skipped += 1

            clear_progress()
            parsed_new = len(file_results) - unchanged
            if failed == 0:
                cprint(t("cli.messages.db_build_parse_ok", parsed=parsed_new, unchanged=unchanged, skipped=skipped), "green")
            else:
                cprint(t("cli.messages.db_build_parse_warn", parsed=parsed_new, unchanged=unchanged, skipped=skipped, failed=failed), "yellow")
                for rel_path, err in failed_files:
                    cprint(t("cli.messages.db_build_parse_fail_item", path=rel_path, error=err), "red")

        t_parse = time.perf_counter() - t_parse_start

        spinner = Spinner(t("cli.messages.db_build_step3_5_versions"))
        spinner.start()
        # P8 优化：full build 期间禁用 FTS 触发器，避免每个 symbol INSERT/UPDATE
        # 都同步维护 trigram FTS 索引（写放大）。批量写完后一次性 rebuild。
        fts_was_disabled = self._disable_fts_triggers()
        t_versions_start = time.perf_counter()
        version_count = 0
        for rel_path, result in file_results.items():
            if result.get("_from_db"):
                self._restore_symbol_snapshots(result["file_instance_id"], result)
                continue
            file_version_id = self._save_file_version(result["file_instance_id"], result)
            result["file_version_id"] = file_version_id
            self._save_symbols_for_version(file_version_id, result["file_instance_id"], result)
            version_count += 1
        spinner.stop(t("cli.messages.db_build_versions_written", count=version_count))
        t_versions = time.perf_counter() - t_versions_start

        spinner = Spinner(t("cli.messages.db_build_step4_5_calls"))
        spinner.start()
        # 导入所有支持语言的标准库符号（Python + Rust/Java/Go/C/C++/C#/TS/Kotlin/Ruby/Swift/Scala/Elixir/PHP）
        # 这一步在构建调用图之前完成，确保后续 callee 匹配能命中标准库符号
        t_stdlib_start = time.perf_counter()
        self.import_all_stdlib_symbols()
        self.import_project_dependencies()
        t_stdlib = time.perf_counter() - t_stdlib_start

        t_call_resolve_start = time.perf_counter()
        self._build_call_graph_multi_lang(file_results)
        t_call_resolve = time.perf_counter() - t_call_resolve_start

        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute("SELECT COUNT(*) as c FROM calls c JOIN symbols s ON c.caller_id = s.id JOIN file_instances fi ON s.file_instance_id = fi.id WHERE fi.workspace_id = ?", (ws_id,))
        total_calls = cur.fetchone()["c"]
        cur = self.conn.execute("SELECT COUNT(*) as c FROM calls c JOIN symbols s ON c.caller_id = s.id JOIN file_instances fi ON s.file_instance_id = fi.id WHERE fi.workspace_id = ? AND c.callee_id IS NOT NULL", (ws_id,))
        resolved_calls = cur.fetchone()["c"]
        spinner.stop(t("cli.messages.db_build_calls_count", total=total_calls, resolved=resolved_calls))

        spinner = Spinner(t("cli.messages.db_build_step5_5_depth"))
        spinner.start()
        t_depth_start = time.perf_counter()
        self._build_depth()
        self._update_symbol_version_depths()
        t_depth = time.perf_counter() - t_depth_start
        spinner.stop(t("cli.messages.db_build_depth_done"))

        # P8: 批量写完后一次性重建 FTS 索引 + 重新启用触发器
        t_fts_start = time.perf_counter()
        if fts_was_disabled:
            self._rebuild_and_enable_fts()
        t_fts = time.perf_counter() - t_fts_start

        t_commit_start = time.perf_counter()
        self.conn.commit()
        t_commit = time.perf_counter() - t_commit_start

        # 步骤 6/6: GC 归档（类 Java Young GC，扫描 pending 文件命中 ignore 的迁入 archived_files）
        # 注意：在 commit 之后执行，避免归档事务与构建事务冲突
        # P11: 条件化 GC —— 无新解析文件时跳过（避免 os.walk 全仓遍历构建 matcher）
        # parsed_new 是实际解析（非 unchanged）的文件数；为 0 说明全部走增量，不需要 GC
        t_gc_start = time.perf_counter()
        try:
            if parsed_new > 0:
                gc_result = self.gc_archive(force=False)
                if gc_result["archived"] > 0:
                    cprint(t("cli.messages.db_build_step6_gc", count=gc_result['archived']), "yellow")
                    for reason, count in gc_result["reasons"].items():
                        cprint(t("cli.messages.db_build_gc_reason", reason=reason, count=count), "dim")
            # P11: 无新解析文件时跳过 GC，matcher 不构建、os.walk 不执行
        except Exception as e:
            # GC 失败不阻塞构建
            cprint(t("cli.messages.db_build_gc_fail", error=e), "yellow")
        t_gc = time.perf_counter() - t_gc_start

        duration = time.time() - t_start

        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute("""
            SELECT COUNT(*) as cnt FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
        """, (ws_id,))
        total_symbols = cur.fetchone()["cnt"]

        parsed_new = len(file_results) - unchanged
        print_build_summary(parsed_new, unchanged, skipped, failed, total_symbols, total_calls, resolved_calls, duration)

        # 阶段计时汇总（用于定位性能瓶颈）
        # P10: 细拆 scan+parse+register → register（逐文件 SQL） + parse（tree-sitter）
        t_other = duration - (t_register + t_parse + t_versions + t_stdlib + t_call_resolve + t_depth + t_fts + t_commit + t_gc)
        cprint()
        cprint("── 阶段耗时分解 ──────────────────────", "cyan", bold=True)
        cprint(f"  register (逐文件SQL)    : {t_register:8.2f}s  (注册/mtime/version 查询)", "dim")
        cprint(f"  parse (tree-sitter)     : {t_parse:8.2f}s  (多线程源码解析)", "dim")
        cprint(f"  symbol write            : {t_versions:8.2f}s  (写入符号/版本)", "dim")
        cprint(f"  stdlib import           : {t_stdlib:8.2f}s  (标准库符号导入)", "dim")
        cprint(f"  call resolve + write    : {t_call_resolve:8.2f}s  (调用关系解析+写入)", "yellow")
        cprint(f"  depth                   : {t_depth:8.2f}s  (拓扑深度计算)", "dim")
        cprint(f"  fts rebuild             : {t_fts:8.2f}s  (FTS5 索引重建)", "dim")
        cprint(f"  commit                  : {t_commit:8.2f}s  (事务提交)", "dim")
        cprint(f"  gc archive              : {t_gc:8.2f}s  (GC 归档)", "dim")
        if t_other > 0.01:
            cprint(f"  other                   : {t_other:8.2f}s  (其他开销)", "dim")
        cprint(f"  total                   : {duration:8.2f}s", "cyan", bold=True)
        cprint("──────────────────────────────────────", "cyan")

        # P13: 暴露阶段耗时数据到实例，供 perf test 读取做回归基线对比
        self._stage_timings = {
            "register": t_register,
            "parse": t_parse,
            "symbol_write": t_versions,
            "stdlib_import": t_stdlib,
            "call_resolve_write": t_call_resolve,
            "depth": t_depth,
            "fts_rebuild": t_fts,
            "commit": t_commit,
            "gc_archive": t_gc,
            "total": duration,
            "files_total": total,
            "files_parsed": parsed_new,
            "files_unchanged": unchanged,
        }
    

    # ---- P8: FTS5 触发器延后重建 ----

    def _disable_fts_triggers(self) -> bool:
        """禁用 symbols_fts 的 3 个同步触发器，返回是否成功禁用。

        在 full build 期间禁用触发器，避免每个 symbol INSERT/UPDATE 都同步维护
        trigram FTS 索引（写放大）。批量写完后调用 _rebuild_and_enable_fts() 重建。

        Returns:
            True 表示触发了禁用（需要后续重建），False 表示 FTS 表不存在或已禁用
        """
        try:
            # 检查 symbols_fts 表是否存在
            cur = self.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='symbols_fts'"
            )
            if not cur.fetchone():
                return False
            # DROP 3 个触发器（IF EXISTS 确保幂等）
            self.conn.execute("DROP TRIGGER IF EXISTS symbols_fts_ai")
            self.conn.execute("DROP TRIGGER IF EXISTS symbols_fts_ad")
            self.conn.execute("DROP TRIGGER IF EXISTS symbols_fts_au")
            return True
        except Exception:
            return False

    def _rebuild_and_enable_fts(self):
        """一次性重建 FTS5 索引并重新启用同步触发器。

        在 _disable_fts_triggers() 之后调用，确保 FTS 索引与 symbols 表一致。
        """
        try:
            # 重建 FTS 索引（从 symbols 表重新构建全文索引）
            self.conn.execute("INSERT INTO symbols_fts(symbols_fts) VALUES('rebuild')")
            # 重新创建同步触发器
            self.conn.execute("""
                CREATE TRIGGER IF NOT EXISTS symbols_fts_ai AFTER INSERT ON symbols BEGIN
                    INSERT INTO symbols_fts(rowid, name, qualified_name)
                    VALUES (new.id, new.name, new.qualified_name);
                END
            """)
            self.conn.execute("""
                CREATE TRIGGER IF NOT EXISTS symbols_fts_ad AFTER DELETE ON symbols BEGIN
                    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name)
                    VALUES ('delete', old.id, old.name, old.qualified_name);
                END
            """)
            self.conn.execute("""
                CREATE TRIGGER IF NOT EXISTS symbols_fts_au AFTER UPDATE ON symbols BEGIN
                    INSERT INTO symbols_fts(symbols_fts, rowid, name, qualified_name)
                    VALUES ('delete', old.id, old.name, old.qualified_name);
                    INSERT INTO symbols_fts(rowid, name, qualified_name)
                    VALUES (new.id, new.name, new.qualified_name);
                END
            """)
        except Exception:
            pass

    def _load_file_result_from_db(self, file_instance_id: int, file_version_id: int,
        rel_path: str, abs_path: str, module_path: str) -> Optional[Dict]:
        """从数据库加载已解析的文件结果（增量构建用）"""
        try:
            # 加载文件版本的基本信息
            cur = self.conn.execute(
                "SELECT content_hash, total_lines FROM file_versions WHERE id = ?",
                (file_version_id,)
            )
            fv_row = cur.fetchone()
            if not fv_row:
                return None

            # 加载该版本的所有符号（含 symbol_contents 详情）
            cur = self.conn.execute("""
                SELECT sv.id, sv.symbol_hash, sv.qualified_name, sv.start_line, sv.end_line,
                       sv.module_path, sv.depth, sv.is_deleted,
                       sc.name, sc.kind, sc.content, sc.signature, sc.has_comment,
                       sc.comment_content as doc_comment
                FROM file_symbol_versions sv
                JOIN symbol_contents sc ON sv.symbol_hash = sc.content_hash
                WHERE sv.file_version_id = ? AND sv.is_deleted = 0
            """, (file_version_id,))
            symbols = []
            for row in cur:
                sym = dict(row)
                # 从DB加载的符号没有 raw_calls（调用点信息），需要重新解析文件才能获取
                # 但对于符号索引来说已经足够
                sym["calls"] = []  # 调用点信息不在DB中，增量时其他文件的调用点来自call_versions
                sym.setdefault("issues", [])
                symbols.append(sym)

            # 从快照表加载调用关系（通过 symbols 表关联 file_instance_id）
            cur = self.conn.execute("""
                SELECT c.caller_name, c.caller_module, c.callee_name, c.callee_module,
                       c.callee_qualified, c.callee_file, c.callee_id, c.call_line, c.is_cross_file
                FROM calls c
                JOIN symbols s ON c.caller_id = s.id
                WHERE s.file_instance_id = ?
            """, (file_instance_id,))
            raw_calls = [dict(row) for row in cur]

            return {
                "abs_path": abs_path,
                "rel_path": rel_path,
                "module_path": module_path,
                "file_instance_id": file_instance_id,
                "file_version_id": file_version_id,
                "symbols": symbols,
                "raw_calls": raw_calls,
                "imports": [],  # imports 不存储，但不影响符号索引
                "content_hash": fv_row["content_hash"],
                "total_lines": fv_row["total_lines"],
                "inline_modules": [],
                "_from_db": True,  # 标记来自DB（跳过写入）
            }
        except Exception as e:
            return None


    def _restore_symbol_snapshots(self, file_instance_id: int, result: Dict):
        """从DB加载的结果恢复符号快照到 symbols 表（增量构建用）

        如果快照已存在则跳过，否则从 symbol_contents 和 file_symbol_versions 恢复。
        """
        # 检查是否已有快照
        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM symbols WHERE file_instance_id = ?",
            (file_instance_id,)
        )
        if cur.fetchone()["cnt"] > 0:
            return  # 已有快照，不需要恢复

        # 从 file_symbol_versions + symbol_contents 恢复
        for sym in result.get("symbols", []):
            qname = sym.get("qualified_name", "")
            name = sym.get("name", "")
            kind = sym.get("kind", "fn")
            start_line = sym.get("start_line", 0)
            end_line = sym.get("end_line", 0)
            module_path = sym.get("module_path", result.get("module_path", ""))
            content_hash = sym.get("symbol_hash", "")
            depth = sym.get("depth", -1)
            has_comment = sym.get("has_comment", 0)

            self.conn.execute("""
                INSERT OR IGNORE INTO symbols
                (file_instance_id, name, kind, qualified_name, module_path,
                 start_line, end_line, content_hash, depth, has_comment)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (file_instance_id, name, kind, qname, module_path,
                  start_line, end_line, content_hash, depth, has_comment))


    def _build_call_graph_multi_lang(self, file_results: Dict[str, Dict[str, Any]]):
        """多语言调用关系构建（多级解析策略）

        解析策略（按优先级）：
        1. 精确匹配：callee_module.callee_name 完全匹配 qualified_name
        2. import 解析：通过文件的 import 列表将 callee_module 映射到实际模块路径
        3. 简名唯一匹配：callee_name 在全局符号表中唯一存在
        4. 简名同文件匹配：callee_name 在当前文件中存在

        性能优化（P0/P3）：
        - 后缀反向索引：策略 2/4 的后缀匹配从 O(M*N) 优化为 O(M*K)
        - external_symbols 批量加载：策略 5 从 M 次 DB 查询优化为 1 次批量加载
        """
        total_calls = 0
        resolved_count = 0

        # ---- 第一阶段：构建符号索引 + 后缀反向索引 ----
        # qualified_name -> {file, symbol}
        all_symbols_map: Dict[str, Dict] = {}
        # 简名 -> [qualified_name, ...]（用于 fallback 匹配）
        name_index: Dict[str, List[str]] = defaultdict(list)
        # 每个文件的符号简名集合（用于同文件匹配）
        file_symbols: Dict[str, Set[str]] = defaultdict(set)
        # P0 优化：后缀反向索引
        # 后缀（含前导点，如 ".module.name"）-> [qualified_name, ...]
        # 用于策略 2/4 的后缀匹配，从 O(N) 遍历优化为 O(K) 索引查找
        # 内存开销：平均每 qname 4 段 → 4 个索引项，20 万符号约 80 万项/40MB
        suffix_index: Dict[str, List[str]] = defaultdict(list)

        for rel_path, result in file_results.items():
            for sym in result.get("symbols", []):
                qname = sym.get("qualified_name", "")
                if not qname:
                    continue
                all_symbols_map[qname] = {"file": rel_path, "symbol": sym}
                # 简名 = qualified_name 的最后一段（支持 . 和 :: 分隔符）
                norm_qname = qname.replace("::", ".")
                parts = norm_qname.rsplit(".", 1)
                simple_name = parts[-1] if parts else qname
                name_index[simple_name].append(qname)
                file_symbols[rel_path].add(simple_name)
                # P0：构建后缀索引（含前导点，与原 endswith 语义一致）
                # 例如 "com.foo.Bar.method" → 索引 ".method", ".Bar.method", ".foo.Bar.method", ".com.foo.Bar.method"
                norm_parts = norm_qname.split(".")
                for i in range(len(norm_parts)):
                    suffix = "." + ".".join(norm_parts[i:])
                    suffix_index[suffix].append(qname)

        # ---- P3+P7 合并：批量加载 external_symbols 到内存 + 构建 qname_id_map ----
        # 策略 5 原来对每个未解析调用执行 DB 查询，改为内存查找
        # P7: 同时构建 qname_id_map（避免第二次全表扫描 external_symbols）
        # 优化：只加载解析需要的 4 列（id, symbol_name, qualified_name, package_name），
        #       跳过 package_version/signature/docstring（397k 行 × 3 列 → 节省 ~50% 内存和时间）
        ext_by_qname: Dict[str, Dict] = {}
        ext_by_name: Dict[str, List[Dict]] = defaultdict(list)
        qname_id_map: Dict[str, int] = {}
        try:
            cur = self.conn.execute(
                "SELECT id, symbol_name, qualified_name, package_name FROM external_symbols"
            )
            for row in cur:
                d = {"id": row[0], "symbol_name": row[1],
                     "qualified_name": row[2], "package_name": row[3]}
                qn = d.get("qualified_name", "")
                if qn:
                    ext_by_qname[qn] = d
                    qname_id_map[qn] = -d["id"]  # P7: 外部符号用负 id
                ext_by_name[d.get("symbol_name", "")].append(d)
        except Exception:
            # external_symbols 表可能不存在（旧版本 DB）
            pass

        # ---- P7 优化：一次性构建 qname_id_map（项目符号）+ file_sym_id_map ----
        # 替代 _write_calls_db 内每文件全表扫描 symbols
        file_sym_id_map: Dict[int, Dict[str, int]] = defaultdict(dict)
        cur = self.conn.execute("SELECT id, name, qualified_name, file_instance_id FROM symbols")
        for row in cur:
            qn = row["qualified_name"]
            if qn:
                qname_id_map[qn] = row["id"]
            file_sym_id_map[row["file_instance_id"]][row["name"]] = row["id"]

        # ---- 第二阶段：构建 import 索引 ----
        # file_path -> {alias/module_name: full_module_path}
        file_imports: Dict[str, Dict[str, str]] = {}
        for rel_path, result in file_results.items():
            imports = result.get("imports", [])
            import_map = {}
            for imp in imports:
                module = imp.get("module", "")
                if not module:
                    continue
                # Go: "github.com/user/project/pkg" → 包名 "pkg"
                # Java: "com.tokenslim.sdk.TokenSlimClient" → 类名 "TokenSlimClient"
                # C/C++: "stdio.h" → "stdio"
                if "/" in module:
                    parts = module.rstrip("/").split("/")
                    alias = parts[-1]
                elif "." in module:
                    parts = module.split(".")
                    alias = parts[-1]
                else:
                    alias = module.replace(".h", "").replace(".hpp", "")
                import_map[alias] = module
            file_imports[rel_path] = import_map

        # ---- 第三阶段：解析调用关系 ----
        # P7 优化：批量收集 calls/call_versions 记录，循环后 executemany 一次性写入
        calls_to_insert: List[tuple] = []          # calls 表的 INSERT 元组
        call_versions_to_insert: List[tuple] = []  # call_versions 表的 INSERT 元组
        changed_file_instance_ids: List[int] = []  # 需要删除旧 calls 的文件实例

        for rel_path, result in file_results.items():
            # 如果结果来自DB，calls 已在 calls 表中，无需 DELETE+重写（消除写放大）
            if result.get("_from_db"):
                existing_calls = result.get("raw_calls", [])
                total_calls += len(existing_calls)
                resolved_count += sum(1 for c in existing_calls if c.get("callee_qualified"))
                continue

            raw_calls = result.get("raw_calls", [])
            calls = []
            current_imports = file_imports.get(rel_path, {})

            for raw in raw_calls:
                callee_qname = ""
                callee_file = ""
                callee_id = 0
                is_cross = 0

                callee_name = raw.get("callee_name", "")
                callee_module = raw.get("callee_module", "")

                if not callee_name:
                    calls.append(self._make_call_entry(raw, "", "", 0, 0))
                    continue

                # 策略 1：精确匹配 module.name
                if callee_module:
                    test_qname = f"{callee_module}.{callee_name}"
                    if test_qname in all_symbols_map:
                        callee_qname = test_qname
                        callee_file = all_symbols_map[test_qname]["file"]
                        callee_id = all_symbols_map[test_qname]["symbol"].get("id", 0)
                        if callee_file != rel_path:
                            is_cross = 1

                # 策略 2：通过 import 映射 module 后匹配
                if not callee_qname and callee_module:
                    # callee_module 可能是 import 别名，尝试映射到实际模块路径
                    if callee_module in current_imports:
                        # 尝试用 import 的完整路径的末段作为模块名
                        full_mod = current_imports[callee_module]
                        # 构建可能的 qualified_name
                        mod_parts = full_mod.replace("/", ".").split(".")
                        # 尝试最后一段 + 函数名
                        for i in range(len(mod_parts)):
                            test_mod = ".".join(mod_parts[i:])
                            test_qname = f"{test_mod}.{callee_name}"
                            if test_qname in all_symbols_map:
                                callee_qname = test_qname
                                callee_file = all_symbols_map[test_qname]["file"]
                                callee_id = all_symbols_map[test_qname]["symbol"].get("id", 0)
                                if callee_file != rel_path:
                                    is_cross = 1
                                break

                    # P0 优化：后缀索引替代全表遍历（O(N) → O(K)，K 为后缀候选数）
                    if not callee_qname:
                        suffix = f".{callee_module}.{callee_name}"
                        for qname in suffix_index.get(suffix, []):
                            callee_qname = qname
                            callee_file = all_symbols_map[qname]["file"]
                            callee_id = all_symbols_map[qname]["symbol"].get("id", 0)
                            if callee_file != rel_path:
                                is_cross = 1
                            break

                # 策略 3：简名唯一匹配（即使 callee_module 为空也尝试）
                if not callee_qname and callee_name in name_index:
                    candidates = name_index[callee_name]
                    if len(candidates) == 1:
                        callee_qname = candidates[0]
                        callee_file = all_symbols_map[callee_qname]["file"]
                        callee_id = all_symbols_map[callee_qname]["symbol"].get("id", 0)
                        if callee_file != rel_path:
                            is_cross = 1
                    elif len(candidates) > 1:
                        # 多个候选，优先选当前文件的
                        for qname in candidates:
                            if all_symbols_map[qname]["file"] == rel_path:
                                callee_qname = qname
                                callee_file = rel_path
                                callee_id = all_symbols_map[qname]["symbol"].get("id", 0)
                                break
                        # 如果当前文件没有，且 callee_module 匹配某个候选的父级
                        if not callee_qname and callee_module:
                            for qname in candidates:
                                # 支持 :: 和 . 分隔符
                                norm_qname = qname.replace("::", ".")
                                parts = norm_qname.rsplit(".", 1)
                                if len(parts) > 1 and parts[0].endswith(callee_module):
                                    callee_qname = qname
                                    callee_file = all_symbols_map[qname]["file"]
                                    callee_id = all_symbols_map[qname]["symbol"].get("id", 0)
                                    if callee_file != rel_path:
                                        is_cross = 1
                                    break

                # 策略 4：同文件简名匹配（P0 优化：后缀索引替代全表遍历）
                if not callee_qname and callee_name in file_symbols.get(rel_path, set()):
                    suffix = f".{callee_name}"
                    for qname in suffix_index.get(suffix, []):
                        if all_symbols_map[qname]["file"] == rel_path:
                            callee_qname = qname
                            callee_file = rel_path
                            callee_id = all_symbols_map[qname]["symbol"].get("id", 0)
                            break

                # 策略 5：查找外部符号表（P3 优化：内存查找替代逐条 DB 查询）
                if not callee_qname:
                    if callee_module:
                        test_qname = f"{callee_module}.{callee_name}"
                        ext_sym = ext_by_qname.get(test_qname)
                        if ext_sym:
                            callee_qname = test_qname
                            callee_file = f"external://{ext_sym['package_name']}"
                            callee_id = -ext_sym["id"]
                            is_cross = 1
                    else:
                        ext_syms = ext_by_name.get(callee_name, [])
                        if len(ext_syms) == 1:
                            ext_sym = ext_syms[0]
                            callee_qname = ext_sym["qualified_name"]
                            callee_file = f"external://{ext_sym['package_name']}"
                            callee_id = -ext_sym["id"]
                            is_cross = 1

                if callee_qname:
                    resolved_count += 1

                calls.append(self._make_call_entry(
                    raw, callee_qname, callee_file, callee_id, is_cross
                ))

            # P7: 收集到批量列表（不再逐条 INSERT）
            fi_id = result["file_instance_id"]
            changed_file_instance_ids.append(fi_id)
            sym_id_map_fi = file_sym_id_map.get(fi_id, {})

            # 收集 calls 表 INSERT 元组（caller_id 多级 fallback 与 _write_calls_db 一致）
            for call in calls:
                caller_id = 0
                caller_qname = call.get("caller_qualified", "")
                caller_name_raw = call.get("caller_name", "")
                if caller_qname and caller_qname in qname_id_map:
                    caller_id = qname_id_map[caller_qname]
                elif caller_name_raw and caller_name_raw in qname_id_map:
                    caller_id = qname_id_map[caller_name_raw]
                elif caller_name_raw:
                    simple_name = caller_name_raw
                    for sep in ("::", ".", "#"):
                        if sep in simple_name:
                            simple_name = simple_name.rsplit(sep, 1)[-1]
                    caller_id = sym_id_map_fi.get(simple_name, 0)
                if caller_id == 0:
                    continue
                callee_q = call.get("callee_qualified", "")
                callee_id_resolved = qname_id_map.get(callee_q, 0) if callee_q else 0
                calls_to_insert.append((
                    caller_id, caller_name_raw, call.get("caller_module", ""),
                    call["callee_name"], call.get("callee_module", ""),
                    callee_q, call.get("callee_file", ""),
                    callee_id_resolved, call.get("call_line", 0),
                    call.get("is_cross_file", 0),
                ))

            # 收集 call_versions 表 INSERT 元组
            fn_hash_map = {}
            for sym in result["symbols"]:
                if sym["kind"] in ("fn", "test_fn"):
                    fn_hash_map[sym["qualified_name"]] = sym["content_hash"]
            for inline_mod in result.get("inline_modules", []):
                for sym in inline_mod["symbols"]:
                    if sym["kind"] in ("fn", "test_fn"):
                        fn_hash_map[sym["qualified_name"]] = sym["content_hash"]
            mod_path = result.get("module_path", "")
            fv_id = result["file_version_id"]
            for call in calls:
                caller_qualified = call.get("caller_qualified", "")
                if not caller_qualified:
                    if call.get("caller_name"):
                        caller_qualified = f"{mod_path}::{call['caller_name']}"
                caller_hash = fn_hash_map.get(caller_qualified, "")
                call_versions_to_insert.append((
                    fv_id, caller_qualified, caller_hash,
                    call["callee_name"], call["callee_module"], call["callee_qualified"],
                    call["callee_file"], call["call_line"], call["is_cross_file"],
                ))
            total_calls += len(calls)

        # ---- P7: 批量写入 calls + call_versions ----
        # 批量删除已变更文件的旧 calls（分批 IN 子句，避免 SQLite 999 参数限制）
        if changed_file_instance_ids:
            BATCH = 500
            for i in range(0, len(changed_file_instance_ids), BATCH):
                chunk = changed_file_instance_ids[i:i + BATCH]
                placeholders = ",".join("?" * len(chunk))
                self.conn.execute(
                    f"DELETE FROM calls WHERE caller_id IN ("
                    f"SELECT id FROM symbols WHERE file_instance_id IN ({placeholders}))",
                    chunk,
                )
        if calls_to_insert:
            self.conn.executemany(
                """INSERT INTO calls
                   (caller_id, caller_name, caller_module, callee_name,
                    callee_module, callee_qualified, callee_file, callee_id,
                    call_line, is_cross_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                calls_to_insert,
            )
        if call_versions_to_insert:
            self.conn.executemany(
                """INSERT INTO call_versions
                   (file_version_id, caller_qualified, caller_hash, callee_name,
                    callee_module, callee_qualified, callee_file, call_line, is_cross_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                call_versions_to_insert,
            )

        print(t("cli.messages.db_build_calls_summary", total=total_calls, resolved=resolved_count, percent=resolved_count * 100 // total_calls if total_calls else 0))


    def _make_call_entry(self, raw: Dict, callee_qname: str, callee_file: str,
                         callee_id: int, is_cross: int) -> Dict:
        """构造调用关系记录"""
        return {
            "caller_name": raw.get("caller_name", ""),
            "caller_qualified": raw.get("caller_qualified", ""),
            "caller_module": raw.get("caller_module", ""),
            "callee_name": raw.get("callee_name", ""),
            "callee_module": raw.get("callee_module", ""),
            "callee_qualified": callee_qname,
            "callee_file": callee_file,
            "callee_id": callee_id,
            "call_line": raw.get("call_line", 0),
            "is_cross_file": is_cross,
        }


    def _infer_module_path_generic(self, rel_path: str, lang: str) -> str:
        """通用模块路径推断

        Args:
            rel_path: 相对路径
            lang: 语言

        Returns:
            模块路径字符串
        """
        path = rel_path.replace("\\", "/")

        if lang == "rust":
            return self._infer_module_path(rel_path)

        # 去掉扩展名
        for ext in get_supported_extensions():
            if path.endswith(ext):
                path = path[: -len(ext)]
                break

        # 去掉常见的 src/ 前缀
        for prefix in ("src/", "lib/", "app/", "main/"):
            if path.startswith(prefix):
                path = path[len(prefix):]
                break

        # 去掉 index/__init__ 等入口文件名
        basename = os.path.basename(path)
        if basename in ("index", "__init__", "mod"):
            dirname = os.path.dirname(path)
            if dirname:
                path = dirname
            else:
                path = "(root)"

        # 用点分隔
        return path.replace("/", ".")


    def build_call_graph(self, file_results: Dict[str, Dict[str, Any]], crate_name: str = ""):
        """构建调用关系图

        Args:
            file_results: 文件解析结果字典
            crate_name: crate 名称
        """
        if not crate_name:
            crate_name = self._detect_crate_name()

        self.call_resolver.crate_name = crate_name
        self.call_resolver.lib_crate_alias = "lib"
        self.call_resolver.load_all_symbols(file_results)
        total_calls = 0
        for rel_path, result in file_results.items():
            calls = self._resolve_file_calls(rel_path, result)
            self._write_calls_db(result["file_instance_id"], calls)
            self._save_calls_for_version(result["file_version_id"], calls, result)
            total_calls += len(calls)

        print(t("cli.messages.db_build_calls_simple", total=total_calls))


    def _detect_crate_name(self) -> str:
        """从 Cargo.toml 检测 crate 名称"""
        cargo_toml = os.path.join(self.workspace_root, "Cargo.toml")
        if os.path.exists(cargo_toml):
            try:
                import re
                with open(cargo_toml, "r", encoding="utf-8") as f:
                    content = f.read()
                m = re.search(r'name\s*=\s*"([^"]+)"', content)
                if m:
                    return m.group(1)
            except Exception:
                pass
        return "tokenslim"


    def _register_file_db(self, abs_path: str, module_path: str) -> int:
        """注册文件到数据库（使用 file_instances 表）

        Args:
            abs_path: 绝对路径
            module_path: 模块路径

        Returns:
            file_instance_id
        """
        ws_id = self._get_active_workspace_id()
        rel_path = norm_path(os.path.relpath(abs_path, self.workspace_root))
        mtime = os.path.getmtime(abs_path)

        cur = self.conn.execute(
            "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = ?",
            (ws_id, rel_path),
        )
        row = cur.fetchone()

        if row:
            self.conn.execute(
                "UPDATE file_instances SET mtime = ?, module_path = ?, status = 'pending' WHERE id = ?",
                (mtime, module_path, row["id"]),
            )
            return row["id"]
        else:
            cur = self.conn.execute(
                """INSERT INTO file_instances
                   (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
                   VALUES (?, ?, ?, '', ?, 0, 0, 'pending', ?)""",
                (ws_id, rel_path, norm_path(abs_path), mtime, module_path),
            )
            return cur.lastrowid


    def _get_file_version(self, file_instance_id: int) -> Optional[sqlite3.Row]:
        """获取文件的最新版本"""
        cur = self.conn.execute(
            "SELECT * FROM file_versions WHERE file_instance_id = ? ORDER BY version_num DESC LIMIT 1",
            (file_instance_id,),
        )
        return cur.fetchone()


    def _save_file_version(self, file_instance_id: int, result: Dict[str, Any]) -> int:
        """创建新的文件版本，如果内容没变则返回现有版本号"""
        content_hash = result["content_hash"]
        mtime = os.path.getmtime(result["abs_path"]) if "abs_path" in result else 0
        total_lines = result["total_lines"]
        parsed_at = time.time()
        language = detect_language_from_path(result.get("rel_path", ""))

        # 确保 file_contents 中有记录
        self.conn.execute(
            "INSERT OR IGNORE INTO file_contents (content_hash, language, total_lines, first_seen_at) VALUES (?, ?, ?, ?)",
            (content_hash, language, total_lines, parsed_at),
        )

        latest = self._get_file_version(file_instance_id)
        if latest and latest["content_hash"] == content_hash:
            # 更新 mtime
            self.conn.execute(
                "UPDATE file_versions SET mtime = ? WHERE id = ?",
                (mtime, latest["id"]),
            )
            # 更新 file_instances 的 current_content_hash 和 last_parsed
            self.conn.execute(
                "UPDATE file_instances SET current_content_hash = ?, last_parsed = ?, total_lines = ?, mtime = ? WHERE id = ?",
                (content_hash, parsed_at, total_lines, mtime, file_instance_id),
            )
            # 更新 ast_cache 元数据（即使内容未变，记录 parsed_at 用于跨进程缓存判断）
            self._update_ast_cache(latest["id"], result, content_hash, parsed_at)
            return latest["id"]

        # 计算符号 diff（与上一版本比较）
        prev_version_id = latest["id"] if latest else None

        if latest:
            self.conn.execute(
                "UPDATE file_versions SET is_current = 0 WHERE id = ?",
                (latest["id"],),
            )
            version_num = latest["version_num"] + 1
        else:
            version_num = 1

        cur = self.conn.execute(
            """INSERT INTO file_versions
               (file_instance_id, version_num, content_hash, mtime, total_lines, parsed_at, is_current, is_deleted)
               VALUES (?, ?, ?, ?, ?, ?, 1, 0)""",
            (file_instance_id, version_num, content_hash, mtime, total_lines, parsed_at),
        )
        new_version_id = cur.lastrowid

        # 更新 file_instances 的 current_content_hash 和 last_parsed
        self.conn.execute(
            "UPDATE file_instances SET current_content_hash = ?, last_parsed = ?, total_lines = ?, mtime = ? WHERE id = ?",
            (content_hash, parsed_at, total_lines, mtime, file_instance_id),
        )

        # 写入 ast_cache 元数据（v28 新增：AST 增量解析元信息）
        self._update_ast_cache(new_version_id, result, content_hash, parsed_at)

        # 如果有前一版本，计算符号 diff 并设置删除标记
        if prev_version_id:
            self._compute_and_apply_symbol_diff(prev_version_id, new_version_id)

        return new_version_id

    def _update_ast_cache(
        self,
        file_version_id: int,
        result: Dict[str, Any],
        content_hash: str,
        parsed_at: float,
    ) -> None:
        """更新 file_versions.ast_cache 字段（v28 新增）

        存储格式：JSON 编码的元数据字节流
        {
            "content_hash": str,         # 上次解析的 content_hash
            "parsed_at": float,          # 上次解析时间
            "incremental": bool,         # 是否走了增量解析路径
            "changed_ranges_count": int, # 变更区间数量（0 表示全量解析）
            "language": str,             # 语言标识
        }

        tree-sitter Tree 对象无法跨进程序列化，此处只存元数据。
        实际的 Tree 对象缓存在 BaseParser._tree_cache（进程内）。
        """
        import json
        metadata = {
            "content_hash": content_hash,
            "parsed_at": parsed_at,
            "incremental": result.get("incremental", False),
            "changed_ranges_count": len(result.get("changed_ranges", [])),
            "language": result.get("language", ""),
        }
        try:
            self.conn.execute(
                "UPDATE file_versions SET ast_cache = ? WHERE id = ?",
                (json.dumps(metadata).encode("utf-8"), file_version_id),
            )
        except sqlite3.OperationalError:
            # ast_cache 字段不存在（v27 库未迁移到 v28），降级为无操作
            pass

    def _read_ast_cache(self, file_instance_id: int) -> Optional[Dict[str, Any]]:
        """读取 file_versions.ast_cache 元数据（v28 新增）

        用于跨进程判断上次解析状态：
        - 若 ast_cache.content_hash 与当前文件 content_hash 相同，可跳过解析
        - 若 ast_cache.incremental 为 True，可复用增量解析状态

        Returns:
            元数据字典或 None（无缓存或字段不存在）
        """
        import json
        cur = self.conn.execute(
            "SELECT ast_cache FROM file_versions WHERE file_instance_id = ? AND is_current = 1 LIMIT 1",
            (file_instance_id,),
        )
        row = cur.fetchone()
        if not row or not row["ast_cache"]:
            return None
        try:
            return json.loads(row["ast_cache"].decode("utf-8") if isinstance(row["ast_cache"], bytes) else row["ast_cache"])
        except (json.JSONDecodeError, UnicodeDecodeError):
            return None


    def _compute_symbol_diff(self, prev_version_id: int, curr_version_id: int) -> Dict:
        """计算两个版本之间的符号差异

        Args:
            prev_version_id: 上一版本 ID
            curr_version_id: 当前版本 ID

        Returns:
            {"added": [...], "removed": [...], "modified": [...]}
        """
        # 获取上一版本的符号
        cur = self.conn.execute(
            "SELECT symbol_hash, qualified_name, start_line, end_line FROM file_symbol_versions WHERE file_version_id = ?",
            (prev_version_id,),
        )
        prev_symbols = {row["qualified_name"]: dict(row) for row in cur}

        # 获取当前版本的符号
        cur = self.conn.execute(
            "SELECT symbol_hash, qualified_name, start_line, end_line FROM file_symbol_versions WHERE file_version_id = ?",
            (curr_version_id,),
        )
        curr_symbols = {row["qualified_name"]: dict(row) for row in cur}

        added = []
        removed = []
        modified = []

        all_names = set(prev_symbols.keys()) | set(curr_symbols.keys())

        for name in all_names:
            prev = prev_symbols.get(name)
            curr = curr_symbols.get(name)

            if prev and not curr:
                removed.append(prev)
            elif curr and not prev:
                added.append(curr)
            elif prev and curr and prev["symbol_hash"] != curr["symbol_hash"]:
                modified.append(curr)

        return {
            "added": added,
            "removed": removed,
            "modified": modified,
        }


    def _compute_and_apply_symbol_diff(self, prev_version_id: int, curr_version_id: int):
        """计算符号 diff 并应用删除标记

        为当前版本中不存在的符号（相对于上一版本）设置 is_deleted=1
        """
        # 获取上一版本的符号
        cur = self.conn.execute(
            "SELECT symbol_hash, qualified_name FROM file_symbol_versions WHERE file_version_id = ?",
            (prev_version_id,),
        )
        prev_symbols = {row["qualified_name"]: row["symbol_hash"] for row in cur}

        # 获取当前版本的符号
        cur = self.conn.execute(
            "SELECT id, symbol_hash, qualified_name FROM file_symbol_versions WHERE file_version_id = ?",
            (curr_version_id,),
        )
        curr_symbols = {row["qualified_name"]: row["symbol_hash"] for row in cur}
        curr_ids = {row["qualified_name"]: row["id"] for row in cur}

        # 找出删除的符号（上一版本有，当前版本没有）
        removed_names = set(prev_symbols.keys()) - set(curr_symbols.keys())

        # 对于删除的符号，在当前版本中插入标记为 is_deleted=1 的记录
        for name in removed_names:
            symbol_hash = prev_symbols[name]
            # 获取上一版本中的位置信息
            cur = self.conn.execute(
                "SELECT start_line, end_line, module_path, depth FROM file_symbol_versions WHERE file_version_id = ? AND qualified_name = ?",
                (prev_version_id, name),
            )
            prev_row = cur.fetchone()
            if prev_row:
                self.conn.execute(
                    """INSERT INTO file_symbol_versions
                       (file_version_id, symbol_hash, qualified_name, start_line, end_line, module_path, depth, is_deleted)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 1)""",
                    (curr_version_id, symbol_hash, name,
                     prev_row["start_line"], prev_row["end_line"],
                     prev_row["module_path"], prev_row["depth"]),
                )


    def _save_symbols_for_version(self, file_version_id: int, file_instance_id: int, result: Dict[str, Any]):
        """为文件版本创建所有符号版本（批量写入优化，相同 hash 只存一次）

        性能优化（原 N×3 次 SQL → 现 5 次 SQL）：
        1. 批量 INSERT OR IGNORE symbol_contents（content_hash 是 PK，自动去重）
        2. 批量 SELECT 已存在的 symbols（按 qualified_name IN (...) 一次查询）
        3. 批量 UPDATE 已存在的 symbols（executemany）
        4. 批量 INSERT 新增的 symbols（executemany）
        5. 批量 INSERT file_symbol_versions（executemany）

        整个操作在外层 build() 的事务中执行，无需显式 BEGIN/COMMIT。
        """
        all_symbols = list(result["symbols"])
        for inline_mod in result.get("inline_modules", []):
            all_symbols.extend(inline_mod["symbols"])

        if not all_symbols:
            return

        # 1. 补算 content_hash（多语言 parser 可能没计算，现场补算）
        for sym in all_symbols:
            if "content_hash" not in sym:
                sym["content_hash"] = compute_content_hash(sym.get("content", ""))

        # 2. 批量 INSERT OR IGNORE symbol_contents（content_hash 是 PK，自动去重）
        self.conn.executemany(
            """INSERT OR IGNORE INTO symbol_contents
               (content_hash, name, kind, content, signature, has_comment, comment_content, qualified_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            [(s["content_hash"], s["name"], s["kind"], s["content"],
              s["signature"], s["has_comment"], s.get("comment_content", ""),
              s["qualified_name"]) for s in all_symbols],
        )

        # 3. 批量查询已存在的 symbols（按 qualified_name 一次查询，避免 N 次 SELECT）
        qualified_names = [s["qualified_name"] for s in all_symbols]
        # 去重 qualified_name 避免重复占位符（同一文件可能有同名符号，罕见但安全处理）
        unique_qnames = list(set(qualified_names))
        placeholders = ",".join("?" * len(unique_qnames))
        cur = self.conn.execute(
            f"SELECT id, qualified_name FROM symbols WHERE qualified_name IN ({placeholders})",
            unique_qnames,
        )
        existing_map = {row["qualified_name"]: row["id"] for row in cur.fetchall()}

        # 4. 分离已存在（UPDATE）和新增（INSERT）的符号
        to_update = []
        to_insert = []
        for sym in all_symbols:
            qname = sym["qualified_name"]
            if qname in existing_map:
                to_update.append((
                    file_instance_id, sym["content_hash"], sym["name"], sym["kind"], sym["visibility"],
                    sym["start_line"], sym["end_line"], sym["start_col"], sym["end_col"],
                    sym["signature"], sym["has_comment"], sym["module_path"],
                    existing_map[qname],
                ))
            else:
                to_insert.append((
                    file_instance_id, sym["content_hash"], sym["name"], sym["kind"], sym["visibility"],
                    sym["start_line"], sym["end_line"],
                    sym["start_col"], sym["end_col"], sym["signature"], sym["has_comment"],
                    sym["module_path"], qname,
                ))

        # 5. 批量 UPDATE 已存在的 symbols
        if to_update:
            self.conn.executemany(
                """UPDATE symbols SET
                   file_instance_id = ?, symbol_hash = ?, name = ?, kind = ?, visibility = ?,
                   start_line = ?, end_line = ?, start_col = ?, end_col = ?,
                   signature = ?, has_comment = ?, module_path = ?
                   WHERE id = ?""",
                to_update,
            )

        # 6. 批量 UPSERT 新增的 symbols（ON CONFLICT 防止重复行）
        if to_insert:
            self.conn.executemany(
                """INSERT INTO symbols
                   (file_instance_id, symbol_hash, name, kind, visibility, start_line, end_line,
                    start_col, end_col, signature, has_comment, comment_status,
                    module_path, qualified_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                   ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET
                    symbol_hash = excluded.symbol_hash,
                    kind = excluded.kind,
                    visibility = excluded.visibility,
                    end_line = excluded.end_line,
                    start_col = excluded.start_col,
                    end_col = excluded.end_col,
                    signature = excluded.signature,
                    has_comment = excluded.has_comment,
                    module_path = excluded.module_path,
                    qualified_name = excluded.qualified_name""",
                to_insert,
            )

        # 7. 批量 INSERT file_symbol_versions
        self.conn.executemany(
            """INSERT INTO file_symbol_versions
               (file_version_id, symbol_hash, qualified_name, start_line, end_line, module_path, depth, is_deleted)
               VALUES (?, ?, ?, ?, ?, ?, ?, 0)""",
            [(file_version_id, s["content_hash"], s["qualified_name"],
              s["start_line"], s["end_line"], s["module_path"], -1) for s in all_symbols],
        )


    def _ensure_symbol_content(self, sym: Dict[str, Any]):
        """确保符号内容已存储（相同 hash 只存一次）

        优化：用 INSERT OR IGNORE 替代 SELECT-then-INSERT，省掉一次查询。
        symbol_contents 表的 content_hash 是 PRIMARY KEY，自动去重。
        """
        self.conn.execute(
            """INSERT OR IGNORE INTO symbol_contents
               (content_hash, name, kind, content, signature, has_comment, comment_content, qualified_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (sym["content_hash"], sym["name"], sym["kind"], sym["content"],
             sym["signature"], sym["has_comment"], sym.get("comment_content", ""),
             sym["qualified_name"]),
        )


    def _get_or_create_symbol(self, file_instance_id: int, sym: Dict[str, Any]) -> int:
        """获取或创建符号（通过 qualified_name 匹配）"""
        cur = self.conn.execute(
            "SELECT id FROM symbols WHERE qualified_name = ?",
            (sym["qualified_name"],),
        )
        row = cur.fetchone()
        if row:
            self.conn.execute(
                """UPDATE symbols SET 
                   file_instance_id = ?, symbol_hash = ?, name = ?, kind = ?, visibility = ?,
                   start_line = ?, end_line = ?, start_col = ?, end_col = ?,
                   signature = ?, has_comment = ?, module_path = ?
                   WHERE id = ?""",
                (file_instance_id, sym["content_hash"], sym["name"], sym["kind"], sym["visibility"],
                 sym["start_line"], sym["end_line"], sym["start_col"], sym["end_col"],
                 sym["signature"], sym["has_comment"], sym["module_path"], row["id"]),
            )
            return row["id"]
        else:
            cur = self.conn.execute(
                """INSERT INTO symbols
                   (file_instance_id, symbol_hash, name, kind, visibility, start_line, end_line,
                    start_col, end_col, signature, has_comment, comment_status,
                    module_path, qualified_name)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
                   ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET
                    symbol_hash = excluded.symbol_hash,
                    kind = excluded.kind,
                    visibility = excluded.visibility,
                    end_line = excluded.end_line,
                    start_col = excluded.start_col,
                    end_col = excluded.end_col,
                    signature = excluded.signature,
                    has_comment = excluded.has_comment,
                    module_path = excluded.module_path,
                    qualified_name = excluded.qualified_name
                   RETURNING id""",
                (file_instance_id, sym["content_hash"], sym["name"], sym["kind"], sym["visibility"],
                 sym["start_line"], sym["end_line"],
                 sym["start_col"], sym["end_col"], sym["signature"], sym["has_comment"],
                 sym["module_path"], sym["qualified_name"]),
            )
            return cur.fetchone()[0]


    def _insert_symbol(self, file_instance_id: int, sym: Dict[str, Any]):
        """向 symbols 表插入单个符号记录

        若符号 dict 中没有 content_hash，则根据 content 字段自动计算。
        comment_status 初始化为 'pending'，后续由注释恢复流程更新。

        Args:
            file_instance_id: 所属文件实例 ID
            sym: 符号信息字典，需包含 name/kind/visibility/start_line/end_line/
                 start_col/end_col/signature/has_comment/module_path/qualified_name
        """
        if "content_hash" not in sym:
            sym["content_hash"] = compute_content_hash(sym.get("content", ""))
        self.conn.execute(
            """INSERT INTO symbols
               (file_instance_id, symbol_hash, name, kind, visibility, start_line, end_line,
                start_col, end_col, signature, has_comment, comment_status,
                module_path, qualified_name)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?, ?)
               ON CONFLICT(file_instance_id, name, start_line) DO UPDATE SET
                symbol_hash = excluded.symbol_hash,
                kind = excluded.kind,
                visibility = excluded.visibility,
                end_line = excluded.end_line,
                start_col = excluded.start_col,
                end_col = excluded.end_col,
                signature = excluded.signature,
                has_comment = excluded.has_comment,
                module_path = excluded.module_path,
                qualified_name = excluded.qualified_name""",
            (file_instance_id, sym["content_hash"], sym["name"], sym["kind"], sym["visibility"],
             sym["start_line"], sym["end_line"],
             sym["start_col"], sym["end_col"], sym["signature"], sym["has_comment"],
             sym["module_path"], sym["qualified_name"]),
        )


    def _resolve_file_calls(self, rel_path: str, result: Dict[str, Any]) -> List[Dict[str, Any]]:
        """解析单个文件的所有调用"""
        resolved_calls = []
        module_path = result["module_path"]

        for raw_call in result["raw_calls"]:
            callee_info = self.call_resolver.resolve_call(
                rel_path,
                module_path,
                raw_call["callee_name"],
                raw_call.get("callee_module", raw_call.get("callee_path", "")),
                raw_call.get("callee_is_qualified", False),
            )

            resolved_call = {
                "caller_name": raw_call["caller_name"],
                "caller_module": module_path,
                "callee_name": raw_call["callee_name"],
                "callee_module": callee_info["module_path"] if callee_info else "",
                "callee_qualified": callee_info["qualified_name"] if callee_info else "",
                "callee_file": callee_info["file"] if callee_info else "",
                "callee_id": callee_info.get("id", 0) if callee_info else 0,
                "call_line": raw_call["call_line"],
                "is_cross_file": 1 if callee_info and callee_info["file"] != rel_path else 0,
            }
            resolved_calls.append(resolved_call)

        return resolved_calls


    def _write_calls_db(self, file_instance_id: int, calls: List[Dict[str, Any]]):
        """将文件的调用关系写入数据库，先删除旧快照再写入新关系

        Args:
            file_instance_id: 文件实例 ID
            calls: 调用关系字典列表，每项含 caller_qualified / callee_qualified 等字段
        """
        # 先删除该文件已有的调用快照
        self.conn.execute(
            "DELETE FROM calls WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id = ?)",
            (file_instance_id,)
        )

        # 同文件内 name -> id（最后一级 fallback，无法区分同名方法）
        sym_id_map = {}
        cur = self.conn.execute(
            "SELECT id, name FROM symbols WHERE file_instance_id = ?", (file_instance_id,)
        )
        for row in cur:
            sym_id_map[row["name"]] = row["id"]

        # 全局 qualified_name -> id（精确匹配，能区分同名方法）
        qname_id_map = {}
        cur = self.conn.execute("SELECT id, qualified_name FROM symbols")
        for row in cur:
            qname_id_map[row["qualified_name"]] = row["id"]

        # 外部符号 qualified_name -> -id（负值表示外部符号）
        cur = self.conn.execute("SELECT id, qualified_name FROM external_symbols")
        for row in cur:
            qname_id_map[row["qualified_name"]] = -row["id"]

        for call in calls:
            # 多级 fallback 策略匹配 caller_id：
            # 1. caller_qualified 精确匹配 qname_id_map（最优，带类名）
            # 2. caller_name 直接匹配 qname_id_map（无类语言：C/Go/HCL 等）
            # 3. 提取 caller_name 最后一段（简名）匹配同文件 sym_id_map（兜底）
            caller_id = 0
            caller_qname = call.get("caller_qualified", "")
            caller_name_raw = call.get("caller_name", "")

            if caller_qname and caller_qname in qname_id_map:
                caller_id = qname_id_map[caller_qname]
            elif caller_name_raw and caller_name_raw in qname_id_map:
                caller_id = qname_id_map[caller_name_raw]
            elif caller_name_raw:
                # 提取简名（最后一段），兼容不同分隔符
                simple_name = caller_name_raw
                for sep in ("::", ".", "#"):
                    if sep in simple_name:
                        simple_name = simple_name.rsplit(sep, 1)[-1]
                caller_id = sym_id_map.get(simple_name, 0)

            if caller_id == 0:
                continue

            callee_id = qname_id_map.get(call["callee_qualified"], 0) if call.get("callee_qualified") else 0

            self.conn.execute(
                """INSERT INTO calls
                   (caller_id, caller_name, caller_module, callee_name,
                    callee_module, callee_qualified, callee_file, callee_id,
                    call_line, is_cross_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (caller_id, caller_name_raw, call.get("caller_module", ""),
                 call["callee_name"], call.get("callee_module", ""),
                 call.get("callee_qualified", ""), call.get("callee_file", ""),
                 callee_id, call.get("call_line", 0),
                 call.get("is_cross_file", 0)),
            )


    def _save_calls_for_version(self, file_version_id: int, calls: List[Dict[str, Any]], result: Dict[str, Any]):
        """为文件版本创建调用关系版本"""
        fn_hash_map = {}
        for sym in result["symbols"]:
            if sym["kind"] in ("fn", "test_fn"):
                fn_hash_map[sym["qualified_name"]] = sym["content_hash"]
        for inline_mod in result.get("inline_modules", []):
            for sym in inline_mod["symbols"]:
                if sym["kind"] in ("fn", "test_fn"):
                    fn_hash_map[sym["qualified_name"]] = sym["content_hash"]

        for call in calls:
            # 直接用 parser 输出的 caller_qualified（与 symbols.qualified_name 一致）
            # fallback：旧 parser 没有 caller_qualified 字段时拼装 module_path::caller_name
            caller_qualified = call.get("caller_qualified", "")
            if not caller_qualified:
                if call.get("caller_name"):
                    caller_qualified = f"{result['module_path']}::{call['caller_name']}"
                else:
                    caller_qualified = ""
            caller_hash = fn_hash_map.get(caller_qualified, "")

            self.conn.execute(
                """INSERT INTO call_versions
                   (file_version_id, caller_qualified, caller_hash, callee_name,
                    callee_module, callee_qualified, callee_file, call_line, is_cross_file)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (file_version_id, caller_qualified, caller_hash,
                 call["callee_name"], call["callee_module"], call["callee_qualified"],
                 call["callee_file"], call["call_line"], call["is_cross_file"]),
            )


    def _update_symbol_version_depths(self):
        """更新当前文件-符号关联的深度"""
        cur = self.conn.execute(
            """SELECT fsv.id, s.depth 
               FROM file_symbol_versions fsv
               JOIN symbols s ON fsv.qualified_name = s.qualified_name
               JOIN file_versions fv ON fsv.file_version_id = fv.id
               WHERE fv.is_current = 1 AND fsv.is_deleted = 0"""
        )
        updates = []
        for row in cur:
            updates.append((row["depth"], row["id"]))

        self.conn.executemany(
            "UPDATE file_symbol_versions SET depth = ? WHERE id = ?",
            updates,
        )


    def _build_depth(self):
        """计算每个函数的拓扑深度"""
        cur = self.conn.execute(
            "SELECT id, qualified_name FROM symbols WHERE kind IN ('fn', 'test_fn')"
        )
        all_fns = {row["id"]: row["qualified_name"] for row in cur}

        call_graph = defaultdict(list)
        cur = self.conn.execute(
            "SELECT caller_id, callee_id FROM calls WHERE callee_id > 0"
        )
        for row in cur:
            call_graph[row["caller_id"]].append(row["callee_id"])

        depth_cache = {}

        def compute_depth(fn_id: int, visited: Set[int]) -> int:
            """递归计算函数的调用深度，带缓存与环检测"""
            if fn_id in depth_cache:
                return depth_cache[fn_id]

            if fn_id in visited:
                return 0

            visited.add(fn_id)

            callees = call_graph.get(fn_id, [])
            if not callees:
                depth = 0
            else:
                max_callee_depth = 0
                for callee_id in callees:
                    d = compute_depth(callee_id, visited)
                    if d > max_callee_depth:
                        max_callee_depth = d
                depth = max_callee_depth + 1

            visited.remove(fn_id)
            depth_cache[fn_id] = depth
            return depth

        for fn_id in all_fns:
            compute_depth(fn_id, set())

        # P5 优化：executemany 批量更新替代逐个 UPDATE
        updates = [(depth, fn_id) for fn_id, depth in depth_cache.items()]
        self.conn.executemany(
            "UPDATE symbols SET depth = ? WHERE id = ?",
            updates,
        )

        self.conn.commit()

    # --------------------------------------------------------------------
    # 增量刷新
    # --------------------------------------------------------------------


    def refresh_file(self, file_path: str):
        """刷新单个文件（增量更新），支持多语言"""
        abs_path = os.path.abspath(file_path)
        if not os.path.exists(abs_path):
            return

        lang = detect_language_from_path(abs_path)
        if not lang:
            return

        rel_path = norm_path(os.path.relpath(abs_path, self.workspace_root))
        print(t("cli.messages.db_build_refresh", path=rel_path, lang=lang))

        self._refresh_file_internal(abs_path, rel_path, lang)


    def _refresh_file_internal(self, abs_path: str, rel_path: str, lang: str = "rust"):
        """内部刷新逻辑（支持多语言）"""
        if lang == "rust":
            self._refresh_file_rust(abs_path, rel_path)
        else:
            self._refresh_file_generic(abs_path, rel_path, lang)


    def _refresh_file_rust(self, abs_path: str, rel_path: str):
        """Rust 文件刷新逻辑（增量方式）"""
        if not self.module_resolver.module_to_file:
            self.module_resolver.resolve_all(self.parser)

        module_path = self.module_resolver.get_file_module(rel_path)
        if not module_path:
            module_path = self._infer_module_path(rel_path)

        file_instance_id = self._register_file_db(abs_path, module_path)

        result = self.parser.parse_file(abs_path, module_path)
        result["abs_path"] = abs_path
        result["file_instance_id"] = file_instance_id
        result["module_path"] = module_path
        result["rel_path"] = rel_path
        result.setdefault("inline_modules", [])

        latest_fv = self._get_file_version(file_instance_id)
        if latest_fv and latest_fv["content_hash"] == result["content_hash"]:
            self.conn.execute(
                "UPDATE file_versions SET mtime = ? WHERE id = ?",
                (os.path.getmtime(abs_path), latest_fv["id"]),
            )
            self.conn.commit()
            return

        new_fv_id = self._save_file_version(file_instance_id, result)
        result["file_version_id"] = new_fv_id
        self._save_symbols_for_version(new_fv_id, file_instance_id, result)

        # 增量方式：从DB加载其他文件结果 + 新解析的当前文件
        all_file_results = self._collect_all_current_file_results()
        all_file_results[rel_path] = result

        # 只重算当前文件的调用关系（使用完整的符号索引）
        self._build_call_graph_multi_lang({rel_path: result} | {
            k: v for k, v in all_file_results.items() if k != rel_path
        })

        # 清理旧版本的调用关系
        if latest_fv:
            self.conn.execute(
                "DELETE FROM call_versions WHERE file_version_id = ?",
                (latest_fv["id"],),
            )

        self._build_depth()
        self._update_symbol_version_depths()
        self.conn.commit()


    def _refresh_file_generic(self, abs_path: str, rel_path: str, lang: str):
        """通用语言文件刷新逻辑（增量方式）"""
        from ..parsers import create_parser

        parser = create_parser(rel_path)
        if not parser:
            return

        module_path = self._infer_module_path_generic(rel_path, lang)
        file_instance_id = self._register_file_db(abs_path, module_path)

        try:
            result = parser.parse_file(abs_path, module_path)
        except Exception as e:
            print(t("cli.messages.db_build_refresh_fail", path=rel_path, error=e))
            return

        result["abs_path"] = abs_path
        result["file_instance_id"] = file_instance_id
        result["module_path"] = module_path
        result["rel_path"] = rel_path
        result.setdefault("inline_modules", [])

        latest_fv = self._get_file_version(file_instance_id)
        if latest_fv and latest_fv["content_hash"] == result.get("content_hash", ""):
            self.conn.execute(
                "UPDATE file_versions SET mtime = ? WHERE id = ?",
                (os.path.getmtime(abs_path), latest_fv["id"]),
            )
            self.conn.commit()
            return

        new_fv_id = self._save_file_version(file_instance_id, result)
        result["file_version_id"] = new_fv_id
        self._save_symbols_for_version(new_fv_id, file_instance_id, result)

        # 增量方式：从DB加载其他文件结果
        all_file_results = self._collect_all_current_file_results()
        all_file_results[rel_path] = result

        # 重算调用关系（符号索引来自DB+新文件，只写入变化文件的调用）
        self._build_call_graph_multi_lang(all_file_results)

        # 清理旧版本的调用关系
        if latest_fv:
            self.conn.execute(
                "DELETE FROM call_versions WHERE file_version_id = ?",
                (latest_fv["id"],),
            )

        self._build_depth()
        self._update_symbol_version_depths()
        self.conn.commit()


    def remove_file(self, file_path: str):
        """删除文件（清理数据库中的相关记录，保留版本历史）

        注意：保留 file_versions / file_symbol_versions / call_versions 历史，
        只标记 file_instances 表中的状态为 deleted，并清除 symbols / calls 当前快照。
        """
        ws_id = self._get_active_workspace_id()
        rel_path = norm_path(file_path)

        cur = self.conn.execute(
            "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = ?",
            (ws_id, rel_path),
        )
        row = cur.fetchone()
        if not row:
            return

        file_instance_id = row["id"]

        # 清除当前快照（保留历史版本）
        self.conn.execute("DELETE FROM calls WHERE caller_id IN (SELECT id FROM symbols WHERE file_instance_id = ?)", (file_instance_id,))
        self.conn.execute("DELETE FROM symbols WHERE file_instance_id = ?", (file_instance_id,))

        # 标记文件为已删除
        self.conn.execute(
            "UPDATE file_instances SET status = 'deleted', mtime = ? WHERE id = ?",
            (time.time(), file_instance_id),
        )

        # 标记最新版本为已删除
        cur = self.conn.execute(
            "SELECT id FROM file_versions WHERE file_instance_id = ? AND is_current = 1",
            (file_instance_id,),
        )
        latest = cur.fetchone()
        if latest:
            self.conn.execute(
                "UPDATE file_versions SET is_deleted = 1 WHERE id = ?",
                (latest["id"],),
            )

        self.conn.commit()
        print(t("cli.messages.db_build_marked_deleted", path=rel_path, count=self._count_file_versions(file_instance_id)))


    def _count_file_versions(self, file_instance_id: int) -> int:
        """统计文件的历史版本数"""
        cur = self.conn.execute(
            "SELECT COUNT(*) as cnt FROM file_versions WHERE file_instance_id = ?",
            (file_instance_id,),
        )
        return cur.fetchone()["cnt"]


    def _infer_module_path(self, rel_path: str) -> str:
        """从文件路径推断模块路径（简化版）"""
        path = rel_path.replace("\\", "/")
        if path.startswith("src/"):
            path = path[4:]
        if path.endswith(".rs"):
            path = path[:-3]
        if path.endswith("/mod"):
            path = path[:-4]
        if path == "lib":
            return "lib"
        if path == "main":
            return "main"
        return "lib::" + path.replace("/", "::")


    def _collect_all_current_file_results(self) -> Dict[str, Dict[str, Any]]:
        """收集所有当前版本的文件解析结果（用于调用关系解析）

        从数据库加载已解析的结果，不重新解析文件。
        """
        results = {}

        cur = self.conn.execute("""
            SELECT fv.id as fv_id, fi.id as fi_id, fi.rel_path, fi.abs_path, fi.module_path
            FROM file_versions fv
            JOIN file_instances fi ON fv.file_instance_id = fi.id
            WHERE fv.is_current = 1 AND fv.is_deleted = 0
              AND fi.workspace_id = ?
        """, (self._get_active_workspace_id(),))

        for row in cur:
            rel_path = row["rel_path"]
            abs_path = row["abs_path"]
            module_path = row["module_path"]
            fi_id = row["fi_id"]
            fv_id = row["fv_id"]

            result = self._load_file_result_from_db(fi_id, fv_id, rel_path, abs_path, module_path)
            if result:
                results[rel_path] = result

        return results

    # --------------------------------------------------------------------
    # 查询接口
    # --------------------------------------------------------------------


