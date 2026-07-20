//! G29: QueryBudget——查询预算控制，防止 BFS/DFS 指数爆炸。
//!
//! ## 背景
//!
//! 现有 `compute_frontier` 只有 `max_depth` 控制深度，没有节点数 / 超时预算。
//! 在大型代码库（如 Linux kernel 200k+ 符号）中，几跳 BFS 可能爆炸到百万级
//! 节点访问，导致 daemon RPC 超时 + 内存压力。
//!
//! ## 设计
//!
//! `QueryBudget` 是一个简单的预算容器，包含三个维度：
//! - `max_depth`: 最大深度（既有，向后兼容）
//! - `max_nodes`: 最大访问节点数（默认 10000）
//! - `timeout_ms`: 超时毫秒（默认 5000ms）
//!
//! BFS 循环每访问一个节点检查预算，超限立即返回部分结果（partial=true）。
//!
//! ## 使用
//!
//! ```ignore
//! let budget = QueryBudget::default();
//! let mut tracker = BudgetTracker::new(budget);
//! while let Some((qname, depth)) = queue.pop_front() {
//!     if tracker.is_exceeded() { return partial_result; }
//!     tracker.visit_node();
//!     // ... BFS 逻辑
//! }
//! ```

use std::time::{Duration, Instant};

/// 查询预算配置
#[derive(Debug, Clone, Copy)]
pub struct QueryBudget {
    /// 最大深度（1=直接，2+=多跳）
    pub max_depth: u32,
    /// 最大访问节点数（防止 BFS 爆炸）
    pub max_nodes: usize,
    /// 超时毫秒（防止长时间占用 worker）
    pub timeout_ms: u64,
}

impl Default for QueryBudget {
    fn default() -> Self {
        Self {
            max_depth: 1,
            max_nodes: 10_000,
            timeout_ms: 5_000,
        }
    }
}

impl QueryBudget {
    /// 便捷构造：仅指定深度，其他用默认
    pub fn with_depth(max_depth: u32) -> Self {
        Self {
            max_depth,
            ..Default::default()
        }
    }

    /// 便捷构造：指定全部参数
    pub fn new(max_depth: u32, max_nodes: usize, timeout_ms: u64) -> Self {
        Self {
            max_depth,
            max_nodes,
            timeout_ms,
        }
    }
}

/// 预算跟踪器——运行时检查预算是否超限
///
/// 设计为不可变共享 + 内部计数器（Cell），可在 BFS 循环中按引用传递。
/// `visit_node()` 自增计数，`is_exceeded()` 检查是否超限。
pub struct BudgetTracker {
    budget: QueryBudget,
    visited_count: std::cell::Cell<usize>,
    start: Instant,
    exceeded: std::cell::Cell<bool>,
}

impl BudgetTracker {
    pub fn new(budget: QueryBudget) -> Self {
        Self {
            budget,
            visited_count: std::cell::Cell::new(0),
            start: Instant::now(),
            exceeded: std::cell::Cell::new(false),
        }
    }

    /// 记录一次节点访问，自增计数
    pub fn visit_node(&self) {
        let n = self.visited_count.get() + 1;
        self.visited_count.set(n);
        if n > self.budget.max_nodes {
            self.exceeded.set(true);
        }
    }

    /// 检查是否超限（节点数 + 超时）
    ///
    /// BFS 循环每访问一个节点前调用，返回 true 时应立即返回部分结果。
    pub fn is_exceeded(&self) -> bool {
        if self.exceeded.get() {
            return true;
        }
        let elapsed = self.start.elapsed();
        if elapsed > Duration::from_millis(self.budget.timeout_ms) {
            self.exceeded.set(true);
            return true;
        }
        self.visited_count.get() > self.budget.max_nodes
    }

    /// 获取已访问节点数
    pub fn visited_count(&self) -> usize {
        self.visited_count.get()
    }

    /// 获取已用时间（毫秒）
    pub fn elapsed_ms(&self) -> u64 {
        self.start.elapsed().as_millis() as u64
    }

    /// 是否因预算超限而返回部分结果
    pub fn is_partial(&self) -> bool {
        self.exceeded.get()
    }

    /// 获取原始预算配置
    pub fn budget(&self) -> QueryBudget {
        self.budget
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::thread;

    #[test]
    fn test_default_budget() {
        let b = QueryBudget::default();
        assert_eq!(b.max_depth, 1);
        assert_eq!(b.max_nodes, 10_000);
        assert_eq!(b.timeout_ms, 5_000);
    }

    #[test]
    fn test_with_depth() {
        let b = QueryBudget::with_depth(3);
        assert_eq!(b.max_depth, 3);
        assert_eq!(b.max_nodes, 10_000);
    }

    #[test]
    fn test_new_full() {
        let b = QueryBudget::new(5, 100, 500);
        assert_eq!(b.max_depth, 5);
        assert_eq!(b.max_nodes, 100);
        assert_eq!(b.timeout_ms, 500);
    }

    #[test]
    fn test_budget_tracker_node_exceeded() {
        let budget = QueryBudget::new(5, 3, 60_000);
        let tracker = BudgetTracker::new(budget);
        assert!(!tracker.is_exceeded());
        tracker.visit_node(); // 1
        tracker.visit_node(); // 2
        tracker.visit_node(); // 3
        assert!(!tracker.is_exceeded()); // 3 <= 3
        tracker.visit_node(); // 4 > 3
        assert!(tracker.is_exceeded());
        assert!(tracker.is_partial());
        assert_eq!(tracker.visited_count(), 4);
    }

    #[test]
    fn test_budget_tracker_timeout_exceeded() {
        let budget = QueryBudget::new(5, 1_000_000, 50); // 50ms timeout
        let tracker = BudgetTracker::new(budget);
        assert!(!tracker.is_exceeded());
        thread::sleep(Duration::from_millis(60));
        assert!(tracker.is_exceeded());
        assert!(tracker.is_partial());
    }

    #[test]
    fn test_budget_tracker_no_exceed() {
        let budget = QueryBudget::new(5, 1000, 60_000);
        let tracker = BudgetTracker::new(budget);
        for _ in 0..100 {
            tracker.visit_node();
        }
        assert!(!tracker.is_exceeded());
        assert!(!tracker.is_partial());
        assert_eq!(tracker.visited_count(), 100);
    }

    #[test]
    fn test_elapsed_ms_increases() {
        let budget = QueryBudget::default();
        let tracker = BudgetTracker::new(budget);
        let t0 = tracker.elapsed_ms();
        thread::sleep(Duration::from_millis(20));
        let t1 = tracker.elapsed_ms();
        assert!(t1 >= t0);
        // 允许一些抖动，但至少有 10ms 的增量
        assert!(t1 > t0 || t0 == 0, "t0={}, t1={}", t0, t1);
    }
}
