"""L5 resolved_edges 计算引擎测试

测试内容：
1. limit 参数修复（get_resolved_edges 支持 limit）
2. 降级模式：从 calls 表复制（resolution_method="from_calls"）
3. CAS 模式：从 cas_raw_calls 解析（4 级 resolution_method）
4. build_context 不存在时返回 error
5. CLI resolve 子命令端到端
"""
import os
import sys
import sqlite3
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


# ============================================
# 测试夹具：临时 DB + schema
# ============================================

@pytest.fixture
def temp_db():
    """pytest fixture：创建临时内存 DB"""
    conn = _make_temp_db()
    yield conn
    conn.close()


def _make_temp_db():
    """创建临时内存 DB，初始化必要的表（供 fixture 和 main 入口共用）"""
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")

    # workspaces 表（toolchain schema 的外键依赖）
    conn.execute("""CREATE TABLE IF NOT EXISTS workspaces (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT UNIQUE NOT NULL,
        root_path TEXT NOT NULL,
        created_at REAL NOT NULL
    )""")
    conn.execute("INSERT INTO workspaces (id, name, root_path, created_at) VALUES (1, 'test_ws', '/test', 0)")

    # file_instances 表
    conn.execute("""CREATE TABLE IF NOT EXISTS file_instances (
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
        UNIQUE(workspace_id, rel_path)
    )""")

    # symbols 表
    conn.execute("""CREATE TABLE IF NOT EXISTS symbols (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_instance_id INTEGER NOT NULL,
        symbol_hash TEXT NOT NULL,
        name TEXT NOT NULL,
        kind TEXT NOT NULL,
        visibility TEXT DEFAULT 'private',
        start_line INTEGER NOT NULL,
        end_line INTEGER NOT NULL,
        start_col INTEGER DEFAULT 0,
        end_col INTEGER DEFAULT 0,
        signature TEXT DEFAULT '',
        has_comment INTEGER DEFAULT 0,
        comment_status TEXT DEFAULT 'pending',
        module_path TEXT DEFAULT '',
        qualified_name TEXT DEFAULT '',
        depth INTEGER DEFAULT -1,
        FOREIGN KEY (file_instance_id) REFERENCES file_instances(id)
    )""")

    # calls 表
    conn.execute("""CREATE TABLE IF NOT EXISTS calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        caller_id INTEGER NOT NULL,
        caller_name TEXT NOT NULL,
        caller_module TEXT NOT NULL,
        callee_name TEXT NOT NULL,
        callee_module TEXT DEFAULT '',
        callee_qualified TEXT DEFAULT '',
        callee_file TEXT DEFAULT '',
        callee_id INTEGER DEFAULT 0,
        call_line INTEGER DEFAULT 0,
        is_cross_file INTEGER DEFAULT 0,
        FOREIGN KEY (caller_id) REFERENCES symbols(id)
    )""")

    # toolchain schema（resolved_edges + workspace_build_contexts 等）
    from callwarden.db.db_toolchain import init_toolchain_schema
    init_toolchain_schema(conn)

    # workspace_manifests 表
    conn.execute("""CREATE TABLE IF NOT EXISTS workspace_manifests (
        workspace_id INTEGER NOT NULL,
        rel_path TEXT NOT NULL,
        content_hash TEXT NOT NULL,
        cas_key TEXT,
        raw_hash TEXT,
        source_encoding TEXT DEFAULT 'utf-8',
        bom_kind TEXT DEFAULT 'none',
        newline_style TEXT DEFAULT 'lf',
        file_size INTEGER DEFAULT 0,
        mtime_ns INTEGER DEFAULT 0,
        is_dirty INTEGER DEFAULT 0,
        updated_at REAL NOT NULL,
        PRIMARY KEY (workspace_id, rel_path)
    )""")

    # cas_raw_calls 表
    conn.execute("""CREATE TABLE IF NOT EXISTS cas_raw_calls (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cas_key TEXT NOT NULL,
        caller_local_id INTEGER DEFAULT NULL,
        caller_name TEXT NOT NULL,
        callee_name TEXT NOT NULL,
        call_line INTEGER NOT NULL,
        call_ordinal INTEGER DEFAULT 0,
        FOREIGN KEY (cas_key) REFERENCES cas_file_cache(cas_key),
        UNIQUE(cas_key, caller_local_id, call_line, callee_name, call_ordinal)
    )""")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_cas_raw_calls_cas_key ON cas_raw_calls(cas_key)")

    # cas_symbols 表
    conn.execute("""CREATE TABLE IF NOT EXISTS cas_symbols (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        cas_key TEXT NOT NULL,
        local_symbol_id INTEGER NOT NULL,
        symbol_content_hash TEXT NOT NULL,
        name TEXT NOT NULL,
        local_qualified_name TEXT NOT NULL,
        lexical_parent_local_id INTEGER DEFAULT NULL,
        kind TEXT NOT NULL,
        start_line INTEGER NOT NULL,
        end_line INTEGER NOT NULL,
        start_col INTEGER DEFAULT 0,
        end_col INTEGER DEFAULT 0,
        start_byte INTEGER DEFAULT 0,
        end_byte INTEGER DEFAULT 0,
        visibility TEXT DEFAULT 'private',
        signature TEXT DEFAULT '',
        has_comment INTEGER DEFAULT 0,
        depth INTEGER DEFAULT -1,
        FOREIGN KEY (cas_key) REFERENCES cas_file_cache(cas_key),
        UNIQUE(cas_key, local_symbol_id)
    )""")

    # cas_file_cache 表（cas_raw_calls/cas_symbols 的外键依赖）
    conn.execute("""CREATE TABLE IF NOT EXISTS cas_file_cache (
        cas_key TEXT PRIMARY KEY,
        content_hash TEXT NOT NULL,
        language TEXT NOT NULL,
        raw_size INTEGER DEFAULT 0,
        symbol_count INTEGER DEFAULT 0,
        call_count INTEGER DEFAULT 0,
        state TEXT DEFAULT 'ready',
        created_at REAL NOT NULL,
        last_used_at REAL DEFAULT 0
    )""")

    conn.commit()
    return conn


