"""Phase 4-3 差分测试：metrics / audit / backup 纯计算 Rust↔Python 行为一致性。

验证 Rust `callwarden_core.metrics_percentile` / `metrics_format_labels` /
`audit_canonical_json` / `audit_compute_signature` / `backup_compute_file_sha256` /
`backup_compute_meta_checksum` 与对应 Python 真相源的行为一致性。

契约：docs/design/phase4-3-metrics-health-audit-contract.md §4 D2-D5 测试矩阵
"""

import hashlib
import json
import os
import hmac as py_hmac
import tempfile
from pathlib import Path

import pytest


callwarden_core = pytest.importorskip("callwarden_core")


# ============================================================
# D2: metrics_percentile
# ============================================================


def _py_percentile(sorted_values, p):
    """Python 真相源：server/metrics.py:_percentile"""
    if not sorted_values:
        return 0.0
    n = len(sorted_values)
    if n == 1:
        return sorted_values[0]
    k = (n - 1) * p
    f = int(k)
    c = k - f
    if f + 1 < n:
        return sorted_values[f] + c * (sorted_values[f + 1] - sorted_values[f])
    return sorted_values[f]


class TestD2MetricsPercentile:
    """D2: metrics_percentile 行为一致性。"""

    def test_d2_1_p50_odd(self):
        """D2.1: [1,2,3,4,5] p50 → 3.0"""
        rust = callwarden_core.metrics_percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5)
        py = _py_percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.5)
        assert rust == py == 3.0

    def test_d2_2_p99(self):
        """D2.2: [1,2,3,4,5] p99"""
        rust = callwarden_core.metrics_percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.99)
        py = _py_percentile([1.0, 2.0, 3.0, 4.0, 5.0], 0.99)
        assert rust == py

    def test_d2_3_empty(self):
        """D2.3: 空列表 → 0.0"""
        rust = callwarden_core.metrics_percentile([], 0.5)
        py = _py_percentile([], 0.5)
        assert rust == py == 0.0

    def test_d2_4_single(self):
        """D2.4: 单元素 [42] p50 → 42.0"""
        rust = callwarden_core.metrics_percentile([42.0], 0.5)
        py = _py_percentile([42.0], 0.5)
        assert rust == py == 42.0

    def test_d2_5_p0(self):
        """D2.5: p0 → 最小值"""
        rust = callwarden_core.metrics_percentile([10.0, 20.0, 30.0], 0.0)
        py = _py_percentile([10.0, 20.0, 30.0], 0.0)
        assert rust == py == 10.0

    def test_d2_6_p100(self):
        """D2.6: p100 → 最大值"""
        rust = callwarden_core.metrics_percentile([10.0, 20.0, 30.0], 1.0)
        py = _py_percentile([10.0, 20.0, 30.0], 1.0)
        assert rust == py == 30.0

    def test_d2_7_even_count(self):
        """D2.7: 偶数个元素 p50"""
        values = [1.0, 2.0, 3.0, 4.0]
        rust = callwarden_core.metrics_percentile(values, 0.5)
        py = _py_percentile(values, 0.5)
        assert rust == py

    def test_d2_8_negative_values(self):
        """D2.8: 负数"""
        values = [-5.0, -2.0, 0.0, 3.0]
        rust = callwarden_core.metrics_percentile(values, 0.5)
        py = _py_percentile(values, 0.5)
        assert rust == py


# ============================================================
# D3: metrics_format_labels
# ============================================================


def _py_format_labels(label_key):
    """Python 真相源：server/metrics.py:_format_labels"""
    if not label_key:
        return ""
    parts = label_key.split(",")
    formatted = []
    for part in parts:
        if "=" in part:
            k, v = part.split("=", 1)
            formatted.append(f'{k}="{v}"')
        else:
            formatted.append(part)
    return "{" + ",".join(formatted) + "}"


