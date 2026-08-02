//! Protected_Mutation 串行化点 —— daemon 进程内唯一写操作入口（Req 14.6, 14.7, 14.14）。
//!
//! ## 设计
//! - 所有 Protected_Mutation（Envelope 发布、verdict 封存、Reveal_Event、Evidence 追加、
//!   gate decision、task_apply、task_close、Lease 操作）经由本模块的单一串行化点应用。
//! - 系统不暴露第二个串行化点（Req 14.6）。
//! - 写请求在串行化点排队，格式正确的并发请求在配置的超时内完成（Req 14.14）。
//! - SQLite 写锁仅为事务互斥，不参与授权/ownership/lease/独立性判定（Req 14.7）。
//!
//! ## 超时语义
//! 等待超时返回 Structured_Reason（error_code="request_timeout"），不改变任务状态。
//! 超时不是数据库锁错误——Req 14.14 保证在超时内完成的请求不返回 database-lock error。
//!
//! ## 使用方式
//! daemon 启动时创建 `SerializationPoint` 实例，通过 `Arc` 共享。
//! dispatch 层对 Protected_Mutation 方法调用 `serialization.execute(timeout, || { ... })`。
//! 只读方法不经过串行化点。

use std::sync::Mutex;
use std::time::{Duration, Instant};

use super::dispatch::DaemonRpcError;

/// 默认请求超时（30 秒）
pub const DEFAULT_REQUEST_TIMEOUT: Duration = Duration::from_secs(30);

/// 串行化点内部状态（当前持有者标记）
struct Inner {
    /// 当前是否有写操作在执行
    busy: bool,
}

/// Protected_Mutation 唯一串行化点（Req 14.6）。
///
/// 线程安全：内部 Mutex + 自旋等待实现带超时的互斥。
/// 同一 daemon 进程内只有一个实例（通过 Arc 共享），系统不暴露第二个。
pub struct SerializationPoint {
    inner: Mutex<Inner>,
    /// 配置的请求超时
    timeout: Duration,
}

impl SerializationPoint {
    /// 创建串行化点（daemon 启动时调用一次）。
    pub fn new(timeout: Duration) -> Self {
        Self {
            inner: Mutex::new(Inner { busy: false }),
            timeout,
        }
    }

    /// 使用默认超时创建串行化点。
    pub fn with_default_timeout() -> Self {
        Self::new(DEFAULT_REQUEST_TIMEOUT)
    }

    /// 获取配置的请求超时。
    pub fn timeout(&self) -> Duration {
        self.timeout
    }

    /// 在串行化点执行 Protected_Mutation（Req 14.6, 14.14）。
    ///
    /// 获取互斥后执行闭包 `f`，完成后释放。
    /// 若在 `timeout` 内无法获取互斥，返回 request_timeout Structured_Reason，
    /// 不改变任何任务状态。
    ///
    /// 注意：`f` 内部的 SQLite 写锁仅为事务互斥（Req 14.7），
    /// 本串行化点才是授权/顺序的保证。
    pub fn execute<F, T>(&self, f: F) -> Result<T, DaemonRpcError>
    where
        F: FnOnce() -> Result<T, DaemonRpcError>,
    {
        self.execute_with_timeout(f, self.timeout)
    }

    /// 在串行化点执行 Protected_Mutation（自定义超时）。
    pub fn execute_with_timeout<F, T>(
        &self,
        f: F,
        timeout: Duration,
    ) -> Result<T, DaemonRpcError>
    where
        F: FnOnce() -> Result<T, DaemonRpcError>,
    {
        let deadline = Instant::now() + timeout;

        // 自旋等待获取互斥（带退避）
        loop {
            {
                let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
                if !inner.busy {
                    inner.busy = true;
                    break;
                }
            }

            // 检查超时
            if Instant::now() >= deadline {
                return Err(DaemonRpcError::new(
                    "request_timeout",
                    format!(
                        "Protected_Mutation 等待串行化点超时（{}ms），请求未执行，状态未改变",
                        timeout.as_millis()
                    ),
                ));
            }

            // 短暂退避（1ms），避免忙等消耗 CPU
            std::thread::sleep(Duration::from_millis(1));
        }

        // 执行闭包（持有串行化点）
        let result = f();

        // 释放串行化点
        {
            let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
            inner.busy = false;
        }

        result
    }

    /// 尝试立即获取串行化点（非阻塞）。
    ///
    /// 用于诊断和测试：检查串行化点是否空闲。
    pub fn try_acquire(&self) -> bool {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        if !inner.busy {
            inner.busy = true;
            true
        } else {
            false
        }
    }

