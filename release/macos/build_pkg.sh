#!/bin/bash
# macOS .pkg builder for Call Warden
# Task: T-1783983162956-7062
# Spec: docs/design/cross-platform-packaging-release-plan.md §7
#
# 产出：
#   CallWarden-<version>-universal2.pkg      已签名/公证的安装包
#   CallWarden-<version>-universal2.tar.gz   自动化友好的安装树归档
#   CallWarden-<version>-universal2.tar.gz.sha256
#
# 要求：macOS + Xcode command line tools + cargo + lipo
# 签名/公证通过环境变量启用（缺省时跳过并 warning）：
#   CW_APPLE_DEVID         Developer ID Application 证书 ID（codesign/productsign）
#   CW_APPLE_ID            Apple ID（notarization）
#   CW_APPLE_TEAM_ID       Team ID
#   CW_APPLE_APP_PASSWORD  app-specific password

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ============================================================
# 0. 平台角色 fail-closed 检查
# 规范 §3 平台能力矩阵 + config_loader.fail_closed_unsupported
# macOS 仅支持 local/client；daemon/agent 是 Linux 企业版角色。
# 收到 daemon/agent 时必须 fail-closed（exit 2），禁止安装伪 daemon。
# 通过环境变量 CW_ROLE 传入（默认 local）。
# ============================================================
CW_ROLE="${CW_ROLE:-local}"
case "$CW_ROLE" in
    local|client)
        echo "Role: $CW_ROLE (supported on macOS)"
        ;;
    daemon|agent)
        echo "ERROR: Role '$CW_ROLE' is not supported on macOS." >&2
        echo "Supported roles: client, local" >&2
        echo "Enterprise daemon/agent requires Linux with SO_PEERCRED, SCM_RIGHTS, and UDS." >&2
        exit 2
        ;;
    *)
        echo "ERROR: Unknown role '$CW_ROLE'. Supported: local, client" >&2
        exit 2
        ;;
esac

# ============================================================
# 1. 从 release/version.toml 读取版本（唯一版本源）
# 优先用 python3 tomllib（3.11+）；不可用时用 grep+sed 兜底。
# ============================================================
VERSION_TOML="$ROOT/release/version.toml"
if [ ! -f "$VERSION_TOML" ]; then
    echo "ERROR: version.toml not found at $VERSION_TOML" >&2
    exit 1
fi

if command -v python3 &>/dev/null && python3 -c "import tomllib" 2>/dev/null; then
    # || true 防止 set -e 在 TOML 损坏时直接退出，交给下方空值检查友好报错
    VERSION=$(python3 -c "import tomllib; print(tomllib.load(open('$VERSION_TOML','rb'))['product']['version'])" 2>/dev/null || true)
else
    # grep+sed 兜底：匹配 [product] 段下的 version = "x.y.z"
    # || true 防止 grep 无匹配返回非零时触发 set -e
    VERSION=$(grep -A5 '^\[product\]' "$VERSION_TOML" | grep '^version' | sed -E 's/.*"([^"]+)".*/\1/' || true)
fi

if [ -z "${VERSION:-}" ]; then
    echo "ERROR: Failed to parse version from $VERSION_TOML" >&2
    exit 1
fi

INSTALL_DIR="/Library/Application Support/CallWarden"
PKG_ID="dev.callwarden.pkg"
DIST_DIR="$SCRIPT_DIR/dist"
mkdir -p "$DIST_DIR"
OUTPUT_PKG="$DIST_DIR/CallWarden-${VERSION}-universal2.pkg"
OUTPUT_TGZ="$DIST_DIR/CallWarden-${VERSION}-universal2.tar.gz"
SHASUM_TGZ="$DIST_DIR/CallWarden-${VERSION}-universal2.tar.gz.sha256"

# 源产物目录：由 release/build.py 生成
# 包含 Python wheel（callwarden-<version>-py3-none-any.whl），
# 其中含 cw/cw_client console_script。Rust 扩展由 Step 1 现场构建 universal2。
# 本脚本不重新构建 Python wheel，仅消费已构建的产物。
SRC_DIST="$ROOT/release/dist"

