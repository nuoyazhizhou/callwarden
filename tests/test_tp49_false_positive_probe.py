"""TP-49 假阳性卡反证用例（关屿判定书定稿口径）。

隔离名未 arm 实例（无任何前置触碰）下：
- 变体 1：``is_daemon_available()`` 必须为 False——未 arm 占位者可被
  CreateFileW 连上（假阳性），但无法应答协议 ping；现实现（连接成功即
  True）在本用例下为红，修复后绿。
- 变体 2：先 exists() 再连接 → 连接失败——锁死「探测消耗」行为作为
  回归记录（属性查询打开消耗 armed/un-armed 实例，与返回值无关；
  Python ``os.path.exists`` 对存在的管道甚至返回 False 假阴性）。

边界：全程 ``callwarden-isotest-*`` 隔离管道名，零生产接触（不碰
``callwarden-<sid>`` 生产命名、不启 daemon）。仅 Windows 可跑。
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


def test_tp49_variant1_unarmed_pipe_not_available(monkeypatch):
    """变体 1（闭环断言定稿）：未 arm 实例（无前置触碰）→ False。"""
    pipe_name = _isotest_pipe_name("v1")
    handle = _create_unarmed_pipe(pipe_name)
    try:
        monkeypatch.setattr(
            cw_config, "get_default_daemon_endpoint", lambda: pipe_name
        )
        # 修复前：连接成功即 True（假阳性，本断言红）
        # 修复后：协议探针无应答 → False
        assert cw_config.is_daemon_available() is False
    finally:
        _close_handle(handle)


def test_tp49_variant2_exists_touch_destroys_instance(monkeypatch):
    """变体 2（回归记录）：先 exists() 触碰 → 实例被消耗 → 连接失败。

    同时记录 Python ``os.path.exists`` 的假阴性方向（对存在的管道返回
    False）——消耗与返回值无关。
    """
    pipe_name = _isotest_pipe_name("v2")
    handle = _create_unarmed_pipe(pipe_name)
    try:
        # 对照组：未触碰时连接成功（证明后续失败由 exists() 消耗导致）
        from callwarden.server.daemon_autostart import try_connect

        conn = try_connect(pipe_name)
        assert conn is not None, "未触碰的未 arm 实例应可连接（批次 1 场景 C）"
        conn.close()

        # 重建实例后做 exists() 触碰（Python 假阴性：返回 False 但消耗）
        _close_handle(handle)
        handle = _create_unarmed_pipe(pipe_name)
        assert os.path.exists(pipe_name) is False, (
            "Python os.path.exists 对存在的管道返回 False（假阴性方向，回归记录）"
        )

        # 触碰后：连接失败（实例已被属性查询打开消耗）
        conn_after = try_connect(pipe_name)
        assert conn_after is None, "exists() 触碰后实例应已消耗（探测即破坏）"
    finally:
        _close_handle(handle)
