"""统一构建管道：setuptools + maturin 编排。

确保 Python wheel 安装时自带平台 Rust 扩展。
构建顺序：Rust .pyd/.so → Python wheel (包含 Rust 扩展) → wheelhouse 锁定依赖。

P1-G（2026-07-25）：新增 ``--pyinstaller`` 子命令构建角色产物（local / client）。
PyInstaller spec 已删除 Python tree-sitter 和 16 种 grammar 的 hidden imports，
并通过 ``_PARSER_GRAMMAR_EXCLUDES`` 显式排除 ``callwarden.parsers.*`` 和
``tree_sitter*``。本脚本在 PyInstaller 构建后自动调用
``release/inspect_pyinstaller_bundle.py`` 执行 fail closed 检查，并生成包含
parser ABI 的 artifact manifest（设计：rust-only-parser-cutover-plan.md
§8 Phase 5 步骤 6 + §8 Phase 6 manifest 字段）。

用法：
    python release/build.py              # 完整构建（rust + wheel + wheelhouse + manifest）
    python release/build.py --check      # 只验证版本一致性
    python release/build.py --wheel      # 只构建 Python wheel
    python release/build.py --rust       # 只构建 Rust 扩展
    python release/build.py --pyinstaller [--role local|client|all]
                                        # 构建 PyInstaller 角色产物并运行 bundle inspector
"""

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RELEASE_DIR = ROOT / "release"


def run(cmd, cwd=None, check=True):
    """运行命令并打印输出。"""
    print(f"  > {' '.join(cmd)}")
    result = subprocess.run(cmd, cwd=cwd or ROOT, capture_output=False)
    if check and result.returncode != 0:
        print(f"  [FAIL] Command failed with exit code {result.returncode}")
        sys.exit(1)
    return result


def check_versions():
    """验证版本一致性。"""
    print("Step 1: Version consistency check")
    run([sys.executable, str(RELEASE_DIR / "version_sync.py")])
    print()


def build_rust_extension():
    """构建 Rust PyO3 扩展。"""
    print("Step 2: Building Rust extension (callwarden_core)")
    rust_ext_dir = ROOT / "rust_ext"

    # cargo build --lib --release (限定构建 cdylib 扩展库)
    run(["cargo", "build", "--lib", "--release"], cwd=rust_ext_dir)

    # 查找构建产物
    target_dir = rust_ext_dir / "target" / "release"
    if sys.platform == "win32":
        lib_file = target_dir / "callwarden_core.dll"
        dest_name = "callwarden_core.pyd"
    elif sys.platform == "darwin":
        lib_file = target_dir / "libcallwarden_core.dylib"
        dest_name = "callwarden_core.so"
    else:
        lib_file = target_dir / "libcallwarden_core.so"
        dest_name = "callwarden_core.so"

    if not lib_file.exists():
        print(f"  [FAIL] Rust extension not found: {lib_file}")
        sys.exit(1)

    # 复制到项目根目录（供 setuptools 打包）
    dest = ROOT / dest_name
    import shutil
    shutil.copy2(str(lib_file), str(dest))
    print(f"  [OK] Rust extension copied to {dest_name}")
    print()


def _ensure_rust_ext_at_root():
    """P0-3 修复（问题 3）：干净 runner 上 Rust 扩展可能尚未复制到根目录。

    复审报告 §3 P0-3：CI 先在 rust_ext/target 构建，但 release/build.py --wheel
    要求根目录已存在 .pyd/.so；干净 runner 不会自动复制，Gate 2 会更早失败。

    本函数从 rust_ext/target/release/ 查找构建产物并复制到根目录，
    若根目录已存在且非空则跳过。失败时 fail-fast 退出。
    """
    if sys.platform == "win32":
        binary_name = "callwarden_core.pyd"
        src_name = "callwarden_core.dll"
    elif sys.platform == "darwin":
        binary_name = "callwarden_core.so"
        src_name = "libcallwarden_core.dylib"
    else:
        binary_name = "callwarden_core.so"
        src_name = "libcallwarden_core.so"

    root_binary = ROOT / binary_name
    if root_binary.exists() and root_binary.stat().st_size >= 1024:
        return  # 已存在且非空，无需复制

    src_path = ROOT / "rust_ext" / "target" / "release" / src_name
    if not src_path.exists():
        print(f"  [FAIL] Rust extension not built: {src_path}")
        print(f"  请先运行 'python release/build.py --rust' 或 'cd rust_ext && cargo build --release'")
        sys.exit(1)

    import shutil
    shutil.copy2(str(src_path), str(root_binary))
    print(f"  [OK] Copied Rust extension from rust_ext/target/release/ to {binary_name}")


