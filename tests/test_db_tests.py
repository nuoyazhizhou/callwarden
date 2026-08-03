"""P1 任务 4.7：测试运行绑定 Evidence 与历史 PASS 隔离单元测试。

验证 TestRelationMixin 的 P1 扩展（Requirements 1.3, 6.4–6.5, 6.11–6.12, 7.1–7.3）：
- import_test_results 扩展 binding_context：提供时生成唯一 run_id 并追加 Evidence
- import_test_results 无 binding_context：旧记录标记为 historical_unbound（Req 7.2）
- record_bound_test_run：新 run 绑定 contract/snapshot/verifier 并追加 Evidence（Req 7.1, 7.3）
- get_test_run_evidence_status：通过 derive_freshness 派生状态（Req 6.4–6.5, 6.11–6.12）
- list_historical_unbound_runs：查询无 Evidence 绑定的历史记录（Req 7.2）
- get_run_evidence_binding：通过 run_id 查询关联的 evidence_id 和契约信息（Req 7.1, 7.3）

关联契约：docs/design/requirements.md Requirement 1.3 / 6.4–6.5 / 6.11–6.12 / 7.1–7.3。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

# 仓库父目录加入 sys.path（与 test_db_task_contracts.py 同模式）
_PKG_PARENT = str(Path(__file__).resolve().parents[1].parent)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db import CodeGraphDB
from callwarden.db.db_task_evidence import (
    EVIDENCE_TYPE_TEST_RUN,
    FRESHNESS_FRESH,
    FRESHNESS_INVALID,
    FRESHNESS_STALE,
    FRESHNESS_SUPERSEDED,
)
from callwarden.db.task_snapshot import WorkspaceSnapshot


# ============================================
# Fixtures
# ============================================

@pytest.fixture
def db_with_tests(tmp_path):
    """创建一个带 test_runs / task_evidence_events / verifier_registry 表的临时数据库。

    关闭外键检查：test_runs.test_fn_id REFERENCES symbols.id，但本测试套件
    验证 TestRelationMixin 的 P1 扩展行为，不是外键约束语义；测试 test_fn_id
    不对应真实 symbols 记录。CW_USE_RUST_STORAGE=1（默认）时 PRAGMA foreign_keys=ON，
    需显式关闭以避免 FOREIGN KEY constraint failed。
    """
    db_path = str(tmp_path / "test_db_tests.db")
    db = CodeGraphDB(db_path, workspace_root=str(tmp_path))
    db.register_workspace("test", str(tmp_path), "测试")
    db.conn.execute("PRAGMA foreign_keys=OFF")
    yield db
    db.close()


def _make_snapshot(
    head_commit: str = "abc123",
    file_hashes: dict | None = None,
    symbol_hashes: dict | None = None,
    graph_refresh_version: str = "v1",
) -> WorkspaceSnapshot:
    """构造一个 WorkspaceSnapshot 用于绑定。"""
    snap = WorkspaceSnapshot(
        head_commit=head_commit,
        dirty_diff_hash="sha256:dirty",
        file_hashes=file_hashes or {"src/auth.py": "sha256:file1"},
        symbol_hashes=symbol_hashes or {"auth.login": "sha256:sym1"},
        graph_refresh_version=graph_refresh_version,
    )
    # 计算 snapshot_id（与生产路径一致）
    from callwarden.db.task_snapshot import compute_snapshot_id
    snap.snapshot_id = compute_snapshot_id(snap)
    return snap


def _make_binding_context(
    snapshot: WorkspaceSnapshot | None = None,
    task_id: str = "T-test-001",
    contract_id: str = "C-test",
    contract_revision: int = 1,
    contract_hash: str = "sha256:contract1",
    verifier_name: str = "pytest",
    verifier_version: str = "8.x",
    verifier_config_hash: str = "sha256:vcfg1",
) -> dict:
    """构造一个完整的 binding_context 字典。"""
    return {
        "task_id": task_id,
        "contract_id": contract_id,
        "contract_revision": contract_revision,
        "contract_hash": contract_hash,
        "snapshot": snapshot or _make_snapshot(),
        "verifier_name": verifier_name,
        "verifier_version": verifier_version,
        "verifier_config_hash": verifier_config_hash,
        "selectors": ["tests/test_auth.py"],
        "producer_identity": "test-session",
    }


_JUNIT_XML_PASSED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="suite" tests="2" failures="0" errors="0" skipped="0">
    <testcase classname="test_auth" name="test_login" time="0.1"/>
    <testcase classname="test_auth" name="test_logout" time="0.05"/>
  </testsuite>
</testsuites>"""

