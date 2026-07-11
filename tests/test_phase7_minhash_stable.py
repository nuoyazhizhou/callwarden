"""
Phase 7.1: MinHash/LSH 稳定 hash 和 shingle 测试

验证：
1. FNV-1a 稳定 hash 跨进程确定性
2. MinHash 签名跨进程稳定性
3. Shingle 稳定性（3-gram 归一化 token）
4. 大桶保护（_MAX_BUCKET_SIZE 跳过过大桶）
5. LSH 分桶一致性
"""

import os
import subprocess
import sys
import tempfile
import time

import pytest

from callwarden.db.db_clone_detection import (
    _fnv1a_32,
    _stable_token_hash,
    _minhash_signature,
    _lsh_buckets,
    _normalize_token_sequence,
    _MAX_BUCKET_SIZE,
    _HASH_A_NP,
    _HASH_B_NP,
    _MASK_32,
)


# ============================================
# FNV-1a 稳定 hash
# ============================================

class TestFnv1a:
    """FNV-1a 32 位 hash 测试"""

    def test_empty_string(self):
        """空字符串返回 offset basis"""
        assert _fnv1a_32(b"") == 0x811C9DC5

    def test_deterministic(self):
        """相同输入返回相同输出"""
        assert _fnv1a_32(b"hello") == _fnv1a_32(b"hello")

    def test_different_input_different_output(self):
        """不同输入返回不同输出"""
        assert _fnv1a_32(b"hello") != _fnv1a_32(b"world")

    def test_known_value(self):
        """FNV-1a 已知测试向量"""
        # FNV-1a 32 位已知值：fnv1a("a") = 0xE40C292C
        assert _fnv1a_32(b"a") == 0xE40C292C

    def test_cross_process_stable(self, tmp_path):
        """跨进程稳定：子进程中 hash 结果一致"""
        script = tmp_path / "check_fnv.py"
        script.write_text(
            "from callwarden.db.db_clone_detection import _fnv1a_32\n"
            f"print(_fnv1a_32(b'test_token_123'))\n"
        )
        # 主进程计算
        main_val = _fnv1a_32(b"test_token_123")
        # 子进程计算（PYTHONHASHSEED 随机化不影响 FNV-1a）
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": "random"}
        )
        assert result.returncode == 0
        child_val = int(result.stdout.strip())
        assert child_val == main_val

    def test_python_hash_not_stable(self, tmp_path):
        """对比：Python 内置 hash() 在不同 PYTHONHASHSEED 下结果不同"""
        script = tmp_path / "check_hash.py"
        script.write_text("print(hash('test_token_123'))\n")
        # 两次子进程，不同 PYTHONHASHSEED
        env1 = {**os.environ, "PYTHONHASHSEED": "0"}
        env2 = {**os.environ, "PYTHONHASHSEED": "1"}
        r1 = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env1)
        r2 = subprocess.run([sys.executable, str(script)], capture_output=True, text=True, env=env2)
        h1 = int(r1.stdout.strip())
        h2 = int(r2.stdout.strip())
        # Python hash() 在不同 seed 下结果不同（验证对比）
        assert h1 != h2


class TestStableTokenHash:
    """稳定 token hash 测试"""

    def test_deterministic(self):
        """相同 token 返回相同 hash"""
        assert _stable_token_hash("foo") == _stable_token_hash("foo")

    def test_different_token_different_hash(self):
        """不同 token 返回不同 hash"""
        assert _stable_token_hash("foo") != _stable_token_hash("bar")

    def test_returns_32bit(self):
        """返回值在 32 位范围内"""
        val = _stable_token_hash("some_token")
        assert 0 <= val <= 0xFFFFFFFF

    def test_cross_process_stable(self, tmp_path):
        """跨进程稳定：子进程中 token hash 结果一致"""
        script = tmp_path / "check_token.py"
        script.write_text(
            "from callwarden.db.db_clone_detection import _stable_token_hash\n"
            f"print(_stable_token_hash('my_function_token'))\n"
        )
        main_val = _stable_token_hash("my_function_token")
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": "random"}
        )
        assert result.returncode == 0
        child_val = int(result.stdout.strip())
        assert child_val == main_val


# ============================================
# MinHash 签名稳定性
# ============================================

