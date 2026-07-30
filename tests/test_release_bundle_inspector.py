"""PyInstaller 发布包清单和体积门禁测试。"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from release.inspect_pyinstaller_bundle import (
    PARSER_DISTRIBUTIONS,
    REQUIRED_MODULE_ROOTS,
    REQUIRED_MODULE_ROOTS_CLIENT,
    inspect_bundle,
)


ROOT = Path(__file__).resolve().parents[1]
INSPECTOR = ROOT / "release" / "inspect_pyinstaller_bundle.py"
REQUIRED_FIXTURE_MODULES = sorted(
    f"{root}.__init__" for root in REQUIRED_MODULE_ROOTS
)


def _write_bundle(tmp_path: Path, modules: list[str]) -> tuple[Path, Path]:
    """创建最小发布目录和兼容 PyInstaller 的 PYZ TOC。"""
    bundle = tmp_path / "callwarden"
    internal = bundle / "_internal"
    internal.mkdir(parents=True)
    (bundle / ("cw.exe" if sys.platform == "win32" else "cw")).write_bytes(b"cw")
    (internal / "python-runtime.bin").write_bytes(b"x" * 1024)
    # P1-G：bundle 必须包含 Rust callwarden_core 扩展（占位文件，不真实加载）
    core_ext = ".pyd" if sys.platform == "win32" else ".so"
    (internal / f"callwarden_core{core_ext}").write_bytes(b"rust-core-placeholder")
    toc = tmp_path / "PYZ-00.toc"
    toc.write_text(
        repr([(name, f"/fake/{name}.pyc", "PYMODULE") for name in modules]),
        encoding="utf-8",
    )
    return bundle, toc


def test_inspector_accepts_single_shared_runtime(tmp_path):
    bundle, toc = _write_bundle(
        tmp_path,
        REQUIRED_FIXTURE_MODULES
        + ["callwarden.cw", "mcp.server.fastmcp", "pydantic"],
    )

    report, errors = inspect_bundle(bundle, toc, max_unpacked_mb=1)

    assert errors == []
    assert report["internal_directories"] == ["_internal"]
    assert report["module_roots"]["callwarden"] >= 1
    assert report["forbidden_modules"] == []


def test_inspector_rejects_nested_duplicate_runtime(tmp_path):
    bundle, toc = _write_bundle(tmp_path, REQUIRED_FIXTURE_MODULES)
    (bundle / "cw-client" / "_internal").mkdir(parents=True)

    _, errors = inspect_bundle(bundle, toc)

    assert any("必须且只能有一个根级 _internal" in error for error in errors)


@pytest.mark.parametrize(
    "module",
    [
        "fastmcp",
        "boto3.s3",
        "opentelemetry.sdk",
        "dns.resolver",
        "email_validator",
        "semgrep",
        "sentence_transformers",
        "sqlite_vec",
        "torch",
        "transformers",
    ],
)
def test_inspector_rejects_forbidden_module_roots(tmp_path, module):
    bundle, toc = _write_bundle(tmp_path, REQUIRED_FIXTURE_MODULES + [module])

    report, errors = inspect_bundle(bundle, toc)

    assert module in report["forbidden_modules"]
    assert any("禁止打包的模块根" in error for error in errors)


def test_inspector_rejects_oversized_bundle(tmp_path):
    bundle, toc = _write_bundle(tmp_path, REQUIRED_FIXTURE_MODULES)

    _, errors = inspect_bundle(bundle, toc, max_unpacked_mb=0.0001)

    assert any("超过门禁" in error for error in errors)


def test_inspector_cli_writes_failure_report(tmp_path):
    bundle, toc = _write_bundle(
        tmp_path,
        REQUIRED_FIXTURE_MODULES + ["fastmcp"],
    )
    report_path = tmp_path / "reports" / "bundle.json"

    result = subprocess.run(
        [
            sys.executable,
            str(INSPECTOR),
            "--bundle",
            str(bundle),
            "--pyz-toc",
            str(toc),
            "--report",
            str(report_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 2
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["errors"]


def test_inspector_rejects_missing_required_module_root(tmp_path):
    modules = [
        module
        for module in REQUIRED_FIXTURE_MODULES
        if not module.startswith("requests.")
    ]
    bundle, toc = _write_bundle(tmp_path, modules)

    report, errors = inspect_bundle(bundle, toc)

    assert report["missing_required_module_roots"] == ["requests"]
    assert any("缺少必需模块根" in error for error in errors)


def test_release_spec_uses_targeted_mcp_collection_and_strip():
    spec = (ROOT / "release" / "pyinstaller" / "callwarden.spec").read_text(
        encoding="utf-8"
    )
    cargo = (ROOT / "rust_ext" / "Cargo.toml").read_text(encoding="utf-8")
    workflow = (
        ROOT / ".github" / "workflows" / "pyinstaller-build.yml"
    ).read_text(encoding="utf-8")

    assert "collect_submodules(" not in spec
    assert "'CW_RUST_EXT_PATH'" in spec
    assert "'mailbox', 'mimetypes'" not in spec
    assert "'mcp.server.fastmcp.server'" in spec
    for module in ["'fastmcp'", "'boto3'", "'botocore'", "'opentelemetry'"]:
        assert module in spec
    assert 'strip = "symbols"' in cargo
    assert "inspect_pyinstaller_bundle.py" in workflow
    assert "server --check-imports" in workflow
    assert "release/pyinstaller/requirements-build.txt" in workflow
    assert "pip install torch" not in workflow
    assert "pip install -r requirements.txt" not in workflow


def test_pyz_fixture_is_literal_eval_compatible(tmp_path):
    """防止测试夹具退化成 inspector 无法读取的自造格式。"""
    _, toc = _write_bundle(tmp_path, ["callwarden.cw"])
    parsed = ast.literal_eval(toc.read_text(encoding="utf-8"))
    assert parsed[0][0] == "callwarden.cw"


def test_inspector_reads_pyinstaller_6_wrapped_toc(tmp_path):
    bundle, toc = _write_bundle(tmp_path, REQUIRED_FIXTURE_MODULES)
    module_toc = ast.literal_eval(toc.read_text(encoding="utf-8"))
    toc.write_text(repr(("/tmp/PYZ-00.pyz", module_toc)), encoding="utf-8")

    report, errors = inspect_bundle(bundle, toc)

    assert errors == []
    assert report["module_count"] == len(REQUIRED_FIXTURE_MODULES)


def test_mcp_import_check_has_no_startup_side_effects(monkeypatch, capsys):
    from callwarden.server import mcp_server

    calls = []

    class FakeServer:
        def run(self):
            calls.append("run")

    monkeypatch.setattr(sys, "argv", ["cw", "--check-imports"])
    monkeypatch.setattr(mcp_server, "create_mcp_server", FakeServer)
    monkeypatch.setattr(
        mcp_server,
        "_auto_sync_agents_md",
        lambda: calls.append("sync"),
    )
    monkeypatch.setattr(
        mcp_server,
        "_ensure_semgrep_rules_cache",
        lambda: calls.append("semgrep"),
    )

    mcp_server.main()

    assert calls == []
    assert capsys.readouterr().out.strip() == "Call Warden MCP imports OK"


# ============================================
# P0-B: 角色专属 bundle 检查（client/agent 无 parser）
# ============================================


def _write_client_bundle(tmp_path: Path, modules: list[str]) -> tuple[Path, Path]:
    """创建 client/agent 发布目录（含 cw-client 入口）和 PYZ TOC。"""
    bundle = tmp_path / "callwarden-client"
    internal = bundle / "_internal"
    internal.mkdir(parents=True)
    (bundle / "cw-client").write_bytes(b"cw-client")
    (bundle / "cw-agent").write_bytes(b"cw-agent")
    (internal / "python-runtime.bin").write_bytes(b"x" * 512)
    # P1-G：client/agent bundle 也需要 Rust callwarden_core（canonicalize_source_py 等）
    core_ext = ".pyd" if sys.platform == "win32" else ".so"
    (internal / f"callwarden_core{core_ext}").write_bytes(b"rust-core-placeholder")
    toc = tmp_path / "PYZ-00.toc"
    toc.write_text(
        repr([(name, f"/fake/{name}.pyc", "PYMODULE") for name in modules]),
        encoding="utf-8",
    )
    return bundle, toc


def _add_parser_files(bundle: Path) -> None:
    """向 bundle 注入 parser/grammar 文件（模拟泄漏场景）。"""
    internal = bundle / "_internal"
    # tree-sitter 核心
    (internal / "tree_sitter").mkdir(parents=True, exist_ok=True)
    (internal / "tree_sitter" / "__init__.pyc").write_bytes(b"tree_sitter")
    # tree-sitter 核心 binding 原生库（_binding.abi3.so / _binding.cp*.pyd）
    core_ext = ".pyd" if sys.platform == "win32" else ".so"
    (internal / "tree_sitter" / f"_binding.abi3{core_ext}").write_bytes(b"binding")
    # grammar wheel
    (internal / "tree_sitter_python").mkdir(parents=True, exist_ok=True)
    (internal / "tree_sitter_python" / "binding.pyd").write_bytes(b"grammar")
    # callwarden.parsers 实现模块（源文件 + 字节码）
    (internal / "callwarden" / "parsers").mkdir(parents=True, exist_ok=True)
    (internal / "callwarden" / "parsers" / "rust.pyc").write_bytes(b"parser")
    (internal / "callwarden" / "parsers" / "python_parser.py").write_bytes(b"# py")


_CLIENT_FIXTURE_MODULES = sorted(
    f"{root}.__init__" for root in REQUIRED_MODULE_ROOTS_CLIENT
)


def test_inspector_client_role_does_not_require_numpy(tmp_path):
    """client 角色不要求 numpy 模块根（client 不做本地解析）。"""
    bundle, toc = _write_client_bundle(
        tmp_path,
        _CLIENT_FIXTURE_MODULES + ["callwarden.cli.daemon_commands"],
    )

    report, errors = inspect_bundle(bundle, toc, role="client")

    assert "numpy" not in report["missing_required_module_roots"]
    assert report["role"] == "client"


def test_inspector_client_role_forbids_parser_distributions(tmp_path):
    """client 角色自动禁止所有 parser distribution（parser distribution=0）。"""
    bundle, toc = _write_client_bundle(
        tmp_path,
        _CLIENT_FIXTURE_MODULES + ["callwarden.cli.daemon_commands"],
    )
    _add_parser_files(bundle)

    report, errors = inspect_bundle(bundle, toc, role="client")

    # parser distribution 必须被报告为非零
    dists = report["distributions"]
    assert dists["tree_sitter"]["file_count"] > 0
    assert dists["tree_sitter_python"]["file_count"] > 0
    assert dists["callwarden_parsers"]["file_count"] > 0
    # 必须有零容忍错误
    assert any("必须为空" in error for error in errors)
    assert any("tree_sitter" in error for error in errors)


def test_inspector_local_role_p1_g_forbids_parser_distributions(tmp_path):
    """P1-G 后 local 角色也禁止 parser distribution（fail closed）。

    设计 §8 Phase 5 步骤 6：所有 bundle 都禁止 PARSER_DISTRIBUTIONS。
    向后兼容通过 ``allow_parser_distributions=True`` 显式开启。
    """
    bundle, toc = _write_bundle(
        tmp_path,
        REQUIRED_FIXTURE_MODULES + ["callwarden.cw", "tree_sitter"],
    )
    _add_parser_files(bundle)

    report, errors = inspect_bundle(bundle, toc, role="local")

    # P1-G 后 local 角色也必须报 parser distribution 错误
    assert any("必须为空" in error for error in errors)
    assert any("tree_sitter" in error for error in errors)
    # 文件级 fail closed 检查也必须触发
    assert any("_binding" in error for error in errors), (
        "应检测到 _binding*.pyd/.so 文件"
    )
    assert any("callwarden.parsers" in error for error in errors), (
        "应检测到 callwarden/parsers 源文件"
    )
    assert report["role"] == "local"


def test_inspector_allow_parser_distributions_flag_skips_p1_g_checks(tmp_path):
    """``allow_parser_distributions=True`` 跳过 P1-G fail closed 检查（向后兼容）。"""
    bundle, toc = _write_bundle(
        tmp_path,
        REQUIRED_FIXTURE_MODULES + ["callwarden.cw", "tree_sitter"],
    )
    _add_parser_files(bundle)

    report, errors = inspect_bundle(
        bundle, toc, role="local", allow_parser_distributions=True
    )

    # 向后兼容模式下不报 parser distribution 错误
    assert not any("必须为空" in error for error in errors)
    assert not any("_binding" in error for error in errors)
    assert not any("callwarden.parsers" in error for error in errors)


def test_inspector_p1_g_requires_callwarden_core_present(tmp_path):
    """P1-G: bundle 中必须存在 Rust callwarden_core 扩展。"""
    bundle, toc = _write_bundle(
        tmp_path,
        REQUIRED_FIXTURE_MODULES + ["callwarden.cw"],
    )
    # 删除 callwarden_core 文件
    core_ext = ".pyd" if sys.platform == "win32" else ".so"
    (bundle / "_internal" / f"callwarden_core{core_ext}").unlink()

    report, errors = inspect_bundle(bundle, toc, role="local")

    assert any("callwarden_core" in error for error in errors)
    assert any("P1-G" in error or "Rust" in error for error in errors)


def test_inspector_rejects_duplicate_callwarden_core(tmp_path):
    """R10-P1-2-b: bundle 中重复的 callwarden_core 扩展必须 fail closed。

    场景：PyInstaller spec 同时声明 ``binaries`` 和 ``hiddenimports`` 中的
    ``callwarden_core`` 会导致两份 .pyd/.so 被收集（一份来自项目根目录的
    binaries，一份来自 site-packages 的 hiddenimports）。冻结包体积膨胀
    ~36MB，且运行时 sys.modules 加载顺序不确定。
    """
    bundle, toc = _write_bundle(
        tmp_path,
        REQUIRED_FIXTURE_MODULES + ["callwarden.cw"],
    )
    # _write_bundle 已在 _internal 下放置一份 callwarden_core
    # 再添加第二份（模拟 PyInstaller 重复收集场景）
    core_ext = ".pyd" if sys.platform == "win32" else ".so"
    # 模拟 binaries 收集的根级副本（PyInstaller --onedir 中 binaries='.' 会放到 _internal/）
    # 同时模拟 hiddenimports 收集的带 ABI 后缀副本
    abi_name = (
        "callwarden_core.cp314-win_amd64.pyd"
        if sys.platform == "win32"
        else "callwarden_core.cpython-314-x86_64-linux-gnu.so"
    )
    (bundle / "_internal" / abi_name).write_bytes(b"duplicate-rust-core")

    report, errors = inspect_bundle(bundle, toc, role="local")

    # 必须报重复扩展错误
    dup_errors = [e for e in errors if "重复存在" in e or "R10-P1-2-b" in e]
    assert dup_errors, (
        f"期望检测到重复 callwarden_core 扩展，实际错误: {errors}"
    )
    # distribution 报告中 callwarden_core 应有 2 个文件
    assert report["distributions"]["callwarden_core"]["file_count"] == 2


def test_inspector_verify_rust_parse_rejects_duplicate(tmp_path):
    """R10-P1-2-b: --verify-rust-parse 在发现重复扩展时直接 fail closed。

    之前 ``_verify_rust_parse`` 用 ``core_files[0]`` 取第一个加载，
    重复场景下可能加载到错误版本。修复后必须报错，不选择加载。
    """
    from release.inspect_pyinstaller_bundle import _verify_rust_parse

    bundle = tmp_path / "callwarden"
    internal = bundle / "_internal"
    internal.mkdir(parents=True)
    # 放置两份 callwarden_core 文件
    core_ext = ".pyd" if sys.platform == "win32" else ".so"
    (internal / f"callwarden_core{core_ext}").write_bytes(b"core-1")
    abi_name = (
        "callwarden_core.cp314-win_amd64.pyd"
        if sys.platform == "win32"
        else "callwarden_core.cpython-314-x86_64-linux-gnu.so"
    )
    (internal / abi_name).write_bytes(b"core-2")

    errors = _verify_rust_parse(bundle)

    assert errors, "期望重复扩展时 _verify_rust_parse 报错"
    assert any("重复存在" in e or "R10-P1-2-b" in e for e in errors), errors


def test_inspector_verify_rust_parse_uses_real_module_name():
    """R10-P1-2-a: _verify_rust_parse 必须用真实模块名 callwarden_core 加载。

    之前使用 ``callwarden_core_verify`` 会导致 PyO3 在 Python 3.11+ 抛
    SystemError（module name mismatch），且加载后的模块不会注册到
    sys.modules['callwarden_core']，后续生产代码 ``import callwarden_core``
    仍会找不到模块。修复后必须用真实模块名 ``callwarden_core``，与
    Rust #[pymodule] 声明一致。

    本测试通过检查源码字符串确认 spec_from_file_location 的第一个参数
    是 "callwarden_core"（而非 "callwarden_core_verify" 或其他变体），
    避免在测试中真实加载 PyO3 扩展（cross-platform ABI 不兼容）。
    """
    source = INSPECTOR.read_text(encoding="utf-8")

    # 定位 _verify_rust_parse 函数体
    func_start = source.find("def _verify_rust_parse(")
    assert func_start >= 0, "找不到 _verify_rust_parse 函数"
    # 截取到下一个 def 之前
    next_def = source.find("\ndef ", func_start + 10)
    if next_def < 0:
        next_def = len(source)
    func_body = source[func_start:next_def]

    # 必须使用 "callwarden_core" 作为模块名
    assert '"callwarden_core"' in func_body or "'callwarden_core'" in func_body, (
        "R10-P1-2-a: _verify_rust_parse 必须用真实模块名 'callwarden_core' 加载"
    )
    # 必须不能在 spec_from_file_location 调用中使用 callwarden_core_verify 等自定义名称
    # 只检查 spec_from_file_location 调用行（避免误判 docstring 中的历史说明）
    import re as _re

    spec_call = _re.search(
        r'spec_from_file_location\s*\(\s*["\']([^"\']+)["\']', func_body
    )
    assert spec_call is not None, (
        "R10-P1-2-a: _verify_rust_parse 应调用 spec_from_file_location 加载模块"
    )
    module_name = spec_call.group(1)
    assert module_name == "callwarden_core", (
        f"R10-P1-2-a: spec_from_file_location 模块名必须为 'callwarden_core'，"
        f"实际为 '{module_name}'"
    )


def test_inspector_spec_does_not_declare_callwarden_core_in_hiddenimports():
    """R10-P1-2-c: PyInstaller spec 不应在 hiddenimports 中声明 callwarden_core。

    之前同时声明 binaries 和 hiddenimports 中的 callwarden_core 会导致
    PyInstaller 收集两份 .pyd/.so（一份来自 binaries，一份来自 site-packages）。
    修复后仅通过 binaries 提供。
    """
    spec = (ROOT / "release" / "pyinstaller" / "callwarden.spec").read_text(
        encoding="utf-8"
    )

    # 只检查两个 hiddenimports 列表本身。`_common_excludes` 必须保留
    # `callwarden_core`，用于阻止 PyInstaller 从 site-packages 再收集一份
    # 扩展；把它当成 hiddenimport 会产生假失败。
    import re

    matches = []
    for list_name in ("_local_hiddenimports", "_client_agent_hiddenimports"):
        block = re.search(
            rf"{list_name}\s*=\s*\[(.*?)\]",
            spec,
            flags=re.DOTALL,
        )
        assert block is not None, f"spec 缺少 {list_name} 列表"
        matches.extend(re.findall(r"^\s*'callwarden_core'\s*,\s*$", block.group(1), re.MULTILINE))
    assert not matches, (
        "spec 的 hiddenimports 中仍声明了 'callwarden_core'，"
        "应仅通过 binaries 提供（R10-P1-2-c）"
    )


@pytest.mark.parametrize(
    "filename",
    [
        "callwarden_core.pyd",
        "callwarden_core.so",
        "callwarden_core.cp314-win_amd64.pyd",
        "callwarden_core.cpython-314-x86_64-linux-gnu.so",
        "callwarden_core.abi3.so",
    ],
)
def test_inspector_classify_callwarden_core_with_abi_suffix(filename):
    """R4: inspector 识别带 ABI 后缀的 Rust 扩展变体（PEP 3149 / Windows ABI tag）。

    PyInstaller 打包 PyO3 扩展时会保留 wheel 的 ABI 后缀，inspector 必须识别
    这些变体，否则会误报"Rust 扩展缺失"导致 P1-G 门禁假红。
    """
    from release.inspect_pyinstaller_bundle import _is_callwarden_core_file

    assert _is_callwarden_core_file(filename) is True
    # 不相关文件必须返回 False
    assert _is_callwarden_core_file("tree_sitter.pyd") is False
    assert _is_callwarden_core_file("callwarden_core.py") is False
    assert _is_callwarden_core_file("not_callwarden_core.pyd") is False


def test_inspector_accepts_abi_suffixed_callwarden_core(tmp_path):
    """R4: bundle 中带 ABI 后缀的 callwarden_core 应通过 P1-G 存在性检查。"""
    bundle = tmp_path / "callwarden"
    internal = bundle / "_internal"
    internal.mkdir(parents=True)
    (bundle / "cw.exe").write_bytes(b"cw")
    (internal / "python-runtime.bin").write_bytes(b"x" * 64)
    # 模拟 PyInstaller 打包 PyO3 扩展时的 ABI 后缀文件名
    abi_name = (
        "callwarden_core.cp314-win_amd64.pyd"
        if sys.platform == "win32"
        else "callwarden_core.cpython-314-x86_64-linux-gnu.so"
    )
    (internal / abi_name).write_bytes(b"rust-core-placeholder")

    toc = tmp_path / "PYZ-00.toc"
    toc.write_text(
        repr([(name, f"/fake/{name}.pyc", "PYMODULE") for name in REQUIRED_FIXTURE_MODULES]),
        encoding="utf-8",
    )

    report, errors = inspect_bundle(bundle, toc, role="local")

    # 不应报 Rust 扩展缺失
    assert not any("callwarden_core" in error for error in errors), errors
    # distribution 报告中应将 ABI 后缀文件归入 callwarden_core
    assert report["distributions"]["callwarden_core"]["file_count"] == 1


def test_inspector_parser_distributions_constant_covers_all_parsers():
    """PARSER_DISTRIBUTIONS 常量覆盖 tree_sitter 核心 + grammar + callwarden_parsers。"""
    assert "tree_sitter" in PARSER_DISTRIBUTIONS
    assert "callwarden_parsers" in PARSER_DISTRIBUTIONS
    assert "tree_sitter_rust" in PARSER_DISTRIBUTIONS
    assert "tree_sitter_python" in PARSER_DISTRIBUTIONS
    assert len(PARSER_DISTRIBUTIONS) >= 18  # 1 核心 + 1 parsers + 16 grammar


def test_inspector_client_role_cli_passes_role(tmp_path):
    """CLI --role client 正确传递到 inspect_bundle 并写入报告。"""
    bundle, toc = _write_client_bundle(
        tmp_path,
        _CLIENT_FIXTURE_MODULES + ["callwarden.cli.daemon_commands"],
    )
    report_path = tmp_path / "reports" / "client-bundle.json"

    result = subprocess.run(
        [
            sys.executable,
            str(INSPECTOR),
            "--bundle",
            str(bundle),
            "--pyz-toc",
            str(toc),
            "--report",
            str(report_path),
            "--role",
            "client",
        ],
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    report = json.loads(report_path.read_text(encoding="utf-8"))
    assert report["role"] == "client"
    assert "numpy" not in report["missing_required_module_roots"]
