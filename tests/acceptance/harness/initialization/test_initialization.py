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


@pytest.fixture(scope="module")
def env():
    e = InitializationEnv()
    manifest = e.start_daemon()
    # 钉 TP-09 自己 run 根内部署的候选散列（harness 现算），而非 TP-06 冻结候选：
    # F-DAEMON-01 修复（2026-10-12）后 TP-09 候选换成修复构建，冻结散列属 TP-06/07 发布门禁。
    assert manifest["daemon_binary_sha256"] == e.expected_sha256
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
    # IDOR 修复（2026-10-13）后 owner 过滤上推到 workspace.list：foreign workspace
    # （owner_uid=424242 != peer uid）对非 owner **完全不可见**，故 CLI 解析阶段
    # 即失败（not_found，不确认其存在）。修复前 list 无过滤，CLI 能解析到该
    # workspace、再在 activate 阶段返回 forbidden。两种结果都是 fail-closed 拒绝：
    # not_found = 存在性隐藏（更严），forbidden = 可见但拒绝。两者皆可接受。
    combined = (r["stdout"] + r["stderr"]).lower()
    assert "forbidden" in combined or "not_found" in combined or "无法解析" in combined, (
        f"foreign workspace must be rejected (forbidden/not_found), got: {r['stdout'][:200]}{r['stderr'][:200]}")


def test_idor_foreign_workspace_invisible_in_list(env):
    """🔴 IDOR 修复（2026-10-13）：workspace.list 必须按 owner 过滤。

    修复前 live handler 忽略 peer，返回 registry 全量行（含其他 owner 的
    宿主机路径 client_view_root / host_real_root）。修复后 foreign workspace
    对非 owner 不可见。本测试插入 owner_uid=424243 的 foreign 行，断言：
    ① 它不出现在 workspace list 输出中（含路径不泄露）；
    ② 本 run 注册的自身 workspace 仍然可见（不得过度过滤）。
    """
    import sqlite3
    import time
    reg = env.run_root / "home" / ".callwarden" / "daemon" / "registry.db"
    conn = sqlite3.connect(reg)
    ts = int(time.time())
    conn.execute(
        "INSERT OR REPLACE INTO daemon_workspaces (workspace_id, workspace_instance_id,"
        " snapshot_id, owner_uid, client_view_root, host_real_root, status,"
        " registered_at, last_active_at) VALUES (?,?,?,?,?,?,?,?,?)",
        (999010, "FOREIGN-INSTANCE-010", "snap-f2", 424243,
         "C:\\foreign-leak-path", "C:\\foreign-leak-path", "active", ts, ts))
    conn.commit()
    conn.close()

    # 自身 workspace 必须先注册（保证列表非空，验证未过度过滤）
    r_reg = env.run_cli(["workspace", "register", "tp09-own", str(env.workspace_dir)])
    assert r_reg["rc"] == 0, f"own register failed: {r_reg['stdout'][:200]}"

    r = env.run_cli(["workspace", "list"])
    assert r["rc"] == 0, f"list failed: {r['stdout'][:200]}{r['stderr'][:200]}"
    out = r["stdout"]
    # ① foreign 行的路径与 instance 不得泄露
    assert "foreign-leak-path" not in out, (
        f"IDOR: foreign host path leaked into workspace list: {out[:400]}")
    assert "FOREIGN-INSTANCE-010" not in out, (
        f"IDOR: foreign instance id leaked into workspace list: {out[:400]}")
    # ② 自身 workspace 必须可见（过度过滤回归守卫）：列表非空且含本 run 根路径
    assert "Workspaces (1 total)" in out or "Workspaces (2 total)" in out, (
        f"own workspace missing from list (over-filtering): {out[:400]}")
    assert str(env.workspace_dir) in out, (
        f"own workspace path missing from list (over-filtering): {out[:400]}")


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

