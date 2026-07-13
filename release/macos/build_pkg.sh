#!/bin/bash
# macOS .pkg builder for Call Warden
# Task: T-1783983162956-7062
#
# Produces: CallWarden-<version>-universal2.pkg
# Requires: macOS with Xcode command line tools

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VERSION="0.3.0"
INSTALL_DIR="/Library/Application Support/CallWarden"
PKG_ID="dev.callwarden.pkg"
OUTPUT="$SCRIPT_DIR/dist/CallWarden-${VERSION}-universal2.pkg"

echo "=== Building macOS .pkg ==="
echo "Version: $VERSION"
echo "Install dir: $INSTALL_DIR"

# 1. Build universal2 Rust extension
echo "Step 1: Building universal2 Rust extension"
cd "$ROOT/rust_ext"
cargo build --release --target x86_64-apple-darwin
cargo build --release --target aarch64-apple-darwin

# Merge into universal2
UNIVERSAL_DIR="$SCRIPT_DIR/build/universal2"
mkdir -p "$UNIVERSAL_DIR"
lipo -create \
    target/x86_64-apple-darwin/release/libcallwarden_core.dylib \
    target/aarch64-apple-darwin/release/libcallwarden_core.dylib \
    -output "$UNIVERSAL_DIR/callwarden_core.so"

# Verify universal2
ARCHS=$(lipo -archs "$UNIVERSAL_DIR/callwarden_core.so")
echo "  Architectures: $ARCHS"
if [[ "$ARCHS" != *"x86_64"* ]] || [[ "$ARCHS" != *"arm64"* ]]; then
    echo "ERROR: Not a universal2 binary"
    exit 1
fi

# 2. Prepare package root
echo "Step 2: Preparing package root"
PKG_ROOT="$SCRIPT_DIR/build/pkgroot"
rm -rf "$PKG_ROOT"
mkdir -p "$PKG_ROOT/$INSTALL_DIR/bin"
mkdir -p "$PKG_ROOT/$INSTALL_DIR/lib"
mkdir -p "$PKG_ROOT/$INSTALL_DIR/config"
mkdir -p "$PKG_ROOT/usr/local/bin"

# Copy binaries
cp "$ROOT/dist/macos/cw" "$PKG_ROOT/$INSTALL_DIR/bin/"
cp "$ROOT/dist/macos/cw_client" "$PKG_ROOT/$INSTALL_DIR/bin/"
cp "$UNIVERSAL_DIR/callwarden_core.so" "$PKG_ROOT/$INSTALL_DIR/lib/"

# Create shims in /usr/local/bin
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

# 3. Build component package
echo "Step 3: Building component package"
COMPONENT_PKG="$SCRIPT_DIR/build/callwarden-component.pkg"
pkgbuild \
    --root "$PKG_ROOT" \
    --identifier "$PKG_ID" \
    --version "$VERSION" \
    --install-location "/" \
    "$COMPONENT_PKG"

# 4. Sign (requires Apple Developer ID)
echo "Step 4: Signing"
if command -v productsign &>/dev/null && [ -n "${APPLE_SIGNING_ID:-}" ]; then
    productsign \
        --sign "$APPLE_SIGNING_ID" \
        "$COMPONENT_PKG" \
        "$OUTPUT"
    echo "  Signed with: $APPLE_SIGNING_ID"
else
    cp "$COMPONENT_PKG" "$OUTPUT"
    echo "  WARNING: Unsigned (set APPLE_SIGNING_ID for production)"
fi

# 5. Notarize (requires Apple Developer ID + app-specific password)
echo "Step 5: Notarization"
if command -v xcrun &>/dev/null && [ -n "${APPLE_ID:-}" ]; then
    xcrun notarytool submit "$OUTPUT" \
        --apple-id "$APPLE_ID" \
        --team-id "${APPLE_TEAM_ID:-}" \
        --password "${APPLE_APP_PASSWORD:-}" \
        --wait
    xcrun stapler staple "$OUTPUT"
    echo "  Notarized and stapled"
else
    echo "  WARNING: Not notarized (set APPLE_ID for production)"
fi

echo ""
echo "=== Build complete ==="
echo "Output: $OUTPUT"
ls -lh "$OUTPUT"
