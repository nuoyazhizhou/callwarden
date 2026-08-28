"""结构化校验角色治理文档与 role-protocol.md 单源一致性。

取代旧版 substring 存在性检查（旧版对复制枚举、字段顺序、路由矛盾和 capability 漂移一律误报 PASS）。

覆盖范围：
1. 协议内部一致性：workflow_status 枚举唯一且分层完备、Handoff 字段固定顺序、outcome 分层完备；
2. 派生文档单源纪律：AGENTS/Skill/user-guide/四模板不得复制状态枚举、outcome 枚举、
   Handoff 字段块或 finding schema，只能引用 role-protocol.md；
3. 双轨整改路由 guard：凡写「交/给/→ Planner」的整改路由必须带 pre/post-cutover 限定，
   防止计划缺陷被塞给 Executor；
4. runtime capability guard：Planner v1 模板必须 design-only、`executor_replan_requested`
   与 `READY/PLAN` 引用必须带 capability/cutover 说明；
5. 归档字节级校验：archive/role-loop/templates/README.md 声明的原始 blob id 必须与
   归档文件实际内容一致（sha1 blob hash，等价 git hash-object，无 filter）；
6. --self-test 负向测试：对故意破坏的副本断言每个检查项都会报对应错误码。

用法（仓库根）：
  & C:\\Python314\\python.exe scripts/validate_template_compliance.py
  & C:\\Python314\\python.exe scripts/validate_template_compliance.py --self-test
"""

from __future__ import annotations

import argparse
import hashlib
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PROTOCOL = REPO_ROOT / ".agents/skills/cw-task-loop/references/role-protocol.md"
DEFAULT_AGENTS = REPO_ROOT / "AGENTS.md"
DEFAULT_SKILL = REPO_ROOT / ".agents/skills/cw-task-loop/SKILL.md"
DEFAULT_USER_GUIDE = REPO_ROOT / ".agents/skills/cw-task-loop/references/user-guide.md"
DEFAULT_DESIGN_V1 = REPO_ROOT / "docs/design/cw-role-handoff-task-loop.md"
DEFAULT_DESIGN_V2 = REPO_ROOT / "docs/design/cw-role-handoff-task-loop-v2-amendment.md"
DEFAULT_ARCHIVE_README = REPO_ROOT / "archive/role-loop/templates/README.md"
DEFAULT_TEMPLATE_DIR = REPO_ROOT
DEFAULT_ARCHIVE_DIR = REPO_ROOT / "archive/role-loop/templates/legacy"

CAPABILITY = "planner_governance_v1"
DESIGN_V1_BLOB = "34668462a8c135e106d32fea869b66cb8eec8a56"

# 协议必须包含的固定 workflow_status 全集（18 项，含 unknown 兜底值）。删除任一枚举值
# （例如把 execution_ready 移除协议）都必须先更新本集合——防止枚举被静默删减后校验仍通过。
REQUIRED_STATUS_ENUM = frozenset({
    "queued", "planning_pending", "planning_in_progress", "execution_ready",
    "execution_in_progress", "replanning_pending", "replanning_in_progress",
    "remediation_pending", "remediation_in_progress", "review_pending",
    "adjudication_pending", "applied_pending_close", "completed", "reverted",
    "governance_blocked", "waiting_for_decision", "waiting_for_input", "unknown",
})
# 必须精确分层的 outcome：design-only 恰好为 planner 三项，已实现恰好为六项。
REQUIRED_IMPLEMENTED_OUTCOMES = frozenset({
    "executor_ready_for_review", "executor_blocked_to_user", "reviewer_pass",
    "reviewer_blocked", "adjudicator_accepted", "adjudicator_returned",
})
REQUIRED_DESIGN_ONLY_OUTCOMES = frozenset({
    "planner_ready_for_execution", "planner_replan_required",
    "executor_replan_requested",
})
# execution_ready 必须标记为协议保留值（workflow_status_for() 从不返回它）。
REQUIRED_RESERVED_STATUSES = frozenset({
    "execution_ready", "planning_pending", "planning_in_progress",
    "replanning_pending", "replanning_in_progress", "waiting_for_decision",
    "waiting_for_input",
})
# 已实现/保留两个分层都必须与固定集**精确相等**（不只是"保留值不得混入已实现"）：
# 把 queued 从已实现层移到保留层这类分层漂移也必须报错。
REQUIRED_IMPLEMENTED_STATUSES = frozenset(REQUIRED_STATUS_ENUM - REQUIRED_RESERVED_STATUSES)

