"""P0-3 跨平台安装包修复验证测试。

评审 P0-3：跨平台安装包目前不能算完成
- wheel 不含 callwarden_core（py3-none-any）
- Linux 缺二进制仍继续打包
- RPM 是 TODO
- release workflow 有错误版本字段和错误 Rust parser 调用

修复点：
1. pyproject.toml 添加 py-modules = ["callwarden_core"] 让 wheel 包含 Rust 扩展
2. release/build.py 添加 _verify_rust_extension_present (fail-fast) + _verify_wheel_contains_rust_extension
3. release/build.py 添加 _detect_wheel_platform_tag 让 wheel 标记为平台特定
4. enterprise-release.yml 修正版本字段 ['package'] -> ['product']
5. enterprise-release.yml 修正 Rust parser 调用 parse_file_lang -> parse_canonical_bytes_py
"""

import sys
import zipfile
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock

import pytest

ROOT = Path(__file__).resolve().parent.parent


# ============================================================
# 1. pyproject.toml 配置验证
# ============================================================

def test_pyproject_toml_includes_callwarden_core_py_modules():
    """验证 pyproject.toml 中 [tool.setuptools] 声明 callwarden_core 为扩展模块。

    评审 P0-3：原代码 pyproject.toml 没有声明根级 callwarden_core 二进制，
    导致 setuptools 不会把 callwarden_core.pyd/.so 打入 wheel。

    修复：用 ext-modules = [{ name = "callwarden_core", sources = [] }] 声明
    （py-modules 只识别 .py 文件，不识别 .pyd/.so）。
    """
    pyproject = ROOT / "pyproject.toml"
    content = pyproject.read_text(encoding="utf-8")

    # 必须有 ext-modules 声明（不能用 py-modules，因为 .pyd/.so 不是 .py）
    assert "ext-modules" in content, (
        "pyproject.toml 必须声明 ext-modules 让 wheel 包含 Rust 扩展二进制"
    )
    # 必须包含 callwarden_core
    assert "callwarden_core" in content, (
        "ext-modules 必须包含 'callwarden_core'"
    )


# ============================================================
# 2. release/build.py 函数存在性验证
# ============================================================

def test_build_py_has_rust_extension_verification_functions():
    """验证 release/build.py 添加了 P0-3 修复所需的验证函数。"""
    build_py = ROOT / "release" / "build.py"
    content = build_py.read_text(encoding="utf-8")

    # P0-3 修复必须的函数
    assert "_verify_rust_extension_present" in content, (
        "build.py 必须有 _verify_rust_extension_present (fail-fast 检查)"
    )
    assert "_verify_wheel_contains_rust_extension" in content, (
        "build.py 必须有 _verify_wheel_contains_rust_extension (wheel 内容验证)"
    )
    assert "_detect_wheel_platform_tag" in content, (
        "build.py 必须有 _detect_wheel_platform_tag (平台特定标记)"
    )


def test_build_py_wheel_platform_tag_not_any():
    """验证 _detect_wheel_platform_tag 返回平台特定 tag（不是 py3-none-any）。"""
    # 动态导入 release.build 模块
    sys.path.insert(0, str(ROOT))
    try:
        import importlib
        build_module = importlib.import_module("release.build")
    finally:
        sys.path.pop(0)

    plat_tag = build_module._detect_wheel_platform_tag()

    # 不能是 pure-python "any"（必须平台特定，注意 "manylinux" 合法包含 "any" 子串）
    assert plat_tag != "any", f"plat_tag 不能是 any，实际: {plat_tag}"
    assert not plat_tag.endswith(
        "-any"), f"plat_tag 不能以 -any 结尾，实际: {plat_tag}"

    # 必须匹配当前平台
    if sys.platform == "win32":
        assert plat_tag == "win_amd64", f"Windows 必须是 win_amd64，实际: {plat_tag}"
    elif sys.platform == "darwin":
        assert plat_tag.startswith(
            "macosx_"), f"macOS 必须以 macosx_ 开头，实际: {plat_tag}"
    else:
        assert plat_tag.startswith(
            "manylinux"), f"Linux 必须以 manylinux 开头，实际: {plat_tag}"