def _insert_file(conn, workspace_id, rel_path):
    """插入 file_instance，返回 id"""
    cur = conn.execute(
        "INSERT INTO file_instances (workspace_id, rel_path, abs_path, mtime) VALUES (?, ?, ?, 0)",
        (workspace_id, rel_path, f"/{rel_path}"),
    )
    conn.commit()
    return cur.lastrowid


def _insert_symbol(conn, file_instance_id, name, qualified_name, start_line):
    """插入 symbol，返回 id"""
    cur = conn.execute(
        """INSERT INTO symbols (file_instance_id, symbol_hash, name, kind,
           start_line, end_line, qualified_name)
           VALUES (?, ?, ?, 'fn', ?, ?, ?)""",
        (file_instance_id, f"hash_{name}", name, start_line, start_line + 10, qualified_name),
    )
    conn.commit()
    return cur.lastrowid


def _insert_call(conn, caller_id, caller_name, callee_name, callee_id, call_line):
    """插入 call 记录"""
    conn.execute(
        """INSERT INTO calls (caller_id, caller_name, caller_module,
           callee_name, callee_id, call_line)
           VALUES (?, ?, '', ?, ?, ?)""",
        (caller_id, caller_name, callee_name, callee_id, call_line),
    )
    conn.commit()


# ============================================
# 测试用例
# ============================================

def test_get_resolved_edges_supports_limit(temp_db):
    """测试 1：get_resolved_edges 支持 limit 参数（bug 修复验证）"""
    from callwarden.db.db_toolchain import (
        store_resolved_edges, get_resolved_edges, init_toolchain_schema,
    )
    conn = temp_db
    init_toolchain_schema(conn)

    # 插入 5 条 resolved_edges
    edges = [
        {"caller_symbol_id": 1, "callee_symbol_id": 2, "callee_name": f"fn{i}",
         "callee_file": "a.py", "call_line": i * 10, "resolution_method": "from_calls"}
        for i in range(5)
    ]
    store_resolved_edges(conn, 1, "hash1", edges)

    # 不带 limit：返回全部 5 条
    all_edges = get_resolved_edges(conn, 1, "hash1")
    assert len(all_edges) == 5

    # 带 limit=3：返回 3 条
    limited = get_resolved_edges(conn, 1, "hash1", limit=3)
    assert len(limited) == 3

    # 带 limit + caller_symbol_id
    filtered = get_resolved_edges(conn, 1, "hash1", caller_symbol_id=1, limit=2)
    assert len(filtered) == 2
    assert all(e.caller_symbol_id == 1 for e in filtered)