FINDING_FIELDS = (
    "finding_id", "severity", "scope", "finding_type", "introduced_by_change",
    "call_chain", "impact_radius", "root_cause", "reproduction", "impact",
    "minimal_fix", "owner_route", "blocking", "acceptance",
)
HANDOFF_BLOCK_MARKERS = ("from_role:", "outcome:", "next_role:", "independence_requirement:")
ROUTE_GUARD_TOKENS = ("pre-cutover", "post-cutover", "升级用户", "design-only", "按 §3", "按双轨", "按缺陷类别", "双轨")
REPLAN_GUARD_TOKENS = ("pre-cutover", "无法持久化", "design-only", CAPABILITY, "拒绝持久化")
RESERVED_GUARD_TOKENS = ("协议保留", CAPABILITY, "design-only")
# 3 行窗口内出现 ≥6 个不同枚举值判定为复制列表（正当引用通常 ≤4 个）。
ENUM_DUP_WINDOW = 3
ENUM_DUP_THRESHOLD = 6
FINDING_DUP_THRESHOLD = 6


@dataclass
class Violation:
    code: str
    path: str
    message: str

    def __str__(self) -> str:
        return f"[{self.code}] {self.path}: {self.message}"


@dataclass
class ProtocolSpec:
    status_enum: list[str] = field(default_factory=list)
    implemented_statuses: list[str] = field(default_factory=list)
    reserved_statuses: list[str] = field(default_factory=list)
    handoff_fields: list[str] = field(default_factory=list)
    outcome_enum: list[str] = field(default_factory=list)
    implemented_outcomes: list[str] = field(default_factory=list)
    design_only_outcomes: list[str] = field(default_factory=list)


def read_text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _backtick_values(text: str) -> list[str]:
    return re.findall(r"`([a-z][a-z0-9_]*)`", text)


def _collect_list_block(lines: list[str], start_idx: int, stop_markers: tuple[str, ...]) -> str:
    """收集 bullet 列表条目及其续行，直到空行或含 stop marker 的行。"""
    chunk: list[str] = []
    for line in lines[start_idx:]:
        if any(marker in line for marker in stop_markers):
            break
        if line.strip() == "":
            if chunk:
                break
            continue
        chunk.append(line)
    return "\n".join(chunk)


def parse_protocol(text: str) -> tuple[ProtocolSpec | None, list[Violation]]:
    spec = ProtocolSpec()
    violations: list[Violation] = []
    lines = text.splitlines()
    path = str(DEFAULT_PROTOCOL.name)

    # §2 状态枚举：锚点行到“实现分层”行之间。
    anchor_idx = next((i for i, ln in enumerate(lines) if "唯一枚举在本文维护" in ln), None)
    layer_idx = next((i for i, ln in enumerate(lines) if ln.startswith("实现分层")), None)
    if anchor_idx is None or layer_idx is None or layer_idx <= anchor_idx:
        return None, [Violation("E_PROTO_PARSE", path, "无法定位 §2 状态枚举锚点（“唯一枚举在本文维护”/“实现分层”）")]
    spec.status_enum = _backtick_values("\n".join(lines[anchor_idx + 1:layer_idx]))

    # §2 分层：已实现行与协议保留行各自的 bullet 块。
    impl_idx = next((i for i, ln in enumerate(lines) if "已实现可 emit" in ln), None)
    reserved_idx = next((i for i, ln in enumerate(lines) if "协议保留值" in ln), None)
    if impl_idx is None or reserved_idx is None or reserved_idx <= impl_idx:
        return None, [Violation("E_PROTO_PARSE", path, "无法定位 §2 实现分层 bullet（“已实现可 emit”/“协议保留值”）")]
    status_set = set(spec.status_enum)
    spec.implemented_statuses = [v for v in _backtick_values(lines[impl_idx]) if v in status_set] + [
        v for v in _backtick_values(_collect_list_block(lines, impl_idx + 1, ("协议保留值",))) if v in status_set
    ]
    spec.reserved_statuses = [v for v in _backtick_values(lines[reserved_idx]) if v in status_set] + [
        v for v in _backtick_values(_collect_list_block(lines, reserved_idx + 1, ())) if v in status_set
    ]

    # §5 Handoff 字段块：```text fenced block，顶层字段为 2 空格缩进（identity 子字段为 4 空格）。
    handoff_idx = next((i for i, ln in enumerate(lines) if ln.strip() == "Handoff:"), None)
    if handoff_idx is None:
        return None, [Violation("E_PROTO_PARSE", path, "无法定位 §5 Handoff 字段块（```text 内的 Handoff:）")]
    for line in lines[handoff_idx + 1:]:
        stripped = line.strip()
        if stripped.startswith("```"):
            break
        if not stripped or ":" not in stripped:
            continue
        if line.startswith("  ") and not line.startswith("    "):
            spec.handoff_fields.append(stripped.split(":", 1)[0])

    # outcome 全集：Handoff 块 outcome 行以 | 分隔。
    outcome_line = next((ln for ln in lines[handoff_idx:] if ln.strip().startswith("outcome:")), None)
    if outcome_line is None:
        return None, [Violation("E_PROTO_PARSE", path, "无法定位 Handoff 块 outcome 枚举行")]
    spec.outcome_enum = [v.strip() for v in outcome_line.strip().split(":", 1)[1].split("|") if v.strip()]

    # §5 outcome 分层。
    out_impl_idx = next((i for i, ln in enumerate(lines) if "已实现（daemon/CLI" in ln), None)
    out_design_idx = next((i for i, ln in enumerate(lines) if ln.startswith("- design-only")), None)
    if out_impl_idx is None or out_design_idx is None or out_design_idx <= out_impl_idx:
        return None, [Violation("E_PROTO_PARSE", path, "无法定位 §5 outcome 实现分层 bullet")]
    outcome_set = set(spec.outcome_enum)
    spec.implemented_outcomes = [v for v in _backtick_values(lines[out_impl_idx]) if v in outcome_set] + [
        v for v in _backtick_values(_collect_list_block(lines, out_impl_idx + 1, ("- design-only",))) if v in outcome_set
    ]
    spec.design_only_outcomes = [v for v in _backtick_values(lines[out_design_idx]) if v in outcome_set] + [
        v for v in _backtick_values(_collect_list_block(lines, out_design_idx + 1, ())) if v in outcome_set
    ]
    return spec, violations


