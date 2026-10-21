//! 进程级 SQLite 连接池（CI-DEBT 深水区：消除同进程双实例 WAL Hazard）
//!
//! # 根因
//!
//! 同进程内曾长期并存两个指向同一 `.db` 文件的 SQLite 实例：
//!
//! - Python 主连接：`db/db_base.py` 的 `sqlite3.connect(db_path, check_same_thread=False)`
//!   ——长活，跨整个测试/服务生命周期。
//! - Rust 侧短连接：每个 PyO3 入口都 `Connection::open(db_path)`，
//!   **open → 操作 → drop**（如 `batch_register_files` / `batch_register_queries`）。
//!
//! POSIX（Linux）上 fcntl 锁按**进程**而非句柄分配，同一进程的两个连接
//! **无法互斥 WAL-index**。短连接 drop 时的 WAL checkpoint 与 Python 主连接的
//! autocheckpoint 交错，可在同一页上产生撕裂，表现为
//! `sqlite3.DatabaseError: database disk image is malformed`（CI ubuntu 独有；
//! Windows LockFileEx 按句柄加锁，故本地全绿——修复只能靠 CI 验证）。
//!
//! # 方案
//!
//! 把 Rust 侧的短连接收敛为**每 db_path 一个进程级长活连接**。
//! open 期的 `PRAGMA wal_checkpoint(PASSIVE)` 随之只执行一次（而非每次调用），
//! 既保留"读到 Python 最新写入"的行为，又消除 checkpoint 风暴。
//!
//! # 锁语义与死锁规避
//!
//! 池的 `Mutex` **只在单次调用期间持有**（`with_pooled_conn` 全程持锁）。
//! 调用方因此不能在同一线程内嵌套调用两个池函数——那会自锁。
//! 已审计：`batch_*` / `incremental_build_query` / `manifest_query` /
//! `cas_merge_query` / `cas_write_query` 的下游 helper
//! （`init_codegraph_schema` / `merge_cas_to_codegraph` /
//! `CasStore::ensure_file_generations_table`）全部只接收 `&Connection`，
//! 不会回头再取池连接，故当前无重入。
//!
//! 未来若引入重入，`with_pooled_conn` 会在同一线程二次加锁时**检测并快速失败**
//! （见 `IN_POOL` 线程局部标记），而不是静默死锁。
//!
//! # 已知剩余窗口
//!
//! Python 主连接与 Rust 池连接仍是同进程两个实例（跨库种锁不可解），
//! 但 checkpoint 由"每调用一次"降为"每进程一次"，交错窗口大幅收窄；
//! 且池连接不再随调用 drop，故不会在 Python 事务中途触发隐式 checkpoint。

use std::cell::Cell;
use std::collections::HashMap;
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};

/// 进程级连接池：`规范化 db_path -> 长活 Connection`。
static POOL: OnceLock<Mutex<HashMap<String, Connection>>> = OnceLock::new();

fn pool() -> &'static Mutex<HashMap<String, Connection>> {
    POOL.get_or_init(|| Mutex::new(HashMap::new()))
}

thread_local! {
    /// 本线程持有池锁的层数——用于重入检测，防止 `with_pooled_conn` 自死锁。
    static IN_POOL: Cell<usize> = const { Cell::new(0) };
}

/// 进入池作用域的标记：Drop 时自动把层数减回去（含错误提前返回路径）。
struct PoolScope;

impl PoolScope {
    fn enter() -> Result<Self, rusqlite::Error> {
        let depth = IN_POOL.with(|c| {
            let d = c.get();
            c.set(d + 1);
            d
        });
        if depth > 0 {
            IN_POOL.with(|c| c.set(depth)); // 回滚本次自增
            // std Mutex 不可重入，二次加锁会永久阻塞——快速失败优于挂死。
            return Err(rusqlite::Error::InvalidQuery);
        }
        Ok(PoolScope)
    }
}

impl Drop for PoolScope {
    fn drop(&mut self) {
        IN_POOL.with(|c| c.set(c.get().saturating_sub(1)));
    }
}

/// 池键规范化。
///
/// Windows 文件名大小写不敏感且路径分隔符可互换（`C:\a\b` 与 `c:/a/b` 指向同一
/// 文件），必须归一，否则同一文件会在池中留下两个连接——那正好是本模块要消除的
/// 双实例本身。非 Windows 平台路径大小写敏感，原样保留。
fn pool_key(db_path: &str) -> String {
    if cfg!(windows) {
        db_path.replace('/', "\\").to_lowercase()
    } else {
        db_path.to_string()
    }
}

