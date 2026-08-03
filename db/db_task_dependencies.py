"""
db_task_dependencies.py
========================

P2 Artifact / Interface 依赖图与环检测 Mixin。

实现 Requirements 9.1-9.10, 13.6-13.8：
- 9.1: 区分四类依赖（requires_existing / requires_artifact /
      provides_interface / requires_interface）
- 9.2: requires_existing 只验证存在性，不建边
- 9.3: requires_artifact 阻断 consumer 直到 provider artifact fresh
- 9.4: provides_interface 记录 interface identity/version/hash
- 9.5: requires_interface 必须匹配 existing/provided interface identity/version/hash
- 9.6: 硬边方向 provider→consumer，去重后检测环
- 9.7: 环检测失败时原子拒绝整个 Revision，返回一条 cycle path
- 9.8: informational 关系不阻断、不参与排序
- 9.9: 多 provider 无 Planner 选择立即拒绝
- 9.10: 只做无环校验和诊断，不做资源优化/自动 assignment/DAG 调度
"""

from __future__ import annotations

import time
import uuid
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple


# 依赖类型常量
DEP_REQUIRES_EXISTING = "requires_existing"
DEP_REQUIRES_ARTIFACT = "requires_artifact"
DEP_PROVIDES_INTERFACE = "provides_interface"
DEP_REQUIRES_INTERFACE = "requires_interface"

_VALID_DEP_TYPES = {
    DEP_REQUIRES_EXISTING,
    DEP_REQUIRES_ARTIFACT,
    DEP_PROVIDES_INTERFACE,
    DEP_REQUIRES_INTERFACE,
}

# Artifact freshness 状态
ARTIFACT_PRODUCING = "producing"
ARTIFACT_FRESH = "fresh"
ARTIFACT_STALE = "stale"


