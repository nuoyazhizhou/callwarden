"""CLI-070（T-1787322798916-b06209dc）：cw vuln-blast → get_vulnerability_blast_radius thin client 契约。

验证 _handle_vuln_blast：
1. success：db.get_vulnerability_blast_radius 经 route_rpc 调 get_vulnerability_blast_radius
   READ_ONLY，finding_id/severity_filter/depth 透传；输出 risk_level/total_findings/
   findings 明细（severity 图标/rule/file_path/impacted_count）。
2. 空 findings：no findings 提示不崩溃。
3. 无 blast_radius 时输出稳定（不 KeyError）。
"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import callwarden.cli.main as main_mod  # noqa: E402


def test_cli070_vuln_blast_routes_to_daemon(monkeypatch, capsys):
    """success：vuln-blast 经 route_rpc 调用 get_vulnerability_blast_radius。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "risk_level": "high",
            "total_findings": 1,
            "total_impacted_symbols": 3,
            "findings": [
                {"finding_id": 7, "severity": "ERROR", "rule_name": "no-eval",
                 "file_path": "a.py", "symbol_qualified": "pkg.foo",
                 "impacted_count": 3,
                 "blast_radius": {"by_layer": {"0": 1, "1": 2}}},
            ],
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_vuln_blast(["--finding-id", "7", "--depth", "5"], proxy)
    assert rc is True
    assert captured.get("method") == "get_vulnerability_blast_radius"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("finding_id") == 7
    assert captured["params"].get("depth") == 5
    out = capsys.readouterr().out
    assert "high" in out
    assert "no-eval" in out and "a.py" in out
    assert "pkg.foo" in out and "0:1" in out


def test_cli070_vuln_blast_no_findings(monkeypatch, capsys):
    """空 findings：no findings 提示不崩溃。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        return {"risk_level": "low", "total_findings": 0,
                "total_impacted_symbols": 0, "findings": []}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_vuln_blast([], proxy)
    assert rc is True
    out = capsys.readouterr().out
    assert "no matching findings" in out.lower()


def test_cli070_vuln_blast_missing_optional_fields_stable(monkeypatch, capsys):
    """缺省字段稳定：finding 无 blast_radius/file_path/symbol_qualified 时不 KeyError。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        return {"risk_level": "medium", "total_findings": 1,
                "total_impacted_symbols": 1,
                "findings": [{"finding_id": 1, "severity": "WARN"}]}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_vuln_blast(["--severity", "WARN"], proxy)
    assert rc is True
    assert captured["params"].get("severity_filter") == "WARN"
    out = capsys.readouterr().out
    assert "1" in out
