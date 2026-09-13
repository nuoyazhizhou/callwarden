"""CLI-061 (A′ cli_command_projection) `cw semgrep` HTTP thin-client 验证。

覆盖 task step `fixture_matrix`（target_file: tests/test_cli_061_http_rpc.py）：
  - success：db.run_semgrep / run_semgrep_and_save / scan_semgrep_incremental 为写面
    （PROTECTED_MUTATION）；get_semgrep_summary 为只读汇总（READ_ONLY）——
    均经 RpcDBProxy._rpc_call → route_rpc，Python 仅编排输出
  - 参数契约：target_paths/config/languages/timeout（及 base_branch/head）透传
  - 结构不变量：Rust 侧 semgrep_handlers.rs（CLI-061 新增）为唯一 authority；
    run_semgrep 返回 {success,total_findings,severity_counts,results}，
    get_semgrep_summary 返回 {success,total_findings,by_severity,by_language,top_rules}

Rust 侧由 cargo 单测覆盖（见 semgrep_handlers.rs）。
"""

import callwarden.cli.main as main_mod


def test_cli061_semgrep_run_routes_to_daemon(monkeypatch, capsys):
    """success：semgrep scan（无 --save/--quick）经 route_rpc 调用 run_semgrep。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "success": True, "total_findings": 2,
            "severity_counts": {"ERROR": 1, "WARNING": 1},
            "results": [
                {"rule_id": "no-eval", "rule_name": "no-eval", "message": "avoid eval",
                 "severity": "ERROR", "path": "a.py", "start_line": 1,
                 "language": "python", "fix": ""},
                {"rule_id": "no-todo", "rule_name": "no-todo", "message": "no todo",
                 "severity": "WARNING", "path": "b.py", "start_line": 2,
                 "language": "python", "fix": ""},
            ],
            "paths_scanned": ["a.py", "b.py"], "errors": [],
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_semgrep(["scan"], proxy)
    assert rc is True
    assert captured.get("method") == "run_semgrep"
    assert captured.get("op") == "PROTECTED_MUTATION"
    assert captured["params"].get("config") == "p/default"
    out = capsys.readouterr().out
    assert "no-eval" in out and "avoid eval" in out


def test_cli061_semgrep_summary_routes_to_daemon(monkeypatch, capsys):
    """--quick → db.get_semgrep_summary 经 route_rpc 调用 get_semgrep_summary。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {
            "success": True, "total_findings": 2,
            "by_severity": {"ERROR": 1, "WARNING": 1},
            "by_language": {"python": 2},
            "top_rules": [["no-eval", {"count": 1, "message": "avoid eval", "severity": "ERROR"}]],
            "errors": [],
        }

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_semgrep(["scan", "--quick"], proxy)
    assert rc is True
    assert captured.get("method") == "get_semgrep_summary"
    # stale 依据（A 类=测试侧陈旧期望）：get_semgrep_summary 是只读汇总（不落库），
    # 未登记进 RpcDBProxy._METHOD_MAP（cli/main.py:1079-1266）→ _rpc_call 走未知方法
    # 分支固定 op_class="READ_ONLY"（cli/main.py:1293-1298）。旧断言 PROTECTED_MUTATION
    # 与现状不符（仅 run_semgrep/run_semgrep_and_save/scan_semgrep_incremental 为写面）。
    assert captured.get("op") == "READ_ONLY"
    out = capsys.readouterr().out
    assert "no-eval" in out


def test_cli061_semgrep_quick_target_paths_transmitted(monkeypatch, capsys):
    """C-07 回归：--quick 的显式路径必须以具名 target_paths 透传，不得降级为 arg0。

    修复前 `get_semgrep_summary` 未登记进 `_METHOD_MAP`，`_rpc_call` 走未知方法兜底
    把位置参数降级为 `arg0` → Rust 侧只读 `target_paths`（缺省 `["."]`）→ 用户显式
    路径被静默丢弃、退化为全工作区扫描。
    """
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"success": True, "total_findings": 0, "by_severity": {},
                "by_language": {}, "top_rules": [], "errors": []}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_semgrep(["scan", "--quick", "cli"], proxy)
    assert rc is True
    assert captured.get("method") == "get_semgrep_summary"
    assert captured.get("op") == "READ_ONLY"
    assert captured["params"].get("target_paths") == ["cli"], (
        "显式路径必须经 _METHOD_MAP 具名透传；降级为 arg0 即 C-07 回归"
    )
    assert "arg0" not in captured["params"]


def test_cli061_semgrep_scan_failure(monkeypatch, capsys):
    """daemon 业务失败：success=False + error，Python 展示错误不崩溃。"""
    captured = {}

    def _fake_route(method, params, op_class):
        captured["method"] = method
        captured["params"] = params
        captured["op"] = op_class
        return {"success": False, "error": "semgrep 未安装（PATH 中找不到 semgrep）", "results": []}

    monkeypatch.setattr(main_mod, "route_rpc", _fake_route)

    proxy = main_mod.RpcDBProxy(workspace_root="C:/git_work/x")
    rc = main_mod._handle_semgrep(["scan"], proxy)
    assert rc is True
    assert captured.get("method") == "run_semgrep"
    out = capsys.readouterr().out
    assert "semgrep 未安装" in out
