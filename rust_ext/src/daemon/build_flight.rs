//! build_graph 单飞/预算/超时协调（参考 GitNexus refresh_index 设计）。
//!
//! 设计映射（testcode/GitNexus/docs/mcp-reference.md「refresh_index」节）：
//! - index-only：build_graph 只重建索引、不写仓库文件（callwarden 既有语义）；
//! - bounded：预扫可索引文件数，超过 `CALLWARDEN_BUILD_MAX_FILES`（默认
//!   5000，0=不限制）拒绝并报 `build_too_large`；
//! - 单飞（single-flight）：同一 workspace 同时只允许一个构建，进行中
//!   再调用报 `build_in_progress`（附当前扫描进度）；
//! - 超时：daemon 侧等待上限 `CALLWARDEN_BUILD_WAIT_MS`（默认 15s），
//!   到点返回 `accepted=true` 的结构化响应，构建在后台单飞继续跑完并
//!   发布 lastBuild（worker 永不中断，与 GitNexus "never interrupts"
//!   语义一致）；
//! - 可观测：`dry_run=true` 报 estimatedFiles/inProgress/wouldRun/
//!   lastBuild/progress（GitNexus progress notifications 的等价物）。
//!
//! 与 GitNexus 的偏差（默认等待 15s vs 10 分钟）：callwarden daemon
//! dispatch 持有全局状态锁，长等待会饿死 /health（watchdog 连续 2 次
//! 失败即判死）；到点转后台后由 Python 工具层轮询 dry_run 收敛结果，
//! 轮询 RPC 短平快，不再长期占锁。

use serde_json::{json, Value};
use std::collections::HashMap;
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Arc, Mutex};
use std::time::Duration;

/// 单个构建的进度（原子计数，daemon 线程写、dispatch 线程读）。
#[derive(Debug, Default)]
pub struct BuildProgress {
    pub scanned: AtomicU64,
    pub inserted: AtomicU64,
    pub unchanged: AtomicU64,
    pub skipped_cached: AtomicU64,
    pub deleted: AtomicU64,
    pub archived: AtomicU64,
    pub skipped_unparsed: AtomicU64,
    pub started_at: AtomicU64,
}

impl BuildProgress {
    /// 进度快照（dry_run/单飞拒绝报错里的可观测载荷）。
    pub fn snapshot(&self) -> Value {
        json!({
            "scanned": self.scanned.load(Ordering::Relaxed),
            "inserted": self.inserted.load(Ordering::Relaxed),
            "unchanged": self.unchanged.load(Ordering::Relaxed),
            "skipped_cached": self.skipped_cached.load(Ordering::Relaxed),
            "deleted": self.deleted.load(Ordering::Relaxed),
            "archived": self.archived.load(Ordering::Relaxed),
            "skipped_unparsed": self.skipped_unparsed.load(Ordering::Relaxed),
            "started_at": self.started_at.load(Ordering::Relaxed),
        })
    }
}

#[derive(Debug)]
struct FlightEntry {
    progress: Arc<BuildProgress>,
    seq: u64,
}

/// per-workspace 构建登记表（单飞）+ 最近一次完成结果（lastBuild）。
///
/// key = canonical workspace root（posix 化），与 Python 侧轮询无共享状态，
/// 全部经 RPC 读取。
#[derive(Debug, Default)]
pub struct BuildFlights {
    seq: AtomicU64,
    inner: Mutex<HashMap<String, FlightEntry>>,
    last: Mutex<HashMap<String, Value>>,
}

impl BuildFlights {
    pub fn new() -> Self {
        Self::default()
    }