_JUNIT_XML_MIXED = """<?xml version="1.0" encoding="utf-8"?>
<testsuites>
  <testsuite name="suite" tests="3" failures="1" errors="0" skipped="1">
    <testcase classname="test_auth" name="test_login" time="0.1"/>
    <testcase classname="test_auth" name="test_logout" time="0.2">
      <failure type="AssertionError">assert false</failure>
    </testcase>
    <testcase classname="test_auth" name="test_skip" time="0.0">
      <skipped message="not relevant"/>
    </testcase>
  </testsuite>
</testsuites>"""


# ============================================
# import_test_results 无 binding_context（Req 7.2: historical_unbound）
# ============================================

class TestImportTestResultsHistoricalUnbound:
    """验证无 binding_context 时按历史记录导入。"""

    def test_no_binding_returns_basic_stats(self, db_with_tests):
        """无 binding_context 时返回基础统计，无 run_id/evidence_id 字段。"""
        stats = db_with_tests.import_test_results(_JUNIT_XML_PASSED)
        assert stats["total"] == 2
        assert stats["passed"] == 2
        assert "run_id" not in stats
        assert "evidence_id" not in stats
        assert "binding" not in stats

    def test_no_binding_records_are_historical_unbound(self, db_with_tests):
        """无 binding_context 导入的记录可通过 list_historical_unbound_runs 查到。"""
        db_with_tests.import_test_results(
            _JUNIT_XML_PASSED, ci_run_id="ci-old-run-001"
        )
        unbound = db_with_tests.list_historical_unbound_runs(limit=100)
        assert len(unbound) == 2
        for record in unbound:
            assert record["freshness_status"] == "historical_unbound"
            # 旧记录的 ci_run_id 不以 TRUN- 开头
            assert not record["ci_run_id"].startswith("TRUN-")

    def test_empty_ci_run_id_also_historical(self, db_with_tests):
        """ci_run_id 为空时也算 historical_unbound。"""
        db_with_tests.import_test_results(_JUNIT_XML_PASSED)
        unbound = db_with_tests.list_historical_unbound_runs(limit=100)
        assert len(unbound) == 2


# ============================================
# import_test_results 有 binding_context（Req 7.1, 7.3）
# ============================================

class TestImportTestResultsBound:
    """验证有 binding_context 时新 run 绑定 Evidence。"""

    def test_binding_generates_unique_run_id(self, db_with_tests):
        """有 binding_context 时生成 TRUN- 前缀的唯一 run_id。"""
        ctx = _make_binding_context()
        stats = db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)
        assert stats["total"] == 2
        assert "run_id" in stats
        assert stats["run_id"].startswith("TRUN-")
        assert stats["binding"] == "fresh"

    def test_binding_appends_evidence(self, db_with_tests):
        """有 binding_context 时追加一条 Evidence。"""
        ctx = _make_binding_context()
        stats = db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)
        assert stats["evidence_appended"] is True
        assert stats["evidence_id"].startswith("E-")
        # Evidence 落库校验
        evidence = db_with_tests.get_evidence(stats["evidence_id"])
        assert evidence is not None
        assert evidence["evidence_type"] == EVIDENCE_TYPE_TEST_RUN
        assert evidence["task_id"] == ctx["task_id"]
        assert evidence["contract_id"] == ctx["contract_id"]
        assert evidence["contract_revision"] == ctx["contract_revision"]
        assert evidence["verifier_name"] == ctx["verifier_name"]

    def test_binding_writes_evidence_id_back_to_test_runs(self, db_with_tests):
        """Evidence 追加成功后将 evidence_id 写回 test_runs.ci_url 字段。"""
        ctx = _make_binding_context()
        stats = db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)
        run_id = stats["run_id"]
        evidence_id = stats["evidence_id"]

        binding = db_with_tests.get_run_evidence_binding(run_id)
        assert binding["binding_status"] == "fresh"
        assert binding["evidence_id"] == evidence_id
        assert binding["contract_id"] == ctx["contract_id"]

    def test_binding_missing_required_fields_falls_back(self, db_with_tests):
        """binding_context 缺必填字段时降级为 historical_unbound。"""
        ctx = _make_binding_context()
        # 删除必填字段
        del ctx["contract_hash"]
        stats = db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)
        # run_id 仍然生成（在调用 _append_test_run_evidence 之前）
        assert "run_id" in stats
        assert stats["evidence_appended"] is False
        assert stats["binding"] == "historical_unbound"
        assert "evidence_error" in stats

    def test_binding_with_non_workspace_snapshot_falls_back(self, db_with_tests):
        """binding_context.snapshot 不是 WorkspaceSnapshot 时降级。"""
        ctx = _make_binding_context()
        ctx["snapshot"] = {"invalid": "not a snapshot"}
        stats = db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)
        assert stats["evidence_appended"] is False
        assert stats["binding"] == "historical_unbound"

    def test_binding_mixed_results_still_appends_evidence(self, db_with_tests):
        """有失败/跳过的测试运行也能追加 Evidence。"""
        ctx = _make_binding_context()
        stats = db_with_tests.import_test_results(_JUNIT_XML_MIXED, binding_context=ctx)
        assert stats["total"] == 3
        assert stats["passed"] == 1
        assert stats["failed"] == 1
        assert stats["skipped"] == 1
        assert stats["evidence_appended"] is True
        assert stats["binding"] == "fresh"

    def test_multiple_runs_have_distinct_run_ids(self, db_with_tests):
        """多次导入生成不同的 run_id。"""
        ctx = _make_binding_context()
        stats1 = db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)
        stats2 = db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)
        assert stats1["run_id"] != stats2["run_id"]
        assert stats1["evidence_id"] != stats2["evidence_id"]