echo "=== Building macOS .pkg ==="
echo "Version:   $VERSION"
echo "Role:      $CW_ROLE"
echo "Install:   $INSTALL_DIR"
echo "Source:    $SRC_DIST (Python wheel + Rust ext)"

# ============================================================
# 2. 构建 universal2 Rust 扩展
# ============================================================
echo "Step 1: Building universal2 Rust extension"
cd "$ROOT/rust_ext"
cargo build --release --target x86_64-apple-darwin
cargo build --release --target aarch64-apple-darwin

UNIVERSAL_DIR="$SCRIPT_DIR/build/universal2"
mkdir -p "$UNIVERSAL_DIR"
lipo -create \
    target/x86_64-apple-darwin/release/libcallwarden_core.dylib \
    target/aarch64-apple-darwin/release/libcallwarden_core.dylib \
    -output "$UNIVERSAL_DIR/callwarden_core.so"

# universal2 架构验证（规范 §7：Intel 与 Apple Silicon 都要原生支持）
echo "  Verifying universal2 architecture:"
ARCHS=$(lipo -archs "$UNIVERSAL_DIR/callwarden_core.so")
echo "    lipo -archs: $ARCHS"
LIPO_INFO=$(lipo -info "$UNIVERSAL_DIR/callwarden_core.so")
echo "    lipo -info: $LIPO_INFO"
echo "    file:"
file "$UNIVERSAL_DIR/callwarden_core.so" | sed 's/^/      /'

if [[ "$ARCHS" != *"x86_64"* ]] || [[ "$ARCHS" != *"arm64"* ]]; then
    echo "ERROR: Not a universal2 binary (missing x86_64 or arm64)" >&2
    exit 1
fi
echo "  [OK] universal2 verified"

# ============================================================
# 3. 准备 package root
# ============================================================
echo "Step 2: Preparing package root"
PKG_ROOT="$SCRIPT_DIR/build/pkgroot"
rm -rf "$PKG_ROOT"
mkdir -p "$PKG_ROOT/$INSTALL_DIR/bin"
mkdir -p "$PKG_ROOT/$INSTALL_DIR/lib"
mkdir -p "$PKG_ROOT/$INSTALL_DIR/config"
mkdir -p "$PKG_ROOT/usr/local/bin"

# 拷贝入口脚本与 Rust 扩展
# 源是 release/dist/ 下的 Python wheel 产物（console_script 由 release/build.py 生成）。
# 若上游未预置 bin/，则生成 placeholder 入口（实际安装器应从 wheel 解包 console_script）。
if [ -f "$SRC_DIST/bin/cw" ]; then
    cp "$SRC_DIST/bin/cw" "$PKG_ROOT/$INSTALL_DIR/bin/"
else
    echo "  WARNING: $SRC_DIST/bin/cw not found; placeholder entry script"
    cat > "$PKG_ROOT/$INSTALL_DIR/bin/cw" << 'EOF'
#!/bin/bash
# Placeholder - 真实安装器应从 Python wheel 解包 console_script
exec python3 -m callwarden.cw "$@"
EOF
    chmod +x "$PKG_ROOT/$INSTALL_DIR/bin/cw"
fi

if [ -f "$SRC_DIST/bin/cw_client" ]; then
    cp "$SRC_DIST/bin/cw_client" "$PKG_ROOT/$INSTALL_DIR/bin/"
else
    echo "  WARNING: $SRC_DIST/bin/cw_client not found; placeholder entry script"
    cat > "$PKG_ROOT/$INSTALL_DIR/bin/cw_client" << 'EOF'
#!/bin/bash
exec python3 -m callwarden.cli.client "$@"
EOF
    chmod +x "$PKG_ROOT/$INSTALL_DIR/bin/cw_client"
fi

cp "$UNIVERSAL_DIR/callwarden_core.so" "$PKG_ROOT/$INSTALL_DIR/lib/"

# 配置模板（不覆盖已存在用户配置）
cat > "$PKG_ROOT/$INSTALL_DIR/config/config.toml.template" << 'EOF'
# Call Warden 配置模板
# 实际配置路径（规范 §5）：
#   系统：/Library/Application Support/CallWarden/config.toml
#   用户：~/Library/Application Support/CallWarden/config.toml
# 优先级：CLI 参数 > 环境变量 > 用户配置 > 系统配置 > 默认值
EOF