class TestD3MetricsFormatLabels:
    """D3: metrics_format_labels 行为一致性。"""

    def test_d3_1_single_label(self):
        """D3.1: 单标签"""
        rust = callwarden_core.metrics_format_labels("status=ok")
        py = _py_format_labels("status=ok")
        assert rust == py == '{status="ok"}'

    def test_d3_2_multiple_labels(self):
        """D3.2: 多标签"""
        rust = callwarden_core.metrics_format_labels("method=ping,status=ok")
        py = _py_format_labels("method=ping,status=ok")
        assert rust == py == '{method="ping",status="ok"}'

    def test_d3_3_empty(self):
        """D3.3: 空字符串"""
        rust = callwarden_core.metrics_format_labels("")
        py = _py_format_labels("")
        assert rust == py == ""

    def test_d3_4_no_equals(self):
        """D3.4: 无 = 的部分（保持原样）"""
        rust = callwarden_core.metrics_format_labels("plain")
        py = _py_format_labels("plain")
        assert rust == py == "{plain}"

    def test_d3_5_value_with_special_chars(self):
        """D3.5: 值包含特殊字符"""
        rust = callwarden_core.metrics_format_labels("path=/api/v1")
        py = _py_format_labels("path=/api/v1")
        assert rust == py == '{path="/api/v1"}'


# ============================================================
# D4: audit_canonical_json + audit_compute_signature
# ============================================================


def _py_canonical_json(payload):
    """Python 真相源：db/db_audit_chain.py:canonical_json"""
    return json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _py_compute_signature(prev_signature, payload_hash, hmac_key):
    """Python 真相源：db/db_audit_chain.py:_compute_signature"""
    message = f"{prev_signature}|{payload_hash}".encode("utf-8")
    if hmac_key is not None:
        return py_hmac.new(hmac_key, message, hashlib.sha256).hexdigest()
    return hashlib.sha256(message).hexdigest()


class TestD4AuditPureCompute:
    """D4: audit 纯计算行为一致性。"""

    def test_d4_1_canonical_json_key_sorted(self):
        """D4.1: canonical_json key 排序"""
        payload = {"b": 2, "a": 1, "c": 3}
        rust = callwarden_core.audit_canonical_json(json.dumps(payload))
        py = _py_canonical_json(payload)
        assert rust == py == '{"a":1,"b":2,"c":3}'

    def test_d4_2_canonical_json_nested(self):
        """D4.2: 嵌套 dict"""
        payload = {"outer": {"z": 1, "a": 2}, "n": 5}
        rust = callwarden_core.audit_canonical_json(json.dumps(payload))
        py = _py_canonical_json(payload)
        assert rust == py

    def test_d4_3_canonical_json_unicode(self):
        """D4.3: Unicode 字符"""
        payload = {"name": "中文", "val": "café"}
        rust = callwarden_core.audit_canonical_json(json.dumps(payload, ensure_ascii=False))
        py = _py_canonical_json(payload)
        assert rust == py

    def test_d4_4_signature_sha256(self):
        """D4.4: SHA-256 签名（无 HMAC key）"""
        rust = callwarden_core.audit_compute_signature("", "abc123", None)
        py = _py_compute_signature("", "abc123", None)
        assert rust == py

    def test_d4_5_signature_hmac(self):
        """D4.5: HMAC-SHA256 签名"""
        key = b"secret_key_123"
        rust = callwarden_core.audit_compute_signature("prev_sig", "payload_hash", key)
        py = _py_compute_signature("prev_sig", "payload_hash", key)
        assert rust == py

    def test_d4_6_signature_chain(self):
        """D4.6: 链式签名（prev + payload）"""
        rust1 = callwarden_core.audit_compute_signature("", "hash1", None)
        py1 = _py_compute_signature("", "hash1", None)
        rust2 = callwarden_core.audit_compute_signature(rust1, "hash2", None)
        py2 = _py_compute_signature(py1, "hash2", None)
        assert rust1 == py1
        assert rust2 == py2

    def test_d4_7_signature_empty_inputs(self):
        """D4.7: 空输入签名"""
        rust = callwarden_core.audit_compute_signature("", "", None)
        py = _py_compute_signature("", "", None)
        assert rust == py

    def test_d4_8_signature_hmac_empty_key(self):
        """D4.8: 空 HMAC key（Some([])）"""
        rust = callwarden_core.audit_compute_signature("prev", "hash", b"")
        py = _py_compute_signature("prev", "hash", b"")
        assert rust == py