/// 新建连接并执行与 Python `db_base.py` 对齐的 PRAGMA 序列。
///
/// 顺序与各 `open_readwrite` 原实现保持一致：
/// busy_timeout → journal_mode=WAL → synchronous=NORMAL → foreign_keys=OFF
/// → wal_checkpoint(PASSIVE)。
///
/// `foreign_keys` 必须用 `execute_batch` 传裸 `OFF` 关键字：`pragma_update` 传字符串
/// 会被 SQLite 当作 truthy=1，FK 仍启用（见 `incremental_build_query.rs` 原注释）。
fn open_pooled(db_path: &str) -> Result<Connection, rusqlite::Error> {
    let conn = Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE,
    )?;
    // busy_timeout 必须在任何 SQL 之前设置
    conn.busy_timeout(Duration::from_secs(5))?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "synchronous", "NORMAL")?;
    conn.pragma_update(None, "foreign_keys", "OFF")?;
    // CI-DEBT 深水区 A/B（CW_RUST_OPEN_CHECKPOINT=0 可关闭）：open 时 checkpoint 把
    // WAL 帧回写主库，与 Python 侧 autocheckpoint 交错曾产生撕裂页
    // （database disk image is malformed / disk I/O error）。单例化后该 checkpoint
    // 每进程只执行一次；env=0 门与原注释保留，供 CI 定性实验。
    if std::env::var("CW_RUST_OPEN_CHECKPOINT").as_deref() != Ok("0") {
        let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
    }
    Ok(conn)
}

/// 取用池化连接执行 `f`，返回其结果。
///
/// 连接在整个 `f` 执行期间保持打开且池锁被持有，因此 `f` 内看到的是稳定的
/// 快照视角，不会被并发 drop 掉另一实例。
///
/// # 错误
///
/// - 池锁已中毒：返回 `rusqlite::Error::InvalidQuery`（不 panic）。
/// - 同线程重入：返回 `rusqlite::Error::InvalidQuery`（不静默死锁）。
/// - `open_pooled` 失败：原样透出。
pub fn with_pooled_conn<T>(
    db_path: &str,
    f: impl FnOnce(&Connection) -> T,
) -> Result<T, rusqlite::Error> {
    let _scope = PoolScope::enter()?;

    let key = pool_key(db_path);
    let mut guard = match pool().lock() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    };

    if !guard.contains_key(&key) {
        let conn = open_pooled(db_path)?;
        guard.insert(key.clone(), conn);
    }

    // 借出 &Connection：条目在 map 中，guard 全程持锁，故引用有效期覆盖 f。
    let out = {
        let conn = guard
            .get(&key)
            .expect("池条目刚插入或已存在，缺失说明池内部状态损坏");
        f(conn)
    };

    // CI-DEBT 诊断门：CW_RUST_POOL_LIFETIME=short 时每次调用结束即关闭并从池中
    // 移除该 key 的连接，回到池化前的 open→drop 语义。关闭前按
    // `close_pooled_connections` 的同款行为先 `wal_checkpoint(TRUNCATE)`
    // （best-effort，失败忽略）——池化前最后一个连接 close 时 SQLite 也会
    // 自动 checkpoint 并删除 -wal/-shm。该门只用于 CI 定性"连接寿命"是否为
    // malformed 根因，不是产品行为。
    if std::env::var("CW_RUST_POOL_LIFETIME").as_deref() == Ok("short") {
        if let Some(conn) = guard.remove(&key) {
            let _ = conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);");
            drop(conn);
        }
    }

    drop(guard);
    Ok(out)
}

/// 关闭并移除池连接。
///
/// - `db_path = Some(p)`：只关闭该路径的连接。
/// - `db_path = None`：关闭全部。
///
/// drop 前尽力 `PRAGMA wal_checkpoint(TRUNCATE)`，把 WAL 回收到主库，使调用方
/// （如测试 tmp_path 清理、`db_base.close()`）随后能安全删除/替换文件。
/// checkpoint 失败一律忽略——清理路径不应因 checkpoint 失败而抛错，更不得 panic。
pub fn close_pooled_connections(db_path: Option<&str>) {
    let mut guard = match pool().lock() {
        Ok(g) => g,
        Err(poisoned) => poisoned.into_inner(),
    };

    let targets: Vec<String> = match db_path {
        Some(p) => {
            let k = pool_key(p);
            if guard.contains_key(&k) {
                vec![k]
            } else {
                Vec::new()
            }
        }
        None => guard.keys().cloned().collect(),
    };

    for key in targets {
        if let Some(conn) = guard.remove(&key) {
            // 尽力回写 WAL；失败忽略（清理路径必须 best-effort）。
            let _ = conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);");
            drop(conn);
        }
    }
}