def _verify_rust_extension_present():
    """P0-3 修复：构建 wheel 前强制检查 callwarden_core 二进制存在（fail-fast）。

    评审 P0-3：原代码在 Rust 扩展缺失时仍继续构建 wheel，导致 wheel 退化为
    py3-none-any 且不含 callwarden_core，安装后无 Rust 加速。这是隐性 fail-open。
    """
    if sys.platform == "win32":
        binary_name = "callwarden_core.pyd"
    else:
        binary_name = "callwarden_core.so"

    binary_path = ROOT / binary_name
    if not binary_path.exists():
        print(f"  [FAIL] Rust extension binary missing: {binary_path}")
        print(f"  拒绝构建 wheel：先运行 'python release/build.py --rust' 构建 Rust 扩展")
        sys.exit(1)

    # 验证文件非空（防止复制失败留下 0 字节文件）
    size = binary_path.stat().st_size
    if size < 1024:
        print(f"  [FAIL] Rust extension binary too small ({size} bytes): {binary_path}")
        print(f"  可能是构建失败或复制被截断，请重新运行 'python release/build.py --rust'")
        sys.exit(1)

    print(f"  [OK] Rust extension binary present: {binary_name} ({size} bytes)")


def _detect_wheel_platform_tag():
    """P0-3 修复：根据当前平台返回 wheel platform tag。

    让 wheel 标记为平台特定（manylinux2014_x86_64 / win_amd64 / macosx_*），
    而不是默认的 py3-none-any，避免 pip 在不兼容平台误装。
    """
    if sys.platform == "win32":
        return "win_amd64"
    elif sys.platform == "darwin":
        # macOS universal2 暂用当前架构 tag，CI 中通过矩阵构建 universal2
        import platform
        machine = platform.machine().lower()
        if machine == "arm64":
            return "macosx_11_0_arm64"
        return "macosx_11_0_x86_64"
    else:
        # Linux：默认 manylinux2014_x86_64；arm64 由 CI 矩阵覆盖
        import platform
        machine = platform.machine().lower()
        if machine == "aarch64":
            return "manylinux2014_aarch64"
        return "manylinux2014_x86_64"


def _verify_wheel_contains_rust_extension(wheel_path):
    """P0-3 修复：验证 wheel 中确实包含 callwarden_core 二进制。

    防止 pyproject.toml 配置错误导致 wheel 仍缺失 Rust 扩展（fail-visible）。
    """
    import zipfile
    if not wheel_path.exists():
        print(f"  [FAIL] Wheel not found: {wheel_path}")
        sys.exit(1)

    with zipfile.ZipFile(str(wheel_path)) as zf:
        names = zf.namelist()
        rust_files = [
            n for n in names
            if n.startswith("callwarden_core.") and (n.endswith(".pyd") or n.endswith(".so"))
        ]
        if not rust_files:
            print(f"  [FAIL] Wheel does not contain callwarden_core binary: {wheel_path}")
            print(f"  Wheel contents (first 20 entries):")
            for n in names[:20]:
                print(f"    {n}")
            print(f"  评审 P0-3：pyproject.toml 中 py-modules=['callwarden_core'] 是否配置正确？")
            sys.exit(1)
        print(f"  [OK] Wheel contains Rust extension: {rust_files[0]}")


def build_python_wheel():
    """构建 Python wheel。"""
    print("Step 3: Building Python wheel")

    # P0-3 修复（问题 3）：干净 runner 上 Rust 扩展可能尚未复制到根目录。
    # 先从 rust_ext/target/release/ 自动复制，再 fail-fast 校验。
    _ensure_rust_ext_at_root()

    # P0-3 修复：构建前强制检查 Rust 扩展存在
    _verify_rust_extension_present()

    # P0-3 修复（问题 1）：原代码使用 --config-setting --build-option=--plat-name={plat_tag}
    # 会导致 argparse 报 "argument --config-setting/-C: expected one argument"
    # （--build-option=... 以 -- 开头，argparse 把它当作新选项而非 --config-setting 的值）。
    # 同时 --build-option 在 setuptools 60+ 已废弃，setuptools 68+ 完全移除。
    #
    # 修复方案：删除 --config-setting 整行，依赖 setup.py 的 BinaryDistribution.has_ext_modules()=True
    # 让 bdist_wheel 自动把 wheel 标记为平台特定（cp311-cp311-{plat_tag}）。
    # _detect_wheel_platform_tag() 仅用于日志展示，不再传给 build。
    plat_tag = _detect_wheel_platform_tag()
    print(f"  Target platform tag (auto-detected by setup.py has_ext_modules): {plat_tag}")

    # 调用 python -m build --wheel，让 setup.py 的 PrebuiltBuildExt 复制 .pyd/.so 到 build/lib/
    run([
        sys.executable, "-m", "build", "--wheel",
        "--outdir", str(RELEASE_DIR / "dist"),
    ])

    # P0-3 修复：验证 wheel 中确实包含 Rust 扩展二进制
    import glob
    wheels = glob.glob(str(RELEASE_DIR / "dist" / "*.whl"))
    if not wheels:
        print("  [FAIL] No wheel produced")
        sys.exit(1)
    _verify_wheel_contains_rust_extension(Path(wheels[0]))
    print()


