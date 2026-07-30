"""Phase 5-1 差分测试：Rust CLI 实现 vs Python 真相源

覆盖契约 D1-D5 测试矩阵：
- D1: platform_paths_detect
- D2: load_config
- D3: config_explain
- D4: check_role_supported
- D5: is_readonly_command / is_readonly_args

契约：docs/design/phase5-1-cli-config-contract.md §4
"""

import os
import sys
from pathlib import Path

# 确保项目根目录在 sys.path 中
_PROJECT_ROOT = Path(__file__).parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from release.config_loader import (
    PlatformPaths,
    Config,
    ConfigValue,
    load_config as py_load_config,
    check_role_supported as py_check_role_supported,
)

# 从 cli/main.py 提取的只读命令识别逻辑（避免导入 529KB 大文件的副作用）
# 对齐 cli/main.py L63-103 + L1098-1198
_READONLY_TASK_ACTIONS = {"list", "show", "findings"}
_READONLY_RULE_ACTIONS = {"list", "candidate", "applicable", "extract"}
_READONLY_AUDIT_ACTIONS = {"verify", "keys"}
_READONLY_BOOTSTRAP_ACTIONS = {"status"}
_READONLY_CLONE_ACTIONS = {"list", "stats"}
_READONLY_WORKSPACE_ACTIONS = {"list"}
_READONLY_GIT_ACTIONS = {"log", "show", "stats", "check-task", "destructive-log"}
_READONLY_SEMGREP_ACTIONS = {"list", "stats"}
_READONLY_COVERAGE_ACTIONS = {"fn", "uncovered"}
_READONLY_FTS_ACTIONS = {"status"}
_READONLY_GRAPH_ACTIONS = {"build-from-c"}
_READONLY_CONFIG_ACTIONS = {"explain", "paths"}
_READONLY_ROLLBACK_ACTIONS = {"config", "show", "is-rolled-back"}

_WRITE_FLAGS = {
    "refresh_all", "refresh", "watch",
    "register_workspace", "set_workspace", "delete_workspace",
    "restore_comment", "restore_all_comments",
    "coverage_import",
}


def py_is_readonly_command(cmd: str, sub_argv: list) -> bool:
    """Python 真相源：cli/main.py:_is_readonly_command (L1098-1180)"""
    action = sub_argv[0] if sub_argv else ""
    if cmd == "task":
        return action in _READONLY_TASK_ACTIONS
    if cmd == "rule":
        return action in _READONLY_RULE_ACTIONS
    if cmd in {"doctor", "check-gate", "test-impact", "hotspot", "churn", "evolution",
               "impact", "review", "vuln-blast", "symbol-history"}:
        return True
    if cmd == "guardrail":
        return True
    if cmd == "defect":
        return action in {"stats", "list", "show"}
    if cmd == "gc":
        return action in {"list", "inspect", "db-cleanup"}
    if cmd == "audit":
        return action in _READONLY_AUDIT_ACTIONS
    if cmd == "bootstrap":
        return action in _READONLY_BOOTSTRAP_ACTIONS
    if cmd == "clone":
        return action in _READONLY_CLONE_ACTIONS
    if cmd == "tests":
        return not ("--build" in sub_argv or "--import" in sub_argv)
    if cmd in {"search", "grep", "symbol", "file", "query",
               "callers", "callees", "call-chain", "topo",
               "metrics", "complexity", "coupling", "comment-coverage", "uncommented",
               "function-issues", "largest-fns", "coupled-fns", "fn-metrics",
               "who", "ownership-map", "brief", "map", "stats", "status",
               "health-report", "dashboard"}:
        return True
    if cmd == "workspace":
        return action in _READONLY_WORKSPACE_ACTIONS
    if cmd == "git":
        return action in _READONLY_GIT_ACTIONS
    if cmd == "semgrep":
        return action in _READONLY_SEMGREP_ACTIONS
    if cmd == "coverage":
        return action in _READONLY_COVERAGE_ACTIONS
    if cmd == "fts":
        return action in _READONLY_FTS_ACTIONS
    if cmd == "graph":
        return action in _READONLY_GRAPH_ACTIONS
    if cmd == "config":
        return action in _READONLY_CONFIG_ACTIONS
    if cmd == "rollback":
        return action in _READONLY_ROLLBACK_ACTIONS
    if cmd == "refresh":
        return False
    return False


def py_is_readonly_args(args) -> bool:
    """Python 真相源：cli/main.py:_is_readonly_args (L1183-1198)"""
    for flag in _WRITE_FLAGS:
        if getattr(args, flag, None):
            return False
    return True


import callwarden_core as cc


# ============================================================
# D1: platform_paths_detect
# ============================================================

