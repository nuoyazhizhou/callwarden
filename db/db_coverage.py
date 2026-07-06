"""
db_coverage.py
==============

覆盖率智能模块 Mixin 类。

提供从 LCOV / Cobertura 报告导入行级覆盖率数据、函数级覆盖率查询、
未覆盖函数发现、测试影响选择等功能。基于 coverage_data 表存储的真实
覆盖率数据（区别于 analyzers.coverage 中基于启发式的注释/测试覆盖率统计）。

继承自 analyzers.coverage.CoverageMixin，保留原有注释覆盖率、调用热力图等方法。
"""

from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from typing import Any, Dict, List, Optional, Set

from ..analyzers.coverage import CoverageMixin as _AnalyzerCoverageMixin


class CoverageMixin(_AnalyzerCoverageMixin):
    """覆盖率智能 Mixin

    通过 self.conn 访问数据库连接，提供覆盖率报告导入与智能分析功能。
    继承 analyzers.coverage.CoverageMixin，同时保留注释覆盖率等启发式统计方法。
    """

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize_path(p: str) -> str:
        """将路径统一为正斜杠形式并去除首尾空白"""
        return p.replace("\\", "/").strip()

    def _match_file_instance(self, raw_path: str) -> Optional[int]:
        """将覆盖率报告中的文件路径匹配到 file_instance_id

        匹配策略（按优先级）：
        1. rel_path 精确匹配
        2. rel_path 后缀匹配（报告路径以 rel_path 结尾，或反之）
        3. abs_path 后缀匹配

        Args:
            raw_path: 报告中的文件路径（可能为绝对或相对路径）

        Returns:
            匹配到的 file_instance_id，未匹配返回 None
        """
        ws_id = self._get_active_workspace_id()
        norm = self._normalize_path(raw_path)
        if not norm:
            return None

        # 策略1：rel_path 精确匹配
        cur = self.conn.execute(
            "SELECT id FROM file_instances WHERE workspace_id = ? AND rel_path = ? LIMIT 1",
            (ws_id, norm),
        )
        row = cur.fetchone()
        if row:
            return row["id"]

        # 策略2：rel_path 后缀匹配
        cur = self.conn.execute(
            "SELECT id, rel_path FROM file_instances WHERE workspace_id = ? AND status != 'archived'",
            (ws_id,),
        )
        candidates = cur.fetchall()
        for cand in candidates:
            rel = cand["rel_path"]
            if rel and (rel.endswith(norm) or norm.endswith(rel)):
                return cand["id"]

        # 策略3：abs_path 后缀匹配
        for cand in candidates:
            abs_p = cand["abs_path"] if "abs_path" in cand.keys() else ""
            if abs_p:
                abs_norm = self._normalize_path(abs_p)
                if abs_norm.endswith(norm) or norm.endswith(abs_norm):
                    return cand["id"]

        return None

    def _load_symbols_for_file(self, file_instance_id: int) -> List[Dict]:
        """加载文件中所有符号，用于行号范围匹配

        Returns:
            符号列表，每个元素包含 id / start_line / end_line
        """
        cur = self.conn.execute(
            """
            SELECT id, start_line, end_line FROM symbols
            WHERE file_instance_id = ?
              AND file_instance_id NOT IN (
                  SELECT id FROM file_instances WHERE status = 'archived'
              )
            ORDER BY start_line
            """,
            (file_instance_id,),
        )
        return [dict(row) for row in cur]

    @staticmethod
    def _find_symbol_for_line(symbols: List[Dict], line: int) -> Optional[int]:
        """在符号列表中找到包含指定行号的符号

        Args:
            symbols: 已加载的符号列表
            line: 行号

        Returns:
            符号 ID，未找到返回 None
        """
        for sym in symbols:
            if sym["start_line"] <= line <= sym["end_line"]:
                return sym["id"]
        return None

    def _clear_coverage_by_source(self, report_source: str):
        """清除指定来源的旧覆盖率数据，避免重复导入产生冗余"""
        self.conn.execute(
            "DELETE FROM coverage_data WHERE report_source = ?",
            (report_source,),
        )

    def _batch_insert_coverage(
        self,
        file_instance_id: int,
        line_hits: List[tuple],
        report_source: str,
    ) -> tuple:
        """批量插入行覆盖率数据并关联符号

        Args:
            file_instance_id: 文件实例 ID
            line_hits: [(line_number, hit_count), ...] 列表
            report_source: 来源标识（lcov / cobertura）

        Returns:
            (lines_imported, symbols_matched) 元组
        """
        if not line_hits:
            return (0, 0)

        symbols = self._load_symbols_for_file(file_instance_id)
        now = time.time()
        rows = []
        symbols_matched = 0

        for line_no, hit_count in line_hits:
            symbol_id = self._find_symbol_for_line(symbols, line_no)
            if symbol_id:
                symbols_matched += 1
            rows.append((
                file_instance_id,
                symbol_id,
                line_no,
                line_no,
                hit_count,
                report_source,
                now,
            ))

        self.conn.executemany(
            """
            INSERT INTO coverage_data
                (file_instance_id, symbol_id, line_start, line_end, hit_count, report_source, imported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        return (len(rows), symbols_matched)

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def import_lcov(self, file_path: str) -> Dict[str, Any]:
        """解析 LCOV 格式覆盖率报告并导入数据库

        LCOV 格式（逐行文本）：
            SF:src/main.rs        — 源文件路径
            DA:10,1               — 行号,命中次数
            DA:11,0               — 行号,命中次数（0 表示未覆盖）
            LF:3                  — 总行数
            LH:2                  — 命中行数
            end_of_record         — 记录结束

        路径匹配通过 rel_path 进行（统一正斜杠）。
        行覆盖率关联到 symbols（通过 start_line <= line <= end_line）。

        Args:
            file_path: LCOV 报告文件路径

        Returns:
            导入统计字典
        """
        report_source = "lcov"
        self._clear_coverage_by_source(report_source)

        files_total = 0
        files_matched = 0
        lines_imported = 0
        symbols_matched = 0

        # 当前记录的文件路径与行覆盖率收集器
        current_sf: str = ""
        current_lines: List[tuple] = []

        with open(file_path, "r", encoding="utf-8", errors="replace") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line:
                    continue

                if line.startswith("SF:"):
                    current_sf = line[3:]
                    current_lines = []
                elif line.startswith("DA:"):
                    parts = line[3:].split(",")
                    if len(parts) >= 2:
                        try:
                            line_no = int(parts[0])
                            hit_count = int(parts[1])
                            current_lines.append((line_no, hit_count))
                        except ValueError:
                            continue
                elif line.startswith("end_of_record"):
                    files_total += 1
                    file_instance_id = self._match_file_instance(current_sf)
                    if file_instance_id:
                        files_matched += 1
                        li, sm = self._batch_insert_coverage(
                            file_instance_id, current_lines, report_source
                        )
                        lines_imported += li
                        symbols_matched += sm
                    current_sf = ""
                    current_lines = []

        # 处理文件末尾没有 end_of_record 的最后一条记录
        if current_sf and current_lines:
            files_total += 1
            file_instance_id = self._match_file_instance(current_sf)
            if file_instance_id:
                files_matched += 1
                li, sm = self._batch_insert_coverage(
                    file_instance_id, current_lines, report_source
                )
                lines_imported += li
                symbols_matched += sm

        self.conn.commit()

        return {
            "files_total": files_total,
            "files_matched": files_matched,
            "lines_imported": lines_imported,
            "symbols_matched": symbols_matched,
        }

    def import_cobertura(self, file_path: str) -> Dict[str, Any]:
        """解析 Cobertura XML 格式覆盖率报告并导入数据库

        Cobertura XML 格式：
            <class filename="src/main.rs" line-rate="0.5">
                <lines>
                    <line number="10" hits="1"/>
                    <line number="11" hits="0"/>
                </lines>
            </class>

        同样关联到 file_instances 和 symbols。

        Args:
            file_path: Cobertura XML 报告文件路径

        Returns:
            导入统计字典
        """
        report_source = "cobertura"
        self._clear_coverage_by_source(report_source)

        files_total = 0
        files_matched = 0
        lines_imported = 0
        symbols_matched = 0

        tree = ET.parse(file_path)
        root = tree.getroot()

        # 遍历所有 <class> 元素（可能嵌套在 packages/classes 下）
        for class_elem in root.iter("class"):
            filename = class_elem.get("filename", "")
            if not filename:
                continue

            files_total += 1
            file_instance_id = self._match_file_instance(filename)
            if not file_instance_id:
                continue

            files_matched += 1
            current_lines: List[tuple] = []

            # 查找 <lines> 下的所有 <line> 元素
            lines_container = class_elem.find("lines")
            if lines_container is not None:
                for line_elem in lines_container.iter("line"):
                    number = line_elem.get("number")
                    hits = line_elem.get("hits")
                    if number is None or hits is None:
                        continue
                    try:
                        line_no = int(number)
                        hit_count = int(hits)
                        current_lines.append((line_no, hit_count))
                    except ValueError:
                        continue

            if current_lines:
                li, sm = self._batch_insert_coverage(
                    file_instance_id, current_lines, report_source
                )
                lines_imported += li
                symbols_matched += sm

        self.conn.commit()

        return {
            "files_total": files_total,
            "files_matched": files_matched,
            "lines_imported": lines_imported,
            "symbols_matched": symbols_matched,
        }

    def get_coverage_for_symbol(self, qualified_name: str) -> Optional[Dict[str, Any]]:
        """获取函数级覆盖率

        查找符号的行范围，查询 coverage_data 中该范围的行命中情况。

        Args:
            qualified_name: 函数限定名

        Returns:
            覆盖率信息字典，包含：
            - qualified_name: 函数限定名
            - file_path: 文件路径
            - start_line / end_line: 行范围
            - total_lines: 符号总行数
            - tracked_lines: 有覆盖率数据的行数
            - covered_lines: 命中行数
            - coverage_pct: 覆盖率百分比
            - uncovered_lines: 未覆盖行号列表
            未找到符号返回 None
        """
        ws_id = self._get_active_workspace_id()

        # 查找符号
        cur = self.conn.execute(
            """
            SELECT s.id, s.start_line, s.end_line, fi.rel_path
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ? AND s.qualified_name = ?
            LIMIT 1
            """,
            (ws_id, qualified_name),
        )
        row = cur.fetchone()
        if not row:
            return None

        symbol_id = row["id"]
        start_line = row["start_line"]
        end_line = row["end_line"]
        total_lines = end_line - start_line + 1

        # 查询该符号行范围内的覆盖率数据
        cur = self.conn.execute(
            """
            SELECT line_start, hit_count FROM coverage_data
            WHERE symbol_id = ? AND line_start >= ? AND line_end <= ?
            ORDER BY line_start
            """,
            (symbol_id, start_line, end_line),
        )

        tracked_lines = 0
        covered_lines = 0
        uncovered_lines: List[int] = []

        for cd_row in cur:
            tracked_lines += 1
            if cd_row["hit_count"] > 0:
                covered_lines += 1
            else:
                uncovered_lines.append(cd_row["line_start"])

        coverage_pct = round(covered_lines / tracked_lines * 100, 1) if tracked_lines > 0 else 0.0

        return {
            "qualified_name": qualified_name,
            "file_path": row["rel_path"],
            "start_line": start_line,
            "end_line": end_line,
            "total_lines": total_lines,
            "tracked_lines": tracked_lines,
            "covered_lines": covered_lines,
            "coverage_pct": coverage_pct,
            "uncovered_lines": uncovered_lines,
        }

    def find_uncovered_functions(
        self, module_filter: str = "", threshold: int = 50
    ) -> List[Dict[str, Any]]:
        """查找覆盖率低于阈值的函数

        查询所有有覆盖率数据的函数，返回覆盖率 < threshold% 的函数列表。

        Args:
            module_filter: 模块路径前缀过滤（空字符串不过滤）
            threshold: 覆盖率阈值百分比，低于此值的函数返回

        Returns:
            未充分覆盖的函数列表，按覆盖率升序排列
        """
        ws_id = self._get_active_workspace_id()

        sql = """
            SELECT
                s.id, s.qualified_name, s.start_line, s.end_line,
                s.module_path, fi.rel_path,
                COUNT(cd.id) as tracked_lines,
                SUM(CASE WHEN cd.hit_count > 0 THEN 1 ELSE 0 END) as covered_lines
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            LEFT JOIN coverage_data cd ON cd.symbol_id = s.id
            WHERE fi.workspace_id = ?
              AND s.kind IN ('fn', 'function', 'method')
        """
        params: list = [ws_id]

        if module_filter:
            sql += " AND s.module_path LIKE ?"
            params.append(f"{module_filter}%")

        sql += " GROUP BY s.id HAVING tracked_lines > 0"

        cur = self.conn.execute(sql, params)

        results = []
        for row in cur:
            tracked = row["tracked_lines"]
            covered = row["covered_lines"]
            pct = covered / tracked * 100 if tracked > 0 else 0.0

            if pct < threshold:
                results.append({
                    "qualified_name": row["qualified_name"],
                    "file_path": row["rel_path"],
                    "module_path": row["module_path"],
                    "start_line": row["start_line"],
                    "end_line": row["end_line"],
                    "tracked_lines": tracked,
                    "covered_lines": covered,
                    "coverage_pct": round(pct, 1),
                })

        results.sort(key=lambda x: x["coverage_pct"])
        return results

    def test_impact_selection(self, qualified_name: str) -> List[Dict[str, Any]]:
        """测试影响选择

        通过调用链反查，找到所有调用该函数的测试函数。
        使用 BFS 遍历反向调用图，收集所有直接和间接调用者，
        从中筛选测试函数。

        测试函数特征：
        - 名称包含 test / Test / spec
        - 或 module_path 包含 test / tests

        Args:
            qualified_name: 目标函数限定名

        Returns:
            需要运行的测试函数列表
        """
        ws_id = self._get_active_workspace_id()

        # BFS 反向遍历调用图
        visited: Set[str] = set()
        queue: List[str] = [qualified_name]
        all_callers: Dict[str, Dict] = {}

        while queue:
            current_batch = [q for q in queue if q not in visited]
            if not current_batch:
                break

            # 查询当前批次的所有调用者
            placeholders = ",".join("?" * len(current_batch))
            cur = self.conn.execute(
                f"""
                SELECT DISTINCT
                    s.qualified_name, s.name, s.module_path,
                    s.start_line, fi.rel_path
                FROM calls c
                JOIN symbols s ON c.caller_id = s.id
                JOIN file_instances fi ON s.file_instance_id = fi.id
                WHERE fi.workspace_id = ?
                  AND c.callee_qualified IN ({placeholders})
                """,
                [ws_id] + current_batch,
            )

            next_queue: List[str] = []
            for row in cur:
                caller_qn = row["qualified_name"]
                if caller_qn and caller_qn not in visited:
                    all_callers[caller_qn] = {
                        "qualified_name": caller_qn,
                        "name": row["name"],
                        "module_path": row["module_path"],
                        "start_line": row["start_line"],
                        "file_path": row["rel_path"],
                    }
                    next_queue.append(caller_qn)

            for q in current_batch:
                visited.add(q)
            queue = next_queue

        # 从所有调用者中筛选测试函数
        def _is_test_function(caller: Dict) -> bool:
            """判断调用者是否为测试函数（名称或模块路径含 test / spec）"""
            name = (caller.get("name") or "").lower()
            qn = (caller.get("qualified_name") or "").lower()
            mod = (caller.get("module_path") or "").lower()
            return (
                "test" in name
                or "spec" in name
                or "test" in qn
                or "spec" in qn
                or "test" in mod
            )

        return [c for c in all_callers.values() if _is_test_function(c)]
