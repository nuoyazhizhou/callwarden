"""TP-49 假阳性卡反证用例（关屿判定书定稿口径；ADR-002 单轨适配）。

隔离名未 arm 实例（无任何前置触碰）下：
- 变体 1：``is_daemon_available(pipe_name)`` 必须为 False——管道实例
  真实存在（CreateNamedPipeW 已创建），但单轨下非 http(s) 端点一律
  fail-closed；False 不依赖「端点不存在」，TP-49 假阳性方向（占位者
  可被 CreateFileW 连上）由形态门禁 + 协议探针关闭。
- 变体 2：管道端点在单轨下 ``try_connect`` 恒返回 None（传输已删除，
  触碰消耗机制无生产代码路径可达）——锁死「管道端点不可连接」作为
  回归记录，并保留 Python ``os.path.exists`` 对存在的管道返回 False
  的假阴性记录（属性查询消耗 armed/un-armed 实例，与返回值无关）。

边界：全程 ``callwarden-isotest-*`` 隔离管道名，零生产接触（不碰
``callwarden-<sid>`` 生产命名、不启 daemon、不探测默认端点）。仅
Windows 可跑。
"""

import ctypes
import os
import sys
import uuid

import pytest

import config as cw_config

pytestmark = pytest.mark.skipif(
    sys.platform != "win32", reason="Windows 命名管道取证，仅 win32"
)


def _isotest_pipe_name(tag: str) -> str:
    return rf"\\.\pipe\callwarden-isotest-{tag}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


def _create_unarmed_pipe(pipe_name: str) -> int:
    """以生产同款形态（owner-only 默认安全属性、PIPE_UNLIMITED_INSTANCES
    语义下的单实例）创建一个**未 arm**（从不 ConnectNamedPipe）的管道实例，
    返回服务端句柄。调用方负责 CloseHandle。"""
    kernel32 = ctypes.windll.kernel32
    PIPE_ACCESS_DUPLEX = 0x3
    FILE_FLAG_FIRST_PIPE_INSTANCE = 0x00080000
    PIPE_TYPE_BYTE = 0x4
    PIPE_WAIT = 0x2
    INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value

    class SECURITY_ATTRIBUTES(ctypes.Structure):
        _fields_ = [
            ("nLength", ctypes.c_uint),
            ("lpSecurityDescriptor", ctypes.c_void_p),
            ("bInheritHandle", ctypes.c_int),
        ]

    kernel32.CreateNamedPipeW.restype = ctypes.c_void_p
    kernel32.CreateNamedPipeW.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.c_uint,
        ctypes.POINTER(SECURITY_ATTRIBUTES),
    ]
    sa = SECURITY_ATTRIBUTES(ctypes.sizeof(SECURITY_ATTRIBUTES), None, 0)
    handle = kernel32.CreateNamedPipeW(
        pipe_name,
        PIPE_ACCESS_DUPLEX | FILE_FLAG_FIRST_PIPE_INSTANCE,
        PIPE_TYPE_BYTE | PIPE_WAIT,
        1,  # 单实例：本测试独占
        1024,
        1024,
        0,
        ctypes.byref(sa),
    )
    assert handle not in (None, INVALID_HANDLE_VALUE), (
        f"CreateNamedPipeW 失败: {pipe_name}"
    )
    return handle


def _close_handle(handle: int) -> None:
    ctypes.windll.kernel32.CloseHandle(ctypes.c_void_p(handle))


def test_tp49_variant1_unarmed_pipe_not_available():
    """变体 1（闭环断言定稿，单轨适配）：未 arm 实例（无前置触碰）→ False。

    单轨前：默认端点 monkeypatch 成管道，连接成功即 True（假阳性）；
    单轨后：显式传管道端点，非 http(s) 形态门禁 fail-closed → False。
    端点真实存在（未 arm 管道），证明 False 不靠「端点不存在」，
    且不接触生产默认端点（零生产接触边界保留）。
    """
    pipe_name = _isotest_pipe_name("v1")
    handle = _create_unarmed_pipe(pipe_name)
    try:
        # 单轨前（连接成功即 True，假阳性，本断言红）
        # 单轨后：非 http(s) 端点 fail-closed → False
        assert cw_config.is_daemon_available(pipe_name) is False
    finally:
        _close_handle(handle)


def test_tp49_variant2_pipe_endpoint_never_connectable():
    """变体 2（回归记录，单轨适配）：管道端点单轨下恒不可连接。

    单轨前：未触碰的未 arm 实例可连接（CreateFileW 假阳性方向），
    exists() 触碰后实例被消耗而连接失败（探测即破坏）；
    单轨后：``try_connect`` 仅处理 http(s) 端点，管道端点恒 None
    （触碰机制无生产代码路径可达）。保留 ``os.path.exists`` 假阴性
    记录（属性查询消耗实例，与返回值无关）。
    """
    pipe_name = _isotest_pipe_name("v2")
    handle = _create_unarmed_pipe(pipe_name)
    try:
        from callwarden.server.daemon_autostart import try_connect

        # 对照：未触碰 → None（单轨形态门禁，与实例状态无关）
        conn = try_connect(pipe_name)
        assert conn is None, "单轨下管道端点应不可连接（形态门禁）"

        # 重建实例后做 exists() 触碰（Python 假阴性：返回 False 但消耗）
        _close_handle(handle)
        handle = _create_unarmed_pipe(pipe_name)
        assert os.path.exists(pipe_name) is False, (
            "Python os.path.exists 对存在的管道返回 False（假阴性方向，回归记录）"
        )

        # 触碰后同样 None（恒定性；非「触碰消耗」归因）
        conn_after = try_connect(pipe_name)
        assert conn_after is None, "单轨下管道端点恒不可连接（与触碰无关）"
    finally:
        _close_handle(handle)
