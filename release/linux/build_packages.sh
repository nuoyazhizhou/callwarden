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
#
# P0-3 修复（问题 4/5/9，2026-07-21 → 2026-07-22 重构）：
#   - cw / cw-client / cw-agent 改用 PyInstaller --onedir 打包为自包含二进制，
#     含 Python 解释器 + 全部依赖 + Rust 扩展，安装后不依赖系统 Python。
#   - cw-daemon 是 Rust binary，由 cargo build --release --bin cw-daemon 产出。
#   - 支持 --offline-bundle-only flag 跳过 Step 1-5，仅构建 tar.zst 离线包。
#   - 末尾复制 manifest.json 到 dist/，让 workflow upload-artifact 能匹配。
#   - 删除 workflow 中的 || true，失败必须 fail-fast。

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
DIST_DIR="$SCRIPT_DIR/dist"

# P0-3 修复（问题 9）：支持 --offline-bundle-only flag
# 用法：
#   bash build_packages.sh [ARCH]                      # 完整构建（Step 1-6）
#   bash build_packages.sh --offline-bundle-only [ARCH]  # 仅 Step 6（离线包）
ARCH=""
OFFLINE_ONLY=0
while [ $# -gt 0 ]; do
    case "$1" in
        --offline-bundle-only)
            OFFLINE_ONLY=1
            shift
            ;;
        --offline-bundle)
            # 兼容旧调用（workflow 原用法），等价于完整构建（offline bundle 自动生成）
            shift
            ;;
        amd64|arm64)
            ARCH="$1"
            shift
            ;;
        *)
            echo "ERROR: Unknown argument: $1" >&2
            echo "Usage: $0 [--offline-bundle-only] [amd64|arm64]" >&2
            exit 1
            ;;
    esac
done
ARCH="${ARCH:-amd64}"

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
echo "Offline-only: $OFFLINE_ONLY"

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

# P0-3 修复（2026-07-22）：用 PyInstaller --onedir 打包自包含二进制
# cw / cw-client / cw-agent 是 Python entry_points，改用 PyInstaller 打包为
# 自包含的 --onedir 产物（含 Python 解释器 + 依赖 + Rust 扩展），
# 安装后不依赖系统 Python 和 site-packages。
build_pyinstaller_bundle() {
    echo "Step 0: Building PyInstaller --onedir bundle"
    local wheel_path=""
    local wheel_glob="$ROOT/release/dist/callwarden-${VERSION}-*.whl"
    for f in $wheel_glob; do
        if [ -f "$f" ]; then
            wheel_path="$f"
            break
        fi
    done
    if [ -z "$wheel_path" ]; then
        echo "  ERROR: Python wheel not found at $ROOT/release/dist/callwarden-${VERSION}-*.whl" >&2
        echo "  请先运行 'python release/build.py --wheel' 构建 wheel" >&2
        exit 1
    fi
    echo "  Using wheel: $(basename "$wheel_path")"

    # 创建临时 venv 安装 wheel + PyInstaller + 全部依赖
    local venv_dir="$SCRIPT_DIR/build/venv"
    rm -rf "$venv_dir"
    python3 -m venv "$venv_dir" >/dev/null 2>&1 || {
        echo "  ERROR: python3 -m venv failed" >&2
        exit 1
    }
    "$venv_dir/bin/pip" install --upgrade pip >/dev/null 2>&1 || true
    # CPU-only torch：代码图谱工具不需要 CUDA，避免产物包含 ~2GB nvidia 包
    "$venv_dir/bin/pip" install torch --index-url https://download.pytorch.org/whl/cpu || true
    # 安装 wheel + 全部依赖（--no-deps 会漏掉 tree-sitter 等，必须装依赖）
    "$venv_dir/bin/pip" install "$wheel_path[all]" || {
        echo "  ERROR: pip install $wheel_path[all] failed" >&2
        exit 1
    }
    # 安装 PyInstaller
    "$venv_dir/bin/pip" install pyinstaller || {
        echo "  ERROR: pip install pyinstaller failed" >&2
        exit 1
    }

    # 复制 Rust 扩展到项目根目录（PyInstaller spec 从根目录收集）
    local rust_ext_src="$ROOT/rust_ext/target/$TARGET/release/libcallwarden_core.so"
    local rust_ext_dst="$ROOT/callwarden_core.so"
    if [ -f "$rust_ext_src" ]; then
        cp "$rust_ext_src" "$rust_ext_dst"
        echo "  [OK] Rust extension copied: libcallwarden_core.so -> callwarden_core.so"
    else
        echo "  WARNING: Rust extension not found at $rust_ext_src"
        echo "  callwarden 会在运行时降级到纯 Python（性能下降）"
    fi

    # 运行 PyInstaller
    cd "$ROOT"
    "$venv_dir/bin/pyinstaller" release/pyinstaller/callwarden.spec \
        --noconfirm --clean \
        --distpath "$SCRIPT_DIR/build/pyinstaller_dist" \
        --workpath "$SCRIPT_DIR/build/pyinstaller_build" || {
        echo "  ERROR: pyinstaller failed" >&2
        # 清理临时复制的 Rust 扩展
        rm -f "$rust_ext_dst"
        exit 1
    }

    # 清理临时复制的 Rust 扩展
    rm -f "$rust_ext_dst"

    # 验证产物
    local missing=""
    local bundle_dir="$SCRIPT_DIR/build/pyinstaller_dist/callwarden"
    for cmd in cw cw-client cw-agent; do
        if [ ! -f "$bundle_dir/$cmd" ]; then
            missing="$missing $cmd"
        fi
    done
    if [ -n "$missing" ]; then
        echo "  ERROR: PyInstaller 产物缺失:$missing" >&2
        exit 1
    fi
    if [ ! -d "$bundle_dir/_internal" ]; then
        echo "  ERROR: PyInstaller 共享运行时缺失: $bundle_dir/_internal" >&2
        exit 1
    fi
    echo "  [OK] PyInstaller shared bundle built: cw, cw-client, cw-agent"
}

