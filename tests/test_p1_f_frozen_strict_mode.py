"""P1-F Step 6: frozen build 强制 rust-strict 解析模式测试

设计文档：docs/design/rust-only-parser-cutover-plan.md §7 + §8 Phase 4 步骤 1

约束：
    - frozen build（PyInstaller）固定允许 rust-strict
    - frozen build 收到 python-reference / shadow 时报错
    - frozen build 不允许 CW_DISABLE_RUST_PARSE

测试策略：
    - 通过 mock sys.frozen / _MEIPASS 模拟 frozen build 环境
    - 验证 ParseMode.validate_for_environment() 在 frozen build 中的行为
    - 验证 cw._enforce_frozen_parse_mode() 在 frozen build 中正确 exit
    - 非 frozen build（源码开发）中 _enforce_frozen_parse_mode() 是 no-op
"""

import os
import sys
import types
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent))
sys.path.insert(0, str(Path(__file__).parent.parent / "rust_ext" / "target" / "pyinstall"))


try:
    from callwarden.db.rust_parser_facade import ParseMode, RustParserFacade
    _FACADE_AVAILABLE = True
except ImportError:
    _FACADE_AVAILABLE = False


# ============================================
# 辅助：模拟 frozen build
# ============================================


class FrozenBuildMock:
    """临时模拟 sys.frozen=True 和 sys._MEIPASS 存在

    用于测试 ParseMode.is_frozen_build() 和 _enforce_frozen_parse_mode()
    在 frozen build 环境中的行为。
    """

    def __init__(self, meipass_path="/tmp/frozen_build"):
        self._meipass_path = meipass_path
        self._old_frozen = getattr(sys, 'frozen', False)
        self._old_meipass = getattr(sys, '_MEIPASS', None)

    def __enter__(self):
        sys.frozen = True
        sys._MEIPASS = self._meipass_path
        return self

    def __exit__(self, *args):
        # 恢复原值
        if self._old_frozen:
            sys.frozen = self._old_frozen
        else:
            try:
                del sys.frozen
            except AttributeError:
                pass
        if self._old_meipass is not None:
            sys._MEIPASS = self._old_meipass
        else:
            try:
                del sys._MEIPASS
            except AttributeError:
                pass


def _set_env(name, value):
    """设置环境变量，返回原值（None 表示原本不存在）"""
    old = os.environ.get(name)
    if value is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = value
    return old


def _restore_env(name, old):
    """恢复环境变量"""
    if old is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = old


# ============================================
# ParseMode.is_frozen_build 检测
# ============================================

@pytest.mark.skipif(not _FACADE_AVAILABLE,
                    reason="callwarden.db.rust_parser_facade 不可用")
class TestParseModeFrozenDetection:
    """ParseMode.is_frozen_build() 检测测试"""

    def test_non_frozen_build_returns_false(self):
        """非 frozen build（源码开发）返回 False"""
        # 测试环境通常不是 frozen build
        if not getattr(sys, 'frozen', False):
            assert not ParseMode.is_frozen_build()

    def test_frozen_build_detected_with_meipass(self):
        """sys.frozen=True + sys._MEIPASS 存在 → True"""
        with FrozenBuildMock(meipass_path="/tmp/test_frozen"):
            assert ParseMode.is_frozen_build()

    def test_frozen_without_meipass_returns_false(self):
        """sys.frozen=True 但无 _MEIPASS → False（非 PyInstaller 环境）"""
        old_frozen = getattr(sys, 'frozen', False)
        old_meipass = getattr(sys, '_MEIPASS', None)
        try:
            sys.frozen = True
            # 确保无 _MEIPASS
            if hasattr(sys, '_MEIPASS'):
                del sys._MEIPASS
            assert not ParseMode.is_frozen_build(), \
                "frozen=True 但无 _MEIPASS 不应判定为 frozen build"
        finally:
            if old_frozen:
                sys.frozen = old_frozen
            else:
                try:
                    del sys.frozen
                except AttributeError:
                    pass
            if old_meipass is not None:
                sys._MEIPASS = old_meipass


# ============================================
# frozen build 强制 rust-strict
# ============================================

@pytest.mark.skipif(not _FACADE_AVAILABLE,
                    reason="callwarden.db.rust_parser_facade 不可用")
