"""
db.py
=====

代码知识图谱数据库主入口。

通过 Mixin 模式组合各功能模块：
- CodeGraphBase: 核心基类（数据库连接、schema 迁移、工作区管理）
- BuildMixin: 构建功能（文件扫描、解析、调用图构建）
- QueryMixin: 查询功能（符号查询、状态统计、模块图）
- CommentMixin: 注释恢复功能
- GitMixin: Git 集成功能
- MetricsMixin: 代码度量（圈复杂度、耦合度、健康检查）
- SummaryMixin: 代码摘要与项目简报（AI 摘要版本化、仓库地图）
- VectorMixin: 向量嵌入与语义搜索（jina/ollama 嵌入、余弦相似度）
- OwnershipMixin: 文件所有权分析（CODEOWNERS + git blame）
- TaskMixin: 任务驱动 MCP（任务/步骤/变更审计）
- CallChainMixin: 调用链分析（来自 analyzers）
- IssueAnalyzerMixin: 缺陷检测（来自 analyzers）
- CoverageMixin: 覆盖率统计与智能分析（LCOV/Cobertura 导入、函数级覆盖率、测试影响选择）
- GuardrailMixin: 生产安全护栏（DB/API/Incident 三类可阻断规则扫描，编辑前检查）
- ImpactMixin: 变更影响智能（blast_radius、跨层影响分析、review readiness 报告）
- EvolutionMixin: 代码演化智能（函数变更频率、缺陷关联、热点评分、churn 分析）
- DefectKbMixin: 缺陷知识库（缺陷模式搜索、修复建议、从历史修复学习缺陷模式）
- TokenSavingsMixin: Token 节省账本（记录每次操作的 token 节省，宣传利器 + 优化依据）
- BranchMixin: 分支感知图谱（独立工作区方案，分支注册/差异/切换/合并预览）
- EditSafetyMixin: 安全文件编辑（propose_edit hash 校验 + 原子写入 + 审计日志）
- CrossRepoMixin: 跨仓库分析（依赖检测、共享符号、跨仓库影响传播）
- LspMixin: LSP 集成（hover/definition/references/diagnostics/completion）
- GCMixin: 代码图谱 GC（归档被 .gitignore/.callwardenignore 命中的文件，类 Java GC 分代回收）
- TaskAttributionMixin: 任务-符号变更归因（task/step/edit 到 symbol version 的解释层）
"""

from __future__ import annotations

from typing import Optional

from ..config import DB_PATH
from .db_base import CodeGraphBase
from .db_build import BuildMixin
from .db_query import QueryMixin
from .db_comment import CommentMixin
from .db_git import GitMixin
from .db_metrics import MetricsMixin
from .db_summary import SummaryMixin
from .db_tasks import TaskMixin
from .db_vector import VectorMixin
from .db_ownership import OwnershipMixin
from .db_coverage import CoverageMixin
from .db_guardrail import GuardrailMixin
from .db_impact import ImpactMixin
from .db_evolution import EvolutionMixin
from .db_defect_kb import DefectKbMixin
from .db_token_savings import TokenSavingsMixin
from .db_branch import BranchMixin
from .db_edit import EditSafetyMixin
from .db_cross_repo import CrossRepoMixin
from .db_lsp import LspMixin
from .db_check_gate import CheckGateMixin
from .db_gc import GCMixin
from .db_stdlib import StdlibMixin
from .db_external import ExternalMixin
from .db_task_attribution import TaskAttributionMixin
from ..analyzers import CallChainMixin, IssueAnalyzerMixin


class CodeGraphDB(
    CodeGraphBase,
    BuildMixin,
    QueryMixin,
    CommentMixin,
    GitMixin,
    MetricsMixin,
    SummaryMixin,
    VectorMixin,
    OwnershipMixin,
    TaskMixin,
    CallChainMixin,
    IssueAnalyzerMixin,
    CoverageMixin,
    GuardrailMixin,
    ImpactMixin,
    EvolutionMixin,
    DefectKbMixin,
    TokenSavingsMixin,
    BranchMixin,
    EditSafetyMixin,
    CrossRepoMixin,
    LspMixin,
    CheckGateMixin,
    GCMixin,
    StdlibMixin,
    ExternalMixin,
    TaskAttributionMixin,
):
    """代码知识图谱数据库

    整合所有功能模块的主类，提供统一的访问接口。
    """

    def __init__(self, db_path: str = "", workspace_root: Optional[str] = None):
        """初始化代码知识图谱数据库，委托父类完成连接与工作区初始化

        Args:
            db_path: SQLite 数据库文件路径，为空时使用默认路径
            workspace_root: 工作区根目录路径，为空时使用当前工作目录
        """
        super().__init__(db_path=db_path, workspace_root=workspace_root)