# ============================================
# record_bound_test_run（Req 1.3, 7.1–7.3）
# ============================================

class TestRecordBoundTestRun:
    """验证 record_bound_test_run 直接 API。"""

    def test_returns_fresh_binding(self, db_with_tests):
        """完整参数返回 binding=fresh 和 evidence_id。"""
        snapshot = _make_snapshot()
        result = db_with_tests.record_bound_test_run(
            task_id="T-test-002",
            contract_id="C-record",
            contract_revision=1,
            contract_hash="sha256:contract-record",
            snapshot=snapshot,
            verifier_name="pytest",
            verifier_version="8.x",
            verifier_config_hash="sha256:vcfg-record",
            test_results=[
                {"test_name": "test_a", "status": "passed", "duration_ms": 10.0},
                {"test_name": "test_b", "status": "failed", "duration_ms": 20.0},
            ],
            selectors=["tests/test_a.py"],
            producer_identity="agent-session-1",
        )
        assert result["run_id"].startswith("TRUN-")
        assert result["evidence_appended"] is True
        assert result["binding"] == "fresh"
        assert result["evidence_id"].startswith("E-")
        assert "error" not in result or result["error"] == ""

    def test_non_workspace_snapshot_falls_back(self, db_with_tests):
        """snapshot 非 WorkspaceSnapshot 时降级为 historical_unbound。"""
        result = db_with_tests.record_bound_test_run(
            task_id="T-test-003",
            contract_id="C-bad",
            contract_revision=1,
            contract_hash="sha256:bad",
            snapshot="not-a-snapshot",  # type: ignore[arg-type]
            verifier_name="pytest",
            verifier_version="8.x",
            verifier_config_hash="sha256:bad",
            test_results=[],
        )
        assert result["binding"] == "historical_unbound"
        assert result["evidence_appended"] is False
        assert result["evidence_id"] is None
        assert "snapshot must be WorkspaceSnapshot" in result["error"]


# ============================================
# get_test_run_evidence_status（Req 6.4–6.5, 6.11–6.12）
# ============================================

