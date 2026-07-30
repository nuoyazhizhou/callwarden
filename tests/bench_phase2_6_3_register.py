"""Phase 2-6-3 批量注册性能压测。

对比 Rust ``batch_register_files``（单事务 + 预处理语句）与 Python 逐文件循环
（``_register_file_db`` + ``_get_file_version``）的 register 阶段耗时。

运行方式::

    $env:PYTHONPATH="c:\\git_work"
    python tests/bench_phase2_6_3_register.py

输出示例::

    ===== Phase 2-6-3 批量注册性能压测 =====
    文件数: 200
    重复次数: 5

    --- Python 逐文件路径 ---
    run 1: 0.1234s
    ...
    中位数: 0.1187s

    --- Rust batch_register_files 路径 ---
    run 1: 0.0156s
    ...
    中位数: 0.0148s

    --- 对比 ---
    加速比: 8.02x
    Python: 0.1187s (200 files, 0.000594s/file)
    Rust:   0.0148s (200 files, 0.000074s/file)

关联：
    - 契约：docs/design/phase2-6-3-batch-register-contract.md
    - manifest §7 Phase 2-6-3 verify 步骤的载体
"""
from __future__ import annotations

import os
import sys
import sqlite3
import statistics
import tempfile
import time
from typing import Any, Dict, List

# 确保项目根目录在 sys.path
_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

try:
    import callwarden_core  # type: ignore
except ImportError as e:
    print(f"ERROR: callwarden_core 不可加载: {e}")
    sys.exit(1)


