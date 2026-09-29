"""conftest.py —— 收敛架构验证套件共享 fixtures。

设计（cw-rust-client-convergence-design.md §4.1 场景 A / §4.4 场景 D）：
- `isolated_http_daemon`：spawn 隔离 daemon（release 二进制 + 临时 data root），
  提供干净的 HTTP RPC 端点，供并发写（M3）与 fail-closed（M4）测试使用，
  **不触碰生产 daemon**（本机 127.0.0.1:12487 的 cw-daemon 由既有会话持有）；
- `rpc_client`：基于隔离 daemon 的 HttpDaemonRpcClient；
- `qa_workspace`：在隔离 daemon 上注册临时 workspace 并返回
  (workspace_id, workspace_instance_id, root)。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import uuid

import pytest

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# 复用 release 验收套件的隔离 daemon 启动/等待/清理助手（同源，避免复制漂移）
_TESTS_DIR = os.path.join(_REPO_ROOT, "tests")
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

from test_http_daemon_release_acceptance import (  # noqa: E402
    _backup_http_manifest,
    _restore_or_clean_http_manifest,
    _spawn_isolated_daemon,
    _terminate,
    _wait_manifest,
)

_RELEASE_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "release", "cw-daemon.exe")
_DEBUG_BIN = os.path.join(_REPO_ROOT, "rust_ext", "target", "debug", "cw-daemon.exe")


def _pick_bin() -> str:
    """优先 release 二进制（与生产 runtime 一致）；缺失回退 debug。"""
    if os.path.isfile(_RELEASE_BIN):
        return _RELEASE_BIN
    if os.path.isfile(_DEBUG_BIN):
        return _DEBUG_BIN
    raise RuntimeError("cw-daemon.exe 未构建（需 cargo build --bin cw-daemon）")


@pytest.fixture(scope="module")
def isolated_http_daemon():
    """模块级隔离 HTTP daemon：data_root 全隔离，测完清理 manifest + 进程 + 目录。"""
    from callwarden.server.daemon_client import HttpDaemonRpcClient

    bin_path = _pick_bin()
    data_root = tempfile.mkdtemp(prefix="cw_convergence_")
    backup = _backup_http_manifest()
    proc = _spawn_isolated_daemon(bin_path, data_root)
    try:
        manifest = _wait_manifest(proc, timeout=20)
        if manifest is None:
            stdout = (proc.stdout.read(4000).decode("utf-8", "replace")
                      if proc.stdout else "")
            stderr = (proc.stderr.read(4000).decode("utf-8", "replace")
                      if proc.stderr else "")
            # 生产 daemon 正持有 SID 级 instance 锁(daemon-instance.<SID>.lock,
            # AGENTS.md §34 DaemonMutex)时,隔离 daemon 无法启动。这是环境约束
            # (隔离套件设计为不触碰生产 daemon,需在生产 daemon 停止时运行),
            # 不是测试失败——skip 而非 fail,避免本地有 daemon 在跑时误报红。
            if "E_DAEMON_ALREADY_RUNNING" in stderr:
                _terminate(proc)
                pytest.skip(
                    "隔离 daemon 无法启动:生产 daemon 正持有 SID 级 instance 锁。"
                    "隔离收敛套件需在生产 daemon 停止时运行(cw daemon stop)。"
                )
            pytest.fail(
                f"隔离 daemon 未发布 manifest\nstdout={stdout}\nstderr={stderr}"
            )
        client = HttpDaemonRpcClient(
            endpoint=manifest["endpoint"],
            verify_health=False,
            validate_manifest=False,
            timeout=15,
        )
        yield {
            "proc": proc,
            "manifest": manifest,
            "endpoint": manifest["endpoint"],
            "client": client,
            "data_root": data_root,
        }
    finally:
        _terminate(proc)
        _restore_or_clean_http_manifest(proc.pid, backup)
        shutil.rmtree(data_root, ignore_errors=True)


@pytest.fixture()
def rpc_client(isolated_http_daemon):
    return isolated_http_daemon["client"]


def _ensure_task_db_workspace(data_root: str, ws_id: int, name: str, root: str) -> None:
    """在隔离 daemon 的 task-DB `workspaces` 表插入/更新权威 workspace 行。

    与 test_lease_rpc.py 的 lease_env fixture 同源：Rust task handler 的
    lease/apply/close 依赖 task-DB `workspaces` 表绑定（active_workspace_id /
    task_bound_workspace_id），HTTP `workspace.register` 只写 daemon 注册表
    （daemon_workspaces），两者必须一致否则 E_IDENTITY_NOT_WIRED。
    """
    import sqlite3

    task_db = os.path.join(data_root, "task.db")
    if not os.path.exists(task_db):
        raise RuntimeError(f"task-DB 不存在: {task_db}")
    conn = sqlite3.connect(task_db)
    try:
        conn.execute(
            "INSERT OR REPLACE INTO workspaces (id, name, root_path, created_at, is_active) "
            "VALUES (?, ?, ?, ?, 1)",
            (ws_id, name, root, time.time()),
        )
        conn.commit()
    finally:
        conn.close()


@pytest.fixture()
def qa_workspace(isolated_http_daemon, rpc_client):
    """在隔离 daemon 注册临时 workspace，返回 dict(workspace_id, workspace_instance_id, root, name)。

    注册流程：
    1. HTTP `workspace.register` → 拿权威 workspace_id（daemon 注册表）；
    2. 同步 task-DB `workspaces` 行（Rust 任务写面绑定来源）。

    stale 依据（本批新增 workspace_instance_id）：
    旧断言/用法：仅返回数字 workspace_id，下游 task.create / agent.register / lease.* 只传 workspace_id。
    现状权威：daemon 路由层已强制要求非空字符串 workspace_instance_id，缺失即 fail-closed
      - rust_ext/src/daemon/task_loop/create.rs:36-37（ERR_TASK_WORKSPACE_INSTANCE_REQUIRED）
      - rust_ext/src/daemon/task_loop/create.rs:277-287（空实例 → E_TASK_WORKSPACE_INSTANCE_REQUIRED）
      - rust_ext/src/daemon/workspace_reconciliation.rs:74-80（禁止空实例合成 ws-{id}）
    故此处从 `workspace.register` 回包透出 daemon 颁发的权威 instance id，
    供下游写面显式传入（测试侧对齐新语义，非弱化断言）。
    """
    data_root = isolated_http_daemon["data_root"]
    root = tempfile.mkdtemp(prefix="cw_qa_ws_")
    name = f"qa-ws-{uuid.uuid4().hex[:8]}"
    reg = rpc_client.call("workspace.register", {
        "name": name,
        "client_view_root": root,
        "description": "QA convergence workspace",
    })
    ws_id = reg.get("workspace_id")
    # daemon 颁发的权威 workspace_instance_id（必填字符串），回包字段名见
    # server/daemon_client.py:3266 workspace_register 及 daemon 侧 WORKSPACE_CAPTURE。
    ws_inst = reg.get("workspace_instance_id")
    _ensure_task_db_workspace(data_root, ws_id, name, root)
    yield {
        "workspace_id": ws_id,
        "workspace_instance_id": ws_inst,
        "root": root,
        "name": name,
    }
    shutil.rmtree(root, ignore_errors=True)


def call_rpc(client, method: str, params: dict):
    """便捷 RPC 调用：返回 result dict（DaemonRemoteError 原样上抛）。"""
    return client.call(method, params)


# ----------------------------------------------------------------------
# T1 种子 workspace fixture(全功能全量测试基建)
# ----------------------------------------------------------------------
_SEED_SAMPLE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seed_sample")


@pytest.fixture(scope="module")
def seed_workspace(isolated_http_daemon):
    """已 build_graph 的种子 workspace,供全量测试(T2 MCP / T3 CLI)真实调用用。

    与 qa_workspace(空目录)不同,seed_workspace 把 tests/convergence/seed_sample
    的多语言样本代码拷进隔离 workspace root 并 build_graph,产出可查询的真实
    符号图谱(符号/调用边),再从真实 query 结果提取已知符号名,装配 SeedContext
    供 param_provider 产真实前置状态参数。

    module-scoped:全量测试单次 build_graph 复用,避免每个用例重建(build_graph
    是慢方法)。

    返回 dict:
      - client: 该隔离 daemon 的 HttpDaemonRpcClient
      - endpoint: HTTP 端点
      - ctx: param_provider.SeedContext(已填真实前置状态)
      - workspace_id / workspace_instance_id / root
      - stats: build_graph 后的 query.stats 结果(用于断言符号非空)
    """
    from callwarden.server.daemon_client import HttpDaemonRpcClient
    from param_provider import SeedContext

    data_root = isolated_http_daemon["data_root"]
    endpoint = isolated_http_daemon["endpoint"]

    # 独立 client(与其他 fixture 隔离,显式 configure 种子 root)
    root = tempfile.mkdtemp(prefix="cw_seed_ws_")
    # 拷贝种子样本代码到 workspace root
    for fname in os.listdir(_SEED_SAMPLE_DIR):
        src = os.path.join(_SEED_SAMPLE_DIR, fname)
        if os.path.isfile(src):
            shutil.copy2(src, os.path.join(root, fname))

    client = HttpDaemonRpcClient(endpoint=endpoint, verify_health=False,
                                 validate_manifest=False, timeout=180)

    # 1. 注册 workspace(拿权威 instance id)
    reg = client.call("workspace.register", {
        "name": f"seed-{uuid.uuid4().hex[:8]}",
        "client_view_root": root,
        "description": "T1 seed workspace (build_graph 样本)",
    })
    ws_id = reg.get("workspace_id")
    ws_inst = reg.get("workspace_instance_id")
    _ensure_task_db_workspace(data_root, ws_id, reg.get("name", "seed"), root)

    # 2. build_graph(全量解析种子样本,慢方法但样本小,秒级)
    build = client.call("workspace.build_graph", {"workspace_instance_id": ws_inst})

    # 3. publish snapshot(query.* 依赖已发布 snapshot;db_path 由 daemon 返回)
    db_result = client.call("mcp.common.get_db_path_for_daemon", {})
    db_path = db_result.get("db_path") if isinstance(db_result, dict) else None
    stats = None
    known_qnames: list = []
    known_files: list = []
    if db_path:
        try:
            client.call("snapshot.publish", {
                "workspace_instance_id": ws_inst,
                "build_context_hash": "",
                "db_path": db_path,
            })
            stats = client.call("query.stats", {"workspace_instance_id": ws_inst})
            # 从真实 search 结果提取已知符号名(填 SeedContext)
            try:
                found = client.call("query.search", {
                    "workspace_instance_id": ws_inst,
                    "query": "compute",
                    "limit": 20,
                })
                rows = found if isinstance(found, list) else found.get("results", []) \
                    if isinstance(found, dict) else []
                for r in rows:
                    if not isinstance(r, dict):
                        continue
                    qn = r.get("qualified_name") or r.get("name")
                    if qn and qn not in known_qnames:
                        known_qnames.append(qn)
                    fp = r.get("file_path") or r.get("rel_path")
                    if fp and fp not in known_files:
                        known_files.append(fp)
            except Exception:
                pass  # search 失败不阻断 fixture;ctx 用样本已知默认
        except Exception:
            pass  # publish/stats 失败不阻断;标记 stats=None,用例可据此 skip

    # 样本里确定存在的符号(search 拿不到时的可靠回退)
    if not known_qnames:
        known_qnames = ["compute", "add", "multiply"]
    if not known_files:
        known_files = ["calc.py", "service.ts"]

    ctx = SeedContext(
        workspace_id=ws_id,
        workspace_instance_id=ws_inst,
        root=root,
        known_qualified_names=known_qnames,
        known_callee_names=["add", "multiply"],
        known_file_paths=known_files,
    )

    yield {
        "client": client,
        "endpoint": endpoint,
        "ctx": ctx,
        "workspace_id": ws_id,
        "workspace_instance_id": ws_inst,
        "root": root,
        "stats": stats,
        "build": build,
    }
    shutil.rmtree(root, ignore_errors=True)
