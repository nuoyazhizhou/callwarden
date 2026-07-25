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
    # grammar wheel
    (internal / "tree_sitter_python").mkdir(parents=True, exist_ok=True)
    (internal / "tree_sitter_python" / "binding.pyd").write_bytes(b"grammar")
    # callwarden.parsers 实现模块
    (internal / "callwarden" / "parsers").mkdir(parents=True, exist_ok=True)
    (internal / "callwarden" / "parsers" / "rust.pyc").write_bytes(b"parser")


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


def test_inspector_local_role_allows_parser_distributions(tmp_path):
    """local 角色不禁止 parser distribution（保留 Python parser 回退路径）。"""
    bundle, toc = _write_bundle(
        tmp_path,
        REQUIRED_FIXTURE_MODULES + ["callwarden.cw", "tree_sitter"],
    )
    _add_parser_files(bundle)

    report, errors = inspect_bundle(bundle, toc, role="local")

    # local 角色不应因 parser distribution 报错
    assert not any("必须为空" in error for error in errors)
    assert report["role"] == "local"


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
