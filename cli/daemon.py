"""cw-daemon: Linux system daemon 前台入口。

仅在 Linux 上可用。提供 UDS socket、SO_PEERCRED ACL、CAS、Replicator、SnapshotManager。
Windows/macOS 上启动时直接 fail-closed。

用法：
    cw-daemon --config /etc/callwarden/config.toml
    cw-daemon --foreground
    cw-daemon --version
"""

import sys


def main():
    """cw-daemon 主入口。"""
    if sys.platform not in ("linux", "win32"):
        print(
            "ERROR: cw-daemon is supported on Linux and Windows.\n"
            "On macOS, use 'cw server' for MCP stdio/SSE mode.",
            file=sys.stderr,
        )
        sys.exit(2)

    from callwarden.cli.main import run_daemon_mode
    sys.exit(run_daemon_mode(sys.argv[1:]))


if __name__ == "__main__":
    main()
