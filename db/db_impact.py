"""
db_impact.py
============

变更影响智能 Mixin。

提供 blast_radius / diff_to_symbol / cross_layer_impact / review_readiness_report 等方法。
通过 Mixin 模式集成到 CodeGraphDB 主类，可通过 self.conn（sqlite3.Connection）访问数据库。
"""

from __future__ import annotations

import re
import subprocess
import time
from typing import Any, Dict, List, Optional

from .db_risk import (
    classify_edge,
    collect_risk_signals,
    compute_risk_score,
    edge_confidence_weight,
    is_noisy_raw_name,
    is_security_named,
    summarize_edges,
)


class ImpactMixin:
    """变更影响智能 Mixin

    提供变更影响半径计算（blast_radius）、diff 到符号映射（diff_to_symbol）、
    跨层影响分析（cross_layer_impact）、审查就绪报告（review_readiness_report）等能力。

    依赖：
    - symbols 表：通过 symbol_hash / qualified_name / start_line / end_line 定位符号
    - calls 表：通过 callee_qualified 反向追溯调用方
    - symbol_contents 表：通过 content 字段做 SQL / API / 配置层启发式分析
    - change_impacts 表：跨层影响结果持久化（source_symbol / target_layer）
    """

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_path(p: str) -> str:
        """将路径统一为正斜杠形式并去除首尾空白与引号"""
        return p.replace("\\", "/").strip().strip('"')

    def _query_overlapping_symbols(
        self, ws_id: int, rel_path: str, start_line: int, end_line: int
    ) -> List[Dict[str, Any]]:
        """查询行号范围与符号 [start_line, end_line] 重叠的符号

        重叠条件：symbol.start_line <= end_line AND symbol.end_line >= start_line
        先按 rel_path 精确匹配，未命中则尝试后缀匹配兜底。

        Args:
            ws_id: 工作区 ID
            rel_path: 文件相对路径（正斜杠）
            start_line: 变更区间起始行
            end_line: 变更区间结束行

        Returns:
            符号字典列表（symbol_hash / qualified_name / rel_path）
        """
        # 精确 rel_path 匹配
        cur = self.conn.execute(
            """
            SELECT s.symbol_hash, s.qualified_name, fi.rel_path
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND fi.rel_path = ?
              AND s.start_line <= ? AND s.end_line >= ?
            """,
            (ws_id, rel_path, end_line, start_line),
        )
        rows = [dict(r) for r in cur.fetchall()]
        if rows:
            return rows

        # 后缀匹配兜底（应对路径前缀差异）
        cur = self.conn.execute(
            """
            SELECT s.symbol_hash, s.qualified_name, fi.rel_path
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
              AND s.start_line <= ? AND s.end_line >= ?
            """,
            (ws_id, end_line, start_line),
        )
        out: List[Dict[str, Any]] = []
        for r in cur:
            rel = r["rel_path"] or ""
            if rel and (rel.endswith(rel_path) or rel_path.endswith(rel)):
                out.append(dict(r))
        return out

    def _persist_impacts(
        self,
        source_hash: str,
        source_qn: str,
        code_layer: List[Dict[str, Any]],
        db_layer: List[Dict[str, Any]],
        api_layer: List[Dict[str, Any]],
        config_layer: List[Dict[str, Any]],
    ) -> None:
        """将跨层影响分析结果持久化到 change_impacts 表

        每次分析先清除该源符号的旧记录，再批量写入，保证幂等。

        Args:
            source_hash: 源符号 hash（作为 source_symbol 稳定标识）
            source_qn: 源符号限定名
            code_layer / db_layer / api_layer / config_layer: 各层影响项列表
        """
        now = time.time()
        # 清除该源的旧记录，避免重复分析导致冗余
        self.conn.execute(
            "DELETE FROM change_impacts WHERE source_symbol = ?", (source_hash,)
        )

        rows: List[tuple] = []
        for item in code_layer:
            rows.append((source_hash, "call", item.get("qualified_name", ""), "code", 1.0, now))
        for item in db_layer:
            rows.append((source_hash, "sql_table", item.get("table", ""), "db", 0.8, now))
        for item in api_layer:
            rows.append((source_hash, "api_endpoint", item.get("symbol", ""), "api", 0.9, now))
        for item in config_layer:
            rows.append((source_hash, "config_ref", item.get("config_key", ""), "config", 0.7, now))

        if rows:
            self.conn.executemany(
                """
                INSERT INTO change_impacts
                    (source_symbol, impact_type, target_symbol, target_layer, confidence, detected_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        self.conn.commit()

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def diff_to_symbol(self, diff_text: str) -> List[Dict[str, Any]]:
        """解析 git diff 文本，映射到受影响的符号

        解析流程：
        1. 从 `+++ b/path` / `--- a/path` 提取文件路径
        2. 从 `@@ -old,n +new,m @@` 提取行号范围
        3. 查询 symbols 表，找到行号范围与符号 start_line/end_line 重叠的符号
        4. 根据 +/- 行计数与文件是否被删除，判定 change_type

        change_type 判定规则：
        - 文件被删除（+++ /dev/null）→ "deleted"，按旧行范围匹配符号
        - 仅有删除行（-）→ "deleted"
        - 仅有新增行（+）→ "added"
        - 增删并存 → "modified"

        简化实现：用正则解析 diff，不追求完美。

        Args:
            diff_text: git diff 输出文本（unified 格式）

        Returns:
            受影响符号列表，每个元素包含：
            - symbol_hash: 符号 hash
            - qualified_name: 符号限定名
            - file_path: 文件路径（正斜杠）
            - change_type: "added" / "modified" / "deleted"
            按 symbol_hash 去重，保留首次出现的 change_type。
        """
        ws_id = self._get_active_workspace_id()
        results: List[Dict[str, Any]] = []

        old_file: Optional[str] = None
        new_file: Optional[str] = None
        file_deleted = False
        hunk_old_start = 0
        hunk_old_len = 0
        hunk_new_start = 0
        hunk_new_len = 0
        hunk_added = 0
        hunk_removed = 0
        has_hunk = False

        new_file_re = re.compile(r"^\+\+\+\s+b/(.*)$")
        new_devnull_re = re.compile(r"^\+\+\+\s+/dev/null")
        old_file_re = re.compile(r"^---\s+a/(.*)$")
        hunk_re = re.compile(r"^@@ -(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s*@@")

        def flush() -> None:
            """刷新当前 hunk 缓冲区，将受影响符号写入结果列表"""
            nonlocal has_hunk, hunk_added, hunk_removed
            if not has_hunk:
                return
            # 删除时用旧路径 + 旧行范围；否则用新路径 + 新行范围
            if file_deleted:
                query_path = old_file
                start = hunk_old_start
                length = hunk_old_len
            else:
                query_path = new_file
                start = hunk_new_start
                length = hunk_new_len

            has_hunk = False
            hunk_added = 0
            hunk_removed = 0

            if not query_path:
                return

            end = start + max(length, 1) - 1

            # 判定变更类型
            if file_deleted:
                change_type = "deleted"
            elif hunk_removed > 0 and hunk_added == 0:
                change_type = "deleted"
            elif hunk_added > 0 and hunk_removed == 0:
                change_type = "added"
            else:
                change_type = "modified"

            syms = self._query_overlapping_symbols(ws_id, query_path, start, end)
            for sym in syms:
                results.append({
                    "symbol_hash": sym["symbol_hash"],
                    "qualified_name": sym["qualified_name"],
                    "file_path": query_path,
                    "change_type": change_type,
                })

        for raw_line in diff_text.splitlines():
            # 新文件为 /dev/null → 文件被删除
            if new_devnull_re.match(raw_line):
                flush()
                new_file = None
                file_deleted = True
                continue
            m_new = new_file_re.match(raw_line)
            if m_new:
                flush()
                new_file = self._normalize_path(m_new.group(1))
                file_deleted = False
                continue
            m_old = old_file_re.match(raw_line)
            if m_old:
                flush()
                old_file = self._normalize_path(m_old.group(1))
                continue
            m_hunk = hunk_re.match(raw_line)
            if m_hunk:
                flush()
                hunk_old_start = int(m_hunk.group(1))
                hunk_old_len = int(m_hunk.group(2)) if m_hunk.group(2) else 1
                hunk_new_start = int(m_hunk.group(3))
                hunk_new_len = int(m_hunk.group(4)) if m_hunk.group(4) else 1
                hunk_added = 0
                hunk_removed = 0
                has_hunk = True
                continue
            if has_hunk:
                if raw_line.startswith("+"):
                    hunk_added += 1
                elif raw_line.startswith("-"):
                    hunk_removed += 1

        flush()

        # 按 symbol_hash 去重，保留首次出现的 change_type
        seen: Dict[str, Dict[str, Any]] = {}
        for r in results:
            if r["symbol_hash"] not in seen:
                seen[r["symbol_hash"]] = r
        return list(seen.values())

    # ------------------------------------------------------------------
    # Phase 6-1: Rust GraphStore 短路（blast_radius）
    # ------------------------------------------------------------------

    def _blast_radius_via_rust(self, symbol_hash: str, depth: int) -> Optional[Dict[str, Any]]:
        """Rust 版 blast_radius 短路（CSR 内存索引 BFS）

        通过 `_get_graph_store()` 复用已加载的 GraphStore（Rust CSR 邻接表），
        执行 BFS 反向遍历。失败时返回 None，由调用方降级到 SQL 路径。

        与 Python `blast_radius` 行为一致性：
        - 第 0 层为源符号
        - qualified_name + symbol_id 双重去重（对齐 Python 的 qn + hash 去重）
        - cross_layer_impact 仍走 Python（涉及正则提取，本阶段未迁移）
        - symbol_hash + visibility 字段通过单次批量 SQL 补全（Rust 不持有这两列）

        Args:
            symbol_hash: 源符号 hash
            depth: BFS 遍历最大深度

        Returns:
            与 Python blast_radius 相同结构的 dict，或 None（Rust 不可用/失败）
        """
        store = self._get_graph_store()
        if store is None:
            return None
        # 等待 calls 加载完成（避免首次查询 fallback 到 SQL）
        if store.load_state() != "graph_ready":
            self._wait_for_calls_ready(timeout=2.0)
            store = self._get_graph_store()
            if store is None or store.load_state() != "graph_ready":
                return None

        ws_id = self._get_active_workspace_id()

        # 1. 查源符号 symbol_hash → symbol_id + 完整元数据（单次 SQL）
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
            # 源符号不存在：返回与 Python 一致的空结构
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
        _source_info = {
            "symbol_hash": row["symbol_hash"],
            "qualified_name": source_qn,
            "name": row["name"],
            "module_path": row["module_path"],
            "file_path": row["rel_path"],
            "visibility": row["visibility"],
            "kind": row["kind"],
        }

        # 2. 调用 Rust blast_radius（CSR 内存 BFS）
        try:
            rust_batch = store.blast_radius(source_id, depth)
        except Exception:
            # Rust 查询异常 → fail-soft 降级到 SQL
            return None

        # 3. 转换为 Python layers 格式
        rust_layers = rust_batch.to_list()

        # 4. 批量补全 symbol_hash + visibility（Rust 不持有这两列）
        # 单次 SQL 查询所有 layer 中的 symbol_id，构建 lookup dict
        all_symbol_ids: List[int] = []
        for layer in rust_layers:
            for sym in layer["symbols"]:
                sym_id = sym.get("symbol_id")
                if sym_id is not None:
                    all_symbol_ids.append(sym_id)

        # 源符号信息已在 step 1 查到，无需重复
        id_to_hash_vis: Dict[int, tuple] = {source_id: (row["symbol_hash"], row["visibility"])}
        if all_symbol_ids:
            # 排除已查到的源符号，避免重复
            other_ids = [i for i in all_symbol_ids if i != source_id]
            if other_ids:
                placeholders = ",".join("?" * len(other_ids))
                cur2 = self.conn.execute(
                    f"""
                    SELECT s.id, s.symbol_hash, s.visibility
                    FROM symbols s
                    WHERE s.id IN ({placeholders})
                    """,  # nosec B608 - placeholders 仅为 ? 占位符，值全部参数化
                    other_ids,
                )
                for r in cur2:
                    id_to_hash_vis[r["id"]] = (r["symbol_hash"], r["visibility"])

        # 5. 组装最终 layers（与 Python 格式完全一致）
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

        # 6. cross_layer_impact 仍走 Python（涉及正则提取，本阶段未迁移）
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
        """计算变更影响半径（BFS 反向遍历调用图）

        从源符号出发，通过 calls 表反向查找所有调用方（who calls this symbol），
        逐层 BFS 遍历到 depth 层。同时调用 cross_layer_impact 获取跨层影响，
        合并到 by_layer。

        Args:
            symbol_hash: 源符号 hash
            depth: BFS 遍历最大深度（默认 3）

        Returns:
            影响树字典：
            - source_symbol: 源符号限定名
            - source_hash: 源符号 hash
            - depth: 遍历深度
            - layers: [{"depth": 0, "symbols": [...]}, ...]，第 0 层为源符号自身
            - total_impacted: 影响树中符号总数（含源符号）
            - by_layer: {"code": N, "db": M, "api": K, "config": L}，来自 cross_layer_impact
            源符号不存在时返回空结构。
        """
        # Phase 6-1 wire-production: Rust GraphStore 短路（CSR 内存索引 BFS）
        # rollback_config 中 feature=rust_blast_radius 置为 1 时回退 Python SQL 路径
        # Rust 失败时 fail-soft 降级到 SQL BFS
        if not self.is_feature_rolled_back("rust_blast_radius"):
            rust_result = self._blast_radius_via_rust(symbol_hash, depth)
            if rust_result is not None:
                return rust_result

        ws_id = self._get_active_workspace_id()

        # 查找源符号
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
        # 去重集合：按 qualified_name 与 symbol_hash 双重去重
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
                """,  # nosec B608 - placeholders 仅为 ? 占位符，值全部参数化
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

        # 跨层影响，合并到 by_layer
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

    def cross_layer_impact(self, symbol_hash: str) -> Dict[str, Any]:
        """跨层影响分析

        分析源符号对四个层面的潜在影响：
        - 代码层（code）：通过 calls 表反向查找调用方
        - DB 层（db）：从 symbol_contents.content 用正则提取 SQL 表名
          （FROM / UPDATE / INSERT INTO / DELETE FROM）
        - API 层（api）：检测函数名是否含 route/handler/endpoint 关键词，
          或 content 中存在 HTTP 方法注解（#[get(...)] / #[post(...)]）
          或路由装饰器（@app.route(...) / @app.get(...)）
        - 配置层（config）：从 content 用正则提取配置项引用
          （env::var(...) / std::env::var(...) / config.get(...)）

        纯只读 API：分析结果直接返回，不再持久化到 change_impacts 表
        （原 _persist_impacts 写入的数据从未被读取，移除以消除 fsync 开销）。

        Phase 6-1 P2 wire-production：Rust 短路（feature=rust_cross_layer_impact）。
        Rust 负责 db/api/config 三层正则匹配（regex crate），
        Python 负责 code 层 SQL 查询。Rust 失败时 fail-soft 降级到 Python 全路径。

        Args:
            symbol_hash: 源符号 hash

        Returns:
            {"code": [...], "db": [...], "api": [...], "config": [...]}
            源符号不存在时各层均为空列表。
        """
        ws_id = self._get_active_workspace_id()

        # 查找源符号及其内容
        cur = self.conn.execute(
            """
            SELECT s.id, s.symbol_hash, s.qualified_name, s.name, s.module_path,
                   s.visibility, s.kind, fi.rel_path, sc.content
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            LEFT JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash
            WHERE fi.workspace_id = ? AND s.symbol_hash = ?
            LIMIT 1
            """,
            (ws_id, symbol_hash),
        )
        row = cur.fetchone()
        if not row:
            return {"code": [], "db": [], "api": [], "config": []}

        source_qn = row["qualified_name"] or ""
        source_name = row["name"] or ""
        content = row["content"] or ""

        # ---- 代码层：反向查找调用方（Python 负责 SQL 查询）----
        code_layer: List[Dict[str, Any]] = []
        cur = self.conn.execute(
            """
            SELECT DISTINCT
                s.qualified_name, s.name, s.module_path, s.visibility, s.kind, fi.rel_path
            FROM calls c
            JOIN symbols s ON c.caller_id = s.id
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
              AND c.callee_id > 0
              AND c.callee_id = ?
            """,
            (ws_id, row["id"]),
        )
        for r in cur:
            code_layer.append({
                "qualified_name": r["qualified_name"],
                "name": r["name"],
                "module_path": r["module_path"],
                "visibility": r["visibility"],
                "kind": r["kind"],
                "file_path": r["rel_path"],
            })

        # Phase 6-1 P2 wire-production: Rust 短路（feature=rust_cross_layer_impact）
        # Rust 负责 db/api/config 三层正则匹配（regex crate），code 层已由 Python 查出
        if not self.is_feature_rolled_back("rust_cross_layer_impact"):
            rust_result = self._cross_layer_impact_via_rust(
                source_qn, source_name, content, code_layer
            )
            if rust_result is not None:
                return rust_result

        # ---- Python 全路径 fallback ----

        # ---- DB 层：从 content 中正则提取 SQL 表名 ----
        db_layer: List[Dict[str, Any]] = []
        table_names = set()
        sql_patterns = [
            re.compile(r"\bFROM\s+(\w+)", re.IGNORECASE),
            re.compile(r"\bUPDATE\s+(\w+)", re.IGNORECASE),
            re.compile(r"\bINSERT\s+INTO\s+(\w+)", re.IGNORECASE),
            re.compile(r"\bDELETE\s+FROM\s+(\w+)", re.IGNORECASE),
        ]
        for pat in sql_patterns:
            for m in pat.finditer(content):
                table_names.add(m.group(1))
        for tbl in sorted(table_names):
            db_layer.append({"table": tbl, "source": source_qn})

        # ---- API 层：函数名关键词或 HTTP 方法注解 / 路由装饰器 ----
        api_layer: List[Dict[str, Any]] = []
        name_lower = source_name.lower()
        is_api_name = (
            "route" in name_lower
            or "handler" in name_lower
            or "endpoint" in name_lower
        )
        # Rust 属性注解：#[get(...)] / #[post(...)] 等
        http_annotation = re.search(
            r"#\[(?:get|post|put|delete|patch|head|options)\s*\(",
            content,
            re.IGNORECASE,
        )
        # Python 路由装饰器：@app.route(...) / @app.get(...) 等
        route_decorator = re.search(
            r"@\w+\.(?:route|get|post|put|delete|patch)\s*\(",
            content,
            re.IGNORECASE,
        )
        if is_api_name or http_annotation or route_decorator:
            reasons = []
            if is_api_name:
                reasons.append("function_name_keyword")
            if http_annotation:
                reasons.append("http_method_annotation")
            if route_decorator:
                reasons.append("route_decorator")
            api_layer.append({
                "symbol": source_qn,
                "name": source_name,
                "reason": ",".join(reasons),
            })

        # ---- 配置层：从 content 中正则提取配置项引用 ----
        config_layer: List[Dict[str, Any]] = []
        config_keys = set()
        config_patterns = [
            re.compile(r"env::var\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
            re.compile(r"std::env::var\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
            re.compile(r"config\.get\s*\(\s*['\"]([^'\"]+)['\"]\s*\)"),
        ]
        for pat in config_patterns:
            for m in pat.finditer(content):
                config_keys.add(m.group(1))
        for key in sorted(config_keys):
            config_layer.append({"config_key": key, "source": source_qn})

        # 注：change_impacts 表写入已移除（write-only 死表，从未被 SELECT 读取）。
        # cross_layer_impact 现在是纯只读 API，blast_radius/review_readiness 不再触发
        # DELETE + INSERT + commit，消除 fsync 开销（1K 79ms → <1ms）。
        # 如需持久化分析结果，调用方显式调 _persist_impacts。

        return {
            "code": code_layer,
            "db": db_layer,
            "api": api_layer,
            "config": config_layer,
        }

    def _cross_layer_impact_via_rust(
        self,
        source_qn: str,
        source_name: str,
        content: str,
        code_layer: List[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Rust 短路：调用 callwarden_core.py_cross_layer_impact

        Python 负责 code 层 SQL 查询，Rust 负责 db/api/config 三层正则匹配。

        Args:
            source_qn: 源符号限定名
            source_name: 源符号名称
            content: 符号源代码内容
            code_layer: Python 已查到的 code 层调用方列表

        Returns:
            {"code": [...], "db": [...], "api": [...], "config": [...]} 或 None
        """
        try:
            import callwarden_core  # type: ignore
        except ImportError:
            return None

        # 构造 Rust 输入：List[Tuple[str, str, str, str, str, str]]
        rust_code_layer = [
            (
                e.get("qualified_name", ""),
                e.get("name", ""),
                e.get("module_path", ""),
                e.get("visibility", ""),
                e.get("kind", ""),
                e.get("file_path", ""),
            )
            for e in code_layer
        ]

        try:
            result = callwarden_core.py_cross_layer_impact(
                source_qn, source_name, content, rust_code_layer
            )
            # 物化懒批对象为 list（AGENTS.md 规则 17）
            return {
                "code": list(result.get("code", [])),
                "db": list(result.get("db", [])),
                "api": list(result.get("api", [])),
                "config": list(result.get("config", [])),
            }
        except Exception:
            # Rust 异常 → fail-soft 降级到 Python
            return None

    def review_readiness_report(
        self, symbol_hash: str, min_confidence: Optional[float] = None
    ) -> Dict[str, Any]:
        """审查就绪报告

        综合影响半径与跨层影响，给出变更审查建议：
        - 调用 blast_radius 获取影响范围
        - 调用 cross_layer_impact 获取跨层影响
        - 风险等级：影响 > 20 个符号为 high，> 5 为 medium，否则 low
        - 必测项：列出受影响的 public 函数（visibility=public 且 kind 为函数/方法）
        - 人工审查点：列出 block 级别的影响（DB/API 层）
        - 若 CoverageMixin 可用（self.get_coverage_for_symbol 存在），附带源符号覆盖率

        Args:
            symbol_hash: 源符号 hash
            min_confidence: 可选置信度下限（0.8=WILL_BREAK / 0.4=LIKELY_AFFECTED）；
                给定时只保留 confidence >= 该值的候选（含 UNAFFECTED 过滤）。
                缺省 None 时输出与不传参完全一致（向后兼容）。

        Returns:
            报告字典：
            - impact_scope: "high" / "medium" / "low"
            - risk_level: 同 impact_scope（旧阈值口径，保持向后兼容）
            - total_impacted: 影响符号总数
            - must_test: 受影响 public 函数列表
            - review_points: DB/API 层人工审查点列表
            - by_layer: 各层影响计数
            - coverage: （可选）源符号覆盖率信息
            - risk_score: 六信号加权总分（深度/跨模块/测试缺口/安全面/调用压力/变更热度）
            - risk_signals: 上述六信号各自封顶后的分值
            - edge_confidence: 受影响符号的边置信度四档汇总
              （per_symbol/min_confidence/classification/histogram/ranked）
            - weighted_impacted: 按边置信度加权后的影响面（替代纯计数口径）
        """
        blast = self.blast_radius(symbol_hash)
        cross = self.cross_layer_impact(symbol_hash)
        total = blast["total_impacted"]

        # 风险等级 / 影响范围
        if total > 20:
            scope = "high"
        elif total > 5:
            scope = "medium"
        else:
            scope = "low"

        # 必测项：受影响的 public 函数
        must_test: List[Dict[str, Any]] = []
        public_kinds = {"fn", "function", "method"}
        seen_test: set = set()
        for layer in blast["layers"]:
            for sym in layer["symbols"]:
                vis = (sym.get("visibility") or "").lower()
                kind = (sym.get("kind") or "").lower()
                qn = sym.get("qualified_name") or ""
                if vis == "public" and kind in public_kinds and qn not in seen_test:
                    seen_test.add(qn)
                    must_test.append({
                        "qualified_name": qn,
                        "name": sym.get("name", ""),
                        "file_path": sym.get("file_path", ""),
                    })

        # 人工审查点：DB / API 层影响（block 级别）
        review_points: List[Dict[str, Any]] = []
        for item in cross["db"]:
            tbl = item.get("table", "")
            review_points.append({
                "layer": "db",
                "target": tbl,
                "source": item.get("source", ""),
                "message": f"DB 表受影响: {tbl}",
            })
        for item in cross["api"]:
            sym_name = item.get("name", "")
            review_points.append({
                "layer": "api",
                "target": item.get("symbol", ""),
                "source": sym_name,
                "message": f"API 端点受影响: {sym_name}",
            })

        report: Dict[str, Any] = {
            "impact_scope": scope,
            "risk_level": scope,
            "total_impacted": total,
            "must_test": must_test,
            "review_points": review_points,
            "by_layer": blast["by_layer"],
        }

        # 覆盖率（如果 CoverageMixin 可用）
        source_qn = blast.get("source_symbol", "")
        if hasattr(self, "get_coverage_for_symbol") and source_qn:
            try:
                cov = self.get_coverage_for_symbol(source_qn)
                if cov:
                    report["coverage"] = {
                        "qualified_name": cov.get("qualified_name"),
                        "coverage_pct": cov.get("coverage_pct", 0.0),
                        "covered_lines": cov.get("covered_lines", 0),
                        "tracked_lines": cov.get("tracked_lines", 0),
                    }
            except Exception:
                # 覆盖率查询失败不影响报告生成
                pass

        # 风险评分模型 + 边置信度四档（新增字段，不改旧键语义）
        report.update(
            self._build_risk_profile(symbol_hash, blast, must_test, min_confidence)
        )
        return report

    # ------------------------------------------------------------------
    # Smart CI：变更驱动的最小检查集选择
    # ------------------------------------------------------------------

    # 语法检查推荐命令（按语言；未列语言不产出 syntax 检查项）
    _SYNTAX_COMMANDS: Dict[str, str] = {
        "python": "python -m py_compile {file}",
        "rust": "cargo check",
        "javascript": "npx tsc --noEmit",
        "typescript": "npx tsc --noEmit",
    }

    @staticmethod
    def _escape_like(value: str) -> str:
        """转义 LIKE 通配符，避免路径中的 % / _ 被当作通配符。"""
        return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")

    def _symbols_for_changed_files(
        self, ws_id: int, changed_files: List[str]
    ) -> List[Dict[str, Any]]:
        """把变更文件解析为工作区内的符号

        精确 rel_path 优先；未命中时按后缀匹配兜底（应对 diff 路径前缀差异）。
        结果按 (file_path, qualified_name, symbol_hash) 排序，保证同一变更
        反复调用得到完全一致的选择集。

        Args:
            ws_id: 工作区 ID
            changed_files: 变更文件路径列表（相对或绝对路径）

        Returns:
            符号列表（symbol_hash / qualified_name / file_path）
        """
        found: Dict[str, Dict[str, Any]] = {}
        for raw in changed_files:
            rel = self._normalize_path(raw or "")
            if not rel:
                continue
            rows = self.conn.execute(
                """
                SELECT s.symbol_hash, s.qualified_name, fi.rel_path
                FROM symbols s
                JOIN file_instances fi ON s.file_instance_id = fi.id
                WHERE fi.workspace_id = ? AND fi.rel_path = ?
                ORDER BY s.qualified_name, s.id
                """,
                (ws_id, rel),
            ).fetchall()
            if not rows:
                rows = self.conn.execute(
                    """
                    SELECT s.symbol_hash, s.qualified_name, fi.rel_path
                    FROM symbols s
                    JOIN file_instances fi ON s.file_instance_id = fi.id
                    WHERE fi.workspace_id = ? AND fi.rel_path LIKE ? ESCAPE '\\'
                    ORDER BY fi.rel_path, s.qualified_name, s.id
                    """,
                    (ws_id, "%" + self._escape_like(rel)),
                ).fetchall()
            for row in rows:
                key = f"{row['qualified_name']}\u0000{row['symbol_hash']}"
                if key in found:
                    continue
                found[key] = {
                    "symbol_hash": row["symbol_hash"] or "",
                    "qualified_name": row["qualified_name"] or "",
                    "file_path": self._normalize_path(row["rel_path"] or ""),
                }
        return sorted(
            found.values(),
            key=lambda s: (s["file_path"], s["qualified_name"], s["symbol_hash"]),
        )

    def _impacted_symbols_for_seeds(
        self, seeds: List[Dict[str, Any]], depth: int
    ) -> List[Dict[str, Any]]:
        """合并各触达符号的影响半径（blast_radius BFS），按限定名去重排序。

        Args:
            seeds: 触达符号列表（含 symbol_hash）
            depth: 影响半径遍历深度

        Returns:
            受影响符号列表（qualified_name / file_path / symbol_hash），
            按 qualified_name 稳定排序；blast_radius 失败时 fail-soft 跳过。
        """
        impacted: Dict[str, Dict[str, Any]] = {}
        for seed in seeds:
            symbol_hash = seed.get("symbol_hash") or ""
            if not symbol_hash:
                # 无 hash 时至少把种子自身纳入影响面
                qn = seed.get("qualified_name") or ""
                if qn:
                    impacted.setdefault(qn, {
                        "qualified_name": qn,
                        "symbol_hash": "",
                        "file_path": seed.get("file_path", ""),
                    })
                continue
            try:
                blast = self.blast_radius(symbol_hash, depth)
            except Exception:
                blast = None
            if not blast:
                qn = seed.get("qualified_name") or ""
                if qn:
                    impacted.setdefault(qn, dict(seed))
                continue
            for layer in blast.get("layers") or []:
                for sym in layer.get("symbols") or []:
                    qn = sym.get("qualified_name") or ""
                    if not qn:
                        continue
                    impacted.setdefault(qn, {
                        "qualified_name": qn,
                        "symbol_hash": sym.get("symbol_hash") or "",
                        "file_path": sym.get("file_path") or "",
                    })
        return sorted(impacted.values(), key=lambda s: s["qualified_name"])

    def _select_tests_for_symbols(
        self, qualified_names: List[str]
    ) -> List[Dict[str, Any]]:
        """对受影响符号复用 test_impact_selection，合并去重测试清单。

        Args:
            qualified_names: 受影响符号限定名（已排序）

        Returns:
            测试列表（qualified_name / name / file_path / module_path /
            start_line / covers），按测试限定名排序；covers 为触发该测试的
            受影响符号限定名（升序）。
        """
        merged: Dict[str, Dict[str, Any]] = {}
        if not hasattr(self, "test_impact_selection"):
            return []
        for qn in qualified_names:
            try:
                tests = self.test_impact_selection(qn) or []
            except Exception:
                continue
            for t in tests:
                test_qn = t.get("qualified_name") or ""
                if not test_qn:
                    continue
                entry = merged.get(test_qn)
                if entry is None:
                    entry = {
                        "qualified_name": test_qn,
                        "name": t.get("name") or "",
                        "file_path": t.get("file_path") or "",
                        "module_path": t.get("module_path") or "",
                        "start_line": t.get("start_line"),
                        "covers": [],
                    }
                    merged[test_qn] = entry
                if qn not in entry["covers"]:
                    entry["covers"].append(qn)
        for entry in merged.values():
            entry["covers"].sort()
        return [merged[qn] for qn in sorted(merged)]

    def _detect_guardrail_findings(
        self, changed_files: List[str]
    ) -> List[Dict[str, Any]]:
        """对变更文件做 guardrail 只读检测（check_before_edit，不落库）。

        Args:
            changed_files: 变更文件路径列表

        Returns:
            finding 列表（rule_id / severity / file_path / message），按
            (file_path, rule_id, message) 稳定排序；文件不可读时无产出。
        """
        findings: List[Dict[str, Any]] = []
        if not hasattr(self, "check_before_edit"):
            return findings
        for rel in changed_files:
            try:
                result = self.check_before_edit(rel, "")
            except Exception:
                continue
            for f in (result or {}).get("findings") or []:
                findings.append({
                    "rule_id": f.get("rule_id") or "",
                    "severity": f.get("severity") or "",
                    "file_path": rel,
                    "message": f.get("message") or "",
                })
        return sorted(
            findings,
            key=lambda f: (f["file_path"], f["rule_id"], f["message"]),
        )

    def _syntax_command(self, rel_path: str) -> Optional[str]:
        """按文件语言给出语法检查推荐命令（不支持的语言返回 None）。"""
        try:
            from ..config import detect_language_from_path
            lang = detect_language_from_path(rel_path)
        except Exception:
            return None
        template = self._SYNTAX_COMMANDS.get((lang or "").lower())
        if not template:
            return None
        return template.format(file=rel_path)

    def smart_ci_plan(
        self,
        changed_files: Optional[List[str]] = None,
        diff_text: str = "",
        depth: int = 2,
    ) -> Dict[str, Any]:
        """Smart CI：按变更影响面选择最小充分检查集

        流程（纯只读）：
        1. 解析变更面：diff_text 经 diff_to_symbol 映射符号；changed_files 经
           符号表解析（精确/后缀匹配）。两者可同时给出，按符号去重。
        2. 影响半径：对触达符号逐个 blast_radius 合并影响面（去重 + 稳定排序）。
        3. 测试选择：对影响面内每个符号复用 test_impact_selection 合并测试。
        4. 检查集：语法检查（按语言）+ guardrail 规则检测（check_before_edit，
           只读不落库）+ 测试命令；每项附推荐命令与预期证据形式。

        验收口径：
        - 同一变更反复调用输出完全一致（全链路稳定排序）；
        - 无受影响符号时返回空检查集（checks / test_selection 均为空）。

        Args:
            changed_files: 变更文件路径列表（可选）
            diff_text: git diff 文本（可选）
            depth: 影响半径遍历深度（默认 2）

        Returns:
            {
                "changed_files": [...],
                "seed_symbols": [...],       # diff/文件直接触达的符号
                "impacted_symbols": [...],   # 影响半径合并后的符号
                "test_selection": [...],     # 需要运行的测试
                "checks": [...],             # 推荐检查项（含 command/evidence）
                "selection": {...},          # 选择统计（可审计）
                "summary": "...",
            }
        """
        normalized_files: List[str] = []
        seeds: Dict[str, Dict[str, Any]] = {}

        if diff_text:
            for item in self.diff_to_symbol(diff_text) or []:
                rel = self._normalize_path(item.get("file_path") or "")
                if rel:
                    normalized_files.append(rel)
                key = item.get("symbol_hash") or item.get("qualified_name") or ""
                if key and key not in seeds:
                    seeds[key] = {
                        "symbol_hash": item.get("symbol_hash") or "",
                        "qualified_name": item.get("qualified_name") or "",
                        "file_path": rel,
                        "change_type": item.get("change_type") or "",
                    }

        for raw in changed_files or []:
            rel = self._normalize_path(raw or "")
            if rel:
                normalized_files.append(rel)

        normalized_files = sorted(set(normalized_files))

        if normalized_files:
            ws_id = self._get_active_workspace_id()
            for sym in self._symbols_for_changed_files(ws_id, normalized_files):
                key = sym.get("symbol_hash") or sym.get("qualified_name") or ""
                if key and key not in seeds:
                    seeds[key] = dict(sym)

        seed_list = sorted(
            seeds.values(),
            key=lambda s: (
                s.get("file_path", ""),
                s.get("qualified_name", ""),
                s.get("symbol_hash", ""),
            ),
        )
        impacted = self._impacted_symbols_for_seeds(seed_list, depth)

        result: Dict[str, Any] = {
            "changed_files": normalized_files,
            "seed_symbols": seed_list,
            "impacted_symbols": impacted,
            "test_selection": [],
            "checks": [],
            "selection": {
                "strategy": "impact-radius+guardrail",
                "depth": depth,
                "changed_file_count": len(normalized_files),
                "seed_symbol_count": len(seed_list),
                "impacted_symbol_count": len(impacted),
                "test_count": 0,
                "rule_ids": [],
            },
            "summary": "",
        }

        # 无受影响符号 → 空检查集（验收口径）
        if not impacted:
            result["summary"] = "无受影响符号，检查集为空"
            return result

        tests = self._select_tests_for_symbols(
            [s["qualified_name"] for s in impacted if s.get("qualified_name")]
        )
        result["test_selection"] = tests

        checks: List[Dict[str, Any]] = []

        # 1) 测试检查项：按测试文件聚合（同一文件只推荐一次，附带覆盖的测试名）
        tests_by_file: Dict[str, List[str]] = {}
        for t in tests:
            fp = t.get("file_path") or ""
            if not fp:
                continue
            tests_by_file.setdefault(fp, []).append(t["qualified_name"])
        for fp in sorted(tests_by_file):
            names = sorted(set(tests_by_file[fp]))
            checks.append({
                "check_id": f"test:{fp}",
                "kind": "test",
                "target": fp,
                "command": f"pytest -q {fp}",
                "evidence": "pytest 汇总行（passed/failed 计数，0 failed）",
                "related_symbols": names,
            })

        # 2) 语法检查项：按变更文件语言
        for rel in normalized_files:
            cmd = self._syntax_command(rel)
            if not cmd:
                continue
            checks.append({
                "check_id": f"syntax:{rel}",
                "kind": "syntax",
                "target": rel,
                "command": cmd,
                "evidence": "命令退出码 0（无语法错误）",
            })

        # 3) guardrail 规则检查项：按 (rule_id, file) 聚合命中
        guard_findings = self._detect_guardrail_findings(normalized_files)
        rule_ids = sorted({f["rule_id"] for f in guard_findings if f["rule_id"]})
        seen_rule_file = set()
        for f in guard_findings:
            key = (f["rule_id"], f["file_path"])
            if key in seen_rule_file:
                continue
            seen_rule_file.add(key)
            checks.append({
                "check_id": f"guardrail:{f['rule_id']}:{f['file_path']}",
                "kind": "guardrail",
                "target": f["file_path"],
                "rule_id": f["rule_id"],
                "severity": f["severity"],
                "command": f"cw guardrail scan --file {f['file_path']}",
                "evidence": "cw guardrail scan 输出该文件无 block 级 finding",
            })

        checks.sort(key=lambda c: c["check_id"])
        result["checks"] = checks
        result["selection"]["test_count"] = len(tests)
        result["selection"]["rule_ids"] = rule_ids
        result["summary"] = (
            f"受影响符号 {len(impacted)} 个，选中测试 {len(tests)} 个，"
            f"检查项 {len(checks)} 项"
        )
        return result

    # ------------------------------------------------------------------
    # 风险评分 / 边置信度（评审就绪报告增强）
    # ------------------------------------------------------------------

    @staticmethod
    def _is_test_symbol(sym: Dict[str, Any]) -> bool:
        """影响树内符号是否属于测试（名字 test 前缀或路径段 test/tests）。"""
        name = (sym.get("name") or "").lower()
        path = (sym.get("file_path") or "").replace("\\", "/").lower()
        if name.startswith("test"):
            return True
        return any(seg.startswith("test") for seg in path.split("/") if seg)

    def _count_recent_commits(self, rel_path: str) -> int:
        """源文件近期提交次数（变更热度信号；非 git 仓库/失败回退 0）。"""
        root = getattr(self, "workspace_root", "") or ""
        if not root or not rel_path:
            return 0
        try:
            proc = subprocess.run(
                ["git", "-C", root, "log", "--oneline", "--", rel_path],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return 0
        if proc.returncode != 0:
            return 0
        return len([line for line in proc.stdout.splitlines() if line.strip()])

    def _edge_confidence_weights(
        self, ws_id: int, source_hash: str, others: List[Dict[str, Any]]
    ) -> Dict[str, float]:
        """逐调用方对影响树的边置信度权重（四档，越小越不可靠）。

        方向：受影响符号 X 被拖入影响树，靠的是 X 自己的调用边指向
        "已变更/已受影响"的符号（源符号或更浅层被影响符号）。故按
        caller = X、callee ∈ 锚点集合 的边评估：

        - 已解析边（callee_id>0）：同文件 → EXTRACTED(1.0)，跨文件 → INFERRED(0.8)；
        - raw 边（callee_id=0）且 callee_qualified 精确命中锚点 → AMBIGUOUS(0.4)；
        - raw 边只有裸名命中锚点 → UNRESOLVED(0.2)（过短/过通用名字不降级，防噪声）。

        同一 caller 取所有相关边的最低权重（保守口径）。raw 边可能带来
        影响树之外的候选调用方（未解析边不会被 blast_radius 遍历到），
        它们也进入结果，作为"低置信度候选"暴露给审查方。

        Returns:
            (weights, call_lines)：
            - weights: {caller_symbol_hash: 最低置信度权重}
            - call_lines: {caller_symbol_hash: 该最低权重边所在调用行号}
              （同权重多边时取最小非零行号，保证 ranked 排序稳定可复现）
        """
        anchor_qns = {
            (s.get("qualified_name") or ""): (s.get("name") or "")
            for s in others
            if s.get("qualified_name")
        }
        source_qn = self._symbol_qualified_name(ws_id, source_hash)
        if source_qn:
            anchor_qns.setdefault(source_qn, source_qn.rsplit(".", 1)[-1])
        if not anchor_qns:
            return {}, {}
        weights: Dict[str, float] = {}
        call_lines: Dict[str, int] = {}

        def _record(caller_hash: str, weight: float, call_line: Any) -> None:
            """记录最低权重边；同权重取更小非零行号（未知行号不覆盖已知）。"""
            line = int(call_line or 0)
            prev = weights.get(caller_hash)
            if prev is None or weight < prev:
                weights[caller_hash] = weight
                call_lines[caller_hash] = line
            elif weight == prev:
                prev_line = call_lines.get(caller_hash, 0)
                if line and (prev_line == 0 or line < prev_line):
                    call_lines[caller_hash] = line

        anchor_list = list(anchor_qns.keys())
        placeholders = ",".join("?" * len(anchor_list))
        impacted_hashes = [
            s.get("symbol_hash") for s in others if s.get("symbol_hash")
        ]

        # 已解析边：caller 在影响树内、callee 命中锚点
        if impacted_hashes:
            caller_ph = ",".join("?" * len(impacted_hashes))
            cur = self.conn.execute(
                f"""
                SELECT s1.symbol_hash AS caller_hash,
                       (fi1.id = fi2.id) AS same_file,
                       c.call_line AS call_line
                FROM calls c
                JOIN symbols s1 ON s1.id = c.caller_id
                JOIN file_instances fi1 ON fi1.id = s1.file_instance_id
                JOIN symbols s2 ON s2.id = c.callee_id
                JOIN file_instances fi2 ON fi2.id = s2.file_instance_id
                WHERE fi1.workspace_id = ?
                  AND c.callee_id > 0
                  AND s1.symbol_hash IN ({caller_ph})
                  AND s2.qualified_name IN ({placeholders})
                """,  # nosec B608 - placeholders 仅为 ? 占位符，值全部参数化
                [ws_id, *impacted_hashes, *anchor_list],
            )
            for row in cur:
                caller_hash = row["caller_hash"] or ""
                if not caller_hash:
                    continue
                weight = edge_confidence_weight(
                    classify_edge(1, bool(row["same_file"]), "")
                )
                _record(caller_hash, weight, row["call_line"])

        # 未解析边：qualified 命中锚点 → AMBIGUOUS；裸名命中 → UNRESOLVED
        name_to_anchors: Dict[str, List[str]] = {}
        for qn, name in anchor_qns.items():
            if name and not is_noisy_raw_name(name):
                name_to_anchors.setdefault(name, []).append(qn)
        raw_names = list(name_to_anchors.keys())
        raw_ph = ",".join("?" * len(raw_names)) if raw_names else "NULL"
        cur = self.conn.execute(
            f"""
            SELECT s1.symbol_hash AS caller_hash,
                   c.callee_qualified, c.callee_name, c.call_line AS call_line
            FROM calls c
            JOIN symbols s1 ON s1.id = c.caller_id
            JOIN file_instances fi1 ON fi1.id = s1.file_instance_id
            WHERE fi1.workspace_id = ?
              AND c.callee_id = 0
              AND (c.callee_qualified IN ({placeholders})
                   OR c.callee_name IN ({raw_ph}))
            """,  # nosec B608 - placeholders 仅为 ? 占位符，值全部参数化
            [ws_id, *anchor_list, *raw_names],
        )
        ambiguous = edge_confidence_weight("AMBIGUOUS")
        unresolved = edge_confidence_weight("UNRESOLVED")
        for row in cur:
            caller_hash = row["caller_hash"] or ""
            if not caller_hash:
                continue
            cq = (row["callee_qualified"] or "").strip()
            cname = (row["callee_name"] or "").strip()
            if cq and cq in anchor_qns:
                weight = ambiguous
            elif cname and cname in name_to_anchors:
                weight = unresolved
            else:
                continue
            _record(caller_hash, weight, row["call_line"])

        for sym in others:
            sh = sym.get("symbol_hash") or ""
            if sh:
                weights.setdefault(sh, edge_confidence_weight("EXTRACTED"))
                call_lines.setdefault(sh, 0)
        return weights, call_lines

    def _symbol_qualified_name(self, ws_id: int, symbol_hash: str) -> str:
        """按 hash 取符号限定名（源符号锚点用）；不存在返回空串。"""
        cur = self.conn.execute(
            """
            SELECT s.qualified_name
            FROM symbols s
            JOIN file_instances fi ON fi.id = s.file_instance_id
            WHERE fi.workspace_id = ? AND s.symbol_hash = ?
            LIMIT 1
            """,
            (ws_id, symbol_hash),
        )
        row = cur.fetchone()
        return (row["qualified_name"] or "") if row else ""

    def _build_risk_profile(
        self,
        symbol_hash: str,
        blast: Dict[str, Any],
        must_test: List[Dict[str, Any]],
        min_confidence: Optional[float] = None,
    ) -> Dict[str, Any]:
        """六信号风险评分 + 边置信度四档汇总（纯派生，不写库）。"""
        layers = blast.get("layers") or []
        impacted = [sym for layer in layers for sym in layer.get("symbols", [])]
        others = [sym for sym in impacted if sym.get("symbol_hash") != symbol_hash]
        if not others:
            return {
                "risk_score": 0.0,
                "risk_signals": {},
                "edge_confidence": {
                    "min_confidence": 0.0,
                    "classification": "UNAFFECTED",
                    "histogram": {"WILL_BREAK": 0, "LIKELY_AFFECTED": 0, "UNAFFECTED": 0},
                    "per_symbol": [],
                    "ranked": [],
                },
                "weighted_impacted": 0.0,
            }

        ws_id = self._get_active_workspace_id()
        weights, call_lines = self._edge_confidence_weights(ws_id, symbol_hash, others)
        # 权重按 symbol_hash 记账（raw 候选可能不在影响树内），输出前统一换名并补元数据
        hash_to_info: Dict[str, Dict[str, str]] = {
            (s.get("symbol_hash") or ""): {
                "qualified_name": s.get("qualified_name") or "",
                "file_path": s.get("file_path") or "",
                "kind": s.get("kind") or "",
            }
            for s in others
            if s.get("symbol_hash")
        }
        missing = [h for h in weights if h not in hash_to_info]
        if missing:
            ph = ",".join("?" * len(missing))
            cur = self.conn.execute(
                f"""
                SELECT s.symbol_hash, s.qualified_name, s.kind,
                       fi.rel_path AS file_path
                FROM symbols s
                JOIN file_instances fi ON fi.id = s.file_instance_id
                WHERE fi.workspace_id = ? AND s.symbol_hash IN ({ph})
                """,  # nosec B608 - placeholders 仅为 ? 占位符，值全部参数化
                [ws_id, *missing],
            )
            for row in cur:
                hash_to_info[row["symbol_hash"]] = {
                    "qualified_name": row["qualified_name"] or "",
                    "file_path": row["file_path"] or "",
                    "kind": row["kind"] or "",
                }
        per_symbol: Dict[str, float] = {}
        candidate_meta: Dict[str, Dict[str, Any]] = {}
        for h, w in weights.items():
            info = hash_to_info.get(h) or {}
            qn = info.get("qualified_name") or h
            per_symbol[qn] = w
            candidate_meta[qn] = {
                "file_path": info.get("file_path") or "",
                "kind": info.get("kind") or "",
                "call_line": int(call_lines.get(h, 0)),
            }

        source_module = ""
        source_file = ""
        if layers and layers[0].get("symbols"):
            src = layers[0]["symbols"][0]
            source_module = src.get("module_path") or ""
            source_file = src.get("file_path") or ""

        signals = collect_risk_signals(
            max_depth=max(
                (layer.get("depth", 0) for layer in layers if layer.get("symbols")),
                default=0,
            ),
            impacted_modules=[sym.get("module_path") or "" for sym in others],
            source_module=source_module,
            must_test_count=len(must_test),
            test_symbol_count=sum(1 for sym in others if self._is_test_symbol(sym)),
            security_named_count=sum(
                1
                for sym in others
                if is_security_named(
                    sym.get("name") or "", sym.get("qualified_name") or ""
                )
            ),
            direct_caller_count=len(layers[1].get("symbols", [])) if len(layers) > 1 else 0,
            churn_commits=self._count_recent_commits(source_file),
        )
        return {
            "risk_score": compute_risk_score(signals),
            "risk_signals": signals,
            "edge_confidence": summarize_edges(
                per_symbol, candidate_meta, min_confidence
            ),
            "weighted_impacted": round(sum(per_symbol.values()), 2),
        }

    # ------------------------------------------------------------------
    # 漏洞爆炸半径（Vulnerability Blast Radius）
    # ------------------------------------------------------------------

    def get_vulnerability_blast_radius(
        self, finding_id: int = 0, severity_filter: str = "", depth: int = 3
    ) -> Dict[str, Any]:
        """计算漏洞的爆炸半径（Semgrep 发现 × 调用链反向传播 = 安全影响面）

        全行业空白特性：传统 Semgrep 只做独立扫描，code-review-graph 只做 PR 爆炸半径，
        无人做"漏洞传播影响面"分析。此方法将 Semgrep findings 与调用图结合，
        回答"这个漏洞能影响多少下游调用方"这个安全关键问题。

        实现思路：
        1. 从 semgrep_findings 表查出漏洞所在符号（通过 symbol_id / content_hash / 行号匹配）
        2. 对每个漏洞符号调用 blast_radius 反向遍历调用图，获取所有可能被影响的调用方
        3. 按漏洞严重度（ERROR/WARN/INFO）和传播深度评估风险等级
        4. 汇总输出：漏洞列表 + 每个漏洞的影响树 + 总体风险评级

        Args:
            finding_id: 指定 Semgrep finding ID（为 0 则扫描所有匹配 severity_filter 的 findings）
            severity_filter: 严重度过滤（ERROR/WARN/INFO，为空则不过滤）
            depth: 调用图反向遍历深度（默认 3 层）

        Returns:
            {
                "total_findings": N,           # 扫描的漏洞总数
                "total_impacted_symbols": M,   # 受影响符号总数（去重）
                "risk_level": "critical/high/medium/low",
                "findings": [                  # 每个漏洞的影响详情
                    {
                        "finding_id": 1,
                        "rule_id": "...",
                        "rule_name": "...",
                        "severity": "ERROR",
                        "file_path": "...",
                        "symbol_qualified": "...",
                        "symbol_hash": "...",
                        "blast_radius": {...},  # 复用 blast_radius 输出格式
                        "impacted_count": N,
                    },
                    ...
                ],
                "impacted_symbols_summary": {  # 受影响符号汇总
                    "by_layer": {"code": N, "db": M, "api": K, "config": L},
                    "high_risk_callers": [...],  # 高风险调用方（被多个漏洞影响）
                }
            }
        """
        _ws_id = self._get_active_workspace_id()

        # 1. 查询 Semgrep findings
        if finding_id > 0:
            cur = self.conn.execute(
                """
                SELECT sf.id, sf.rule_id, sf.rule_name, sf.severity, sf.message,
                       sf.start_line, sf.end_line, sf.symbol_id, sf.symbol_qualified,
                       sf.content_hash, fi.rel_path
                FROM semgrep_findings sf
                LEFT JOIN file_instances fi ON sf.file_instance_id = fi.id
                WHERE sf.id = ?
                """,
                (finding_id,),
            )
        else:
            # 扫描所有匹配 severity 的 findings
            if severity_filter:
                cur = self.conn.execute(
                    """
                    SELECT sf.id, sf.rule_id, sf.rule_name, sf.severity, sf.message,
                           sf.start_line, sf.end_line, sf.symbol_id, sf.symbol_qualified,
                           sf.content_hash, fi.rel_path
                    FROM semgrep_findings sf
                    LEFT JOIN file_instances fi ON sf.file_instance_id = fi.id
                    WHERE sf.severity = ?
                    ORDER BY sf.severity DESC, sf.id ASC
                    """,
                    (severity_filter,),
                )
            else:
                cur = self.conn.execute(
                    """
                    SELECT sf.id, sf.rule_id, sf.rule_name, sf.severity, sf.message,
                           sf.start_line, sf.end_line, sf.symbol_id, sf.symbol_qualified,
                           sf.content_hash, fi.rel_path
                    FROM semgrep_findings sf
                    LEFT JOIN file_instances fi ON sf.file_instance_id = fi.id
                    ORDER BY sf.severity DESC, sf.id ASC
                    """
                )

        findings_rows = [dict(r) for r in cur.fetchall()]
        if not findings_rows:
            return {
                "total_findings": 0,
                "total_impacted_symbols": 0,
                "risk_level": "none",
                "findings": [],
                "impacted_symbols_summary": {
                    "by_layer": {"code": 0, "db": 0, "api": 0, "config": 0},
                    "high_risk_callers": [],
                },
            }

        # 2. 对每个漏洞符号调用 blast_radius 反向遍历
        findings_results: List[Dict[str, Any]] = []
        all_impacted_hashes: set = set()
        impacted_caller_count: Dict[str, int] = {}  # qualified_name -> 被多少漏洞影响

        for row in findings_rows:
            symbol_hash = row["content_hash"] or ""
            symbol_qualified = row["symbol_qualified"] or ""

            # 如果 finding 没有 symbol_hash，尝试用 symbol_id 反查
            if not symbol_hash and row["symbol_id"]:
                cur2 = self.conn.execute(
                    "SELECT symbol_hash FROM symbols WHERE id = ?",
                    (row["symbol_id"],),
                )
                sym_row = cur2.fetchone()
                if sym_row:
                    symbol_hash = sym_row["symbol_hash"]

            # 调用 blast_radius 计算影响树（复用现有实现）
            if symbol_hash:
                br = self.blast_radius(symbol_hash, depth=depth)
                impacted_count = br.get("total_impacted", 0)
                _by_layer = br.get("by_layer", {})
            else:
                # 无符号关联的漏洞，影响范围无法计算
                br = {"layers": [], "total_impacted": 0, "by_layer": {"code": 0, "db": 0, "api": 0, "config": 0}}
                impacted_count = 0

            # 收集受影响符号（用于全局去重统计）
            for layer in br.get("layers", []):
                for sym in layer.get("symbols", []):
                    h = sym.get("symbol_hash", "")
                    qn = sym.get("qualified_name", "")
                    if h:
                        all_impacted_hashes.add(h)
                    if qn:
                        impacted_caller_count[qn] = impacted_caller_count.get(qn, 0) + 1

            findings_results.append({
                "finding_id": row["id"],
                "rule_id": row["rule_id"],
                "rule_name": row["rule_name"],
                "severity": row["severity"],
                "message": row["message"],
                "file_path": row["rel_path"] or "",
                "start_line": row["start_line"],
                "symbol_qualified": symbol_qualified,
                "symbol_hash": symbol_hash,
                "blast_radius": br,
                "impacted_count": impacted_count,
            })

        # 3. 评估总体风险等级
        # 规则：有 ERROR 级漏洞且影响 >10 个符号 → critical；
        #       ERROR 级且影响 >3 → high；WARN 级 → medium；其余 → low
        has_error = any(f["severity"] in ("ERROR", "error", "CRITICAL", "critical") for f in findings_results)
        total_impacted = len(all_impacted_hashes)

        if has_error and total_impacted > 10:
            risk_level = "critical"
        elif has_error and total_impacted > 3:
            risk_level = "high"
        elif any(f["severity"] in ("WARN", "warn", "WARNING") for f in findings_results):
            risk_level = "medium"
        else:
            risk_level = "low"

        # 4. 识别高风险调用方（被多个漏洞影响的符号）
        high_risk_callers = [
            {"qualified_name": qn, "affected_by_count": cnt}
            for qn, cnt in sorted(impacted_caller_count.items(), key=lambda x: -x[1])
            if cnt >= 2
        ][:20]  # Top 20

        # 汇总 by_layer
        summary_by_layer = {"code": 0, "db": 0, "api": 0, "config": 0}
        for f in findings_results:
            bl = f["blast_radius"].get("by_layer", {})
            for k in summary_by_layer:
                summary_by_layer[k] += bl.get(k, 0)

        return {
            "total_findings": len(findings_results),
            "total_impacted_symbols": total_impacted,
            "risk_level": risk_level,
            "findings": findings_results,
            "impacted_symbols_summary": {
                "by_layer": summary_by_layer,
                "high_risk_callers": high_risk_callers,
            },
        }

    # ------------------------------------------------------------------
    # 克隆感知影响分析（Clone-Aware Impact）
    # ------------------------------------------------------------------

    def get_clone_aware_impact(
        self, qualified_name: str, depth: int = 3
    ) -> Dict[str, Any]:
        """克隆感知的变更影响分析（H11）

        在 blast_radius 基础上联动 clone_pairs 表：当源符号有克隆代码时，
        克隆代码的变更也会影响相同的调用方，因此影响半径应包含克隆符号的影响。

        实现思路：
        1. 查找源符号的 symbol_hash 和 symbol_id
        2. 调用 blast_radius 获取原符号的影响半径
        3. 调用 list_clones(symbol_id) 获取克隆对
        4. 对每个克隆符号，调用 blast_radius 获取其影响半径
        5. 合并返回，标注哪些影响来自克隆

        Args:
            qualified_name: 源符号限定名
            depth: BFS 遍历深度（默认 3）

        Returns:
            {
                "source_symbol": 源符号信息,
                "original_blast_radius": 原符号影响半径,
                "clones": [克隆符号信息列表],
                "clone_blast_radii": [每个克隆的影响半径],
                "total_impacted_with_clones": 合并后影响符号总数,
            }
        """
        ws_id = self._get_active_workspace_id()

        # 1. 查找源符号
        cur = self.conn.execute(
            """
            SELECT s.id, s.symbol_hash, s.qualified_name, s.name, s.module_path,
                   s.kind, fi.rel_path
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND s.qualified_name = ?
            LIMIT 1
            """,
            (ws_id, qualified_name),
        )
        row = cur.fetchone()
        if not row:
            return {
                "source_symbol": "",
                "error": f"符号不存在: {qualified_name}",
            }

        source = dict(row)
        symbol_id = source["id"]
        symbol_hash = source["symbol_hash"]

        # 2. 原符号影响半径
        original_radius = self.blast_radius(symbol_hash, depth=depth)

        # 3. 查找克隆对
        clone_impacts: List[Dict[str, Any]] = []
        clone_infos: List[Dict[str, Any]] = []
        if hasattr(self, "list_clones"):
            clones = self.list_clones(symbol_id=symbol_id, limit=50)
            for c in clones:
                # 确定克隆符号是 symbol_a 还是 symbol_b
                if c.get("symbol_a_qualified") == qualified_name:
                    clone_qn = c.get("symbol_b_qualified", "")
                    clone_file = c.get("file_b", "")
                    clone_line = c.get("symbol_b_line", 0)
                else:
                    clone_qn = c.get("symbol_a_qualified", "")
                    clone_file = c.get("file_a", "")
                    clone_line = c.get("symbol_a_line", 0)

                clone_infos.append({
                    "qualified_name": clone_qn,
                    "file": clone_file,
                    "line": clone_line,
                    "similarity": c.get("similarity", 0),
                    "clone_type": c.get("clone_type", 0),
                })

                # 查找克隆符号的 hash，计算其 blast_radius
                if clone_qn:
                    cur2 = self.conn.execute(
                        """
                        SELECT s.symbol_hash
                        FROM symbols s
                        JOIN file_instances fi ON s.file_instance_id = fi.id
                        WHERE fi.workspace_id = ? AND s.qualified_name = ?
                        LIMIT 1
                        """,
                        (ws_id, clone_qn),
                    )
                    clone_row = cur2.fetchone()
                    if clone_row:
                        clone_radius = self.blast_radius(clone_row["symbol_hash"], depth=depth)
                        clone_impacts.append({
                            "clone_symbol": clone_qn,
                            "blast_radius": clone_radius,
                        })

        # 4. 合并影响符号总数（去重）
        all_impacted = set()
        for layer in original_radius.get("layers", []):
            for sym in layer.get("symbols", []):
                if isinstance(sym, dict):
                    all_impacted.add(sym.get("qualified_name", ""))
                elif isinstance(sym, str):
                    all_impacted.add(sym)
        for ci in clone_impacts:
            for layer in ci.get("blast_radius", {}).get("layers", []):
                for sym in layer.get("symbols", []):
                    if isinstance(sym, dict):
                        all_impacted.add(sym.get("qualified_name", ""))
                    elif isinstance(sym, str):
                        all_impacted.add(sym)
        all_impacted.discard("")

        return {
            "source_symbol": {
                "qualified_name": source["qualified_name"],
                "name": source["name"],
                "kind": source["kind"],
                "file": source["rel_path"],
                "symbol_hash": symbol_hash,
            },
            "original_blast_radius": original_radius,
            "clones": clone_infos,
            "clone_blast_radii": clone_impacts,
            "total_impacted_with_clones": len(all_impacted),
        }
