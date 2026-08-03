"""Phase 4-1 UDS framing/SO_PEERCRED/RPC dispatch PyO3 暴露层差分测试。

**本文件是 manifest §7 中 Phase 4-1 的 ✅(behavioral) 标记载体。**

差分测试矩阵（契约 docs/design/phase4-1-uds-framing-contract.md §3-§5）：
  TestProtocolConstantsDiff：协议常量查询差分
    - C1: header_size = 4（与 Python HEADER.size 一致）
    - C2: default_max_message_bytes = 8*1024*1024（与 Python 一致）
    - C3: default_max_fds = 1（与 Python 一致）

  TestProtocolEncodePayloadDiff：帧编码差分（F1-F6）
    - F1: 空 dict {} → b"{}"
    - F2: 简单 dict {"a":1,"b":"hello"} → 紧凑 JSON
    - F3: 嵌套 dict {"a":{"b":[1,2,3]}} → 紧凑 JSON
    - F4: Unicode dict {"name":"中文"} → UTF-8 原生（非 \\uXXXX）
    - F5: 特殊字符 dict {"key":"a,b:c"} → 正确转义
    - F6: 非 dict（list/number/string）→ 抛异常

  TestProtocolDecodePayloadDiff：帧解码差分（R1-R4）
    - R1: 合法 payload → 正确 dict
    - R2: 非法 UTF-8 bytes → 抛异常
    - R3: 非法 JSON bytes → 抛异常
    - R4: 非 dict JSON（list/number）→ 抛异常

  TestProtocolBuildFrameDiff：完整帧构造差分
    - B1: header + payload 字节完全一致
    - B2: header 是 4 字节大端 u32

  TestProtocolParseHeaderDiff：header 解析差分
    - H1: 合法 header → payload 长度
    - H2: 不足 4 字节 → 抛异常

  TestProtocolValidateMessageSizeDiff：消息大小验证差分
    - S1: size=0 → 抛异常
    - S2: size=max_bytes → 合法
    - S3: size=max_bytes+1 → 抛异常
    - S4: 自定义 max_bytes

  TestProtocolParseResponseDiff：响应解析差分
    - P1: 成功响应 → 返回 result
    - P2: 失败响应 → 抛异常，含 code 和 message
    - P3: 默认错误 code/message
    - P4: 缺少 ok 字段 → 抛异常

  TestProtocolMakeResponseDiff：响应构造差分
    - M1: make_ok_response 结构一致
    - M2: make_error_response 结构一致

  TestPeercredDiff：peercred 跨平台查询
    - PC1: peercred_is_available 与平台一致
    - PC2: peercred_info 返回字段完整

  TestDispatchDiff：dispatch 路由表查询
    - D1: dispatch_list_methods 返回非空列表
    - D2: dispatch_list_error_codes 返回 6 个错误码
    - D3: dispatch_is_admin_method 对 admin 方法返回 True
    - D4: dispatch_is_admin_method 对非 admin 方法返回 False

预期差异（见契约 §4）：
  - JSON 序列化：Python json.dumps(ensure_ascii=False, separators=(",",":")) 与
    Rust serde_json::to_vec 应字节级一致（Rust 内部通过 pydict_to_json_value 委托
    Python json.dumps 确保 100% 一致）
  - 异常类型：Python 抛 ProtocolError/DaemonRemoteError，Rust 抛 RuntimeError；
    差分测试只验证是否抛异常 + 异常消息匹配，不比较异常类型

前置条件：
  - Rust 扩展 callwarden_core 必须可加载
  - 如果不可加载，本测试套件会显式 skip 并给出修复指引

关联：
  - 契约：docs/design/phase4-1-uds-framing-contract.md
  - Python 真相源：server/daemon_protocol.py
  - Rust 真相源：rust_ext/src/daemon_query.rs
"""
from __future__ import annotations

import json
import os
import struct
import sys
from typing import Any, Dict

import pytest

# ============================================
# 前置条件：Rust 扩展可用性检查
# ============================================

_PKG_PARENT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PKG_PARENT not in sys.path:
    sys.path.insert(0, _PKG_PARENT)

