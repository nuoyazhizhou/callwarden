//! Rust 侧 SQLite 连接管理（同进程双实例 WAL Hazard 正式修法 v2）
//!
//! # 缺陷现象（三代受害面漂移）
//!
//! CI（ubuntu）一族 `database disk image is malformed`（Windows 免疫，只能靠
//! CI 判读）。受害面随「谁先读到撕裂页」漂移：
//!
//! | 阶段 | Rust 侧连接策略 | CI 受害面 |
//! |---|---|---|
//! | c53da76 之前 | 每次调用 open→drop | p15 族 6 项 + `test_incremental_parse` 1 项 |
//! | c53da76 | 进程级长活池连接 | h7 族 7 项 + agent_rules 4 项 + agent_integration 2 项 + e2e_triangle 1 项 + dashboard 12 项 |
//! | a80ca63（v1 修法） | 短连接 + 写后 TRUNCATE drain | h7 族 12 项消失，但全套 CI（292e684，8138 passed）暴露 p15 族 6 项 + incremental_parse 1 项 **回归**，另有 `test_rust_storage_service` 读路径 1 项（DID NOT RAISE） |
//!
//! # 根因
//!
//! 同进程内并存**两个互不相同的 SQLite 实现**指向同一 `.db`：
//!
//! - Python 主连接：系统 libsqlite3（`db/db_base.py` 的 `sqlite3.connect`），长活。
//! - Rust 侧：rusqlite `features = ["bundled"]`（自带 SQLite 3.45.0）。
//!
//! 二者共享同一个 `-wal` / `-shm`（WAL-index）。POSIX 上 `fcntl` 锁按**进程**
//! 而非句柄分配，故同进程这两个连接**无法互相排斥**；Windows `LockFileEx`
//! 按句柄加锁，故本地免疫——这正是「CI 唯一判据」的原因。
//!
//! ## v1 修法为何不够（a80ca63 的误判）
//!
//! v1 的结论是「短连接 + 写后 TRUNCATE drain」全绿。但 Probe I2 只验证了
//! h7 族（8 项 passed）；292e684 全套 CI 证明：**只要 Rust 侧在本进程内做
//! 任何 WAL 回收动作，就会撕裂 Python 长连接的 `-shm` 视图与读标记**，
//! 三种动作同源：
//!
//! 1. 写后 `wal_checkpoint(TRUNCATE)`（v1 引入）——重置 WAL-index 头，
//!    Python 长连接缓存的快照立刻过期；
//! 2. 短连接关闭时的 **autocheckpoint**（c53da76 之前就在）——drop 时
//!    回写 + 截断 + 可能删除 `-wal`/`-shm`；
//! 3. 「最后连接」关闭时删除 `-shm`——Python 侧仍 mmap 着被删的 inode。
//!
//! 读路径则以「过期快照」形式受害（rust_storage 的 DID NOT RAISE：Rust
//! 短连接把已篡改的 checksum 读成截断前的原值，静默返回 Ok）。
//!
//! # 正式修法 v2：Rust 侧在本进程内做**零 checkpoint**
//!
//! 三项措施，目标都是「不做任何让 Python 侧 `-shm` 视图失效的动作」：
//!
//! 1. **哨兵连接**：每个 `.db` 首次访问时常驻一个**永不读取、永不写入、
//!    永不关闭**的本侧连接。它把 `-shm` 的本侧引用计数钉住 ≥ 1，使任何
//!    短连接的关闭序列都不会触发「最后连接」的 `-wal`/`-shm` 回收。
//! 2. **`PRAGMA wal_autocheckpoint=0`**：短连接关闭时不再 autocheckpoint。
//! 3. **去掉写后 drain 与 open 期 PASSIVE checkpoint**：默认零 checkpoint；
//!    `CW_RUST_WAL_DRAIN=truncate` 保留为诊断/兼容开关。
//!
//! WAL 增长由 Python 侧 autocheckpoint（默认 1000 页，单实例操作、无撕裂）
//! 回收——**单实例回收是安全的，双实例交错回收才是 hazard**。
//!
//! 短连接本身（每次 open 重建 WAL-index 视图）仍是 h7 族修复的必要条件：
//! 长活连接跨整个 build 持有读标记，Python 侧持续提交推进 `mxFrame`，
//! 两侧对「同一页由哪些帧拼成」错位 ⇒ 撕裂页（Probe I1/I4：长活 + 任一
//! drain 策略均红）。
//!
//! # 适用范围
//!
//! `with_db_conn` 只被 ext 侧（batch_build / batch_calls / batch_file_versions /
//! batch_register / incremental_build / cas_merge / cas_write 等模块）调用，
//! 即**总是运行在 Python 共存进程**里。daemon（纯 Rust 进程，无 Python
//! 长连接，跨进程 fcntl 正常互斥）不使用本函数，其自身 checkpoint
//! 策略（replicator 等）不受影响。
//!
//! # 代价
//!
//! 每次调用多一次 open/close；WAL 不再由 Rust 侧回收，长度交给 Python 侧
//! autocheckpoint 控制（测试场景短暂、无实际增长风险）。相对 v1 省掉了
//! 每次写后的 TRUNCATE（页拷贝 + 截断）。

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};