def build_wheelhouse():
    """锁定第三方依赖到 wheelhouse。"""
    print("Step 4: Building wheelhouse (locked dependencies)")
    wheelhouse = RELEASE_DIR / "wheelhouse"
    wheelhouse.mkdir(exist_ok=True)
    run([
        sys.executable, "-m", "pip", "wheel",
        "--wheel-dir", str(wheelhouse),
        "--no-deps",
        str(ROOT),
    ])
    print(f"  [OK] Wheelhouse: {wheelhouse}")
    print()


def _parse_toml_simple(text: str) -> dict:
    """Python < 3.11 且未安装 tomli 时的零依赖 TOML 解析降级实现。"""
    import re
    result: dict = {}
    cur_sec = None
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m_sec = re.match(r"^\[([a-zA-Z0-9_\-]+)\]$", line)
        if m_sec:
            cur_sec = m_sec.group(1)
            result.setdefault(cur_sec, {})
            continue
        m_kv = re.match(r"^([a-zA-Z0-9_\-]+)\s*=\s*(.+)$", line)
        if m_kv and cur_sec:
            k, v_str = m_kv.group(1), m_kv.group(2).strip()
            if v_str.startswith('"') and v_str.endswith('"'):
                v = v_str[1:-1]
            elif v_str.startswith("[") and v_str.endswith("]"):
                v = re.findall(r'"([^"]*)"', v_str)
            elif v_str.isdigit():
                v = int(v_str)
            else:
                v = v_str
            result[cur_sec][k] = v
    return result


def load_version_toml() -> dict:
    """加载 release/version.toml。"""
    version_file = RELEASE_DIR / "version.toml"
    with open(version_file, "r", encoding="utf-8") as f:
        content = f.read()
    try:
        import tomllib
        return tomllib.loads(content)
    except ImportError:
        try:
            import tomli
            return tomli.loads(content)
        except ImportError:
            return _parse_toml_simple(content)


