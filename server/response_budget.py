"""MCP 响应预算（参考 GitNexus docs/mcp-reference.md 响应预算设计）

动机：全量 MCP 工具返回动辄数千 token 的列表/映射，膨胀 agent 的
prompt context。本模块在 MCP 工具出口径做确定性预算约束：

- 预算来源：``CALLWARDEN_MCP_DEFAULT_MAX_TOKENS`` env（默认 0 = 不限制，
  与 GitNexus "缺省行为不变" 语义一致；非法值启动期 fail-closed）。
- 估算：4 字节/token（GitNexus 同款），对紧凑 JSON 序列化结果取整，
  确定性可复现（同一结果同一预算必得同一裁剪）。
- 裁剪：deepest-first——优先裁最深层列表的尾部（每层至少保留 1 条），
  逐层回退直到落入预算或无可裁列表。
- 标记：裁剪发生后在根 dict 注入 ``_budget`` 元数据（GitNexus
  ``pagination.trimmedForBudget.shownByDepth`` 同构），含裁剪前/后
  估算 token、各路径 shown/dropped 计数。根非 dict（如 -> list 工具）
  时仅就地裁剪、不注入元数据（保持输出模型签名不变）。
"""

from __future__ import annotations

import json
import os
from typing import Any, Dict, List, Tuple

_BYTES_PER_TOKEN = 4

_BUDGET_KEY = "_budget"

EnvError = ValueError


def default_max_tokens() -> int:
    """读取 CALLWARDEN_MCP_DEFAULT_MAX_TOKENS（0 = 不限制）。

    非法值 fail-closed 抛 EnvError（与 GitNexus "非法值启动失败" 同款，
    静默回退会悄悄放弃上下文保护）。
    """
    raw = os.environ.get("CALLWARDEN_MCP_DEFAULT_MAX_TOKENS", "").strip()
    if not raw:
        return 0
    try:
        value = int(raw)
    except ValueError as exc:
        raise EnvError(
            "CALLWARDEN_MCP_DEFAULT_MAX_TOKENS 必须是非负整数"
            f"（0=不限制），当前值：{raw!r}"
        ) from exc
    if value < 0:
        raise EnvError(
            "CALLWARDEN_MCP_DEFAULT_MAX_TOKENS 不能为负（0=不限制），"
            f"当前值：{raw!r}"
        )
    return value


def estimate_tokens(value: Any) -> int:
    """确定性 token 估算：紧凑 JSON 序列化字节数 / 4（GitNexus 同款）。"""
    if value is None:
        return 0
    try:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        payload = str(value)
    return max(1, len(payload.encode("utf-8")) // _BYTES_PER_TOKEN)


def _deepest_lists(
    node: Any, path: str, depth: int, out: List[Tuple[str, int, list]]
) -> None:
    """收集全部 list 节点（路径、深度、引用），供 deepest-first 裁剪。"""
    if isinstance(node, dict):
        for key, child in node.items():
            _deepest_lists(child, f"{path}.{key}" if path else str(key), depth + 1, out)
    elif isinstance(node, list):
        out.append((path, depth, node))
        for i, child in enumerate(node):
            _deepest_lists(child, f"{path}[{i}]", depth + 1, out)


def trim_to_budget(result: Any, max_tokens: int) -> Any:
    """就地 deepest-first 裁剪直到落入预算；返回 (裁剪后结果, 是否裁剪)。

    每轮选深度最大的列表（同深取先出现者）删尾项（保留 ≥1 条），直到
    估算 token ≤ max_tokens 或所有列表都只剩 1 条。
    """
    if max_tokens <= 0 or not isinstance(result, (dict, list)):
        return result, False
    if estimate_tokens(result) <= max_tokens:
        return result, False

    dropped: Dict[str, int] = {}
    shown: Dict[str, int] = {}
    # 元数据本身占 token：先注入占位再裁剪；回填 shown_by_depth 后可能
    # 再次越界，故「裁剪→回填→复检」迭代到稳定（最多 3 轮）。
    if isinstance(result, dict):
        result[_BUDGET_KEY] = {"trimmed": True, "max_tokens": max_tokens}
    for _round in range(3):
        while estimate_tokens(result) > max_tokens:
            lists: List[Tuple[str, int, list]] = []
            _deepest_lists(result, "", 0, lists)
            # deepest-first；同深度按出现顺序（栈序=先根后子，稳定）
            target = None
            for path, depth, lst in lists:
                if len(lst) > 1:
                    if target is None or depth > target[1]:
                        target = (path, depth, lst)
            if target is None:
                break
            path, _depth, lst = target
            lst.pop()
            dropped[path] = dropped.get(path, 0) + 1
            shown[path] = len(lst)
        if isinstance(result, dict):
            result[_BUDGET_KEY] = {
                "trimmed": True,
                "max_tokens": max_tokens,
                "estimated_tokens_after": estimate_tokens(result),
                "strategy": "deepest_first_drop_tail",
                "shown_by_depth": [
                    {"path": p, "shown": shown[p], "dropped": dropped[p]}
                    for p in sorted(dropped, key=lambda k: (-dropped[k], k))
                ],
            }
        if estimate_tokens(result) <= max_tokens:
            break
    return result, True


def enforce_budget(result: Any, max_tokens: int | None = None) -> Any:
    """MCP 工具出口预算钩子：max_tokens 缺省取 env，0/None 不限制。"""
    if max_tokens is None:
        max_tokens = default_max_tokens()
    if max_tokens <= 0:
        return result
    trimmed, _ = trim_to_budget(result, max_tokens)
    return trimmed
