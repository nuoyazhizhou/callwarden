"""param_provider.py —— 全功能全量测试的参数 provider(T1 基建骨架)。

目标(全量测试 T2/T3):为 243 个 MCP 工具与 234 个 CLI 叶子命令的**全参数
真实调用**提供"真实前置状态"参数值。provider 不臆造随机值,而是:

1. 读取固化的工具/命令 schema(fixtures/mcp_full_schema.json、
   fixtures/cli_full_params.json,由 extract_mcp_schema.py / extract_cli_params.py
   从活的 MCP server 与 CLI argparse 提取,是真相源);
2. 结合一个**已 build_graph 的种子 workspace** 的真实前置状态
   (SeedContext:已知符号限定名、文件相对路径、workspace_id/instance、
   已创建的 task_id/step_id 等),按参数名与类型解析出合理值;
3. 对无法从种子状态推导的参数,回退到按类型的确定性占位值(不随机,
   保证测试可复现)。

设计原则:
- **确定性**:同一 schema + 同一 SeedContext → 同一组参数(可复现)。
- **真实优先**:能用种子真实符号/文件/ID 的,绝不用占位。
- **只读/写分层**:provider 只产参数,不决定是否调用;调用方(T2/T3
  测试)按 op_class(READ_ONLY / PROTECTED_MUTATION / GOVERNANCE_WRITE)
  决定隔离 daemon 上是否真的发写。
- **骨架**:本模块是 T1 交付的骨架 + 覆盖主要参数名族的映射;T2/T3
  推进时按实际调用失败(缺参/类型不符)增量补充 _NAME_RULES,不追求
  一次覆盖全部 477 个工具的每个参数。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

_FIXTURES_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
_MCP_SCHEMA_PATH = os.path.join(_FIXTURES_DIR, "mcp_full_schema.json")
_CLI_PARAMS_PATH = os.path.join(_FIXTURES_DIR, "cli_full_params.json")


# ----------------------------------------------------------------------
# 种子前置状态(由种子 workspace fixture 填充,provider 据此产真实值)
# ----------------------------------------------------------------------
@dataclass
class SeedContext:
    """已 build_graph 的种子 workspace 的真实前置状态。

    provider 用这些真实值填充对应参数名;缺省(None/空)时回退占位值。
    由 tests/convergence/conftest.py 的 seed_workspace fixture 装配。
    """

    workspace_id: Optional[int] = None
    workspace_instance_id: Optional[str] = None
    root: Optional[str] = None
    # 种子代码里真实存在的符号限定名(供 symbol/callers/callees/call-chain)
    known_qualified_names: List[str] = field(default_factory=list)
    # 种子里真实存在的调用边被调方简单名(供 callers)
    known_callee_names: List[str] = field(default_factory=list)
    # 种子里真实存在的文件相对路径(供 file/coverage/history)
    known_file_paths: List[str] = field(default_factory=list)
    # 已在种子 workspace 上创建的任务/步骤(供 task.* / evidence.*)
    task_id: Optional[str] = None
    step_id: Optional[str] = None
    # 深度前置(由 deep fixture 预建的真实实体)
    lease_token: Optional[str] = None          # 已 acquire 的 implementer lease token
    fencing_counter: Optional[int] = None      # 对应 fencing counter
    agent_id: Optional[str] = None             # 已注册 agent identity
    session_id: Optional[str] = None
    model_id: Optional[str] = None
    role: str = "implementer"
    request_id: Optional[str] = None
    branch_name: Optional[str] = None          # 已注册 branch
    candidate_id: Optional[str] = None         # 已建 rule candidate
    job_id: Optional[str] = None               # 已建 job
    symbol_hash: Optional[str] = None          # 真实符号 hash
    finding_id: Optional[str] = None
    evidence_path: Optional[str] = None        # docs/evidence/ 下真实相对路径
    snapshot_id: Optional[str] = None

    def first_qname(self) -> str:
        return self.known_qualified_names[0] if self.known_qualified_names else "compute"

    def first_callee(self) -> str:
        return self.known_callee_names[0] if self.known_callee_names else "add"

    def first_file(self) -> str:
        return self.known_file_paths[0] if self.known_file_paths else "calc.py"

    def identity(self) -> dict:
        """返回完整 identity dict(供 lease/task 写操作)。"""
        return {
            "agent_id": self.agent_id or "deep-agent",
            "session_id": self.session_id or "deep-sess",
            "model_id": self.model_id or "qa-model",
            "role": self.role or "implementer",
        }


# ----------------------------------------------------------------------
# 参数名 → 值解析规则(按名族匹配,真实优先)
# ----------------------------------------------------------------------
def _resolve_by_name(pname: str, ctx: SeedContext) -> Any:
    """按参数名族返回真实前置值;无匹配返回 _UNRESOLVED 交给类型回退。"""
    p = pname.lower()

    # workspace 归属
    if p in ("workspace_id",):
        return ctx.workspace_id if ctx.workspace_id is not None else 1
    if p in ("workspace_instance_id",):
        return ctx.workspace_instance_id or ""
    if p in ("root", "client_view_root", "workspace_root", "project_root", "dir", "directory"):
        return ctx.root or "."

    # 符号 / 调用图
    if p in ("qualified_name", "qname", "symbol", "symbol_name", "function_name", "name"):
        return ctx.first_qname()
    if p in ("callee_name", "callee", "target"):
        return ctx.first_callee()
    if p in ("caller_name", "caller"):
        return ctx.first_qname()

    # 文件 / 路径
    if p in ("file_path", "path", "file", "rel_path", "relative_path"):
        return ctx.first_file()

    # 任务编排
    if p in ("task_id",):
        return ctx.task_id or "T-seed-000000000000-00000000"
    if p in ("step_id",):
        return ctx.step_id or "step-seed-0"
    if p in ("parent_id", "parent_task_id"):
        return ctx.task_id or ""
    if p in ("provider_task_id", "consumer_task_id", "superseded_id", "anchor_task_id"):
        return ctx.task_id or "T-seed-000000000000-00000000"

    # identity(深度前置)
    if p in ("agent_id", "identity_agent_id"):
        return ctx.agent_id or "deep-agent"
    if p in ("session_id", "identity_session_id"):
        return ctx.session_id or "deep-sess"
    if p in ("model_id", "identity_model_id"):
        return ctx.model_id or "qa-model"
    if p in ("role", "identity_role"):
        return ctx.role or "implementer"
    if p in ("identity",):
        return ctx.identity()
    if p in ("request_id", "report_request_id"):
        return ctx.request_id or "req-deep-0001"

    # lease / fencing(深度前置)
    if p in ("lease_token", "token"):
        return ctx.lease_token or "tok-deep-0001"
    if p in ("fencing_counter",):
        return ctx.fencing_counter if ctx.fencing_counter is not None else 1
    if p in ("ttl_seconds",):
        return 300

    # 符号 hash / finding
    if p in ("symbol_hash",):
        return ctx.symbol_hash or "0" * 64
    if p in ("finding_id",):
        return ctx.finding_id or "finding-seed-0"
    if p in ("symbol_a", "symbol_b"):
        # diff_callers/diff_callees:用真实符号限定名(两符号对比)
        return ctx.first_qname()
    if p in ("symbol_id", "caller_symbol_id", "callee_symbol_id"):
        # propose_symbol_id_patch 等:symbol_id 是整型 id(非符号名),给占位 int 1
        return 1

    # gate / artifact / interface 专有实体 id(resolve_gate_findings/
    # record_artifact_identity/select_interface_provider 等)
    if p in ("gate_id",):
        return "gate-seed-0"
    if p in ("artifact_id",):
        return "artifact-seed-0"
    if p in ("interface_id", "interface_name"):
        return "iface-seed-0"
    if p in ("changed_files",):
        return []
    if p in ("dependencies",):
        return []

    # 编辑 / patch(工具专有)
    if p in ("new_content", "content", "new_text", "replacement"):
        return "# deep-round placeholder content\n"
    if p in ("old_content", "old_text", "expected_content"):
        return ""
    if p in ("start_line", "line", "line_number"):
        return 1
    if p in ("end_line",):
        return 5
    if p in ("edit_id",):
        return "0"

    # branch(深度前置)
    if p in ("branch_name",):
        return ctx.branch_name or "seed-branch"
    if p in ("source_branch", "from_branch"):
        return ctx.branch_name or "seed-branch"
    if p in ("target_branch", "to_branch", "base_branch", "head"):
        return ctx.branch_name or "main"

    # rule candidate(深度前置)
    if p in ("candidate_id",):
        return ctx.candidate_id or "cand-seed-0"
    if p in ("rule", "rule_text"):
        return "禁止裸 except(应捕获具体异常类型)"
    if p in ("title",):
        return "深度轮测试任务"
    if p in ("scope",):
        return {}
    if p in ("severity",):
        return "info"

    # job(深度前置)
    if p in ("job_id",):
        return ctx.job_id or "job-seed-0"

    # evidence / verdict / contract(治理写专有)
    if p in ("evidence_path", "manifest_path", "view_manifest_path"):
        return ctx.evidence_path or "docs/evidence/seed/manifest.json"
    if p in ("evidence_id",):
        return "ev-seed-0001"
    if p in ("evidence_type",):
        return "test_run"
    if p in ("payload", "evidence_json"):
        return "{}"
    if p in ("payload_hash", "view_manifest_hash", "contract_hash", "expected_previous_hash"):
        return "0" * 64
    if p in ("contract_id",):
        return "contract-seed-0"
    if p in ("contract_revision", "revision"):
        return 1
    if p in ("snapshot_id",):
        return ctx.snapshot_id or ""
    if p in ("envelope", "envelope_path"):
        return ctx.evidence_path or "docs/evidence/seed/envelope.json"
    if p in ("build_context_hash", "build_context"):
        return "0" * 40

    # gc / archive(专有)
    if p in ("archive_path",):
        return ctx.evidence_path or "docs/evidence/seed/archive.tar"
    if p in ("audit_id",):
        return "1"
    if p in ("older_than", "older_than_days", "grace_days"):
        return 30

    # 常见分页 / 限制
    if p in ("limit", "top", "max", "max_results", "count", "keep_last"):
        return 10
    if p in ("offset", "skip", "page"):
        return 0
    if p in ("depth", "max_depth"):
        return 3

    # 常见过滤
    if p in ("kind",):
        return "fn"
    if p in ("language", "lang"):
        return "python"
    if p in ("module_filter", "file_filter", "pattern", "query"):
        return "compute"
    if p in ("context",):
        return {}
    if p in ("since", "window", "time_window"):
        return "30d"
    if p in ("format",):
        return "json"
    if p in ("severity_filter", "category_filter", "status", "status_filter"):
        return ""
    if p in ("force", "dry_run", "reverse", "history", "fail"):
        return False
    if p in ("reason",):
        return "deep-round test"
    if p in ("outcome",):
        return "executor_ready_for_review"
    if p in ("from_role",):
        return "executor"

    return _UNRESOLVED


_UNRESOLVED = object()


# ----------------------------------------------------------------------
# 按类型的确定性占位值(名族无匹配时回退)
# ----------------------------------------------------------------------
def _resolve_by_type(ptype: str, default: Any) -> Any:
    """按 JSON schema 类型返回确定性占位值。default 非空时优先用 default。"""
    if default is not None:
        return default
    t = (ptype or "string").lower()
    if t in ("integer", "int", "number"):
        return 1
    if t in ("boolean", "bool"):
        return False
    if t in ("array", "list"):
        return []
    if t in ("object", "dict"):
        return {}
    return "seed"


# ----------------------------------------------------------------------
# MCP 工具参数生成
# ----------------------------------------------------------------------
def load_mcp_schema() -> Dict[str, Any]:
    with open(_MCP_SCHEMA_PATH, encoding="utf-8") as f:
        return json.load(f)


def build_mcp_params(tool: Dict[str, Any], ctx: SeedContext,
                     required_only: bool = False) -> Dict[str, Any]:
    """为单个 MCP 工具生成参数 dict。

    Args:
        tool: mcp_full_schema.json 的 tools[i]:{name, required:[...], params:{...}}
        ctx: 种子前置状态
        required_only: True 时只填 required 参数(最小可调用集);
                       False 时填全部参数(全参数调用,T2 主用途)

    Returns:
        {pname: value} 参数字典。
    """
    required = set(tool.get("required") or [])
    params_schema = tool.get("params") or {}
    out: Dict[str, Any] = {}
    for pname, pspec in params_schema.items():
        if required_only and pname not in required:
            continue
        resolved = _resolve_by_name(pname, ctx)
        if resolved is _UNRESOLVED:
            resolved = _resolve_by_type(
                (pspec or {}).get("type", "string"),
                (pspec or {}).get("default"),
            )
        out[pname] = resolved
    return out


# ----------------------------------------------------------------------
# CLI 命令参数生成(argv 列表)
# ----------------------------------------------------------------------
def load_cli_params() -> Dict[str, Any]:
    with open(_CLI_PARAMS_PATH, encoding="utf-8") as f:
        return json.load(f)


def build_cli_argv(cmd_str: str, cmd_spec: Dict[str, Any], ctx: SeedContext,
                   required_only: bool = False) -> List[str]:
    """为单个 CLI 叶子命令生成 argv(不含 'cw' 前缀,含子命令 token)。

    Args:
        cmd_str: 命令字符串(如 "callers" 或 "assignment create"),空格分隔子命令
        cmd_spec: cli_full_params.json 的 commands[cmd_str]:
                  {positionals:[{name}], options:[{opt, takes_value}]}
        ctx: 种子前置状态
        required_only: True 时只填 positionals(argparse 位置参数即必填);
                       False 时追加所有 options

    Returns:
        argv 列表,如 ["callers", "compute", "--limit", "10"]
    """
    argv: List[str] = cmd_str.split()

    # 位置参数(必填)
    for pos in cmd_spec.get("positionals") or []:
        pname = pos.get("name", "")
        val = _resolve_by_name(pname, ctx)
        if val is _UNRESOLVED:
            val = "seed"
        argv.append(str(val))

    if required_only:
        return argv

    # 可选项
    for opt in cmd_spec.get("options") or []:
        flag = (opt.get("opt") or "").rstrip(",").strip()
        # argparse extract 残留的 "-h," 帮助项跳过
        if not flag or flag in ("-h", "--help"):
            continue
        argv.append(flag)
        if opt.get("takes_value"):
            # 用 flag 去掉前导 '--' 作为参数名解析真实值
            pname = flag.lstrip("-").replace("-", "_")
            val = _resolve_by_name(pname, ctx)
            if val is _UNRESOLVED:
                val = "seed"
            argv.append(str(val))
    return argv
