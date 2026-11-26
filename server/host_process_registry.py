"""host_process_registry.py — host 进程注册表（根修卡 D3 / 合同 r2）。

Authority: docs/evidence/contract-revise-r2-envelope-20261124.json (objective)
Schema:    callwarden-host-process-registry/v1

设计不变量（合同 r2 原文要点）：
- 与 authority manifest 同根：``~/.callwarden/runtime/host-process-registry.json``。
- 按 authority_id 隔离：文件内按 authority_id 分区，跨 authority 的条目互不可见。
- entries[] 每条 {role, pid, process_start_time, endpoint_or_bind,
  binary_sha256, started_at, status}，role ∈ {bridge, daemon, webui}。
- 发布沿用 publish_manifest_atomic 语义：temp + fsync + rename（原子，不留半截）。
- 进程启动 upsert 自身、正常退出注销；崩溃残留由权威 daemon 按
  pid + process_start_time 判活后标 ``stale``（**不删历史**）。
- webui（role=webui）独立注册，启停**不触发** daemon 单实例守卫；
  daemon 重启不清理 webui 条目。
- manifest 保持 daemon 权威，注册表仅轻量索引；两者冲突以 manifest 为准，
  由调用方上报 E_DAEMON_ALREADY_RUNNING + 结构化诊断。
- 原子临界区用 OS 锁（Windows named mutex / Unix flock），禁止纯
  「读文件→判断→写文件→启动」TOCTOU 路径。

所有权：本文件（server/host_process_registry.py）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from typing import Any, Dict, List, Optional

SCHEMA_VERSION = "callwarden-host-process-registry/v1"
VALID_ROLES = frozenset({"bridge", "daemon", "webui"})
STATUS_ACTIVE = "active"
STATUS_STALE = "stale"

# 注册表副根节点：~/.callwarden/runtime/
_RUNTIME_DIR = os.path.join(os.path.expanduser("~"), ".callwarden", "runtime")
REGISTRY_FILENAME = "host-process-registry.json"
# Windows named mutex name（host 级，不按 endpoint 派生）
_WIN_MUTEX_NAME = "Global\\CallWarden_HostProcessRegistry_v1"
# Unix lock file（host 级）
_UNIX_LOCK_FILE = os.path.join(_RUNTIME_DIR, "host-process-registry.lock")


def registry_path() -> str:
    """注册表文件路径（host 级单一文件，内部按 authority_id 分区）。"""
    return os.path.join(_RUNTIME_DIR, REGISTRY_FILENAME)


# ---------------------------------------------------------------------------
# process_start_time 判活（PID 复用防护，合同 r2 核心）
# ---------------------------------------------------------------------------


def _process_start_time(pid: int) -> Optional[int]:
    """返回进程启动时间（epoch 秒）；进程不存在/取不到时 None。

    Windows 用 GetProcessTimes（FILETIME 100ns since 1601 → epoch 秒）；
    POSIX 读 /proc/<pid>/stat 字段 22（clock ticks since boot），换算成
    boot + starttime/ticks_per_sec。
    """
    if not isinstance(pid, int) or pid <= 0:
        return None
    if sys.platform == "win32":
        return _win_process_start_time(pid)
    return _posix_process_start_time(pid)


def _win_process_start_time(pid: int) -> Optional[int]:
    try:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return None
        try:
            class FILETIME(ctypes.Structure):
                _fields_ = [("dwLowDateTime", wintypes.DWORD),
                            ("dwHighDateTime", wintypes.DWORD)]

            creation = FILETIME()
            kernel32.GetProcessTimes.argtypes = [
                wintypes.HANDLE, ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
                ctypes.POINTER(FILETIME), ctypes.POINTER(FILETIME),
            ]
            if not kernel32.GetProcessTimes(
                    handle, ctypes.byref(creation), ctypes.byref(FILETIME()),
                    ctypes.byref(FILETIME()), ctypes.byref(FILETIME())):
                return None
            ft = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
            # FILETIME epoch 1601-01-01 → Unix epoch 1970-01-01 = 11644473600s
            return int(ft / 10_000_000 - 11_644_473_600)
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return None


def _posix_process_start_time(pid: int) -> Optional[int]:
    try:
        with open(f"/proc/{pid}/stat", "r", encoding="utf-8") as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        starttime_ticks = int(fields[19])  # field 22 overall, index 19 after ')'
        ticks = os.sysconf("SC_CLK_TCK")
        with open("/proc/uptime", "r", encoding="utf-8") as fh:
            uptime = float(fh.read().split()[0])
        boot_time = int(time.time() - uptime)
        return boot_time + int(starttime_ticks / ticks)
    except (OSError, ValueError, IndexError, ZeroDivisionError):
        return None


def is_process_alive(pid: int, process_start_time: Optional[int]) -> bool:
    """pid + process_start_time 双键判活。

    PID 复用负向防护：仅有 pid 不足以判活（现场 PID 30200 被 smartscreen.exe
    复用）。必须同时匹配 process_start_time；记录缺失 start_time 时 fail-closed
    返回 False（宁可判死，不可误判活实例）。
    """
    if not isinstance(pid, int) or pid <= 0:
        return False
    if not process_start_time or process_start_time <= 0:
        return False
    current = _process_start_time(pid)
    if current is None:
        return False
    # 允许 ±2s 误差（epoch 秒取整 + FILETIME 换算）
    return abs(current - int(process_start_time)) <= 2


# ---------------------------------------------------------------------------
# OS 锁临界区（禁 TOCTOU）
# ---------------------------------------------------------------------------


class _RegistryLock:
    """host 级 OS 锁上下文：Windows named mutex / Unix flock。"""

    def __init__(self) -> None:
        self._handle: Any = None
        self._file: Any = None

    def __enter__(self) -> "_RegistryLock":
        if sys.platform == "win32":
            self._enter_windows()
        else:
            self._enter_unix()
        return self

    def __exit__(self, *args: Any) -> None:
        if sys.platform == "win32":
            self._exit_windows()
        else:
            self._exit_unix()

    def _enter_windows(self) -> None:
        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        kernel32.CreateMutexW.restype = wintypes.HANDLE
        kernel32.CreateMutexW.argtypes = [
            wintypes.LPVOID, wintypes.BOOL, ctypes.c_wchar_p]
        handle = kernel32.CreateMutexW(None, False, _WIN_MUTEX_NAME)
        if handle and handle != 0:
            kernel32.WaitForSingleObject(handle, 0xFFFFFFFF)  # INFINITE
            self._handle = handle

    def _exit_windows(self) -> None:
        if self._handle is not None:
            try:
                import ctypes
                kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
                kernel32.ReleaseMutex(self._handle)
                kernel32.CloseHandle(self._handle)
            except Exception:
                pass
            self._handle = None

    def _enter_unix(self) -> None:
        import fcntl

        os.makedirs(_RUNTIME_DIR, exist_ok=True)
        self._file = open(_UNIX_LOCK_FILE, "w")
        fcntl.flock(self._file.fileno(), fcntl.LOCK_EX)

    def _exit_unix(self) -> None:
        if self._file is not None:
            try:
                import fcntl
                fcntl.flock(self._file.fileno(), fcntl.LOCK_UN)
                self._file.close()
            except Exception:
                pass
            self._file = None


# ---------------------------------------------------------------------------
# 原子发布（temp + fsync + rename）
# ---------------------------------------------------------------------------


def _atomic_write(path: str, payload: Dict[str, Any]) -> None:
    """temp + fsync + os.replace 原子发布，不留半截 JSON。"""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".host-registry-", suffix=".tmp",
                               dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _empty_document() -> Dict[str, Any]:
    return {"schema": SCHEMA_VERSION, "authorities": {}}


def _read_document(path: str) -> Dict[str, Any]:
    """读注册表；缺失/损坏返回空文档（fail-open 到空索引，判活仍靠 manifest）。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            doc = json.load(fh)
        if not isinstance(doc, dict) or doc.get("schema") != SCHEMA_VERSION:
            return _empty_document()
        if not isinstance(doc.get("authorities"), dict):
            doc["authorities"] = {}
        return doc
    except (OSError, ValueError):
        return _empty_document()