def test_fdaemon01_registry_resolution_contract(env, monkeypatch):
    """F-DAEMON-01 回归守卫：registry 解析必须走「启动期注入」单链。

    2026-10-12 修复前，`task_collab.rs` 的 `list_unified_workspaces()` /
    `unified_workspace_authority()` 在**请求时**读 env `CW_DAEMON_REGISTRY_DB`，
    env 未设则兜底 `config.rs` 硬编码 `/var/lib/callwarden/registry.db`（Windows 即
    `C:\\var\\lib\\callwarden` = 生产 registry），与启动期 `--registry` 无关——
    实测隔离 daemon 的 `workspace.list` 返回 153 条生产 workspace。

    修复后（路线 1）：两个函数改读 `self.registry_db_path`（`with_registry_db_path()`
    启动期注入），范式同 `task_collab_lifecycle.rs`。本测试把**正确行为**钉住：
    ① 两个函数体内不得再出现请求时 env 重解析；
    ② 必须引用 `self.registry_db_path`；
    ③ `dispatch.rs` 的 backup_restore 内联解析已改为经 store 访问器；
    ④ harness 的 env 注入保留作纵深防御（不再是唯一防线）。
    """
    run_reg = env.run_root / "home" / ".callwarden" / "daemon" / "registry.db"
    assert run_reg.is_file(), "run-root registry must exist (isolation anchor)"
    assert env.registry_workspace_count() >= 1, "run-root registry should hold this run's workspaces"

    collab = (REPO_ROOT / "rust_ext" / "src" / "daemon" / "task_collab.rs").read_text(
        encoding="utf-8", errors="replace")

    # ① 两个 unified 函数体内不得再出现请求时 env 重解析
    for fn_name in ("list_unified_workspaces", "unified_workspace_authority"):
        start = collab.find(f"pub fn {fn_name}(")
        assert start != -1, f"{fn_name} not found in task_collab.rs"
        body = collab[start:start + 2200]
        assert 'std::env::var("CW_DAEMON_REGISTRY_DB")' not in body, (
            f"{fn_name} still re-parses CW_DAEMON_REGISTRY_DB at request time "
            "(F-DAEMON-01 regression)")
        # ② 必须复用启动期注入字段
        assert "self.registry_db_path" in body, (
            f"{fn_name} does not reuse injected self.registry_db_path")

    # ③ dispatch 侧 backup_restore 不得再内联 env 解析，须经 store 访问器
    disp = (REPO_ROOT / "rust_ext" / "src" / "daemon" / "dispatch.rs").read_text(
        encoding="utf-8", errors="replace")
    arm = disp[disp.find('"mcp.backup_restore.is_rust_backup_rolled_back" =>'):]
    arm = arm[:arm.find('"mcp.cli_admin.connection_test" =>')] if '"mcp.cli_admin.connection_test" =>' in arm else arm[:1500]
    assert 'std::env::var("CW_DAEMON_REGISTRY_DB")' not in arm, (
        "dispatch.rs backup_restore arm still re-parses env at request time "
        "(F-DAEMON-01 regression)")
    assert "registry_db_path()" in arm, (
        "dispatch.rs backup_restore arm must use the store accessor")

    # ④ 活体对照：daemon 侧**不注入** CW_DAEMON_REGISTRY_DB（见 init_harness
    # start_daemon 的对照实验注释），workspace.list 仍必须只返回 run 根 workspace——
    # 这才是修复生效的直接证据（修复前同配置返回 153 条生产 workspace）。
    assert "CW_DAEMON_REGISTRY_DB" not in os.environ, (
        "harness must NOT inject CW_DAEMON_REGISTRY_DB into the daemon environment; "
        "the fix routes registry resolution through startup --registry injection")
    r = env.run_cli(["workspace", "list"])
    assert r["rc"] == 0, f"workspace list failed: {r['stdout'][:300]}{r['stderr'][:300]}"
    listed = r["stdout"]
    assert "C:\\var\\lib\\callwarden" not in listed and "/var/lib/callwarden" not in listed, (
        f"workspace list leaked production registry paths (F-DAEMON-01 regression): {listed[:400]}")


def test_registry_path_must_stay_inside_run_root(env):
    """registry 越界（指向生产路径）必须被 harness 拒绝。

    daemon 侧 env 注入已移除（F-DAEMON-01 修复后走启动期 --registry），
    故此处改为验证 daemon 实际使用的 registry 落在 run 根内。
    """
    # daemon 启动期 --registry 指向 run 根（IsolatedDaemon 解析）
    daemon_reg = env.daemon._resolved_registry()
    resolved = Path(daemon_reg).resolve()
    assert str(env.run_root) in str(resolved), (
        f"daemon registry escapes run root: {resolved}")
    prod_like = Path(os.environ.get("USERPROFILE", "")) / ".callwarden" / "daemon" / "registry.db"
    if prod_like.exists():
        assert resolved != prod_like.resolve(), "daemon registry points at production!"


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
    """隔离 daemon 必须来自 run 根候选（非生产 runtime），且 manifest 忠实报告其散列。"""
    manifest = env.daemon._manifest
    assert manifest["daemon_binary_sha256"] == env.expected_sha256, (
        f"manifest sha {manifest['daemon_binary_sha256']} != deployed candidate "
        f"{env.expected_sha256}")
    assert "cw-acceptance" in manifest["daemon_executable"], (
        f"daemon exe not from run root: {manifest['daemon_executable']}")
    # 候选必须真实来自 run 根，而非生产 runtime（生产 exe 在 ~/.callwarden/runtime/）
    assert ".callwarden" not in manifest["daemon_executable"], (
        f"daemon exe looks like production runtime: {manifest['daemon_executable']}")
