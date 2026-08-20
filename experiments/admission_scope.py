"""G0 纳样前的任务范围与实现证据契约。

历史批次的 JSONL 不可回写。本模块只用于新批次纳样前校验：Creator 必须提供
一个 batch-specific scope contract，声明任务 profile、必需变更路径和允许变更路径。
"""

from __future__ import annotations

import fnmatch
from typing import Any, Dict, Iterable, List, Mapping


class ScopeContractError(ValueError):
    """纳样范围契约不满足时抛出。"""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


_DOC_SUFFIXES = (".md", ".rst", ".txt", ".adoc")
_SOURCE_SUFFIXES = (
    ".py", ".pyi", ".rs", ".ts", ".tsx", ".js", ".jsx", ".go", ".java",
    ".kt", ".kts", ".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".cs",
    ".rb", ".php", ".swift", ".scala", ".ex", ".exs", ".hcl",
)
_GENERATED_MARKERS = ("/__pycache__/", "\\__pycache__\\", "/target/", "\\target\\", "/dist/", "\\dist\\")


def _norm(path: Any) -> str:
    normalized = str(path or "").replace("\\", "/").strip()
    while normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized.lower()


def _is_document(path: str) -> bool:
    return path.endswith(_DOC_SUFFIXES)


def _is_source_file(path: str) -> bool:
    return path.endswith(_SOURCE_SUFFIXES)


def _is_generated(path: str) -> bool:
    normalized = f"/{path}/"
    return any(marker in normalized for marker in _GENERATED_MARKERS)


def _matches(path: str, patterns: Iterable[str]) -> bool:
    normalized = _norm(path)
    for pattern in patterns:
        candidate = _norm(pattern)
        if candidate and (fnmatch.fnmatch(normalized, candidate)
                          or normalized == candidate
                          or normalized.startswith(candidate.rstrip("/") + "/")):
            return True
    return False


def _changed_paths(source: Any) -> List[str]:
    rows = getattr(source, "change_audit_diffs", []) or []
    paths = {_norm(row.get("file_path")) for row in rows if isinstance(row, Mapping)}
    return sorted(path for path in paths if path)


def validate_scope_contract(
    source: Any, contract: Mapping[str, Any], *, tracked_paths: Iterable[str] | None = None,
) -> Dict[str, Any]:
    """校验任务实际 diff 是否满足 Creator 声明的范围契约。"""
    if not isinstance(contract, Mapping):
        raise ScopeContractError("EXP_SCOPE_CONTRACT_INVALID", "top-level JSON value must be an object")
    task_id = str(getattr(source, "task_id", ""))
    expected_id = str(contract.get("task_id") or "")
    if expected_id and expected_id != task_id:
        raise ScopeContractError("EXP_SCOPE_TASK_MISMATCH", f"expected={expected_id}, actual={task_id}")

    profile = str(contract.get("profile") or "").strip().lower()
    if profile not in {"design", "code_change", "review"}:
        raise ScopeContractError("EXP_SCOPE_PROFILE_INVALID", f"profile={profile!r}")

    changed = _changed_paths(source)
    if not changed:
        raise ScopeContractError("EXP_SCOPE_NO_CHANGE", "change_audit has no tracked changed file")

    if not bool(contract.get("allow_generated", False)):
        generated = [path for path in changed if _is_generated(path)]
        if generated:
            raise ScopeContractError("EXP_SCOPE_GENERATED_CHANGE", ", ".join(generated[:10]))

    required = [_norm(path) for path in contract.get("required_paths", []) if _norm(path)]
    if not required:
        raise ScopeContractError("EXP_SCOPE_REQUIRED_PATHS_MISSING", "required_paths must be non-empty")
    # required_paths 既可以是精确文件，也可以是目录/通配符；逐个变更路径
    # 以 required pattern 匹配，避免目录声明被当作精确文件而误拒绝。
    missing = [path for path in required if not any(_matches(changed_path, [path]) for changed_path in changed)]
    if missing:
        raise ScopeContractError("EXP_SCOPE_REQUIRED_PATH_MISSING", ", ".join(missing))

    allowed = [_norm(path) for path in contract.get("allowed_paths", []) if _norm(path)]
    if not allowed:
        raise ScopeContractError("EXP_SCOPE_ALLOWED_PATHS_MISSING", "allowed_paths must be non-empty")
    outside = [path for path in changed if not _matches(path, allowed)]
    if outside:
        raise ScopeContractError("EXP_SCOPE_OUTSIDE_ALLOWED_PATHS", ", ".join(outside[:10]))

    source_paths = [path for path in changed if _is_source_file(path)]
    if profile == "code_change" and not source_paths:
        raise ScopeContractError(
            "EXP_SCOPE_CODE_EVIDENCE_MISSING",
            "code_change requires at least one non-document tracked source file",
        )
    if profile == "code_change" and not any(_is_source_file(path) for path in required):
        raise ScopeContractError(
            "EXP_SCOPE_IMPLEMENTATION_PATH_MISSING",
            "code_change required_paths must include an implementation source path",
        )
    if profile == "code_change" and tracked_paths is not None:
        tracked = {_norm(path) for path in tracked_paths if _norm(path)}
        untracked = [path for path in source_paths if path not in tracked]
        if untracked:
            raise ScopeContractError("EXP_SCOPE_SOURCE_NOT_TRACKED", ", ".join(untracked))

    return {
        "task_id": task_id,
        "profile": profile,
        "changed_paths": changed,
        "required_paths": required,
        "allowed_paths": allowed,
        "source_file_count": len(source_paths),
    }