def test_compute_degraded_from_calls(temp_db):
    """测试 2：降级模式——从 calls 表复制（无 workspace_manifests）"""
    from callwarden.db.db_toolchain import register_build_context
    from callwarden.analyzers.resolved_edges_engine import compute_resolved_edges

    conn = temp_db
    ws_id = 1

    # 注册 build context
    ctx = register_build_context(conn, ws_id, "debug", set_active=True)
    bch = ctx.build_context_hash

    # 插入文件 + 符号 + calls
    fi_id = _insert_file(conn, ws_id, "main.py")
    caller_id = _insert_symbol(conn, fi_id, "main", "main", 1)
    callee_id = _insert_symbol(conn, fi_id, "helper", "helper", 10)
    _insert_call(conn, caller_id, "main", "helper", callee_id, 5)

    # 计算（无 workspace_manifests → 降级从 calls 表）
    result = compute_resolved_edges(conn, ws_id, bch)
    assert result["source"] == "calls_table"
    assert result["count"] == 1
    assert result["skipped"] == 0

    edge = result["edges"][0]
    assert edge["caller_symbol_id"] == caller_id
    assert edge["callee_symbol_id"] == callee_id
    assert edge["callee_name"] == "helper"
    assert edge["resolution_method"] == "from_calls"


def test_compute_cas_mode(temp_db):
    """测试 3：CAS 模式——从 cas_raw_calls 解析（有 workspace_manifests + cas_raw_calls）"""
    from callwarden.db.db_toolchain import register_build_context
    from callwarden.db.db_workspace_manifest import upsert_manifest
    from callwarden.analyzers.resolved_edges_engine import compute_resolved_edges

    conn = temp_db
    ws_id = 1

    # 注册 build context
    ctx = register_build_context(conn, ws_id, "debug", set_active=True)
    bch = ctx.build_context_hash

    # 插入文件 + 符号
    fi_id = _insert_file(conn, ws_id, "main.py")
    caller_id = _insert_symbol(conn, fi_id, "main", "main", 1)
    # qualified_name="Helper.helper"（不等于 callee_name "helper"，触发 simple_name_unique）
    callee_id = _insert_symbol(conn, fi_id, "helper", "Helper.helper", 10)

    # 插入 workspace_manifests（含 cas_key）
    cas_key = "cas_key_abc"
    upsert_manifest(conn, ws_id, "main.py", "content_hash_123", cas_key=cas_key)

    # 插入 cas_file_cache（外键依赖）
    conn.execute(
        "INSERT INTO cas_file_cache (cas_key, content_hash, language, created_at) VALUES (?, ?, 'python', 0)",
        (cas_key, "content_hash_123"),
    )

    # 插入 cas_symbols（caller: local_id=0, qname="main"）
    conn.execute(
        """INSERT INTO cas_symbols (cas_key, local_symbol_id, symbol_content_hash,
           name, local_qualified_name, kind, start_line, end_line)
           VALUES (?, 0, 'sym_hash_main', 'main', 'main', 'fn', 1, 10)""",
        (cas_key,),
    )

    # 插入 cas_raw_calls（main 调用 helper）
    conn.execute(
        """INSERT INTO cas_raw_calls (cas_key, caller_local_id, caller_name, callee_name, call_line)
           VALUES (?, 0, 'main', 'helper', 5)""",
        (cas_key,),
    )
    conn.commit()

    # 计算（有 workspace_manifests + cas_raw_calls → CAS 模式）
    result = compute_resolved_edges(conn, ws_id, bch)
    assert result["source"] == "cas"
    assert result["count"] == 1
    assert result["skipped"] == 0

    edge = result["edges"][0]
    assert edge["caller_symbol_id"] == caller_id
    assert edge["callee_name"] == "helper"
    # helper 是简名全局唯一（只有一个 helper 符号）→ simple_name_unique
    assert edge["resolution_method"] == "simple_name_unique"
    assert edge["callee_symbol_id"] == callee_id


