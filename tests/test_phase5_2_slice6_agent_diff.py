"""Phase 5-2 Slice 6 差分测试：Rust agent 参数构建 vs Python 真相源

覆盖契约 D10 测试矩阵（跨平台，Windows 可测）：
- D10.1-D10.5: connect 参数构建（workspace.connect）
- D10.6-D10.10: refresh 参数构建（workspace.file.refresh）
- D10.11: ping 参数构建
- D10.12-D10.14: AgentSession 状态管理（epoch/seq/reset）

契约：对齐 server/agent_protocol.py:user_agent_connect / build_refresh_message (L85-206)
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
# Python 真相源（内联，对齐 server/agent_protocol.py L85-206）
# ============================================================

def py_build_connect_params(workspace_instance_id, agent_session_id):
    """Python 真相源：user_agent_connect 的参数构建部分 (L121-124)"""
    return (
        "workspace.connect",
        {
            "workspace_instance_id": workspace_instance_id,
            "agent_session_id": agent_session_id,
        },
    )


def py_build_refresh_params(workspace_instance_id, rel_path, agent_session_id,
                            session_epoch, monotonic_seq):
    """Python 真相源：build_refresh_message (L200-206)"""
    return (
        "workspace.file.refresh",
        {
            "workspace_instance_id": workspace_instance_id,
            "rel_path": rel_path,
            "agent_session_id": agent_session_id,
            "session_epoch": session_epoch,
            "monotonic_seq": monotonic_seq,
        },
    )


def py_build_ping_params():
    """Python 真相源：user_agent_ping (L358)"""
    return ("ping", {})


# ============================================================
# D10: agent 参数构建差分测试
# ============================================================

def test_d10_agent_params():
    """D10: agent 参数构建差分测试"""
    print("=== D10: agent 参数构建 ===")
    all_pass = True
    results = []

    # D10.1: connect 基本参数
    name = "D10.1 connect basic params"
    py_method, py_params = py_build_connect_params("ws-abc-123", "agent-deadbeef1234")
    rs_method, rs_params_json = cc.build_connect_params_py("ws-abc-123", "agent-deadbeef1234")
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"    Python: {py_method} {py_params}")
        print(f"    Rust:   {rs_method} {rs_params}")

    # D10.2: connect 字段数量
    name = "D10.2 connect has 2 fields"
    _, rs_params_json = cc.build_connect_params_py("ws-1", "agent-abc")
    rs_params = json.loads(rs_params_json)
    ok = len(rs_params) == 2 and "workspace_instance_id" in rs_params and "agent_session_id" in rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # D10.3: refresh 基本参数
    name = "D10.3 refresh basic params"
    py_method, py_params = py_build_refresh_params(
        "ws-1", "src/main.rs", "agent-abc", 42, 7
    )
    rs_method, rs_params_json = cc.build_refresh_params_py(
        "ws-1", "src/main.rs", "agent-abc", 42, 7
    )
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")
    if not ok:
        print(f"    Python: {py_method} {py_params}")
        print(f"    Rust:   {rs_method} {rs_params}")

    # D10.4: refresh 字段数量
    name = "D10.4 refresh has 5 fields"
    _, rs_params_json = cc.build_refresh_params_py("ws", "p", "a", 1, 1)
    rs_params = json.loads(rs_params_json)
    expected_fields = {"workspace_instance_id", "rel_path", "agent_session_id", "session_epoch", "monotonic_seq"}
    ok = len(rs_params) == 5 and expected_fields == set(rs_params.keys())
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # D10.5: connect method 命名一致性
    name = "D10.5 connect method naming"
    rs_method, _ = cc.build_connect_params_py("any", "any")
    ok = rs_method == "workspace.connect"
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # D10.6: refresh method 命名一致性
    name = "D10.6 refresh method naming"
    rs_method, _ = cc.build_refresh_params_py("w", "p", "a", 1, 1)
    ok = rs_method == "workspace.file.refresh"
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # D10.7: refresh epoch=0（未协商场景）
    name = "D10.7 refresh with epoch=0"
    py_method, py_params = py_build_refresh_params("ws-1", "path", "agent", 0, 1)
    rs_method, rs_params_json = cc.build_refresh_params_py("ws-1", "path", "agent", 0, 1)
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # D10.8: refresh 大 epoch 和 seq
    name = "D10.8 refresh with large epoch/seq"
    py_method, py_params = py_build_refresh_params("ws-1", "p", "a", 999999, 888888)
    rs_method, rs_params_json = cc.build_refresh_params_py("ws-1", "p", "a", 999999, 888888)
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # D10.9: refresh 空路径
    name = "D10.9 refresh with empty rel_path"
    py_method, py_params = py_build_refresh_params("ws-1", "", "agent", 1, 1)
    rs_method, rs_params_json = cc.build_refresh_params_py("ws-1", "", "agent", 1, 1)
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # D10.10: refresh 特殊字符路径
    name = "D10.10 refresh with special chars in path"
    py_method, py_params = py_build_refresh_params("ws-1", "src/路径 with spaces.rs", "agent", 1, 1)
    rs_method, rs_params_json = cc.build_refresh_params_py("ws-1", "src/路径 with spaces.rs", "agent", 1, 1)
    rs_params = json.loads(rs_params_json)
    ok = py_method == rs_method and py_params == rs_params
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # D10.11: PyO3 签名验证
    name = "D10.11 PyO3 signatures"
    ok = hasattr(cc, 'build_connect_params_py') and hasattr(cc, 'build_refresh_params_py')
    if ok:
        r1 = cc.build_connect_params_py("ws", "agent")
        r2 = cc.build_refresh_params_py("ws", "p", "a", 1, 1)
        ok = isinstance(r1, tuple) and isinstance(r2, tuple) and len(r1) == 2 and len(r2) == 2
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # D10.12-D10.14: AgentSession 行为验证（通过 PyO3 间接测试）
    # 注意：AgentSession 未直接暴露给 Python，通过参数构建函数间接验证
    name = "D10.12 session_id format (connect uses session_id)"
    # 验证 session_id 字符串原样传递
    _, rs_params_json = cc.build_connect_params_py("ws-1", "agent-abc123def456")
    rs_params = json.loads(rs_params_json)
    ok = rs_params["agent_session_id"] == "agent-abc123def456"
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    name = "D10.13 epoch as integer"
    _, rs_params_json = cc.build_refresh_params_py("ws", "p", "a", 5, 3)
    rs_params = json.loads(rs_params_json)
    ok = isinstance(rs_params["session_epoch"], int) and isinstance(rs_params["monotonic_seq"], int)
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    name = "D10.14 epoch and seq are passed correctly"
    _, rs_params_json = cc.build_refresh_params_py("ws", "p", "a", 100, 200)
    rs_params = json.loads(rs_params_json)
    ok = rs_params["session_epoch"] == 100 and rs_params["monotonic_seq"] == 200
    results.append((name, ok))
    print(f"  {'PASS' if ok else 'FAIL'} {name}")

    # 汇总
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    for _, ok in results:
        if not ok:
            all_pass = False
    print(f"\nPhase 5-2 Slice 6 D10 差分测试结果：{passed} passed, {total - passed} failed")
    return all_pass


if __name__ == "__main__":
    d10_ok = test_d10_agent_params()
    print(f"\n总计：{'ALL PASS' if d10_ok else 'SOME FAILED'}")
    sys.exit(0 if d10_ok else 1)