/// 当前池中缓存的连接数（供测试与诊断使用）。
pub fn pooled_connection_count() -> usize {
    match pool().lock() {
        Ok(g) => g.len(),
        Err(poisoned) => poisoned.into_inner().len(),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::{Mutex, MutexGuard, OnceLock};

    /// 池是**进程级**全局状态，而 cargo test 默认并行跑测试线程。
    /// 断言 `pooled_connection_count()` 需要独占视图，故本模块所有测试
    /// 经此锁串行化。
    fn test_lock() -> MutexGuard<'static, ()> {
        static LOCK: OnceLock<Mutex<()>> = OnceLock::new();
        LOCK.get_or_init(|| Mutex::new(()))
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    fn tmp_db(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("cw_pool_test_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        dir.join(name)
    }

    #[test]
    fn test_pool_key_normalizes_windows_case_and_separator() {
        if cfg!(windows) {
            assert_eq!(pool_key("C:/A/B.db"), pool_key("c:\\a\\b.db"));
        } else {
            assert_eq!(pool_key("/A/B.db"), pool_key("/A/B.db"));
        }
    }

    #[test]
    fn test_with_pooled_conn_reuses_single_connection() {
        let _guard = test_lock();
        let db = tmp_db("reuse.db");
        let _ = std::fs::remove_file(&db);
        let path = db.to_string_lossy().to_string();

        close_pooled_connections(None);

        with_pooled_conn(&path, |c| {
            c.execute_batch("CREATE TABLE IF NOT EXISTS t(x INTEGER);")
                .unwrap();
        })
        .unwrap();

        // 第二次调用命中缓存，池内仍只有 1 条
        with_pooled_conn(&path, |c| {
            c.execute_batch("INSERT INTO t VALUES (1);").unwrap();
        })
        .unwrap();

        assert_eq!(pooled_connection_count(), 1);

        let n = with_pooled_conn(&path, |c| {
            c.query_row("SELECT COUNT(*) FROM t", [], |r| r.get::<_, i64>(0))
                .unwrap()
        })
        .unwrap();
        assert_eq!(n, 1);

        close_pooled_connections(Some(&path));
        assert_eq!(pooled_connection_count(), 0);
        let _ = std::fs::remove_file(&db);
    }

    #[test]
    fn test_same_file_different_case_shares_one_connection() {
        let _guard = test_lock();
        let db = tmp_db("case_share.db");
        let _ = std::fs::remove_file(&db);
        let path = db.to_string_lossy().to_string();
        close_pooled_connections(None);

        with_pooled_conn(&path, |c| {
            c.execute_batch("CREATE TABLE IF NOT EXISTS t(x INTEGER);").unwrap();
        })
        .unwrap();

        // Windows 下大小写/分隔符不同的同一路径必须复用同一连接，
        // 否则池里会留下两个实例——那正是本模块要消除的双实例本身。
        let alt = path.replace('/', "\\").to_uppercase();
        with_pooled_conn(&alt, |c| {
            c.execute_batch("INSERT INTO t VALUES (7);").unwrap();
        })
        .unwrap();

        assert_eq!(pooled_connection_count(), 1);

        close_pooled_connections(Some(&path));
        assert_eq!(pooled_connection_count(), 0);
        let _ = std::fs::remove_file(&db);
    }

    #[test]
    fn test_close_all_drains_pool() {
        let _guard = test_lock();
        let a = tmp_db("drain_a.db");
        let b = tmp_db("drain_b.db");
        let _ = std::fs::remove_file(&a);
        let _ = std::fs::remove_file(&b);
        let (pa, pb) = (
            a.to_string_lossy().to_string(),
            b.to_string_lossy().to_string(),
        );

        close_pooled_connections(None);

        with_pooled_conn(&pa, |c| {
            c.execute_batch("CREATE TABLE IF NOT EXISTS a(x);").unwrap();
        })
        .unwrap();
        with_pooled_conn(&pb, |c| {
            c.execute_batch("CREATE TABLE IF NOT EXISTS b(x);").unwrap();
        })
        .unwrap();
        assert_eq!(pooled_connection_count(), 2);

        close_pooled_connections(None);
        assert_eq!(pooled_connection_count(), 0);

        let _ = std::fs::remove_file(&a);
        let _ = std::fs::remove_file(&b);
    }

    #[test]
    fn test_reentrant_call_fails_fast_instead_of_deadlocking() {
        let _guard = test_lock();
        let db = tmp_db("reentrant.db");
        let _ = std::fs::remove_file(&db);
        let path = db.to_string_lossy().to_string();
        close_pooled_connections(None);

        // 闭包内再次取池连接：必须快速返回 Err，不能挂死
        let outer =
            with_pooled_conn(&path, |_outer| with_pooled_conn(&path, |_inner| 42usize)).unwrap();

        assert!(outer.is_err(), "重入必须返回 Err 而非死锁");
        close_pooled_connections(Some(&path));
        let _ = std::fs::remove_file(&db);
    }

    #[test]
    fn test_close_unknown_path_is_noop() {
        let _guard = test_lock();
        close_pooled_connections(Some("C:/definitely/not/here.db"));
        assert_eq!(pooled_connection_count(), 0);
    }
}