"""Phase 5-1 B 差分测试：Rust 路由决策 vs Python 真相源

覆盖契约 D1-D5 测试矩阵：
- D1: get_daemon_mode / DaemonMode::from_str
- D2: is_daemon_required
- D3: is_daemon_available
- D4: route_command
- D5: daemon_socket_path

契约：docs/design/phase5-1b-router-contract.md §4
"""

import os
import sys
import tempfile
from pathlib import Path

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

# ============================================================
# Python 真相源（内联，对齐 config.py L1319-1383）
# ============================================================

# 对齐 config.py L1319-1321
PY_DEFAULT_DAEMON_SOCKET = "/run/callwarden/callwarden.sock"

# 对齐 config.py L1343
PY_DAEMON_MODE_ENV = "CW_DAEMON_MODE"
PY_DAEMON_SOCKET_ENV = "CW_DAEMON_SOCKET"


def py_get_daemon_mode() -> str:
    """Python 真相源：config.py:get_daemon_mode() (L1366-1368)"""
    return os.environ.get(PY_DAEMON_MODE_ENV, "auto")


def py_is_daemon_required() -> bool:
    """Python 真相源：config.py:is_daemon_required() (L1371-1373)"""
    return py_get_daemon_mode() == "enterprise"


def py_is_daemon_available(socket_path: str, platform: str) -> bool:
    """Python 真相源：config.py:is_daemon_available() (L1376-1383)

    参数化版本（原函数无参数，用全局 DAEMON_SOCKET_PATH + os.name）。
    """
    # Windows/macOS 永远不可用（UDS 是 Linux 特有）
    if platform != "linux":
        return False
    return os.path.exists(socket_path)


def py_daemon_socket_path() -> str:
    """Python 真相源：config.py:DAEMON_SOCKET_PATH (L1319-1321)"""
    return os.environ.get(PY_DAEMON_SOCKET_ENV, PY_DEFAULT_DAEMON_SOCKET)


def py_route_command(mode: str, socket_path: str, platform: str) -> str:
    """Python 真相源：综合路由决策（显式化，对齐契约 §3.2 决策矩阵）

    Python config.py 未实现该函数，但隐含逻辑由 is_daemon_required + is_daemon_available 组合。
    本函数是 Python 真相源的"应有实现"，与 Rust route_command 对齐。
    """
    if mode == "local":
        return "local"
    if mode == "enterprise":
        if py_is_daemon_available(socket_path, platform):
            return "enterprise"
        return "unavailable"
    # auto / unknown
    if py_is_daemon_available(socket_path, platform):
        return "enterprise"
    return "local"


# ============================================================
# Rust 实现（通过 PyO3 调用）
# ============================================================

import callwarden_core as cc


# ============================================================
# D1: get_daemon_mode
# ============================================================

def test_d1_get_daemon_mode():
    """D1: get_daemon_mode — Rust vs Python 模式判断"""
    print("=== D1: get_daemon_mode ===")

    test_cases = [
        # (env_value, expected_str) — Python 和 Rust 对已知值行为一致
        ("local", "local"),           # D1.1
        ("enterprise", "enterprise"), # D1.2
        ("auto", "auto"),             # D1.3
        # D1.4 未设置环境变量 → auto（默认）
    ]

    all_match = True
    for env_value, expected in test_cases:
        # 设置环境变量
        orig = os.environ.get(PY_DAEMON_MODE_ENV)
        os.environ[PY_DAEMON_MODE_ENV] = env_value

        py_result = py_get_daemon_mode()
        rs_result = cc.get_daemon_mode_py()

        match = py_result == rs_result == expected
        status = "PASS" if match else "FAIL"
        if not match:
            all_match = False
        print(f"  {status} CW_DAEMON_MODE={env_value:12s} "
              f"expected={expected:11s} py={py_result:11s} rs={rs_result}")

        # 恢复
        if orig is None:
            os.environ.pop(PY_DAEMON_MODE_ENV, None)
        else:
            os.environ[PY_DAEMON_MODE_ENV] = orig

    # D1.5: 未知值 — 预期差异（契约 §5.4）
    # Python get_daemon_mode() 返回原始字符串 "unknown"
    # Rust DaemonMode::from_str fail-soft normalize 为 "auto"
    # 语义一致（未知值在 is_daemon_required/route_command 中等同 auto），字符串值不同
    orig = os.environ.get(PY_DAEMON_MODE_ENV)
    os.environ[PY_DAEMON_MODE_ENV] = "unknown"
    py_result = py_get_daemon_mode()
    rs_result = cc.get_daemon_mode_py()
    # 验证语义一致：两者都不等于 "enterprise"（即 is_daemon_required=False）
    py_required = py_result == "enterprise"
    rs_required = rs_result == "enterprise"
    match = (not py_required) and (not rs_required) and rs_result == "auto"
    status = "PASS" if match else "FAIL"
    if not match:
        all_match = False
    print(f"  {status} D1.5 unknown_value (expected diff): "
          f"py='{py_result}' rs='{rs_result}' "
          f"(both is_daemon_required=False)")
    if orig is None:
        os.environ.pop(PY_DAEMON_MODE_ENV, None)
    else:
        os.environ[PY_DAEMON_MODE_ENV] = orig

    # D1.4: 未设置环境变量
    orig = os.environ.pop(PY_DAEMON_MODE_ENV, None)
    py_result = py_get_daemon_mode()
    rs_result = cc.get_daemon_mode_py()
    match = py_result == rs_result == "auto"
    status = "PASS" if match else "FAIL"
    if not match:
        all_match = False
    print(f"  {status} CW_DAEMON_MODE=<unset>   "
          f"expected=auto         py={py_result:11s} rs={rs_result}")
    # 恢复
    if orig is not None:
        os.environ[PY_DAEMON_MODE_ENV] = orig

    assert all_match, "D1: get_daemon_mode mismatch"
    print("  D1: ALL PASS\n")