def test_compute_cas_resolution_exact_match(temp_db):
    """测试 4：CAS 模式——exact_match（callee_name 是 qualified_name）"""
    from callwarden.db.db_toolchain import register_build_context
    from callwarden.db.db_workspace_manifest import upsert_manifest
    from callwarden.analyzers.resolved_edges_engine import compute_resolved_edges

    conn = temp_db
    ws_id = 1
    ctx = register_build_context(conn, ws_id, "debug", set_active=True)
    bch = ctx.build_context_hash

    # 文件 A：caller
    fi_a = _insert_file(conn, ws_id, "a.py")
    caller_id = _insert_symbol(conn, fi_a, "func_a", "mod.func_a", 1)

    # 文件 B：callee（qualified_name = "mod.func_b"）
    fi_b = _insert_file(conn, ws_id, "b.py")
    callee_id = _insert_symbol(conn, fi_b, "func_b", "mod.func_b", 1)

    # CAS 数据
    cas_key_a = "cas_a"
    upsert_manifest(conn, ws_id, "a.py", "hash_a", cas_key=cas_key_a)
    conn.execute(
        "INSERT INTO cas_file_cache (cas_key, content_hash, language, created_at) VALUES (?, ?, 'python', 0)",
        (cas_key_a, "hash_a"),
    )
    conn.execute(
        """INSERT INTO cas_symbols (cas_key, local_symbol_id, symbol_content_hash,
           name, local_qualified_name, kind, start_line, end_line)
           VALUES (?, 0, 'h', 'func_a', 'mod.func_a', 'fn', 1, 10)""",
        (cas_key_a,),
    )
    # callee_name 用 qualified_name "mod.func_b" → exact_match
    conn.execute(
        """INSERT INTO cas_raw_calls (cas_key, caller_local_id, caller_name, callee_name, call_line)
           VALUES (?, 0, 'func_a', 'mod.func_b', 5)""",
        (cas_key_a,),
    )
    conn.commit()

    result = compute_resolved_edges(conn, ws_id, bch)
    assert result["source"] == "cas"
    edge = result["edges"][0]
    assert edge["resolution_method"] == "exact_match"
    assert edge["callee_symbol_id"] == callee_id


def test_compute_cas_resolution_unresolved(temp_db):
    """测试 5：CAS 模式——unresolved（callee 不存在）"""
    from callwarden.db.db_toolchain import register_build_context
    from callwarden.db.db_workspace_manifest import upsert_manifest
    from callwarden.analyzers.resolved_edges_engine import compute_resolved_edges

    conn = temp_db
    ws_id = 1
    ctx = register_build_context(conn, ws_id, "debug", set_active=True)
    bch = ctx.build_context_hash

    fi_a = _insert_file(conn, ws_id, "a.py")
    caller_id = _insert_symbol(conn, fi_a, "func_a", "func_a", 1)

    cas_key = "cas_x"
    upsert_manifest(conn, ws_id, "a.py", "hash_x", cas_key=cas_key)
    conn.execute(
        "INSERT INTO cas_file_cache (cas_key, content_hash, language, created_at) VALUES (?, ?, 'python', 0)",
        (cas_key, "hash_x"),
    )
    conn.execute(
        """INSERT INTO cas_symbols (cas_key, local_symbol_id, symbol_content_hash,
           name, local_qualified_name, kind, start_line, end_line)
           VALUES (?, 0, 'h', 'func_a', 'func_a', 'fn', 1, 10)""",
        (cas_key,),
    )
    # callee_name "nonexistent" → unresolved
    conn.execute(
        """INSERT INTO cas_raw_calls (cas_key, caller_local_id, caller_name, callee_name, call_line)
           VALUES (?, 0, 'func_a', 'nonexistent', 5)""",
        (cas_key,),
    )
    conn.commit()

    result = compute_resolved_edges(conn, ws_id, bch)
    assert result["source"] == "cas"
    edge = result["edges"][0]
    assert edge["resolution_method"] == "unresolved"
    assert edge["callee_symbol_id"] == 0


