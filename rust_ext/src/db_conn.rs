//! Rust 侧 SQLite 连接管理（同进程双实例 WAL Hazard 正式修法 v3）
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
//! # 正式修法 v2/v3：短连接 + 哨兵 + autocheckpoint=0 + 温和 drain
//!
//! v2（「零 checkpoint」）在 CI 上翻车：h7 族 12 项以 **Rust 侧打开数据库
//! 即 malformed** 失败（`batch_resolve_and_save_calls`：打开 CodeGraph
//! 数据库失败: database disk image is malformed）。原因：全新的 Rust 短连接
//! 面对一个带着 Python 侧遗留 `-wal`/`-shm` 状态的库时，必须靠 **open 期
//! PASSIVE checkpoint** 触发 wal-index 重建，否则读到过期 wal-index ⇒ 拼出
//! 撕裂页。v1 正是靠 open 期 PASSIVE + 写后 TRUNCATE 让 h7 族转绿。
//!
//! v3 在三者间取交集——**温和 drain**：
//!
//! 1. **短连接**（v1 即 h7 族修复的必要条件；长活池是 c53da76 的根因载体）。
//! 2. **open 期 `wal_checkpoint(PASSIVE)`**：每次 open 触发 wal-index 重建，
//!    保住 h7 族（`CW_RUST_OPEN_CHECKPOINT=0` 可关，默认开）。
//! 3. **哨兵连接**：每个 `.db` 常驻一个永不关闭的本侧连接，钉住 `-shm`
//!    引用计数 ≥ 1 ⇒ 短连接关闭序列不会触发「最后连接」的 `-wal`/`-shm`
//!    回收（`CW_RUST_SENTINEL_CONN=0` 可关，默认开）。
//! 4. **`PRAGMA wal_autocheckpoint=0`**：短连接 drop 不再 autocheckpoint
//!    （c53da76 之前 p15 族失败的直接撕裂源）。
//! 5. **写后 PASSIVE drain（默认）**：只回写可安全回写的帧、不强制截断
//!    ——与 TRUNCATE（v1）的区别在于**不在有读者持有读标记时强行清空
//!    WAL**。`CW_RUST_WAL_DRAIN=truncate`（v1 行为）/ `off`（v2 行为）
//!    保留为诊断复现臂（CI Probe B/G 实时对照）。
//!
//! WAL 累积由 Python 侧 autocheckpoint（默认 1000 页、单实例操作）回收。
//!
//! ## v3 CI 判读（run 39788826909，2026-10-22）：h7 未转绿
//!
//! v3 恢复 open 期 PASSIVE + 写后 PASSIVE 后，h7 族仍以「Rust 侧打开
//! 数据库即 malformed」失败（h7 7 + agent_integration 2 + e2e_triangle 1
//! + incremental 1 + dashboard 8 errors；`--maxfail=10` 早停，p15 未跑到）。
//! ⇒ v2 的「缺 open 期 PASSIVE」归因被推翻：v1（h7 绿）与 v2/v3（h7 红）
//! 的差异集是 **{哨兵 on/off, autocheckpoint 0/1000, TRUNCATE/PASSIVE}**。
//! 核心嫌疑 = **哨兵连接钉 -shm**：Rust 后续短连接读到的是 Python 侧系统
//! libsqlite3 写入的外来 `-shm` 视图 ⇒ 打开时 wal-index 错位。v1 无哨兵时
//! 短连接 close 删除 `-wal`/`-shm`，下次 open 从干净状态重建 wal-index
//! （h7 治愈路径），代价是删 `-shm` 撕 Python 长连接（v1 的 p15 撕裂路径）。
//! **h7 与 p15 需求互斥——双实例前提下「两族从未同时绿过」不可逾越；**
//! 根本的唯一交集 = 同进程单一 libsqlite3 实例（Rust 侧改链接系统 sqlite3，
//! 两侧共用同一 VFS/`-shm` 映射；旁证：CI 上同进程双 Python 连接
//! Probe D 持续全绿）。哨兵隔离臂由 ci.yml Probe H/I 判读。
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
//! 每次调用多一次 open/close；WAL 不由 Rust 侧 TRUNCATE 回收，长度交给
//! Python 侧 autocheckpoint 控制（测试场景短暂、无实际增长风险）。

use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicBool, AtomicU8, Ordering};
use std::sync::{Mutex, OnceLock};
use std::time::Duration;

use rusqlite::{Connection, OpenFlags};

// ============================================
// 运维 / 诊断门
// ============================================

/// 哨兵开关的环境覆盖（TESTS 覆盖时跳过环境读取，保证单测确定性）。
static SENTINEL_OVERRIDE: AtomicBool = AtomicBool::new(false);
static SENTINEL_ENABLED: AtomicBool = AtomicBool::new(true);

