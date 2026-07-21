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
#   CW_BUILD_UNSIGNED      设为 "true"/"1" 强制跳过签名/notarization（用于 dry_run）
#
# P0-3 修复（问题 8，2026-07-21）：
#   - workflow 原传 APPLE_*（APPLE_DEVELOPER_ID/APPLE_APP_SPECIFIC_PASSWORD/APPLE_TEAM_ID）
#     与脚本读取的 CW_APPLE_* 不匹配，导致签名/公证永远 skipped。
#     现已对齐 workflow 也用 CW_APPLE_* 前缀。
#   - 新增 CW_BUILD_UNSIGNED 支持（与 workflow 旧 env 兼容）。
#   - bin/cw / bin/cw-client 缺失时改为 fail-closed（原是 placeholder，安装后 cw --version
#     会因 Python/callwarden 包不在系统 PATH 而 ModuleNotFoundError）。
#     现在改为从 wheel 提取 console_scripts（pip install 到临时 venv）。

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
# 其中含 cw/cw-client console_script。Rust 扩展由 Step 1 现场构建 universal2。
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
# P0-3 修复（问题 8）：原代码在 $SRC_DIST/bin/cw 缺失时生成 placeholder 入口
# `exec python3 -m callwarden.cw "$@"`，但 macOS pkg 安装到
# /Library/Application Support/CallWarden/bin/cw，placeholder 假设 python3 + callwarden
# 包在系统 PATH 中——干净 macOS 上两个假设都不成立。
# 改为：从已下载的 wheel 中提取 console_scripts（pip install 到临时 venv 后复制）。
echo "  Extracting Python console_scripts from wheel"
WHEEL_GLOB="$SRC_DIST/callwarden-${VERSION}-*.whl"
WHEEL_PATH=""
for f in $WHEEL_GLOB; do
    if [ -f "$f" ]; then
        WHEEL_PATH="$f"
        break
    fi
done
if [ -z "$WHEEL_PATH" ]; then
    echo "  ERROR: Python wheel not found at $SRC_DIST/callwarden-${VERSION}-*.whl" >&2
    echo "  请先在 macOS runner 上构建 wheel（Gate 2），或从 GitHub Actions 下载 wheel artifact" >&2
    exit 1
fi
echo "  Using wheel: $(basename "$WHEEL_PATH")"

VENV_DIR="$SCRIPT_DIR/build/venv"
rm -rf "$VENV_DIR"
python3 -m venv "$VENV_DIR" >/dev/null 2>&1 || {
    echo "  ERROR: python3 -m venv failed" >&2
    exit 1
}
"$VENV_DIR/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
"$VENV_DIR/bin/pip" install --no-deps "$WHEEL_PATH" || {
    echo "  ERROR: pip install $WHEEL_PATH failed" >&2
    exit 1
}

# 验证 console_scripts 存在（fail-closed）
for script in cw cw-client; do
    if [ ! -f "$VENV_DIR/bin/$script" ]; then
        echo "  ERROR: console_script $script missing after pip install" >&2
        echo "  检查 pyproject.toml [project.scripts] 是否声明 $script" >&2
        exit 1
    fi
done
echo "  [OK] console_scripts extracted: cw, cw-client"

# 复制 console_scripts 到 pkg root
cp "$VENV_DIR/bin/cw" "$PKG_ROOT/$INSTALL_DIR/bin/"
cp "$VENV_DIR/bin/cw-client" "$PKG_ROOT/$INSTALL_DIR/bin/"

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
exec "/Library/Application Support/CallWarden/bin/cw-client" "$@"
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
    "$PKG_ROOT/$INSTALL_DIR/bin/cw-client"
)

# P0-3 修复（问题 8）：支持 CW_BUILD_UNSIGNED 显式跳过签名（与 workflow 对齐）
# workflow dry_run 时设 CW_BUILD_UNSIGNED=true，跳过签名/公证步骤。
# 原代码只在 CW_APPLE_DEVID 为空时跳过，但 workflow 的 dry_run 不会清空 secrets，
# 必须显式检查 CW_BUILD_UNSIGNED。
SKIP_CODESIGN=0
if [ "${CW_BUILD_UNSIGNED:-false}" = "true" ] || [ "${CW_BUILD_UNSIGNED:-}" = "1" ]; then
    SKIP_CODESIGN=1
    echo "  CW_BUILD_UNSIGNED=true, codesign 将被跳过"
fi

if [ "$SKIP_CODESIGN" = "0" ] && [ -n "${CW_APPLE_DEVID:-}" ] && command -v codesign &>/dev/null; then
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
    if [ "$SKIP_CODESIGN" = "1" ]; then
        echo "  WARNING: codesign skipped (CW_BUILD_UNSIGNED=true)"
    else
        echo "  WARNING: codesign skipped (set CW_APPLE_DEVID for production signing)"
    fi
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
if [ "$SKIP_CODESIGN" = "0" ] && [ -n "${CW_APPLE_DEVID:-}" ] && command -v productsign &>/dev/null; then
    productsign \
        --sign "$CW_APPLE_DEVID" \
        --timestamp \
        "$COMPONENT_PKG" \
        "$OUTPUT_PKG"
    echo "  [OK] productsign with $CW_APPLE_DEVID"
else
    cp "$COMPONENT_PKG" "$OUTPUT_PKG"
    if [ "$SKIP_CODESIGN" = "1" ]; then
        echo "  WARNING: productsign skipped (CW_BUILD_UNSIGNED=true)"
    else
        echo "  WARNING: productsign skipped (set CW_APPLE_DEVID)"
    fi
fi

# ============================================================
# 7. Notarization + stapling
# 规范 §7：notarization、stapling
# 环境变量：CW_APPLE_ID / CW_APPLE_TEAM_ID / CW_APPLE_APP_PASSWORD
# ============================================================
echo "Step 6: Notarization + stapling"
if [ "$SKIP_CODESIGN" = "0" ] && [ -n "${CW_APPLE_ID:-}" ] && [ -n "${CW_APPLE_APP_PASSWORD:-}" ] && \
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
    if [ "$SKIP_CODESIGN" = "1" ]; then
        echo "  WARNING: notarization skipped (CW_BUILD_UNSIGNED=true)"
    else
        echo "  WARNING: notarization skipped"
        echo "    Set CW_APPLE_ID, CW_APPLE_APP_PASSWORD, CW_APPLE_TEAM_ID for production"
    fi
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

# stapler validate（仅公证后才有效；CW_BUILD_UNSIGNED 跳过校验避免误报）
if [ "$SKIP_CODESIGN" = "0" ] && [ -n "${CW_APPLE_ID:-}" ] && command -v xcrun &>/dev/null; then
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
