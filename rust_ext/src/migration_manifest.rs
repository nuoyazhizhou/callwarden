//! Migration Manifest —— 迁移状态程序化基线（Phase 0 Step 2）
//!
//! 本模块定义迁移状态的数据结构和 trait，作为后续每个功能子任务 contract 步骤的
//! 程序化基线。对应的真相源是 `docs/design/migration-manifest.md`。
//!
//! 设计原则：
//! - 只定义数据结构和 trait 契约，不实现复杂业务逻辑
//! - MigrationStatus 枚举对应 manifest 第 7 节迁移状态跟踪表的状态标记
//! - 后续功能子任务实现 service trait 时，可通过本模块查询迁移状态
//! - 不持久化：状态真相源在 manifest.md + cw 任务树，本模块只提供内存查询接口

use std::collections::HashMap;

/// 迁移状态枚举（对应 manifest.md 第 7 节状态标记）
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash)]
pub enum MigrationStatus {
    /// 未开始
    NotStarted,
    /// 部分完成
    Partial,
    /// 已完成
    Done,
    /// 待 review
    PendingReview,
}

impl MigrationStatus {
    /// 从 manifest.md 表格中的 emoji 标记解析
    pub fn from_marker(marker: &str) -> Self {
        match marker.trim() {
            "🔴" => MigrationStatus::NotStarted,
            "🟡" => MigrationStatus::Partial,
            "✅" => MigrationStatus::Done,
            "⏸️" | "⏸" => MigrationStatus::PendingReview,
            _ => MigrationStatus::NotStarted,
        }
    }

    /// 转为 manifest.md 表格中的 emoji 标记
    pub fn to_marker(self) -> &'static str {
        match self {
            MigrationStatus::NotStarted => "🔴",
            MigrationStatus::Partial => "🟡",
            MigrationStatus::Done => "✅",
            MigrationStatus::PendingReview => "⏸️",
        }
    }
}

/// 单个迁移步骤状态（对应 manifest.md 第 7 节表格的一列）
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StepKind {
    Contract,
    Implement,
    DifferentialTest,
    WireProduction,
    Verify,
    Refresh,
    Review,
}

impl StepKind {
    pub fn as_str(self) -> &'static str {
        match self {
            StepKind::Contract => "contract",
            StepKind::Implement => "implement",
            StepKind::DifferentialTest => "differential-test",
            StepKind::WireProduction => "wire-production",
            StepKind::Verify => "verify",
            StepKind::Refresh => "refresh",
            StepKind::Review => "review",
        }
    }
}

/// 单个功能子任务的迁移状态记录
#[derive(Debug, Clone)]
pub struct MigrationItem {
    pub phase: u8,
    pub feature: String,
    pub task_id: String,
    /// 7 个步骤的状态，按 StepKind 顺序
    pub steps: [MigrationStatus; 7],
}

impl MigrationItem {
    /// 判断功能子任务是否全部完成（所有步骤 Done）
    pub fn is_complete(&self) -> bool {
        self.steps.iter().all(|s| *s == MigrationStatus::Done)
    }

    /// 判断是否可进入 review（前 6 步 Done，review 待 review）
    pub fn ready_for_review(&self) -> bool {
        self.steps[..6].iter().all(|s| *s == MigrationStatus::Done)
            && self.steps[6] == MigrationStatus::PendingReview
    }

    /// 完成进度百分比（0-100）
    pub fn progress(&self) -> u8 {
        let done = self
            .steps
            .iter()
            .filter(|s| **s == MigrationStatus::Done)
            .count() as u32;
        ((done * 100) / 7) as u8
    }
}

/// 迁移 Manifest 内存索引
///
/// 加载自 `docs/design/migration-manifest.md` 第 7 节状态跟踪表。
/// 当前为 stub：实际加载由后续任务实现（可从 cw 任务树查询或解析 markdown）。
pub struct MigrationManifest {
    items: HashMap<String, MigrationItem>,
}

impl MigrationManifest {
    /// 创建空 manifest
    pub fn new() -> Self {
        Self {
            items: HashMap::new(),
        }
    }

    /// 注册一个功能子任务的迁移状态
    pub fn register(&mut self, item: MigrationItem) {
        self.items.insert(item.feature.clone(), item);
    }

    /// 查询指定功能子任务的迁移状态
    pub fn get(&self, feature: &str) -> Option<&MigrationItem> {
        self.items.get(feature)
    }

    /// 返回指定 Phase 下所有功能子任务
    pub fn items_in_phase(&self, phase: u8) -> Vec<&MigrationItem> {
        self.items.values().filter(|i| i.phase == phase).collect()
    }

    /// 整体完成进度（0-100）
    pub fn overall_progress(&self) -> u8 {
        if self.items.is_empty() {
            return 0;
        }
        let total_steps = self.items.len() * 7;
        let done_steps: usize = self
            .items
            .values()
            .map(|i| {
                i.steps
                    .iter()
                    .filter(|s| **s == MigrationStatus::Done)
                    .count()
            })
            .sum();
        (done_steps * 100 / total_steps) as u8
    }
}

impl Default for MigrationManifest {
    fn default() -> Self {
        Self::new()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_status_marker_roundtrip() {
        for status in [
            MigrationStatus::NotStarted,
            MigrationStatus::Partial,
            MigrationStatus::Done,
            MigrationStatus::PendingReview,
        ] {
            let marker = status.to_marker();
            assert_eq!(MigrationStatus::from_marker(marker), status);
        }
    }

    #[test]
    fn test_item_progress() {
        let item = MigrationItem {
            phase: 0,
            feature: "test".to_string(),
            task_id: "T-test".to_string(),
            steps: [
                MigrationStatus::Done,
                MigrationStatus::Done,
                MigrationStatus::Done,
                MigrationStatus::Partial,
                MigrationStatus::NotStarted,
                MigrationStatus::NotStarted,
                MigrationStatus::NotStarted,
            ],
        };
        assert_eq!(item.progress(), 42); // 3/7 ≈ 42%
        assert!(!item.is_complete());
        assert!(!item.ready_for_review());
    }

    #[test]
    fn test_item_ready_for_review() {
        let item = MigrationItem {
            phase: 0,
            feature: "test".to_string(),
            task_id: "T-test".to_string(),
            steps: [
                MigrationStatus::Done,
                MigrationStatus::Done,
                MigrationStatus::Done,
                MigrationStatus::Done,
                MigrationStatus::Done,
                MigrationStatus::Done,
                MigrationStatus::PendingReview,
            ],
        };
        assert!(item.ready_for_review());
        assert!(!item.is_complete());
    }

    #[test]
    fn test_manifest_overall_progress() {
        let mut m = MigrationManifest::new();
        assert_eq!(m.overall_progress(), 0);

        m.register(MigrationItem {
            phase: 0,
            feature: "a".to_string(),
            task_id: "T-a".to_string(),
            steps: [MigrationStatus::Done; 7],
        });
        assert_eq!(m.overall_progress(), 100);
    }
}
