"""Phase 4 / D3 行为级验收（根修卡 T-1795507699058-41d9fcbc）：
单实例守卫——真实进程级验证（非纯 mock）。

背景：reviewer 总复评（evidence/T-1795507699058-41d9fcbc/
reviewer-verdict-rootfix.json uncovered_paths[1]）记录「D3（自愈守卫）
验收不在本次总复评证据包内」。既有 tests/test_host_process_registry.py
覆盖了守卫逻辑（含 F2 并发负向），但 Popen / diagnostic 多为 mock。
本文件补齐**真实进程级**证据：

被测机制（HEAD 源码锚点）：
- server/daemon_autostart.py `spawn_http_daemon`：manifest 权威判活
  （pid+process_start_time 双键）→ host 级互斥
  （`acquire_host_daemon_mutex`，Windows 命名互斥体
  Global\\CallWarden_Daemon_<sha256(authority|task_db)[:16]>，同机所有
  endpoint 共享一把）→ 显式权威 bind（无 bind 即 fail-closed）；
- rust_ext/src/daemon/single_instance.rs `acquire_default_instance_lock`：
  authority-scoped 跨进程文件锁（fs2 LockFileEx），daemon 启动最早段
  （HTTP 预绑定/schema/任务库打开之前）获取，AlreadyRunning → 打印
  E_DAEMON_ALREADY_RUNNING 并 return 1（exit code 1）。手动 `cw-daemon`
  无参启动同样经过此处（Python daemon_mutex 的超集）。

场景（四个测试）：
1. 并发双 spawn（两个**独立 Python 子进程** + 真实 OS host 锁 +
   真实 manifest/bind 解析路径，Popen 计数拦截）：恰有一次真实 Popen——
   host 锁唯一权威（F2 测试的真实进程版）；
2. 探活失败不产生第二实例：真实隔离 daemon 存活 + 真实 manifest 判活，
   spawn_http_daemon 返回 None 且 Popen 0 次（诊断判活路径为真实实现）；
3. 真实第二 daemon 进程被拒（无参启动 fail-closed 的进程级证据）：
   隔离 daemon 存活时直接无参启动第二个 daemon 二进制，第二进程在
   single-instance 锁处失败退出（exit != 0，stderr 含
   E_DAEMON_ALREADY_RUNNING），原 daemon 仍存活且 health 正常——
   同一台机只存在一个 daemon 实例；
4. 无参 spawn fail-closed（真实读取路径）：无 manifest、无
   CW_DAEMON_HTTP_ENDPOINT 时 spawn_http_daemon 返回 None 且 Popen 0 次
   （不 mock diagnostic / bind 解析）。

隔离合规：隔离 daemon 走 w3_live fixture（动态端口 + tmp data_root +
USERPROFILE 重定向）；场景 1/4 的 host 锁环境全部隔离到 tmp
（USERPROFILE 重定向派生 CALLWARDEN_DIR/DB_PATH/manifest 路径），
绝不触碰共享 daemon（PID 28728）及其权威库，不重启任何共享实例。
"""

import json
import os
import subprocess
import sys
import time

import pytest

from callwarden.server.daemon_client import HttpDaemonRpcClient

# 与 D1/D2 同款：显式覆盖 CW_DAEMON_BIN（find_daemon_binary 按 mtime
# 取最新候选，debug 构建含 Step1/Step2 修复）。
_WORKTREE_BIN = os.path.abspath(os.path.join(
    os.path.dirname(os.path.realpath(__file__)), os.pardir, "rust_ext",
    "target", "release", "cw-daemon.exe"))
os.environ["CW_DAEMON_BIN"] = _WORKTREE_BIN

_REPO_ROOT = os.path.abspath(os.path.join(
    os.path.dirname(os.path.realpath(__file__)), os.pardir))

