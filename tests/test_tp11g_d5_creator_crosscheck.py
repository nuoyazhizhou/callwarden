"""TP-11G 2a 层 4：client 归属交叉校验（D5）回归用例。

不变量（§A7/A8 实测）：ping 响应的 daemon_pid == 管道创建者 PID
（GetNamedPipeServerProcessId 恒返回管道最初创建者，与句柄类型无关）。

- 诚实实例（自报自身 PID == 创建者）→ ``call("ping")`` 成功；
- 非创建者实例（自报 PID != 创建者）→ ``DaemonUnavailableError``（双 PID 诊断，
  fail-closed：Unavailable 优于错误归属）；
- creator 查询不可得（非管道 conn / 旧版本 daemon 无 pid 字段）→ 告警放行不阻断。

mock server：同步模式命名管道 + 生产同款帧（``!I`` 长度前缀 + JSON），
``reported_pid`` 可注入错误值模拟「非创建者实例应答」。
边界：全程 ``callwarden-isotest-*`` 隔离管道名，零生产接触（不启真 daemon）。
仅 win32 可跑。
"""

import ctypes
import json
import os
import struct
import sys
import threading
import time
import uuid
from ctypes import wintypes

import pytest

if sys.platform != "win32":
    # 模块级 SKIP（pytestmark skipif 的必要补充）：:36 起的 ctypes.windll
    # 是**模块级** Windows-only 初始化——pytestmark 只跳测试执行、不跳
    # collect 时的模块级代码，Linux collect 在 ctypes.windll 处
    # AttributeError 击穿 collect 门（CI-DEBT CD-1，CI run 38927663848
    # 实证）。docstring 已注明「仅 win32 可跑」，此处让 skip 语义覆盖
    # 模块加载期。pytestmark 保留作为 win32 内的二次防护。
    pytest.skip("Windows 命名管道归属校验，仅 win32", allow_module_level=True)

from callwarden.server import daemon_client as dc

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows 命名管道归属校验，仅 win32"
)

HEADER = struct.Struct("!I")
kernel32 = ctypes.windll.kernel32

# CreateNamedPipeW 常量（同步模式：mock 不需要 overlapped；NULL 安全属性
# 即默认 DACL，同用户连接足够——mock 非生产保真对象）
PIPE_ACCESS_DUPLEX = 0x3
PIPE_TYPE_MESSAGE = 0x4
PIPE_READMODE_MESSAGE = 0x8
PIPE_WAIT = 0x0
PIPE_UNLIMITED_INSTANCES = 0xFF
PIPE_BUFFER_SIZE = 65536
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
ERROR_PIPE_CONNECTED = 535

kernel32.CreateNamedPipeW.restype = ctypes.c_void_p
kernel32.CreateNamedPipeW.argtypes = [
    wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
]
kernel32.ConnectNamedPipe.argtypes = [ctypes.c_void_p, ctypes.c_void_p]


def _isotest_pipe_name(tag: str) -> str:
    return rf"\\.\pipe\callwarden-isotest-d5-{tag}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _create_pipe(name: str) -> int:
    """同步模式 mock 管道实例（NULL 安全属性：同用户连接足够）。"""
    h = kernel32.CreateNamedPipeW(
        name, PIPE_ACCESS_DUPLEX,
        PIPE_TYPE_MESSAGE | PIPE_READMODE_MESSAGE | PIPE_WAIT,
        PIPE_UNLIMITED_INSTANCES, PIPE_BUFFER_SIZE, PIPE_BUFFER_SIZE, 0, None,
    )
    if h in (None, INVALID_HANDLE_VALUE):
        raise OSError("CreateNamedPipeW failed: %d" % kernel32.GetLastError())
    return h


def _read_message(handle: int) -> bytes:
    """message-mode 一次 ReadFile 读整条消息（buffer 大于消息即可）。"""
    buf = ctypes.create_string_buffer(PIPE_BUFFER_SIZE)
    read = wintypes.DWORD(0)
    if not kernel32.ReadFile(handle, buf, PIPE_BUFFER_SIZE, ctypes.byref(read), None):
        raise OSError("ReadFile failed: %d" % kernel32.GetLastError())
    return buf.raw[: read.value]


def _write_message(handle: int, payload: bytes) -> None:
    frame = HEADER.pack(len(payload)) + payload
    written = wintypes.DWORD(0)
    if not kernel32.WriteFile(handle, frame, len(frame), ctypes.byref(written), None):
        raise OSError("WriteFile failed: %d" % kernel32.GetLastError())