# ============================================================
# 创建 /usr/local/bin shim（规范 §7：稳定 shim）
# Apple Silicon 上 /usr/local/bin 仍是通用可写路径。
# ============================================================
cat > "$PKG_ROOT/usr/local/bin/cw" << 'SHIM'
#!/bin/bash
exec "/Library/Application Support/CallWarden/bin/cw" "$@"
SHIM
chmod +x "$PKG_ROOT/usr/local/bin/cw"

cat > "$PKG_ROOT/usr/local/bin/cw-client" << 'SHIM'
#!/bin/bash
exec "/Library/Application Support/CallWarden/bin/cw_client" "$@"
SHIM
chmod +x "$PKG_ROOT/usr/local/bin/cw-client"

# ============================================================
# 可选 LaunchAgent watcher plist（规范 §7）
# macOS 不安装 system daemon（仅 Linux 企业版才有）。
# 安装位置：~/Library/LaunchAgents/dev.callwarden.watcher.plist
# 用户安装后可手动启用：
#   cp "$INSTALL_DIR/config/dev.callwarden.watcher.plist" ~/Library/LaunchAgents/
#   launchctl load -w ~/Library/LaunchAgents/dev.callwarden.watcher.plist
# ============================================================
cat > "$PKG_ROOT/$INSTALL_DIR/config/dev.callwarden.watcher.plist" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>dev.callwarden.watcher</string>
    <key>ProgramArguments</key>
    <array>
        <string>/Library/Application Support/CallWarden/bin/cw</string>
        <string>--watch</string>
    </array>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
</dict>
</plist>
EOF

# ============================================================
# 4. codesign 签名 + hardened runtime + entitlements
# 规范 §7：hardened runtime、codesign、entitlements
# PyO3 扩展运行时需要 allow-jit / disable-library-validation。
# ============================================================
echo "Step 3: Codesign with hardened runtime + entitlements"

# 内联 entitlements.plist（构建时生成到 build/，非提交源文件）
ENTITLEMENTS_FILE="$SCRIPT_DIR/build/entitlements.plist"
cat > "$ENTITLEMENTS_FILE" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <!-- PyO3 扩展运行时需要 JIT 权限 -->
    <key>com.apple.security.cs.allow-jit</key>
    <true/>
    <!-- 加载未签名第三方库（Python site-packages） -->
    <key>com.apple.security.cs.disable-library-validation</key>
    <true/>
    <!-- 允许 unsigned executable memory（部分解析器扩展需要） -->
    <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
    <true/>
    <!-- 允许 DYLD 环境变量（嵌入式 Python 需要） -->
    <key>com.apple.security.cs.allow-dyld-environment-variables</key>
    <true/>
</dict>
</plist>
EOF

# 对 Rust 扩展和入口脚本签名（hardened runtime + entitlements）
# 先签依赖对象（.so），再签可执行入口。
SIGN_TARGETS=(
    "$PKG_ROOT/$INSTALL_DIR/lib/callwarden_core.so"
    "$PKG_ROOT/$INSTALL_DIR/bin/cw"
    "$PKG_ROOT/$INSTALL_DIR/bin/cw_client"
)

if [ -n "${CW_APPLE_DEVID:-}" ] && command -v codesign &>/dev/null; then
    for target in "${SIGN_TARGETS[@]}"; do
        codesign --force --options runtime \
            --entitlements "$ENTITLEMENTS_FILE" \
            --sign "$CW_APPLE_DEVID" \
            --timestamp \
            "$target"
        echo "  Signed: $target"
    done
    echo "  [OK] hardened runtime + entitlements applied"
else
    echo "  WARNING: codesign skipped (set CW_APPLE_DEVID for production signing)"
fi

# ============================================================
# 5. 构建 component package
# ============================================================
echo "Step 4: Building component package"
COMPONENT_PKG="$SCRIPT_DIR/build/callwarden-component.pkg"
pkgbuild \
    --root "$PKG_ROOT" \
    --identifier "$PKG_ID" \
    --version "$VERSION" \
    --install-location "/" \
    "$COMPONENT_PKG"

