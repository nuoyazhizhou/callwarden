"""P21: 第三方库目录自动检测算法测试

验证 _detect_third_party_dir 能基于内容特征识别第三方库目录，
不再依赖手动 .callwardenignore 配置。

测试场景：
1. 已知目录名（node_modules/vendor 等）直接判定
2. 大文件密度检测（> 5 个 > 100KB）
3. minified 文件检测（.min.js）
4. 可疑目录名 + 辅助信号（static/ + 大文件）
5. 正常业务代码目录不误判
6. 深度限制（> 2 层只做目录名检测）
7. build_full_graph 集成测试
"""
from __future__ import annotations

import os
import tempfile

from callwarden.db.db import CodeGraphDB
from callwarden.db.db_build import _detect_third_party_dir


def _make_dir_with_files(root: str, dir_name: str, files: dict[str, int]) -> str:
    """创建目录并填充指定大小的文件。

    Args:
        root: 根目录
        dir_name: 子目录名
        files: {filename: size_bytes} 字典
    """
    d = os.path.join(root, dir_name)
    os.makedirs(d, exist_ok=True)
    for name, size in files.items():
        f_path = os.path.join(d, name)
        with open(f_path, "wb") as f:
            f.write(b"\0" * size)
    return d


def test_known_dir_name_node_modules():
    """已知第三方库目录名（node_modules）直接判定"""
    with tempfile.TemporaryDirectory() as root:
        d = _make_dir_with_files(root, "node_modules", {})
        is_tp, reason = _detect_third_party_dir(d, "node_modules")
        assert is_tp is True
        assert "known_dir" in reason


def test_known_dir_name_vendor():
    """已知第三方库目录名（vendor）直接判定"""
    with tempfile.TemporaryDirectory() as root:
        d = _make_dir_with_files(root, "vendor", {})
        is_tp, reason = _detect_third_party_dir(d, "vendor")
        assert is_tp is True
        assert "known_dir" in reason


def test_large_file_density():
    """大文件密度检测：> 5 个 > 500KB 的源码文件"""
    with tempfile.TemporaryDirectory() as root:
        # 创建 6 个 550KB .js 文件
        files = {f"lib_{i}.js": 550 * 1024 for i in range(6)}
        d = _make_dir_with_files(root, "mydir", files)
        is_tp, reason = _detect_third_party_dir(d, "mydir")
        assert is_tp is True
        assert "large_files" in reason


def test_large_file_below_threshold():
    """大文件密度低于阈值不判定（4 个文件，阈值 5）"""
    with tempfile.TemporaryDirectory() as root:
        # 创建 4 个 550KB 文件（< 5 阈值）
        files = {f"lib_{i}.js": 550 * 1024 for i in range(4)}
        d = _make_dir_with_files(root, "mydir", files)
        is_tp, _ = _detect_third_party_dir(d, "mydir")
        assert is_tp is False


def test_minified_file():
    """minified 文件检测"""
    with tempfile.TemporaryDirectory() as root:
        d = _make_dir_with_files(root, "mydir", {
            "jquery.min.js": 10,  # 小文件但含 .min.
            "app.js": 5,
        })
        is_tp, reason = _detect_third_party_dir(d, "mydir")
        assert is_tp is True
        assert reason == "minified"


def test_suspicious_dir_with_large_file():
    """可疑目录名（static）+ 1 个 > 500KB 源码文件 → 判定"""
    with tempfile.TemporaryDirectory() as root:
        d = _make_dir_with_files(root, "static", {
            "big.js": 550 * 1024,
            "small.js": 5,
        })
        is_tp, reason = _detect_third_party_dir(d, "static")
        assert is_tp is True
        assert "suspicious_dir" in reason


def test_suspicious_dir_without_large_file():
    """可疑目录名（static）但无大文件 → 不判定"""
    with tempfile.TemporaryDirectory() as root:
        d = _make_dir_with_files(root, "static", {
            "app.js": 5 * 1024,  # 5KB，正常业务代码
            "main.js": 3 * 1024,
        })
        is_tp, _ = _detect_third_party_dir(d, "static")
        assert is_tp is False


def test_normal_dir_not_detected():
    """正常业务代码目录不误判"""
    with tempfile.TemporaryDirectory() as root:
        d = _make_dir_with_files(root, "src", {
            "main.js": 2 * 1024,
            "utils.js": 1 * 1024,
            "app.js": 3 * 1024,
        })
        is_tp, _ = _detect_third_party_dir(d, "src")
        assert is_tp is False