_RUST_EXT_AVAILABLE = False
_RUST_IMPORT_ERROR: str | None = None

try:
    from callwarden_core import (  # type: ignore[import-not-found]
        dispatch_is_admin_method,
        dispatch_list_error_codes,
        dispatch_list_methods,
        peercred_info,
        peercred_is_available,
        protocol_build_frame,
        protocol_constants,
        protocol_decode_payload,
        protocol_encode_payload,
        protocol_make_error_response,
        protocol_make_ok_response,
        protocol_parse_header,
        protocol_parse_response,
        protocol_validate_message_size,
    )
    _RUST_EXT_AVAILABLE = True
except ImportError as exc:
    _RUST_IMPORT_ERROR = str(exc)

# Python 真相源
from server.daemon_protocol import (  # noqa: E402
    DEFAULT_MAX_FDS,
    DEFAULT_MAX_MESSAGE_BYTES,
    HEADER,
    DaemonRemoteError,
    ProtocolError,
    parse_response,
)


pytestmark = pytest.mark.skipif(
    not _RUST_EXT_AVAILABLE,
    reason=f"callwarden_core 未安装: {_RUST_IMPORT_ERROR}",
)


# ============================================
# 辅助函数
# ============================================


def py_encode_payload(message: Dict[str, Any]) -> bytes:
    """Python 真相源：编码 payload（不含 header）。"""
    return json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def py_build_frame(message: Dict[str, Any]) -> bytes:
    """Python 真相源：构造完整帧。"""
    payload = py_encode_payload(message)
    return HEADER.pack(len(payload)) + payload


# ============================================
# TestProtocolConstantsDiff：协议常量查询差分
# ============================================


class TestProtocolConstantsDiff:
    """协议常量查询差分（C1-C3）。"""

    def test_c1_header_size(self):
        """C1: header_size = 4（与 Python HEADER.size 一致）。"""
        consts = protocol_constants()
        assert consts["header_size"] == HEADER.size == 4

    def test_c2_default_max_message_bytes(self):
        """C2: default_max_message_bytes = 8*1024*1024（与 Python 一致）。"""
        consts = protocol_constants()
        assert consts["default_max_message_bytes"] == DEFAULT_MAX_MESSAGE_BYTES == 8 * 1024 * 1024

    def test_c3_default_max_fds(self):
        """C3: default_max_fds = 1（与 Python 一致）。"""
        consts = protocol_constants()
        assert consts["default_max_fds"] == DEFAULT_MAX_FDS == 1


# ============================================
# TestProtocolEncodePayloadDiff：帧编码差分
# ============================================


class TestProtocolEncodePayloadDiff:
    """帧编码差分（F1-F6）。"""

    @pytest.mark.parametrize(
        "message",
        [
            {},  # F1: 空 dict
            {"a": 1, "b": "hello"},  # F2: 简单 dict
            {"a": {"b": [1, 2, 3]}},  # F3: 嵌套 dict
            {"name": "中文"},  # F4: Unicode
            {"key": "a,b:c"},  # F5: 特殊字符
            {"nested": {"unicode": "日本語"}},  # F4 补充
            {"num": 3.14, "flag": True, "null": None},  # 类型覆盖
        ],
    )
    def test_f1_f5_encode_diff(self, message):
        """F1-F5: Python 与 Rust 编码结果字节级一致。"""
        py_payload = py_encode_payload(message)
        rust_payload = protocol_encode_payload(message)
        assert bytes(rust_payload) == py_payload

    def test_f4_unicode_native(self):
        """F4: Unicode 字符以 UTF-8 原生编码，非 \\uXXXX 转义。"""
        message = {"name": "中文"}
        rust_payload = bytes(protocol_encode_payload(message))
        # UTF-8 原生：包含 "中文" 的 UTF-8 字节，而非 "\\u4e2d\\u6587"
        assert "中文".encode("utf-8") in rust_payload
        assert "\\u" not in rust_payload.decode("utf-8")

    def test_f6_non_dict_raises(self):
        """F6: 非 dict 输入抛异常（list/number/string）。"""
        with pytest.raises(Exception):
            protocol_encode_payload([1, 2, 3])  # type: ignore[arg-type]


