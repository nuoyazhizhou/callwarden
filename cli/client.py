"""cw-client: 仅 RPC/MCP proxy 入口。

不包含 parser 和本地 DB 写能力，只通过 UDS 连接 Enterprise Daemon。
Windows/macOS 上如果 daemon 不可用则 fail-closed。

用法：
    cw-client ping
    cw-client query --symbol "module.fn"
    cw-client refresh --workspace /path/to/project
"""

import sys


def main():
    """cw-client 主入口。"""
    from callwarden.cli.main import run_client_mode
    sys.exit(run_client_mode(sys.argv[1:]))


if __name__ == "__main__":
    main()
