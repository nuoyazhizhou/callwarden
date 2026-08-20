"""Phase 5-2 Slice 7: wire-production 路由整合差分测试。

验证 Python cw-client 入口的 Rust 加速路径路由逻辑：
1. 默认走 Python `run_daemon_command`（CW_USE_RUST_CLIENT 未设置）
2. CW_USE_RUST_CLIENT=1 时探测 Rust binary，存在则 exec，不存在则降级
3. rollback_config 表已登记且 rollback_flag=0
4. rollback_flag=1 时强制走 Python（回滚机制）
5. _find_cw_client_binary 查找逻辑（环境变量/PATH/开发路径）

跨平台测试：Windows 上验证路由逻辑（mock binary 存在性），不实际 exec。
"""
import os
import sys
import json
from pathlib import Path
from unittest import mock

# 确保能 import callwarden
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT.parent))


def _import_main_module():
    """导入 cli/main.py 模块（避免触发 main() 入口）。"""
    import importlib.util
    import types

    # 确保 callwarden 包和子包在 sys.modules 中完整注册
    if "callwarden" not in sys.modules:
        sys.modules["callwarden"] = types.ModuleType("callwarden")
        sys.modules["callwarden"].__path__ = [str(_PROJECT_ROOT)]
    if "callwarden.cli" not in sys.modules:
        sys.modules["callwarden.cli"] = types.ModuleType("callwarden.cli")
        sys.modules["callwarden.cli"].__path__ = [str(_PROJECT_ROOT / "cli")]

    # 把 cli 子包作为属性挂到 callwarden 上（mock.patch 需要 getattr 链）
    setattr(sys.modules["callwarden"], "cli", sys.modules["callwarden.cli"])

    # 预导入 daemon_commands 模块，使 mock.patch 能找到它
    dc_path = _PROJECT_ROOT / "cli" / "daemon_commands.py"
    dc_spec = importlib.util.spec_from_file_location(
        "callwarden.cli.daemon_commands", dc_path)
    dc_mod = importlib.util.module_from_spec(dc_spec)
    sys.modules["callwarden.cli.daemon_commands"] = dc_mod
    dc_spec.loader.exec_module(dc_mod)
    # 把 daemon_commands 作为属性挂到 callwarden.cli 上
    setattr(sys.modules["callwarden.cli"], "daemon_commands", dc_mod)

    # 导入 main 模块
    main_path = _PROJECT_ROOT / "cli" / "main.py"
    spec = importlib.util.spec_from_file_location(
        "callwarden.cli.main", main_path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["callwarden.cli.main"] = mod
    spec.loader.exec_module(mod)
    return mod


def _import_db():
    """导入 CodeGraphDB（从父目录作为 callwarden 包）。"""
    from callwarden.db.db import CodeGraphDB
    return CodeGraphDB


# ============================================
# D11 差分测试：路由逻辑验证
# ============================================

def test_d11_routing():
    """D11: 路由逻辑差分测试。"""
    results = []
    main_mod = _import_main_module()

    # D11.1: _find_cw_client_binary 在无环境变量/无 PATH binary 时返回 None
    name = "D11.1 _find_cw_client_binary returns None without binary"
    with mock.patch.dict(os.environ, {}, clear=True):
        # 清除相关环境变量
        env_without = {k: v for k, v in os.environ.items()
                       if k not in ("CW_CLIENT_BIN", "PATH")}
        with mock.patch.dict(os.environ, env_without, clear=True):
            with mock.patch("shutil.which", return_value=None):
                # 确保开发路径不存在 binary
                with mock.patch("os.path.isfile", return_value=False):
                    result = main_mod._find_cw_client_binary()
                    ok = result is None
                    results.append((name, ok))
                    print(f"  {'PASS' if ok else 'FAIL'} {name}: result={result}")

    # D11.2: _find_cw_client_binary 优先使用 CW_CLIENT_BIN 环境变量
    name = "D11.2 _find_cw_client_binary honors CW_CLIENT_BIN env"
    # 创建临时文件模拟 binary
    import tempfile
    with tempfile.NamedTemporaryFile(delete=False, suffix="-cw-client") as tmp:
        tmp_path = tmp.name
    try:
        with mock.patch.dict(os.environ, {"CW_CLIENT_BIN": tmp_path}):
            with mock.patch("os.path.isfile", return_value=True):
                with mock.patch("os.access", return_value=True):
                    result = main_mod._find_cw_client_binary()
                    ok = result is not None and str(result) == tmp_path
                    results.append((name, ok))
                    print(f"  {'PASS' if ok else 'FAIL'} {name}: result={result}")
    finally:
        os.unlink(tmp_path)

    # D11.3：Windows client mode 已启用，并路由到 Python daemon client。
    name = "D11.3 Windows client mode 路由到 Python"
    with mock.patch("sys.platform", "win32"):
        called = {"python_called": False}

        def fake_windows_run_daemon_command(argv, include_serve=True):
            called["python_called"] = True
            return 0

        with mock.patch("callwarden.cli.daemon_commands.run_daemon_command",
                        fake_windows_run_daemon_command):
            rc = main_mod.run_client_mode(["ping"])
        ok = rc == 0 and called["python_called"]
        results.append((name, ok))
        print(f"  {'PASS' if ok else 'FAIL'} {name}: rc={rc}")

    # D11.4: run_client_mode 无参数时打印简介并返回 0
    name = "D11.4 run_client_mode prints help when no args"
    with mock.patch("sys.platform", "linux"):
        with mock.patch("builtins.print") as mock_print:
            rc = main_mod.run_client_mode([])
            printed = " ".join(str(c.args[0]) for c in mock_print.call_args_list
                               if c.args)
            ok = rc == 0 and "Client Mode" in printed and "Subcommands:" in printed
            results.append((name, ok))
            print(f"  {'PASS' if ok else 'FAIL'} {name}: rc={rc}")

    # D11.5: CW_USE_RUST_CLIENT=1 但无 binary 时降级回 Python
    name = "D11.5 CW_USE_RUST_CLIENT=1 falls back to Python when no binary"
    with mock.patch("sys.platform", "linux"):
        with mock.patch.dict(os.environ, {"CW_USE_RUST_CLIENT": "1"}):
            with mock.patch.object(main_mod, "_find_cw_client_binary",
                                   return_value=None):
                # mock run_daemon_command 避免实际执行
                called = {"python_called": False}

                def fake_run_daemon_command(argv, include_serve=True):
                    called["python_called"] = True
                    return 0

                with mock.patch("callwarden.cli.daemon_commands.run_daemon_command",
                                fake_run_daemon_command):
                    rc = main_mod.run_client_mode(["ping"])
                    ok = rc == 0 and called["python_called"]
                    results.append((name, ok))
                    print(f"  {'PASS' if ok else 'FAIL'} {name}: "
                          f"rc={rc}, python_called={called['python_called']}")

    # D11.6: CW_USE_RUST_CLIENT=1 且有 binary 时 exec Rust binary
    name = "D11.6 CW_USE_RUST_CLIENT=1 execs Rust binary when available"
    with mock.patch("sys.platform", "linux"):
        with mock.patch.dict(os.environ, {"CW_USE_RUST_CLIENT": "1"}):
            with mock.patch.object(main_mod, "_find_cw_client_binary",
                                   return_value=Path("/fake/cw-client")):
                # mock subprocess.run 避免实际 exec
                exec_called = {"called": False, "args": None}

                class FakeProc:
                    returncode = 42

                def fake_run(cmd, env=None):
                    exec_called["called"] = True
                    exec_called["args"] = cmd
                    return FakeProc()

                with mock.patch("subprocess.run", fake_run):
                    rc = main_mod.run_client_mode(["ping"])
                    # Windows 上 Path("/fake/cw-client") → \fake\cw-client
                    args_str = str(exec_called["args"][0]).replace("\\", "/")
                    ok = (rc == 42 and exec_called["called"]
                          and "fake/cw-client" in args_str)
                    results.append((name, ok))
                    print(f"  {'PASS' if ok else 'FAIL'} {name}: "
                          f"rc={rc}, exec_called={exec_called['called']}, "
                          f"args0={exec_called['args'][0]}")

    # D11.7: 默认（CW_USE_RUST_CLIENT 未设置）走 Python
    name = "D11.7 default routing goes to Python"
    with mock.patch("sys.platform", "linux"):
        with mock.patch.dict(os.environ, {}, clear=True):
            called = {"python_called": False}

            def fake_run_daemon_command(argv, include_serve=True):
                called["python_called"] = True
                return 0

            # 这是路由差分测试，不应依赖本机是否正在运行 daemon；
            # 真实 daemon round-trip 由 W2.3/W2.4 进程级 E2E 覆盖。
            with mock.patch("callwarden.cli.daemon_commands.run_daemon_command",
                            fake_run_daemon_command):
                rc = main_mod.run_client_mode(["ping"])
                ok = rc == 0 and called["python_called"]
                results.append((name, ok))
                print(f"  {'PASS' if ok else 'FAIL'} {name}: "
                      f"rc={rc}, python_called={called['python_called']}")

    # D11.8: Rust binary exec 失败时降级回 Python（fail-soft）
    name = "D11.8 Rust binary exec failure falls back to Python"
    with mock.patch("sys.platform", "linux"):
        with mock.patch.dict(os.environ, {"CW_USE_RUST_CLIENT": "1"}):
            with mock.patch.object(main_mod, "_find_cw_client_binary",
                                   return_value=Path("/fake/cw-client")):
                def fake_run_raises(cmd, env=None):
                    raise OSError("Permission denied")

                python_called = {"called": False}

                def fake_run_daemon_command(argv, include_serve=True):
                    python_called["called"] = True
                    return 0

                with mock.patch("subprocess.run", fake_run_raises):
                    with mock.patch("callwarden.cli.daemon_commands."
                                    "run_daemon_command",
                                    fake_run_daemon_command):
                        rc = main_mod.run_client_mode(["ping"])
                        ok = rc == 0 and python_called["called"]
                        results.append((name, ok))
                        print(f"  {'PASS' if ok else 'FAIL'} {name}: "
                              f"rc={rc}, python_called={python_called['called']}")

    # D11.9: rollback_config 表已登记 rust_cw_client_routing
    name = "D11.9 rollback_config registered for rust_cw_client_routing"
    CodeGraphDB = _import_db()
    db = CodeGraphDB()
    try:
        config = db.get_rollback_config("T-1785281740250-bce9a676")
        config_blob = config.get("config_blob", {}) if config else {}
        # config_blob 是 dict（反序列化后），检查 flag 字段
        ok = (config is not None
              and config.get("feature_name") == "rust_cw_client_routing"
              and config.get("phase") == 5
              and config.get("rollback_flag") == 0
              and config_blob.get("flag") == "CW_USE_RUST_CLIENT")
        results.append((name, ok))
        print(f"  {'PASS' if ok else 'FAIL'} {name}: "
              f"feature={config.get('feature_name') if config else None}, "
              f"flag={config.get('rollback_flag') if config else None}, "
              f"config_blob={config_blob}")
    finally:
        db.close()

    # D11.10: is_feature_rolled_back 默认返回 False（rollback_flag=0）
    name = "D11.10 is_feature_rolled_back returns False by default"
    CodeGraphDB = _import_db()
    db = CodeGraphDB()
    try:
        rolled_back = db.is_feature_rolled_back("rust_cw_client_routing")
        ok = rolled_back is False
        results.append((name, ok))
        print(f"  {'PASS' if ok else 'FAIL'} {name}: rolled_back={rolled_back}")
    finally:
        db.close()

    # D11.11: set_rollback_flag=1 后 is_feature_rolled_back 返回 True
    name = "D11.11 is_feature_rolled_back returns True after set_rollback_flag=1"
    CodeGraphDB = _import_db()
    db = CodeGraphDB()
    try:
        # 设置 rollback_flag=1 模拟回滚
        result = db.set_rollback_flag("T-1785281740250-bce9a676", 1,
                                       reason="test_rollback")
        rolled_back = db.is_feature_rolled_back("rust_cw_client_routing")
        ok = (result.get("success") and rolled_back is True)
        results.append((name, ok))
        print(f"  {'PASS' if ok else 'FAIL'} {name}: rolled_back={rolled_back}")
        # 恢复 rollback_flag=0
        db.set_rollback_flag("T-1785281740250-bce9a676", 0,
                              reason="test_restore")
    finally:
        db.close()

    # D11.12: rollback_flag=1 恢复后 is_feature_rolled_back 返回 False
    name = "D11.12 is_feature_rolled_back returns False after restoring flag=0"
    CodeGraphDB = _import_db()
    db = CodeGraphDB()
    try:
        rolled_back = db.is_feature_rolled_back("rust_cw_client_routing")
        ok = rolled_back is False
        results.append((name, ok))
        print(f"  {'PASS' if ok else 'FAIL'} {name}: rolled_back={rolled_back}")
    finally:
        db.close()

    # D11.13: _try_exec_rust_cw_client 返回 None 当 binary 不可用
    name = "D11.13 _try_exec_rust_cw_client returns None when no binary"
    with mock.patch.object(main_mod, "_find_cw_client_binary",
                           return_value=None):
        result = main_mod._try_exec_rust_cw_client(["ping"])
        ok = result is None
        results.append((name, ok))
        print(f"  {'PASS' if ok else 'FAIL'} {name}: result={result}")

    # D11.14: _try_exec_rust_cw_client 返回退出码当 binary 可用
    name = "D11.14 _try_exec_rust_cw_client returns exit code when binary available"
    with mock.patch.object(main_mod, "_find_cw_client_binary",
                           return_value=Path("/fake/cw-client")):
        class FakeProc:
            returncode = 7

        def fake_run(cmd, env=None):
            return FakeProc()

        with mock.patch("subprocess.run", fake_run):
            result = main_mod._try_exec_rust_cw_client(["ping"])
            ok = result == 7
            results.append((name, ok))
            print(f"  {'PASS' if ok else 'FAIL'} {name}: result={result}")

    # 汇总
    print()
    passed = sum(1 for _, ok in results if ok)
    failed = sum(1 for _, ok in results if not ok)
    print(f"Phase 5-2 Slice 7 D11 路由差分测试结果：{passed} passed, {failed} failed")
    if failed == 0:
        print("总计：ALL PASS")
    else:
        print("总计：HAS FAILURES")
    return results


if __name__ == "__main__":
    results = test_d11_routing()
    failed = sum(1 for _, ok in results if not ok)
    sys.exit(1 if failed > 0 else 0)
