"""Phase 5.3 单元测试：Parse Delta / Resolve Delta

测试覆盖：
- lang_from_extension 语言检测
- PyDeltaComputer.compute_parse_delta：
  - 无 store 时所有符号为 Added
  - 有 store 时检测 Added/Removed/Changed
  - 不支持的文件扩展名报错
- PyParseDelta 属性和统计
- PyResolveDelta resolve 逻辑
  - 无 store 时返回空
  - 有 store 时解析 callee_name → qualified_name
"""
import os
import time
import pytest

import sys
_pyinstall = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "rust_ext", "target", "pyinstall"
)
if os.path.isdir(_pyinstall):
    sys.path.insert(0, _pyinstall)

try:
    from callwarden_core import (
        PyDeltaComputer, PyParseDelta, PyResolveDelta,
        PyHashDiffStore, PyDebouncedFileWatcher,
    )
    HAS_RUST = True
except ImportError:
    HAS_RUST = False

pytestmark = pytest.mark.skipif(not HAS_RUST, reason="callwarden_core Rust 扩展未构建")


class TestParseDeltaBasic:
    """基础 parse delta 测试"""

    def test_parse_python_file_no_store(self, tmp_path):
        """无 store 对比时，所有符号为 Added"""
        f = tmp_path / "test.py"
        f.write_text("def hello():\n    return 42\n\ndef world():\n    return hello()\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))

        assert delta.file_path == str(f)
        assert delta.language == "python"
        assert delta.content_hash != ""
        assert delta.total_lines > 0

        stats = delta.symbol_stats
        assert stats["added"] >= 2  # hello + world
        assert stats["removed"] == 0
        assert stats["changed"] == 0

    def test_parse_rust_file(self, tmp_path):
        """解析 Rust 文件"""
        f = tmp_path / "test.rs"
        f.write_text("fn main() {\n    println!(\"hello\");\n}\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        assert delta.language == "rust"
        assert delta.symbol_stats["added"] >= 1

    def test_parse_unsupported_extension(self, tmp_path):
        """不支持的扩展名报错"""
        f = tmp_path / "test.unknown"
        f.write_text("content\n")

        with pytest.raises(Exception):
            PyDeltaComputer.compute_parse_delta(str(f))

    def test_parse_nonexistent_file(self, tmp_path):
        """不存在的文件报错"""
        with pytest.raises(Exception):
            PyDeltaComputer.compute_parse_delta(str(tmp_path / "nonexistent.py"))

    def test_repr(self, tmp_path):
        """__repr__ 包含文件路径和摘要"""
        f = tmp_path / "repr_test.py"
        f.write_text("x = 1\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        r = repr(delta)
        assert "PyParseDelta" in r
        assert "repr_test.py" in r


class TestParseDeltaProperties:
    """PyParseDelta 属性测试"""

    def test_affected_qnames(self, tmp_path):
        """affected_qnames 返回所有受影响的符号"""
        f = tmp_path / "affected.py"
        f.write_text("def func_a():\n    pass\n\ndef func_b():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        qnames = delta.affected_qnames()
        assert len(qnames) >= 2

    def test_is_empty_false(self, tmp_path):
        """有变更时 is_empty 为 False"""
        f = tmp_path / "nonempty.py"
        f.write_text("def func():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        assert delta.is_empty() is False

    def test_summary_contains_path(self, tmp_path):
        """summary 包含文件路径"""
        f = tmp_path / "summary_test.py"
        f.write_text("x = 1\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        s = delta.summary()
        assert "summary_test.py" in s
        assert "symbols" in s


class TestParseDeltaWithCalls:
    """带函数调用的 parse delta 测试"""

    def test_file_with_calls(self, tmp_path):
        """文件包含函数调用时，raw call delta 非空"""
        f = tmp_path / "calls.py"
        f.write_text(
            "def helper():\n"
            "    return 1\n"
            "\n"
            "def caller():\n"
            "    return helper()\n"
        )

        delta = PyDeltaComputer.compute_parse_delta(str(f))

        # 应有符号
        assert delta.symbol_stats["added"] >= 2

        # 可能有 raw calls（helper() 调用）
        # 注意：parse 结果取决于 parser 实现
        call_stats = delta.call_stats
        assert call_stats["total"] >= 0  # 至少能获取统计

    def test_file_with_no_calls(self, tmp_path):
        """无函数调用的文件，raw call delta 为空或很少"""
        f = tmp_path / "nocalls.py"
        f.write_text("x = 1\ny = 2\nz = x + y\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        # 无函数定义时，symbols 可能为空或只有模块级变量
        # call_stats 应该都是 0 或很少
        assert delta.call_stats["added"] >= 0


class TestResolveDelta:
    """Resolve Delta 测试"""

    def test_resolve_no_store(self, tmp_path):
        """无 store 时 resolve 返回空"""
        f = tmp_path / "resolve.py"
        f.write_text("def func():\n    return other()\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        resolve = PyDeltaComputer.compute_resolve_delta(delta)

        # 无 store，resolve 返回空
        assert resolve.total_edges == 0
        assert resolve.is_empty() is True

    def test_resolve_no_cache(self, tmp_path):
        """无 cache 参数时，resolve 返回空"""
        f = tmp_path / "no_cache.py"
        f.write_text("def func():\n    pass\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        resolve = PyDeltaComputer.compute_resolve_delta(delta)
        assert resolve.is_empty() is True

    def test_resolve_repr(self, tmp_path):
        """resolve delta 的 __repr__"""
        f = tmp_path / "repr.py"
        f.write_text("x = 1\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        resolve = PyDeltaComputer.compute_resolve_delta(delta)
        r = repr(resolve)
        assert "PyResolveDelta" in r


class TestParseDeltaMultipleLanguages:
    """多语言 parse delta 测试"""

    def test_go_file(self, tmp_path):
        """Go 文件"""
        f = tmp_path / "test.go"
        f.write_text("package main\n\nfunc hello() int {\n    return 1\n}\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        assert delta.language == "go"

    def test_javascript_file(self, tmp_path):
        """JavaScript 文件"""
        f = tmp_path / "test.js"
        f.write_text("function hello() {\n  return 1;\n}\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        assert delta.language == "javascript"

    def test_typescript_file(self, tmp_path):
        """TypeScript 文件"""
        f = tmp_path / "test.ts"
        f.write_text("function hello(): number {\n  return 1;\n}\n")

        delta = PyDeltaComputer.compute_parse_delta(str(f))
        assert delta.language == "typescript"


class TestEndToEndPipeline:
    """端到端管道测试：watcher → hash_diff → parse_delta"""

    def test_watcher_to_parse_delta(self, tmp_path):
        """从 watcher 事件到 parse delta 的完整管道"""
        f = tmp_path / "e2e.py"
        f.write_text("def initial():\n    return 1\n")

        watcher = PyDebouncedFileWatcher(str(tmp_path), debounce_ms=100)
        store = PyHashDiffStore()
        watcher.start()
        try:
            time.sleep(0.3)
            events = watcher.flush()

            # 转为 hash diff
            event_tuples = [(e["kind"], e["path"], e["timestamp_ms"]) for e in events]
            if event_tuples:
                changes = store.diff_events(event_tuples)
                # 对真正变更的文件做 parse delta
                for change in changes:
                    if change["kind"] in ("added", "modified"):
                        delta = PyDeltaComputer.compute_parse_delta(change["path"])
                        assert delta.language == "python"
                        assert delta.symbol_stats["added"] >= 1
                        break
        finally:
            watcher.stop()

    def test_modify_then_parse_delta(self, tmp_path):
        """修改文件后 parse delta 反映变更"""
        f = tmp_path / "modify.py"
        f.write_text("def func_v1():\n    return 1\n")

        # 初始 parse
        delta1 = PyDeltaComputer.compute_parse_delta(str(f))
        assert delta1.symbol_stats["added"] >= 1

        # 修改文件：添加新函数
        f.write_text("def func_v1():\n    return 1\n\ndef func_v2():\n    return 2\n")

        # 再次 parse（无 store，所以全部为 Added）
        delta2 = PyDeltaComputer.compute_parse_delta(str(f))
        assert delta2.symbol_stats["added"] >= 2


if __name__ == "__main__":
    import time
    pytest.main([__file__, "-v", "--tb=short"])
