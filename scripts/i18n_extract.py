"""
i18n 提取脚本 v2（第一步：提取 → 生成映射表）

优化:
- 正确识别 print/cprint 的第一个参数（排除颜色参数等）
- 基于函数名+上下文生成更有意义的 key
- 支持多行 print 语句
- 检测 f-string 和变量插值
- 提取变量名列表

用法:
    python scripts/i18n_extract.py <file_path> [output_json]
"""
import re
import json
import sys
import os
import ast


def is_chinese(text: str) -> bool:
    """判断文本是否包含中文"""
    for ch in text:
        if '\u4e00' <= ch <= '\u9fff':
            return True
    return False


def find_enclosing_function(lines: list, line_idx: int) -> str:
    """找到指定行所在的函数名"""
    for i in range(line_idx - 1, -1, -1):
        line = lines[i]
        m = re.match(r'^def\s+(\w+)\s*\(', line)
        if m:
            return m.group(1)
    return "unknown"


def extract_first_arg_text(full_line: str, func_name: str) -> tuple:
    """提取 print/cprint 的第一个字符串参数

    返回: (文本, 是否fstring, 变量名列表)
    如果第一个参数不是字符串字面量，返回 None
    """
    # 找到函数调用的第一个括号
    paren_match = re.search(r'(?:c|)print\s*\(', full_line)
    if not paren_match:
        return None, False, []

    start = paren_match.end()

    # 找到第一个参数的结束位置（逗号或右括号）
    # 需要处理字符串内的逗号和括号嵌套
    depth = 1
    i = start
    str_char = None
    in_string = False
    first_arg_start = start
    first_arg_end = -1
    is_fstring = False

    while i < len(full_line) and depth > 0:
        ch = full_line[i]

        if in_string:
            if ch == '\\' and i + 1 < len(full_line):
                i += 2
                continue
            if ch == str_char:
                in_string = False
            i += 1
            continue

        if ch in ('"', "'"):
            str_char = ch
            in_string = True
            # 检查是否 f-string
            if i > 0 and full_line[i-1] == 'f':
                is_fstring = True
            i += 1
            continue

        if ch == '(':
            depth += 1
        elif ch == ')':
            depth -= 1
            if depth == 0:
                first_arg_end = i
                break
        elif ch == ',' and depth == 1:
            first_arg_end = i
            break

        i += 1

    if first_arg_end == -1:
        return None, False, []

    first_arg = full_line[first_arg_start:first_arg_end].strip()

    # 提取第一个参数中的所有字符串
    # 处理 f-string 或普通字符串
    all_text = ""
    str_matches = re.findall(r'''f?(['"])((?:\\.|[^\\])*?)\1''', first_arg)

    var_names = []
    for quote, content in str_matches:
        all_text += content
        # 提取变量名
        vars_in_str = re.findall(r'\{([^{}]+)\}', content)
        var_names.extend(vars_in_str)

    if not all_text.strip():
        return None, is_fstring, var_names

    return all_text, is_fstring, list(set(var_names))


def make_key(function_name: str, text: str, prefix: str = "cli.messages") -> str:
    """根据函数名和文本生成有意义的翻译键"""
    fn = function_name.replace('_handle_', '').replace('_', '_')

    # 提取英文单词
    english_words = re.findall(r'[a-zA-Z][a-zA-Z0-9_]+', text)

    # 清理文本中的符号
    cleaned = re.sub(r'[=+\-\s#✓✗❌✅▶►▸◆■●▪▫※\d\.\!\?\:\，\。\、\；\：\(\)（）\[\]【】]', ' ', text.strip())
    cleaned = re.sub(r'\s+', ' ', cleaned).strip()

    # 策略: 函数名 + 英文关键词 或 文本特征
    key_parts = [fn.replace('-', '_')]

    if english_words:
        # 取前 3 个有意义的英文单词
        meaningful = [w.lower() for w in english_words
                      if w.lower() not in {'the', 'a', 'an', 'is', 'are', 'of', 'in', 'to', 'for'}
                      and len(w) > 2][:3]
        if meaningful:
            key_parts.extend(meaningful)
    else:
        # 纯中文，用文本前几个字的特征 + 短 hash
        short_hash = abs(hash(cleaned[:30])) % 10000
        key_parts.append(f'msg_{short_hash}')

    key = '_'.join(key_parts)
    key = re.sub(r'[^a-z0-9_]', '_', key.lower())
    key = re.sub(r'_+', '_', key).strip('_')

    if len(key) > 80:
        key = key[:80]

    return f"{prefix}.{key}"


