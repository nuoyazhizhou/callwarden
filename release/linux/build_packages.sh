#!/bin/bash
# Linux deb/rpm package builder for Call Warden
# Task: T-1783983162956-b2a8
#
# 5 子包（设计 §8.1 / §4.4）:
#   callwarden-client     - cw-client + MCP proxy，不含 parser/CAS
#   callwarden-local      - cw + Rust 扩展 + local DB + MCP + watcher
#   callwarden-agent      - cw-agent + systemd user unit + client
#   callwarden-daemon     - cw-daemon + system unit + 迁移/备份工具
#   callwarden-enterprise - 元包 = daemon + agent + client
#
# 设计 §8: deb 优先 / RPM 等同；离线场景提供 tar.zst（含包+repo metadata+SBOM+manifest+安装脚本）

set -euo pipefail

# 0. 平台约束（设计 §11）：daemon/agent 角色在非 Linux 上 fail-closed
# Linux 安装包只能在 Linux 上构建，避免在 Windows/macOS 上误产生产物。
if [ "$(uname -s)" != "Linux" ]; then
    echo "ERROR: Linux packages can only be built on Linux (current: $(uname -s))" >&2
    echo "daemon/agent roles are fail-closed on non-Linux platforms." >&2
    exit 1
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ARCH="${1:-amd64}"
DIST_DIR="$SCRIPT_DIR/dist"

# 1. 从 release/version.toml 读取版本（唯一真相源，设计 version.toml 头部注释）
#    [product] 段下的 version 字段
VERSION="$(awk -F'"' '/^\[product\]/{f=1} f&&/^version[[:space:]]*=/{print $2; exit}' "$ROOT/release/version.toml")"
if [ -z "${VERSION:-}" ]; then
    echo "ERROR: failed to read version from $ROOT/release/version.toml" >&2
    exit 1
fi

echo "=== Building Linux packages ==="
echo "Version: $VERSION (from release/version.toml)"
echo "Architecture: $ARCH"

# 架构映射：Debian arch -> Rust target
case "$ARCH" in
    amd64) TARGET="x86_64-unknown-linux-gnu" ;;
    arm64) TARGET="aarch64-unknown-linux-gnu" ;;
    *)
        echo "ERROR: Unsupported architecture: $ARCH" >&2
        exit 1
        ;;
esac

# 占位符替换：将 control / maintainer 脚本中的 __VERSION__ / __ARCH__ 注入实际值
substitute() {
    local src="$1" dst="$2"
    sed -e "s/__VERSION__/$VERSION/g" -e "s/__ARCH__/$ARCH/g" "$src" > "$dst"
}

# 2. 构建 Rust 扩展
echo "Step 1: Building Rust extension"
cd "$ROOT/rust_ext"
cargo build --release --target "$TARGET"

# 3. 准备各子包 root 目录
echo "Step 2: Preparing package roots"

# --- callwarden-client（设计 §8.1: cw-client + MCP proxy，不含 parser/CAS）---
CLIENT_ROOT="$SCRIPT_DIR/build/client"
rm -rf "$CLIENT_ROOT"
mkdir -p "$CLIENT_ROOT/usr/bin"
cp "$ROOT/dist/linux/cw-client" "$CLIENT_ROOT/usr/bin/" 2>/dev/null || \
    echo "  NOTE: cw-client binary not found (placeholder root created)"

# --- callwarden-local（cw + Rust 扩展 + local DB + MCP + watcher）---
LOCAL_ROOT="$SCRIPT_DIR/build/local"
rm -rf "$LOCAL_ROOT"
mkdir -p "$LOCAL_ROOT/usr/bin" "$LOCAL_ROOT/usr/lib/callwarden" "$LOCAL_ROOT/etc/callwarden"
cp "$ROOT/dist/linux/cw" "$LOCAL_ROOT/usr/bin/" 2>/dev/null || \
    echo "  NOTE: cw binary not found (placeholder root created)"
cp "$ROOT/rust_ext/target/$TARGET/release/libcallwarden_core.so" \
   "$LOCAL_ROOT/usr/lib/callwarden/" 2>/dev/null || \
    echo "  NOTE: libcallwarden_core.so not found (placeholder root created)"
cp "$SCRIPT_DIR/deb/config.toml.template" "$LOCAL_ROOT/etc/callwarden/config.toml" 2>/dev/null || true

