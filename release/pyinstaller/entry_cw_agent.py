#!/usr/bin/env python3
"""PyInstaller 入口包装：cw-agent 命令（无 parser 轻量包）。

绕过 cli.main（其顶层 ``from ..db import CodeGraphDB`` 会拉入
db.py → db_build.py → parsers → tree_sitter 链），直接调用
``callwarden.server.agent_watcher.run_agent_watcher_loop`` 等 server 模块。

通过 sys.modules stub 注入 ``callwarden`` 和 ``callwarden.db`` 包，
跳过：
- ``callwarden/__init__.py`` 的 ``from .db import CodeGraphDB``
- ``callwarden/db/__init__.py`` 的 ``from .db import CodeGraphDB``

stub 设置 ``__path__ = []``，PyInstaller FrozenImporter 通过
``sys.meta_path`` 接管子模块加载（从 PYZ 归档定位），不依赖文件系统路径。

子命令派发（start/stop/status）等价于 cli/main.py 的 run_agent_mode，
但全部通过 server.* 模块实现，不经过 cli.main 顶层 import。
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
if 'callwarden' not in sys.modules:
    _cw_stub = types.ModuleType('callwarden')
    _cw_stub.__path__ = _PKG_PATH
    _cw_stub.__version__ = '0.3.6'
    sys.modules['callwarden'] = _cw_stub

# === sys.modules stub：跳过 callwarden/db/__init__.py 的 CodeGraphDB import ===
if 'callwarden.db' not in sys.modules:
    _db_stub = types.ModuleType('callwarden.db')
    _db_stub.__path__ = _DB_PATH
    _db_stub.CodeGraphDB = None
    sys.modules['callwarden.db'] = _db_stub

# 与 cw-client 一致，不 stub ``callwarden.cli``。``cli/__init__.py`` 是空文件，
# PyInstaller FrozenInstaller 会从 PYZ 归档自动加载 ``callwarden.cli``
# （已在 _client_agent_hiddenimports 声明）。手动 stub 会覆盖 PyInstaller
# 真实包，导致子模块加载失败（ModuleNotFoundError）。详见 entry_cw_client.py
# 同名注释。


# === cw-agent 辅助函数（等价于 cli/main.py 的 _agent_* 系列）===

def _agent_pid_file():
    """agent PID 文件路径（per-UID）：~/.callwarden/agent.pid。"""
    return os.path.join(os.path.expanduser('~'), '.callwarden', 'agent.pid')


def _agent_log_file():
    """agent 日志文件路径：~/.callwarden/agent.log。"""
    return os.path.join(os.path.expanduser('~'), '.callwarden', 'agent.log')


def _print_agent_usage():
    """打印 cw-agent 用法。"""
    print('Call Warden Agent Mode (Linux only)')
    print('  Per-UID file watcher → UDS → Enterprise Daemon')
    print()
    print('Usage: cw-agent <command> [options]')
    print()
    print('Commands:')
    print('  start [--watch-dir DIR] [--workspace-id ID]')
    print('          启动 watcher（前台运行，systemd --user 调用）')
    print('  stop    停止运行中的 agent（读取 PID 文件，发送 SIGTERM）')
    print('  status  查询 agent 运行状态')


def _parse_agent_start_args(argv):
    """解析 `cw-agent start` 参数。"""
    opts = {
        'watch_dir': os.getcwd(),
        'workspace_id': None,
        'unknown': [],
        'help': False,
    }
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == '--watch-dir' and i + 1 < len(argv):
            opts['watch_dir'] = argv[i + 1]
            i += 2
        elif arg == '--workspace-id' and i + 1 < len(argv):
            opts['workspace_id'] = argv[i + 1]
            i += 2
        elif arg == '--help' or arg == '-h':
            opts['help'] = True
            i += 1
        else:
            opts['unknown'].append(arg)
            i += 1
    return opts


def _agent_start(argv):
    """cw-agent start 实现：启动 watcher 主循环。

    等价于 cli/main.py 的 _agent_start，但全部通过 server.* 模块实现。
    """
    import logging
    import signal
    import threading

    opts = _parse_agent_start_args(argv)
    if opts.get('help'):
        print('Usage: cw-agent start [--watch-dir DIR] [--workspace-id ID]')
        print()
        print('Options:')
        print('  --watch-dir DIR       监控目录（默认当前工作目录）')
        print('  --workspace-id ID     workspace_instance_id（默认从 watch-dir 推导）')
        return 0

    watch_dir = os.path.abspath(opts['watch_dir'])
    if not os.path.isdir(watch_dir):
        print(f'ERROR: watch-dir 不存在：{watch_dir}', file=sys.stderr)
        return 2

    # 配置日志（写入 ~/.callwarden/agent.log）
    log_file = _agent_log_file()
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    logging.basicConfig(
        filename=log_file,
        level=logging.INFO,
        format='%(asctime)s [%(levelname)s] %(name)s: %(message)s',
    )
    # 同时输出到 stderr（systemd 会重定向到 journal）
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(logging.Formatter('%(levelname)s: %(message)s'))
    logging.getLogger().addHandler(console)

    # 1. 加载或创建 AgentSession
    from callwarden.server.agent_session import AgentSession
    session = AgentSession.create_or_load()
    logging.info('agent session 加载：%s', session)

    # 2. 推导 workspace_instance_id
    workspace_id = opts['workspace_id']
    if not workspace_id:
        from callwarden.server.daemon_client import derive_workspace_instance_id
        workspace_id = derive_workspace_instance_id(watch_dir)
    logging.info('workspace_instance_id=%s', workspace_id)

    # 3. 写 PID 文件
    pid_file = _agent_pid_file()
    os.makedirs(os.path.dirname(pid_file), exist_ok=True)
    with open(pid_file, 'w') as f:
        f.write(str(os.getpid()))
    logging.info('PID 文件：%s', pid_file)

    # 4. 与 daemon 握手（user_agent_connect）
    from callwarden.server.daemon_client import UnixDaemonRpcClient
    from callwarden.server.agent_protocol import (
        user_agent_connect, user_agent_ping, AgentProtocolError,
    )
    from callwarden.config import DAEMON_SOCKET_PATH
    rpc_client = UnixDaemonRpcClient(socket_path=DAEMON_SOCKET_PATH)

    try:
        ping_resp = user_agent_ping(rpc_client)
        logging.info(
            'daemon ping OK：peer_uid=%s pid=%s',
            ping_resp.get('peer_uid'), ping_resp.get('pid'),
        )
    except AgentProtocolError as e:
        logging.error('daemon 不可达：%s', e)
        try:
            os.remove(pid_file)
        except OSError:
            pass
        return 2

    try:
        epoch = user_agent_connect(rpc_client, workspace_id, session)
        logging.info('session_epoch=%d', epoch)
    except AgentProtocolError as e:
        logging.error('握手失败：%s', e)
        try:
            os.remove(pid_file)
        except OSError:
            pass
        return 2

    # 5. 注册 workspace（如果尚未注册）
    try:
        rpc_client.call('workspace.register', {
            'client_view_root': watch_dir,
        })
    except Exception as e:
        logging.warning('workspace.register 失败（可能已注册）：%s', e)

    # 6. 加载支持的扩展名集合
    from callwarden.config import get_supported_extensions
    supported_exts = get_supported_extensions()
    logging.info('支持的扩展名：%d 个', len(supported_exts))

    # 7. 启动 watcher 主循环
    from callwarden.server.agent_watcher import (
        run_agent_watcher_loop, HAS_WATCHDOG,
    )
    if not HAS_WATCHDOG:
        logging.error('watchdog 未安装')
        try:
            os.remove(pid_file)
        except OSError:
            pass
        return 2

    stop_event = threading.Event()

    def _signal_handler(signum, frame):
        logging.info('收到信号 %d，准备退出', signum)
        stop_event.set()

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)

    try:
        return run_agent_watcher_loop(
            agent_session=session,
            daemon_rpc_client=rpc_client,
            workspace_instance_id=workspace_id,
            watch_dir=watch_dir,
            supported_exts=supported_exts,
            stop_event=stop_event,
        )
    finally:
        try:
            os.remove(pid_file)
        except OSError:
            pass
        logging.info('agent 退出')


def _agent_stop(argv):
    """cw-agent stop 实现：发送 SIGTERM 停止运行中的 agent。"""
    pid_file = _agent_pid_file()
    if not os.path.isfile(pid_file):
        print('agent 未运行（PID 文件不存在）')
        return 1
    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())
    except (ValueError, OSError) as e:
        print(f'ERROR: 读取 PID 文件失败：{e}', file=sys.stderr)
        return 1
    try:
        import signal
        os.kill(pid, signal.SIGTERM)
        print(f'已发送 SIGTERM 到 PID {pid}')
        # 等待 PID 文件被清理（最多 5 秒）
        import time
        for _ in range(50):
            if not os.path.isfile(pid_file):
                print('agent 已停止')
                return 0
            time.sleep(0.1)
        print('WARNING: agent 5 秒内未退出，可能需要 SIGKILL', file=sys.stderr)
        return 1
    except ProcessLookupError:
        print(f'PID {pid} 不存在，清理 PID 文件')
        try:
            os.remove(pid_file)
        except OSError:
            pass
        return 0
    except PermissionError as e:
        print(f'ERROR: 无权限发送信号：{e}', file=sys.stderr)
        return 1


def _agent_status(argv):
    """cw-agent status 实现：查询 agent 运行状态。"""
    import json
    pid_file = _agent_pid_file()
    if not os.path.isfile(pid_file):
        print(json.dumps({'running': False, 'pid': None, 'pid_file': pid_file}))
        return 1
    try:
        with open(pid_file, 'r') as f:
            pid = int(f.read().strip())
    except (ValueError, OSError) as e:
        print(json.dumps({'running': False, 'pid': None, 'error': str(e)}))
        return 1
    # 检查进程是否存活（kill 0 不发送信号，只检查权限/存在性）
    try:
        os.kill(pid, 0)
        print(json.dumps({'running': True, 'pid': pid, 'pid_file': pid_file}))
        return 0
    except ProcessLookupError:
        print(json.dumps({'running': False, 'pid': pid, 'stale_pid_file': True}))
        return 1
    except PermissionError as e:
        print(json.dumps({'running': True, 'pid': pid, 'permission_error': str(e)}))
        return 0


def main():
    """cw-agent 主入口：Linux per-UID watcher agent。

    平台门禁：非 Linux 直接退出（SO_PEERCRED、SCM_RIGHTS、UDS 是 Linux 特有）。
    """
    if sys.platform != 'linux':
        print(
            'ERROR: cw-agent is only supported on Linux.\n'
            'Enterprise agent requires SO_PEERCRED, SCM_RIGHTS, and UDS.\n'
            "On Windows/macOS, use 'cw' in local mode.",
            file=sys.stderr,
        )
        sys.exit(2)

    argv = sys.argv[1:]
    if not argv:
        _print_agent_usage()
        sys.exit(0)

    cmd = argv[0]
    rest = argv[1:]

    if cmd in {'-h', '--help'}:
        _print_agent_usage()
        sys.exit(0)
    if cmd == 'start':
        sys.exit(_agent_start(rest))
    if cmd == 'stop':
        sys.exit(_agent_stop(rest))
    if cmd == 'status':
        sys.exit(_agent_status(rest))
    print(f'ERROR: unknown command: {cmd}', file=sys.stderr)
    _print_agent_usage()
    sys.exit(2)


if __name__ == '__main__':
    main()
