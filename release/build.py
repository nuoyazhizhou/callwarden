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


def build_python_wheel():
    """构建 Python wheel。"""
    print("Step 3: Building Python wheel")
    run([sys.executable, "-m", "build", "--wheel", "--outdir", str(RELEASE_DIR / "dist")])
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