/// 写后 drain 模式：0=off（v2 行为）、1=passive（默认，v3）、2=truncate（v1 行为）。
static DRAIN_OVERRIDE: AtomicBool = AtomicBool::new(false);
static DRAIN_MODE: AtomicU8 = AtomicU8::new(1);

const DRAIN_OFF: u8 = 0;
const DRAIN_PASSIVE: u8 = 1;
const DRAIN_TRUNCATE: u8 = 2;

fn sentinel_enabled() -> bool {
    if SENTINEL_OVERRIDE.load(Ordering::Relaxed) {
        SENTINEL_ENABLED.load(Ordering::Relaxed)
    } else {
        std::env::var("CW_RUST_SENTINEL_CONN").as_deref() != Ok("0")
    }
}

fn drain_mode() -> u8 {
    if DRAIN_OVERRIDE.load(Ordering::Relaxed) {
        DRAIN_MODE.load(Ordering::Relaxed)
    } else {
        match std::env::var("CW_RUST_WAL_DRAIN").as_deref() {
            Ok("off") => DRAIN_OFF,
            Ok("truncate") => DRAIN_TRUNCATE,
            _ => DRAIN_PASSIVE,
        }
    }
}

/// 单测 / 诊断用：编程式覆盖哨兵开关（进程内立即生效，绕过环境变量）。
pub fn set_sentinel_for_tests(enabled: bool) {
    SENTINEL_ENABLED.store(enabled, Ordering::Relaxed);
    SENTINEL_OVERRIDE.store(true, Ordering::Relaxed);
}

/// 单测 / 诊断用：编程式覆盖写后 drain 模式（"off" / "passive" / "truncate"）。
pub fn set_wal_drain_for_tests(mode: &str) {
    let m = match mode {
        "off" => DRAIN_OFF,
        "truncate" => DRAIN_TRUNCATE,
        _ => DRAIN_PASSIVE,
    };
    DRAIN_MODE.store(m, Ordering::Relaxed);
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
/// autocheckpoint；open 期执行一次 PASSIVE checkpoint。
///
/// 顺序与历史各模块自带的 `open_readwrite` 保持一致：
/// busy_timeout → journal_mode=WAL → synchronous=NORMAL → foreign_keys=OFF
/// → wal_autocheckpoint=0 → wal_checkpoint(PASSIVE)。
///
/// `foreign_keys` 必须用 `execute_batch` 传裸 `OFF` 关键字：`pragma_update`
/// 传字符串会被 SQLite 当作 truthy=1，FK 仍启用（见 `incremental_build_query.rs`
/// 原注释）。
///
/// `wal_autocheckpoint=0`：短连接 drop 时的 autocheckpoint 是 c53da76 之前
/// p15 族 malformed 的直接撕裂源（POSIX 同进程锁不排除 Python 侧读标记，
/// drop 时的回写/截断会把 Python 长连接的 `-shm` 视图撕成不一致快照）。
///
/// **open 期 PASSIVE checkpoint**（`CW_RUST_OPEN_CHECKPOINT=0` 可关）：
/// 全新短连接面对 Python 侧遗留的 `-wal`/`-shm` 状态时，必须触发一次
/// wal-index 重建才能拿到一致快照——v2（零 checkpoint）的 CI 实测中，
/// h7 族 12 项正是因缺这一步而以「打开数据库即 malformed」失败。
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
    if std::env::var("CW_RUST_OPEN_CHECKPOINT").as_deref() != Ok("0") {
        let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
    }
    Ok(conn)
}