# 复用差分测试的 schema（与 db/schema.py 对齐，核心表子集）
_CODEGRAPH_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    root_path TEXT UNIQUE NOT NULL,
    created_at REAL NOT NULL,
    is_active INTEGER DEFAULT 0,
    description TEXT DEFAULT '',
    active_task_id TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS file_contents (
    content_hash TEXT PRIMARY KEY,
    language TEXT DEFAULT '',
    total_lines INTEGER DEFAULT 0,
    first_seen_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS file_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    abs_path TEXT NOT NULL,
    current_content_hash TEXT DEFAULT '',
    mtime REAL NOT NULL,
    total_lines INTEGER DEFAULT 0,
    last_parsed REAL DEFAULT 0,
    status TEXT DEFAULT 'pending',
    module_path TEXT DEFAULT '',
    FOREIGN KEY (workspace_id) REFERENCES workspaces(id),
    FOREIGN KEY (current_content_hash) REFERENCES file_contents(content_hash),
    UNIQUE(workspace_id, rel_path)
);
CREATE TABLE IF NOT EXISTS file_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    version_num INTEGER NOT NULL,
    content_hash TEXT NOT NULL,
    mtime REAL NOT NULL,
    total_lines INTEGER DEFAULT 0,
    parsed_at REAL NOT NULL,
    is_current INTEGER DEFAULT 1,
    is_deleted INTEGER DEFAULT 0,
    commit_hash TEXT DEFAULT '',
    ast_cache BLOB DEFAULT NULL,
    FOREIGN KEY (file_instance_id) REFERENCES file_instances(id),
    FOREIGN KEY (content_hash) REFERENCES file_contents(content_hash)
);
CREATE INDEX IF NOT EXISTS idx_file_versions_instance ON file_versions(file_instance_id);
CREATE INDEX IF NOT EXISTS idx_file_versions_current ON file_versions(is_current);
"""


def _make_codegraph_db(db_path: str) -> None:
    """构建测试用 CodeGraph DB（核心表，与 schema.py 对齐）"""
    conn = sqlite3.connect(db_path)
    conn.executescript(_CODEGRAPH_SCHEMA_DDL)
    conn.execute(
        "INSERT INTO workspaces (id, name, root_path, created_at, is_active, description) "
        "VALUES (1, 'bench_ws', '/tmp/bench', 0, 1, 'benchmark')"
    )
    conn.commit()
    conn.close()


def _python_register_file_db(conn: sqlite3.Connection, workspace_id: int, abs_path: str,
                              module_path: str, rel_path: str, mtime: float) -> int:
    """Python 真相源：_register_file_db 核心逻辑（db_build.py）"""
    cur = conn.execute(
        "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = ?",
        (workspace_id, rel_path),
    )
    row = cur.fetchone()
    if row:
        conn.execute(
            "UPDATE file_instances SET mtime = ?, module_path = ?, status = 'pending' WHERE id = ?",
            (mtime, module_path, row[0]),
        )
        return row[0]
    else:
        cur = conn.execute(
            """INSERT INTO file_instances
               (workspace_id, rel_path, abs_path, current_content_hash, mtime, total_lines, last_parsed, status, module_path)
               VALUES (?, ?, ?, '', ?, 0, 0, 'pending', ?)""",
            (workspace_id, rel_path, abs_path, mtime, module_path),
        )
        return cur.lastrowid


def _python_get_file_version(conn: sqlite3.Connection, file_instance_id: int):
    """Python 真相源：_get_file_version 核心逻辑"""
    cur = conn.execute(
        "SELECT * FROM file_versions WHERE file_instance_id = ? ORDER BY version_num DESC LIMIT 1",
        (file_instance_id,),
    )
    return cur.fetchone()


def _gen_test_files(n: int) -> List[Dict[str, Any]]:
    """生成 n 个测试文件信息"""
    files = []
    for i in range(n):
        files.append({
            "rel_path": f"src/module_{i // 50}/file_{i}.py",
            "abs_path": f"/proj/src/module_{i // 50}/file_{i}.py",
            "module_path": f"module_{i // 50}",
            "mtime": 1000000.0 + i * 1.0,
        })
    return files


def _bench_python(files: List[Dict[str, Any]], db_path: str) -> float:
    """Python 逐文件路径：_register_file_db + _get_file_version 循环"""
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM file_instances")
    conn.execute("DELETE FROM file_versions")
    conn.commit()

    t0 = time.perf_counter()
    for f in files:
        fid = _python_register_file_db(conn, 1, f["abs_path"], f["module_path"],
                                        f["rel_path"], f["mtime"])
        _python_get_file_version(conn, fid)
    conn.commit()
    elapsed = time.perf_counter() - t0
    conn.close()
    return elapsed


def _bench_rust(files: List[Dict[str, Any]], db_path: str) -> float:
    """Rust batch_register_files 路径：单事务 + 预处理语句"""
    # 清空 DB（用 Python 连接清空，因为 Rust API 不做清理）
    conn = sqlite3.connect(db_path)
    conn.execute("DELETE FROM file_instances")
    conn.execute("DELETE FROM file_versions")
    conn.commit()
    conn.close()

    t0 = time.perf_counter()
    result = callwarden_core.batch_register_files(db_path, 1, files, skip_version_lookup=False)
    elapsed = time.perf_counter() - t0

    if not result.get("success"):
        raise RuntimeError(f"Rust batch_register_files 失败: {result.get('error')}")

    return elapsed


def _run_bench(n_files: int = 200, n_runs: int = 5) -> None:
    """运行性能压测"""
    print(f"===== Phase 2-6-3 批量注册性能压测 =====")
    print(f"文件数: {n_files}")
    print(f"重复次数: {n_runs}")
    print()

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = os.path.join(tmpdir, "bench.db")
        _make_codegraph_db(db_path)
        files = _gen_test_files(n_files)

        # Python 路径
        print("--- Python 逐文件路径 ---")
        py_times = []
        for i in range(n_runs):
            t = _bench_python(files, db_path)
            py_times.append(t)
            print(f"  run {i+1}: {t:.4f}s")
        py_median = statistics.median(py_times)
        print(f"  中位数: {py_median:.4f}s")
        print()

        # Rust 路径
        print("--- Rust batch_register_files 路径 ---")
        rust_times = []
        for i in range(n_runs):
            t = _bench_rust(files, db_path)
            rust_times.append(t)
            print(f"  run {i+1}: {t:.4f}s")
        rust_median = statistics.median(rust_times)
        print(f"  中位数: {rust_median:.4f}s")
        print()

        # 对比
        speedup = py_median / rust_median if rust_median > 0 else float("inf")
        print("--- 对比 ---")
        print(f"  加速比: {speedup:.2f}x")
        print(f"  Python: {py_median:.4f}s ({n_files} files, {py_median/n_files:.6f}s/file)")
        print(f"  Rust:   {rust_median:.4f}s ({n_files} files, {rust_median/n_files:.6f}s/file)")
        print()

        # 数据一致性验证（可选）
        # 用 Python 和 Rust 分别注册，对比 DB 状态
        print("--- 数据一致性验证 ---")
        db_py = os.path.join(tmpdir, "py.db")
        db_rust = os.path.join(tmpdir, "rust.db")
        _make_codegraph_db(db_py)
        _make_codegraph_db(db_rust)

        # Python 路径
        conn_py = sqlite3.connect(db_py)
        for f in files:
            fid = _python_register_file_db(conn_py, 1, f["abs_path"], f["module_path"],
                                            f["rel_path"], f["mtime"])
            _python_get_file_version(conn_py, fid)
        conn_py.commit()
        conn_py.close()

        # Rust 路径
        callwarden_core.batch_register_files(db_rust, 1, files, skip_version_lookup=False)

        # 对比
        conn_py = sqlite3.connect(db_py)
        conn_rust = sqlite3.connect(db_rust)
        py_rows = conn_py.execute(
            "SELECT workspace_id, rel_path, abs_path, current_content_hash, mtime, "
            "total_lines, last_parsed, status, module_path "
            "FROM file_instances ORDER BY rel_path"
        ).fetchall()
        rust_rows = conn_rust.execute(
            "SELECT workspace_id, rel_path, abs_path, current_content_hash, mtime, "
            "total_lines, last_parsed, status, module_path "
            "FROM file_instances ORDER BY rel_path"
        ).fetchall()
        conn_py.close()
        conn_rust.close()

        if py_rows == rust_rows:
            print(f"  ✅ file_instances 表完全一致（{len(py_rows)} 行）")
        else:
            print(f"  ❌ file_instances 表不一致！Python {len(py_rows)} 行, Rust {len(rust_rows)} 行")
            for i, (p, r) in enumerate(zip(py_rows, rust_rows)):
                if p != r:
                    print(f"    行 {i}: Python={p}")
                    print(f"    行 {i}: Rust  ={r}")
                    if i >= 3:
                        print("    ...（更多差异省略）")
                        break


if __name__ == "__main__":
    _run_bench(n_files=200, n_runs=5)
