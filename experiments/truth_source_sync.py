"""多 LLM 契约三份真相源的同步前置检查。

这个检查只判断三份指定文档是否存在、非空并包含共同的阶段/门禁锚点；它不把
AGENTS.md、README 或其他说明文件当成替代真相源。详细语义仍由三份文档本身定义。
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, Mapping


TRUTH_SOURCE_PATHS = (
    "docs/design/requirements.md",
    "docs/design/multi-llm-contract-driven-collaboration-design.md",
    "docs/design/tasks.md",
)


def _normalized_paths(paths: Iterable[str]) -> tuple[str, ...]:
    """规范化路径，避免 Windows 分隔符让范围核验失效。"""
    return tuple(
        str(path).replace("\\", "/").strip().lstrip("./").lower()
        for path in paths
        if str(path).strip()
    )


class TruthSourceSyncError(ValueError):
    """三份真相源无法作为一致集合读取。"""

    def __init__(self, code: str, detail: str):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


def validate_truth_sources(root: str | Path, required_anchors: Iterable[str] = ()) -> Dict[str, object]:
    """读取并校验三份真相源，返回可审计的路径/hash 输入信息。"""
    base = Path(root)
    contents: Dict[str, str] = {}
    missing = []
    for relative in TRUTH_SOURCE_PATHS:
        path = base / relative
        if not path.is_file():
            missing.append(relative)
            continue
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise TruthSourceSyncError("EXP_TRUTH_SOURCE_EMPTY", relative)
        contents[relative] = text
    if missing:
        raise TruthSourceSyncError("EXP_TRUTH_SOURCE_MISSING", ", ".join(missing))

    anchors = tuple(str(anchor) for anchor in required_anchors if str(anchor).strip())
    absent = {
        relative: [anchor for anchor in anchors if anchor not in text]
        for relative, text in contents.items()
    }
    absent = {relative: values for relative, values in absent.items() if values}
    if absent:
        detail = "; ".join(f"{path}: {','.join(values)}" for path, values in absent.items())
        raise TruthSourceSyncError("EXP_TRUTH_SOURCE_ANCHOR_MISSING", detail)

    return {
        "paths": list(TRUTH_SOURCE_PATHS),
        "sizes": {relative: len(text.encode("utf-8")) for relative, text in contents.items()},
        "required_anchors": list(anchors),
    }


def validate_truth_source_change_set(
    changed_paths: Iterable[str], required_paths: Iterable[str], allowed_paths: Iterable[str],
) -> Dict[str, object]:
    """校验声明为三份规格同步的任务，不能用 AGENTS.md 伪造完成。"""
    expected = _normalized_paths(TRUTH_SOURCE_PATHS)
    changed = _normalized_paths(changed_paths)
    required = _normalized_paths(required_paths)
    allowed = _normalized_paths(allowed_paths)

    missing_declared = [path for path in expected if path not in required]
    if missing_declared:
        raise TruthSourceSyncError(
            "EXP_TRUTH_SOURCE_REQUIRED_PATH_MISSING", ", ".join(missing_declared))
    if "agents.md" in allowed:
        raise TruthSourceSyncError(
            "EXP_TRUTH_SOURCE_AGENTS_SUBSTITUTE_FORBIDDEN", "AGENTS.md")
    missing_changed = [path for path in expected if path not in changed]
    if missing_changed:
        raise TruthSourceSyncError(
            "EXP_TRUTH_SOURCE_CHANGE_MISSING", ", ".join(missing_changed))
    outside = [path for path in changed if path not in allowed]
    if outside:
        raise TruthSourceSyncError("EXP_TRUTH_SOURCE_OUTSIDE_ALLOWED_PATHS", ", ".join(outside))

    return {
        "changed_paths": list(changed),
        "required_paths": list(required),
        "allowed_paths": list(allowed),
        "truth_source_paths": list(expected),
    }