# ============================================================
# D2: is_daemon_required
# ============================================================

def test_d2_is_daemon_required():
    """D2: is_daemon_required — mode == enterprise"""
    print("=== D2: is_daemon_required ===")

    test_cases = [
        ("local", False),       # D2.1
        ("enterprise", True),   # D2.2
        ("auto", False),        # D2.3
    ]

    all_match = True
    for env_value, expected in test_cases:
        orig = os.environ.get(PY_DAEMON_MODE_ENV)
        os.environ[PY_DAEMON_MODE_ENV] = env_value

        py_result = py_is_daemon_required()
        rs_result = cc.is_daemon_required_py()

        match = py_result == rs_result == expected
        status = "PASS" if match else "FAIL"
        if not match:
            all_match = False
        print(f"  {status} mode={env_value:12s} "
              f"expected={str(expected):5s} py={str(py_result):5s} rs={str(rs_result):5s}")

        if orig is None:
            os.environ.pop(PY_DAEMON_MODE_ENV, None)
        else:
            os.environ[PY_DAEMON_MODE_ENV] = orig

    assert all_match, "D2: is_daemon_required mismatch"
    print("  D2: ALL PASS\n")


# ============================================================
# D3: is_daemon_available
# ============================================================

def test_d3_is_daemon_available():
    """D3: is_daemon_available — 平台×socket 存在矩阵"""
    print("=== D3: is_daemon_available ===")

    # 创建临时文件模拟 socket 存在
    tmp = Path(tempfile.gettempdir()) / "cw_test_socket_d3.sock"
    tmp.write_bytes(b"")

    test_cases = [
        # (socket_path, platform, expected)
        (str(tmp), "linux", True),              # D3.1
        ("/run/callwarden/nonexistent.sock", "linux", False),  # D3.2
        ("C:\\callwarden\\socket.sock", "windows", False),      # D3.3
        ("/tmp/callwarden.sock", "macos", False),               # D3.4
    ]

    all_match = True
    for socket_path, platform, expected in test_cases:
        py_result = py_is_daemon_available(socket_path, platform)
        rs_result = cc.is_daemon_available_py(socket_path, platform)

        match = py_result == rs_result == expected
        status = "PASS" if match else "FAIL"
        if not match:
            all_match = False
        print(f"  {status} platform={platform:8s} socket_exists={os.path.exists(socket_path)} "
              f"expected={str(expected):5s} py={str(py_result):5s} rs={str(rs_result):5s}")

    tmp.unlink(missing_ok=True)

    assert all_match, "D3: is_daemon_available mismatch"
    print("  D3: ALL PASS\n")


# ============================================================
# D4: route_command
# ============================================================