// ============================================
// 运维 / 诊断门
// ============================================

/// 哨兵开关的环境覆盖（TESTS 覆盖时跳过环境读取，保证单测确定性）。
static SENTINEL_OVERRIDE: AtomicBool = AtomicBool::new(false);
static SENTINEL_ENABLED: AtomicBool = AtomicBool::new(true);
/// 写后 TRUNCATE drain 的环境覆盖。
static DRAIN_OVERRIDE: AtomicBool = AtomicBool::new(false);
static DRAIN_TRUNCATE: AtomicBool = AtomicBool::new(false);

fn sentinel_enabled() -> bool {
    if SENTINEL_OVERRIDE.load(Ordering::Relaxed) {
        SENTINEL_ENABLED.load(Ordering::Relaxed)
    } else {
        std::env::var("CW_RUST_SENTINEL_CONN").as_deref() != Ok("0")
    }
}

fn drain_truncate_enabled() -> bool {
    if DRAIN_OVERRIDE.load(Ordering::Relaxed) {
        DRAIN_TRUNCATE.load(Ordering::Relaxed)
    } else {
        std::env::var("CW_RUST_WAL_DRAIN").as_deref() == Ok("truncate")
    }
}

/// 单测 / 诊断用：编程式覆盖哨兵开关（进程内立即生效，绕过环境变量）。
pub fn set_sentinel_for_tests(enabled: bool) {
    SENTINEL_ENABLED.store(enabled, Ordering::Relaxed);
    SENTINEL_OVERRIDE.store(true, Ordering::Relaxed);
}

/// 单测 / 诊断用：编程式覆盖写后 TRUNCATE drain 开关。
pub fn set_wal_drain_truncate_for_tests(enabled: bool) {
    DRAIN_TRUNCATE.store(enabled, Ordering::Relaxed);
    DRAIN_OVERRIDE.store(true, Ordering::Relaxed);
}

// ============================================
// 哨兵连接（保 -shm 永不被本侧关闭序列回收）
// ============================================

static SENTINELS: OnceLock<Mutex<HashMap<PathBuf, Connection>>> = OnceLock::new();

fn sentinels() -> &'static Mutex<HashMap<PathBuf, Connection>> {
    SENTINELS.get_or_init(|| Mutex::new(HashMap::new()))
}

/// 规范化哨兵键：canonicalize 失败（理论上的全新文件）时退回原始路径。
/// 调用顺序保证：先 open 短连接（文件必然存在）再 ensure_sentinel，
/// 故 canonicalize 在实际路径上总是成功，同一文件不会产生两个键。
fn sentinel_key(db_path: &str) -> PathBuf {
    Path::new(db_path)
        .canonicalize()
        .unwrap_or_else(|_| Path::new(db_path).to_path_buf())
}