/// Rust 侧访问 DB 的**唯一入口**：短连接 + open 期 PASSIVE + 哨兵 +
/// autocheckpoint=0 + 写后温和 drain（v3）。
///
/// 语义：
///
/// 1. `open_db_conn` 开一个专用短连接（创建文件）；open 期的 PASSIVE
///    checkpoint 触发 wal-index 重建——面对 Python 侧遗留的 `-wal`/`-shm`
///    状态时这是 h7 族不 malformed 的必要步骤（v2 翻车的直接原因）。
/// 2. `ensure_sentinel` 钉住该 `.db` 的 `-shm`（本侧任何连接关闭序列都
///    不再触发「最后连接」的 `-wal`/`-shm` 回收）。
/// 3. 执行 `f`——写路径在闭包内自带 `BEGIN IMMEDIATE` / `COMMIT` / `ROLLBACK`。
/// 4. 写后按 `drain_mode()` 处理：PASSIVE（默认）温和回写不截断；
///    TRUNCATE（`CW_RUST_WAL_DRAIN=truncate`，v1 诊断复现臂）截断；
///    off（v2 行为）跳过。
/// 5. 关闭短连接（`wal_autocheckpoint=0`，drop 不做 autocheckpoint）。
///
/// # 为什么必须短连接
///
/// 同进程的 Python 主连接与 Rust 连接是两个不同的 SQLite 实现，共享同一个
/// `-shm`；POSIX `fcntl` 锁按进程分配，两者互不排斥。长活连接跨整个 build
/// 持有 WAL 读标记与缓存的 WAL-index 视图，Python 侧持续提交会让两侧对
/// 「同一页由哪些帧拼成」产生错位 ⇒ 撕裂页 ⇒ `database disk image is malformed`。
/// Probe I 实测：长活 + `TRUNCATE` / `PASSIVE` drain 均红；短连接转绿。
///
/// # 为什么 drain 只能温和（v3 对 v1/v2 的修正）
///
/// - v1（写后 TRUNCATE）：撕裂 Python 长连接快照 ⇒ p15 族 + incremental_parse
///   回归（292e684 全套 CI 实证）。
/// - v2（零 checkpoint）：h7 族 12 项以「打开即 malformed」失败（open 期缺
///   wal-index 重建）。
/// - v3：open 期 PASSIVE 保 h7；写后 PASSIVE 只回写无读者冲突的帧、不强制
///   截断，故不撕 p15；哨兵 + `wal_autocheckpoint=0` 杜绝 drop 期回收。
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
    match drain_mode() {
        DRAIN_TRUNCATE => {
            // 诊断复现臂（v1 行为）：截断 WAL——会撕裂 Python 长连接快照。
            let _ = conn.execute_batch("PRAGMA wal_checkpoint(TRUNCATE);");
        }
        DRAIN_PASSIVE => {
            // 温和 drain（v3 默认）：回写可安全回写的帧，不强制截断。
            let _ = conn.execute_batch("PRAGMA wal_checkpoint(PASSIVE);");
        }
        _ => {}
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
        // DRAIN 门由 test_with_db_conn_drain_mode_matrix 单一测试串行独占，
        // 这里不恢复，避免与并行 drain 矩阵测试的全局门状态竞态。
        set_sentinel_for_tests(true);
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
        // 默认（哨兵开 + passive drain）：数据经 WAL 立即可见，新连接读得到。
        // 该断言对 drain 开关不敏感（三种设置下都成立），避免与并行单测的
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
    fn test_with_db_conn_drain_mode_matrix() {
        // drain 模式矩阵（单测内串行持有 DRAIN 门，避免与并行测试竞态）：
        // - truncate（v1 诊断臂）：写后 WAL 被截断到 0；
        // - passive（v3 默认）：帧回写进主库，但 -wal 文件不被截断；
        // - off（v2 行为）：零 checkpoint 下已提交事务仍立即可读。
        let mk = |name: &str| {
            let db = tmp_db(name);
            let _ = std::fs::remove_file(&db);
            db.to_string_lossy().to_string()
        };
        let write = |path: &str| {
            with_db_conn(path, |c| {
                c.execute_batch("CREATE TABLE IF NOT EXISTS t(x INTEGER);")
                    .unwrap();
                c.execute_batch("BEGIN IMMEDIATE; INSERT INTO t VALUES (1); COMMIT;")
                    .unwrap();
            })
            .unwrap();
        };
        let wal_len = |path: &str| {
            std::fs::metadata(format!("{}-wal", path))
                .map(|m| m.len())
                .unwrap_or(0)
        };
        let count = |path: &str| {
            with_db_conn(path, |c| {
                c.query_row("SELECT COUNT(*) FROM t", [], |r| r.get::<_, i64>(0))
                    .unwrap()
            })
            .unwrap()
        };

        set_wal_drain_for_tests("truncate");
        let p = mk("drain_truncate.db");
        write(&p);
        assert_eq!(wal_len(&p), 0, "TRUNCATE drain 必须把 WAL 收干净");
        assert_eq!(count(&p), 1, "TRUNCATE 后数据必须可读");

        set_wal_drain_for_tests("passive");
        let p2 = mk("drain_passive.db");
        write(&p2);
        assert!(
            wal_len(&p2) > 0,
            "PASSIVE drain 只回写不截断——-wal 文件必须保留"
        );
        assert_eq!(count(&p2), 1, "PASSIVE drain 后数据必须立即可读");

        set_wal_drain_for_tests("off");
        let p3 = mk("drain_off.db");
        write(&p3);
        assert_eq!(count(&p3), 1, "零 checkpoint（off）下已提交事务也必须可读");

        set_wal_drain_for_tests("passive");
        close_sentinel_connections();
        for p in [p, p2, p3] {
            let _ = std::fs::remove_file(&p);
            let _ = std::fs::remove_file(format!("{}-wal", p));
            let _ = std::fs::remove_file(format!("{}-shm", p));
        }
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
