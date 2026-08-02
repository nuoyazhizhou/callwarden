"""Degraded_Mode 操作分类与分流策略（Req 14.27–14.30, 14.34–14.37）。

等待窗口耗尽仍未建连即进入 Degraded_Mode，按 class(op) 分流：
- read_only: 直连只读连接执行 [Req 14.28]
- Index_Write: 直连写入 [Req 14.29]
- Governance_Write: fail closed，返回 Structured_Reason [Req 14.30]

class(op) 对同一操作恒定，不随 degraded 取值、重试次数或调用方变化 [Req 14.34]。
跨类操作按组成部分分级：components(op) 拆分 [Req 14.35]。
状态推进只挂在 Governance_Write 组成部分的成功路径上 [Req 14.36]。
Structured_Reason 标识已执行/被拒组成部分 [Req 14.37]。

所有权：本文件（server/degraded_mode.py）。
设计参考：docs/design/multi-llm-contract-driven-collaboration-design.md §13.5.7
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, FrozenSet, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# 操作分类 [Req 14.27, 14.34]
# ---------------------------------------------------------------------------


class OperationClass(Enum):
    """操作类别——对同一操作恒定，不随上下文变化 [Req 14.34]。"""

    READ_ONLY = "read_only"
    INDEX_WRITE = "index_write"
    GOVERNANCE_WRITE = "governance_write"


# Governance_Write = Protected_Mutation 全集 [Req 14.30]
# 与 rust_ext/src/daemon/dispatch.rs PROTECTED_MUTATION_METHODS 保持一致
GOVERNANCE_WRITE_OPS: FrozenSet[str] = frozenset([
    "snapshot.publish",
    "workspace.file.refresh",
    "workspace.recover",
    "backup",
    "restore",
    "verdict.submit",
    "reveal.submit",
    "evidence.append",
    "gate.decide",
    "task.apply",
    "task.close",
    "lease.acquire",
    "lease.release",
    "lease.extend",
])

# Index_Write：更新索引但不承载授权语义的写操作 [Req 14.29]
INDEX_WRITE_OPS: FrozenSet[str] = frozenset([
    "workspace.register",
    "workspace.file.update",
    "graph.refresh",
    "symbol.update",
    "parse.result.commit",
    "generation.record",
])

# read_only：只读查询 [Req 14.28]
# 不在此集合中的未知操作默认归类为 read_only（保守策略：不拒绝查询）


def classify_operation(method: str) -> OperationClass:
    """对操作进行三分类 [Req 14.27, 14.34]。

    分类对同一操作恒定，不随 degraded 取值、重试次数或调用方变化。
    """
    if method in GOVERNANCE_WRITE_OPS:
        return OperationClass.GOVERNANCE_WRITE
    if method in INDEX_WRITE_OPS:
        return OperationClass.INDEX_WRITE
    return OperationClass.READ_ONLY


# ---------------------------------------------------------------------------
# 跨类操作组成部分拆分 [Req 14.35]
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OperationComponent:
    """操作的组成部分。"""

    name: str
    op_class: OperationClass


# 跨类操作的组成部分注册表
# 新增用户可见入口一律按组成部分分类 [Req 14.35]
_COMPONENT_REGISTRY: Dict[str, List[OperationComponent]] = {
    # workspace.file.refresh 是跨类操作：索引刷新 + 治理记录
    "workspace.file.refresh": [
        OperationComponent("index_refresh", OperationClass.INDEX_WRITE),
        OperationComponent("governance_record", OperationClass.GOVERNANCE_WRITE),
    ],
    # snapshot.publish 是跨类操作：索引发布 + 治理封存
    "snapshot.publish": [
        OperationComponent("index_publish", OperationClass.INDEX_WRITE),
        OperationComponent("governance_seal", OperationClass.GOVERNANCE_WRITE),
    ],
}


def components(method: str) -> List[OperationComponent]:
    """拆分操作为组成部分 [Req 14.35]。

    单一类别的操作返回单元素列表。
    跨类操作返回多元素列表，分级判定作用于组成部分而非整个入口。
    """
    if method in _COMPONENT_REGISTRY:
        return _COMPONENT_REGISTRY[method]

    # 单一类别操作
    op_class = classify_operation(method)
    return [OperationComponent(name=method, op_class=op_class)]


# ---------------------------------------------------------------------------
# Structured_Reason [Req 14.30, 14.37]
# ---------------------------------------------------------------------------


@dataclass
class StructuredReason:
    """结构化拒绝/降级原因。

    携带稳定错误码、i18n message key 与可执行恢复指引 [Req 14.30]。
    标识已执行/被拒组成部分 [Req 14.37]。
    """

    # 稳定错误码（文案变化不改变码值）
    code: str
    # i18n message key（zh_CN 与 en_US 均可解析）
    message_key: str
    # 可执行恢复指引（平台相关的 daemon 拉起命令）
    recovery_guidance: str
    # 已执行的组成部分
    executed_components: List[str] = field(default_factory=list)
    # 被拒的组成部分
    rejected_components: List[str] = field(default_factory=list)
    # 附加上下文
    context: Dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> Dict:
        """序列化为字典（用于 JSON-RPC 响应）。"""
        return {
            "code": self.code,
            "message_key": self.message_key,
            "recovery_guidance": self.recovery_guidance,
            "executed_components": self.executed_components,
            "rejected_components": self.rejected_components,
            "context": self.context,
        }


# ---------------------------------------------------------------------------
# 分流策略 [Req 14.27–14.30]
# ---------------------------------------------------------------------------


@dataclass
class DegradedRoutingDecision:
    """Degraded_Mode 分流决策。"""

    # 是否允许执行
    allowed: bool
    # 操作类别
    op_class: OperationClass
    # 执行模式
    mode: str  # "direct_read", "direct_write", "fail_closed"
    # 拒绝原因（仅 fail_closed 时有值）
    reason: Optional[StructuredReason] = None
    # 已执行的组成部分（跨类操作部分执行时）
    executed_components: List[str] = field(default_factory=list)
    # 被拒的组成部分
    rejected_components: List[str] = field(default_factory=list)


def route_degraded(
    method: str,
    endpoint: str,
    platform: str = "linux",
) -> DegradedRoutingDecision:
    """Degraded_Mode 下的分流决策 [Req 14.27–14.30]。

    Args:
        method: RPC 方法名
        endpoint: Daemon_Endpoint（用于恢复指引）
        platform: 平台标识（linux/macos/windows）

    Returns:
        分流决策，包含是否允许、执行模式、拒绝原因等。
    """
    comps = components(method)

    # 检查是否有 Governance_Write 组成部分
    governance_comps = [c for c in comps if c.op_class == OperationClass.GOVERNANCE_WRITE]
    index_comps = [c for c in comps if c.op_class == OperationClass.INDEX_WRITE]
    read_comps = [c for c in comps if c.op_class == OperationClass.READ_ONLY]

    # 纯只读：直连只读连接 [Req 14.28]
    if not governance_comps and not index_comps:
        return DegradedRoutingDecision(
            allowed=True,
            op_class=OperationClass.READ_ONLY,
            mode="direct_read",
        )

    # 纯 Index_Write（无 Governance 部分）：直连写入 [Req 14.29]
    if not governance_comps and index_comps:
        return DegradedRoutingDecision(
            allowed=True,
            op_class=OperationClass.INDEX_WRITE,
            mode="direct_write",
        )

    # 跨类操作：Index_Write 部分可执行，Governance_Write 部分 fail closed [Req 14.35]
    if governance_comps and (index_comps or read_comps):
        executed = [c.name for c in index_comps + read_comps]
        rejected = [c.name for c in governance_comps]
        return DegradedRoutingDecision(
            allowed=False,
            op_class=OperationClass.GOVERNANCE_WRITE,
            mode="fail_closed",
            reason=_make_governance_rejection(method, endpoint, platform, executed, rejected),
            executed_components=executed,
            rejected_components=rejected,
        )

    # 纯 Governance_Write：fail closed [Req 14.30]
    rejected = [c.name for c in governance_comps]
    return DegradedRoutingDecision(
        allowed=False,
        op_class=OperationClass.GOVERNANCE_WRITE,
        mode="fail_closed",
        reason=_make_governance_rejection(method, endpoint, platform, [], rejected),
        rejected_components=rejected,
    )


def _make_governance_rejection(
    method: str,
    endpoint: str,
    platform: str,
    executed: List[str],
    rejected: List[str],
) -> StructuredReason:
    """构造 Governance_Write 拒绝的 Structured_Reason [Req 14.30]。

    恢复指引给出该平台的具体的 daemon 拉起命令与端点位置。
    """
    recovery = _platform_recovery_guidance(endpoint, platform)

    return StructuredReason(
        code="E_GOVERNANCE_WRITE_DEGRADED",
        message_key="error.governance_write_degraded",
        recovery_guidance=recovery,
        executed_components=executed,
        rejected_components=rejected,
        context={
            "method": method,
            "endpoint": endpoint,
            "platform": platform,
        },
    )


def _platform_recovery_guidance(endpoint: str, platform: str) -> str:
    """生成平台相关的可执行恢复指引 [Req 14.30]。

    给出具体拉起命令，而不是泛化的"数据库正忙"。
    """
    if platform == "windows":
        return (
            f"daemon 未运行。请执行: cw_daemon.exe --socket \"{endpoint}\" "
            f"或确认 Windows 服务 CallWarden Daemon 已启动。"
            f"端点: {endpoint}"
        )
    elif platform == "macos":
        return (
            f"daemon 未运行。请执行: launchctl start com.callwarden.daemon "
            f"或手动运行: cw_daemon --socket \"{endpoint}\"。"
            f"端点: {endpoint}"
        )
    else:  # linux
        return (
            f"daemon 未运行。请执行: systemctl --user start callwarden-daemon.service "
            f"或手动运行: cw_daemon --socket \"{endpoint}\"。"
            f"端点: {endpoint}"
        )


# ---------------------------------------------------------------------------
# 状态推进约束 [Req 14.36]
# ---------------------------------------------------------------------------


def can_advance_state(method: str, degraded: bool) -> bool:
    """判断操作是否可以推进任务/步骤状态 [Req 14.36]。

    状态推进只挂在 Governance_Write 组成部分的成功路径上。
    Degraded_Mode 下 Index_Write 组成部分成功时，任务与步骤状态从未被写入。
    """
    if not degraded:
        # 正常模式下，所有操作按原有逻辑推进
        return True

    # Degraded_Mode 下，只有 Governance_Write 成功才推进状态
    # 但 Governance_Write 在 Degraded_Mode 下是 fail closed 的，
    # 所以 Degraded_Mode 下永远不会推进状态
    op_class = classify_operation(method)
    return op_class != OperationClass.GOVERNANCE_WRITE and False


def produces_evidence_or_gate(method: str, degraded: bool) -> bool:
    """判断操作是否产生 Evidence 或 gate decision [Req 14.36]。

    Degraded_Mode 下索引刷新成功不得被解释为门禁通过或条款满足。
    """
    if not degraded:
        return method in GOVERNANCE_WRITE_OPS

    # Degraded_Mode 下不产生任何 Evidence 与 gate decision
    return False
