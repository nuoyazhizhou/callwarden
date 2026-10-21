"""Phase 5-2 Slice 3 差分测试：Rust build_simple_request vs Python 真相源

覆盖契约 D5 测试矩阵（跨平台，Windows 可测）：
- D5.1-D5.4: 4 个简单命令的参数构建（list/status/health/schema-version）
- D5.5: 未知 action 错误处理
- D5.6-D5.8: 边界情况（status 缺少 workspace_id / 空字符串 / 非 status 忽略 workspace_id）
- D5.9: method 命名一致性（验证 RPC method 正确性）

契约：对齐 cli/daemon_commands.py:run_daemon_command 的简单命令分支 (L553-596)
"""

import json
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest  # noqa: E402 —— sys.path/环境注入须先于 import(脚本直跑未走 site-packages,顺序为硬约束)
import callwarden_core as cc  # noqa: E402 —— sys.path/环境注入须先于 import(脚本直跑未走 site-packages,顺序为硬约束)

# 差分契约守卫：本文件为 Rust/Python 差分对比测试，禁止以返回值代替断言
# （pytest 忽略非 None 返回值，仅发 PytestReturnNotNoneWarning）。差分不匹配时
# 以 return all_pass 收尾会结构性永不红，故在此局部将该警告升级为 error。
pytestmark = pytest.mark.filterwarnings("error::pytest.PytestReturnNotNoneWarning")


# ============================================================
# Python 真相源（内联，对齐 cli/daemon_commands.py:run_daemon_command L553-596）
# ============================================================

def py_build_simple_request(action, workspace_id=None):
    """Python 真相源：cli/daemon_commands.py:run_daemon_command 的简单命令分支。

    对齐 Python 的 RPC 调用：
    - list: client.call("workspace.list")  — 无 params（call 内部转 {}）
    - status: client.call("workspace.status", {"workspace_instance_id": workspace_id})
    - health: client.call("health", {})
    - schema-version: client.call("schema.version", {})

    返回 (method, params) 元组，未知 action 返回 None。
    """
    if action == "list":
        return ("workspace.list", {})
    elif action == "status":
        if workspace_id is None:
            return None  # Python 会因 args.workspace_id 未定义而报错
        return ("workspace.status", {"workspace_instance_id": workspace_id})
    elif action == "health":
        return ("health", {})
    elif action == "schema-version":
        return ("schema.version", {})
    else:
        return None


# ============================================================
# D5: 简单命令参数构建差分测试
# ============================================================

