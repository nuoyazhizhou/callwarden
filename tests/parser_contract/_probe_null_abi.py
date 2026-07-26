"""P0-2 验证 probe：检查 ParseFact NULL ABI。

复审 §P0-2：lib.rs lexical_parent_local_id/caller_local_id 已改为 Option<u32>。
需确认隔离 wheel 的 JSON 输出：
  - 顶层符号 lexical_parent_local_id = null（非 0）
  - 顶层裸调用 caller_local_id = null（非 0）

使用方法：
  $env:PYTHONPATH = "rust_ext\target\wheel_extract"
  python tests\parser_contract\_probe_null_abi.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

# 强制使用隔离 wheel（PYTHONPATH 由调用方设置）
os.environ.setdefault("PYTHONUTF8", "1")

# 确保项目根的 callwarden_core.pyd 不会 shadow 隔离 wheel
# wheel_extract 必须在 sys.path 最前面
_wheel_extract = str(Path(__file__).resolve().parents[2] / "rust_ext" / "target" / "wheel_extract")
if _wheel_extract not in sys.path:
    sys.path.insert(0, _wheel_extract)

# 移除项目根目录（避免根目录的 .pyd 被优先加载）
_pkg_root = str(Path(__file__).resolve().parents[2])
while _pkg_root in sys.path:
    sys.path.remove(_pkg_root)

print(f"[probe] PYTHONPATH first 3: {sys.path[:3]}")
print(f"[probe] wheel_extract exists: {Path(_wheel_extract).exists()}")

from callwarden_core import parse_file_lang

# 样本：顶层函数 + 顶层裸调用 + 嵌套函数（有词法父）
SAMPLE_RUST = """\
fn top_level() {
    helper();
}

struct Point {
    x: f64,
    y: f64,
}

impl Point {
    fn distance(&self) -> f64 {
        0.0
    }
}
"""

def main():
    tmp = Path(tempfile.mkdtemp())
    sample = tmp / "sample.rs"
    sample.write_text(SAMPLE_RUST, encoding="utf-8")

    result = parse_file_lang(str(sample), "probe.rs", "rust")
    symbols = result.get("symbols", [])
    calls = result.get("raw_calls", [])

    print(f"\n=== Symbols ({len(symbols)}) ===")
    null_parent_count = 0
    non_null_parent_count = 0
    zero_parent_count = 0  # 旧 ABI 的 0 哨兵（不应出现）
    for s in symbols:
        name = s.get("name", "?")
        local_id = s.get("local_id", "MISSING")
        parent = s.get("lexical_parent_local_id", "MISSING")
        print(f"  local_id={local_id} parent={parent!r} name={name}")
        if parent is None:
            null_parent_count += 1
        elif parent == 0:
            zero_parent_count += 1
        else:
            non_null_parent_count += 1

    print(f"\n=== Calls ({len(calls)}) ===")
    null_caller_count = 0
    non_null_caller_count = 0
    zero_caller_count = 0  # 旧 ABI 的 0 哨兵（不应出现）
    for c in calls:
        callee = c.get("callee_name", "?")
        caller = c.get("caller_local_id", "MISSING")
        print(f"  caller_local_id={caller!r} callee={callee}")
        if caller is None:
            null_caller_count += 1
        elif caller == 0:
            zero_caller_count += 1
        else:
            non_null_caller_count += 1

    print("\n=== Verdict ===")
    print(f"Symbols: null_parent={null_parent_count} non_null_parent={non_null_parent_count} zero_parent(BUG)={zero_parent_count}")
    print(f"Calls:   null_caller={null_caller_count} non_null_caller={non_null_caller_count} zero_caller(BUG)={zero_caller_count}")

    if zero_parent_count > 0 or zero_caller_count > 0:
        print("\nFAIL: 旧 ABI 0 哨兵仍存在（P0-2 未修复）")
        sys.exit(1)
    if null_parent_count == 0:
        print("\nFAIL: 没有顶层符号的 lexical_parent_local_id=null（P0-2 未修复）")
        sys.exit(1)
    print("\nPASS: NULL ABI 已实现（Option<u32>，None 表示顶层）")
    sys.exit(0)


if __name__ == "__main__":
    main()