def test_compute_cas_resolution_same_file(temp_db):
    """测试 6：CAS 模式——same_file（callee 在同文件，简名有歧义）"""
    from callwarden.db.db_toolchain import register_build_context
    from callwarden.db.db_workspace_manifest import upsert_manifest
    from callwarden.analyzers.resolved_edges_engine import compute_resolved_edges

    conn = temp_db
    ws_id = 1
    ctx = register_build_context(conn, ws_id, "debug", set_active=True)
    bch = ctx.build_context_hash

    # 同一文件有两个同名 helper（不同 qualified_name）
    fi_a = _insert_file(conn, ws_id, "a.py")
    caller_id = _insert_symbol(conn, fi_a, "func", "func", 1)
    helper1_id = _insert_symbol(conn, fi_a, "helper", "ClassA.helper", 10)
    helper2_id = _insert_symbol(conn, fi_a, "helper", "ClassB.helper", 20)
    # 另一文件也有 helper（使简名非全局唯一）
    fi_b = _insert_file(conn, ws_id, "b.py")
    _insert_symbol(conn, fi_b, "helper", "Other.helper", 5)

    cas_key = "cas_sf"
    upsert_manifest(conn, ws_id, "a.py", "hash_sf", cas_key=cas_key)
    conn.execute(
        "INSERT INTO cas_file_cache (cas_key, content_hash, language, created_at) VALUES (?, ?, 'python', 0)",
        (cas_key, "hash_sf"),
    )
    conn.execute(
        """INSERT INTO cas_symbols (cas_key, local_symbol_id, symbol_content_hash,
           name, local_qualified_name, kind, start_line, end_line)
           VALUES (?, 0, 'h', 'func', 'func', 'fn', 1, 10)""",
        (cas_key,),
    )
    # callee_name "helper" 在同文件有多个 → same_file 取第一个
    conn.execute(
        """INSERT INTO cas_raw_calls (cas_key, caller_local_id, caller_name, callee_name, call_line)
           VALUES (?, 0, 'func', 'helper', 5)""",
        (cas_key,),
    )
    conn.commit()

    result = compute_resolved_edges(conn, ws_id, bch)
    assert result["source"] == "cas"
    edge = result["edges"][0]
    # helper 在同文件 a.py 中找到 → same_file
    assert edge["resolution_method"] == "same_file"
    assert edge["callee_file"] == "a.py"


def test_compute_build_context_not_found(temp_db):
    """测试 7：build_context 不存在时返回 error"""
    from callwarden.analyzers.resolved_edges_engine import compute_resolved_edges

    result = compute_resolved_edges(temp_db, 1, "nonexistent_hash")
    assert result["source"] == "none"
    assert result["count"] == 0
    assert "error" in result


def test_store_and_query_resolved_edges(temp_db):
    """测试 8：compute → store → query 端到端流程"""
    from callwarden.db.db_toolchain import (
        register_build_context, store_resolved_edges, get_resolved_edges,
        count_resolved_edges, delete_resolved_edges,
    )
    from callwarden.analyzers.resolved_edges_engine import compute_resolved_edges

    conn = temp_db
    ws_id = 1
    ctx = register_build_context(conn, ws_id, "debug", set_active=True)
    bch = ctx.build_context_hash

    fi_id = _insert_file(conn, ws_id, "main.py")
    caller_id = _insert_symbol(conn, fi_id, "main", "main", 1)
    callee_id = _insert_symbol(conn, fi_id, "helper", "helper", 10)
    _insert_call(conn, caller_id, "main", "helper", callee_id, 5)

    # 计算
    result = compute_resolved_edges(conn, ws_id, bch)
    assert result["count"] == 1

    # 先清旧再写入
    deleted = delete_resolved_edges(conn, ws_id, bch)
    stored = store_resolved_edges(conn, ws_id, bch, result["edges"])
    assert stored == 1

    # 查询验证
    assert count_resolved_edges(conn, ws_id, bch) == 1
    edges = get_resolved_edges(conn, ws_id, bch)
    assert len(edges) == 1
    assert edges[0].caller_symbol_id == caller_id
    assert edges[0].callee_symbol_id == callee_id
    assert edges[0].resolution_method == "from_calls"


# ============================================
# 第 4 级解析测试：include_path / sysroot
# ============================================

