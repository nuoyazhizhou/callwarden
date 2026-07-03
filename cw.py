#!/usr/bin/env python3
"""
cw - Call Warden 统一命令行入口

使用方式：
    cw install [--all] [--lang rust python]   安装依赖
    cw server [--transport stdio|sse]          启动 MCP Server
    cw --init                                  构建代码图谱
    cw --search "login"                        搜索符号
    cw --call-chain "module::function"         查看调用链
    cw guardrail scan                          安全护栏扫描
    cw gc archive                              归档被 ignore 命中的文件
    cw test <module>                           运行测试（如 test_p0_bugfixes）

也可设置别名：
    alias cw="python /path/to/callwarden/cw.py"
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


def main():
    """统一入口：根据第一个参数分发到对应模块"""
    args = sys.argv[1:] if len(sys.argv) > 1 else []

    # cw install [opts] → 安装依赖
    if args and args[0] == "install":
        sys.argv = ["cw"] + args[1:]
        mod = importlib.import_module(f"{_PKG}.install")
        mod.main()
        return

    # cw server [opts] → 启动 MCP Server
    if args and args[0] == "server":
        sys.argv = ["cw"] + args[1:]
        mod = importlib.import_module(f"{_PKG}.server.mcp_server")
        mod.main()
        return

    # cw test <module> → 运行测试
    if args and args[0] == "test":
        test_name = args[1] if len(args) > 1 else ""
        if not test_name:
            print("用法: cw test <module>  如 cw test test_p0_bugfixes")
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