# ============================================
# TestProtocolDecodePayloadDiff：帧解码差分
# ============================================


class TestProtocolDecodePayloadDiff:
    """帧解码差分（R1-R4）。"""

    def test_r1_legal_payload(self):
        """R1: 合法 payload → 正确 dict。"""
        original = {"a": 1, "b": "hello", "c": [1, 2, 3]}
        payload = py_encode_payload(original)
        result = protocol_decode_payload(payload)
        assert dict(result) == original

    def test_r1_unicode_payload(self):
        """R1 补充: Unicode payload 正确解码。"""
        original = {"name": "中文", "value": "日本語"}
        payload = py_encode_payload(original)
        result = protocol_decode_payload(payload)
        assert dict(result) == original

    def test_r2_invalid_utf8_raises(self):
        """R2: 非法 UTF-8 bytes → 抛异常。"""
        invalid_utf8 = b"\xff\xfe\x00\x01"
        with pytest.raises(Exception):
            protocol_decode_payload(invalid_utf8)

    def test_r3_invalid_json_raises(self):
        """R3: 非法 JSON bytes → 抛异常。"""
        invalid_json = b"not a json"
        with pytest.raises(Exception):
            protocol_decode_payload(invalid_json)

    def test_r4_non_dict_json_raises(self):
        """R4: 非 dict JSON（list/number）→ 抛异常。"""
        with pytest.raises(Exception):
            protocol_decode_payload(b"[1, 2, 3]")
        with pytest.raises(Exception):
            protocol_decode_payload(b"42")


# ============================================
# TestProtocolBuildFrameDiff：完整帧构造差分
# ============================================


class TestProtocolBuildFrameDiff:
    """完整帧构造差分（B1-B2）。"""

    @pytest.mark.parametrize(
        "message",
        [
            {},
            {"a": 1},
            {"name": "中文"},
            {"nested": {"b": [1, 2, 3]}},
        ],
    )
    def test_b1_frame_diff(self, message):
        """B1: header + payload 字节级一致。"""
        py_frame = py_build_frame(message)
        rust_frame = bytes(protocol_build_frame(message))
        assert rust_frame == py_frame

    def test_b2_header_is_be_u32(self):
        """B2: header 是 4 字节大端 u32。"""
        message = {"a": 1}
        rust_frame = bytes(protocol_build_frame(message))
        payload = py_encode_payload(message)
        expected_header = struct.pack("!I", len(payload))
        assert rust_frame[:4] == expected_header
        assert rust_frame[4:] == payload


# ============================================
# TestProtocolParseHeaderDiff：header 解析差分
# ============================================


class TestProtocolParseHeaderDiff:
    """header 解析差分（H1-H2）。"""

    def test_h1_legal_header(self):
        """H1: 合法 header → payload 长度。"""
        for size in [1, 100, 8388608, 4294967295]:
            header = struct.pack("!I", size)
            assert protocol_parse_header(header) == size

    def test_h2_short_header_raises(self):
        """H2: 不足 4 字节 → 抛异常。"""
        with pytest.raises(Exception):
            protocol_parse_header(b"\x00\x00\x01")
        with pytest.raises(Exception):
            protocol_parse_header(b"")


# ============================================
# TestProtocolValidateMessageSizeDiff：消息大小验证差分
# ============================================


class TestProtocolValidateMessageSizeDiff:
    """消息大小验证差分（S1-S4）。"""

    def test_s1_size_zero_raises(self):
        """S1: size=0 → 抛异常。"""
        with pytest.raises(Exception):
            protocol_validate_message_size(0)

    def test_s2_size_at_max_ok(self):
        """S2: size=max_bytes → 合法（不抛异常）。"""
        # 默认 max_bytes
        protocol_validate_message_size(DEFAULT_MAX_MESSAGE_BYTES)
        # 自定义 max_bytes
        protocol_validate_message_size(100, 100)

    def test_s3_size_over_max_raises(self):
        """S3: size=max_bytes+1 → 抛异常。"""
        with pytest.raises(Exception):
            protocol_validate_message_size(DEFAULT_MAX_MESSAGE_BYTES + 1)
        with pytest.raises(Exception):
            protocol_validate_message_size(101, 100)

    def test_s4_custom_max_bytes(self):
        """S4: 自定义 max_bytes 边界。"""
        # 正好等于自定义 max
        protocol_validate_message_size(50, 50)
        # 超过自定义 max
        with pytest.raises(Exception):
            protocol_validate_message_size(51, 50)