    /// 登记新构建；该 workspace 已有构建在跑时返回 `Err(现有进度)`（单飞）。
    /// 返回 `Ok((build_seq, progress))`，seq 单调递增供轮询对账。
    pub fn begin(
        &self,
        root_key: &str,
    ) -> Result<(u64, Arc<BuildProgress>), Arc<BuildProgress>> {
        let mut map = self.inner.lock().expect("build flights mutex poisoned");
        if let Some(entry) = map.get(root_key) {
            return Err(entry.progress.clone());
        }
        let seq = self.seq.fetch_add(1, Ordering::Relaxed) + 1;
        let progress = Arc::new(BuildProgress {
            started_at: AtomicU64::new(now_secs()),
            ..BuildProgress::default()
        });
        map.insert(
            root_key.to_string(),
            FlightEntry {
                progress: progress.clone(),
                seq,
            },
        );
        Ok((seq, progress))
    }

    /// 构建结束（成功或失败）：移除单飞登记并记录 lastBuild（注入
    /// build_seq/completed_at，供轮询方与 accepted 响应对账）。
    ///
    /// `on_complete` 是审计回执出口（实现见 `fs_handlers::append_build_receipt`）：
    /// 仅在确实移除了一条 flight（真实构建发生）时触发，回调收到注入
    /// build_seq/completed_at 后的最终 stats；返回 Err 代表审计写入失败，
    /// 由调用方 fail-closed。重复 finish / 无登记的 ghost 不触发（幂等，E6）。
    pub fn finish<F>(&self, root_key: &str, mut stats: Value, on_complete: F) -> Result<(), String>
    where
        F: FnOnce(&Value) -> Result<(), String>,
    {
        let seq = {
            let mut map = self.inner.lock().expect("build flights mutex poisoned");
            map.remove(root_key).map(|e| e.seq)
        };
        if let Some(seq) = seq {
            if let Some(obj) = stats.as_object_mut() {
                obj.insert("build_seq".into(), json!(seq));
                obj.insert("completed_at".into(), json!(now_secs()));
            }
            self.last
                .lock()
                .expect("build flights last mutex poisoned")
                .insert(root_key.to_string(), stats.clone());
            on_complete(&stats)
        } else {
            Ok(())
        }
    }

    pub fn is_running(&self, root_key: &str) -> bool {
        self.inner
            .lock()
            .expect("build flights mutex poisoned")
            .contains_key(root_key)
    }

    /// 进行中构建的进度快照（含单飞序号）；无构建时 None。
    pub fn progress(&self, root_key: &str) -> Option<Value> {
        let map = self.inner.lock().expect("build flights mutex poisoned");
        map.get(root_key).map(|e| {
            let mut v = e.progress.snapshot();
            if let Some(obj) = v.as_object_mut() {
                obj.insert("build_seq".into(), json!(e.seq));
            }
            v
        })
    }

    /// 最近一次完成结果（可能被后续构建覆盖；调用方用 build_seq 对账）。
    pub fn last(&self, root_key: &str) -> Option<Value> {
        self.last
            .lock()
            .expect("build flights last mutex poisoned")
            .get(root_key)
            .cloned()
    }
}

fn now_secs() -> u64 {
    std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .map(|d| d.as_secs())
        .unwrap_or(0)
}

/// bounded：可索引文件数上限（`CALLWARDEN_BUILD_MAX_FILES`，默认 5000，
/// 0=不限制；非法值按默认处理）。
pub fn build_max_files() -> usize {
    parse_usize_env("CALLWARDEN_BUILD_MAX_FILES").unwrap_or(5000)
}

/// daemon 侧等待上限（`CALLWARDEN_BUILD_WAIT_MS`，默认 15000）。
pub fn build_wait_bound() -> Duration {
    Duration::from_millis(parse_u64_env("CALLWARDEN_BUILD_WAIT_MS").unwrap_or(15_000))
}

fn parse_usize_env(name: &str) -> Option<usize> {
    std::env::var(name).ok()?.trim().parse::<usize>().ok()
}

