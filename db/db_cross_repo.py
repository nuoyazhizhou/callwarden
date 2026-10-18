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

from ..analyzers import IssueAnalyzerMixin


# 跨仓依赖检测专用的扩展名 → 语言映射（键为 _IMPORT_PATTERNS 支持的语言）。
# .js/.jsx 等映射到 typescript：_IMPORT_PATTERNS 无独立 javascript 键，
# JS 与 TS 共用同一组 import 模式（含 require()）。
_CROSS_REPO_EXT_TO_LANG = {
    ".py": "python",
    ".pyi": "python",
    ".rs": "rust",
    ".go": "go",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".js": "typescript",
    ".jsx": "typescript",
    ".mjs": "typescript",
    ".cjs": "typescript",
}


def _cross_repo_module_language(module_path: str) -> str:
    """跨仓库依赖检测专用的 module_path 语言推断（模块级函数，不经 self 分发）。

    为什么不能用 self._detect_language_from_module_path（生产 MRO 遮蔽实证）：
    真实 CodeGraphDB 的 MRO 共 47 类，IssueAnalyzerMixin 在 #12、
    CrossRepoMixin 在 #22——detect_cross_repo_deps 内经 self 调用时实际
    分发到 IssueAnalyzerMixin 版本（__qualname__ 实证）。该版本服务
    「缺陷检测规则选择」：子串匹配扩展名（'analyzers.coverage' 命中
    ".c" 子串误判为 c）、无扩展名兜底 rust（issues.py:210-247）。而生产
    symbols.module_path 实测为模块路径无扩展名（如 analyzers.coverage、
    src.main），python 仓的 import 语句被错配到 rust/c 模式，系统性 0 命中。
    模块级函数不经 MRO 分发，彻底规避该遮蔽。

    语义（与 CrossRepoMixin wrapper 自带简化版一致）：
    - 无扩展名 → 'python'（跨仓 import 匹配面以 python 为主）
    - 含 "::" → 'rust'（Rust 模块路径特征）
    - 有扩展名 → 按 _CROSS_REPO_EXT_TO_LANG 映射
    - 未知扩展名 → 'python'（默认）

    分工：本函数只服务 detect_cross_repo_deps 的 import 模式选择；
    wrapper _detect_language_from_module_path 继续服务「缺陷检测规则
    选择」的委托语义（471e3a4 的类型检查修复保留不动，勿回退）。
    """
    mp = (module_path or "").strip()
    if not mp:
        return "python"
    # Rust 模块路径特征：无扩展名但含 "::" → rust
    if "::" in mp:
        return "rust"
    # 取最后一段路径组件的扩展名（兼容 "/" 与 "\\" 分隔）
    base = mp.replace("\\", "/").rsplit("/", 1)[-1]
    if "." in base:
        ext = "." + base.rsplit(".", 1)[-1].lower()
        return _CROSS_REPO_EXT_TO_LANG.get(ext, "python")
    return "python"


