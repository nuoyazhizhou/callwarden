"""AbiContractService —— 生产代码访问 ABI 与错误码契约的查询服务（Phase 0 子任务 2 Step 4）

设计文档：docs/design/abi-error-code-contract.md
真相源：docs/design/abi-error-code-contract.md + rust_ext/src/abi_contract.rs

本服务为生产代码（CLI/MCP/daemon）提供 ABI 契约的运行时查询能力：
    - 错误码枚举查询（exit_code、is_retryable、as_str）
    - ParseStatus 状态推导（from_diagnostics）
    - CAS 状态查询（ready/partial/building）
    - ABI 版本常量查询

设计原则：
    - 只读：本服务只读取常量，不修改
    - 无状态：所有方法都是纯函数
    - 无锁：不访问数据库，无 SQLite 锁冲突
    - Python 端镜像 Rust abi_contract 模块的常量和枚举
    - 后续 Phase 1+ 可通过 PyO3 直接调用 Rust 模块，本服务作为过渡

错误语义：
    - 所有查询返回明确值，不抛异常
    - 未知错误码返回 None
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


# ============================================
# ABI 版本常量（镜像 rust_ext/src/abi_contract.rs）
# ============================================

ABI_VERSION = "v1"
INPUT_ABI_VERSION = "v1"
EXTRACTION_CONFIG_VERSION = "v1"

# Schema 版本（真相源在 db/schema.py，此处为镜像常量）
# 变更时必须同步更新 db/schema.py 和 abi-error-code-contract.md
SCHEMA_VERSION = 41

# CAS 状态常量
CAS_STATE_BUILDING = "building"
CAS_STATE_READY = "ready"
CAS_STATE_PARTIAL = "partial"

# GraphStore 加载状态
LOAD_STATE_EMPTY = "empty"
LOAD_STATE_SYMBOLS_READY = "symbols_ready"
LOAD_STATE_GRAPH_READY = "graph_ready"

# workspace_id=0 表示不过滤（兼容旧测试和单 workspace DB）
WORKSPACE_ID_UNFILTERED = 0


# ============================================
# ParseStatus 枚举（镜像 Rust ParseStatus）
# ============================================

class ParseStatus:
    """解析状态枚举。

    对应 abi_contract.rs 的 ParseStatus：
        - Ok: 解析成功
        - Partial: 语法错误，结果可用但不替换 snapshot
        - Failed: 解析失败
        - Unsupported: 不支持的语言/构造
    """

    OK = "ok"
    PARTIAL = "partial"
    FAILED = "failed"
    UNSUPPORTED = "unsupported"

    @staticmethod
    def from_diagnostics(
        syntax_error_count: int,
        unsupported_construct_count: int,
        fatal: bool = False,
    ) -> str:
        """根据诊断字段推导解析状态。

        对应 abi-error-code-contract.md §1.5 状态推导规则：
            - fatal → failed
            - syntax=0, unsupported=0 → ok
            - syntax>0, unsupported=0 → partial
            - syntax=0, unsupported>0 → unsupported
            - syntax>0, unsupported>0 → partial
        """
        if fatal:
            return ParseStatus.FAILED
        if syntax_error_count == 0 and unsupported_construct_count == 0:
            return ParseStatus.OK
        if unsupported_construct_count > 0 and syntax_error_count == 0:
            return ParseStatus.UNSUPPORTED
        return ParseStatus.PARTIAL

    @staticmethod
    def should_publish_to_cas(status: str) -> bool:
        """是否应该发布到 CAS。"""
        return status in (ParseStatus.OK, ParseStatus.PARTIAL)

    @staticmethod
    def cas_state(status: str) -> str:
        """CAS 状态（发布时使用）。"""
        if status == ParseStatus.OK:
            return CAS_STATE_READY
        if status == ParseStatus.PARTIAL:
            return CAS_STATE_PARTIAL
        raise ValueError(
            f"ParseStatus {status!r} 不应该发布到 CAS"
        )

    @staticmethod
    def should_replace_snapshot(status: str) -> bool:
        """是否替换上一代 snapshot。"""
        return status == ParseStatus.OK


# ============================================
# ErrorCode 枚举（镜像 Rust ErrorCode）
# ============================================

@dataclass(frozen=True)
class _ErrorCodeInfo:
    """错误码元数据。"""
    code: str
    exit_code: int
    is_retryable: bool
    description: str


_ERROR_CODE_REGISTRY: dict[str, _ErrorCodeInfo] = {
    "PARSE_OK": _ErrorCodeInfo("PARSE_OK", 0, False, "解析成功"),
    "PARSE_PARTIAL": _ErrorCodeInfo("PARSE_PARTIAL", 0, False, "语法错误，结果可用但不替换 snapshot"),
    "PARSE_FAILED": _ErrorCodeInfo("PARSE_FAILED", 1, False, "解析失败"),
    "PARSE_UNSUPPORTED": _ErrorCodeInfo("PARSE_UNSUPPORTED", 1, False, "不支持的语言/构造"),
    "PARSE_FATAL": _ErrorCodeInfo("PARSE_FATAL", 2, False, "不可恢复错误（OOM/IO）"),
    "CAS_LOCKED": _ErrorCodeInfo("CAS_LOCKED", 2, True, "CAS 写锁冲突"),
    "DB_LOCKED": _ErrorCodeInfo("DB_LOCKED", 2, True, "SQLite 写锁冲突"),
    "SNAPSHOT_STALE": _ErrorCodeInfo("SNAPSHOT_STALE", 1, False, "stale generation"),
    "ACL_DENIED": _ErrorCodeInfo("ACL_DENIED", 3, False, "UID/workspace 权限不足"),
    "BUDGET_EXCEEDED": _ErrorCodeInfo("BUDGET_EXCEEDED", 3, False, "资源预算超限"),
    "RECOVERY_FAILED": _ErrorCodeInfo("RECOVERY_FAILED", 2, False, "恢复失败"),
    "TRANSPORT_ERROR": _ErrorCodeInfo("TRANSPORT_ERROR", 2, False, "UDS 传输错误"),
}


def list_error_codes() -> list[str]:
    """列出所有错误码。"""
    return list(_ERROR_CODE_REGISTRY.keys())


def get_error_code_info(code: str) -> Optional[_ErrorCodeInfo]:
    """查询错误码元数据。"""
    return _ERROR_CODE_REGISTRY.get(code)


def get_exit_code(code: str) -> int:
    """获取错误码对应的 exit code。未知错误码返回 2。"""
    info = _ERROR_CODE_REGISTRY.get(code)
    return info.exit_code if info else 2


def is_retryable(code: str) -> bool:
    """判断错误码是否可重试。"""
    info = _ERROR_CODE_REGISTRY.get(code)
    return info.is_retryable if info else False


def error_code_from_parse_status(status: str, fatal: bool = False) -> str:
    """ParseStatus 到 ErrorCode 的映射。"""
    if fatal:
        return "PARSE_FATAL"
    mapping = {
        ParseStatus.OK: "PARSE_OK",
        ParseStatus.PARTIAL: "PARSE_PARTIAL",
        ParseStatus.FAILED: "PARSE_FAILED",
        ParseStatus.UNSUPPORTED: "PARSE_UNSUPPORTED",
    }
    return mapping.get(status, "PARSE_FAILED")


# ============================================
# 生产查询服务
# ============================================

class AbiContractService:
    """ABI 契约查询服务（只读/无状态/无锁）。

    用法：
        service = AbiContractService()
        status = service.parse_status_from_diagnostics(1, 0)
        error_code = service.error_code_from_parse_status(status)
        exit_code = service.get_exit_code(error_code)
    """

    @staticmethod
    def parse_status_from_diagnostics(
        syntax_error_count: int,
        unsupported_construct_count: int,
        fatal: bool = False,
    ) -> str:
        """根据诊断字段推导解析状态。"""
        return ParseStatus.from_diagnostics(
            syntax_error_count, unsupported_construct_count, fatal
        )

    @staticmethod
    def error_code_from_parse_status(status: str, fatal: bool = False) -> str:
        """ParseStatus 到 ErrorCode 的映射。"""
        return error_code_from_parse_status(status, fatal)

    @staticmethod
    def get_exit_code(error_code: str) -> int:
        """获取错误码对应的 exit code。"""
        return get_exit_code(error_code)

    @staticmethod
    def is_retryable(error_code: str) -> bool:
        """判断错误码是否可重试。"""
        return is_retryable(error_code)

    @staticmethod
    def should_publish_to_cas(parse_status: str) -> bool:
        """是否应该发布到 CAS。"""
        return ParseStatus.should_publish_to_cas(parse_status)

    @staticmethod
    def cas_state_for_status(parse_status: str) -> str:
        """ParseStatus 对应的 CAS 状态。"""
        return ParseStatus.cas_state(parse_status)

    @staticmethod
    def should_replace_snapshot(parse_status: str) -> bool:
        """是否替换上一代 snapshot。"""
        return ParseStatus.should_replace_snapshot(parse_status)

    @staticmethod
    def list_error_codes() -> list[str]:
        """列出所有错误码。"""
        return list_error_codes()

    @staticmethod
    def get_abi_versions() -> dict:
        """返回 ABI 版本常量。"""
        return {
            "abi_version": ABI_VERSION,
            "input_abi_version": INPUT_ABI_VERSION,
            "extraction_config_version": EXTRACTION_CONFIG_VERSION,
            "schema_version": SCHEMA_VERSION,
        }

    @staticmethod
    def get_cas_states() -> dict:
        """返回 CAS 状态常量。"""
        return {
            "building": CAS_STATE_BUILDING,
            "ready": CAS_STATE_READY,
            "partial": CAS_STATE_PARTIAL,
        }

    @staticmethod
    def get_load_states() -> dict:
        """返回 GraphStore 加载状态常量。"""
        return {
            "empty": LOAD_STATE_EMPTY,
            "symbols_ready": LOAD_STATE_SYMBOLS_READY,
            "graph_ready": LOAD_STATE_GRAPH_READY,
        }