def _setup_include_path_test(conn, ws_id, include_paths=None, sysroot="", tc_include_dirs=None):
    """搭建 include_path/sysroot 解析测试的通用夹具。

    场景：caller 文件 src/main.c 调用 gpio_set()，两个 candidate：
    - candidate A：定义在 include/driver/gpio.h（头文件搜索路径下）
    - candidate B：定义在 src/legacy/gpio.c（非头文件搜索路径）

    build_context.include_paths 或 toolchain.sysroot/include_dirs 配置后，
    第 4 级解析应唯一命中 candidate A。
    """
    import json as _json
    from callwarden.db.db_toolchain import register_build_context

    # 注册 build_context（含 include_paths）
    ctx = register_build_context(
        conn, ws_id, "debug",
        include_paths=include_paths or [],
        set_active=True,
    )
    bch = ctx.build_context_hash

    # 直接用 SQL 插入 toolchain（绕过 probe，因为测试用假编译器路径）
    # 并绑定到 workspace + build_context
    if sysroot or tc_include_dirs:
        import time as _time
        conn.execute(
            "INSERT INTO toolchains (name, compiler_path, compiler_type, sysroot, "
            "include_dirs, predefined_macros, fingerprint, created_at, updated_at) "
            "VALUES (?, ?, 'gcc', ?, ?, '{}', ?, ?, ?)",
            (
                "gcc_arm_test", "/usr/bin/arm-gcc", sysroot,
                _json.dumps(tc_include_dirs or []),
                f"fp_{sysroot}_{_json.dumps(tc_include_dirs or [])}",
                _time.time(), _time.time(),
            ),
        )
        tc_id = conn.execute("SELECT id FROM toolchains WHERE name='gcc_arm_test'").fetchone()[0]
        conn.execute(
            "INSERT INTO workspace_toolchains (workspace_id, toolchain_id, build_context_hash) "
            "VALUES (?, ?, ?)",
            (ws_id, tc_id, bch),
        )

    # 文件 1：caller src/main.c
    fi_main = _insert_file(conn, ws_id, "src/main.c")
    caller_id = _insert_symbol(conn, fi_main, "main", "main", 1)

    # 文件 2：callee candidate A — include/driver/gpio.h（头文件搜索路径下）
    # qualified_name="DriverGpio.gpio_set"（不等于 callee_name "gpio_set"，避免 exact_match）
    fi_hdr = _insert_file(conn, ws_id, "include/driver/gpio.h")
    callee_a_id = _insert_symbol(conn, fi_hdr, "gpio_set", "DriverGpio.gpio_set", 10)

    # 文件 3：callee candidate B — src/legacy/gpio.c（非头文件搜索路径）
    fi_legacy = _insert_file(conn, ws_id, "src/legacy/gpio.c")
    callee_b_id = _insert_symbol(conn, fi_legacy, "gpio_set", "legacy_gpio_set", 5)

    # CAS 数据：src/main.c 调用 gpio_set()
    cas_key = "cas_inc_path"
    from callwarden.db.db_workspace_manifest import upsert_manifest
    upsert_manifest(conn, ws_id, "src/main.c", "hash_main", cas_key=cas_key)
    conn.execute(
        "INSERT INTO cas_file_cache (cas_key, content_hash, language, created_at) "
        "VALUES (?, ?, 'c', 0)",
        (cas_key, "hash_main"),
    )
    conn.execute(
        "INSERT INTO cas_symbols (cas_key, local_symbol_id, symbol_content_hash, "
        "name, local_qualified_name, kind, start_line, end_line) "
        "VALUES (?, 0, 'h_main', 'main', 'main', 'fn', 1, 10)",
        (cas_key,),
    )
    # callee_name "gpio_set"（简名，有 2 个 candidate → 不走 simple_name_unique）
    conn.execute(
        "INSERT INTO cas_raw_calls (cas_key, caller_local_id, caller_name, callee_name, call_line) "
        "VALUES (?, 0, 'main', 'gpio_set', 5)",
        (cas_key,),
    )
    conn.commit()

    return bch, caller_id, callee_a_id, callee_b_id


def test_compute_cas_resolution_include_path(temp_db):
    """测试 9：CAS 模式——include_path（简名歧义，build_context include_paths 唯一命中）"""
    from callwarden.analyzers.resolved_edges_engine import compute_resolved_edges

    conn = temp_db
    ws_id = 1
    bch, caller_id, callee_a_id, callee_b_id = _setup_include_path_test(
        conn, ws_id, include_paths=["include/"]
    )

    result = compute_resolved_edges(conn, ws_id, bch)
    assert result["source"] == "cas"
    assert result["count"] == 1

    edge = result["edges"][0]
    # gpio_set 有 2 个 candidate，include/driver/gpio.h 在 "include/" 下 → include_path
    assert edge["resolution_method"] == "include_path"
    assert edge["callee_symbol_id"] == callee_a_id
    assert edge["callee_file"] == "include/driver/gpio.h"
    assert edge["caller_symbol_id"] == caller_id