class CrossRepoMixin:
    """跨仓库分析 Mixin

    依赖：
    - workspaces 表：多个 workspace 记录不同仓库
    - symbols / symbol_contents 表：跨仓库符号去重
    - cross_repo_deps 表：跨仓库依赖关系持久化
    """

    # 跨仓检测专用语言推断：模块级函数经 staticmethod 暴露到实例面，
    # 不经 self 方法分发——生产 MRO 中 IssueAnalyzerMixin(#12) 会遮蔽
    # 本类(#22)的 wrapper（'analyzers.coverage' 被子串误判为 'c'、
    # 无扩展名兜底 'rust'），详见 _cross_repo_module_language docstring。
    _cross_repo_module_language = staticmethod(_cross_repo_module_language)

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

            # 推断语言（跨仓检测专用模块级推断，不经 self 分发：
            # 生产 MRO 中 IssueAnalyzerMixin(#12) 会遮蔽 CrossRepoMixin(#22)
            # 的 wrapper——'analyzers.coverage' 命中 ".c" 子串误判为 'c'、
            # 无扩展名兜底 'rust'，python 仓 import 模式错配 0 命中）
            lang = _cross_repo_module_language(sym.get("module_path", ""))
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
                            #
                            # P1-2 修复（2026-07-22）：移除 import_path.endswith(cand_qn.split(".")[-1])
                            # 条件。candidates 是按 cand_qn.split(".")[-1] == module_name 筛出的，
                            # 而 import_path 中必然包含 module_name，所以此条件对任意候选恒真，
                            # 等于取第一个候选而非真正的 FQN 后缀匹配。改为只保留
                            # import_path.endswith(cand_qn)（完整 FQN 后缀匹配）。
                            for cand_qn, cand_hash in candidates:
                                # FQN 后缀匹配：import_path 以完整 cand_qn 结尾
                                # （如 import a.b.c.foo 匹配 b.c.foo，但不匹配 b.c.bar）
                                if cand_qn and import_path.endswith(cand_qn):
                                    matched_qn = cand_qn
                                    matched_hash = cand_hash
                                    break
                            if matched_qn is None and candidates:
                                # 没有 FQN 后缀匹配，取第一个候选（向后兼容 + 降低 confidence）
                                matched_qn, matched_hash = candidates[0]

                        if matched_qn is None:
                            continue

                        # P1-2 修复：同一轮扫描内用 (source_hash, target_hash, target_ws_id) 去重，
                        # 避免同一对符号被多次 import 语句重复记录。
                        # 旧实现缺 target_ws_id，同一 CAS symbol 出现在多个目标仓库时
                        # 后续仓库会被误去重（不同 target_workspace 的同 hash 符号被丢弃）
                        pair_key = (sym["symbol_hash"], matched_hash, t_id)
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
        # 注：原 ws_id = self._get_active_workspace_id() 为死代码（声明后从未使用），
        # 且会阻塞未挂载完整 CodeGraphDB 的测试场景。已移除。

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

        # P1-2 修复（2026-07-22）：cross_repo_impact 方向修正
        # cross_repo_deps 表语义：source_workspace 的 source_symbol_hash 导入了
        # target_workspace 的 target_symbol_hash（source 依赖 target）。
        # 改变一个符号时，它作为 target（被依赖方），应查找哪些 source 仓库依赖它。
        #
        # 旧实现错误地查 WHERE source_symbol_hash = ? 找 target_workspace_id
        # （即"我作为调用方依赖了哪些仓库"），但改变调用方不会反向影响被调用方。
        # 正确方向是查 WHERE target_symbol_hash = ? 找 source_workspace_id
        # （即"哪些仓库依赖了我"）。
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

        # P1-2 修复：只遍历 reverse_deps（依赖了我的仓库），
        # 移除原 direct_deps 循环（方向错误：我依赖的仓库不受我改变影响）
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
        """从 module_path 推断语言。

        分工说明（471e3a4 修复 + 生产 MRO 遮蔽升级）：
        - 本 wrapper 服务「缺陷检测规则选择」的委托语义：当本类与其他
          Mixin 组合、其 MRO 覆盖了本方法时，委托 IssueAnalyzerMixin
          版本（rust 兜底服务缺陷检测，issues.py:210-247）。
        - 跨仓库依赖检测（detect_cross_repo_deps）不使用本 wrapper——
          生产 CodeGraphDB MRO 共 47 类，IssueAnalyzerMixin 在 #12、
          CrossRepoMixin 在 #22，经 self 调用实际分发到 IssueAnalyzerMixin
          版本，其子串匹配（'analyzers.coverage' → 'c'）与 rust 兜底会
          使 python 仓 import 模式错配 0 命中；跨仓检测改用模块级
          _cross_repo_module_language（不经 MRO 分发）。
        判据用「解析目标是否为本类自带实现」而非 hasattr——后者恒真
        （self 上即定义着本方法），曾导致永不到达自带简化版：IssueAnalyzerMixin
        的无扩展名兜底是 rust（服务缺陷检测规则选择，issues.py:216），
        与跨仓依赖检测场景冲突，python 仓（生产 module_path 实测无扩展名，
        如 analyzers.coverage）被系统性误判，import 模式错配 0 命中。
        """
        own_impl = CrossRepoMixin._detect_language_from_module_path
        cls_impl = getattr(type(self), "_detect_language_from_module_path", None)
        cls_impl_fn = getattr(cls_impl, "__func__", cls_impl)
        own_impl_fn = getattr(own_impl, "__func__", own_impl)
        if cls_impl_fn is not own_impl_fn:
            # 仅当 type(self) 解析到的实现不是本类自带版本时才委托
            try:
                return IssueAnalyzerMixin._detect_language_from_module_path(self, module_path)
            except Exception:
                pass
        # 自带简化版（与跨仓检测专用 _cross_repo_module_language 同源）
        return _cross_repo_module_language(module_path)