def check_protocol_internal(spec: ProtocolSpec, path: Path) -> list[Violation]:
    violations: list[Violation] = []
    name = str(path)

    def dup(values: list[str]) -> list[str]:
        seen: set[str] = set()
        dups: set[str] = set()
        for v in values:
            if v in seen:
                dups.add(v)
            seen.add(v)
        return sorted(dups)

    status_dups = dup(spec.status_enum)
    if status_dups:
        violations.append(Violation("E_PROTO_ENUM_DUP", name, f"workflow_status 枚举存在重复值: {status_dups}"))
    enum_set = set(spec.status_enum)
    if enum_set != set(REQUIRED_STATUS_ENUM):
        missing = sorted(set(REQUIRED_STATUS_ENUM) - enum_set)
        extra = sorted(enum_set - set(REQUIRED_STATUS_ENUM))
        violations.append(Violation(
            "E_PROTO_REQUIRED_STATUS_MISSING", name,
            f"workflow_status 枚举与固定必需集不一致（缺失 {missing}，多出 {extra}）；"
            f"增删枚举必须先修订校验器 REQUIRED_STATUS_ENUM",
        ))
    impl_set, reserved_set = set(spec.implemented_statuses), set(spec.reserved_statuses)
    if (impl_set | reserved_set) != enum_set or (impl_set & reserved_set):
        missing = sorted(enum_set - impl_set - reserved_set)
        overlap = sorted(impl_set & reserved_set)
        violations.append(Violation(
            "E_PROTO_LAYER_MISMATCH", name,
            f"实现分层与枚举不一致（遗漏 {missing}，重叠 {overlap}，已实现 {len(impl_set)}，保留 {len(reserved_set)}）",
        ))
    # 固定分层断言（精确集）：已实现/保留集合各自必须与固定集完全一致。
    # workflow_status_for() 实际返回集 = REQUIRED_IMPLEMENTED_STATUSES（open 任务投影 queued）；
    # 保留值集 = 各 capability 未声明前 daemon 不 emit 的状态。任何单项搬迁（如把
    # queued 移入保留层）都改变对称差，必须报错。
    if impl_set != set(REQUIRED_IMPLEMENTED_STATUSES):
        wrong_impl = sorted(impl_set ^ set(REQUIRED_IMPLEMENTED_STATUSES))
        violations.append(Violation(
            "E_PROTO_IMPLEMENTED_FIXED", name,
            f"已实现 workflow_status 集与固定集不一致（对称差 {wrong_impl}）；"
            f"已实现应恰好为 workflow_status_for() 实际返回集（{len(REQUIRED_IMPLEMENTED_STATUSES)} 项）",
        ))
    if reserved_set != set(REQUIRED_RESERVED_STATUSES):
        wrong_reserved = sorted(reserved_set ^ set(REQUIRED_RESERVED_STATUSES))
        violations.append(Violation(
            "E_PROTO_RESERVED_FIXED", name,
            f"协议保留 workflow_status 集与固定集不一致（对称差 {wrong_reserved}）；"
            f"保留值应恰好为 capability 未声明前 daemon 不 emit 的 {len(REQUIRED_RESERVED_STATUSES)} 项",
        ))
    if not spec.handoff_fields or spec.handoff_fields[0] != "task_id" or spec.handoff_fields[-1] != "identity":
        violations.append(Violation(
            "E_PROTO_HANDOFF_ORDER", name,
            f"Handoff 字段顺序违规：task_id 必须首字段、identity 必须末位；实际首={spec.handoff_fields[:1]} 末={spec.handoff_fields[-1:]}",
        ))
    if dup(spec.handoff_fields):
        violations.append(Violation("E_PROTO_HANDOFF_ORDER", name, f"Handoff 字段重复: {dup(spec.handoff_fields)}"))
    out_impl_set, out_design_set, out_enum_set = (
        set(spec.implemented_outcomes), set(spec.design_only_outcomes), set(spec.outcome_enum),
    )
    if (out_impl_set | out_design_set) != out_enum_set or (out_impl_set & out_design_set):
        missing = sorted(out_enum_set - out_impl_set - out_design_set)
        overlap = sorted(out_impl_set & out_design_set)
        violations.append(Violation(
            "E_PROTO_OUTCOME_LAYER_MISMATCH", name,
            f"outcome 分层与枚举不一致（遗漏 {missing}，重叠 {overlap}，已实现 {len(out_impl_set)}，design-only {len(out_design_set)}）",
        ))
    # 固定分层断言：已实现恰好为六种、design-only 恰好为 planner 三项。
    if out_impl_set != set(REQUIRED_IMPLEMENTED_OUTCOMES):
        wrong_impl = sorted(out_impl_set ^ set(REQUIRED_IMPLEMENTED_OUTCOMES))
        violations.append(Violation(
            "E_PROTO_OUTCOME_LAYER_FIXED", name,
            f"已实现 outcome 集与固定集不一致（对称差 {wrong_impl}）；已实现应恰好为六种"
            f"（CLI/daemon 当前接受集）",
        ))
    if out_design_set != set(REQUIRED_DESIGN_ONLY_OUTCOMES):
        wrong_design = sorted(out_design_set ^ set(REQUIRED_DESIGN_ONLY_OUTCOMES))
        violations.append(Violation(
            "E_PROTO_OUTCOME_LAYER_FIXED", name,
            f"design-only outcome 集与固定集不一致（对称差 {wrong_design}）；design-only 应恰好为"
            f" planner 三项（依赖 {CAPABILITY}）",
        ))
    return violations