# 场景 1 子进程脚本：真实 host 锁竞争，Popen 计数拦截（返回假进程）。
# 被测对象是「同机所有 spawn 汇聚的 host 锁唯一权威」，不是 Popen 之后
# 的 daemon 进程本身——Popen 计数是「真正走到启动」的观测点。
_SPAWN_WORKER_SRC = r'''
import json
import os
import sys
import time

repo, out_file, go_file, ready_file, hpr_dir = sys.argv[1:6]
sys.path.insert(0, repo)

from server import daemon_autostart as da
import server.host_process_registry as hpr

# 注册表隔离到父进程指定目录（不碰真实 ~/.callwarden/runtime）
hpr._RUNTIME_DIR = hpr_dir
hpr._UNIX_LOCK_FILE = os.path.join(hpr_dir, "registry.lock")

counter = {"popen": 0}


class _FakeProc:
    pid = 999001


def _fake_popen(cmd, **kwargs):
    counter["popen"] += 1
    return _FakeProc()


# daemon_autostart 的两条 Popen 路径（win32 helper / POSIX）都引用
# subprocess.Popen 模块属性，patch 模块属性即全局生效。
da.subprocess.Popen = _fake_popen
da._spawn_last_attempt_monotonic = None

with open(ready_file, "w") as fh:
    fh.write(str(os.getpid()))

deadline = time.time() + 30
while not os.path.exists(go_file):
    if time.time() > deadline:
        with open(out_file, "w") as fh:
            json.dump({"error": "go-file timeout"}, fh)
        sys.exit(2)
    time.sleep(0.05)

result = da.spawn_http_daemon(wait_window=5.0)
with open(out_file, "w") as fh:
    json.dump({"result": result, "popen": counter["popen"]}, fh)
'''


def _isolated_spawn_env(tmp_path, extra=None):
    """场景 1 子进程 env：USERPROFILE 重定向 → CALLWARDEN_DIR / DB_PATH /
    manifest 路径全部落到 tmp（两子进程相同 → host 锁相同）。"""
    userhome = os.path.join(str(tmp_path), "userhome")
    os.makedirs(os.path.join(userhome, ".callwarden"), exist_ok=True)
    env = os.environ.copy()
    env["USERPROFILE"] = userhome
    env["CW_DAEMON_HTTP_ENDPOINT"] = "https://127.0.0.1:45999"
    env["CW_DAEMON_BIN"] = _WORKTREE_BIN
    env.pop("CW_CLIENT_CERT", None)
    env.pop("CW_CLIENT_KEY", None)
    env.pop("CW_CA_CERT", None)
    env.pop("CW_PKI_DIR", None)
    if extra:
        env.update(extra)
    return env


