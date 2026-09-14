# -*- coding: utf-8 -*-
"""RP-06-cli CLI thin client 单元测试（mock route_rpc，不依赖 live daemon）

覆盖卡 T-1788726774416-354c5a00（变体 key role-prompt-v1-rp06-cli）acceptance：
- spec §4.1：params 恰为 task_id + 可选 expected_workspace_instance_id；
  format/role/identity 等禁止字段绝不发送给 daemon；
- spec §4.3：--format 只改变本地展示（llm 默认逐字输出 prompt.text；card
  结构化投影仅复述 bundle 字段；json 原样输出 bundle），不进入任何 hash；
- 不复用/扩散 legacy derive_workspace_instance_id fallback（AST 级 +
  运行时 monkeypatch 双断言）；
- fail-closed：daemon 不可用/业务错误输出结构化错误码，无本地 fallback
  渲染；
- cli/main.py 薄注册面：prompt parser、dispatch、_READONLY_TASK_ACTIONS；
- 薄壳纯净度：无 sqlite3/get_db/db 业务模块 import（cli 软门禁白名单为空）。

注：live RPC round-trip 需部署含 RP-05 的新 daemon（当前 974bddcf 旧构建），
按卡片 scope 本测试面以 mock 为准，live probe 由 RP-10 部署后验证承接。
"""
from __future__ import annotations

import ast
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import callwarden.cli.task_prompt as ctp  # noqa: E402
from callwarden.server.daemon_client import (  # noqa: E402
    DaemonRemoteError,
    DaemonUnavailableError,
)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_MODULE_PATH = os.path.join(_REPO_ROOT, "cli", "task_prompt.py")
_MAIN_PATH = os.path.join(_REPO_ROOT, "cli", "main.py")

TASK_ID = "T-1788726774416-354c5a00"

BUNDLE = {
    "schema_version": "role_prompt_bundle_v1",
    "task_id": TASK_ID,
    "prompt_kind": "role_work",
    "authority": {
        "workspace_id": 1,
        "workspace_instance_id": "4baea3ff12c2ea5c",
        "workspace_binding_id": "tb-x",
        "workspace_capture_id": "wc-x",
        "snapshot_id": None,
        "source_event_watermark": 123,
    },
    "routing": {
        "decision": "READY",
        "action": "CLAIM",
        "required_role": "executor",
        "next_action": "claim step",
        "step_id": "S-1",
    },
    "contract": {
        "task_contract_id": "TC-x",
        "task_contract_revision": 1,
        "task_contract_hash": "sha256:aa",
        "role_contract_revision_id": "RCR-x",
        "role_contract_hash": "sha256:bb",
        "role_contract_prompt_template_id": "cw.aprime.executor.startup.v1",
        "role_contract_prompt_hash": "sha256:cc",
        "identity_policy_status": "resolved",
    },
    "template": {
        "source": "role_contract",
        "body_template_id": "cw.aprime.executor.startup.v1",
        "body_template_hash": "sha256:dd",
        "compiler_policy_id": "cw.role_prompt.compiler_policy.v1",
        "compiler_policy_hash": "sha256:ee",
        "manifest_hash": "sha256:ff",
    },
    "authorization": {
        "routing_state": "action_ready",
        "valid_for_claim": False,
        "mutation_recheck_required": True,
    },
    "omissions": [],
    "context_hash": "sha256:11",
    "prompt": {"text": "EXECUTOR PROMPT TEXT\nline 2", "sha256": "sha256:22"},
    "bundle_hash": "sha256:33",
    "generated_at": "2026-09-07T00:00:00Z",
}


@pytest.fixture()
def bundle_route(monkeypatch):
    """route_rpc mock：捕获调用参数并返回固定 bundle。"""
    captured = {}

    def fake_route(rpc_method, params, op_class="READ_ONLY"):
        captured["rpc_method"] = rpc_method
        captured["params"] = params
        captured["op_class"] = op_class
        return dict(BUNDLE)

    monkeypatch.setattr(ctp, "route_rpc", fake_route)
    return captured


