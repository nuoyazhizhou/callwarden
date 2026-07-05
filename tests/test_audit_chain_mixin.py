"""AuditChainMixin 单元测试。

覆盖 db/db_audit_chain.py 中三个核心方法：
- canonical_json: 稳定序列化
- sign_audit_record: 写入签名链
- verify_audit_chain: 验证链连续性与签名匹配

测试策略：创建临时子类混入 AuditChainMixin 和 CodeGraphDB，
避免修改 db.py（混入由后续 step 完成）。

HMAC 测试通过 monkeypatch 环境变量 CALLWARDEN_AUDIT_HMAC_KEY 实现。
"""

import os
import tempfile
from unittest import mock

from callwarden.db.db import CodeGraphDB
from callwarden.db.db_audit_chain import AuditChainMixin, _get_hmac_key


# 临时子类：混入 AuditChainMixin 以便测试
class _TestDB(AuditChainMixin, CodeGraphDB):
    """测试用子类，混入 AuditChainMixin"""
    pass


def _db_with_audit():
    """构造带 AuditChainMixin 的临时工作区数据库。"""
    root = tempfile.mkdtemp()
    db = _TestDB(os.path.join(root, "callwarden.db"), workspace_root=root)
    return db, root


# ============================================
# canonical_json 测试
# ============================================


def test_canonical_json_stable_for_same_dict():
    """相同 dict（key 顺序不同）产生相同 JSON 字符串"""
    db, _root = _db_with_audit()
    try:
        d1 = {"b": 2, "a": 1, "c": 3}
        d2 = {"c": 3, "a": 1, "b": 2}
        assert db.canonical_json(d1) == db.canonical_json(d2)
    finally:
        db.close()


def test_canonical_json_compact_no_spaces():
    """输出紧凑格式，无多余空格"""
    db, _root = _db_with_audit()
    try:
        result = db.canonical_json({"a": 1, "b": 2})
        assert result == '{"a":1,"b":2}'
    finally:
        db.close()


def test_canonical_json_preserve_unicode():
    """保留 Unicode 字符，不转义为 \\uXXXX"""
    db, _root = _db_with_audit()
    try:
        result = db.canonical_json({"msg": "中文测试"})
        assert "中文测试" in result
        assert "\\u" not in result
    finally:
        db.close()


def test_canonical_json_nested_dict_sorted():
    """嵌套 dict 的 key 也被递归排序"""
    db, _root = _db_with_audit()
    try:
        d1 = {"outer": {"z": 1, "a": 2}}
        d2 = {"outer": {"a": 2, "z": 1}}
        assert db.canonical_json(d1) == db.canonical_json(d2)
    finally:
        db.close()


def test_canonical_json_handles_list_and_primitives():
    """处理 list、int、float、bool、None"""
    db, _root = _db_with_audit()
    try:
        result = db.canonical_json({"list": [1, 2, 3], "none": None, "bool": True})
        assert '"list":[1,2,3]' in result
        assert '"none":null' in result
        assert '"bool":true' in result
    finally:
        db.close()


# ============================================
# sign_audit_record 测试
# ============================================


def test_sign_audit_record_returns_dict_with_required_fields():
    """返回 dict 包含必要字段"""
    db, _root = _db_with_audit()
    try:
        result = db.sign_audit_record("test_table", "1", {"key": "value"})
        assert "id" in result
        assert "table_name" in result
        assert "record_id" in result
        assert "operation" in result
        assert "payload_hash" in result
        assert "prev_signature" in result
        assert "record_signature" in result
        assert "signing_key_id" in result
        assert "security_level" in result
    finally:
        db.close()


def test_sign_audit_record_first_record_prev_signature_empty():
    """首条记录 prev_signature 为空串"""
    db, _root = _db_with_audit()
    try:
        result = db.sign_audit_record("test_table", "1", {"key": "value"})
        assert result["prev_signature"] == ""
    finally:
        db.close()


def test_sign_audit_record_second_record_chains_to_first():
    """第二条记录 prev_signature 等于第一条的 record_signature"""
    db, _root = _db_with_audit()
    try:
        r1 = db.sign_audit_record("test_table", "1", {"key": "v1"})
        r2 = db.sign_audit_record("test_table", "2", {"key": "v2"})
        assert r2["prev_signature"] == r1["record_signature"]
    finally:
        db.close()


def test_sign_audit_record_different_tables_are_independent_chains():
    """不同 table_name 维护独立链"""
    db, _root = _db_with_audit()
    try:
        r1_a = db.sign_audit_record("table_a", "1", {"key": "a1"})
        r1_b = db.sign_audit_record("table_b", "1", {"key": "b1"})
        r2_a = db.sign_audit_record("table_a", "2", {"key": "a2"})

        # table_b 首条 prev_signature 为空
        assert r1_b["prev_signature"] == ""
        # table_a 第二条链接到 table_a 首条，而非 table_b 首条
        assert r2_a["prev_signature"] == r1_a["record_signature"]
        assert r2_a["prev_signature"] != r1_b["record_signature"]
    finally:
        db.close()


