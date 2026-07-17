"""测试关联 Mixin：建立 test_fn ↔ 被测 fn 的关联关系

回答 agent 高频问题："foo() 有哪些 test 在测它？"

推断规则（优先级降序）：
1. direct_call（high）：test_fn 直接调用了 fn（最可靠）
2. name_convention（mid）：test_fn 名字匹配（test_foo / testFoo / foo_test → foo）
3. indirect（low）：test_fn 调用了 fn 的 callers 链中某函数（间接测试）

使用方式：
- db.build_test_relations()        # 全量扫描，重建关联表
- db.get_test_cases(qn)            # 查 foo() 的测试列表
- db.get_tested_functions(test_qn) # 查 test_foo() 测了哪些函数（反向）
"""
import re
import time
from typing import Dict, List, Optional, Any


class TestRelationMixin:
    """测试关联 Mixin：test_fn ↔ 被测 fn 关系推断与查询"""

    # 命名约定的正则模式（test_foo / testFoo / foo_test / test_foo_bar 等）
    # 匹配后 group(1) 是被测函数名候选
    _TEST_NAME_PATTERNS = [
        re.compile(r"^test[_]?([A-Z].*)$"),       # testFoo / test_foo → foo（首字母小写化）
        re.compile(r"^(.*)_test$"),               # foo_test → foo
    ]

    def _normalize_test_name(self, test_name: str) -> List[str]:
        """从 test 函数名推断被测函数名候选列表

        test_foo → ["foo"]
        testFoo  → ["foo"]  (驼峰首字母小写化)
        foo_test → ["foo"]
        test_foo_bar → ["foo_bar", "foo"]  (尝试多种切分)
        无匹配返回空列表
        """
        candidates = []
        for pat in self._TEST_NAME_PATTERNS:
            m = pat.match(test_name)
            if m:
                name = m.group(1)
                # testFoo → foo（首字母小写化）
                if name and name[0].isupper():
                    name = name[0].lower() + name[1:]
                # test_foo_bar → 也加入 foo_bar（去前导下划线）
                candidates.append(name)
                # 如果含下划线，尝试多级切分（test_parse_file → parse_file / parse）
                if "_" in name:
                    parts = name.split("_")
                    for i in range(1, len(parts)):
                        candidates.append("_".join(parts[:i]))
                        candidates.append("_".join(parts[i:]))
                break
        # 去重保序
        seen = set()
        result = []
        for c in candidates:
            if c and c not in seen:
                seen.add(c)
                result.append(c)
        return result

    def build_test_relations(self, force: bool = False) -> Dict[str, int]:
        """全量扫描 test 函数符号，推断并填充 test_case_relations 表

        识别 test 函数的策略（不依赖 test_fn kind，因为 Python 解析器不区分）：
        - 文件路径在 tests/ 目录下 + 函数名以 test_ 开头
        - 或文件路径在 test_*.py 文件中
        - 或文件路径在 server/ 下且函数名以 test_ 开头（嵌入式测试）

        Args:
            force: True 时先清空再重建；False 时增量（跳过已存在的对）

        Returns:
            统计字典
        """
        ws_id = self._get_active_workspace_id()
        now = time.time()

        if force:
            self.conn.execute("DELETE FROM test_case_relations WHERE workspace_id = ?", (ws_id,))
            self.conn.commit()

        stats = {"total_test_fns": 0, "direct_call": 0, "name_convention": 0, "indirect": 0, "inserted": 0}

        # 1. 取所有可能是 test 的函数（路径在 tests/ 或文件名 test_*.py 或函数名 test_*）
        cur = self.conn.execute(
            """SELECT s.id, s.name, s.qualified_name, s.file_instance_id, fi.rel_path
               FROM symbols s
               JOIN file_instances fi ON s.file_instance_id = fi.id
               WHERE fi.workspace_id = ? AND fi.status != 'archived'
                 AND s.kind IN ('fn', 'test_fn', 'method', 'function')
                 AND (
                   fi.rel_path LIKE 'tests/%'
                   OR fi.rel_path LIKE '%/test_%'
                   OR s.name LIKE 'test_%'
                 )""",
            (ws_id,),
        )
        test_fns = [dict(r) for r in cur]
        stats["total_test_fns"] = len(test_fns)

        # 2. 取所有 fn 符号（建立 name → id 映射，用于 name_convention 匹配）
        cur = self.conn.execute(
            """SELECT s.id, s.name, s.qualified_name
               FROM symbols s
               JOIN file_instances fi ON s.file_instance_id = fi.id
               WHERE fi.workspace_id = ? AND fi.status != 'archived'
                 AND s.kind IN ('fn', 'method', 'function')""",
            (ws_id,),
        )
        fn_by_name: Dict[str, List[int]] = {}
        for r in cur:
            fn_by_name.setdefault(r["name"], []).append(r["id"])

        # 3. 对每个 test_fn 推断关联
        # 性能优化：批量查询替代 N+1
        # 3a. 一次性查所有 test_fn 的 direct_call callees（IN 子句，分批避免占位符过多）
        test_fn_ids = [t["id"] for t in test_fns]
        direct_calls_map: Dict[int, List[int]] = {}  # test_fn_id -> [callee_id, ...]
        batch_size = 500
        for i in range(0, len(test_fn_ids), batch_size):
            chunk = test_fn_ids[i:i + batch_size]
            placeholders = ",".join("?" * len(chunk))
            cur = self.conn.execute(
                f"""SELECT DISTINCT c.caller_id, c.callee_id
                    FROM calls c
                    WHERE c.caller_id IN ({placeholders}) AND c.callee_id > 0""",
                chunk,
            )
            for r in cur:
                direct_calls_map.setdefault(r["caller_id"], []).append(r["callee_id"])

        # 3b. 收集所有 test_fn 的 callees（用于方法 C 的 indirect 查询）
        # 原逻辑：只有"没有 direct_call 命中"的 test_fn 才查 indirect
        # 但 indirect 查询需要先拿到 test_fn 的 callees（已在 3a 中收集）
        # 所以这里收集所有 callee_id 用于批量查 callers
        indirect_callees_to_query: List[int] = []
        indirect_map: Dict[int, List[int]] = {}  # callee_id -> [caller_id, ...] 用于回查
        seen_callees = set()
        for test_fn in test_fns:
            test_fn_id = test_fn["id"]
            callees = direct_calls_map.get(test_fn_id, [])
            for callee_id in callees:
                if callee_id not in seen_callees:
                    seen_callees.add(callee_id)
                    indirect_callees_to_query.append(callee_id)

        # 3c. 批量查所有 indirect callee 的 callers
        for i in range(0, len(indirect_callees_to_query), batch_size):
            chunk = indirect_callees_to_query[i:i + batch_size]
            placeholders = ",".join("?" * len(chunk))
            cur = self.conn.execute(
                f"""SELECT DISTINCT c.callee_id, c.caller_id
                    FROM calls c
                    JOIN symbols s ON c.caller_id = s.id
                    JOIN file_instances fi ON s.file_instance_id = fi.id
                    WHERE fi.workspace_id = ? AND c.callee_id IN ({placeholders})
                      AND s.kind IN ('fn', 'method', 'function')""",
                [ws_id] + chunk,
            )
            for r in cur:
                indirect_map.setdefault(r["callee_id"], []).append(r["caller_id"])

        # 3d. 基于批量结果组装关联（避免循环内查 DB）
        all_relations = []  # [(test_fn_id, tested_fn_id, method, confidence), ...]
        for test_fn in test_fns:
            test_fn_id = test_fn["id"]
            tested_ids = set()  # (tested_fn_id, method, confidence)

            # 方法 A：direct_call
            for callee_id in direct_calls_map.get(test_fn_id, []):
                tested_ids.add((callee_id, "direct_call", "high"))

            # 方法 B：name_convention - test_foo → foo
            name_candidates = self._normalize_test_name(test_fn["name"])
            for cand_name in name_candidates:
                if cand_name in fn_by_name:
                    for fid in fn_by_name[cand_name]:
                        # direct_call 优先级更高，不覆盖
                        if not any(t[0] == fid for t in tested_ids):
                            tested_ids.add((fid, "name_convention", "mid"))

            # 方法 C：indirect - 通过批量查询结果回查
            if not any(t[1] == "direct_call" for t in tested_ids):
                callees = direct_calls_map.get(test_fn_id, [])
                for callee_id in callees:
                    for indirect_fn_id in indirect_map.get(callee_id, []):
                        if not any(t[0] == indirect_fn_id for t in tested_ids):
                            tested_ids.add((indirect_fn_id, "indirect", "low"))

            for tested_fn_id, method, confidence in tested_ids:
                stats[method] = stats.get(method, 0) + 1
                all_relations.append((test_fn_id, tested_fn_id, method, confidence))

        # 3e. 批量入库（executemany 替代逐条 INSERT）
        if all_relations:
            rows = [
                (ws_id, test_fn_id, tested_fn_id, method, confidence, now)
                for test_fn_id, tested_fn_id, method, confidence in all_relations
            ]
            try:
                self.conn.executemany(
                    """INSERT OR IGNORE INTO test_case_relations
                       (workspace_id, test_fn_id, tested_fn_id, match_method, confidence, detected_at)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    rows,
                )
                stats["inserted"] = self.conn.total_changes
            except Exception:
                pass  # 忽略约束冲突

        self.conn.commit()
        return stats

    def get_test_cases(self, qualified_name: str) -> List[Dict[str, Any]]:
        """查询符号的测试 case 列表

        Args:
            qualified_name: 被测函数的限定名

        Returns:
            测试 case 列表，按 confidence 降序（high > mid > low），每条含：
            - test_fn_id / test_qualified_name / test_name / test_file / test_start_line
            - match_method / confidence
        """
        ws_id = self._get_active_workspace_id()

        cur = self.conn.execute(
            """SELECT tcr.test_fn_id, tcr.match_method, tcr.confidence,
                      s.name as test_name, s.qualified_name as test_qualified_name,
                      s.start_line as test_start_line,
                      fi.rel_path as test_file
               FROM test_case_relations tcr
               JOIN symbols s ON tcr.test_fn_id = s.id
               JOIN file_instances fi ON s.file_instance_id = fi.id
               WHERE tcr.workspace_id = ?
                 AND tcr.tested_fn_id = (
                   SELECT s2.id FROM symbols s2
                   JOIN file_instances fi2 ON s2.file_instance_id = fi2.id
                   WHERE fi2.workspace_id = ? AND s2.qualified_name = ?
                   LIMIT 1
                 )
               ORDER BY CASE tcr.confidence WHEN 'high' THEN 0 WHEN 'mid' THEN 1 ELSE 2 END""",
            (ws_id, ws_id, qualified_name),
        )
        return [dict(r) for r in cur]

    def get_tested_functions(self, test_qualified_name: str) -> List[Dict[str, Any]]:
        """反向查询：test_foo() 测了哪些函数

        Args:
            test_qualified_name: test 函数的限定名

        Returns:
            被测函数列表
        """
        ws_id = self._get_active_workspace_id()

        cur = self.conn.execute(
            """SELECT tcr.tested_fn_id, tcr.match_method, tcr.confidence,
                      s.name as tested_name, s.qualified_name as tested_qualified_name,
                      s.start_line as tested_start_line, s.end_line as tested_end_line,
                      fi.rel_path as tested_file
               FROM test_case_relations tcr
               JOIN symbols s ON tcr.tested_fn_id = s.id
               JOIN file_instances fi ON s.file_instance_id = fi.id
               WHERE tcr.workspace_id = ?
                 AND tcr.test_fn_id = (
                   SELECT s2.id FROM symbols s2
                   JOIN file_instances fi2 ON s2.file_instance_id = fi2.id
                   WHERE fi2.workspace_id = ? AND s2.qualified_name = ?
                   LIMIT 1
                 )
               ORDER BY CASE tcr.confidence WHEN 'high' THEN 0 WHEN 'mid' THEN 1 ELSE 2 END""",
            (ws_id, ws_id, test_qualified_name),
        )
        return [dict(r) for r in cur]

    def get_test_coverage_summary(self, qualified_name: str) -> Dict[str, Any]:
        """查询符号的测试覆盖情况摘要

        Returns:
            {"has_tests": bool, "test_count": int, "high_confidence_count": int, "tests": [...]}
        """
        tests = self.get_test_cases(qualified_name)
        high_count = sum(1 for t in tests if t.get("confidence") == "high")
        return {
            "has_tests": len(tests) > 0,
            "test_count": len(tests),
            "high_confidence_count": high_count,
            "tests": tests[:10],  # 最多返回 10 条
        }

    # ============================================
    # 测试运行结果（test_runs）
    # ============================================

    def import_test_results(self, junit_xml: str, ci_run_id: str = "",
                            ci_url: str = "") -> Dict[str, int]:
        """从 JUnit XML 导入测试运行结果

        解析 JUnit XML（pytest --junitxml 生成），将每个 test case 的执行结果
        存入 test_runs 表。通过 test_name 匹配 symbols 表的 test 函数。

        Args:
            junit_xml: JUnit XML 文件内容或文件路径
            ci_run_id: CI 运行 ID（可选，用于关联同一次运行）
            ci_url: CI 运行 URL（可选）

        Returns:
            {"total": N, "passed": N, "failed": N, "skipped": N, "error": N, "matched": N}
        """
        import os
        import xml.etree.ElementTree as ET

        ws_id = self._get_active_workspace_id()
        now = time.time()

        # 如果传入的是文件路径，读取内容
        if os.path.isfile(junit_xml):
            with open(junit_xml, "r", encoding="utf-8") as f:
                xml_content = f.read()
        else:
            xml_content = junit_xml

        stats = {"total": 0, "passed": 0, "failed": 0, "skipped": 0, "error": 0, "matched": 0}

        try:
            root = ET.fromstring(xml_content)
        except ET.ParseError as e:
            return {"parse_error": f"XML parse error: {e}"}

        # 构建 test_name → symbol_id 映射（一次性查询）
        cur = self.conn.execute(
            """SELECT s.id as id, s.name as name, s.qualified_name as qualified_name
               FROM symbols s
               JOIN file_instances fi ON s.file_instance_id = fi.id
               WHERE fi.workspace_id = ? AND fi.status != 'archived'
                 AND s.name LIKE 'test_%'""",
            (ws_id,),
        )
        name_to_id: Dict[str, int] = {}
        for r in cur:
            name_to_id[r["name"]] = r["id"]
            # 也用短名匹配（去掉 class 前缀）
            short_name = r["name"].split(".")[-1] if "." in r["name"] else r["name"]
            name_to_id.setdefault(short_name, r["id"])

        # 遍历 JUnit XML 的 testcase 节点
        for tc in root.iter("testcase"):
            test_name = tc.get("name", "")
            test_class = tc.get("classname", "")
            test_file = tc.get("file", "")
            time_str = tc.get("time", "0")
            duration_ms = float(time_str) * 1000 if time_str else 0

            # 判定状态
            status = "passed"
            error_msg = ""
            error_type = ""
            failure = tc.find("failure")
            error = tc.find("error")
            skipped = tc.find("skipped")
            if failure is not None:
                status = "failed"
                error_msg = (failure.text or "")[:500]
                error_type = failure.get("type", "")
            elif error is not None:
                status = "error"
                error_msg = (error.text or "")[:500]
                error_type = error.get("type", "")
            elif skipped is not None:
                status = "skipped"

            stats["total"] += 1
            stats[status] = stats.get(status, 0) + 1

            # 匹配 symbol_id
            test_fn_id = 0
            # 尝试：test_class.test_name → test_name → 短名
            full_name = f"{test_class}.{test_name}" if test_class else test_name
            if full_name in name_to_id:
                test_fn_id = name_to_id[full_name]
                stats["matched"] += 1
            elif test_name in name_to_id:
                test_fn_id = name_to_id[test_name]
                stats["matched"] += 1
            else:
                short = test_name.split(".")[-1] if "." in test_name else test_name
                if short in name_to_id:
                    test_fn_id = name_to_id[short]
                    stats["matched"] += 1

            self.conn.execute(
                """INSERT INTO test_runs
                   (workspace_id, test_fn_id, test_name, test_class, test_file,
                    status, duration_ms, error_message, error_type,
                    ci_run_id, ci_url, run_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (ws_id, test_fn_id, test_name, test_class, test_file,
                 status, duration_ms, error_msg, error_type,
                 ci_run_id, ci_url, now),
            )

        self.conn.commit()
        return stats

    def get_test_stability(self, qualified_name: str, limit: int = 50) -> Dict[str, Any]:
        """查询符号关联测试的稳定性

        查找通过 test_case_relations 关联到此符号的所有 test_fn，
        再查 test_runs 表获取它们的运行历史。

        Args:
            qualified_name: 被测函数的限定名
            limit: 最多返回多少条运行记录

        Returns:
            {"total_runs": N, "pass_rate": float, "avg_duration_ms": float,
             "recent_failures": [...], "by_test": {...}}
        """
        ws_id = self._get_active_workspace_id()

        # 先找到符号 ID
        cur = self.conn.execute(
            """SELECT s.id FROM symbols s
               JOIN file_instances fi ON s.file_instance_id = fi.id
               WHERE fi.workspace_id = ? AND s.qualified_name = ?
               LIMIT 1""",
            (ws_id, qualified_name),
        )
        row = cur.fetchone()
        if not row:
            return {"total_runs": 0, "pass_rate": 0.0, "avg_duration_ms": 0, "recent_failures": [], "by_test": {}}

        tested_fn_id = row[0]

        # 查关联的 test_fn 的运行记录
        cur = self.conn.execute(
            """SELECT tr.status, tr.duration_ms, tr.error_message, tr.error_type,
                      tr.run_at, tr.test_name, tr.ci_run_id
               FROM test_runs tr
               WHERE tr.workspace_id = ? AND tr.test_fn_id IN (
                   SELECT test_fn_id FROM test_case_relations
                   WHERE workspace_id = ? AND tested_fn_id = ?
               )
               ORDER BY tr.run_at DESC
               LIMIT ?""",
            (ws_id, ws_id, tested_fn_id, limit),
        )
        runs = [dict(r) for r in cur]

        if not runs:
            return {"total_runs": 0, "pass_rate": 0.0, "avg_duration_ms": 0, "recent_failures": [], "by_test": {}}

        total = len(runs)
        passed = sum(1 for r in runs if r["status"] == "passed")
        avg_duration = sum(r.get("duration_ms", 0) for r in runs) / total if total else 0
        recent_failures = [
            {
                "test_name": r["test_name"],
                "error_type": r.get("error_type", ""),
                "error_message": r.get("error_message", "")[:200],
                "run_at": r["run_at"],
            }
            for r in runs if r["status"] in ("failed", "error")
        ][:10]

        # 按 test 分组统计
        by_test: Dict[str, Dict[str, int]] = {}
        for r in runs:
            name = r["test_name"]
            if name not in by_test:
                by_test[name] = {"total": 0, "passed": 0, "failed": 0}
            by_test[name]["total"] += 1
            if r["status"] == "passed":
                by_test[name]["passed"] += 1
            elif r["status"] in ("failed", "error"):
                by_test[name]["failed"] += 1

        return {
            "total_runs": total,
            "pass_rate": round(passed / total, 3) if total else 0.0,
            "avg_duration_ms": round(avg_duration, 1),
            "recent_failures": recent_failures,
            "by_test": by_test,
        }