def generate_manifest(bundles=None, bundle_reports=None):
    """生成 artifact manifest。

    P1-G（2026-07-25）：manifest 现在记录 parser ABI（设计 §8 Phase 6）和
    PyInstaller 角色产物的体积/distribution 信息，便于发布审计与回滚对照。

    Args:
        bundles: 可选，PyInstaller 产物路径列表（``dist/callwarden`` 等）。
            提供时把这些目录一并写入 manifest 的 ``bundles`` 字段。
        bundle_reports: 可选，bundle inspector 报告 dict 列表（与 bundles 一一对应）。
            提供时把每个 bundle 的 role / unpacked_bytes / distribution 信息写入 manifest。
    """
    print("Step 5: Generating artifact manifest")
    import hashlib
    import json
    import platform as _platform
    import time

    version_data = load_version_toml()

    manifest = {
        "product": version_data["product"]["name"],
        "version": version_data["product"]["version"],
        "build_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        # P1-G: 显式记录 parser ABI 与 runtime 元数据（设计 §8 Phase 6）
        "abi": version_data["abi"],
        "runtime": version_data.get("runtime", {}),
        "platforms": version_data["platforms"],
        # 构建机平台元数据（用于跨平台产物对照，CI 多矩阵构建时区分 OS/arch/libc）
        "build_host": {
            "os": _platform.system(),
            "machine": _platform.machine(),
            "python": sys.version.split()[0],
        },
        "artifacts": [],
    }

    # 扫描 dist 目录
    dist_dir = RELEASE_DIR / "dist"
    if dist_dir.exists():
        for f in sorted(dist_dir.iterdir()):
            if f.suffix in (".whl", ".tar.gz"):
                sha256 = hashlib.sha256(f.read_bytes()).hexdigest()
                manifest["artifacts"].append({
                    "filename": f.name,
                    "size": f.stat().st_size,
                    "sha256": sha256,
                })

    # P1-G: PyInstaller 角色产物清单
    if bundles:
        manifest_bundles = []
        for idx, bundle_path in enumerate(bundles):
            entry = {
                "path": str(bundle_path.relative_to(ROOT)) if bundle_path.is_absolute()
                else str(bundle_path),
            }
            if bundle_reports and idx < len(bundle_reports):
                report = bundle_reports[idx]
                entry["role"] = report.get("role", "local")
                entry["unpacked_bytes"] = report.get("unpacked_bytes", 0)
                entry["unpacked_mb"] = report.get("unpacked_mb", 0.0)
                entry["file_count"] = report.get("file_count", 0)
                entry["module_count"] = report.get("module_count", 0)
                # distribution 摘要（仅记录非零 distribution，便于审计）
                distributions = report.get("distributions", {})
                entry["distributions"] = {
                    name: {
                        "file_count": info["file_count"],
                        "byte_count": info["byte_count"],
                    }
                    for name, info in distributions.items()
                    if info.get("file_count", 0) > 0
                }
            manifest_bundles.append(entry)
        manifest["bundles"] = manifest_bundles

    manifest_path = RELEASE_DIR / "artifact-manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"  [OK] Manifest: {manifest_path}")
    print(f"  Artifacts: {len(manifest['artifacts'])}")
    if bundles:
        print(f"  Bundles: {len(bundles)}")
    print()


# ============================================================
# P1-G: PyInstaller 角色产物构建（设计 §8 Phase 5 步骤 6 + Phase 6）
# ============================================================

# 角色 → (bundle 目录名, PYZ TOC 文件名, 产物存在平台) 映射
# spec 文件中两个 COLLECT 共享同一份 spec 调用，PYZ TOC 编号由 PyInstaller 分配：
#   - PYZ-00.toc：local bundle（Analysis 1，所有平台）
#   - PYZ-01.toc：client/agent bundle（Analysis 2，仅 Linux）
# 这里只描述目标产物，spec 内部已通过 _PARSER_GRAMMAR_EXCLUDES fail closed。
_ROLE_BUNDLE_MAP = {
    "local": {
        "bundle_dir": "callwarden",
        "pyz_toc_candidates": ("PYZ-00.toc",),
        "role_flag": "local",
        "linux_only": False,
    },
    "client": {
        "bundle_dir": "callwarden-client",
        "pyz_toc_candidates": ("PYZ-01.toc", "PYZ-00.toc"),
        "role_flag": "client",
        "linux_only": True,
    },
}


def _parse_role_arg(argv):
    """从 sys.argv 解析 ``--role`` 参数，返回 (role, consumed_argv)。

    支持的值：``local`` / ``client`` / ``all``（默认 ``all``，构建当前平台支持的全部 bundle）。
    返回的 consumed_argv 用于从 sys.argv 中剥离 ``--role <value>`` 后再传给 main 流程。
    """
    role = "all"
    consumed = []
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--role":
            if i + 1 >= len(argv):
                print("  [FAIL] --role 需要一个参数（local/client/all）")
                sys.exit(1)
            role = argv[i + 1]
            consumed.extend([i, i + 1])
            i += 2
            continue
        if arg.startswith("--role="):
            role = arg.split("=", 1)[1]
            consumed.append(i)
            i += 1
            continue
        i += 1

    if role not in ("local", "client", "all"):
        print(f"  [FAIL] 未知 role: {role}（支持: local/client/all）")
        sys.exit(1)
    return role, consumed


def _resolve_pyz_toc(spec_work_dir, candidates):
    """在 PyInstaller 工作目录下查找 PYZ TOC 文件。

    PyInstaller 6 多 COLLECT spec 把不同 Analysis 的 PYZ 写在 build/<specname>/ 下，
    编号 PYZ-00 / PYZ-01 / ...。Windows/macOS 只构建 local bundle（无 PYZ-01）。
    """
    for name in candidates:
        path = spec_work_dir / name
        if path.is_file():
            return path
    return None


