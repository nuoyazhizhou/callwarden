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

注意：Python 3.14 + Windows 下通过 pip entry_point（cw.exe）启动时，
sqlite3 文件连接可能因并发访问间歇性失败。如遇
"unable to open database file" 错误，请使用 `python cw.py` 或设置别名。
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


def _warmup_sqlite():
    """预热 sqlite3 文件连接，避免并发访问时 SQLITE_BUSY

    根因：MCP Server 与 CLI 跨进程并发访问同一个 db 文件。
    SQLite WAL 模式允许并发读写，但需要 busy_timeout 让内核等待锁释放。
    本函数在 cw.py 顶部主动用真实项目数据库路径打开连接，设置 busy_timeout
    并触发 WAL 文件创建（-wal/-shm），为后续连接预热 VFS 缓存。

    Returns:
        True 表示预热成功，False 表示失败
    """
    import sqlite3
    import time
    try:
        from callwarden.config import detect_project_root, get_project_db_path
        cwd = os.getcwd()
        root = detect_project_root(cwd)
        if not root:
            return False
        db_path = get_project_db_path(root)
        os.makedirs(os.path.dirname(db_path), exist_ok=True)
        # 预热：connect + busy_timeout + WAL + 真实写入（创建 -wal/-shm 文件）
        for attempt in range(3):
            try:
                conn = sqlite3.connect(db_path)
                # 关键：先设置 busy_timeout，再执行任何 SQL
                conn.execute("PRAGMA busy_timeout=5000")
                conn.execute("PRAGMA journal_mode=WAL")
                # 真实写入：强制创建 -wal/-shm 文件，避免后续连接被锁
                conn.execute("CREATE TABLE IF NOT EXISTS _warmup (id INTEGER)")
                conn.execute("DROP TABLE IF EXISTS _warmup")
                conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                conn.close()
                return True
            except Exception:
                time.sleep(0.5 * (attempt + 1))
        return False
    except Exception:
        return False


def _check_entry_point_sqlite():
    """检测 entry_point 启动时 sqlite3 是否可用，不可用时给出友好提示

    Python 3.14 + Windows 下 cw.exe（entry_point）启动时，sqlite3 文件连接
    可能因并发访问间歇性失败。检测到失败时打印提示并退出，
    让用户改用 `python cw.py` 或设置别名。
    """
    if not _is_entry_point_launch():
        return  # 不是 entry_point 启动，不需要检测

    if _warmup_sqlite():
        return  # 预热成功，继续执行

    # 预热失败，打印友好提示并退出
    # 用 i18n t() 获取翻译（en_US 系统显示英文，zh_CN 系统显示中文），
    # 并处理 Windows GBK 终端编码问题：stderr 非 utf-8 时 reconfigure
    try:
        from callwarden.i18n import t
        msg = t(
            "cli.messages.entry_point_sqlite_error",
            cw_py=os.path.abspath(__file__),
            args=" ".join(sys.argv[1:]),
        )
    except Exception:
        # i18n 模块本身不可用时，退回到英文硬编码（避免循环依赖）
        msg = (
            "Error: sqlite3 failed to open the database file when launched via cw.exe.\n"
            "Solutions: 1) python cw.py ...  2) alias cw=\"python cw.py\"  "
            "3) retry  4) stop MCP Server (cw server --stop)"
        )

    # 编码安全：Windows GBK 终端下中文 print 可能 UnicodeEncodeError。
    # 检测 stderr.encoding，非 utf-8 时 reconfigure（Python 3.7+）。
    try:
        enc = (sys.stderr.encoding or "").lower()
        if enc and enc not in ("utf-8", "utf8"):
            sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        # reconfigure 不可用（重定向到 StringIO 等）：直接写字节
        try:
            sys.stderr.buffer.write(msg.encode("utf-8", errors="replace"))
            sys.stderr.buffer.write(b"\n")
            sys.stderr.flush()
            sys.exit(1)
        except Exception:
            pass  # 最后兜底，用默认 print

    print(msg, file=sys.stderr)
    sys.exit(1)


# 在任何 callwarden 模块导入之前执行检测
_check_entry_point_sqlite()


def _ensure_utf8_output():
    """强制 stdout/stderr 使用 UTF-8 编码，避免 Windows GBK 控制台无法输出 Unicode 字符

    复用 cli/console.py 的 ensure_utf8_output()，保持单一实现。
    """
    from callwarden.cli.console import ensure_utf8_output
    ensure_utf8_output()


def main():
    """统一入口：根据第一个参数分发到对应模块"""
    # 修复 Bug T-1783751418408-44eb: Windows GBK 控制台无法输出 Unicode 字符
    _ensure_utf8_output()

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
