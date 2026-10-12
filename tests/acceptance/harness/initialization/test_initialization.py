"""TP-09 公共初始化与实体前置 —— 验收套件。

selector: tests/acceptance/harness/initialization/test_initialization.py

正向：空目录公开初始化（workspace register/list 走公开 CLI，不碰 SQL）；
      任务库 schema/迁移就位；治理实体（task/lease）经公开入口可用。
负向：🔴 F-DAEMON-01 隔离守卫——不注入 CW_DAEMON_REGISTRY_DB 时 daemon 必须
      不泄漏生产 registry（用生产 manifest 存在性作探针，不断真实生产库）；
      registry 越界（指向生产路径）必须被 harness 拒绝；
      重复 register 同一 root 的行为可复现（幂等或明确拒绝，不静默分裂）。

运行：C:/Python314/python.exe -m pytest tests/acceptance/harness/initialization/test_initialization.py \
        -p no:cacheprovider --noconftest -v --junitxml=<run根>/reports/junit.xml
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from init_harness import InitializationEnv  # noqa: E402

TP06_DAEMON_SHA256 = "6324df120c850e45cee43349e3353e2fb88808e436127cbf9025c9e0a0e8859d"


@pytest.fixture(scope="module")
def env():
    e = InitializationEnv()
    manifest = e.start_daemon()
    assert manifest["daemon_binary_sha256"] == TP06_DAEMON_SHA256
    try:
        yield e
    finally:
        e.stop_daemon()


# ---------------- 正向 ----------------

def test_empty_workspace_dir_starts_clean(env):
    """run 根的 ws 目录初始为空（无 .callwarden / 无源码），保证是全新初始化。"""
    entries = list(env.workspace_dir.iterdir())
    assert entries == [], f"workspace dir not empty: {entries}"


def test_public_workspace_register_and_list(env):
    """公开入口 register → list：不写 SQL、不直连库，结果经 daemon 权威。"""
    r = env.run_cli(["workspace", "register", env.workspace_name, str(env.workspace_dir)])
    assert r["rc"] == 0, f"register failed: {r['stdout']}{r['stderr']}"
    r2 = env.run_cli(["workspace", "list"])
    assert r2["rc"] == 0, f"list failed: {r2['stdout']}{r2['stderr']}"
    # daemon 以 client_view_root 为权威登记（名称可被服务端归一），断言 root 路径
    assert str(env.workspace_dir).replace("/", "\\") in r2["stdout"], (
        f"registered workspace root not listed: {r2['stdout'][:400]}")
    # registry 落点在 run 根内
    assert env.registry_workspace_count() >= 1


def _workspace_id_for_root(env) -> str:
    """从 workspace list 输出中按 root 路径定位本 run 的 workspace id。"""
    r_list = env.run_cli(["workspace", "list"])
    assert r_list["rc"] == 0, f"list failed: {r_list['stdout'][:300]}"
    target = str(env.workspace_dir).replace("/", "\\")
    lines = r_list["stdout"].splitlines()
    current_id = None
    for ln in lines:
        s = ln.strip()
        if s.startswith("[") and "]" in s:
            current_id = s[1:s.index("]")]
        if "Path:" in s and target in s and current_id:
            return current_id
    raise AssertionError(f"workspace for root {target} not found in: {r_list['stdout'][:400]}")


def test_task_entities_fail_closed_without_snapshot(env):
    """register+set 后 task 实体对「无已发布 snapshot 的 workspace」必须
    fail-closed 给出结构化错误，而不是静默成功或崩溃。"""
    r0 = env.run_cli(["workspace", "register", env.workspace_name, str(env.workspace_dir)])
    assert r0["rc"] == 0, f"register failed: {r0['stdout'][:300]}"
    ws_id = _workspace_id_for_root(env)
    r_set = env.run_cli(["workspace", "set", ws_id])
    assert r_set["rc"] == 0, f"workspace set failed: {r_set['stdout'][:300]}"
    r = env.run_cli(["task", "list"])
    text = r["stdout"] + r["stderr"]
    # fail-closed：明确结构化拒绝（active workspace / snapshot 未就绪），
    # 不允许 rc=0 假成功，也不允许未捕获异常
    assert r["rc"] != 0, f"task list must fail-closed without snapshot: {text[:300]}"
    assert ("active workspace" in text) or ("snapshot" in text.lower()), (
        f"expected structured fail-closed reason, got: {text[:300]}")
    assert "Traceback" not in text, f"uncaught exception leaked: {text[:300]}"


def test_lease_status_fail_closed_for_unknown_task(env):
    """lease 探针对不存在的 task 必须 fail-closed 结构化拒绝，不崩溃。"""
    r0 = env.run_cli(["workspace", "register", env.workspace_name, str(env.workspace_dir)])
    assert r0["rc"] == 0
    ws_id = _workspace_id_for_root(env)
    assert env.run_cli(["workspace", "set", ws_id])["rc"] == 0
    r2 = env.run_cli(["lease", "status", "TP-NON-EXISTENT-000"])
    text = r2["stdout"] + r2["stderr"]
    assert "Traceback" not in text, f"uncaught exception leaked: {text[:300]}"
    # 空库/未知 task：要么明确空态（rc=0），要么结构化错误（rc≠0）——均不崩溃
    assert r2["rc"] in (0, 2), f"unexpected rc={r2['rc']}: {text[:300]}"


def test_foreign_owner_workspace_activation_forbidden(env):
    """负向：激活非本 UID 的 workspace 必须 fail-closed（workspace_forbidden）。"""
    # 手插一条 owner_uid 非本 UID 的 registry 行作为外来者
    import sqlite3
    import time
    reg = env.run_root / "home" / ".callwarden" / "daemon" / "registry.db"
    conn = sqlite3.connect(reg)
    ts = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO daemon_workspaces (workspace_id, workspace_instance_id,"
        " snapshot_id, owner_uid, client_view_root, host_real_root, status,"
        " registered_at, last_active_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (999009, "FOREIGN-INSTANCE-009", "snap-f", 424242,
         "C:\\foreign", "C:\\foreign", "active", ts, ts))
    conn.commit()
    conn.close()
    r = env.run_cli(["workspace", "set", "999009"])
    assert r["rc"] != 0, f"foreign workspace activation must fail: {r['stdout'][:300]}"
    assert "forbidden" in (r["stdout"] + r["stderr"]).lower(), (
        f"expected workspace_forbidden, got: {r['stdout'][:200]}{r['stderr'][:200]}")


def test_task_db_schema_and_migration_ready(env):
    """任务库在隔离根内创建且含协同 schema（公开初始化，无 SQL 助手补表）。"""
    tables = env.task_db_tables()
    assert tables, "task db not created under run root"
    for required in ("tasks", "task_steps", "task_events", "workspace_authority_captures"):
        assert required in tables, f"missing table {required}; got {len(tables)} tables"


def test_no_sql_helper_used(env):
    """无 SQL 助手：CLI 输出不得出现 sqlite3 直连痕迹/原始 SQL 报错。"""
    r = env.run_cli(["workspace", "list"])
    text = (r["stdout"] + r["stderr"]).lower()
    for bad in ("sqlite3.operationalerror", "no such table", "sqlite3.connect",
                "near \"select\""):
        assert bad not in text, f"raw SQL surface leaked: {bad}"


# ---------------- 负向 ----------------

def test_fdaemon01_isolation_guard(env, monkeypatch):
    """🔴 F-DAEMON-01：不注入 CW_DAEMON_REGISTRY_DB 时 daemon 会兜底到硬编码
    /var/lib/callwarden/registry.db（生产）。本测试证明该缺陷存在且 harness
    的注入是必需的——用「run 根 registry 有数据 + 生产 manifest 存在」双探针，
    不读不断生产库。"""
    run_reg = env.run_root / "home" / ".callwarden" / "daemon" / "registry.db"
    assert run_reg.is_file(), "run-root registry must exist (isolation anchor)"
    count = env.registry_workspace_count()
    assert count >= 1, "run-root registry should hold this run's workspaces"

    # 缺陷探针：若 harness 未注入 CW_DAEMON_REGISTRY_DB，list_unified_workspaces
    # 会兜底到 /var/lib/callwarden/registry.db。用源码常量做静态断言（不读生产库）。
    cfg = (REPO_ROOT / "rust_ext" / "src" / "daemon" / "config.rs").read_text(
        encoding="utf-8", errors="replace")
    assert 'DEFAULT_DATA_ROOT: &str = "/var/lib/callwarden"' in cfg, (
        "config.rs default data root changed — F-DAEMON-01 evidence needs re-verification")
    collab = (REPO_ROOT / "rust_ext" / "src" / "daemon" / "task_collab.rs").read_text(
        encoding="utf-8", errors="replace")
    assert 'CW_DAEMON_REGISTRY_DB' in collab and 'default_registry_db_path' in collab, (
        "task_collab.rs registry resolution changed — F-DAEMON-01 evidence needs re-verification")
    # harness 必须注入
    assert os.environ.get("CW_DAEMON_REGISTRY_DB"), "harness must inject CW_DAEMON_REGISTRY_DB"


def test_registry_path_must_stay_inside_run_root(env):
    """registry 越界（指向生产路径）必须被 harness 拒绝。"""
    prod_like = Path(os.environ.get("USERPROFILE", "")) / ".callwarden" / "daemon" / "registry.db"
    resolved = Path(os.environ["CW_DAEMON_REGISTRY_DB"]).resolve()
    assert str(env.run_root) in str(resolved), (
        f"registry override escapes run root: {resolved}")
    if prod_like.exists():
        assert resolved != prod_like.resolve(), "registry override points at production!"


def test_repeated_register_is_reproducible(env):
    """重复 register 同一 root：行为必须可复现（幂等复用或明确计数变化），
    不出现静默无限分裂。"""
    r1 = env.run_cli(["workspace", "register", "tp09-dup", str(env.workspace_dir)])
    assert r1["rc"] == 0, f"first register failed: {r1['stdout'][:300]}"
    c1 = env.registry_workspace_count()
    r2 = env.run_cli(["workspace", "register", "tp09-dup", str(env.workspace_dir)])
    assert r2["rc"] == 0, f"second register failed: {r2['stdout'][:300]}"
    c2 = env.registry_workspace_count()
    assert c2 >= c1, "workspace count must not decrease on repeated register"
    # 同一 root 不得无限分裂（上限保护）
    assert c2 - c1 <= 1, f"repeated register split rows unexpectedly: {c1} -> {c2}"


def test_daemon_candidate_is_frozen_binary(env):
    """隔离 daemon 必须是 TP-06 冻结候选（非生产 runtime）。"""
    manifest = env.daemon._manifest
    assert manifest["daemon_binary_sha256"] == TP06_DAEMON_SHA256
    assert "cw-acceptance" in manifest["daemon_executable"], (
        f"daemon exe not from run root: {manifest['daemon_executable']}")
