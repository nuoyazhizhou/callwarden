"""直接验证 _handle_topo 修复:喂 str 列表(daemon RPC 实际返回)和 dict 列表(旧本地),
确认两种都不再抛 'str' object has no attribute 'get'。"""
import os, sys
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, os.path.dirname(REPO))
os.environ["PYTHONUTF8"] = "1"
import importlib
pkg = os.path.basename(REPO)
main = importlib.import_module(f"{pkg}.cli.main")


class FakeDB:
    def __init__(self, ret):
        self._ret = ret
    def get_topological_order(self, limit):
        return self._ret[:limit]


# 场景1:daemon RPC 返回 str 列表(原 bug 触发场景)
print("=== 场景1: str 列表(daemon 实际返回)===")
try:
    main._handle_topo(["--limit", "3"], FakeDB(["mod::a", "mod::b", "mod::c"]))
    print("[OK] str 列表不崩溃")
except Exception as e:
    print(f"[FAIL] {type(e).__name__}: {e}")

# 场景2:dict 列表(旧本地路径)
print("=== 场景2: dict 列表(旧本地路径)===")
try:
    main._handle_topo(["--limit", "2"], FakeDB([
        {"name": "a", "depth": 0, "start_line": 10, "rel_path": "x.py", "qualified_name": "mod::a"},
        {"name": "b", "depth": 1, "start_line": 20, "rel_path": "y.py", "qualified_name": "mod::b"},
    ]))
    print("[OK] dict 列表不崩溃")
except Exception as e:
    print(f"[FAIL] {type(e).__name__}: {e}")
