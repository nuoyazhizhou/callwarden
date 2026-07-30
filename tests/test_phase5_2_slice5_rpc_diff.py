"""Phase 5-2 Slice 5 差分测试：Rust build_rpc_request vs Python 真相源

覆盖契约 D7 测试矩阵（跨平台，Windows 可测）：
- D7.1-D7.11: 11 个 RPC 命令的 method 映射和参数传递
- D7.12: 未知 action 错误处理
- D7.13: 无效 JSON 错误处理
- D7.14: PyO3 签名验证

契约：对齐 cli/daemon_commands.py:run_daemon_command 的对应分支 (L580-642)
"""

import json
import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import callwarden_core as cc


# ============================================================
# Python 真相源（内联，对齐 cli/daemon_commands.py:run_daemon_command L580-642）
# ============================================================

def py_build_rpc_request(action, params):
    """Python 真相源：cli/daemon_commands.py:run_daemon_command 的 RPC 命令分支。

    返回 (method, params) 元组，未知 action 返回 None。
    """
    mapping = {
        "register": "workspace.register",
        "backup": "backup",
        "restore": "restore",
        "gc-cas": "gc.cas",
        "gc-snapshots": "gc.snapshots",
        "snapshot-stats": "snapshot.stats",
        "snapshot-list": "snapshot.list_workspaces",
        "snapshot-evict": "snapshot.evict",
        "mount-register": "mount.register",
        "mount-list": "mount.list",
        "mount-delete": "mount.delete",
    }
    method = mapping.get(action)
    if method is None:
        return None
    return (method, params)


# ============================================================
# D7: RPC 命令参数构建差分测试
# ============================================================

def test_d7_rpc_params():
    """D7: RPC 命令参数构建差分测试"""
    print("=== D7: RPC 命令参数构建 ===")
    all_pass = True
    results = []

    # 测试数据：(action, params_dict)
    test_cases = [
        ("register", {"client_view_root": "/tmp", "git_remote_url": "", "git_head_commit_sha": "abc", "toolchain_fingerprint": ""}),
        ("backup", {"output_path": "/tmp/backup.db"}),
        ("restore", {"source_path": "/tmp/backup.db"}),
        ("gc-cas", {"workspace_instance_id": "ws-1", "grace_days": 7}),
        ("gc-snapshots", {"keep_last": 3}),
        ("snapshot-stats", {}),
        ("snapshot-list", {}),
        ("snapshot-evict", {"workspace_instance_id": "ws-1"}),
        ("mount-register", {"container_id": "ubuntu", "container_path": "/mnt", "host_path": "/tmp", "mapping_type": "bind"}),
        ("mount-list", {}),
        ("mount-delete", {"container_id": "ubuntu", "container_path": "/mnt"}),
    ]

    for i, (action, params) in enumerate(test_cases, 1):
        name = f"D7.{i} {action}"
        py_result = py_build_rpc_request(action, params)
        if py_result is None:
            results.append((name, False))
            print(f"  FAIL {name} — Python 返回 None")
            continue
        py_method, py_params = py_result
        rs_method, rs_params_json = cc.build_rpc_request_py(action, json.dumps(params))
        rs_params = json.loads(rs_params_json)
        ok = py_method == rs_method and py_params == rs_params
        results.append((name, ok))
        print(f"  {'PASS' if ok else 'FAIL'} {name}")
        if not ok:
            print(f"    Python: {py_method} {py_params}")
            print(f"    Rust:   {rs_method} {rs_params}")

    # D7.12: 未知 action 错误处理
    name = "D7.12 unknown action error"
    rs_method, rs_err = cc.build_rpc_request_py("unknown", "{}")
    ok = rs_method == "ERROR" and "不支持的 RPC action" in rs_err
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # D7.13: 无效 JSON 错误处理
    name = "D7.13 invalid JSON error"
    rs_method, rs_err = cc.build_rpc_request_py("backup", "not json")
    ok = rs_method == "ERROR"
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # D7.14: mount-list with container_id
    name = "D7.14 mount-list with container_id"
    py_method, py_params = py_build_rpc_request("mount-list", {"container_id": "ubuntu"})
    rs_method, rs_params_json = cc.build_rpc_request_py("mount-list", json.dumps({"container_id": "ubuntu"}))
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # 汇总
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    for _, ok in results:
        if not ok:
            all_pass = False
    print(f"\nPhase 5-2 Slice 5 D7 差分测试结果：{passed} passed, {total - passed} failed")
    return all_pass


# ============================================================
# D8: PyO3 签名验证
# ============================================================

def test_d8_py_signature():
    """D8: PyO3 函数签名验证"""
    print("\n=== D8: PyO3 签名验证 ===")
    ok = hasattr(cc, 'build_rpc_request_py')
    print(f"  {'PASS' if ok else 'FAIL'} build_rpc_request_py exists")
    if not ok:
        return False

    result = cc.build_rpc_request_py("backup", '{"output_path":"/tmp/b"}')
    ok = isinstance(result, tuple) and len(result) == 2
    print(f"  {'PASS' if ok else 'FAIL'} build_rpc_request_py returns (str, str)")
    return ok


if __name__ == "__main__":
    d7_ok = test_d7_rpc_params()
    d8_ok = test_d8_py_signature()
    print(f"\n总计：{'ALL PASS' if d7_ok and d8_ok else 'SOME FAILED'}")
    sys.exit(0 if d7_ok and d8_ok else 1)
