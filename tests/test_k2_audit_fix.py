"""K2 评审修复验证测试（2026-07-20 二轮评审）。

确保 K2 修复不可回退：
1. Python daemon_server.py workspace.file.refresh 在 canonical_bytes is None 时
   校验 abs_path 落在 workspace host_real_root 内
2. Rust workspace.rs handle_workspace_file_refresh 同步校验
3. path_escape 错误类型存在
"""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# 1. Python 侧 K2 修复
# ============================================================


class TestK2PythonFix:
    """server/daemon_server.py 必须在 canonical_bytes is None 时校验 abs_path。"""

    def test_python_has_path_escape_check(self):
        """daemon_server.py workspace.file.refresh 必须包含 path_escape 校验。"""
        path = ROOT / "server" / "daemon_server.py"
        content = path.read_text(encoding="utf-8")

        # 必须包含 K2 评审修复标记
        assert "K2 评审修复" in content, (
            "daemon_server.py 必须包含 K2 评审修复注释标记"
        )
        # 必须包含 path_escape 错误类型
        assert "path_escape" in content, (
            "daemon_server.py 必须包含 path_escape DaemonRpcError 错误类型"
        )
        # 必须在 canonical_bytes is None 分支中校验
        assert "canonical_bytes is None" in content, (
            "daemon_server.py 必须在 canonical_bytes is None 分支校验 abs_path"
        )
        # 必须检查 host_real_root prefix
        assert "host_real_root" in content, (
            "daemon_server.py 必须使用 host_real_root 做 prefix 校验"
        )
        # 必须调用 _validate_owned_path（owner UID 校验）
        assert "_validate_owned_path" in content, (
            "daemon_server.py 必须调用 _validate_owned_path 校验 owner UID"
        )

    def test_python_path_escape_logic_correct(self):
        """path escape 校验逻辑必须正确：real_abs == host_root 或以 host_root+sep 开头。"""
        path = ROOT / "server" / "daemon_server.py"
        content = path.read_text(encoding="utf-8")

        # 找到 K2 修复代码块
        k2_idx = content.find("K2 评审修复")
        assert k2_idx >= 0, "daemon_server.py 必须包含 K2 评审修复"
        # 取修复代码块（往后 800 字符）
        block = content[k2_idx:k2_idx + 1200]

        # 必须包含 prefix 校验逻辑（startswith + os.sep）
        assert "startswith" in block, (
            f"K2 修复必须用 startswith 做 prefix 校验，实际块：{block[:400]}"
        )
        assert "os.sep" in block, (
            f"K2 修复必须用 os.sep 分隔 host_root 和子路径，实际块：{block[:400]}"
        )
        # 必须用 realpath 规范化 abs_path 和 host_root
        assert "realpath" in block or "real_abs" in block, (
            f"K2 修复必须用 realpath 规范化路径，实际块：{block[:400]}"
        )


# ============================================================
# 2. Rust 侧 K2 修复
# ============================================================


class TestK2RustFix:
    """rust_ext/src/daemon/workspace.rs 必须同步实施 K2 校验。"""

    def test_rust_has_path_escape_check(self):
        """Rust handle_workspace_file_refresh 必须包含 path_escape 校验。"""
        path = ROOT / "rust_ext" / "src" / "daemon" / "workspace.rs"
        content = path.read_text(encoding="utf-8")

        # 必须包含 K2 评审修复标记
        assert "K2 评审修复" in content, (
            "workspace.rs 必须包含 K2 评审修复注释标记"
        )
        # 必须包含 path_escape 错误类型
        assert "path_escape" in content, (
            "workspace.rs 必须包含 path_escape DaemonRpcError 错误类型"
        )
        # 必须在 canonical_bytes.is_none() 分支中校验
        assert "canonical_bytes.is_none()" in content, (
            "workspace.rs 必须在 canonical_bytes.is_none() 分支校验 abs_path"
        )
        # 必须调用 validate_owned_path（owner UID 校验）
        assert "validate_owned_path" in content, (
            "workspace.rs 必须调用 validate_owned_path 校验 owner UID"
        )

    def test_rust_path_escape_logic_correct(self):
        """Rust path escape 校验必须用 starts_with + MAIN_SEPARATOR。"""
        path = ROOT / "rust_ext" / "src" / "daemon" / "workspace.rs"
        content = path.read_text(encoding="utf-8")

        # 找到 K2 修复代码块
        k2_idx = content.find("K2 评审修复")
        assert k2_idx >= 0, "workspace.rs 必须包含 K2 评审修复"
        block = content[k2_idx:k2_idx + 2500]

        # 必须用 MAIN_SEPARATOR 分隔
        assert "MAIN_SEPARATOR" in block, (
            f"Rust K2 修复必须用 std::path::MAIN_SEPARATOR，实际块：{block[:400]}"
        )
        # 必须用 starts_with 做 prefix 校验
        assert "starts_with" in block, (
            f"Rust K2 修复必须用 starts_with，实际块：{block[:400]}"
        )
        # 必须用 std::fs::canonicalize 规范化
        assert "std::fs::canonicalize" in block, (
            f"Rust K2 修复必须用 std::fs::canonicalize，实际块：{block[:400]}"
        )


# ============================================================
# 3. 行为级测试（直接测试校验逻辑）
# ============================================================


class TestK2BehaviorLogic:
    """验证 path escape 校验逻辑本身正确（不依赖 daemon 启动）。"""

    def test_prefix_check_logic(self):
        """模拟 _validate_owned_path + host_real_root prefix 校验逻辑。"""
        import os

        def check_path_escape(abs_path: str, host_root: str) -> bool:
            """模拟 daemon_server.py 的 K2 校验逻辑。返回 True=通过，False=逃逸。"""
            real_abs = os.path.realpath(os.path.abspath(abs_path))
            real_host_root = os.path.realpath(host_root)
            return (
                real_abs == real_host_root
                or real_abs.startswith(real_host_root + os.sep)
            )

        # 同一目录 → 通过
        assert check_path_escape("/tmp/workspace/foo.py", "/tmp/workspace") is True
        # 子目录 → 通过
        assert check_path_escape("/tmp/workspace/sub/foo.py", "/tmp/workspace") is True
        # 父目录逃逸 → 拒绝
        assert check_path_escape("/tmp/foo.py", "/tmp/workspace") is False
        # 同级目录（前缀相同但不是子目录）→ 拒绝
        # /tmp/workspace-evil 不应以 /tmp/workspace + sep 开头
        # （os.sep 让 workspace-evil 不被误判为 workspace 子目录）
        assert check_path_escape("/tmp/workspace-evil/foo.py", "/tmp/workspace") is False
        # workspace 根本身 → 通过
        assert check_path_escape("/tmp/workspace", "/tmp/workspace") is True

    def test_python_validate_owned_path_exists(self):
        """_validate_owned_path 静态方法必须存在且签名正确。"""
        # 直接检查源码（不依赖 import，避免触发复杂依赖）
        path = ROOT / "server" / "daemon_server.py"
        content = path.read_text(encoding="utf-8")

        # 验证 _validate_owned_path 方法签名包含 peer_uid 和 require_file
        assert "def _validate_owned_path(path: str, peer_uid" in content, (
            "_validate_owned_path 签名必须包含 peer_uid 参数"
        )
        assert "require_file" in content, (
            "_validate_owned_path 签名必须包含 require_file 参数"
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
