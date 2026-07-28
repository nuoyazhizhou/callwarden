"""MigrationManifestService —— 生产代码访问迁移 manifest 的查询服务（Phase 0 Step 4）

设计文档：docs/design/rust-full-migration-self-bootstrap-plan.md §4 Step 4
真相源：docs/design/migration-manifest.md

本模块是生产代码（cli/mcp_server/其他 server 模块）与 migration-manifest.md
之间的查询入口。它解析 manifest 第 7 节迁移状态跟踪表，提供按 phase / feature
查询的 API。

设计原则：
    - 只读：本服务只读取 manifest.md，不修改
    - 无状态：每次查询重新读取文件，不缓存（manifest 更新后立即可见）
    - 无锁：manifest.md 是文档文件，无 SQLite 锁冲突
    - 可配置：通过 CW_MIGRATION_MANIFEST_PATH 覆盖默认路径

错误语义：
    - manifest.md 不存在 → 返回空结果 + warning（不抛异常，避免阻塞生产）
    - 解析失败 → 返回部分结果 + warning
    - 查询无匹配 → 返回空列表
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# manifest.md 默认路径（项目根目录 / docs / design / migration-manifest.md）
_DEFAULT_MANIFEST_PATH = (
    Path(__file__).resolve().parent.parent / "docs" / "design" / "migration-manifest.md"
)


def _get_manifest_path() -> Path:
    """返回 manifest.md 路径（可通过 CW_MIGRATION_MANIFEST_PATH 覆盖）。"""
    env_path = os.environ.get("CW_MIGRATION_MANIFEST_PATH")
    if env_path:
        return Path(env_path)
    return _DEFAULT_MANIFEST_PATH


@dataclass
class MigrationStepStatus:
    """单个功能子任务的 7 个步骤状态。"""

    contract: str = "🔴"
    implement: str = "🔴"
    differential_test: str = "🔴"
    wire_production: str = "🔴"
    verify: str = "🔴"
    refresh: str = "🔴"
    review: str = "🔴"

    def done_count(self) -> int:
        """已完成步骤数（✅）。"""
        return sum(1 for s in self._as_tuple() if s == "✅")

    def _as_tuple(self) -> tuple:
        return (
            self.contract,
            self.implement,
            self.differential_test,
            self.wire_production,
            self.verify,
            self.refresh,
            self.review,
        )

    def progress(self) -> int:
        """完成进度百分比（0-100）。"""
        return (self.done_count() * 100) // 7


@dataclass
class MigrationFeature:
    """单个功能子任务的迁移状态。"""

    phase: int
    feature: str
    task_id: str = ""
    steps: MigrationStepStatus = field(default_factory=MigrationStepStatus)

    def is_complete(self) -> bool:
        return self.steps.done_count() == 7

    def ready_for_review(self) -> bool:
        return (
            self.steps.done_count() == 6
            and self.steps.review == "⏸️"
        )


def _parse_status_table(content: str) -> List[MigrationFeature]:
    """解析 manifest.md 第 7 节迁移状态跟踪表。

    表格格式：
    | Phase | 功能子任务 | contract | implement | differential-test | wire-production | verify | refresh | review |
    |---|---|---|---|---|---|---|---|---|
    | 0 | 迁移 manifest 与生产调用链盘点 | 🟡 本文 | N/A | N/A | N/A | N/A | N/A | ⏸️ |
    """
    # 匹配第 7 节到第 8 节之间的内容
    section_match = re.search(
        r"## 7\. 迁移状态跟踪表(.*?)## 8\.",
        content,
        re.DOTALL,
    )
    if not section_match:
        return []

    table_content = section_match.group(1)
    features: List[MigrationFeature] = []

    for line in table_content.splitlines():
        line = line.strip()
        if not line.startswith("|"):
            continue
        if "Phase" in line or "---" in line:
            continue
        # 解析数据行：| 0 | 迁移 manifest 与生产调用链盘点 | 🟡 本文 | N/A | ... |
        cells = [c.strip() for c in line.split("|")]
        # split 后首尾是空字符串
        cells = [c for c in cells if c != ""]
        if len(cells) < 9:
            continue
        # cells[0] = phase, cells[1] = feature, cells[2..8] = 7 个步骤状态
        try:
            phase = int(cells[0])
        except ValueError:
            continue
        feature = cells[1]
        # 提取 task_id（如果有）
        task_id = ""
        task_match = re.search(r"T-[\w-]+", feature)
        if task_match:
            task_id = task_match.group(0)
        # 步骤状态：取每列的 emoji 前缀（可能是 🔴/🟡/✅/⏸️）
        step_emojis = []
        for i in range(2, 9):
            cell = cells[i] if i < len(cells) else ""
            emoji = _extract_status_emoji(cell)
            step_emojis.append(emoji)

        features.append(
            MigrationFeature(
                phase=phase,
                feature=feature,
                task_id=task_id,
                steps=MigrationStepStatus(
                    contract=step_emojis[0],
                    implement=step_emojis[1],
                    differential_test=step_emojis[2],
                    wire_production=step_emojis[3],
                    verify=step_emojis[4],
                    refresh=step_emojis[5],
                    review=step_emojis[6],
                ),
            )
        )
    return features


def _extract_status_emoji(cell: str) -> str:
    """从表格单元格提取状态 emoji。"""
    if "🔴" in cell:
        return "🔴"
    if "🟡" in cell:
        return "🟡"
    if "✅" in cell:
        return "✅"
    if "⏸️" in cell or "⏸" in cell:
        return "⏸️"
    # N/A 或其他 → 视为未开始
    return "🔴"


class MigrationManifestService:
    """迁移 manifest 查询服务（只读、无状态、无锁）。

    用法：
        service = MigrationManifestService()
        for feature in service.list_features(phase=0):
            print(f"{feature.feature}: {feature.steps.progress()}%")
        overall = service.overall_progress()
    """

    def __init__(self, manifest_path: Optional[Path] = None) -> None:
        self.path = manifest_path or _get_manifest_path()

    def _read(self) -> str:
        """读取 manifest.md（不存在时返回空字符串）。"""
        if not self.path.exists():
            return ""
        return self.path.read_text(encoding="utf-8")

    def list_features(self, phase: Optional[int] = None) -> List[MigrationFeature]:
        """列出功能子任务（可选按 phase 过滤）。"""
        features = _parse_status_table(self._read())
        if phase is not None:
            features = [f for f in features if f.phase == phase]
        return features

    def get_feature(self, feature_name: str) -> Optional[MigrationFeature]:
        """查询指定功能子任务的迁移状态。"""
        for f in self.list_features():
            if feature_name in f.feature:
                return f
        return None

    def overall_progress(self) -> int:
        """整体完成进度百分比（0-100）。"""
        features = self.list_features()
        if not features:
            return 0
        total_steps = len(features) * 7
        done_steps = sum(f.steps.done_count() for f in features)
        return (done_steps * 100) // total_steps

    def phase_progress(self, phase: int) -> int:
        """指定 Phase 的完成进度百分比（0-100）。"""
        features = self.list_features(phase=phase)
        if not features:
            return 0
        total_steps = len(features) * 7
        done_steps = sum(f.steps.done_count() for f in features)
        return (done_steps * 100) // total_steps

    def ready_for_review(self) -> List[MigrationFeature]:
        """返回所有待 review 的功能子任务。"""
        return [f for f in self.list_features() if f.ready_for_review()]

    def completed(self) -> List[MigrationFeature]:
        """返回所有已完成的功能子任务。"""
        return [f for f in self.list_features() if f.is_complete()]