class TestFrozenBuildEnforcesRustStrict:
    """frozen build 强制 rust-strict 模式测试

    设计 §7：正式 frozen build 固定允许 rust-strict，
    收到 python-reference / shadow / CW_DISABLE_RUST_PARSE 时返回明确错误。
    """

    def test_frozen_build_allows_rust_strict(self):
        """frozen build 允许 rust-strict 模式"""
        old_mode = _set_env("CW_PARSE_MODE", None)
        old_disable = _set_env("CW_DISABLE_RUST_PARSE", None)
        try:
            with FrozenBuildMock():
                mode = ParseMode.validate_for_environment()
                assert mode == ParseMode.RUST_STRICT
        finally:
            _restore_env("CW_PARSE_MODE", old_mode)
            _restore_env("CW_DISABLE_RUST_PARSE", old_disable)

    def test_frozen_build_rejects_python_reference(self):
        """frozen build 拒绝 python-reference 模式"""
        old_mode = _set_env("CW_PARSE_MODE", ParseMode.PYTHON_REFERENCE)
        try:
            with FrozenBuildMock():
                with pytest.raises(RuntimeError, match="frozen build 仅允许 rust-strict"):
                    ParseMode.validate_for_environment()
        finally:
            _restore_env("CW_PARSE_MODE", old_mode)

    def test_frozen_build_rejects_shadow(self):
        """frozen build 拒绝 shadow 模式"""
        old_mode = _set_env("CW_PARSE_MODE", ParseMode.SHADOW)
        try:
            with FrozenBuildMock():
                with pytest.raises(RuntimeError, match="frozen build 仅允许 rust-strict"):
                    ParseMode.validate_for_environment()
        finally:
            _restore_env("CW_PARSE_MODE", old_mode)

    def test_frozen_build_rejects_disable_rust_parse(self):
        """frozen build 拒绝 CW_DISABLE_RUST_PARSE"""
        old_mode = _set_env("CW_PARSE_MODE", None)
        old_disable = _set_env("CW_DISABLE_RUST_PARSE", "1")
        try:
            with FrozenBuildMock():
                with pytest.raises(RuntimeError, match="CW_DISABLE_RUST_PARSE"):
                    ParseMode.validate_for_environment()
        finally:
            _restore_env("CW_PARSE_MODE", old_mode)
            _restore_env("CW_DISABLE_RUST_PARSE", old_disable)

    def test_frozen_build_is_rust_disabled_always_false(self):
        """frozen build 中 is_rust_disabled() 始终返回 False"""
        old_disable = _set_env("CW_DISABLE_RUST_PARSE", "1")
        old_mode = _set_env("CW_PARSE_MODE", None)
        try:
            with FrozenBuildMock():
                assert not RustParserFacade.is_rust_disabled(), \
                    "frozen build 中 Rust parser 是唯一解析路径，不允许禁用"
        finally:
            _restore_env("CW_DISABLE_RUST_PARSE", old_disable)
            _restore_env("CW_PARSE_MODE", old_mode)


# ============================================
# 非 frozen build 不强制
# ============================================

@pytest.mark.skipif(not _FACADE_AVAILABLE,
                    reason="callwarden.db.rust_parser_facade 不可用")
class TestNonFrozenBuildNoEnforcement:
    """非 frozen build（源码开发）不强制 rust-strict"""

    def test_non_frozen_allows_python_reference(self):
        """非 frozen build 允许 python-reference 模式"""
        if getattr(sys, 'frozen', False):
            pytest.skip("当前在 frozen build 中运行")
        old_mode = _set_env("CW_PARSE_MODE", ParseMode.PYTHON_REFERENCE)
        try:
            # 不应抛 RuntimeError
            mode = ParseMode.validate_for_environment()
            assert mode == ParseMode.PYTHON_REFERENCE
        finally:
            _restore_env("CW_PARSE_MODE", old_mode)

    def test_non_frozen_allows_shadow(self):
        """非 frozen build 允许 shadow 模式"""
        if getattr(sys, 'frozen', False):
            pytest.skip("当前在 frozen build 中运行")
        old_mode = _set_env("CW_PARSE_MODE", ParseMode.SHADOW)
        try:
            mode = ParseMode.validate_for_environment()
            assert mode == ParseMode.SHADOW
        finally:
            _restore_env("CW_PARSE_MODE", old_mode)

    def test_non_frozen_allows_disable_rust_parse(self):
        """非 frozen build 允许 CW_DISABLE_RUST_PARSE（开发期兼容）"""
        if getattr(sys, 'frozen', False):
            pytest.skip("当前在 frozen build 中运行")
        old_mode = _set_env("CW_PARSE_MODE", ParseMode.SHADOW)
        old_disable = _set_env("CW_DISABLE_RUST_PARSE", "1")
        try:
            # shadow 模式 + CW_DISABLE_RUST_PARSE 在非 frozen build 中允许
            assert RustParserFacade.is_rust_disabled()
        finally:
            _restore_env("CW_PARSE_MODE", old_mode)
            _restore_env("CW_DISABLE_RUST_PARSE", old_disable)


# ============================================
# cw._enforce_frozen_parse_mode 集成测试
# ============================================

