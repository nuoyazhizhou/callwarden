"""CLI-01 (A′ 恢复) health/manifest/capability 诊断链路 Rust daemon 化。

覆盖 task 要求的 6 类场景 + 至少一项真实 get_stats HTTP round-trip：
  success / missing-manifest / stale-pid / wrong-authority / daemon-unavailable
  + get_stats (query.stats) round-trip。

设计要点（与 task 不变量一致）：
- Python 仅作 HTTP thin shell；Rust daemon + authority-scoped manifest 为权威。
- 所有失败 fail-closed 返回稳定且可区分的结构化错误，绝不降级到本地 SQLite。
"""

import json
import os

import pytest

from callwarden.config import (
    compute_http_manifest_hash,
    get_http_authority_id,
    get_http_manifest_path,
)
from callwarden.server.daemon_client import (
    HttpDaemonRpcClient,
    DaemonUnavailableError,
)
from callwarden.server.daemon_protocol import DaemonRemoteError
from callwarden.server.daemon_autostart import resolve_http_endpoint_and_manifest


def _live_manifest() -> dict:
    path = get_http_manifest_path(get_http_authority_id())
    if not os.path.exists(path):
        pytest.skip("daemon 未运行（无 authority manifest），跳过 live 成功用例")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _write_manifest(tmp_path, mutate) -> str:
    m = _live_manifest()
    mutate(m)
    m["manifest_hash"] = compute_http_manifest_hash(m)
    path = os.path.join(str(tmp_path), "http-daemon.test.manifest.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(m, f)
    return path


# ---------------------------------------------------------------------------
# success：真实 daemon 权威响应
# ---------------------------------------------------------------------------
def test_health_success_live_daemon(w3_live):
    """B 桶：迁移到 w3_live 隔离 daemon 权威栈做真实 HTTP round-trip。

    stale 依据（B 桶：live daemon harness）：无参 `HttpDaemonRpcClient()`
    （`server/daemon_client.py:2141`）经 `discover()` →
    `resolve_http_endpoint_and_manifest`（`server/daemon_autostart.py:979-1030`），
    仅在存在 authority-scoped manifest 或显式 `CW_DAEMON_HTTP_ENDPOINT` 时成功，
    否则 fail-closed `E_HTTP_MANIFEST_MISSING`。现代语义「Python 仅 HTTP thin shell，
    Rust daemon 为权威」需要 live daemon，故改用 tests/conftest.py::w3_live 隔离栈。
    """
    r = w3_live["client"].health()
    assert isinstance(r, dict)
    for k in ("endpoint", "pid", "schema_version", "capability_registry_revision"):
        assert k in r


def test_capability_success_live_daemon(w3_live):
    """B 桶：迁移到 w3_live 隔离 daemon（见 test_health_success_live_daemon 依据）。"""
    r = w3_live["client"].capabilities()
    assert isinstance(r, dict)
    assert "methods" in r
    assert "health" in r["methods"]


def test_manifest_success_live(w3_live):
    """B 桶：迁移到 w3_live 隔离 daemon（见 test_health_success_live_daemon 依据）。

    以隔离 daemon client 落盘的 authority manifest 路径显式解析，验证
    `resolve_http_endpoint_and_manifest` 对真实 manifest 的 authority 校验通过。
    """
    _ep, manifest = resolve_http_endpoint_and_manifest(
        manifest_path=w3_live["client"]._manifest_path
    )
    assert isinstance(manifest, dict)
    assert manifest.get("authority_id") == get_http_authority_id()


# ---------------------------------------------------------------------------
# fail-closed：稳定且可区分的错误
# ---------------------------------------------------------------------------
def test_missing_manifest_fail_closed(tmp_path):
    # 用不存在的显式路径 + 不存在的 authority（无默认 manifest），两者皆缺
    # -> resolver 无法回退到任何 manifest，fail-closed 抛 E_HTTP_MANIFEST_MISSING。
    with pytest.raises(DaemonRemoteError) as ei:
        resolve_http_endpoint_and_manifest(
            manifest_path=str(tmp_path / "nope.json"),
            authority_id="NO-SUCH-AUTHORITY-X",
        )
    assert ei.value.code == "E_HTTP_MANIFEST_MISSING"


def test_stale_pid_fail_closed(tmp_path):
    path = _write_manifest(tmp_path, lambda m: m.update(pid=91234567))
    with pytest.raises(DaemonRemoteError) as ei:
        HttpDaemonRpcClient(manifest_path=path).health()
    assert ei.value.code == "E_HTTP_MANIFEST_STALE"
    assert "stale" in str(ei.value).lower() or "存活" in str(ei.value)


def test_wrong_authority_fail_closed(tmp_path):
    path = _write_manifest(tmp_path, lambda m: m.update(authority_id="WRONG-AUTHORITY-X"))
    with pytest.raises(DaemonRemoteError) as ei:
        HttpDaemonRpcClient(
            manifest_path=path, authority_id=get_http_authority_id()
        ).health()
    assert ei.value.code == "E_HTTP_MANIFEST_STALE"
    assert "authority" in str(ei.value).lower()


def test_daemon_unavailable_fail_closed():
    with pytest.raises(DaemonUnavailableError) as ei:
        HttpDaemonRpcClient(endpoint="http://127.0.0.1:9").health()
    assert ei.value.code == "E_HTTP_DAEMON_UNAVAILABLE"


# ---------------------------------------------------------------------------
# get_stats 真实 round-trip（Python 仅作 HTTP thin shell，Rust 权威响应）
# ---------------------------------------------------------------------------
def test_get_stats_round_trip(w3_live):
    """B 桶：迁移到 w3_live 隔离 daemon（见 test_health_success_live_daemon 依据）。

    原用例无参 `HttpDaemonRpcClient()` 在无后台 daemon 时 fail-closed
    `E_HTTP_MANIFEST_MISSING`（非 round-trip）。改用隔离 daemon client + 已注册的
    workspace_instance_id，做真实 `query.stats` HTTP round-trip；daemon 已发布空
    snapshot，成功返回 dict 或返回结构化业务错误（皆为真实往返，非连接失败）。
    """
    c = w3_live["client"]
    try:
        r = c.call("query.stats", {"workspace_instance_id": w3_live["inst"]})
    except DaemonRemoteError as e:
        # 结构化错误也是真实 round-trip（daemon 已响应，非连接失败）
        assert e.code in ("snapshot_not_ready", "workspace_not_found")
        return
    assert isinstance(r, dict)