def test_llm_default_prints_prompt_text_verbatim(capsys, bundle_route) -> None:
    """默认格式 llm：逐字输出 prompt.text。"""
    assert ctp.run_task_prompt(TASK_ID) is True
    out = capsys.readouterr().out
    assert out == BUNDLE["prompt"]["text"] + "\n"
    assert bundle_route["rpc_method"] == "task.prompt.compile"
    assert bundle_route["params"] == {"task_id": TASK_ID}
    assert bundle_route["op_class"] == "READ_ONLY"


def test_format_never_sent_to_daemon(capsys, monkeypatch) -> None:
    """spec §4.3：--format 只改变本地展示，绝不发送给 daemon。"""
    captured = {}

    def fake_route(rpc_method, params, op_class="READ_ONLY"):
        captured["params"] = params
        return dict(BUNDLE)

    monkeypatch.setattr(ctp, "route_rpc", fake_route)
    for fmt in ("llm", "card", "json"):
        ctp.run_task_prompt(TASK_ID, fmt=fmt)
    assert captured["params"] == {"task_id": TASK_ID}, "params 不得包含 format"


def test_expected_workspace_instance_id_passthrough_only_when_provided(
    capsys, monkeypatch,
) -> None:
    """spec §4.1/§4.2：可选 caller assertion 仅在显式提供时透传。"""
    captured = []

    def fake_route(rpc_method, params, op_class="READ_ONLY"):
        captured.append(dict(params))
        return dict(BUNDLE)

    monkeypatch.setattr(ctp, "route_rpc", fake_route)
    ctp.run_task_prompt(TASK_ID)
    ctp.run_task_prompt(TASK_ID, expected_workspace_instance_id="wc-daemon-returned")
    assert captured[0] == {"task_id": TASK_ID}
    assert captured[1] == {
        "task_id": TASK_ID,
        "expected_workspace_instance_id": "wc-daemon-returned",
    }


def test_no_client_side_workspace_derivation(monkeypatch, capsys) -> None:
    """spec §4.3：不得复用/扩散 legacy derive_workspace_instance_id fallback。"""

    def forbidden(*args, **kwargs):
        raise AssertionError("task prompt 不得调用 derive_workspace_instance_id")

    monkeypatch.setattr(ctp, "route_rpc", lambda *a, **k: dict(BUNDLE))
    # 模块级 patch derive（若未来误 import 也会在此暴露）
    monkeypatch.setattr(
        "callwarden.server.daemon_client.derive_workspace_instance_id", forbidden
    )
    ctp.run_task_prompt(TASK_ID)
    ctp.run_task_prompt(TASK_ID, fmt="card")
    ctp.run_task_prompt(TASK_ID, fmt="json")


def test_card_renders_bundle_fields_without_fabrication(
    capsys, bundle_route,
) -> None:
    """card：结构化投影全部逐字来自 bundle，缺失标 —，不伪造。"""
    ctp.run_task_prompt(TASK_ID, fmt="card")
    out = capsys.readouterr().out
    assert "prompt_kind: role_work" in out
    assert "Decision: READY" in out
    assert "Required role: executor" in out
    assert "Task contract hash: sha256:aa" in out
    assert "Compiler policy: cw.role_prompt.compiler_policy.v1" in out
    assert "bundle_hash: sha256:33" in out
    assert "Valid for claim: False" in out
    # 缺失字段占位（不伪造）：构造缺 routing 的 bundle
    broken = {k: v for k, v in BUNDLE.items() if k != "routing"}
    ctp.run_task_prompt(TASK_ID, fmt="card")  # 上一个 bundle_route 仍返回全量


def test_card_missing_fields_rendered_as_placeholder(capsys, monkeypatch) -> None:
    """card：字段缺失时标 —（skill 同款纪律：不猜测、不补造）。"""

    def sparse_route(rpc_method, params, op_class="READ_ONLY"):
        return {"schema_version": "role_prompt_bundle_v1", "task_id": TASK_ID}

    monkeypatch.setattr(ctp, "route_rpc", sparse_route)
    ctp.run_task_prompt(TASK_ID, fmt="card")
    out = capsys.readouterr().out
    assert "Decision: —" in out
    assert "bundle_hash: —" in out


def test_json_prints_exact_bundle(capsys, bundle_route) -> None:
    """json：daemon 返回的完整 bundle 原样输出（CLI/MCP/Skill 字段一致性）。"""
    ctp.run_task_prompt(TASK_ID, fmt="json")
    out = capsys.readouterr().out
    assert json.loads(out) == BUNDLE


