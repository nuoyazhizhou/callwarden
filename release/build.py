"""统一构建管道：setuptools + maturin 编排。

确保 Python wheel 安装时自带平台 Rust 扩展。
构建顺序：Rust .pyd/.so → Python wheel (包含 Rust 扩展) → wheelhouse 锁定依赖。

用法：
    python release/build.py              # 完整构建
    python release/build.py --check      # 只验证版本一致性
    python release/build.py --wheel      # 只构建 Python wheel
    python release/build.py --rust       # 只构建 Rust 扩展
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

    # cargo build --release
    run(["cargo", "build", "--release"], cwd=rust_ext_dir)

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


def generate_manifest():
    """生成 artifact manifest。"""
    print("Step 5: Generating artifact manifest")
    import hashlib
    import json
    import time

    try:
        import tomllib
    except ImportError:
        import tomli as tomllib

    with open(RELEASE_DIR / "version.toml", "rb") as f:
        version_data = tomllib.load(f)

    manifest = {
        "product": version_data["product"]["name"],
        "version": version_data["product"]["version"],
        "build_time": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "abi": version_data["abi"],
        "platforms": version_data["platforms"],
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

    manifest_path = RELEASE_DIR / "artifact-manifest.json"
    with open(manifest_path, "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2, ensure_ascii=False)
    print(f"  [OK] Manifest: {manifest_path}")
    print(f"  Artifacts: {len(manifest['artifacts'])}")
    print()


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

    # 完整构建
    build_rust_extension()
    build_python_wheel()
    build_wheelhouse()
    generate_manifest()

    print("=" * 60)
    print("[PASS] Build complete")
    print("=" * 60)


if __name__ == "__main__":
    main()
