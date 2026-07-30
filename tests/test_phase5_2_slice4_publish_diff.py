"""Phase 5-2 Slice 4 差分测试：Rust build_publish_params vs Python 真相源

覆盖契约 D9 测试矩阵（跨平台，Windows 可测）：
- D9.1-D9.5: publish 参数构建（method + 2 个字段）
- D9.6: PyO3 签名验证

契约：对齐 server/daemon_client.py:UnixDaemonRpcClient.publish_snapshot (L103-119)

注意：
- FD 打开和 SCM_RIGHTS 传递是 Unix-only 副作用，无法在 Windows 差分测试
- 本测试仅验证参数构建逻辑，对齐 Python 的 RPC 参数结构
"""

import json
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import callwarden_core as cc


# ============================================================
# Python 真相源（内联，对齐 server/daemon_client.py:publish_snapshot L103-119）
# ============================================================

def py_build_publish_params(workspace_instance_id: str, build_context_hash: str = ""):
    """Python 真相源：UnixDaemonRpcClient.publish_snapshot 的参数构建部分。

    对齐 Python：
    ```python
    params = {
        "workspace_instance_id": workspace_instance_id,
        "build_context_hash": build_context_hash,
    }
    method = "snapshot.publish"
    ```

    注意：Python 端在调用前还会做 WAL checkpoint + os.open(db_path, O_RDONLY)，
    这些是 Unix-only 副作用，不在本函数中处理。
    """
    return (
        "snapshot.publish",
        {
            "workspace_instance_id": workspace_instance_id,
            "build_context_hash": build_context_hash,
        },
    )


# ============================================================
# D9: publish 参数构建差分测试
# ============================================================

def test_d9_publish_params():
    """D9: publish 参数构建差分测试"""
    print("=== D9: publish 参数构建 ===")
    all_pass = True
    results = []

    # D9.1: 基本参数（空 build_context_hash）
    name = "D9.1 basic params (empty build_context_hash)"
    py_method, py_params = py_build_publish_params("ws-abc-123", "")
    rs_method, rs_params_json = cc.build_publish_params_py("ws-abc-123", "")
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"    Python: {py_method} {py_params}")
        print(f"    Rust:   {rs_method} {rs_params}")

    # D9.2: 带 build_context_hash
    name = "D9.2 params with build_context_hash"
    py_method, py_params = py_build_publish_params("ws-1", "ctx-hash-xyz")
    rs_method, rs_params_json = cc.build_publish_params_py("ws-1", "ctx-hash-xyz")
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"    Python: {py_method} {py_params}")
        print(f"    Rust:   {rs_method} {rs_params}")

    # D9.3: 空 workspace_instance_id
    name = "D9.3 empty workspace_instance_id"
    py_method, py_params = py_build_publish_params("", "")
    rs_method, rs_params_json = cc.build_publish_params_py("", "")
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"    Python: {py_method} {py_params}")
        print(f"    Rust:   {rs_method} {rs_params}")

    # D9.4: method 命名一致性
    name = "D9.4 method naming consistency"
    expected_method = "snapshot.publish"
    rs_method, _ = cc.build_publish_params_py("any", "any")
    ok = rs_method == expected_method
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # D9.5: params 字段数量验证（仅 2 个字段）
    name = "D9.5 params has exactly 2 fields"
    _, rs_params_json = cc.build_publish_params_py("ws-1", "ctx")
    rs_params = json.loads(rs_params_json)
    ok = len(rs_params) == 2 and "workspace_instance_id" in rs_params and "build_context_hash" in rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # D9.6: PyO3 签名验证
    name = "D9.6 PyO3 signature"
    ok = hasattr(cc, 'build_publish_params_py')
    if ok:
        result = cc.build_publish_params_py("ws", "ctx")
        ok = isinstance(result, tuple) and len(result) == 2
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # 汇总
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    for _, ok in results:
        if not ok:
            all_pass = False
    print(f"\nPhase 5-2 Slice 4 D9 差分测试结果：{passed} passed, {total - passed} failed")
    return all_pass


if __name__ == "__main__":
    d9_ok = test_d9_publish_params()
    print(f"\n总计：{'ALL PASS' if d9_ok else 'SOME FAILED'}")
    sys.exit(0 if d9_ok else 1)