# --- callwarden-agent（cw-agent + systemd user unit + client）---
AGENT_ROOT="$SCRIPT_DIR/build/agent"
rm -rf "$AGENT_ROOT"
mkdir -p "$AGENT_ROOT/usr/bin" "$AGENT_ROOT/usr/lib/systemd/user"
cp "$ROOT/dist/linux/cw-agent" "$AGENT_ROOT/usr/bin/" 2>/dev/null || \
    echo "  NOTE: cw-agent binary not found (placeholder root created)"
# agent systemd user unit（设计 §8.2: 不自动启用 linger，管理员可选）
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

# --- callwarden-daemon（cw-daemon + system unit + 迁移/备份工具）---
DAEMON_ROOT="$SCRIPT_DIR/build/daemon"
rm -rf "$DAEMON_ROOT"
mkdir -p "$DAEMON_ROOT/usr/bin" \
         "$DAEMON_ROOT/usr/lib/systemd/system" \
         "$DAEMON_ROOT/usr/lib/sysusers.d" \
         "$DAEMON_ROOT/usr/lib/tmpfiles.d" \
         "$DAEMON_ROOT/etc/callwarden"
cp "$ROOT/dist/linux/cw-daemon" "$DAEMON_ROOT/usr/bin/" 2>/dev/null || \
    echo "  NOTE: cw-daemon binary not found (placeholder root created)"

# systemd unit（优先使用 deb/systemd/ 占位文件，含 ExecStartPre schema 检查；设计 §8.2）
if [ -f "$SCRIPT_DIR/deb/systemd/callwarden-daemon.service" ]; then
    cp "$SCRIPT_DIR/deb/systemd/callwarden-daemon.service" "$DAEMON_ROOT/usr/lib/systemd/system/"
else
    # 降级：使用 cicd 目录的 unit（无 ExecStartPre schema 检查）
    cp "$ROOT/cicd/callwarden-daemon.service" "$DAEMON_ROOT/usr/lib/systemd/system/"
fi

# sysusers.d：创建 callwarden 用户和 callwarden-clients 组（设计 §8.2）
if [ -f "$SCRIPT_DIR/deb/sysusers.d/callwarden.conf" ]; then
    cp "$SCRIPT_DIR/deb/sysusers.d/callwarden.conf" "$DAEMON_ROOT/usr/lib/sysusers.d/"
else
    cat > "$DAEMON_ROOT/usr/lib/sysusers.d/callwarden.conf" << 'EOF'
u callwarden - "Call Warden daemon" /var/lib/callwarden /usr/sbin/nologin
g callwarden-clients -
m callwarden callwarden-clients
EOF
fi

# tmpfiles.d：创建 /run/callwarden（设计 §8.2: socket 0660）
if [ -f "$SCRIPT_DIR/deb/tmpfiles.d/callwarden.conf" ]; then
    cp "$SCRIPT_DIR/deb/tmpfiles.d/callwarden.conf" "$DAEMON_ROOT/usr/lib/tmpfiles.d/"
else
    cat > "$DAEMON_ROOT/usr/lib/tmpfiles.d/callwarden.conf" << 'EOF'
d /run/callwarden 0755 callwarden callwarden-clients -
EOF
fi

# 4. 构建 deb 包（5 子包：client / local / agent / daemon）
echo "Step 3: Building deb packages"
mkdir -p "$DIST_DIR"

# 构建单个 deb 包：注入 control + maintainer scripts，调用 dpkg-deb
build_deb() {
    local pkg="$1"
    local pkg_root="$SCRIPT_DIR/build/$pkg"
    local pkg_name="callwarden-$pkg"
    local control_src="$SCRIPT_DIR/deb/control.$pkg"

    if [ ! -f "$control_src" ]; then
        echo "  SKIP: $pkg_name (no control file)"
        return 0
    fi

    # DEBIAN/control（注入版本/架构）
    mkdir -p "$pkg_root/DEBIAN"
    substitute "$control_src" "$pkg_root/DEBIAN/control"

    # maintainer scripts（preinst/postinst/prerm/postrm，存在则注入）
    local script
    for script in preinst postinst prerm postrm; do
        local ms="$SCRIPT_DIR/deb/$pkg.$script"
        if [ -f "$ms" ]; then
            substitute "$ms" "$pkg_root/DEBIAN/$script"
            chmod 0755 "$pkg_root/DEBIAN/$script"
        fi
    done

    local deb_file="$DIST_DIR/${pkg_name}_${VERSION}_${ARCH}.deb"
    if command -v dpkg-deb >/dev/null 2>&1; then
        dpkg-deb --build "$pkg_root" "$deb_file"
        echo "  Built: ${pkg_name}_${VERSION}_${ARCH}.deb"
    else
        # 无 dpkg-deb 时降级为 tar.gz 占位
        echo "  WARNING: dpkg-deb not available, creating tar.gz placeholder for $pkg_name"
        tar -czf "${deb_file%.deb}.tar.gz" -C "$pkg_root" .
    fi
}