def test_invalid_format_structured_error_no_rpc(capsys, monkeypatch) -> None:
    """非法 format：结构化错误，不触达 daemon。"""
    def must_not_call(*a, **k):
        raise AssertionError("非法 format 不得触达 daemon")

    monkeypatch.setattr(ctp, "route_rpc", must_not_call)
    ctp.run_task_prompt(TASK_ID, fmt="yaml")
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload["ok"] is False and payload["code"] == "E_TASK_PROMPT_INVALID_FORMAT"


def test_daemon_unavailable_fail_closed_no_fallback(capsys, monkeypatch) -> None:
    """daemon 不可用：结构化 E_DAEMON_UNAVAILABLE，无本地 fallback 渲染。"""

    def failing_route(rpc_method, params, op_class="READ_ONLY"):
        raise DaemonUnavailableError("E_HTTP_DAEMON_UNAVAILABLE: fail-closed")

    monkeypatch.setattr(ctp, "route_rpc", failing_route)
    for fmt in ("llm", "card", "json"):
        ctp.run_task_prompt(TASK_ID, fmt=fmt)
        payload = json.loads(capsys.readouterr().out)
        assert payload["ok"] is False
        assert payload["code"] == "E_DAEMON_UNAVAILABLE"


def test_remote_business_error_passthrough(capsys, monkeypatch) -> None:
    """daemon 业务错误（如 E_TASK_PROMPT_AUTHORITY_MISMATCH）结构化透传。"""

    def remote_error_route(rpc_method, params, op_class="READ_ONLY"):
        raise DaemonRemoteError("E_TASK_PROMPT_AUTHORITY_MISMATCH", "authority mismatch")

    monkeypatch.setattr(ctp, "route_rpc", remote_error_route)
    ctp.run_task_prompt(TASK_ID)
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["code"] == "E_TASK_PROMPT_AUTHORITY_MISMATCH"


def test_non_object_bundle_fail_closed(capsys, monkeypatch) -> None:
    """daemon 返回非对象 bundle：fail-closed（不重建、不猜结构）。"""

    def bad_route(rpc_method, params, op_class="READ_ONLY"):
        return ["not", "a", "dict"]

    monkeypatch.setattr(ctp, "route_rpc", bad_route)
    ctp.run_task_prompt(TASK_ID)
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False


def test_no_derive_import_in_source_ast() -> None:
    """AST 级：task_prompt.py 不得 import derive_workspace_instance_id /
    detect_project_root（legacy fallback 不得扩散，spec §4.3）。"""
    tree = ast.parse(open(_MODULE_PATH, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names = {a.name for a in node.names}
            assert "derive_workspace_instance_id" not in names
            assert "detect_project_root" not in names


def test_purity_no_sqlite_no_db_imports() -> None:
    """薄壳纯净度（cli 软门禁白名单为空，新文件零违例）：AST 级断言。"""
    tree = ast.parse(open(_MODULE_PATH, encoding="utf-8").read())
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names = {a.name for a in node.names}
            assert not any(n.startswith("sqlite3") for n in names), "禁止 sqlite3"
            assert not any(n.startswith("callwarden.db") for n in names), "禁止 db 模块"
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            assert not mod.startswith("sqlite3"), "禁止 sqlite3"
            assert not mod.startswith("callwarden.db"), "禁止 db 业务模块"
            assert "get_db" not in {a.name for a in node.names}, "禁止 get_db"


def test_main_registers_prompt_readonly() -> None:
    """cli/main.py 薄注册面：prompt 入 _READONLY_TASK_ACTIONS、parser 与
    dispatch 分支存在（AST 检查，避免整包 import 副作用）。"""
    src = open(_MAIN_PATH, encoding="utf-8").read()
    assert '"prompt"' in src.split("_READONLY_TASK_ACTIONS =")[1].split("\n")[0], \
        "prompt 必须加入 _READONLY_TASK_ACTIONS"
    assert 'sub.add_parser(\n        "prompt"' in src or 'add_parser(\n        "prompt"' in src, \
        "task 子命令必须注册 prompt parser"
    assert 'opts.action == "prompt"' in src, "dispatch 必须有 prompt 分支"
    assert "run_task_prompt" in src, "dispatch 必须委托 cli/task_prompt.py"
