"""C7 后续：明确前置缺失 → pytest.skip 的统一守卫。

只覆盖环境前置（daemon 二进制/进程不可用、native 构建缺库/缺链接器）；
真实 RPC/契约/断言失败与真实编译错误一律保持红。判定来源复用
`tests/_w3_harness.py` 的 `_prereq_unavailable_reason`，避免第二套语义漂移。
"""
from __future__ import annotations

import shutil
import time

import pytest

from _w3_harness import _prereq_unavailable_reason as prereq_unavailable_reason

# native 构建前置缺失标记：保守白名单，只认链接器/原生库缺失。
NATIVE_BUILD_PREREQ_MARKERS = (
    "LNK1181",
    "sqlite3.lib",
    "could not find native static library",
    "link.exe not found",
)

# 隔离 daemon 与生产 daemon 争用 SID 级命名管道的报文标记（实测
# 2026-10-31：manifest 发布后 daemon 立即 `CreateNamedPipe 失败 (error 5)` 退出，
# 后续 RPC 全部 E_HTTP_DAEMON_UNAVAILABLE / WinError 10061/10054）。
DAEMON_PIPE_CONFLICT_MARKERS = (
    "CreateNamedPipe",
    "error 5",
    "10061",
    "10054",
    "actively refused",
)


def require_cargo() -> str:
    """cargo 缺失 → skip；返回 cargo 路径。"""
    cargo = shutil.which("cargo")
    if cargo is None:
        pytest.skip("未找到 cargo，无法构建新鲜二进制")
    return cargo


def require_daemon_binary(bin_path):
    """隔离 daemon 二进制缺失 → skip；否则原样返回。"""
    if bin_path is None:
        pytest.skip("cw-daemon 二进制不可用（需先 cargo build --bin cw-daemon）")
    return bin_path


def skip_if_daemon_exited_before_manifest(
    proc,
    stderr_text: str = "",
    context: str = "隔离 daemon 未发布 manifest",
) -> None:
    """daemon 在 manifest 前退出 → skip；进程存活则返回（调用方保持 fail）。

    「manifest 超时但进程仍存活」不在此降级：可能是真实卡死，必须保持红。
    """
    rc = proc.poll()
    if rc is None:
        return
    tail = (stderr_text or "")[-2000:]
    pytest.skip(f"{context}（daemon 提前退出 returncode={rc}）\nstderr={tail}")


def native_build_skip_reason(build) -> str | None:
    """cargo build 失败且命中原生库/链接器缺失标记 → 原因；否则 None。"""
    if getattr(build, "returncode", 0) == 0:
        return None
    blob = (build.stdout or "") + "\n" + (build.stderr or "")
    for marker in NATIVE_BUILD_PREREQ_MARKERS:
        if marker in blob:
            return f"native 构建前置缺失（命中 {marker}）"
    return None


def read_proc_stderr(proc, limit: int = 4000) -> str:
    """仅在进程已退出时读取 stderr 尾部（活进程上读取会阻塞）。"""
    if proc is None or proc.stderr is None or proc.poll() is None:
        return ""
    try:
        data = proc.stderr.read() or b""
    except Exception:
        return ""
    if isinstance(data, str):
        return data[-limit:]
    return data.decode("utf-8", "replace")[-limit:]


def skip_if_isolated_daemon_pipe_conflict(
    proc,
    *,
    settle_seconds: float = 3.0,
    context: str = "隔离 daemon 与生产 daemon 争用 SID 级命名管道",
) -> None:
    """manifest 发布后 daemon 因管道冲突退出 → skip；存活或无冲突标记 → 返回。

    生产 daemon 已占用 `\\\\.\\pipe\\callwarden-<SID>` 时，隔离 daemon 会在发布
    HTTP manifest 之后立即以 `CreateNamedPipe 失败 (error 5)` 退出，后续 RPC
    全部报 E_HTTP_DAEMON_UNAVAILABLE —— 属明确环境前置冲突，不是代码回归。
    「进程存活」或「已退出但报文无冲突标记」一律返回，由调用方保持红。
    """
    if proc is None:
        return
    deadline = time.time() + settle_seconds
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)
    if proc.poll() is None:
        return
    stderr_text = read_proc_stderr(proc)
    for marker in DAEMON_PIPE_CONFLICT_MARKERS:
        if marker in stderr_text:
            pytest.skip(
                f"{context}（daemon 已退出 returncode={proc.poll()}，命中 {marker}）；"
                f"停止生产 daemon（cw daemon stop）后重跑\n{stderr_text[-1500:]}"
            )


def skip_if_native_build_prereq_unavailable(build, *, tail_chars: int = 3000) -> None:
    """命中 native 缺失标记 → skip；否则返回，由调用方 pytest.fail。"""
    reason = native_build_skip_reason(build)
    if reason is None:
        return
    blob = ((build.stdout or "") + (build.stderr or ""))[-tail_chars:]
    pytest.skip(f"{reason}\n{blob}")