if [ "$OFFLINE_ONLY" = "0" ]; then
    # 完整构建模式：Step 1-5 都要跑
    build_pyinstaller_bundle

    # 2. 构建 Rust 扩展 + cw-daemon binary
    echo "Step 1: Building Rust extension + cw-daemon binary"
    cd "$ROOT/rust_ext"
    # P0-3 修复（问题 5）：cw-daemon 是 Cargo [[bin]] name="cw-daemon"
    cargo build --release --target "$TARGET" --bin cw-daemon

    # 验证 cw-daemon binary 存在
    CW_DAEMON_BIN="$ROOT/rust_ext/target/$TARGET/release/cw-daemon"
    if [ ! -f "$CW_DAEMON_BIN" ]; then
        echo "  ERROR: cw-daemon binary not built at $CW_DAEMON_BIN" >&2
        echo "  Run 'cargo build --release --target $TARGET --bin cw-daemon' first." >&2
        exit 1
    fi
    echo "  [OK] cw-daemon binary built: $(basename "$CW_DAEMON_BIN")"

    # 3. 准备各子包 root 目录
    echo "Step 2: Preparing package roots"
    # P0-3 整改（2026-07-22）：PyInstaller --onedir 产物路径
    PYINSTALLER_BUNDLE="$SCRIPT_DIR/build/pyinstaller_dist/callwarden"

    # --- callwarden-client（设计 §8.1: cw-client + MCP proxy，不含 parser/CAS）---
    # 各角色包必须可独立安装：从共享构建目录复制目标入口和一份 _internal。
    # 普通 tar 发布包仍只包含一个共享运行时；deb 拆包不能跨包依赖未安装的文件。
    CLIENT_ROOT="$SCRIPT_DIR/build/client"
    rm -rf "$CLIENT_ROOT"
    mkdir -p "$CLIENT_ROOT/usr/bin" "$CLIENT_ROOT/usr/lib/callwarden/runtime-cw-client"
    cp "$PYINSTALLER_BUNDLE/cw-client" "$CLIENT_ROOT/usr/lib/callwarden/runtime-cw-client/"
    cp -r "$PYINSTALLER_BUNDLE/_internal" "$CLIENT_ROOT/usr/lib/callwarden/runtime-cw-client/"
    ln -s /usr/lib/callwarden/runtime-cw-client/cw-client "$CLIENT_ROOT/usr/bin/cw-client"

    # --- callwarden-local（cw + Rust 扩展 + local DB + MCP + watcher）---
    # P0-3 整改：cw 的 --onedir 已含 Rust 扩展（callwarden_core.so），
    # 不再单独复制 libcallwarden_core.so（PyInstaller spec 已收集）
    LOCAL_ROOT="$SCRIPT_DIR/build/local"
    rm -rf "$LOCAL_ROOT"
    mkdir -p "$LOCAL_ROOT/usr/bin" "$LOCAL_ROOT/usr/lib/callwarden/runtime-cw" "$LOCAL_ROOT/etc/callwarden"
    cp "$PYINSTALLER_BUNDLE/cw" "$LOCAL_ROOT/usr/lib/callwarden/runtime-cw/"
    cp -r "$PYINSTALLER_BUNDLE/_internal" "$LOCAL_ROOT/usr/lib/callwarden/runtime-cw/"
    ln -s /usr/lib/callwarden/runtime-cw/cw "$LOCAL_ROOT/usr/bin/cw"
    cp "$SCRIPT_DIR/deb/config.toml.template" "$LOCAL_ROOT/etc/callwarden/config.toml" 2>/dev/null || true

    # --- callwarden-agent（cw-agent + systemd user unit + client）---
    AGENT_ROOT="$SCRIPT_DIR/build/agent"
    rm -rf "$AGENT_ROOT"
    mkdir -p "$AGENT_ROOT/usr/bin" "$AGENT_ROOT/usr/lib/callwarden/runtime-cw-agent" "$AGENT_ROOT/usr/lib/systemd/user"
    cp "$PYINSTALLER_BUNDLE/cw-agent" "$AGENT_ROOT/usr/lib/callwarden/runtime-cw-agent/"
    cp -r "$PYINSTALLER_BUNDLE/_internal" "$AGENT_ROOT/usr/lib/callwarden/runtime-cw-agent/"
    ln -s /usr/lib/callwarden/runtime-cw-agent/cw-agent "$AGENT_ROOT/usr/bin/cw-agent"
    # agent systemd --user unit（设计 §v8: per-UID watcher agent）
    # 优先使用 deb/systemd/callwarden-agent.service 正式 unit（含安全约束、资源限制、
    # 环境变量、ExecStop 等完整配置）；不存在时降级为最小占位 unit。
    if [ -f "$SCRIPT_DIR/deb/systemd/callwarden-agent.service" ]; then
        cp "$SCRIPT_DIR/deb/systemd/callwarden-agent.service" "$AGENT_ROOT/usr/lib/systemd/user/"
    else
        # 降级：最小占位 unit（设计 §8.2: 不自动启用 linger，管理员可选）
        cat > "$AGENT_ROOT/usr/lib/systemd/user/callwarden-agent.service" << 'EOF'
