"""P2 依赖图与环检测工具（Req 9.1-9.10）

拆分自 server/mcp_server.py（5138-5328 行区间），由 register(mcp) 注册。
"""

# P2: 依赖图与环检测工具（Req 9.1-9.10, 13.7-13.8）

from typing import Optional

from mcp.server.fastmcp import FastMCP

from .._mcp_common import get_db


def register(mcp: FastMCP) -> None:
    @mcp.tool()
    def import_envelope_dependencies(
        workspace_id: int,
        task_id: str,
        contract_id: str,
        contract_revision: int,
        dependencies: list,
    ) -> dict:
        """从 Envelope 导入四类依赖声明（Req 9.1）。

        依赖类型：requires_existing / requires_artifact /
        provides_interface / requires_interface。

        Args:
            workspace_id: 工作区 ID
            task_id: 声明依赖的任务 ID
            contract_id: 契约 ID
            contract_revision: 契约 revision
            dependencies: 依赖列表，每项含 dependency_type/target_ref/target_task_id/is_informational

        Returns:
            {"imported": int, "skipped": int, "errors": list}
        """
        db = get_db()
        return db.import_envelope_dependencies(
            workspace_id, task_id, contract_id, contract_revision, dependencies,
        )

    @mcp.tool()
    def record_artifact_identity(
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
            artifact_hash: artifact 内容摘要（sha256:...），非空时 freshness=fresh
            workspace_snapshot_id: 产出时绑定的工作区快照

        Returns:
            artifact_id（ART-<uuid>）
        """
        db = get_db()
        return db.record_artifact_identity(
            workspace_id, task_id, contract_id, contract_revision,
            artifact_type, artifact_ref, artifact_hash, workspace_snapshot_id,
        )

    @mcp.tool()
    def get_artifact_freshness(
        workspace_id: int,
        task_id: str,
        artifact_ref: str = "",
    ) -> Optional[dict]:
        """查询 artifact freshness 状态（Req 9.3，Gate 判定用）。

        Returns:
            {"artifact_id", "freshness_status", "artifact_hash", "produced_at"} 或 None
        """
        db = get_db()
        return db.get_artifact_freshness(workspace_id, task_id, artifact_ref)

    @mcp.tool()
    def publish_interface(
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
        db = get_db()
        return db.publish_interface(
            workspace_id, task_id, contract_id, contract_revision,
            interface_name, version, interface_hash,
        )

    @mcp.tool()
    def get_interface_providers(
        workspace_id: int,
        interface_name: str,
        version: str = "",
    ) -> list:
        """查询匹配的 interface provider 列表（Req 9.5, 9.9）。

        Returns:
            provider 列表，每项含 interface_id/provider_task_id/version/hash
        """
        db = get_db()
        return db.get_interface_providers(workspace_id, interface_name, version)

    @mcp.tool()
    def select_interface_provider(
        workspace_id: int,
        consumer_task_id: str,
        contract_id: str,
        contract_revision: int,
        interface_name: str,
        selected_provider_task_id: str,
    ) -> dict:
        """记录 Planner 的显式 provider 选择（Req 9.9）。

        Returns:
            {"success": bool, "error": str}
        """
        db = get_db()
        return db.select_interface_provider(
            workspace_id, consumer_task_id, contract_id, contract_revision,
            interface_name, selected_provider_task_id,
        )

    @mcp.tool()
    def build_hard_dependency_edges(
        workspace_id: int,
        contract_id: str,
        contract_revision: int,
    ) -> dict:
        """为指定契约 revision 构建硬依赖图边（Req 9.6）。

        边方向 provider→consumer，去重后用于环检测。

        Returns:
            {"edges_built": int, "edges_skipped": int}
        """
        db = get_db()
        return db.build_hard_dependency_edges(workspace_id, contract_id, contract_revision)

    @mcp.tool()
    def detect_cycle(workspace_id: int) -> dict:
        """检测硬依赖图中的环，返回最小 cycle path（Req 9.7）。

        只做无环校验和诊断，不提供自动排程/assignment/抢占（Req 9.10）。

        Returns:
            {"has_cycle": bool, "cycle_path": list}
        """
        db = get_db()
        return db.detect_cycle(workspace_id)

    @mcp.tool()
    def validate_revision_dependencies(
        workspace_id: int,
        contract_id: str,
        contract_revision: int,
    ) -> dict:
        """验证指定 Revision 的依赖完整性（Req 9.7, 9.9）。

        验证内容：硬依赖图无环 + 多 provider 有显式选择。
        revision 有环时原子拒绝。

        Returns:
            {"valid": bool, "errors": list, "cycle_path": list}
        """
        db = get_db()
        return db.validate_revision_dependencies(workspace_id, contract_id, contract_revision)

    @mcp.tool()
    def get_dependency_edges(
        workspace_id: int,
        task_id: str = "",
    ) -> list:
        """查询硬依赖图边（Req 9.6，诊断用）。

        Args:
            task_id: 可选，按任务 ID 过滤（provider 或 consumer 匹配）

        Returns:
            依赖边列表，每项含 provider_task_id/consumer_task_id/edge_type/source_type/is_hard
        """
        db = get_db()
        return db.get_dependency_edges(workspace_id, task_id)