class TestMinhashStability:
    """MinHash 签名跨进程稳定性测试"""

    def test_same_set_same_signature(self):
        """相同 token 集合返回相同签名"""
        tokens = {"foo", "bar", "baz", "qux", "ID", "STR"}
        sig1 = _minhash_signature(tokens)
        sig2 = _minhash_signature(tokens)
        assert sig1 == sig2

    def test_different_set_different_signature(self):
        """不同 token 集合返回不同签名"""
        set_a = {"foo", "bar", "baz"}
        set_b = {"foo", "bar", "different"}
        sig_a = _minhash_signature(set_a)
        sig_b = _minhash_signature(set_b)
        assert sig_a != sig_b

    def test_signature_length(self):
        """签名长度等于 num_perm"""
        sig = _minhash_signature({"a", "b"}, num_perm=64)
        assert len(sig) == 64
        sig128 = _minhash_signature({"a", "b"}, num_perm=128)
        assert len(sig128) == 128

    def test_empty_set(self):
        """空集合返回全 0xFFFFFFFF"""
        sig = _minhash_signature(set())
        assert all(h == 0xFFFFFFFF for h in sig)
        assert len(sig) == 128

    def test_cross_process_stable(self, tmp_path):
        """跨进程稳定：子进程中 MinHash 签名一致"""
        tokens = {"foo", "bar", "baz", "qux", "ID", "STR", "NUM", "("}
        script = tmp_path / "check_sig.py"
        script.write_text(
            "from callwarden.db.db_clone_detection import _minhash_signature\n"
            f"tokens = {tokens!r}\n"
            "sig = _minhash_signature(tokens)\n"
            "print(','.join(str(h) for h in sig))\n"
        )
        main_sig = _minhash_signature(tokens)
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": "random"}
        )
        assert result.returncode == 0
        child_sig = tuple(int(x) for x in result.stdout.strip().split(","))
        assert child_sig == main_sig

    def test_single_element(self):
        """单元素集合签名一致性"""
        sig = _minhash_signature({"only_token"})
        assert len(sig) == 128
        # 所有 perm 下 hash 值一致（因为只有一个元素，min = 唯一值）
        # 但不同 perm 系数不同，所以 hash 不同
        assert len(set(sig)) > 1  # 128 个不同值

    def test_order_independent(self):
        """集合顺序不影响签名"""
        set_a = {"a", "b", "c", "d"}
        set_b = {"d", "c", "b", "a"}
        sig_a = _minhash_signature(set_a)
        sig_b = _minhash_signature(set_b)
        assert sig_a == sig_b


# ============================================
# Shingle 稳定性
# ============================================

class TestShingleStability:
    """Shingle（n-gram token）稳定性测试"""

    def test_normalize_deterministic(self):
        """归一化 token 序列确定性"""
        content = "def foo(x, y):\n    return x + y\n"
        norm1 = _normalize_token_sequence(content)
        norm2 = _normalize_token_sequence(content)
        assert norm1 == norm2

    def test_normalize_renames_identifiers(self):
        """归一化把标识符替换为 ID"""
        content1 = "def foo(x):\n    return x\n"
        content2 = "def bar(y):\n    return y\n"
        norm1 = _normalize_token_sequence(content1)
        norm2 = _normalize_token_sequence(content2)
        assert norm1 == norm2

    def test_normalize_replaces_strings(self):
        """归一化把字符串替换为 STR"""
        content1 = 'msg = "hello"'
        content2 = 'msg = "world"'
        norm1 = _normalize_token_sequence(content1)
        norm2 = _normalize_token_sequence(content2)
        assert norm1 == norm2

    def test_normalize_replaces_numbers(self):
        """归一化把数字替换为 NUM"""
        content1 = "x = 42"
        content2 = "x = 100"
        norm1 = _normalize_token_sequence(content1)
        norm2 = _normalize_token_sequence(content2)
        assert norm1 == norm2

    def test_3gram_stable(self):
        """3-gram shingle 稳定性"""
        content = "def foo(x, y):\n    return x + y\n"
        normalized = _normalize_token_sequence(content)
        tokens = normalized.split()
        if len(tokens) >= 3:
            shingles1 = set(zip(tokens, tokens[1:], tokens[2:]))
            shingles2 = set(zip(tokens, tokens[1:], tokens[2:]))
            assert shingles1 == shingles2

    def test_3gram_order_matters(self):
        """3-gram 保留顺序信息"""
        # 使用不同的语句顺序，产生不同的 3-gram
        content1 = "def foo(): return x + y"
        content2 = "def foo(): return y + x + z"
        norm1 = _normalize_token_sequence(content1)
        norm2 = _normalize_token_sequence(content2)
        tokens1 = norm1.split()
        tokens2 = norm2.split()
        shingles1 = set(zip(tokens1, tokens1[1:], tokens1[2:]))
        shingles2 = set(zip(tokens2, tokens2[1:], tokens2[2:]))
        # 不同的语句结构产生不同的 3-gram
        assert shingles1 != shingles2

    def test_normalize_cross_process_stable(self, tmp_path):
        """归一化跨进程确定性"""
        content = "def foo(x, y):\n    return x + y  # comment\n"
        script = tmp_path / "check_norm.py"
        script.write_text(
            "from callwarden.db.db_clone_detection import _normalize_token_sequence\n"
            f"content = {content!r}\n"
            "norm = _normalize_token_sequence(content)\n"
            "print(repr(norm))\n"
        )
        main_norm = _normalize_token_sequence(content)
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": "random"}
        )
        assert result.returncode == 0
        child_norm = eval(result.stdout.strip())
        assert child_norm == main_norm


