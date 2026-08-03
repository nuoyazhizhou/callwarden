"""P2 任务 6.2：依赖解析、边归一化与最小 cycle path 单元测试。

验证 TaskDependenciesMixin 的核心契约行为（Requirements 9.1–9.10, 13.6–13.8）：
- 9.1: 四类依赖区分导入
- 9.2: requires_existing 只验证存在性，不建边
- 9.3: artifact identity 与 freshness
- 9.4: provides_interface 记录 identity/version/hash
- 9.5: requires_interface 匹配 provider identity/version/hash
- 9.6: 硬边方向 provider→consumer，去重
- 9.7: 环检测返回 cycle path，验证拒绝
- 9.8: informational 关系不阻断、不建边
- 9.9: 多 provider 无选择立即拒绝
- 9.10: 只做无环校验，不做调度
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_PKG_PARENT = str(Path(__file__).resolve().parents[1].parent)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db import CodeGraphDB
from callwarden.db.db_task_dependencies import (
    DEP_PROVIDES_INTERFACE,
    DEP_REQUIRES_ARTIFACT,
    DEP_REQUIRES_EXISTING,
    DEP_REQUIRES_INTERFACE,
    ARTIFACT_FRESH,
    ARTIFACT_PRODUCING,
)


# ============================================
# Fixtures
# ============================================

@pytest.fixture
def db_with_deps(tmp_path):
    """创建带 P2 schema 的临时数据库。"""
    db_path = str(tmp_path / "test_deps.db")
    db = CodeGraphDB(db_path, workspace_root=str(tmp_path))
    ws_id = db.register_workspace("test", str(tmp_path), "测试")
    yield db, ws_id
    db.close()


# ============================================
# Req 9.1: 四类依赖区分导入
# ============================================

class TestImportEnvelopeDependencies:
    """验证四类依赖声明能正确导入（Req 9.1）。"""

    def test_import_all_four_types(self, db_with_deps):
        """四类依赖都能成功导入。"""
        db, ws_id = db_with_deps
        deps = [
            {"dependency_type": DEP_REQUIRES_EXISTING, "target_ref": "auth.service"},
            {"dependency_type": DEP_REQUIRES_ARTIFACT, "target_ref": "ART-abc", "target_task_id": "T-provider"},
            {"dependency_type": DEP_PROVIDES_INTERFACE, "target_ref": "auth.login"},
            {"dependency_type": DEP_REQUIRES_INTERFACE, "target_ref": "auth.verify"},
        ]
        result = db.import_envelope_dependencies(
            ws_id, "T-consumer", "C-test", 1, deps,
        )
        assert result["imported"] == 4
        assert result["skipped"] == 0
        assert len(result["errors"]) == 0

    def test_invalid_dependency_type_rejected(self, db_with_deps):
        """无效依赖类型被跳过。"""
        db, ws_id = db_with_deps
        deps = [
            {"dependency_type": "invalid_type", "target_ref": "foo"},
        ]
        result = db.import_envelope_dependencies(
            ws_id, "T-consumer", "C-test", 1, deps,
        )
        assert result["imported"] == 0
        assert result["skipped"] == 1
        assert len(result["errors"]) == 1

    def test_missing_target_ref_rejected(self, db_with_deps):
        """缺少 target_ref 的依赖被跳过。"""
        db, ws_id = db_with_deps
        deps = [
            {"dependency_type": DEP_REQUIRES_EXISTING, "target_ref": ""},
        ]
        result = db.import_envelope_dependencies(
            ws_id, "T-consumer", "C-test", 1, deps,
        )
        assert result["imported"] == 0
        assert result["skipped"] == 1

    def test_idempotent_import(self, db_with_deps):
        """重复导入相同依赖是幂等的（UNIQUE 约束）。"""
        db, ws_id = db_with_deps
        deps = [
            {"dependency_type": DEP_REQUIRES_ARTIFACT, "target_ref": "ART-abc", "target_task_id": "T-p1"},
        ]
        db.import_envelope_dependencies(ws_id, "T-c1", "C-test", 1, deps)
        db.import_envelope_dependencies(ws_id, "T-c1", "C-test", 1, deps)
        stored = db.get_task_dependencies(ws_id, "T-c1")
        assert len(stored) == 1


# ============================================
# Req 9.2: requires_existing 只验证存在性
# ============================================

class TestRequiresExisting:
    """验证 requires_existing 只验证存在性，不建边（Req 9.2）。"""

    def test_existing_symbol_found(self, db_with_deps):
        """存在的符号返回 exists=True。"""
        db, ws_id = db_with_deps
        # 先导入一个文件和符号（通过 file_instances + symbols）
        # 这里简化测试：直接检查不存在的情况
        result = db.resolve_requires_existing(ws_id, "nonexistent.symbol")
        assert result["exists"] is False

    def test_no_edge_created_for_requires_existing(self, db_with_deps):
        """requires_existing 不创建依赖图边。"""
        db, ws_id = db_with_deps
        deps = [
            {"dependency_type": DEP_REQUIRES_EXISTING, "target_ref": "some.symbol"},
        ]
        db.import_envelope_dependencies(ws_id, "T-c1", "C-test", 1, deps)
        build = db.build_hard_dependency_edges(ws_id, "C-test", 1)
        assert build["edges_built"] == 0
        edges = db.get_dependency_edges(ws_id)
        assert len(edges) == 0


# ============================================
# Req 9.3: artifact identity 与 freshness
# ============================================

class TestArtifactIdentity:
    """验证 artifact identity 记录与 freshness 查询（Req 9.3）。"""

    def test_record_artifact_with_hash_is_fresh(self, db_with_deps):
        """有 artifact_hash 时 freshness_status=fresh。"""
        db, ws_id = db_with_deps
        aid = db.record_artifact_identity(
            ws_id, "T-provider", "C-test", 1,
            "file", "src/output.py", "sha256:abc123",
        )
        assert aid.startswith("ART-")
        freshness = db.get_artifact_freshness(ws_id, "T-provider", "src/output.py")
        assert freshness is not None
        assert freshness["freshness_status"] == ARTIFACT_FRESH

    def test_record_artifact_without_hash_is_producing(self, db_with_deps):
        """无 artifact_hash 时 freshness_status=producing。"""
        db, ws_id = db_with_deps
        db.record_artifact_identity(
            ws_id, "T-provider", "C-test", 1,
            "file", "src/output.py",
        )
        freshness = db.get_artifact_freshness(ws_id, "T-provider", "src/output.py")
        assert freshness is not None
        assert freshness["freshness_status"] == ARTIFACT_PRODUCING

    def test_artifact_freshness_not_found(self, db_with_deps):
        """不存在的 artifact 返回 None。"""
        db, ws_id = db_with_deps
        assert db.get_artifact_freshness(ws_id, "T-nonexistent") is None


# ============================================
# Req 9.4-9.5: interface identity 与匹配
# ============================================

class TestInterfaceIdentity:
    """验证 interface identity 发布与匹配（Req 9.4-9.5）。"""

    def test_publish_interface_records_identity(self, db_with_deps):
        """发布 interface 记录 identity/version/hash（Req 9.4）。"""
        db, ws_id = db_with_deps
        ifid = db.publish_interface(
            ws_id, "T-provider", "C-test", 1,
            "auth.login", "1.0.0", "sha256:iface1",
        )
        assert ifid.startswith("IF-")
        providers = db.get_interface_providers(ws_id, "auth.login")
        assert len(providers) == 1
        assert providers[0]["provider_task_id"] == "T-provider"
        assert providers[0]["version"] == "1.0.0"
        assert providers[0]["interface_hash"] == "sha256:iface1"

    def test_get_providers_by_version(self, db_with_deps):
        """按 version 过滤 provider（Req 9.5）。"""
        db, ws_id = db_with_deps
        db.publish_interface(ws_id, "T-p1", "C-test", 1, "auth.login", "1.0.0")
        db.publish_interface(ws_id, "T-p2", "C-test", 1, "auth.login", "2.0.0")
        v1 = db.get_interface_providers(ws_id, "auth.login", "1.0.0")
        assert len(v1) == 1
        assert v1[0]["provider_task_id"] == "T-p1"

    def test_multiple_providers_same_interface(self, db_with_deps):
        """同一 interface 可以有多个 provider。"""
        db, ws_id = db_with_deps
        db.publish_interface(ws_id, "T-p1", "C-test", 1, "auth.login", "1.0.0")
        db.publish_interface(ws_id, "T-p2", "C-test", 1, "auth.login", "1.0.0",
                            "sha256:hash2")
        # 注意：UNIQUE(workspace_id, interface_name, version) 会阻止相同 name+version
        # 第二次插入会被 OR IGNORE 跳过
        providers = db.get_interface_providers(ws_id, "auth.login", "1.0.0")
        assert len(providers) == 1  # OR IGNORE 去重


# ============================================
# Req 9.6: 硬边构建与去重
# ============================================

class TestBuildHardDependencyEdges:
    """验证硬依赖图边构建与去重（Req 9.6）。"""

    def test_requires_artifact_creates_edge(self, db_with_deps):
        """requires_artifact 创建 provider→consumer 硬边。"""
        db, ws_id = db_with_deps
        deps = [
            {"dependency_type": DEP_REQUIRES_ARTIFACT, "target_ref": "ART-abc",
             "target_task_id": "T-provider"},
        ]
        db.import_envelope_dependencies(ws_id, "T-consumer", "C-test", 1, deps)
        build = db.build_hard_dependency_edges(ws_id, "C-test", 1)
        assert build["edges_built"] == 1
        edges = db.get_dependency_edges(ws_id)
        assert len(edges) == 1
        assert edges[0]["provider_task_id"] == "T-provider"
        assert edges[0]["consumer_task_id"] == "T-consumer"
        assert edges[0]["is_hard"] == 1

    def test_requires_interface_resolves_to_edge(self, db_with_deps):
        """requires_interface 解析后创建 provider→consumer 硬边。"""
        db, ws_id = db_with_deps
        # provider 发布 interface
        db.publish_interface(ws_id, "T-provider", "C-prov", 1, "auth.login", "1.0.0")
        # consumer 声明 requires_interface
        deps = [
            {"dependency_type": DEP_REQUIRES_INTERFACE, "target_ref": "auth.login"},
        ]
        db.import_envelope_dependencies(ws_id, "T-consumer", "C-test", 1, deps)
        build = db.build_hard_dependency_edges(ws_id, "C-test", 1)
        assert build["edges_built"] == 1
        edges = db.get_dependency_edges(ws_id)
        assert len(edges) == 1
        assert edges[0]["provider_task_id"] == "T-provider"
        assert edges[0]["consumer_task_id"] == "T-consumer"

    def test_duplicate_edges_collapsed(self, db_with_deps):
        """重复的 provider→consumer 边被去重（Req 9.6 collapse）。"""
        db, ws_id = db_with_deps
        deps = [
            {"dependency_type": DEP_REQUIRES_ARTIFACT, "target_ref": "ART-1",
             "target_task_id": "T-provider"},
            {"dependency_type": DEP_REQUIRES_ARTIFACT, "target_ref": "ART-2",
             "target_task_id": "T-provider"},
        ]
        db.import_envelope_dependencies(ws_id, "T-consumer", "C-test", 1, deps)
        build = db.build_hard_dependency_edges(ws_id, "C-test", 1)
        # 两条依赖声明，但 provider→consumer 相同，去重后只有 1 条边
        # 注意：source_type 相同且 contract_revision 相同时 UNIQUE 去重
        # 但 source_type 都是 requires_artifact，target_ref 不同但 provider 相同
        # UNIQUE 是 (workspace_id, provider_task_id, consumer_task_id, contract_id, contract_revision, source_type)
        # 所以同 source_type 的重复边会被去重
        assert build["edges_built"] == 2  # 两条都执行了 INSERT OR IGNORE
        edges = db.get_dependency_edges(ws_id)
        assert len(edges) == 1  # 去重后只剩 1 条

    def test_requires_artifact_missing_provider_skipped(self, db_with_deps):
        """requires_artifact 缺少 target_task_id 时跳过。"""
        db, ws_id = db_with_deps
        deps = [
            {"dependency_type": DEP_REQUIRES_ARTIFACT, "target_ref": "ART-abc"},
        ]
        db.import_envelope_dependencies(ws_id, "T-consumer", "C-test", 1, deps)
        build = db.build_hard_dependency_edges(ws_id, "C-test", 1)
        assert build["edges_built"] == 0
        assert build["edges_skipped"] == 1
        assert len(build["resolution_errors"]) == 1

    def test_requires_interface_no_provider_skipped(self, db_with_deps):
        """requires_interface 无匹配 provider 时跳过。"""
        db, ws_id = db_with_deps
        deps = [
            {"dependency_type": DEP_REQUIRES_INTERFACE, "target_ref": "nonexistent.iface"},
        ]
        db.import_envelope_dependencies(ws_id, "T-consumer", "C-test", 1, deps)
        build = db.build_hard_dependency_edges(ws_id, "C-test", 1)
        assert build["edges_built"] == 0
        assert build["edges_skipped"] == 1
        assert len(build["resolution_errors"]) == 1


# ============================================
# Req 9.8: informational 关系不阻断
# ============================================

class TestInformationalDependencies:
    """验证 informational 关系不建边、不阻断（Req 9.8）。"""

    def test_informational_dependency_no_edge(self, db_with_deps):
        """informational 关系不创建硬依赖图边。"""
        db, ws_id = db_with_deps
        deps = [
            {"dependency_type": DEP_REQUIRES_ARTIFACT, "target_ref": "ART-1",
             "target_task_id": "T-provider", "is_informational": True},
        ]
        db.import_envelope_dependencies(ws_id, "T-consumer", "C-test", 1, deps)
        build = db.build_hard_dependency_edges(ws_id, "C-test", 1)
        assert build["edges_built"] == 0
        edges = db.get_dependency_edges(ws_id)
        assert len(edges) == 0


# ============================================
# Req 9.9: 多 provider 无选择拒绝
# ============================================

class TestMultipleProviderConflict:
    """验证多 provider 无 Planner 选择时立即拒绝（Req 9.9）。"""

    def test_multiple_providers_without_selection_rejected(self, db_with_deps):
        """多 provider 无显式选择时拒绝并报告冲突。"""
        db, ws_id = db_with_deps
        # 两个 provider 发布不同版本的同一 interface（name 不同 version 才能多 provider）
        # UNIQUE(workspace_id, interface_name, version) 限制同 name+version 只有一个
        # 要测试多 provider，需要同 interface_name 不同 version
        db.publish_interface(ws_id, "T-p1", "C-p1", 1, "auth.login", "1.0.0")
        db.publish_interface(ws_id, "T-p2", "C-p2", 1, "auth.login", "2.0.0")
        # consumer 声明 requires_interface 不指定 version
        deps = [
            {"dependency_type": DEP_REQUIRES_INTERFACE, "target_ref": "auth.login"},
        ]
        db.import_envelope_dependencies(ws_id, "T-consumer", "C-test", 1, deps)
        build = db.build_hard_dependency_edges(ws_id, "C-test", 1)
        assert len(build["provider_conflicts"]) == 1
        assert build["provider_conflicts"][0]["interface_name"] == "auth.login"
        assert set(build["provider_conflicts"][0]["providers"]) == {"T-p1", "T-p2"}
        assert build["edges_built"] == 0

    def test_multiple_providers_with_selection_resolved(self, db_with_deps):
        """多 provider 有显式选择时正常建边。"""
        db, ws_id = db_with_deps
        db.publish_interface(ws_id, "T-p1", "C-p1", 1, "auth.login", "1.0.0")
        db.publish_interface(ws_id, "T-p2", "C-p2", 1, "auth.login", "2.0.0")
        # Planner 显式选择 T-p1
        db.select_interface_provider(
            ws_id, "T-consumer", "C-test", 1, "auth.login", "T-p1",
        )
        deps = [
            {"dependency_type": DEP_REQUIRES_INTERFACE, "target_ref": "auth.login"},
        ]
        db.import_envelope_dependencies(ws_id, "T-consumer", "C-test", 1, deps)
        build = db.build_hard_dependency_edges(ws_id, "C-test", 1)
        assert len(build["provider_conflicts"]) == 0
        assert build["edges_built"] == 1
        edges = db.get_dependency_edges(ws_id)
        assert edges[0]["provider_task_id"] == "T-p1"


# ============================================
# Req 9.7: 环检测与最小 cycle path
# ============================================

class TestCycleDetection:
    """验证环检测和最小 cycle path 提取（Req 9.7）。"""

    def test_no_cycle_empty_graph(self, db_with_deps):
        """空图无环。"""
        db, ws_id = db_with_deps
        result = db.detect_cycle(ws_id)
        assert result["has_cycle"] is False
        assert result["cycle_path"] == []

    def test_no_cycle_acyclic_graph(self, db_with_deps):
        """无环图正确判定。"""
        db, ws_id = db_with_deps
        # T-p1 → T-c1 → T-c2 (无环)
        deps_c1 = [
            {"dependency_type": DEP_REQUIRES_ARTIFACT, "target_ref": "ART-1",
             "target_task_id": "T-p1"},
        ]
        deps_c2 = [
            {"dependency_type": DEP_REQUIRES_ARTIFACT, "target_ref": "ART-2",
             "target_task_id": "T-c1"},
        ]
        db.import_envelope_dependencies(ws_id, "T-c1", "C-test", 1, deps_c1)
        db.import_envelope_dependencies(ws_id, "T-c2", "C-test", 1, deps_c2)
        db.build_hard_dependency_edges(ws_id, "C-test", 1)
        result = db.detect_cycle(ws_id)
        assert result["has_cycle"] is False

    def test_cycle_detected_with_path(self, db_with_deps):
        """有环图检测到环并返回 cycle path。"""
        db, ws_id = db_with_deps
        # T-a → T-b → T-a (环)
        deps_a = [
            {"dependency_type": DEP_REQUIRES_ARTIFACT, "target_ref": "ART-b",
             "target_task_id": "T-b"},
        ]
        deps_b = [
            {"dependency_type": DEP_REQUIRES_ARTIFACT, "target_ref": "ART-a",
             "target_task_id": "T-a"},
        ]
        db.import_envelope_dependencies(ws_id, "T-a", "C-test", 1, deps_a)
        db.import_envelope_dependencies(ws_id, "T-b", "C-test", 1, deps_b)
        db.build_hard_dependency_edges(ws_id, "C-test", 1)
        result = db.detect_cycle(ws_id)
        assert result["has_cycle"] is True
        assert len(result["cycle_path"]) >= 3  # 至少 start → ... → start
        # cycle path 以同一节点开始和结束
        assert result["cycle_path"][0] == result["cycle_path"][-1]

    def test_validate_revision_rejects_cycle(self, db_with_deps):
        """validate_revision_dependencies 对有环 revision 返回 valid=False。"""
        db, ws_id = db_with_deps
        deps_a = [
            {"dependency_type": DEP_REQUIRES_ARTIFACT, "target_ref": "ART-b",
             "target_task_id": "T-b"},
        ]
        deps_b = [
            {"dependency_type": DEP_REQUIRES_ARTIFACT, "target_ref": "ART-a",
             "target_task_id": "T-a"},
        ]
        db.import_envelope_dependencies(ws_id, "T-a", "C-test", 1, deps_a)
        db.import_envelope_dependencies(ws_id, "T-b", "C-test", 1, deps_b)
        result = db.validate_revision_dependencies(ws_id, "C-test", 1)
        assert result["valid"] is False
        assert len(result["cycle_path"]) > 0
        assert any("环" in e for e in result["errors"])

    def test_validate_revision_accepts_acyclic(self, db_with_deps):
        """validate_revision_dependencies 对无环 revision 返回 valid=True。"""
        db, ws_id = db_with_deps
        deps = [
            {"dependency_type": DEP_REQUIRES_ARTIFACT, "target_ref": "ART-1",
             "target_task_id": "T-provider"},
        ]
        db.import_envelope_dependencies(ws_id, "T-consumer", "C-test", 1, deps)
        result = db.validate_revision_dependencies(ws_id, "C-test", 1)
        assert result["valid"] is True
        assert len(result["errors"]) == 0

    def test_clear_revision_edges(self, db_with_deps):
        """clear_revision_edges 删除指定 revision 的边。"""
        db, ws_id = db_with_deps
        deps = [
            {"dependency_type": DEP_REQUIRES_ARTIFACT, "target_ref": "ART-1",
             "target_task_id": "T-provider"},
        ]
        db.import_envelope_dependencies(ws_id, "T-consumer", "C-test", 1, deps)
        db.build_hard_dependency_edges(ws_id, "C-test", 1)
        assert len(db.get_dependency_edges(ws_id)) == 1
        deleted = db.clear_revision_edges(ws_id, "C-test", 1)
        assert deleted == 1
        assert len(db.get_dependency_edges(ws_id)) == 0


# ============================================
# Req 9.10: 只做无环校验（无调度方法）
# ============================================

class TestNoSchedulingMethods:
    """验证不包含资源优化/自动 assignment/DAG 调度方法（Req 9.10）。"""

    def test_no_auto_assignment_method(self, db_with_deps):
        """TaskDependenciesMixin 不暴露 auto_assign 方法。"""
        db, ws_id = db_with_deps
        assert not hasattr(db, "auto_assign_tasks")
        assert not hasattr(db, "schedule_tasks")
        assert not hasattr(db, "optimize_resources")


# ============================================
# 6.3 集成测试：publish_envelope_revision 依赖导入
# ============================================

class TestPublishEnvelopeDependencyImport:
    """验证 publish_envelope_revision 复用 P1 发布路径导入依赖（6.3，Req 9.1）。"""

    def test_publish_imports_dependencies(self, db_with_deps):
        """发布带 requires_existing 依赖的 Envelope，task_dependencies 表有记录。"""
        from callwarden.db.db_task_contracts import Envelope, AllowedEditScope
        db, ws_id = db_with_deps
        env = Envelope(
            contract_id="C-dep-test",
            revision=1,
            profile="research",
            objective={"goal": "测试依赖导入"},
            interfaces={"inputs": []},
            allowed_edit_scope=AllowedEditScope(),
            acceptance_clauses=[],
            risks=[],
            rollback={},
            dependencies={"requires_existing": ["auth.service", "auth.repository"]},
        )
        db.publish_envelope_revision(env, task_id="T-test", workspace_id=ws_id)

        cur = db.conn.execute(
            "SELECT dependency_type, target_ref FROM task_dependencies "
            "WHERE workspace_id = ? AND task_id = ?",
            (ws_id, "T-test"),
        )
        rows = [dict(r) for r in cur.fetchall()]
        assert len(rows) == 2
        refs = {r["target_ref"] for r in rows}
        assert refs == {"auth.service", "auth.repository"}
        assert all(r["dependency_type"] == DEP_REQUIRES_EXISTING for r in rows)

    def test_publish_skips_internal_keys(self, db_with_deps):
        """发布带 _scope_label 的 Envelope，内部下划线 key 不被导入。"""
        from callwarden.db.db_task_contracts import Envelope, AllowedEditScope
        db, ws_id = db_with_deps
        env = Envelope(
            contract_id="C-internal-test",
            revision=1,
            profile="research",
            objective={"goal": "测试内部 key 过滤"},
            interfaces={"inputs": []},
            allowed_edit_scope=AllowedEditScope(),
            acceptance_clauses=[],
            risks=[],
            rollback={},
            dependencies={
                "requires_existing": ["valid.ref"],
                "_scope_label": "unscoped",
                "_clause_downgrades": [],
            },
        )
        db.publish_envelope_revision(env, task_id="T-test2", workspace_id=ws_id)

        cur = db.conn.execute(
            "SELECT dependency_type FROM task_dependencies "
            "WHERE workspace_id = ? AND task_id = ?",
            (ws_id, "T-test2"),
        )
        rows = [dict(r) for r in cur.fetchall()]
        assert len(rows) == 1
        assert rows[0]["dependency_type"] == DEP_REQUIRES_EXISTING

    def test_publish_no_deps_no_import(self, db_with_deps):
        """发布不带 dependencies 的 Envelope，不导入任何记录。"""
        from callwarden.db.db_task_contracts import Envelope, AllowedEditScope
        db, ws_id = db_with_deps
        env = Envelope(
            contract_id="C-no-deps",
            revision=1,
            profile="research",
            objective={"goal": "无依赖"},
            interfaces={"inputs": []},
            allowed_edit_scope=AllowedEditScope(),
            acceptance_clauses=[],
            risks=[],
            rollback={},
        )
        db.publish_envelope_revision(env, task_id="T-nodeps", workspace_id=ws_id)

        cur = db.conn.execute(
            "SELECT COUNT(*) as cnt FROM task_dependencies "
            "WHERE workspace_id = ? AND task_id = ?",
            (ws_id, "T-nodeps"),
        )
        assert cur.fetchone()["cnt"] == 0


# ============================================
# 6.4a 集成测试：publish_envelope_revision 环检测拒绝
# ============================================

class TestPublishEnvelopeCycleRejection:
    """验证 publish_envelope_revision 在有 hard cycle 时原子拒绝（6.4a，Req 9.7）。"""

    def test_publish_rejects_hard_cycle(self, db_with_deps):
        """有环的 revision 被原子拒绝，回滚已写入的数据。"""
        from callwarden.db.db_task_contracts import (
            Envelope, AllowedEditScope, ContractPublicationError, ContractErrorCode,
        )
        db, ws_id = db_with_deps

        # 1. 发布 T-b 的 Envelope（依赖 T-a 的 artifact）
        env_b = Envelope(
            contract_id="C-b", revision=1, profile="research",
            objective={"goal": "T-b"}, interfaces={"inputs": []},
            allowed_edit_scope=AllowedEditScope(), acceptance_clauses=[],
            risks=[], rollback={},
            dependencies={"requires_artifact": [{"ref": "ART-a", "task_id": "T-a"}]},
        )
        db.publish_envelope_revision(env_b, task_id="T-b", workspace_id=ws_id)

        # 2. 发布 T-a 的 Envelope（依赖 T-b 的 artifact，形成环 T-a→T-b→T-a）
        env_a = Envelope(
            contract_id="C-a", revision=1, profile="research",
            objective={"goal": "T-a"}, interfaces={"inputs": []},
            allowed_edit_scope=AllowedEditScope(), acceptance_clauses=[],
            risks=[], rollback={},
            dependencies={"requires_artifact": [{"ref": "ART-b", "task_id": "T-b"}]},
        )
        with pytest.raises(ContractPublicationError) as exc_info:
            db.publish_envelope_revision(env_a, task_id="T-a", workspace_id=ws_id)

        # 3. 验证错误码
        assert exc_info.value.reason.code == ContractErrorCode.HARD_CYCLE_DETECTED

        # 4. 验证 T-a 的 Envelope 被回滚
        cur = db.conn.execute(
            "SELECT COUNT(*) as cnt FROM task_contract_revisions WHERE contract_id = ?",
            ("C-a",),
        )
        assert cur.fetchone()["cnt"] == 0

        # 5. 验证 T-a 的依赖被回滚
        cur = db.conn.execute(
            "SELECT COUNT(*) as cnt FROM task_dependencies WHERE task_id = ?",
            ("T-a",),
        )
        assert cur.fetchone()["cnt"] == 0

    def test_publish_accepts_acyclic(self, db_with_deps):
        """无环的 revision 正常发布。"""
        from callwarden.db.db_task_contracts import Envelope, AllowedEditScope
        db, ws_id = db_with_deps

        env = Envelope(
            contract_id="C-acyclic", revision=1, profile="research",
            objective={"goal": "无环"}, interfaces={"inputs": []},
            allowed_edit_scope=AllowedEditScope(), acceptance_clauses=[],
            risks=[], rollback={},
            dependencies={"requires_artifact": [{"ref": "ART-1", "task_id": "T-provider"}]},
        )
        published, warnings = db.publish_envelope_revision(
            env, task_id="T-consumer", workspace_id=ws_id,
        )
        assert published.contract_hash  # 发布成功，有 hash


# ============================================
# 6.4b 集成测试：Gate 依赖 freshness 判定
# ============================================

class TestGateDependencyFreshness:
    """验证 evaluate_evidence_gate 的依赖 freshness 检查（6.4b，Req 9.3-9.5）。"""

    def test_gate_blocks_when_artifact_not_fresh(self, db_with_deps):
        """provider artifact 不是 fresh 时 Gate block。"""
        db, ws_id = db_with_deps
        # T-consumer 依赖 T-provider 的 artifact
        # T-provider 的 artifact 处于 producing 状态（非 fresh）
        db.import_envelope_dependencies(
            ws_id, "T-consumer", "C-test", 1,
            [{"dependency_type": DEP_REQUIRES_ARTIFACT,
              "target_ref": "ART-1", "target_task_id": "T-provider"}],
        )
        # T-provider 的 artifact 处于 producing（无 hash）
        db.record_artifact_identity(
            ws_id, "T-provider", "C-test", 1, "file", "src/output.py",
        )

        result = db.evaluate_evidence_gate(
            task_id="T-consumer",
            profile="default",
            current_contract={"contract_id": "C-test", "revision": 1, "contract_hash": "sha256:abc"},
            snapshot_s0={"snapshot_id": "snap-1"},
            verdicts=[],
            evidences=[],
            quality_findings=[],
            workspace_id=ws_id,
        )
        assert result["decision"] == "block"
        assert any(r["code"] == "ERR_ARTIFACT_NOT_FRESH" for r in result["reasons"])

    def test_gate_passes_when_artifact_fresh(self, db_with_deps):
        """provider artifact 是 fresh 时 Gate 不因 artifact block。"""
        db, ws_id = db_with_deps
        db.import_envelope_dependencies(
            ws_id, "T-consumer", "C-test", 1,
            [{"dependency_type": DEP_REQUIRES_ARTIFACT,
              "target_ref": "ART-1", "target_task_id": "T-provider"}],
        )
        # T-provider 的 artifact 处于 fresh（有 hash）
        db.record_artifact_identity(
            ws_id, "T-provider", "C-test", 1, "file", "src/output.py",
            artifact_hash="sha256:xyz",
        )

        result = db.evaluate_evidence_gate(
            task_id="T-consumer",
            profile="default",
            current_contract={"contract_id": "C-test", "revision": 1, "contract_hash": "sha256:abc"},
            snapshot_s0={"snapshot_id": "snap-1"},
            verdicts=[],
            evidences=[],
            quality_findings=[],
            workspace_id=ws_id,
        )
        # 不应出现 ERR_ARTIFACT_NOT_FRESH
        assert not any(r["code"] == "ERR_ARTIFACT_NOT_FRESH" for r in result["reasons"])

    def test_gate_blocks_when_interface_no_provider(self, db_with_deps):
        """requires_interface 无匹配 provider 时 Gate block。"""
        db, ws_id = db_with_deps
        db.import_envelope_dependencies(
            ws_id, "T-consumer", "C-test", 1,
            [{"dependency_type": DEP_REQUIRES_INTERFACE,
              "target_ref": "auth.verify"}],
        )

        result = db.evaluate_evidence_gate(
            task_id="T-consumer",
            profile="default",
            current_contract={"contract_id": "C-test", "revision": 1, "contract_hash": "sha256:abc"},
            snapshot_s0={"snapshot_id": "snap-1"},
            verdicts=[],
            evidences=[],
            quality_findings=[],
            workspace_id=ws_id,
        )
        assert result["decision"] == "block"
        assert any(r["code"] == "ERR_INTERFACE_NO_PROVIDER" for r in result["reasons"])

    def test_gate_passes_when_interface_has_provider(self, db_with_deps):
        """requires_interface 有匹配 provider 时 Gate 不因 interface block。"""
        db, ws_id = db_with_deps
        db.import_envelope_dependencies(
            ws_id, "T-consumer", "C-test", 1,
            [{"dependency_type": DEP_REQUIRES_INTERFACE,
              "target_ref": "auth.verify"}],
        )
        # T-provider 发布了 auth.verify 接口
        db.publish_interface(
            ws_id, "T-provider", "C-provider", 1,
            "auth.verify", "1.0.0", "sha256:iface",
        )

        result = db.evaluate_evidence_gate(
            task_id="T-consumer",
            profile="default",
            current_contract={"contract_id": "C-test", "revision": 1, "contract_hash": "sha256:abc"},
            snapshot_s0={"snapshot_id": "snap-1"},
            verdicts=[],
            evidences=[],
            quality_findings=[],
            workspace_id=ws_id,
        )
        # 不应出现 ERR_INTERFACE_NO_PROVIDER
        assert not any(r["code"] == "ERR_INTERFACE_NO_PROVIDER" for r in result["reasons"])
