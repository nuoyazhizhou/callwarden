"""RustParserFacade —— 生产代码访问 Rust parser 的统一窄接口（P1-E Step 0）

设计文档：docs/design/rust-only-parser-cutover-plan.md §8 Phase 3 步骤 1

本模块是生产代码（db_build / db_check_gate / db_external / watcher 等）与
``callwarden_core`` Rust 扩展之间唯一的解析入口。所有生产调用点通过本 facade
访问 Rust parser，不再直接实例化 Python parser。

统一窄接口涵盖：
    - canonicalize bytes（调用 ``callwarden_core.canonicalize_source_py``）
    - parse canonical bytes（调用 ``callwarden_core.parse_canonical_bytes_py``）
    - parse file by lang（调用 ``callwarden_core.parse_file_lang``）
    - batch / stream（C 专用快路径 + 多语言通用路径）
    - diagnostics（语法错误、unsupported construct）
    - generation metadata（CAS hash、parser ABI 版本）

错误语义（设计 §5.3）：Rust 解析失败必须显式记录并 fail closed，
本 facade 不允许静默回退到 Python parser。调用方拿到 ``error`` 字段后
应按 ``failed`` / ``partial`` / ``unsupported`` 状态处理，不得覆盖上一代
snapshot。

解析模式（设计 §7，P1-F Step 0）：
    - ``rust-strict``：正式发布默认，只用 Rust，失败显式记录
    - ``shadow``：源码开发/CI，Rust 为主，Python reference 同步解析并只比较，
      不影响发布结果；diagnostics 写独立目录，不污染 CAS/manifest/snapshot
    - ``python-reference``：源码开发，仅历史对照，不进入冻结包
    通过环境变量 ``CW_PARSE_MODE`` 配置。frozen build（PyInstaller）强制
    ``rust-strict``，收到 ``python-reference`` 或 ``CW_DISABLE_RUST_PARSE``
    时返回明确错误。

注意：
    - Python parser 仍保留在源码仓库作为开发 reference，但本 facade 不调用它。
    - 单文件 / 批量 / 流式三种调用形态共享同一份 canonical bytes 合约。
    - facade 不持有可变状态，可在多线程 / 多进程中安全共享。
"""

from __future__ import annotations

import os
import sys
from typing import Any, Dict, List, Optional, Tuple


# ────────────────────────────────────────────────────────────────────
# 常量与语言映射
# ────────────────────────────────────────────────────────────────────

# Rust 多语言通用路径支持的语言（不含 C，C 走专用快路径）
# 每次 _ensure_rust_available() 时从 callwarden_core.supported_languages() 动态读取，
# 此处仅作为文档化的快照和单元测试断言用。
_RUST_MULTILANG_LANGS_REFERENCE: Tuple[str, ...] = (
    "python", "rust", "go", "java", "typescript", "javascript",
    "ruby", "php", "scala", "csharp", "cpp", "kotlin", "swift",
    "elixir", "hcl",
)

# parser ABI 版本（与 rust_ext/src/lib.rs 的 core_version 一致）
# 用于 CAS key 隔离与跨版本兼容判断（设计 §9.3）
def _rust_core_version() -> str:
    """返回 Rust 扩展 core_version（用于 CAS key 与 manifest）"""
    try:
        from callwarden_core import core_version
        return core_version()
    except ImportError:
        return "unknown"


# ────────────────────────────────────────────────────────────────────
# 解析模式（设计 §7，P1-F Step 0）
# ────────────────────────────────────────────────────────────────────