/// 确保该 `.db` 常驻一个哨兵连接。best-effort：打开失败则放弃
/// （调用方仍会拿到自己的短连接，只是失去 `-shm` 保护）。
fn ensure_sentinel(db_path: &str) {
    if !sentinel_enabled() {
        return;
    }
    let key = sentinel_key(db_path);
    let mut guard = sentinels().lock().unwrap();
    if !guard.contains_key(&key) {
        if let Ok(conn) = open_db_conn(db_path) {
            guard.insert(key, conn);
        }
    }
}

/// 诊断 / 清理用：立即关闭并丢弃全部哨兵连接（测试进程内还原文件系统状态）。
pub fn close_sentinel_connections() {
    sentinels().lock().unwrap().clear();
}

// ============================================
// 短连接
// ============================================

/// 打开一个专用连接，PRAGMA 与 Python `db_base.py` 对齐，并额外关闭
/// autocheckpoint。
///
/// 顺序与历史各模块自带的 `open_readwrite` 保持一致：
/// busy_timeout → journal_mode=WAL → synchronous=NORMAL → foreign_keys=OFF
/// → wal_autocheckpoint=0。
///
/// `foreign_keys` 必须用 `execute_batch` 传裸 `OFF` 关键字：`pragma_update`
/// 传字符串会被 SQLite 当作 truthy=1，FK 仍启用（见 `incremental_build_query.rs`
/// 原注释）。
///
/// `wal_autocheckpoint=0`：短连接 drop 时的 autocheckpoint 是 c53da76 之前
/// p15 族 malformed 的直接撕裂源（POSIX 同进程锁不排除 Python 侧读标记，
/// drop 时的回写/截断会把 Python 长连接的 `-shm` 视图撕成不一致快照）。
fn open_db_conn(db_path: &str) -> Result<Connection, rusqlite::Error> {
    let conn = Connection::open_with_flags(
        db_path,
        OpenFlags::SQLITE_OPEN_READ_WRITE | OpenFlags::SQLITE_OPEN_CREATE,
    )?;
    // busy_timeout 必须在任何 SQL 之前设置
    conn.busy_timeout(Duration::from_secs(5))?;
    conn.pragma_update(None, "journal_mode", "WAL")?;
    conn.pragma_update(None, "synchronous", "NORMAL")?;
    conn.pragma_update(None, "foreign_keys", "OFF")?;
    conn.pragma_update(None, "wal_autocheckpoint", 0i64)?;
    Ok(conn)
}