# ============================================================
# D5: backup_compute_file_sha256 + backup_compute_meta_checksum
# ============================================================


class TestD5BackupPureCompute:
    """D5: backup 纯计算行为一致性。"""

    def test_d5_1_file_sha256_small(self, tmp_path):
        """D5.1: 小文件 SHA-256"""
        test_file = tmp_path / "test.txt"
        content = b"hello world\n"
        test_file.write_bytes(content)

        rust = callwarden_core.backup_compute_file_sha256(str(test_file))
        py = hashlib.sha256(content).hexdigest()
        assert rust == py
        assert len(rust) == 64  # SHA-256 hex 长度

    def test_d5_2_file_sha256_large(self, tmp_path):
        """D5.2: 大文件 SHA-256（> 64KB，测试流式读取）"""
        test_file = tmp_path / "large.bin"
        content = b"x" * (65536 * 3 + 100)  # ~200KB
        test_file.write_bytes(content)

        rust = callwarden_core.backup_compute_file_sha256(str(test_file))
        py = hashlib.sha256(content).hexdigest()
        assert rust == py

    def test_d5_3_file_sha256_empty(self, tmp_path):
        """D5.3: 空文件 SHA-256"""
        test_file = tmp_path / "empty.txt"
        test_file.write_bytes(b"")

        rust = callwarden_core.backup_compute_file_sha256(str(test_file))
        py = hashlib.sha256(b"").hexdigest()
        assert rust == py

    def test_d5_4_file_sha256_binary(self, tmp_path):
        """D5.4: 二进制文件"""
        test_file = tmp_path / "data.bin"
        content = bytes(range(256)) * 10
        test_file.write_bytes(content)

        rust = callwarden_core.backup_compute_file_sha256(str(test_file))
        py = hashlib.sha256(content).hexdigest()
        assert rust == py

    def test_d5_5_file_sha256_not_found(self, tmp_path):
        """D5.5: 文件不存在 → PyRuntimeError"""
        with pytest.raises(Exception):
            callwarden_core.backup_compute_file_sha256(str(tmp_path / "missing.bin"))

    def test_d5_6_meta_checksum_with_checksum_field(self):
        """D5.6: meta 含 checksum 字段（应排除）"""
        meta = {"backup_id": "B-123", "timestamp": "2026-07-28", "checksum": "abc"}
        meta_json = json.dumps(meta)
        rust = callwarden_core.backup_compute_meta_checksum(meta_json)

        # Python 真相源：排除 checksum 字段
        meta_copy = {k: v for k, v in meta.items() if k != "checksum"}
        content = json.dumps(meta_copy, sort_keys=True, ensure_ascii=False)
        py = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert rust == py

    def test_d5_7_meta_checksum_without_checksum_field(self):
        """D5.7: meta 不含 checksum 字段"""
        meta = {"backup_id": "B-456", "files": ["a.db", "b.db"]}
        meta_json = json.dumps(meta)
        rust = callwarden_core.backup_compute_meta_checksum(meta_json)

        content = json.dumps(meta, sort_keys=True, ensure_ascii=False)
        py = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert rust == py

    def test_d5_8_meta_checksum_nested(self):
        """D5.8: 嵌套 meta"""
        meta = {
            "backup_id": "B-789",
            "files": [{"name": "a.db", "size": 100}, {"name": "b.db", "size": 200}],
            "checksum": "old_checksum",
        }
        meta_json = json.dumps(meta)
        rust = callwarden_core.backup_compute_meta_checksum(meta_json)

        meta_copy = {k: v for k, v in meta.items() if k != "checksum"}
        content = json.dumps(meta_copy, sort_keys=True, ensure_ascii=False)
        py = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert rust == py

    def test_d5_9_meta_checksum_unicode(self):
        """D5.9: meta 含 Unicode 字符"""
        meta = {"backup_id": "B-中文", "note": "café"}
        meta_json = json.dumps(meta, ensure_ascii=False)
        rust = callwarden_core.backup_compute_meta_checksum(meta_json)

        content = json.dumps(meta, sort_keys=True, ensure_ascii=False)
        py = hashlib.sha256(content.encode("utf-8")).hexdigest()
        assert rust == py