# ============================================
# TestProtocolParseResponseDiff：响应解析差分
# ============================================


class TestProtocolParseResponseDiff:
    """响应解析差分（P1-P4）。"""

    def test_p1_success_response(self):
        """P1: 成功响应 → 返回 result。"""
        response = {"ok": True, "result": "hello"}
        py_result = parse_response(response)
        rust_result = protocol_parse_response(response)
        assert rust_result == py_result == "hello"

    def test_p1_success_with_complex_result(self):
        """P1 补充: 复杂 result（dict/list）。"""
        result_data = {"key": "value", "num": 42, "items": [1, 2, 3]}
        response = {"ok": True, "result": result_data}
        py_result = parse_response(response)
        rust_result = protocol_parse_response(response)
        assert rust_result == py_result

    def test_p1_success_null_result(self):
        """P1 补充: result=None（缺省）。"""
        response = {"ok": True}
        py_result = parse_response(response)
        rust_result = protocol_parse_response(response)
        assert rust_result is None or rust_result == py_result

    def test_p2_error_response_raises(self):
        """P2: 失败响应 → 抛异常，含 code 和 message。"""
        response = {
            "ok": False,
            "error": {"code": "test_error", "message": "test message"},
        }
        # Python 端抛 DaemonRemoteError
        with pytest.raises(DaemonRemoteError) as py_exc_info:
            parse_response(response)
        assert py_exc_info.value.code == "test_error"
        assert py_exc_info.value.message == "test message"

        # Rust 端抛 RuntimeError
        with pytest.raises(Exception) as rust_exc_info:
            protocol_parse_response(response)
        assert "test_error" in str(rust_exc_info.value)
        assert "test message" in str(rust_exc_info.value)

    def test_p3_default_error_code_message(self):
        """P3: 默认错误 code/message（error 缺字段时）。"""
        response = {"ok": False, "error": {}}
        with pytest.raises(DaemonRemoteError) as py_exc_info:
            parse_response(response)
        assert py_exc_info.value.code == "daemon_error"
        assert py_exc_info.value.message == "unknown daemon error"

        with pytest.raises(Exception) as rust_exc_info:
            protocol_parse_response(response)
        assert "daemon_error" in str(rust_exc_info.value)

    def test_p4_missing_ok_field_raises(self):
        """P4: 缺少 ok 字段 → 抛异常（Python 视为非 True）。"""
        response = {"result": "hello"}
        # Python: response.get("ok") is True → False → 抛 DaemonRemoteError
        with pytest.raises(DaemonRemoteError):
            parse_response(response)
        # Rust: 同样抛异常
        with pytest.raises(Exception):
            protocol_parse_response(response)


# ============================================
# TestProtocolMakeResponseDiff：响应构造差分
# ============================================


class TestProtocolMakeResponseDiff:
    """响应构造差分（M1-M2）。"""

    def test_m1_make_ok_response(self):
        """M1: make_ok_response 结构一致。"""
        rust_resp = dict(protocol_make_ok_response("hello"))
        assert rust_resp["ok"] is True
        assert rust_resp["result"] == "hello"

    def test_m1_make_ok_response_with_dict(self):
        """M1 补充: result 为 dict。"""
        result_data = {"key": "value", "num": 42}
        rust_resp = dict(protocol_make_ok_response(result_data))
        assert rust_resp["ok"] is True
        assert rust_resp["result"] == result_data

    def test_m2_make_error_response(self):
        """M2: make_error_response 结构一致。"""
        rust_resp = dict(protocol_make_error_response("test_code", "test message"))
        assert rust_resp["ok"] is False
        assert rust_resp["error"]["code"] == "test_code"
        assert rust_resp["error"]["message"] == "test message"


