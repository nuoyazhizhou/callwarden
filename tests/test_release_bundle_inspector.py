"""PyInstaller 发布包清单和体积门禁测试。"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

from release.inspect_pyinstaller_bundle import inspect_bundle


ROOT = Path(__file__).resolve().parents[1]
INSPECTOR = ROOT / "release" / "inspect_pyinstaller_bundle.py"


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
        ["callwarden.cw", "mcp.server.fastmcp", "pydantic"],
    )

    report, errors = inspect_bundle(bundle, toc, max_unpacked_mb=1)

    assert errors == []
    assert report["internal_directories"] == ["_internal"]
    assert report["module_roots"]["callwarden"] == 1
    assert report["forbidden_modules"] == []


def test_inspector_rejects_nested_duplicate_runtime(tmp_path):
    bundle, toc = _write_bundle(tmp_path, ["callwarden.cw"])
    (bundle / "cw-client" / "_internal").mkdir(parents=True)

    _, errors = inspect_bundle(bundle, toc)

    assert any("必须且只能有一个根级 _internal" in error for error in errors)


@pytest.mark.parametrize("module", ["fastmcp", "boto3.s3", "opentelemetry.sdk"])
def test_inspector_rejects_forbidden_module_roots(tmp_path, module):
    bundle, toc = _write_bundle(tmp_path, ["callwarden.cw", module])

    report, errors = inspect_bundle(bundle, toc)

    assert module in report["forbidden_modules"]
    assert any("禁止打包的模块根" in error for error in errors)


def test_inspector_rejects_oversized_bundle(tmp_path):
    bundle, toc = _write_bundle(tmp_path, ["callwarden.cw"])

    _, errors = inspect_bundle(bundle, toc, max_unpacked_mb=0.0001)

    assert any("超过门禁" in error for error in errors)


def test_inspector_cli_writes_failure_report(tmp_path):
    bundle, toc = _write_bundle(tmp_path, ["callwarden.cw", "fastmcp"])
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


def test_pyz_fixture_is_literal_eval_compatible(tmp_path):
    """防止测试夹具退化成 inspector 无法读取的自造格式。"""
    _, toc = _write_bundle(tmp_path, ["callwarden.cw"])
    parsed = ast.literal_eval(toc.read_text(encoding="utf-8"))
    assert parsed[0][0] == "callwarden.cw"


def test_inspector_reads_pyinstaller_6_wrapped_toc(tmp_path):
    bundle, toc = _write_bundle(tmp_path, ["callwarden.cw"])
    module_toc = ast.literal_eval(toc.read_text(encoding="utf-8"))
    toc.write_text(repr(("/tmp/PYZ-00.pyz", module_toc)), encoding="utf-8")

    report, errors = inspect_bundle(bundle, toc)

    assert errors == []
    assert report["module_count"] == 1


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