    /// 释放串行化点（与 try_acquire 配对使用）。
    pub fn release(&self) {
        let mut inner = self.inner.lock().unwrap_or_else(|e| e.into_inner());
        inner.busy = false;
    }
}

impl Default for SerializationPoint {
    fn default() -> Self {
        Self::with_default_timeout()
    }
}

// SAFETY: SerializationPoint 内部只有 Mutex<Inner>（Send+Sync）和 Duration（Copy）
// 自动满足 Send+Sync，无需 unsafe impl。

// ============================================
// 单元测试
// ============================================

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU32, Ordering};
    use std::sync::Arc;
    use std::thread;

    #[test]
    fn test_basic_execute() {
        let sp = SerializationPoint::with_default_timeout();
        let result = sp.execute(|| Ok(42));
        assert_eq!(result.unwrap(), 42);
    }

    #[test]
    fn test_execute_propagates_error() {
        let sp = SerializationPoint::with_default_timeout();
        let result: Result<u32, _> = sp.execute(|| {
            Err(DaemonRpcError::new("test_error", "模拟失败"))
        });
        assert!(result.is_err());
        assert_eq!(result.unwrap_err().code, "test_error");
    }

    #[test]
    fn test_sequential_execution() {
        let sp = SerializationPoint::with_default_timeout();
        let counter = AtomicU32::new(0);

        for _ in 0..10 {
            sp.execute(|| {
                counter.fetch_add(1, Ordering::SeqCst);
                Ok(())
            })
            .unwrap();
        }

        assert_eq!(counter.load(Ordering::SeqCst), 10);
    }

    #[test]
    fn test_concurrent_execution_all_complete() {
        let sp = Arc::new(SerializationPoint::new(Duration::from_secs(10)));
        let counter = Arc::new(AtomicU32::new(0));
        let mut handles = vec![];

        for _ in 0..8 {
            let sp = Arc::clone(&sp);
            let counter = Arc::clone(&counter);
            handles.push(thread::spawn(move || {
                for _ in 0..5 {
                    sp.execute(|| {
                        counter.fetch_add(1, Ordering::SeqCst);
                        Ok(())
                    })
                    .unwrap();
                }
            }));
        }

        for h in handles {
            h.join().unwrap();
        }

        // 8 线程 × 5 次 = 40 次全部完成
        assert_eq!(counter.load(Ordering::SeqCst), 40);
    }

    #[test]
    fn test_timeout_returns_structured_reason() {
        let sp = Arc::new(SerializationPoint::new(Duration::from_millis(50)));

        // 先占住串行化点
        sp.try_acquire();

        // 在另一个线程尝试执行，应超时
        let sp2 = Arc::clone(&sp);
        let handle = thread::spawn(move || {
            sp2.execute(|| Ok(()))
        });

        let result = handle.join().unwrap();
        assert!(result.is_err());
        let err = result.unwrap_err();
        assert_eq!(err.code, "request_timeout");
        assert!(err.message.contains("超时"));
        assert!(err.message.contains("状态未改变"));

        // 释放
        sp.release();
    }

    #[test]
    fn test_try_acquire_and_release() {
        let sp = SerializationPoint::with_default_timeout();
        assert!(sp.try_acquire());
        // 再次获取应失败
        assert!(!sp.try_acquire());
        sp.release();
        // 释放后可以再次获取
        assert!(sp.try_acquire());
        sp.release();
    }

    #[test]
    fn test_timeout_does_not_change_state() {
        let sp = Arc::new(SerializationPoint::new(Duration::from_millis(30)));
        let state = Arc::new(AtomicU32::new(0));

        // 占住串行化点
        sp.try_acquire();

        // 尝试执行（会超时），验证状态不变
        let sp2 = Arc::clone(&sp);
        let state2 = Arc::clone(&state);
        let handle = thread::spawn(move || {
            sp2.execute(|| {
                state2.store(999, Ordering::SeqCst);
                Ok(())
            })
        });

        let result = handle.join().unwrap();
        assert!(result.is_err());
        // 状态未被修改（闭包未执行）
        assert_eq!(state.load(Ordering::SeqCst), 0);

        sp.release();
    }

    #[test]
    fn test_default_timeout_value() {
        let sp = SerializationPoint::with_default_timeout();
        assert_eq!(sp.timeout(), Duration::from_secs(30));
    }
}