class MockPipeServer:
    """最小命名管道 mock：应答一次 ping，``pid`` 字段报 ``reported_pid``。

    （真实 daemon 经 dispatch.rs:275 返回 state.pid；mock 可注入错误值
    模拟「非创建者实例应答」——共存场景下诚实第二实例自报自身 PID。）
    """

    def __init__(self, name: str, reported_pid: int):
        self.reported_pid = reported_pid
        self.handle = _create_pipe(name)
        self.ready = threading.Event()
        self.served = threading.Event()
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        self.ready.set()
        # ConnectNamedPipe 阻塞等 client；535 = ERROR_PIPE_CONNECTED（正常）
        if not kernel32.ConnectNamedPipe(self.handle, None):
            if kernel32.GetLastError() != ERROR_PIPE_CONNECTED:
                self.served.set()
                return
        frame = _read_message(self.handle)
        (size,) = HEADER.unpack(frame[:4])
        req = json.loads(frame[4:4 + size].decode("utf-8"))
        resp = {
            "id": req.get("id"),
            "ok": True,
            "result": {"status": "ok", "pid": self.reported_pid},
        }
        _write_message(self.handle, json.dumps(resp).encode("utf-8"))
        self.served.set()
        time.sleep(0.3)  # 给 client 侧读取留窗口
        kernel32.DisconnectNamedPipe(self.handle)
        kernel32.CloseHandle(self.handle)


def _make_client(name: str) -> "dc.UnixDaemonRpcClient":
    return dc.UnixDaemonRpcClient(socket_path=name, endpoint_override=True, timeout=5.0)


def test_honest_pid_passes() -> None:
    """诚实实例（自报自身 PID == 创建者）→ ping 成功且 pid 透传。"""
    name = _isotest_pipe_name("honest")
    MockPipeServer(name, reported_pid=os.getpid())
    client = _make_client(name)
    result = client.call("ping")
    assert result["pid"] == os.getpid()


def test_mismatch_pid_fails_closed() -> None:
    """非创建者实例（自报 PID != 创建者）→ DaemonUnavailableError（双 PID 诊断）。"""
    name = _isotest_pipe_name("mismatch")
    wrong_pid = os.getpid() + 7777
    MockPipeServer(name, reported_pid=wrong_pid)
    client = _make_client(name)
    with pytest.raises(dc.DaemonUnavailableError) as excinfo:
        client.call("ping")
    msg = str(excinfo.value)
    # 双 PID 诊断都在消息里
    assert "daemon_pid=%d" % wrong_pid in msg
    assert "pid=%d" % os.getpid() in msg
    assert "共存检出" in msg


def test_creator_query_unavailable_warn_and_allow() -> None:
    """creator 查询不可得（非管道 conn）→ 告警放行不阻断（unknown ≠ mismatch）。"""
    class FakeConn:  # 无 _handle：模拟非直连管道（bridge/TCP）或查询不可得
        pass

    resp = {"ok": True, "result": {"pid": os.getpid()}}
    # 不应抛——unknown 是「无法判断」，不是「已确认异常」
    dc._verify_pipe_creator_attribution(FakeConn(), resp)
    # 旧版本 daemon（响应无 pid 字段）同样放行
    dc._verify_pipe_creator_attribution(FakeConn(), {"ok": True, "result": {}})


def test_mismatch_direct_helper() -> None:
    """helper 直测：真实管道句柄（creator=本进程）+ 伪造 pid → 失配抛出。"""
    name = _isotest_pipe_name("direct")
    handle = _create_pipe(name)
    try:
        fake_conn = type("FakePipeConn", (), {"_handle": handle})()
        wrong = os.getpid() + 42
        with pytest.raises(dc.DaemonUnavailableError) as excinfo:
            dc._verify_pipe_creator_attribution(
                fake_conn, {"ok": True, "result": {"pid": wrong}}
            )
        assert str(wrong) in str(excinfo.value)
    finally:
        kernel32.CloseHandle(handle)


def test_is_daemon_available_endpoint_parameterized() -> None:
    """D3.3 参数化（config.is_daemon_available(endpoint)）：端点被消费。

    - armed isotest 实例 → True（协议探针通过）
    - un-armed isotest 实例 → False（批次 1：连接成功 != 可用）
    - 不存在的非管道路径 → False（与 rs windows_endpoint_available 一致）
    - None → 默认端点 smoke（仅验证可调用，不假设本机 daemon 状态）
    """
    from callwarden import config as cw_config

    # armed：mock server 已 ConnectNamedPipe 挂起监听并可应答协议 ping
    name_armed = _isotest_pipe_name("param-armed")
    MockPipeServer(name_armed, reported_pid=os.getpid())
    assert cw_config.is_daemon_available(name_armed) is True

    # un-armed：只建不监听（批次 1 场景 C：连接成功但协议探针失败 → fail-closed）
    name_unarmed = _isotest_pipe_name("param-unarmed")
    handle = _create_pipe(name_unarmed)
    try:
        assert cw_config.is_daemon_available(name_unarmed) is False
    finally:
        kernel32.CloseHandle(handle)

    # 不存在的非管道路径（与 rs windows_endpoint_available 的 false 分支一致）
    assert (
        cw_config.is_daemon_available("C:\\callwarden\\isotest-nonexistent.sock")
        is False
    )

    # None → 默认端点 smoke
    assert isinstance(cw_config.is_daemon_available(None), bool)
    assert isinstance(cw_config.is_daemon_available(), bool)