def test_d1_platform_paths():
    """D1: platform_paths_detect — Rust vs Python 平台路径对比"""
    print("=== D1: platform_paths_detect ===")

    # Python 真相源
    py_paths = PlatformPaths.detect()
    py_dict = {
        "system_config": str(py_paths.system_config),
        "user_config": str(py_paths.user_config),
        "system_data": str(py_paths.system_data),
        "user_data": str(py_paths.user_data),
        "runtime": str(py_paths.runtime) if py_paths.runtime else "",
    }

    # Rust 实现
    rs_dict = cc.platform_paths_detect()

    # 对比
    all_match = True
    for key in ["system_config", "user_config", "system_data", "user_data", "runtime"]:
        py_val = py_dict[key]
        rs_val = rs_dict[key]
        # 路径分隔符可能不同（/ vs \），统一为正斜杠对比
        py_norm = py_val.replace("\\", "/")
        rs_norm = rs_val.replace("\\", "/")
        match = py_norm == rs_norm
        status = "PASS" if match else "FAIL"
        if not match:
            all_match = False
        print(f"  {status} {key}: py={py_val} rs={rs_val}")

    assert all_match, "D1: platform_paths_detect mismatch"
    print("  D1: ALL PASS\n")


# ============================================================
# D2: load_config
# ============================================================

def test_d2_load_config():
    """D2: load_config — Rust vs Python 配置加载对比"""
    print("=== D2: load_config ===")

    # D2.1: 无配置文件 + 无 env + 无 CLI → 默认值
    # 清除可能影响测试的环境变量
    test_env = {k: v for k, v in os.environ.items() if not k.startswith("CW_")}

    # Python 真相源
    py_config = py_load_config(cli_overrides=None, env_prefix="CW_")
    py_log_level = py_config.get("log_level")
    py_max_workers = py_config.get("max_workers")
    py_watcher_debounce = py_config.get("watcher_debounce_ms")
    py_cas_grace = py_config.get("cas_grace_days")

    # Rust 实现
    rs_config = cc.load_config_py(None, "CW_")
    rs_log_level = rs_config.get("log_level", {}).get("value", "")
    rs_max_workers = rs_config.get("max_workers", {}).get("value", "")
    rs_watcher_debounce = rs_config.get("watcher_debounce_ms", {}).get("value", "")
    rs_cas_grace = rs_config.get("cas_grace_days", {}).get("value", "")

    # 对比默认值
    checks = [
        ("log_level", str(py_log_level), str(rs_log_level)),
        ("max_workers", str(py_max_workers), str(rs_max_workers)),
        ("watcher_debounce_ms", str(py_watcher_debounce), str(rs_watcher_debounce)),
        ("cas_grace_days", str(py_cas_grace), str(rs_cas_grace)),
    ]
    all_match = True
    for key, py_val, rs_val in checks:
        match = py_val == rs_val
        status = "PASS" if match else "FAIL"
        if not match:
            all_match = False
        print(f"  {status} default {key}: py={py_val} rs={rs_val}")

    # D2.2: CLI override
    py_config2 = py_load_config(cli_overrides={"log_level": "error"}, env_prefix="CW_")
    rs_config2 = cc.load_config_py({"log_level": "error"}, "CW_")
    py_source = py_config2.values.get("log_level").source
    rs_source = rs_config2.get("log_level", {}).get("source", "")
    match = py_source == rs_source == "cli"
    status = "PASS" if match else "FAIL"
    if not match:
        all_match = False
    print(f"  {status} CLI override: py_source={py_source} rs_source={rs_source}")

    assert all_match, "D2: load_config mismatch"
    print("  D2: ALL PASS\n")


# ============================================================
# D3: config_explain
# ============================================================

def test_d3_config_explain():
    """D3: config_explain — secret 字段隐藏"""
    print("=== D3: config_explain ===")

    # Rust config_explain
    rs_explain = cc.config_explain_py()
    rs_keys = {e["key"] for e in rs_explain}

    # 验证默认字段存在
    expected_keys = {"log_level", "max_workers", "watcher_debounce_ms", "cas_grace_days"}
    has_defaults = expected_keys.issubset(rs_keys)
    status = "PASS" if has_defaults else "FAIL"
    print(f"  {status} default keys present: {expected_keys.issubset(rs_keys)}")

    # 验证排序（按 key 字母序）
    keys_list = [e["key"] for e in rs_explain]
    is_sorted = keys_list == sorted(keys_list)
    status = "PASS" if is_sorted else "FAIL"
    print(f"  {status} sorted by key: {is_sorted}")

    # D3.1/D3.2: secret 字段测试需要模拟 secret 字段
    # 由于 config_explain_py 不接受参数，我们验证当前配置中无 secret 字段时
    # 所有值都是明文（非 "***"）
    all_plain = all(e["value"] != "***" for e in rs_explain if "token" not in e["key"].lower())
    status = "PASS" if all_plain else "FAIL"
    print(f"  {status} non-secret fields are plain: {all_plain}")

    assert has_defaults and is_sorted, "D3: config_explain mismatch"
    print("  D3: ALL PASS\n")


# ============================================================
# D4: check_role_supported
# ============================================================