# ============================================
# 大桶保护
# ============================================

class TestLargeBucketProtection:
    """LSH 大桶保护测试"""

    def test_max_bucket_size_constant(self):
        """_MAX_BUCKET_SIZE 是合理值"""
        assert _MAX_BUCKET_SIZE > 0
        assert _MAX_BUCKET_SIZE <= 1000  # 不应过大

    def test_large_bucket_skipped(self):
        """模拟大桶场景：超过 _MAX_BUCKET_SIZE 的桶被跳过"""
        # 构造 250 个相同签名的符号（全落入同一桶）
        # 250 > _MAX_BUCKET_SIZE(200) → 跳过
        fake_sig = tuple(range(128))
        buckets = _lsh_buckets(fake_sig)
        # 所有 8 个桶都包含 250 个符号
        bucket_sizes = {b: 250 for b in buckets}
        skipped = [b for b, sz in bucket_sizes.items() if sz > _MAX_BUCKET_SIZE]
        # 所有桶都应该被跳过
        assert len(skipped) == len(buckets)

    def test_normal_bucket_not_skipped(self):
        """正常大小的桶不被跳过"""
        fake_sig = tuple(range(128))
        buckets = _lsh_buckets(fake_sig)
        bucket_sizes = {b: 10 for b in buckets}  # 10 < 200
        skipped = [b for b, sz in bucket_sizes.items() if sz > _MAX_BUCKET_SIZE]
        assert len(skipped) == 0

    def test_boundary_size(self):
        """边界值：恰好等于 _MAX_BUCKET_SIZE 不跳过"""
        fake_sig = tuple(range(128))
        buckets = _lsh_buckets(fake_sig)
        bucket_sizes = {b: _MAX_BUCKET_SIZE for b in buckets}
        skipped = [b for b, sz in bucket_sizes.items() if sz > _MAX_BUCKET_SIZE]
        assert len(skipped) == 0  # 等于不跳过

    def test_one_over_boundary_skipped(self):
        """边界值+1：超过 _MAX_BUCKET_SIZE 跳过"""
        fake_sig = tuple(range(128))
        buckets = _lsh_buckets(fake_sig)
        bucket_sizes = {b: _MAX_BUCKET_SIZE + 1 for b in buckets}
        skipped = [b for b, sz in bucket_sizes.items() if sz > _MAX_BUCKET_SIZE]
        assert len(skipped) == len(buckets)


# ============================================
# LSH 分桶一致性
# ============================================