def test_d4_route_command():
    """D4: route_command — 路由决策矩阵"""
    print("=== D4: route_command ===")

    # 创建临时文件模拟 socket 存在
    tmp = Path(tempfile.gettempdir()) / "cw_test_socket_d4.sock"
    tmp.write_bytes(b"")

    test_cases = [
        # (mode, socket_path, platform, expected)
        ("local", str(tmp), "linux", "local"),                # D4.1
        ("enterprise", str(tmp), "linux", "enterprise"),      # D4.2
        ("enterprise", "/run/cw/nonexistent.sock", "linux", "unavailable"),  # D4.3
        ("enterprise", "C:\\socket.sock", "windows", "unavailable"),          # D4.4
        ("auto", str(tmp), "linux", "enterprise"),            # D4.5
        ("auto", "/run/cw/nonexistent.sock", "linux", "local"),                # D4.6
        ("auto", "C:\\socket.sock", "windows", "local"),      # D4.7
        ("local", "C:\\socket.sock", "windows", "local"),      # D4.8
        # 额外：未知 mode → auto fail-soft
        ("unknown", str(tmp), "linux", "enterprise"),
        ("unknown", "/run/cw/nonexistent.sock", "linux", "local"),
    ]

    all_match = True
    for mode, socket_path, platform, expected in test_cases:
        py_result = py_route_command(mode, socket_path, platform)
        rs_result = cc.route_command_py(mode, socket_path, platform)

        match = py_result == rs_result == expected
        status = "PASS" if match else "FAIL"
        if not match:
            all_match = False
        print(f"  {status} mode={mode:11s} platform={platform:8s} "
              f"socket_exists={os.path.exists(socket_path)} "
              f"expected={expected:12s} py={py_result:12s} rs={rs_result}")

    tmp.unlink(missing_ok=True)

    assert all_match, "D4: route_command mismatch"
    print("  D4: ALL PASS\n")


# ============================================================
# D5: daemon_socket_path
# ============================================================

def test_d5_daemon_socket_path():
    """D5: daemon_socket_path — 环境变量 + 默认值"""
    print("=== D5: daemon_socket_path ===")

    all_match = True

    # D5.1: 环境变量覆盖
    orig = os.environ.get(PY_DAEMON_SOCKET_ENV)
    os.environ[PY_DAEMON_SOCKET_ENV] = "/tmp/x.sock"
    py_result = py_daemon_socket_path()
    rs_result = cc.daemon_socket_path_py()
    match = py_result == rs_result == "/tmp/x.sock"
    status = "PASS" if match else "FAIL"
    if not match:
        all_match = False
    print(f"  {status} D5.1 env_override: expected=/tmp/x.sock "
          f"py={py_result} rs={rs_result}")

    # D5.2: 未设置环境变量 → 默认值
    if orig is None:
        os.environ.pop(PY_DAEMON_SOCKET_ENV, None)
    else:
        os.environ[PY_DAEMON_SOCKET_ENV] = orig
    os.environ.pop(PY_DAEMON_SOCKET_ENV, None)
    py_result = py_daemon_socket_path()
    rs_result = cc.daemon_socket_path_py()
    match = py_result == rs_result == PY_DEFAULT_DAEMON_SOCKET
    status = "PASS" if match else "FAIL"
    if not match:
        all_match = False
    print(f"  {status} D5.2 default: expected={PY_DEFAULT_DAEMON_SOCKET} "
          f"py={py_result} rs={rs_result}")

    # 恢复
    if orig is not None:
        os.environ[PY_DAEMON_SOCKET_ENV] = orig

    assert all_match, "D5: daemon_socket_path mismatch"
    print("  D5: ALL PASS\n")


# ============================================================
# 主入口
# ============================================================

def main():
    print("Phase 5-1 B 差分测试：Rust 路由决策 vs Python 真相源\n")

    tests = [
        test_d1_get_daemon_mode,
        test_d2_is_daemon_required,
        test_d3_is_daemon_available,
        test_d4_route_command,
        test_d5_daemon_socket_path,
    ]

    passed = 0
    failed = 0
    for test in tests:
        try:
            test()
            passed += 1
        except AssertionError as e:
            print(f"  ASSERTION FAILED: {e}\n")
            failed += 1
        except Exception as e:
            print(f"  ERROR: {e}\n")
            failed += 1

    print(f"\n{'='*60}")
    print(f"Phase 5-1 B 差分测试结果：{passed} passed, {failed} failed")
    print(f"{'='*60}")

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