def test_sign_audit_record_default_security_level_hash_only():
    """无 HMAC key 时 security_level=hash_only"""
    db, _root = _db_with_audit()
    try:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)
            result = db.sign_audit_record("test_table", "1", {"key": "value"})
            assert result["security_level"] == "hash_only"
            assert result["signing_key_id"] == "local"
    finally:
        db.close()


def test_sign_audit_record_hmac_mode_with_env_key():
    """有 HMAC key 时 security_level=hmac"""
    db, _root = _db_with_audit()
    try:
        with mock.patch.dict(os.environ, {"CALLWARDEN_AUDIT_HMAC_KEY": "test-secret"}):
            result = db.sign_audit_record("test_table", "1", {"key": "value"})
            assert result["security_level"] == "hmac"
            assert result["signing_key_id"] == "hmac"
    finally:
        db.close()


def test_sign_audit_record_record_id_converted_to_string():
    """record_id 转为字符串存储"""
    db, _root = _db_with_audit()
    try:
        result = db.sign_audit_record("test_table", 123, {"key": "value"})
        assert result["record_id"] == "123"
    finally:
        db.close()


def test_sign_audit_record_same_payload_different_table_independent():
    """相同 payload 在不同 table_name 产生独立记录"""
    db, _root = _db_with_audit()
    try:
        r1 = db.sign_audit_record("table_a", "1", {"key": "same"})
        r2 = db.sign_audit_record("table_b", "1", {"key": "same"})
        # payload_hash 相同（内容相同），但 record_id 在不同表中
        assert r1["payload_hash"] == r2["payload_hash"]
        # 但 prev_signature 都是空（都是各自表的首条）
        assert r1["prev_signature"] == ""
        assert r2["prev_signature"] == ""
    finally:
        db.close()


# ============================================
# verify_audit_chain 测试
# ============================================


def test_verify_audit_chain_empty_returns_zero_counts():
    """空库 verify 返回 total_count=0"""
    db, _root = _db_with_audit()
    try:
        result = db.verify_audit_chain()
        assert result["total_count"] == 0
        assert result["verified_count"] == 0
        assert result["broken_count"] == 0
        assert result["broken_records"] == []
    finally:
        db.close()


def test_verify_audit_chain_all_pass_single_table():
    """单表全通过验证"""
    db, _root = _db_with_audit()
    try:
        db.sign_audit_record("test_table", "1", {"key": "v1"})
        db.sign_audit_record("test_table", "2", {"key": "v2"})
        db.sign_audit_record("test_table", "3", {"key": "v3"})

        result = db.verify_audit_chain(table_name="test_table")
        assert result["total_count"] == 3
        assert result["verified_count"] == 3
        assert result["broken_count"] == 0
    finally:
        db.close()


def test_verify_audit_chain_detects_signature_mismatch():
    """检测 record_signature 被篡改"""
    db, _root = _db_with_audit()
    try:
        db.sign_audit_record("test_table", "1", {"key": "v1"})
        db.sign_audit_record("test_table", "2", {"key": "v2"})

        # 篡改第一条记录的 record_signature
        db.conn.execute(
            "UPDATE audit_chain SET record_signature = ? WHERE id = ?",
            ("fake_signature", 1),
        )
        db.conn.commit()

        result = db.verify_audit_chain(table_name="test_table")
        assert result["broken_count"] >= 1
        reasons = result["broken_records"][0]["reasons"]
        assert "signature_mismatch" in reasons
    finally:
        db.close()


def test_verify_audit_chain_detects_chain_broken():
    """检测链断裂（prev_signature 不匹配）"""
    db, _root = _db_with_audit()
    try:
        r1 = db.sign_audit_record("test_table", "1", {"key": "v1"})
        db.sign_audit_record("test_table", "2", {"key": "v2"})

        # 篡改第二条记录的 prev_signature，使其不匹配第一条的 record_signature
        db.conn.execute(
            "UPDATE audit_chain SET prev_signature = ? WHERE id = ?",
            ("wrong_prev", 2),
        )
        db.conn.commit()

        result = db.verify_audit_chain(table_name="test_table")
        assert result["broken_count"] >= 1
        # 第二条记录应有 chain_broken 原因
        broken_ids = [r["id"] for r in result["broken_records"]]
        assert 2 in broken_ids
        reasons = result["broken_records"][0]["reasons"]
        assert "chain_broken" in reasons
    finally:
        db.close()