def test_compute_cas_resolution_sysroot(temp_db):
    """测试 10：CAS 模式——sysroot（简名歧义，toolchain include_dirs basename 唯一命中）"""
    from callwarden.analyzers.resolved_edges_engine import compute_resolved_edges

    conn = temp_db
    ws_id = 1
    # toolchain include_dirs=["/sysroot/usr/include"]，basename="include"
    # candidate A 的 rel_path="include/driver/gpio.h" 以 "include" 开头 → sysroot 命中
    # candidate B 的 rel_path="src/legacy/gpio.c" 不以 "include" 开头 → 不命中
    bch, caller_id, callee_a_id, callee_b_id = _setup_include_path_test(
        conn, ws_id, tc_include_dirs=["/sysroot/usr/include"]
    )

    result = compute_resolved_edges(conn, ws_id, bch)
    assert result["source"] == "cas"
    edge = result["edges"][0]
    # include_dirs basename="include"，candidate A 的 rel_path 以 "include" 开头 → sysroot
    assert edge["resolution_method"] == "sysroot"
    assert edge["callee_symbol_id"] == callee_a_id
    assert edge["callee_file"] == "include/driver/gpio.h"


def test_compute_cas_resolution_include_path_ambiguous(temp_db):
    """测试 11：CAS 模式——include_path 歧义（多 candidate 都在 search_paths 下 → unresolved）"""
    from callwarden.analyzers.resolved_edges_engine import compute_resolved_edges

    conn = temp_db
    ws_id = 1
    # 修改夹具：两个 candidate 都在 "include/" 下
    # 重新搭建：candidate B 也放到 include/ 下
    bch, caller_id, callee_a_id, callee_b_id = _setup_include_path_test(
        conn, ws_id, include_paths=["include/"]
    )
    # 把 candidate B 的文件改为 include/legacy/gpio.c（也在 include/ 下）
    conn.execute(
        "UPDATE file_instances SET rel_path='include/legacy/gpio.c' WHERE id="
        + str(conn.execute("SELECT id FROM file_instances WHERE rel_path='src/legacy/gpio.c'").fetchone()[0])
    )
    conn.commit()

    result = compute_resolved_edges(conn, ws_id, bch)
    assert result["source"] == "cas"
    edge = result["edges"][0]
    # 两个 candidate 都在 "include/" 下 → 歧义 → unresolved
    assert edge["resolution_method"] == "unresolved"
    assert edge["callee_symbol_id"] == 0


def test_compute_cas_resolution_include_path_no_match(temp_db):
    """测试 12：CAS 模式——无 include_path 配置时走 unresolved"""
    from callwarden.analyzers.resolved_edges_engine import compute_resolved_edges

    conn = temp_db
    ws_id = 1
    # 不配置 include_paths 也不配置 sysroot/include_dirs → search_paths=None
    # 第 4 级跳过，直接 unresolved
    bch, caller_id, callee_a_id, callee_b_id = _setup_include_path_test(conn, ws_id)

    result = compute_resolved_edges(conn, ws_id, bch)
    assert result["source"] == "cas"
    edge = result["edges"][0]
    # 无 search_paths → 第 4 级跳过 → unresolved
    assert edge["resolution_method"] == "unresolved"
    assert edge["callee_symbol_id"] == 0


# ====================================
# 主入口
# ====================================

def main():
    print("=" * 60)
    print("L5 resolved_edges 计算引擎测试")
    print("=" * 60)

    # 手动运行测试
    tests = [
        ("test_get_resolved_edges_supports_limit", test_get_resolved_edges_supports_limit),
        ("test_compute_degraded_from_calls", test_compute_degraded_from_calls),
        ("test_compute_cas_mode", test_compute_cas_mode),
        ("test_compute_cas_resolution_exact_match", test_compute_cas_resolution_exact_match),
        ("test_compute_cas_resolution_unresolved", test_compute_cas_resolution_unresolved),
        ("test_compute_cas_resolution_same_file", test_compute_cas_resolution_same_file),
        ("test_compute_build_context_not_found", test_compute_build_context_not_found),
        ("test_store_and_query_resolved_edges", test_store_and_query_resolved_edges),
    ]

    passed = 0
    failed = 0
    for name, test_fn in tests:
        conn = None
        try:
            conn = _make_temp_db()
            test_fn(conn)
            print(f"PASS {name}")
            passed += 1
        except Exception as e:
            print(f"FAIL {name}: {e}")
            failed += 1
        finally:
            if conn:
                conn.close()

    print("=" * 60)
    print(f"Results: {passed} passed, {failed} failed")
    print("=" * 60)
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