def build_pyinstaller_bundle(role="all"):
    """运行 PyInstaller spec 构建角色产物。

    P1-G（2026-07-25）：spec 已删除 Python tree-sitter 和 16 种 grammar 的
    hidden imports，并通过 ``_PARSER_GRAMMAR_EXCLUDES`` 排除 ``callwarden.parsers.*``
    和 ``tree_sitter*``。本函数只负责调用 PyInstaller，spec 内部完成 fail closed
    排除；fail closed 检查由 ``inspect_bundle_artifacts`` 调用 bundle inspector 完成。

    Args:
        role: ``local`` / ``client`` / ``all``（默认 ``all``，构建当前平台支持的全部 bundle）。
            ``client`` bundle 仅在 Linux 上由 spec 生成（Windows/macOS 不构建）。
    """
    print("Step P1-G: Building PyInstaller role artifacts")
    print(f"  Role: {role}")
    print(f"  Platform: {sys.platform}")

    spec_path = RELEASE_DIR / "pyinstaller" / "callwarden.spec"
    if not spec_path.is_file():
        print(f"  [FAIL] spec 不存在: {spec_path}")
        sys.exit(1)

    # P1-G 前置：Rust 扩展必须在根目录（spec 通过 CW_RUST_EXT_PATH 或根目录加载）
    _ensure_rust_ext_at_root()
    _verify_rust_extension_present()

    # PyInstaller 工作目录：build/<specname>/（specname = callwarden）
    spec_work_dir = ROOT / "build" / "callwarden"

    # 调用 PyInstaller，spec 内部决定构建哪些 bundle（Linux 多 COLLECT，其他平台仅 local）
    run([
        sys.executable, "-m", "PyInstaller",
        str(spec_path),
        "--noconfirm",
        "--clean",
        "--workpath", str(spec_work_dir),
        "--distpath", str(ROOT / "dist"),
    ])

    # 列出实际生成的 bundle，供 inspect 阶段使用
    produced_bundles = []
    for role_name, info in _ROLE_BUNDLE_MAP.items():
        if role not in ("all", role_name):
            continue
        if info["linux_only"] and not sys.platform.startswith("linux"):
            print(f"  [SKIP] role={role_name} 仅在 Linux 上构建，当前平台跳过")
            continue
        bundle_path = ROOT / "dist" / info["bundle_dir"]
        if not bundle_path.is_dir():
            print(f"  [WARN] role={role_name} 产物未生成: {bundle_path}")
            continue
        pyz_toc = _resolve_pyz_toc(spec_work_dir, info["pyz_toc_candidates"])
        if pyz_toc is None:
            print(f"  [FAIL] role={role_name} PYZ TOC 未找到于 {spec_work_dir}")
            sys.exit(1)
        produced_bundles.append((role_name, bundle_path, pyz_toc))
        print(f"  [OK] role={role_name} bundle={bundle_path} pyz_toc={pyz_toc}")

    if not produced_bundles:
        print("  [FAIL] 未生成任何 bundle（检查 spec 和平台支持）")
        sys.exit(1)

    print()
    return produced_bundles


