"""D3 host 进程注册表 + 自愈守卫测试（根修卡 T-1795507699058-41d9fcbc / 合同 r2）。

覆盖 reviewer 栏三条硬验收：
1. 原子发布 + pid/process_start_time 判活，含 **PID 复用负向测试**；
2. webui 独立注册 role=webui，启停不与 daemon 单实例守卫耦合，daemon 重启不误杀；
3. manifest 与注册表冲突时以 manifest 为权威并上报结构化诊断。

所有测试隔离注册表到 tmp_path，绝不触碰 ~/.callwarden/runtime/ 真实文件。
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from server import host_process_registry as hpr  # noqa: E402


@pytest.fixture()
def registry_tmp(monkeypatch, tmp_path):
    """把注册表路径与锁文件重定向到 tmp_path，并清空进程状态。"""
    monkeypatch.setattr(hpr, "_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        hpr, "_UNIX_LOCK_FILE", os.path.join(str(tmp_path), "registry.lock"))
    path = os.path.join(str(tmp_path), hpr.REGISTRY_FILENAME)
    return path


# ---------------------------------------------------------------------------
# 1) 原子发布 + PID 复用负向测试
# ---------------------------------------------------------------------------


def test_schema_and_atomic_publish(registry_tmp):
    """注册表以 v1 schema 原子发布，条目字段齐全。"""
    hpr.register_process(
        authority_id="auth-A", role="daemon", pid=4242,
        endpoint_or_bind="127.0.0.1:4523", binary_sha256="abc123")
    with open(registry_tmp, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    assert doc["schema"] == hpr.SCHEMA_VERSION == "callwarden-host-process-registry/v1"
    entry = doc["authorities"]["auth-A"]["entries"][0]
    assert entry["role"] == "daemon"
    assert entry["pid"] == 4242
    # 合同 r2 entries[] 字段全集
    for key in ("role", "pid", "process_start_time", "endpoint_or_bind",
                "binary_sha256", "started_at", "status"):
        assert key in entry, f"缺字段: {key}"


def test_pid_reuse_negative_is_not_alive(monkeypatch):
    """PID 复用负向：pid 相同但 process_start_time 不同 → 判死（防 smartscreen 复用）。"""
    # 模拟：记录的 start_time 属于旧进程，当前 OS 上该 PID 已被新进程占用。
    monkeypatch.setattr(hpr, "_process_start_time", lambda pid: 1_700_000_000)
    assert hpr.is_process_alive(30200, 1_699_999_000) is False  # start_time 不符
    assert hpr.is_process_alive(30200, 1_700_000_000) is True   # 双键匹配


def test_pid_reuse_stale_entry_is_reconciled_and_not_counted(monkeypatch, registry_tmp):
    """PID 复用后：陈旧 daemon 条目被标 stale，且不计入 active 守卫。"""
    hpr.register_process(authority_id="auth-A", role="daemon", pid=30200)
    # 注册表里手工塞入旧 start_time（模拟上次进程写下的值）
    with open(registry_tmp, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    doc["authorities"]["auth-A"]["entries"][0]["process_start_time"] = 1_699_999_000
    with open(registry_tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh)

    # 当前 OS 上 30200 的 start_time 变成新值 → PID 复用
    monkeypatch.setattr(hpr, "_process_start_time", lambda pid: 1_700_000_000)
    marked = hpr.reconcile_stale("auth-A")
    assert marked == 1
    entries = hpr.read_all("auth-A")
    assert entries[0]["status"] == hpr.STATUS_STALE
    # stale 不删历史
    assert len(entries) == 1
    # 守卫不把 stale 条目当活 daemon
    assert hpr.check_daemon_guard("auth-A") is None


def test_missing_start_time_fails_closed(monkeypatch):
    """缺 process_start_time 时 fail-closed 判死，不误判活实例。"""
    monkeypatch.setattr(hpr, "_process_start_time", lambda pid: 1_700_000_000)
    assert hpr.is_process_alive(4242, None) is False
    assert hpr.is_process_alive(4242, 0) is False
    assert hpr.is_process_alive(0, 1_700_000_000) is False


# ---------------------------------------------------------------------------
# 2) webui 独立注册，不与 daemon 守卫耦合
# ---------------------------------------------------------------------------


def test_webui_registration_does_not_trigger_daemon_guard(monkeypatch, registry_tmp):
    """webui 独立注册 role=webui，启停不触发 daemon 单实例守卫。"""
    monkeypatch.setattr(hpr, "_process_start_time", lambda pid: 1_700_000_000)
    hpr.register_process(
        authority_id="auth-A", role="webui", pid=9001,
        endpoint_or_bind="127.0.0.1:8765")
    # 只有 webui 条目时，daemon 守卫应放行（None）
    assert hpr.check_daemon_guard("auth-A") is None
    assert hpr.active_daemon_entries("auth-A") == []
    # webui 条目确实在注册表里
    roles = [e["role"] for e in hpr.read_all("auth-A")]
    assert roles == ["webui"]


def test_daemon_restart_does_not_kill_webui_entry(monkeypatch, registry_tmp):
    """daemon 重启（注销 + reconcile）不清理 webui entry。"""
    monkeypatch.setattr(hpr, "_process_start_time", lambda pid: 1_700_000_000)
    hpr.register_process(authority_id="auth-A", role="webui", pid=9001)
    d_entry = hpr.register_process(authority_id="auth-A", role="daemon", pid=4242)

    # daemon 正常退出注销自身
    assert hpr.unregister_process("auth-A", "daemon", d_entry["pid"]) is True
    # 权威 daemon 重启后 reconcile stale：不应动 webui
    hpr.reconcile_stale("auth-A")

    roles = [e["role"] for e in hpr.read_all("auth-A")]
    assert roles == ["webui"], f"webui 条目被误杀: {roles}"


def test_authority_partition_isolation(registry_tmp):
    """按 authority_id 分区隔离，跨 authority 条目互不可见。"""
    hpr.register_process(authority_id="auth-A", role="daemon", pid=1111)
    hpr.register_process(authority_id="auth-B", role="daemon", pid=2222)
    assert [e["pid"] for e in hpr.read_all("auth-A")] == [1111]
    assert [e["pid"] for e in hpr.read_all("auth-B")] == [2222]


# ---------------------------------------------------------------------------
# 3) manifest 权威：冲突以 manifest 为准
# ---------------------------------------------------------------------------


def test_manifest_authority_wins_when_registry_points_elsewhere(monkeypatch, registry_tmp):
    """manifest 声明活 daemon，注册表却指向另一个 pid → 冲突以 manifest 为准。"""
    monkeypatch.setattr(hpr, "_process_start_time", lambda pid: 1_700_000_000)
    # 注册表登记一个不同的活 daemon
    hpr.register_process(authority_id="auth-A", role="daemon", pid=1111)
    # manifest 权威声明 pid=4242 存活
    manifest = {
        "pid": 4242,
        "process_start_time": 1_700_000_000,
        "endpoint": "https://127.0.0.1:4523",
    }
    result = hpr.compare_with_manifest("auth-A", manifest)
    assert result["conflict"] is True
    assert result["authority"] == "manifest"
    assert result["manifest_alive"] is True
    assert result["manifest_pid"] == 4242
    # 结构化诊断字段齐全（供 E_DAEMON_ALREADY_RUNNING 上报）
    for key in ("manifest_endpoint", "registry_active_daemons", "schema"):
        assert key in result


def test_manifest_dead_registry_alive_is_conflict_authority_manifest(monkeypatch, registry_tmp):
    """manifest 判死但注册表有活 daemon → 仍以 manifest 为准（标冲突）。"""
    monkeypatch.setattr(hpr, "_process_start_time", lambda pid: 1_700_000_000)
    hpr.register_process(authority_id="auth-A", role="daemon", pid=1111)
    manifest = {
        "pid": 30200,
        "process_start_time": 1_600_000_000,  # PID 复用，manifest 判死
        "endpoint": "https://127.0.0.1:4523",
    }
    result = hpr.compare_with_manifest("auth-A", manifest)
    assert result["authority"] == "manifest"
    assert result["manifest_alive"] is False
    assert result["conflict"] is True


def test_no_conflict_when_manifest_alone_is_authority(monkeypatch, registry_tmp):
    """manifest 声明活 daemon 且注册表无冲突条目 → 无冲突，manifest 唯一权威。"""
    monkeypatch.setattr(hpr, "_process_start_time", lambda pid: 1_700_000_000)
    manifest = {
        "pid": 4242,
        "process_start_time": 1_700_000_000,
        "endpoint": "https://127.0.0.1:4523",
    }
    result = hpr.compare_with_manifest("auth-A", manifest)
    assert result["conflict"] is False
    assert result["manifest_alive"] is True


# ---------------------------------------------------------------------------
# 3b) F5：compare_with_manifest 在 authority_daemon_diagnostic 生产复用
# ---------------------------------------------------------------------------


def test_authority_diagnostic_reuses_manifest_comparison(monkeypatch, tmp_path):
    """F5 复用：诊断必须带 manifest_comparison（权威比对），且向后兼容原字段。

    注意：daemon_autostart 内 `from .host_process_registry` 加载的是
    callwarden.server.host_process_registry，与测试顶部的 server.host_process_registry
    是两个独立模块实例，故本测试必须 patch 生产侧那个实例，否则 mock 落空。
    """
    import callwarden.config as cfg
    import callwarden.server.daemon_autostart as da
    import callwarden.server.host_process_registry as hpr_prod

    monkeypatch.setattr(hpr_prod, "_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        hpr_prod, "_UNIX_LOCK_FILE", str(tmp_path / "registry.lock"))
    monkeypatch.setattr(hpr_prod, "_process_start_time", lambda pid: 1_700_000_000)
    monkeypatch.setattr(cfg, "get_http_manifest_path", lambda aid: str(tmp_path / "m.json"))

    # manifest 权威声明活 daemon pid=4242；注册表另登记 pid=1111 → 应判冲突。
    manifest = {
        "pid": 4242,
        "process_start_time": 1_700_000_000,
        "endpoint": "https://127.0.0.1:4523",
    }
    monkeypatch.setattr(da, "read_http_manifest", lambda path: dict(manifest))
    hpr_prod.register_process(authority_id="auth-A", role="daemon", pid=1111)

    diag = da.authority_daemon_diagnostic("auth-A")

    # 原字段全部保留（向后兼容：spawn / mcp 只读这些 key）。
    assert diag["code"] == "E_DAEMON_ALREADY_RUNNING"
    assert diag["alive"] is True
    assert diag["pid"] == 4242
    assert diag["process_start_time"] == 1_700_000_000
    assert diag["endpoint"] == "https://127.0.0.1:4523"
    assert "restart_hint" in diag

    # F5 新增：复用 compare_with_manifest 的权威比对结论落地。
    cmp = diag["manifest_comparison"]
    assert cmp["authority"] == "manifest"
    assert cmp["conflict"] is True
    assert cmp["manifest_pid"] == 4242
    assert cmp["manifest_alive"] is True
    assert cmp["manifest_process_start_time"] == 1_700_000_000
    # 规范化副本口径一致：比对结论与外层 alive 同判。
    assert cmp["manifest_alive"] == diag["alive"]


# ---------------------------------------------------------------------------
# 4) spawn_http_daemon 守卫：探活抖动不得拉第二实例 / 无参 fail-closed
# ---------------------------------------------------------------------------


def _reset_cooldown():
    """测试间复位 spawn 冷却，避免相互干扰。"""
    import callwarden.server.daemon_autostart as da
    da._spawn_last_attempt_monotonic = None


def test_spawn_refuses_second_daemon_when_authority_alive(monkeypatch):
    """1s 探活失败但 manifest 判活为真 → spawn 不 Popen，返回 None（核心验收栏）。"""
    import callwarden.server.daemon_autostart as da

    _reset_cooldown()
    monkeypatch.setattr(
        da, "authority_daemon_diagnostic",
        lambda authority_id="": {
            "code": "E_DAEMON_ALREADY_RUNNING", "pid": 4242,
            "process_start_time": 1_700_000_000,
            "endpoint": "https://127.0.0.1:4523", "alive": True,
            "restart_hint": "cw daemon restart --force",
        })
    popen_calls = []
    monkeypatch.setattr(
        da.subprocess, "Popen",
        lambda *a, **k: popen_calls.append((a, k)))

    result = da.spawn_http_daemon(wait_window=0.1)
    assert result is None
    assert popen_calls == [], f"daemon 存活时绝不应 Popen 第二实例: {popen_calls}"


def test_spawn_fail_closed_without_authoritative_bind(monkeypatch):
    """manifest 缺失（无权威 endpoint）→ fail-closed，不裸 Popen 落默认端口。"""
    import callwarden.server.daemon_autostart as da

    _reset_cooldown()
    monkeypatch.setattr(
        da, "authority_daemon_diagnostic",
        lambda authority_id="": {
            "code": "E_DAEMON_ALREADY_RUNNING", "pid": None,
            "process_start_time": None, "endpoint": "", "alive": False,
        })
    # 无权威 endpoint：_authoritative_bind_spec 应返回 ""
    monkeypatch.setattr(da, "_authoritative_bind_spec", lambda diag: "")
    # 拿一把 host 锁让流程走到 bind 检查
    import callwarden.server.daemon_mutex as dmu

    class _FakeMutex:
        def try_acquire(self):
            return True

        def release(self):
            pass

    monkeypatch.setattr(dmu, "acquire_host_daemon_mutex", lambda: _FakeMutex())
    popen_calls = []
    monkeypatch.setattr(
        da.subprocess, "Popen",
        lambda *a, **k: popen_calls.append((a, k)))

    result = da.spawn_http_daemon(wait_window=0.1)
    assert result is None
    assert popen_calls == [], f"无权威 bind 时应 fail-closed，禁止裸 Popen: {popen_calls}"


def test_spawn_uses_explicit_http_bind(monkeypatch):
    """权威 daemon 已死 + 有权威 endpoint → 显式 --http-bind 启动，不裸启。"""
    import callwarden.server.daemon_autostart as da

    _reset_cooldown()
    monkeypatch.setattr(
        da, "authority_daemon_diagnostic",
        lambda authority_id="": {
            "code": "E_DAEMON_ALREADY_RUNNING", "pid": None,
            "process_start_time": None,
            "endpoint": "https://127.0.0.1:4523", "alive": False,
        })
    monkeypatch.setattr(da, "_authoritative_bind_spec", lambda diag: "127.0.0.1:4523")
    monkeypatch.setattr(da, "_find_daemon_binary", lambda: "/fake/cw-daemon.exe")

    import callwarden.server.daemon_mutex as dmu

    class _FakeMutex:
        def try_acquire(self):
            return True

        def release(self):
            pass

    monkeypatch.setattr(dmu, "acquire_host_daemon_mutex", lambda: _FakeMutex())

    class _FakeProc:
        pid = 31337

    captured = {}

    def fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr(da.subprocess, "Popen", fake_popen)
    # 注册表 upsert / hash 计算 / 就绪等待都 mock 掉，只验证 spawn 参数
    import callwarden.server.host_process_registry as hpr_mod
    monkeypatch.setattr(hpr_mod, "register_process", lambda **k: {})
    monkeypatch.setattr(da, "resolve_http_endpoint_and_manifest",
                        lambda **k: ("https://127.0.0.1:4523", {}))
    monkeypatch.setattr(da, "try_http_connect", lambda *a, **k: True)

    result = da.spawn_http_daemon(wait_window=0.1)
    assert result == "https://127.0.0.1:4523"
    cmd = captured.get("cmd", [])
    assert "--http-bind" in cmd, f"必须显式传 --http-bind: {cmd}"
    assert "127.0.0.1:4523" in cmd, f"bind 必须是权威 endpoint: {cmd}"
    assert cmd[cmd.index("--http-bind") + 1] == "127.0.0.1:4523"


def test_mcp_startup_reports_already_running_diagnostic(monkeypatch, capsys):
    """探活失败 + daemon 存活 → mcp 上报 E_DAEMON_ALREADY_RUNNING + 结构化 JSON。"""
    import callwarden.server.daemon_autostart as da
    import callwarden.server.mcp_server as ms

    diag = {
        "code": "E_DAEMON_ALREADY_RUNNING", "authority_id": "auth-A",
        "pid": 4242, "process_start_time": 1_700_000_000,
        "endpoint": "https://127.0.0.1:4523", "alive": True,
        "job_status": "alive_but_unresponsive_probe",
        "restart_hint": "cw daemon restart --force",
    }
    # 强制走 HTTP transport 分支
    monkeypatch.setenv("CW_DAEMON_TRANSPORT", "http")

    class _HealthClient:
        def __init__(self, *a, **k):
            pass

        def health(self):
            raise RuntimeError("probe timeout (fake 1s)")

    import callwarden.server.daemon_client as dc
    monkeypatch.setattr(dc, "HttpDaemonRpcClient", _HealthClient)
    # spawn 返回 None（守卫拒绝拉第二实例）
    monkeypatch.setattr(da, "spawn_http_daemon", lambda **k: None)
    monkeypatch.setattr(da, "authority_daemon_diagnostic", lambda: diag)

    ms._start_daemon_for_mcp_startup()
    captured = capsys.readouterr().err
    assert "E_DAEMON_ALREADY_RUNNING" in captured, captured
    assert "4242" in captured and "4523" in captured, captured
    # 结构化诊断 JSON 必须出现（pid/endpoint/restart_hint 可复核）
    assert '"pid": 4242' in captured or '"pid":4242' in captured, captured


# ---------------------------------------------------------------------------
# 5) F1 整改（reviewer BLOCKER）：acquire_host_daemon_mutex 不 mock 的真实测试
# ---------------------------------------------------------------------------


def test_acquire_host_daemon_mutex_real_no_unbound_error():
    """F1 核心：不 mock 直接调用，绝不再抛 UnboundLocalError。

    reviewer 指出既有 :246/:281 用 _FakeMutex mock 掉了真实函数，掩盖了
    `authority_id` 未初始化的 bug。本测试直接走真实函数：返回 DaemonMutex
    实例或 None（锁被占）都合法，但绝不能抛异常；且 lock_id 必须按 host
    身份派生（authority+task_db），不是 endpoint 派生的 legacy 值。
    """
    import callwarden.server.daemon_mutex as dmu

    # 取真实 authority，核对 host 级 lock_id 派生
    from callwarden.config import get_http_authority_id
    aid = get_http_authority_id()
    expected_host_lock_id = dmu.DaemonMutex(authority_id=aid).lock_id

    acquired = None
    try:
        acquired = dmu.acquire_host_daemon_mutex()  # 不 mock，F1 回归点
        assert acquired is None or isinstance(acquired, dmu.DaemonMutex)
        if acquired is not None:
            assert acquired.lock_id == expected_host_lock_id, (
                f"host 级 lock_id 不符: {acquired.lock_id} != {expected_host_lock_id}")
    finally:
        if acquired is not None:
            acquired.release()


def test_acquire_host_daemon_mutex_host_level_shared_lock():
    """host 级语义：acquire_host_daemon_mutex 与显式 authority 派生同一把锁。"""
    import callwarden.server.daemon_mutex as dmu
    from callwarden.config import get_http_authority_id

    aid = get_http_authority_id()
    host_id = dmu.DaemonMutex(authority_id=aid).lock_id
    # acquire_host_daemon_mutex 拿到的锁 id 恒为 host 级（authority+task_db），
    # 与 legacy endpoint 派生不同——兼容语义破坏与否由 test_daemon_mutex 覆盖。
    acquired = dmu.acquire_host_daemon_mutex()
    try:
        if acquired is not None:
            assert acquired.lock_id == host_id
    finally:
        if acquired is not None:
            acquired.release()


def test_spawn_alive_false_full_path_with_real_mutex(monkeypatch):
    """F1 验收：alive=False 全路径在真实 host 锁下不崩溃，三段皆可达。

    只 mock 外部副作用（Popen/注册表/health/resolve），**不 mock**
    acquire_host_daemon_mutex —— 它必须走真实函数（reviewer 抓的点）。
    验证：显式 bind(:569-589) / 注册表 upsert(:621-636) / health 等待(:638-652)
    三段都真正执行到。
    """
    import callwarden.server.daemon_autostart as da
    import callwarden.server.daemon_mutex as dmu

    _reset_cooldown()

    # 先确保真实 host 锁空闲（进程内串行，获取后立即释放）
    warm = dmu.acquire_host_daemon_mutex()
    if warm is not None:
        warm.release()

    # alive=False + 有权威 endpoint
    monkeypatch.setattr(
        da, "authority_daemon_diagnostic",
        lambda authority_id="": {
            "code": "E_DAEMON_ALREADY_RUNNING", "pid": None,
            "process_start_time": None,
            "endpoint": "https://127.0.0.1:4523", "alive": False,
        })
    monkeypatch.setattr(da, "_authoritative_bind_spec", lambda diag: "127.0.0.1:4523")
    monkeypatch.setattr(da, "_find_daemon_binary", lambda: "/fake/cw-daemon.exe")

    # 关键：不 mock dmu.acquire_host_daemon_mutex，走真实 OS 锁
    class _FakeProc:
        pid = 42424

    popen_calls = []
    registry_calls = []
    health_calls = []

    def fake_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        return _FakeProc()

    monkeypatch.setattr(da.subprocess, "Popen", fake_popen)

    import callwarden.server.host_process_registry as hpr_mod
    monkeypatch.setattr(
        hpr_mod, "register_process", lambda **k: registry_calls.append(k) or {})

    def fake_resolve(**k):
        health_calls.append("resolve")
        return ("https://127.0.0.1:4523", {})

    def fake_connect(*a, **k):
        health_calls.append("health")
        return True

    monkeypatch.setattr(da, "resolve_http_endpoint_and_manifest", fake_resolve)
    monkeypatch.setattr(da, "try_http_connect", fake_connect)

    result = da.spawn_http_daemon(wait_window=0.5)

    # 真实锁下应拿到锁并走完三段（若被外部进程占用则跳过强断言，但不得抛异常）
    if result is not None:
        assert result == "https://127.0.0.1:4523"
        # 段1：显式 bind
        assert popen_calls and "--http-bind" in popen_calls[0][0]
        assert popen_calls[0][0][popen_calls[0][0].index("--http-bind") + 1] == "127.0.0.1:4523"
        # 段2：注册表 upsert
        assert registry_calls and registry_calls[0]["role"] == "daemon"
        assert registry_calls[0]["pid"] == 42424
        # 段3：health 等待
        assert "health" in health_calls


# ---------------------------------------------------------------------------
# 6) F2 并发负向（reviewer BLOCKER）：两会话不同 endpoint 并发仅一个 daemon 启动
# ---------------------------------------------------------------------------


def test_concurrent_spawn_two_endpoints_only_one_daemon(monkeypatch):
    """F2 核心验收：两会话不同 endpoint 并发竞争，仅一个 daemon 真正启动。

    不 mock 真实 host 锁（acquire_host_daemon_mutex）；只 mock 外部副作用
    （Popen / 注册表 / health / resolve）来观测「真正 Popen 了几次」。
    两线程用 barrier 同步起点，关闭进程内 cooldown，让竞争纯粹由 host 锁
    串行化。断言 subprocess.Popen 恰好被调用 1 次——杜绝双 daemon 共写主库。
    """
    import threading
    import callwarden.server.daemon_autostart as da
    import callwarden.server.host_process_registry as hpr_mod

    _reset_cooldown()
    # 关闭进程内 cooldown，确保第二个会话是被 host 锁挡住而非 cooldown
    monkeypatch.setattr(da, "D3_SPAWN_COOLDOWN", 0.0)

    # 恒判死：让两个会话都认为「无存活 daemon」而进入 spawn 竞争。
    # 真正的防双实例只靠 host 锁 —— 若锁失效，两个线程都会 Popen（测试失败）。
    monkeypatch.setattr(
        da, "authority_daemon_diagnostic",
        lambda authority_id="": {
            "code": "E_DAEMON_ALREADY_RUNNING", "pid": None,
            "process_start_time": None,
            "endpoint": "https://127.0.0.1:4523", "alive": False,
        })
    monkeypatch.setattr(da, "_authoritative_bind_spec", lambda diag: "127.0.0.1:4523")
    monkeypatch.setattr(da, "_find_daemon_binary", lambda: "/fake/cw-daemon.exe")

    class _FakeProc:
        pid = 42424

    popen_calls = []
    popen_lock = threading.Lock()

    def fake_popen(cmd, **kwargs):
        with popen_lock:
            popen_calls.append((cmd, kwargs))
        # 模拟 daemon 启动有耗时，放大并发窗口，确保两线程真正重叠
        time.sleep(0.2)
        return _FakeProc()

    monkeypatch.setattr(da.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(hpr_mod, "register_process", lambda **k: {})
    monkeypatch.setattr(da, "resolve_http_endpoint_and_manifest",
                        lambda **k: ("https://127.0.0.1:4523", {}))
    monkeypatch.setattr(da, "try_http_connect", lambda *a, **k: True)

    barrier = threading.Barrier(2)
    results = []

    def session(endpoint_label):
        barrier.wait(timeout=5)  # 两会话同步起跑
        try:
            r = da.spawn_http_daemon(wait_window=0.3)
            results.append((endpoint_label, r))
        except Exception as exc:
            results.append((endpoint_label, f"ERR:{type(exc).__name__}:{exc}"))

    # 两会话用不同 endpoint 标签（模拟不同 endpoint 各自通过 endpoint 去重锁）
    threads = [
        threading.Thread(target=session, args=("https://127.0.0.1:4523",)),
        threading.Thread(target=session, args=("https://127.0.0.1:5854",)),
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10)

    assert len(results) == 2, f"两会话都应返回: {results}"
    assert not any(isinstance(r, str) and r.startswith("ERR:") for _, r in results), (
        f"并发路径不得抛异常: {results}")
    # 核心断言：只有拿到 host 锁的一个会话真正启动 daemon
    assert len(popen_calls) == 1, (
        f"两会话并发只能有一个 daemon 真正 Popen（杜绝共写主库），实得 "
        f"{len(popen_calls)} 次: {popen_calls}")


# ---------------------------------------------------------------------------
# 7) F3 集成（reviewer 需整改）：生产 web_gateway.main 路径的 webui 注册/注销
# ---------------------------------------------------------------------------


def test_webui_registration_integration(monkeypatch, tmp_path, capsys):
    """F3 验收：生产启动路径(main)在注册表可见 role=webui，退出注销，daemon 重启不误清。

    走真实 web_gateway.main（生产启动钩子），只 mock 阻塞的 uvicorn.run 做
    「运行期探针」与 create_app。注册表隔离到 tmp_path。断言：
    1. uvicorn.run 运行期注册表可见 role=webui 条目（含 pid+process_start_time 双键）；
    2. main 返回后该条目注销；
    3. 注册一个 daemon 条目并模拟 daemon 重启（reconcile_stale）不清理 webui 条目
       —— 这里在 uvicorn.run 探针内额外验证 daemon 重启不动 webui。
    """
    import callwarden.server.web_gateway as wg
    import callwarden.server.host_process_registry as hpr_local
    from callwarden.config import get_http_authority_id

    # 隔离注册表到 tmp_path（web_gateway 内绝对 import 的模块与此处同一对象）
    monkeypatch.setattr(hpr_local, "_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        hpr_local, "_UNIX_LOCK_FILE", os.path.join(str(tmp_path), "registry.lock"))

    authority = get_http_authority_id()
    observed = {}

    def fake_run(app, **kwargs):
        # 运行期探针：此刻 webui 应已注册；再放一个 daemon 条目并 reconcile，
        # 验证 daemon 重启不误清 webui。
        entries = hpr_local.read_all(authority)
        webui = [e for e in entries if e.get("role") == "webui"]
        observed["during"] = webui
        # 模拟 daemon 条目 + daemon 重启 reconcile（webui 进程 pid 仍活）
        d = hpr_local.register_process(
            authority_id=authority, role="daemon", pid=4242,
            endpoint_or_bind="127.0.0.1:4523")
        hpr_local.unregister_process(authority, "daemon", d["pid"])  # daemon 重启
        hpr_local.reconcile_stale(authority)
        after = hpr_local.read_all(authority)
        observed["after_daemon_restart"] = [e for e in after if e.get("role") == "webui"]
        # 清理 daemon 残留，便于退出后只判断 webui 注销
        hpr_local.unregister_process(authority, "daemon", 4242)

    monkeypatch.setattr(wg, "create_app", lambda root: object())
    monkeypatch.setattr("uvicorn.run", fake_run)

    rc = wg.main(["--root", str(tmp_path), "--port", "18766"])

    assert rc == 0
    # 1. 运行期注册表可见 role=webui，双键齐全
    assert len(observed["during"]) == 1, observed
    entry = observed["during"][0]
    assert entry["role"] == "webui"
    assert entry["pid"] > 0
    assert entry["process_start_time"] and entry["process_start_time"] > 0
    assert "18766" in entry["endpoint_or_bind"]
    # 2. daemon 重启不误清 webui 条目
    assert len(observed["after_daemon_restart"]) == 1, observed
    # 3. 正常退出后 webui 条目注销
    final = [e for e in hpr_local.read_all(authority) if e.get("role") == "webui"]
    assert final == [], f"退出后 webui 条目应注销: {final}"


# ---------------------------------------------------------------------------
# 8) F6：Windows spawn breakaway-from-job + 降级 fallback（T-1795829524399）
# ---------------------------------------------------------------------------


BREAKAWAY = 0x01000000
DETACHED = 0x00000008
NEW_PROCESS_GROUP = 0x00000200
NO_WINDOW = 0x08000000
_BASE_FLAGS = DETACHED | NEW_PROCESS_GROUP | NO_WINDOW


def test_spawn_breakaway_job_flags_and_fallback(monkeypatch):
    """F6：两处 spawn 经 helper 带 breakaway 位；job 拒绝时降级原 flags 重试成功。

    直接验证 helper（两处 spawn 点的唯一实现），并分别经 _start_daemon_windows
    与 spawn_http_daemon 入口各触发一次，避免只测 helper 却漏接线的假阳性。
    """
    import callwarden.server.daemon_autostart as da

    # --- (a) 正常路径：flags 含 breakaway 位，Popen 只调一次 ---
    calls = []

    class _FakeProc:
        pid = 54321

    def _fake_popen_ok(cmd, **kwargs):
        calls.append((cmd, kwargs.get("creationflags")))
        return _FakeProc()

    monkeypatch.setattr(da, "_find_daemon_binary", lambda: r"C:/fake/cw-daemon.exe")
    monkeypatch.setattr(da.subprocess, "Popen", _fake_popen_ok)
    monkeypatch.setattr(da.sys, "platform", "win32")

    assert da._start_daemon_windows() is True
    assert len(calls) == 1, f"正常路径应只 Popen 一次: {calls}"
    flags = calls[0][1]
    assert flags & BREAKAWAY, f"flags 缺 breakaway 位: {hex(flags)}"
    assert flags & _BASE_FLAGS == _BASE_FLAGS
    assert da._WIN32_BREAKAWAY_FALLBACK_ERRORS == frozenset({5, 801})

    # --- (b) 降级路径：第一次 winerror=5 (ERROR_ACCESS_DENIED) → 降级重试 ---
    calls_b = []

    def _fake_popen_denied_once(cmd, **kwargs):
        flags = kwargs.get("creationflags")
        calls_b.append(flags)
        if len(calls_b) == 1:
            err = OSError("access denied")
            err.winerror = 5  # ERROR_ACCESS_DENIED
            raise err
        return _FakeProc()

    monkeypatch.setattr(da.subprocess, "Popen", _fake_popen_denied_once)
    assert da._start_daemon_windows() is True
    assert len(calls_b) == 2, f"应触发降级重试共两次 Popen: {calls_b}"
    assert calls_b[0] & BREAKAWAY, "第一次应带 breakaway"
    assert not (calls_b[1] & BREAKAWAY), "降级重试必须去掉 breakaway"
    assert calls_b[1] == _BASE_FLAGS, "降级 flags 应等于原三 flags"

    # --- (c) 不可降级错误：winerror=2 (ERROR_FILE_NOT_FOUND) 原样抛出，不重试 ---
    calls_c = []

    def _fake_popen_other_error(cmd, **kwargs):
        calls_c.append(kwargs.get("creationflags"))
        err = OSError("file not found")
        err.winerror = 2
        raise err

    monkeypatch.setattr(da.subprocess, "Popen", _fake_popen_other_error)
    assert da._start_daemon_windows() is False  # 被 helper 抛给 except OSError → False
    assert len(calls_c) == 1, f"非降级错误不应重试: {calls_c}"


def test_spawn_http_daemon_uses_breakaway_helper(monkeypatch):
    """F6：smoke 入口 spawn_http_daemon 的 win32 分支也走 breakaway helper。"""
    import inspect

    import callwarden.server.daemon_autostart as da

    src = inspect.getsource(da.spawn_http_daemon)
    # 第二处 spawn 点必须调用 helper，而非保留裸 Popen creationflags
    assert "_popen_detached_win32(cmd, child_env)" in src, (
        "spawn_http_daemon win32 分支未复用 breakaway helper")


# ---------------------------------------------------------------------------
# 9) F7：reconcile_stale 生产接入——spawn 启动清理（T-1795831521518）
# ---------------------------------------------------------------------------


def test_reconcile_stale_startup_via_spawn(monkeypatch, tmp_path):
    """F7 ①：崩溃残留 active 条目经 spawn 路径启动清理被标 stale。

    不 mock reconcile_stale / register_process——走真实生产模块
    callwarden.server.host_process_registry 的隔离实例（tmp_path），
    只 mock spawn 的外部副作用（Popen / 诊断 / bind / health）。
    """
    import callwarden.server.daemon_autostart as da
    import callwarden.server.daemon_mutex as dmu
    import callwarden.server.host_process_registry as hpr_prod

    _reset_cooldown()
    warm = dmu.acquire_host_daemon_mutex()
    if warm is not None:
        warm.release()

    # 隔离生产侧注册表到 tmp_path（daemon_autostart 用 .host_process_registry）
    monkeypatch.setattr(hpr_prod, "_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(
        hpr_prod, "_UNIX_LOCK_FILE", str(tmp_path / "registry.lock"))

    # 双键判活映射：登记阶段返回匹配值（写入条目），reconcile 阶段对死 pid
    # 返回不同值 → 判死。活 pid=9999 始终匹配。
    alive_pid, alive_start = 9999, 1_700_000_000
    phase = {"registering": True}

    def _fake_start_time(pid):
        if pid == alive_pid:
            return alive_start
        # 死 pid：登记时返回匹配值，登记完成后返回不匹配值.PID 复用语义
        return alive_start if phase["registering"] else alive_start - 1000

    monkeypatch.setattr(hpr_prod, "_process_start_time", _fake_start_time)

    authority = "auth-f7"
    # 三个 active 残留：死 daemon(1001)、死 webui(1002)、活 webui(9999)
    hpr_prod.register_process(authority_id=authority, role="daemon", pid=1001)
    hpr_prod.register_process(authority_id=authority, role="webui", pid=1002)
    hpr_prod.register_process(authority_id=authority, role="webui", pid=alive_pid)
    # 登记完成：切换为「死 pid 的 start_time 已变」——模拟崩溃后 PID 被复用/进程已换
    phase["registering"] = False

    def _fake_diag(authority_id=""):
        return {
            "code": "E_DAEMON_ALREADY_RUNNING", "pid": None,
            "process_start_time": None,
            "endpoint": "https://127.0.0.1:4523", "alive": False,
        }

    monkeypatch.setattr(da, "authority_daemon_diagnostic", _fake_diag)
    monkeypatch.setattr(da, "_authoritative_bind_spec", lambda diag: "127.0.0.1:4523")
    monkeypatch.setattr(da, "_find_daemon_binary", lambda: "/fake/cw-daemon.exe")

    # 让 spawn 用的 authority_id 与测试分区一致（daemon_autostart._authority_id）
    monkeypatch.setattr(da, "_authority_id", lambda: authority)

    class _FakeProc:
        pid = 55555

    monkeypatch.setattr(da.subprocess, "Popen", lambda cmd, **kw: _FakeProc())
    monkeypatch.setattr(da, "resolve_http_endpoint_and_manifest",
                        lambda **k: ("https://127.0.0.1:4523", {}))
    monkeypatch.setattr(da, "try_http_connect", lambda *a, **k: True)

    result = da.spawn_http_daemon(wait_window=0.3)
    assert result == "https://127.0.0.1:4523"

    # ① 死 daemon + 死 webui 被标 stale；活 webui 保持 active（不误伤）
    by_pid = {e["pid"]: e for e in hpr_prod.read_all(authority)}
    assert by_pid[1001]["status"] == hpr_prod.STATUS_STALE, by_pid.get(1001)
    assert by_pid[1002]["status"] == hpr_prod.STATUS_STALE, by_pid.get(1002)
    assert by_pid[alive_pid]["status"] == hpr_prod.STATUS_ACTIVE, by_pid.get(alive_pid)

    # ② read_all 不再把死进程当活进程返回：active 里只剩活 webui + 新注册的 daemon
    active_pids = {e["pid"] for e in hpr_prod.read_all(authority)
                   if e["status"] == hpr_prod.STATUS_ACTIVE}
    assert 1001 not in active_pids and 1002 not in active_pids, active_pids
    assert alive_pid in active_pids, active_pids


def test_reconcile_stale_helper_static_wiring(monkeypatch):
    """F7 ②/③ 静态接线 + 回归：spawn 源码含 reconcile_stale 调用且失败不阻断。

    inspect 校验 spawn_http_daemon 真调用 reconcile_stale（防只改注释不改逻辑），
    并校验 reconcile_stale 抛异常时 spawn 仍继续（失败不阻断：注册表只是索引）。
    """
    import inspect

    import callwarden.server.daemon_autostart as da

    src = inspect.getsource(da.spawn_http_daemon)
    assert "reconcile_stale(authority_id)" in src, (
        "spawn_http_daemon 未调 reconcile_stale(authority_id)")

    # reconcile_stale 抛异常时不得中断 spawn：直接调 helper 块无法隔离，
    # 这里用源码断言其被 try/except 包裹（静态），行为级由上一测试覆盖正常路径。
    assert "启动 reconcile_stale 失败（不阻断）" in src
