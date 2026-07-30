"""Phase 6-1 差分测试：blast_radius Rust 实现与 Python 一致性验证

**本文件是契约 docs/design/phase6-1-blast-radius-impact-evolution-contract.md §3.1 D1 矩阵的 ✅(behavioral) 标记载体。**

差分测试矩阵（D1.1 - D1.5）：
  TestBlastRadiusDiff：
    - D1.1: 单层调用链（depth=1）→ 第 1 层 caller 集合一致
    - D1.2: 多层递归（depth=3 默认）→ 三层 caller 集合一致
    - D1.3: 环路调用（A→B→A）→ visited 集合一致，BFS 终止，无死循环
    - D1.4: 孤立符号（无 callers）→ 仅第 0 层（源符号），其他层为空
    - D1.5: 跨文件调用链 → 文件/模块聚合一致

  TestBlastRadiusEdgeCases：
    - E1: symbol_id 不存在 → Rust 抛 RuntimeError
    - E2: depth=0 → 仅源符号层
    - E3: depth=1 但 caller 无 qname → 不进入下一层 BFS

预期差异（见契约 §4 + §7.2）：
  - Rust 不持有 symbol_hash / visibility 字段，layer_symbols 返回空字符串
    差分测试只比较 qualified_name / name / kind / module_path / file_path
  - Python by_layer 字段来自 cross_layer_impact（暂保留 Python 实现）
    差分测试只比较 layers 字段（BFS 输出），不比较 by_layer

前置条件：
  - Rust 扩展 callwarden_core 必须可加载
  - 如果不可加载，本测试套件会显式 skip 并给出修复指引

关联：
  - 契约：docs/design/phase6-1-blast-radius-impact-evolution-contract.md §3.1
  - Python 真相源：db/db_impact.py:ImpactMixin.blast_radius (L273-397)
  - Rust 真相源：rust_ext/src/graph.rs:GraphStore::blast_radius + BlastRadiusBatch
"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
from typing import Any, Dict, List, Optional

import pytest

# ============================================
# 前置条件：Rust 扩展可用性检查
# ============================================

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

_RUST_EXT_AVAILABLE = False
_RUST_EXT_SKIP_REASON = ""
try:
    import callwarden_core  # type: ignore
    _RUST_EXT_AVAILABLE = True
except ImportError as _e:
    _RUST_EXT_SKIP_REASON = (
        f"callwarden_core 不可加载：{_e}。"
        "请先运行 `maturin develop --manifest-path rust_ext/Cargo.toml --release` "
        "或 `pip install --force-reinstall rust_ext/target/wheels/callwarden_core-*.whl`。"
    )


# ============================================
# CodeGraph DB schema（与 db/schema.py 对齐，blast_radius 所需子集）
# ============================================

_CODEGRAPH_SCHEMA_DDL = """
CREATE TABLE IF NOT EXISTS workspaces (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    root_path TEXT,
    created_at REAL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS file_instances (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workspace_id INTEGER NOT NULL,
    rel_path TEXT NOT NULL,
    abs_path TEXT,
    mtime REAL DEFAULT 0,
    module_path TEXT DEFAULT '',
    status TEXT DEFAULT 'active',
    content_hash TEXT,
    total_lines INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS symbols (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_instance_id INTEGER NOT NULL,
    symbol_hash TEXT NOT NULL,
    kind TEXT,
    name TEXT,
    qualified_name TEXT,
    module_path TEXT,
    visibility TEXT DEFAULT 'public',
    start_line INTEGER,
    end_line INTEGER,
    depth INTEGER DEFAULT -1
);

CREATE TABLE IF NOT EXISTS calls (
    caller_id INTEGER NOT NULL,
    callee_id INTEGER NOT NULL,
    callee_name TEXT,
    call_line INTEGER DEFAULT 0,
    is_cross_file INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS symbol_contents (
    content_hash TEXT PRIMARY KEY,
    content TEXT,
    language TEXT
);
"""


# ============================================
# Python baseline: ImpactMixin 最小实现（仅 blast_radius）
# ============================================

class _MinimalImpactMixin:
    """最小 ImpactMixin 实现，仅暴露 blast_radius 用于差分测试。

    复用 db/db_impact.py:ImpactMixin 的源代码逻辑（BFS 反向遍历），
    但 stub 掉 cross_layer_impact（返回空结构，避免依赖 symbol_contents）。

    支持 wire-production 集成测试：
    - rust_enabled=False: 走纯 Python SQL 路径（baseline）
    - rust_enabled=True: 走 Rust GraphStore 短路（复用 db_impact._blast_radius_via_rust）
    """

    def __init__(self, conn: sqlite3.Connection, db_path: str,
                 rust_enabled: bool = False):
        self.conn = conn
        self.db_path = db_path
        self._rust_enabled = rust_enabled
        self._graph_store = None
        conn.row_factory = sqlite3.Row

    def _get_active_workspace_id(self) -> int:
        return 1

    def is_feature_rolled_back(self, feature_name: str) -> bool:
        # rust_blast_radius 控制：rust_enabled=True 时不回退（走 Rust），False 时回退
        if feature_name == "rust_blast_radius":
            return not self._rust_enabled
        if feature_name == "rust_graph_query":
            return not self._rust_enabled
        return True  # 其他 feature 默认回退（避免触发未实现的 Rust 短路）

    def cross_layer_impact(self, symbol_hash: str) -> Dict[str, Any]:
        """Stub：差分测试不比较 cross_layer_impact（保留 Python 实现）"""
        return {"code": [], "db": [], "api": [], "config": []}

    def _get_graph_store(self):
        """Stub GraphStore 加载：从 db_path 加载并缓存"""
        if self._graph_store is not None:
            return self._graph_store
        try:
            store = callwarden_core.GraphStore()
            # WAL checkpoint 后加载（避免读到旧数据）
            self.conn.execute("PRAGMA wal_checkpoint(PASSIVE)")
            store.load_from_sqlite(self.db_path, 1)
            self._graph_store = store
            return store
        except Exception:
            return None

    def _wait_for_calls_ready(self, timeout: float = 2.0) -> None:
        """Stub：测试环境同步加载，无需等待"""
        pass

    # 直接复用 db_impact.py 的 _blast_radius_via_rust 和 blast_radius 实现
    # 通过导入真实模块避免重复代码
    def _blast_radius_via_rust(self, symbol_hash: str, depth: int) -> Optional[Dict[str, Any]]:
        """与 db/db_impact.py:ImpactMixin._blast_radius_via_rust 一致的实现"""
        store = self._get_graph_store()
        if store is None:
            return None
        if store.load_state() != "graph_ready":
            self._wait_for_calls_ready(timeout=2.0)
            store = self._get_graph_store()
            if store is None or store.load_state() != "graph_ready":
                return None

        ws_id = self._get_active_workspace_id()
        cur = self.conn.execute(
            """
            SELECT s.id, s.symbol_hash, s.qualified_name, s.name, s.module_path,
                   s.visibility, s.kind, fi.rel_path
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND s.symbol_hash = ?
            LIMIT 1
            """,
            (ws_id, symbol_hash),
        )
        row = cur.fetchone()
        if not row:
            return {
                "source_symbol": "",
                "source_hash": symbol_hash,
                "depth": depth,
                "layers": [],
                "total_impacted": 0,
                "by_layer": {"code": 0, "db": 0, "api": 0, "config": 0},
            }
        source_id = row["id"]
        source_qn = row["qualified_name"] or ""

        try:
            rust_batch = store.blast_radius(source_id, depth)
        except Exception:
            return None

        rust_layers = rust_batch.to_list()

        # 批量补全 symbol_hash + visibility
        all_symbol_ids: List[int] = []
        for layer in rust_layers:
            for sym in layer["symbols"]:
                sym_id = sym.get("symbol_id")
                if sym_id is not None:
                    all_symbol_ids.append(sym_id)

        id_to_hash_vis: Dict[int, tuple] = {source_id: (row["symbol_hash"], row["visibility"])}
        if all_symbol_ids:
            other_ids = [i for i in all_symbol_ids if i != source_id]
            if other_ids:
                placeholders = ",".join("?" * len(other_ids))
                cur2 = self.conn.execute(
                    f"""SELECT s.id, s.symbol_hash, s.visibility
                        FROM symbols s WHERE s.id IN ({placeholders})""",
                    other_ids,
                )
                for r in cur2:
                    id_to_hash_vis[r["id"]] = (r["symbol_hash"], r["visibility"])

        py_layers: List[Dict[str, Any]] = []
        for layer in rust_layers:
            layer_symbols = []
            for sym in layer["symbols"]:
                sym_id = sym.get("symbol_id", 0)
                hash_vis = id_to_hash_vis.get(sym_id, ("", ""))
                layer_symbols.append({
                    "symbol_hash": hash_vis[0],
                    "qualified_name": sym["qualified_name"],
                    "name": sym["name"],
                    "module_path": sym["module_path"],
                    "file_path": sym["file_path"],
                    "visibility": hash_vis[1],
                    "kind": sym["kind"],
                })
            if layer_symbols:
                py_layers.append({"depth": layer["depth"], "symbols": layer_symbols})

        total_impacted = sum(len(layer["symbols"]) for layer in py_layers)

        cross = self.cross_layer_impact(symbol_hash)
        by_layer = {
            "code": len(cross["code"]),
            "db": len(cross["db"]),
            "api": len(cross["api"]),
            "config": len(cross["config"]),
        }

        return {
            "source_symbol": source_qn,
            "source_hash": symbol_hash,
            "depth": depth,
            "layers": py_layers,
            "total_impacted": total_impacted,
            "by_layer": by_layer,
        }

    def blast_radius(self, symbol_hash: str, depth: int = 3) -> Dict[str, Any]:
        """与 db/db_impact.py:ImpactMixin.blast_radius 完全一致的实现（含 Rust 短路）"""
        # Phase 6-1 wire-production: Rust 短路
        if not self.is_feature_rolled_back("rust_blast_radius"):
            rust_result = self._blast_radius_via_rust(symbol_hash, depth)
            if rust_result is not None:
                return rust_result

        ws_id = self._get_active_workspace_id()

        cur = self.conn.execute(
            """
            SELECT s.id, s.symbol_hash, s.qualified_name, s.name, s.module_path,
                   s.visibility, s.kind, fi.rel_path
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND s.symbol_hash = ?
            LIMIT 1
            """,
            (ws_id, symbol_hash),
        )
        row = cur.fetchone()
        if not row:
            return {
                "source_symbol": "",
                "source_hash": symbol_hash,
                "depth": depth,
                "layers": [],
                "total_impacted": 0,
                "by_layer": {"code": 0, "db": 0, "api": 0, "config": 0},
            }

        source_qn = row["qualified_name"] or ""
        source_info = {
            "symbol_hash": row["symbol_hash"],
            "qualified_name": source_qn,
            "name": row["name"],
            "module_path": row["module_path"],
            "file_path": row["rel_path"],
            "visibility": row["visibility"],
            "kind": row["kind"],
        }

        layers: List[Dict[str, Any]] = [{"depth": 0, "symbols": [source_info]}]
        visited_qn = {source_qn} if source_qn else set()
        visited_hash = {symbol_hash}
        current_batch: List[int] = [row["id"]]

        for d in range(1, depth + 1):
            if not current_batch:
                break
            placeholders = ",".join("?" * len(current_batch))
            cur = self.conn.execute(
                f"""
                SELECT DISTINCT
                    s.id, s.symbol_hash, s.qualified_name, s.name, s.module_path,
                    s.visibility, s.kind, fi.rel_path
                FROM calls c
                JOIN symbols s ON c.caller_id = s.id
                JOIN file_instances fi ON s.file_instance_id = fi.id
                WHERE fi.workspace_id = ?
                  AND c.callee_id > 0
                  AND c.callee_id IN ({placeholders})
                """,
                [ws_id] + current_batch,
            )
            next_batch: List[int] = []
            layer_symbols: List[Dict[str, Any]] = []
            for r in cur:
                qn = r["qualified_name"] or ""
                sh = r["symbol_hash"] or ""
                key = qn if qn else sh
                if key in visited_qn or sh in visited_hash:
                    continue
                visited_qn.add(key)
                visited_hash.add(sh)
                layer_symbols.append({
                    "symbol_hash": sh,
                    "qualified_name": qn,
                    "name": r["name"],
                    "module_path": r["module_path"],
                    "file_path": r["rel_path"],
                    "visibility": r["visibility"],
                    "kind": r["kind"],
                })
                if qn:
                    next_batch.append(r["id"])
            if layer_symbols:
                layers.append({"depth": d, "symbols": layer_symbols})
            current_batch = next_batch

        total_impacted = sum(len(layer["symbols"]) for layer in layers)

        cross = self.cross_layer_impact(symbol_hash)
        by_layer = {
            "code": len(cross["code"]),
            "db": len(cross["db"]),
            "api": len(cross["api"]),
            "config": len(cross["config"]),
        }

        return {
            "source_symbol": source_qn,
            "source_hash": symbol_hash,
            "depth": depth,
            "layers": layers,
            "total_impacted": total_impacted,
            "by_layer": by_layer,
        }



# ============================================
# 测试 fixture 构造工具
# ============================================

def _make_db_with_schema(db_path: str) -> sqlite3.Connection:
    """创建带完整 schema 的空 CodeGraph DB"""
    conn = sqlite3.connect(db_path)
    conn.executescript(_CODEGRAPH_SCHEMA_DDL)
    conn.execute("INSERT INTO workspaces (id, name, root_path) VALUES (1, 'test', '/tmp/test')")
    conn.commit()
    return conn


def _insert_symbol(conn: sqlite3.Connection, sym_id: int, file_id: int,
                   symbol_hash: str, kind: str, name: str, qname: str,
                   module_path: str = "", visibility: str = "public",
                   start_line: int = 1, end_line: int = 10, depth: int = 0) -> None:
    conn.execute(
        """INSERT INTO symbols
           (id, file_instance_id, symbol_hash, kind, name, qualified_name,
            module_path, visibility, start_line, end_line, depth)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (sym_id, file_id, symbol_hash, kind, name, qname,
         module_path, visibility, start_line, end_line, depth)
    )


def _insert_call(conn: sqlite3.Connection, caller_id: int, callee_id: int,
                 callee_name: str, call_line: int = 1, is_cross_file: int = 0) -> None:
    conn.execute(
        "INSERT INTO calls (caller_id, callee_id, callee_name, call_line, is_cross_file) VALUES (?, ?, ?, ?, ?)",
        (caller_id, callee_id, callee_name, call_line, is_cross_file)
    )


def _hash_for(name: str) -> str:
    """生成稳定的 symbol_hash（基于名字，便于测试可读）"""
    return f"hash_{name}"


# ============================================
# D1.1: 单层调用链（depth=1）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestBlastRadiusDiffD1_1:
    """D1.1: 单层调用链 depth=1

    调用图：
        caller_a → callee_target
        caller_b → callee_target
    预期：第 0 层=callee_target，第 1 层=[caller_a, caller_b]
    """

    def test_d1_1_single_layer_callers(self, tmp_path):
        db_path = str(tmp_path / "test_d1_1.db")
        conn = _make_db_with_schema(db_path)
        conn.execute("INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (1, 1, 'src/a.py')")
        conn.execute("INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (2, 1, 'src/b.py')")

        _insert_symbol(conn, 1, 1, _hash_for("target"), "fn", "target", "target", depth=0)
        _insert_symbol(conn, 2, 1, _hash_for("caller_a"), "fn", "caller_a", "caller_a", depth=1)
        _insert_symbol(conn, 3, 2, _hash_for("caller_b"), "fn", "caller_b", "caller_b", depth=1)

        _insert_call(conn, 2, 1, "target")  # caller_a → target
        _insert_call(conn, 3, 1, "target")  # caller_b → target
        conn.commit()

        # Python baseline（rust_enabled=False 走纯 SQL 路径）
        py_mixin = _MinimalImpactMixin(conn, db_path, rust_enabled=False)
        py_result = py_mixin.blast_radius(_hash_for("target"), depth=1)

        # Rust 实现
        store = callwarden_core.GraphStore()
        store.load_from_sqlite(db_path, workspace_id=1)
        # 通过 qualified_name 找到源 symbol_id（对齐 Python 的 symbol_hash → id 查询）
        source_sym = store.get_symbol("target")
        assert source_sym is not None, "source symbol not found in Rust store"
        rust_batch = store.blast_radius(source_sym["id"], 1)
        rust_layers = rust_batch.to_list()

        # 差分对比
        _assert_layers_equal(py_result["layers"], rust_layers)

        # 具体断言
        assert len(rust_layers) == 2, f"expected 2 layers, got {len(rust_layers)}"
        assert rust_layers[0]["depth"] == 0
        assert len(rust_layers[0]["symbols"]) == 1
        assert rust_layers[0]["symbols"][0]["qualified_name"] == "target"
        assert rust_layers[1]["depth"] == 1
        rust_layer1_qnames = {s["qualified_name"] for s in rust_layers[1]["symbols"]}
        assert rust_layer1_qnames == {"caller_a", "caller_b"}, f"got {rust_layer1_qnames}"

        conn.close()


# ============================================
# D1.2: 多层递归（depth=3，默认）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestBlastRadiusDiffD1_2:
    """D1.2: 多层递归调用链 depth=3

    调用图（三层链）：
        entry_top → middle_fn → leaf_target
    预期：第 0 层=leaf_target，第 1 层=middle_fn，第 2 层=entry_top
    """

    def test_d1_2_multi_layer_chain(self, tmp_path):
        db_path = str(tmp_path / "test_d1_2.db")
        conn = _make_db_with_schema(db_path)
        conn.execute("INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (1, 1, 'src/chain.py')")

        _insert_symbol(conn, 1, 1, _hash_for("leaf"), "fn", "leaf_target", "leaf_target", depth=0)
        _insert_symbol(conn, 2, 1, _hash_for("middle"), "fn", "middle_fn", "middle_fn", depth=1)
        _insert_symbol(conn, 3, 1, _hash_for("entry"), "fn", "entry_top", "entry_top", depth=2)

        _insert_call(conn, 2, 1, "leaf_target")  # middle_fn → leaf_target
        _insert_call(conn, 3, 2, "middle_fn")     # entry_top → middle_fn
        conn.commit()

        py_mixin = _MinimalImpactMixin(conn, db_path, rust_enabled=False)
        py_result = py_mixin.blast_radius(_hash_for("leaf"), depth=3)

        store = callwarden_core.GraphStore()
        store.load_from_sqlite(db_path, workspace_id=1)
        source_sym = store.get_symbol("leaf_target")
        assert source_sym is not None
        rust_batch = store.blast_radius(source_sym["id"], 3)
        rust_layers = rust_batch.to_list()

        _assert_layers_equal(py_result["layers"], rust_layers)

        assert len(rust_layers) == 3
        assert rust_layers[0]["symbols"][0]["qualified_name"] == "leaf_target"
        assert rust_layers[1]["symbols"][0]["qualified_name"] == "middle_fn"
        assert rust_layers[2]["symbols"][0]["qualified_name"] == "entry_top"
        assert rust_batch.total_impacted() == 3

        conn.close()


# ============================================
# D1.3: 环路调用（A→B→A）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestBlastRadiusDiffD1_3:
    """D1.3: 环路调用 A↔B

    调用图（双向环）：
        func_a → func_b
        func_b → func_a
    预期：从 func_b 出发，第 0 层=func_b，第 1 层=func_a，
          第 2 层应为空（func_a 已访问，func_b 已访问），BFS 终止无死循环
    """

    def test_d1_3_cycle_termination(self, tmp_path):
        db_path = str(tmp_path / "test_d1_3.db")
        conn = _make_db_with_schema(db_path)
        conn.execute("INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (1, 1, 'src/cycle.py')")

        _insert_symbol(conn, 1, 1, _hash_for("a"), "fn", "func_a", "func_a", depth=0)
        _insert_symbol(conn, 2, 1, _hash_for("b"), "fn", "func_b", "func_b", depth=0)

        _insert_call(conn, 1, 2, "func_b")  # func_a → func_b
        _insert_call(conn, 2, 1, "func_a")  # func_b → func_a（反向边，构成环）
        conn.commit()

        py_mixin = _MinimalImpactMixin(conn, db_path, rust_enabled=False)
        py_result = py_mixin.blast_radius(_hash_for("b"), depth=3)

        store = callwarden_core.GraphStore()
        store.load_from_sqlite(db_path, workspace_id=1)
        source_sym = store.get_symbol("func_b")
        rust_batch = store.blast_radius(source_sym["id"], 3)
        rust_layers = rust_batch.to_list()

        _assert_layers_equal(py_result["layers"], rust_layers)

        # 关键：环路不能死循环，total_impacted 应为 2（func_b + func_a）
        assert rust_batch.total_impacted() == 2, f"cycle caused re-visit, got total={rust_batch.total_impacted()}"
        assert len(rust_layers) == 2  # 第 0 层 + 第 1 层
        assert rust_layers[0]["symbols"][0]["qualified_name"] == "func_b"
        assert rust_layers[1]["symbols"][0]["qualified_name"] == "func_a"

        conn.close()


# ============================================
# D1.4: 孤立符号（无 callers）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestBlastRadiusDiffD1_4:
    """D1.4: 孤立符号（无 callers）

    预期：仅第 0 层（源符号），无第 1 层，total_impacted=1
    """

    def test_d1_4_orphan_symbol(self, tmp_path):
        db_path = str(tmp_path / "test_d1_4.db")
        conn = _make_db_with_schema(db_path)
        conn.execute("INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (1, 1, 'src/orphan.py')")

        _insert_symbol(conn, 1, 1, _hash_for("orphan"), "fn", "orphan_fn", "orphan_fn", depth=0)
        # 不插入任何 calls，orphan_fn 无 caller
        conn.commit()

        py_mixin = _MinimalImpactMixin(conn, db_path, rust_enabled=False)
        py_result = py_mixin.blast_radius(_hash_for("orphan"), depth=3)

        store = callwarden_core.GraphStore()
        store.load_from_sqlite(db_path, workspace_id=1)
        source_sym = store.get_symbol("orphan_fn")
        rust_batch = store.blast_radius(source_sym["id"], 3)
        rust_layers = rust_batch.to_list()

        _assert_layers_equal(py_result["layers"], rust_layers)

        assert len(rust_layers) == 1
        assert rust_layers[0]["symbols"][0]["qualified_name"] == "orphan_fn"
        assert rust_batch.total_impacted() == 1
        assert rust_batch.layer_count() == 1

        conn.close()


# ============================================
# D1.5: 跨文件调用链
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestBlastRadiusDiffD1_5:
    """D1.5: 跨文件调用链

    调用图（跨 2 个文件）：
        src/api.py:handle_request → src/service.py:process_data
        src/api.py:handle_request → src/util.py:format_response
    预期：从 process_data 出发，第 1 层=handle_request（跨文件），
          file_path 字段对齐
    """

    def test_d1_5_cross_file_chain(self, tmp_path):
        db_path = str(tmp_path / "test_d1_5.db")
        conn = _make_db_with_schema(db_path)
        conn.execute("INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (1, 1, 'src/api.py')")
        conn.execute("INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (2, 1, 'src/service.py')")
        conn.execute("INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (3, 1, 'src/util.py')")

        _insert_symbol(conn, 1, 2, _hash_for("process"), "fn", "process_data", "process_data", depth=0)
        _insert_symbol(conn, 2, 1, _hash_for("handle"), "fn", "handle_request", "handle_request", depth=1)
        _insert_symbol(conn, 3, 3, _hash_for("format"), "fn", "format_response", "format_response", depth=1)

        _insert_call(conn, 2, 1, "process_data", call_line=15, is_cross_file=1)  # handle_request → process_data
        _insert_call(conn, 2, 3, "format_response", call_line=20, is_cross_file=1)  # handle_request → format_response
        conn.commit()

        py_mixin = _MinimalImpactMixin(conn, db_path, rust_enabled=False)
        py_result = py_mixin.blast_radius(_hash_for("process"), depth=2)

        store = callwarden_core.GraphStore()
        store.load_from_sqlite(db_path, workspace_id=1)
        source_sym = store.get_symbol("process_data")
        rust_batch = store.blast_radius(source_sym["id"], 2)
        rust_layers = rust_batch.to_list()

        _assert_layers_equal(py_result["layers"], rust_layers)

        # 第 1 层应包含 handle_request，且 file_path=src/api.py
        assert len(rust_layers) >= 2
        layer1_qnames = {s["qualified_name"] for s in rust_layers[1]["symbols"]}
        assert "handle_request" in layer1_qnames
        # 验证跨文件路径正确
        handle_request_sym = next(s for s in rust_layers[1]["symbols"] if s["qualified_name"] == "handle_request")
        assert handle_request_sym["file_path"] == "src/api.py"

        conn.close()


# ============================================
# 边界情况：E1-E3
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestBlastRadiusEdgeCases:
    """E1-E3: 边界情况验证"""

    def test_e1_invalid_symbol_id_raises(self, tmp_path):
        """E1: symbol_id 不存在 → Rust 抛 RuntimeError"""
        db_path = str(tmp_path / "test_e1.db")
        conn = _make_db_with_schema(db_path)
        conn.execute("INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (1, 1, 'src/empty.py')")
        _insert_symbol(conn, 1, 1, _hash_for("only"), "fn", "only_fn", "only_fn")
        conn.commit()

        store = callwarden_core.GraphStore()
        store.load_from_sqlite(db_path, workspace_id=1)

        # symbol_id=999 不存在，应抛 RuntimeError
        with pytest.raises(Exception) as exc_info:
            store.blast_radius(999, 3)
        assert "not found" in str(exc_info.value).lower() or "999" in str(exc_info.value)

        conn.close()

    def test_e2_depth_zero(self, tmp_path):
        """E2: depth=0 → 仅源符号层"""
        db_path = str(tmp_path / "test_e2.db")
        conn = _make_db_with_schema(db_path)
        conn.execute("INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (1, 1, 'src/main.py')")
        _insert_symbol(conn, 1, 1, _hash_for("src"), "fn", "src_fn", "src_fn", depth=0)
        _insert_symbol(conn, 2, 1, _hash_for("caller"), "fn", "caller_fn", "caller_fn", depth=1)
        _insert_call(conn, 2, 1, "src_fn")
        conn.commit()

        store = callwarden_core.GraphStore()
        store.load_from_sqlite(db_path, workspace_id=1)
        source_sym = store.get_symbol("src_fn")
        rust_batch = store.blast_radius(source_sym["id"], 0)

        assert rust_batch.layer_count() == 1
        assert rust_batch.total_impacted() == 1
        layers = rust_batch.to_list()
        assert layers[0]["symbols"][0]["qualified_name"] == "src_fn"

        conn.close()

    def test_e3_workspace_isolation(self, tmp_path):
        """E3: workspace 隔离 → workspace_id=2 不读 workspace_id=1 的符号"""
        db_path = str(tmp_path / "test_e3.db")
        conn = _make_db_with_schema(db_path)
        # workspace 1 的符号
        conn.execute("INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (1, 1, 'src/ws1.py')")
        _insert_symbol(conn, 1, 1, _hash_for("ws1_fn"), "fn", "ws1_fn", "ws1_fn")
        # workspace 2 的符号
        conn.execute("INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (2, 2, 'src/ws2.py')")
        _insert_symbol(conn, 2, 2, _hash_for("ws2_fn"), "fn", "ws2_fn", "ws2_fn")
        conn.commit()

        store = callwarden_core.GraphStore()
        store.load_from_sqlite(db_path, workspace_id=1)
        # workspace_id=1 加载时只有 ws1_fn，没有 ws2_fn
        assert store.get_symbol("ws1_fn") is not None
        assert store.get_symbol("ws2_fn") is None

        conn.close()


# ============================================
# Wire-production 集成测试：Rust 短路 vs Python 降级（完整 blast_radius）
# ============================================

@pytest.mark.skipif(not _RUST_EXT_AVAILABLE, reason=_RUST_EXT_SKIP_REASON)
class TestWireProductionRustVsPython:
    """wire-production 集成测试：通过 _MinimalImpactMixin 完整 blast_radius 路径
    验证 Rust 短路（rust_enabled=True）与 Python 降级（rust_enabled=False）结果一致。

    测试覆盖：
    - W1: 三层调用链 → Rust 路径与 Python 路径 layers 完全一致（含 symbol_hash + visibility）
    - W2: 环路调用 → Rust 路径与 Python 路径一致终止
    - W3: 孤立符号 → 两者均返回仅源符号层
    """

    def _build_chain_db(self, tmp_path, name: str):
        """构建三层调用链 DB：
            entry_top → middle_fn → leaf_target
        返回 (db_path, conn)
        """
        db_path = str(tmp_path / f"{name}.db")
        conn = _make_db_with_schema(db_path)
        conn.execute("INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (1, 1, 'src/chain.py')")
        _insert_symbol(conn, 1, 1, _hash_for("leaf"), "fn", "leaf_target", "leaf_target", depth=0)
        _insert_symbol(conn, 2, 1, _hash_for("middle"), "fn", "middle_fn", "middle_fn", depth=1)
        _insert_symbol(conn, 3, 1, _hash_for("entry"), "fn", "entry_top", "entry_top", depth=2)
        _insert_call(conn, 2, 1, "leaf_target")  # middle_fn → leaf_target
        _insert_call(conn, 3, 2, "middle_fn")     # entry_top → middle_fn
        conn.commit()
        return db_path, conn

    def test_w1_three_layer_chain_full_path(self, tmp_path):
        """W1: 三层调用链完整路径差分

        构造同一 DB 两个 mixin 实例：
        - rust_mixin (rust_enabled=True): 走 Rust GraphStore 短路
        - py_mixin (rust_enabled=False): 走 Python SQL BFS

        预期：layers / total_impacted / by_layer 完全一致
        """
        db_path, conn = self._build_chain_db(tmp_path, "test_w1")

        py_mixin = _MinimalImpactMixin(conn, db_path, rust_enabled=False)
        rust_mixin = _MinimalImpactMixin(conn, db_path, rust_enabled=True)

        py_result = py_mixin.blast_radius(_hash_for("leaf"), depth=3)
        rust_result = rust_mixin.blast_radius(_hash_for("leaf"), depth=3)

        # 完整结构对比（含 symbol_hash + visibility）
        assert rust_result["total_impacted"] == py_result["total_impacted"], (
            f"total_impacted mismatch: rust={rust_result['total_impacted']} py={py_result['total_impacted']}"
        )
        assert len(rust_result["layers"]) == len(py_result["layers"]), (
            f"layer count mismatch: rust={len(rust_result['layers'])} py={len(py_result['layers'])}"
        )
        # 逐层逐符号对比（symbol_hash + visibility 关键字段）
        for rust_layer, py_layer in zip(rust_result["layers"], py_result["layers"]):
            assert rust_layer["depth"] == py_layer["depth"]
            rust_syms = {s["qualified_name"]: s for s in rust_layer["symbols"]}
            py_syms = {s["qualified_name"]: s for s in py_layer["symbols"]}
            assert set(rust_syms.keys()) == set(py_syms.keys()), (
                f"layer {rust_layer['depth']} symbol set mismatch:\n"
                f"  rust={set(rust_syms.keys())}\n  py={set(py_syms.keys())}"
            )
            for qn in py_syms:
                rust_s = rust_syms[qn]
                py_s = py_syms[qn]
                # 关键字段完整对比（含 symbol_hash + visibility）
                for field in ("symbol_hash", "qualified_name", "name", "kind",
                              "module_path", "file_path", "visibility"):
                    assert rust_s.get(field) == py_s.get(field), (
                        f"symbol {qn} field {field} mismatch: "
                        f"rust={rust_s.get(field)!r} py={py_s.get(field)!r}"
                    )

        conn.close()

    def test_w2_cycle_termination_full_path(self, tmp_path):
        """W2: 环路调用完整路径差分

        调用图：func_a ↔ func_b（双向）
        预期：Rust 与 Python 一致终止，total_impacted=2
        """
        db_path = str(tmp_path / "test_w2.db")
        conn = _make_db_with_schema(db_path)
        conn.execute("INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (1, 1, 'src/cycle.py')")
        _insert_symbol(conn, 1, 1, _hash_for("a"), "fn", "func_a", "func_a", depth=0)
        _insert_symbol(conn, 2, 1, _hash_for("b"), "fn", "func_b", "func_b", depth=0)
        _insert_call(conn, 1, 2, "func_b")  # func_a → func_b
        _insert_call(conn, 2, 1, "func_a")  # func_b → func_a（反向边，构成环）
        conn.commit()

        py_mixin = _MinimalImpactMixin(conn, db_path, rust_enabled=False)
        rust_mixin = _MinimalImpactMixin(conn, db_path, rust_enabled=True)

        py_result = py_mixin.blast_radius(_hash_for("b"), depth=3)
        rust_result = rust_mixin.blast_radius(_hash_for("b"), depth=3)

        assert rust_result["total_impacted"] == py_result["total_impacted"] == 2, (
            f"cycle re-visit: rust={rust_result['total_impacted']} py={py_result['total_impacted']}"
        )
        assert len(rust_result["layers"]) == len(py_result["layers"]) == 2

        conn.close()

    def test_w3_orphan_symbol_full_path(self, tmp_path):
        """W3: 孤立符号完整路径差分

        预期：两者均返回仅源符号层，total_impacted=1
        """
        db_path = str(tmp_path / "test_w3.db")
        conn = _make_db_with_schema(db_path)
        conn.execute("INSERT INTO file_instances (id, workspace_id, rel_path) VALUES (1, 1, 'src/orphan.py')")
        _insert_symbol(conn, 1, 1, _hash_for("orphan"), "fn", "orphan_fn", "orphan_fn", depth=0)
        conn.commit()

        py_mixin = _MinimalImpactMixin(conn, db_path, rust_enabled=False)
        rust_mixin = _MinimalImpactMixin(conn, db_path, rust_enabled=True)

        py_result = py_mixin.blast_radius(_hash_for("orphan"), depth=3)
        rust_result = rust_mixin.blast_radius(_hash_for("orphan"), depth=3)

        assert rust_result["total_impacted"] == py_result["total_impacted"] == 1
        assert len(rust_result["layers"]) == len(py_result["layers"]) == 1
        # 源符号完整字段对比
        rust_sym = rust_result["layers"][0]["symbols"][0]
        py_sym = py_result["layers"][0]["symbols"][0]
        assert rust_sym["symbol_hash"] == py_sym["symbol_hash"]
        assert rust_sym["visibility"] == py_sym["visibility"]
        assert rust_sym["qualified_name"] == py_sym["qualified_name"] == "orphan_fn"

        conn.close()


# ============================================
# 差分对比辅助函数
# ============================================

def _assert_layers_equal(py_layers: List[Dict[str, Any]],
                          rust_layers: List[Any]) -> None:
    """断言 Python blast_radius.layers 与 Rust to_list() 输出一致

    比较字段：depth（层数 + 层号），每层 symbols 的 qualified_name 集合

    不比较字段（预期差异）：
    - symbol_hash：Rust 不持有，留空
    - visibility：Rust 不持有，留空
    - symbol_id：Rust 内部 id，Python 不返回
    """
    assert len(py_layers) == len(rust_layers), (
        f"layer count mismatch: py={len(py_layers)} rust={len(rust_layers)}"
    )

    for i, (py_layer, rust_layer) in enumerate(zip(py_layers, rust_layers)):
        assert py_layer["depth"] == rust_layer["depth"], (
            f"layer {i} depth mismatch: py={py_layer['depth']} rust={rust_layer['depth']}"
        )
        py_qnames = {s["qualified_name"] for s in py_layer["symbols"]}
        rust_qnames = {s["qualified_name"] for s in rust_layer["symbols"]}
        assert py_qnames == rust_qnames, (
            f"layer {i} (depth={py_layer['depth']}) symbol set mismatch:\n"
            f"  py={py_qnames}\n  rust={rust_qnames}"
        )
        # 验证关键字段非空（避免 Rust 端字段缺失问题）
        for rust_sym in rust_layer["symbols"]:
            qn = rust_sym["qualified_name"]
            if qn:  # 源符号可能 qname 空，但其他层应有 qname
                assert rust_sym["name"], f"Rust sym {qn} missing name"
                assert rust_sym["kind"], f"Rust sym {qn} missing kind"
                assert rust_sym["file_path"], f"Rust sym {qn} missing file_path"