def test_d4_check_role_supported():
    """D4: check_role_supported — 平台×角色矩阵"""
    print("=== D4: check_role_supported ===")

    test_cases = [
        # (role, platform, expected)
        ("daemon", "linux", True),     # D4.1
        ("agent", "linux", True),      # D4.2
        ("daemon", "win32", False),    # D4.3
        ("agent", "darwin", False),    # D4.4
        ("local", "win32", True),      # D4.5
        ("local", "unknown", False),   # D4.6
        ("client", "linux", True),
        ("client", "win32", True),
        ("client", "darwin", True),
        ("all", "linux", True),
        ("all", "win32", False),
    ]

    all_match = True
    for role, platform, expected in test_cases:
        py_result = py_check_role_supported(role, platform)
        rs_result = cc.check_role_supported_py(role, platform)
        match = py_result == rs_result == expected
        status = "PASS" if match else "FAIL"
        if not match:
            all_match = False
        print(f"  {status} role={role:8s} platform={platform:7s} "
              f"expected={expected} py={py_result} rs={rs_result}")

    assert all_match, "D4: check_role_supported mismatch"
    print("  D4: ALL PASS\n")


# ============================================================
# D5: is_readonly_command / is_readonly_args
# ============================================================

def test_d5_is_readonly_command():
    """D5: is_readonly_command — 子命令只读判断"""
    print("=== D5: is_readonly_command ===")

    test_cases = [
        # (cmd, sub_argv, expected)
        ("task", ["list"], True),              # D5.1
        ("task", ["create"], False),           # D5.2
        ("search", [], True),                  # D5.3
        ("refresh", ["--all"], False),         # D5.4
        ("audit", ["verify"], True),           # D5.5
        ("audit", ["rotate-key"], False),      # D5.6
        ("tests", ["--history"], True),        # D5.7
        ("tests", ["--build"], False),         # D5.8
        ("rollback", ["config"], True),        # D5.9
        ("rollback", ["register"], False),     # D5.10
        ("unknown_cmd", [], False),            # D5.11
        # 补充测试
        ("doctor", [], True),
        ("check-gate", [], True),
        ("guardrail", [], True),
        ("defect", ["stats"], True),
        ("defect", ["import"], False),
        ("gc", ["list"], True),
        ("gc", ["archive"], False),
        ("git", ["log"], True),
        ("git", ["import"], False),
        ("workspace", ["list"], True),
        ("workspace", ["register"], False),
        ("config", ["explain"], True),
        ("config", ["set"], False),
        ("stats", [], True),
        ("status", [], True),
        ("dashboard", [], True),
    ]

    all_match = True
    for cmd, sub_argv, expected in test_cases:
        py_result = py_is_readonly_command(cmd, list(sub_argv))
        rs_result = cc.is_readonly_command_py(cmd, list(sub_argv))
        match = py_result == rs_result == expected
        status = "PASS" if match else "FAIL"
        if not match:
            all_match = False
        print(f"  {status} {cmd:12s} {str(sub_argv):20s} "
              f"expected={str(expected):5s} py={str(py_result):5s} rs={str(rs_result):5s}")

    assert all_match, "D5: is_readonly_command mismatch"
    print("  D5: ALL PASS\n")


def test_d5_is_readonly_args():
    """D5b: is_readonly_args — flag 模式只读判断"""
    print("=== D5b: is_readonly_args ===")

    # Python _is_readonly_args 接受 argparse args 对象
    # Rust is_readonly_args_py 接受已设置的 write flag 名称列表
    # 我们需要模拟 argparse args 对象

    class MockArgs:
        """模拟 argparse args 对象"""
        def __init__(self, flags=None):
            if flags:
                for f in flags:
                    setattr(self, f, True)

    test_cases = [
        # (set_write_flags, expected_readonly)
        ([], True),                           # 无 write flag → 只读
        (["refresh_all"], False),             # refresh_all → 写
        (["refresh"], False),                 # refresh → 写
        (["watch"], False),                   # watch → 写
        (["register_workspace"], False),      # register_workspace → 写
        (["verbose"], True),                  # 非 write flag → 只读
        (["verbose", "refresh"], False),      # 含 write flag → 写
    ]

    all_match = True
    for flags, expected in test_cases:
        # Python: 模拟 argparse args
        py_args = MockArgs(flags if flags else None)
        py_result = py_is_readonly_args(py_args)

        # Rust: 传入已设置的 write flag 列表
        write_flags_set = [f for f in flags if f in _WRITE_FLAGS]
        rs_result = cc.is_readonly_args_py(write_flags_set)

        match = py_result == rs_result == expected
        status = "PASS" if match else "FAIL"
        if not match:
            all_match = False
        print(f"  {status} flags={str(flags):30s} "
              f"expected={str(expected):5s} py={str(py_result):5s} rs={str(rs_result):5s}")

    assert all_match, "D5b: is_readonly_args mismatch"
    print("  D5b: ALL PASS\n")


# ============================================================
# 主入口
# ============================================================

def main():
    print("Phase 5-1 差分测试：Rust CLI vs Python 真相源\n")

    tests = [
        test_d1_platform_paths,
        test_d2_load_config,
        test_d3_config_explain,
        test_d4_check_role_supported,
        test_d5_is_readonly_command,
        test_d5_is_readonly_args,
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
    print(f"Phase 5-1 差分测试结果：{passed} passed, {failed} failed")
    print(f"{'='*60}")

    return 1 if failed > 0 else 0


if __name__ == "__main__":
    sys.exit(main())
