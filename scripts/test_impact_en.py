"""测试 _handle_impact 模块在英文环境下的输出

用法: python scripts/test_impact_en.py
"""
import os
import sys
from io import StringIO
from contextlib import redirect_stdout

# 切换到英文
os.environ["CALLWARDEN_LANG"] = "en_US"

# 确保项目根在 sys.path
project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, project_root)

from callwarden.i18n import set_language
set_language("en_US")

from callwarden.db import CodeGraphDB
from callwarden.cli.main import _handle_impact

# 初始化 db（与 _run_subcommand_mode 一致）
db = CodeGraphDB()

buf = StringIO()
with redirect_stdout(buf):
    _handle_impact(["nonexistent_hash"], db)

db.close()

out = buf.getvalue()
print("=== 英文环境输出 ===")
print(out)
print("=== END ===")

# 简单断言：输出应包含英文关键词
assert "Change Impact Radius" in out, f"FAIL: 期望英文标题，实际输出: {out}"
assert "Symbol not found" in out, f"FAIL: 期望英文 'Symbol not found'，实际输出: {out}"
assert "未找到符号" not in out, f"FAIL: 不应包含中文，实际输出: {out}"
print("PASS: 英文环境测试通过")