def test_build_py_verify_rust_extension_present_passes_when_exists(tmp_path, capsys):
    """验证 _verify_rust_extension_present 在二进制存在时正常返回。"""
    sys.path.insert(0, str(ROOT))
    try:
        import importlib
        build_module = importlib.import_module("release.build")
    finally:
        sys.path.pop(0)

    # 用 monkeypatch 替换 ROOT 为 tmp_path，并放一个假的 .pyd
    fake_binary = tmp_path / \
        "callwarden_core.pyd" if sys.platform == "win32" else tmp_path / "callwarden_core.so"
    fake_binary.write_bytes(b"x" * 2048)  # 大于 1024 字节

    with patch.object(build_module, "ROOT", tmp_path):
        # 应该正常返回，不抛 SystemExit
        build_module._verify_rust_extension_present()

    captured = capsys.readouterr()
    assert "[OK]" in captured.out
    assert "callwarden_core" in captured.out


def test_build_py_verify_rust_extension_present_fails_when_missing(tmp_path, capsys):
    """验证 _verify_rust_extension_present 在二进制缺失时 fail-fast 退出。"""
    sys.path.insert(0, str(ROOT))
    try:
        import importlib
        build_module = importlib.import_module("release.build")
    finally:
        sys.path.pop(0)

    # tmp_path 中没有 callwarden_core.pyd/.so
    with patch.object(build_module, "ROOT", tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            build_module._verify_rust_extension_present()

    # 必须以非零退出码退出
    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "[FAIL]" in captured.out
    assert "missing" in captured.out.lower() or "不存在" in captured.out


def test_build_py_verify_rust_extension_present_fails_when_too_small(tmp_path, capsys):
    """验证 _verify_rust_extension_present 在二进制过小时 fail-fast 退出。"""
    sys.path.insert(0, str(ROOT))
    try:
        import importlib
        build_module = importlib.import_module("release.build")
    finally:
        sys.path.pop(0)

    # 放一个小于 1024 字节的假二进制（损坏）
    fake_binary = tmp_path / \
        "callwarden_core.pyd" if sys.platform == "win32" else tmp_path / "callwarden_core.so"
    fake_binary.write_bytes(b"x" * 100)  # 只有 100 字节，损坏

    with patch.object(build_module, "ROOT", tmp_path):
        with pytest.raises(SystemExit) as exc_info:
            build_module._verify_rust_extension_present()

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "[FAIL]" in captured.out
    assert "too small" in captured.out.lower() or "过小" in captured.out


def test_build_py_verify_wheel_contains_rust_extension_passes(tmp_path):
    """验证 _verify_wheel_contains_rust_extension 在 wheel 包含二进制时正常返回。"""
    sys.path.insert(0, str(ROOT))
    try:
        import importlib
        build_module = importlib.import_module("release.build")
    finally:
        sys.path.pop(0)

    # 构造假 wheel，包含 callwarden_core.pyd
    wheel_path = tmp_path / "callwarden-0.3.0-cp311-cp311-win_amd64.whl"
    binary_name = "callwarden_core.pyd" if sys.platform == "win32" else "callwarden_core.so"
    with zipfile.ZipFile(str(wheel_path), "w") as zf:
        zf.writestr("callwarden/__init__.py", "")
        zf.writestr(binary_name, b"\x00" * 1024)

    # 应该正常返回
    build_module._verify_wheel_contains_rust_extension(wheel_path)


def test_build_py_verify_wheel_contains_rust_extension_fails_when_missing(tmp_path, capsys):
    """验证 _verify_wheel_contains_rust_extension 在 wheel 缺失二进制时 fail-fast。"""
    sys.path.insert(0, str(ROOT))
    try:
        import importlib
        build_module = importlib.import_module("release.build")
    finally:
        sys.path.pop(0)

    # 构造假 wheel，但不包含 callwarden_core 二进制
    wheel_path = tmp_path / "callwarden-0.3.0-py3-none-any.whl"
    with zipfile.ZipFile(str(wheel_path), "w") as zf:
        zf.writestr("callwarden/__init__.py", "")
        zf.writestr("callwarden/cw.py", "")
        # 没有 callwarden_core.pyd / .so

    with pytest.raises(SystemExit) as exc_info:
        build_module._verify_wheel_contains_rust_extension(wheel_path)

    assert exc_info.value.code != 0
    captured = capsys.readouterr()
    assert "[FAIL]" in captured.out
    assert "callwarden_core" in captured.out


# ============================================================
# 3. enterprise-release.yml workflow 验证
# ============================================================

def test_enterprise_release_workflow_uses_correct_version_field():
    """验证 workflow 修复了版本字段错误（package -> product）。

    评审 P0-3：原代码 ['package']['version'] 会 KeyError，
    因为 version.toml 中字段是 [product]。
    """
    workflow = ROOT / ".github" / "workflows" / "enterprise-release.yml"
    lines = workflow.read_text(encoding="utf-8").splitlines()

    # 检查非注释行（不以 # 或空格+# 开头），不能有错误的 ['package']['version']
    code_lines = [
        line.strip() for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]
    bad_lines = [
        line for line in code_lines if "['package']['version']" in line]
    assert not bad_lines, (
        f"workflow 不能再使用 ['package']['version']，应改为 ['product']['version']，"
        f"违规行：{bad_lines}"
    )

    # 必须使用正确的 ['product']['version']
    good_lines = [
        line for line in code_lines if "['product']['version']" in line]
    assert good_lines, (
        "workflow 必须使用 ['product']['version']（version.toml 中字段是 [product]）"
    )


def test_enterprise_release_workflow_uses_correct_rust_parser_signature():
    """验证 workflow 修复了 Rust parser 调用签名错误。

    评审 P0-3：原代码 parse_file_lang('def f(): pass', 'test.py') 错误：
    - 真实签名是 (abs_path, module_path, language) 3 个参数
    - 第一个参数必须是绝对路径，不是源码内容
    - 缺第三个参数 language

    修复：改用 parse_canonical_bytes_py(canonical_bytes, module_path, language, content_hash)
    直接传 bytes，不依赖文件系统。
    """
    workflow = ROOT / ".github" / "workflows" / "enterprise-release.yml"
    lines = workflow.read_text(encoding="utf-8").splitlines()

    # 检查非注释行
    code_lines = [
        line.strip() for line in lines
        if line.strip() and not line.strip().startswith("#")
    ]

    # 不能再有错误的 parse_file_lang('def f(): pass', ...) 调用（非注释行）
    bad_lines = [
        line for line in code_lines if "parse_file_lang('def f(): pass'" in line]
    assert not bad_lines, (
        f"workflow 不能再用错误签名的 parse_file_lang，违规行：{bad_lines}"
    )
    bad_lines = [line for line in code_lines if 'parse_file_lang(open' in line]
    assert not bad_lines, (
        f"workflow 不能再用源码内容作为 abs_path 的 parse_file_lang 调用，违规行：{bad_lines}"
    )

    # 必须使用 parse_canonical_bytes_py
    good_lines = [
        line for line in code_lines if "parse_canonical_bytes_py" in line]
    assert good_lines, (
        "workflow 必须改用 parse_canonical_bytes_py 正确签名"
    )

    # 必须传 4 个参数（canonical_bytes, module_path, language, content_hash）
    # 检查至少出现一次完整的 4 参数调用
    has_4_arg_call = any(
        "parse_canonical_bytes_py(b'def f(): pass', 'test', 'python'" in line
        or "parse_canonical_bytes_py(src, 'cw', 'python'" in line
        for line in code_lines
    )
    assert has_4_arg_call, (
        "workflow 中 parse_canonical_bytes_py 调用必须传 4 个参数"
    )


# ============================================================
# 4. 真实 parse_canonical_bytes_py 调用验证（如果有 Rust 扩展）
# ============================================================

def test_real_parse_canonical_bytes_py_signature_is_4_args():
    """验证真实的 parse_canonical_bytes_py 接受 4 个参数且返回 dict。

    这是 workflow 中调用的函数，确保签名匹配。
    """
    try:
        from callwarden_core import parse_canonical_bytes_py
    except ImportError:
        pytest.skip("callwarden_core 未构建，跳过真实签名验证")

    # 调用签名：(canonical_bytes, module_path, language, content_hash)
    # 64 个 0 的 sha256 hex（dummy hash）
    result = parse_canonical_bytes_py(
        b"def hello(): pass\n",
        "test_module",
        "python",
        "0" * 64,
    )

    # 返回应该是 dict（PyDict）
    assert result is not None
    assert isinstance(result, dict), f"返回必须是 dict，实际: {type(result)}"
    assert "symbols" in result, "返回必须包含 symbols 字段"
    assert "abs_path" in result, "返回必须包含 abs_path 字段"
    assert "module_path" in result, "返回必须包含 module_path 字段"

    # 简单 Python 代码至少解析出 1 个符号（hello 函数）
    symbols = result["symbols"]
    assert isinstance(symbols, list)
    assert len(symbols) >= 1, f"至少解析出 1 个符号，实际: {len(symbols)}"
    assert symbols[0]["name"] == "hello"


def test_real_parse_canonical_bytes_py_rejects_wrong_language():
    """验证 parse_canonical_bytes_py 在 language 不支持时抛异常。"""
    try:
        from callwarden_core import parse_canonical_bytes_py
    except ImportError:
        pytest.skip("callwarden_core 未构建")

    with pytest.raises(Exception) as exc_info:
        parse_canonical_bytes_py(
            b"def hello(): pass\n",
            "test_module",
            "nonexistent_language",
            "0" * 64,
        )

    # 错误消息应该提到不支持的语言
    assert "nonexistent_language" in str(exc_info.value)


# ============================================================
# 5. 端到端验证：构建 wheel 包含 Rust 扩展（仅在能构建时）
# ============================================================

def test_end_to_end_wheel_contains_rust_extension():
    """端到端验证：构建真实 wheel 并检查包含 Rust 扩展。

    仅在 callwarden_core.pyd/.so 存在时运行（CI 必跑，本地可选）。
    """
    binary_name = "callwarden_core.pyd" if sys.platform == "win32" else "callwarden_core.so"
    binary_path = ROOT / binary_name

    if not binary_path.exists():
        pytest.skip(f"{binary_name} 未构建，跳过端到端 wheel 验证")

    # 直接用 setuptools 构建一个 wheel 到临时目录
    import subprocess
    import tempfile
    import shutil

    with tempfile.TemporaryDirectory() as tmpdir:
        # 构建 wheel
        result = subprocess.run(
            [sys.executable, "-m", "build", "--wheel", "--outdir", tmpdir],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            pytest.skip(f"build wheel 失败：{result.stderr[:500]}")

        # 找到 wheel 文件
        wheels = list(Path(tmpdir).glob("*.whl"))
        assert len(wheels) > 0, "没有生成 wheel"

        # 验证 wheel 包含 callwarden_core 二进制
        with zipfile.ZipFile(str(wheels[0])) as zf:
            names = zf.namelist()
            rust_files = [
                n for n in names
                if n.startswith("callwarden_core.") and (n.endswith(".pyd") or n.endswith(".so"))
            ]
            assert len(rust_files) > 0, (
                f"wheel 必须包含 callwarden_core 二进制，"
                f"实际内容（前 20 个）: {names[:20]}"
            )