class ParseMode:
    """解析模式枚举（设计 §7）

    三种模式：
        - ``rust-strict``：正式发布默认，只用 Rust，失败显式记录
        - ``shadow``：源码开发/CI，Rust 为主，Python reference 同步解析并只比较
        - ``python-reference``：源码开发，仅历史对照，不进入冻结包

    通过环境变量 ``CW_PARSE_MODE`` 配置。frozen build 强制 ``rust-strict``，
    收到 ``python-reference`` 或 ``CW_DISABLE_RUST_PARSE`` 时报错。

    本类仅提供模式判断与环境校验，不负责实际 shadow 比较逻辑（那是开发期
    reference adapter 的职责，见设计 §11 Agent D）。
    """

    RUST_STRICT = "rust-strict"
    SHADOW = "shadow"
    PYTHON_REFERENCE = "python-reference"

    _ALL_MODES: Tuple[str, ...] = (RUST_STRICT, SHADOW, PYTHON_REFERENCE)

    @classmethod
    def get_active_mode(cls) -> str:
        """读取 ``CW_PARSE_MODE`` 环境变量并返回当前解析模式

        默认值：``rust-strict``（设计 §7 正式发布默认）

        Returns:
            当前激活的解析模式字符串（属于 ``_ALL_MODES``）

        Raises:
            ValueError: ``CW_PARSE_MODE`` 设置为未知值时
        """
        raw = os.environ.get("CW_PARSE_MODE", "").strip().lower()
        if not raw:
            return cls.RUST_STRICT
        if raw not in cls._ALL_MODES:
            raise ValueError(
                f"未知 CW_PARSE_MODE={raw!r}，应为 {cls._ALL_MODES} 之一"
            )
        return raw

    @classmethod
    def is_frozen_build(cls) -> bool:
        """检测当前是否运行在 PyInstaller frozen build 中

        frozen build 的判定：``sys.frozen`` 为真且存在 ``_MEIPASS`` 属性
        （PyInstaller 单文件/单目录打包的标志）。

        设计 §7：正式 frozen build 固定允许 ``rust-strict``，收到
        ``python-reference`` 或 ``CW_DISABLE_RUST_PARSE`` 时返回明确错误。
        """
        return bool(getattr(sys, "frozen", False)) and hasattr(sys, "_MEIPASS")

    @classmethod
    def validate_for_environment(cls, mode: Optional[str] = None) -> str:
        """校验解析模式在当前环境是否可用

        设计 §7 约束：
            - frozen build 仅允许 ``rust-strict``
            - frozen build 不允许 ``CW_DISABLE_RUST_PARSE``
            - frozen build 不允许 ``python-reference`` / ``shadow``

        Args:
            mode: 待校验的模式；None 时读取当前激活模式

        Returns:
            校验通过的模式字符串

        Raises:
            RuntimeError: frozen build 收到非 rust-strict 模式或 CW_DISABLE_RUST_PARSE
        """
        active = mode or cls.get_active_mode()
        if cls.is_frozen_build():
            if active != cls.RUST_STRICT:
                raise RuntimeError(
                    f"frozen build 仅允许 rust-strict 模式，"
                    f"当前 CW_PARSE_MODE={active!r}。frozen build 不支持 "
                    f"python-reference 或 shadow 模式（设计 §7）。"
                )
            if bool(os.environ.get("CW_DISABLE_RUST_PARSE")):
                raise RuntimeError(
                    "frozen build 不允许设置 CW_DISABLE_RUST_PARSE。"
                    "Rust parser 是 frozen build 的唯一解析路径（设计 §7）。"
                )
        return active

    @classmethod
    def shadow_diagnostics_path(cls) -> Optional[str]:
        """返回 shadow 模式独立 diagnostics 写入路径

        设计 §7：``shadow`` 结果写独立 diagnostics，不污染 CAS、manifest、snapshot。
        本方法仅返回路径，不创建目录；调用方（开发期 reference adapter）负责
        创建文件并写入差异结果。

        Returns:
            非 shadow 模式返回 None；shadow 模式返回
            ``$HOME/.callwarden/shadow_diagnostics/`` 路径字符串
        """
        try:
            if cls.get_active_mode() != cls.SHADOW:
                return None
        except ValueError:
            return None
        home = os.path.expanduser("~")
        return os.path.join(home, ".callwarden", "shadow_diagnostics")

    @classmethod
    def allows_python_reference(cls) -> bool:
        """检测当前模式是否允许调用 Python reference parser

        设计 §7：
            - ``python-reference``：仅此模式允许调用 Python parser
            - ``shadow``：Python reference 仅用于差异比较，不进入生产路径
              （仍可调用，但结果只写 diagnostics）
            - ``rust-strict``：禁止任何 Python parser 调用

        本方法供开发期 reference adapter 判断是否启动 Python 比较分支，
        生产路径（db_build / db_check_gate / db_external）不调用本方法。

        Returns:
            True 表示当前模式允许调用 Python reference parser
        """
        try:
            mode = cls.get_active_mode()
        except ValueError:
            return False
        return mode in (cls.SHADOW, cls.PYTHON_REFERENCE)


