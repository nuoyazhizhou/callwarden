"""cw-agent: Linux per-UID watcher agent 入口。

仅在 Linux 上可用。通过 UDS/FD 向 Enterprise Daemon 报告文件事件。
Windows/macOS 上启动时直接 fail-closed。

用法：
    cw-agent start --workspace /path/to/project
    cw-agent status
    cw-agent stop
"""

import sys


def main():
    """cw-agent 主入口。"""
    if sys.platform not in ("linux", "win32"):
        print(
            "ERROR: cw-agent is supported on Linux and Windows.\n"
            "On macOS, use 'cw' in local mode.",
            file=sys.stderr,
        )
        sys.exit(2)

    from callwarden.cli.main import run_agent_mode
    sys.exit(run_agent_mode(sys.argv[1:]))


if __name__ == "__main__":
    main()