for pkg in client local agent daemon; do
    build_deb "$pkg"
done

# 5. 构建 enterprise 元包（设计 §8.1: daemon + agent + client）
echo "Step 4: Building enterprise meta-package"
ENTERPRISE_ROOT="$SCRIPT_DIR/build/enterprise"
rm -rf "$ENTERPRISE_ROOT"
mkdir -p "$ENTERPRISE_ROOT/DEBIAN"
substitute "$SCRIPT_DIR/deb/control.enterprise" "$ENTERPRISE_ROOT/DEBIAN/control"
# enterprise 元包 postinst
if [ -f "$SCRIPT_DIR/deb/enterprise.postinst" ]; then
    substitute "$SCRIPT_DIR/deb/enterprise.postinst" "$ENTERPRISE_ROOT/DEBIAN/postinst"
    chmod 0755 "$ENTERPRISE_ROOT/DEBIAN/postinst"
fi
ENTERPRISE_DEB="$DIST_DIR/callwarden-enterprise_${VERSION}_${ARCH}.deb"
if command -v dpkg-deb >/dev/null 2>&1; then
    dpkg-deb --build "$ENTERPRISE_ROOT" "$ENTERPRISE_DEB"
    echo "  Built: callwarden-enterprise_${VERSION}_${ARCH}.deb"
else
    tar -czf "${ENTERPRISE_DEB%.deb}.tar.gz" -C "$ENTERPRISE_ROOT" .
fi

# 6. RPM 构建（设计 §8: deb 优先 / RPM 等同）
echo "Step 5: RPM build (equivalent capability)"
if command -v rpmbuild >/dev/null 2>&1; then
    echo "  rpmbuild available - RPM 提供与 deb 等同能力"
    # TODO: 生成 callwarden.spec 并调用 rpmbuild -bb
    # 当前以 deb 为第一优先，RPM spec 待补全以提供等同能力（设计 §8.1）
    echo "  NOTE: RPM spec not yet implemented; deb is the primary format."
else
    echo "  rpmbuild not available - skipping RPM (deb is primary, 设计 §8.1)"
fi

# 7. 离线 tar.zst bundle（设计 §8.1: 含包 + repo metadata + SBOM + manifest + 安装脚本）
echo "Step 6: Building offline tar.zst bundle"
BUNDLE_DIR="$SCRIPT_DIR/build/offline-bundle"
BUNDLE_NAME="callwarden-offline_${VERSION}_${ARCH}"
BUNDLE_ROOT="$BUNDLE_DIR/$BUNDLE_NAME"
rm -rf "$BUNDLE_DIR"
mkdir -p "$BUNDLE_ROOT/packages" "$BUNDLE_ROOT/repo" "$BUNDLE_ROOT/sbom" "$BUNDLE_ROOT/scripts"