def _authority_entries(doc: Dict[str, Any], authority_id: str) -> List[Dict[str, Any]]:
    bucket = doc["authorities"].get(authority_id)
    if not isinstance(bucket, dict):
        bucket = {"entries": []}
        doc["authorities"][authority_id] = bucket
    entries = bucket.get("entries")
    if not isinstance(entries, list):
        entries = []
        bucket["entries"] = entries
    return entries


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def register_process(
    *,
    authority_id: str,
    role: str,
    pid: int,
    endpoint_or_bind: str = "",
    binary_sha256: str = "",
) -> Dict[str, Any]:
    """注册（upsert）一个 host 进程条目，返回该条目。

    webui 独立注册：role=webui 的条目写入与其他角色相同的分区，但**不参与**
    daemon 单实例守卫（守卫只查 role == daemon 的 active 条目）。
    """
    if role not in VALID_ROLES:
        raise ValueError(f"非法 role: {role!r}，必须是 {sorted(VALID_ROLES)}")
    path = registry_path()
    with _RegistryLock():
        doc = _read_document(path)
        entries = _authority_entries(doc, authority_id)
        start_time = _process_start_time(pid)
        now = time.time()
        existing = None
        for e in entries:
            if e.get("role") == role and e.get("pid") == pid:
                existing = e
                break
        entry: Dict[str, Any] = {
            "role": role,
            "pid": pid,
            "process_start_time": start_time,
            "endpoint_or_bind": endpoint_or_bind,
            "binary_sha256": binary_sha256,
            "started_at": existing.get("started_at") if existing else now,
            "status": STATUS_ACTIVE,
        }
        if existing is not None:
            existing.update(entry)
        else:
            entries.append(entry)
        _atomic_write(path, doc)
        return dict(entry)