fn parse_u64_env(name: &str) -> Option<u64> {
    std::env::var(name).ok()?.trim().parse::<u64>().ok()
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn begin_single_flight_and_finish_records_last() {
        let flights = BuildFlights::new();
        let (seq, progress) = flights.begin("ws://root-a").expect("first begin ok");
        assert_eq!(seq, 1);
        progress.scanned.store(7, Ordering::Relaxed);
        // 单飞：重复 begin 返回现有进度
        let existing = flights.begin("ws://root-a").expect_err("single flight");
        assert_eq!(existing.scanned.load(Ordering::Relaxed), 7);
        assert!(flights.is_running("ws://root-a"));
        let snap = flights.progress("ws://root-a").unwrap();
        assert_eq!(snap["scanned"], 7);
        assert_eq!(snap["build_seq"], 1);
        // 不同 workspace 互不干扰
        assert!(flights.begin("ws://root-b").is_ok());
        // 结束后登记移除、lastBuild 可查且带序号
        let mut audited = Vec::new();
        flights
            .finish("ws://root-a", json!({"ok": true, "scanned": 7}), |s| {
                audited.push(s.clone());
                Ok(())
            })
            .unwrap();
        // 审计回执回调收到注入 build_seq/completed_at 后的最终 stats
        assert_eq!(audited.len(), 1);
        assert_eq!(audited[0]["build_seq"], 1);
        assert!(audited[0]["completed_at"].is_u64());
        assert!(!flights.is_running("ws://root-a"));
        let last = flights.last("ws://root-a").unwrap();
        assert_eq!(last["build_seq"], 1);
        assert_eq!(last["scanned"], 7);
        assert!(last["completed_at"].is_u64());
        // 结束后可再次 begin（序号递增）
        let (seq3, _) = flights.begin("ws://root-a").unwrap();
        assert_eq!(seq3, 3);
    }

    #[test]
    fn finish_without_begin_is_noop_and_no_receipt() {
        let flights = BuildFlights::new();
        let mut calls = 0;
        flights
            .finish("ghost", json!({"x": 1}), |_| {
                calls += 1;
                Ok(())
            })
            .unwrap();
        assert_eq!(calls, 0, "无 begin 的 finish 不得落审计回执（E6 幂等）");
        assert!(flights.last("ghost").is_none());
        assert!(!flights.is_running("ghost"));
    }

    #[test]
    fn finish_idempotent_no_second_receipt() {
        let flights = BuildFlights::new();
        flights.begin("ws://idem").unwrap();
        let mut calls = 0;
        flights.finish("ws://idem", json!({"ok": true}), |_| {
            calls += 1;
            Ok(())
        }).unwrap();
        // 重复 finish：单次飞已移除，回调不得再触发
        flights.finish("ws://idem", json!({"ok": true}), |_| {
            calls += 1;
            Ok(())
        }).unwrap();
        assert_eq!(calls, 1, "重复 finish 只落 1 条回执");
    }

    #[test]
    fn finish_propagates_audit_error() {
        let flights = BuildFlights::new();
        flights.begin("ws://audit-err").unwrap();
        let err = flights
            .finish("ws://audit-err", json!({"ok": true}), |_| {
                Err("E_WRITE_AUDIT_UNAVAILABLE".to_string())
            })
            .unwrap_err();
        assert_eq!(err, "E_WRITE_AUDIT_UNAVAILABLE", "审计写失败必须上抛，不吞异常");
    }

    #[test]
    fn progress_and_last_absent_for_unknown_workspace() {
        let flights = BuildFlights::new();
        assert!(flights.progress("nope").is_none());
        assert!(flights.last("nope").is_none());
    }

    #[test]
    fn env_bounds_default() {
        // env 未设置时取默认（5000 / 15s）
        assert_eq!(build_max_files(), 5000);
        assert_eq!(build_wait_bound(), Duration::from_millis(15_000));
    }

    #[test]
    fn parse_env_helpers_missing_means_none() {
        assert_eq!(parse_usize_env("CW_TEST_NO_SUCH_ENV_X"), None);
        assert_eq!(parse_u64_env("CW_TEST_NO_SUCH_ENV_X"), None);
    }
}