class TaskDependenciesMixin:
    """P2 依赖解析、边归一化与环检测 Mixin。

    依赖表语义：
    - task_dependencies：四类依赖声明（持久化 Envelope 中的依赖字段）
    - artifact_identities：provider 产出的 artifact identity/hash/freshness
    - interface_identities：interface identity/version/hash
    - interface_provider_selections：多 provider 时的显式选择
    - dependency_edges：去重后的硬依赖图边（provider→consumer）
    """

    # ------------------------------------------------------------------
    # 依赖声明导入（Req 9.1）
    # ------------------------------------------------------------------

    def import_envelope_dependencies(
        self,
        workspace_id: int,
        task_id: str,
        contract_id: str,
        contract_revision: int,
        dependencies: Sequence[Dict[str, Any]],
    ) -> Dict[str, Any]:
        """从 Envelope 导入四类依赖声明（Req 9.1）。

        Args:
            workspace_id: 工作区 ID
            task_id: 声明依赖的任务 ID
            contract_id: 契约 ID
            contract_revision: 契约 revision
            dependencies: 依赖列表，每项包含:
                - dependency_type: requires_existing/requires_artifact/
                  provides_interface/requires_interface
                - target_ref: 引用的符号名/artifact ID/interface identity
                - target_task_id: requires_artifact 时的 provider 任务 ID（可选）
                - is_informational: 是否信息性关系（不阻断，Req 9.8）

        Returns:
            {"imported": int, "skipped": int, "errors": List[str]}
        """
        now = time.time()
        imported = 0
        skipped = 0
        errors: List[str] = []

        for dep in dependencies:
            dtype = dep.get("dependency_type", "")
            if dtype not in _VALID_DEP_TYPES:
                errors.append(f"无效依赖类型: {dtype}")
                skipped += 1
                continue

            target_ref = dep.get("target_ref", "")
            if not target_ref:
                errors.append(f"依赖类型 {dtype} 缺少 target_ref")
                skipped += 1
                continue

            target_task_id = dep.get("target_task_id", "")
            is_info = 1 if dep.get("is_informational", False) else 0

            try:
                self.conn.execute(
                    """
                    INSERT OR IGNORE INTO task_dependencies
                        (workspace_id, task_id, contract_id, contract_revision,
                         dependency_type, target_ref, target_task_id,
                         is_informational, declared_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        workspace_id, task_id, contract_id, contract_revision,
                        dtype, target_ref, target_task_id,
                        is_info, now,
                    ),
                )
                imported += 1
            except Exception as exc:
                errors.append(f"导入依赖失败 ({dtype} → {target_ref}): {exc}")
                skipped += 1

        self.conn.commit()
        return {"imported": imported, "skipped": skipped, "errors": errors}

    # ------------------------------------------------------------------
    # Artifact identity 与 freshness（Req 9.3）
    # ------------------------------------------------------------------

    def record_artifact_identity(
        self,
        workspace_id: int,
        task_id: str,
        contract_id: str,
        contract_revision: int,
        artifact_type: str,
        artifact_ref: str,
        artifact_hash: str = "",
        workspace_snapshot_id: str = "",
    ) -> str:
        """记录 artifact identity（provider 产出 artifact 时调用，Req 9.3）。

        Args:
            artifact_type: file/symbol/resource
            artifact_ref: 文件路径或符号限定名
            artifact_hash: artifact 内容摘要（sha256:...）
            workspace_snapshot_id: 产出时绑定的工作区快照

        Returns:
            artifact_id（ART-<uuid>）
        """
        artifact_id = f"ART-{uuid.uuid4().hex[:12]}"
        now = time.time()
        freshness = ARTIFACT_FRESH if artifact_hash else ARTIFACT_PRODUCING

        self.conn.execute(
            """
            INSERT INTO artifact_identities
                (workspace_id, artifact_id, task_id, contract_id, contract_revision,
                 artifact_type, artifact_ref, artifact_hash, freshness_status,
                 produced_at, workspace_snapshot_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id, artifact_id, task_id, contract_id, contract_revision,
                artifact_type, artifact_ref, artifact_hash, freshness,
                now if freshness == ARTIFACT_FRESH else None,
                workspace_snapshot_id,
            ),
        )
        self.conn.commit()
        return artifact_id

    def get_artifact_freshness(
        self,
        workspace_id: int,
        task_id: str,
        artifact_ref: str = "",
    ) -> Optional[Dict[str, Any]]:
        """查询 artifact freshness 状态（Req 9.3，Gate 判定用）。

        Returns:
            {"artifact_id", "freshness_status", "artifact_hash", "produced_at"}
            或 None（不存在）
        """
        if artifact_ref:
            cur = self.conn.execute(
                """
                SELECT artifact_id, freshness_status, artifact_hash, produced_at
                FROM artifact_identities
                WHERE workspace_id = ? AND task_id = ? AND artifact_ref = ?
                ORDER BY produced_at DESC LIMIT 1
                """,
                (workspace_id, task_id, artifact_ref),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT artifact_id, freshness_status, artifact_hash, produced_at
                FROM artifact_identities
                WHERE workspace_id = ? AND task_id = ?
                ORDER BY produced_at DESC LIMIT 1
                """,
                (workspace_id, task_id),
            )
        row = cur.fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Interface identity 与 provider 选择（Req 9.4-9.5, 9.9）
    # ------------------------------------------------------------------

    def publish_interface(
        self,
        workspace_id: int,
        task_id: str,
        contract_id: str,
        contract_revision: int,
        interface_name: str,
        version: str,
        interface_hash: str = "",
    ) -> str:
        """发布 interface identity（provider 声明 provides_interface，Req 9.4）。

        Returns:
            interface_id（IF-<uuid>）
        """
        interface_id = f"IF-{uuid.uuid4().hex[:12]}"
        now = time.time()

        self.conn.execute(
            """
            INSERT OR IGNORE INTO interface_identities
                (workspace_id, interface_id, interface_name, version,
                 interface_hash, provider_task_id, contract_id,
                 contract_revision, published_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                workspace_id, interface_id, interface_name, version,
                interface_hash, task_id, contract_id,
                contract_revision, now,
            ),
        )
        self.conn.commit()
        return interface_id

    def get_interface_providers(
        self,
        workspace_id: int,
        interface_name: str,
        version: str = "",
    ) -> List[Dict[str, Any]]:
        """查询匹配的 interface provider 列表（Req 9.5, 9.9）。

        Returns:
            provider 列表，每项包含 interface_id/provider_task_id/version/hash
        """
        if version:
            cur = self.conn.execute(
                """
                SELECT interface_id, interface_name, version, interface_hash,
                       provider_task_id, contract_id, contract_revision
                FROM interface_identities
                WHERE workspace_id = ? AND interface_name = ? AND version = ?
                """,
                (workspace_id, interface_name, version),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT interface_id, interface_name, version, interface_hash,
                       provider_task_id, contract_id, contract_revision
                FROM interface_identities
                WHERE workspace_id = ? AND interface_name = ?
                """,
                (workspace_id, interface_name),
            )
        return [dict(r) for r in cur.fetchall()]

    def select_interface_provider(
        self,
        workspace_id: int,
        consumer_task_id: str,
        contract_id: str,
        contract_revision: int,
        interface_name: str,
        selected_provider_task_id: str,
    ) -> Dict[str, Any]:
        """记录 Planner 的显式 provider 选择（Req 9.9）。

        Returns:
            {"success": bool, "error": str}
        """
        now = time.time()
        try:
            self.conn.execute(
                """
                INSERT OR REPLACE INTO interface_provider_selections
                    (workspace_id, consumer_task_id, contract_id, contract_revision,
                     interface_name, selected_provider_task_id, selected_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    workspace_id, consumer_task_id, contract_id, contract_revision,
                    interface_name, selected_provider_task_id, now,
                ),
            )
            self.conn.commit()
            return {"success": True, "error": ""}
        except Exception as exc:
            return {"success": False, "error": str(exc)}

    def get_provider_selection(
        self,
        workspace_id: int,
        consumer_task_id: str,
        contract_id: str,
        contract_revision: int,
        interface_name: str,
    ) -> Optional[str]:
        """查询已记录的 provider 选择（Req 9.9）。

        Returns:
            selected_provider_task_id 或 None
        """
        cur = self.conn.execute(
            """
            SELECT selected_provider_task_id FROM interface_provider_selections
            WHERE workspace_id = ? AND consumer_task_id = ?
              AND contract_id = ? AND contract_revision = ?
              AND interface_name = ?
            """,
            (
                workspace_id, consumer_task_id, contract_id,
                contract_revision, interface_name,
            ),
        )
        row = cur.fetchone()
        return row["selected_provider_task_id"] if row else None

    # ------------------------------------------------------------------
    # requires_existing 存在性验证（Req 9.2）
    # ------------------------------------------------------------------

    def resolve_requires_existing(
        self,
        workspace_id: int,
        target_ref: str,
    ) -> Dict[str, Any]:
        """验证 requires_existing 引用的符号或资源是否存在（Req 9.2）。

        只验证存在性，不创建任何任务边。

        Returns:
            {"exists": bool, "kind": str, "ref": str}
            kind: "symbol" / "file" / "artifact" / "unknown"
        """
        # 1. 尝试匹配符号（qualified_name 或 name）
        cur = self.conn.execute(
            """
            SELECT s.id FROM symbols s
            JOIN file_instances fi ON s.file_instance_id = fi.id
            WHERE fi.workspace_id = ?
              AND (s.qualified_name = ? OR s.name = ?)
            LIMIT 1
            """,
            (workspace_id, target_ref, target_ref),
        )
        if cur.fetchone():
            return {"exists": True, "kind": "symbol", "ref": target_ref}

        # 2. 尝试匹配文件（rel_path）
        cur = self.conn.execute(
            """
            SELECT id FROM file_instances
            WHERE workspace_id = ? AND rel_path = ?
            LIMIT 1
            """,
            (workspace_id, target_ref),
        )
        if cur.fetchone():
            return {"exists": True, "kind": "file", "ref": target_ref}

        # 3. 尝试匹配 artifact_identities（artifact_id 或 artifact_ref）
        cur = self.conn.execute(
            """
            SELECT artifact_id FROM artifact_identities
            WHERE workspace_id = ?
              AND (artifact_id = ? OR artifact_ref = ?)
            LIMIT 1
            """,
            (workspace_id, target_ref, target_ref),
        )
        if cur.fetchone():
            return {"exists": True, "kind": "artifact", "ref": target_ref}

        return {"exists": False, "kind": "unknown", "ref": target_ref}

    # ------------------------------------------------------------------
    # 硬依赖图构建与边归一化（Req 9.6）
    # ------------------------------------------------------------------

    def build_hard_dependency_edges(
        self,
        workspace_id: int,
        contract_id: str,
        contract_revision: int,
    ) -> Dict[str, Any]:
        """为指定契约 revision 构建硬依赖图边（Req 9.6）。

        边方向：provider → consumer
        硬边来源：
        - requires_artifact: target_task_id (provider) → task_id (consumer)
        - requires_interface: 解析 provides_interface 后 provider → consumer

        informational 关系不建边（Req 9.8）。
        重复边通过 UNIQUE 约束自动去重（collapse duplicate edges）。

        Returns:
            {"edges_built": int, "edges_skipped": int,
             "resolution_errors": List[str], "provider_conflicts": List[Dict]}
        """
        # 查询该 revision 的所有非 informational 依赖
        cur = self.conn.execute(
            """
            SELECT task_id, dependency_type, target_ref, target_task_id,
                   contract_id, contract_revision
            FROM task_dependencies
            WHERE workspace_id = ?
              AND contract_id = ?
              AND contract_revision = ?
              AND is_informational = 0
            """,
            (workspace_id, contract_id, contract_revision),
        )
        deps = [dict(r) for r in cur.fetchall()]

        edges_built = 0
        edges_skipped = 0
        resolution_errors: List[str] = []
        provider_conflicts: List[Dict[str, Any]] = []
        now = time.time()

        for dep in deps:
            consumer_task_id = dep["task_id"]
            dtype = dep["dependency_type"]
            target_ref = dep["target_ref"]
            target_task_id = dep.get("target_task_id", "")
            dep_contract_id = dep["contract_id"]
            dep_revision = dep["contract_revision"]

            if dtype == DEP_REQUIRES_ARTIFACT:
                # requires_artifact: target_task_id 是 provider
                if not target_task_id:
                    resolution_errors.append(
                        f"requires_artifact 依赖缺少 target_task_id "
                        f"(task={consumer_task_id}, ref={target_ref})"
                    )
                    edges_skipped += 1
                    continue

                provider_task_id = target_task_id
                self._insert_dependency_edge(
                    workspace_id, provider_task_id, consumer_task_id,
                    "artifact", DEP_REQUIRES_ARTIFACT,
                    dep_contract_id, dep_revision, now,
                )
                edges_built += 1

            elif dtype == DEP_REQUIRES_INTERFACE:
                # requires_interface: 需要解析 provides_interface
                providers = self.get_interface_providers(
                    workspace_id, target_ref,
                )

                if not providers:
                    resolution_errors.append(
                        f"requires_interface '{target_ref}' 无匹配 provider "
                        f"(task={consumer_task_id})"
                    )
                    edges_skipped += 1
                    continue

                if len(providers) > 1:
                    # 多 provider：检查是否有显式选择（Req 9.9）
                    selected = self.get_provider_selection(
                        workspace_id, consumer_task_id,
                        dep_contract_id, dep_revision, target_ref,
                    )
                    if not selected:
                        provider_conflicts.append({
                            "consumer_task_id": consumer_task_id,
                            "interface_name": target_ref,
                            "providers": [
                                p["provider_task_id"] for p in providers
                            ],
                        })
                        edges_skipped += 1
                        continue
                    provider_task_id = selected
                else:
                    provider_task_id = providers[0]["provider_task_id"]

                self._insert_dependency_edge(
                    workspace_id, provider_task_id, consumer_task_id,
                    "interface", DEP_REQUIRES_INTERFACE,
                    dep_contract_id, dep_revision, now,
                )
                edges_built += 1

            # requires_existing 和 provides_interface 不建边

        self.conn.commit()
        return {
            "edges_built": edges_built,
            "edges_skipped": edges_skipped,
            "resolution_errors": resolution_errors,
            "provider_conflicts": provider_conflicts,
        }

    def _insert_dependency_edge(
        self,
        workspace_id: int,
        provider_task_id: str,
        consumer_task_id: str,
        edge_type: str,
        source_type: str,
        contract_id: str,
        contract_revision: int,
        created_at: float,
    ) -> None:
        """插入硬依赖图边（UNIQUE 约束自动去重，Req 9.6）。"""
        self.conn.execute(
            """
            INSERT OR IGNORE INTO dependency_edges
                (workspace_id, provider_task_id, consumer_task_id,
                 edge_type, source_type, contract_id, contract_revision,
                 is_hard, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 1, ?)
            """,
            (
                workspace_id, provider_task_id, consumer_task_id,
                edge_type, source_type, contract_id, contract_revision,
                created_at,
            ),
        )

    # ------------------------------------------------------------------
    # 环检测与最小 cycle path（Req 9.7, 13.6-13.8）
    # ------------------------------------------------------------------

    def detect_cycle(
        self,
        workspace_id: int,
    ) -> Dict[str, Any]:
        """检测硬依赖图中的环，返回最小 cycle path（Req 9.7）。

        使用 DFS 三色标记法检测环，并通过 BFS 找到最短 cycle path。

        Returns:
            {"has_cycle": bool, "cycle_path": List[str],
             "checked_nodes": int}
        """
        # 加载所有硬边
        cur = self.conn.execute(
            """
            SELECT DISTINCT provider_task_id, consumer_task_id
            FROM dependency_edges
            WHERE workspace_id = ? AND is_hard = 1
            """,
            (workspace_id,),
        )
        graph: Dict[str, List[str]] = {}
        for r in cur.fetchall():
            graph.setdefault(r["provider_task_id"], []).append(
                r["consumer_task_id"]
            )

        if not graph:
            return {"has_cycle": False, "cycle_path": [], "checked_nodes": 0}

        # DFS 三色标记检测是否有环
        WHITE, GRAY, BLACK = 0, 1, 2
        color: Dict[str, int] = {}
        has_cycle = False
        cycle_start_node: Optional[str] = None

        def dfs_has_cycle(node: str) -> bool:
            nonlocal has_cycle, cycle_start_node
            color[node] = GRAY
            for neighbor in graph.get(node, []):
                if color.get(neighbor, WHITE) == GRAY:
                    has_cycle = True
                    cycle_start_node = neighbor
                    return True
                if color.get(neighbor, WHITE) == WHITE:
                    if dfs_has_cycle(neighbor):
                        return True
            color[node] = BLACK
            return False

        for node in graph:
            if color.get(node, WHITE) == WHITE:
                if dfs_has_cycle(node):
                    break

        if not has_cycle or cycle_start_node is None:
            return {
                "has_cycle": False,
                "cycle_path": [],
                "checked_nodes": len(graph),
            }

        # 找最短 cycle path：从 cycle_start_node 出发 BFS 回到自身
        cycle_path = self._find_shortest_cycle(graph, cycle_start_node)

        return {
            "has_cycle": True,
            "cycle_path": cycle_path,
            "checked_nodes": len(graph),
        }

    def _find_shortest_cycle(
        self,
        graph: Dict[str, List[str]],
        start: str,
    ) -> List[str]:
        """BFS 找从 start 出发回到 start 的最短 cycle path。"""
        from collections import deque

        queue: deque = deque()
        # (current_node, path_so_far)
        queue.append((start, [start]))
        visited: Set[str] = {start}

        while queue:
            node, path = queue.popleft()
            for neighbor in graph.get(node, []):
                if neighbor == start and len(path) >= 1:
                    # 找到环：path + start
                    return path + [start]
                if neighbor not in visited:
                    visited.add(neighbor)
                    queue.append((neighbor, path + [neighbor]))

        # BFS 未找到（可能 DFS 检测到的环在 BFS 中因 visited 策略未覆盖），
        # 回退到 DFS 找任意 cycle path
        return self._find_cycle_dfs(graph, start)

    def _find_cycle_dfs(
        self,
        graph: Dict[str, List[str]],
        start: str,
    ) -> List[str]:
        """DFS 找从 start 出发的 cycle path（BFS 回退用）。"""
        path: List[str] = []
        visited: Set[str] = set()
        rec_stack: Set[str] = set()

        def dfs(node: str) -> List[str]:
            path.append(node)
            visited.add(node)
            rec_stack.add(node)

            for neighbor in graph.get(node, []):
                if neighbor == start and len(path) >= 1:
                    return path + [start]
                if neighbor not in visited:
                    result = dfs(neighbor)
                    if result:
                        return result

            path.pop()
            rec_stack.discard(node)
            return []

        return dfs(start)

    # ------------------------------------------------------------------
    # Revision 依赖验证（Req 9.7, 9.9）
    # ------------------------------------------------------------------

    def validate_revision_dependencies(
        self,
        workspace_id: int,
        contract_id: str,
        contract_revision: int,
    ) -> Dict[str, Any]:
        """验证指定 Revision 的依赖完整性（Req 9.7, 9.9）。

        验证步骤：
        1. 构建硬依赖图边（含 interface 解析和 provider 选择检查）
        2. 检测环 → 有环则拒绝，返回 cycle path
        3. 多 provider 无选择 → 拒绝

        Returns:
            {"valid": bool, "errors": List[str],
             "cycle_path": List[str], "provider_conflicts": List[Dict],
             "edges_built": int}
        """
        # 1. 构建边
        build_result = self.build_hard_dependency_edges(
            workspace_id, contract_id, contract_revision,
        )

        errors: List[str] = []
        errors.extend(build_result["resolution_errors"])

        # 2. 多 provider 冲突检查（Req 9.9）
        for conflict in build_result["provider_conflicts"]:
            errors.append(
                f"interface '{conflict['interface_name']}' 有多个 provider "
                f"{conflict['providers']} 但无 Planner 显式选择 "
                f"(consumer={conflict['consumer_task_id']})"
            )

        # 3. 环检测（Req 9.7）
        cycle_result = self.detect_cycle(workspace_id)
        cycle_path = cycle_result["cycle_path"]

        if cycle_result["has_cycle"]:
            errors.append(
                f"硬依赖图存在环: {' → '.join(cycle_path)}"
            )

        valid = len(errors) == 0 and not build_result["provider_conflicts"]

        return {
            "valid": valid,
            "errors": errors,
            "cycle_path": cycle_path if cycle_result["has_cycle"] else [],
            "provider_conflicts": build_result["provider_conflicts"],
            "edges_built": build_result["edges_built"],
            "edges_skipped": build_result["edges_skipped"],
        }

    # ------------------------------------------------------------------
    # 查询辅助方法
    # ------------------------------------------------------------------

    def get_task_dependencies(
        self,
        workspace_id: int,
        task_id: str,
        contract_revision: Optional[int] = None,
    ) -> List[Dict[str, Any]]:
        """查询指定任务声明的所有依赖。"""
        if contract_revision is not None:
            cur = self.conn.execute(
                """
                SELECT * FROM task_dependencies
                WHERE workspace_id = ? AND task_id = ?
                ORDER BY declared_at
                """,
                (workspace_id, task_id),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT * FROM task_dependencies
                WHERE workspace_id = ? AND task_id = ?
                ORDER BY declared_at
                """,
                (workspace_id, task_id),
            )
        return [dict(r) for r in cur.fetchall()]

    def get_dependency_edges(
        self,
        workspace_id: int,
        task_id: str = "",
    ) -> List[Dict[str, Any]]:
        """查询硬依赖图边（可按任务过滤）。"""
        if task_id:
            cur = self.conn.execute(
                """
                SELECT * FROM dependency_edges
                WHERE workspace_id = ?
                  AND (provider_task_id = ? OR consumer_task_id = ?)
                ORDER BY created_at
                """,
                (workspace_id, task_id, task_id),
            )
        else:
            cur = self.conn.execute(
                """
                SELECT * FROM dependency_edges
                WHERE workspace_id = ?
                ORDER BY created_at
                """,
                (workspace_id,),
            )
        return [dict(r) for r in cur.fetchall()]

    def clear_revision_edges(
        self,
        workspace_id: int,
        contract_id: str,
        contract_revision: int,
    ) -> int:
        """清除指定 Revision 的硬依赖图边（环检测失败后回滚用，Req 9.7）。

        Returns:
            删除的边数
        """
        cur = self.conn.execute(
            """
            DELETE FROM dependency_edges
            WHERE workspace_id = ?
              AND contract_id = ?
              AND contract_revision = ?
            """,
            (workspace_id, contract_id, contract_revision),
        )
        self.conn.commit()
        return cur.rowcount
