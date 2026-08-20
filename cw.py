#!/usr/bin/env python3
"""
cw - Call Warden 统一命令行入口

使用方式：
    cw install [--all] [--lang rust python]   安装依赖
    cw server [--transport stdio|sse]          启动 MCP Server
    cw --refresh-all                           构建代码图谱
    cw --search "login"                        搜索符号
    cw --call-chain "module::function"         查看调用链
    cw guardrail scan                          安全护栏扫描
    cw gc archive                              归档被 ignore 命中的文件
    cw test <module>                           运行测试（如 test_p0_bugfixes）

也可设置别名：
    alias cw="python /path/to/callwarden/cw.py"

T04（cw-rust-client-convergence）：移除 SQLite 预热（_warmup_sqlite）与
entry_point sqlite 检测——收敛架构下 Python 是纯 client，业务 SQL 全部下沉
daemon（写路径权威 daemon，Python 零本地写）。
"""
import sys
import os
import importlib

# 确保父目录在 Python 路径中，使 callwarden 包可被正确导入
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

# 包名（取目录名，自动适应重命名）
_PKG = os.path.basename(os.path.dirname(os.path.abspath(__file__)))


def _is_entry_point_launch():
    """检测当前是否通过 pip entry_point（cw.exe）启动

    entry_point 启动时 sys.argv[0] 是 Scripts/cw 路径，
    而 python cw.py 启动时 sys.argv[0] 是 cw.py 本身。
    """
    if not sys.argv:
        return False
    argv0 = sys.argv[0].lower()
    return ("scripts\\cw" in argv0) or ("scripts/cw" in argv0)


def _ensure_utf8_output():
    """强制 stdout/stderr 使用 UTF-8 编码，避免 Windows GBK 控制台无法输出 Unicode 字符

    复用 cli/console.py 的 ensure_utf8_output()，保持单一实现。
    """
    from callwarden.cli.console import ensure_utf8_output
    ensure_utf8_output()


def _enforce_frozen_parse_mode():
    """P1-F Step 6: frozen build 强制 rust-strict 解析模式

    设计文档：docs/design/rust-only-parser-cutover-plan.md §7 + §8 Phase 4 步骤 1

    约束：
        - frozen build（PyInstaller）固定允许 rust-strict
        - frozen build 收到 python-reference / shadow 时报错退出
        - frozen build 不允许 CW_DISABLE_RUST_PARSE

    本函数在非 frozen build（源码开发）中是 no-op，不影响开发流程。
    在 frozen build 中，若环境变量配置违反约束，打印明确错误并 exit(2)。

    延迟导入 ParseMode 避免在 cw.py 顶部拉入 db 包链。
    """
    if not (getattr(sys, 'frozen', False) and hasattr(sys, '_MEIPASS')):
        return  # 非 frozen build，no-op

    try:
        from callwarden.db.rust_parser_facade import ParseMode
        ParseMode.validate_for_environment()
    except RuntimeError as e:
        # frozen build 收到非法 CW_PARSE_MODE 或 CW_DISABLE_RUST_PARSE
        msg = (
            f"ERROR: frozen build parse mode 校验失败：{e}\n"
            "frozen build 仅允许 rust-strict 模式（设计 §7）。\n"
            "请清除 CW_PARSE_MODE 和 CW_DISABLE_RUST_PARSE 环境变量后重试。"
        )
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
        print(msg, file=sys.stderr)
        sys.exit(2)
    except Exception as e:
        # 其他导入/运行时错误：打印警告但不阻塞（避免 frozen build 无法启动）
        # ParseMode 不可用时退化为默认 rust-strict，符合 frozen build 行为
        try:
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError):
            pass
        print(
            f"WARNING: frozen build parse mode 校验跳过：{e}",
            file=sys.stderr,
        )


def main():
    """统一入口：根据第一个参数分发到对应模块"""
    # 修复 Bug T-1783751418408-44eb: Windows GBK 控制台无法输出 Unicode 字符
    _ensure_utf8_output()

    # P1-F Step 6: frozen build 强制 rust-strict 解析模式（设计 §7）
    _enforce_frozen_parse_mode()

    args = sys.argv[1:] if len(sys.argv) > 1 else []

    # cw install [opts] → 安装依赖
    if args and args[0] == "install":
        sys.argv = ["cw"] + args[1:]
        mod = importlib.import_module(f"{_PKG}.install")
        mod.main()
        return

    # cw server [opts] → 启动 MCP Server
    if args and args[0] == "server":
        # 企业 daemon 是独立 Rust 进程，不能交给 FastMCP stdio。
        # nohup/systemd 下 stdin 可能已关闭，MCP runner 会因此抛 closed-file。
        if "--mode" in args[1:]:
            mode_index = args.index("--mode", 1)
            if mode_index + 1 < len(args) and args[mode_index + 1] == "daemon":
                from callwarden.cli.main import run_daemon_mode
                raise SystemExit(run_daemon_mode(args[1:]))
        sys.argv = ["cw"] + args[1:]
        mod = importlib.import_module(f"{_PKG}.server.mcp_server")
        mod.main()
        return

    # cw test <module> → 运行测试
    if args and args[0] == "test":
        test_name = args[1] if len(args) > 1 else ""
        if not test_name:
            from .i18n import t
            print(t("cli_test_usage"))
            sys.exit(1)
        sys.argv = ["cw"] + args[2:]
        mod = importlib.import_module(f"{_PKG}.tests.{test_name}")
        # 调用 pytest 风格的入口
        import pytest
        sys.exit(pytest.main([f"{_PKG}/tests/{test_name}.py"] + sys.argv[1:]))

    # 其余命令（--flag 风格和子命令）透传给 CLI 主入口
    mod = importlib.import_module(f"{_PKG}.cli.main")
    mod.main()


if __name__ == "__main__":
    main()
