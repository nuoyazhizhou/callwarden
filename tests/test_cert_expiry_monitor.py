r"""§9.31 证书到期监控（daemon /health `cert_expires_in_days` + 客户端告警）。

- 单测：verify_health 对临期/过期/-1 哨兵/字段缺失四种 health 响应的告警行为
  （实例级限频一次）。
- 集成（隔离 daemon 栈，复用 test_mtls_control_plane 夹具）：
  /health 响应含 cert_expires_in_days 键且为非负整数。
"""

import json
import logging
import os
import sys

import pytest

from callwarden.config import HTTP_DEFAULT_TIMEOUT
from callwarden.server.daemon_client import (
    CERT_EXPIRY_WARN_DAYS,
    HttpDaemonRpcClient,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

pytestmark = pytest.mark.skipif(
    sys.platform != "win32",
    reason="§9.31 证书到期监控用例需要 Windows + loopback HTTPS daemon（集成部分）",
)

_DEAD_ENDPOINT = "https://127.0.0.1:9"  # fail-closed 快速失败端点（不真正连接）


def _client_with_health(monkeypatch, health, caplog=None):
    """构造 verify_health 单测夹具：monkeypatch _http_get 返回固定 health。"""
    client = HttpDaemonRpcClient(endpoint=_DEAD_ENDPOINT, verify_health=False)
    monkeypatch.setattr(client, "_http_get", lambda url: health)
    return client


# ----------------------------------------------------------------------
# 单测：_warn_cert_expiry 告警语义
# ----------------------------------------------------------------------

def test_near_expiry_warns_once(monkeypatch, caplog):
    """0 <= days < 阈值 → warning 一条；第二次 verify_health 限频不再告警。"""
    days = CERT_EXPIRY_WARN_DAYS - 1
    client = _client_with_health(
        monkeypatch, {"pid": 1, "cert_expires_in_days": days})
    with caplog.at_level(logging.WARNING, logger="callwarden.server.daemon_client"):
        client.verify_health(endpoint=_DEAD_ENDPOINT)
        client.verify_health(endpoint=_DEAD_ENDPOINT)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert str(days) in warnings[0].getMessage()
    assert "cw pki renew" in warnings[0].getMessage()


def test_expired_warns_distinct_message(monkeypatch, caplog):
    """days < 0 且非 -1 → 已过期 warning（消息区分）。"""
    client = _client_with_health(
        monkeypatch, {"pid": 1, "cert_expires_in_days": -3})
    with caplog.at_level(logging.WARNING, logger="callwarden.server.daemon_client"):
        client.verify_health(endpoint=_DEAD_ENDPOINT)
    msgs = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(msgs) == 1
    assert "过期" in msgs[0]


def test_sentinel_minus_one_is_silent(monkeypatch, caplog):
    """-1（PKI 材料缺省哨兵）→ 静默跳过。"""
    client = _client_with_health(
        monkeypatch, {"pid": 1, "cert_expires_in_days": -1})
    with caplog.at_level(logging.WARNING, logger="callwarden.server.daemon_client"):
        client.verify_health(endpoint=_DEAD_ENDPOINT)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]


def test_missing_field_is_silent(monkeypatch, caplog):
    """旧版 daemon（无该字段）→ 静默，不告警不报错。"""
    client = _client_with_health(monkeypatch, {"pid": 1})
    with caplog.at_level(logging.WARNING, logger="callwarden.server.daemon_client"):
        health = client.verify_health(endpoint=_DEAD_ENDPOINT)
    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
    assert health == {"pid": 1}


# ----------------------------------------------------------------------
# 集成：隔离 daemon /health 含 cert_expires_in_days
# ----------------------------------------------------------------------

try:
    from test_mtls_control_plane import (  # noqa: F401
        build_ssl_context,
        live_daemon,
        wait_health,
    )
    _HARNESS_OK = True
except Exception:  # pragma: no cover - 夹具不可导入时跳过集成段
    _HARNESS_OK = False

requires_harness = pytest.mark.skipif(
    not _HARNESS_OK, reason="test_mtls_control_plane 夹具不可用")


@requires_harness
def test_live_health_has_cert_expires_in_days(live_daemon):
    """S9.31-a：daemon /health 响应包含 cert_expires_in_days（≥0）。"""
    manifest, _mpath, data_root = live_daemon
    endpoint = manifest["endpoint"]
    ctx = build_ssl_context(data_root)
    health = wait_health(endpoint, ctx)
    days = health.get("cert_expires_in_days")
    assert isinstance(days, int), f"/health 缺 cert_expires_in_days: {health}"
    assert days >= 0, f"fresh pki 的 server 证书应未到期，got {days}"
    assert json.dumps(health)  # 序列化自检（可打印诊断）
