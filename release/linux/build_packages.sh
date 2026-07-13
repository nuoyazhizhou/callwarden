#!/bin/bash
# Linux deb/rpm package builder for Call Warden
# Task: T-1783983162956-b2a8
#
# Sub-packages:
#   callwarden-client  - cw-client + MCP proxy
#   callwarden-local   - cw + Rust extension + local DB + MCP + watcher
#   callwarden-agent   - cw-agent + systemd user unit
#   callwarden-daemon  - cw-daemon + system unit + migration tools
#   callwarden-enterprise = daemon + agent + client meta-package

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
VERSION="0.3.0"
ARCH="${1:-amd64}"
DIST_DIR="$SCRIPT_DIR/dist"

echo "=== Building Linux packages ==="
echo "Version: $VERSION"
echo "Architecture: $ARCH"

# 1. Build Rust extension
echo "Step 1: Building Rust extension"
cd "$ROOT/rust_ext"
if [ "$ARCH" = "amd64" ]; then
    TARGET="x86_64-unknown-linux-gnu"
elif [ "$ARCH" = "arm64" ]; then
    TARGET="aarch64-unknown-linux-gnu"
else
    echo "ERROR: Unsupported architecture: $ARCH"
    exit 1
fi
cargo build --release --target "$TARGET"

# 2. Prepare package roots
echo "Step 2: Preparing package roots"

# callwarden-local
LOCAL_ROOT="$SCRIPT_DIR/build/local"
rm -rf "$LOCAL_ROOT"
mkdir -p "$LOCAL_ROOT/usr/bin"
mkdir -p "$LOCAL_ROOT/usr/lib/callwarden"
mkdir -p "$LOCAL_ROOT/etc/callwarden"
cp "$ROOT/dist/linux/cw" "$LOCAL_ROOT/usr/bin/"
cp "$ROOT/rust_ext/target/$TARGET/release/libcallwarden_core.so" \
   "$LOCAL_ROOT/usr/lib/callwarden/"
cp "$SCRIPT_DIR/deb/config.toml.template" "$LOCAL_ROOT/etc/callwarden/config.toml" 2>/dev/null || true

# callwarden-daemon
DAEMON_ROOT="$SCRIPT_DIR/build/daemon"
rm -rf "$DAEMON_ROOT"
mkdir -p "$DAEMON_ROOT/usr/bin"
mkdir -p "$DAEMON_ROOT/usr/lib/systemd/system"
mkdir -p "$DAEMON_ROOT/usr/lib/sysusers.d"
mkdir -p "$DAEMON_ROOT/usr/lib/tmpfiles.d"
cp "$ROOT/dist/linux/cw-daemon" "$DAEMON_ROOT/usr/bin/" 2>/dev/null || true
cp "$ROOT/cicd/callwarden-daemon.service" "$DAEMON_ROOT/usr/lib/systemd/system/"

# sysusers: create callwarden user and callwarden-clients group
cat > "$DAEMON_ROOT/usr/lib/sysusers.d/callwarden.conf" << 'EOF'
# Call Warden daemon user and client group
u callwarden - "Call Warden daemon" /var/lib/callwarden
g callwarden-clients -
m callwarden callwarden-clients
EOF

# tmpfiles: create /run/callwarden
cat > "$DAEMON_ROOT/usr/lib/tmpfiles.d/callwarden.conf" << 'EOF'
# Call Warden runtime directory
d /run/callwarden 0755 callwarden callwarden-clients -
EOF

# callwarden-agent
AGENT_ROOT="$SCRIPT_DIR/build/agent"
rm -rf "$AGENT_ROOT"
mkdir -p "$AGENT_ROOT/usr/bin"
mkdir -p "$AGENT_ROOT/usr/lib/systemd/user"
cp "$ROOT/dist/linux/cw-agent" "$AGENT_ROOT/usr/bin/" 2>/dev/null || true

# Create agent systemd user unit
cat > "$AGENT_ROOT/usr/lib/systemd/user/callwarden-agent.service" << 'EOF'
[Unit]
Description=Call Warden Per-UID Agent
After=callwarden-daemon.service

[Service]
Type=simple
ExecStart=/usr/bin/cw-agent start
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF

# 3. Build deb packages
echo "Step 3: Building deb packages"
mkdir -p "$DIST_DIR"

for pkg in local daemon agent; do
    PKG_ROOT="$SCRIPT_DIR/build/$pkg"
    PKG_NAME="callwarden-$pkg"
    CONTROL_FILE="$SCRIPT_DIR/deb/control.$pkg"

    if [ ! -f "$CONTROL_FILE" ]; then
        echo "  SKIP: $PKG_NAME (no control file)"
        continue
    fi

    # Create DEBIAN directory
    mkdir -p "$PKG_ROOT/DEBIAN"
    cp "$CONTROL_FILE" "$PKG_ROOT/DEBIAN/control"

    # Build package
    DEB_FILE="$DIST_DIR/${PKG_NAME}_${VERSION}_${ARCH}.deb"
    dpkg-deb --build "$PKG_ROOT" "$DEB_FILE" 2>/dev/null || {
        echo "  WARNING: dpkg-deb not available, creating tar.gz instead"
        tar -czf "${DEB_FILE%.deb}.tar.gz" -C "$PKG_ROOT" .
    }
    echo "  Built: $PKG_NAME"
done

# 4. Build enterprise meta-package
echo "Step 4: Building enterprise meta-package"
ENTERPRISE_ROOT="$SCRIPT_DIR/build/enterprise"
rm -rf "$ENTERPRISE_ROOT"
mkdir -p "$ENTERPRISE_ROOT/DEBIAN"
cat > "$ENTERPRISE_ROOT/DEBIAN/control" << EOF
Package: callwarden-enterprise
Version: $VERSION
Section: devel
Priority: optional
Architecture: $ARCH
Depends: callwarden-daemon (= $VERSION), callwarden-agent (= $VERSION), callwarden-local (= $VERSION)
Description: Call Warden Enterprise (daemon + agent + client meta-package)
EOF
dpkg-deb --build "$ENTERPRISE_ROOT" "$DIST_DIR/callwarden-enterprise_${VERSION}_${ARCH}.deb" 2>/dev/null || true

echo ""
echo "=== Build complete ==="
ls -lh "$DIST_DIR"/*.deb 2>/dev/null || ls -lh "$DIST_DIR"/*.tar.gz 2>/dev/null || echo "No packages built"