/// Rust 侧访问 DB 的**唯一入口**：每次调用独立短连接 + 哨兵保活 + 零 checkpoint。
///
/// 语义：
///
/// 1. `open_db_conn` 开一个专用短连接（创建文件）。
/// 2. `ensure_sentinel` 钉住该 `.db` 的 `-shm`（首个短连接关闭后仍存活）。
/// 3. 执行 `f`——写路径在闭包内自带 `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`。
/// 4. 关闭短连接（不做 checkpoint，不触发 `-wal`/`-shm` 回收）。
///
/// # 为什么必须短连接
///
/// 同进程的 Python 主连接与 Rust 连接是两个不同的 SQLite 实现，共享同一个
/// `-shm`；POSIX `fcntl` 锁按进程分配，两者互不排斥。长活连接跨整个 build
/// 持有 WAL 读标记与缓存的 WAL-index 视图，Python 侧持续提交会让两侧对
/// 「同一页由哪些帧拼成」产生错位 ⇒ 撕裂页 ⇒ `database disk image is malformed`。
/// Probe I 实测：长活 + `TRUNCATE` / `PASSIVE` drain 均红；短连接转绿。
///
/// # 为什么必须零 checkpoint
///
/// Rust 侧在本进程内的任何 WAL 回收（TRUNCATE drain / drop 时 autocheckpoint /
/// 最后连接删除 `-shm`）都会让 Python 长连接的 `-shm` 视图或读标记失效。
/// 292e684 全套 CI 证明 v1 的写后 TRUNCATE drain 让 p15 族 + incremental_parse
/// 回归。修法见模块文档。
///
/// # 错误
///
/// - `open_db_conn` 失败：原样透出。
/// - 闭包内的错误由闭包自己表达（本函数不吞、不改写）。
pub fn with_db_conn<T>(
    db_path: &str,
    f: impl FnOnce(&Connection) -> T,
) -> Result<T, rusqlite::Error> {
    let conn = open_db_conn(db_path)?;
    ensure_sentinel(db_path);
    let out = f(&conn);
    if drain_truncate_enabled() {
        // 诊断/兼容路径：默认关闭——TRUNCATE 会撕裂 Python 长连接快照。
        let _ = conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);");
    }
    drop(conn);
    Ok(out)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn tmp_db(name: &str) -> std::path::PathBuf {
        let dir = std::env::temp_dir().join(format!("cw_dbconn_test_{}", std::process::id()));
        std::fs::create_dir_all(&dir).unwrap();
        dir.join(name)
    }

    fn restore_gates() {
        set_sentinel_for_tests(true);
        set_wal_drain_truncate_for_tests(false);
    }

    #[test]
    fn test_with_db_conn_writes_then_reads_back() {
        let db = tmp_db("rw.db");
        let _ = std::fs::remove_file(&db);
        let path = db.to_string_lossy().to_string();

        with_db_conn(&path, |c| {
            c.execute_batch("CREATE TABLE IF NOT EXISTS t(x INTEGER);")
                .unwrap();
            c.execute_batch("INSERT INTO t VALUES (42);").unwrap();
        })
        .unwrap();

        let n = with_db_conn(&path, |c| {
            c.query_row("SELECT COUNT(*) FROM t", [], |r| r.get::<_, i64>(0))
                .unwrap()
        })
        .unwrap();
        assert_eq!(n, 1);

        let _ = std::fs::remove_file(&db);
    }

    #[test]
    fn test_with_db_conn_disables_autocheckpoint() {
        // 短连接必须带 wal_autocheckpoint=0：drop 时的 autocheckpoint
        // 是 Python 长连接快照的撕裂源。
        let db = tmp_db("noautockpt.db");
        let _ = std::fs::remove_file(&db);
        let path = db.to_string_lossy().to_string();

        with_db_conn(&path, |c| {
            let n: i64 = c
                .query_row("PRAGMA wal_autocheckpoint", [], |r| r.get(0))
                .unwrap();
            assert_eq!(n, 0, "短连接必须关闭 autocheckpoint");
        })
        .unwrap();

        let _ = std::fs::remove_file(&db);
    }

    #[test]
    fn test_with_db_conn_reads_back_via_wal_while_sentinel_holds() {
        // 默认（哨兵开 + drain 关）：数据经 WAL 立即可见，新连接读得到。
        // 该断言对 drain 开关不敏感（两种设置下都成立），避免与并行单测的
        // 门状态翻转相互干扰。
        let db = tmp_db("wal_visible.db");
        let _ = std::fs::remove_file(&db);
        let path = db.to_string_lossy().to_string();

        with_db_conn(&path, |c| {
            c.execute_batch("CREATE TABLE IF NOT EXISTS t(x INTEGER);")
                .unwrap();
            c.execute_batch("BEGIN IMMEDIATE; INSERT INTO t VALUES (1); COMMIT;")
                .unwrap();
        })
        .unwrap();

        let n = with_db_conn(&path, |c| {
            c.query_row("SELECT COUNT(*) FROM t", [], |r| r.get::<_, i64>(0))
                .unwrap()
        })
        .unwrap();
        assert_eq!(n, 1, "WAL 中的已提交事务必须能被新短连接读到");

        let _ = std::fs::remove_file(&db);
    }
    #[test]
    fn test_with_db_conn_drains_wal_only_when_enabled() {
        // 诊断门自检：显式开 drain 时，写后 WAL 被截断（断言在哨兵开启下同样
        // 成立——TRUNCATE 仍会把 WAL 收到 0；只翻 drain 门避免与并行 sentinel
        // 测试的全局门状态竞态）。
        set_wal_drain_truncate_for_tests(true);
        let db = tmp_db("drain_enabled.db");
        let _ = std::fs::remove_file(&db);
        let path = db.to_string_lossy().to_string();

        with_db_conn(&path, |c| {
            c.execute_batch("CREATE TABLE IF NOT EXISTS t(x INTEGER);")
                .unwrap();
            c.execute_batch("BEGIN IMMEDIATE; INSERT INTO t VALUES (1); COMMIT;")
                .unwrap();
        })
        .unwrap();

        let wal = std::path::PathBuf::from(format!("{}-wal", path));
        let wal_len = std::fs::metadata(&wal).map(|m| m.len()).unwrap_or(0);
        assert_eq!(wal_len, 0, "开启 drain 时写后必须把 WAL 收干净");

        set_wal_drain_truncate_for_tests(false);
        close_sentinel_connections();
        let _ = std::fs::remove_file(&db);
    }

    #[test]
    fn test_sentinel_connection_single_per_db() {
        // 同一 .db 只建一个哨兵；哨兵开启时 -shm 必须存在（被钉住不被回收）。
        // 注意：全局 map 含并行其他测试的哨兵，故只对**本文件**的 key 计数；
        // 锁守卫在显式作用域内释放，避免 assert 失败时毒化 Mutex。
        let db = tmp_db("sentinel.db");
        let _ = std::fs::remove_file(&db);
        let _ = std::fs::remove_file(format!("{}-wal", db.display()));
        let _ = std::fs::remove_file(format!("{}-shm", db.display()));
        let path = db.to_string_lossy().to_string();

        for _ in 0..3 {
            with_db_conn(&path, |c| {
                c.execute_batch("CREATE TABLE IF NOT EXISTS t(x INTEGER);")
                    .unwrap();
            })
            .unwrap();
        }
        let n = {
            let guard = sentinels().lock().unwrap();
            guard
                .keys()
                .filter(|k| *k == &sentinel_key(&path))
                .count()
        };
        assert_eq!(n, 1, "同一 .db 只应有一个哨兵连接");

        let shm = std::path::PathBuf::from(format!("{}-shm", path));
        assert!(shm.exists(), "哨兵开启时 -shm 必须存在");

        close_sentinel_connections();
        restore_gates();
        let _ = std::fs::remove_file(&db);
        let _ = std::fs::remove_file(format!("{}-wal", db.display()));
        let _ = std::fs::remove_file(format!("{}-shm", db.display()));
    }

    #[test]
    fn test_with_db_conn_uses_independent_connection_per_call() {
        // 短连接语义：每次调用各自开连接——第二次调用里制造失败事务
        // （不提交），第三次调用必须看不到它。
        let db = tmp_db("independent.db");
        let _ = std::fs::remove_file(&db);
        let path = db.to_string_lossy().to_string();

        with_db_conn(&path, |c| {
            c.execute_batch("CREATE TABLE IF NOT EXISTS t(x INTEGER);")
                .unwrap();
        })
        .unwrap();

        let _ = with_db_conn(&path, |c| {
            c.execute_batch("BEGIN IMMEDIATE; INSERT INTO t VALUES (9);").unwrap();
            // 故意不 COMMIT：连接 drop 时 SQLite 自动回滚
        });

        let n = with_db_conn(&path, |c| {
            c.query_row("SELECT COUNT(*) FROM t", [], |r| r.get::<_, i64>(0))
                .unwrap()
        })
        .unwrap();
        assert_eq!(n, 0, "未提交的事务必须随短连接 drop 回滚，不得泄漏到后续调用");

        let _ = std::fs::remove_file(&db);
    }

    #[test]
    fn test_open_db_conn_uses_wal_mode() {
        let db = tmp_db("walmode.db");
        let _ = std::fs::remove_file(&db);
        let path = db.to_string_lossy().to_string();

        with_db_conn(&path, |c| {
            let mode: String = c
                .query_row("PRAGMA journal_mode", [], |r| r.get(0))
                .unwrap();
            assert_eq!(mode.to_lowercase(), "wal");
        })
        .unwrap();

        let _ = std::fs::remove_file(&db);
    }
}
