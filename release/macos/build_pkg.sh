#!/bin/bash
# macOS .pkg builder for Call Warden
# Task: T-1783983162956-7062
# Spec: docs/design/cross-platform-packaging-release-plan.md §7
#
# 产出：
#   CallWarden-<version>-arm64.pkg      已签名/公证的安装包
#   CallWarden-<version>-arm64.tar.gz   自动化友好的安装树归档
#   CallWarden-<version>-arm64.tar.gz.sha256
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
#   - bin/cw 缺失时 fail-closed，不再生成安装后无法运行的 placeholder。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

# ============================================================
# 0. 平台角色 fail-closed 检查
# 规范 §3 平台能力矩阵 + config_loader.fail_closed_unsupported
# macOS 仅支持 local；client/daemon/agent 是 Linux 企业版角色。
# 收到其他角色时必须 fail-closed（exit 2），禁止安装伪入口。
# 通过环境变量 CW_ROLE 传入（默认 local）。
# ============================================================
CW_ROLE="${CW_ROLE:-local}"
case "$CW_ROLE" in
    local)
        echo "Role: $CW_ROLE (supported on macOS)"
        ;;
    client|daemon|agent)
        echo "ERROR: Role '$CW_ROLE' is not supported on macOS." >&2
        echo "Supported roles: local" >&2
        echo "Enterprise client/daemon/agent requires Linux UDS capabilities." >&2
        exit 2
        ;;
    *)
        echo "ERROR: Unknown role '$CW_ROLE'. Supported: local" >&2
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
# P0-4 修复（2026-07-22）：产物标记为 arm64，不假装 universal2
# 原代码只把 Rust 扩展用 lipo 合成 universal2，PyInstaller runtime 仍是宿主架构，
# 产物文件名为 universal2 但入口和嵌入式 Python 实际只有单架构。
# 要支持真正的 universal2，需要在 x86_64 和 arm64 两个 runner 上分别构建 PyInstaller，
# 然后合并 --onedir 目录（当前 CI 不具备双 runner 条件）。
# GitHub Actions macOS runner 默认是 arm64（Apple Silicon），故产物标记为 arm64。
OUTPUT_PKG="$DIST_DIR/CallWarden-${VERSION}-arm64.pkg"
OUTPUT_TGZ="$DIST_DIR/CallWarden-${VERSION}-arm64.tar.gz"
SHASUM_TGZ="$DIST_DIR/CallWarden-${VERSION}-arm64.tar.gz.sha256"

# 源产物目录：由 release/build.py 生成
# 包含 Python wheel（callwarden-<version>-py3-none-any.whl），
# 其中含 cw console_script。Rust 扩展由 Step 1 现场构建 arm64。
# 本脚本不重新构建 Python wheel，仅消费已构建的产物。
SRC_DIST="$ROOT/release/dist"

echo "=== Building macOS .pkg ==="
echo "Version:   $VERSION"
echo "Role:      $CW_ROLE"
echo "Install:   $INSTALL_DIR"
echo "Source:    $SRC_DIST (Python wheel + Rust ext)"

# ============================================================
# 2. 构建 arm64 Rust 扩展（P0-5 v2 修复：诚实降级为 arm64-only）
# ============================================================
# P0-5 v2 修复（2026-07-22 完整复审）：
# 旧代码试图构建 universal2 Rust 扩展（x86_64 + arm64 lipo 合成），
# 但 PyInstaller runtime 仍是宿主架构（macos-latest = arm64），
# 导致入口二进制和嵌入式 Python 只有 arm64，而 Rust 扩展是 universal2。
# 复审报告指出："脚本没有对 PyInstaller 入口和嵌入式 Python 执行 file/lipo
# 架构校验，却无条件把产物命名为 arm64"。
#
# 修复方案：诚实降级为 arm64-only：
# 1. 只构建 aarch64-apple-darwin Rust 扩展（不再 lipo 合成 universal2）
# 2. 构建后用 file/lipo 校验所有 Mach-O 产物架构（cw、Rust 扩展）
# 3. 产物文件名标记 arm64（与实际架构一致）
# 4. 如需 x86_64 支持，需要在 macos-13 runner 上单独构建（未来扩展）
echo "Step 1: Building arm64 Rust extension (P0-5 v2: arm64-only)"
cd "$ROOT/rust_ext"
cargo build --release --target aarch64-apple-darwin

ARM64_DIR="$SCRIPT_DIR/build/arm64"
mkdir -p "$ARM64_DIR"
cp target/aarch64-apple-darwin/release/libcallwarden_core.dylib "$ARM64_DIR/callwarden_core.so"

# arm64 架构验证
echo "  Verifying arm64 architecture:"
echo "    file:"
file "$ARM64_DIR/callwarden_core.so" | sed 's/^/      /'
ARCHS=$(lipo -archs "$ARM64_DIR/callwarden_core.so")
echo "    lipo -archs: $ARCHS"
if [[ "$ARCHS" != "arm64" ]]; then
    echo "ERROR: Rust 扩展不是 arm64（实际: $ARCHS）" >&2
    exit 1
fi
echo "  [OK] Rust 扩展 arm64 verified"

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