# ============================================
# TestPeercredDiff：peercred 跨平台查询
# ============================================


class TestPeercredDiff:
    """peercred 跨平台查询差分。"""

    def test_pc1_peercred_is_available_matches_platform(self):
        """PC1: peercred_is_available 与平台一致。"""
        rust_available = peercred_is_available()
        if sys.platform == "win32":
            assert rust_available is False
        else:
            assert rust_available is True

    def test_pc2_peercred_info_fields(self):
        """PC2: peercred_info 返回字段完整。"""
        info = dict(peercred_info())
        assert "available" in info
        assert "platform" in info
        assert "supports_pid" in info
        assert "method" in info

        if sys.platform == "win32":
            assert info["available"] is False
            assert info["platform"] == "windows"
            assert info["supports_pid"] is False
            assert info["method"] == "unsupported"
        elif sys.platform.startswith("linux"):
            assert info["available"] is True
            assert info["platform"] == "linux"
            assert info["supports_pid"] is True
            assert info["method"] == "SO_PEERCRED"
        elif sys.platform == "darwin":
            assert info["available"] is True
            assert info["platform"] == "macos"
            assert info["supports_pid"] is False
            assert info["method"] == "LOCAL_PEERCRED"


# ============================================
# TestDispatchDiff：dispatch 路由表查询
# ============================================


class TestDispatchDiff:
    """dispatch 路由表查询差分。"""

    def test_d1_list_methods_non_empty(self):
        """D1: dispatch_list_methods 返回非空列表，每项含 method/description/admin_only。"""
        methods = dispatch_list_methods()
        assert len(methods) > 0
        method_names = set()
        for m in methods:
            d = dict(m)
            assert "method" in d
            assert "description" in d
            assert "admin_only" in d
            assert isinstance(d["admin_only"], bool)
            method_names.add(d["method"])
        # 验证基础方法存在
        assert "ping" in method_names
        assert "health" in method_names
        assert "schema.version" in method_names

    def test_d2_list_error_codes_count(self):
        """D2: dispatch_list_error_codes 返回 6 个错误码。"""
        codes = dispatch_list_error_codes()
        assert len(codes) == 6
        code_names = {dict(c)["code"] for c in codes}
        expected_codes = {
            "invalid_params",
            "method_not_found",
            "internal_error",
            "permission_denied",
            "workspace_not_found",
            "workspace_forbidden",
        }
        assert code_names == expected_codes

    @pytest.mark.parametrize(
        "method,expected_admin",
        [
            ("backup", True),
            ("restore", True),
            ("gc.cas", True),
            ("gc.snapshots", True),
            ("snapshot.evict", True),
            ("mount.register", True),
            ("mount.delete", True),
            ("mount.list", True),
            ("toolchain.register", True),
            ("toolchain.delete", True),
            ("toolchain.bind", True),
            ("build_context.register", False),
            ("build_context.set_active", False),
            ("build_context.delete", False),
            # 非 admin 方法
            ("ping", False),
            ("health", False),
            ("schema.version", False),
            ("workspace.register", False),
            ("workspace.list", False),
            ("query.symbol", False),
            ("query.search", False),
            ("query.callers", False),
            ("query.callees", False),
            ("query.call_chain_down", False),
            ("toolchain.list", False),
            ("toolchain.get", False),
            ("toolchain.resolve", False),
            ("build_context.list", False),
            ("resolved_edges.store", False),
            ("resolved_edges.get", False),
            ("resolved_edges.count", False),
        ],
    )
    def test_d3_d4_is_admin_method(self, method, expected_admin):
        """D3/D4: dispatch_is_admin_method 对 admin 方法返回 True，非 admin 返回 False。"""
        assert dispatch_is_admin_method(method) is expected_admin

    def test_d5_unknown_method_not_admin(self):
        """D5: 未知方法不是 admin-only。"""
        assert dispatch_is_admin_method("nonexistent.method") is False