class TestGetTestRunEvidenceStatus:
    """验证 Evidence Freshness 派生。"""

    def test_empty_evidence_id_returns_historical_unbound(self, db_with_tests):
        """空 evidence_id 返回 historical_unbound，不反向推断。"""
        status, reason = db_with_tests.get_test_run_evidence_status(
            evidence_id="",
            current_contract_revision=1,
        )
        assert status == "historical_unbound"
        assert reason is None

    def test_nonexistent_evidence_returns_invalid(self, db_with_tests):
        """不存在的 evidence_id 返回 invalid。"""
        status, reason = db_with_tests.get_test_run_evidence_status(
            evidence_id="E-nonexistent",
            current_contract_revision=1,
        )
        assert status == FRESHNESS_INVALID
        assert reason is not None

    def test_fresh_when_verifier_registered_and_unchanged(self, db_with_tests):
        """verifier 已注册且无变化时返回 fresh。"""
        # 注册 verifier
        db_with_tests.register_verifier(
            name="pytest",
            version="8.x",
            config_hash="sha256:vcfg1",
            registered_by="test",
        )
        # 追加 Evidence
        ctx = _make_binding_context()
        stats = db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)
        evidence_id = stats["evidence_id"]

        # 当前契约 revision 与绑定一致，snapshot 一致 → fresh
        current_snapshot = ctx["snapshot"]
        status, reason = db_with_tests.get_test_run_evidence_status(
            evidence_id=evidence_id,
            current_contract_revision=1,
            current_snapshot=current_snapshot,
            current_file_hashes=current_snapshot.file_hashes,
            current_symbol_hashes=current_snapshot.symbol_hashes,
            current_graph_version=current_snapshot.graph_refresh_version,
        )
        assert status == FRESHNESS_FRESH
        assert reason is None

    def test_superseded_when_contract_revision_advances(self, db_with_tests):
        """契约 revision 前进时返回 superseded。"""
        db_with_tests.register_verifier(
            name="pytest", version="8.x", config_hash="sha256:vcfg1"
        )
        ctx = _make_binding_context()
        stats = db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)
        evidence_id = stats["evidence_id"]

        # 当前 revision=2 > 绑定 revision=1 → superseded
        status, reason = db_with_tests.get_test_run_evidence_status(
            evidence_id=evidence_id,
            current_contract_revision=2,
        )
        assert status == FRESHNESS_SUPERSEDED

    def test_invalid_when_verifier_not_registered(self, db_with_tests):
        """verifier 未注册时返回 invalid（Req 6.11）。"""
        ctx = _make_binding_context()
        stats = db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)
        evidence_id = stats["evidence_id"]

        # 未注册 verifier → invalid
        status, reason = db_with_tests.get_test_run_evidence_status(
            evidence_id=evidence_id,
            current_contract_revision=1,
        )
        assert status == FRESHNESS_INVALID
        assert reason is not None

    def test_stale_when_file_hash_changes(self, db_with_tests):
        """文件 hash 变化时返回 stale（Req 6.4）。"""
        db_with_tests.register_verifier(
            name="pytest", version="8.x", config_hash="sha256:vcfg1"
        )
        ctx = _make_binding_context()
        stats = db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)
        evidence_id = stats["evidence_id"]

        bound_snapshot = ctx["snapshot"]
        # 改变文件 hash
        changed_file_hashes = {
            path: "sha256:changed" for path in bound_snapshot.file_hashes
        }
        status, reason = db_with_tests.get_test_run_evidence_status(
            evidence_id=evidence_id,
            current_contract_revision=1,
            current_snapshot=bound_snapshot,
            current_file_hashes=changed_file_hashes,
            current_symbol_hashes=bound_snapshot.symbol_hashes,
            current_graph_version=bound_snapshot.graph_refresh_version,
        )
        assert status == FRESHNESS_STALE


# ============================================
# list_historical_unbound_runs（Req 7.2）
# ============================================

class TestListHistoricalUnboundRuns:
    """验证历史未绑定记录查询。"""

    def test_returns_only_non_trun_records(self, db_with_tests):
        """只返回 ci_run_id 不以 TRUN- 开头的记录。"""
        # 旧记录（无 binding_context）
        db_with_tests.import_test_results(
            _JUNIT_XML_PASSED, ci_run_id="ci-old-001"
        )
        # 新记录（有 binding_context）
        ctx = _make_binding_context()
        db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)

        unbound = db_with_tests.list_historical_unbound_runs(limit=100)
        # 只有旧记录（2 条）应该返回
        assert len(unbound) == 2
        for record in unbound:
            assert record["freshness_status"] == "historical_unbound"
            assert record["ci_run_id"] == "ci-old-001"

    def test_limit_parameter(self, db_with_tests):
        """limit 参数限制返回数量。"""
        # 导入多条旧记录
        for i in range(3):
            db_with_tests.import_test_results(
                _JUNIT_XML_PASSED, ci_run_id=f"ci-old-{i}"
            )
        unbound = db_with_tests.list_historical_unbound_runs(limit=3)
        assert len(unbound) <= 3

    def test_empty_when_all_bound(self, db_with_tests):
        """全部为新记录时返回空列表。"""
        ctx = _make_binding_context()
        db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)
        unbound = db_with_tests.list_historical_unbound_runs(limit=100)
        assert unbound == []