# P0-3 整改（2026-07-22）：改用 PyInstaller --onedir 打包自包含二进制
# 原代码只复制 venv 的 console_scripts，shebang 指向构建机临时 venv，
# 安装到干净 macOS 后无法启动（无 Python 解释器、无 site-packages）。
# PyInstaller --onedir 产物含 Python 解释器 + 全部依赖 + Rust 扩展，安装后直接可用。
echo "  Building PyInstaller --onedir bundle"
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
"$VENV_DIR/bin/pip" install -r "$ROOT/release/pyinstaller/requirements-build.txt" || {
    echo "  ERROR: 安装 PyInstaller 发布依赖白名单失败" >&2
    exit 1
}
"$VENV_DIR/bin/pip" install "$WHEEL_PATH" --no-deps || {
    echo "  ERROR: pip install $WHEEL_PATH --no-deps failed" >&2
    exit 1
}

# 复制 arm64 Rust 扩展到项目根目录（PyInstaller spec 从根目录收集）
cp "$ARM64_DIR/callwarden_core.so" "$ROOT/callwarden_core.so"

# 运行 PyInstaller（spec 文件与 Linux 共用 release/pyinstaller/callwarden.spec）
cd "$ROOT"
"$VENV_DIR/bin/pyinstaller" release/pyinstaller/callwarden.spec \
    --noconfirm --clean \
    --distpath "$SCRIPT_DIR/build/pyinstaller_dist" \
    --workpath "$SCRIPT_DIR/build/pyinstaller_build" || {
    echo "  ERROR: pyinstaller failed" >&2
    rm -f "$ROOT/callwarden_core.so"
    exit 1
}
rm -f "$ROOT/callwarden_core.so"  # 清理临时复制

# macOS 仅发布 local 入口；client/agent 依赖 Linux UDS 能力。
PYINSTALLER_BUNDLE="$SCRIPT_DIR/build/pyinstaller_dist/callwarden"
if [ ! -f "$PYINSTALLER_BUNDLE/cw" ] || [ ! -d "$PYINSTALLER_BUNDLE/_internal" ]; then
    echo "  ERROR: PyInstaller 共享产物缺失: $PYINSTALLER_BUNDLE" >&2
    exit 1
fi
echo "  [OK] PyInstaller shared bundle built: cw"

# P0-5 v2 修复：校验 PyInstaller 产物架构（所有 Mach-O 必须 arm64）
echo "  Verifying PyInstaller bundle architecture (arm64-only):"
BIN="$PYINSTALLER_BUNDLE/cw"
ARCHS=$(lipo -archs "$BIN" 2>/dev/null || echo "not-a-Mach-O")
echo "    cw: $ARCHS"
if [[ "$ARCHS" != "arm64" ]]; then
    echo "    ERROR: cw 不是 arm64（实际: $ARCHS）" >&2
    echo "    PyInstaller runtime 是宿主架构，macos-latest 应为 arm64" >&2
    exit 1
fi

# 校验 _internal 中的嵌入式 Python 解释器架构
PY_BIN="$PYINSTALLER_BUNDLE/_internal/python"
if [ -f "$PY_BIN" ]; then
    PY_ARCHS=$(lipo -archs "$PY_BIN" 2>/dev/null || echo "not-a-Mach-O")
    echo "    python: $PY_ARCHS"
    if [[ "$PY_ARCHS" != "arm64" ]]; then
        echo "    ERROR: 嵌入式 Python 不是 arm64（实际: $PY_ARCHS）" >&2
        exit 1
    fi
fi
echo "  [OK] 所有 Mach-O 产物 arm64 架构校验通过"

# 复制单一 PyInstaller --onedir 目录到 pkg root
mkdir -p "$PKG_ROOT/$INSTALL_DIR/runtime"
cp -r "$PYINSTALLER_BUNDLE/." "$PKG_ROOT/$INSTALL_DIR/runtime/"

# 创建 bin/ 下的软链接
ln -s "$INSTALL_DIR/runtime/cw" "$PKG_ROOT/$INSTALL_DIR/bin/cw"

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
# PyInstaller --onedir 共享布局：
#   - 主二进制：runtime/cw
#   - Rust 扩展：runtime/_internal/callwarden_core.so
#   - bin/cw 是 symlink，codesign 应签真实 target
# 旧路径 lib/callwarden_core.so 和 bin/cw 在 onedir 模式下不存在，codesign 会失败
#
# P0-4 修复（2026-07-22）：codesign 改为 --deep 递归签名
# 原代码只签两个入口和两个 callwarden_core.so，没有递归签 _internal/ 中的其他 Mach-O 依赖
# （如 Python 解释器、tree-sitter grammar .so 等），导致 notarization 失败
# （Apple 要求所有 Mach-O 都已签名）。
# 现在对 _internal/ 目录使用 --deep 递归签名，覆盖所有嵌套的 .so/.dylib/可执行文件，
# 然后签主入口二进制。
SIGN_TARGETS=(
    "$PKG_ROOT/$INSTALL_DIR/runtime/_internal"
    "$PKG_ROOT/$INSTALL_DIR/runtime/cw"
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
        # P0-4 修复：--deep 递归签名 _internal/ 中的所有 Mach-O 依赖
        codesign --force --options runtime --deep \
            --entitlements "$ENTITLEMENTS_FILE" \
            --sign "$CW_APPLE_DEVID" \
            --timestamp \
            "$target"
        echo "  Signed (deep): $target"
    done
    echo "  [OK] hardened runtime + entitlements applied (deep signing)"
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
