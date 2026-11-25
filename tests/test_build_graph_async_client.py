"""build_graph 异步收敛（accepted=true + dry_run 轮询）的客户端单元测试。

覆盖 daemon 侧等待上限到点转后台后的工具层契约：
- 轮询用 lastBuild.build_seq 对账，返回后台构建统计；
- 后台构建失败透传 lastBuild 的 error/message；
- 轮询超时报 build_timeout；
- CALLWARDEN_BUILD_POLL_TIMEOUT_MS 解析（非法值回退默认 600s）。
"""

from __future__ import annotations

import pytest

from callwarden.server import daemon_protocol
from callwarden.server.tools import tools_workspace as tw


def test_poll_returns_last_build_after_seq_match(monkeypatch):
    responses = [
        # 第一轮：仍在进行中，lastBuild 属于更早的构建
        {"inProgress": True, "lastBuild": {"build_seq": 1, "ok": True}},
        # 第二轮：accepted 的构建已完成
        {"inProgress": False, "lastBuild": {"build_seq": 2, "ok": True, "scanned": 9}},
    ]
    calls = []

    def fake_route(method, params, op_class="READ_ONLY"):
        calls.append((method, dict(params), op_class))
        return responses[len(calls) - 1]

    monkeypatch.setattr(tw, "_route", fake_route)
    monkeypatch.setattr(tw, "_BUILD_POLL_INTERVAL_SECS", 0.0)
    monkeypatch.setenv("CALLWARDEN_BUILD_POLL_TIMEOUT_MS", "5000")

    result = tw._poll_build_result("ws-1", 2)

    assert result["build_seq"] == 2
    assert result["scanned"] == 9
    assert len(calls) == 2
    # 轮询是只读 dry_run，不占构建单飞
    assert all(method == "workspace.build_graph" for method, _, _ in calls)
    assert all(params.get("dry_run") is True for _, params, _ in calls)
    assert all(params.get("workspace_instance_id") == "ws-1" for _, params, _ in calls)
    assert all(op_class == "READ_ONLY" for _, _, op_class in calls)


def test_poll_raises_background_failure(monkeypatch):
    failure = {
        "inProgress": False,
        "lastBuild": {
            "build_seq": 5,
            "ok": False,
            "error": "build_too_large",
            "message": "文件数超过上限",
        },
    }
    monkeypatch.setattr(tw, "_route", lambda *args, **kwargs: failure)
    monkeypatch.setattr(tw, "_BUILD_POLL_INTERVAL_SECS", 0.0)
    monkeypatch.setenv("CALLWARDEN_BUILD_POLL_TIMEOUT_MS", "5000")

    with pytest.raises(daemon_protocol.DaemonRemoteError) as excinfo:
        tw._poll_build_result("ws-1", 5)

    assert excinfo.value.code == "build_too_large"
    assert "文件数超过上限" in str(excinfo.value)


def test_poll_timeout_reports_accepted_seq(monkeypatch):
    # 永远看不到 accepted 的 build_seq：轮询必须超时而非返回旧结果
    stale = {"inProgress": True, "lastBuild": {"build_seq": 1, "ok": True}}
    monkeypatch.setattr(tw, "_route", lambda *args, **kwargs: stale)
    monkeypatch.setattr(tw, "_BUILD_POLL_INTERVAL_SECS", 0.0)
    monkeypatch.setenv("CALLWARDEN_BUILD_POLL_TIMEOUT_MS", "0")

    with pytest.raises(daemon_protocol.DaemonRemoteError) as excinfo:
        tw._poll_build_result("ws-1", 7)

    assert excinfo.value.code == "build_timeout"
    assert "build_seq=7" in str(excinfo.value)


def test_poll_timeout_env_parsing(monkeypatch):
    monkeypatch.delenv("CALLWARDEN_BUILD_POLL_TIMEOUT_MS", raising=False)
    assert tw._build_poll_timeout_secs() == 600.0
    monkeypatch.setenv("CALLWARDEN_BUILD_POLL_TIMEOUT_MS", "2500")
    assert tw._build_poll_timeout_secs() == 2.5
    monkeypatch.setenv("CALLWARDEN_BUILD_POLL_TIMEOUT_MS", "not-a-number")
    assert tw._build_poll_timeout_secs() == 600.0