def inspect_bundle_artifacts(produced_bundles, max_unpacked_mb=None):
    """对 PyInstaller 产物运行 bundle inspector（fail closed）。

    P1-G 设计 §8 Phase 5 步骤 6 要求：所有 role 默认禁止 PARSER_DISTRIBUTIONS，
    文件级检查 ``_binding*.pyd/.so`` 和 ``callwarden/parsers/*_parser.py``，
    以及 Rust ``callwarden_core`` 必须存在。

    Args:
        produced_bundles: ``build_pyinstaller_bundle`` 返回的 (role, bundle_path, pyz_toc) 列表。
        max_unpacked_mb: 可选，解压体积上限（MiB）。

    Returns:
        (reports, errors) 元组：reports 为每个 bundle 的 inspector 报告 dict，
        errors 为所有 fail closed 错误列表（非空时调用方应 sys.exit(1)）。
    """
    print("Step P1-G: Inspecting bundles (fail closed)")
    import json as _json

    inspector = RELEASE_DIR / "inspect_pyinstaller_bundle.py"
    if not inspector.is_file():
        print(f"  [FAIL] bundle inspector 不存在: {inspector}")
        sys.exit(1)

    reports = []
    all_errors = []
    for role_name, bundle_path, pyz_toc in produced_bundles:
        report_path = RELEASE_DIR / f"bundle-report-{role_name}.json"
        cmd = [
            sys.executable, str(inspector),
            "--bundle", str(bundle_path),
            "--pyz-toc", str(pyz_toc),
            "--report", str(report_path),
            "--role", role_name,
        ]
        if max_unpacked_mb is not None:
            cmd.extend(["--max-unpacked-mb", str(max_unpacked_mb)])
        # 同平台 bundle 额外验证 Rust parse API（CI/本地构建场景）
        cmd.append("--verify-rust-parse")

        result = subprocess.run(cmd, capture_output=True, text=True)
        if result.returncode != 0:
            print(f"  [FAIL] role={role_name} inspector 退出 {result.returncode}")
            if result.stderr:
                for line in result.stderr.splitlines():
                    print(f"    {line}")
            all_errors.append(f"role={role_name}: inspector 退出 {result.returncode}")
            continue

        # 读取报告
        try:
            report = _json.loads(report_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001 - 报告解析失败需捕获
            print(f"  [FAIL] role={role_name} 报告解析失败: {exc}")
            all_errors.append(f"role={role_name}: 报告解析失败 {exc}")
            continue

        # 报告内部 errors 字段（inspector 可能 exit 0 但报告里仍有 errors）
        report_errors = report.get("errors", []) or []
        if report_errors:
            for err in report_errors:
                all_errors.append(f"role={role_name}: {err}")
            print(f"  [FAIL] role={role_name} 报告包含 {len(report_errors)} 个错误")
        else:
            print(
                f"  [OK] role={role_name} unpacked={report.get('unpacked_mb', 0)} MB "
                f"modules={report.get('module_count', 0)} files={report.get('file_count', 0)}"
            )
        reports.append(report)

    print()
    return reports, all_errors


def main():
    """主入口。"""
    print("=" * 60)
    print("Call Warden Unified Build Pipeline")
    print("=" * 60)
    print()

    if "--check" in sys.argv:
        check_versions()
        return

    check_versions()

    if "--rust" in sys.argv:
        build_rust_extension()
        return

    if "--wheel" in sys.argv:
        build_python_wheel()
        return

    # P1-G: PyInstaller 角色产物构建
    if "--pyinstaller" in sys.argv:
        role, consumed = _parse_role_arg(sys.argv)
        # 剥离 --role 参数后剩余 argv 仅供日志，不影响 PyInstaller 流程
        build_pyinstaller_bundle(role=role)
        # inspect 阶段单独运行，便于调用方只构建不 inspect
        if "--no-inspect" in sys.argv:
            return
        # 重新构建产物列表（build_pyinstaller_bundle 已返回，这里复用）
        # 通过扫描 dist/ 重新发现，避免 main 与 build 阶段的状态耦合
        produced = _discover_produced_bundles(role)
        reports, errors = inspect_bundle_artifacts(produced)
        # 同步生成 manifest，包含 parser ABI 和 bundle 报告
        bundle_paths = [p for (_, p, _) in produced]
        generate_manifest(bundles=bundle_paths, bundle_reports=reports)
        if errors:
            print("=" * 60)
            print(f"[FAIL] Bundle inspector found {len(errors)} errors")
            for err in errors:
                print(f"  - {err}")
            print("=" * 60)
            sys.exit(1)
        print("=" * 60)
        print("[PASS] PyInstaller bundles built and inspected")
        print("=" * 60)
        return

    # 完整构建
    build_rust_extension()
    build_python_wheel()
    build_wheelhouse()
    generate_manifest()

    print("=" * 60)
    print("[PASS] Build complete")
    print("=" * 60)


def _discover_produced_bundles(role):
    """扫描 dist/ 目录发现已构建的 bundle，用于 inspect 阶段复用。

    与 ``build_pyinstaller_bundle`` 内部的发现逻辑保持一致，但只读不构建。
    返回 (role_name, bundle_path, pyz_toc) 列表；缺失则 fail-fast 退出。
    """
    spec_work_dir = ROOT / "build" / "callwarden"
    produced = []
    for role_name, info in _ROLE_BUNDLE_MAP.items():
        if role not in ("all", role_name):
            continue
        if info["linux_only"] and not sys.platform.startswith("linux"):
            continue
        bundle_path = ROOT / "dist" / info["bundle_dir"]
        if not bundle_path.is_dir():
            continue
        pyz_toc = _resolve_pyz_toc(spec_work_dir, info["pyz_toc_candidates"])
        if pyz_toc is None:
            print(f"  [FAIL] role={role_name} PYZ TOC 未找到于 {spec_work_dir}")
            sys.exit(1)
        produced.append((role_name, bundle_path, pyz_toc))
    return produced


if __name__ == "__main__":
    main()