def _window_dup_hits(lines: list[str], values: set[str]) -> int:
    """返回首个触发复制判定的窗口起始行号（1-based），未触发返回 0。"""
    for i in range(max(1, len(lines) - ENUM_DUP_WINDOW + 1)):
        found: set[str] = set()
        for ln in lines[i:i + ENUM_DUP_WINDOW]:
            found.update(v for v in re.findall(r"`([a-z][a-z0-9_]*)`", ln) if v in values)
        if len(found) >= ENUM_DUP_THRESHOLD:
            return i + 1
    return 0


def check_derived_doc(path: Path, text: str, spec: ProtocolSpec, *, role: str | None = None) -> list[Violation]:
    """检查派生文档（AGENTS/Skill/user-guide/四模板）的单源纪律与路由/capability guard。"""
    violations: list[Violation] = []
    name = str(path)
    lines = text.splitlines()
    protocol_ref = "role-protocol.md"

    if protocol_ref not in text:
        violations.append(Violation("E_PROTOCOL_REF_MISSING", name, "未引用唯一单源 role-protocol.md"))

    status_hit = _window_dup_hits(lines, set(spec.status_enum))
    if status_hit:
        violations.append(Violation(
            "E_DUP_STATUS_ENUM", name,
            f"第 {status_hit} 行附近 {ENUM_DUP_WINDOW} 行内出现 ≥{ENUM_DUP_THRESHOLD} 个 workflow_status 枚举值：疑似复制状态表，应引用 role-protocol.md §2",
        ))
    outcome_hit = _window_dup_hits(lines, set(spec.outcome_enum))
    if outcome_hit:
        violations.append(Violation(
            "E_DUP_OUTCOME_ENUM", name,
            f"第 {outcome_hit} 行附近 {ENUM_DUP_WINDOW} 行内出现 ≥{ENUM_DUP_THRESHOLD} 个 outcome 枚举值：疑似复制 outcome 列表，应引用 role-protocol.md §5",
        ))

    marker_hits = [m for m in HANDOFF_BLOCK_MARKERS if m in text]
    if len(marker_hits) >= 3:
        violations.append(Violation(
            "E_DUP_HANDOFF_BLOCK", name,
            f"出现 Handoff 字段级标记 {marker_hits}（≥3 个）：疑似内联 Handoff 字段块，应引用 role-protocol.md §5",
        ))

    finding_hits = [f for f in FINDING_FIELDS if f"`{f}`" in text]
    if len(finding_hits) >= FINDING_DUP_THRESHOLD:
        violations.append(Violation(
            "E_DUP_FINDING_SCHEMA", name,
            f"出现 finding 字段 {finding_hits}（≥{FINDING_DUP_THRESHOLD} 个）：疑似复制 finding schema，应引用 role-protocol.md §4",
        ))

    route_re = re.compile(r"(?:交|给|→)\s*Planner")
    for i, line in enumerate(lines):
        if route_re.search(line):
            context = "\n".join(lines[max(0, i - 1):i + 2])
            if not any(tok in context for tok in ROUTE_GUARD_TOKENS):
                violations.append(Violation(
                    "E_ROUTE_PLANNER_NO_CUTOVER", name,
                    f"第 {i + 1} 行的「交/给/→ Planner」路由缺少 cutover 限定（pre/post-cutover、升级用户或双轨）：计划缺陷 pre-cutover 必须升级用户",
                ))
        if "`executor_replan_requested`" in line:
            context = "\n".join(lines[max(0, i - 1):i + 2])
            if not any(tok in context for tok in REPLAN_GUARD_TOKENS):
                violations.append(Violation(
                    "E_REPLAN_NO_CUTOVER", name,
                    f"第 {i + 1} 行引用 `executor_replan_requested` 但未说明 pre-cutover 无法持久化（capability {CAPABILITY}）",
                ))
        if "READY/PLAN" in line:
            context = "\n".join(lines[max(0, i - 1):i + 2])
            if not any(tok in context for tok in RESERVED_GUARD_TOKENS):
                violations.append(Violation(
                    "E_RESERVED_DISPATCH_NO_GUARD", name,
                    f"第 {i + 1} 行引用 `READY/PLAN` 但未标注协议保留（capability {CAPABILITY} 声明前 daemon 不产生）",
                ))

    if role == "planner":
        if "design-only" not in text or CAPABILITY not in text:
            violations.append(Violation(
                "E_PLANNER_DESIGN_ONLY_MISSING", name,
                f"Planner 模板必须包含 design-only 声明与 capability {CAPABILITY}（daemon 未声明前不得作为现行派工入口）",
            ))
    return violations


