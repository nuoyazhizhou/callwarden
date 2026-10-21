//! Rust 侧 SQLite 连接管理（CI-DEBT 深水区：同进程双实例 WAL Hazard 正式修法）
//!
//! # 缺陷现象
//!
//! CI（ubuntu）一族测试失败于 `database disk image is malformed`（Windows 免疫，
//! 只能靠 CI 判读）。受害面随「谁先读到损坏页」而漂移：
//!
//! - c53da76 之前（Rust 侧每次调用 open→drop）：Python 主连接先读到 ⇒ p15 族
//!   6 项 + `test_incremental_parse` 1 项。
//! - c53da76 之后（Rust 侧进程级长活池连接）：Rust 连接先读到 ⇒ h7 族 7 项 +
//!   agent_rules 4 项 + agent_integration 2 项 + e2e_triangle 1 项 + dashboard 12 项。
//!
//! # 根因（Probe I / G / H 实测定论）
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
//! Probe I 把「长活连接」与「WAL 累积」两个变量拆开（`ci.yml` 的 Probe I 步骤）：
//!
//! | 配置 | 结果 |
//! |---|---|
//! | I1 长活连接 + 每次调用后 `wal_checkpoint(TRUNCATE)` | **红**（7 failed）|
//! | I4 长活连接 + 每次调用后 `wal_checkpoint(PASSIVE)`  | **红**（7 failed）|
//! | I2 全模块短连接 + drain                              | **绿**（8 passed）|
//!
//! ⇒ **WAL drain 不能替代「关连接」**：drain 只把 WAL 帧回写主库并清空 WAL，
//! **不刷新长活连接已缓存的 WAL-index 视图与 WAL 读标记**。长活连接跨整个
//! build 生命周期持有该读标记，而 Python 主连接在其间持续提交新事务推进
//! `mxFrame`，两侧对同一页的「帧集合」认知就会错位 ⇒ 拼出非一致快照的页镜像
//! ⇒ 撕裂页 ⇒ `malformed`（同一机制在 TEXT 载荷上表现为非法 UTF-8）。
//!
//! Probe H（逐模块回退矩阵）显示：单独关掉任一模块仍 malform，**全部关掉才
//! 全绿** ⇒ malform 由「连接长活」这一性质本身引起，与某模块的写路径细节无关。
//!
//! # 正式修法
//!
//! **Rust 侧访问 DB 一律走「每次调用独立短连接 + 写后 WAL drain」**：
//!
//! 1. **短连接**：`open → 事务 → COMMIT/ROLLBACK → close`。
//!    每次重开都重新读 `-shm` 头重建 WAL-index 视图与读标记 ⇒ 读到的一定是
//!    一致快照。Probe I2 / G1 / G2 / H-control 四处独立观测均为绿。
//! 2. **写后 `wal_checkpoint(TRUNCATE)`**：把 Rust 写入的帧**全量**回写主库并
//!    清空 WAL ⇒ Python 侧后续的 autocheckpoint 不再需要与 Rust 遗留的帧交错，
//!    撕裂窗口消失。此项不可省：c53da76 之前的「短连接但只在 open 期做
//!    PASSIVE checkpoint」在 p15 族 6 项 + incremental_parse 1 项上仍以
//!    Python 侧 `malformed` 失败；只有「短连接 + TRUNCATE drain」全绿。
//!
//! 读路径另走 `crate::cas_query::open_readonly_bounded`（只读短连接、8s 有界
//! 超时），且按 AGENTS.md 规则 32 **不执行** checkpoint——只读连接经 WAL +
//! `-shm` 总能读到最新已提交数据。
//!
//! # 代价
//!
//! 相对 c53da76 的长活池：每次 Rust 调用多一次 open/close 与一次 TRUNCATE
//! checkpoint（`synchronous=NORMAL` 下 checkpoint 不同步落盘，成本为页拷贝 +
//! WAL 截断，远小于一次 fsync）。c53da76 引入池化本为消除双实例 hazard，
//! 实测证明「单实例长活」反而制造了撕裂窗口——**正确性优先于省一次 open**。
//!
//! # 进程级连接池已移除
//!
//! c53da76 引入的 `POOL` / `pool_key` / `PoolScope`（重入检测）/ `close_pooled_connections`
//! 及其诊断门（`CW_RUST_POOL_OFF` / `CW_RUST_POOL_LIFETIME` / `CW_RUST_POOL_WAL_DRAIN`）
//! 随本修法一并移除：长活连接既是根因载体，这些门就没有保留价值。
//! `CW_RUST_OPEN_CHECKPOINT` 保留为**运维开关**（见 `open_db_conn` 注释）。

use std::time::Duration;

use rusqlite::{Connection, OpenFlags};

