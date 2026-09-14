"""Role Prompt Compiler v1 CLI HTTP thin client（RP-06-cli，spec §4.3）.

``cw task prompt T-... [--format llm|card|json]`` 薄壳实现：

- 唯一 authority 是 daemon RPC ``task.prompt.compile``（RP-05 落地，
  dispatch.rs 注册；route matrix RP-07/08 发布，N=243）。本模块只做
  HTTP JSON-RPC 透传 + 本地展示，无本地模板、无 SQLite、无 workspace
  推导、无 fallback 业务。
- spec §4.1：request params 恰为 ``task_id`` + 可选
  ``expected_workspace_instance_id``；禁止携带 role/format/identity/
  credential/lease token 等字段。``--format`` 只改变本地展示，绝不发送
  给 daemon、不进入任何 hash（spec §4.3）。
- spec §4.3：不得复用/扩散 ``task next-action`` 的 legacy
  ``derive_workspace_instance_id`` fallback——本命令不推导 workspace，
  只有调用方显式提供 ``--expected-workspace-instance-id`` 时才透传。
- 三格式（仅本地展示，daemon bundle 字段原样投影，缺失标 ``—``，
  不伪造）：
  - ``llm``（默认）：逐字输出 ``prompt.text``；
  - ``card``：结构化角色卡投影（routing/contract/authorization/
    authority/template/hash 字段）；
  - ``json``：daemon 返回的完整 bundle JSON 原样输出。
- fail-closed：daemon 不可达/业务错误时输出结构化错误，绝不回退本地
  渲染或本地 SQL（§3 分层：CLI 禁止重建 bundle）。
- 纯净度：不 import sqlite3 / get_db / db 业务模块（check_client_purity
  门禁；cli 软门禁白名单为空，新文件零违例）。
"""

from __future__ import annotations

import json

from ..server.daemon_client import (
    DaemonRemoteError,
    DaemonUnavailableError,
    route_rpc,
)

_RPC = "task.prompt.compile"
_FORMATS = ("llm", "card", "json")


def _missing(value) -> str:
    """bundle 字段缺失时的占位（不猜测、不补造）。"""
    return "—" if value is None else str(value)


def _error_payload(code: str, message: str) -> str:
    return json.dumps(
        {"ok": False, "code": code, "message": message},
        ensure_ascii=False, indent=2,
    )


def _render_card(bundle: dict) -> None:
    """结构化角色卡投影：全部字段逐字取自 daemon bundle，缺失标 —。"""
    def section(title: str, rows: list[tuple[str, object]]) -> None:
        print(f"### {title}")
        for key, value in rows:
            print(f"{key}: {_missing(value)}")
        print()

    print(f"prompt_kind: {_missing(bundle.get('prompt_kind'))}")
    print()

    routing = bundle.get("routing") or {}
    section("Routing", [
        ("Decision", routing.get("decision")),
        ("Action", routing.get("action")),
        ("Required role", routing.get("required_role")),
        ("Next action", routing.get("next_action")),
        ("Step", routing.get("step_id")),
    ])

    contract = bundle.get("contract") or {}
    section("Contract", [
        ("Task contract", contract.get("task_contract_id")),
        ("Task contract revision", contract.get("task_contract_revision")),
        ("Task contract hash", contract.get("task_contract_hash")),
        ("Role contract hash", contract.get("role_contract_hash")),
        ("Prompt template", contract.get("role_contract_prompt_template_id")),
        ("Prompt hash", contract.get("role_contract_prompt_hash")),
        ("Identity policy", contract.get("identity_policy_status")),
    ])

    authorization = bundle.get("authorization") or {}
    section("Authorization", [
        ("Routing state", authorization.get("routing_state")),
        ("Valid for claim", authorization.get("valid_for_claim")),
        ("Mutation recheck required", authorization.get("mutation_recheck_required")),
    ])

    authority = bundle.get("authority") or {}
    section("Authority", [
        ("Workspace", authority.get("workspace_id")),
        ("Workspace instance", authority.get("workspace_instance_id")),
        ("Workspace binding", authority.get("workspace_binding_id")),
        ("Workspace capture", authority.get("workspace_capture_id")),
        ("Event watermark", authority.get("source_event_watermark")),
    ])

    template = bundle.get("template") or {}
    section("Template", [
        ("Source", template.get("source")),
        ("Body template", template.get("body_template_id")),
        ("Body template hash", template.get("body_template_hash")),
        ("Compiler policy", template.get("compiler_policy_id")),
        ("Compiler policy hash", template.get("compiler_policy_hash")),
        ("Manifest hash", template.get("manifest_hash")),
    ])

    prompt = bundle.get("prompt") or {}
    print("### Hashes")
    print(f"prompt.sha256: {_missing(prompt.get('sha256'))}")
    print(f"context_hash: {_missing(bundle.get('context_hash'))}")
    print(f"bundle_hash: {_missing(bundle.get('bundle_hash'))}")
    print(f"generated_at: {_missing(bundle.get('generated_at'))}")
    omissions = bundle.get("omissions")
    print(f"omissions: {json.dumps(omissions, ensure_ascii=False) if omissions else '—'}")


def run_task_prompt(
    task_id: str,
    fmt: str = "llm",
    expected_workspace_instance_id: str = "",
) -> bool:
    """``cw task prompt`` 薄客户端入口（读命令，无 mutation）。

    Args:
        task_id: 精确任务 ID（唯一必填业务字段，daemon 侧校验）。
        fmt: 本地展示格式 llm|card|json（默认 llm；不发送给 daemon）。
        expected_workspace_instance_id: 可选 caller assertion（spec §4.2）；
            仅当调用方显式提供时透传，本命令绝不推导 workspace。

    Returns:
        True（命令已处理；错误也已结构化输出，不抛出到调用链）。
    """
    if fmt not in _FORMATS:
        print(_error_payload(
            "E_TASK_PROMPT_INVALID_FORMAT",
            f"invalid --format {fmt!r}; expected one of {', '.join(_FORMATS)}",
        ))
        return True

    # spec §4.1：params 恰为 task_id + 可选 expected_workspace_instance_id；
    # 不推导 workspace、不注入 format/role/identity（§4.3）。
    params = {"task_id": task_id}
    if expected_workspace_instance_id:
        params["expected_workspace_instance_id"] = expected_workspace_instance_id

    try:
        bundle = route_rpc(_RPC, params, "READ_ONLY")
    except DaemonRemoteError as exc:
        # 结构化业务错误原样透传（不伪装、不降级、不本地渲染）
        print(_error_payload(exc.code, exc.message))
        return True
    except DaemonUnavailableError as exc:
        # fail-closed：daemon 不可用无本地 fallback（spec §4.3）
        print(_error_payload("E_DAEMON_UNAVAILABLE", str(exc)))
        return True

    if not isinstance(bundle, dict):
        print(_error_payload(
            "E_DAEMON_UNAVAILABLE",
            "daemon returned a non-object bundle; fail-closed",
        ))
        return True

    if fmt == "json":
        print(json.dumps(bundle, ensure_ascii=False, indent=2))
        return True

    prompt = bundle.get("prompt") or {}
    text = prompt.get("text")
    if fmt == "llm":
        # 逐字输出 prompt.text；缺失时输出完整 bundle（仅展示，不伪造）
        print(text if isinstance(text, str)
              else json.dumps(bundle, ensure_ascii=False, indent=2))
        return True

    _render_card(bundle)
    return True
