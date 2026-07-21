"""
db_cross_repo.py
================

跨仓库分析 Mixin。

提供跨仓库依赖检测、共享符号识别、跨仓库影响分析等能力。
通过 Mixin 模式集成到 CodeGraphDB 主类。

核心思路：
- 每个仓库用独立 workspace 记录（复用 workspaces 表）
- 通过 symbol_contents.content_hash 跨仓库去重，识别共享代码
- 通过 import 语句正则匹配，检测跨仓库依赖
- 依赖关系持久化到 cross_repo_deps 表
"""

from __future__ import annotations

import re
import time
from typing import Any, Dict, List, Optional, Tuple


class CrossRepoMixin:
    """跨仓库分析 Mixin

    依赖：
    - workspaces 表：多个 workspace 记录不同仓库
    - symbols / symbol_contents 表：跨仓库符号去重
    - cross_repo_deps 表：跨仓库依赖关系持久化
    """

    # 跨仓库 import 语句的正则模式（多语言支持）
    _IMPORT_PATTERNS = {
        # Python: import xxx / from xxx import yyy
        "python": [
            re.compile(r"^\s*import\s+([\w\.]+)", re.MULTILINE),
            re.compile(r"^\s*from\s+([\w\.]+)\s+import", re.MULTILINE),
        ],
        # Rust: use xxx::yyy
        "rust": [
            re.compile(r"^\s*use\s+([\w:]+)", re.MULTILINE),
        ],
        # Go: import "xxx" / import ( "xxx" )
        "go": [
            re.compile(r"^\s*import\s+\"([^\"]+)\"", re.MULTILINE),
            re.compile(r"^\s*\"([^\"]+)\"", re.MULTILINE),
        ],
        # TypeScript/JavaScript: import xxx from 'xxx' / require('xxx')
        "typescript": [
            re.compile(r"^\s*import\s+.*\s+from\s+['\"]([^'\"]+)['\"]", re.MULTILINE),
            re.compile(r"^\s*require\(\s*['\"]([^'\"]+)['\"]\s*\)", re.MULTILINE),
        ],
    }

    def detect_cross_repo_deps(
        self,
        source_workspace: str,
        target_workspace: str = "",
    ) -> Dict[str, Any]:
        """检测跨仓库依赖关系

        通过扫描源仓库中所有符号的 content，用 import 语句正则匹配，
        检查 import 的模块名是否在目标仓库的符号中存在。

        Args:
            source_workspace: 源仓库名称
            target_workspace: 目标仓库名称（为空则扫描所有其他仓库）

        Returns:
            {
                "source_workspace": str,
                "detected_deps": [
                    {
                        "target_workspace": str,
                        "dependency_type": "import",
                        "source_symbol": str,
                        "target_symbol": str,
                        "evidence": str,     -- 匹配的 import 语句
                        "confidence": float,
                    },
                    ...
                ],
                "total_deps": int,
            }
        """
        source_ws_id = self._find_workspace_id_by_name(source_workspace)
        if not source_ws_id:
            return {"source_workspace": source_workspace, "detected_deps": [], "total_deps": 0}

        # 确定目标仓库列表
        if target_workspace:
            target_ids = [self._find_workspace_id_by_name(target_workspace)]
            target_ids = [t for t in target_ids if t]
        else:
            # 扫描所有其他 workspace
            cur = self.conn.execute(
                "SELECT id, name FROM workspaces WHERE id != ?",
                (source_ws_id,),
            )
            target_ids = [(r["id"], r["name"]) for r in cur]

        if not target_ids:
            return {"source_workspace": source_workspace, "detected_deps": [], "total_deps": 0}

        # 收集源仓库所有符号的 content
        cur = self.conn.execute(
            """
            SELECT s.symbol_hash, s.qualified_name, s.module_path, sc.content, fi.rel_path
            FROM symbols s
            JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
            """,
            (source_ws_id,),
        )
        source_symbols = [dict(r) for r in cur]

        # 收集目标仓库的符号名集合（用于匹配 import）
        # P1-2 修复（复审报告 §127-131）：原代码用 Dict[name, (qn, hash)]，
        # 重名 symbol（不同 module 下的同名函数/类）会被后写入的覆盖，导致只保留最后一个。
        # 改为 Dict[name, List[(qualified_name, symbol_hash)]] 按短名聚合所有重名候选，
        # 匹配时按 FQN 优先级挑选（FQN 全匹配 > FQN 后缀匹配 > 短名匹配）。
        target_symbol_names: Dict[int, Dict[str, List[Tuple[str, str]]]] = {}  # ws_id -> {symbol_name: [(qualified_name, symbol_hash), ...]}
        # 同时建立 FQN 反向索引，用于 FQN 全匹配（精度最高）
        target_symbol_fqns: Dict[int, Dict[str, Tuple[str, str]]] = {}  # ws_id -> {fqn: (name, symbol_hash)}
        for t in target_ids:
            t_id = t[0] if isinstance(t, tuple) else t
            t_name = t[1] if isinstance(t, tuple) else ""
            cur = self.conn.execute(
                """
                SELECT s.name, s.qualified_name, s.symbol_hash
                FROM symbols s
                JOIN file_instances fi ON s.file_instance_id = fi.id
                WHERE fi.workspace_id = ?
                """,
                (t_id,),
            )
            by_name: Dict[str, List[Tuple[str, str]]] = {}
            by_fqn: Dict[str, Tuple[str, str]] = {}
            for r in cur:
                name = r["name"]
                qn = r["qualified_name"]
                sh = r["symbol_hash"]
                # 短名索引：保留所有同名候选
                by_name.setdefault(name, []).append((qn, sh))
                # FQN 索引：FQN 唯一（数据库 schema 保证 qualified_name 在 file_instance 内唯一）
                if qn and qn not in by_fqn:
                    by_fqn[qn] = (name, sh)
            target_symbol_names[t_id] = by_name
            target_symbol_fqns[t_id] = by_fqn

        # 扫描源符号的 content 中的 import 语句
        detected_deps: List[Dict[str, Any]] = []
        # P1-2 修复：用 set 去重避免同一 (source_hash, target_hash) 对在同一轮扫描内被多次记录
        recorded_pairs: set = set()
        now = time.time()

        for sym in source_symbols:
            content = sym.get("content", "") or ""
            if not content:
                continue

            # 推断语言
            lang = self._detect_language_from_module_path(sym.get("module_path", ""))
            patterns = self._IMPORT_PATTERNS.get(lang, [])

            for pattern in patterns:
                for match in pattern.finditer(content):
                    import_path = match.group(1)
                    # P1-2 修复：保留 import 全路径用于 FQN 匹配（原代码只取最后一段）
                    # 优先级 1：用全路径作为 FQN 直接匹配（精度最高）
                    # 优先级 2：用 import 路径最后一段做短名匹配（向后兼容）
                    module_name = import_path.split(".")[-1].split("::")[-1].split("/")[-1]
                    if not module_name:
                        continue

                    for t_id, t_fqns in target_symbol_fqns.items():
                        # 优先级 1：FQN 全匹配（import_path 与目标 FQN 完全一致）
                        matched_qn: Optional[str] = None
                        matched_hash: Optional[str] = None
                        # 尝试多种 FQN 形式：原始路径 / 把 :: / 换成 . /
                        for candidate_fqn in (import_path,
                                               import_path.replace("::", "."),
                                               import_path.replace("/", ".")):
                            if candidate_fqn in t_fqns:
                                _, matched_hash = t_fqns[candidate_fqn]
                                matched_qn = candidate_fqn
                                break

                        # 优先级 2：短名匹配（向后兼容）— 遍历所有同名候选
                        if matched_qn is None:
                            t_names = target_symbol_names.get(t_id, {})
                            candidates = t_names.get(module_name, [])
                            if not candidates:
                                continue  # 当前目标仓库无此短名
                            # P1-2 修复：原 Dict[name] 只保留最后一个候选，
                            # 现在遍历所有候选，选择 FQN 与 import_path 后缀匹配的；
                            # 若都不后缀匹配，选第一个候选（向后兼容行为）
                            for cand_qn, cand_hash in candidates:
                                # 后缀匹配：import_path 以 cand_qn 结尾（如 a.b.c.foo 匹配 c.foo）
                                if cand_qn and (
                                    import_path.endswith(cand_qn)
                                    or import_path.endswith(cand_qn.split(".")[-1])
                                ):
                                    matched_qn = cand_qn
                                    matched_hash = cand_hash
                                    break
                            if matched_qn is None and candidates:
                                # 没有后缀匹配，取第一个候选（向后兼容 + 给出 confidence 降低提示）
                                matched_qn, matched_hash = candidates[0]

                        if matched_qn is None:
                            continue

                        # P1-2 修复：同一轮扫描内用 (source_hash, target_hash) 去重，
                        # 避免同一对符号被多次 import 语句重复记录
                        pair_key = (sym["symbol_hash"], matched_hash)
                        if pair_key in recorded_pairs:
                            continue
                        recorded_pairs.add(pair_key)

                        # 获取目标仓库名
                        cur = self.conn.execute(
                            "SELECT name FROM workspaces WHERE id = ?",
                            (t_id,),
                        )
                        t_row = cur.fetchone()
                        t_name = t_row["name"] if t_row else ""

                        # P1-2 修复：根据匹配类型调整 confidence
                        # - FQN 全匹配：confidence=0.95（高置信度）
                        # - FQN 后缀匹配：confidence=0.85
                        # - 短名匹配（向后兼容）：confidence=0.7（有重名风险）
                        if import_path in target_symbol_fqns[t_id] or \
                           import_path.replace("::", ".") in target_symbol_fqns[t_id] or \
                           import_path.replace("/", ".") in target_symbol_fqns[t_id]:
                            confidence = 0.95
                        elif matched_qn and import_path.endswith(matched_qn):
                            confidence = 0.85
                        else:
                            confidence = 0.7

                        dep = {
                            "target_workspace": t_name,
                            "dependency_type": "import",
                            "source_symbol": sym["qualified_name"],
                            "target_symbol": matched_qn,
                            "evidence": match.group(0).strip(),
                            "confidence": confidence,
                        }
                        detected_deps.append(dep)

                        # 持久化到 cross_repo_deps 表
                        # P1-2 修复：INSERT OR IGNORE 配合 schema v41 的 UNIQUE 索引实现幂等，
                        # 重复扫描不再追加新行（基于五元组 source_ws/target_ws/source_hash/
                        # target_hash/dependency_type 去重）
                        self.conn.execute(
                            """
                            INSERT OR IGNORE INTO cross_repo_deps
                                (source_workspace_id, target_workspace_id, dependency_type,
                                 source_symbol_hash, target_symbol_hash, evidence, confidence, detected_at)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            (
                                source_ws_id,
                                t_id,
                                "import",
                                sym["symbol_hash"],
                                matched_hash,
                                dep["evidence"],
                                confidence,
                                now,
                            ),
                        )
                        # P1-2 修复：原 break 只匹配一个目标仓库，改为 continue 允许多仓库匹配
                        # （不同目标仓库的相同 import 是真实场景，应全部记录）

        self.conn.commit()

        return {
            "source_workspace": source_workspace,
            "detected_deps": detected_deps,
            "total_deps": len(detected_deps),
        }

    def find_shared_symbols(
        self,
        workspace_a: str = "",
        workspace_b: str = "",
    ) -> Dict[str, Any]:
        """查找跨仓库共享符号（相同 content_hash 的函数）

        利用 symbol_contents 表的 content_hash 去重特性：
        如果两个仓库中存在相同 content_hash 的符号，说明它们共享相同实现。

        Args:
            workspace_a: 仓库名称 A（为空则扫描所有 workspace）
            workspace_b: 仓库名称 B（为空则 A 与所有其他仓库对比）

        Returns:
            {
                "total_shared": int,
                "shared_symbols": [
                    {
                        "content_hash": str,
                        "workspace_a": str,
                        "workspace_b": str,
                        "qualified_name_a": str,
                        "qualified_name_b": str,
                        "file_a": str,
                        "file_b": str,
                    },
                    ...
                ]
            }
        """
        # 构建查询：找 content_hash 在多个 workspace 中出现
        sql = """
            SELECT sc.content_hash, s.qualified_name, fi.rel_path, w.id as ws_id, w.name as ws_name
            FROM symbols s
            JOIN symbol_contents sc ON s.symbol_hash = sc.content_hash
            JOIN file_instances fi ON s.file_instance_id = fi.id
            JOIN workspaces w ON fi.workspace_id = w.id
            WHERE s.kind = 'fn'
        """
        params: list = []

        if workspace_a:
            ws_a_id = self._find_workspace_id_by_name(workspace_a)
            if not ws_a_id:
                return {"total_shared": 0, "shared_symbols": []}
            sql += " AND fi.workspace_id = ?"
            params.append(ws_a_id)

        sql += " ORDER BY sc.content_hash"

        cur = self.conn.execute(sql, params)
        rows = [dict(r) for r in cur]

        # 按 content_hash 分组，找出现在在多个 workspace 的
        from collections import defaultdict
        by_hash: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
        for r in rows:
            by_hash[r["content_hash"]].append(r)

        shared_symbols: List[Dict[str, Any]] = []
        for content_hash, syms in by_hash.items():
            if len(syms) < 2:
                continue
            # 检查是否跨 workspace
            ws_ids = set(s["ws_id"] for s in syms)
            if len(ws_ids) < 2:
                continue
            # 找到不同 workspace 的配对
            for i in range(len(syms)):
                for j in range(i + 1, len(syms)):
                    if syms[i]["ws_id"] != syms[j]["ws_id"]:
                        # 如果指定了 workspace_b，过滤
                        if workspace_b:
                            ws_b_id = self._find_workspace_id_by_name(workspace_b)
                            if syms[j]["ws_id"] != ws_b_id:
                                continue
                        shared_symbols.append({
                            "content_hash": content_hash,
                            "workspace_a": syms[i]["ws_name"],
                            "workspace_b": syms[j]["ws_name"],
                            "qualified_name_a": syms[i]["qualified_name"],
                            "qualified_name_b": syms[j]["qualified_name"],
                            "file_a": syms[i]["rel_path"],
                            "file_b": syms[j]["rel_path"],
                        })

        return {
            "total_shared": len(shared_symbols),
            "shared_symbols": shared_symbols,
        }

    def cross_repo_impact(
        self,
        symbol_hash: str,
        depth: int = 2,
    ) -> Dict[str, Any]:
        """跨仓库影响分析

        给定一个符号，分析它的变更会影响哪些其他仓库。
        通过 cross_repo_deps 表 + blast_radius 联合分析。

        Args:
            symbol_hash: 变更符号的 hash
            depth: 影响传播深度

        Returns:
            {
                "source_symbol": str,
                "source_workspace": str,
                "impacted_repos": [
                    {
                        "workspace": str,
                        "impacted_symbols": [...],
                        "dependency_type": str,
                        "confidence": float,
                    },
                    ...
                ],
                "total_impacted_repos": int,
                "risk_level": "low/medium/high",
            }
        """
        ws_id = self._get_active_workspace_id()

        # 查找源符号
        cur = self.conn.execute(
            """
            SELECT s.symbol_hash, s.qualified_name, w.name as ws_name
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            JOIN workspaces w ON fi.workspace_id = w.id
            WHERE s.symbol_hash = ?
            LIMIT 1
            """,
            (symbol_hash,),
        )
        source_row = cur.fetchone()
        if not source_row:
            return {
                "source_symbol": "",
                "source_workspace": "",
                "impacted_repos": [],
                "total_impacted_repos": 0,
                "risk_level": "none",
            }

        source_qn = source_row["qualified_name"]
        source_ws = source_row["ws_name"]

        # 1. 查找直接依赖该符号的其他仓库（cross_repo_deps 表）
        cur = self.conn.execute(
            """
            SELECT DISTINCT
                crd.target_workspace_id,
                w.name as target_ws_name,
                crd.dependency_type,
                crd.confidence,
                crd.evidence
            FROM cross_repo_deps crd
            JOIN workspaces w ON crd.target_workspace_id = w.id
            WHERE crd.source_symbol_hash = ?
            """,
            (symbol_hash,),
        )
        direct_deps = [dict(r) for r in cur]

        # 2. 反向查找：哪些仓库的符号调用了源符号（通过 cross_repo_deps 反查）
        cur = self.conn.execute(
            """
            SELECT DISTINCT
                crd.source_workspace_id,
                w.name as source_ws_name,
                crd.dependency_type,
                crd.confidence,
                crd.source_symbol_hash
            FROM cross_repo_deps crd
            JOIN workspaces w ON crd.source_workspace_id = w.id
            WHERE crd.target_symbol_hash = ?
            """,
            (symbol_hash,),
        )
        reverse_deps = [dict(r) for r in cur]

        # 3. 用 blast_radius 找同仓库内的传播（depth 层）
        local_blast = {"total_impacted": 0}
        if hasattr(self, "blast_radius"):
            try:
                local_blast = self.blast_radius(symbol_hash, depth=depth)
            except Exception:
                pass

        # 汇总受影响仓库
        impacted_repos: Dict[str, Dict[str, Any]] = {}

        for dep in direct_deps:
            ws_name = dep["target_ws_name"]
            if ws_name not in impacted_repos:
                impacted_repos[ws_name] = {
                    "workspace": ws_name,
                    "impacted_symbols": [],
                    "dependency_type": dep["dependency_type"],
                    "confidence": dep["confidence"],
                }

        for dep in reverse_deps:
            ws_name = dep["source_ws_name"]
            if ws_name not in impacted_repos:
                impacted_repos[ws_name] = {
                    "workspace": ws_name,
                    "impacted_symbols": [],
                    "dependency_type": dep["dependency_type"],
                    "confidence": dep["confidence"],
                }
            # 反向依赖的源符号是受影响的
            if dep["source_symbol_hash"]:
                impacted_repos[ws_name]["impacted_symbols"].append(dep["source_symbol_hash"])

        impacted_list = list(impacted_repos.values())
        total = len(impacted_list)

        # 风险等级：受影响仓库 >3 → high，>1 → medium，否则 low
        if total > 3:
            risk_level = "high"
        elif total > 1:
            risk_level = "medium"
        else:
            risk_level = "low"

        return {
            "source_symbol": source_qn,
            "source_workspace": source_ws,
            "local_impacted_count": local_blast.get("total_impacted", 0),
            "impacted_repos": impacted_list,
            "total_impacted_repos": total,
            "risk_level": risk_level,
        }

    def cross_repo_summary(self) -> Dict[str, Any]:
        """跨仓库分析总览

        Returns:
            {
                "total_repos": int,
                "repos": [...],
                "total_cross_deps": int,
                "total_shared_symbols": int,
                "deps_by_type": {...},
            }
        """
        # 仓库列表
        cur = self.conn.execute(
            """
            SELECT w.id, w.name, w.root_path, w.created_at,
                   COUNT(DISTINCT s.id) as symbol_count
            FROM workspaces w
            LEFT JOIN file_instances fi ON fi.workspace_id = w.id
            LEFT JOIN symbols s ON s.file_instance_id = fi.id
            GROUP BY w.id
            ORDER BY w.created_at
            """
        )
        repos = [dict(r) for r in cur]

        # 跨仓库依赖统计
        cur = self.conn.execute(
            """
            SELECT dependency_type, COUNT(*) as cnt
            FROM cross_repo_deps
            GROUP BY dependency_type
            """
        )
        deps_by_type = {r["dependency_type"]: r["cnt"] for r in cur}

        cur = self.conn.execute("SELECT COUNT(*) as cnt FROM cross_repo_deps")
        total_deps = cur.fetchone()["cnt"]

        # 共享符号统计
        shared = self.find_shared_symbols()

        return {
            "total_repos": len(repos),
            "repos": repos,
            "total_cross_deps": total_deps,
            "total_shared_symbols": shared["total_shared"],
            "deps_by_type": deps_by_type,
        }

    def _find_workspace_id_by_name(self, name: str) -> Optional[int]:
        """通过工作区名称查找 ID"""
        cur = self.conn.execute(
            "SELECT id FROM workspaces WHERE name = ? LIMIT 1",
            (name,),
        )
        row = cur.fetchone()
        return row["id"] if row else None

    def _detect_language_from_module_path(self, module_path: str) -> str:
        """从 module_path 推断语言（复用 IssueAnalyzerMixin 的逻辑，若可用）"""
        if hasattr(self, "_detect_language_from_module_path"):
            # 优先用 IssueAnalyzerMixin 的实现（如果已加载）
            try:
                return IssueAnalyzerMixin._detect_language_from_module_path(self, module_path)
            except Exception:
                pass
        # 简化版
        mp = (module_path or "").lower()
        if ".py" in mp:
            return "python"
        if ".rs" in mp or "::" in mp:
            return "rust"
        if ".go" in mp:
            return "go"
        if ".ts" in mp or ".js" in mp:
            return "typescript"
        return "python"  # 默认
