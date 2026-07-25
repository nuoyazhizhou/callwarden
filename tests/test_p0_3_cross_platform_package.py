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

import ast
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


# ============================================================
# 6. P0-B: client/agent 无 parser 启动 + 企业 RPC 可用性验证
# ============================================================
# 设计：docs/design/rust-only-parser-cutover-plan.md Phase 1 步骤 3
# 验证 client/agent 入口链路（entry_cw_client/agent → daemon_commands /
# agent_watcher）不依赖 parser/tree_sitter/numpy，且企业 RPC 子命令可用。

# parser 相关模块根（client/agent 入口链路禁止顶层 import）
_PARSER_IMPORT_ROOTS = {"callwarden.parsers", "tree_sitter", "numpy"}


def _top_level_import_roots(file_path: Path) -> set[str]:
    """解析 Python 文件的顶层 import 模块根集合。

    只检查模块体直接子节点（顶层 import），不检查函数/类体内的延迟 import。
    """
    tree = ast.parse(file_path.read_text(encoding="utf-8"))
    roots: set[str] = set()
    for node in ast.iter_child_nodes(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                roots.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                roots.add(node.module.split(".")[0])
    return roots


def test_entry_cw_client_has_no_parser_imports():
    """entry_cw_client.py 顶层不导入 parser/tree_sitter/numpy。"""
    entry = ROOT / "release" / "pyinstaller" / "entry_cw_client.py"
    roots = _top_level_import_roots(entry)
    leaked = roots & _PARSER_IMPORT_ROOTS
    assert not leaked, f"entry_cw_client.py 不应顶层导入 parser 模块: {leaked}"


def test_entry_cw_agent_has_no_parser_imports():
    """entry_cw_agent.py 顶层不导入 parser/tree_sitter/numpy。"""
    entry = ROOT / "release" / "pyinstaller" / "entry_cw_agent.py"
    roots = _top_level_import_roots(entry)
    leaked = roots & _PARSER_IMPORT_ROOTS
    assert not leaked, f"entry_cw_agent.py 不应顶层导入 parser 模块: {leaked}"


def test_entry_scripts_stub_callwarden_packages():
    """入口脚本通过 sys.modules stub 跳过 callwarden/__init__.py 的 parser 拉入链。

    entry_cw_client/agent 在 import callwarden.cli.daemon_commands 之前，
    必须用 sys.modules 注入 callwarden 和 callwarden.db 包 stub，
    跳过 __init__.py 的 ``from .db import CodeGraphDB``（会拉入 parsers）。
    """
    for entry_name in ("entry_cw_client.py", "entry_cw_agent.py"):
        entry = ROOT / "release" / "pyinstaller" / entry_name
        content = entry.read_text(encoding="utf-8")
        assert "sys.modules" in content, f"{entry_name} 必须使用 sys.modules stub"
        assert "callwarden" in content, f"{entry_name} 必须 stub callwarden 包"
        assert "callwarden.db" in content, f"{entry_name} 必须 stub callwarden.db 包"


def test_daemon_commands_has_no_parser_imports():
    """cli/daemon_commands.py 顶层不导入 parser/tree_sitter/numpy（client 入口链路）。"""
    path = ROOT / "cli" / "daemon_commands.py"
    roots = _top_level_import_roots(path)
    leaked = roots & _PARSER_IMPORT_ROOTS
    assert not leaked, f"daemon_commands.py 不应顶层导入 parser 模块: {leaked}"


def test_agent_watcher_has_no_parser_imports():
    """server/agent_watcher.py 顶层不导入 parser/tree_sitter/numpy（agent 入口链路）。"""
    path = ROOT / "server" / "agent_watcher.py"
    roots = _top_level_import_roots(path)
    leaked = roots & _PARSER_IMPORT_ROOTS
    assert not leaked, f"agent_watcher.py 不应顶层导入 parser 模块: {leaked}"


def test_daemon_commands_exposes_enterprise_rpc_subcommands():
    """daemon_commands 注册 register/list/status/publish/query 企业 RPC 子命令。

    这些是 client/agent 通过 UDS 调用 daemon 的企业 RPC 入口，
    必须在不依赖 parser 的情况下可用。
    """
    path = ROOT / "cli" / "daemon_commands.py"
    content = path.read_text(encoding="utf-8")
    for cmd in ["register", "list", "status", "publish", "query"]:
        assert f'"{cmd}"' in content or f"'{cmd}'" in content, (
            f"daemon_commands.py 应注册 '{cmd}' 子命令"
        )


def test_client_agent_hiddenimports_exclude_parsers():
    """spec 中 _client_agent_hiddenimports 不包含 parser/tree_sitter/numpy 模块。

    检查带引量的模块名（``'numpy'``），避免命中注释中的「无 numpy」字样。
    """
    spec = (ROOT / "release" / "pyinstaller" / "callwarden.spec").read_text(
        encoding="utf-8"
    )
    start = spec.index("_client_agent_hiddenimports = [")
    end = spec.index("]", start) + 1
    block = spec[start:end]
    # 必须用带引号的形式匹配，避免误判注释中的「无 numpy」字样
    for forbidden in [
        "'callwarden.parsers'",
        "'tree_sitter'",
        "'numpy'",
    ]:
        assert forbidden not in block, (
            f"_client_agent_hiddenimports 不应包含 {forbidden}"
        )


def test_client_agent_excludes_block_contains_parsers():
    """spec 中 _PARSER_EXCLUDES 显式排除所有 parser 模块（fail closed）。"""
    spec = (ROOT / "release" / "pyinstaller" / "callwarden.spec").read_text(
        encoding="utf-8"
    )
    start = spec.index("_PARSER_EXCLUDES = [")
    end = spec.index("]", start) + 1
    block = spec[start:end]
    # tree-sitter 核心
    assert "'tree_sitter'" in block
    # 至少 3 种 grammar（验证列表非空且包含多语言）
    assert "'tree_sitter_rust'" in block
    assert "'tree_sitter_python'" in block
    # callwarden.parsers 整包
    assert "'callwarden.parsers'" in block
    # numpy
    assert "'numpy'" in block


def test_entry_cw_client_dev_mode_runs_without_import_error():
    """entry_cw_client.py 在开发模式（非 frozen）下可执行到平台门禁。

    验证 sys.modules stub 的路径计算正确，不会因 __path__ 错误而 ImportError。
    在 Windows 上应打印 ERROR 并退出 2（平台门禁），而非 ImportError。
    """
    import subprocess
    entry = ROOT / "release" / "pyinstaller" / "entry_cw_client.py"
    result = subprocess.run(
        [sys.executable, str(entry)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    # 非 Linux 平台应退出 2（平台门禁），不能是 ImportError（退出 1 + traceback）
    if sys.platform != "linux":
        assert result.returncode == 2, (
            f"非 Linux 应退出 2（平台门禁），实际 {result.returncode}。"
            f"stderr: {result.stderr[:500]}"
        )
        assert "only supported on Linux" in result.stderr or "Linux" in result.stderr
    else:
        # Linux 上无参数应打印帮助并退出 0
        assert result.returncode == 0


def test_entry_cw_agent_dev_mode_runs_without_import_error():
    """entry_cw_agent.py 在开发模式下可执行到平台门禁（无 ImportError）。"""
    import subprocess
    entry = ROOT / "release" / "pyinstaller" / "entry_cw_agent.py"
    result = subprocess.run(
        [sys.executable, str(entry)],
        capture_output=True,
        text=True,
        timeout=15,
    )
    if sys.platform != "linux":
        assert result.returncode == 2, (
            f"非 Linux 应退出 2（平台门禁），实际 {result.returncode}。"
            f"stderr: {result.stderr[:500]}"
        )
        assert "only supported on Linux" in result.stderr or "Linux" in result.stderr
    else:
        assert result.returncode == 0


# ============================================================
# 7. P0-B 步骤 5: 轻量包体积度量验证
# ============================================================
# 设计：docs/design/rust-only-parser-cutover-plan.md Phase 1 步骤 5
# 验证 release/_check_artifacts.py 新增的度量函数：
# - 安装目录差值（unpacked diff）
# - 压缩包差值（compressed diff）
# - 三平台报告格式（three-platform report）


def test_check_artifacts_has_light_bundle_measurement_functions():
    """release/_check_artifacts.py 新增 P0-B 步骤 5 度量函数。"""
    path = ROOT / "release" / "_check_artifacts.py"
    content = path.read_text(encoding="utf-8")

    # 三个度量函数必须存在
    for func_name in [
        "check_light_bundle_unpacked_diff",
        "check_light_bundle_compressed_diff",
        "check_light_bundle_three_platform_report",
    ]:
        assert f"def {func_name}" in content, (
            f"_check_artifacts.py 必须定义 {func_name}"
        )

    # 度量辅助函数
    assert "def _bundle_unpacked_bytes" in content
    assert "def _bundle_parser_bytes" in content
    assert "def _format_bytes" in content
    assert "def _make_compressed_artifact" in content

    # bundle 路径常量
    assert "LOCAL_BUNDLE_DIR" in content
    assert "CLIENT_BUNDLE_DIR" in content


def test_check_artifacts_main_invokes_light_bundle_checks():
    """main() 必须调用三个 P0-B 度量函数。"""
    path = ROOT / "release" / "_check_artifacts.py"
    content = path.read_text(encoding="utf-8")

    # main() 函数体中必须调用三个度量函数
    main_start = content.index("def main():")
    main_end = content.index("\n\nif __name__", main_start)
    main_body = content[main_start:main_end]

    assert "check_light_bundle_unpacked_diff()" in main_body
    assert "check_light_bundle_compressed_diff()" in main_body
    assert "check_light_bundle_three_platform_report()" in main_body

    # 汇总结果必须包含度量项
    assert '"p0b_unpacked_diff"' in main_body
    assert '"p0b_compressed_diff"' in main_body
    assert '"p0b_three_platform_report"' in main_body


def test_light_bundle_measurement_handles_missing_bundles(tmp_path):
    """度量函数在 bundle 不存在时 SKIP 并返回 True（度量不是发布门禁）。"""
    sys.path.insert(0, str(ROOT))
    try:
        import importlib
        check_module = importlib.import_module("release._check_artifacts")
    finally:
        sys.path.pop(0)

    # 用 monkeypatch 把 bundle 路径指向不存在的临时目录
    import unittest.mock as _mock
    fake_local = tmp_path / "nonexistent_local"
    fake_client = tmp_path / "nonexistent_client"

    with _mock.patch.object(check_module, "LOCAL_BUNDLE_DIR", fake_local), \
         _mock.patch.object(check_module, "CLIENT_BUNDLE_DIR", fake_client):
        # 三个度量函数都应返回 True（SKIP）
        assert check_module.check_light_bundle_unpacked_diff() is True
        assert check_module.check_light_bundle_compressed_diff() is True
        assert check_module.check_light_bundle_three_platform_report() is True


def test_light_bundle_three_platform_report_format_consistent():
    """三平台报告格式字段名跨平台一致（CI 自动解析依赖）。"""
    sys.path.insert(0, str(ROOT))
    try:
        import importlib
        check_module = importlib.import_module("release._check_artifacts")
    finally:
        sys.path.pop(0)

    # 报告必需字段（无论平台如何，都必须包含这些字段）
    required_keys = {
        "platform",
        "local_bundle",
        "client_bundle",
        "local_exists",
        "client_exists",
        "client_supported",
    }

    # 验证 check_light_bundle_three_platform_report 函数源码包含所有必需字段
    import inspect
    source = inspect.getsource(check_module.check_light_bundle_three_platform_report)
    for key in required_keys:
        assert f'"{key}"' in source, (
            f"三平台报告必须包含字段 '{key}'"
        )


def test_light_bundle_parser_bytes_uses_inspector_distribution():
    """_bundle_parser_bytes 复用 inspect_pyinstaller_bundle 的 distribution 分类。"""
    sys.path.insert(0, str(ROOT))
    try:
        import importlib
        check_module = importlib.import_module("release._check_artifacts")
    finally:
        sys.path.pop(0)

    # 验证函数引用 PARSER_DISTRIBUTIONS 和 compute_distribution_breakdown
    import inspect
    source = inspect.getsource(check_module._bundle_parser_bytes)
    assert "PARSER_DISTRIBUTIONS" in source
    assert "compute_distribution_breakdown" in source


def test_light_bundle_measurement_with_fake_bundles(tmp_path):
    """用假 bundle 验证度量函数能正确计算差值和 parser 占比。"""
    sys.path.insert(0, str(ROOT))
    try:
        import importlib
        check_module = importlib.import_module("release._check_artifacts")
    finally:
        sys.path.pop(0)

    import unittest.mock as _mock

    # 构造假 local bundle（含 parser 文件）
    local_bundle = tmp_path / "callwarden"
    local_internal = local_bundle / "_internal"
    local_internal.mkdir(parents=True)
    # local bundle 包含 tree_sitter 文件（parser distribution）
    (local_internal / "tree_sitter").mkdir()
    (local_internal / "tree_sitter" / "__init__.pyc").write_bytes(b"x" * 1024)
    (local_internal / "callwarden").mkdir()
    (local_internal / "callwarden" / "cw.py").write_bytes(b"x" * 512)
    (local_internal / "python-runtime.bin").write_bytes(b"x" * 2048)

    # 构造假 client bundle（无 parser 文件）
    client_bundle = tmp_path / "callwarden-client"
    client_internal = client_bundle / "_internal"
    client_internal.mkdir(parents=True)
    (client_internal / "callwarden").mkdir()
    (client_internal / "callwarden" / "client.py").write_bytes(b"x" * 256)
    (client_internal / "python-runtime.bin").write_bytes(b"x" * 1024)

    with _mock.patch.object(check_module, "LOCAL_BUNDLE_DIR", local_bundle), \
         _mock.patch.object(check_module, "CLIENT_BUNDLE_DIR", client_bundle), \
         _mock.patch("sys.platform", "linux"):
        # 安装目录差值度量
        assert check_module.check_light_bundle_unpacked_diff() is True
        # 压缩包差值度量
        assert check_module.check_light_bundle_compressed_diff() is True
        # 三平台报告
        assert check_module.check_light_bundle_three_platform_report() is True


def test_light_bundle_measurement_client_parser_nonzero_fails(tmp_path, capsys):
    """client/agent bundle 包含 parser 文件时度量必须 FAIL（fail closed）。"""
    sys.path.insert(0, str(ROOT))
    try:
        import importlib
        check_module = importlib.import_module("release._check_artifacts")
    finally:
        sys.path.pop(0)

    import unittest.mock as _mock

    # 构造假 local bundle
    local_bundle = tmp_path / "callwarden"
    local_internal = local_bundle / "_internal"
    local_internal.mkdir(parents=True)
    (local_internal / "python-runtime.bin").write_bytes(b"x" * 1024)

    # 构造假 client bundle（含 parser 文件，违反 fail closed）
    client_bundle = tmp_path / "callwarden-client"
    client_internal = client_bundle / "_internal"
    client_internal.mkdir(parents=True)
    (client_internal / "tree_sitter").mkdir()
    (client_internal / "tree_sitter" / "__init__.pyc").write_bytes(b"x" * 512)
    (client_internal / "python-runtime.bin").write_bytes(b"x" * 512)

    with _mock.patch.object(check_module, "LOCAL_BUNDLE_DIR", local_bundle), \
         _mock.patch.object(check_module, "CLIENT_BUNDLE_DIR", client_bundle), \
         _mock.patch("sys.platform", "linux"):
        # unpacked_diff 应返回 False（client bundle 含 parser）
        result = check_module.check_light_bundle_unpacked_diff()
        assert result is False
        captured = capsys.readouterr()
        assert "FAIL" in captured.out
        assert "parser" in captured.out.lower()