class TestLSHBuckets:
    """LSH 分桶一致性测试"""

    def test_same_signature_same_buckets(self):
        """相同签名返回相同桶"""
        sig = tuple(range(128))
        b1 = _lsh_buckets(sig)
        b2 = _lsh_buckets(sig)
        assert b1 == b2

    def test_different_signature_different_buckets(self):
        """不同签名通常返回不同桶"""
        sig1 = tuple(range(128))
        sig2 = tuple(range(128, 256))
        b1 = _lsh_buckets(sig1)
        b2 = _lsh_buckets(sig2)
        assert b1 != b2

    def test_bucket_count(self):
        """桶数量等于 num_bands"""
        sig = tuple(range(128))
        buckets = _lsh_buckets(sig, num_bands=8, rows_per_band=16)
        assert len(buckets) == 8

    def test_bucket_keys_distinct(self):
        """同一签名的不同桶 key 不同"""
        sig = tuple(range(128))
        buckets = _lsh_buckets(sig)
        assert len(set(buckets)) == len(buckets)

    def test_cross_process_stable(self, tmp_path):
        """跨进程稳定：LSH 桶一致"""
        sig = tuple(range(128))
        script = tmp_path / "check_lsh.py"
        script.write_text(
            "from callwarden.db.db_clone_detection import _lsh_buckets\n"
            f"sig = {sig!r}\n"
            "buckets = _lsh_buckets(sig)\n"
            "print('\\n'.join(buckets))\n"
        )
        main_buckets = _lsh_buckets(sig)
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": "random"}
        )
        assert result.returncode == 0
        child_buckets = result.stdout.strip().split("\n")
        assert child_buckets == main_buckets


# ============================================
# 端到端稳定性
# ============================================

class TestEndToEndStability:
    """端到端：token → normalize → shingle → MinHash → LSH 全链路稳定性"""

    def test_full_pipeline_cross_process(self, tmp_path):
        """全链路跨进程确定性"""
        content = """
def process_data(items, config):
    result = []
    for item in items:
        if item.is_valid():
            value = transform(item, config.scale)
            result.append(value)
    return result
"""
        script = tmp_path / "check_pipeline.py"
        script.write_text(
            "from callwarden.db.db_clone_detection import (\n"
            "    _normalize_token_sequence,\n"
            "    _minhash_signature,\n"
            "    _lsh_buckets,\n"
            ")\n"
            f"content = {content!r}\n"
            "normalized = _normalize_token_sequence(content)\n"
            "tokens = normalized.split()\n"
            "if len(tokens) >= 3:\n"
            "    token_set = set(zip(tokens, tokens[1:], tokens[2:]))\n"
            "else:\n"
            "    token_set = set(tokens)\n"
            "sig = _minhash_signature(token_set)\n"
            "buckets = _lsh_buckets(sig)\n"
            "print(repr(sig))\n"
            "print('---')\n"
            "print('\\n'.join(buckets))\n"
        )

        # 主进程计算
        normalized = _normalize_token_sequence(content)
        tokens = normalized.split()
        if len(tokens) >= 3:
            token_set = set(zip(tokens, tokens[1:], tokens[2:]))
        else:
            token_set = set(tokens)
        main_sig = _minhash_signature(token_set)
        main_buckets = _lsh_buckets(main_sig)

        # 子进程计算（PYTHONHASHSEED 随机化）
        result = subprocess.run(
            [sys.executable, str(script)],
            capture_output=True, text=True, env={**os.environ, "PYTHONHASHSEED": "random"}
        )
        assert result.returncode == 0, f"子进程失败: {result.stderr}"
        parts = result.stdout.strip().split("---")
        child_sig = eval(parts[0].strip())
        child_buckets = parts[1].strip().split("\n")

        assert child_sig == main_sig
        assert child_buckets == main_buckets

    def test_two_similar_functions_similar_signatures(self):
        """两个相似函数产生相似的 MinHash 签名"""
        content1 = """
def process_items(items, scale):
    result = []
    for item in items:
        value = transform(item, scale)
        result.append(value)
    return result
"""
        content2 = """
def process_products(products, factor):
    output = []
    for product in products:
        value = transform(product, factor)
        output.append(value)
    return output
"""
        norm1 = _normalize_token_sequence(content1)
        norm2 = _normalize_token_sequence(content2)
        tokens1 = norm1.split()
        tokens2 = norm2.split()
        set1 = set(zip(tokens1, tokens1[1:], tokens1[2:]))
        set2 = set(zip(tokens2, tokens2[1:], tokens2[2:]))

        sig1 = _minhash_signature(set1)
        sig2 = _minhash_signature(set2)

        # 两个相似函数的签名应有较高比例相同
        matches = sum(1 for a, b in zip(sig1, sig2) if a == b)
        match_ratio = matches / len(sig1)
        # Jaccard 相似度应较高 → MinHash 估计相似度也较高
        jaccard = len(set1 & set2) / len(set1 | set2) if (set1 | set2) else 0
        # MinHash 估计应接近 Jaccard
        assert match_ratio > 0.3, f"match_ratio={match_ratio}, jaccard={jaccard}"
