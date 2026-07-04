"""从映射表中提取 _handle_impact 模块的条目，生成审核报告"""
import json
import sys

with open('scripts/i18n_cli_main_map.json', 'r', encoding='utf-8') as f:
    mapping = json.load(f)

mappings = mapping['mappings']

# 筛选 _handle_impact 的条目
impact_items = {}
for key, val in mappings.items():
    if '_handle_impact' in val.get('functions', []):
        impact_items[key] = val

print(f"_handle_impact 模块: {len(impact_items)} 个唯一 key")
print()

for i, (key, val) in enumerate(impact_items.items(), 1):
    zh = val['zh']
    has_vars = val['has_vars']
    var_names = val['var_names']
    occurrences = val['occurrences']
    
    print(f"--- {i}. key: {key}")
    print(f"    zh: {zh}")
    if has_vars:
        print(f"    变量: {var_names}")
    for occ in occurrences:
        print(f"    行{occ['line']:4d} [{occ['type']:6s}] fstring={occ['is_fstring']}")
    print()

# 输出建议的 key 命名方案
print("=" * 60)
print("建议的 key 命名方案（人工审核后）:")
print("=" * 60)
print()
suggested_keys = {
    "impact_title": "=== 变更影响半径分析 ===",
    "impact_symbol_not_found": "  ✗ 未找到符号: {symbol_hash}",
    "impact_source_symbol": "  源符号: {symbol}",
    "impact_source_hash": "  源 hash: {hash}...",
    "impact_depth": "  遍历深度: {depth}",
    "impact_total": "  影响符号总数: {total}",
    "impact_by_layer_title": "  跨层影响分布:",
    "impact_layer_code": "    代码层: {count} 个",
    "impact_layer_db": "    DB 层:  {count} 个",
    "impact_layer_api": "    API 层: {count} 个",
    "impact_layer_config": "    配置层: {count} 个",
    "impact_layer_label_source": "源符号",
    "impact_layer_label_depth": "第 {depth} 层",
    "impact_layer_symbols": "  【{label}】（{count} 个符号）:",
    "impact_symbol_item": "    {kind:8s} {name}",
    "impact_symbol_file": "             {file}",
    "impact_more_symbols": "    ... 还有 {count} 个",
}

for key, zh in suggested_keys.items():
    full_key = f"cli.messages.{key}"
    print(f"  {full_key}")
    print(f"    zh: {zh}")
    print()