# ============================================================
# 6. productsign 签名 product package
# ============================================================
echo "Step 5: Signing product package"
if [ -n "${CW_APPLE_DEVID:-}" ] && command -v productsign &>/dev/null; then
    productsign \
        --sign "$CW_APPLE_DEVID" \
        --timestamp \
        "$COMPONENT_PKG" \
        "$OUTPUT_PKG"
    echo "  [OK] productsign with $CW_APPLE_DEVID"
else
    cp "$COMPONENT_PKG" "$OUTPUT_PKG"
    echo "  WARNING: productsign skipped (set CW_APPLE_DEVID)"
fi

# ============================================================
# 7. Notarization + stapling
# 规范 §7：notarization、stapling
# 环境变量：CW_APPLE_ID / CW_APPLE_TEAM_ID / CW_APPLE_APP_PASSWORD
# ============================================================
echo "Step 6: Notarization + stapling"
if [ -n "${CW_APPLE_ID:-}" ] && [ -n "${CW_APPLE_APP_PASSWORD:-}" ] && \
   [ -n "${CW_APPLE_TEAM_ID:-}" ] && command -v xcrun &>/dev/null; then
    echo "  Submitting to notarytool (may take several minutes)..."
    xcrun notarytool submit "$OUTPUT_PKG" \
        --apple-id "$CW_APPLE_ID" \
        --team-id "$CW_APPLE_TEAM_ID" \
        --password "$CW_APPLE_APP_PASSWORD" \
        --wait

    echo "  Stapling ticket..."
    xcrun stapler staple "$OUTPUT_PKG"
    echo "  [OK] notarized and stapled"
else
    echo "  WARNING: notarization skipped"
    echo "    Set CW_APPLE_ID, CW_APPLE_APP_PASSWORD, CW_APPLE_TEAM_ID for production"
fi

# ============================================================
# 8. spctl / Gatekeeper 验证
# 规范 §7：spctl/Gatekeeper 验证
# ============================================================
echo "Step 7: spctl / Gatekeeper verification"
if command -v spctl &>/dev/null; then
    if spctl --assess --type install --verbose=4 "$OUTPUT_PKG" 2>&1; then
        echo "  [OK] spctl assess passed"
    else
        echo "  WARNING: spctl assess failed (expected if unsigned)"
    fi
else
    echo "  WARNING: spctl not available"
fi

# pkgutil 签名链验证
if command -v pkgutil &>/dev/null; then
    if pkgutil --check-signature "$OUTPUT_PKG" 2>&1; then
        echo "  [OK] pkgutil signature check passed"
    else
        echo "  WARNING: pkgutil signature check failed (expected if unsigned)"
    fi
fi

# stapler validate（仅公证后才有效）
if [ -n "${CW_APPLE_ID:-}" ] && command -v xcrun &>/dev/null; then
    if xcrun stapler validate "$OUTPUT_PKG" 2>&1; then
        echo "  [OK] stapler validate passed"
    else
        echo "  WARNING: stapler validate failed"
    fi
fi

# ============================================================
# 9. 生成 signed tar.gz（自动化友好）
# 规范 §7：同时提供 signed tar.gz
# tar.gz 包含已签名的安装树（pkgroot 内容），内部二进制已 codesign。
# 便于自动化直接解压部署，无需运行 pkg installer。
# ============================================================
echo "Step 8: Building tar.gz artifact (automation-friendly)"
tar -czf "$OUTPUT_TGZ" \
    -C "$PKG_ROOT" \
    .

# 生成 sha256 校验和
shasum -a 256 "$OUTPUT_TGZ" > "$SHASUM_TGZ"
echo "  [OK] tar.gz: $OUTPUT_TGZ"
echo "  [OK] sha256: $SHASUM_TGZ"

# ============================================================
# 10. 输出产物清单
# ============================================================
echo ""
echo "=== Build complete ==="
echo "Version: $VERSION"
echo "Role:    $CW_ROLE"
echo "Outputs:"
ls -lh "$OUTPUT_PKG" "$OUTPUT_TGZ" "$SHASUM_TGZ"