def test_d5_simple_params():
    """D5: 简单命令参数构建差分测试"""
    print("=== D5: 简单命令参数构建 ===")
    all_pass = True
    results = []

    # -----------------------------------------------
    # D5.1: list（无参数 RPC）
    # -----------------------------------------------
    name = "D5.1 list action"
    py_method, py_params = py_build_simple_request("list")
    rs_method, rs_params_json = cc.build_simple_request_py("list", None)
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"    Python: {py_method} {py_params}")
        print(f"    Rust:   {rs_method} {rs_params}")

    # -----------------------------------------------
    # D5.2: status with workspace_id
    # -----------------------------------------------
    name = "D5.2 status action with workspace_id"
    py_method, py_params = py_build_simple_request("status", "ws-abc-123")
    rs_method, rs_params_json = cc.build_simple_request_py("status", "ws-abc-123")
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"    Python: {py_method} {py_params}")
        print(f"    Rust:   {rs_method} {rs_params}")

    # -----------------------------------------------
    # D5.3: health（无参数 RPC）
    # -----------------------------------------------
    name = "D5.3 health action"
    py_method, py_params = py_build_simple_request("health")
    rs_method, rs_params_json = cc.build_simple_request_py("health", None)
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"    Python: {py_method} {py_params}")
        print(f"    Rust:   {rs_method} {rs_params}")

    # -----------------------------------------------
    # D5.4: schema-version（无参数 RPC，method 用点号）
    # -----------------------------------------------
    name = "D5.4 schema-version action"
    py_method, py_params = py_build_simple_request("schema-version")
    rs_method, rs_params_json = cc.build_simple_request_py("schema-version", None)
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"    Python: {py_method} {py_params}")
        print(f"    Rust:   {rs_method} {rs_params}")

    # -----------------------------------------------
    # D5.5: 未知 action 错误处理
    # -----------------------------------------------
    name = "D5.5 unknown action error"
    rs_method, rs_err = cc.build_simple_request_py("unknown-cmd", None)
    ok = rs_method == "ERROR" and "不支持的 action" in rs_err
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"    Rust method: {rs_method}")
        print(f"    Rust error:  {rs_err}")

    # -----------------------------------------------
    # D5.6: status 缺少 workspace_id 错误处理
    # -----------------------------------------------
    name = "D5.6 status missing workspace_id error"
    rs_method, rs_err = cc.build_simple_request_py("status", None)
    ok = rs_method == "ERROR" and "workspace_id" in rs_err
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"    Rust method: {rs_method}")
        print(f"    Rust error:  {rs_err}")

    # -----------------------------------------------
    # D5.7: status 空字符串 workspace_id（仍发送）
    # -----------------------------------------------
    name = "D5.7 status empty workspace_id"
    py_method, py_params = py_build_simple_request("status", "")
    rs_method, rs_params_json = cc.build_simple_request_py("status", "")
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"    Python: {py_method} {py_params}")
        print(f"    Rust:   {rs_method} {rs_params}")

    # -----------------------------------------------
    # D5.8: 非 status 命令忽略 workspace_id
    # -----------------------------------------------
    name = "D5.8 non-status ignores workspace_id"
    # list 传入 workspace_id 应被忽略
    py_method, py_params = py_build_simple_request("list")
    rs_method, rs_params_json = cc.build_simple_request_py("list", "ignored-ws")
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"    Python: {py_method} {py_params}")
        print(f"    Rust:   {rs_method} {rs_params}")

    # -----------------------------------------------
    # D5.9: method 命名一致性
    # -----------------------------------------------
    name = "D5.9 method naming consistency"
    expected_methods = {
        "list": "workspace.list",
        "status": "workspace.status",
        "health": "health",
        "schema-version": "schema.version",
    }
    ok = True
    for action, expected_method in expected_methods.items():
        ws = "ws-test" if action == "status" else None
        rs_method, _ = cc.build_simple_request_py(action, ws)
        if rs_method != expected_method:
            ok = False
            print(f"    {action}: expected {expected_method}, got {rs_method}")
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # 汇总
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    for name, ok in results:
        if not ok:
            all_pass = False
    print(f"\nPhase 5-2 Slice 3 D5 差分测试结果：{passed} passed, {total - passed} failed")
    assert all_pass, (
        "Rust/Python 差分不匹配必须使测试失败"
        "：不得以 return 值当断言（pytest 忽略非 None 返回值）；逐用例明细见上方 stdout"
    )


# ============================================================
# D6: PyO3 签名验证
# ============================================================

def test_d6_py_signature():
    """D6: PyO3 函数签名验证"""
    print("\n=== D6: PyO3 签名验证 ===")
    all_pass = True

    # 验证 build_simple_request_py 存在且返回 tuple
    ok = hasattr(cc, 'build_simple_request_py')
    print(f"  {'PASS' if ok else 'FAIL'} build_simple_request_py exists")
    assert ok, "build_simple_request_py 必须存在于 callwarden_core（PyO3 绑定缺失）"

    # 验证返回 (str, str) 元组
    result = cc.build_simple_request_py("list", None)
    ok = isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], str) and isinstance(result[1], str)
    print(f"  {'PASS' if ok else 'FAIL'} build_simple_request_py returns (str, str)")
    if not ok:
        all_pass = False
        print(f"    actual: {type(result)} {result}")

    assert all_pass, (
        "Rust/Python 差分不匹配必须使测试失败"
        "：不得以 return 值当断言（pytest 忽略非 None 返回值）；逐用例明细见上方 stdout"
    )


if __name__ == "__main__":
    try:
        test_d5_simple_params()
        test_d6_py_signature()
        print("\n总计：ALL PASS")
        sys.exit(0)
    except AssertionError:
        print("\n总计：SOME FAILED")
        sys.exit(1)
