"""P0-3 修复：清理 golden fixture 中已过时的 known_gaps。

复审 §P0-3：golden fixture 的 signature known_gaps 让 test_signature_alignment
对所有 16 语言直接 return（假绿）。probe 证实 15 个支持语言的 signature/visibility
已对齐，known_gaps 已过时。

本脚本移除：
- field="signature" 的 known_gap（Rust 已实现 extract_signature，probe 验证对齐）
- field="symbols" 且描述含 "不递归提取 impl" 的 gap（Rust 已实现 impl 方法提取）
- field="symbols.kind" 且描述含 "统一标记为 'fn'" 的 gap（Rust 已区分 method/fn）

保留：
- field="raw_calls" 的 gap（println! 宏调用，需单独验证）
- parser="python" 的 gap（Python parser 限制，不是 Rust 问题）
- C 语言的 signature gap（Rust 不支持 C，是真实缺口）
"""
import json
from pathlib import Path

GOLDEN_DIR = Path(__file__).resolve().parent / "golden"

# Rust 不支持的语言（保留 signature gap）
UNSUPPORTED_LANGS = {"c"}

def is_stale_signature_gap(gap, lang):
    """signature gap 在支持语言上已过时（Rust 已实现 extract_signature）。"""
    if gap.get("field") != "signature":
        return False
    if lang in UNSUPPORTED_LANGS:
        return False  # 不支持的语言保留 gap
    return True

def is_stale_symbols_gap(gap):
    """symbols gap 'Rust 不递归提取 impl 块内部方法' 已过时。"""
    if gap.get("field") != "symbols":
        return False
    desc = gap.get("description", "").lower()
    return "impl" in desc and "方法" in desc

def is_stale_kind_gap(gap):
    """symbols.kind gap '统一标记为 fn' 已过时。"""
    if gap.get("field") != "symbols.kind":
        return False
    desc = gap.get("description", "").lower()
    return "fn" in desc or "统一" in desc

def clean_fixture(path):
    """清理单个 golden fixture 的过时 known_gaps。"""
    lang = path.stem
    data = json.loads(path.read_text(encoding="utf-8"))
    gaps = data.get("known_gaps", [])
    if not gaps:
        return 0

    original_count = len(gaps)
    kept = []
    removed = []
    for gap in gaps:
        if is_stale_signature_gap(gap, lang):
            removed.append(f"signature: {gap.get('description', '')[:50]}")
        elif is_stale_symbols_gap(gap):
            removed.append(f"symbols: {gap.get('description', '')[:50]}")
        elif is_stale_kind_gap(gap):
            removed.append(f"symbols.kind: {gap.get('description', '')[:50]}")
        else:
            kept.append(gap)

    if removed:
        data["known_gaps"] = kept
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"  [{lang}] removed {len(removed)} stale gaps, kept {len(kept)}:")
        for r in removed:
            print(f"    - {r}")
        return len(removed)
    return 0


def main():
    print(f"Cleaning golden fixtures in {GOLDEN_DIR}")
    total_removed = 0
    for path in sorted(GOLDEN_DIR.glob("*.json")):
        total_removed += clean_fixture(path)
    print(f"\nTotal stale gaps removed: {total_removed}")


if __name__ == "__main__":
    main()