def test_concurrent_spawn_two_real_processes_only_one_popen(tmp_path):
    """两个独立 Python 子进程并发 spawn：真实 host 锁下恰有一次 Popen。"""
    hpr_dir = os.path.join(str(tmp_path), "hpr")
    os.makedirs(hpr_dir, exist_ok=True)
    worker_src = os.path.join(str(tmp_path), "spawn_worker.py")
    with open(worker_src, "w", encoding="utf-8") as fh:
        fh.write(_SPAWN_WORKER_SRC)

    env = _isolated_spawn_env(tmp_path,
                              {"CW_TEST_HPR_DIR": hpr_dir})
    ready_files = []
    out_files = []
    procs = []
    for i in range(2):
        ready = os.path.join(str(tmp_path), f"ready_{i}")
        out = os.path.join(str(tmp_path), f"result_{i}.json")
        ready_files.append(ready)
        out_files.append(out)
        p = subprocess.Popen(
            [sys.executable, worker_src, _REPO_ROOT, out,
             os.path.join(str(tmp_path), "go"), ready, hpr_dir],
            env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        procs.append(p)

    go_file = os.path.join(str(tmp_path), "go")
    try:
        # 等两个子进程都完成 import（真实 host 锁竞争前的对齐点）
        deadline = time.time() + 60
        for ready in ready_files:
            while not os.path.exists(ready):
                assert time.time() < deadline, f"子进程未就绪: {ready}"
                time.sleep(0.1)
        # 同时发令：两子进程并发进入 spawn_http_daemon
        with open(go_file, "w") as fh:
            fh.write("go")

        results = []
        for p, out in zip(procs, out_files):
            stdout, stderr = p.communicate(timeout=60)
            assert p.returncode == 0, (
                f"子进程异常退出 rc={p.returncode}: "
                f"{stderr.decode('utf-8', 'replace')[-800:]}")
            with open(out, "r", encoding="utf-8") as fh:
                results.append(json.load(fh))
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
        if os.path.exists(go_file):
            pass

    assert len(results) == 2, f"应有两份子进程结果: {results}"
    assert not any("error" in r for r in results), f"子进程报错: {results}"
    total_popen = sum(r.get("popen", 0) for r in results)
    assert total_popen == 1, (
        f"两会话并发只能有一个真正走到 Popen（host 锁唯一权威），"
        f"实得 {total_popen}: {results}")
    # 两个会话都正常返回（None = 未就绪/被挡，均不得抛异常）
    assert all(r.get("result", "MISSING") is None for r in results), (
        f"spawn 返回异常: {results}")
    print(f"[D3 实测/并发双 spawn] 2 个真实子进程并发竞争 host 锁，"
          f"Popen 次数={total_popen}（期望恰为 1），results={results}")


def test_live_daemon_blocks_second_spawn_real_probe(w3_live, tmp_path,
                                                    monkeypatch):
    """探活失败不产生第二实例：真实隔离 daemon 存活 + 真实 manifest 判活。"""
    from callwarden import config as cfg
    from server import daemon_autostart as da
    import server.host_process_registry as hpr_prod

    # 隔离 daemon 的真实 manifest（w3_live data_root/userhome/.callwarden）
    man_dir = os.path.join(w3_live["data_root"], "userhome", ".callwarden")
    manifests = [f for f in os.listdir(man_dir)
                 if f.startswith("http-daemon.") and f.endswith(".manifest.json")]
    assert manifests, f"隔离 manifest 未找到: {os.listdir(man_dir)}"
    man_path = os.path.join(man_dir, manifests[0])
    with open(man_path, "r", encoding="utf-8") as fh:
        manifest = json.load(fh)
    assert manifest.get("pid") == w3_live["proc"].pid, (
        f"manifest pid 与隔离进程不符: {manifest.get('pid')} vs "
        f"{w3_live['proc'].pid}")

    # 真实判活路径：manifest 路径指向隔离 daemon（不 mock diagnostic）
    monkeypatch.setattr(cfg, "get_http_manifest_path", lambda aid="": man_path)
    # 注册表隔离（host_process_registry 生产模块实例）
    monkeypatch.setattr(hpr_prod, "_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(hpr_prod, "_UNIX_LOCK_FILE",
                        os.path.join(str(tmp_path), "registry.lock"))

    popen_calls = []

    def _no_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        raise AssertionError(f"daemon 存活时绝不应 Popen 第二实例: {cmd}")

    monkeypatch.setattr(da.subprocess, "Popen", _no_popen)

    result = da.spawn_http_daemon(wait_window=0.3)
    assert result is None, (
        f"权威 daemon 存活时 spawn 应拒绝（返回 None），实得: {result!r}")
    assert popen_calls == [], (
        f"daemon 存活时 Popen 被调用 {len(popen_calls)} 次: {popen_calls}")

    # 诊断必须真实反映存活 daemon（pid + endpoint 可复核）
    diag = da.authority_daemon_diagnostic()
    assert diag["alive"] is True, f"诊断未判活: {diag!r}"
    assert diag["pid"] == w3_live["proc"].pid
    print(f"[D3 实测/探活拒第二实例] 真实 manifest 判活 pid="
          f"{diag['pid']} endpoint={diag['endpoint']}，spawn 返回 None，"
          f"Popen=0 次")


def test_second_daemon_process_rejected_when_alive(w3_live):
    """真实第二 daemon 进程（无参启动）在 single-instance 锁处被拒退出。"""
    proc = w3_live["proc"]
    client = w3_live["client"]
    endpoint = w3_live["endpoint"]
    bin_path = w3_live.get("daemon_bin") or _find_bin()
    data_root = w3_live["data_root"]
    userhome = os.path.join(data_root, "userhome")

    # 与隔离 daemon 完全相同的存储环境（同 authority → 同一把
    # single-instance 锁：manifest_dir/daemon-instance.<authority>.lock）
    env = os.environ.copy()
    env["USERPROFILE"] = userhome
    env["CW_DAEMON_TASK_DB"] = os.path.join(data_root, "task.db")
    env["CW_DAEMON_REGISTRY_DB"] = os.path.join(data_root, "registry.db")
    env["CW_DAEMON_CODEGRAPH_DB_TEMPLATE"] = os.path.join(
        userhome, ".callwarden", "callwarden.db")
    env["CALLWARDEN_SKIP_AUTO_SETUP"] = "1"
    env["CALLWARDEN_LANG"] = "en_US"
    env.pop("CW_CLIENT_CERT", None)
    env.pop("CW_CLIENT_KEY", None)
    env.pop("CW_CA_CERT", None)
    env.pop("CW_PKI_DIR", None)

    # 无参启动第二个 daemon（无 --http-bind）：single-instance 锁在
    # HTTP 预绑定之前获取，Expected：AlreadyRunning → exit 1。
    t0 = time.monotonic()
    second = subprocess.Popen(
        [bin_path], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        stdout, stderr = second.communicate(timeout=30)
    except subprocess.TimeoutExpired:
        second.kill()
        stdout, stderr = second.communicate()
        raise AssertionError(
            "第二实例未在 30s 内被拒退出（single-instance 守卫失效？）")
    elapsed = time.monotonic() - t0
    err_text = stderr.decode("utf-8", "replace")

    assert second.returncode != 0, (
        f"第二实例应以非零退出码被拒，实得 rc=0（产生了第二实例！）"
        f" stdout={stdout.decode('utf-8', 'replace')[-500:]}")
    assert "E_DAEMON_ALREADY_RUNNING" in err_text, (
        f"stderr 缺 E_DAEMON_ALREADY_RUNNING: {err_text[-800:]}")
    # 原 daemon 不受影响：进程存活 + health 可用
    assert proc.poll() is None, "原 daemon 进程意外退出"
    client.verify_health(endpoint=endpoint)
    print(f"[D3 实测/第二实例被拒] 无参第二 daemon 在 {elapsed:.2f}s 内 "
          f"rc={second.returncode} 被拒（E_DAEMON_ALREADY_RUNNING），"
          f"原 daemon pid={proc.pid} 仍存活且 health 正常")


def _find_bin():
    """定位 daemon 二进制（与 w3_harness.find_daemon_binary 同候选集）。"""
    import hashlib
    candidates = [
        os.path.join(_REPO_ROOT, "rust_ext", "target", "release", "cw-daemon.exe"),
        os.path.join(_REPO_ROOT, "rust_ext", "target", "debug", "cw-daemon.exe"),
        os.environ.get("CW_DAEMON_BIN", ""),
    ]
    best = None
    for c in candidates:
        if c and os.path.isfile(c):
            m = os.path.getmtime(c)
            if best is None or m > best[0]:
                best = (m, c)
    assert best is not None, "未找到 cw-daemon 二进制"
    digest = hashlib.sha256(open(best[1], "rb").read()).hexdigest().upper()
    print(f"[D3] daemon binary: {best[1]} sha256={digest}")
    return best[1]


def test_spawn_fail_closed_without_authoritative_bind_real_path(
        tmp_path, monkeypatch):
    """无参 spawn fail-closed（真实读取路径）：无 manifest / 无显式 endpoint。"""
    from callwarden import config as cfg
    from server import daemon_autostart as da
    import server.daemon_mutex as dmu
    import server.host_process_registry as hpr_prod

    # 干净 tmp CALLWARDEN_DIR：manifest 不存在（真实读取，不 mock diagnostic）
    cwdir = os.path.join(str(tmp_path), "cwdir")
    os.makedirs(cwdir, exist_ok=True)
    monkeypatch.setattr(cfg, "CALLWARDEN_DIR", cwdir)
    monkeypatch.setattr(
        cfg, "get_http_manifest_path",
        lambda aid="": os.path.join(cwdir, "http-daemon.none.manifest.json"))
    monkeypatch.delenv("CW_DAEMON_HTTP_ENDPOINT", raising=False)
    # 注册表隔离
    monkeypatch.setattr(hpr_prod, "_RUNTIME_DIR", str(tmp_path))
    monkeypatch.setattr(hpr_prod, "_UNIX_LOCK_FILE",
                        os.path.join(str(tmp_path), "registry.lock"))

    # 确保 host 锁空闲（真实锁；若被占则本测试降级为锁竞争路径，
    # 结果同为 None + Popen 0，但 fail-closed 分支未被覆盖 → 显式失败）
    warm = dmu.acquire_host_daemon_mutex()
    assert warm is not None, "host 锁被占用，无法覆盖 fail-closed 分支"
    warm.release()

    popen_calls = []

    def _no_popen(cmd, **kwargs):
        popen_calls.append((cmd, kwargs))
        raise AssertionError(f"无权威 bind 时绝不应 Popen: {cmd}")

    monkeypatch.setattr(da.subprocess, "Popen", _no_popen)

    result = da.spawn_http_daemon(wait_window=0.3)
    assert result is None, (
        f"无权威 bind 时应 fail-closed（返回 None），实得: {result!r}")
    assert popen_calls == [], (
        f"无权威 bind 时 Popen 被调用 {len(popen_calls)} 次: {popen_calls}")
    print("[D3 实测/无参 fail-closed] 无 manifest、无显式 endpoint，"
          "真实读取路径下 spawn 返回 None，Popen=0 次（未落默认端口）")
