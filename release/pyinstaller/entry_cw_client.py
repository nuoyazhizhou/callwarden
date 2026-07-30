#!/usr/bin/env python3
"""PyInstaller 入口包装：cw-client 命令（无 parser 轻量包）。

绕过 cli.main（其顶层 ``from ..db import CodeGraphDB`` 会拉入
db.py → db_build.py → parsers → tree_sitter 链），直接调用
``callwarden.cli.daemon_commands.run_daemon_command``。

通过 sys.modules stub 注入 ``callwarden`` 和 ``callwarden.db`` 包，
跳过：
- ``callwarden/__init__.py`` 的 ``from .db import CodeGraphDB``
- ``callwarden/db/__init__.py`` 的 ``from .db import CodeGraphDB``

stub 设置 ``__path__ = []``，PyInstaller FrozenImporter 通过
``sys.meta_path`` 接管子模块加载（从 PYZ 归档定位），不依赖文件系统路径。
"""

import os
import sys
import types

# === 计算 callwarden 包源码路径 ===
# 冻结模式下 FrozenImporter 通过 sys.meta_path 从 PYZ 加载，但 __path__
# 不能为空列表——FrozenImporter 查找子模块时会检查父包 __path__。
# 设置为 _MEIPASS 中的实际路径，FrozenImporter 能正确定位 PYZ 子模块。
if hasattr(sys, 'frozen') and sys.frozen:
    _MEIPASS = getattr(sys, '_MEIPASS', '')
    _PKG_PATH = [os.path.join(_MEIPASS, 'callwarden')]
    _DB_PATH = [os.path.join(_MEIPASS, 'callwarden', 'db')]
else:
    # entry 脚本在 release/pyinstaller/，callwarden 包根在上三级
    _ENTRY_DIR = os.path.dirname(os.path.abspath(__file__))
    _PKG_ROOT = os.path.dirname(os.path.dirname(_ENTRY_DIR))
    _PKG_PATH = [_PKG_ROOT]
    _DB_PATH = [os.path.join(_PKG_ROOT, 'db')]

# === sys.modules stub：跳过 callwarden/__init__.py 的 parser 拉入链 ===
# 必须在任何 ``import callwarden.*`` 之前执行。
# 不执行 __init__.py，但保留包身份（__path__）以允许子模块导入。
if 'callwarden' not in sys.modules:
    _cw_stub = types.ModuleType('callwarden')
    _cw_stub.__path__ = _PKG_PATH
    _cw_stub.__version__ = '0.3.6'
    sys.modules['callwarden'] = _cw_stub

# === sys.modules stub：跳过 callwarden/db/__init__.py 的 CodeGraphDB import ===
# db_daemon 等子模块通过 hiddenimports 显式收集，FrozenImporter 可从 PYZ 加载。
if 'callwarden.db' not in sys.modules:
    _db_stub = types.ModuleType('callwarden.db')
    _db_stub.__path__ = _DB_PATH
    _db_stub.CodeGraphDB = None  # 满足 `from callwarden.db import CodeGraphDB` 语法
    sys.modules['callwarden.db'] = _db_stub

# FrozenImporter 需要父包节点先存在；仅收集 daemon_commands 子模块时，
# 某些平台不会自动创建 callwarden.cli，显式注入轻量父包避免运行时缺包。
if 'callwarden.cli' not in sys.modules:
    _cli_stub = types.ModuleType('callwarden.cli')
    _cli_stub.__path__ = _PKG_PATH
    sys.modules['callwarden.cli'] = _cli_stub


def main():
    """cw-client 主入口：直接调用 daemon_commands，绕过 cli.main。

    cw-client 是纯 RPC proxy，不含 parser 和本地 DB 写能力。
    平台门禁：非 Linux 直接退出（UDS + SCM_RIGHTS 是 Linux 特有）。
    """
    if sys.platform != 'linux':
        print(
            'ERROR: cw-client is only supported on Linux (UDS + SCM_RIGHTS).',
            file=sys.stderr,
        )
        sys.exit(2)

    argv = sys.argv[1:]
    if not argv:
        print('Call Warden Client Mode')
        print('  Connects to Enterprise Daemon via UDS')
        print('  No local parser or CAS write capability')
        print('  Subcommands: ping, register, list, status, publish, query,')
        print('               health, schema-version, backup, restore,')
        print('               gc-cas, gc-snapshots, mount, toolchain, mode')
        print("  Use 'cw-client --help' for details.")
        sys.exit(0)

    # 直接调用 daemon_commands.run_daemon_command（include_serve=False）
    # 等价于 cli/main.py 的 run_client_mode，但不经过 cli.main 顶层 import。
    from callwarden.cli.daemon_commands import run_daemon_command
    sys.exit(run_daemon_command(argv, include_serve=False))


if __name__ == '__main__':
    main()
