//! Authoritative_Clock —— daemon 进程唯一权威时间源（Req 14.11, 14.12）。
//!
//! ## 职责
//! 以 daemon 进程时钟作为唯一权威时间源，供以下场景使用：
//! - Lease 获取与过期判定
//! - Blind_First_Pass_Verdict 与 Reveal_Event 顺序
//! - Evidence 产生时间
//! - Attestation 签发时间与有效期窗口
//! - Gate decision 时间
//! - Evidence 保留窗口计量
//!
//! ## 单调性保证
//! 同一 daemon 生命周期内，对已提交事件的时间戳单调不回退。
//! 实现：AtomicU64 记录上次发出的秒级时间戳，若系统时钟回拨则沿用上次值。
//!
//! ## 客户端时间戳（Req 14.12）
//! 客户端提供的时间戳只作为参考元数据记录，不参与：
//! - Lease 过期判定
//! - verdict-before-reveal 顺序判定
//! - Evidence 保留窗口计量
//!
//! ## 使用方式
//! daemon 启动时创建一个 `AuthoritativeClock` 实例，通过 `Arc` 共享给所有 handler。
//! 所有需要权威时间的操作调用 `clock.now_secs()` 或 `clock.now_millis()`。

use std::sync::atomic::{AtomicU64, Ordering};
use std::time::{SystemTime, UNIX_EPOCH};

/// daemon 进程权威时钟（Req 14.11）。
///
/// 线程安全：内部使用 AtomicU64 保证并发调用时单调性。
/// 同一 daemon 生命周期内，`now_secs()` 返回值单调不回退。
pub struct AuthoritativeClock {
    /// 上次发出的秒级时间戳（保证单调不回退）
    last_secs: AtomicU64,
    /// 上次发出的毫秒级时间戳（保证单调不回退）
    last_millis: AtomicU64,
}

impl AuthoritativeClock {
    /// 创建权威时钟实例（daemon 启动时调用一次）。
    pub fn new() -> Self {
        Self {
            last_secs: AtomicU64::new(0),
            last_millis: AtomicU64::new(0),
        }
    }

    /// 获取当前权威时间（秒级 Unix 时间戳）。
    ///
    /// 单调性保证：若系统时钟回拨，返回上次发出的值（不回退）。
    /// 用于：Lease 过期、gate decision 时间、Evidence 保留窗口。
    pub fn now_secs(&self) -> u64 {
        let wall = system_time_secs();
        // CAS 循环保证单调：只有 wall > last 时才更新
        loop {
            let last = self.last_secs.load(Ordering::Acquire);
            let next = wall.max(last);
            match self.last_secs.compare_exchange_weak(
                last,
                next,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => return next,
                Err(_) => continue, // 并发竞争，重试
            }
        }
    }

    /// 获取当前权威时间（毫秒级 Unix 时间戳）。
    ///
    /// 单调性保证：同 `now_secs()`，毫秒精度。
    /// 用于：Attestation 签发时间、verdict/Reveal_Event 顺序（需要更细粒度）。
    pub fn now_millis(&self) -> u64 {
        let wall = system_time_millis();
        loop {
            let last = self.last_millis.load(Ordering::Acquire);
            let next = wall.max(last);
            match self.last_millis.compare_exchange_weak(
                last,
                next,
                Ordering::AcqRel,
                Ordering::Acquire,
            ) {
                Ok(_) => return next,
                Err(_) => continue,
            }
        }
    }

    /// 记录客户端时间戳为参考元数据（Req 14.12）。
    ///
    /// 返回一个标记结构，明确表示此时间仅供参考，不参与授权判定。
    /// 调用方应将此值存入审计字段，不得用于 Lease 过期或 verdict 顺序。
    pub fn record_client_timestamp(&self, client_ts: u64) -> ClientTimestampRef {
        ClientTimestampRef {
            client_ts,
            authoritative_ts: self.now_secs(),
        }
    }
}

impl Default for AuthoritativeClock {
    fn default() -> Self {
        Self::new()
    }
}

/// 客户端时间戳参考记录（Req 14.12：仅供参考，不参与授权判定）。
///
/// 存储时作为审计元数据字段，不得用于：
/// - Lease 过期判定
/// - verdict-before-reveal 顺序
/// - Evidence 保留窗口计量
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct ClientTimestampRef {
    /// 客户端自报的时间戳（参考值，可能不准确）
    pub client_ts: u64,
    /// 对应的权威时间戳（daemon 接收时刻）
    pub authoritative_ts: u64,
}

/// 获取系统时间（秒级 Unix 时间戳）
fn system_time_secs() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// 获取系统时间（毫秒级 Unix 时间戳）
fn system_time_millis() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.as_millis() as u64)
        .unwrap_or(0)
}

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use std::thread;

    #[test]
    fn test_now_secs_returns_reasonable_value() {
        let clock = AuthoritativeClock::new();
        let ts = clock.now_secs();
        // 2024-01-01 之后
        assert!(ts > 1_700_000_000, "时间戳应大于 2024 年: {}", ts);
        // 2100 年之前
        assert!(ts < 4_102_444_800, "时间戳应小于 2100 年: {}", ts);
    }

    #[test]
    fn test_now_millis_returns_reasonable_value() {
        let clock = AuthoritativeClock::new();
        let ts = clock.now_millis();
        assert!(ts > 1_700_000_000_000, "毫秒时间戳应大于 2024 年: {}", ts);
    }

    #[test]
    fn test_monotonic_non_decreasing() {
        let clock = AuthoritativeClock::new();
        let mut prev = clock.now_secs();
        for _ in 0..1000 {
            let curr = clock.now_secs();
            assert!(curr >= prev, "时间戳回退: {} < {}", curr, prev);
            prev = curr;
        }
    }

    #[test]
    fn test_monotonic_millis_non_decreasing() {
        let clock = AuthoritativeClock::new();
        let mut prev = clock.now_millis();
        for _ in 0..1000 {
            let curr = clock.now_millis();
            assert!(curr >= prev, "毫秒时间戳回退: {} < {}", curr, prev);
            prev = curr;
        }
    }

    #[test]
    fn test_concurrent_monotonic() {
        let clock = Arc::new(AuthoritativeClock::new());
        let mut handles = vec![];

        for _ in 0..4 {
            let c = Arc::clone(&clock);
            handles.push(thread::spawn(move || {
                let mut prev = 0u64;
                for _ in 0..500 {
                    let curr = c.now_millis();
                    assert!(curr >= prev, "并发时间戳回退: {} < {}", curr, prev);
                    prev = curr;
                }
            }));
        }

        for h in handles {
            h.join().unwrap();
        }
    }

    #[test]
    fn test_client_timestamp_ref() {
        let clock = AuthoritativeClock::new();
        let client_ref = clock.record_client_timestamp(12345);
        assert_eq!(client_ref.client_ts, 12345);
        // 权威时间应该是合理的当前时间
        assert!(client_ref.authoritative_ts > 1_700_000_000);
    }

    #[test]
    fn test_default_trait() {
        let clock = AuthoritativeClock::default();
        let ts = clock.now_secs();
        assert!(ts > 0);
    }
}