def test_binary_files_not_counted():
    """二进制文件（.a/.so）不计入大文件统计"""
    with tempfile.TemporaryDirectory() as root:
        # 10 个 4MB 的 .a 文件（二进制，不应触发误判）
        files = {f"lib_{i}.a": 4 * 1024 * 1024 for i in range(10)}
        d = _make_dir_with_files(root, "libbin", files)
        is_tp, _ = _detect_third_party_dir(d, "libbin")
        assert is_tp is False, ".a 二进制文件不应触发第三方库检测"


def test_large_c_files_not_misjudged():
    """大型 C 源码文件不误判（业务代码可能 > 100KB）"""
    with tempfile.TemporaryDirectory() as root:
        # 5 个 200KB 的 .c 文件（业务代码，不超 500KB 阈值）
        files = {f"module_{i}.c": 200 * 1024 for i in range(5)}
        d = _make_dir_with_files(root, "controller", files)
        is_tp, _ = _detect_third_party_dir(d, "controller")
        assert is_tp is False, "200KB 的 .c 文件不应触发第三方库检测"


def test_depth_limit():
    """深度 > 2 的目录只做目录名检测"""
    with tempfile.TemporaryDirectory() as root:
        # 创建深度 4 的目录（a/b/c/d，rel_path 有 3 个 /，depth=3）
        deep = os.path.join(root, "a", "b", "c", "d")
        os.makedirs(deep)
        # 深路径下有大量大文件，但因深度限制不检测内容
        for i in range(10):
            with open(os.path.join(deep, f"lib_{i}.js"), "wb") as f:
                f.write(b"\0" * 200 * 1024)
        rel_path = "a/b/c/d"
        is_tp, _ = _detect_third_party_dir(deep, rel_path)
        # depth = 3 > 2，不做内容检测，d 不是已知目录名，不判定
        assert is_tp is False


def test_build_full_graph_auto_ignore_third_party():
    """build_full_graph 自动跳过第三方库目录"""
    with tempfile.TemporaryDirectory() as root:
        # 创建正常业务代码
        with open(os.path.join(root, "main.py"), "w") as f:
            f.write("def hello():\n    print('hello')\n")

        # 创建第三方库目录（6 个 > 500KB 的 .js 文件）
        lib_dir = os.path.join(root, "libs")
        os.makedirs(lib_dir)
        for i in range(6):
            with open(os.path.join(lib_dir, f"big_{i}.js"), "wb") as f:
                f.write(b"\0" * 550 * 1024)

        db = CodeGraphDB(
            os.path.join(root, "callwarden.db"),
            workspace_root=root,
        )
        try:
            db.build_full_graph(force=True)

            # 验证 libs/ 目录的文件没有被扫描
            cur = db.conn.execute(
                "SELECT COUNT(*) as c FROM file_instances WHERE rel_path LIKE 'libs/%'"
            )
            assert cur.fetchone()["c"] == 0, "libs/ 目录的文件不应被扫描"

            # 验证 main.py 被正常扫描
            cur = db.conn.execute(
                "SELECT COUNT(*) as c FROM file_instances WHERE rel_path = 'main.py'"
            )
            assert cur.fetchone()["c"] == 1
        finally:
            db.close()


def test_build_full_graph_normal_dir_not_ignored():
    """build_full_graph 不误判正常业务代码目录"""
    with tempfile.TemporaryDirectory() as root:
        # 创建正常业务代码目录
        src_dir = os.path.join(root, "src")
        os.makedirs(src_dir)
        for name, size in [("main.js", 2048), ("utils.js", 1024)]:
            with open(os.path.join(src_dir, name), "w") as f:
                f.write(f"// {name}\nfunction foo() {{}}\n")

        db = CodeGraphDB(
            os.path.join(root, "callwarden.db"),
            workspace_root=root,
        )
        try:
            db.build_full_graph(force=True)

            # src/ 目录的文件应被正常扫描
            cur = db.conn.execute(
                "SELECT COUNT(*) as c FROM file_instances WHERE rel_path LIKE 'src/%'"
            )
            assert cur.fetchone()["c"] == 2
        finally:
            db.close()
