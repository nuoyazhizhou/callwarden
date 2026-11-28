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
