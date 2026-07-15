#!/bin/bash
# Call Warden 离线安装脚本
# Task: T-1783983162956-b2a8
# 设计 §8.1: 离线场景提供包含包、repo metadata、SBOM、manifest 和安装脚本的 tar.zst
#
# 用法: ./install-offline.sh [client|local|agent|daemon|enterprise]
#   不带参数则安装 enterprise（含全部依赖）
#
# 目录布局（由 build_packages.sh 打包为 tar.zst）:
#   callwarden-offline_<ver>_<arch>/
#   ├── packages/        .deb 文件
#   ├── repo/            Packages 索引
#   ├── sbom/            sbom.spdx.json
#   ├── scripts/         install-offline.sh（本文件）
#   └── manifest.json

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
BUNDLE_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PKG_DIR="$BUNDLE_DIR/packages"

TARGET="${1:-enterprise}"

echo "=== Call Warden offline installer ==="
echo "Bundle: $BUNDLE_DIR"
echo "Target: $TARGET"

# 校验 manifest 存在
if [ ! -f "$BUNDLE_DIR/manifest.json" ]; then
    echo "WARNING: manifest.json not found" >&2
fi

# 校验目标包名合法
case "$TARGET" in
    client|local|agent|daemon|enterprise) ;;
    *)
        echo "ERROR: unknown target '$TARGET' (use: client|local|agent|daemon|enterprise)" >&2
        exit 1
        ;;
esac

# 注册本地 repo（若 Packages 索引存在），便于 apt 解析依赖
if [ -f "$BUNDLE_DIR/repo/Packages" ]; then
    echo "deb [trusted=yes] file://$PKG_DIR ./" > /etc/apt/sources.list.d/callwarden-offline.list 2>/dev/null || true
    apt-get update -o Dir::Etc::sourcelist="/etc/apt/sources.list.d/callwarden-offline.list" \
                   -o Dir::Etc::sourceparts="-" 2>/dev/null || true
fi

# 查找目标 .deb
shopt -s nullglob
FILES=( "$PKG_DIR/callwarden-${TARGET}"_*.deb )
if [ ${#FILES[@]} -eq 0 ]; then
    echo "ERROR: no package matching callwarden-${TARGET}_*.deb in $PKG_DIR" >&2
    exit 1
fi

# 依次安装，依赖缺失时用 apt-get -f 修复
for f in "${FILES[@]}"; do
    echo "Installing $f"
    dpkg -i "$f" || apt-get install -f -y
done

echo "=== Installation complete ==="
echo "Verify: cw --version"