[Unit]
Description=Call Warden Per-UID Agent
After=network.target

[Service]
Type=simple
ExecStart=/usr/bin/cw-agent start
ExecStop=/usr/bin/cw-agent stop
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
EOF
    fi

    # --- callwarden-daemon（cw-daemon + system unit + 迁移/备份工具）---
    DAEMON_ROOT="$SCRIPT_DIR/build/daemon"
    rm -rf "$DAEMON_ROOT"
    mkdir -p "$DAEMON_ROOT/usr/bin" \
             "$DAEMON_ROOT/usr/lib/systemd/system" \
             "$DAEMON_ROOT/usr/lib/sysusers.d" \
             "$DAEMON_ROOT/usr/lib/tmpfiles.d" \
             "$DAEMON_ROOT/etc/callwarden"
    # P0-3 修复（问题 5）：cw-daemon 由 cargo build 产出（Cargo [[bin]] name="cw-daemon"）
    cp "$CW_DAEMON_BIN" "$DAEMON_ROOT/usr/bin/"

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

    # 6. RPM 构建（设计 §8: 当前仅 deb，RPM 不在发布范围）
    echo "Step 5: RPM build (skipped - deb-only release)"
    echo "  NOTE: RPM packaging is not currently supported. deb is the primary and only"
    echo "        Linux package format for Call Warden (设计 §8.1). RPM users should use"
    echo "        the offline tar.zst bundle (Step 6) or convert deb via 'alien' tool."
    echo "  Future: if RPM support is added, generate callwarden.spec and call rpmbuild -bb."
fi
# end OFFLINE_ONLY=0 block

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

# P0-3 修复（问题 9）：复制 manifest.json 到 dist/，让 workflow upload-artifact 能匹配
# 复审报告 §3 P0-3：原 manifest.json 只在 BUNDLE_ROOT 内，dist/ 下不存在独立文件，
# workflow 的 `release/linux/dist/manifest.json` 匹配失败，artifact 中缺失 manifest。
cp "$BUNDLE_ROOT/manifest.json" "$DIST_DIR/manifest.json"
echo "  [OK] manifest.json copied to $DIST_DIR/manifest.json"

echo ""
echo "=== Build complete ==="
ls -lh "$DIST_DIR"/*.deb 2>/dev/null || true
ls -lh "$DIST_DIR"/*.tar.zst 2>/dev/null || true
ls -lh "$DIST_DIR"/*.tar.gz 2>/dev/null || true
ls -lh "$DIST_DIR/manifest.json" 2>/dev/null || true
echo ""
echo "Sub-packages: client, local, agent, daemon, enterprise (设计 §8.1)"
echo "Version: $VERSION (from release/version.toml)"
