"""TP-07 最小安全进程/传输底座 —— 验收套件。

selector: tests/acceptance/harness/bootstrap/test_bootstrap.py
运行（Windows 冻结解释器）：
  C:/Python314/python.exe -m pytest tests/acceptance/harness/bootstrap/test_bootstrap.py \
      -p no:cacheprovider --junitxml=<run 根>/reports/junit.xml

正向：隔离启动 → source/hash/PID/manifest/executable/endpoint 一致 → 日志排空 → HTTP 服务。
负向：非 loopback 绑定被拒、daemon 早死被检出、陈旧 manifest 不被误纳、强杀 cleanup、
      生产目录不被写入（禁止 production 发现）。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from daemon_bootstrap import (
    BootstrapError,
    DEFAULT_RUN_ROOT,
    IsolatedDaemon,
    TP06_DAEMON_SHA256,
    production_manifest_dir,
    sha256_of,
    snapshot_dir,
)

DAEMON_EXE = Path(os.environ.get(
    "CW_TP07_DAEMON_EXE",
    r"C:/Users/wanpi/AppData/Local/Temp/cw-acceptance/TP-07/src/cw-daemon.exe"))
# CW_TP07_DAEMON_EXE 是否被显式设置：缺失时区分两种处置（与 TP-11G D2
# 同族）——未显式设置且默认候选缺失（Linux CI 全量 pytest job 不构建
# daemon 候选）→ 模块级 skip（带环境范围声明）；显式设置仍缺失 →
# 保持 FileNotFoundError 硬错（fail-loud，acceptance-gates 注入候选后
# 缺失仍是真缺陷）。
if not DAEMON_EXE.is_file() and "CW_TP07_DAEMON_EXE" not in os.environ:
    pytest.skip(
        f"daemon 候选缺失且未显式指定 CW_TP07_DAEMON_EXE"
        f"（{DAEMON_EXE}）：本机/Linux CI 无已验收候选，TP-07 底座用例"
        f"仅在有候选时真跑；acceptance-gates 或本地经 CW_TP07_DAEMON_EXE"
        f" 注入候选后运行",
        allow_module_level=True)


def _run_root_for(name: str) -> Path:
    return DEFAULT_RUN_ROOT / "runs" / name


@pytest.fixture
def daemon(request):
    """每个测试一个独立 run 子根 + 独立 daemon；teardown 保证进程退出。"""
    d = IsolatedDaemon(run_root=_run_root_for(request.node.name), daemon_exe=DAEMON_EXE)
    try:
        yield d
    finally:
        if d.proc is not None and d.proc.poll() is None:
            d.stop()
        try:
            d.dump_log()
        except OSError:
            pass


# ---------------- 正向 ----------------

def test_daemon_starts_and_manifest_consistent(daemon):
    manifest = daemon.start()
    result = daemon.verify_consistency()
    checks = result["checks"]
    assert all(checks.values()), f"failed checks: {[k for k, v in checks.items() if not v]}"
    assert manifest["manifest_version"] == "callwarden-http-manifest/v1"
    assert manifest["daemon_binary_sha256"] == TP06_DAEMON_SHA256
    assert manifest["pid"] == daemon.proc.pid
    assert manifest["endpoint"].startswith("http://127.0.0.1:")
    info = daemon.stop()
    assert info["stopped"]


def test_http_endpoint_serves(daemon):
    daemon.start()
    probe = daemon.probe_http()
    assert probe["reachable"], f"endpoint not serving: {probe}"
    assert isinstance(probe["status"], int)


def test_logs_drained(daemon):
    daemon.start()
    daemon.stop()
    text = "".join(daemon.log_lines)
    assert len(daemon.log_lines) > 0, "no log lines drained"
    # daemon 预绑定成功后会打这条 INFO（cw_daemon.rs 的 eprintln）
    assert "HTTP MVP pre-bound" in text or "pre-bound" in text, (
        f"startup banner missing; log tail: {text[-400:]!r}")


def test_daemon_writes_state_only_under_run_root(daemon):
    daemon.start()
    daemon.stop()
    # 关键状态文件必须都在 run 根内
    manifests = list(daemon.callwarden_dir.glob("http-daemon.*.manifest.json"))
    assert manifests, "no manifest written under run-root home"
    reg = daemon._resolved_registry()
    assert reg.is_file() or reg.parent.is_dir(), "registry db path not created under run root"


# ---------------- 负向 ----------------

def test_non_loopback_bind_rejected(daemon):
    """启动失败：非 loopback 绑定必须 fail-closed 退出，不建 listener、不发 manifest。"""
    daemon.http_bind = "10.0.0.1:0"
    with pytest.raises(BootstrapError) as exc:
        daemon.start(timeout=30)
    assert "exited early" in str(exc.value)
    assert daemon.proc.returncode != 0
    # 不能留下任何 manifest（bind 前 loopback 校验即拒绝）
    manifests = list(daemon.callwarden_dir.glob("http-daemon.*.manifest.json"))
    assert not manifests, f"non-loopback bind must not publish manifest: {manifests}"


def test_startup_timeout_is_fail_closed(daemon):
    """超时：就绪等待超时必须 fail-closed 抛错，不能把未就绪报成成功。"""
    with pytest.raises(BootstrapError) as exc:
        daemon.start(timeout=0.1)
    assert "manifest not ready within" in str(exc.value)
    info = daemon.stop()
    assert info["stopped"]


def test_stale_manifest_not_adopted(daemon):
    """陈旧 manifest（已死 PID）不能被新 daemon 误纳为自己的就绪证据。"""
    first = daemon.start()
    first_pid = first["pid"]
    daemon.stop()
    # 停止后 manifest 仍在磁盘，PID 已死
    stale = list(daemon.callwarden_dir.glob("http-daemon.*.manifest.json"))
    assert stale, "stopped daemon should leave its manifest on disk"
    stale_data = json.loads(stale[0].read_text(encoding="utf-8"))
    assert stale_data["pid"] == first_pid

    # 同一 home 重启：必须发布自己的新 manifest，不是沿用陈旧的
    daemon.proc = None
    daemon._manifest = None
    second = daemon.start()
    assert second["pid"] != first_pid
    assert second["pid"] == daemon.proc.pid
    daemon.stop()


def test_force_kill_cleanup(daemon):
    """强杀后进程必须真正消失，且 run 根可完整清理（不留孤儿）。"""
    daemon.start()
    pid = daemon.proc.pid
    daemon.proc.kill()
    daemon.proc.wait(timeout=10)
    assert daemon.proc.poll() is not None
    # 清理只动 run 根内本 daemon 写过的子树
    removed = daemon.cleanup_run_root()
    assert all(str(daemon.run_root) in r for r in removed)
    assert not daemon.callwarden_dir.exists()


def test_no_production_manifest_written(daemon):
    """禁止 production 发现：daemon 运行期间真实 HOME 的 manifest 目录不能被新增/改动。"""
    prod_dir = production_manifest_dir()
    before = snapshot_dir(prod_dir)
    daemon.start()
    time.sleep(1.0)
    daemon.stop()
    after = snapshot_dir(prod_dir)
    if not before["exists"] and not after["exists"]:
        pytest.skip("production .callwarden does not exist on this machine")
    # 文件集合与 mtime 必须完全不变（本卡的隔离 env 已把写入重定向到 run 根）
    assert before["files"] == after["files"], (
        f"production manifest dir mutated: before={before} after={after}")