/// 打开一个专用连接，并执行与 Python `db_base.py` 对齐的 PRAGMA 序列。
///
/// 顺序与历史各模块自带的 `open_readwrite` 保持一致：
/// busy_timeout → journal_mode=WAL → synchronous=NORMAL → foreign_keys=OFF
/// → wal_checkpoint(PASSIVE)。
///
/// `foreign_keys` 必须用 `execute_batch` 传裸 `OFF` 关键字：`pragma_update` 传字符串
/// 会被 SQLite 当作 truthy=1，FK 仍启用（见 `incremental_build_query.rs` 原注释）。
///
/// `CW_RUST_OPEN_CHECKPOINT=0` 可关闭 open 期的 `wal_checkpoint(PASSIVE)`。
/// **定位：运维开关**，不是诊断门——用于在磁盘/文件系统异常时排除 checkpoint
/// 路径（如某些网络盘上 WAL checkpoint 会失败）。默认执行。
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
    if std::env::var("CW_RUST_OPEN_CHECKPOINT").as_deref() != Ok("0") {
        let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
    }
    Ok(conn)
}

/// Rust 侧访问 DB 的**唯一入口**：每次调用独立短连接，写后把 WAL 收干净。
///
/// 语义（与 c53da76 之前的历史实现一致，但补上了写后 drain）：
///
/// 1. `open_db_conn` 开一个专用连接（PRAGMA 与 Python 侧对齐）。
/// 2. 执行 `f`——写路径在闭包内自带 `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`。
/// 3. `PRAGMA wal_checkpoint(TRUNCATE)` 把本次写入的帧全量回写主库并清空 WAL。
/// 4. 关闭连接（最后一个连接关闭时 SQLite 还会自动 checkpoint 并删除
///    `-wal` / `-shm`）。
///
/// # 为什么必须短连接
///
/// 同进程的 Python 主连接与 Rust 连接是两个不同的 SQLite 实现，共享同一个
/// `-shm`；POSIX `fcntl` 锁按进程分配，两者互不排斥。长活连接跨整个 build
/// 持有 WAL 读标记与缓存的 WAL-index 视图，Python 侧持续提交会让两侧对
/// 「同一页由哪些帧拼成」产生错位 ⇒ 撕裂页 ⇒ `database disk image is malformed`。
/// Probe I 实测：长活 + `TRUNCATE` drain 仍红（drain 不刷新长活连接的 WAL-index
/// 视图），全模块短连接转绿。详见模块文档。
///
/// # 错误
///
/// - `open_db_conn` 失败：原样透出。
/// - 闭包内的错误由闭包自己表达（本函数不吞、不改写）。
pub fn with_db_conn<T>(db_path: &str, f: impl FnOnce(&Connection) -> T) -> Result<T, rusqlite::Error> {
    let conn = open_db_conn(db_path)?;
    let out = f(&conn);
    // 写后 drain：把 WAL 帧全量回写主库并清空 WAL（best-effort，失败忽略——
    // 清理路径不应因 checkpoint 失败而抛错，更不得 panic）。
    let _ = conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);");
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
    fn test_with_db_conn_drains_wal_after_write() {
        let db = tmp_db("drain.db");
        let _ = std::fs::remove_file(&db);
        let path = db.to_string_lossy().to_string();

        with_db_conn(&path, |c| {
            c.execute_batch("CREATE TABLE IF NOT EXISTS t(x INTEGER);")
                .unwrap();
            c.execute_batch("BEGIN IMMEDIATE; INSERT INTO t VALUES (1); COMMIT;")
                .unwrap();
        })
        .unwrap();

        // 写后 drain ⇒ WAL 已被清空（不存在或长度为 0）
        let wal = std::path::PathBuf::from(format!("{}-wal", path));
        let wal_len = std::fs::metadata(&wal).map(|m| m.len()).unwrap_or(0);
        assert_eq!(wal_len, 0, "写后必须把 WAL 收干净，否则 Python 侧 autocheckpoint 会与之交错");

        // drain 后数据仍在主库文件里（新连接必须读得到）
        let n = with_db_conn(&path, |c| {
            c.query_row("SELECT COUNT(*) FROM t", [], |r| r.get::<_, i64>(0))
                .unwrap()
        })
        .unwrap();
        assert_eq!(n, 1);

        let _ = std::fs::remove_file(&db);
    }

    #[test]
    fn test_with_db_conn_uses_independent_connection_per_call() {
        // 短连接语义：每次调用各自开连接——第二次调用必须仍能读到第一次的写入，
        // 且不能被第一次的失败事务污染。
        let db = tmp_db("independent.db");
        let _ = std::fs::remove_file(&db);
        let path = db.to_string_lossy().to_string();

        with_db_conn(&path, |c| {
            c.execute_batch("CREATE TABLE IF NOT EXISTS t(x INTEGER);")
                .unwrap();
        })
        .unwrap();

        // 第二次调用里制造一次失败事务（不提交），第三次调用必须看不到它
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
