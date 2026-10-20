"""CI-DEBT 深水区 A/B 最小复现 + 修复对照（临时，实验后删除）。

背景：Probe A（CW_USE_RUST_STORAGE=0）证明 phase7 ×8 disk I/O error 与 Rust
扩展无关——纯 Python 路径下仍复现。嫌疑集中在同进程两条 sqlite3 连接 +
WAL + DDL（POSIX fcntl 锁按进程分配，同进程两连接无法互斥 WAL-index）。

本文件把嫌疑拆成可判读的最小实验（全部在 CI Linux 上跑，Windows 免疫）：

  test_dual_conn_with_pragma_block  复刻 JobExecutor.start() 的连接序列
                                  （第二条连接 + PRAGMA 批次 + executescript）
  test_dual_conn_bare              第二条连接仅 executescript（无 PRAGMA 批次）
  test_dual_conn_mmap              第二条连接 + mmap_size PRAGMA（分离 mmap 嫌疑）
  test_shared_conn                 单连接共享（修复对照：应绿）

判读：前三个中任一红 = 该因素是 IOERR 根因链一环；shared_conn 绿 = 连接
合并是有效修复方向。
"""
import os
import sqlite3
import tempfile


def _fresh_db():
    """建库 + 第一条连接设 WAL + 建表（模拟 CodeGraphDB conn A）。"""
    path = os.path.join(tempfile.mkdtemp(), "repro.db")
    a = sqlite3.connect(path)
    a.execute("PRAGMA journal_mode=WAL")
    a.execute("CREATE TABLE IF NOT EXISTS base(x)")
    a.commit()
    return a, path


_JOBS_DDL = (
    "CREATE TABLE IF NOT EXISTS jobs (id INTEGER PRIMARY KEY AUTOINCREMENT, "
    "job_id TEXT NOT NULL UNIQUE, workspace_id INTEGER NOT NULL, "
    "job_type TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending');"
    "CREATE INDEX IF NOT EXISTS idx_jobs_workspace ON jobs(workspace_id);"
)


def test_dual_conn_with_pragma_block():
    """复刻 JobExecutor.start()：第二条连接 + PRAGMA 批次 + executescript。"""
    a, path = _fresh_db()
    try:
        b = sqlite3.connect(path, timeout=30.0, check_same_thread=False)
        b.row_factory = sqlite3.Row
        for pragma in (
            "PRAGMA journal_mode=WAL",
            "PRAGMA busy_timeout=5000",
            "PRAGMA synchronous=NORMAL",
            "PRAGMA wal_autocheckpoint=1000",
            "PRAGMA cache_size=-262144",
            "PRAGMA mmap_size=268435456",
            "PRAGMA temp_store=MEMORY",
        ):
            b.execute(pragma)
        b.executescript(_JOBS_DDL)  # ← CI 上此处 disk I/O error
        b.commit()
        assert b.execute("SELECT COUNT(*) c FROM jobs").fetchone()[0] == 0
    finally:
        a.close()


def test_dual_conn_bare():
    """第二条连接仅 executescript（无 PRAGMA 批次）——分离 PRAGMA 批次嫌疑。"""
    a, path = _fresh_db()
    try:
        b = sqlite3.connect(path)
        b.executescript(_JOBS_DDL)
        b.commit()
        assert b.execute("SELECT COUNT(*) c FROM jobs").fetchone()[0] == 0
    finally:
        a.close()


def test_dual_conn_mmap():
    """第二条连接 + mmap_size PRAGMA——分离 mmap 嫌疑。"""
    a, path = _fresh_db()
    try:
        b = sqlite3.connect(path)
        b.execute("PRAGMA mmap_size=268435456")
        b.executescript(_JOBS_DDL)
        b.commit()
        assert b.execute("SELECT COUNT(*) c FROM jobs").fetchone()[0] == 0
    finally:
        a.close()


def test_shared_conn():
    """修复对照：单连接（不开第二条）执行同一 DDL——应绿。"""
    a, _path = _fresh_db()
    try:
        a.executescript(_JOBS_DDL)
        a.commit()
        assert a.execute("SELECT COUNT(*) c FROM jobs").fetchone()[0] == 0
    finally:
        a.close()
