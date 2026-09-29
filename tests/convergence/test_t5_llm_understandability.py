"""T5:真实 LLM(DeepSeek)可理解性端到端(收敛套件)。

给 LLM 一批 MCP 工具的 name+description(+params),外加自然语言意图,要求它
以 JSON 返回选择的工具。评估工具文档能否让 LLM 正确选工具,定位命名/描述歧义。

需 .env 配置 OPENAI_API_KEY/OPENAI_BASE_URL(DeepSeek 兼容);未配置则 skip。
LLM 有非确定性,故断言用基线阈值(不要求 100%),防可理解性回退。

首轮实测(deepseek-v4-flash,2026-09-30):
  - 窄候选选对+参数覆盖:15/15
  - 全量 243 纯名字候选:9/10(唯一失败 detect_cycle/detect_cycles 命名歧义)
  - 易混淆带描述精确区分:7/7
发现:detect_cycle(硬依赖图环)/detect_cycles(循环调用)命名仅差 1 字母,
语义不同(依赖图 vs 调用图),LLM 在纯名字候选下无法区分 → 命名歧义(非缺陷,
两工具都可用),建议改名或描述加前缀。
"""
from __future__ import annotations

import json
import os

import pytest

from llm_channel import LLMChannel, llm_available

_FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
_SCHEMA = os.path.join(_FIXTURES, "mcp_full_schema.json")

pytestmark = pytest.mark.skipif(
    not llm_available(),
    reason="未配置 OPENAI_API_KEY/OPENAI_BASE_URL(.env);T5 LLM 可理解性测试跳过",
)


def _load_schema():
    with open(_SCHEMA, encoding="utf-8") as f:
        return {t["name"]: t for t in json.load(f)["tools"]}


def _card(tools_by_name, name):
    t = tools_by_name.get(name) or {}
    desc = (t.get("description") or "").strip()
    head = desc.split("Args:")[0].split("参数")[0].strip()[:280]
    return {"name": name, "summary": head, "required": t.get("required") or []}


_SYS = (
    "你是代码分析助手。根据用户意图从候选工具中选唯一最合适的,"
    '只返回 JSON {"tool":"名字","arguments":{...}}。工具名必须来自候选。'
    "arguments 含该工具全部 required 参数。不输出其它文字。"
)

# (意图, 候选工具, 可接受期望集合)
_TASKS = [
    ("哪些函数调用了 process_order",
     ["get_callers", "get_callees", "get_call_chain_down", "search_symbols"],
     {"get_callers"}),
    ("process_order 内部调用了哪些函数",
     ["get_callers", "get_callees", "get_call_chain_down", "get_symbol"],
     {"get_callees"}),
    ("搜索名字含 auth 的函数和类",
     ["search_symbols", "get_symbol", "get_symbol_location", "file_grep"],
     {"search_symbols"}),
    ("获取代码库统计(文件/函数/调用数)",
     ["get_stats", "get_status", "health_report", "project_brief"],
     {"get_stats", "get_status"}),
    ("修改 UserService.login 会影响哪些符号",
     ["get_impact", "get_callers", "blast_radius", "cross_layer_impact"],
     {"get_impact", "blast_radius"}),
    ("payment.py 定义了哪些符号",
     ["get_file_symbols", "file_read", "search_symbols", "get_symbol"],
     {"get_file_symbols"}),
    ("创建新任务:标题'重构支付模块'",
     ["task_create", "task_split", "task_create_subtask", "assignment_create"],
     {"task_create"}),
    ("获取 calculate_tax 的复杂度、行数度量",
     ["get_function_metrics", "get_complexity_hotspots", "get_largest_functions", "get_symbol"],
     {"get_function_metrics"}),
    ("读取 config.py 完整内容",
     ["file_read", "get_file_symbols", "file_grep", "get_symbol"],
     {"file_read"}),
    ("正则搜索代码文本 TODO",
     ["file_grep", "search_symbols", "get_issue_summary", "find_issues"],
     {"file_grep"}),
]

# 首轮基线:窄候选 10/10。LLM 非确定,阈值设 0.8 防回退。
_MIN_CORRECT_RATE = 0.8


def test_llm_tool_selection_understandability():
    """LLM 按工具 description 选对率不低于基线阈值。"""
    tools_by_name = _load_schema()
    ch = LLMChannel()
    correct = 0
    detail = []
    for intent, candidates, expected in _TASKS:
        cards = [_card(tools_by_name, c) for c in candidates]
        lines = ["候选工具:"]
        for c in cards:
            lines.append(f"- {c['name']}: {c['summary']} (required={c['required']})")
        lines.append(f"\n用户意图:{intent}")
        user = "\n".join(lines)
        try:
            r = ch.chat([{"role": "system", "content": _SYS},
                         {"role": "user", "content": user}],
                        temperature=0.0, max_tokens=800, json_mode=True)
            chosen = (json.loads(r["content"]) if r["content"] else {}).get("tool", "")
        except Exception as e:  # noqa: BLE001
            chosen = f"<error:{type(e).__name__}>"
        ok = chosen in expected
        correct += ok
        detail.append(f"{'OK' if ok else 'XX'} {intent} -> {chosen} (期望{sorted(expected)})")

    rate = correct / len(_TASKS)
    assert rate >= _MIN_CORRECT_RATE, (
        f"LLM 工具选对率 {rate:.2f} < 基线 {_MIN_CORRECT_RATE}\n" + "\n".join(detail)
    )