def git_blob_hash(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def check_archive_blobs(readme_path: Path, archive_dir: Path) -> list[Violation]:
    """校验归档 README 声明的原始 blob id 与归档文件实际字节一致（append-only provenance）。"""
    violations: list[Violation] = []
    if not readme_path.exists():
        return [Violation("E_ARCHIVE_README_MISSING", str(readme_path), "归档 README 不存在，无法执行字节级校验")]
    text = read_text(readme_path)
    rows = re.findall(r"^\|\s*`([^`]+)`\s*\|\s*`([0-9a-f]{40})`\s*\|", text, flags=re.MULTILINE)
    if not rows:
        return [Violation("E_ARCHIVE_README_MISSING", str(readme_path), "归档 README 未声明任何 blob id 表行")]
    for filename, expected in rows:
        target = archive_dir / filename
        if not target.exists():
            violations.append(Violation("E_ARCHIVE_FILE_MISSING", str(target), f"归档文件不存在（README 声明 blob {expected[:12]}…）"))
            continue
        actual = git_blob_hash(target.read_bytes())
        if actual != expected:
            violations.append(Violation(
                "E_ARCHIVE_BLOB_MISMATCH", str(target),
                f"归档被改写或行尾被转换：期望原始 blob {expected[:12]}…，实际 {actual[:12]}…（归档必须字节级原样保存）",
            ))
    return violations


def check_design_docs(v1_path: Path | None, v2_path: Path | None) -> list[Violation]:
    violations: list[Violation] = []
    if v1_path and v1_path.exists():
        # 冻结 v1 是字节级历史基线：只核验固定原始 blob，不要求它包含任何 supersede/
        # role-protocol 指针（指针唯一存在于 v2 amendment；向 v1 追加内容即改写冻结基线）。
        actual = git_blob_hash(v1_path.read_bytes())
        if actual != DESIGN_V1_BLOB:
            violations.append(Violation(
                "E_DESIGN_V1_BLOB_MISMATCH", str(v1_path),
                f"冻结 v1 设计被改写：期望原始 blob {DESIGN_V1_BLOB[:12]}…，实际 {actual[:12]}…；"
                f"supersede 指针只能写入 v2 amendment，不得修改 v1",
            ))
    if v2_path and v2_path.exists():
        text = read_text(v2_path)
        anchors = (CAPABILITY, "双轨", "Supersede 映射", "design-only", DESIGN_V1_BLOB)
        for token in anchors:
            if token not in text:
                violations.append(Violation(
                    "E_DESIGN_V2_ANCHOR_MISSING", str(v2_path), f"v2 amendment 缺少关键锚点：{token}",
                ))
    return violations


ROLE_BY_NAME = {
    "planner": "planner",
    "executor": "executor",
    "reviewer": "reviewer",
    "adjudicator": "adjudicator",
}


def detect_role(path: Path) -> str | None:
    lower = path.name.lower()
    for key, role in ROLE_BY_NAME.items():
        if key in lower:
            return role
    return None


def run_checks(
    protocol: Path,
    agents: Path,
    skill: Path | None,
    user_guide: Path | None,
    templates: list[Path],
    design_v1: Path | None,
    design_v2: Path | None,
    archive_readme: Path | None,
    archive_dir: Path | None,
) -> list[Violation]:
    violations: list[Violation] = []
    text = read_text(protocol)
    spec, parse_errors = parse_protocol(text)
    violations.extend(parse_errors)
    if spec is None:
        return violations
    violations.extend(check_protocol_internal(spec, protocol))

    for path, role in ((agents, None), (skill, None), (user_guide, None)):
        if path and path.exists():
            violations.extend(check_derived_doc(path, read_text(path), spec, role=role))
    for template in templates:
        if not template.exists():
            violations.append(Violation("E_TEMPLATE_MISSING", str(template), "模板文件不存在"))
            continue
        role = detect_role(template)
        if role is None:
            violations.append(Violation("E_TEMPLATE_ROLE_UNKNOWN", str(template), "无法从文件名识别角色"))
            continue
        violations.extend(check_derived_doc(template, read_text(template), spec, role=role))

    violations.extend(check_design_docs(design_v1, design_v2))
    if archive_readme:
        violations.extend(check_archive_blobs(archive_readme, archive_dir or archive_readme.parent / "legacy"))
    return violations


# ---------------------------------------------------------------- self-test ---

SELF_TEST_CASES: list[tuple[str, str]] = [
    ("proto_enum_dup", "E_PROTO_ENUM_DUP"),
    ("proto_layer_gap", "E_PROTO_LAYER_MISMATCH"),
    ("proto_handoff_order", "E_PROTO_HANDOFF_ORDER"),
    ("proto_outcome_layer", "E_PROTO_OUTCOME_LAYER_MISMATCH"),
    ("dup_status_enum", "E_DUP_STATUS_ENUM"),
    ("dup_outcome_enum", "E_DUP_OUTCOME_ENUM"),
    ("dup_handoff_block", "E_DUP_HANDOFF_BLOCK"),
    ("dup_finding_schema", "E_DUP_FINDING_SCHEMA"),
    ("planner_route_no_guard", "E_ROUTE_PLANNER_NO_CUTOVER"),
    ("replan_no_guard", "E_REPLAN_NO_CUTOVER"),
    ("ready_plan_no_guard", "E_RESERVED_DISPATCH_NO_GUARD"),
    ("planner_design_only_missing", "E_PLANNER_DESIGN_ONLY_MISSING"),
    ("protocol_ref_missing", "E_PROTOCOL_REF_MISSING"),
    ("archive_blob_mismatch", "E_ARCHIVE_BLOB_MISMATCH"),
    # 复审漏检回归：删枚举（execution_ready）后校验必须报错，不得静默通过。
    ("proto_required_status_missing", "E_PROTO_REQUIRED_STATUS_MISSING"),
    # 复审漏检回归：outcome 分区成立但分层错误（planner outcome 混入已实现）必须报错。
    ("proto_outcome_fixed", "E_PROTO_OUTCOME_LAYER_FIXED"),
    # 复审漏检回归：把 queued 从已实现层搬到保留层（分区仍完整）必须报错。
    ("proto_status_layer_fixed", "E_PROTO_IMPLEMENTED_FIXED"),
    # 冻结 v1 是字节级基线：任何改写（含追加 supersede 指针）都必须报错。
    ("design_v1_blob_mismatch", "E_DESIGN_V1_BLOB_MISMATCH"),
]

ENUM_LIST_SNIPPET = "`queued`、`planning_pending`、`execution_ready`、`remediation_pending`、`review_pending`、`completed`、`governance_blocked`。"
OUTCOME_LIST_SNIPPET = "`executor_ready_for_review`、`executor_blocked_to_user`、`reviewer_pass`、`reviewer_blocked`、`adjudicator_accepted`、`adjudicator_returned`、`planner_ready_for_execution`。"
HANDOFF_BLOCK_SNIPPET = (
    "Handoff:\n  task_id: T-1\n  step_id: null\n  from_role: executor\n  outcome: executor_ready_for_review\n"
    "  next_role: reviewer\n  next_action: review\n  reason: done\n  independence_requirement: required\n"
)
FINDING_SCHEMA_SNIPPET = (
    "finding 字段：`finding_id`、`severity`、`scope`、`root_cause`、`reproduction`、`minimal_fix`、`owner_route`、`blocking`。"
)


def _break_protocol(raw: str, case: str) -> str:
    if case == "proto_enum_dup":
        return raw.replace("`queued`、`planning_pending`", "`queued`、`queued`、`planning_pending`", 1)
    if case == "proto_layer_gap":
        return raw.replace("`completed`、`reverted`、`governance_blocked`", "`completed`、`governance_blocked`", 1)
    if case == "proto_handoff_order":
        return raw.replace(
            "  task_id: <daemon next-action 返回的精确任务 ID>\n  step_id: <相关步骤 ID 或 null>",
            "  step_id: <相关步骤 ID 或 null>\n  task_id: <daemon next-action 返回的精确任务 ID>", 1,
        )
    if case == "proto_outcome_layer":
        return raw.replace(
            "`planner_ready_for_execution`、\n  `planner_replan_required`、`executor_replan_requested`",
            "`planner_ready_for_execution`", 1,
        )
    if case == "proto_required_status_missing":
        # 模拟复审操作：从枚举删掉 execution_ready（保留值标记行不动）。
        return raw.replace("`execution_ready`、`execution_in_progress`", "`execution_in_progress`", 1)
    if case == "proto_outcome_fixed":
        # 分区仍完整（planner_ready_for_execution 移入已实现、从 design-only 语义上仍并列），
        # 但已实现集与固定六项不一致，固定分层断言必须报错。
        return raw.replace(
            "`adjudicator_accepted`、\n  `adjudicator_returned`；",
            "`adjudicator_accepted`、\n  `planner_ready_for_execution`；", 1,
        )
    if case == "proto_status_layer_fixed":
        # 复审漏检回归：把一个已实现状态（governance_blocked）搬到保留层（分区仍完整、
        # 枚举不变），固定分层断言（E_PROTO_IMPLEMENTED_FIXED）必须报错，不能因分区完整
        # 而误报 PASS。选 governance_blocked 而非 queued：queued 在「open 任务投影为
        # queued」括注里以反引号二次出现，仅删枚举首项仍会被括注回填进已实现集。
        raw = raw.replace(
            "`reverted`、`governance_blocked`、`unknown`（`workflow_status_for()`",
            "`reverted`、`unknown`（`workflow_status_for()`", 1,
        )
        raw = raw.replace(
            "- **协议保留值（daemon 不 emit；归属见顶部 capability 分层）**：`execution_ready`、",
            "- **协议保留值（daemon 不 emit；归属见顶部 capability 分层）**：`governance_blocked`、`execution_ready`、", 1,
        )
        return raw
    return raw


def _break_doc(raw: str, case: str) -> str:
    if case == "dup_status_enum":
        return raw + "\n\n状态表：\n" + ENUM_LIST_SNIPPET + "\n"
    if case == "dup_outcome_enum":
        return raw + "\n\noutcome 列表：\n" + OUTCOME_LIST_SNIPPET + "\n"
    if case == "dup_handoff_block":
        return raw + "\n\n" + HANDOFF_BLOCK_SNIPPET + "\n"
    if case == "dup_finding_schema":
        return raw + "\n\n" + FINDING_SCHEMA_SNIPPET + "\n"
    if case == "planner_route_no_guard":
        return raw + "\n计划边界有缺陷时直接交 Planner 处理。\n"
    if case == "replan_no_guard":
        return raw + "\n复杂度不成立时提交 `executor_replan_requested`。\n"
    if case == "ready_plan_no_guard":
        return raw + "\n收到 `READY/PLAN` 后开始规划。\n"
    if case == "planner_design_only_missing":
        return re.sub(r"> \*\*design-only 声明：\*\*.*?\n\n", "", raw, flags=re.DOTALL)
    if case == "protocol_ref_missing":
        return raw.replace("role-protocol.md", "protocol.md")
    return raw


def run_self_test(root: Path) -> tuple[int, int]:
    protocol = DEFAULT_PROTOCOL
    agents = DEFAULT_AGENTS
    executor_tpl = root / "Callwarden 无人值守循环启动模板：Executor v4.md"
    planner_tpl = root / "Callwarden 无人值守循环启动模板：Planner v1.md"
    protocol_raw = read_text(protocol)
    spec, _ = parse_protocol(protocol_raw)
    if spec is None:
        print("self-test 失败：真实协议无法解析，先修复协议锚点")
        return 0, 1

    passed, failed = 0, 0
    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        for case, expected_code in SELF_TEST_CASES:
            errors: list[str] = []
            if case.startswith("proto_"):
                broken = _break_protocol(protocol_raw, case)
                broken_spec, parse_errs = parse_protocol(broken)
                if broken_spec is None:
                    errors = [v.code for v in parse_errs]
                else:
                    errors = [v.code for v in check_protocol_internal(broken_spec, protocol)]
            elif case == "archive_blob_mismatch":
                readme = tmp_dir / "README.md"
                readme.write_text(
                    "| 归档文件 | 原始 blob id |\n|---|---|\n| `fake.md` | `" + "0" * 40 + "` |\n",
                    encoding="utf-8",
                )
                (tmp_dir / "fake.md").write_text("not the original bytes\n", encoding="utf-8", newline="")
                errors = [v.code for v in check_archive_blobs(readme, tmp_dir)]
            elif case == "design_v1_blob_mismatch":
                tampered = tmp_dir / "design_v1_tampered.md"
                tampered.write_bytes(
                    read_text(DEFAULT_DESIGN_V1).encode("utf-8")
                    + "> Supersede: 本文档已被 v2 amendment 取代\n".encode("utf-8")
                )
                errors = [v.code for v in check_design_docs(tampered, None)]
            elif case == "planner_design_only_missing":
                broken = _break_doc(read_text(planner_tpl), case)
                target = tmp_dir / "planner.md"
                target.write_text(broken, encoding="utf-8")
                errors = [v.code for v in check_derived_doc(target, broken, spec, role="planner")]
            elif case == "protocol_ref_missing":
                broken = _break_doc(read_text(executor_tpl), case)
                target = tmp_dir / "executor.md"
                target.write_text(broken, encoding="utf-8")
                errors = [v.code for v in check_derived_doc(target, broken, spec, role="executor")]
            else:
                broken = _break_doc(read_text(executor_tpl), case)
                target = tmp_dir / "executor.md"
                target.write_text(broken, encoding="utf-8")
                errors = [v.code for v in check_derived_doc(target, broken, spec, role="executor")]
            if expected_code in errors:
                passed += 1
                print(f"  PASS {case} -> {expected_code}")
            else:
                failed += 1
                print(f"  FAIL {case}: 期望 {expected_code}，实际 {sorted(set(errors)) or '（无错误，误报 PASS）'}")
    print(f"self-test: {passed} 通过, {failed} 失败（共 {len(SELF_TEST_CASES)} 用例）")
    return passed, failed


def main() -> int:
    parser = argparse.ArgumentParser(description="结构化校验角色治理文档与 role-protocol.md 单源一致性")
    parser.add_argument("--agents", type=Path, default=DEFAULT_AGENTS)
    parser.add_argument("--protocol", type=Path, default=DEFAULT_PROTOCOL)
    parser.add_argument("--skill", type=Path, default=DEFAULT_SKILL)
    parser.add_argument("--user-guide", type=Path, default=DEFAULT_USER_GUIDE)
    parser.add_argument("--design", type=Path, default=DEFAULT_DESIGN_V1, help="冻结 v1 设计")
    parser.add_argument("--design-v2", type=Path, default=DEFAULT_DESIGN_V2, help="v2 amendment")
    parser.add_argument("--template", action="append", type=Path, default=None, help="角色模板（可多次）")
    parser.add_argument("--archive-readme", type=Path, default=DEFAULT_ARCHIVE_README)
    parser.add_argument("--archive-dir", type=Path, default=DEFAULT_ARCHIVE_DIR)
    parser.add_argument("--self-test", action="store_true", help="负向自测：故意破坏副本，断言每个检查项报错")
    args = parser.parse_args()

    if args.self_test:
        _, failed = run_self_test(REPO_ROOT)
        return 1 if failed else 0

    templates = args.template or [
        REPO_ROOT / "Callwarden 无人值守循环启动模板：Planner v1.md",
        REPO_ROOT / "Callwarden 无人值守循环启动模板：Executor v4.md",
        REPO_ROOT / "Callwarden 无人值守循环启动模板：Reviewer v4.md",
        REPO_ROOT / "Callwarden 无人值守循环启动模板：Adjudicator v4.md",
    ]
    violations = run_checks(
        protocol=args.protocol,
        agents=args.agents,
        skill=args.skill,
        user_guide=args.user_guide,
        templates=templates,
        design_v1=args.design,
        design_v2=args.design_v2,
        archive_readme=args.archive_readme,
        archive_dir=args.archive_dir,
    )
    if violations:
        print(f"结构化合规检查失败（{len(violations)} 项）")
        for v in violations:
            print(v)
        return 1
    print(f"结构化合规检查通过：协议单源、{len(templates)} 个角色模板、Skill/user-guide、设计 supersede 与归档 blob 均一致")
    return 0


if __name__ == "__main__":
    sys.exit(main())
