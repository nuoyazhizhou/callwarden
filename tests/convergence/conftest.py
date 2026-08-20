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
    """在隔离 daemon 注册临时 workspace，返回 dict(workspace_id, root, name)。

    注册流程：
    1. HTTP `workspace.register` → 拿权威 workspace_id（daemon 注册表）；
    2. 同步 task-DB `workspaces` 行（Rust 任务写面绑定来源）。
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
    _ensure_task_db_workspace(data_root, ws_id, name, root)
    yield {
        "workspace_id": ws_id,
        "root": root,
        "name": name,
    }
    shutil.rmtree(root, ignore_errors=True)


def call_rpc(client, method: str, params: dict):
    """便捷 RPC 调用：返回 result dict（DaemonRemoteError 原样上抛）。"""
    return client.call(method, params)
