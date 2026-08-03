"""P1 任务 4.2：Canonical Envelope / profile 校验 / revision 发布与 hash 单元测试。

验证 TaskContractsMixin 与模块级函数的核心契约行为（Requirements 1.1 / 2.1–2.11 /
5.4 / 7.4 / 7.9 / 7.11–7.16）：
- Envelope_Parser / Envelope_Printer：结构化 ↔ Canonical UTF-8 字节
- Contract_Hash：排除 hash 自身与纯展示字段；语义变更改变 hash
- profile 校验：5 个合法 profile，必填字段缺失 fail closed
- declarative / executable clause 分类：executable 缺字段降级为 declarative
- publish_envelope_revision：revision 单调递增、空 scope 三分支、不可变 append-only
- 结构化错误/警告码：稳定码 + i18n key

关联契约：docs/design/requirements.md Requirement 2 / 7。
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

# 仓库父目录加入 sys.path（与 test_p0_4_rollback_config.py 同模式）
_PKG_PARENT = str(Path(__file__).resolve().parents[1].parent)
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

from callwarden.db.db_task_contracts import (
    AcceptanceClause,
    AllowedEditScope,
    ContractErrorCode,
    ContractPublicationError,
    ContractWarningCode,
    Envelope,
    SCOPE_LABEL_EXPLICIT,
    SCOPE_LABEL_MIGRATION_PENDING,
    SCOPE_LABEL_UNSCOPED,
    classify_clause,
    classify_scope,
    compute_contract_hash,
    envelope_to_canonical_bytes,
    get_max_published_revision,
    make_contract_reason,
    make_contract_warning,
    validate_profile,
)
from callwarden.db.db import CodeGraphDB


# ============================================
# Fixtures
# ============================================

@pytest.fixture
def db_with_contracts(tmp_path):
    """创建一个带 task_contract_revisions 表的临时数据库。"""
    db_path = str(tmp_path / "test_contracts.db")
    db = CodeGraphDB(db_path, workspace_root=str(tmp_path))
    db.register_workspace("test", str(tmp_path), "测试")
    yield db
    db.close()


def _make_code_change_envelope(
    contract_id: str = "C-test",
    revision: int = 1,
    profile: str = "code_change",
) -> Envelope:
    """构造一个合法的 code_change Envelope（带显式 scope 与 executable clause）。"""
    return Envelope(
        contract_id=contract_id,
        revision=revision,
        profile=profile,
        objective={"goal": "修复 bug", "non_goals": ["不更换协议"]},
        interfaces={"inputs": ["Credentials"], "outputs": ["AuthResult"]},
        allowed_edit_scope=AllowedEditScope(
            files=["src/auth/service.py", "tests/test_auth.py"],
            symbols=["auth.service.authenticate"],
            generated_from=["task_steps.target_file"],
        ),
        acceptance_clauses=[
            AcceptanceClause(
                clause_id="AC-1",
                kind="executable",
                subject="tests/test_auth.py::test_login",
                operator="test_pass",
                expected=True,
                verifier={"name": "pytest", "version": "8.x", "config_hash": "sha256:abc"},
                freshness="same_contract_and_current_change_snapshot",
                severity="block",
            ),
        ],
        risks=[{"risk": "改变审计日志", "mitigation": "保留 reason code"}],
        rollback={"strategy": "还原 diff", "verification": "重跑测试"},
        dependencies={"requires_existing": ["auth.repository.find_user"]},
    )


# ============================================
# Canonical 序列化与 round-trip（Req 2.3–2.5）
# ============================================

class TestCanonicalSerialization:
    """验证 Envelope_Printer / Envelope_Parser 的 Canonical 序列化。"""

    def test_round_trip_identical_bytes(self):
        """Req 2.4: print → parse → print → parse 产生相同 canonical 字节。"""
        env = _make_code_change_envelope()
        bytes1 = envelope_to_canonical_bytes(env, exclude_hash=True)
        parsed = Envelope.from_dict(json.loads(bytes1.decode("utf-8")))
        bytes2 = envelope_to_canonical_bytes(parsed, exclude_hash=True)
        assert bytes1 == bytes2

    def test_path_normalization_backslash_to_slash(self):
        """Req 2.3: 路径规范化为正斜杠。"""
        env = _make_code_change_envelope()
        env.allowed_edit_scope.files = ["src\\auth\\service.py"]
        canonical = envelope_to_canonical_bytes(env, exclude_hash=True).decode("utf-8")
        assert "src/auth/service.py" in canonical
        assert "src\\auth\\service.py" not in canonical

    def test_deterministic_array_ordering(self):
        """Req 2.3: 数组按 canonical json 字符串稳定排序。"""
        env = _make_code_change_envelope()
        # 故意打乱 files 顺序
        env.allowed_edit_scope.files = ["z/last.py", "a/first.py", "m/middle.py"]
        bytes_a = envelope_to_canonical_bytes(env, exclude_hash=True)
        env.allowed_edit_scope.files = ["a/first.py", "m/middle.py", "z/last.py"]
        bytes_b = envelope_to_canonical_bytes(env, exclude_hash=True)
        assert bytes_a == bytes_b

    def test_presentation_fields_excluded_from_hash(self):
        """Req 2.5, 2.8: 纯展示字段变化不改变 Contract_Hash。"""
        env = _make_code_change_envelope()
        hash1 = compute_contract_hash(env)
        env.created_at = 999999.0
        env.created_by = "different-session"
        hash2 = compute_contract_hash(env)
        assert hash1 == hash2

    def test_semantic_change_alters_hash(self):
        """Req 2.6: 语义字段变化必须改变 Contract_Hash。"""
        env = _make_code_change_envelope()
        hash1 = compute_contract_hash(env)
        env.objective["goal"] = "完全不同的目标"
        hash2 = compute_contract_hash(env)
        assert hash1 != hash2

    def test_hash_excludes_hash_field(self):
        """Req 2.8: contract_hash 自身不参与 hash 计算。"""
        env = _make_code_change_envelope()
        hash1 = compute_contract_hash(env)
        env.contract_hash = "sha256:fake-different"
        hash2 = compute_contract_hash(env)
        assert hash1 == hash2

    def test_hash_format(self):
        """Req 2.8: hash 格式为 sha256:前缀 + 64 位十六进制。"""
        env = _make_code_change_envelope()
        h = compute_contract_hash(env)
        assert h.startswith("sha256:")
        hex_part = h[len("sha256:"):]
        assert len(hex_part) == 64
        int(hex_part, 16)  # 必须是合法十六进制


# ============================================
# Profile 校验（Req 5.4, 5.6–5.11）
# ============================================

class TestProfileValidation:
    """验证 validate_profile 行为。"""

    def test_invalid_profile_rejected(self):
        """Req 5.11: 非法 profile 返回 INVALID_PROFILE。"""
        env = Envelope(contract_id="C", revision=1, profile="invalid_profile")
        reason = validate_profile(env)
        assert reason is not None
        assert reason.code == ContractErrorCode.INVALID_PROFILE

    def test_code_change_requires_scope_and_clauses(self):
        """Req 5.8: code_change 必填 allowed_edit_scope + acceptance_clauses。"""
        env = Envelope(
            contract_id="C", revision=1, profile="code_change",
            objective={"goal": "x"},
            interfaces={"inputs": []},
            # 缺 allowed_edit_scope（空）和 acceptance_clauses
        )
        reason = validate_profile(env)
        assert reason is not None
        assert reason.code == ContractErrorCode.ENVELOPE_GRAMMAR

    def test_high_risk_requires_risks_and_rollback(self):
        """Req 5.9: high_risk 必填 risks + rollback。"""
        env = Envelope(
            contract_id="C", revision=1, profile="high_risk",
            objective={"goal": "x"},
            interfaces={"inputs": []},
            allowed_edit_scope=AllowedEditScope(files=["a.py"]),
            acceptance_clauses=[AcceptanceClause(clause_id="AC-1", kind="declarative", statement="s")],
            # 缺 risks 和 rollback
        )
        reason = validate_profile(env)
        assert reason is not None
        assert reason.code == ContractErrorCode.ENVELOPE_GRAMMAR

    @pytest.mark.parametrize("profile", ["research", "design", "code_change", "high_risk", "review"])
    def test_all_valid_profiles_accepted_when_complete(self, profile):
        """5 个合法 profile 在必填字段齐全时应通过。"""
        env = _make_code_change_envelope(profile=profile)
        # 按 profile 补齐必填字段
        if profile in ("research",):
            env.risks = []
            env.rollback = {}
            env.allowed_edit_scope = AllowedEditScope()
            env.acceptance_clauses = []
        if profile in ("design",):
            env.acceptance_clauses = []
            env.allowed_edit_scope = AllowedEditScope()
        if profile in ("review",):
            env.risks = []
            env.rollback = {}
            env.allowed_edit_scope = AllowedEditScope()
            env.interfaces = {}
        reason = validate_profile(env)
        assert reason is None, f"profile {profile} 应通过：{reason}"

    def test_profile_reason_has_stable_code_and_i18n_key(self):
        """Req 1.12: reason 携带稳定错误码与 i18n key。"""
        env = Envelope(contract_id="C", revision=1, profile="bad")
        reason = validate_profile(env)
        assert reason.code == ContractErrorCode.INVALID_PROFILE
        assert reason.message_key == "errors.contract_invalid_profile"
        # message 可解析（不抛异常）
        msg = reason.message()
        assert isinstance(msg, str) and msg


# ============================================
# Clause 分类（Req 2.10–2.11）
# ============================================

class TestClauseClassification:
    """验证 executable clause 缺字段降级为 declarative。"""

    def test_executable_missing_verifier_downgrades(self):
        """Req 2.11: executable 缺 verifier 降级为 declarative。"""
        clause = AcceptanceClause(
            clause_id="AC-1", kind="executable", subject="test.py::test_x",
            operator="test_pass", expected=True, verifier={},
            freshness="same_contract", severity="block",
        )
        classified, reason = classify_clause(clause)
        assert classified.kind == "declarative"
        assert reason is not None
        assert reason.code == ContractErrorCode.EXECUTABLE_CLAUSE_INCOMPLETE

    def test_executable_missing_subject_downgrades(self):
        """Req 2.11: executable 缺 subject 降级为 declarative。"""
        clause = AcceptanceClause(
            clause_id="AC-2", kind="executable", subject="",  # 空
            operator="test_pass", expected=True,
            verifier={"name": "pytest", "version": "8.x", "config_hash": "sha256:abc"},
            freshness="same_contract", severity="block",
        )
        classified, reason = classify_clause(clause)
        assert classified.kind == "declarative"
        assert reason is not None

    def test_executable_complete_stays_executable(self):
        """字段齐全的 executable 不降级。"""
        clause = AcceptanceClause(
            clause_id="AC-3", kind="executable", subject="test.py::test_y",
            operator="test_pass", expected=True,
            verifier={"name": "pytest", "version": "8.x", "config_hash": "sha256:abc"},
            freshness="same_contract", severity="block",
        )
        classified, reason = classify_clause(clause)
        assert classified.kind == "executable"
        assert reason is None

    def test_declarative_unchanged(self):
        """declarative clause 不走分类逻辑。"""
        clause = AcceptanceClause(
            clause_id="AC-4", kind="declarative", statement="不泄露信息", severity="block",
        )
        classified, reason = classify_clause(clause)
        assert classified.kind == "declarative"
        assert reason is None


# ============================================
# 空 Allowed_Edit_Scope 三分支（Req 7.11–7.16）
# ============================================

class TestEmptyScopeClassification:
    """验证 classify_scope 三分支与警告码。"""

    def test_code_change_empty_scope_rejected(self):
        """Req 7.11: code_change 空 scope 拒绝发布。"""
        env = Envelope(
            contract_id="C", revision=1, profile="code_change",
            objective={"goal": "x"}, interfaces={"inputs": []},
            allowed_edit_scope=AllowedEditScope(),
            acceptance_clauses=[AcceptanceClause(clause_id="AC-1", kind="declarative", statement="s")],
        )
        label, reject, warning = classify_scope(env, existing_steps_have_target=True)
        assert reject is not None
        assert reject.code == ContractErrorCode.EMPTY_SCOPE_REJECTED
        assert warning is None

    def test_high_risk_empty_scope_rejected(self):
        """Req 7.11: high_risk 空 scope 拒绝发布。"""
        env = Envelope(
            contract_id="C", revision=1, profile="high_risk",
            objective={"goal": "x"}, interfaces={"inputs": []},
            allowed_edit_scope=AllowedEditScope(),
            acceptance_clauses=[AcceptanceClause(clause_id="AC-1", kind="declarative", statement="s")],
            risks=[{"risk": "r"}], rollback={"strategy": "s"},
        )
        label, reject, warning = classify_scope(env, existing_steps_have_target=True)
        assert reject is not None
        assert reject.code == ContractErrorCode.EMPTY_SCOPE_REJECTED

    def test_research_empty_scope_with_target_unscoped(self):
        """Req 7.12, 7.15: research 空 scope + 有 target → unscoped + 警告。"""
        env = Envelope(
            contract_id="C", revision=1, profile="research",
            objective={"goal": "x"}, interfaces={"inputs": []},
            allowed_edit_scope=AllowedEditScope(),
        )
        label, reject, warning = classify_scope(env, existing_steps_have_target=True)
        assert reject is None
        assert label == SCOPE_LABEL_UNSCOPED
        assert warning is not None
        assert warning.severity == "warning"
        assert warning.code == ContractWarningCode.UNSCOPED_PUBLICATION

    def test_design_empty_scope_no_target_migration_pending(self):
        """Req 7.13: design 空 scope + 无 target → scope_migration_pending。"""
        env = Envelope(
            contract_id="C", revision=1, profile="design",
            objective={"goal": "x"}, interfaces={"inputs": []},
            risks=[{"risk": "r"}], rollback={"strategy": "s"},
            allowed_edit_scope=AllowedEditScope(),
        )
        label, reject, warning = classify_scope(env, existing_steps_have_target=False)
        assert reject is None, "scope_migration_pending 发布期不拒绝"
        assert label == SCOPE_LABEL_MIGRATION_PENDING
        assert warning is not None

    def test_explicit_scope_no_warning(self):
        """非空 scope → explicit，无拒绝无警告。"""
        env = Envelope(
            contract_id="C", revision=1, profile="code_change",
            objective={"goal": "x"}, interfaces={"inputs": []},
            allowed_edit_scope=AllowedEditScope(files=["src/main.py"]),
            acceptance_clauses=[AcceptanceClause(clause_id="AC-1", kind="declarative", statement="s")],
        )
        label, reject, warning = classify_scope(env, existing_steps_have_target=True)
        assert label == SCOPE_LABEL_EXPLICIT
        assert reject is None
        assert warning is None

    def test_warning_has_stable_code_and_i18n_key(self):
        """Req 7.16: 警告码稳定 + i18n key。"""
        env = Envelope(
            contract_id="C", revision=1, profile="research",
            objective={"goal": "x"}, interfaces={"inputs": []},
            allowed_edit_scope=AllowedEditScope(),
        )
        _, _, warning = classify_scope(env, existing_steps_have_target=True)
        assert warning.code == ContractWarningCode.UNSCOPED_PUBLICATION
        assert warning.message_key == "warnings.contract_unscoped_publication"
        msg = warning.message()
        assert isinstance(msg, str) and msg


# ============================================
# publish_envelope_revision 集成测试（Req 2.6–2.9, 7.11–7.17）
# ============================================

class TestPublishEnvelopeRevision:
    """验证 publish_envelope_revision 端到端行为。"""

    def test_publish_code_change_success(self, db_with_contracts):
        """code_change Envelope 正常发布并写入数据库。"""
        db = db_with_contracts
        env = _make_code_change_envelope(contract_id="C-pub-1", revision=1)
        envelope, warnings = db.publish_envelope_revision(
            env, task_id="T-test-1", workspace_id=1, created_by="session-A"
        )
        assert envelope.contract_hash.startswith("sha256:")
        assert envelope.created_at > 0
        assert warnings == []

        # 数据库验证
        cur = db.conn.execute(
            "SELECT contract_id, revision, contract_hash, profile, task_id, created_by "
            "FROM task_contract_revisions WHERE contract_id=?",
            ("C-pub-1",),
        )
        row = cur.fetchone()
        assert row is not None
        assert row["revision"] == 1
        assert row["contract_hash"] == envelope.contract_hash
        assert row["profile"] == "code_change"
        assert row["task_id"] == "T-test-1"
        assert row["created_by"] == "session-A"

    def test_publish_revision_must_be_monotonic(self, db_with_contracts):
        """Req 2.7: revision 必须单调递增。"""
        db = db_with_contracts
        env1 = _make_code_change_envelope(contract_id="C-mono", revision=1)
        db.publish_envelope_revision(env1, task_id="T-1")

        # revision=1 重复 → 拒绝
        env1_dup = _make_code_change_envelope(contract_id="C-mono", revision=1)
        env1_dup.objective["goal"] = "不同的目标"  # 试图用相同 revision 发布不同内容
        with pytest.raises(ContractPublicationError) as exc:
            db.publish_envelope_revision(env1_dup, task_id="T-1")
        assert exc.value.reason.code == ContractErrorCode.REVISION_NOT_MONOTONIC

        # revision=2 允许
        env2 = _make_code_change_envelope(contract_id="C-mono", revision=2)
        env2.objective["goal"] = "更新后的目标"
        envelope, _ = db.publish_envelope_revision(env2, task_id="T-1")
        assert envelope.revision == 2

    def test_publish_code_change_empty_scope_rejected(self, db_with_contracts):
        """Req 7.11: code_change 空 scope 拒绝发布，保留上一已接受 revision。"""
        db = db_with_contracts
        env = _make_code_change_envelope(contract_id="C-empty", revision=1)
        env.allowed_edit_scope = AllowedEditScope()  # 清空 scope
        with pytest.raises(ContractPublicationError) as exc:
            db.publish_envelope_revision(env, task_id="T-1")
        assert exc.value.reason.code == ContractErrorCode.EMPTY_SCOPE_REJECTED
        # 数据库不应有任何记录
        cur = db.conn.execute(
            "SELECT COUNT(*) FROM task_contract_revisions WHERE contract_id=?",
            ("C-empty",),
        )
        assert cur.fetchone()[0] == 0

    def test_publish_research_empty_scope_unscoped_warning(self, db_with_contracts):
        """Req 7.12, 7.15: research 空 scope 发布成功 + 非阻断警告。"""
        db = db_with_contracts
        env = Envelope(
            contract_id="C-research-unscoped", revision=1, profile="research",
            objective={"goal": "研究"}, interfaces={"inputs": []},
            allowed_edit_scope=AllowedEditScope(),  # 空 scope
        )
        envelope, warnings = db.publish_envelope_revision(env, task_id="T-1")
        assert envelope.contract_hash  # 发布成功
        # 应有 unscoped 警告
        unscoped_warnings = [w for w in warnings if w.code == ContractWarningCode.UNSCOPED_PUBLICATION]
        assert len(unscoped_warnings) >= 1
        # 数据库应有记录
        cur = db.conn.execute(
            "SELECT COUNT(*) FROM task_contract_revisions WHERE contract_id=?",
            ("C-research-unscoped",),
        )
        assert cur.fetchone()[0] == 1

    def test_publish_research_empty_scope_migration_pending(self, db_with_contracts):
        """Req 7.13: research 空 scope + 既有 step 无 target → scope_migration_pending。"""
        db = db_with_contracts
        # 不在 task_steps 表插入任何带 target 的 step（默认无 target）
        env = Envelope(
            contract_id="C-migration", revision=1, profile="design",
            objective={"goal": "设计"}, interfaces={"inputs": []},
            risks=[{"risk": "r"}], rollback={"strategy": "s"},
            allowed_edit_scope=AllowedEditScope(),
        )
        envelope, warnings = db.publish_envelope_revision(env, task_id="T-no-target")
        # 发布期不拒绝
        assert envelope.contract_hash
        # 应记录 scope_label = scope_migration_pending
        assert envelope.dependencies.get("_scope_label") == SCOPE_LABEL_MIGRATION_PENDING
        # 应有警告
        assert any(w.code == ContractWarningCode.UNSCOPED_PUBLICATION for w in warnings)

    def test_publish_invalid_profile_rejected(self, db_with_contracts):
        """非法 profile 发布时 fail closed。"""
        db = db_with_contracts
        env = _make_code_change_envelope()
        env.profile = "bad_profile"
        with pytest.raises(ContractPublicationError) as exc:
            db.publish_envelope_revision(env, task_id="T-1")
        assert exc.value.reason.code == ContractErrorCode.INVALID_PROFILE

    def test_published_envelope_payload_contains_hash(self, db_with_contracts):
        """已发布 Envelope 的 payload 必须包含 contract_hash（用于回溯）。"""
        db = db_with_contracts
        env = _make_code_change_envelope(contract_id="C-payload", revision=1)
        envelope, _ = db.publish_envelope_revision(env, task_id="T-1")
        cur = db.conn.execute(
            "SELECT envelope_payload FROM task_contract_revisions WHERE contract_id=?",
            ("C-payload",),
        )
        payload = json.loads(cur.fetchone()[0])
        assert payload["contract_hash"] == envelope.contract_hash
        assert payload["revision"] == 1

    def test_get_current_envelope_returns_latest(self, db_with_contracts):
        """get_current_envelope_for_task 返回最大 revision。"""
        db = db_with_contracts
        env1 = _make_code_change_envelope(contract_id="C-latest", revision=1)
        env1.objective["goal"] = "v1"
        db.publish_envelope_revision(env1, task_id="T-latest")

        env2 = _make_code_change_envelope(contract_id="C-latest", revision=2)
        env2.objective["goal"] = "v2"
        db.publish_envelope_revision(env2, task_id="T-latest")

        current = db.get_current_envelope_for_task("T-latest")
        assert current is not None
        assert current.revision == 2
        assert current.objective["goal"] == "v2"

    def test_get_revisions_returns_sorted(self, db_with_contracts):
        """get_envelope_revisions 按 revision 升序返回（即使按非顺序查询）。"""
        db = db_with_contracts
        # 必须按升序发布（revision 单调递增约束）
        for rev in [1, 2, 3]:
            env = _make_code_change_envelope(contract_id="C-sort", revision=rev)
            env.objective["goal"] = f"v{rev}"
            db.publish_envelope_revision(env, task_id="T-sort")
        revisions = db.get_envelope_revisions("C-sort")
        assert [r["revision"] for r in revisions] == [1, 2, 3]


# ============================================
# parse_envelope / hash_revision 一致性（Req 2.1, 2.2, 2.9）
# ============================================

class TestParseEnvelopeAndHashConsistency:
    """验证 parse_envelope 与 hash_revision 一致性校验。"""

    def test_parse_rejects_missing_contract_id(self, db_with_contracts):
        """Req 2.2: contract_id 缺失 → ENVELOPE_GRAMMAR。"""
        with pytest.raises(ContractPublicationError) as exc:
            db_with_contracts.parse_envelope({"revision": 1, "profile": "research"})
        assert exc.value.reason.code == ContractErrorCode.ENVELOPE_GRAMMAR

    def test_parse_rejects_invalid_revision(self, db_with_contracts):
        """Req 2.2: revision <= 0 → ENVELOPE_GRAMMAR。"""
        with pytest.raises(ContractPublicationError) as exc:
            db_with_contracts.parse_envelope(
                {"contract_id": "C", "revision": 0, "profile": "research"}
            )
        assert exc.value.reason.code == ContractErrorCode.ENVELOPE_GRAMMAR

    def test_parse_rejects_invalid_profile(self, db_with_contracts):
        """Req 2.2: profile 非法 → INVALID_PROFILE。"""
        with pytest.raises(ContractPublicationError) as exc:
            db_with_contracts.parse_envelope(
                {"contract_id": "C", "revision": 1, "profile": "bad"}
            )
        assert exc.value.reason.code == ContractErrorCode.INVALID_PROFILE

    def test_parse_downgrades_incomplete_executable(self, db_with_contracts):
        """Req 2.11: parse 时 executable 缺字段降级为 declarative。"""
        data = {
            "contract_id": "C-down",
            "revision": 1,
            "profile": "code_change",
            "objective": {"goal": "x"},
            "interfaces": {"inputs": []},
            "allowed_edit_scope": {"files": ["a.py"], "symbols": [], "generated_from": []},
            "acceptance_clauses": [
                {
                    "clause_id": "AC-1",
                    "kind": "executable",
                    "subject": "",
                    "operator": "test_pass",
                    "expected": True,
                    "verifier": {},
                    "freshness": "same_contract",
                    "severity": "block",
                }
            ],
        }
        env = db_with_contracts.parse_envelope(data)
        assert env.acceptance_clauses[0].kind == "declarative"
        # 降级 reason 记录在 dependencies._clause_downgrades
        downgrades = env.dependencies.get("_clause_downgrades", [])
        assert len(downgrades) >= 1
        assert downgrades[0]["code"] == ContractErrorCode.EXECUTABLE_CLAUSE_INCOMPLETE

    def test_verify_hash_revision_consistency_ok(self, db_with_contracts):
        """hash 与 revision 一致时返回 None。"""
        env = _make_code_change_envelope()
        env.contract_hash = compute_contract_hash(env)
        assert db_with_contracts.verify_hash_revision_consistency(env) is None

    def test_verify_hash_revision_mismatch(self, db_with_contracts):
        """Req 2.9: hash 不匹配 → HASH_REVISION_MISMATCH。"""
        env = _make_code_change_envelope()
        env.contract_hash = "sha256:fake-mismatch-hash-value-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        reason = db_with_contracts.verify_hash_revision_consistency(env)
        assert reason is not None
        assert reason.code == ContractErrorCode.HASH_REVISION_MISMATCH


# ============================================
# 不可变 append-only（Req 2.7 + Evidence_Ledger 语义预备）
# ============================================

class TestAppendOnlyImmutable:
    """验证 task_contract_revisions 是不可变 append-only（每次发布只追加）。"""

    def test_multiple_revisions_all_preserved(self, db_with_contracts):
        """多次 revision 发布后所有历史记录都保留。"""
        db = db_with_contracts
        for rev in [1, 2, 3]:
            env = _make_code_change_envelope(contract_id="C-history", revision=rev)
            env.objective["goal"] = f"v{rev}"
            db.publish_envelope_revision(env, task_id="T-hist")

        revisions = db.get_envelope_revisions("C-history")
        assert len(revisions) == 3
        # 每个 revision 都有独立的 hash
        hashes = [r["contract_hash"] for r in revisions]
        assert len(set(hashes)) == 3

    def test_unique_constraint_on_contract_id_revision(self, db_with_contracts):
        """UNIQUE(contract_id, revision) 约束生效。"""
        db = db_with_contracts
        env = _make_code_change_envelope(contract_id="C-uniq", revision=1)
        db.publish_envelope_revision(env, task_id="T-1")

        # 直接通过 SQL 插入相同 (contract_id, revision)
        with pytest.raises(Exception):
            db.conn.execute(
                "INSERT INTO task_contract_revisions "
                "(contract_id, revision, contract_hash, profile, task_id, envelope_payload, created_at) "
                "VALUES ('C-uniq', 1, 'h', 'code_change', 'T-1', '{}', 0)"
            )