# ────────────────────────────────────────────────────────────────────
# Facade 主体
# ────────────────────────────────────────────────────────────────────


class RustParserFacade:
    """Rust parser 统一窄接口

    所有方法均为类方法或静态方法，无需实例化。Facade 不持有可变状态，
    可在多线程 / 多进程中安全共享。

    使用示例：

        from callwarden.db.rust_parser_facade import RustParserFacade

        # 单文件解析
        result = RustParserFacade.parse_file("/path/foo.py", "module.foo", "python")

        # 批量解析（多语言通用）
        pool = RustParserFacade.batch_parse_files_lang(
            files=[("/path/a.py", "mod.a"), ("/path/b.py", "mod.b")],
            language="python",
            num_threads=4,
        )

        # canonicalize + parse（消除 TOCTOU，daemon/CAS 路径用）
        canon = RustParserFacade.canonicalize_source("/path/foo.py")
        result = RustParserFacade.parse_canonical_bytes(
            canon["canonical_bytes"], "module.foo", "python", canon["content_hash"],
        )
    """

    # ─────────────────────────────────────────────
    # 可用性检测
    # ─────────────────────────────────────────────

    @staticmethod
    def is_available() -> bool:
        """检测 Rust 扩展是否可加载

        Returns:
            True 表示 ``callwarden_core`` 可 import 且至少一种 parse 接口可用
        """
        try:
            import callwarden_core  # noqa: F401
            return True
        except ImportError:
            return False

    @staticmethod
    def is_rust_disabled() -> bool:
        """检测是否通过环境变量强制关闭 Rust 路径

        设计文档 §7 ``CW_PARSE_MODE`` 的迁移期兼容开关。

        规则（P1-F Step 0）：
            - ``rust-strict`` 模式：始终返回 False（fail closed，不允许禁用 Rust）
            - ``shadow`` / ``python-reference`` 模式：尊重 ``CW_DISABLE_RUST_PARSE``
            - 未知 ``CW_PARSE_MODE``：保留旧行为（尊重 ``CW_DISABLE_RUST_PARSE``）
            - frozen build：始终返回 False（frozen build 的 Rust 是唯一解析路径）
        """
        # frozen build 强制 Rust，不允许禁用
        if ParseMode.is_frozen_build():
            return False
        try:
            mode = ParseMode.get_active_mode()
        except ValueError:
            # 未知 mode 保留旧行为
            return bool(os.environ.get("CW_DISABLE_RUST_PARSE"))
        if mode == ParseMode.RUST_STRICT:
            return False
        return bool(os.environ.get("CW_DISABLE_RUST_PARSE"))

    @staticmethod
    def supported_languages() -> Tuple[str, ...]:
        """Rust 多语言通用路径支持的语言列表（不含 C）

        Returns:
            语言标识元组；Rust 不可用时返回空元组
        """
        try:
            from callwarden_core import supported_languages
            return tuple(supported_languages())
        except ImportError:
            return ()

    @staticmethod
    def supports_language(lang: Optional[str]) -> bool:
        """检测 Rust 是否支持指定语言的解析

        Args:
            lang: 语言标识；None 视为 "c"（C 专用快路径）

        Returns:
            True 表示该语言可由 Rust 解析
        """
        if lang is None or lang == "c":
            # C 走专用快路径
            try:
                from callwarden_core import batch_parse_c_files_pool  # noqa: F401
                return True
            except ImportError:
                pass
            try:
                from callwarden_core import batch_parse_c_files  # noqa: F401
                return True
            except ImportError:
                return False
        return lang in RustParserFacade.supported_languages()

    # ─────────────────────────────────────────────
    # Canonicalize bytes（设计 §5.1 输入契约）
    # ─────────────────────────────────────────────

    @staticmethod
    def canonicalize_source(abs_path: str) -> Dict[str, Any]:
        """规范化源文件字节（BOM 剥离 + 编码检测 + CRLF→LF）

        设计 §5.1：所有生产入口必须从同一份 raw bytes 生成 canonical bytes，
        hash 与 parse 使用同一份 canonical bytes，禁止重新按路径读文件。

        Args:
            abs_path: 文件绝对路径

        Returns:
            dict 字段：
                - canonical_bytes: bytes（规范化后字节）
                - content_hash: str（sha256(canonical_bytes)）
                - canonical_total: int（canonical_bytes 长度）
                - raw_total: int（原始字节长度，含 BOM）
                - metadata: dict（raw_hash / source_encoding / bom_kind / newline_style）

        Raises:
            RuntimeError: Rust 扩展不可用或 canonicalize 失败（fail closed）
            FileNotFoundError: 文件不存在
        """
        try:
            from callwarden_core import canonicalize_source_py
        except ImportError as e:
            raise RuntimeError(
                "callwarden_core.canonicalize_source_py 不可用，"
                "Rust-only 路径不允许回退 Python parser（设计 §3.1.5）"
            ) from e
        return canonicalize_source_py(abs_path)

    # ─────────────────────────────────────────────
    # 单文件 parse
    # ─────────────────────────────────────────────

    @staticmethod
    def parse_file(abs_path: str, module_path: str, lang: str) -> Dict[str, Any]:
        """解析单个文件（按语言路由到对应 Rust 接口）

        C 语言走 ``parse_c_file``；其他语言走 ``parse_file_lang``。
        失败时返回带 ``error`` 字段的 dict，不抛异常（fail closed）。

        Args:
            abs_path: 文件绝对路径
            module_path: 模块路径（如 "module.foo"）
            lang: 语言标识（"c" / "python" / "rust" / ...）

        Returns:
            Rust parser 结果 dict，字段与 ``parse_file_lang`` 一致。
            失败时包含 ``error`` 字段（非空字符串），调用方应按失败处理。
        """
        if lang == "c":
            try:
                from callwarden_core import parse_c_file
            except ImportError as e:
                return {
                    "error": f"callwarden_core.parse_c_file 不可用: {e}",
                    "abs_path": abs_path,
                    "module_path": module_path,
                    "language": lang,
                }
            try:
                r = parse_c_file(abs_path, module_path)
                if r is None:
                    return {"error": "parse_c_file returned None", "abs_path": abs_path,
                            "module_path": module_path, "language": lang}
                return r
            except Exception as e:
                return {"error": f"parse_c_file 异常: {e}", "abs_path": abs_path,
                        "module_path": module_path, "language": lang}

        # 多语言通用路径
        try:
            from callwarden_core import parse_file_lang
        except ImportError as e:
            return {
                "error": f"callwarden_core.parse_file_lang 不可用: {e}",
                "abs_path": abs_path,
                "module_path": module_path,
                "language": lang,
            }
        try:
            r = parse_file_lang(abs_path, module_path, lang)
            if r is None:
                return {"error": "parse_file_lang returned None", "abs_path": abs_path,
                        "module_path": module_path, "language": lang}
            return r
        except Exception as e:
            return {"error": f"parse_file_lang 异常: {e}", "abs_path": abs_path,
                    "module_path": module_path, "language": lang}

    @staticmethod
    def parse_canonical_bytes(
        canonical_bytes: bytes,
        module_path: str,
        language: str,
        content_hash: str,
    ) -> Dict[str, Any]:
        """解析已规范化的 canonical bytes（不读文件）

        设计 §5.1：消除 TOCTOU，daemon 先 canonicalize + hash，再传同一份 bytes
        给 parser，CAS key 与 parse 来自同一份 canonical bytes。

        Args:
            canonical_bytes: 已规范化的字节（来自 ``canonicalize_source``）
            module_path: 模块路径
            language: 语言标识
            content_hash: canonical bytes 的 sha256（来自 ``canonicalize_source``）

        Returns:
            Rust parser 结果 dict；失败时包含 ``error`` 字段
        """
        try:
            from callwarden_core import parse_canonical_bytes_py
        except ImportError as e:
            return {
                "error": f"callwarden_core.parse_canonical_bytes_py 不可用: {e}",
                "module_path": module_path,
                "language": language,
                "content_hash": content_hash,
            }
        try:
            r = parse_canonical_bytes_py(canonical_bytes, module_path, language, content_hash)
            if r is None:
                return {"error": "parse_canonical_bytes_py returned None",
                        "module_path": module_path, "language": language,
                        "content_hash": content_hash}
            return r
        except Exception as e:
            return {"error": f"parse_canonical_bytes_py 异常: {e}",
                    "module_path": module_path, "language": language,
                    "content_hash": content_hash}

    # ─────────────────────────────────────────────
    # 批量 / 流式 parse
    # ─────────────────────────────────────────────

    @staticmethod
    def batch_parse_files_lang(
        files: List[Tuple[str, str]],
        language: str,
        num_threads: Optional[int] = None,
    ):
        """批量解析多语言文件（流式 pool，结果存 Rust 侧）

        Args:
            files: [(abs_path, module_path), ...]
            language: 语言标识（非 "c"）
            num_threads: 线程数（None = Rust 默认）

        Returns:
            ``ParseResultPool`` 对象（``pool.len()`` / ``pool.get_at(i)``）
            不可用时返回 None（调用方应按 fail closed 处理）

        Note:
            调用方应在 ``try/except`` 中处理 ImportError 与运行时异常，
            并对每个 ``pool.get_at(i)`` 检查 ``error`` 字段。
        """
        try:
            from callwarden_core import batch_parse_files_lang_pool
        except ImportError:
            return None
        try:
            return batch_parse_files_lang_pool(files, language, num_threads=num_threads)
        except Exception:
            return None

    @staticmethod
    def batch_parse_c_files(
        files: List[Tuple[str, str]],
        num_threads: Optional[int] = None,
    ):
        """批量解析 C 文件（流式 pool 优先，回退一次性 list）

        优先级：
            1. ``batch_parse_c_files_stream``（按完成顺序流式回传）
            2. ``batch_parse_c_files_pool``（结果存 Rust 侧，按 index get_at）
            3. ``batch_parse_c_files``（一次性 Python list）

        Args:
            files: [(abs_path, module_path), ...]
            num_threads: 线程数

        Returns:
            tuple ``(mode, result)``：
                - mode="stream": result 是 ``ParseResultStream``（迭代）
                - mode="pool": result 是 ``ParseResultPool``（按 index get_at）
                - mode="list": result 是 List[dict]
                - mode="none": result=None（调用方 fail closed）
        """
        # 1. stream 模式优先
        try:
            from callwarden_core import batch_parse_c_files_stream
            stream = batch_parse_c_files_stream(files, num_threads=num_threads)
            if stream is not None:
                return ("stream", stream)
        except Exception:
            pass

        # 2. pool 模式
        try:
            from callwarden_core import batch_parse_c_files_pool
            pool = batch_parse_c_files_pool(files, num_threads=num_threads)
            if pool is not None:
                return ("pool", pool)
        except Exception:
            pass

        # 3. list 模式（最旧接口，一次性转 Python list）
        try:
            from callwarden_core import batch_parse_c_files
            results = batch_parse_c_files(files, num_threads=num_threads)
            return ("list", results)
        except Exception:
            pass

        return ("none", None)

    # ─────────────────────────────────────────────
    # Diagnostics 与 generation metadata
    # ─────────────────────────────────────────────

    @staticmethod
    def extract_diagnostics(parse_result: Dict[str, Any]) -> Dict[str, Any]:
        """从 parse_result 中提取诊断信息

        设计 §5.2 输出契约：每个语言至少覆盖 syntax error count /
        unsupported construct count / partial parse marker / fatal parse error。

        Args:
            parse_result: Rust parser 返回的结果 dict

        Returns:
            dict 字段：
                - status: "ok" / "partial" / "unsupported" / "failed" / "stale"
                - syntax_error_count: int
                - unsupported_construct_count: int
                - fatal_parse_error: Optional[str]
                - partial_parse: bool
                - error: Optional[str]（兼容老接口的顶层 error 字段）
        """
        if not parse_result:
            return {"status": "failed", "syntax_error_count": 0,
                    "unsupported_construct_count": 0, "fatal_parse_error": "empty result",
                    "partial_parse": False, "error": "empty result"}

        top_error = parse_result.get("error")
        if top_error:
            # Rust parser 顶层 error → failed
            return {"status": "failed", "syntax_error_count": 0,
                    "unsupported_construct_count": 0,
                    "fatal_parse_error": str(top_error),
                    "partial_parse": False, "error": str(top_error)}

        # 多语言通用路径返回 parse_errors / unsupported_constructs 字段
        # （rust_ext/src/multi_lang.rs 的 ParseResult schema）
        syntax_errors = parse_result.get("parse_errors") or []
        unsupported = parse_result.get("unsupported_constructs") or []

        # 兼容 C 专用路径的字段名（parse_error / parse_errors）
        if isinstance(syntax_errors, int):
            syntax_error_count = syntax_errors
        else:
            syntax_error_count = len(syntax_errors) if isinstance(syntax_errors, list) else 0

        if isinstance(unsupported, int):
            unsupported_count = unsupported
        else:
            unsupported_count = len(unsupported) if isinstance(unsupported, list) else 0

        # 判定状态
        if syntax_error_count == 0 and unsupported_count == 0:
            status = "ok"
        elif unsupported_count > 0 and syntax_error_count == 0:
            # 仅 unsupported → partial（部分构造不支持但 parse 成功）
            status = "partial"
        else:
            # 有 syntax error → partial（仍发布了可用事实）
            status = "partial"

        return {
            "status": status,
            "syntax_error_count": syntax_error_count,
            "unsupported_construct_count": unsupported_count,
            "fatal_parse_error": None,
            "partial_parse": status == "partial",
            "error": None,
        }

    @staticmethod
    def generation_metadata(parse_result: Dict[str, Any]) -> Dict[str, Any]:
        """生成 generation 元数据（CAS key / parser ABI / hash）

        设计 §5.1：记录 encoding、BOM、newline style、raw hash 和 canonical hash。
        用于 CAS key 隔离、跨版本兼容判断和 stale generation 检测。

        Args:
            parse_result: Rust parser 返回的结果 dict（或 canonicalize_source 的输出）

        Returns:
            dict 字段：
                - content_hash: str（canonical bytes sha256）
                - parser_abi: str（Rust core_version）
                - total_lines: int
                - canonical_total: Optional[int]
                - raw_total: Optional[int]
                - metadata: Optional[dict]（来自 canonicalize_source）
        """
        return {
            "content_hash": parse_result.get("content_hash", ""),
            "parser_abi": _rust_core_version(),
            "total_lines": parse_result.get("total_lines", 0),
            "canonical_total": parse_result.get("canonical_total"),
            "raw_total": parse_result.get("raw_total"),
            "metadata": parse_result.get("metadata"),
        }

    # ─────────────────────────────────────────────
    # 结果归一化（兼容 _save_symbols_for_version 所需字段）
    # ─────────────────────────────────────────────

    @staticmethod
    def normalize_result(
        result: Dict[str, Any],
        abs_path: str,
        module_path: str,
        rel_path: str,
        file_instance_id: int,
    ) -> Dict[str, Any]:
        """归一化 Rust parser 输出，补齐 _save_symbols_for_version 所需字段

        从 db_build.py 的 _normalize_rust_symbols 抽取，统一补齐：
            - start_col / end_col（Rust 不返回，默认 0）
            - has_comment：bool → int
            - imports: List[str] → List[Dict]
            - abs_path / module_path / rel_path / file_instance_id / inline_modules

        Args:
            result: Rust parser 返回的原始结果 dict
            abs_path: 文件绝对路径
            module_path: 模块路径
            rel_path: 相对路径
            file_instance_id: 文件实例 ID

        Returns:
            归一化后的 result dict（in-place 修改并返回）
        """
        # 顶层元数据
        result["abs_path"] = abs_path
        result["file_instance_id"] = file_instance_id
        result["module_path"] = module_path
        result["rel_path"] = rel_path
        result.setdefault("inline_modules", [])

        # 符号字段补齐
        for sym in result.get("symbols", []):
            sym.setdefault("start_col", 0)
            sym.setdefault("end_col", 0)
            if isinstance(sym.get("has_comment"), bool):
                sym["has_comment"] = int(sym["has_comment"])
        for mod in result.get("inline_modules", []):
            for sym in mod.get("symbols", []):
                sym.setdefault("start_col", 0)
                sym.setdefault("end_col", 0)
                if isinstance(sym.get("has_comment"), bool):
                    sym["has_comment"] = int(sym["has_comment"])

        # imports: Rust 返回 List[str]，Python 旧路径返回 List[Dict]
        imports = result.get("imports")
        if imports and isinstance(imports[0], str):
            result["imports"] = [{"module": m} for m in imports]

        # Python docstring 检测（Rust make_symbol 不检测 docstring）
        # 仅对 .py 文件生效，使用 ast 模块补全 has_comment / comment_content
        if rel_path and rel_path.endswith(".py"):
            RustParserFacade._detect_python_docstrings(result.get("symbols", []))
            for mod in result.get("inline_modules", []):
                RustParserFacade._detect_python_docstrings(mod.get("symbols", []))

        return result

    @staticmethod
    def _detect_python_docstrings(symbols: List[Dict[str, Any]]) -> None:
        """检测 Python 符号的 docstring，补全 has_comment / comment_content

        Rust make_symbol 不检测 docstring（硬编码 has_comment=false），
        此函数用 ast.parse 解析符号 content，检测首条语句是否为字符串字面量。
        仅对 has_comment=0 的符号生效，避免覆盖已有结果。
        """
        import ast
        for sym in symbols:
            if sym.get("has_comment"):
                continue
            content = sym.get("content", "")
            if not content or len(content) < 12:
                continue
            try:
                tree = ast.parse(content)
                if not tree.body:
                    continue
                node = tree.body[0]
                if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                    continue
                if not node.body:
                    continue
                first_stmt = node.body[0]
                if isinstance(first_stmt, ast.Expr):
                    val = first_stmt.value
                    if isinstance(val, ast.Constant) and isinstance(val.value, str):
                        sym["has_comment"] = 1
                        sym["comment_content"] = val.value
                    elif hasattr(ast, "Str") and isinstance(val, ast.Str):
                        sym["has_comment"] = 1
                        sym["comment_content"] = val.s
            except (SyntaxError, ValueError):
                pass


# ────────────────────────────────────────────────────────────────────
# 模块级单例（便于直接 import 使用）
# ────────────────────────────────────────────────────────────────────

# 推荐用法：
#     from callwarden.db.rust_parser_facade import rust_parser_facade
#     result = rust_parser_facade.parse_file(...)
rust_parser_facade = RustParserFacade()