# ============================================
# get_run_evidence_binding（Req 7.1, 7.3）
# ============================================

class TestGetRunEvidenceBinding:
    """验证 run_id → evidence_id 绑定查询。"""

    def test_non_trun_run_id_returns_historical_unbound(self, db_with_tests):
        """非 TRUN- 前缀的 run_id 直接返回 historical_unbound，不反向推断。"""
        result = db_with_tests.get_run_evidence_binding("ci-old-001")
        assert result["binding_status"] == "historical_unbound"
        assert "evidence_id" not in result

    def test_empty_run_id_returns_historical_unbound(self, db_with_tests):
        """空 run_id 返回 historical_unbound。"""
        result = db_with_tests.get_run_evidence_binding("")
        assert result["binding_status"] == "historical_unbound"

    def test_trun_run_id_returns_binding_details(self, db_with_tests):
        """TRUN- run_id 返回完整的绑定详情。"""
        ctx = _make_binding_context()
        stats = db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)
        run_id = stats["run_id"]
        evidence_id = stats["evidence_id"]

        binding = db_with_tests.get_run_evidence_binding(run_id)
        assert binding["binding_status"] == "fresh"
        assert binding["evidence_id"] == evidence_id
        assert binding["contract_id"] == ctx["contract_id"]
        assert binding["contract_revision"] == ctx["contract_revision"]
        assert binding["contract_hash"] == ctx["contract_hash"]
        assert binding["verifier_name"] == ctx["verifier_name"]
        assert binding["verifier_version"] == ctx["verifier_version"]
        assert binding["verifier_config_hash"] == ctx["verifier_config_hash"]

    def test_trun_run_id_without_evidence_returns_historical(self, db_with_tests):
        """TRUN- run_id 但无对应 evidence 时返回 historical_unbound。

        场景：binding_context 提供但 evidence 追加失败（如缺必填字段），
        此时 test_runs.ci_url 不会被写入 evidence_id。
        """
        ctx = _make_binding_context()
        del ctx["contract_hash"]  # 触发降级
        stats = db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)
        run_id = stats["run_id"]

        binding = db_with_tests.get_run_evidence_binding(run_id)
        assert binding["binding_status"] == "historical_unbound"


# ============================================
# 不反向推断旧记录绑定（Req 7.3 关键约束）
# ============================================

class TestNoRetroactiveInference:
    """验证旧记录不会被反向推断绑定。"""

    def test_old_records_never_become_fresh(self, db_with_tests):
        """旧记录即使后追加了新 Evidence 也不会被标记为 fresh。"""
        # 1. 先导入旧记录
        db_with_tests.import_test_results(
            _JUNIT_XML_PASSED, ci_run_id="ci-old-001"
        )
        # 2. 后导入新绑定记录
        ctx = _make_binding_context()
        db_with_tests.import_test_results(_JUNIT_XML_PASSED, binding_context=ctx)

        # 旧记录仍应 historical_unbound
        unbound = db_with_tests.list_historical_unbound_runs(limit=100)
        assert len(unbound) == 2
        for record in unbound:
            assert record["freshness_status"] == "historical_unbound"

        # 旧 run_id 查询不应返回 fresh 绑定
        old_binding = db_with_tests.get_run_evidence_binding("ci-old-001")
        assert old_binding["binding_status"] == "historical_unbound"

    def test_old_ci_run_id_not_promoted_to_trun(self, db_with_tests):
        """旧 ci_run_id 不会被改写为 TRUN- 前缀。"""
        db_with_tests.import_test_results(
            _JUNIT_XML_PASSED, ci_run_id="ci-old-keep-id"
        )
        unbound = db_with_tests.list_historical_unbound_runs(limit=100)
        for record in unbound:
            assert record["ci_run_id"] == "ci-old-keep-id"
            assert not record["ci_run_id"].startswith("TRUN-")