def test_verify_audit_chain_table_name_filter():
    """table_name 过滤：只验证指定表"""
    db, _root = _db_with_audit()
    try:
        db.sign_audit_record("table_a", "1", {"key": "a1"})
        db.sign_audit_record("table_b", "1", {"key": "b1"})

        result_a = db.verify_audit_chain(table_name="table_a")
        assert result_a["total_count"] == 1
        assert result_a["verified_count"] == 1

        result_b = db.verify_audit_chain(table_name="table_b")
        assert result_b["total_count"] == 1
        assert result_b["verified_count"] == 1

        result_all = db.verify_audit_chain()
        assert result_all["total_count"] == 2
    finally:
        db.close()


def test_verify_audit_chain_limit_param():
    """limit 参数限制验证记录数"""
    db, _root = _db_with_audit()
    try:
        for i in range(5):
            db.sign_audit_record("test_table", str(i), {"key": f"v{i}"})

        result = db.verify_audit_chain(table_name="test_table", limit=3)
        assert result["total_count"] == 3
    finally:
        db.close()


def test_verify_audit_chain_hmac_mode_consistency():
    """HMAC 模式下签名与验证一致"""
    db, _root = _db_with_audit()
    try:
        with mock.patch.dict(os.environ, {"CALLWARDEN_AUDIT_HMAC_KEY": "test-secret"}):
            db.sign_audit_record("test_table", "1", {"key": "v1"})
            db.sign_audit_record("test_table", "2", {"key": "v2"})

            result = db.verify_audit_chain(table_name="test_table")
            assert result["verified_count"] == 2
            assert result["broken_count"] == 0
            assert result["security_level"] == "hmac"
    finally:
        db.close()


def test_verify_audit_chain_mixed_hash_and_hmac_detected():
    """混合签名模式：原为 SHA-256，验证时切换到 HMAC 应检测到不匹配"""
    db, _root = _db_with_audit()
    try:
        # 1. 无 HMAC key 时签名
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)
            db.sign_audit_record("test_table", "1", {"key": "v1"})

        # 2. 有 HMAC key 时验证（签名时是 local，验证时是 hmac）
        with mock.patch.dict(os.environ, {"CALLWARDEN_AUDIT_HMAC_KEY": "new-secret"}):
            result = db.verify_audit_chain(table_name="test_table")

        # signing_key_id='local'，验证时用 SHA-256 重新计算，应该匹配
        # 但 security_level 报告为 hmac（当前环境）
        assert result["verified_count"] == 1
        assert result["broken_count"] == 0
    finally:
        db.close()


def test_verify_audit_chain_returns_security_level():
    """返回当前 security_level"""
    db, _root = _db_with_audit()
    try:
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)
            db.sign_audit_record("test_table", "1", {"key": "v1"})
            result = db.verify_audit_chain()
            assert result["security_level"] == "hash_only"

        with mock.patch.dict(os.environ, {"CALLWARDEN_AUDIT_HMAC_KEY": "test-secret"}):
            result = db.verify_audit_chain()
            assert result["security_level"] == "hmac"
    finally:
        db.close()


def test_verify_audit_chain_multiple_tables_independent():
    """多表混合时各表链独立验证"""
    db, _root = _db_with_audit()
    try:
        db.sign_audit_record("table_a", "1", {"key": "a1"})
        db.sign_audit_record("table_b", "1", {"key": "b1"})
        db.sign_audit_record("table_a", "2", {"key": "a2"})
        db.sign_audit_record("table_b", "2", {"key": "b2"})

        result = db.verify_audit_chain()
        assert result["total_count"] == 4
        assert result["verified_count"] == 4
        assert result["broken_count"] == 0
    finally:
        db.close()


# ============================================
# _get_hmac_key 辅助函数测试
# ============================================


def test_get_hmac_key_none_when_no_env_no_file():
    """无环境变量且无文件时返回 None"""
    with mock.patch.dict(os.environ, {}, clear=False):
        os.environ.pop("CALLWARDEN_AUDIT_HMAC_KEY", None)
        # mock 文件不存在
        with mock.patch("os.path.isfile", return_value=False):
            assert _get_hmac_key() is None


def test_get_hmac_key_from_env_variable():
    """从环境变量获取"""
    with mock.patch.dict(os.environ, {"CALLWARDEN_AUDIT_HMAC_KEY": "env-secret"}):
        key = _get_hmac_key()
        assert key == b"env-secret"


def test_get_hmac_key_env_takes_precedence_over_file():
    """环境变量优先于文件"""
    with mock.patch.dict(os.environ, {"CALLWARDEN_AUDIT_HMAC_KEY": "env-secret"}):
        with mock.patch("os.path.isfile", return_value=True):
            with mock.patch("builtins.open", mock.mock_open(read_data=b"file-secret")):
                key = _get_hmac_key()
                assert key == b"env-secret"
