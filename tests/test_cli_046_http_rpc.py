"""CLI-046 (A′ cli_command_projection) `cw metrics` HTTP thin-client 验证。

**stale 依据（B 桶 · W3 隔离 harness 迁移）**：旧版本用例打桩 `route_rpc`，
未覆盖真实 HTTP transport；现改打 `w3_live` 隔离 cw-daemon（真实
`HttpDaemonRpcClient` → HTTP POST /v1/rpc）。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_046_http_rpc.py）。
本模块走**真实 HTTP transport**：`_handle_metrics` → RpcDBProxy._rpc_call
→ route_rpc → HTTP POST /v1/rpc → 隔离 cw-daemon（query.metrics_summary，READ_ONLY）。
不再打桩 route_rpc；通过「隔离 daemon 的活 client 安装为 HTTP 单例 + 断言
client.last_request_body 命中 query.metrics_summary」证明请求真正到达 Rust daemon，
Python 仅编排输出。
"""

import os

import pytest

import callwarden.cli.main as main_mod
from callwarden.server.daemon_client import HttpDaemonRpcClient


@pytest.fixture(scope="module")
def cli_live(w3_live):
    """把隔离 daemon 的活 client 安装为 route_rpc 使用的 HTTP 单例。

    route_rpc 经 `HttpDaemonRpcClient.get_instance()` 取单例；不安装则单例按真实
    HOME 的 manifest 发现，命不中隔离 daemon。workspace 显式 configure 为 w3_live
    注册过的同一 root，避免 route_rpc 兜底按 cwd 绑定到 callwarden 仓库自身。
    """
    client = w3_live["client"]
    root = os.path.join(w3_live["data_root"], "repo")
    client.configure_workspace(root)
    prev = HttpDaemonRpcClient._instance
    HttpDaemonRpcClient._instance = client
    try:
        yield {"client": client, "root": root}
    finally:
        HttpDaemonRpcClient._instance = prev


def test_cli046_metrics_routes_to_daemon(cli_live, capsys):
    """success：`cw metrics` 经真实 HTTP transport 调 query.metrics_summary。"""
    client = cli_live["client"]
    client.last_request_body = None

    proxy = main_mod.RpcDBProxy(workspace_root=cli_live["root"])
    rc = main_mod._handle_metrics([], proxy)
    assert rc is True

    # 真实 transport 证据：出向信封命中隔离 daemon 的 query.metrics_summary
    body = client.last_request_body or {}
    assert body.get("method") == "query.metrics_summary", (
        f"应经 HTTP 打到 query.metrics_summary：{body!r}"
    )

    out = capsys.readouterr().out
    assert out.strip(), "metrics 应渲染 daemon 回包输出"