# 复制所有构建产物（deb 或降级 tar.gz）到 packages/
cp "$DIST_DIR"/*.deb "$BUNDLE_ROOT/packages/" 2>/dev/null || true
cp "$DIST_DIR"/*.tar.gz "$BUNDLE_ROOT/packages/" 2>/dev/null || true

# repo metadata（dpkg-scanpackages 生成 Packages 索引，便于 apt 离线安装）
if command -v dpkg-scanpackages >/dev/null 2>&1; then
    (cd "$BUNDLE_ROOT/packages" && dpkg-scanpackages . /dev/null > "$BUNDLE_ROOT/repo/Packages")
else
    echo "# Packages index placeholder - run: dpkg-scanpackages . /dev/null > Packages" \
        > "$BUNDLE_ROOT/repo/Packages"
fi

# SBOM（SPDX 2.3 占位）
cat > "$BUNDLE_ROOT/sbom/sbom.spdx.json" << EOF
{
  "spdxVersion": "SPDX-2.3",
  "name": "callwarden-offline-bundle",
  "dataLicense": "CC0-1.0",
  "documentNamespace": "https://callwarden.dev/spdx/callwarden-${VERSION}-${ARCH}",
  "creationInfo": {
    "created": "$(date -u +%Y-%m-%dT%H:%M:%SZ)",
    "creators": ["Tool: callwarden build_packages.sh"]
  },
  "packages": [
    {"name": "callwarden-client", "versionInfo": "$VERSION", "downloadLocation": "NOASSERTION"},
    {"name": "callwarden-local", "versionInfo": "$VERSION", "downloadLocation": "NOASSERTION"},
    {"name": "callwarden-agent", "versionInfo": "$VERSION", "downloadLocation": "NOASSERTION"},
    {"name": "callwarden-daemon", "versionInfo": "$VERSION", "downloadLocation": "NOASSERTION"},
    {"name": "callwarden-enterprise", "versionInfo": "$VERSION", "downloadLocation": "NOASSERTION"}
  ]
}
EOF

# manifest
cat > "$BUNDLE_ROOT/manifest.json" << EOF
{
  "product": "callwarden",
  "version": "$VERSION",
  "architecture": "$ARCH",
  "format": "tar.zst",
  "contents": {
    "packages": ["callwarden-client", "callwarden-local", "callwarden-agent", "callwarden-daemon", "callwarden-enterprise"],
    "repo_metadata": "repo/Packages",
    "sbom": "sbom/sbom.spdx.json",
    "install_script": "scripts/install-offline.sh"
  }
}
EOF

# 安装脚本（优先使用 deb/offline/install-offline.sh，否则生成占位）
if [ -f "$SCRIPT_DIR/deb/offline/install-offline.sh" ]; then
    cp "$SCRIPT_DIR/deb/offline/install-offline.sh" "$BUNDLE_ROOT/scripts/"
else
    cat > "$BUNDLE_ROOT/scripts/install-offline.sh" << 'EOF'
#!/bin/bash
# 离线安装脚本（占位）
set -euo pipefail
DIR="$(cd "$(dirname "$0")/.." && pwd)"
echo "Installing Call Warden packages from $DIR/packages"
for deb in "$DIR"/packages/*.deb; do
    [ -f "$deb" ] || continue
    dpkg -i "$deb" || apt-get install -f -y
done
echo "Done."
EOF
fi
chmod 0755 "$BUNDLE_ROOT/scripts/install-offline.sh"

# 打包为 tar.zst（优先 tar --zstd，降级 zstd 管道，再降级 tar.gz）
if command -v tar >/dev/null 2>&1 && tar --help 2>&1 | grep -q -- '--zstd'; then
    (cd "$BUNDLE_DIR" && tar --zstd -cf "$DIST_DIR/${BUNDLE_NAME}.tar.zst" "$BUNDLE_NAME")
    echo "  Built: ${BUNDLE_NAME}.tar.zst"
elif command -v zstd >/dev/null 2>&1; then
    (cd "$BUNDLE_DIR" && tar -cf - "$BUNDLE_NAME" | zstd -o "$DIST_DIR/${BUNDLE_NAME}.tar.zst")
    echo "  Built: ${BUNDLE_NAME}.tar.zst (via zstd pipe)"
else
    echo "  WARNING: zstd not available, creating tar.gz fallback"
    (cd "$BUNDLE_DIR" && tar -czf "$DIST_DIR/${BUNDLE_NAME}.tar.gz" "$BUNDLE_NAME")
fi

echo ""
echo "=== Build complete ==="
ls -lh "$DIST_DIR"/*.deb 2>/dev/null || true
ls -lh "$DIST_DIR"/*.tar.zst 2>/dev/null || true
ls -lh "$DIST_DIR"/*.tar.gz 2>/dev/null || true
echo ""
echo "Sub-packages: client, local, agent, daemon, enterprise (设计 §8.1)"
echo "Version: $VERSION (from release/version.toml)"