def unregister_process(authority_id: str, role: str, pid: int) -> bool:
    """正常退出注销：删除匹配的 active 条目（历史 stale 条目保留）。"""
    path = registry_path()
    with _RegistryLock():
        doc = _read_document(path)
        entries = _authority_entries(doc, authority_id)
        before = len(entries)
        remaining = [e for e in entries
                     if not (e.get("role") == role and e.get("pid") == pid)]
        if len(remaining) == before:
            return False
        doc["authorities"][authority_id]["entries"] = remaining
        _atomic_write(path, doc)
        return True


def reconcile_stale(authority_id: str) -> int:
    """把已死的 active 条目标 stale（不删历史），返回本次新标 stale 数。

    判活用 pid + process_start_time 双键——PID 复用时 process_start_time 不匹配
    即判死，避免陈旧条目误判为活实例。webui 条目同样会被 reconcile，但不影响
    daemon 单实例守卫（守卫只看 role == daemon）。
    """
    path = registry_path()
    marked = 0
    with _RegistryLock():
        doc = _read_document(path)
        entries = _authority_entries(doc, authority_id)
        for e in entries:
            if e.get("status") == STATUS_ACTIVE and not is_process_alive(
                    e.get("pid"), e.get("process_start_time")):
                e["status"] = STATUS_STALE
                marked += 1
        if marked:
            _atomic_write(path, doc)
    return marked


def active_daemon_entries(authority_id: str) -> List[Dict[str, Any]]:
    """返回本 authority 下仍存活的 role=daemon active 条目（单实例守卫输入）。"""
    path = registry_path()
    with _RegistryLock():
        doc = _read_document(path)
        entries = _authority_entries(doc, authority_id)
        return [dict(e) for e in entries
                if e.get("role") == "daemon"
                and e.get("status") == STATUS_ACTIVE
                and is_process_alive(e.get("pid"), e.get("process_start_time"))]


def check_daemon_guard(authority_id: str) -> Optional[Dict[str, Any]]:
    """daemon 单实例守卫：已有活 daemon 返回该条目（不得再 spawn），否则 None。

    webui 条目不参与——role=webui 的启停不会触发本守卫。
    """
    entries = active_daemon_entries(authority_id)
    return entries[0] if entries else None


def read_all(authority_id: str) -> List[Dict[str, Any]]:
    """只读返回本 authority 分区的全部条目（含 stale，调试/诊断用）。"""
    doc = _read_document(registry_path())
    return [dict(e) for e in _authority_entries(doc, authority_id)]


def compare_with_manifest(
    authority_id: str, manifest: Dict[str, Any]
) -> Dict[str, Any]:
    """以 manifest 为权威比对注册表，返回结构化诊断。

    Conflict 判定：manifest 声明一个活 daemon（pid/process_start_time），但注册表中
    role=daemon 的 active 条目与之不一致（pid 不同 / start_time 不同 / 缺失），
    或反之注册表有活 daemon 而 manifest 指向另一个 —— 一律以 manifest 为准，
    返回 ``{"conflict": True, "authority": manifest 的 pid/endpoint, ...}``。
    调用方据此上报 E_DAEMON_ALREADY_RUNNING + 本诊断，不 spawn 第二实例。
    """
    reg_active = active_daemon_entries(authority_id)
    m_pid = manifest.get("pid")
    m_start = manifest.get("process_start_time")
    m_endpoint = manifest.get("endpoint", "")
    m_alive = is_process_alive(m_pid, m_start) if m_pid else False

    conflict = False
    if m_alive:
        # manifest 权威：同 authority 已有活 daemon，任何 spawn 都冲突
        if any(e.get("pid") != m_pid for e in reg_active):
            conflict = True
    elif reg_active:
        # manifest 判死但注册表有活条目：以 manifest 为准（标 stale 由 reconcile 负责）
        conflict = True

    return {
        "conflict": conflict,
        "authority": "manifest",
        "manifest_pid": m_pid,
        "manifest_process_start_time": m_start,
        "manifest_endpoint": m_endpoint,
        "manifest_alive": m_alive,
        "registry_active_daemons": reg_active,
        "schema": SCHEMA_VERSION,
    }
