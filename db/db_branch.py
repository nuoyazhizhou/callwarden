"""
db_branch.py
============

分支感知图谱 Mixin（F4：独立工作区方案）。

通过为每个分支注册独立 workspace 实现分支感知图谱：
- workspace.name = 分支名（如 "main" / "feature-x"）
- workspace.root_path = 物理仓库根路径 + "#" + 分支名（保证 UNIQUE 约束）
- 不新增 schema 表，完全复用已有 workspaces / symbols / file_instances 表

提供分支工作区注册、列举、符号差异比较、上下文切换、合并预览能力。
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from ..config import norm_path


class BranchMixin:
    """分支感知图谱 Mixin（独立工作区方案）

    依赖：
    - workspaces 表：每个分支对应一个 workspace（name=分支名）
    - symbols 表：通过 file_instance_id -> file_instances.workspace_id 关联到分支
    - calls 表：blast_radius 反向遍历调用图（来自 ImpactMixin）

    设计说明：
    - workspaces.root_path 有 UNIQUE 约束，同一仓库的多个分支通过
      root_path 追加 "#分支名" 保证唯一性，物理路径不变。
    - switch_branch_context 会还原真实的 workspace_root 给 module_resolver，
      保证后续文件路径解析正常。
    """

    # ------------------------------------------------------------------
    # 内部工具方法
    # ------------------------------------------------------------------

    @staticmethod
    def _make_branch_root(repo_root: str, branch_name: str) -> str:
        """构造分支工作区的唯一 root_path（物理路径 + #分支名）

        用 "#" 分隔符保证同一仓库的多个分支 root_path 不冲突，
        "#" 在路径中极少出现，不会与真实目录产生歧义。
        """
        return f"{repo_root}#{branch_name}"

    @staticmethod
    def _extract_real_root(root_path: str) -> str:
        """从分支工作区的 root_path 还原真实物理路径（去除 #分支名 后缀）"""
        if "#" in root_path:
            return root_path.split("#", 1)[0]
        return root_path

    def _find_workspace_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """按名称查找工作区，返回完整行字典或 None"""
        cur = self.conn.execute(
            "SELECT * FROM workspaces WHERE name = ?",
            (name,),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def _count_symbols_in_workspace(self, workspace_id: int) -> int:
        """统计指定工作区的符号数（symbols 表 COUNT）"""
        cur = self.conn.execute(
            """
            SELECT COUNT(*) as cnt
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
            """,
            (workspace_id,),
        )
        row = cur.fetchone()
        return row["cnt"] if row else 0

    def _load_workspace_symbols(self, workspace_id: int) -> Dict[str, Dict[str, Any]]:
        """加载工作区的全部符号，按 qualified_name 索引

        Returns:
            {qualified_name: {"symbol_hash": ..., "name": ..., "kind": ..., ...}}
            qualified_name 为空的符号被跳过。
        """
        cur = self.conn.execute(
            """
            SELECT s.symbol_hash, s.qualified_name, s.name, s.kind, s.module_path, fi.rel_path
            FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
            """,
            (workspace_id,),
        )
        result: Dict[str, Dict[str, Any]] = {}
        for r in cur:
            qn = r["qualified_name"] or ""
            if not qn:
                continue
            result[qn] = {
                "symbol_hash": r["symbol_hash"],
                "qualified_name": qn,
                "name": r["name"],
                "kind": r["kind"],
                "module_path": r["module_path"],
                "file_path": r["rel_path"],
            }
        return result

    # ------------------------------------------------------------------
    # 公开 API
    # ------------------------------------------------------------------

    def register_branch_workspace(
        self, branch_name: str, repo_root: str = ""
    ) -> Dict[str, Any]:
        """注册分支工作区

        将分支注册为独立 workspace（name=branch_name），复用 register_workspace。
        若分支已存在则直接返回，不重复创建。

        root_path 处理：物理路径 + "#分支名"，保证同一仓库的多个分支不冲突
        （workspaces.root_path 有 UNIQUE 约束）。

        Args:
            branch_name: 分支名（如 "main" / "feature-x"）
            repo_root: 仓库物理根路径，为空则使用 self.workspace_root

        Returns:
            {"workspace_id": int, "branch_name": str, "is_new": bool}
        """
        # 确定物理根路径
        if not repo_root:
            repo_root = self.workspace_root
        repo_root = norm_path(os.path.abspath(repo_root))

        # 已存在则直接返回
        existing = self._find_workspace_by_name(branch_name)
        if existing:
            return {
                "workspace_id": existing["id"],
                "branch_name": branch_name,
                "is_new": False,
            }

        # 构造唯一 root_path（追加 #branch_name 保证 UNIQUE 约束）
        branch_root = self._make_branch_root(repo_root, branch_name)
        workspace_id = self.register_workspace(
            branch_name, branch_root, description=f"分支工作区: {branch_name}"
        )

        return {
            "workspace_id": workspace_id,
            "branch_name": branch_name,
            "is_new": True,
        }

    def list_branch_workspaces(self) -> List[Dict[str, Any]]:
        """列出所有分支工作区

        Returns:
            每个工作区字典，含 id / name / root_path / created_at / symbol_count
            按 id 升序排列。
        """
        cur = self.conn.execute(
            """
            SELECT w.id, w.name, w.root_path, w.created_at, w.is_active,
                   (SELECT COUNT(*) FROM symbols s
                    JOIN file_instances fi ON s.file_instance_id = fi.id
                    WHERE fi.workspace_id = w.id) as symbol_count
            FROM workspaces w
            ORDER BY w.id ASC
            """
        )
        return [dict(row) for row in cur]

    def diff_branches(
        self, source_branch: str, target_branch: str
    ) -> Dict[str, Any]:
        """比较两个分支的符号差异

        通过比较两个 workspace 的 symbols 表（按 qualified_name 对比 symbol_hash）。

        - added: target 有但 source 没有
        - removed: source 有但 target 没有
        - modified: 两边都有但 symbol_hash 不同
        - unchanged_count: 两边都有且 hash 相同的符号数

        Args:
            source_branch: 源分支名
            target_branch: 目标分支名

        Returns:
            差异字典；分支不存在时返回 {"error": "..."}。
            added 项含 target 侧 symbol_hash；removed 项含 source 侧 symbol_hash；
            modified 项含 source_hash 和 target_hash。
        """
        src_ws = self._find_workspace_by_name(source_branch)
        tgt_ws = self._find_workspace_by_name(target_branch)
        if not src_ws:
            return {"error": f"源分支不存在: {source_branch}"}
        if not tgt_ws:
            return {"error": f"目标分支不存在: {target_branch}"}

        src_syms = self._load_workspace_symbols(src_ws["id"])
        tgt_syms = self._load_workspace_symbols(tgt_ws["id"])

        added: List[Dict[str, Any]] = []
        removed: List[Dict[str, Any]] = []
        modified: List[Dict[str, Any]] = []
        unchanged_count = 0

        # 遍历 target 符号：判断 added / modified / unchanged
        for qn, tgt_sym in tgt_syms.items():
            if qn not in src_syms:
                added.append({
                    "qualified_name": qn,
                    "symbol_hash": tgt_sym["symbol_hash"],
                    "name": tgt_sym["name"],
                    "kind": tgt_sym["kind"],
                })
            else:
                src_sym = src_syms[qn]
                if src_sym["symbol_hash"] != tgt_sym["symbol_hash"]:
                    modified.append({
                        "qualified_name": qn,
                        "source_hash": src_sym["symbol_hash"],
                        "target_hash": tgt_sym["symbol_hash"],
                        "name": tgt_sym["name"],
                        "kind": tgt_sym["kind"],
                    })
                else:
                    unchanged_count += 1

        # 遍历 source 符号：判断 removed
        for qn, src_sym in src_syms.items():
            if qn not in tgt_syms:
                removed.append({
                    "qualified_name": qn,
                    "symbol_hash": src_sym["symbol_hash"],
                    "name": src_sym["name"],
                    "kind": src_sym["kind"],
                })

        return {
            "added": added,
            "removed": removed,
            "modified": modified,
            "unchanged_count": unchanged_count,
        }

    def switch_branch_context(self, branch_name: str) -> Dict[str, Any]:
        """切换活动工作区到指定分支

        复用 self.active_workspace 机制（set_active_workspace），
        并还原真实物理路径给 module_resolver（去除 #分支名 后缀），
        保证后续文件路径解析正常。

        Args:
            branch_name: 分支名

        Returns:
            {"branch_name": str, "workspace_id": int, "symbol_count": N}
            分支不存在时返回 {"error": "..."}。
        """
        ws = self._find_workspace_by_name(branch_name)
        if not ws:
            return {"error": f"分支工作区不存在: {branch_name}"}

        # 调用基类方法切换活动工作区（会设置 active_workspace 并重建 resolver）
        self.set_active_workspace(branch_name)

        # 还原真实物理路径（branch workspace 的 root_path 含 #branch_name 后缀）
        real_root = self._extract_real_root(ws["root_path"])
        if real_root != self.workspace_root:
            self.workspace_root = real_root
            # 重建解析器，保证后续文件路径解析正常
            try:
                from .parsers import ModuleResolver, CallResolver
                self.module_resolver = ModuleResolver(self.workspace_root)
                self.call_resolver = CallResolver(self.module_resolver, self.parser)
            except Exception:
                # 解析器重建失败不阻断切换（符号查询/差异比较仍可用）
                pass

        symbol_count = self._count_symbols_in_workspace(ws["id"])
        return {
            "branch_name": branch_name,
            "workspace_id": ws["id"],
            "symbol_count": symbol_count,
        }

    def merge_preview(
        self, source_branch: str, target_branch: str
    ) -> Dict[str, Any]:
        """合并预览：分析 source 分支变更对 target 分支的影响

        基于 diff_branches 结果，对 added/modified 符号（target 侧）调用 blast_radius，
        汇总受影响符号数、影响层级和风险等级。

        分析流程：
        1. 调用 diff_branches 获取符号差异
        2. 切换到 target 分支上下文，保证 blast_radius 在 target 工作区计算
        3. 对 added/modified 符号逐个调用 blast_radius（depth=3）
        4. 汇总所有受影响符号（去重）并评估风险等级

        风险等级规则：
        - affected_symbols > 20 → high
        - affected_symbols > 5  → medium
        - 其余                   → low

        Args:
            source_branch: 源分支名
            target_branch: 目标分支名

        Returns:
            {
                "affected_symbols": N,     # 受影响符号总数（去重）
                "impact_layers": [...],    # 每个变更符号的影响层级汇总
                "risk_level": "low/medium/high",
            }
            分支不存在时透传 diff_branches 的 {"error": "..."}。
        """
        diff = self.diff_branches(source_branch, target_branch)
        if "error" in diff:
            return diff

        # 切换到 target 分支上下文，保证 blast_radius 在 target 工作区计算
        switch_result = self.switch_branch_context(target_branch)
        if "error" in switch_result:
            return switch_result

        # 收集需要分析的符号 hash（target 侧的 added 和 modified）
        # added 用 target 的 symbol_hash；modified 用 target_hash
        target_hashes: List[str] = []
        for item in diff.get("added", []):
            h = item.get("symbol_hash", "")
            if h:
                target_hashes.append(h)
        for item in diff.get("modified", []):
            h = item.get("target_hash", "")
            if h:
                target_hashes.append(h)

        # 逐个调用 blast_radius，去重收集受影响符号
        seen_hashes: set = set()
        all_impacted: set = set()
        impact_layers: List[Dict[str, Any]] = []

        for symbol_hash in target_hashes:
            if symbol_hash in seen_hashes:
                continue
            seen_hashes.add(symbol_hash)
            try:
                br = self.blast_radius(symbol_hash, depth=3)
            except Exception:
                # 单个符号分析失败不中断整体预览
                continue

            # 收集影响树中的所有符号 hash
            for layer in br.get("layers", []):
                for sym in layer.get("symbols", []):
                    h = sym.get("symbol_hash", "")
                    if h:
                        all_impacted.add(h)

            # 记录每个源符号的影响层级
            impact_layers.append({
                "source_symbol": br.get("source_symbol", ""),
                "source_hash": symbol_hash,
                "total_impacted": br.get("total_impacted", 0),
                "by_layer": br.get("by_layer", {}),
            })

        affected_count = len(all_impacted)

        # 风险等级评估
        if affected_count > 20:
            risk_level = "high"
        elif affected_count > 5:
            risk_level = "medium"
        else:
            risk_level = "low"

        return {
            "affected_symbols": affected_count,
            "impact_layers": impact_layers,
            "risk_level": risk_level,
        }