def extract_prints(file_path: str) -> list:
    """提取文件中所有 print/cprint 语句的可翻译文本"""
    results = []

    with open(file_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()

    print_re = re.compile(r'^\s*(c|)print\s*\(')

    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        if stripped.startswith('#'):
            i += 1
            continue

        if not print_re.match(line):
            i += 1
            continue

        func_name = find_enclosing_function(lines, i)

        # 处理跨行
        full_line = line.rstrip('\n')
        paren_count = line.count('(') - line.count(')')
        j = i + 1
        while paren_count > 0 and j < len(lines):
            next_stripped = lines[j].strip()
            if next_stripped.startswith('#'):
                j += 1
                continue
            full_line += ' ' + next_stripped
            paren_count += lines[j].count('(') - lines[j].count(')')
            j += 1

        # 判断是否已经国际化
        if re.search(r'\bi18n\.t\(', full_line) or re.search(r'\bt\([\'\"]', full_line):
            # 更精确的判断：第一个参数是 t() 调用
            first_arg_match = re.search(r'(?:c|)print\s*\(\s*t\(', full_line)
            if first_arg_match:
                results.append({
                    "line": i + 1,
                    "function": func_name,
                    "type": "cprint" if 'cprint' in line else "print",
                    "original": full_line.strip()[:200],
                    "status": "already_i18n"
                })
                i = j if j > i + 1 else i + 1
                continue

        # 提取第一个参数的文本
        text, is_fstring, var_names = extract_first_arg_text(full_line, func_name)

        if text is None:
            # 没有可提取的字符串（如 print() 空行，或第一个参数是变量）
            results.append({
                "line": i + 1,
                "function": func_name,
                "type": "cprint" if 'cprint' in line else "print",
                "original": full_line.strip()[:150],
                "status": "skip_no_string"
            })
            i = j if j > i + 1 else i + 1
            continue

        if not text.strip():
            results.append({
                "line": i + 1,
                "function": func_name,
                "type": "cprint" if 'cprint' in line else "print",
                "original": full_line.strip()[:150],
                "status": "skip_empty"
            })
            i = j if j > i + 1 else i + 1
            continue

        has_chinese = is_chinese(text)

        if not has_chinese:
            # 纯英文，标记为可能需要翻译
            status = "maybe_english"
        else:
            status = "todo"

        auto_key = make_key(func_name, text)

        results.append({
            "line": i + 1,
            "function": func_name,
            "type": "cprint" if 'cprint' in line else "print",
            "original": full_line.strip()[:200],
            "text": text,
            "is_fstring": is_fstring,
            "var_names": var_names,
            "auto_key": auto_key,
            "status": status
        })

        i = j if j > i + 1 else i + 1

    return results


def build_mapping(extract_results: list) -> dict:
    """从提取结果构建翻译映射表"""
    mappings = {}
    stats = {
        "total_prints": len(extract_results),
        "todo": 0,
        "already_i18n": 0,
        "skip_no_string": 0,
        "skip_empty": 0,
        "maybe_english": 0,
        "unique_texts": 0,
        "unique_keys": 0,
        "by_function": {}
    }

    for item in extract_results:
        status = item["status"]
        stats[status] = stats.get(status, 0) + 1

        func = item.get("function", "unknown")
        stats["by_function"][func] = stats["by_function"].get(func, 0) + 1

        if status not in ("todo", "maybe_english"):
            continue

        text = item["text"]
        key = item["auto_key"]

        if key not in mappings:
            mappings[key] = {
                "zh": text,
                "en": "",
                "occurrences": [],
                "has_vars": len(item.get("var_names", [])) > 0,
                "var_names": item.get("var_names", []),
                "functions": [],
                "notes": ""
            }
            stats["unique_keys"] += 1

        # 记录出现位置
        occ = {
            "line": item["line"],
            "type": item["type"],
            "function": item.get("function", ""),
            "is_fstring": item.get("is_fstring", False),
            "original_snippet": item["original"][:100]
        }
        mappings[key]["occurrences"].append(occ)

        func_name = item.get("function", "")
        if func_name and func_name not in mappings[key]["functions"]:
            mappings[key]["functions"].append(func_name)

    stats["unique_texts"] = len(set(m["zh"] for m in mappings.values()))

    return {
        "stats": stats,
        "mappings": mappings
    }


def main():
    if len(sys.argv) < 2:
        print("用法: python scripts/i18n_extract.py <file_path> [output_json]")
        print()
        print("示例:")
        print("  python scripts/i18n_extract.py cli/main.py scripts/i18n_cli_main.json")
        sys.exit(1)

    file_path = sys.argv[1]
    if not os.path.exists(file_path):
        print(f"错误: 文件不存在: {file_path}")
        sys.exit(1)

    output_path = sys.argv[2] if len(sys.argv) > 2 else None

    print("=" * 60)
    print(f"i18n 提取: {file_path}")
    print("=" * 60)

    results = extract_prints(file_path)
    mapping = build_mapping(results)

    stats = mapping["stats"]
    print()
    print("📊 统计:")
    print(f"  总 print 数:    {stats['total_prints']}")
    print(f"  待处理 (todo):  {stats.get('todo', 0)}")
    print(f"  已国际化:       {stats.get('already_i18n', 0)}")
    print(f"  无字符串参数:   {stats.get('skip_no_string', 0)}")
    print(f"  空文本:         {stats.get('skip_empty', 0)}")
    print(f"  可能英文:       {stats.get('maybe_english', 0)}")
    print(f"  唯一 key 数:    {stats.get('unique_keys', 0)}")
    print(f"  唯一文本数:     {stats.get('unique_texts', 0)}")
    print()

    print("📁 按函数分布 (前 15):")
    by_func_sorted = sorted(stats["by_function"].items(), key=lambda x: -x[1])[:15]
    for fn, count in by_func_sorted:
        print(f"  {fn:40s} {count:4d} 个")
    print()

    # 输出前 10 条待处理的样本
    todo_items = [r for r in results if r["status"] == "todo"]
    print(f"📝 前 10 条待处理样本:")
    for item in todo_items[:10]:
        text_preview = item["text"][:70]
        vars_str = f" vars={item['var_names']}" if item["var_names"] else ""
        print(f"  行{item['line']:4d} [{item['function'][:25]:25s}] {text_preview}{vars_str}")
    print()
    print(f"  自动key示例: {todo_items[0]['auto_key'] if todo_items else 'N/A'}")
    print()

    if output_path:
        out_dir = os.path.dirname(output_path)
        if out_dir and not os.path.exists(out_dir):
            os.makedirs(out_dir)

        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(mapping, f, ensure_ascii=False, indent=2)
        print(f"✅ 映射表已保存到: {output_path}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