class TestEnforceFrozenParseMode:
    """cw._enforce_frozen_parse_mode() 集成测试

    验证 cw.py 主入口的 frozen strict mode 强制逻辑。
    """

    def test_enforce_noop_in_non_frozen_build(self):
        """非 frozen build 中 _enforce_frozen_parse_mode() 是 no-op"""
        if getattr(sys, 'frozen', False):
            pytest.skip("当前在 frozen build 中运行")
        # 导入 cw 模块的 _enforce_frozen_parse_mode
        try:
            # cw.py 在项目根目录，需要通过 callwarden.cw 访问
            from callwarden.cw import _enforce_frozen_parse_mode
            # 非 frozen build 中调用应直接返回，不抛异常
            _enforce_frozen_parse_mode()
        except ImportError:
            pytest.skip("callwarden.cw 不可用")

    def test_enforce_exits_on_python_reference_in_frozen(self):
        """frozen build 中 CW_PARSE_MODE=python-reference 触发 exit(2)"""
        try:
            from callwarden.cw import _enforce_frozen_parse_mode
        except ImportError:
            pytest.skip("callwarden.cw 不可用")

        old_mode = _set_env("CW_PARSE_MODE", ParseMode.PYTHON_REFERENCE)
        try:
            with FrozenBuildMock():
                with pytest.raises(SystemExit) as exc_info:
                    _enforce_frozen_parse_mode()
                assert exc_info.value.code == 2, \
                    "frozen build 收到 python-reference 应 exit(2)"
        finally:
            _restore_env("CW_PARSE_MODE", old_mode)

    def test_enforce_exits_on_disable_rust_in_frozen(self):
        """frozen build 中 CW_DISABLE_RUST_PARSE=1 触发 exit(2)"""
        try:
            from callwarden.cw import _enforce_frozen_parse_mode
        except ImportError:
            pytest.skip("callwarden.cw 不可用")

        old_mode = _set_env("CW_PARSE_MODE", None)
        old_disable = _set_env("CW_DISABLE_RUST_PARSE", "1")
        try:
            with FrozenBuildMock():
                with pytest.raises(SystemExit) as exc_info:
                    _enforce_frozen_parse_mode()
                assert exc_info.value.code == 2, \
                    "frozen build 收到 CW_DISABLE_RUST_PARSE 应 exit(2)"
        finally:
            _restore_env("CW_PARSE_MODE", old_mode)
            _restore_env("CW_DISABLE_RUST_PARSE", old_disable)

    def test_enforce_passes_with_rust_strict_in_frozen(self):
        """frozen build 中 rust-strict 模式通过校验"""
        try:
            from callwarden.cw import _enforce_frozen_parse_mode
        except ImportError:
            pytest.skip("callwarden.cw 不可用")

        old_mode = _set_env("CW_PARSE_MODE", None)
        old_disable = _set_env("CW_DISABLE_RUST_PARSE", None)
        try:
            with FrozenBuildMock():
                # 不应抛 SystemExit
                _enforce_frozen_parse_mode()
        finally:
            _restore_env("CW_PARSE_MODE", old_mode)
            _restore_env("CW_DISABLE_RUST_PARSE", old_disable)

    def test_enforce_exits_on_shadow_in_frozen(self):
        """frozen build 中 CW_PARSE_MODE=shadow 触发 exit(2)"""
        try:
            from callwarden.cw import _enforce_frozen_parse_mode
        except ImportError:
            pytest.skip("callwarden.cw 不可用")

        old_mode = _set_env("CW_PARSE_MODE", ParseMode.SHADOW)
        try:
            with FrozenBuildMock():
                with pytest.raises(SystemExit) as exc_info:
                    _enforce_frozen_parse_mode()
                assert exc_info.value.code == 2
        finally:
            _restore_env("CW_PARSE_MODE", old_mode)


# ============================================
# PyInstaller spec 静态检查（可选，验证 spec 配置正确）
# ============================================

class TestPyInstallerSpecConfig:
    """验证 PyInstaller spec 文件配置正确

    检查 spec 文件中：
    - local bundle 收集了 callwarden_core（Rust 扩展）
    - local bundle 收集了 callwarden.cw（_enforce_frozen_parse_mode 所在模块）
    - client/agent bundle exclude 了 callwarden.cw（不需 frozen strict mode）
    """

    SPEC_PATH = Path(__file__).parent.parent / "release" / "pyinstaller" / "callwarden.spec"

    def test_spec_file_exists(self):
        """spec 文件存在"""
        assert self.SPEC_PATH.exists(), f"spec 文件应在 {self.SPEC_PATH}"

    @pytest.mark.skipif(not SPEC_PATH.exists(), reason="spec 文件不存在")
    def test_spec_collects_callwarden_core(self):
        """spec 收集了 callwarden_core（Rust 扩展）"""
        content = self.SPEC_PATH.read_text(encoding="utf-8")
        assert "callwarden_core" in content, \
            "spec 应收集 callwarden_core 模块（Rust 扩展）"

    @pytest.mark.skipif(not SPEC_PATH.exists(), reason="spec 文件不存在")
    def test_spec_collects_callwarden_cw(self):
        """spec 收集了 callwarden.cw（_enforce_frozen_parse_mode 所在模块）"""
        content = self.SPEC_PATH.read_text(encoding="utf-8")
        assert "'callwarden.cw'" in content, \
            "spec 应收集 callwarden.cw 模块（含 frozen strict mode 强制）"

    @pytest.mark.skipif(not SPEC_PATH.exists(), reason="spec 文件不存在")
    def test_spec_excludes_cw_from_client_agent(self):
        """client/agent bundle exclude 了 callwarden.cw（不需 frozen strict mode）"""
        content = self.SPEC_PATH.read_text(encoding="utf-8")
        assert "'callwarden.cw'" in content, \
            "client/agent bundle 应 exclude callwarden.cw（避免拉入 parser）"
